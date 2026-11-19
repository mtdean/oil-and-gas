"""Colorado ECMC (formerly COGCC).

Production: one CSV per report year (1999+) with monthly rows per well x formation,
plus a rolling current-year file (monthly_prod.csv). Column names drift between
years (e.g. 2025 has 'GasSrinkage', ISO dates, literal NULLs) so we parse by name.
Wells: WELLS_SHP.ZIP (daily) with lat/lon, spud, depth, status.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd
import shapefile

from ..http import download
from .base import Frame, RunContext, Source

BASE = "https://ecmc.state.co.us/documents/data/downloads/"
START = 1999


def _norm(c: str) -> str:
    return c.strip().lower().replace("_", "").replace("gassrinkage", "gasshrinkage")


class Colorado(Source):
    name = "co"
    title = "Colorado ECMC monthly production by well/formation + well locations"
    state = "CO"
    url = "https://ecmc.colorado.gov/data-maps-reports/downloadable-data-documents"
    tables = ("production_monthly", "wells")
    cadence = "production monthly; wells daily"
    granularity = "well_formation"
    history = "1999-01"
    has_latlon = True

    def fetch(self, ctx: RunContext) -> list[Path]:
        changed = []
        y0 = date.today().year
        start = max(START, y0 - ctx.limit_years) if ctx.limit_years else START
        for y in range(start, y0):
            if y == 1999:
                url, dest = f"{BASE}production/1999_prod_reports.csv", self.raw / "1999_prod_reports.csv"
            else:
                url, dest = f"{BASE}production/{y}_prod_reports.zip", self.raw / f"{y}_prod_reports.zip"
            f = download(url, dest, sess=ctx.sess, force=ctx.force, immutable=y < y0 - 2, max_age_hours=24 * 7, allow_404=True)
            if f.status != 404 and f.changed:
                changed.append(f.path)
        f = download(f"{BASE}production/monthly_prod.csv", self.raw / "monthly_prod.csv", sess=ctx.sess, force=ctx.force, max_age_hours=20)
        if f.changed:
            changed.append(f.path)
        f = download(f"{BASE}gis/WELLS_SHP.ZIP", self.raw / "WELLS_SHP.ZIP", sess=ctx.sess, force=ctx.force, max_age_hours=20)
        if f.changed:
            changed.append(f.path)
        return changed

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        files = sorted(self.raw.glob("*_prod_reports.*")) + [self.raw / "monthly_prod.csv"] if changed is None else sorted(changed)
        for p in files:
            if p.name == "WELLS_SHP.ZIP":
                continue
            if not p.exists():
                continue
            yield "production_monthly", p.stem, self._prod(p)
        if changed is None or any(p.name == "WELLS_SHP.ZIP" for p in changed):
            yield "wells", "data", self._wells()

    def _prod(self, path: Path) -> pd.DataFrame:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
                raw = pd.read_csv(z.open(name), dtype=str, na_values=["NULL", ""], keep_default_na=True, low_memory=False, encoding_errors="replace")
        else:
            raw = pd.read_csv(path, dtype=str, na_values=["NULL", ""], low_memory=False, encoding_errors="replace")
        raw.columns = [_norm(c) for c in raw.columns]
        g = lambda c: raw[c] if c in raw.columns else pd.Series([pd.NA] * len(raw), dtype="string")
        num = lambda c: pd.to_numeric(g(c), errors="coerce")
        api10 = "05" + g("apicountycode").astype("string").str.zfill(3) + g("apisequencenumber").astype("string").str.zfill(5)
        sidetrack = g("apisidetrack").astype("string").fillna("00").str.zfill(2)
        api14 = api10 + sidetrack + "00"
        period = pd.to_datetime(dict(year=num("reportyear"), month=num("reportmonth"), day=1), errors="coerce")
        formation = g("formationcode").astype("string")
        df = pd.DataFrame({
            "entity_id": api14 + ":" + formation.fillna(""), "entity_type": "well_formation",
            "api14": api14, "api10": api10, "state": "CO", "period": period,
            "oil_bbl": num("oilproduced"), "gas_mcf": num("gasproduced"), "water_bbl": num("waterproduced"),
            "days_on": num("daysproduced"), "oil_sold_bbl": num("oilsales"), "gas_sold_mcf": num("gassales"),
            "gas_flared_mcf": num("flaredvented"),
            "operator": g("opname"), "well_name": g("well"), "formation": formation,
            "extra": None,
        })
        acc = pd.to_datetime(g("accepteddate"), errors="coerce", format="mixed")
        df["_acc"] = acc
        df["extra"] = [json.dumps({"facility_id": f, "well_status": s, "doc_num": d, "revised": r})
                       for f, s, d, r in zip(g("facilityid"), g("wellstatus"), g("docnum"), g("revised"))]
        df = df.sort_values("_acc").drop_duplicates(["entity_id", "period"], keep="last").drop(columns="_acc")
        return df[df.period.notna()]

    def _wells(self) -> pd.DataFrame:
        with zipfile.ZipFile(self.raw / "WELLS_SHP.ZIP") as z:
            names = {n.lower(): n for n in z.namelist()}
            base = next(n[:-4] for n in names if n.endswith(".dbf"))
            r = shapefile.Reader(dbf=io.BytesIO(z.read(names[base + ".dbf"])))
            fields = [f[0] for f in r.fields[1:]]
            df = pd.DataFrame([list(rec) for rec in r.iterRecords()], columns=fields)
        api8 = df["API_County"].astype(str).str.zfill(3) + df["API_Seq"].astype(str).str.zfill(5)
        api10 = "05" + api8
        dt = lambda c: pd.to_datetime(df[c], errors="coerce") if c in df.columns else pd.NaT
        out = pd.DataFrame({
            "well_id": api10, "api10": api10, "api14": api10 + "0000", "state": "CO",
            "well_name": df.get("Well_Name"), "well_number": df.get("Well_Num"), "operator": df.get("Operator"),
            "field": df.get("Field_Name"), "lat": pd.to_numeric(df["Latitude"], errors="coerce"), "lon": pd.to_numeric(df["Longitude"], errors="coerce"),
            "crs_note": "NAD83 as published", "well_type": df.get("Well_Class"), "status": df.get("Facil_Stat"),
            "trajectory": df.get("Citing_Typ"), "spud_date": dt("Spud_Date"), "td_ft": pd.to_numeric(df.get("Max_MD"), errors="coerce"),
            "extra": [json.dumps({"facility_id": fi, "api_label": al, "max_tvd": tv, "loc_qual": lq, "status_date": str(sd)})
                      for fi, al, tv, lq, sd in zip(df.get("Facil_Id"), df.get("API_Label"), df.get("Max_TVD"), df.get("Loc_Qual"), df.get("Stat_Date"))],
        })
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
