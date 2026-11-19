"""Dallas Fed Energy Survey — quarterly diffusion indexes (business activity, capex, employment, costs...) 2016Q1+."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

URL = "https://www.dallasfed.org/~/media/Documents/research/surveys/des/documents/all_data_qq.xlsx"


class DallasFed(Source):
    name = "dallasfed"
    title = "Dallas Fed Energy Survey indexes (quarterly)"
    url = "https://www.dallasfed.org/research/surveys/des/data"
    tables = ("macro_series",)
    cadence = "quarterly"
    granularity = "n/a"
    history = "2016-Q1"

    def fetch(self, ctx: RunContext) -> list[Path]:
        f = download(URL, self.raw / "all_data_qq.xlsx", sess=ctx.sess, force=ctx.force, max_age_hours=24 * 3)
        return [f.path] if f.changed else []

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        x = pd.ExcelFile(self.raw / "all_data_qq.xlsx", engine="calamine")
        frames = []
        for sheet in x.sheet_names:
            df = pd.read_excel(x, sheet)
            df.columns = [str(c).strip() for c in df.columns]
            date_col = df.columns[0]
            long = df.melt(id_vars=[date_col], var_name="code", value_name="value")
            long["period"] = pd.to_datetime(long[date_col], errors="coerce")
            long["value"] = pd.to_numeric(long["value"], errors="coerce")
            long = long.dropna(subset=["period", "value"])
            grp = sheet.strip().lower().replace(" ", "_").replace("+", "and")
            frames.append(pd.DataFrame({
                "series_id": "dfed:" + grp + ":" + long["code"].str.lower(), "name": f"Dallas Fed Energy Survey ({sheet}) {long['code'].iloc[0] if len(long) else ''}".replace(f" {long['code'].iloc[0] if len(long) else ''}", "") + " " + long["code"],
                "units": "diffusion index / % respondents", "frequency": "Q", "period": long["period"], "value": long["value"], "region": "Eleventh District",
            }))
        yield "macro_series", "data", pd.concat(frames, ignore_index=True)
