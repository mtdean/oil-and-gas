"""Location-based production forecast with Monte Carlo economics.

Given a lat/lon (or an existing API10):
1. pick analog wells from well_decline x well_locations within a radius (expanding until enough),
   optionally filtered by vintage / formation / product;
2. build P10/P50/P90/mean type curves from the analogs' actual monthly history, extended to the
   full horizon with Arps fits of those percentile curves;
3. Monte Carlo: each trial samples an analog (distance-weighted), perturbs its fitted Arps
   parameters, draws a correlated oil/gas price path, and runs the cash-flow engine;
4. report EUR / NPV / IRR / payout / breakeven distributions and tornado sensitivities.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

from ..paths import DATA
from ..storage import connect, build_views
from . import calibration, decline, economics, prices
from .wellcurves import ADIR

HORIZON = 360
EARTH_MI = 3958.8


def analytics_connection() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with canonical + analytics views (never locks the warehouse file)."""
    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{DATA / 'tmp'}'")
    build_views(con)
    return con


def haversine_sql(lat: float, lon: float, lat_col="lat", lon_col="lon") -> str:
    return (f"{EARTH_MI} * 2 * asin(sqrt(pow(sin(radians({lat_col} - ({lat})) / 2), 2) + "
            f"cos(radians({lat})) * cos(radians({lat_col})) * pow(sin(radians({lon_col} - ({lon})) / 2), 2)))")


@dataclass
class AnalogSpec:
    radius_mi: float = 10.0
    max_radius_mi: float = 50.0
    min_wells: int = 25
    max_wells: int = 400
    vintage_min: int = 2014
    formation: str | None = None          # substring match on well_decline.formation (case-insensitive)
    product: str | None = None            # 'oil' | 'gas' | None (auto: majority of analogs)
    include_lease_alloc: bool = False     # TX/KS multi-well lease allocations are noisy; off by default
    min_months: int = 6
    vintage_half_life: float = 6.0        # recency weighting of analogs: an analog N years older than the newest loses 50% weight per half-life (0 = off)


def find_analogs(con, lat: float, lon: float, spec: AnalogSpec) -> pd.DataFrame:
    r = spec.radius_mi
    while True:
        where = [f"d.vintage >= {spec.vintage_min}", f"dist <= {r}", f"d.oil_fit_n + d.gas_fit_n >= {spec.min_months}",
                 "(d.oil_fit_ok OR d.gas_fit_ok)"]
        if not spec.include_lease_alloc:
            where.append("d.alloc <> 'lease_alloc'")
        where.append("d.alloc NOT IN ('q_split', 'a_split')")  # quarterly/annual totals spread over months: no real decline shape
        where.append("NOT d.history_truncated")
        if spec.formation:
            where.append(f"lower(coalesce(d.formation, '')) LIKE '%{spec.formation.lower()}%'")
        if spec.product:
            where.append(f"d.primary_product = '{spec.product}'")
        df = con.execute(f"""
            SELECT * FROM (
                SELECT d.*, l.lat, l.lon, l.location_source, {haversine_sql(lat, lon, 'l.lat', 'l.lon')} AS dist
                FROM well_decline d JOIN well_locations l USING (api10)
                WHERE l.lat BETWEEN {lat - 1} AND {lat + 1} AND l.lon BETWEEN {lon - 1.2} AND {lon + 1.2}
            ) d WHERE {' AND '.join(where)} ORDER BY dist LIMIT {spec.max_wells}
        """).df()
        if len(df) >= spec.min_wells or r >= spec.max_radius_mi:
            df.attrs["radius_used"] = r
            break
        r = min(r * 1.6, spec.max_radius_mi)
    if df.empty:
        return df
    df["weight"] = 1.0 / (1.0 + df["dist"] / max(r / 3, 1.0))
    df["weight"] /= df["weight"].sum()
    return df


