"""Build the per-well analytics tables.

well_monthly  - one row per (api10, month): well-level and well+formation rows summed to the
                wellbore, plus Texas and Kansas lease volumes allocated to member wells
                (equal split across wells on the lease that month; `alloc` records how).
well_decline  - one row per api10: first/last production, cums, IP metrics, Arps fits for oil
                and gas (qi, Di, b), re-anchored EUR and remaining reserves, status.
type_curves   - P10/P50/P90/mean monthly volumes by month-since-first-production for
                state / state+county / state+formation groups and vintage buckets.
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from ..paths import PARQUET
from ..storage import connect
from . import decline

log = logging.getLogger("ogdata.analytics")
ADIR = PARQUET / "analytics"
T_MAX = 360           # months of history kept per well (30 years from first production)
HORIZON = 360         # forecast horizon from first production, months
ECON_OIL = 30.0       # bbl/month ~ 1 bbl/d
ECON_GAS = 300.0      # mcf/month ~ 10 mcf/d
CHUNK = 40_000


def _copy(con: duckdb.DuckDBPyConnection, sql: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    con.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    tmp.replace(out)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    log.info("wrote %s rows -> %s", f"{n:,}", out.relative_to(PARQUET))
    return n


# ----------------------------------------------------------------------------- well_monthly
WELL_MONTHLY_SQL = """
WITH direct AS (
    SELECT api10, state, period,
           sum(oil_bbl) AS oil_bbl, sum(gas_mcf) AS gas_mcf, sum(water_bbl) AS water_bbl, max(days_on) AS days_on,
           any_value(operator) AS operator, any_value(county) AS county,
           string_agg(DISTINCT formation, '|' ORDER BY formation) AS formation,
           'direct' AS alloc, 1 AS n_wells, any_value(source) AS source
    FROM production_monthly
    WHERE entity_type IN ('well', 'well_formation') AND api10 IS NOT NULL AND coalesce(period_months, 1) = 1
    GROUP BY api10, state, period
),
tx_members AS (
    SELECT entity_id, api10, count(*) OVER (PARTITION BY entity_id) AS n_wells
    FROM (SELECT DISTINCT entity_id, api10 FROM entity_wells
          WHERE source = 'tx' AND api10 IS NOT NULL
            AND coalesce(json_extract_string(extra, '$.well_shutin_dt'), '000000') = '000000')
),
tx_alloc AS (
    SELECT m.api10, 'TX' AS state, p.period,
           p.oil_bbl / m.n_wells AS oil_bbl, p.gas_mcf / m.n_wells AS gas_mcf, p.water_bbl / m.n_wells AS water_bbl, p.days_on,
           p.operator, p.county, p.field AS formation,
           CASE WHEN m.n_wells = 1 THEN 'single' ELSE 'lease_alloc' END AS alloc, m.n_wells, 'tx' AS source
    FROM production_monthly p JOIN tx_members m ON p.entity_id = m.entity_id
    WHERE p.state = 'TX' AND p.entity_type = 'lease'
      AND NOT EXISTS (SELECT 1 FROM direct d WHERE d.state = 'TX' AND d.api10 = m.api10 AND d.period = p.period)
),
ks_members AS (
    SELECT entity_id, api10, valid_from, valid_to FROM entity_wells WHERE source = 'ks' AND api10 IS NOT NULL
),
ks_alloc AS (
    SELECT m.api10, 'KS' AS state, p.period,
           p.oil_bbl / n.n_wells AS oil_bbl, p.gas_mcf / n.n_wells AS gas_mcf, p.water_bbl / n.n_wells AS water_bbl, p.days_on,
           p.operator, p.county, p.field AS formation,
           CASE WHEN n.n_wells = 1 THEN 'single' ELSE 'lease_alloc' END AS alloc, n.n_wells, 'ks' AS source
    FROM production_monthly p
    JOIN ks_members m ON p.entity_id = m.entity_id
         AND p.period >= coalesce(m.valid_from, '1900-01-01') AND p.period <= coalesce(m.valid_to, '2100-01-01')
    JOIN (SELECT p2.entity_id, p2.period, count(DISTINCT m2.api10) AS n_wells
          FROM (SELECT DISTINCT entity_id, period FROM production_monthly WHERE state = 'KS' AND entity_type = 'lease') p2
          JOIN ks_members m2 ON p2.entity_id = m2.entity_id
               AND p2.period >= coalesce(m2.valid_from, '1900-01-01') AND p2.period <= coalesce(m2.valid_to, '2100-01-01')
          GROUP BY 1, 2) n ON n.entity_id = p.entity_id AND n.period = p.period
    WHERE p.state = 'KS' AND p.entity_type = 'lease' AND coalesce(p.period_months, 1) = 1
)
SELECT * FROM direct
UNION ALL SELECT * FROM tx_alloc
UNION ALL SELECT * FROM ks_alloc
"""


def build_well_monthly(con: duckdb.DuckDBPyConnection) -> None:
    out = ADIR / "well_monthly"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.parquet"):
        old.unlink()
    con.execute(f"CREATE OR REPLACE TEMP TABLE wm AS {WELL_MONTHLY_SQL}")
    # a wellbore can be reported under two entity rows in the same month (e.g. TX gas well + oil lease); keep one row
    con.execute("""
        CREATE OR REPLACE TEMP TABLE wm2 AS
        SELECT api10, state, period, sum(oil_bbl) oil_bbl, sum(gas_mcf) gas_mcf, sum(water_bbl) water_bbl, max(days_on) days_on,
               any_value(operator) operator, any_value(county) county, any_value(formation) formation,
               min(alloc) alloc, max(n_wells) n_wells, any_value(source) source
        FROM wm GROUP BY api10, state, period
    """)
    states = [r[0] for r in con.execute("SELECT DISTINCT state FROM wm2 ORDER BY 1").fetchall()]
    for st in states:
        _copy(con, f"SELECT * FROM wm2 WHERE state = '{st}' ORDER BY api10, period", out / f"state={st}.parquet")
    con.execute("DROP TABLE wm; DROP TABLE wm2")


# ----------------------------------------------------------------------------- well_decline
def _state_arrays(con, st: str, lo: int, hi: int):
    """Return the well index frame and (N, T_MAX) oil/gas arrays for wells lo..hi (by row_number) in a state."""
    wells = con.execute(f"""
        WITH w AS (
            SELECT api10, min(period) FILTER (WHERE coalesce(oil_bbl,0)+coalesce(gas_mcf,0) > 0) AS first_prod,
                   max(period) FILTER (WHERE coalesce(oil_bbl,0)+coalesce(gas_mcf,0) > 0) AS last_prod,
                   max(period) AS last_report, count(*) AS months_reported,
                   sum(oil_bbl) AS cum_oil, sum(gas_mcf) AS cum_gas, sum(water_bbl) AS cum_water,
                   arg_max(operator, period) AS operator, arg_max(county, period) AS county, arg_max(formation, period) AS formation,
                   min(alloc) AS alloc, max(n_wells) AS n_wells, any_value(source) AS source
            FROM wm_state GROUP BY api10
        )
        SELECT * FROM (SELECT *, row_number() OVER (ORDER BY api10) - 1 AS rn FROM w) WHERE rn >= {lo} AND rn < {hi} AND first_prod IS NOT NULL
        ORDER BY rn
    """).df()
    if wells.empty:
        return wells, None, None
    con.register("sel", wells[["api10", "first_prod", "rn"]])
    rows = con.execute(f"""
        SELECT s.rn - {lo} AS i, datediff('month', s.first_prod, m.period) AS t, m.oil_bbl, m.gas_mcf
        FROM wm_state m JOIN sel s USING (api10)
        WHERE m.period >= s.first_prod AND datediff('month', s.first_prod, m.period) < {T_MAX}
    """).fetchnumpy()
    con.unregister("sel")
    N = len(wells)
    oil = np.full((N, T_MAX), np.nan, dtype=np.float32)
    gas = np.full((N, T_MAX), np.nan, dtype=np.float32)
    i = np.asarray(rows["i"], dtype=np.int64)
    t = np.asarray(rows["t"], dtype=np.int64)
    o = np.asarray(pd.array(rows["oil_bbl"]).astype("float64") if not isinstance(rows["oil_bbl"], np.ndarray) else rows["oil_bbl"], dtype=np.float64)
    g = np.asarray(pd.array(rows["gas_mcf"]).astype("float64") if not isinstance(rows["gas_mcf"], np.ndarray) else rows["gas_mcf"], dtype=np.float64)
    oil[i, t] = o
    gas[i, t] = g
    return wells, oil, gas


def _product_metrics(Q: np.ndarray, econ: float, prefix: str) -> dict[str, np.ndarray]:
    """Peak-aligned Arps fit plus IP / cum / EUR metrics for one product. Q is (N, T_MAX) monthly volumes."""
    N, T = Q.shape
    Qz = np.where(np.isfinite(Q), Q, 0.0)
    pos = Qz > 0
    cum = Qz.sum(1)
    n_pos = pos.sum(1)
    # peak within the first 12 producing-calendar months
    first12 = np.where(np.arange(T)[None, :] < 12, Qz, -1.0)
    peak = first12.argmax(1)
    peak_vol = Qz[np.arange(N), peak]
    # align on peak
    idx = peak[:, None] + np.arange(decline.MAX_FIT_MONTHS)[None, :]
    valid = idx < T
    aligned = np.where(valid, Q[np.arange(N)[:, None], np.minimum(idx, T - 1)], np.nan)
    fit = decline.fit_grid(aligned)
    # last positive month (relative to peak) and anchor = mean of last 3 positive months
    rel_pos = np.isfinite(aligned) & (aligned > 0)
    any_pos = rel_pos.any(1)
    t_last = np.where(any_pos, decline.MAX_FIT_MONTHS - 1 - np.argmax(rel_pos[:, ::-1], axis=1), 0)
    # mean of last 3 positive values
    order = np.where(rel_pos, np.arange(decline.MAX_FIT_MONTHS)[None, :], -1)
    top3 = np.sort(order, axis=1)[:, -3:]
    vals = np.where(top3 >= 0, aligned[np.arange(N)[:, None], np.maximum(top3, 0)], np.nan)
    q_anchor = np.nanmean(np.where(np.isfinite(vals), vals, np.nan), axis=1) if N else np.array([])
    # forecast remaining from anchor to HORIZON months after first production
    months_left = np.maximum(HORIZON - (peak + t_last + 1), 0)
    H = int(months_left.max()) if N else 0
    remaining = np.zeros(N)
    next12 = np.zeros(N)
    if H > 0 and fit.ok.any():
        fut = decline.forecast_from_anchor(np.where(fit.ok, q_anchor, 0.0), t_last, np.where(fit.ok, fit.di, 0.1), np.where(fit.ok, fit.b, 0.0), H)
        keep = (np.arange(H)[None, :] < months_left[:, None]) & (fut >= econ)
        fut = np.where(keep, fut, 0.0)
        remaining = np.nansum(fut, axis=1)
        next12 = np.nansum(fut[:, :12], axis=1)
    # IP metrics: first 3 producing months mean (per day) and first-12-calendar-month cum from first prod
    first3 = np.where(np.cumsum(pos, axis=1) <= 3, Qz, 0.0).sum(1) / np.maximum(np.minimum(n_pos, 3), 1)
    ip90_pd = np.where(n_pos > 0, first3 / 30.4375, np.nan)
    cum12 = np.where(np.arange(T)[None, :] < 12, Qz, 0.0).sum(1)
    cum12_peak = np.where(np.arange(decline.MAX_FIT_MONTHS)[None, :] < 12, np.where(np.isfinite(aligned), aligned, 0.0), 0.0).sum(1)
    d1 = np.where(fit.ok, 1 - decline.rate(1.0, fit.di, fit.b, 12.0) / 1.0, np.nan)  # first-year effective decline from peak
    return {
        f"{prefix}_cum": cum, f"{prefix}_months_producing": n_pos, f"{prefix}_peak_month": peak, f"{prefix}_peak_vol": peak_vol,
        f"{prefix}_ip90_pd": ip90_pd, f"{prefix}_cum12": cum12, f"{prefix}_cum12_peak": cum12_peak,
        f"{prefix}_qi": fit.qi, f"{prefix}_di": fit.di, f"{prefix}_b": fit.b, f"{prefix}_d1_eff": d1, f"{prefix}_fit_rmse_log": fit.rmse_log,
        f"{prefix}_fit_n": fit.n, f"{prefix}_fit_ok": fit.ok, f"{prefix}_t_last": peak + t_last, f"{prefix}_q_last3": q_anchor,
        f"{prefix}_remaining": np.where(fit.ok, remaining, np.nan), f"{prefix}_eur": np.where(fit.ok, cum + remaining, np.nan),
        f"{prefix}_next12": np.where(fit.ok, next12, np.nan),
    }


def build_well_decline(con: duckdb.DuckDBPyConnection) -> None:
    out = ADIR / "well_decline.parquet"
    frames = []
    states = sorted(p.stem.split("=")[1] for p in (ADIR / "well_monthly").glob("state=*.parquet"))
    for st in states:
        con.execute(f"CREATE OR REPLACE TEMP TABLE wm_state AS SELECT * FROM read_parquet('{ADIR / 'well_monthly' / f'state={st}.parquet'}')")
        n_wells = con.execute("SELECT count(DISTINCT api10) FROM wm_state").fetchone()[0]
        max_period = con.execute("SELECT max(period) FROM wm_state").fetchone()[0]
        for lo in range(0, n_wells, CHUNK):
            wells, oil, gas = _state_arrays(con, st, lo, lo + CHUNK)
            if wells.empty:
                continue
            m = {}
            m.update(_product_metrics(oil, ECON_OIL, "oil"))
            m.update(_product_metrics(gas, ECON_GAS, "gas"))
            df = pd.concat([wells.drop(columns=["rn"]).reset_index(drop=True), pd.DataFrame(m)], axis=1)
            df["boe_cum"] = df["oil_cum"] + df["gas_cum"] / 6
            df["boe_eur"] = df["oil_eur"].fillna(df["oil_cum"]) + df["gas_eur"].fillna(df["gas_cum"]) / 6
            df["primary_product"] = np.where(df["oil_cum"] >= df["gas_cum"] / 6, "oil", "gas")
            df["vintage"] = pd.to_datetime(df["first_prod"]).dt.year
            df["status"] = np.where(pd.to_datetime(df["last_prod"]) >= pd.Timestamp(max_period) - pd.DateOffset(months=3), "active", "inactive")
            df["state_max_period"] = pd.Timestamp(max_period)
            frames.append(df)
            log.info("%s decline: %d-%d of %d wells", st, lo, min(lo + CHUNK, n_wells), n_wells)
        con.execute("DROP TABLE wm_state")
    res = pd.concat(frames, ignore_index=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    res.to_parquet(out, index=False, compression="zstd")
    log.info("wrote %s wells -> %s", f"{len(res):,}", out.relative_to(PARQUET))


# ----------------------------------------------------------------------------- type_curves
VINTAGE_SQL = """CASE WHEN vintage < 2005 THEN 'pre-2005' WHEN vintage < 2010 THEN '2005-09' WHEN vintage < 2015 THEN '2010-14'
                      WHEN vintage < 2020 THEN '2015-19' WHEN vintage < 2023 THEN '2020-22' ELSE '2023+' END"""


def build_type_curves(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"CREATE OR REPLACE VIEW _wm AS SELECT * FROM read_parquet('{ADIR / 'well_monthly' / '*.parquet'}')")
    con.execute(f"CREATE OR REPLACE VIEW _wd AS SELECT * FROM read_parquet('{ADIR / 'well_decline.parquet'}')")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE norm AS
        SELECT m.api10, m.state, d.county, d.formation, {VINTAGE_SQL} AS vintage_bucket, d.alloc,
               datediff('month', d.first_prod, m.period) AS t, m.oil_bbl, m.gas_mcf
        FROM _wm m JOIN _wd d USING (api10, state)
        WHERE d.vintage >= 2000 AND m.period >= d.first_prod AND datediff('month', d.first_prod, m.period) < 120
    """)
    parts = []
    for gtype, key in (("state", "NULL"), ("state_county", "county"), ("state_formation", "formation")):
        for vint in (VINTAGE_SQL.replace("vintage", "vintage_bucket") if False else "vintage_bucket", "'all'"):
            parts.append(f"""
                SELECT '{gtype}' AS group_type, state, {key}::VARCHAR AS key, {vint} AS vintage, t,
                       count(*) AS n, count(DISTINCT api10) AS n_wells,
                       quantile_cont(oil_bbl, 0.1) oil_p10, quantile_cont(oil_bbl, 0.5) oil_p50, quantile_cont(oil_bbl, 0.9) oil_p90, avg(oil_bbl) oil_mean,
                       quantile_cont(gas_mcf, 0.1) gas_p10, quantile_cont(gas_mcf, 0.5) gas_p50, quantile_cont(gas_mcf, 0.9) gas_p90, avg(gas_mcf) gas_mean
                FROM norm WHERE alloc <> 'lease_alloc' {"AND " + key + " IS NOT NULL" if key != "NULL" else ""}
                GROUP BY ALL HAVING count(DISTINCT api10) >= 10
            """)
    _copy(con, " UNION ALL ".join(parts) + " ORDER BY group_type, state, key, vintage, t", ADIR / "type_curves.parquet")
    con.execute("DROP TABLE norm")


def build_all(which: tuple[str, ...] = ("well_monthly", "well_decline", "type_curves")) -> None:
    con = connect()
    try:
        con.execute("SET memory_limit='12GB'; SET threads=8; SET preserve_insertion_order=false")
        if "well_monthly" in which:
            build_well_monthly(con)
        if "well_decline" in which:
            build_well_decline(con)
        if "type_curves" in which:
            build_type_curves(con)
    finally:
        con.close()
