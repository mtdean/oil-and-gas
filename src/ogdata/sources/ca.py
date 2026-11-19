"""California CalGEM — all wells with lat/lon (WellSTAR GIS table, refreshed nightly).

Bulk production files (through 2021 only) are delivered by email from
filerequest.conservation.ca.gov and are not scripted here; 2022+ is only in the
WellSTAR Power BI dashboard. See docs/sources.md.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

URL = "https://gis.conservation.ca.gov/portal/sharing/rest/content/items/0d30c4d9ac8f4f84a53a145e7d68eb6b/data"


class California(Source):
    name = "ca"
    title = "California CalGEM all wells (lat/lon)"
    state = "CA"
    url = "https://www.conservation.ca.gov/calgem/maps/Pages/GISMapping2.aspx"
    tables = ("wells",)
    cadence = "nightly"
    granularity = "n/a (production bulk files are email-request only, through 2021)"
    history = "wells: all"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        f = download(URL, self.raw / "AllWells_table.zip", sess=ctx.sess, force=ctx.force, max_age_hours=20)
        return [f.path] if f.changed else []

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        with zipfile.ZipFile(self.raw / "AllWells_table.zip") as z:
            name = next(n for n in z.namelist() if n.lower().endswith((".csv", ".txt")))
            w = pd.read_csv(z.open(name), dtype=str, low_memory=False, encoding_errors="replace")
        w.columns = [c.strip() for c in w.columns]
        # the CSV export truncates names to 10 chars (shapefile heritage); normalise both spellings
        ren = {"WellTypeLa": "WellTypeLabel", "OperatorNa": "OperatorName", "OperatorCo": "OperatorCode", "isConfiden": "isConfidential",
               "isDirectio": "isDirectionallyDrilled", "BaseMeridi": "BaseMeridian", "WellDesign": "WellDesignation"}
        w = w.rename(columns=ren)
        for c in ("LeaseName", "WellNumber", "OperatorName", "CountyName", "FieldName", "WellTypeLabel", "WellType", "WellStatus", "isDirectionallyDrilled", "SpudDate", "District", "AreaName", "isConfidential", "GISSource"):
            if c not in w.columns:
                w[c] = None
        api = w["API"].astype("string").str.replace(r"\D", "", regex=True)
        api10 = api.str[:10].where(api.str.len() >= 10, ("04" + api.str.zfill(8)).where(api.str.len() <= 8))
        out = pd.DataFrame({
            "well_id": api10, "api10": api10, "api14": api10 + "0000", "state": "CA",
            "well_name": w.get("LeaseName"), "well_number": w.get("WellNumber"), "operator": w.get("OperatorName"), "county": w.get("CountyName"),
            "field": w.get("FieldName"), "lat": pd.to_numeric(w.get("Latitude"), errors="coerce"), "lon": pd.to_numeric(w.get("Longitude"), errors="coerce"),
            "crs_note": "as published by CalGEM (WGS84/NAD83)", "well_type": w.get("WellTypeLabel", w.get("WellType")), "status": w.get("WellStatus"),
            "trajectory": w.get("isDirectionallyDrilled"), "spud_date": pd.to_datetime(w.get("SpudDate"), errors="coerce", format="mixed"),
            "extra": [json.dumps({"district": d, "area": a, "confidential": c, "gis_source": g}) for d, a, c, g in zip(w.get("District"), w.get("AreaName"), w.get("isConfidential"), w.get("GISSource"))],
        }).dropna(subset=["well_id"]).drop_duplicates("well_id", keep="last")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        yield "wells", "data", out
