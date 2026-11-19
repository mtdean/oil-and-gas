"""Kansas Geological Survey — lease-level monthly production (1980+, oil and gas archives) + well master list with lat/lon.

Kansas reports production per lease, not per well. The lease rows list the API
numbers of member wells, which we store in entity_wells. Note: KGS states that
pre-1987 production was licensed from IHS and may not be redistributed.
"""
from __future__ import annotations

import json
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

BASE = "https://www.kgs.ku.edu/PRS/Ora_Archive/"
DECADES = ["1980_1989", "1990_1999", "2000_2009", "2010_2019", "2020_present"]


class Kansas(Source):
    name = "ks"
    title = "Kansas KGS lease-level monthly production + wells"
    state = "KS"
    url = "https://www.kgs.ku.edu/PRS/petroDB.html"
    tables = ("production_monthly", "entity_wells", "wells")
    cadence = "monthly"
    granularity = "lease"
    history = "1980 (pre-1987 licensed, not redistributable)"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for dec in DECADES:
            if ctx.limit_years and dec != "2020_present":
                continue
            for prod in ("oil", "gas"):
                n = f"{prod}_leases_{dec}.zip"
                f = download(BASE + n, self.raw / n, sess=ctx.sess, force=ctx.force, max_age_hours=20 if dec == "2020_present" else 24 * 30)
                if f.changed:
                    changed.append(f.path)
        for n in ("ks_wells.zip", "all_kansas_leases.zip"):
            f = download(BASE + n, self.raw / n, sess=ctx.sess, force=ctx.force, max_age_hours=24 * 3)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("*_leases_*.zip")) if changed is None else [p for p in changed if "_leases_" in p.name]
        for p in files:
            yield "production_monthly", p.stem, self._prod(p)
        if changed is None or any(p.name in ("all_kansas_leases.zip",) for p in changed):
            yield "entity_wells", "leases", self._lease_wells()
        if changed is None or any(p.name == "ks_wells.zip" for p in changed):
            yield "wells", "data", self._wells()

    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".txt") or n.lower().endswith(".csv"))
            return pd.read_csv(z.open(name), dtype=str, low_memory=False, encoding_errors="replace")

    def _prod(self, path: Path) -> pd.DataFrame:
        raw = self._read(path)
        raw.columns = [c.strip().upper() for c in raw.columns]
        my = raw["MONTH-YEAR"].astype("string").str.split("-", expand=True)
        mo = pd.to_numeric(my[0], errors="coerce")
        yr = pd.to_numeric(my[1], errors="coerce")
        months = mo.map(lambda v: 1 if 1 <= v <= 12 else 12 if v == 0 else None)
        period = pd.to_datetime(dict(year=yr, month=mo.where(mo.between(1, 12), 1), day=1), errors="coerce")
        prod = pd.to_numeric(raw["PRODUCTION"], errors="coerce")
        is_oil = raw["PRODUCT"].astype("string").str.upper().str.startswith("O")
        df = pd.DataFrame({
            "entity_id": raw["LEASE_KID"].astype("string"), "entity_type": "lease", "state": "KS", "period": period, "period_months": months,
            "oil_bbl": prod.where(is_oil), "gas_mcf": prod.where(~is_oil),
            "operator": raw["OPERATOR"], "well_name": raw["LEASE"], "county": raw["COUNTY"], "field": raw["FIELD"], "formation": raw["PRODUCING_ZONE"],
            "extra": [json.dumps({"lat": a, "lon": b, "wells": w, "dor_code": d, "product": p}) for a, b, w, d, p in
                      zip(pd.to_numeric(raw["LATITUDE"], errors="coerce").astype(object).where(lambda x: x.notna(), None),
                          pd.to_numeric(raw["LONGITUDE"], errors="coerce").astype(object).where(lambda x: x.notna(), None),
                          raw["WELLS"], raw["DOR_CODE"], raw["PRODUCT"])],
        })
        df = df[df.period.notna() & df.period_months.notna()]
        return df.drop_duplicates(["entity_id", "period", "period_months", "extra"], keep="last")

    def _lease_wells(self) -> pd.DataFrame:
        raw = self._read(self.raw / "all_kansas_leases.zip")
        raw.columns = [c.strip().upper() for c in raw.columns]
        apis = raw["API_NUMBER"].astype("string").fillna("").str.split(r"[,;\s]+")
        ex = raw[["LEASE_KID", "YEAR_START", "YEAR_STOP"]].assign(api=apis).explode("api")
        api = ex["api"].astype("string").str.replace(r"\D", "", regex=True)
        api10 = api.str[:10].where(api.str.len() >= 10)
        out = pd.DataFrame({"entity_id": ex["LEASE_KID"].astype("string"), "entity_type": "lease", "api14": api10 + "0000", "api10": api10, "state": "KS",
                            "valid_from": pd.to_datetime(ex["YEAR_START"], format="%Y", errors="coerce"),
                            "valid_to": pd.to_datetime(ex["YEAR_STOP"], format="%Y", errors="coerce")})
        return out[out.api10.notna()].drop_duplicates(["entity_id", "api10"])

    def _wells(self) -> pd.DataFrame:
        w = self._read(self.raw / "ks_wells.zip")
        w.columns = [c.strip().upper() for c in w.columns]
        api = w["API_NUM_NODASH"].astype("string").str.replace(r"\D", "", regex=True)
        api10 = api.str[:10].where(api.str.len() >= 10)
        dt = lambda c: pd.to_datetime(w[c], errors="coerce", format="mixed") if c in w.columns else pd.NaT
        out = pd.DataFrame({
            "well_id": api10.fillna("KID" + w["KID"].astype(str)), "api10": api10, "api14": api.str.ljust(14, "0").where(api.str.len() >= 10), "state": "KS",
            "well_name": w.get("LEASE_WELL_NAME"), "well_number": w.get("WELL"), "operator": w.get("CURR_OPERATOR"),
            "county": "KS-" + api.str[2:5], "field": w.get("FIELD"), "formation": w.get("PRODUCE_FORM"),
            "lat": pd.to_numeric(w.get("LATITUDE"), errors="coerce"), "lon": pd.to_numeric(w.get("LONGITUDE"), errors="coerce"),
            "crs_note": "NAD27 (KGS); source=" + w.get("LONG_LAT_SOURCE").astype("string").fillna(""), "status": w.get("STATUS2"),
            "spud_date": dt("SPUD"), "completion_date": dt("COMPLETION"), "td_ft": pd.to_numeric(w.get("DEPTH"), errors="coerce"),
            "extra": [json.dumps({"kid": k, "status_code": sc, "oil_lease_kid": o, "gas_lease_kid": g, "plug_date": p, "orig_operator": oo, "ip_oil": io_, "ip_gas": ig})
                      for k, sc, o, g, p, oo, io_, ig in zip(w["KID"], w.get("STATUS"), w.get("OIL_KID"), w.get("GAS_KID"), w.get("PLUGGING"), w.get("ORIG_OPERATOR"), w.get("IP_OIL"), w.get("IP_GAS"))],
        }).drop_duplicates("well_id", keep="last")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
