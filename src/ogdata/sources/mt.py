"""Montana Board of Oil & Gas — full monthly well production history (1986+) + well list with lat/lon."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

PROD_URL = "https://bogfiles.dnrc.mt.gov/Reporting/Production/Historical/MT_Historical_Production.zip"
WELLS_URL = "https://bogfiles.dnrc.mt.gov/Reporting/Wells/MT_CompleteWellList.zip"


def _read_tab(path: Path, member_contains: str) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if member_contains.lower() in n.lower())
        return pd.read_csv(z.open(name), sep="\t", dtype=str, low_memory=False, encoding_errors="replace")


class Montana(Source):
    name = "mt"
    title = "Montana MBOGC monthly well production + well list"
    state = "MT"
    url = "https://bogwebfiles.dnrc.mt.gov/"
    tables = ("production_monthly", "wells")
    cadence = "monthly (mid-month)"
    granularity = "well_formation"
    history = "1986-01"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for url, name in ((PROD_URL, "MT_Historical_Production.zip"), (WELLS_URL, "MT_CompleteWellList.zip")):
            f = download(url, self.raw / name, sess=ctx.sess, force=ctx.force, max_age_hours=20)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        if changed is None or any(p.name.startswith("MT_Historical") for p in changed):
            raw = _read_tab(self.raw / "MT_Historical_Production.zip", "WellProduction")
            raw.columns = [c.strip() for c in raw.columns]
            api14 = raw["API_WellNo"].astype("string").str.replace(r"\D", "", regex=True).str.ljust(14, "0")
            period = pd.to_datetime(raw["Rpt_Date"], errors="coerce").dt.to_period("M").dt.to_timestamp()
            df = pd.DataFrame({
                "entity_id": api14 + ":" + raw["ST_FMTN_CD"].astype("string").fillna(""), "entity_type": "well_formation",
                "api14": api14, "api10": api14.str[:10], "state": "MT", "period": period,
                "oil_bbl": pd.to_numeric(raw["BBLS_OIL_COND"], errors="coerce"), "gas_mcf": pd.to_numeric(raw["MCF_GAS"], errors="coerce"),
                "water_bbl": pd.to_numeric(raw["BBLS_WTR"], errors="coerce"), "days_on": pd.to_numeric(raw["DAYS_PROD"], errors="coerce"),
                "operator": raw["CoName"], "formation": raw["Formation"],
                "extra": [json.dumps({"lease_unit": lu, "op_no": o, "amended": a}) for lu, o, a in zip(raw["Lease_Unit"], raw["OpNo"], raw["AMND_RPT"])],
            })
            df = df[df.period.notna() & (df.period <= pd.Timestamp.today())]
            df = df.drop_duplicates(["entity_id", "period"], keep="last")
            for yr, part in df.groupby(df.period.dt.year):
                yield "production_monthly", str(yr), part
        if changed is None or any(p.name.startswith("MT_Complete") for p in changed):
            w = _read_tab(self.raw / "MT_CompleteWellList.zip", "WellList")
            w.columns = [c.strip() for c in w.columns]
            api14 = w["API_WellNo"].astype("string").str.replace(r"\D", "", regex=True).str.ljust(14, "0")
            out = pd.DataFrame({
                "well_id": api14, "api14": api14, "api10": api14.str[:10], "state": "MT",
                "well_name": w["Well_Nm"], "operator": w["CoName"], "county": w["County"], "field": w.get("Prod_Field"),
                "lat": pd.to_numeric(w["Lat"], errors="coerce"), "lon": pd.to_numeric(w["Long"], errors="coerce"),
                "crs_note": "NAD83 assumed (matches MBOGC shapefile .prj)", "well_type": w["Type"], "status": w["Status"],
                "trajectory": w.get("Slant"), "spud_date": pd.to_datetime(w["Spudded"], errors="coerce"),
                "completion_date": pd.to_datetime(w["Completed"], errors="coerce"), "td_ft": pd.to_numeric(w["DTD"], errors="coerce"),
                "extra": [json.dumps({"permitted": p, "map_symbol": m, "dor_id": d}) for p, m, d in zip(w["Permitted"], w["MapSymbol"], w["DOR_ID"])],
            })
            out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
            yield "wells", "data", out
