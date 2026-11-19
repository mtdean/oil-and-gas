"""Utah DOGM Data Center — monthly production by well x wellbore x formation (1984+), wells with lat/lon."""
from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

BASE = "https://oilgas.ogm.utah.gov/pub/Database/"
RANGES = ["1984To1998", "1999To2008", "2009To2014", "2015To2019", "2020To2024", "2025To2029"]


def _read_zip_csv(path: Path, **kw) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        return pd.read_csv(z.open(name), dtype=str, na_values=["NULL", ""], low_memory=False, encoding_errors="replace", **kw)


class Utah(Source):
    name = "ut"
    title = "Utah DOGM monthly production by well + well header"
    state = "UT"
    url = "https://oilgas.ogm.utah.gov/oilgasweb/data-center/dc-main.xhtml"
    tables = ("production_monthly", "wells")
    cadence = "daily regeneration"
    granularity = "well_formation"
    history = "1984-01"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        y0 = date.today().year
        for rng in RANGES:
            end = int(rng[-4:])
            if ctx.limit_years and end < y0 - ctx.limit_years:
                continue
            current = end >= y0
            f = download(f"{BASE}Production{rng}.zip", self.raw / f"Production{rng}.zip", sess=ctx.sess, force=ctx.force,
                         max_age_hours=20 if current else 24 * 7)
            if f.changed:
                changed.append(f.path)
        for n in ("Wells.zip", "WellHistory.zip"):
            f = download(BASE + n, self.raw / n, sess=ctx.sess, force=ctx.force, max_age_hours=20)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("Production*.zip")) if changed is None else [p for p in changed if p.name.startswith("Production")]
        for p in files:
            yield "production_monthly", p.stem, self._prod(p)
        if changed is None or any(p.name in ("Wells.zip", "WellHistory.zip") for p in changed):
            yield "wells", "data", self._wells()

    def _prod(self, path: Path) -> pd.DataFrame:
        raw = _read_zip_csv(path)
        raw.columns = [c.strip() for c in raw.columns]
        api10 = raw["API"].astype("string").str.replace(r"\D", "", regex=True).str[:10]
        wb = raw["WellBore"].astype("string").fillna("0").str.zfill(2)
        api14 = api10 + wb + "00"
        df = pd.DataFrame({
            "entity_id": api14 + ":" + raw["FormationName"].astype("string").fillna(""), "entity_type": "well_formation",
            "api14": api14, "api10": api10, "state": "UT",
            "period": pd.to_datetime(raw["ReportPeriod"], errors="coerce"),
            "oil_bbl": pd.to_numeric(raw["Oil"], errors="coerce"), "gas_mcf": pd.to_numeric(raw["Gas"], errors="coerce"),
            "water_bbl": pd.to_numeric(raw["Water"], errors="coerce"), "days_on": pd.to_numeric(raw["DaysProd"], errors="coerce"),
            "operator": raw["Operator"], "formation": raw["FormationName"],
            "extra": [json.dumps({"entity": e, "well_status": s, "well_type": t, "amended": a}) for e, s, t, a in
                      zip(raw.get("Entity"), raw.get("WellStatus"), raw.get("WellType"), raw.get("IsAmended"))],
            "_recv": pd.to_datetime(raw.get("Received"), errors="coerce"),
        })
        df = df.sort_values("_recv").drop_duplicates(["entity_id", "period"], keep="last").drop(columns="_recv")
        return df[df.period.notna()]

    def _wells(self) -> pd.DataFrame:
        w = _read_zip_csv(self.raw / "Wells.zip")
        w.columns = [c.strip() for c in w.columns]
        api14 = w["API"].astype("string").str.replace(r"\D", "", regex=True).str.ljust(14, "0")
        traj = pd.Series(pd.NA, index=w.index, dtype="string")
        traj = traj.mask(w.get("Dir_Horiz", pd.Series(dtype=str)).astype("string").str.upper().isin(["Y", "1", "TRUE"]), "H")
        traj = traj.mask(w.get("Dir_Direct", pd.Series(dtype=str)).astype("string").str.upper().isin(["Y", "1", "TRUE"]), "D")
        traj = traj.mask(w.get("Dir_Vert", pd.Series(dtype=str)).astype("string").str.upper().isin(["Y", "1", "TRUE"]), "V")
        out = pd.DataFrame({
            "well_id": api14, "api14": api14, "api10": api14.str[:10], "state": "UT",
            "well_name": w["WellName"], "operator": w["Operator"], "county": w["CountyName"], "field": w["FieldName"],
            "lat": pd.to_numeric(w["Latitude"], errors="coerce"), "lon": pd.to_numeric(w["Longitude"], errors="coerce"),
            "crs_note": "NAD83", "well_type": w["welltype"], "status": w["wellstatus"], "trajectory": traj,
            "first_prod_date": pd.to_datetime(w.get("FirstProdDate"), errors="coerce"),
            "extra": [json.dumps({"cum_oil": o, "cum_gas": g, "cum_water": wt, "lease_type": lt, "unit": u, "multi_lats": ml})
                      for o, g, wt, lt, u, ml in zip(w.get("TotCum_Oil"), w.get("TotCum_Gas"), w.get("TotCum_Water"), w.get("LeaseType"), w.get("UnitName"), w.get("Multi_Lats"))],
        })
        # enrich with WellHistory (spud/completion/TD) — latest record per API10
        hp = self.raw / "WellHistory.zip"
        if hp.exists():
            h = _read_zip_csv(hp)
            h.columns = [c.strip() for c in h.columns]
            h["api10"] = h["WellID"].astype("string").str.replace(r"\D", "", regex=True).str[:10]
            for c in ("RotarySpud", "DrySpud", "WCRCompletionDate", "FirstProdDate"):
                if c in h.columns:
                    h[c] = pd.to_datetime(h[c], errors="coerce")
            agg = h.groupby("api10").agg(spud=("RotarySpud", "min"), comp=("WCRCompletionDate", "max"), td=("MD", lambda s: pd.to_numeric(s, errors="coerce").max()))
            out = out.merge(agg, left_on="api10", right_index=True, how="left")
            out["spud_date"], out["completion_date"], out["td_ft"] = out.pop("spud"), out.pop("comp"), out.pop("td")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
