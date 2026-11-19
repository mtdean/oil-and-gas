"""ONRR Natural Resources Revenue Data — federal & Native American production (national monthly; state/county annual)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pandas as pd

from ..http import download
from .base import Frame, RunContext, Source

BASE = "https://revenuedata.onrr.gov/downloads/"


class ONRR(Source):
    name = "onrr"
    title = "ONRR federal/Native American production (monthly national, annual county)"
    url = "https://revenuedata.onrr.gov/"
    tables = ("macro_series",)
    cadence = "monthly (~3 month lag)"
    granularity = "n/a"
    history = "2003"

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        for n in ("monthly_production.csv", "calendar_year_production.csv"):
            f = download(BASE + n, self.raw / n, sess=ctx.sess, force=ctx.force, max_age_hours=24 * 3)
            if f.changed:
                changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        m = pd.read_csv(self.raw / "monthly_production.csv", dtype=str)
        m.columns = [c.strip().lower().replace(" ", "_") for c in m.columns]
        slug = lambda s: s.astype("string").str.strip().str.lower().str.replace(r"[^a-z0-9]+", "_", regex=True).str.strip("_")
        mon = pd.DataFrame({
            "series_id": "onrr:monthly:" + slug(m["land_class"]) + ":" + slug(m["land_category"]) + ":" + slug(m["commodity"]),
            "name": "ONRR production — " + m["land_class"] + " / " + m["land_category"] + " / " + m["commodity"],
            "units": m["commodity"].str.extract(r"\(([^)]*)\)")[0], "frequency": "M",
            "period": pd.to_datetime(m["date"], errors="coerce", format="mixed"), "value": pd.to_numeric(m["volume"].str.replace(",", ""), errors="coerce"),
            "region": "US",
        })
        yield "macro_series", "monthly", mon[mon.period.notna()]
        c = pd.read_csv(self.raw / "calendar_year_production.csv", dtype=str)
        c.columns = [c_.strip().lower().replace(" ", "_") for c_ in c.columns]
        region = c["state"].fillna(c.get("offshore_region")).astype("string")
        county = c["county"].astype("string")
        cy = pd.DataFrame({
            "series_id": "onrr:cy:" + slug(c["land_class"]) + ":" + slug(c["land_category"]) + ":" + slug(c["product"]) + ":" + slug(region.fillna("")) + ":" + slug(county.fillna("")),
            "name": "ONRR annual production — " + c["product"] + " — " + region.fillna("") + " " + county.fillna(""),
            "units": c["product"].str.extract(r"\(([^)]*)\)")[0], "frequency": "A",
            "period": pd.to_datetime(c["calendar_year"], format="%Y", errors="coerce"), "value": pd.to_numeric(c["volume"].str.replace(",", ""), errors="coerce"),
            "region": region, "extra": '{"fips": "' + c["fips_code"].astype("string").fillna("") + '"}',
        })
        yield "macro_series", "calendar_year", cy[cy.period.notna()]
