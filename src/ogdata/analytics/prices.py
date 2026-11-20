"""Price decks and stochastic price paths from the macro warehouse.

Base deck  : EIA STEO monthly WTI (STEO.WTIPUUS) and Henry Hub (STEO.NGHHUUS) forecasts, extended
             flat (nominal) past the STEO horizon; recent months are overwritten with realised
             monthly averages of the daily spot series.
Vol / corr : annualised volatility and correlation of monthly log returns of the spot series.
Paths      : mean-reverting (Ornstein-Uhlenbeck in log space) around the deck, so long-dated
             months hug the deck while near months carry spot-like volatility.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

WTI_SPOT, HH_SPOT = "eia:PET.RWTC.D", "eia:NG.RNGWHHD.D"
WTI_STEO, HH_STEO = "eia:STEO.WTIPUUS.M", "eia:STEO.NGHHUUS.M"


@dataclass
class Deck:
    months: pd.DatetimeIndex
    wti: np.ndarray
    hh: np.ndarray
    vol_oil: float
    vol_gas: float
    rho: float
    source: str

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({"month": self.months, "wti": self.wti, "hh": self.hh})


def _monthly_avg(con, series: str) -> pd.Series:
    df = con.execute("SELECT date_trunc('month', period) m, avg(value) v FROM macro_series WHERE series_id = ? AND value IS NOT NULL GROUP BY 1 ORDER BY 1", [series]).df()
    return pd.Series(df.v.values, index=pd.DatetimeIndex(df.m))


def load_deck(con: duckdb.DuckDBPyConnection, start: pd.Timestamp | None = None, horizon: int = 360,
              flat_after_steo: bool = True, escalation: float = 0.0) -> Deck:
    start = pd.Timestamp(start or pd.Timestamp.today()).to_period("M").to_timestamp()
    months = pd.date_range(start, periods=horizon, freq="MS")
    out = {}
    for key, spot_id, steo_id, fallback in (("wti", WTI_SPOT, WTI_STEO, 70.0), ("hh", HH_SPOT, HH_STEO, 3.5)):
        spot = _monthly_avg(con, spot_id)
        steo = _monthly_avg(con, steo_id)
        s = pd.Series(np.nan, index=months)
        s.update(steo.reindex(months))
        s.update(spot.reindex(months))        # realised months win
        if s.isna().all():
            s[:] = fallback
        last = s.last_valid_index()
        s = s.ffill()
        if not flat_after_steo or escalation:
            k = np.maximum((np.arange(horizon) - months.get_loc(last)), 0) / 12
            s = s * (1 + escalation) ** k
        out[key] = s.values.astype(float)
    # vol & correlation from monthly log returns of spot, last 10 years
    wti = _monthly_avg(con, WTI_SPOT)
    hh = _monthly_avg(con, HH_SPOT)
    j = pd.concat([wti, hh], axis=1, keys=["wti", "hh"]).dropna()
    j = j[j.index >= j.index.max() - pd.DateOffset(years=10)]
    j = j[(j > 0).all(axis=1)]
    r = np.log(j).diff().dropna()
    vol_oil = float(r.wti.std() * np.sqrt(12)) if len(r) > 24 else 0.35
    vol_gas = float(r.hh.std() * np.sqrt(12)) if len(r) > 24 else 0.60
    rho = float(r.corr().iloc[0, 1]) if len(r) > 24 else 0.3
    return Deck(months, out["wti"], out["hh"], min(vol_oil, 0.9), min(vol_gas, 1.2), rho, "EIA STEO + spot")


def simulate_paths(deck: Deck, sims: int, rng: np.random.Generator, half_life_months: float = 18.0,
                   vol_scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """(sims, H) oil and gas price paths, mean-reverting around the deck in log space."""
    H = len(deck.months)
    kappa = np.log(2) / half_life_months
    s_o = deck.vol_oil * vol_scale / np.sqrt(12)
    s_g = deck.vol_gas * vol_scale / np.sqrt(12)
    z1 = rng.standard_normal((sims, H))
    z2 = deck.rho * z1 + np.sqrt(1 - deck.rho ** 2) * rng.standard_normal((sims, H))
    mu_o, mu_g = np.log(deck.wti), np.log(np.maximum(deck.hh, 0.05))
    xo = np.empty((sims, H)); xg = np.empty((sims, H))
    xo[:, 0] = mu_o[0]; xg[:, 0] = mu_g[0]
    for t in range(1, H):
        xo[:, t] = xo[:, t - 1] + kappa * (mu_o[t] - xo[:, t - 1]) + s_o * z1[:, t]
        xg[:, t] = xg[:, t - 1] + kappa * (mu_g[t] - xg[:, t - 1]) + s_g * z2[:, t]
    # bias-correct so the mean path equals the deck
    xo -= (s_o ** 2) * (1 - np.exp(-2 * kappa * np.arange(H))) / (4 * kappa)
    xg -= (s_g ** 2) * (1 - np.exp(-2 * kappa * np.arange(H))) / (4 * kappa)
    return np.exp(xo), np.exp(xg)
