"""British Columbia — BC Energy Regulator bulk downloads: full well-level monthly production (1954+) and the well file.

https://www.bc-er.ca/data-reports/data-centre/ -> iris.bcogc.ca/download/prod_csv.zip (zone-level monthly
production per completion event, refreshed ~1st of the month) and drill_csv.zip (wells, drilling events,
completion events, status, formation/pool code tables). Metric units converted to bbl/mcf. BC UWIs are
16 characters in either DLS (1...) or NTS (2...) form and are stored in `api10`.
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

PROD_URL = "https://iris.bcogc.ca/download/prod_csv.zip"
DRILL_URL = "https://iris.bcogc.ca/download/drill_csv.zip"
M3_TO_BBL = 6.28981
E3M3_TO_MCF = 35.3147
M_TO_FT = 3.28084


def _dms(s: pd.Series, deg_digits: int) -> pd.Series:
    """BCER packs coordinates as DDMMSSss / DDDMMSSss (seconds with two implied decimals)."""
    t = s.astype("string").str.strip().str.replace(r"\D", "", regex=True)
    ok = t.str.len() >= deg_digits + 6
    d = pd.to_numeric(t.str[:deg_digits], errors="coerce")
    m = pd.to_numeric(t.str[deg_digits:deg_digits + 2], errors="coerce")
    sec = pd.to_numeric(t.str[deg_digits + 2:deg_digits + 4] + "." + t.str[deg_digits + 4:], errors="coerce")
    return (d + m / 60 + sec / 3600).where(ok)


class BritishColumbia(Source):
    name = "bc"
    title = "British Columbia: BCER well production (1954+) + well file"
    state = "BC"
    url = "https://www.bc-er.ca/data-reports/data-centre/"
    tables = ("production_monthly", "wells")
    cadence = "monthly (~1st)"
    granularity = "well"
    history = "1954"
    has_latlon = True
    notes = "UWI stored in api10. Oil includes condensate. Zone-level rows summed to the wellbore."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for url, name in ((PROD_URL, "prod_csv.zip"), (DRILL_URL, "drill_csv.zip")):
            f = download(url, self.raw / name, sess=ctx.sess, force=ctx.force, max_age_hours=24 * 3)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        ddir = self.raw / "drill"
        with zipfile.ZipFile(self.raw / "drill_csv.zip") as z:
            z.extractall(ddir)
        codes = pd.read_csv(ddir / "codes.csv", skiprows=1, dtype=str, keep_default_na=False)
        codes.columns = [c.strip() for c in codes.columns]
        fm = codes[codes["Table ID"].str.strip() == "FORT"]
        fm_name = dict(zip(fm["Code"].str.strip(), fm["Description"].str.strip().str.title()))
        if changed is None or any(p.name == "prod_csv.zip" for p in changed):
            yield from self._production(fm_name)
        if changed is None or any(p.name == "drill_csv.zip" for p in changed):
            yield "wells", "data", self._wells(ddir, fm_name)

    def _production(self, fm_name: dict) -> Iterator[Frame]:
        pdir = self.raw / "prod"
        with zipfile.ZipFile(self.raw / "prod_csv.zip") as z:
            z.extract("zone_prd.csv", pdir)
        con = duckdb.connect()
        con.execute("SET threads=4")
        con.execute(f"""
            CREATE TEMP TABLE zp AS
            SELECT * FROM read_csv('{pdir / 'zone_prd.csv'}', skip=1, header=true, all_varchar=true, quote='"', ignore_errors=true)
        """)
        cols = [r[0] for r in con.execute("DESCRIBE zp").fetchall()]
        c = {k: next(x for x in cols if x.strip().lower().startswith(k)) for k in ("wa_num", "prod_period", "uwi", "formtn_code", "gas_prod_vol", "oil_prod_vol", "water_prod_vol", "cond_prod_vol", "prod_days")}
        q = lambda k: f'"{c[k]}"'
        df = con.execute(f"""
            WITH z AS (
                SELECT trim({q('uwi')}) AS uwi, trim({q('wa_num')}) AS wa, try_strptime(trim({q('prod_period')}) || '01', '%Y%m%d') AS period,
                       trim({q('formtn_code')}) AS fm,
                       try_cast({q('oil_prod_vol')} AS DOUBLE) oil, try_cast({q('gas_prod_vol')} AS DOUBLE) gas,
                       try_cast({q('water_prod_vol')} AS DOUBLE) water, try_cast({q('cond_prod_vol')} AS DOUBLE) cond, try_cast({q('prod_days')} AS DOUBLE) pdays
                FROM zp WHERE length(trim({q('uwi')})) >= 15)
            SELECT uwi, period, any_value(wa) wa, mode(fm) fm,
                   sum(coalesce(oil,0) + coalesce(cond,0)) * {M3_TO_BBL} AS oil_bbl, sum(coalesce(gas,0)) * {E3M3_TO_MCF} AS gas_mcf,
                   sum(coalesce(water,0)) * {M3_TO_BBL} AS water_bbl, sum(coalesce(cond,0)) * {M3_TO_BBL} AS cond_bbl, max(pdays) AS days_on, count(*) AS zones
            FROM z WHERE period IS NOT NULL GROUP BY 1, 2
        """).df()
        con.close()
        df["formation"] = df.fm.map(fm_name).fillna(df.fm)
        out = pd.DataFrame({
            "entity_id": df.uwi, "entity_type": "well", "api10": df.uwi, "state": "BC", "period": df.period,
            "oil_bbl": df.oil_bbl, "gas_mcf": df.gas_mcf, "water_bbl": df.water_bbl, "days_on": df.days_on, "formation": df.formation,
            "extra": '{"wa": "' + df.wa.astype(str) + '", "zones": ' + df.zones.astype(str) + ', "cond_bbl": ' + df.cond_bbl.round(1).astype(str) + "}",
        })
        self.log.info("BC production: %s well-months, %s wells, %s..%s", f"{len(out):,}", f"{out.entity_id.nunique():,}", out.period.min().date(), out.period.max().date())
        for yr, part in out.groupby(out.period.dt.year):
            yield "production_monthly", str(yr), part

    def _wells(self, ddir: Path, fm_name: dict) -> pd.DataFrame:
        rd = lambda n: pd.read_csv(ddir / n, skiprows=1, dtype=str, keep_default_na=False, encoding_errors="replace", on_bad_lines="skip")
        wells = rd("wells.csv"); wells.columns = [c.strip() for c in wells.columns]
        dev = rd("drill_ev.csv"); dev.columns = [c.strip() for c in dev.columns]
        wst = rd("w_status.csv"); wst.columns = [c.strip() for c in wst.columns]
        cev = rd("compl_ev.csv"); cev.columns = [c.strip() for c in cev.columns]
        wa = lambda s: s.astype("string").str.strip().str.zfill(5)
        wells["wa"] = wa(wells["WA Num"]); dev["wa"] = wa(dev["WA NUM"]); wst["wa"] = wa(wst["Wa Num"]); cev["wa"] = wa(cev["Wa_num"])
        wst = wst.sort_values(["wa", "Status Eff Date"]).drop_duplicates("wa", keep="last")
        cev = cev.sort_values(["wa", "Compltn_date"]).drop_duplicates("wa", keep="last")
        dev = dev[dev["UWI"].str.strip().str.len() >= 15].sort_values(["wa", "Drilling Event"]).drop_duplicates("UWI")
        w = dev.merge(wells, on="wa", how="left").merge(wst[["wa", "Mode Code", "Fluid Type", "Ops Type"]], on="wa", how="left").merge(cev[["wa", "Formtn_code", "Compltn_date"]], on="wa", how="left")
        lat = _dms(w["Surf Nad83 Lat"], 2); lon = -_dms(w["Surf Nad83 Long"], 3)
        td_m = pd.to_numeric(w["Td Depth (m)"], errors="coerce")
        traj = w["Directional Flag"].astype("string").str.strip().str.upper().map({"Y": "D", "N": "V"})
        traj = traj.mask(w["Lahee Type"].astype("string").str.contains("HOR", case=False, na=False), "H")
        uwi = w["UWI"].astype("string").str.strip()
        out = pd.DataFrame({
            "well_id": uwi, "api10": uwi, "state": "BC",
            "well_name": w["Well Name"].astype("string").str.replace(r"\s+", " ", regex=True).str.strip(), "operator": w["Oper Abbrev"],
            "field": w["Well Area Name"], "formation": w["Formtn_code"].map(fm_name),
            "lat": lat, "lon": lon, "crs_note": "NAD83 surface (BCER wells.csv, DMS unpacked)",
            "well_type": w["Fluid Type"], "status": w["Mode Code"], "trajectory": traj,
            "spud_date": pd.to_datetime(w["Spud Date"], errors="coerce", format="%Y%m%d"),
            "completion_date": pd.to_datetime(w["Compltn_date"], errors="coerce", format="%Y%m%d"),
            "td_ft": td_m * M_TO_FT,
            "extra": [json.dumps({"wa": a, "lahee": l, "class": c, "ops_type": o, "oper_id": oi}) for a, l, c, o, oi in zip(w.wa, w["Lahee Type"], w["Classfctn Code"], w["Ops Type"], w["Oper Id"])],
        })
        out = out[out.well_id.notna()].drop_duplicates("well_id")
        self.log.info("BC wells: %s wellbores, %s with coordinates", f"{len(out):,}", f"{out.lat.notna().sum():,}")
        return out
