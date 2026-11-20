"""Canadian price benchmarks — Alberta Economic Dashboard API: WCS and WTI (US$/bbl), Alberta natural gas reference price (C$/GJ).

https://economicdashboard.alberta.ca/ (api.economicdata.alberta.ca). Monthly, WTI from 1986, WCS from 2005,
gas from 2010. The WCS-WTI differential is the key heavy-oil basis for Alberta/Saskatchewan economics.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

SERIES = {
    "ab_oil.json": ("https://api.economicdata.alberta.ca/data?table=OilPrices&Type=WCS;WTI",
                    {"WCS": ("ab:WCS", "Western Canadian Select, Hardisty", "$US/bbl"), "WTI": ("ab:WTI", "WTI (Alberta dashboard mirror)", "$US/bbl")}),
    "ab_gas.json": ("https://api.economicdata.alberta.ca/data?table=NaturalGasPrices&Type=NatGas",
                    {"NatGas": ("ab:GASREF", "Alberta natural gas reference price", "$CDN/GJ")}),
}


class CanadaMacro(Source):
    name = "camacro"
    title = "Canada benchmarks: WCS, Alberta gas reference price (Alberta Economic Dashboard)"
    url = "https://economicdashboard.alberta.ca/OilPrice"
    tables = ("macro_series",)
    cadence = "monthly"
    granularity = "n/a"
    history = "1986 (WTI), 2005 (WCS)"

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for name, (url, _) in SERIES.items():
            f = download(url, self.raw / name, sess=ctx.sess, force=ctx.force, max_age_hours=20)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        frames = []
        for name, (_, types) in SERIES.items():
            p = self.raw / name
            if not p.exists():
                continue
            rows = json.loads(p.read_text())
            df = pd.DataFrame(rows)
            df.columns = [c.strip() for c in df.columns]
            df["Type"] = df["Type"].astype(str).str.strip()
            for typ, (sid, label, units) in types.items():
                d = df[df.Type == typ]
                frames.append(pd.DataFrame({
                    "series_id": sid, "name": label, "units": units, "frequency": "M", "region": "AB",
                    "period": pd.to_datetime(d.Date, errors="coerce"), "value": pd.to_numeric(d.Value, errors="coerce"),
                }).dropna(subset=["period", "value"]))
        out = pd.concat(frames, ignore_index=True)
        # derived: WCS - WTI differential
        piv = out.pivot_table(index="period", columns="series_id", values="value")
        if {"ab:WCS", "ab:WTI"} <= set(piv.columns):
            diff = (piv["ab:WCS"] - piv["ab:WTI"]).dropna()
            out = pd.concat([out, pd.DataFrame({"series_id": "ab:WCS_WTI_DIFF", "name": "WCS minus WTI differential", "units": "$US/bbl", "frequency": "M", "region": "AB",
                                                "period": diff.index, "value": diff.values})], ignore_index=True)
        yield "macro_series", "data", out
