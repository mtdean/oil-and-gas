import numpy as np
import pandas as pd

from ogdata.analytics import calibration, decline


def _synthetic(n=60, months=72, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(months)
    Q = np.full((n, months), np.nan)
    vint = rng.integers(2015, 2024, n)
    for i in range(n):
        qi = 8000 * np.exp(rng.normal(0, 0.3)) * (1.03 ** (vint[i] - 2015))  # 3%/yr vintage uplift
        Q[i] = decline.rate(qi, 0.12, 0.9, t, dmin=1e-9) * np.exp(rng.normal(0, 0.08, months))
        Q[i, rng.integers(40, months):] = np.nan  # histories of different lengths
    return Q, vint


def test_hindcast_residuals_centre_near_zero_for_arps_wells():
    Q, _ = _synthetic()
    res = calibration.hindcast(Q, k=12, h=24)
    assert len(res) >= 30
    assert abs(np.median(res)) < 0.15          # the method is unbiased on wells that really are hyperbolic
    assert np.std(res) < 0.4


def test_loo_error_spread_reflects_between_well_dispersion():
    Q, _ = _synthetic()
    w = np.ones(len(Q)) / len(Q)
    res = calibration.loo_error(Q, w, h=24)
    assert len(res) >= 30
    assert 0.1 < np.std(res) < 0.6             # wells differ by ~30% in qi, so the leave-one-out error should too


def test_vintage_trend_detects_uplift():
    Q, vint = _synthetic(n=120, seed=3)
    an = pd.DataFrame({"vintage": vint, "weight": np.ones(len(Q)) / len(Q)})
    slope, se, n = calibration.vintage_trend(an, Q, an.weight.values)
    assert n >= 100
    assert 0.0 < slope < 0.08                  # true uplift is log(1.03) ~ 0.03/yr


def test_calibration_sampler_falls_back_when_sparse():
    c = calibration.Calibration(k_fit=12, horizon=24, residuals=np.array([]), n_hindcast=0, loo_residuals=np.array([]), n_loo=0,
                                vintage_slope=0.0, vintage_slope_se=0.0, vintage_n=0, effective_n=10.0)
    s = c.sample(np.random.default_rng(0), 5000)
    assert abs(s.mean()) < 0.03 and abs(s.std() - 0.35) < 0.03


def test_vintage_weights_decay():
    an = pd.DataFrame({"weight": [0.5, 0.5], "vintage": [2024, 2018]})
    w = calibration.vintage_weights(an, 6.0)
    assert abs(w[0] / w[1] - 2.0) < 1e-9       # six years older at a six-year half-life = half the weight
    assert abs(w.sum() - 1) < 1e-12
