"""Kentucky — Division of Oil and Gas yearly production workbooks (monthly volumes per well, 1997+) + KGS well shapefile.

Production: https://eec.ky.gov/Natural-Resources/Oil-and-Gas/Resources/Pages/Production-Reports.aspx (current year(s))
and .../Archived-Production-Reports.aspx (1997-2023): one Oil and one Gas workbook per year, one row per well with
Jan..Dec columns, permit number, lat/lon, pool, formation. No API numbers in the workbooks, so wells are keyed by the
state permit: api10 = KGS API when the permit matches the KGS well shapefile (https://kgs.uky.edu/ogdata/kyog_dd.zip,
162k wells, weekly), else 'KYP<permit>'. Volumes are published after a one-year confidentiality period.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests

from ..http import download
from .base import Frame, RunContext, Source

PAGES = ["https://eec.ky.gov/Natural-Resources/Oil-and-Gas/Resources/Pages/Production-Reports.aspx",
         "https://eec.ky.gov/Natural-Resources/Oil-and-Gas/Resources/Pages/Archived-Production-Reports.aspx"]
WELLS_URL = "https://kgs.uky.edu/ogdata/kyog_dd.zip"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _norm(c) -> str:
    return re.sub(r"[^a-z0-9]", "", str(c).lower())


class Kentucky(Source):
    name = "ky"
    title = "Kentucky Division of Oil & Gas monthly well production (yearly workbooks) + KGS wells"
    state = "KY"
    url = PAGES[0]
    tables = ("production_monthly", "wells")
    cadence = "annual workbooks (new year each spring); wells weekly"
    granularity = "well"
    history = "1997"
    has_latlon = True
    notes = "Keyed by state permit (api10 = KGS API where matched, else KYP<permit>)."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        links = {}
        for page in PAGES:
            try:
                r = ctx.sess.get(page, timeout=120)
                r.raise_for_status()
            except requests.HTTPError as e:
                # eec.ky.gov answered 403 to the pipeline on 2026-11-20. One attempt, then manual: download the yearly
                # "<year> Oil/Gas Production" workbooks from the two pages into data/raw/ky/ as <year>_oil.xlsx / <year>_gas.xlsx
                self.log.warning("KY production page refused (%s); using workbooks already in %s", e.response.status_code if e.response is not None else e, self.raw)
                continue
            for href, text in re.findall(r'href="([^"]+\.xlsx?)"[^>]*>([^<]*)', r.text, re.I):
                m = re.search(r"(19|20)\d{2}", text or href)
                kind = "oil" if "oil" in (text + href).lower() else "gas" if "gas" in (text + href).lower() else None
                if m and kind:
                    url = href if href.startswith("http") else "https://eec.ky.gov" + href.replace(" ", "%20")
                    links[(int(m.group(0)), kind)] = url
        (self.raw / "links.json").write_text(json.dumps({f"{y}_{k}": u for (y, k), u in sorted(links.items())}, indent=1))
        today = pd.Timestamp.today().year
        for (yr, kind), url in sorted(links.items()):
            if ctx.limit_years and yr < today - ctx.limit_years:
                continue
            dest = self.raw / f"{yr}_{kind}{Path(url.split('?')[0]).suffix.lower()}"
            try:
                f = download(url, dest, sess=ctx.sess, force=ctx.force, immutable=yr < today - 2, max_age_hours=24 * 30, retries=0)
            except requests.HTTPError as e:
                self.log.warning("KY %s refused (%s); drop the file in manually as %s", url, e.response.status_code if e.response is not None else e, dest.name)
                continue
            if f.changed:
                changed.append(f.path)
        f = download(WELLS_URL, self.raw / "kyog_dd.zip", sess=ctx.sess, force=ctx.force, max_age_hours=24 * 14)
        if f.changed:
            changed.append(f.path)
        return changed

    def _read(self, path: Path) -> pd.DataFrame:
        df = pd.read_excel(path, sheet_name=0, dtype=str, engine="xlrd" if path.suffix == ".xls" else "openpyxl")
        df.columns = [_norm(c) for c in df.columns]
        return df

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(p for p in self.raw.glob("*_*.xls*") if re.match(r"\d{4}_(oil|gas)", p.name))
        years = {int(p.name[:4]) for p in files}
        if changed is not None:
            years &= {int(p.name[:4]) for p in changed if re.match(r"\d{4}_(oil|gas)", p.name)}
            if any(p.name == "kyog_dd.zip" for p in changed):
                years = {int(p.name[:4]) for p in files}  # permit->API mapping changed: rebuild all
        kgs = self._kgs()
        permit_api = kgs.set_index("permit")["api10"].to_dict()
        all_wells = []
        for yr in sorted(years):
            parts = []
            for p in [p for p in files if int(p.name[:4]) == yr]:
                kind = "oil" if "_oil" in p.name else "gas"
                df = self._read(p)
                col = lambda *c: next((x for x in c if x in df.columns), None)
                permit = df[col("permit", "permitnumber")].astype("string").str.replace(r"\.0$", "", regex=True).str.strip()
                vol_cols = {m: col(f"{m}{kind}", f"{m}{kind}bbl", f"{m}{kind}mcf", f"{m}") for m in MONTHS}
                long = []
                for i, m in enumerate(MONTHS, 1):
                    c = vol_cols[m]
                    if c is None:
                        continue
                    long.append(pd.DataFrame({"permit": permit, "month": i, "v": pd.to_numeric(df[c], errors="coerce")}))
                lg = pd.concat(long, ignore_index=True)
                lg["kind"] = kind
                meta = pd.DataFrame({"permit": permit, "operator": df[col("company", "operator")], "well_name": df[col("wellname", "leasenm", "leasename")],
                                     "well_no": df[col("wellno", "well")], "county": df[col("county")], "field": df[col("poolname")], "formation": df[col("formation")],
                                     "lat": pd.to_numeric(df[col("lat", "latitude")], errors="coerce"), "lon": pd.to_numeric(df[col("long", "longitude")], errors="coerce"), "year": yr})
                parts.append((lg, meta))
            if not parts:
                continue
            lg = pd.concat([p[0] for p in parts], ignore_index=True)
            meta = pd.concat([p[1] for p in parts], ignore_index=True).drop_duplicates("permit")
            all_wells.append(meta)
            piv = lg.pivot_table(index=["permit", "month"], columns="kind", values="v", aggfunc="sum").reset_index()
            for k in ("oil", "gas"):
                if k not in piv.columns:
                    piv[k] = pd.NA
            piv = piv.merge(meta[["permit", "operator", "well_name", "county", "field", "formation"]], on="permit", how="left")
            api10 = piv.permit.map(permit_api).fillna("KYP" + piv.permit)
            out = pd.DataFrame({
                "entity_id": "KYP" + piv.permit, "entity_type": "well", "api10": api10, "state": "KY",
                "period": pd.to_datetime(dict(year=yr, month=piv.month, day=1)), "oil_bbl": piv.oil, "gas_mcf": piv.gas,
                "operator": piv.operator, "well_name": piv.well_name, "county": piv.county, "field": piv.field, "formation": piv.formation,
                "extra": '{"permit": "' + piv.permit.astype(str) + '"}',
            })
            out = out[(out.oil_bbl.fillna(0) > 0) | (out.gas_mcf.fillna(0) > 0) | out.period.dt.month.eq(12)]  # keep zero months only once a year to mark the well as reporting
            self.log.info("KY %d: %s well-months, %s wells", yr, f"{len(out):,}", f"{out.entity_id.nunique():,}")
            yield "production_monthly", str(yr), out
        if changed is None or any(p.name == "kyog_dd.zip" for p in changed) or all_wells:
            yield "wells", "data", self._wells(kgs, pd.concat(all_wells, ignore_index=True) if all_wells else pd.DataFrame(), permit_api)

    def _kgs(self) -> pd.DataFrame:
        import shapefile
        with zipfile.ZipFile(self.raw / "kyog_dd.zip") as z:
            z.extractall(self.raw / "kgs")
        shp = next((self.raw / "kgs").glob("*.shp"))
        r = shapefile.Reader(str(shp))
        flds = [f[0] for f in r.fields[1:]]
        recs = pd.DataFrame([dict(zip(flds, rec)) for rec in r.iterRecords()])
        api = recs["API"].astype("string").str.replace(r"\D", "", regex=True)
        recs["api10"] = api.str[:10].where(api.str.len() >= 10)
        recs["permit"] = recs["KGS_Permit"].astype("string").str.replace(r"\.0$", "", regex=True).str.strip().replace({"": pd.NA})
        return recs

    def _wells(self, kgs: pd.DataFrame, prod_meta: pd.DataFrame, permit_api: dict) -> pd.DataFrame:
        api10 = kgs.api10.where(kgs.api10.notna(), "KYP" + kgs.permit.astype("string"))
        dt = lambda c: pd.to_datetime(kgs[c], errors="coerce", format="mixed")
        out = pd.DataFrame({
            "well_id": api10, "api10": api10, "state": "KY",
            "well_name": kgs["Org_Farm"].astype("string") + " " + kgs["Org_WellNo"].astype("string"), "operator": kgs["Org_Oper"], "county": kgs["County"].astype("string").str.title(),
            "formation": kgs["Deepst_Pay"], "lat": pd.to_numeric(kgs["Rec_Lat83"], errors="coerce"), "lon": pd.to_numeric(kgs["Rec_Lng83"], errors="coerce"),
            "crs_note": "NAD83 (KGS kyog_dd)", "well_type": kgs["Org_WClass"], "status": kgs["Org_Result"], "trajectory": kgs["bore_type"],
            "completion_date": dt("Cmpl_Date"), "td_ft": pd.to_numeric(kgs["TVD"], errors="coerce"),
            "extra": [json.dumps({"permit": None if pd.isna(p) else str(p), "kgs_recno": int(n), "plug_date": None if pd.isna(d) else str(d.date())}) for p, n, d in zip(kgs.permit, kgs["KGS_Recno"], dt("Plug_Date"))],
        })
        out = out[out.api10.notna() & (out.api10 != "KYP<NA>")]
        # wells that only appear in the production workbooks (permit not in the KGS extract) get the workbook coordinates
        if not prod_meta.empty:
            extra = prod_meta[~prod_meta.permit.isin(kgs.permit.dropna())].drop_duplicates("permit")
            extra = extra[extra.lat.notna()]
            out = pd.concat([out, pd.DataFrame({
                "well_id": "KYP" + extra.permit, "api10": "KYP" + extra.permit, "state": "KY", "well_name": extra.well_name.astype("string") + " " + extra.well_no.astype("string"),
                "operator": extra.operator, "county": extra.county, "field": extra.field, "formation": extra.formation, "lat": extra.lat, "lon": extra.lon,
                "crs_note": "as published in KY DOG production workbook", "extra": '{"permit": "' + extra.permit.astype(str) + '"}'})], ignore_index=True)
        out = out.drop_duplicates("api10")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        self.log.info("KY wells: %s, %s with coordinates", f"{len(out):,}", f"{out.lat.notna().sum():,}")
        return out