def analog_curves(con, api10s: list[str], horizon: int = HORIZON) -> pd.DataFrame:
    """Percentile curves (volume/month) by month since first production, extended with Arps fits."""
    con.register("_an", pd.DataFrame({"api10": api10s}))
    hist = con.execute("""
        SELECT datediff('month', d.first_prod, m.period) AS t, count(*) AS n,
               quantile_cont(m.oil_bbl, 0.1) oil_p10, quantile_cont(m.oil_bbl, 0.5) oil_p50, quantile_cont(m.oil_bbl, 0.9) oil_p90, avg(m.oil_bbl) oil_mean,
               quantile_cont(m.gas_mcf, 0.1) gas_p10, quantile_cont(m.gas_mcf, 0.5) gas_p50, quantile_cont(m.gas_mcf, 0.9) gas_p90, avg(m.gas_mcf) gas_mean
        FROM well_monthly m JOIN well_decline d USING (api10) JOIN _an a USING (api10)
        WHERE m.period >= d.first_prod GROUP BY 1 ORDER BY 1
    """).df()
    con.unregister("_an")
    n_wells = len(api10s)
    hist = hist[hist.n >= max(5, n_wells * 0.25)]           # drop the sparse tail
    out = pd.DataFrame({"t": np.arange(horizon)})
    for col in [c for c in hist.columns if c not in ("t", "n")]:
        series = np.full(horizon, np.nan)
        idx = hist.t.values[hist.t.values < horizon]
        series[idx] = hist[col].values[hist.t.values < horizon]
        # extend with an Arps fit of the percentile curve itself (peak-aligned)
        obs = series.copy()
        if np.isfinite(obs).sum() >= decline.MIN_FIT_POINTS and np.nanmax(obs) > 0:
            peak = int(np.nanargmax(np.where(np.arange(horizon) < 12, np.nan_to_num(obs, nan=-1), -1)))
            fit = decline.fit_grid(obs[None, peak:])
            if fit.ok[0]:
                last = int(np.where(np.isfinite(obs))[0].max())
                anchor = np.nanmean(obs[max(last - 2, peak): last + 1])
                fut = decline.forecast_from_anchor(anchor, last - peak, fit.di[0], fit.b[0], horizon - last - 1)[0]
                series[last + 1:] = fut
        out[col] = series
    out["n"] = out.t.map(dict(zip(hist.t, hist.n))).fillna(0).astype(int)
    return out


def analog_paths(analogs: pd.DataFrame, hist: dict, horizon: int = HORIZON, target_vintage: int | None = None, slope: float = 0.0) -> dict:
    """Per-analog base paths (N, horizon): the well's actual observed months, then its own Arps fit anchored on its last
    three producing months. Returns {'oil','gas','n_obs','vfactor'}; vfactor scales older analogs to the target vintage."""
    n = len(analogs)
    n_obs = hist["n_obs"]
    out = {"n_obs": n_obs}
    t_idx = np.arange(horizon)[None, :]
    for prod in ("oil", "gas"):
        base = np.where(np.isfinite(hist[prod]), hist[prod], 0.0)
        ok = analogs[f"{prod}_fit_ok"].values.astype(bool)
        q0 = np.where(ok, analogs[f"{prod}_q_last3"].values, 0.0).astype(float)
        t_last = np.nan_to_num(analogs[f"{prod}_t_last"].values.astype(float), nan=0.0)
        peak = np.nan_to_num(analogs[f"{prod}_peak_month"].values.astype(float), nan=0.0)
        di = np.where(ok, analogs[f"{prod}_di"].values, 0.1).astype(float)
        b = np.where(ok, analogs[f"{prod}_b"].values, 0.5).astype(float)
        # rate at month m beyond the observed history: hyperbolic from the anchor (t_last, q0)
        dt = t_idx - t_last[:, None]
        ext = decline.rate(q0[:, None], di[:, None], b[:, None], np.maximum(dt, 0) + (t_last - peak)[:, None]) / np.maximum(
            decline.rate(1.0, di[:, None], b[:, None], (t_last - peak)[:, None]), 1e-12) * np.ones_like(dt)
        ext = np.where(ok[:, None], ext, 0.0)
        beyond = t_idx >= n_obs[:, None]
        out[prod] = np.where(beyond, ext, base)
    vf = np.ones(n)
    if slope and target_vintage is not None:
        vf = np.exp(slope * (target_vintage - analogs.vintage.fillna(target_vintage).values.astype(float)))
        vf = np.clip(vf, 0.67, 1.5)
    out["vfactor"] = vf
    return out


