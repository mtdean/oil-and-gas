"""West Virginia DEP — annual production files (1985+) with monthly columns per well, plus well locations from the TAGIS REST service."""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

import pandas as pd

from ..http import download, get_json
from .base import Frame, RunContext, Source

PAGE = "https://dep.wv.gov/oil-and-gas/databaseinfo/Pages/default.aspx"
WELLS_LAYER = "https://tagis.dep.wv.gov/arcgis/rest/services/WVDEP_enterprise/oil_gas/MapServer/7/query"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

_XLRD_PATCHED = False


def _patch_xlrd():
    """WV DEP's 1985-2006 files are Excel 4.0 (BIFF4) worksheets with >16384 rows.
    xlrd caps BIFF<8 sheets at Excel 4's 16384-row limit and asserts when a cell
    lands beyond it, so lift the cap to the 16-bit row index the records actually use."""
    global _XLRD_PATCHED
    if _XLRD_PATCHED:
        return
    import xlrd.sheet
    orig = xlrd.sheet.Sheet.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.utter_max_rows = 65536

    xlrd.sheet.Sheet.__init__ = init
    _XLRD_PATCHED = True


class WestVirginia(Source):
    name = "wv"
    title = "West Virginia DEP annual well production (monthly columns) + wells"
    state = "WV"
    url = PAGE
    tables = ("production_monthly", "wells")
    cadence = "annual (spring/summer); quarterly horizontal updates"
    granularity = "well"
    history = "1985"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        r = ctx.sess.get(PAGE, timeout=60)
        r.raise_for_status()
        links = {urljoin(PAGE, l.replace(" ", "%20")) for l in re.findall(r'href="([^"]*ProductionReports/[^"]+\.(?:xlsx?|zip))"', r.text, re.I)}
        (self.raw / "links.json").write_text(json.dumps(sorted(links), indent=1))
        for url in sorted(links):
            if "H6A" in url:  # quarterly horizontal subsets duplicate the annual files; skip for now
                continue
            name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1].replace("%20", "_"))
            yr = re.search(r"(19\d{2}|20\d{2})", name)
            if not yr:
                continue
            y = int(yr.group(1))
            if ctx.limit_years and y < pd.Timestamp.today().year - ctx.limit_years:
                continue
            f = download(url, self.raw / f"{y}_{name}", sess=ctx.sess, force=ctx.force, immutable=y < pd.Timestamp.today().year - 3, max_age_hours=24 * 7, allow_404=True)
            if f.status != 404 and f.changed:
                changed.append(f.path)
        dest = self.raw / "wells.jsonl"
        if ctx.force or not dest.exists() or (time.time() - dest.stat().st_mtime) > 6 * 86400:
            tmp = dest.with_suffix(".part")
            offset, n = 0, 0
            with tmp.open("w") as out:
                while True:
                    js = get_json(WELLS_LAYER, sess=ctx.sess, params={"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json",
                                                                       "resultOffset": offset, "resultRecordCount": 2000})
                    feats = js.get("features", [])
                    for ft in feats:
                        a = ft["attributes"]
                        geom = ft.get("geometry") or {}
                        a["_lon"], a["_lat"] = geom.get("x"), geom.get("y")
                        out.write(json.dumps(a) + "\n")
                    n += len(feats)
                    offset += len(feats)
                    if not feats or not js.get("exceededTransferLimit", len(feats) == 2000):
                        break
                    time.sleep(0.5)
            tmp.replace(dest)
            self.log.info("wv wells: %d", n)
            changed.append(dest)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(p for p in self.raw.iterdir() if re.match(r"^\d{4}_", p.name)) if changed is None else [p for p in changed if re.match(r"^\d{4}_", p.name)]
        for p in files:
            df = self._prod(p)
            if not df.empty:
                yield "production_monthly", p.stem[:4], df
        if (changed is None or any(p.name == "wells.jsonl" for p in changed)) and (self.raw / "wells.jsonl").exists():
            yield "wells", "data", self._wells()

    def _read(self, path: Path) -> pd.DataFrame | None:
        try:
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as z:
                    name = next(n for n in z.namelist() if re.search(r"\.xlsx?$", n, re.I))
                    return self._read_excel(io.BytesIO(z.read(name)), name)
            return self._read_excel(path, path.name)
        except Exception as e:
            self.log.warning("cannot read %s: %r", path.name, e)
            return None

    def _read_excel(self, src, name: str) -> pd.DataFrame:
        if name.lower().endswith("x"):
            return pd.read_excel(src, dtype=str, engine="calamine")
        _patch_xlrd()
        import xlrd
        with open(os.devnull, "w") as devnull:
            kw = {"file_contents": src.getvalue()} if isinstance(src, io.BytesIO) else {"filename": str(src)}
            book = xlrd.open_workbook(logfile=devnull, **kw)
        return pd.read_excel(book, dtype=str, engine="xlrd")

    def _prod(self, path: Path) -> pd.DataFrame:
        raw = self._read(path)
        if raw is None:
            return pd.DataFrame()
        def norm(c: str) -> str:
            k = c.lower().strip()
            # older files: GAS_PRODUCTION_JANUARY / OIL_PRODUCTION_FEB ... -> jangas / feboil
            m = re.match(r"^(gas|oil|water|ngl)[_ ]?(?:production|prod)?[_ ]?([a-z]{3})[a-z]*$", k.replace("-", "_"))
            if m and m.group(2) in MONTHS:
                return m.group(2) + m.group(1)
            return re.sub(r"[^a-z0-9]", "", k)
        cols = {norm(c): c for c in raw.columns}
        api_c = next((cols[k] for k in ("api", "apinumber", "apinum", "permit", "apino") if k in cols), None)
        if api_c is None or not any(f"jan{k}" in cols for k in ("gas", "oil")):
            self.log.warning("%s: unrecognised layout %s", path.name, list(raw.columns)[:12])
            return pd.DataFrame()
        year = int(path.stem[:4])
        yr = pd.to_numeric(raw[cols["year"]], errors="coerce") if "year" in cols else year
        api = raw[api_c].astype("string").str.replace(r"\D", "", regex=True)
        api10 = api.str[:10].where(api.str.len() >= 10, ("47" + api.str.zfill(8)).where(api.str.len() <= 8))
        op = raw[cols["operator"]] if "operator" in cols else None
        wt = raw[cols["welltype"]] if "welltype" in cols else None
        rows = []
        for i, m in enumerate(MONTHS, 1):
            get = lambda k: pd.to_numeric(raw[cols[f"{m}{k}"]], errors="coerce") if f"{m}{k}" in cols else pd.NA
            rows.append(pd.DataFrame({
                "entity_id": api10, "entity_type": "well", "api14": api10 + "0000", "api10": api10, "state": "WV",
                "period": pd.to_datetime(dict(year=yr, month=i, day=1), errors="coerce"), "period_months": 1,
                "oil_bbl": get("oil"), "gas_mcf": get("gas"), "water_bbl": get("water"), "ngl_bbl": get("ngl"),
                "operator": op, "county": raw[cols["county"]] if "county" in cols else None,
                "extra": [json.dumps({"well_type": t}) for t in (wt if wt is not None else [None] * len(raw))],
            }))
        df = pd.concat(rows, ignore_index=True)
        df = df[df.api10.notna() & df.period.notna()]
        return df.drop_duplicates(["entity_id", "period"], keep="last")

    def _wells(self) -> pd.DataFrame:
        w = pd.read_json(self.raw / "wells.jsonl", lines=True, dtype=False)
        w.columns = [c.lower() for c in w.columns]
        api = w.get("api", w.get("permitid")).astype("string").str.replace(r"\D", "", regex=True)
        api10 = api.str[:10].where(api.str.len() >= 10, ("47" + api.str.zfill(8)).where(api.str.len() <= 8))
        ms = lambda c: pd.to_datetime(pd.to_numeric(w[c], errors="coerce"), unit="ms", errors="coerce") if c in w.columns else pd.NaT
        out = pd.DataFrame({
            "well_id": api10, "api10": api10, "api14": api10 + "0000", "state": "WV",
            "well_name": w.get("farmname"), "well_number": w.get("wellnumber"), "operator": w.get("respparty"), "county": w.get("county"),
            "formation": w.get("formation"), "lat": pd.to_numeric(w.get("_lat"), errors="coerce"), "lon": pd.to_numeric(w.get("_lon"), errors="coerce"),
            "crs_note": "WGS84 (reprojected by TAGIS REST outSR=4326)", "well_type": w.get("welltype"), "status": w.get("wellstatus"),
            "completion_date": ms("compdate"), "td_ft": pd.to_numeric(w.get("welldepth"), errors="coerce"),
            "extra": [json.dumps({"permit": p, "permittype": pt, "welluse": u, "marcellus": m}) for p, pt, u, m in zip(w.get("permit"), w.get("permittype"), w.get("welluse"), w.get("marcellus"))],
        }).dropna(subset=["well_id"]).drop_duplicates("well_id", keep="last")
        return out
