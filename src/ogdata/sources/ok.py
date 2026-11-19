"""Oklahoma Corporation Commission — RBDMS well header (nightly CSV) + Form 1002A completions (xlsx, 2010+).

Oklahoma production is held by the Tax Commission at PUN (lease/unit) level and is
not published as a bulk file; the completions file carries OTC_Prod_Unit_No so
we store the well -> PUN crosswalk in entity_wells for when PUN data is obtained.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

WELLS_URL = "https://oklahoma.gov/content/dam/ok/en/occ/documents/og/ogdatafiles/rbdms-wells.csv"
COMPL_URL = "https://oklahoma.gov/content/dam/ok/en/occ/documents/og/ogdatafiles/completions-wells-formations-base.xlsx"


def _find(cols, *patterns):
    norm = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in cols}
    for p in patterns:
        key = re.sub(r"[^a-z0-9]", "", p.lower())
        if key in norm:
            return norm[key]
    for p in patterns:
        key = re.sub(r"[^a-z0-9]", "", p.lower())
        for k, c in norm.items():
            if key in k:
                return c
    return None


class Oklahoma(Source):
    name = "ok"
    title = "Oklahoma OCC wells (RBDMS) + 1002A completions (well->PUN crosswalk)"
    state = "OK"
    url = "https://oklahoma.gov/occ/divisions/oil-gas/oil-gas-data.html"
    tables = ("wells", "entity_wells")
    cadence = "nightly"
    granularity = "n/a (production is PUN-level at the Tax Commission, request-only)"
    history = "wells: all; completions 2010+"
    has_latlon = True
    notes = "No public bulk production. OTC Production History Request Form needed for PUN volumes (July 1990+)."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for url, name in ((WELLS_URL, "rbdms-wells.csv"), (COMPL_URL, "completions.xlsx")):
            f = download(url, self.raw / name, sess=ctx.sess, force=ctx.force, max_age_hours=20)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        w = pd.read_csv(self.raw / "rbdms-wells.csv", dtype=str, encoding="latin-1", low_memory=False)
        w.columns = [c.strip().upper() for c in w.columns]
        api10 = w["API"].astype("string").str.replace(r"\D", "", regex=True).str[:10]
        api10 = api10.where(api10.str.len() == 10)
        wells = pd.DataFrame({
            "well_id": api10, "api10": api10, "api14": api10 + "0000", "state": "OK",
            "well_name": w.get("WELL_NAME"), "well_number": w.get("WELL_NUM"), "operator": w.get("OPERATOR"), "county": w.get("COUNTY"),
            "lat": pd.to_numeric(w.get("SH_LAT"), errors="coerce"), "lon": pd.to_numeric(w.get("SH_LON"), errors="coerce"),
            "crs_note": "WGS84 (per RBDMS shapefile)", "well_type": w.get("WELLTYPE"), "status": w.get("WELLSTATUS"),
            "extra": [json.dumps({"symbol_class": sc, "section": s, "township": t, "range": r}) for sc, s, t, r in
                      zip(w.get("SYMBOL_CLASS"), w.get("SECTION"), w.get("TOWNSHIP"), w.get("RANGE"))],
        }).dropna(subset=["well_id"])
        wells.loc[(wells.lat == 0) | (wells.lon == 0), ["lat", "lon"]] = pd.NA

        cp = self.raw / "completions.xlsx"
        if cp.exists():
            c = pd.read_excel(cp, dtype=str, engine="calamine")
            cols = list(c.columns)
            api_c = _find(cols, "API_Number", "API")
            pun_c = _find(cols, "OTC_Prod_Unit_No", "PUN")
            spud_c, comp_c, fp_c = _find(cols, "Spud"), _find(cols, "Well_Completion", "Completion_Date"), _find(cols, "First_Prod", "First_Production")
            tvd_c, md_c, form_c = _find(cols, "TVD"), _find(cols, "MD", "Measured_Depth"), _find(cols, "Formation", "Formation_Name")
            lat_c, lon_c = _find(cols, "Surf_Lat_Y", "Surface_Latitude"), _find(cols, "Surf_Long_X", "Surface_Longitude")
            self.log.info("completions columns: api=%s pun=%s spud=%s comp=%s first_prod=%s tvd=%s md=%s form=%s", api_c, pun_c, spud_c, comp_c, fp_c, tvd_c, md_c, form_c)
            if api_c:
                c["api10"] = c[api_c].astype("string").str.replace(r"\D", "", regex=True).str[:10]
                dt = lambda col: pd.to_datetime(c[col], errors="coerce") if col else pd.Series(pd.NaT, index=c.index)
                c["_spud"], c["_comp"], c["_fp"] = dt(spud_c), dt(comp_c), dt(fp_c)
                agg = c.groupby("api10").agg(spud_date=("_spud", "min"), completion_date=("_comp", "max"), first_prod_date=("_fp", "min"),
                                             td_ft=(md_c, lambda s: pd.to_numeric(s, errors="coerce").max()) if md_c else ("_spud", "size"),
                                             formation=(form_c, "last") if form_c else ("_spud", "size"))
                if not md_c:
                    agg = agg.drop(columns="td_ft")
                if not form_c:
                    agg = agg.drop(columns="formation")
                wells = wells.merge(agg, left_on="api10", right_index=True, how="left")
                if lat_c and lon_c:  # fill missing RBDMS coordinates from completions
                    ll = c.groupby("api10").agg(_lat=(lat_c, "last"), _lon=(lon_c, "last"))
                    wells = wells.merge(ll, left_on="api10", right_index=True, how="left")
                    wells["lat"] = wells["lat"].fillna(pd.to_numeric(wells.pop("_lat"), errors="coerce"))
                    wells["lon"] = wells["lon"].fillna(pd.to_numeric(wells.pop("_lon"), errors="coerce"))
                if pun_c:
                    ew = c[["api10", pun_c, "_comp"]].dropna(subset=[pun_c]).drop_duplicates(["api10", pun_c])
                    yield "entity_wells", "pun", pd.DataFrame({
                        "entity_id": ew[pun_c].astype("string").str.strip(), "entity_type": "pun",
                        "api14": ew["api10"] + "0000", "api10": ew["api10"], "state": "OK", "valid_from": ew["_comp"],
                    })
        yield "wells", "data", wells