def simulate_production(analogs: pd.DataFrame, paths: dict, calib: "calibration.Calibration", weights: np.ndarray, sims: int,
                        rng: np.random.Generator, horizon: int = HORIZON) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Draw analog wells (distance x vintage weights); each draw is that well's real history followed by its own
    extrapolation, with the extrapolated months multiplied by a hindcast-sampled error and the whole path scaled to
    the target vintage. Returns (S,H) oil, gas and the picked analog index."""
    pick = rng.choice(len(analogs), size=sims, p=weights)
    eps = calib.sample(rng, sims, anchor_age=paths["n_obs"][pick])   # error pool matched to the age each path is extrapolated from
    months_beyond = np.arange(horizon)[None, :] - paths["n_obs"][pick][:, None]
    beyond = months_beyond >= 0
    prof = calibration.error_profile(months_beyond)             # transient error: ramps in, holds 24 months, decays
    mult = np.exp(eps[:, None] * prof) * paths["vfactor"][pick][:, None]
    out = {}
    for prod in ("oil", "gas"):
        q = paths[prod][pick] * mult
        econ = 30.0 if prod == "oil" else 300.0
        q = np.where(q >= econ, q, 0.0)
        dead = np.cumsum(q <= 0, axis=1) > 0
        dead[:, 0] = False
        q = np.where(dead & beyond & (np.arange(horizon)[None, :] > 12), 0.0, q)  # once below the limit in the extrapolated part, stay shut in
        out[prod] = q
    return out["oil"], out["gas"], pick


@dataclass
class ForecastResult:
    inputs: dict
    analogs: pd.DataFrame
    curves: pd.DataFrame
    deck: prices.Deck
    assumptions: economics.Assumptions
    metrics: dict
    scenarios: dict
    sensitivities: pd.DataFrame
    cashflow_p50: pd.DataFrame
    mc_curves: dict | None = None
    npv_hist: dict | None = None

    def to_json(self) -> str:
        def conv(o):
            if isinstance(o, (np.floating, np.integer)):
                return None if (isinstance(o, np.floating) and not np.isfinite(o)) else o.item()
            if isinstance(o, (pd.Timestamp,)):
                return o.isoformat()
            raise TypeError(str(type(o)))
        an = self.analogs[["api10", "state", "county", "formation", "operator", "vintage", "lat", "lon", "dist", "oil_eur", "gas_eur", "oil_ip90_pd", "gas_ip90_pd", "oil_b", "oil_di", "gas_b", "gas_di", "alloc", "status"]]
        payload = {
            "inputs": self.inputs, "assumptions": self.assumptions.to_dict(),
            "deck": {"source": self.deck.source, "vol_oil": self.deck.vol_oil, "vol_gas": self.deck.vol_gas, "rho": self.deck.rho,
                     "months": [m.strftime("%Y-%m") for m in self.deck.months[:60]], "wti": list(self.deck.wti[:60]), "hh": list(self.deck.hh[:60])},
            "metrics": self.metrics, "scenarios": self.scenarios,
            "curves": self.curves.replace({np.nan: None}).to_dict(orient="list"),
            "sensitivities": self.sensitivities.replace({np.nan: None}).to_dict(orient="records"),
            "cashflow_p50": self.cashflow_p50.replace({np.nan: None}).to_dict(orient="list"),
            "analogs": an.replace({np.nan: None}).to_dict(orient="records"),
        }
        return json.dumps(payload, default=conv)


def _pct(x: np.ndarray, qs=(10, 25, 50, 75, 90)) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {f"p{q}": None for q in qs} | {"mean": None}
    return {f"p{q}": float(np.percentile(x, q)) for q in qs} | {"mean": float(x.mean())}


def run(lat: float, lon: float, spec: AnalogSpec | None = None, assumptions: economics.Assumptions | None = None,
        sims: int = 2000, seed: int = 7, con: duckdb.DuckDBPyConnection | None = None, new_well: bool = True,
        api10: str | None = None) -> ForecastResult:
    con = con or analytics_connection()
    spec = spec or AnalogSpec()
    rng = np.random.default_rng(seed)
    target = None
    if api10:
        target = con.execute("SELECT d.*, l.lat, l.lon FROM well_decline d JOIN well_locations l USING (api10) WHERE api10 = ?", [api10]).df()
        if target.empty:
            raise SystemExit(f"api10 {api10} not in well_decline/well_locations")
        lat, lon = float(target.lat[0]), float(target.lon[0])
    analogs = find_analogs(con, lat, lon, spec)
    if analogs.empty:
        raise SystemExit(f"no analog wells within {spec.max_radius_mi} mi of ({lat}, {lon}) with vintage >= {spec.vintage_min}")
    state = analogs.state.mode().iat[0]
    product = spec.product or analogs.primary_product.mode().iat[0]
    a = (assumptions or economics.Assumptions(state=state))
    if a.state != state and assumptions is None:
        a = economics.Assumptions(state=state)
    if not new_well:
        a = economics.Assumptions(**{**a.__dict__, "capex_mm": 0.0})
    a = a.resolved()
    curves = analog_curves(con, analogs.api10.tolist())
    deck = prices.load_deck(con, horizon=HORIZON)
    p_oil, p_gas = prices.simulate_paths(deck, sims, rng)
    # historical grounding: analog histories, recency weights, hindcast of the method on the analogs themselves
    weights = calibration.vintage_weights(analogs, spec.vintage_half_life)
    analogs = analogs.assign(weight=weights)
    hist = calibration.histories(con, analogs, HORIZON)
    target_vintage = calibration.vintage_target(analogs) if not api10 else int(target.iloc[0]["vintage"]) if target is not None else None
    k_fit = 12
    if api10 and target is not None:
        n_hist = int(max(target.iloc[0]["oil_fit_n"], target.iloc[0]["gas_fit_n"]))
        k_fit = 6 if n_hist < 12 else 12 if n_hist < 24 else 24
    calib = calibration.calibrate(analogs, hist, product, k=k_fit, h=24)
    if api10:
        calib.vintage_slope = 0.0  # PDP paths are the well's own; vintage scaling only applies to analog paths

    if api10 and target is not None:
        # PDP mode: this well's own fitted curve anchored at its last rate; uncertainty from fit RMSE + analog b/Di spread
        tw = target.iloc[0]
        oil = np.zeros((sims, HORIZON)); gas = np.zeros((sims, HORIZON))
        # young wells borrow decline shape from the analogs: with probability 12/(n+12) a path uses an analog's (Di, b)
        n_hist = int(max(tw["oil_fit_n"], tw["gas_fit_n"]))
        w_shrink = 12.0 / (n_hist + 12.0)
        borrow = rng.random(sims) < w_shrink
        apick = rng.choice(len(analogs), size=sims, p=weights)
        eps = calib.sample(rng, sims, anchor_age=np.full(sims, n_hist))   # hindcast error pool for this well's age
        for prod, arr in (("oil", oil), ("gas", gas)):
            if tw[f"{prod}_fit_ok"]:
                sig = min(float(tw[f"{prod}_fit_rmse_log"]) / 3, 0.25)
                q0 = tw[f"{prod}_q_last3"] * np.exp(rng.normal(0, sig, sims) - sig ** 2 / 2)
                a_ok = analogs[f"{prod}_fit_ok"].values[apick].astype(bool)
                di = np.where(borrow & a_ok, analogs[f"{prod}_di"].values[apick], tw[f"{prod}_di"] * np.exp(rng.normal(0, 0.05, sims)))
                b = np.where(borrow & a_ok, analogs[f"{prod}_b"].values[apick], np.clip(tw[f"{prod}_b"] + rng.normal(0, 0.03, sims), 0, 1.5))
                remaining = max(HORIZON - int(tw[f"{prod}_t_last"]) - 1, 12)
                f = decline.forecast_from_anchor(q0, np.full(sims, float(tw[f"{prod}_t_last"] - tw[f"{prod}_peak_month"])), di, b, remaining)
                f = f * np.exp(eps[:, None] * calibration.error_profile(np.arange(remaining))[None, :])
                f = np.where(f >= (30.0 if prod == "oil" else 300.0), f, 0.0)
                arr[:, :remaining] = f
        pick = None
        a = economics.Assumptions(**{**a.__dict__, "capex_mm": 0.0}).resolved()
    else:
        paths = analog_paths(analogs, hist, HORIZON, target_vintage, calib.vintage_slope)
        oil, gas, pick = simulate_production(analogs, paths, calib, weights, sims, rng, HORIZON)

    cf = economics.cashflows(oil, gas, p_oil, p_gas, a)
    npv10 = economics.npv(cf.net_cf, a.discount_rate)
    pv_op = economics.npv(cf.operating_cf, a.discount_rate)
    irr = economics.irr(cf.net_cf) if new_well and not api10 else np.full(sims, np.nan)
    payout = economics.payout_month(cf.net_cf)
    rem_oil = oil.sum(1)
    rem_gas = gas.sum(1)
    cum_oil = float(target.iloc[0]["oil_cum"]) if api10 and target is not None else 0.0
    cum_gas = float(target.iloc[0]["gas_cum"]) if api10 and target is not None else 0.0
    eur_oil, eur_gas = rem_oil + cum_oil, rem_gas + cum_gas
    metrics = {
        "state": state, "product": product, "n_analogs": int(len(analogs)), "radius_mi": float(analogs.attrs.get("radius_used", spec.radius_mi)),
        "mode": "pdp" if api10 else ("new_well" if new_well else "pdp_location"),
        "cum_oil_bbl": cum_oil, "cum_gas_mcf": cum_gas,
        "remaining_oil_bbl": _pct(rem_oil), "remaining_gas_mcf": _pct(rem_gas), "remaining_boe": _pct(rem_oil + rem_gas / 6),
        "eur_oil_bbl": _pct(eur_oil), "eur_gas_mcf": _pct(eur_gas), "eur_boe": _pct(eur_oil + eur_gas / 6),
        "ip90_oil_bpd": _pct(oil[:, :3].mean(1) / 30.4375), "ip90_gas_mcfd": _pct(gas[:, :3].mean(1) / 30.4375),
        "npv10_usd": _pct(npv10), "pv10_operating_usd": _pct(pv_op), "irr": _pct(irr), "payout_months": _pct(payout),
        "life_months": _pct(cf.life_months), "prob_npv10_positive": float((npv10 > 0).mean()),
        "revenue_12m_usd": _pct(cf.revenue[:, :12].sum(1)), "operating_cf_12m_usd": _pct(cf.operating_cf[:, :12].sum(1)),
        "grounding": {**calib.to_dict(), "vintage_half_life_years": spec.vintage_half_life, "target_vintage": target_vintage,
                      "pdp_shrink_weight": (12.0 / (int(max(target.iloc[0]["oil_fit_n"], target.iloc[0]["gas_fit_n"])) + 12.0)) if api10 and target is not None else None},
    }
    # deterministic scenario grid: production P10/P50/P90 x price deck -25% / base / +25%
    scen = {}
    med = {k: np.nanmedian(v) for k, v in (("oil", eur_oil), ("gas", eur_gas))}
    order = np.argsort(rem_oil + rem_gas / 6)
    for name, q in (("bear", 10), ("base", 50), ("bull", 90)):
        i = order[int(len(order) * q / 100) - (1 if q == 90 else 0)]
        o, g = oil[i:i + 1], gas[i:i + 1]
        row = {}
        for pname, mult in (("price_-25%", 0.75), ("price_base", 1.0), ("price_+25%", 1.25)):
            c = economics.cashflows(o, g, deck.wti[None, :] * mult, deck.hh[None, :] * mult, a)
            row[pname] = {"npv10": float(economics.npv(c.net_cf, a.discount_rate)[0]),
                          "irr": (None if not new_well or api10 else float(np.nan_to_num(economics.irr(c.net_cf)[0], nan=np.nan))),
                          "payout_months": float(np.nan_to_num(economics.payout_month(c.net_cf)[0], nan=-1))}
        row["eur_oil_bbl"] = float(o.sum()) + cum_oil; row["eur_gas_mcf"] = float(g.sum()) + cum_gas
        scen[name] = row
    # P50 production profile + base deck -> monthly cash flow table and tornado
    i50 = order[len(order) // 2]
    o50, g50 = oil[i50:i50 + 1], gas[i50:i50 + 1]
    c50 = economics.cashflows(o50, g50, deck.wti[None, :], deck.hh[None, :], a)
    base_npv = float(economics.npv(c50.net_cf, a.discount_rate)[0])
    hh_ratio = deck.hh / deck.wti
    metrics["breakeven_wti_p50"] = economics.breakeven_wti(o50, g50, hh_ratio[None, :], a) if new_well and not api10 else None
    metrics["npv10_p50_profile_base_deck"] = base_npv
    sens_rows = []
    def tweak(label, **kw):
        oo, gg, ao = o50, g50, a
        po, pg = deck.wti[None, :], deck.hh[None, :]
        if "oil_price" in kw: po = po * kw["oil_price"]
        if "gas_price" in kw: pg = pg * kw["gas_price"]
        if "volume" in kw: oo, gg = oo * kw["volume"], gg * kw["volume"]
        if "assump" in kw: ao = economics.Assumptions(**{**a.__dict__, **kw["assump"]}).resolved()
        return float(economics.npv(economics.cashflows(oo, gg, po, pg, ao).net_cf, ao.discount_rate)[0])
    for label, lo_kw, hi_kw in (
        ("Oil price ±20%", {"oil_price": 0.8}, {"oil_price": 1.2}),
        ("Gas price ±30%", {"gas_price": 0.7}, {"gas_price": 1.3}),
        ("EUR / volumes ±20%", {"volume": 0.8}, {"volume": 1.2}),
        ("Capex ±15%", {"assump": {"capex_mm": a.capex_mm * 1.15}}, {"assump": {"capex_mm": a.capex_mm * 0.85}}),
        ("LOE ±25%", {"assump": {"loe_fixed_month": a.loe_fixed_month * 1.25, "loe_var_per_boe": a.loe_var_per_boe * 1.25}},
                      {"assump": {"loe_fixed_month": a.loe_fixed_month * 0.75, "loe_var_per_boe": a.loe_var_per_boe * 0.75}}),
        ("NRI 70% / 80%", {"assump": {"nri": 0.70}}, {"assump": {"nri": 0.80}}),
        ("Oil differential -$6 / $0", {"assump": {"oil_diff": -6.0}}, {"assump": {"oil_diff": 0.0}}),
        ("Discount rate 15% / 8%", {"assump": {"discount_rate": 0.15}}, {"assump": {"discount_rate": 0.08}}),
    ):
        sens_rows.append({"driver": label, "low": tweak(label, **lo_kw) - base_npv, "high": tweak(label, **hi_kw) - base_npv})
    sens = pd.DataFrame(sens_rows)
    sens["swing"] = (sens.high - sens.low).abs()
    sens = sens.sort_values("swing", ascending=False).reset_index(drop=True)
    cf50 = pd.DataFrame({"month": [m.strftime("%Y-%m") for m in deck.months], "oil_bbl": o50[0], "gas_mcf": g50[0], "wti": deck.wti, "hh": deck.hh,
                         "revenue": c50.revenue[0], "taxes": c50.taxes[0], "opex": c50.opex[0], "operating_cf": c50.operating_cf[0], "net_cf": c50.net_cf[0],
                         "cum_net_cf": np.cumsum(c50.net_cf[0])})
    cf50 = cf50[cf50.month <= cf50.month[int(c50.life_months[0]) - 1] if c50.life_months[0] > 0 else True]
    inputs = {"lat": lat, "lon": lon, "api10": api10, "new_well": bool(new_well and not api10), "sims": sims, "seed": seed, "spec": spec.__dict__,
              "generated": pd.Timestamp.now().isoformat(timespec="seconds")}
    mc_curves = {prod: {k: np.percentile(arr, q, axis=0).tolist() for k, q in (("p10", 10), ("p50", 50), ("p90", 90))} for prod, arr in (("oil", oil), ("gas", gas))}
    lo_, hi_ = np.percentile(npv10, [1, 99])
    counts, edges = np.histogram(np.clip(npv10, lo_, hi_), bins=30)
    return ForecastResult(inputs, analogs, curves, deck, a, metrics, scen, sens, cf50, mc_curves, {"edges": edges.tolist(), "counts": counts.tolist()})


def summary_text(r: ForecastResult) -> str:
    m = r.metrics
    f = lambda d, k="p50", s=1.0, fmt="{:,.0f}": ("n/a" if d.get(k) is None else fmt.format(d[k] * s))
    lines = [
        f"Location {r.inputs['lat']:.4f}, {r.inputs['lon']:.4f}  state {m['state']}  primary product {m['product']}",
        f"Analogs: {m['n_analogs']} wells within {m['radius_mi']:.0f} mi, vintage >= {r.inputs['spec']['vintage_min']}"
        + (f", formation ~ {r.inputs['spec']['formation']}" if r.inputs['spec']['formation'] else ""),
        f"Mode: {'new well (capex $%.1fMM)' % r.assumptions.capex_mm if r.inputs['new_well'] else 'existing well / PDP (no capex)'}; NRI {r.assumptions.nri:.0%}; deck {r.deck.source}",
        "",
        f"{'':28s}{'P10':>14s}{'P50':>14s}{'P90':>14s}{'mean':>14s}",
    ]
    pdp = bool(r.inputs.get("api10"))
    if pdp:
        lines.insert(3, f"Cum to date: {m['cum_oil_bbl']:,.0f} bbl oil, {m['cum_gas_mcf']:,.0f} mcf gas")
    vol_rows = ((("Remaining oil (bbl)" if pdp else "EUR oil (bbl)"), ("remaining_oil_bbl" if pdp else "eur_oil_bbl"), 1, "{:,.0f}"),
                (("Remaining gas (mcf)" if pdp else "EUR gas (mcf)"), ("remaining_gas_mcf" if pdp else "eur_gas_mcf"), 1, "{:,.0f}"),
                (("Remaining boe" if pdp else "EUR boe"), ("remaining_boe" if pdp else "eur_boe"), 1, "{:,.0f}"),
                (("Current oil rate (bbl/d)" if pdp else "IP90 oil (bbl/d)"), "ip90_oil_bpd", 1, "{:,.0f}"), (("Current gas rate (mcf/d)" if pdp else "IP90 gas (mcf/d)"), "ip90_gas_mcfd", 1, "{:,.0f}"))
    for label, key, s, fmt in vol_rows + (
                               ("Revenue, first 12m ($)", "revenue_12m_usd", 1, "{:,.0f}"), ("Operating CF, first 12m ($)", "operating_cf_12m_usd", 1, "{:,.0f}"),
                               ("PV10 operating CF ($)", "pv10_operating_usd", 1, "{:,.0f}"), ("NPV10 ($)", "npv10_usd", 1, "{:,.0f}"),
                               ("IRR", "irr", 100, "{:.0f}%"), ("Payout (months)", "payout_months", 1, "{:.0f}"), ("Economic life (months)", "life_months", 1, "{:.0f}")):
        d = m[key]
        lines.append(f"{label:28s}{f(d,'p10',s,fmt):>14s}{f(d,'p50',s,fmt):>14s}{f(d,'p90',s,fmt):>14s}{f(d,'mean',s,fmt):>14s}")
    lines += ["", f"P(NPV10 > 0) = {m['prob_npv10_positive']:.0%}" + (f";  breakeven flat WTI (P50 profile) = ${m['breakeven_wti_p50']:.0f}/bbl" if m.get('breakeven_wti_p50') else ""), "",
              "Scenario NPV10 ($MM): production bear/base/bull (P10/P50/P90 EUR) x price deck -25% / base / +25%"]
    for name, row in r.scenarios.items():
        lines.append(f"  {name:5s} EUR {row['eur_oil_bbl']/1e3:7.0f} Mbbl {row['eur_gas_mcf']/1e6:6.2f} Bcf | " + "  ".join(f"{k}: {v['npv10']/1e6:7.2f}" for k, v in row.items() if k.startswith('price')))
    g = m.get("grounding") or {}
    if g:
        pct = lambda v: "n/a" if v is None else f"{(np.exp(v) - 1) * 100:+.0f}%"
        lines += ["", f"Grounding: {g['effective_n_analogs']:.0f} effective analogs (vintage half-life {g['vintage_half_life_years']:.0f} y)"
                      f"; hindcast fit {g['k_fit_months']} mo -> next {g['horizon_months']} mo on {g['n_hindcast']} analogs: bias {pct(g['hindcast_bias_p50'])}, P10 {pct(g['hindcast_err_p10'])}, P90 {pct(g['hindcast_err_p90'])}"
                      f"; leave-one-out {g['horizon_months']}-mo cum on {g['n_loo']} analogs: P10 {pct(g['loo_err_p10'])}, P90 {pct(g['loo_err_p90'])}"
                      + (f"; vintage trend {g['vintage_slope_per_year'] * 100:+.1f}%/yr, analogs scaled to {g['target_vintage']} vintage (factor capped 0.67-1.5)" if g.get("vintage_slope_per_year") else "; no significant vintage trend")
                      + ("; error pools by anchor age " + ", ".join(f"{k} mo: n={v['n']} P10 {pct(v['p10'])} P90 {pct(v['p90'])}" for k, v in sorted(g.get("pools", {}).items(), key=lambda kv: int(kv[0]))) if g.get("pools") else "")
                      + (f"; PDP shape borrowed from analogs with weight {g['pdp_shrink_weight']:.0%}" if g.get("pdp_shrink_weight") else "")]
    lines += ["", "Tornado (NPV10 change vs P50/base, $MM):"]
    for _, s in r.sensitivities.iterrows():
        lines.append(f"  {s.driver:28s} {s.low/1e6:+8.2f} .. {s.high/1e6:+8.2f}")
    return "\n".join(lines)
