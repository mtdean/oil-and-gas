"""Historical grounding for the Monte Carlo: measure how the forecasting method performs on the analog wells
themselves and feed those measured errors back into the sampling, instead of fixed noise dials.

Three pieces, all computed from the warehouse at forecast time for the chosen analog set:

* ``histories``   the analogs' actual monthly volumes, normalised to months since first production (N x H).
                  Sampled paths start as a real well's observed history, not a smoothed curve.
* ``hindcast``    for analogs with enough history: fit Arps on the first ``k`` producing months (exactly as the
                  pipeline would for a young well), forecast the next ``h`` months, compare with what happened.
                  The log residuals log(actual / forecast) are the empirical error of extrapolating this kind of
                  well in this area; the Monte Carlo multiplies every extrapolated segment by exp(residual).
* ``loo_error``   leave-one-out check of the analog method: predict each analog's h-month cum from the weighted
                  median of the other analogs, compare with its actual. The spread is the error of saying
                  "a new well here will look like its neighbours", reported alongside the band.
* ``vintage_trend`` weighted regression of log(12-month cum) on first-production year across the analogs; when it
                  is significant, older analogs are scaled to today's completion vintage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import decline

HORIZON = 360


ANCHOR_AGES = (12, 24, 48)           # hindcast anchor ages (months of history used for the fit)


@dataclass
class Calibration:
    k_fit: int
    horizon: int
    residuals: np.ndarray            # log(actual_cum_h / forecast_cum_h) from the hindcast at k_fit, one per usable analog
    n_hindcast: int
    loo_residuals: np.ndarray        # log(actual_cum_h / loo_prediction)
    n_loo: int
    vintage_slope: float             # per year, on log(cum12); 0 when not significant
    vintage_slope_se: float
    vintage_n: int
    effective_n: float               # 1 / sum(w^2) of the analog weights
    fallback_sigma: float = 0.35     # used when too few residuals exist
    pools: dict = field(default_factory=dict)   # anchor age -> residual array; paths extrapolated from an older age use the matching pool

    @staticmethod
    def _draw(res: np.ndarray, rng: np.random.Generator, size: int, fallback: float) -> np.ndarray:
        if len(res) >= 20:
            return rng.choice(res, size=size, replace=True)
        if len(res) >= 5:
            return rng.normal(float(np.median(res)), max(float(np.std(res, ddof=1)), 0.15), size)
        return rng.normal(0.0, fallback, size)

    def sample(self, rng: np.random.Generator, size: int, anchor_age: np.ndarray | None = None) -> np.ndarray:
        """Draw extrapolation errors (log multipliers). With `anchor_age` (months of observed history before the
        extrapolation starts, one per path) each path draws from the pool of the nearest hindcast anchor age at or
        below its own age, so a path extrapolated from year 5 uses year-4 errors, not year-1 errors."""
        if anchor_age is None or not self.pools:
            return self._draw(self.residuals, rng, size, self.fallback_sigma)
        ages = sorted(self.pools)
        out = np.empty(size)
        chosen = np.array([max([a for a in ages if a <= x] or [ages[0]]) for x in np.asarray(anchor_age)])
        for a in ages:
            m = chosen == a
            if m.any():
                pool = self.pools[a]
                fb = self.fallback_sigma * (0.6 if a >= 48 else 0.8 if a >= 24 else 1.0)
                out[m] = self._draw(pool, rng, int(m.sum()), fb)
        return out

    def to_dict(self) -> dict:
        q = lambda a, p: (None if len(a) == 0 else float(np.quantile(a, p)))
        return {
            "k_fit_months": self.k_fit, "horizon_months": self.horizon, "n_hindcast": self.n_hindcast,
            "hindcast_bias_p50": q(self.residuals, 0.5), "hindcast_err_p10": q(self.residuals, 0.1), "hindcast_err_p90": q(self.residuals, 0.9),
            "hindcast_sigma": (None if self.n_hindcast < 2 else float(np.std(self.residuals, ddof=1))),
            "n_loo": self.n_loo, "loo_bias_p50": q(self.loo_residuals, 0.5), "loo_err_p10": q(self.loo_residuals, 0.1), "loo_err_p90": q(self.loo_residuals, 0.9),
            "pools": {str(k): {"n": int(len(v)), "p10": q(v, 0.1), "p50": q(v, 0.5), "p90": q(v, 0.9)} for k, v in self.pools.items()},
            "vintage_slope_per_year": self.vintage_slope, "vintage_slope_se": self.vintage_slope_se, "vintage_n": self.vintage_n,
            "effective_n_analogs": self.effective_n,
            "source": "hindcast residuals bootstrapped" if self.n_hindcast >= 20 else ("normal from few residuals" if self.n_hindcast >= 5 else f"fallback sigma {self.fallback_sigma}"),
        }


def histories(con, analogs: pd.DataFrame, horizon: int = HORIZON) -> dict[str, np.ndarray]:
    """(N, horizon) arrays of observed monthly oil and gas per analog, NaN beyond each well's last report."""
    idx = pd.DataFrame({"api10": analogs.api10.values, "i": np.arange(len(analogs))})
    con.register("_cal_an", idx)
    rows = con.execute(f"""
        SELECT a.i, datediff('month', d.first_prod, m.period) AS t, m.oil_bbl, m.gas_mcf
        FROM well_monthly m JOIN well_decline d USING (api10) JOIN _cal_an a USING (api10)
        WHERE m.period >= d.first_prod AND datediff('month', d.first_prod, m.period) < {horizon}
    """).fetchnumpy()
    con.unregister("_cal_an")
    n = len(analogs)
    out = {}
    i = np.asarray(rows["i"], dtype=np.int64); t = np.asarray(rows["t"], dtype=np.int64)
    for prod, col in (("oil", "oil_bbl"), ("gas", "gas_mcf")):
        q = np.full((n, horizon), np.nan, dtype=np.float64)
        v = np.ma.filled(np.ma.asarray(rows[col], dtype=np.float64), np.nan)
        q[i, t] = v
        # months before a well's last report with no row are zeros (shut-in), not unknown
        last = np.full(n, -1)
        np.maximum.at(last, i, t)
        has_row = np.zeros((n, horizon), dtype=bool); has_row[i, t] = True
        before_last = np.arange(horizon)[None, :] <= last[:, None]
        q = np.where(before_last & ~has_row, 0.0, q)
        q = np.where(before_last & np.isnan(q), 0.0, q)
        out[prod] = q
    out["n_obs"] = (last + 1).astype(int)
    return out


