"""Petrinex public data — shared reader for the Alberta and Saskatchewan Conventional Volumetric extracts.

https://www.petrinex.ca/public-data/ publishes one zip per province per production month
(API: https://www.petrinex.gov.ab.ca/publicdata/API/Files/{AB|SK}/Vol/{YYYY-MM}/CSV). Each file is the
complete volumetric submission for that month: every facility and well, every activity (PROD, INJ, FUEL,
FLARE, ...) and product, in metric units (m3 oil/water/condensate, e3m3 gas, hours on production).
Public history starts 2022-01; earlier months are only in the paid AER / SK Ministry products.

We keep the well-level production rows (ActivityID = PROD, FromToIDType = WI), pivot to one row per
well-month and convert to bbl / mcf / days so they sit beside the US data. Petrinex identifiers are UWIs
(16 chars, e.g. 100010206504W400 = survey 1, LE 00, LSD 01, Sec 02, Twp 065, Rge 04 W4, event 00); the
pipeline stores the UWI in `api10` so the analytics layer treats Canadian wells like any other wellbore.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

import pandas as pd

from ..http import download

log = logging.getLogger(__name__)

VOL_URL = "https://www.petrinex.gov.ab.ca/publicdata/API/Files/{prov}/Vol/{month}/CSV"
FIRST_MONTH = "2022-01"
RECHECK_MONTHS = 6          # the newest N production months are re-downloaded weekly (operators amend)
M3_TO_BBL = 6.28981
E3M3_TO_MCF = 35.3147
USECOLS = ["ProductionMonth", "OperatorName", "ReportingFacilityID", "ActivityID", "ProductID", "FromToIDType", "FromToIDIdentifier", "Volume", "Hours"]


def vol_path(raw: Path, prov: str, month: str) -> Path:
    return raw / "petrinex" / f"Vol_{month}-{prov}.zip"


def fetch_vol(prov: str, raw: Path, ctx) -> list[Path]:
    """Download every monthly volumetric zip we do not have yet; re-check the newest months weekly."""
    today = pd.Timestamp.today().to_period("M")
    months = pd.period_range(FIRST_MONTH, today - 1, freq="M")
    changed: list[Path] = []
    for m in months:
        recent = m >= today - RECHECK_MONTHS
        dest = vol_path(raw, prov, str(m))
        f = download(VOL_URL.format(prov=prov, month=m), dest, sess=ctx.sess, force=ctx.force, immutable=not recent,
                     max_age_hours=24 * 7 if recent else None, allow_404=True)
        if f.status == 404:
            continue  # not published yet (Petrinex releases a month ~3-4 weeks after it ends)
        if f.changed:
            changed.append(f.path)
    return changed


def _open_csv(path: Path) -> io.BytesIO:
    """The API zip wraps a second zip which wraps the CSV; tolerate either nesting."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        inner = next((n for n in names if n.lower().endswith(".zip")), None)
        if inner:
            with zipfile.ZipFile(io.BytesIO(z.read(inner))) as z2:
                csv = next(n for n in z2.namelist() if n.lower().endswith(".csv"))
                return io.BytesIO(z2.read(csv))
        csv = next(n for n in names if n.lower().endswith(".csv"))
        return io.BytesIO(z.read(csv))


def read_vol(path: Path, prov: str) -> pd.DataFrame:
    """One monthly file -> well-month production rows in the canonical production_monthly layout."""
    raw = pd.read_csv(_open_csv(path), usecols=USECOLS, dtype=str, keep_default_na=False, encoding="utf-8-sig", low_memory=False)
    raw = raw[(raw.ActivityID == "PROD") & (raw.FromToIDType == "WI") & raw.ProductID.isin(["OIL", "GAS", "WATER", "COND"])]
    if raw.empty:
        return pd.DataFrame()
    raw = raw.assign(Volume=pd.to_numeric(raw.Volume, errors="coerce").fillna(0.0), Hours=pd.to_numeric(raw.Hours, errors="coerce"))
    vol = raw.pivot_table(index=["FromToIDIdentifier", "ProductionMonth"], columns="ProductID", values="Volume", aggfunc="sum", fill_value=0.0)
    for c in ("OIL", "GAS", "WATER", "COND"):
        if c not in vol.columns:
            vol[c] = 0.0
    meta = raw.groupby(["FromToIDIdentifier", "ProductionMonth"]).agg(Hours=("Hours", "max"), OperatorName=("OperatorName", "first"), fac=("ReportingFacilityID", "first"))
    vol = vol.join(meta).reset_index()
    period = pd.to_datetime(vol.ProductionMonth + "-01", errors="coerce")
    days_in_month = period.dt.days_in_month
    days_on = (vol.Hours / 24.0).clip(upper=days_in_month)
    uwi = vol.FromToIDIdentifier.str.strip()
    return pd.DataFrame({
        "entity_id": uwi, "entity_type": "well", "api10": uwi, "state": prov, "period": period,
        "oil_bbl": (vol.OIL + vol.COND) * M3_TO_BBL, "gas_mcf": vol.GAS * E3M3_TO_MCF, "water_bbl": vol.WATER * M3_TO_BBL,
        "days_on": days_on, "operator": vol.OperatorName.str.strip(),
        "extra": '{"cond_bbl": ' + (vol.COND * M3_TO_BBL).round(1).astype(str) + ', "facility": "' + vol.fac.astype(str) + '"}',
    })


def transform_vol(prov: str, raw: Path, changed: list[Path] | None):
    """Yield ('production_monthly', year, df) for every year touched by `changed` (all years if None)."""
    files = sorted((raw / "petrinex").glob(f"Vol_*-{prov}.zip"))
    years = {int(p.stem.split("_")[1][:4]) for p in files}
    if changed is not None:
        years &= {int(p.stem.split("_")[1][:4]) for p in changed if p.name.startswith("Vol_")}
    for yr in sorted(years):
        parts = [read_vol(p, prov) for p in files if p.stem.split("_")[1].startswith(str(yr))]
        parts = [p for p in parts if not p.empty]
        if not parts:
            continue
        df = pd.concat(parts, ignore_index=True)
        df = df[df.period.notna()].drop_duplicates(["entity_id", "period"], keep="last")
        log.info("%s petrinex %d: %s well-months, %s wells", prov, yr, f"{len(df):,}", f"{df.entity_id.nunique():,}")
        yield "production_monthly", str(yr), df


_AER_UWI = re.compile(r"^(?P<le>[0-9A-Z]{2})/(?P<lsd>\d{2})-(?P<sec>\d{2})-(?P<twp>\d{3})-(?P<rge>\d{2})W(?P<mer>\d)/(?P<ev>[0-9A-Z]{1,2})$")


def aer_to_petrinex_uwi(s: pd.Series) -> pd.Series:
    """AER display UWI '00/08-09-051-25W4/0' -> Petrinex key '100080905125W400'."""
    m = s.astype("string").str.strip().str.extract(_AER_UWI)
    out = "1" + m["le"] + m["lsd"] + m["sec"] + m["twp"] + m["rge"] + "W" + m["mer"] + m["ev"].str.zfill(2)
    return out.where(m["le"].notna()).astype("string")


def norm_uwi(s: pd.Series) -> pd.Series:
    """Strip separators from any UWI presentation ('191/05-22-004-08W2/00' -> '191052200408W200')."""
    t = s.astype("string").str.upper().str.replace(r"[^0-9A-Z]", "", regex=True)
    return t.str.replace(r"^[A-Z]{2}WI(?=[12])", "", regex=True)  # SK/AB layers prefix the key with 'SKWI' / 'ABWI'
