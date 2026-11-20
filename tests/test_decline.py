import numpy as np

from ogdata.analytics import decline, economics


def test_grid_fit_recovers_hyperbolic_parameters():
    rng = np.random.default_rng(0)
    t = np.arange(60)
    qi, di, b = 12000.0, 0.15, 0.9
    q = decline.rate(qi, di, b, t, dmin=1e-9) * np.exp(rng.normal(0, 0.05, 60))
    Q = np.vstack([q, q])
    Q[1, 10:14] = np.nan  # shut-in months are ignored
    fit = decline.fit_grid(Q)
    assert fit.ok.all()
    assert abs(fit.qi[0] / qi - 1) < 0.15
    assert abs(fit.b[0] - b) <= 0.2
    assert abs(np.log(fit.di[0] / di)) < 0.4
    assert fit.n[1] == 56


def test_modified_arps_switches_to_terminal_decline():
    t = np.arange(0, 600)
    q = decline.rate(1000.0, 0.3, 1.0, t)
    late = q[400:]
    monthly_decline = 1 - late[1:] / late[:-1]
    assert np.allclose(monthly_decline, 1 - np.exp(-decline.DMIN_MONTHLY), atol=1e-6)
    assert np.all(np.diff(q) <= 0)


def test_forecast_from_anchor_continues_level():
    f = decline.forecast_from_anchor(500.0, 24.0, 0.2, 0.8, 12)
    assert f.shape == (1, 12)
    assert f[0, 0] < 500.0 and f[0, 0] > 400.0
    assert np.all(np.diff(f[0]) < 0)


def test_cashflow_npv_irr_sign():
    oil = decline.rate(15000.0, 0.25, 1.0, np.arange(240))[None, :]
    gas = oil * 2.0
    a = economics.Assumptions(state="TX", capex_mm=8.0)
    cf = economics.cashflows(oil, gas, np.full(oil.shape, 75.0), np.full(oil.shape, 3.0), a)
    assert cf.net_cf[0, 0] < 0  # capex month
    npv = economics.npv(cf.net_cf, 0.10)[0]
    irr = economics.irr(cf.net_cf)[0]
    assert np.isfinite(irr)
    assert (npv > 0) == (irr > 0.10)
    be = economics.breakeven_wti(oil, gas, np.full(oil.shape, 0.04), a)
    assert 20 < be < 150
