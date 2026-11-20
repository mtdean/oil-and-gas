# Analytics layer: well curves, forecasts, Monte Carlo, dashboard

Built by `ogdata analytics` (also run nightly by `scripts/update.sh`). Outputs live in
`data/parquet/analytics/` and are exposed as DuckDB views next to the canonical tables.

## Tables

| view | grain | what |
|---|---|---|
| `well_monthly` | api10 × month | Well-level series. `well` and `well_formation` rows summed to the wellbore. Texas oil leases and Kansas leases are allocated to member wells (`entity_wells`): `alloc = 'single'` when the lease has one well (exact), `'lease_alloc'` when volumes are split equally across the lease's non-shut-in wells (approximate; excluded from type curves and analog selection by default). `'direct'` is a true well-level report. |
| `well_decline` | api10 | First/last production, cum oil/gas/water, IP90 (first 3 producing months, per day), 12-month cum, peak month, Arps fit per product (`*_qi` volume/month at peak, `*_di` nominal monthly decline, `*_b`), first-year effective decline `*_d1_eff`, log-RMSE, anchor rate `*_q_last3`, `*_remaining` and `*_eur` (cum + forecast to 30 years from first production at a 6 %/yr terminal decline and an economic limit of 1 bbl/d or 10 mcf/d), `status` active = produced within 3 months of the state's latest month, `vintage`, `primary_product`. |
| `type_curves` | group × vintage × month | P10/P50/P90/mean monthly volume by month since first production (0–119) for `state`, `state_county` and `state_formation` groups, vintage buckets `2005-09 … 2023+` and `all`; groups with ≥ 10 wells. |
| `well_value` | api10 (active, fitted) | PDP valuation at the base deck: PV10/PV15 of operating cash flow, PV10 at −25 %/+25 % decks, next-12-month cash flow and volumes, remaining volumes, current rates. |
| `state_cashflow` | state × deck × month | Sum of forecast operating cash flow for the next 120 months. |
| `price_deck` | month | WTI / Henry Hub deck used (EIA STEO forecast, then flat), plus the volatility/correlation estimates. |

## Decline fitting

Modified Arps in log space over a grid of `b ∈ [0, 1.5]` (step 0.1) and 44 log-spaced `Di` values;
`log qi` is solved in closed form per grid cell, so a chunk of 40k wells × 240 months is fitted
with two matrix products (the whole 1.0M-well warehouse fits in ~3 minutes). Shut-in months
(zero) are excluded. The fit is aligned on the peak month within the first 12 months. Forecasts
re-anchor the fitted shape on the mean of the last three producing months, which is the usual
PDP practice, and switch to a 6 %/yr exponential once the hyperbolic decline rate falls below it.

Sanity check on the Bakken (ND, 2018+ vintages): median oil EUR 385 Mbbl, IP90 566 bbl/d,
first-year decline 78 %, which lines up with published Bakken type curves.

Caveats: EURs for very young wells (< 12 months) are unstable (wide `b`); lease-allocated TX/KS
wells inherit the lease's shape; Ohio (quarterly/annual) and the Gulf of Mexico (completion-level)
are not in `well_monthly`.

### Truncated history

`well_decline.history_truncated` is true when a well's first observed month is the first month of its
state's published history and the well file says it was spud / first produced more than 6 months earlier
(West Virginia pre-1985 wells, every Alberta and Saskatchewan well older than 2022, ...). These wells take
their vintage from the well file, keep their PDP fit (anchored on the latest months, which is all the
valuation needs) and are excluded from type curves and analog sets, since their early decline was never observed.

### Canadian assumptions

Provinces use Crown-royalty placeholders in `STATE_TAX` (AB 12%/8%, SK 10%/7%, BC 9%/6% of oil/gas revenue),
`STATE_CAPEX_MM` in US$ (AB 6.5, SK 3.0, BC 7.5) and `STATE_DIFF` price bases (light oil −$8 to −$9 vs WTI,
gas −$1.50 to −$1.75 vs Henry Hub). Heavy-oil wells should be run with `--oil-diff` near the current
WCS−WTI differential (ticker and indicators tab). All cash flows are in US$.

## Forecast: `ogdata forecast`

```
ogdata forecast --lat 32.30 --lon -103.60 [--radius 10] [--vintage-min 2014] [--formation BONE]
                [--product oil|gas] [--sims 2000] [--pdp] [--nri .75 --capex-mm 8 --loe-fixed 8000 ...]
                [--json out.json] [--html out.html]
ogdata forecast --api10 3002543210 --html well.html     # existing well: its own decline, no capex
```

1. **Analogs**: wells in `well_decline` ∩ `well_locations` within the radius (expanding ×1.6 up to 50 mi
   until `--min-wells`), vintage ≥ `--vintage-min`, optional formation substring, lease allocations
   excluded unless `--include-lease-alloc`. Weights fall off with distance.
2. **Curves**: P10/P50/P90 of the analogs' actual monthly volumes by month on production, extended to
   30 years with an Arps fit of each percentile curve.
3. **Monte Carlo** (`--sims`): each trial picks an analog (weighted), perturbs its fitted `qi` (±15 % lognormal),
   `Di` (±10 %) and `b` (±0.05), generates 360 months of oil and gas, and draws a correlated price path:
   Ornstein-Uhlenbeck in log space around the deck with 18-month half-life and volatility/correlation
   estimated from 10 years of monthly spot returns.
4. **Economics** (`analytics/economics.py`): revenue (WTI + differential, HH × BTU + basis after shrink,
   NGL yield × % of WTI), severance by state (headline statutory rates, see `STATE_TAX`), ad valorem,
   fixed + variable LOE, gathering, capex at month 0 (state defaults in `STATE_CAPEX_MM`), abandonment,
   shut-in after 6 consecutive negative months. Pre-tax, unlevered, nominal, 10 % discount.
5. **Outputs**: P10/P50/P90/mean of EUR, IP90, NPV10, IRR, payout, life, first-year revenue/CF;
   P(NPV10 > 0); breakeven flat WTI; a 3×3 scenario grid (bear/base/bull production × −25 %/base/+25 % deck);
   tornado sensitivities; the P50 monthly cash-flow table; the analog list. `--html` writes a report with
   fan charts, NPV histogram, cash flow, tornado and an analog map.

## Dashboard: `ogdata dashboard`

Writes `dashboards/og_monitor.html`, a single self-contained page (D3 from cdnjs, data embedded):
ticker (spot prices, rigs, stocks, DUCs, CFTC positioning), production by state, PDP PV10 by state and
10-year cash-flow projection at three decks, price deck, coverage; a 0.2° heat map of wells / PV10 /
12-month cash flow / rates / per-well 12-month cums and EURs; type curves by state and formation with
vintage comparison; vintage productivity tables; leading-indicator small multiples (spot vs STEO,
US production vs STEO, rigs, DUCs, new-well productivity per rig, legacy decline, inventories, CFTC
managed money, Dallas Fed survey, state production); PDP valuation and operator tables.
Styling follows the TCRED situation-monitor design system.
