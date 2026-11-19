"""Ohio ODNR — annual (all wells) and quarterly (horizontal) production xlsx files + wells with lat/lon from the ODNR ArcGIS Hub."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, quote

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

PAGE = "https://ohiodnr.gov/business-and-industry/energy-resources/well-information/production"
WELLS_CSV = "https://gis-odnr.opendata.arcgis.com/api/download/v1/items/e03ec46046af49c49d879ab9be07f980/csv?layers=3"
# ohiodnr.gov serves its HTML only to browsers, so instead of scraping the page we keep the
# known file list here (verified 2026-11-19). Add new files as ODNR posts them (names carry a date suffix).
DAM = "https://dam.assets.ohio.gov/raw/upload/ohiodnr.gov/documents/oil-gas/production/"
STATIC = "https://ohiodnr.gov/static/documents/oil-gas/production/"
FILES = [
    DAM + "2025_Horizontal_and_Vertical_Production_20260831.xlsx",
    DAM + "2024_Horizontal_and_Vertical_Production_07022025.xlsx",
    DAM + "2023_Horizontal_and_Vertical_Production_07022025.xlsx",
    DAM + "2022_Horizontal_and_Vertical_Production_1228223.xlsx",
    DAM + "2021_Horizontal_and_Vertical_Production_03202024.xlsx",
    STATIC + "2020_Horizontal_and_Vertical_Production_11172022.xlsx",
    STATIC + "2019_Horizontal_and_Vertical_Production_11172022.xlsx",
    STATIC + "2018_Horizontal_and_Vertical_Production_08292022.xlsx",
] + [STATIC + f"Combined%20Production%20{y}.xlsx" for y in range(2010, 2018)] + [
    DAM + "2026_AllQtrs_FINAL_20260820.xlsx",
]


class Ohio(Source):
    name = "oh"
    title = "Ohio ODNR production (annual all wells, quarterly horizontal) + wells"
    state = "OH"
    url = PAGE
    tables = ("production_monthly", "wells")
    cadence = "quarterly/annual files (URL list maintained by hand); wells weekly"
    granularity = "well (annual or quarterly totals)"
    history = "2010 (files online); 1984 on request"
    has_latlon = True
    notes = "Production rows are annual (period_months=12) or quarterly (3); no monthly data."

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        links = set(FILES)
        (self.raw / "links.json").write_text(json.dumps(sorted(links), indent=1))
        for url in sorted(links):
            name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1].replace("%20", "_"))
            dest = self.raw / name
            yr = re.search(r"(20\d{2}|19\d{2})", name)
            old = bool(yr) and int(yr.group(1)) < pd.Timestamp.today().year - 2
            f = download(url, dest, sess=ctx.sess, force=ctx.force, immutable=old, max_age_hours=24 * 7, allow_404=True)
            if f.status != 404 and f.changed:
                changed.append(f.path)
        # wells: Hub export job (202 until ready)
        dest = self.raw / "wells.csv"
        if ctx.force or not dest.exists() or (time.time() - dest.stat().st_mtime) > 6 * 86400:
            for attempt in range(20):
                r = ctx.sess.get(WELLS_CSV, timeout=300, allow_redirects=True)
                if r.status_code == 200 and not r.headers.get("Content-Type", "").startswith("application/json"):
                    dest.write_bytes(r.content)
                    changed.append(dest)
                    break
                time.sleep(10)
            else:
                self.log.warning("ODNR wells export never became ready")
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(p for p in self.raw.glob("*.xls*")) if changed is None else [p for p in changed if p.suffix.lower().startswith(".xls")]
        for p in files:
            df = self._prod(p)
            if not df.empty:
                yield "production_monthly", p.stem, df
        if (changed is None or any(p.name == "wells.csv" for p in changed)) and (self.raw / "wells.csv").exists():
            yield "wells", "data", self._wells()

    def _prod(self, path: Path) -> pd.DataFrame:
        try:
            raw = pd.read_excel(path, dtype=str, engine="calamine")
        except Exception as e:
            self.log.warning("cannot read %s: %s", path.name, e)
            return pd.DataFrame()
        cols = {re.sub(r"[^a-z]", "", c.lower()): c for c in raw.columns}
        cols = {("quarter" if k.startswith("quarter") else k): v for k, v in cols.items()}
        g = lambda *keys: next((raw[cols[k]] for k in keys if k in cols), None)
        api = g("apinumber", "api", "apiwellnumber")
        if api is None:
            return pd.DataFrame()
        api14 = api.astype("string").str.replace(r"\D", "", regex=True).str.ljust(14, "0")
        yr = pd.to_numeric(g("productionyear", "year"), errors="coerce")
        q = g("quarter")
        qn = pd.to_numeric(q, errors="coerce") if q is not None else pd.Series(pd.NA, index=raw.index)
        months = qn.map(lambda v: 3 if pd.notna(v) and 1 <= v <= 4 else 12)
        start_month = qn.map(lambda v: int((v - 1) * 3 + 1) if pd.notna(v) and 1 <= v <= 4 else 1)
        first = g("dateoffirstproduction")
        df = pd.DataFrame({
            "entity_id": api14, "entity_type": "well", "api14": api14, "api10": api14.str[:10], "state": "OH",
            "period": pd.to_datetime(dict(year=yr, month=start_month, day=1), errors="coerce"), "period_months": months,
            "oil_bbl": pd.to_numeric(g("oilbbls", "oil"), errors="coerce"), "gas_mcf": pd.to_numeric(g("gasmcf", "gas"), errors="coerce"),
            "water_bbl": pd.to_numeric(g("brinebbls", "brine", "water"), errors="coerce"), "days_on": pd.to_numeric(g("daysinproduction", "days"), errors="coerce"),
            "operator": g("ownername", "owner"), "well_name": g("wellnameandnumber", "wellname"), "county": g("county"),
            "extra": [json.dumps({"township": t, "first_production": str(f)[:10] if f is not None else None}) for t, f in zip(g("township") if g("township") is not None else [None] * len(raw), first if first is not None else [None] * len(raw))],
        })
        return df[df.period.notna()].drop_duplicates(["entity_id", "period", "period_months"], keep="last")

    def _wells(self) -> pd.DataFrame:
        w = pd.read_csv(self.raw / "wells.csv", dtype=str, low_memory=False)
        cols = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in w.columns}
        g = lambda *keys: next((w[cols[k]] for k in keys if k in cols), pd.Series([None] * len(w)))
        api = g("permitnumberapi", "api", "apinumber").astype("string").str.replace(r"\D", "", regex=True)
        api14 = api.str.ljust(14, "0").where(api.str.len() >= 10)
        lat = pd.to_numeric(g("welllatitudenad83", "latitude", "wellwhlat", "lat83"), errors="coerce")
        lon = pd.to_numeric(g("welllongitudenad83", "longitude", "wellwhlong", "long83"), errors="coerce")
        out = pd.DataFrame({
            "well_id": api14, "api14": api14, "api10": api14.str[:10], "state": "OH",
            "well_name": g("wellname"), "well_number": g("wellnumber"), "operator": g("company", "welloperator"), "county": g("county"),
            "formation": g("producingformation"), "lat": lat, "lon": lon, "crs_note": "NAD83",
            "well_type": g("welltype"), "status": g("wellstatus"), "trajectory": g("slant"),
            "completion_date": pd.to_datetime(g("welldatecomplete"), errors="coerce"), "td_ft": pd.to_numeric(g("welltotaldepth"), errors="coerce"),
            "extra": [json.dumps({"township": t, "utica": u, "marcellus": m, "last_nonzero_year": ly}) for t, u, m, ly in zip(g("township"), g("uticashale"), g("marcellusshale"), g("lastnonzeroproductionyear"))],
        }).dropna(subset=["well_id"]).drop_duplicates("well_id", keep="last")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
