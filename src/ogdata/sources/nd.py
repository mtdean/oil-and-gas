"""North Dakota Industrial Commission, Oil & Gas Division — monthly production by well.

Files: https://www.dmr.nd.gov/oilgas/mpr/YYYY_MM.xlsx (sheet 'Oil'), one per month,
available from 2015-05 onward. True well-level (API14), includes surface lat/lon,
oil / water / gas / gas sold / flared / days / runs (oil sold). ND revises recent
months, so we re-check the trailing 14 months and treat older files as immutable.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd

from .. import storage
from ..http import download
from ..schema import api10_from, api14_from
from .base import Frame, RunContext, Source

START = (2015, 5)
URL = "https://www.dmr.nd.gov/oilgas/mpr/{y}_{m:02d}.xlsx"


def _months(start=START, end: date | None = None, limit_years: int | None = None):
    end = end or date.today()
    y, m = start
    if limit_years:
        y = max(y, end.year - limit_years)
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


class NorthDakota(Source):
    name = "nd"
    title = "North Dakota NDIC monthly well production"
    state = "ND"
    url = "https://www.dmr.nd.gov/oilgas/"
    tables = ("production_monthly", "wells")
    cadence = "monthly (~45 days after month end)"
    granularity = "well"
    history = "2015-05"
    has_latlon = True
    notes = "Pre-2015 well history is only in NDIC paid 'premium services'."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        today = date.today()
        for y, m in _months(limit_years=ctx.limit_years):
            dest = self.raw / f"{y}_{m:02d}.xlsx"
            months_old = (today.year - y) * 12 + (today.month - m)
            f = download(URL.format(y=y, m=m), dest, sess=ctx.sess, force=ctx.force,
                         immutable=months_old > 14, max_age_hours=20, allow_404=True)
            if f.status == 404:
                continue
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("????_??.xlsx")) if changed is None else sorted(changed)
        for path in files:
            yield "production_monthly", path.stem, self._parse(path)
        # wells: rebuild from all production partitions (latest attributes per API14)
        yield "wells", "data", self._wells()

    @staticmethod
    def _parse(path: Path) -> pd.DataFrame:
        raw = pd.read_excel(path, sheet_name="Oil")
        raw.columns = [c.strip() for c in raw.columns]
        api = raw["API_WELLNO"].astype("string")
        df = pd.DataFrame({
            "entity_id": api14_from(api), "entity_type": "well",
            "api14": api14_from(api), "api10": api10_from(api), "state": "ND",
            "period": pd.to_datetime(raw["ReportDate"]).dt.to_period("M").dt.to_timestamp(),
            "oil_bbl": raw["Oil"], "gas_mcf": raw["Gas"], "water_bbl": raw["Wtr"], "days_on": raw["Days"],
            "oil_sold_bbl": raw.get("Runs"), "gas_sold_mcf": raw.get("GasSold"), "gas_flared_mcf": raw.get("Flared"),
            "operator": raw["Company"], "well_name": raw["WellName"], "county": raw["County"],
            "field": raw["FieldName"], "formation": raw["Pool"],
        })
        extra_cols = [c for c in ("FileNo", "Quarter", "Section", "Township", "Range", "Lat", "Long") if c in raw.columns]
        ex = raw[extra_cols].copy()
        for c in ("FileNo", "Section", "Township", "Range"):
            if c in ex.columns:
                ex[c] = pd.to_numeric(ex[c], errors="coerce").astype("Int64")
        df["extra"] = ex.to_json(orient="records", lines=True).splitlines()
        return df

    def _wells(self) -> pd.DataFrame:
        parts = storage.list_partitions("production_monthly", self.name)
        if not parts:
            return pd.DataFrame()
        con = duckdb.connect()
        glob = str(storage.partition_dir("production_monthly", self.name) / "*.parquet")
        df = con.execute(f"""
            WITH p AS (SELECT *, json_extract(extra,'$.Lat')::DOUBLE AS lat, json_extract(extra,'$.Long')::DOUBLE AS lon,
                              json_extract_string(extra,'$.FileNo') AS file_no
                       FROM read_parquet('{glob}'))
            SELECT api14 AS well_id, api10, api14, 'ND' AS state,
                   arg_max(well_name, period) AS well_name, arg_max(operator, period) AS operator,
                   arg_max(county, period) AS county, arg_max(field, period) AS field, arg_max(formation, period) AS formation,
                   arg_max(lat, period) AS lat, arg_max(lon, period) AS lon, 'NAD83 as published' AS crs_note,
                   min(period) FILTER (WHERE oil_bbl > 0 OR gas_mcf > 0) AS first_prod_date,
                   max(period) FILTER (WHERE oil_bbl > 0 OR gas_mcf > 0) AS last_prod_date,
                   now() AS updated_at,
                   json_object('ndic_file_no', arg_max(file_no, period), 'months_reported', count(*),
                               'cum_oil_bbl_since_2015', sum(oil_bbl), 'cum_gas_mcf_since_2015', sum(gas_mcf))::VARCHAR AS extra
            FROM p WHERE api14 IS NOT NULL GROUP BY api14, api10
        """).df()
        con.close()
        return df
