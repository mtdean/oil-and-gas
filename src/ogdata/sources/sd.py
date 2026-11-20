"""South Dakota DANR Oil & Gas — per-well monthly production (1950s+) and well file from the SDOIL export bundle.

https://danr.sd.gov/Environment/MineralsMining/OilGas/Data.aspx -> https://sddenr.net/pubs/og/SDOILexport.zip
(Wells.xlsx, Well_Completions.xlsx, Formation_Tops.xlsx, Oil_Gas_Production_All_Years.xlsx sheet 'Export_4_Web',
UIC injection). ~2,100 wells, mostly Harding County Red River; refreshed monthly.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

URL = "https://sddenr.net/pubs/og/SDOILexport.zip"


def _api10(s: pd.Series) -> pd.Series:
    d = s.astype("string").str.replace(r"\D", "", regex=True)
    return d.str[:10].where(d.str.len() >= 10)


def _xlsx(con, path: Path, sheet: str | None = None) -> pd.DataFrame:
    opt = f", sheet='{sheet}'" if sheet else ""
    df = con.execute(f"SELECT * FROM read_xlsx('{path}', all_varchar=true, header=true{opt})").df()
    df.columns = [c.strip() for c in df.columns]
    return df


class SouthDakota(Source):
    name = "sd"
    title = "South Dakota DANR per-well monthly production + wells"
    state = "SD"
    url = "https://danr.sd.gov/Environment/MineralsMining/OilGas/Data.aspx"
    tables = ("production_monthly", "wells")
    cadence = "monthly"
    granularity = "well"
    history = "1950s"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        f = download(URL, self.raw / "SDOILexport.zip", sess=ctx.sess, force=ctx.force, max_age_hours=24 * 7)
        return [f.path] if f.changed else []

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        xdir = self.raw / "export"
        with zipfile.ZipFile(self.raw / "SDOILexport.zip") as z:
            z.extractall(xdir)
        files = {p.name: p for p in xdir.rglob("*.xlsx")}
        con = duckdb.connect()
        prod = _xlsx(con, files["Oil_Gas_Production_All_Years.xlsx"], "Export_4_Web")
        wells = _xlsx(con, files["Wells.xlsx"])
        comp = _xlsx(con, files["Well_Completions.xlsx"])
        con.close()

        api = _api10(prod["API_No"])
        period = pd.to_datetime(prod["Date_Production"], errors="coerce").dt.to_period("M").dt.to_timestamp()
        num = lambda c: pd.to_numeric(prod[c], errors="coerce")
        df = pd.DataFrame({
            "entity_id": api, "entity_type": "well", "api14": api + "0000", "api10": api, "state": "SD", "period": period,
            "oil_bbl": num("Oil_bbls"), "gas_mcf": num("Total_Gas_mcf"), "water_bbl": num("Water_bbls"), "days_on": num("Prod_Days"),
            "gas_sold_mcf": num("Marketed_Gas_mcf"), "operator": prod["Reported_Operator_Name"], "well_name": prod["Well_Name"],
            "county": prod["County"], "field": prod["Field_Name"],
            "extra": [json.dumps({"facility": f, "status": s, "unit": u, "confidential": c}) for f, s, u, c in zip(prod["Facility_ID"], prod["Reported_Well_Status_A"], prod["Enhanced_Recovery_Unit"], prod["Production_Confidential"])],
        })
        df = df[df.api10.notna() & df.period.notna()].drop_duplicates(["entity_id", "period"], keep="last")
        self.log.info("SD production: %s well-months, %s wells, %s..%s", f"{len(df):,}", f"{df.api10.nunique():,}", df.period.min().date(), df.period.max().date())
        for yr, part in df.groupby(df.period.dt.year):
            yield "production_monthly", str(yr), part

        wells["api10"] = _api10(wells["API Number"])
        comp["api10"] = _api10(comp["API Number"])
        comp = comp.sort_values(["api10", "Completion Date"]).drop_duplicates("api10", keep="last")
        w = wells.merge(comp[["api10", "Wellbore Type", "Measured Depth", "True Vertical Depth", "Producing Formation", "Top Producing Interval MD", "Bottom Producing Interval MD"]], on="api10", how="left")
        xl = lambda c: pd.to_datetime(pd.to_numeric(w[c], errors="coerce"), unit="D", origin="1899-12-30", errors="coerce")  # Excel serials
        traj = w["Wellbore Type"].astype("string").str.upper().str[:1].map({"H": "H", "V": "V", "D": "D"})
        md = pd.to_numeric(w["Measured Depth"], errors="coerce"); tvd = pd.to_numeric(w["True Vertical Depth"], errors="coerce")
        out = pd.DataFrame({
            "well_id": w.api10 + "0000", "api14": w.api10 + "0000", "api10": w.api10, "state": "SD",
            "well_name": w["Well Name"], "operator": w["Operator"], "county": w["County"], "field": w["Oil or Gas Field"],
            "formation": w["Producing Formation"].where(w["Producing Formation"].notna(), w["Bottom Hole Formation"]),
            "lat": pd.to_numeric(w["Latitude (GCS NAD 83)"], errors="coerce"), "lon": pd.to_numeric(w["Longitude (GCS NAD 83)"], errors="coerce"),
            "crs_note": "NAD83 (DANR Wells.xlsx)", "well_type": w["Current Well Type"], "status": w["Administrative Status"], "trajectory": traj,
            "spud_date": xl("Spud Date"), "completion_date": xl("Completion Date"), "td_ft": md,
            "lateral_ft": (md - tvd).where((traj == "H") & (md > tvd)),
            "extra": [json.dumps({"permit": p, "unit": u, "plug_date": None if pd.isna(d) else str(d.date()), "permitted_type": t}) for p, u, d, t in zip(w["Permit"], w["Enhanced Recovery Unit"], xl("Plug Date"), w["Permitted Well Type"])],
        })
        out = out[out.api10.notna()].drop_duplicates("api10")
        self.log.info("SD wells: %s, %s with coordinates", f"{len(out):,}", f"{out.lat.notna().sum():,}")
        yield "wells", "data", out
