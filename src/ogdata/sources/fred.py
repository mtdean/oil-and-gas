"""FRED (St. Louis Fed) — commodity prices, rates, credit spreads.

Uses the keyless CSV endpoint (fredgraph.csv?id=...). Falls back to the JSON API
when FRED_API_KEY is set. All series land in macro_series as 'fred:<ID>'.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download, get_json
from .base import Frame, RunContext, Source

# id -> (name, units, frequency, region)
SERIES = {
    "DCOILWTICO": ("Crude Oil Prices: WTI - Cushing, Oklahoma", "$/bbl", "D", "US"),
    "DCOILBRENTEU": ("Crude Oil Prices: Brent - Europe", "$/bbl", "D", "EU"),
    "DHHNGSP": ("Henry Hub Natural Gas Spot Price", "$/MMBtu", "D", "US"),
    "MHHNGSP": ("Henry Hub Natural Gas Spot Price (monthly)", "$/MMBtu", "M", "US"),
    "GASREGW": ("US Regular All Formulations Gas Price", "$/gal", "W", "US"),
    "DGS2": ("2-Year Treasury Constant Maturity", "%", "D", "US"),
    "DGS10": ("10-Year Treasury Constant Maturity", "%", "D", "US"),
    "BAMLH0A0HYM2": ("ICE BofA US High Yield Index Option-Adjusted Spread", "%", "D", "US"),
    "BAMLH0A0HYM2EY": ("ICE BofA US High Yield Index Effective Yield", "%", "D", "US"),
    "BAMLC0A0CM": ("ICE BofA US Corporate Index Option-Adjusted Spread", "%", "D", "US"),
    "BAMLH0A3HYC": ("ICE BofA CCC & Lower US High Yield Index OAS", "%", "D", "US"),
    "DTWEXBGS": ("Nominal Broad US Dollar Index", "index", "D", "US"),
    "IPG211S": ("Industrial Production: Oil and Gas Extraction", "index 2017=100", "M", "US"),
    "PCU211211": ("PPI: Oil and Gas Extraction", "index", "M", "US"),
    "PCU213111213111": ("PPI: Drilling Oil and Gas Wells", "index", "M", "US"),
    "PCU213112213112": ("PPI: Support Activities for Oil and Gas Operations", "index", "M", "US"),
    "CES1021100001": ("All Employees: Oil and Gas Extraction", "thousands", "M", "US"),
}
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
API_URL = "https://api.stlouisfed.org/fred/series/observations"


class FRED(Source):
    name = "fred"
    title = "FRED prices, rates, credit spreads, energy PPI/employment"
    url = "https://fred.stlouisfed.org/"
    tables = ("macro_series",)
    cadence = "daily"
    granularity = "n/a"
    history = "1986 (WTI daily)"
    requires_key = "FRED_API_KEY"  # fred.stlouisfed.org CSV endpoint is bot-blocked from many hosts; the API is not

    def fetch(self, ctx: RunContext) -> list[Path]:
        import os
        changed = []
        key = os.environ.get("FRED_API_KEY")
        for sid in SERIES:
            dest = self.raw / f"{sid}.csv"
            if key:
                js = get_json(API_URL, sess=ctx.sess, params={"series_id": sid, "api_key": key, "file_type": "json"})
                df = pd.DataFrame(js["observations"])[["date", "value"]].rename(columns={"date": "observation_date", "value": sid})
                new = df.to_csv(index=False)
                if ctx.force or not dest.exists() or dest.read_text() != new:
                    dest.write_text(new)
                    changed.append(dest)
            else:
                f = download(CSV_URL.format(id=sid), dest, sess=ctx.sess, force=ctx.force, max_age_hours=12)
                if f.changed:
                    changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        frames = []
        for sid, (nm, units, freq, region) in SERIES.items():
            p = self.raw / f"{sid}.csv"
            if not p.exists():
                continue
            df = pd.read_csv(p)
            df.columns = ["period", "value"]
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna(subset=["value"])
            df["series_id"] = f"fred:{sid}"
            df["name"], df["units"], df["frequency"], df["region"] = nm, units, freq, region
            frames.append(df)
        yield "macro_series", "data", pd.concat(frames, ignore_index=True)
