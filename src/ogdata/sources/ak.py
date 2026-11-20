"""Alaska AOGCC — well-level monthly production + well locations. MANUAL drop-in.

Both AOGCC entry points refuse automated clients (checked 2026-11-19): the Data page
https://www.commerce.alaska.gov/web/aogcc/Data answers HTTP 403 to anything that is
not a browser (also from other networks) and Data Miner https://aogweb.state.ak.us/DataMiner3/
resets the connection. We do not try to work around that. Instead:

  1. Open https://aogweb.state.ak.us/DataMiner3/ in a browser.
  2. Production page -> "CSV Production" (all wells; filter nothing) -> save as
     data/raw/ak/production.csv  (any name matching production*.csv works; several files are concatenated).
  3. Well info / Wells page -> CSV export -> save as data/raw/ak/wells.csv (wells*.csv).
  4. `ogdata update ak --transform-only`

UNTESTED against real exports: the column names below are matched fuzzily (API,
production month, oil/gas/water, days, operator, pool) and the log prints which
columns were picked, so check the first run's log. Alaska APIs are 14-digit and start
with 50. Production history in Data Miner starts in 1958 (Swanson River).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

import pandas as pd

from ..schema import api10_from, api14_from
from .base import Frame, RunContext, Source

DATA_MINER = "https://aogweb.state.ak.us/DataMiner3/"


def _find(cols, *patterns):
    norm = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in cols}
    for p in patterns:
        key = re.sub(r"[^a-z0-9]", "", p.lower())
        if key in norm:
            return norm[key]
    for p in patterns:
        key = re.sub(r"[^a-z0-9]", "", p.lower())
        for k, c in norm.items():
            if key in k:
                return c
    return None


def _read_many(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_csv(p, dtype=str, low_memory=False, encoding_errors="replace") for p in paths]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    df.columns = [str(c).strip() for c in df.columns]
    return df


class Alaska(Source):
    name = "ak"
    title = "Alaska AOGCC well-level monthly production + wells (Data Miner CSV exports)"
    state = "AK"
    url = DATA_MINER
    tables = ("production_monthly", "wells")
    cadence = "daily (Data Miner); manual export"
    granularity = "well"
    history = "1958"
    has_latlon = True
    status = "manual"
    notes = "AOGCC blocks scripted access (403 / connection reset). Export Production and Well CSVs from Data Miner into data/raw/ak/ and run --transform-only. Parser untested until files are present."

    def fetch(self, ctx: RunContext) -> list[Path]:
        prod, wells = sorted(self.raw.glob("production*.csv")), sorted(self.raw.glob("wells*.csv"))
        if not prod and not wells:
            self.log.warning("ak: no exports in %s. Open %s in a browser, export Production (CSV Production button) as production.csv "
                             "and the Well info table as wells.csv, then run `ogdata update ak --transform-only`", self.raw, DATA_MINER)
        return []

    def transform(self, ctx: RunContext, changed: list[Path] | None) -> Iterator[Frame]:
        prod = sorted(self.raw.glob("production*.csv"))
        if prod:
            df = self._prod(_read_many(prod))
            if not df.empty:
                yield "production_monthly", "data", df
        wells = sorted(self.raw.glob("wells*.csv"))
        if wells:
            w = self._wells(_read_many(wells))
            if not w.empty:
                yield "wells", "data", w

    def _period(self, raw: pd.DataFrame) -> pd.Series | None:
        cols = list(raw.columns)
        date_c = _find(cols, "ProductionDate", "Report Date", "ReportDate", "ProdDate", "Production Month", "ReportPeriod", "Period", "Date")
        if date_c and date_c.lower() not in ("year", "month"):
            d = pd.to_datetime(raw[date_c], errors="coerce", format="mixed")
            if d.notna().mean() > 0.5:
                return d.dt.to_period("M").dt.to_timestamp()
        y_c, m_c = _find(cols, "ReportYear", "ProdYear", "Year"), _find(cols, "ReportMonth", "ProdMonth", "Month")
        if y_c and m_c:
            y = pd.to_numeric(raw[y_c], errors="coerce")
            m = pd.to_numeric(raw[m_c], errors="coerce")
            if m.isna().all():  # month names
                m = raw[m_c].astype("string").str[:3].str.lower().map({n: i for i, n in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)})
            return pd.to_datetime(dict(year=y, month=m, day=1), errors="coerce")
        return None

    def _prod(self, raw: pd.DataFrame) -> pd.DataFrame:
        cols = list(raw.columns)
        api_c = _find(cols, "API", "APINumber", "API_Number", "PermitNo", "Permit")
        oil_c, gas_c, wat_c = _find(cols, "Oil", "OilBBL", "Oil (BBL)", "OilProduction"), _find(cols, "Gas", "GasMCF", "Gas (MCF)", "GasProduction"), _find(cols, "Water", "WaterBBL", "Water (BBL)")
        days_c = _find(cols, "Days", "ProductionDays", "DaysProduced", "Prod Days")
        name_c, op_c = _find(cols, "WellName", "Well Name", "Well"), _find(cols, "Operator", "OperatorName", "Company")
        pool_c, fld_c = _find(cols, "Pool", "Reservoir", "Formation", "Zone"), _find(cols, "Field", "FieldName")
        type_c = _find(cols, "ProductionType", "Prod Type", "WellStatus", "Status")
        period = self._period(raw)
        self.log.info("ak production columns: api=%s period=%s oil=%s gas=%s water=%s days=%s pool=%s (of %s)", api_c, period is not None, oil_c, gas_c, wat_c, days_c, pool_c, cols[:20])
        if api_c is None or period is None:
            self.log.warning("ak: could not identify API/period columns in production export; nothing loaded")
            return pd.DataFrame()
        api14 = api14_from(raw[api_c])
        num = lambda c: pd.to_numeric(raw[c].astype("string").str.replace(",", "", regex=False), errors="coerce") if c else pd.NA
        pool = raw[pool_c].astype("string").str.strip() if pool_c else None
        df = pd.DataFrame({
            "entity_id": api14 if pool is None else api14 + ":" + pool.fillna(""),
            "entity_type": "well" if pool is None else "well_formation", "api14": api14, "api10": api14.str[:10], "state": "AK",
            "period": period, "period_months": 1,
            "oil_bbl": num(oil_c), "gas_mcf": num(gas_c), "water_bbl": num(wat_c), "days_on": num(days_c),
            "operator": raw[op_c] if op_c else None, "well_name": raw[name_c] if name_c else None,
            "field": raw[fld_c] if fld_c else None, "formation": pool,
            "extra": [json.dumps({"production_type": t}) for t in (raw[type_c] if type_c else [None] * len(raw))],
        })
        df = df[df.api14.notna() & df.period.notna()]
        # Data Miner lists production and injection in one table; keep producing rows if a type column says so.
        if type_c:
            t = raw.loc[df.index, type_c].astype("string").str.lower()
            inj = t.str.contains("inj", na=False)
            df = df[~inj]
        return df.groupby(["entity_id", "period"], as_index=False).agg(
            {c: ("sum" if c in ("oil_bbl", "gas_mcf", "water_bbl") else ("max" if c == "days_on" else "last")) for c in df.columns if c not in ("entity_id", "period")})

    def _wells(self, w: pd.DataFrame) -> pd.DataFrame:
        cols = list(w.columns)
        api_c = _find(cols, "API", "APINumber", "API_Number", "PermitNo")
        lat_c, lon_c = _find(cols, "Latitude", "Lat", "SurfaceLatitude", "Surf Lat"), _find(cols, "Longitude", "Long", "Lon", "SurfaceLongitude", "Surf Long")
        self.log.info("ak wells columns: api=%s lat=%s lon=%s (of %s)", api_c, lat_c, lon_c, cols[:25])
        if api_c is None:
            self.log.warning("ak: no API column in wells export; nothing loaded")
            return pd.DataFrame()
        api14 = api14_from(w[api_c])
        get = lambda *p: (w[_find(cols, *p)] if _find(cols, *p) else None)
        dt = lambda *p: pd.to_datetime(get(*p), errors="coerce", format="mixed") if _find(cols, *p) else pd.NaT
        num = lambda *p: pd.to_numeric(get(*p), errors="coerce") if _find(cols, *p) else pd.NA
        out = pd.DataFrame({
            "well_id": api14, "api10": api14.str[:10], "api14": api14, "state": "AK",
            "well_name": get("WellName", "Well Name", "Well"), "operator": get("Operator", "OperatorName", "Company"),
            "field": get("Field", "FieldName"), "formation": get("Pool", "Reservoir", "Formation"),
            "lat": num("Latitude", "Lat", "SurfaceLatitude"), "lon": num("Longitude", "Long", "Lon", "SurfaceLongitude"),
            "crs_note": "as published by AOGCC Data Miner (NAD83 assumed)",
            "well_type": get("WellType", "Well Type", "Class"), "status": get("WellStatus", "Status"),
            "spud_date": dt("SpudDate", "Spud"), "completion_date": dt("CompletionDate", "Completion"),
            "td_ft": num("TotalDepth", "TD", "MeasuredDepth", "MD"),
            "extra": [json.dumps({"permit": p}) for p in (get("PermitNo", "Permit") if _find(cols, "PermitNo", "Permit") else [None] * len(w))],
        }).dropna(subset=["well_id"]).drop_duplicates("well_id", keep="last")
        out.loc[(out.lat == 0) | (out.lon == 0), ["lat", "lon"]] = pd.NA
        return out
