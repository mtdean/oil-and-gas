"""Vectorised Arps decline-curve fitting and forecasting.

Model (rates in volume per month, t in months from the peak month):
    hyperbolic  q(t) = qi / (1 + b*Di*t)^(1/b)          0 < b
    exponential q(t) = qi * exp(-Di*t)                   b = 0
    modified Arps: once the instantaneous nominal decline D(t) = Di/(1+b*Di*t)
    falls below Dmin the curve continues exponentially at Dmin.

Fitting is a least-squares fit in log(q) over a (b, Di) grid with the closed-form
optimal log(qi) for each grid cell, so a chunk of N wells x T months is fitted with
two matrix products instead of N scipy calls. Zero/shut-in months are excluded.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

B_GRID = np.array([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5])
DI_GRID = np.exp(np.linspace(np.log(0.002), np.log(0.7), 44))  # nominal monthly decline at t=0
DMIN_ANNUAL = 0.06  # terminal effective decline, 6 %/yr
DMIN_MONTHLY = -np.log(1 - DMIN_ANNUAL) / 12
MIN_FIT_POINTS = 6
MAX_FIT_MONTHS = 240


def _g_matrix(T: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """log-shape g(t) = log(q(t)/qi) for every (b, Di) pair -> (T, G) plus the flattened b, Di."""
    t = np.arange(T, dtype=np.float64)[:, None]
    bb, dd = np.meshgrid(B_GRID, DI_GRID, indexing="ij")
    b = bb.ravel()[None, :]
    d = dd.ravel()[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        g = np.where(b > 0, -np.log1p(b * d * t) / np.where(b > 0, b, 1.0), -d * t)
    return g, bb.ravel(), dd.ravel()


@dataclass
class FitResult:
    qi: np.ndarray        # fitted rate at t=0 (volume / month)
    di: np.ndarray        # nominal monthly initial decline
    b: np.ndarray
    rmse_log: np.ndarray  # RMSE of log residuals
    n: np.ndarray         # points used
    ok: np.ndarray        # bool


def fit_grid(Q: np.ndarray) -> FitResult:
    """Q: (N, T) monthly volumes aligned so column 0 is the peak month; NaN or <=0 = excluded."""
    N, T = Q.shape
    T = min(T, MAX_FIT_MONTHS)
    Q = Q[:, :T].astype(np.float64)
    mask = np.isfinite(Q) & (Q > 0)
    L = np.where(mask, np.log(np.where(mask, Q, 1.0)), 0.0)
    M = mask.astype(np.float64)
    G, b_flat, d_flat = _g_matrix(T)
    n = M.sum(1)                              # (N,)
    S_L = L.sum(1)                            # (N,)
    S_LL = (L * L).sum(1)                     # (N,)
    S_g = M @ G                               # (N, G)
    S_gg = M @ (G * G)                        # (N, G)
    S_Lg = L @ G                              # (N, G)
    with np.errstate(divide="ignore", invalid="ignore"):
        sum_sq = S_LL[:, None] - 2 * S_Lg + S_gg
        sum_r = S_L[:, None] - S_g
        sse = sum_sq - sum_r * sum_r / n[:, None]
    sse = np.where(np.isfinite(sse), sse, np.inf)
    best = sse.argmin(1)
    rows = np.arange(N)
    c = sum_r[rows, best] / np.where(n > 0, n, 1)
    ok = n >= MIN_FIT_POINTS
    qi = np.where(ok, np.exp(c), np.nan)
    rmse = np.where(ok, np.sqrt(np.maximum(sse[rows, best], 0) / np.where(n > 0, n, 1)), np.nan)
    return FitResult(qi=qi, di=np.where(ok, d_flat[best], np.nan), b=np.where(ok, b_flat[best], np.nan), rmse_log=rmse, n=n.astype(int), ok=ok)


def rate(qi, di, b, t, dmin: float = DMIN_MONTHLY):
    """Modified-Arps rate at month(s) t (broadcasts; qi/di/b may be arrays with a trailing axis for t)."""
    qi, di, b, t = (np.asarray(x, dtype=np.float64) for x in (qi, di, b, t))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        hyp = np.where(b > 0, qi / np.power(1 + b * di * t, 1 / np.where(b > 0, b, 1.0)), qi * np.exp(-di * t))
        d_t = di / (1 + b * di * t)
        # switch time t* where D(t*) = dmin  ->  t* = (di/dmin - 1) / (b*di)
        t_star = np.where((b > 0) & (di > dmin), (di / dmin - 1) / np.where(b > 0, b * di, 1.0), np.where(di > dmin, np.inf, 0.0))
        q_star = np.where(b > 0, qi / np.power(1 + b * di * t_star, 1 / np.where(b > 0, b, 1.0)), qi)
        tail = q_star * np.exp(-dmin * (t - t_star))
        out = np.where(t <= t_star, hyp, tail)
        out = np.where(di <= dmin, qi * np.exp(-dmin * t), out)  # already slower than terminal: straight exponential at dmin
    return out


def forecast_from_anchor(q_anchor, t_anchor, di, b, horizon: int, dmin: float = DMIN_MONTHLY) -> np.ndarray:
    """Continue a fitted curve from an observed anchor rate at month t_anchor for `horizon` months (N, horizon).

    The shape comes from the fit (di, b) while the level is re-anchored to recent actuals,
    which is how engineers usually forecast PDP.
    """
    q_anchor, t_anchor, di, b = (np.atleast_1d(np.asarray(x, dtype=np.float64)) for x in (q_anchor, t_anchor, di, b))
    t = t_anchor[:, None] + np.arange(1, horizon + 1)[None, :]
    base = rate(1.0, di[:, None], b[:, None], t_anchor[:, None], dmin)
    fut = rate(1.0, di[:, None], b[:, None], t, dmin)
    with np.errstate(divide="ignore", invalid="ignore"):
        return q_anchor[:, None] * fut / np.where(base > 0, base, np.nan)


def eur(qi, di, b, cum_to_date, t_last, horizon_months: int, econ_limit: float, dmin: float = DMIN_MONTHLY) -> np.ndarray:
    """Cum to date + forecast volumes from t_last+1 to horizon, truncated at the economic limit (volume/month)."""
    qi, di, b, cum, t_last = (np.atleast_1d(np.asarray(x, dtype=np.float64)) for x in (qi, di, b, cum_to_date, t_last))
    H = int(horizon_months)
    out = cum.copy()
    step = 240
    for start in range(0, H, step):
        t = np.arange(start, min(start + step, H), dtype=np.float64)[None, :]
        q = rate(qi[:, None], di[:, None], b[:, None], t, dmin)
        q = np.where((t > t_last[:, None]) & (q >= econ_limit), q, 0.0)
        out += np.nansum(q, axis=1)
    return out
