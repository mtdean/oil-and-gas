# oil-and-gas — US well-level production, well locations, and energy macro data

A Python pipeline that pulls public oil & gas data from every US state regulator
that publishes bulk files, plus federal offshore (BSEE), FracFocus, and macro
sources (EIA, CFTC, ONRR, Dallas Fed, FRED), normalises everything into a handful
of shared tables, stores it as Parquet, and exposes it through DuckDB for analysis.
Built for underwriting credit and equity exposure to specific wells.

```
ogdata list                      # catalogue of sources and their status
ogdata update                    # fetch + transform every active source (incremental)
ogdata update nd tx --force      # rebuild specific sources from scratch
ogdata status                    # row counts per table/source
ogdata sql "select ... "         # query the DuckDB warehouse
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env             # optional: EIA_API_KEY, FRED_API_KEY (free)
.venv/bin/ogdata update --limit-years 2    # smoke test: last 2 years of everything
.venv/bin/ogdata update                    # full history (Texas alone is ~3.5 GB download / ~1 hr)
.venv/bin/ogdata sql "select * from production_by_state_month where state='ND' order by period desc limit 12"
```

Data lands in `data/` (git-ignored; set `OGDATA_DATA=/some/big/disk` to move it):

```
data/raw/<source>/...                 original files exactly as downloaded (kept for audit/replay)
data/parquet/<table>/source=<src>/    normalised Parquet, one directory per source
data/warehouse.duckdb                 DuckDB views over the Parquet (rebuilt by `ogdata build-db`)
data/manifest.json                    ETag/Last-Modified/sha256 per URL (drives incremental fetch)
data/runs.jsonl                       one line per source run (status, rows, seconds, errors)
```

## Tables (see docs/schema.md)

| table | grain | what |
|---|---|---|
| `production_monthly` | entity × period | oil / gas / water / days, with `entity_type` = well, well_formation, lease, luw, pun, completion and `period_months` (1 monthly; 3/12 for OH, PA-conventional, KS annual rows) |
| `wells` | well × source | API numbers, operator, county, field, **lat/lon**, type, status, spud / completion / first-prod dates, depth |
| `entity_wells` | entity × well | lease/LUW/PUN → API membership for states that don't report per well (TX, LA, OK, KS) |
| `completions` | frac job | FracFocus disclosures: job dates, water volumes, TVD, lat/lon |
| `macro_series` | series × period | long-format time series: prices, production by state, stocks, rigs, DUCs, STEO forecasts, COT positioning, credit spreads, surveys |

Convenience views: `production_annual`, `production_by_state_month`, `well_locations`
(best coordinates per API10 across sources), `macro_latest`.

## What "well-level" really means, state by state

Not every state reports production per well. This is the single most important
thing to know before underwriting a well from public data:

| state | production grain | history | lat/lon | source |
|---|---|---|---|---|
| ND | **well** (API14), monthly | 2015-05 | in production file | NDIC monthly xlsx |
| NM | well × pool, monthly | 1973 | wells hub (NAD83) | OCD FTP + ArcGIS hub |
| CO | well × formation, monthly | 1999 | wells shapefile | ECMC |
| UT | well × wellbore × formation, monthly | 1984 | wells csv | DOGM |
| MT | well × formation, monthly | 1986 | well list | MBOGC |
| WY | well × formation, monthly (in annual files) | 1978 | in production file | WOGCC |
| PA | well, monthly unconventional (2015+), annual conventional (1980+) | 1980 | in production file | DEP Greenport |
| WV | well, monthly columns in annual files | 1985 | TAGIS GIS | DEP |
| OH | well, **annual** (vertical) / **quarterly** (horizontal) | 2010 | ODNR hub | ODNR |
| TX | **lease** for oil (many wells), well for gas; monthly | 1993 | county shapefiles | RRC PDQ dump |
| KS | **lease**, monthly | 1980 | wells file (NAD27) | KGS |
| OK | **not published** (Tax Commission PUN data by request); wells + well→PUN map only | — | RBDMS csv | OCC |
| LA | **not public in bulk** (paid DENR subscription, LUW grain); wells + API→LUW map only | — | SONRIS GIS | DENR |
| CA | production bulk files are email-request only and end in 2021; wells only | — | CalGEM GIS | CalGEM |
| AK | Access .mdb extract (not yet parsed) | — | — | AOGCC |
| Federal offshore GOM | **completion**, monthly | 1947 | borehole file | BSEE OGOR-A |

Texas is the big caveat: RRC oil volumes are per lease. `entity_wells` carries the
lease→API membership so volumes can be allocated (by well count, by gas-well test
rates, or by FracFocus completion intensity) downstream. Gas wells in Texas are
one well per "lease" and are joined to their API directly.

## Macro coverage

* **EIA bulk** (no key): WTI/Brent/Henry Hub daily spot; crude & gas production by state
  monthly (1920/1973+); weekly stocks, field production, refinery inputs, imports/exports;
  rotary rig counts (1973+); STEO history + forecasts incl. regional rigs, new wells
  drilled/completed, **DUCs**, tight-oil and shale-gas production by play.
* **EIA API v2** (free key): same series, fresher, plus anything else you add to `PULLS`.
* **CFTC COT** disaggregated futures for crude, gas, products (2010+).
* **ONRR** federal/Native American production monthly (national) and annual (county).
* **Dallas Fed Energy Survey** quarterly indexes and E&P/service breakdowns.
* **FRED** (free key): WTI/Brent/HH mirrors, Treasuries, **HY and CCC OAS**, dollar index,
  oil & gas PPI and employment.
* **Baker Hughes** weekly rigs by state/basin/trajectory: manual xlsx drop-in (site blocks scripts).

## Scheduling

`scripts/update.sh` runs an incremental update and writes to `logs/`. Install the
daily launchd job on macOS with `scripts/install_launchd.sh` (02:30 local), or use cron:

```
30 2 * * * cd /Users/td/oil-and-gas && ./scripts/update.sh
```

Incremental logic: every URL is fetched with If-None-Match / If-Modified-Since and
hashed; historical files older than ~2 years are treated as immutable; sources with
per-month/per-year files only re-transform the files that changed.

## Adding a source

Subclass `ogdata.sources.base.Source`, implement `fetch()` (download raw files,
return the changed ones) and `transform()` (yield `(table, partition, DataFrame)`),
register it in `registry.py`. `storage.write_sql` is available when a file is too
big for pandas (Texas and New Mexico use it).

See `docs/sources.md` for verified URLs, formats, cadences, and gotchas per source.

## Analytics: decline curves, forecasts, Monte Carlo, dashboard

```
ogdata analytics                      # well_monthly, well_decline (Arps fits), type_curves, well_value
ogdata forecast --lat 32.3 --lon -103.6 --sims 5000 --html out.html   # lat/lon -> analogs -> P10/P50/P90 + economics
ogdata forecast --api10 3002543210 --html well.html                   # existing well PDP forecast
ogdata dashboard                      # dashboards/og_monitor.html (Bloomberg-style monitor)
ogdata sql "select * from well_decline where state='ND' and vintage>=2020 limit 5"
```

Method, tables and assumptions: [docs/analytics.md](docs/analytics.md).
