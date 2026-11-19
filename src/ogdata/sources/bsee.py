"""BSEE — federal offshore Gulf of America (OCS) production by completion (OGOR-A), 1947+, and boreholes with lat/lon."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

BASE = "https://www.data.bsee.gov/"
OGORA_COLS = ["lease", "completion_name", "prod_date", "days_on", "product_code", "oil_bbl", "gas_mcf", "water_bbl", "api12",
              "well_status", "area_block", "operator_num", "operator", "field", "injection_vol", "prod_interval_cd",
              "first_prod_date", "unit_agt_number", "unit_aloc_suffix"]


class BSEE(Source):
    name = "bsee"
    title = "BSEE offshore (Gulf of America) completion-level production + boreholes"
    state = "GOM"
    url = "https://www.data.bsee.gov/Main/Production.aspx"
    tables = ("production_monthly", "wells")
    cadence = "monthly (prior years back-filled)"
    granularity = "completion"
    history = "1947-11"
    has_latlon = True
    notes = "'state' is GOM (federal OCS). API is 12-digit; we pad to 14."

    def _files(self, ctx: RunContext):
        y0 = date.today().year
        fixed = ["ogora1947-1979delimit.zip", "ogora1980-1989delimit.zip", "ogora1990-1995delimit.zip"]
        yearly = [f"ogora{y}delimit.zip" for y in range(1996, y0)]
        if ctx.limit_years:
            fixed = []
            yearly = [f for f in yearly if int(f[5:9]) >= y0 - ctx.limit_years]
        return fixed + yearly + ["ogoradelimit.zip"]

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for n in self._files(ctx):
            f = download(f"{BASE}Production/Files/{n}", self.raw / n, sess=ctx.sess, force=ctx.force,
                         max_age_hours=20 if n == "ogoradelimit.zip" else 24 * 7)
            if f.changed:
                if "zip" not in (f.path.read_bytes()[:2] == b"PK" and "zip" or ""):
                    self.log.warning("%s is not a zip (soft 404?)", n)
                    f.path.unlink()
                    continue
                changed.append(f.path)
        f = download(f"{BASE}Well/Files/BoreholeRawData.zip", self.raw / "BoreholeRawData.zip", sess=ctx.sess, force=ctx.force, max_age_hours=20)
        if f.changed:
            changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("ogora*.zip")) if changed is None else [p for p in changed if p.name.startswith("ogora")]
        for p in files:
            yield "production_monthly", p.stem, self._ogora(p)
        if changed is None or any(p.name == "BoreholeRawData.zip" for p in changed):
            yield "wells", "data", self._boreholes()

    def _ogora(self, path: Path) -> pd.DataFrame:
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
            raw = pd.read_csv(z.open(name), header=None, dtype=str, low_memory=False)
        if raw.shape[1] != len(OGORA_COLS):
            raise RuntimeError(f"{path.name}: expected {len(OGORA_COLS)} cols, got {raw.shape[1]}")
        raw.columns = OGORA_COLS
        for c in raw.columns:
            raw[c] = raw[c].str.strip()
        api12 = raw["api12"].str.replace(r"\D", "", regex=True)
        api14 = api12.str.ljust(14, "0").where(api12.str.len() >= 10)
        df = pd.DataFrame({
            "entity_id": raw["lease"] + ":" + api12.fillna("") + ":" + raw["completion_name"].fillna(""), "entity_type": "completion",
            "api14": api14, "api10": api14.str[:10], "state": "GOM",
            "period": pd.to_datetime(raw["prod_date"], format="%Y%m", errors="coerce"),
            "oil_bbl": pd.to_numeric(raw["oil_bbl"], errors="coerce"), "gas_mcf": pd.to_numeric(raw["gas_mcf"], errors="coerce"),
            "water_bbl": pd.to_numeric(raw["water_bbl"], errors="coerce"), "days_on": pd.to_numeric(raw["days_on"], errors="coerce"),
            "operator": raw["operator"], "well_name": raw["completion_name"], "field": raw["field"],
            "extra": [json.dumps({"lease": l, "product_code": pc, "well_status": ws, "area_block": ab, "first_prod_date": fp, "unit": u})
                      for l, pc, ws, ab, fp, u in zip(raw["lease"], raw["product_code"], raw["well_status"], raw["area_block"], raw["first_prod_date"], raw["unit_agt_number"])],
        })
        return df[df.period.notna()]

    def _boreholes(self) -> pd.DataFrame:
        with zipfile.ZipFile(self.raw / "BoreholeRawData.zip") as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
            w = pd.read_csv(z.open(name), dtype=str, low_memory=False)
        w.columns = [c.strip().upper() for c in w.columns]
        api12 = w["API_WELL_NUMBER"].astype("string").str.replace(r"\D", "", regex=True)
        api14 = api12.str.ljust(14, "0")
        dt = lambda c: pd.to_datetime(w[c], errors="coerce") if c in w.columns else pd.NaT
        out = pd.DataFrame({
            "well_id": api14, "api14": api14, "api10": api14.str[:10], "state": "GOM",
            "well_name": (w.get("WELL_NAME").astype(str) + " " + w.get("WELL_NAME_SUFFIX").fillna("").astype(str)).str.strip(),
            "operator": w.get("COMPANY_NAME"),
            "lat": pd.to_numeric(w.get("SURF_LATITUDE"), errors="coerce"), "lon": pd.to_numeric(w.get("SURF_LONGITUDE"), errors="coerce"),
            "crs_note": "NAD27 (BSEE OCS standard) — verify", "well_type": w.get("WELL_TYPE_CODE"), "status": w.get("BOREHOLE_STAT_CD"),
            "spud_date": dt("WELL_SPUD_DATE"), "completion_date": dt("TOTAL_DEPTH_DATE"),
            "td_ft": pd.to_numeric(w.get("BH_TOTAL_MD"), errors="coerce"),
            "extra": [json.dumps({"bottom_lease": bl, "area": a, "block": b, "water_depth": wd, "tvd": tvd, "bh_lat": blat, "bh_lon": blon})
                      for bl, a, b, wd, tvd, blat, blon in zip(w.get("BOTM_LEASE_NUMBER"), w.get("BOTM_AREA_CODE"), w.get("BOTM_BLOCK_NUMBER"),
                                                                w.get("WATER_DEPTH"), w.get("WELL_BORE_TVD"), w.get("BOTM_LATITUDE"), w.get("BOTM_LONGITUDE"))],
        })
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
