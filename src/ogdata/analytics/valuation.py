"""Per-well PDP valuation at the base price deck -> data/parquet/analytics/well_value.parquet and state_cashflow.parquet.

For every well with a usable decline fit: forecast 240 months from its last observed rate,
run the cash-flow engine with state default assumptions (no capex), and store PV10 of operating
cash flow, next-12-month cash flow and volumes, and remaining reserves. Also aggregates monthly
operating cash flow by state for the next 120 months at base / -25% / +25% price decks.
Costs are the module defaults; LOE is scaled down for low-rate (stripper) wells.
"""
from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

from ..paths import PARQUET
from . import decline, economics, prices
from .wellcurves import ADIR

log = logging.getLogger("ogdata.analytics.valuation")
H = 240
CHUNK = 20_000
DECKS = {"base": 1.0, "bear": 0.75, "bull": 1.25}


def _forecast(df: pd.DataFrame, prod: str, rng=None) -> np.ndarray:
    ok = df[f"{prod}_fit_ok"].values.astype(bool)
    q0 = np.where(ok, df[f"{prod}_q_last3"].values, 0.0)
    t_last = np.where(ok, df[f"{prod}_t_last"].values - df[f"{prod}_peak_month"].values, 0.0)
    di = np.where(ok, df[f"{prod}_di"].values, 0.1)
    b = np.where(ok, df[f"{prod}_b"].values, 0.0)
    f = decline.forecast_from_anchor(q0, t_last, di, b, H)
    econ = 30.0 if prod == "oil" else 300.0
    f = np.where(np.isfinite(f) & (f >= econ), f, 0.0)
    # months since last report: shift so the forecast starts at the state's current month
    gap = np.clip(df["gap_months"].values, 0, H - 1).astype(int)
    idx = np.arange(H)[None, :] + gap[:, None]
    f = np.where(idx < H, np.take_along_axis(np.pad(f, ((0, 0), (0, H)), constant_values=0.0), idx, axis=1), 0.0)
    return f


def build(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    if con is None:
        from ..storage import build_views
        con = duckdb.connect()
        build_views(con)
    deck = prices.load_deck(con, horizon=H)
    wd = con.execute("""
        SELECT api10, state, county, operator, formation, vintage, status, alloc, primary_product,
               oil_fit_ok, oil_q_last3, oil_t_last, oil_peak_month, oil_di, oil_b, oil_cum, oil_remaining, oil_ip90_pd,
               gas_fit_ok, gas_q_last3, gas_t_last, gas_peak_month, gas_di, gas_b, gas_cum, gas_remaining, gas_ip90_pd,
               datediff('month', last_prod, state_max_period) AS gap_months
        FROM well_decline WHERE status = 'active' AND (oil_fit_ok OR gas_fit_ok)
    """).df()
    log.info("valuing %s active wells", f"{len(wd):,}")
    out_frames = []
    state_cf = {}
    for st, g in wd.groupby("state"):
        base = economics.Assumptions(state=st, capex_mm=0.0).resolved()
        strip = economics.Assumptions(**{**base.__dict__, "loe_fixed_month": 2500.0, "loe_var_per_boe": 6.0}).resolved()
        for lo in range(0, len(g), CHUNK):
            d = g.iloc[lo:lo + CHUNK].reset_index(drop=True)
            oil = _forecast(d, "oil"); gas = _forecast(d, "gas")
            low_rate = (d.oil_ip90_pd.fillna(0).values < 50) & (d.gas_ip90_pd.fillna(0).values < 300)
            res = {}
            for name, mult in DECKS.items():
                po = deck.wti[None, :H] * mult; pg = deck.hh[None, :H] * mult
                cf_big = economics.cashflows(oil, gas, po, pg, base)
                cf_small = economics.cashflows(oil, gas, po, pg, strip)
                op = np.where(low_rate[:, None], cf_small.operating_cf, cf_big.operating_cf)
                res[name] = op
                sc = state_cf.setdefault((st, name), np.zeros(120))
                sc += op[:, :120].sum(0)
            op = res["base"]
            alive = op != 0
            frame = pd.DataFrame({
                "api10": d.api10, "state": st, "county": d.county, "operator": d.operator, "formation": d.formation, "vintage": d.vintage,
                "primary_product": d.primary_product, "alloc": d.alloc,
                "pv10_usd": economics.npv(op, 0.10), "pv15_usd": economics.npv(op, 0.15), "pv10_bear_usd": economics.npv(res["bear"], 0.10), "pv10_bull_usd": economics.npv(res["bull"], 0.10),
                "cf_next12_usd": op[:, :12].sum(1), "oil_next12_bbl": oil[:, :12].sum(1), "gas_next12_mcf": gas[:, :12].sum(1),
                "oil_remaining_bbl": oil.sum(1), "gas_remaining_mcf": gas.sum(1), "remaining_life_months": alive.sum(1),
                "oil_rate_bpd": oil[:, 0] / 30.4375, "gas_rate_mcfd": gas[:, 0] / 30.4375, "low_rate_cost_model": low_rate,
            })
            out_frames.append(frame)
        log.info("valued %s: %s wells", st, f"{len(g):,}")
    wv = pd.concat(out_frames, ignore_index=True)
    out = ADIR / "well_value.parquet"
    wv.to_parquet(out, index=False, compression="zstd")
    log.info("wrote %s wells -> %s", f"{len(wv):,}", out.relative_to(PARQUET))
    rows = []
    for (st, name), arr in state_cf.items():
        for i, v in enumerate(arr):
            rows.append({"state": st, "deck": name, "month": deck.months[i], "operating_cf_usd": float(v)})
    pd.DataFrame(rows).to_parquet(ADIR / "state_cashflow.parquet", index=False)
    pd.DataFrame({"month": deck.months[:H], "wti": deck.wti[:H], "hh": deck.hh[:H]}).assign(vol_oil=deck.vol_oil, vol_gas=deck.vol_gas, rho=deck.rho).to_parquet(ADIR / "price_deck.parquet", index=False)
    if own:
        con.close()
