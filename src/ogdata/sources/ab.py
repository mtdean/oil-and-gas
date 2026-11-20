"""Alberta — Petrinex public volumetrics (well-level, 2022-01+) + AER ST37 well list with surface/bottom-hole lat/lon.

ST37 (https://www.aer.ca/data-and-performance-reports/statistical-reports/st37) is a free monthly Excel
package: bottom holes by UWI with licence + BH coordinates, surface holes by licence with SH coordinates,
and production strings with field/pool codes and status. Pool names come from Petrinex's public
PRAFieldandPoolCodes table. Pre-2022 production is only in paid AER products (Well Production Data).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd
import requests

from ..http import download
from . import petrinex
from .base import Frame, RunContext, Source

ST37_URL = "https://static.aer.ca/prd/documents/sts/st37/ST_37_Excel.zip"
POOLS_URL = "https://www.petrinex.gov.ab.ca/bbreports/PRAFieldandPoolCodes.csv"
M_TO_FT = 3.28084


def _xlsx(con: duckdb.DuckDBPyConnection, path: Path) -> pd.DataFrame:
    return con.execute(f"SELECT * FROM read_xlsx('{path}', all_varchar = true, header = true)").df()


class Alberta(Source):
    name = "ab"
    title = "Alberta: Petrinex public well volumetrics + AER ST37 well list"
    state = "AB"
    url = "https://www.petrinex.ca/public-data/apd/"
    tables = ("production_monthly", "wells")
    cadence = "monthly (Petrinex ~day 25 for month-2; ST37 monthly)"
    granularity = "well"
    history = "2022-01 (public Petrinex); wells 1949+"
    has_latlon = True
    notes = "UWI stored in api10. Oil includes condensate. Pre-2022 production is a paid AER product."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = petrinex.fetch_vol("AB", self.raw, ctx)
        for url, name, age in ((ST37_URL, "ST_37_Excel.zip", 24 * 7), (POOLS_URL, "PRAFieldandPoolCodes.csv", 24 * 30)):
            try:
                f = download(url, self.raw / name, sess=ctx.sess, force=ctx.force, max_age_hours=age, retries=0)
            except requests.HTTPError as e:
                # static.aer.ca sometimes refuses automated clients (403). Download ST_37_Excel.zip in a browser
                # from the ST37 page and drop it in data/raw/ab/; the transform uses whatever file is present.
                if (self.raw / name).exists():
                    self.log.warning("%s refused (%s); using the existing %s", url, e.response.status_code if e.response is not None else e, name)
                    continue
                raise
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        yield from petrinex.transform_vol("AB", self.raw, changed)
        if changed is None or any(p.name == "ST_37_Excel.zip" for p in changed) or any(p.name.startswith("PRAField") for p in changed):
            yield "wells", "data", self._wells()

    def _wells(self) -> pd.DataFrame:
        xdir = self.raw / "st37"
        with zipfile.ZipFile(self.raw / "ST_37_Excel.zip") as z:
            z.extractall(xdir)
        files = {p.name: p for p in xdir.rglob("*.xlsx")}
        bh_f = next(p for n, p in files.items() if "_BH" in n)
        sh_f = next(p for n, p in files.items() if "_SH" in n)
        ps_f = next(p for n, p in files.items() if "Production_Strings" in n)
        con = duckdb.connect()
        bh = _xlsx(con, bh_f)
        sh = _xlsx(con, sh_f)
        ps = _xlsx(con, ps_f)
        con.close()
        bh.columns = [c.strip() for c in bh.columns]
        sh.columns = [c.strip() for c in sh.columns]
        ps.columns = [c.strip() for c in ps.columns]
        self.log.info("ST37: %s bottom holes, %s surface holes, %s production strings", f"{len(bh):,}", f"{len(sh):,}", f"{len(ps):,}")

        lic = lambda s: s.astype("string").str.strip().str.lstrip("0")
        sh = sh.assign(lic=lic(sh.Well_Licence_Number)).drop_duplicates("lic")
        sh_lat = pd.to_numeric(sh.SH_Actual_Latitude, errors="coerce"); sh_lon = pd.to_numeric(sh.SH_Actual_Longitude, errors="coerce")
        sh_map = pd.DataFrame({"lic": sh.lic, "sh_lat": sh_lat.values, "sh_lon": sh_lon.values, "sh_label": sh.Licence_Surface_Location_Label.values})

        # one production string per well: prefer the string whose UWI equals the well UWI, else the first
        ps = ps.assign(same=(ps.Prod_String_UWI.str.strip() == ps.Well_UWI.str.strip())).sort_values(["Well_UWI", "same"], ascending=[True, False])
        ps1 = ps.drop_duplicates("Well_UWI")[["Well_UWI", "Field_Code", "Pool_Code", "Status_Fluid", "Status_Mode", "Status_Type", "Status_Structure", "Oil_Sands_Area", "Scheme_Type"]]
        pools = pd.read_csv(self.raw / "PRAFieldandPoolCodes.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        pools.columns = [c.strip() for c in pools.columns]
        pools["key"] = pools.FieldCode.str.strip().str.zfill(4) + ":" + pools.PoolCode.str.strip().str.zfill(7)
        pool_name = dict(zip(pools.key, pools.PoolName.str.strip()))
        field_name = dict(zip(pools.FieldCode.str.strip().str.zfill(4), pools.FieldName.str.strip()))
        pkey = ps1.Field_Code.astype("string").str.strip().str.zfill(4) + ":" + ps1.Pool_Code.astype("string").str.strip().str.zfill(7)
        ps1 = ps1.assign(pool_name=pkey.map(pool_name), field_name=ps1.Field_Code.astype("string").str.strip().str.zfill(4).map(field_name))

        w = bh.merge(ps1, on="Well_UWI", how="left")
        w["lic"] = lic(w.Well_Licence_Number)
        w = w.merge(sh_map, on="lic", how="left")
        uwi = petrinex.aer_to_petrinex_uwi(w.Well_UWI)
        bh_lat = pd.to_numeric(w.BH_Latitude, errors="coerce"); bh_lon = pd.to_numeric(w.BH_Longitude, errors="coerce")
        lat = w.sh_lat.where(w.sh_lat.notna(), bh_lat); lon = w.sh_lon.where(w.sh_lon.notna(), bh_lon)
        spud = pd.to_datetime(w.Spud_Date, errors="coerce", format="mixed")
        fdd = pd.to_datetime(w.Final_Drill_Date, errors="coerce", format="mixed")
        td_m = pd.to_numeric(w.Final_Total_Depth, errors="coerce"); tvd_m = pd.to_numeric(w.Max_True_Vertical_Depth, errors="coerce")
        lateral_ft = ((td_m - tvd_m) * M_TO_FT).where((tvd_m > 0) & (td_m - tvd_m > 300 * 0.3048))
        traj = pd.Series("V", index=w.index).mask(lateral_ft.notna(), "H")
        mode = w.Status_Mode.astype("string").str.strip()
        fluid = w.Status_Fluid.astype("string").str.strip()
        out = pd.DataFrame({
            "well_id": uwi, "api10": uwi, "state": "AB",
            "well_name": w.Well_Name.astype("string").str.strip(), "operator": w.Licensee.astype("string").str.strip(),
            "field": w.field_name, "formation": w.pool_name,
            "lat": lat, "lon": lon, "crs_note": "NAD83 (AER ST37); surface hole where licensed, else bottom hole",
            "well_type": fluid.replace({"Not Applicable": pd.NA}), "status": mode.replace({"Not Applicable": pd.NA}),
            "trajectory": traj, "spud_date": spud, "completion_date": fdd,
            "td_ft": td_m * M_TO_FT, "lateral_ft": lateral_ft,
            "extra": [json.dumps({"licence": l, "aer_uwi": u, "bh_lat": None if pd.isna(a) else round(float(a), 6), "bh_lon": None if pd.isna(o) else round(float(o), 6),
                                  "status_type": st, "scheme": sc, "oil_sands_area": osa, "licence_status": ls, "field_code": fc, "pool_code": pc})
                      for l, u, a, o, st, sc, osa, ls, fc, pc in zip(w.Well_Licence_Number, w.Well_UWI, bh_lat, bh_lon, w.Status_Type, w.Scheme_Type, w.Oil_Sands_Area, w.Licence_Status, w.Field_Code, w.Pool_Code)],
        })
        out = out[out.well_id.notna()].drop_duplicates("well_id")
        # Petrinex reports production per event sequence / production string (…W402, …W403). ST37's bottom-hole
        # file is per wellbore, so give every production string a row that inherits the wellbore's location.
        strings = pd.DataFrame({"sid": petrinex.aer_to_petrinex_uwi(ps.Prod_String_UWI), "wid": petrinex.aer_to_petrinex_uwi(ps.Well_UWI)}).dropna()
        strings = strings[(strings.sid != strings.wid) & ~strings.sid.isin(out.well_id)].drop_duplicates("sid")
        extra_rows = strings.merge(out, left_on="wid", right_on="well_id", how="inner")
        if len(extra_rows):
            extra_rows = extra_rows.assign(well_id=extra_rows.sid, api10=extra_rows.sid).drop(columns=["sid", "wid"])
            out = pd.concat([out, extra_rows[out.columns]], ignore_index=True)
        self.log.info("ST37 wells: %s with UWI (%s production-string aliases), %s with coordinates, %s with pool name", f"{len(out):,}", f"{len(extra_rows):,}", f"{out.lat.notna().sum():,}", f"{out.formation.notna().sum():,}")
        return out
