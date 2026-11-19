"""EIA bulk files (no API key): PET.zip, NG.zip, STEO.zip from https://www.eia.gov/opendata/bulkfiles.php

Each zip holds one newline-delimited JSON file; each line is a series object
{series_id, name, units, f, data:[[period, value],...]}. We keep a curated set of
series (prices, production by state, stocks, rigs, STEO drilling/DUC/forecasts)
and write them to macro_series as 'eia:<series_id>'. Updated twice daily by EIA.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

FILES = {
    "PET": "https://api.eia.gov/bulk/PET.zip",
    "NG": "https://api.eia.gov/bulk/NG.zip",
    "STEO": "https://api.eia.gov/bulk/STEO.zip",
}
KEEP = re.compile(
    r"^(PET\.(RWTC|RBRTE|EER_EPMRU_PF4_Y35NY_DPG|EER_EPD2DXL0_PF4_Y35NY_DPG)\.D"          # WTI, Brent, NY gasoline, NY ULSD spot
    r"|PET\.MCRFP[A-Z0-9]{2,4}[12]\.M"                                                      # crude production by area, monthly
    r"|PET\.(WCESTUS1|WCSSTUS1|WGTSTUS1|WDISTUS1|WCRSTUS1|WTTSTUS1|WCRFPUS2|WCRRIUS2|WCREXUS2|WCRIMUS2|WGFUPUS2|WPULEUS3|WGIRIUS2)\.W"  # weekly
    r"|PET\.E_ERTRR[A-Z0-9_]*\.M"                                                            # rigs / footage drilled (from Baker Hughes)
    r"|PET\.(MCRFPUS1|MCRFPUS2|MTTUPUS2|MGFUPUS2)\.A"                                         # annual aggregates
    r"|NG\.RNGWHHD\.D"                                                                        # Henry Hub daily
    r"|NG\.N90[15]0[A-Z]{2}2\.M"                                                              # gas gross withdrawals / marketed by state
    r"|NG\.N5070US2\.W|NG\.NW2_EPG0_SWO_R48_BCF\.W"                                           # weekly storage
    r"|STEO\.(COPRPUS|NGPRPUS|WTIPUUS|BREPUUS|NGHHMCF|NGHHUUS|PATC_WORLD|PAPR_WORLD|COSXPUS|NGMPPUS|PASC_US|COPR_[A-Z0-9]+|RIGS[A-Z0-9]+|DUCS[A-Z0-9]+|NWD[A-Z0-9]+|NWC[A-Z0-9]+|CONW[A-Z0-9]+|COEOP[A-Z0-9]+|NGNW[A-Z0-9]+|TOPR[A-Z0-9]+|SNGPR[A-Z0-9]+)\.M"
    r")$"
)


def _period(p: str, f: str):
    if f == "D" or f == "W" or f == "4":
        return pd.to_datetime(p, format="%Y%m%d", errors="coerce")
    if f == "M":
        return pd.to_datetime(p, format="%Y%m", errors="coerce")
    if f == "Q":
        return pd.Period(p.replace("Q", "Q"), freq="Q").start_time if re.match(r"^\d{4}Q[1-4]$", p) else pd.NaT
    if f == "A":
        return pd.to_datetime(p, format="%Y", errors="coerce")
    return pd.to_datetime(p, errors="coerce")


class EIABulk(Source):
    name = "eia_bulk"
    title = "EIA bulk download (keyless): prices, production by state, stocks, rigs, STEO"
    url = "https://www.eia.gov/opendata/bulkfiles.php"
    tables = ("macro_series",)
    cadence = "twice daily"
    granularity = "n/a"
    history = "1920+ monthly crude; 1986 daily WTI; 1973 gas"

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for k, url in FILES.items():
            f = download(url, self.raw / f"{k}.zip", sess=ctx.sess, force=ctx.force, max_age_hours=12)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("*.zip")) if changed is None else sorted(changed)
        for zp in files:
            frames = []
            with zipfile.ZipFile(zp) as z:
                name = z.namelist()[0]
                with z.open(name) as fh:
                    for line in fh:
                        if b'"series_id"' not in line[:200]:
                            continue
                        # cheap pre-filter before full json parse
                        m = re.search(rb'"series_id"\s*:\s*"([^"]+)"', line[:300])
                        if not m or not KEEP.match(m.group(1).decode()):
                            continue
                        obj = json.loads(line)
                        sid, f = obj["series_id"], obj.get("f", "")
                        data = obj.get("data") or []
                        if not data:
                            continue
                        df = pd.DataFrame(data, columns=["p", "value"])
                        df["period"] = [_period(str(p), f) for p in df["p"]]
                        frames.append(pd.DataFrame({
                            "series_id": "eia:" + sid, "name": obj.get("name"), "units": obj.get("units"), "frequency": f,
                            "period": df["period"], "value": pd.to_numeric(df["value"], errors="coerce"),
                            "region": obj.get("geography") or obj.get("geography2"),
                            "extra": json.dumps({"updated": obj.get("updated"), "last_updated": obj.get("last_updated"), "copyright": obj.get("copyright")}),
                        }))
            if frames:
                out = pd.concat(frames, ignore_index=True)
                self.log.info("%s: %d series, %s rows", zp.stem, len(frames), f"{len(out):,}")
                yield "macro_series", zp.stem, out