def _peak_index(Q: np.ndarray, window: int = 12) -> np.ndarray:
    head = np.where(np.isfinite(Q[:, :window]), Q[:, :window], -1.0)
    return np.argmax(head, axis=1)


def hindcast(Q: np.ndarray, k: int = 12, h: int = 24) -> np.ndarray:
    """Fit each well on its first k producing months (from peak), forecast the next h, return log(actual/forecast) cums.
    Wells without k + h observed months after the peak, or with zero actual/forecast, are skipped."""
    peak = _peak_index(Q)
    n, H = Q.shape
    fit_seg = np.full((n, k), np.nan); act = np.full(n, np.nan)
    for j in range(n):
        p = peak[j]
        if p + k + h > H:
            continue
        seg = Q[j, p:p + k]
        fut = Q[j, p + k:p + k + h]
        if np.isfinite(seg).sum() < max(decline.MIN_FIT_POINTS, k // 2) or np.isfinite(fut).sum() < h * 0.8:
            continue
        fit_seg[j] = seg
        act[j] = np.nansum(fut)
    ok_rows = np.isfinite(act)
    if ok_rows.sum() == 0:
        return np.array([])
    fit = decline.fit_grid(fit_seg[ok_rows])
    qi, di, b = fit.qi, fit.di, fit.b
    t = np.arange(k, k + h)[None, :]
    fc = decline.rate(qi[:, None], di[:, None], b[:, None], t).sum(1)
    good = fit.ok & (fc > 0) & (act[ok_rows] > 0)
    res = np.log(act[ok_rows][good] / fc[good])
    if len(res) >= 20:  # winsorise the tails: a handful of refracs / restarts should not dominate the error pool
        res = np.clip(res, np.quantile(res, 0.02), np.quantile(res, 0.98))
    return np.clip(res, -3, 3)


def error_profile(months_beyond: np.ndarray, ramp: int = 6, hold: int = 24, half_life: float = 36.0) -> np.ndarray:
    """How much of a sampled 24-month hindcast error applies at each month beyond the observed history: ramps in over
    the first `ramp` months, holds through month `hold` (the horizon the error was measured on), then decays with
    `half_life` toward zero. Surprises relative to a decline fit are mostly transient (curtailment, shut-ins, offset
    frac hits, refracs); the long-run decline reasserts itself, so the error is not applied to decades of tail."""
    m = np.asarray(months_beyond, dtype=float)
    prof = np.clip(m / max(ramp, 1), 0, 1)
    tail = m > hold
    prof = np.where(tail, np.power(0.5, (m - hold) / half_life), prof)
    return np.where(m < 0, 0.0, prof)


def loo_error(Q: np.ndarray, weights: np.ndarray, h: int = 24) -> np.ndarray:
    """Leave-one-out: predict each well's h-month cum as the weighted median of the other wells' cums."""
    n_obs = np.isfinite(Q).sum(1)
    cum = np.where(n_obs >= h, np.nansum(Q[:, :h], axis=1), np.nan)
    ok = np.isfinite(cum) & (cum > 0)
    idx = np.where(ok)[0]
    if len(idx) < 3:
        return np.array([])
    res = []
    for j in idx:
        others = idx[idx != j]
        w = weights[others]; c = cum[others]
        order = np.argsort(c); cw = np.cumsum(w[order]) / w[order].sum()
        pred = c[order][np.searchsorted(cw, 0.5)]
        if pred > 0:
            res.append(np.log(cum[j] / pred))
    return np.clip(np.asarray(res), -3, 3)


def vintage_trend(analogs: pd.DataFrame, Q: np.ndarray, weights: np.ndarray) -> tuple[float, float, int]:
    """Weighted OLS slope of log(12-month cum) on vintage year. Returns (slope, se, n); slope is 0 when |t| < 2."""
    n_obs = np.isfinite(Q).sum(1)
    cum12 = np.where(n_obs >= 12, np.nansum(Q[:, :12], axis=1), np.nan)
    recent = analogs.vintage.values >= (analogs.vintage.max() - 8)   # trend over the last eight vintages: older gains (2014-2018 lateral length) are not what a well drilled now inherits
    ok = np.isfinite(cum12) & (cum12 > 0) & analogs.vintage.notna().values & recent
    if ok.sum() < 15 or analogs.vintage[ok].nunique() < 3:
        return 0.0, 0.0, int(ok.sum())
    x = analogs.vintage.values[ok].astype(float); y = np.log(cum12[ok]); w = weights[ok]
    xm = np.average(x, weights=w); ym = np.average(y, weights=w)
    sxx = np.sum(w * (x - xm) ** 2)
    if sxx <= 0:
        return 0.0, 0.0, int(ok.sum())
    slope = np.sum(w * (x - xm) * (y - ym)) / sxx
    resid = y - (ym + slope * (x - xm))
    dof = max(ok.sum() - 2, 1)
    se = float(np.sqrt(np.sum(w * resid ** 2) / dof / sxx))
    sig = abs(slope) / se > 2 if se > 0 else False
    return (float(np.clip(slope, -0.15, 0.15)) if sig else 0.0), se, int(ok.sum())


def calibrate(analogs: pd.DataFrame, hist: dict[str, np.ndarray], product: str, k: int = 12, h: int = 24) -> Calibration:
    Q = hist[product]
    w = analogs.weight.values
    res = hindcast(Q, k=k, h=h)
    pools = {a: hindcast(Q, k=a, h=h) for a in ANCHOR_AGES}
    pools[k] = res if k in pools else pools.get(k, res)
    loo = loo_error(Q, w, h=h)
    slope, se, nv = vintage_trend(analogs, Q, w)
    return Calibration(k_fit=k, horizon=h, residuals=res, n_hindcast=int(len(res)), pools=pools, loo_residuals=loo, n_loo=int(len(loo)),
                       vintage_slope=slope, vintage_slope_se=float(se), vintage_n=nv, effective_n=float(1.0 / np.sum(w ** 2)) if len(w) else 0.0)


def vintage_target(analogs: pd.DataFrame, min_wells: int = 30) -> int | None:
    """Newest first-production year with at least `min_wells` analogs: the level older analogs are scaled to.
    (Scaling to the calendar year would extrapolate the trend beyond the data.)"""
    if "vintage" not in analogs or analogs.vintage.isna().all():
        return None
    counts = analogs.vintage.value_counts()
    good = counts[counts >= min_wells]
    return int(good.index.max()) if len(good) else int(analogs.vintage.max())


def vintage_weights(analogs: pd.DataFrame, half_life_years: float) -> np.ndarray:
    """Distance weights times a recency decay: an analog `d` years older than the newest loses 50% per half-life."""
    w = analogs.weight.values.astype(float)
    if half_life_years and half_life_years > 0 and analogs.vintage.notna().any():
        age = analogs.vintage.max() - analogs.vintage.fillna(analogs.vintage.min()).values
        w = w * np.power(0.5, age / half_life_years)
    return w / w.sum()
