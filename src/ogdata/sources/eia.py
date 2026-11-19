"""EIA Open Data API v2 — prices, production by state, inventories, STEO forecasts.

Needs a free key: https://www.eia.gov/opendata/register.php -> EIA_API_KEY in .env
Each PULL is one route + facets; rows are normalised into macro_series with
series_id 'eia:<series>'. Pagination handled (5000 rows/page).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import get_json
from .base import Frame, RunContext, Source

BASE = "https://api.eia.gov/v2/"

# route, params, frequency, short label
PULLS = [
    # Spot prices (daily): WTI Cushing, Brent, and Henry Hub
    ("petroleum/pri/spt", {"frequency": "daily", "facets[series][]": ["RWTC", "RBRTE"]}, "D", "spot_crude"),
    ("natural-gas/pri/fut", {"frequency": "daily", "facets[series][]": ["RNGWHHD"]}, "D", "spot_hh"),
    # Crude oil production by area (US, PADDs, states) monthly, 1981+
    ("petroleum/crd/crpdn", {"frequency": "monthly"}, "M", "crude_prod"),
    # Natural gas: gross withdrawals (FGW) and marketed production (VGM) by state, monthly
    ("natural-gas/prod/sum", {"frequency": "monthly", "facets[process][]": ["FGW", "VGM"]}, "M", "gas_prod"),
    # Weekly US stocks: crude (ex SPR), SPR, total gasoline, distillate
    ("petroleum/stoc/wstk", {"frequency": "weekly", "facets[series][]": ["WCESTUS1", "WCSSTUS1", "WGTSTUS1", "WDISTUS1", "WCRSTUS1"]}, "W", "stocks"),
    # Weekly supply/disposition: field production, refinery inputs, exports, imports
    ("petroleum/sum/sndw", {"frequency": "weekly", "facets[series][]": ["WCRFPUS2", "WCRRIUS2", "WCREXUS2", "WCRIMUS2", "WGFRPUS2"]}, "W", "weekly_sd"),
    # Rig counts & footage drilled (from EIA's monthly drilling activity, sourced from Baker Hughes)
    ("petroleum/crd/drill", {"frequency": "monthly"}, "M", "drilling"),
    # Short-Term Energy Outlook key series (history + forecast)
    ("steo", {"frequency": "monthly", "facets[seriesId][]": [
        "COPRPUS", "NGPRPUS", "WTIPUUS", "BREPUUS", "NGHHMCF", "PATC_WORLD", "PAPR_WORLD", "COSXPUS", "NGMPPUS",
        "COPR_48", "COPR_PERM", "COPR_BAKK", "COPR_EFT", "NGPRPUS_PERM"]}, "M", "steo"),
]


def _norm(rows: list[dict], freq: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    sid = df.get("series", df.get("seriesId"))
    if sid is None:
        # composite id for routes without a series facet
        parts = [c for c in ("duoarea", "product", "process") if c in df.columns]
        sid = df[parts].astype(str).agg("-".join, axis=1)
    name = df.get("series-description", df.get("seriesDescription"))
    units = df.get("units", df.get("unit"))
    region = None
    for c in ("area-name", "stateDescription", "duoarea", "regionId"):
        if c in df.columns:
            region = df[c]
            break
    period = df["period"].astype(str)
    if freq == "M":
        period = pd.to_datetime(period, format="%Y-%m", errors="coerce")
    elif freq == "A":
        period = pd.to_datetime(period, format="%Y", errors="coerce")
    else:
        period = pd.to_datetime(period, errors="coerce")
    return pd.DataFrame({
        "series_id": "eia:" + sid.astype(str), "name": name, "units": units, "frequency": freq,
        "period": period, "value": pd.to_numeric(df["value"], errors="coerce"), "region": region,
    })


class EIA(Source):
    name = "eia"
    title = "EIA API v2: prices, production by state, stocks, rigs, STEO"
    url = "https://www.eia.gov/opendata/"
    tables = ("macro_series",)
    cadence = "daily/weekly/monthly by series"
    granularity = "n/a"
    history = "1981+ (monthly production), 1986+ (daily WTI)"
    requires_key = "EIA_API_KEY"

    def fetch(self, ctx: RunContext) -> list[Path]:
        key = os.environ["EIA_API_KEY"]
        changed = []
        for route, params, freq, label in PULLS:
            dest = self.raw / f"{label}.json"
            rows: list[dict] = []
            offset = 0
            while True:
                q = {"api_key": key, "data[0]": "value", "length": 5000, "offset": offset,
                     "sort[0][column]": "period", "sort[0][direction]": "asc", **params}
                js = get_json(BASE + route + "/data/", sess=ctx.sess, params=q)
                resp = js.get("response", {})
                page = resp.get("data", [])
                rows.extend(page)
                total = int(resp.get("total", 0) or 0)
                offset += len(page)
                if not page or offset >= total:
                    break
            new = json.dumps(rows, separators=(",", ":"))
            if ctx.force or not dest.exists() or dest.read_text() != new:
                dest.write_text(new)
                changed.append(dest)
            self.log.info("eia %s: %d rows", label, len(rows))
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        for route, params, freq, label in PULLS:
            p = self.raw / f"{label}.json"
            if not p.exists():
                continue
            if changed is not None and p not in changed:
                continue
            df = _norm(json.loads(p.read_text()), freq)
            if not df.empty:
                yield "macro_series", label, df
