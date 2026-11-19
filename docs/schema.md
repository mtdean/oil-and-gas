# Canonical schema

All tables live in `data/parquet/<table>/source=<source>/*.parquet` and are exposed
as DuckDB views of the same name. Columns a source cannot fill are NULL; anything
source-specific is in `extra` (JSON string).

## production_monthly
| column | notes |
|---|---|
| entity_id | source-native id of the reporting unit: API14 (ND, PA, WV, OH), `API14:formation` (CO, UT, MT, WY, NM), `O-dist-lease` / `G-dist-lease` (TX), LEASE_KID (KS), `lease:api12:completion` (BSEE) |
| entity_type | `well`, `well_formation`, `lease`, `luw`, `pun`, `completion` |
| api14 / api10 | normalised digits-only API; api10 = state(2)+county(3)+well(5). NULL for TX oil leases, KS leases |
| state | postal code; `GOM` for federal offshore |
| period | first day of the reporting period |
| period_months | 1 (monthly, default), 3 (OH quarterly), 6/12 (PA semiannual/annual, OH annual, KS annual rows). Filter `coalesce(period_months,1)=1` for true monthly series |
| oil_bbl, gas_mcf, water_bbl, ngl_bbl | volumes. TX: oil leases → oil=LEASE_OIL, gas=casinghead; gas wells → gas=LEASE_GAS, oil=condensate. PA: oil = oil + condensate |
| days_on | producing days |
| oil_sold_bbl, gas_sold_mcf, gas_flared_mcf | where reported (ND, CO, TX dispositions) |
| operator, well_name, county, field, formation | as reported for that period (operator changes are preserved) |

## wells
One row per well per source. Join across sources on `api10`. `well_locations` view
picks the best lat/lon per api10 (state regulator preferred over FracFocus).
`crs_note` records the datum as published (mostly NAD83; KS and BSEE NAD27; TX provides NAD83 columns).

## entity_wells
Membership of wells in production-reporting entities: TX lease→API (from OG_WELL_COMPLETION),
LA LUW→API (current only), OK PUN→API (from 1002A completions), KS lease→API.
`valid_from/valid_to` where known (KS lease years, OK completion date).

## completions
FracFocus disclosures (one per frac job): job_start/end, base water/non-water gallons, TVD, lat/lon, federal/Indian flags.

## macro_series
`series_id` is namespaced: `eia:<EIA series id>`, `fred:<id>`, `cftc:<contract code>:<column>`,
`onrr:...`, `dfed:<sheet>:<code>`, `bh:...`. `frequency` ∈ D/W/M/Q/A. `region` where meaningful.
