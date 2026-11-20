"""New York DEC — annual production by well (2001+, plus 1985-2000 file) and the full well list with lat/lon, from data.ny.gov.

Datasets (Socrata, CSV export):
  mxea-iw3u  Oil and Gas Annual Production: Beginning 2001   (one row per well-year; oil, gas, water, months in production)
  qcf2-zajk  Oil and Gas Annual Production: 1985-2000        (gas and water only)
  szye-wmt3  Oil, Gas, & Other Regulated Wells: Beginning 1860 (well list, surface + bottom-hole lat/lon)
Annual rows carry period_months = 12 (January 1 of the year) and are spread over months in well_monthly (a_split),
like Pennsylvania's conventional wells. Confidentiality lags one year; the current year's file lands each July.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from ..schema import api_digits
from .base import Frame, RunContext, Source

BASE = "https://data.ny.gov/api/v3/views/{id}/export.csv"
FILES = {"prod_2001.csv": "mxea-iw3u", "prod_1985.csv": "qcf2-zajk", "wells.csv": "szye-wmt3"}


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype("string").str.replace(",", "", regex=False), errors="coerce")


class NewYork(Source):
    name = "ny"
    title = "New York DEC annual well production + well list"
    state = "NY"
    url = "https://dec.ny.gov/environmental-protection/oil-gas/wells-data-geographical-information/downloadable-production-data"
    tables = ("production_monthly", "wells")
    cadence = "annual (July)"
    granularity = "well (annual totals)"
    history = "1985 (gas), 2001 (oil)"
    has_latlon = True
    notes = "Annual rows (period_months=12); spread over months in well_monthly as a_split."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for name, sid in FILES.items():
            f = download(BASE.format(id=sid), self.raw / name, sess=ctx.sess, force=ctx.force, max_age_hours=24 * 14)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        frames = []
        for name in ("prod_2001.csv", "prod_1985.csv"):
            p = self.raw / name
            if not p.exists():
                continue
            raw = pd.read_csv(p, dtype=str, keep_default_na=False)
            raw.columns = [c.strip() for c in raw.columns]
            col = lambda *cands: next((c for c in cands if c in raw.columns), None)
            api14 = api_digits(raw[col("API Well Number")]).str.ljust(14, "0")
            yr = _num(raw[col("Reporting Year")])
            months = _num(raw[col("Months in Production")])
            frames.append(pd.DataFrame({
                "entity_id": api14, "entity_type": "well", "api14": api14, "api10": api14.str[:10], "state": "NY",
                "period": pd.to_datetime(dict(year=yr, month=1, day=1), errors="coerce"), "period_months": 12.0,
                "oil_bbl": _num(raw[col("Oil Produced, bbl")]) if col("Oil Produced, bbl") else pd.NA,
                "gas_mcf": _num(raw[col("Gas Produced, Mcf", "Gas Produced, MCF")]),
                "water_bbl": _num(raw[col("Water Produced, bbl")]),
                "days_on": (months * 30.44).where(months.notna()),
                "operator": raw[col("Company Name")], "well_name": raw[col("Well Name")], "county": raw[col("County")],
                "field": raw[col("Production Field", "Production Field Name")], "formation": raw[col("Producing Formation")],
                "extra": [json.dumps({"months_producing": None if pd.isna(m) else int(m), "well_type": t, "status": s}) for m, t, s in zip(months, raw[col("Well Type Code", "Well Type")], raw[col("Well Status Code", "Well Status")])],
            }))
        df = pd.concat(frames, ignore_index=True)
        df = df[df.period.notna() & df.api10.notna()].drop_duplicates(["entity_id", "period"], keep="first")  # 2001+ file wins over 1985-2000 on overlap
        self.log.info("NY production: %s well-years, %s wells, %s..%s", f"{len(df):,}", f"{df.api10.nunique():,}", df.period.min().year, df.period.max().year)
        for yr_, part in df.groupby(df.period.dt.year):
            yield "production_monthly", str(yr_), part

        w = pd.read_csv(self.raw / "wells.csv", dtype=str, keep_default_na=False)
        w.columns = [c.strip() for c in w.columns]
        api14 = api_digits(w["API Well Number"]).str.ljust(14, "0")
        dt = lambda c: pd.to_datetime(w[c], errors="coerce", format="mixed") if c in w else pd.NaT
        slant = w["Slant"].astype("string").str.upper().str[:1].map({"V": "V", "H": "H", "D": "D"}) if "Slant" in w else pd.NA
        out = pd.DataFrame({
            "well_id": api14, "api14": api14, "api10": api14.str[:10], "state": "NY",
            "well_name": w["Well Name"], "operator": w["Company Name"], "county": w["County"], "field": w["Producing Field"], "formation": w["Producing Formation"],
            "lat": _num(w["Surface Latitude"]), "lon": _num(w["Surface Longitude"]), "crs_note": "NAD83 (NYSDEC); surface location",
            "well_type": w["Well Type"], "status": w["Well Status"], "trajectory": slant,
            "spud_date": dt("Date Spudded"), "completion_date": dt("Date Well Completed"),
            "td_ft": _num(w["True Vertical Depth"]) if "True Vertical Depth" in w else pd.NA,
            "extra": [json.dumps({"town": t, "plugged": None if pd.isna(p) else str(p.date()), "bh_lat": None if pd.isna(a) else float(a), "bh_lon": None if pd.isna(o) else float(o), "region": r})
                      for t, p, a, o, r in zip(w["Town"], dt("Date Well Plugged"), _num(w["Bottom Hole Latitude"]), _num(w["Bottom Hole Longitude"]), w["Region"])],
        })
        out = out[out.api10.notna()].drop_duplicates("api10")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        self.log.info("NY wells: %s, %s with coordinates", f"{len(out):,}", f"{out.lat.notna().sum():,}")
        yield "wells", "data", out
