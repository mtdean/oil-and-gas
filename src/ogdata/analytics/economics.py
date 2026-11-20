"""Single-well cash-flow engine (pre-tax, unlevered, nominal dollars) vectorised over scenarios.

All production/price inputs are arrays of shape (S, H): S scenarios x H months.
Severance tax defaults are statutory headline rates and ignore incentives (e.g. OK first-36-month
rate, MT/ND holidays, TX high-cost gas); override per deal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

import numpy as np

# (oil % of revenue, gas % of revenue, note)
STATE_TAX = {
    "TX": (0.046, 0.075, "TX oil 4.6%, gas 7.5% (high-cost gas reductions ignored)"),
    "NM": (0.079, 0.079, "NM severance 3.75% + school 3.15% + conservation ~0.19% + ad valorem prod ~1%"),
    "ND": (0.10, 0.05, "ND 5% extraction + 5% production on oil; gas taxed per mcf (~flat), approximated 5%"),
    "CO": (0.04, 0.04, "CO severance graduated 2-5% less ad valorem credit; ~4% effective"),
    "WY": (0.06, 0.06, "WY 6% severance (+ ad valorem ~6-7% handled in ad_valorem)"),
    "OK": (0.07, 0.07, "OK 7% (5% first 36 months for wells spud after Jul-2015)"),
    "UT": (0.04, 0.04, "UT 3% to $13/bbl, 5% above; ~4%"),
    "MT": (0.09, 0.09, "MT 9% after 12-18 month 0.5% holiday for horizontal wells"),
    "LA": (0.125, 0.04, "LA 12.5% oil; gas $/mcf rate ~ 4% equivalent"),
    "KS": (0.08, 0.08, "KS 8% severance less 3.67% property-tax credit; exemptions for low-volume wells"),
    "PA": (0.0, 0.0, "PA has no severance tax; impact fee ~ $45-60k/yr declining, put in loe_fixed"),
    "WV": (0.05, 0.05, "WV 5% severance"),
    "OH": (0.01, 0.01, "OH ~$0.10/bbl, $0.025/mcf -> ~1%"),
    "CA": (0.0, 0.0, "CA no severance; ad valorem ~1% of reserve value, assessment fee"),
    "AK": (0.0, 0.0, "AK production tax is net-profits based; model separately"),
    "GOM": (0.0, 0.0, "Federal offshore: no state tax; royalty 18.75% (set nri)"),
}
# Typical 2024-26 D&C+facilities cost for a modern horizontal well, $MM, by state (used when capex not given).
STATE_CAPEX_MM = {"TX": 8.0, "NM": 8.5, "ND": 7.5, "CO": 5.5, "WY": 7.0, "OK": 7.5, "UT": 6.0, "MT": 7.5, "LA": 11.0, "PA": 9.0, "WV": 9.0, "OH": 9.0, "KS": 1.2, "CA": 2.0}


@dataclass
class Assumptions:
    state: str = "TX"
    nri: float = 0.75                # net revenue interest (1 - royalty burden) on a 100% WI basis
    wi: float = 1.0                  # working interest
    oil_diff: float = -3.0           # $/bbl vs WTI
    gas_diff: float = -0.75          # $/mmbtu vs Henry Hub (basis)
    btu_factor: float = 1.10         # mmbtu per mcf at the wellhead
    gas_shrink: float = 0.85         # fraction of wellhead gas sold as residue after processing
    ngl_yield_bbl_per_mmcf: float = 60.0   # barrels of NGL per mmcf of wellhead gas (0 for dry gas plays)
    ngl_price_pct_wti: float = 0.33
    sev_tax_oil: float | None = None  # fraction of revenue; None -> STATE_TAX
    sev_tax_gas: float | None = None
    ad_valorem: float = 0.02          # fraction of revenue
    loe_fixed_month: float = 8000.0   # $/well-month
    loe_var_per_boe: float = 4.0      # $/boe
    water_cost_per_bbl: float = 0.0   # if a water forecast is supplied
    gathering_per_mcf: float = 0.35
    capex_mm: float | None = None     # $MM at month 0 (None -> STATE_CAPEX_MM) ; 0 for PDP
    abandonment: float = 100_000.0    # $ at end of life
    discount_rate: float = 0.10       # annual
    econ_limit_months: int = 6        # shut in after this many consecutive negative operating months
    min_life_months: int = 12

    def resolved(self) -> "Assumptions":
        so, sg, _ = STATE_TAX.get(self.state, (0.05, 0.05, ""))
        return replace(self, sev_tax_oil=self.sev_tax_oil if self.sev_tax_oil is not None else so,
                       sev_tax_gas=self.sev_tax_gas if self.sev_tax_gas is not None else sg,
                       capex_mm=self.capex_mm if self.capex_mm is not None else STATE_CAPEX_MM.get(self.state, 7.0))

    def to_dict(self) -> dict:
        d = asdict(self.resolved())
        d["sev_tax_note"] = STATE_TAX.get(self.state, (0, 0, "default 5%"))[2]
        return d


@dataclass
class CashFlows:
    revenue: np.ndarray
    taxes: np.ndarray
    opex: np.ndarray
    operating_cf: np.ndarray     # after royalty, taxes, opex; before capex
    net_cf: np.ndarray           # operating_cf - capex - abandonment
    alive: np.ndarray            # bool mask of producing months
    oil_sold: np.ndarray
    gas_sold: np.ndarray
    life_months: np.ndarray


def cashflows(oil: np.ndarray, gas: np.ndarray, p_oil: np.ndarray, p_gas: np.ndarray, a: Assumptions,
              water: np.ndarray | None = None, capex_override: np.ndarray | None = None) -> CashFlows:
    a = a.resolved()
    oil = np.atleast_2d(oil); gas = np.atleast_2d(gas)
    S, H = oil.shape
    p_oil = np.broadcast_to(np.atleast_2d(p_oil), (S, H)); p_gas = np.broadcast_to(np.atleast_2d(p_gas), (S, H))
    oil = np.nan_to_num(oil); gas = np.nan_to_num(gas)
    gas_sold = gas * a.gas_shrink
    ngl = gas / 1000 * a.ngl_yield_bbl_per_mmcf
    rev_oil = oil * np.maximum(p_oil + a.oil_diff, 0)
    rev_gas = gas_sold * np.maximum(p_gas * a.btu_factor + a.gas_diff, 0)
    rev_ngl = ngl * np.maximum(p_oil * a.ngl_price_pct_wti, 0)
    gross = rev_oil + rev_gas + rev_ngl
    taxes = (rev_oil + rev_ngl) * a.sev_tax_oil + rev_gas * a.sev_tax_gas + gross * a.ad_valorem
    net_rev = (gross - taxes) * a.nri * a.wi
    boe = oil + gas / 6
    opex = (a.loe_fixed_month + a.loe_var_per_boe * boe + a.gathering_per_mcf * gas
            + (a.water_cost_per_bbl * np.nan_to_num(water) if water is not None else 0.0)) * a.wi
    producing = (boe > 0)
    op_cf = np.where(producing, net_rev - opex, 0.0)
    # economic limit: first month after min_life where the trailing window is all negative
    neg = (op_cf < 0) & producing
    k = a.econ_limit_months
    run = np.zeros((S, H), dtype=np.int32)
    for t in range(H):
        run[:, t] = np.where(neg[:, t], (run[:, t - 1] if t else 0) + 1, 0)
    hit = (run >= k) & (np.arange(H)[None, :] >= a.min_life_months)
    first_hit = np.where(hit.any(1), hit.argmax(1), H)
    last_prod = np.where(producing.any(1), H - 1 - np.argmax(producing[:, ::-1], axis=1), 0)
    life = np.minimum(first_hit, last_prod + 1)
    alive = np.arange(H)[None, :] < life[:, None]
    op_cf = np.where(alive, op_cf, 0.0)
    net = op_cf.copy()
    capex = (capex_override if capex_override is not None else np.full(S, a.capex_mm * 1e6)) * a.wi
    net[:, 0] -= capex
    end_idx = np.clip(life - 1, 0, H - 1)
    net[np.arange(S), end_idx] -= a.abandonment * a.wi
    return CashFlows(revenue=np.where(alive, gross, 0), taxes=np.where(alive, taxes, 0), opex=np.where(alive, opex, 0),
                     operating_cf=op_cf, net_cf=net, alive=alive, oil_sold=np.where(alive, oil, 0), gas_sold=np.where(alive, gas_sold, 0), life_months=life)


def npv(cf: np.ndarray, annual_rate: float) -> np.ndarray:
    H = cf.shape[-1]
    disc = (1 + annual_rate) ** (-np.arange(H) / 12)
    return cf @ disc


def irr(cf: np.ndarray, hi: float = 20.0, iters: int = 60) -> np.ndarray:
    """Annualised IRR by bisection. Positive IRRs are searched on [0, hi]; if the undiscounted sum is
    negative the search runs on [-0.9, 0]. NaN where no sign change exists (never pays back)."""
    cf = np.atleast_2d(cf)
    S, H = cf.shape
    t = np.arange(H) / 12
    f = lambda r: (cf * (1 + r[:, None]) ** (-t[None, :])).sum(1)
    f0 = f(np.zeros(S))
    lo_a = np.where(f0 > 0, 0.0, -0.9)
    hi_a = np.where(f0 > 0, hi, 0.0)
    f_lo = f(lo_a)
    f_hi = f(hi_a)
    ok = np.sign(f_lo) != np.sign(f_hi)
    for _ in range(iters):
        mid = (lo_a + hi_a) / 2
        f_mid = f(mid)
        same = np.sign(f_mid) == np.sign(f_lo)
        lo_a = np.where(same, mid, lo_a); f_lo = np.where(same, f_mid, f_lo)
        hi_a = np.where(same, hi_a, mid)
    return np.where(ok & (cf < 0).any(1) & (cf > 0).any(1), (lo_a + hi_a) / 2, np.nan)


def payout_month(cf: np.ndarray) -> np.ndarray:
    c = np.cumsum(np.atleast_2d(cf), axis=1)
    pos = c > 0
    return np.where(pos.any(1), pos.argmax(1), np.nan)


def breakeven_wti(oil: np.ndarray, gas: np.ndarray, hh_ratio: np.ndarray, a: Assumptions, target_rate: float | None = None) -> float:
    """Flat WTI at which NPV(discount_rate)=0 for one production profile; gas price = WTI * hh_ratio (per month)."""
    oil = np.atleast_2d(oil); gas = np.atleast_2d(gas)
    lo, hi = 5.0, 400.0
    rate = a.discount_rate if target_rate is None else target_rate
    for _ in range(50):
        mid = (lo + hi) / 2
        v = npv(cashflows(oil, gas, np.full(oil.shape, mid), np.full(oil.shape, mid) * hh_ratio, a).net_cf, rate)[0]
        lo, hi = (mid, hi) if v < 0 else (lo, mid)
    return (lo + hi) / 2
