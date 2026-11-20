"""Saskatchewan — Petrinex public volumetrics (well-level, 2022-01+) + Ministry of Energy and Resources well layer.

Wells come from the province's public ArcGIS service (Economy/Petroleum MapServer, layers 0 = vertical,
1 = non-vertical): UWI, surface + bottom-hole lat/lon, status, trajectory, spud / on-production dates,
completion pool & formations, crude type. Paginated 2,000 records per request (~95 requests).
Well-level production before 2022 is a paid Ministry subscription (Production/Injection bundle).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import get_json
from . import petrinex
from .base import Frame, RunContext, Source

GIS = "https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Petroleum/MapServer/{layer}/query"
FIELDS = ["WELL_CWI", "LEGACYWELLNAME", "WELLLICENCENUMBER", "WELLLICENCEBUSINESSASSOCIATE", "WELLDRILLINGTRAJECTORY", "WELLSTATUS",
          "WELLDERIVEDSPUDDATE", "WELLFINISHDRILLDATE", "WELLABANDONEDDATE", "RURALMUNICIPALITY", "SURFACEGEOAREA", "SURFACELATITUDE", "SURFACELONGITUDE",
          "SURFACELATLONGDATUM", "WELLBORE_UWI", "WELLBORETYPE", "WELLBORESTATUS", "WELLBORE_BH_MEASUREDDEPTH", "WELLBORE_BH_TRUEVERTICALDEPTH",
          "BOTTOMHOLELATITUDE", "BOTTOMHOLELONGITUDE", "WELLBORECOMPLETION_UWI", "INITIALCOMPLETIONDATE", "ONPRODUCTIONDATE", "CRUDEOILTYPE",
          "COMPLETIONPOOL", "COMPLETIONFORMATIONS", "WELLBORECOMP_CURRENTSTATUS", "WELLBORECOMP_CURRENTCOMPTYPE"]
PAGE = 2000
M_TO_FT = 3.28084


class Saskatchewan(Source):
    name = "sk"
    title = "Saskatchewan: Petrinex public well volumetrics + Ministry well layer"
    state = "SK"
    url = "https://www.petrinex.ca/public-data/spd/"
    tables = ("production_monthly", "wells")
    cadence = "monthly (Petrinex); wells refreshed every 14 days"
    granularity = "well"
    history = "2022-01 (public Petrinex); wells 1900s+"
    has_latlon = True
    notes = "UWI stored in api10. Oil includes condensate. Pre-2022 production is a paid Ministry bundle."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = petrinex.fetch_vol("SK", self.raw, ctx)
        for layer, name in ((0, "wells_vertical.json"), (1, "wells_nonvertical.json")):
            dest = self.raw / name
            if dest.exists() and not ctx.force and (time.time() - dest.stat().st_mtime) < 14 * 86400:
                continue
            feats = []
            offset = 0
            while True:
                js = get_json(GIS.format(layer=layer), sess=ctx.sess, params={
                    "where": "1=1", "outFields": ",".join(FIELDS), "returnGeometry": "false", "f": "json",
                    "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "OBJECTID"})
                batch = [f["attributes"] for f in js.get("features", [])]
                feats.extend(batch)
                if len(batch) < PAGE or not js.get("exceededTransferLimit", len(batch) == PAGE):
                    break
                offset += PAGE
                time.sleep(0.3)
            dest.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "features": feats}))
            self.log.info("SK wells layer %d: %s records", layer, f"{len(feats):,}")
            changed.append(dest)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        yield from petrinex.transform_vol("SK", self.raw, changed)
        if changed is None or any(p.name.startswith("wells_") for p in changed):
            yield "wells", "data", self._wells()

    def _wells(self) -> pd.DataFrame:
        rows = []
        for name in ("wells_vertical.json", "wells_nonvertical.json"):
            p = self.raw / name
            if p.exists():
                rows += json.loads(p.read_text())["features"]
        w = pd.DataFrame(rows)
        if w.empty:
            return pd.DataFrame()
        ms = lambda c: pd.to_datetime(pd.to_numeric(w[c], errors="coerce"), unit="ms", errors="coerce") if c in w else pd.NaT
        uwi = petrinex.norm_uwi(w.WELLBORE_UWI)
        lat = pd.to_numeric(w.SURFACELATITUDE, errors="coerce"); lon = pd.to_numeric(w.SURFACELONGITUDE, errors="coerce")
        bh_lat = pd.to_numeric(w.BOTTOMHOLELATITUDE, errors="coerce"); bh_lon = pd.to_numeric(w.BOTTOMHOLELONGITUDE, errors="coerce")
        md = pd.to_numeric(w.WELLBORE_BH_MEASUREDDEPTH, errors="coerce"); tvd = pd.to_numeric(w.WELLBORE_BH_TRUEVERTICALDEPTH, errors="coerce")
        traj = w.WELLDRILLINGTRAJECTORY.astype("string").str.upper().str[:1].replace({"V": "V", "H": "H", "D": "D"})
        lateral_ft = ((md - tvd) * M_TO_FT).where((traj == "H") & (md > tvd))
        out = pd.DataFrame({
            "well_id": uwi, "api10": uwi, "state": "SK",
            "well_name": w.LEGACYWELLNAME, "operator": w.WELLLICENCEBUSINESSASSOCIATE, "county": w.RURALMUNICIPALITY,
            "field": w.SURFACEGEOAREA, "formation": w.COMPLETIONFORMATIONS.where(w.COMPLETIONFORMATIONS.notna(), w.COMPLETIONPOOL) if "COMPLETIONFORMATIONS" in w else w.get("COMPLETIONPOOL"),
            "lat": lat, "lon": lon, "crs_note": "as published by SK Ministry of Energy and Resources (NAD83); surface location",
            "well_type": w.CRUDEOILTYPE.where(w.CRUDEOILTYPE.notna(), w.WELLBORECOMP_CURRENTCOMPTYPE) if "CRUDEOILTYPE" in w else w.get("WELLBORECOMP_CURRENTCOMPTYPE"),
            "status": w.WELLSTATUS, "trajectory": traj,
            "spud_date": ms("WELLDERIVEDSPUDDATE"), "completion_date": ms("INITIALCOMPLETIONDATE"), "first_prod_date": ms("ONPRODUCTIONDATE"),
            "td_ft": md * M_TO_FT, "lateral_ft": lateral_ft,
            "extra": [json.dumps({"licence": l, "cwi": c, "pool": p, "bh_lat": None if pd.isna(a) else round(float(a), 6), "bh_lon": None if pd.isna(o) else round(float(o), 6),
                                  "wellbore_status": ws, "comp_status": cs, "abandoned": None if pd.isna(ab) else str(ab.date())})
                      for l, c, p, a, o, ws, cs, ab in zip(w.WELLLICENCENUMBER, w.WELL_CWI, w.get("COMPLETIONPOOL", pd.Series([None] * len(w))), bh_lat, bh_lon, w.WELLBORESTATUS, w.get("WELLBORECOMP_CURRENTSTATUS", pd.Series([None] * len(w))), ms("WELLABANDONEDDATE"))],
        })
        out = out[out.well_id.notna() & (out.well_id.str.len() >= 15)]
        # the layer is one row per completion: keep the earliest-completed row per wellbore
        out = out.sort_values(["well_id", "completion_date"]).drop_duplicates("well_id")
        self.log.info("SK wells: %s wellbores, %s with coordinates", f"{len(out):,}", f"{out.lat.notna().sum():,}")
        return out
