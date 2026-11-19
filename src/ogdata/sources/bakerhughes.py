"""Baker Hughes North America rig count (weekly) — MANUAL download.

rigcount.bakerhughes.com blocks non-browser clients, so this connector does not
fetch. Download in a browser from https://rigcount.bakerhughes.com/na-rig-count :
  * "North America Rig Count Report" (weekly xlsx, 2024+)        -> data/raw/bakerhughes/current_report.xlsx
  * "North America Rig Count New Report (2013 - Aug 2025)" xlsx   -> data/raw/bakerhughes/history_2013_2025.xlsx
Then run `ogdata update bakerhughes --transform-only`. The 'NAM Weekly' sheet is
aggregated into US totals and by state / basin / DrillFor / trajectory / county.
Rig counts are also available automatically via EIA (eia:PET.E_ERTRR*, eia:STEO.RIGS*).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download  # noqa: F401 (kept for the disabled fetch path)
from .base import Frame, RunContext, Source

PAGE = "https://rigcount.bakerhughes.com/na-rig-count"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
HIST_URL = "https://rigcount.bakerhughes.com/static-files/e98bcf83-c458-4a88-8f35-4ac4d77628bb"  # 2013 - Aug 2025 (same layout)


class BakerHughes(Source):
    name = "bakerhughes"
    title = "Baker Hughes weekly North America rig count (US by state/basin/type)"
    url = PAGE
    tables = ("macro_series",)
    cadence = "weekly (Friday)"
    granularity = "n/a"
    history = "2013 (detail); older summary via EIA PET rigs series"
    status = "manual"
    notes = "Site blocks scripted downloads; drop the two xlsx files into data/raw/bakerhughes/ and run --transform-only."

    def fetch(self, ctx: RunContext) -> list[Path]:
        have = [p for p in ("current_report.xlsx", "history_2013_2025.xlsx") if (self.raw / p).exists()]
        if not have:
            self.log.warning("bakerhughes: no workbooks in %s — download manually (see module docstring)", self.raw)
        return []

    def _fetch_disabled(self, ctx: RunContext) -> list[Path]:
        changed = []
        r = ctx.sess.get(PAGE, headers=HEADERS, timeout=60)
        r.raise_for_status()
        links = re.findall(r'href="(/static-files/[0-9a-f-]+)"[^>]*title="([^"]*)"', r.text) + \
                re.findall(r'title="([^"]*)"[^>]*href="(/static-files/[0-9a-f-]+)"', r.text)
        current = None
        for a, b in links:
            href, title = (a, b) if a.startswith("/static-files/") else (b, a)
            if title.strip().endswith("North_America Rig_Count Report.xlsx"):
                current = "https://rigcount.bakerhughes.com" + href
                break
        if current is None:
            # fallback: any static-files link near 'Rig_Count Report'
            m = re.search(r'(/static-files/[0-9a-f-]+)[^<]{0,300}Rig_Count Report', r.text)
            if not m:
                raise RuntimeError("could not locate weekly rig count workbook link on page")
            current = "https://rigcount.bakerhughes.com" + m.group(1)
        f = download(current, self.raw / "current_report.xlsx", sess=ctx.sess, force=ctx.force, headers=HEADERS, max_age_hours=20)
        if f.changed:
            changed.append(f.path)
        f = download(HIST_URL, self.raw / "history_2013_2025.xlsx", sess=ctx.sess, force=ctx.force, headers=HEADERS, immutable=True)
        if f.changed:
            changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        frames = []
        for p in ("history_2013_2025.xlsx", "current_report.xlsx"):
            fp = self.raw / p
            if fp.exists():
                frames.append(self._weekly(fp))
        if not frames:
            return
        w = pd.concat(frames, ignore_index=True)
        w = w.drop_duplicates(["publish", "country", "county", "basin", "gom", "drillfor", "location", "state", "trajectory"], keep="last")
        us = w[w.country.str.upper() == "UNITED STATES"]
        out = []

        def agg(df, by, prefix, fmt):
            g = df.groupby(["publish"] + by, dropna=False)["rigs"].sum().reset_index()
            for _, r in g.iterrows():
                pass
            g["series_id"] = prefix + g[by].astype(str).agg(":".join, axis=1).str.lower().str.replace(" ", "_") if by else prefix
            g["name"] = fmt + (" " + g[by].astype(str).agg(" / ".join, axis=1) if by else "")
            g["region"] = g[by[0]] if by else "US"
            return g[["series_id", "name", "region", "publish", "rigs"]]

        out.append(agg(us, [], "bh:us:total", "US rig count"))
        out.append(agg(us, ["drillfor"], "bh:us:drillfor:", "US rigs by target"))
        out.append(agg(us, ["trajectory"], "bh:us:trajectory:", "US rigs by trajectory"))
        out.append(agg(us, ["location"], "bh:us:location:", "US rigs by location"))
        out.append(agg(us, ["state"], "bh:us:state:", "US rigs by state"))
        out.append(agg(us, ["basin"], "bh:us:basin:", "US rigs by basin"))
        out.append(agg(us, ["basin", "drillfor"], "bh:us:basin_drillfor:", "US rigs by basin & target"))
        out.append(agg(us, ["state", "county"], "bh:us:county:", "US rigs by county"))
        res = pd.concat(out, ignore_index=True).rename(columns={"publish": "period", "rigs": "value"})
        res["source"] = self.name
        res["units"], res["frequency"] = "rigs", "W"
        yield "macro_series", "data", res

    @staticmethod
    def _weekly(path: Path) -> pd.DataFrame:
        x = pd.ExcelFile(path, engine="calamine")
        sheet = next((s for s in x.sheet_names if "weekly" in s.lower()), x.sheet_names[-1])
        raw = pd.read_excel(x, sheet, header=None)
        hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip().str.lower() == "country"]
        if len(hdr) == 0:
            raise RuntimeError(f"no header row in {path.name}/{sheet}")
        h = hdr[0]
        df = raw.iloc[h + 1:].copy()
        df.columns = [str(c).strip().lower().replace("/province", "").replace(" ", "_") for c in raw.iloc[h]]
        df = df.rename(columns={"state": "state", "us_publishdate": "publish", "rig_count_value": "rigs"})
        df = df[df["country"].notna()]
        df["publish"] = pd.to_datetime(df["publish"], errors="coerce")
        df["rigs"] = pd.to_numeric(df["rigs"], errors="coerce")
        for c in ("country", "county", "basin", "gom", "drillfor", "location", "state", "trajectory"):
            if c not in df.columns:
                df[c] = None
            df[c] = df[c].astype("string").str.strip()
        return df[df.publish.notna()][["publish", "country", "county", "basin", "gom", "drillfor", "location", "state", "trajectory", "rigs"]]
