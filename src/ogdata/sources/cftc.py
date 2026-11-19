"""CFTC Commitments of Traders — disaggregated futures-only, energy contracts.

Yearly zips: https://www.cftc.gov/files/dea/history/fut_disagg_txt_YYYY.zip (2006+).
We keep crude, natural gas, and refined product markets and store each position
column as its own macro_series row: series_id 'cftc:<contract code>:<column>'.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

URL = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{y}.zip"
START = 2010
KEEP_MARKETS = ("CRUDE OIL", "NAT GAS", "NATURAL GAS", "GASOLINE", "HEATING OIL", "ULSD", "WTI", "BRENT", "PROPANE")
COLS = [
    "Open_Interest_All",
    "Prod_Merc_Positions_Long_All", "Prod_Merc_Positions_Short_All",
    "Swap_Positions_Long_All", "Swap__Positions_Short_All", "Swap__Positions_Spread_All",
    "M_Money_Positions_Long_All", "M_Money_Positions_Short_All", "M_Money_Positions_Spread_All",
    "Other_Rept_Positions_Long_All", "Other_Rept_Positions_Short_All", "Other_Rept_Positions_Spread_All",
    "Tot_Rept_Positions_Long_All", "Tot_Rept_Positions_Short_All",
    "NonRept_Positions_Long_All", "NonRept_Positions_Short_All",
    "Traders_Tot_All",
]


class CFTC(Source):
    name = "cftc"
    title = "CFTC Commitments of Traders (energy futures, disaggregated)"
    url = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm"
    tables = ("macro_series",)
    cadence = "weekly (Friday)"
    granularity = "n/a"
    history = "2010"

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        this_year = date.today().year
        start = max(START, this_year - ctx.limit_years) if ctx.limit_years else START
        for y in range(start, this_year + 1):
            dest = self.raw / f"fut_disagg_txt_{y}.zip"
            f = download(URL.format(y=y), dest, sess=ctx.sess, force=ctx.force,
                         immutable=y < this_year - 1, max_age_hours=20, allow_404=True)
            if f.status != 404 and f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("fut_disagg_txt_*.zip")) if changed is None else sorted(changed)
        for path in files:
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
                raw = pd.read_csv(io.BytesIO(z.read(name)), low_memory=False)
            raw.columns = [c.strip() for c in raw.columns]
            mkt = raw["Market_and_Exchange_Names"].str.upper()
            raw = raw[mkt.str.contains("|".join(KEEP_MARKETS), regex=True)]
            date_col = next(c for c in raw.columns if c.startswith("Report_Date_as"))
            raw = raw.copy()
            raw["period"] = pd.to_datetime(raw[date_col], errors="coerce", format="mixed")
            long = raw.melt(id_vars=["period", "CFTC_Contract_Market_Code", "Market_and_Exchange_Names"],
                            value_vars=[c for c in COLS if c in raw.columns], var_name="col", value_name="value")
            long["value"] = pd.to_numeric(long["value"], errors="coerce")
            df = pd.DataFrame({
                "series_id": "cftc:" + long["CFTC_Contract_Market_Code"].astype(str).str.strip() + ":" + long["col"],
                "name": long["Market_and_Exchange_Names"].str.strip() + " — " + long["col"].str.replace("_", " "),
                "units": "contracts", "frequency": "W", "period": long["period"], "value": long["value"],
                "region": long["Market_and_Exchange_Names"].str.strip(),
            })
            yield "macro_series", path.stem, df
