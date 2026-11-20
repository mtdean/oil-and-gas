"""FracFocus national hydraulic fracturing registry — one row per disclosure (frac job), 2011+, with lat/lon.

Download: https://www.fracfocusdata.org/digitaldownload/FracFocusCSV.zip (~450 MB, refreshed 5x/week).
As of 2026-11-19 the host resets the connection for requests from this machine (an
IP-level block / WAF; it answers from other networks). We make one attempt per run (retries=0) and
fall back to a zip downloaded by hand into data/raw/fracfocus/FracFocusCSV.zip.
We load DisclosureList_1.csv into `completions` and derive a `wells` row per API14
(location fallback for states without good coordinates). The chemical registry
tables are left in the raw zip.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

URL = "https://www.fracfocusdata.org/digitaldownload/FracFocusCSV.zip"
STATE_ABBR = {"texas": "TX", "north dakota": "ND", "new mexico": "NM", "oklahoma": "OK", "colorado": "CO", "pennsylvania": "PA", "wyoming": "WY",
              "louisiana": "LA", "west virginia": "WV", "ohio": "OH", "utah": "UT", "california": "CA", "montana": "MT", "kansas": "KS",
              "arkansas": "AR", "alaska": "AK", "mississippi": "MS", "alabama": "AL", "michigan": "MI", "nebraska": "NE", "kentucky": "KY",
              "virginia": "VA", "illinois": "IL", "indiana": "IN", "new york": "NY", "tennessee": "TN", "south dakota": "SD", "nevada": "NV",
              "missouri": "MO", "florida": "FL", "arizona": "AZ", "oregon": "OR", "idaho": "ID", "maryland": "MD"}


class FracFocus(Source):
    name = "fracfocus"
    title = "FracFocus frac disclosures (completion dates, water volumes, lat/lon)"
    url = "https://fracfocus.org/data-download"
    tables = ("completions", "wells")
    cadence = "5x/week"
    granularity = "completion"
    history = "2011"
    has_latlon = True

    MANUAL = "download FracFocusCSV.zip in a browser from https://fracfocus.org/data-download and drop it at data/raw/fracfocus/FracFocusCSV.zip"

    def fetch(self, ctx: RunContext) -> list[Path]:
        import requests
        dest = self.raw / "FracFocusCSV.zip"
        try:
            f = download(URL, dest, sess=ctx.sess, force=ctx.force, max_age_hours=20, retries=0)
        except (requests.RequestException, ConnectionResetError, OSError) as e:
            if dest.exists():
                self.log.warning("fracfocus: download failed (%s); using existing %s. Manual refresh: %s", e, dest.name, self.MANUAL)
                return []
            self.log.warning("fracfocus: download failed (%s) and no local zip. %s", e, self.MANUAL)
            raise
        return [f.path] if f.changed else []

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        with zipfile.ZipFile(self.raw / "FracFocusCSV.zip") as z:
            name = next(n for n in z.namelist() if n.lower().startswith("disclosurelist"))
            d = pd.read_csv(z.open(name), dtype=str, low_memory=False)
        d.columns = [c.strip() for c in d.columns]
        api = d["APINumber"].astype("string").str.replace(r"\D", "", regex=True)
        api14 = api.str.ljust(14, "0").where(api.str.len() >= 10)
        js = pd.to_datetime(d["JobStartDate"], errors="coerce", format="mixed")
        je = pd.to_datetime(d["JobEndDate"], errors="coerce", format="mixed")
        js = js.where(js.dt.year >= 2005)
        je = je.where(je.dt.year >= 2005)
        state = d["StateName"].astype("string").str.strip().str.lower().map(STATE_ABBR).fillna(d["StateName"])
        comp = pd.DataFrame({
            "api14": api14, "api10": api14.str[:10], "state": state, "job_start": js, "job_end": je,
            "operator": d["OperatorName"], "well_name": d["WellName"], "county": d["CountyName"],
            "lat": pd.to_numeric(d["Latitude"], errors="coerce"), "lon": pd.to_numeric(d["Longitude"], errors="coerce"),
            "tvd_ft": pd.to_numeric(d["TVD"], errors="coerce"),
            "base_water_gal": pd.to_numeric(d["TotalBaseWaterVolume"], errors="coerce"),
            "base_nonwater_gal": pd.to_numeric(d["TotalBaseNonWaterVolume"], errors="coerce"),
            "federal_well": d["FederalWell"].astype("string").str.lower().isin(["true", "1", "yes"]),
            "indian_well": d["IndianWell"].astype("string").str.lower().isin(["true", "1", "yes"]),
            "disclosure_id": d["DisclosureId"],
            "extra": [json.dumps({"projection": p, "ff_version": v}) for p, v in zip(d["Projection"], d["FFVersion"])],
        })
        comp.loc[(comp.lat == 0) | (comp.lon == 0), ["lat", "lon"]] = pd.NA
        yield "completions", "data", comp
        c = comp[comp.api14.notna()].sort_values("job_start")
        g = c.groupby("api14")
        wells = pd.DataFrame({
            "api14": g.size().index, "api10": g["api10"].last().values, "state": g["state"].last().values,
            "well_name": g["well_name"].last().values, "operator": g["operator"].last().values, "county": g["county"].last().values,
            "lat": g["lat"].last().values, "lon": g["lon"].last().values,
            "crs_note": ("FracFocus-reported; projection=" + c.groupby("api14")["extra"].last().str.extract(r'"projection": "([^"]*)"')[0].fillna("?")).values,
            "completion_date": g["job_end"].max().values, "td_ft": g["tvd_ft"].max().values,
            "extra": [json.dumps({"frac_jobs": int(n), "first_job_start": str(fs)[:10], "total_base_water_gal": None if pd.isna(w) else float(w)})
                      for n, fs, w in zip(g.size().values, g["job_start"].min().values, g["base_water_gal"].sum(min_count=1).values)],
        })
        wells["well_id"] = wells["api14"]
        yield "wells", "data", wells
