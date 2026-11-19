"""Wyoming WOGCC — statewide production by well per year (1978+), wide monthly layout with lat/lon.

https://pipeline.wyo.gov/RAllcountyprodbyyear.cfm?nYr=YYYY returns a tab-separated
file (served as .xls). One row per well (x formation) per year with Jan..Dec oil/gas/water/days.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

URL = "https://pipeline.wyo.gov/RAllcountyprodbyyear.cfm?nYr={y}"
START = 1978
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


class Wyoming(Source):
    name = "wy"
    title = "Wyoming WOGCC production by well (annual files, monthly columns)"
    state = "WY"
    url = "https://pipeline.wyo.gov/"
    tables = ("production_monthly", "wells")
    cadence = "live query; recent years re-pulled weekly"
    granularity = "well_formation"
    history = "1978-01"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        y0 = date.today().year
        start = max(START, y0 - ctx.limit_years) if ctx.limit_years else START
        for y in range(start, y0 + 1):
            f = download(URL.format(y=y), self.raw / f"Production-{y}.tsv", sess=ctx.sess, force=ctx.force,
                         immutable=y < y0 - 2, max_age_hours=24 * 7 if y < y0 else 20)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("Production-*.tsv")) if changed is None else sorted(changed)
        for p in files:
            df = self._parse(p)
            if not df.empty:
                yield "production_monthly", p.stem, df
        yield "wells", "data", self._wells()

    @staticmethod
    def _parse(path: Path) -> pd.DataFrame:
        text = path.read_bytes().decode("latin-1")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        raw = pd.read_csv(io.StringIO(text), sep="\t", dtype=str, quoting=csv.QUOTE_NONE, on_bad_lines="skip", engine="python")
        raw.columns = [c.strip().lower() for c in raw.columns]
        if "apino" not in raw.columns:
            return pd.DataFrame()
        apino = raw["apino"].astype("string").str.replace(r"\D", "", regex=True)
        apino = apino.where(apino.str.len() > 0)
        api10 = "49" + apino.str.zfill(8).str[-8:]
        formation = raw["formation"].astype("string").fillna("") if "formation" in raw.columns else ""
        yr = pd.to_numeric(raw["yr"], errors="coerce") if "yr" in raw.columns else int(re.search(r"(\d{4})", path.stem).group(1))
        rows = []
        for i, m in enumerate(MONTHS, start=1):
            oil, gas, wtr, days = (raw.get(f"{m}_oil"), raw.get(f"{m}_gas"), raw.get(f"{m}_water"), raw.get(f"{m}_days"))
            if oil is None:
                continue
            op = raw.get(f"op{i:02d}")
            rows.append(pd.DataFrame({
                "entity_id": api10 + ":" + formation, "entity_type": "well_formation", "api14": api10 + "0000", "api10": api10, "state": "WY",
                "period": pd.to_datetime(dict(year=yr, month=i, day=1), errors="coerce"),
                "oil_bbl": pd.to_numeric(oil, errors="coerce"), "gas_mcf": pd.to_numeric(gas, errors="coerce"),
                "water_bbl": pd.to_numeric(wtr, errors="coerce"), "days_on": pd.to_numeric(days, errors="coerce"),
                "operator": op if op is not None else raw.get("company"), "well_name": raw.get("wellname"), "county": raw.get("countyname"),
                "field": raw.get("field_name"), "formation": formation,
                "_lat": pd.to_numeric(raw.get("lat"), errors="coerce"), "_lon": pd.to_numeric(raw.get("lon"), errors="coerce"),
                "_class": raw.get("wellclass"), "_hd": raw.get("horiz_dir"),
            }))
        df = pd.concat(rows, ignore_index=True)
        df = df[df.api10.notna() & df.period.notna() & (df.period <= pd.Timestamp.today().to_period("M").to_timestamp())]
        df = df[(df.oil_bbl.fillna(0) != 0) | (df.gas_mcf.fillna(0) != 0) | (df.water_bbl.fillna(0) != 0) | (df.days_on.fillna(0) != 0)]
        df["extra"] = [json.dumps({"lat": None if pd.isna(a) else a, "lon": None if pd.isna(b) else b, "well_class": c, "horiz_dir": h})
                       for a, b, c, h in zip(df._lat, df._lon, df._class, df._hd)]
        return df.drop(columns=["_lat", "_lon", "_class", "_hd"]).drop_duplicates(["entity_id", "period"], keep="last")

    def _wells(self) -> pd.DataFrame:
        import duckdb
        from .. import storage
        parts = storage.list_partitions("production_monthly", self.name)
        if not parts:
            return pd.DataFrame()
        glob = str(storage.partition_dir("production_monthly", self.name) / "*.parquet")
        con = duckdb.connect()
        df = con.execute(f"""
            WITH p AS (SELECT *, json_extract(extra,'$.lat')::DOUBLE AS lat, json_extract(extra,'$.lon')::DOUBLE AS lon,
                              json_extract_string(extra,'$.well_class') AS wc, json_extract_string(extra,'$.horiz_dir') AS hd
                       FROM read_parquet('{glob}'))
            SELECT api10 AS well_id, api10, api14, 'WY' AS state, arg_max(well_name, period) AS well_name, arg_max(operator, period) AS operator,
                   arg_max(county, period) AS county, arg_max(field, period) AS field, arg_max(formation, period) AS formation,
                   arg_max(lat, period) FILTER (WHERE lat <> 0) AS lat, arg_max(lon, period) FILTER (WHERE lon <> 0) AS lon,
                   'as published by WOGCC (NAD83 assumed)' AS crs_note, arg_max(wc, period) AS well_type,
                   CASE WHEN upper(arg_max(hd, period)) LIKE 'H%' THEN 'H' END AS trajectory,
                   min(period) FILTER (WHERE oil_bbl > 0 OR gas_mcf > 0) AS first_prod_date,
                   max(period) FILTER (WHERE oil_bbl > 0 OR gas_mcf > 0) AS last_prod_date, now() AS updated_at,
                   json_object('cum_oil_bbl', sum(oil_bbl), 'cum_gas_mcf', sum(gas_mcf))::VARCHAR AS extra
            FROM p GROUP BY api10, api14
        """).df()
        con.close()
        return df
