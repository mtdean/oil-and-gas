"""Canonical table schemas.

Every source is normalised into a small set of shared tables so analysis can
span states. Columns not provided by a source are null. Source-specific extras
are kept in the `extra` JSON column rather than widening the schema.

Granularity caveat: not every state reports production per well. `entity_type`
says what the row actually is: 'well' (API-level), 'lease' (TX oil, KS),
'luw' (LA lease-unit-well), 'pun' (OK production unit number), 'completion'.
"""
from __future__ import annotations

import pandas as pd

# table -> ordered {column: pandas/pyarrow-friendly dtype}
TABLES: dict[str, dict[str, str]] = {
    # One row per well (or per well-like entity) per source. Dedupe across sources via api14/api10.
    "wells": {
        "well_id": "string",        # source-native primary id (API14 where available)
        "api10": "string",          # 10-digit API (state+county+well), zero-padded, digits only
        "api14": "string",          # 14-digit API where known
        "state": "string",          # 2-letter postal code
        "source": "string",
        "well_name": "string",
        "well_number": "string",
        "operator": "string",
        "county": "string",
        "field": "string",
        "formation": "string",      # producing formation / pool / reservoir
        "lat": "Float64",           # WGS84 surface location (as published; see crs_note)
        "lon": "Float64",
        "crs_note": "string",
        "well_type": "string",      # OIL / GAS / INJ / SWD / ... as published
        "status": "string",
        "trajectory": "string",     # H / V / D (horizontal / vertical / directional)
        "spud_date": "datetime64[ns]",
        "completion_date": "datetime64[ns]",
        "first_prod_date": "datetime64[ns]",
        "last_prod_date": "datetime64[ns]",
        "td_ft": "Float64",         # total depth
        "lateral_ft": "Float64",
        "updated_at": "datetime64[ns]",
        "extra": "string",          # JSON
    },
    # Monthly production. One row per entity per month per source.
    "production_monthly": {
        "entity_id": "string",
        "entity_type": "string",    # well | well_formation | lease | luw | pun | completion
        "api14": "string",
        "api10": "string",
        "state": "string",
        "source": "string",
        "period": "datetime64[ns]", # first day of the reporting period
        "period_months": "Float64", # 1 = monthly (default). 3 = quarterly, 12 = annual totals (PA conventional, OH, KS annual rows)
        "oil_bbl": "Float64",
        "gas_mcf": "Float64",
        "water_bbl": "Float64",
        "ngl_bbl": "Float64",
        "days_on": "Float64",
        "oil_sold_bbl": "Float64",
        "gas_sold_mcf": "Float64",
        "gas_flared_mcf": "Float64",
        "operator": "string",
        "well_name": "string",
        "county": "string",
        "field": "string",
        "formation": "string",
        "extra": "string",
    },
    # Hydraulic fracturing / completion events (FracFocus etc.)
    "completions": {
        "api14": "string",
        "api10": "string",
        "state": "string",
        "source": "string",
        "job_start": "datetime64[ns]",
        "job_end": "datetime64[ns]",
        "operator": "string",
        "well_name": "string",
        "county": "string",
        "lat": "Float64",
        "lon": "Float64",
        "tvd_ft": "Float64",
        "base_water_gal": "Float64",
        "base_nonwater_gal": "Float64",
        "federal_well": "boolean",
        "indian_well": "boolean",
        "disclosure_id": "string",
        "extra": "string",
    },
    # Maps production entities (TX leases, LA LUWs, OK PUNs, BSEE leases) to the wells inside them.
    "entity_wells": {
        "entity_id": "string",
        "entity_type": "string",
        "api14": "string",
        "api10": "string",
        "state": "string",
        "source": "string",
        "well_number": "string",
        "valid_from": "datetime64[ns]",
        "valid_to": "datetime64[ns]",
        "extra": "string",
    },
    # Long-format macro time series (prices, production aggregates, rigs, inventories, spreads).
    "macro_series": {
        "series_id": "string",      # our stable id, e.g. 'eia:RWTC', 'fred:DCOILWTICO', 'bh:rigs_us_oil'
        "source": "string",
        "name": "string",
        "units": "string",
        "frequency": "string",      # D W M Q A
        "period": "datetime64[ns]",
        "value": "Float64",
        "region": "string",         # e.g. US, TX, Permian, PADD3
        "extra": "string",
    },
}


def conform(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Reorder/cast a DataFrame to the canonical schema, adding missing columns as null."""
    spec = TABLES[table]
    out = pd.DataFrame(index=df.index)
    for col, dtype in spec.items():
        if col in df.columns:
            s = df[col]
            try:
                if dtype.startswith("datetime"):
                    s = pd.to_datetime(s, errors="coerce")
                    if getattr(s.dt, "tz", None) is not None:
                        s = s.dt.tz_convert(None)
                    yr = s.dt.year
                    s = s.where((yr >= 1800) & (yr <= 2200))  # sentinel / typo dates (1083, 9999) -> NaT
                    s = s.astype("datetime64[ns]")
                elif dtype == "Float64":
                    s = pd.to_numeric(s, errors="coerce").astype("Float64")
                elif dtype == "boolean":
                    s = s.astype("boolean")
                else:
                    s = s.astype("string")
            except Exception as e:  # pragma: no cover
                raise ValueError(f"{table}.{col}: cannot cast to {dtype}: {e}") from e
            out[col] = s
        else:
            out[col] = pd.Series([pd.NA] * len(df), index=df.index, dtype=dtype if dtype != "datetime64[ns]" else "datetime64[ns]")
    return out


def api_digits(s: pd.Series) -> pd.Series:
    """Strip non-digits from an API number column -> string (NA for empty)."""
    s = s.astype("string").str.replace(r"\D", "", regex=True)
    return s.mask(s.str.len() == 0)


def api14_from(s: pd.Series) -> pd.Series:
    d = api_digits(s)
    return d.str.ljust(14, "0").where(d.str.len() <= 14, d.str[:14])


def api10_from(s: pd.Series) -> pd.Series:
    d = api_digits(s)
    return d.str[:10].where(d.str.len() >= 10)
