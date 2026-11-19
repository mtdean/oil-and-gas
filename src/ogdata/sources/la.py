"""Louisiana DENR SONRIS — well header with lat/lon and current LUW code via the public ArcGIS REST service.

Louisiana production is reported per LUW (lease-unit-well code), not per API, and
bulk production is only sold via DENR's Data Subscription Service. SONRIS's web
app terms prohibit scraping, so this connector only touches the GIS REST
endpoint (standard ArcGIS paging, 1 request/sec) and stores the API -> LUW map.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import get_json
from .base import Frame, RunContext, Source

LAYER = "https://sonris-gis.dnr.la.gov/arcgis/rest/services/DNRSvc/OC/MapServer/0/query"
PAGE = 1000


class Louisiana(Source):
    name = "la"
    title = "Louisiana SONRIS wells (GIS) + API->LUW crosswalk"
    state = "LA"
    url = "https://sonris-gis.dnr.la.gov/arcgis/rest/services/DNRSvc/OC/MapServer/0"
    tables = ("wells", "entity_wells")
    cadence = "weekly pull"
    granularity = "n/a (production is LUW-level, paid subscription)"
    history = "wells: all"
    has_latlon = True
    notes = "Production requires DENR Data Subscription Service (paid). Only GIS REST is used here."

    def fetch(self, ctx: RunContext) -> list[Path]:
        dest = self.raw / "wells.jsonl"
        from ..http import manifest
        from datetime import datetime, timezone
        rec = manifest().get(LAYER) or {}
        if dest.exists() and not ctx.force and rec.get("fetched_at"):
            age = datetime.now(timezone.utc) - datetime.fromisoformat(rec["fetched_at"])
            if age.total_seconds() < 6 * 86400:
                return []
        tmp = dest.with_suffix(".part")
        offset, n = 0, 0
        with tmp.open("w") as out:
            while True:
                js = get_json(LAYER, sess=ctx.sess, params={"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json",
                                                             "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "OBJECTID"})
                feats = js.get("features", [])
                for ft in feats:
                    out.write(json.dumps(ft["attributes"]) + "\n")
                n += len(feats)
                offset += PAGE
                if len(feats) < PAGE:
                    break
                time.sleep(1.0)
        tmp.replace(dest)
        manifest().put(LAYER, {"path": str(dest), "rows": n, "fetched_at": datetime.now(timezone.utc).isoformat()})
        self.log.info("la wells: %d features", n)
        return [dest]

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        w = pd.read_json(self.raw / "wells.jsonl", lines=True, dtype=False)
        w.columns = [c.upper() for c in w.columns]
        api = w["API_NUM"].astype("string").str.replace(r"\D", "", regex=True).fillna("")
        api14 = api.where((api.str.len() == 14) & (api != "00000000000000"))
        ms = lambda c: pd.to_datetime(pd.to_numeric(w[c], errors="coerce"), unit="ms", errors="coerce") if c in w.columns else pd.NaT
        wells = pd.DataFrame({
            "well_id": api14.fillna("LASN" + w["WELL_SERIAL_NUM"].astype(str)), "api14": api14, "api10": api14.str[:10], "state": "LA",
            "well_name": w.get("WELL_NAME"), "well_number": w.get("WELL_NUM"), "operator": w.get("ORG_OPER_NAME"), "county": w.get("PARISH_NAME"), "field": w.get("FIELD_NAME"),
            "lat": pd.to_numeric(w.get("SURFACE_LAT_DEC_DEG"), errors="coerce"), "lon": pd.to_numeric(w.get("SURFACE_LONG_DEC_DEG"), errors="coerce"),
            "crs_note": "decimal degrees as published (NAD83 assumed)", "well_type": w.get("PRODUCT_TYPE_CODE"), "status": w.get("WELL_STATUS_CODE"), "formation": w.get("SANDS"), "trajectory": w.get("DIRWELL"),
            "spud_date": ms("SPUD_DATE"), "completion_date": ms("ORIGINAL_COMPLETION_DATE"),
            "td_ft": pd.to_numeric(w.get("MEASURED_DEPTH"), errors="coerce"),
            "extra": [json.dumps({"serial": s, "luw_code": l, "luw_type": lt, "luw_name": ln, "tvd": t})
                      for s, l, lt, ln, t in zip(w.get("WELL_SERIAL_NUM"), w.get("LUW_CODE"), w.get("LUW_TYPE_CODE"), w.get("LUW_NAME"), w.get("TRUE_VERTICAL_DEPTH"))],
        })
        wells.loc[(wells.lat == 0) | (wells.lon == 0), ["lat", "lon"]] = pd.NA
        yield "wells", "data", wells
        luw = w["LUW_CODE"].astype("string").str.strip().fillna("")
        m = pd.DataFrame({"entity_id": luw, "entity_type": "luw", "api14": api14, "api10": api14.str[:10], "state": "LA",
                          "extra": [json.dumps({"serial": s, "luw_type": t}) for s, t in zip(w.get("WELL_SERIAL_NUM"), w.get("LUW_TYPE_CODE"))]})
        yield "entity_wells", "luw", m[m.entity_id.notna() & (m.entity_id != "")]
