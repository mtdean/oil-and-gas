# AGENTS.md — handoff notes for coding agents

This file is for an AI coding agent (Claude Code, Codex, Cursor, …) picking up this repo on a new
machine. Read it top to bottom before running anything. The goal of the owner is to be able to say
"rebuild the database" and have you do it with no further input.

## 1. What this is

`ogdata` is a Python data pipeline that downloads US and Canadian oil & gas **well-level monthly
production**, **well locations (lat/lon)** and **energy macro series** from public regulators, normalises
them into a parquet lake queried through DuckDB views, then builds an analytics layer (decline fits,
type curves, PDP valuation, lat/lon Monte Carlo forecasts) and a Bloomberg-style HTML dashboard.
The owner uses it to underwrite credit and equity tied to specific wells.

Nothing here is secret or licensed except where noted in §6. All sources are public downloads.

## 2. Repo map

| Path | What |
|---|---|
| `src/ogdata/cli.py` | `ogdata` commands: `check`, `list`, `update`, `status`, `build-db`, `sql`, `analytics`, `forecast`, `dashboard` |
| `src/ogdata/sources/*.py` | one connector per source (`fetch()` + `transform()`), registered in `registry.py` |
| `src/ogdata/schema.py` | canonical tables (`wells`, `production_monthly`, `completions`, `entity_wells`, `macro_series`) |
| `src/ogdata/storage.py` | parquet writer + DuckDB views (`data/parquet/<table>/source=<src>/*.parquet`) |
| `src/ogdata/http.py` | conditional downloads with a manifest (`data/manifest.json`), retries, hashing |
| `src/ogdata/paths.py` | paths, `.env` loading, open-file-limit raise |
| `src/ogdata/analytics/` | `wellcurves` (well_monthly / decline / type curves), `valuation`, `forecast`, `economics`, `prices`, `dashboard`, `report` |
| `docs/sources.md` | **every URL, format, quirk and dead end per source** — the first place to look when a connector breaks |
| `docs/schema.md`, `docs/queries.md`, `docs/analytics.md` | table definitions, example SQL, analytics method |
| `scripts/update.sh` | nightly job (update → analytics → dashboard); `scripts/rebuild_all.sh` full rebuild from nothing |
| `tests/` | `pytest` (schema, decline fit, Canada UWI/units helpers) |
| `data/` | git-ignored. `raw/` (~10 GB), `parquet/` (~4 GB incl. 0.8 GB analytics), `warehouse.duckdb`, `runs.jsonl`, `manifest.json` |
| `dashboards/` | git-ignored HTML output |

## 3. Setting up a new machine

```bash
git clone <repo> oil-and-gas && cd oil-and-gas
python3 -m venv .venv && .venv/bin/pip install -e .        # Python 3.12+ (developed on 3.15), ~1 min
cp .env.example .env                                        # then edit: EIA_API_KEY (optional), FRED_API_KEY
.venv/bin/ogdata check                                      # tells you what is missing before you spend hours downloading
```

* **Disk:** 20 GB free. Texas alone is 3.6 GB raw. Put `OGDATA_DATA=/some/big/disk/ogdata` in `.env` to move `data/`.
* **Memory:** analytics sets a 10 GB DuckDB limit and 6 threads; adjust in `wellcurves.build_all` if the box is smaller.
* **Keys:** `FRED_API_KEY` (free, https://fredaccount.stlouisfed.org/apikeys) is needed for credit spreads on the
  dashboard; `EIA_API_KEY` is optional (the keyless EIA bulk files cover everything the dashboard uses).
  On the owner's home machine, keys missing from `.env` were read from a sibling repo's `.env`
  (`paths.load_shared_dotenv`); that fallback is harmless when the sibling repo does not exist.
* **Network:** several sources refuse scripted clients (§6). That is expected, not a bug to fix.

## 4. Rebuilding the database from nothing

One command (logs to `logs/rebuild-*.log`, safe to re-run, resumes where it left off because every
download is conditional and every source skips unchanged files):

```bash
./scripts/rebuild_all.sh            # ~2-3 hours wall clock, dominated by Texas and the analytics step
```

What it does, and what to expect if you run the pieces yourself:

| Step | Command | Time | Notes |
|---|---|---|---|
| Macro | `ogdata update eia_bulk cftc dallasfed onrr fred camacro` | 5 min | FRED needs the key; ICE BofA spreads only have ~3 years of history on FRED |
| Small states | `ogdata update nd co wy ut mt ks oh wv` | 15 min | WV is 24M rows from 1985 (Excel 4 files, handled) |
| Pennsylvania | `ogdata update pa` | 15 periods/run | **deliberately throttled** (1 try per period, 5 s apart, stops after 3 refusals). Full history takes several nights; do not loosen it |
| New Mexico, Texas | `ogdata update nm tx` | 1-1.5 h | TX is lease-level (see §7); 3.6 GB download, re-run resumes |
| Canada | `ogdata update bc sk ab` | 10 min | AB/SK Petrinex: 57 monthly zips each from 2022-01; ST37 may 403 (§6) |
| Stubs | `ogdata update ok la ca ak bsee fracfocus bakerhughes` | 1 min | wells/crosswalks only or manual drop-ins (§6) |
| Views | `ogdata build-db` | seconds | recreates DuckDB views over the parquet |
| Analytics | `ogdata analytics` | 25-30 min | well_monthly (staged, atomic swap) → decline fits (1.3M wells) → type curves → PDP valuation |
| Dashboard | `ogdata dashboard` | 1 min | writes `dashboards/og_monitor.html` (self-contained, ~2 MB) |

Then verify (§8). If a source errors, read its traceback in `logs/ogdata-YYYYMMDD.log`, check
`docs/sources.md` for that source, fix the connector, and re-run just that source:
`ogdata update <name>` (add `--force` to refetch, `--transform-only` to re-parse what is already downloaded).

## 5. Keeping it fresh

`scripts/update.sh` is the nightly job: incremental update of all active sources, then analytics and the
dashboard. Install it with `scripts/install_launchd.sh` (macOS, 02:30) or cron. Two things the owner
hit that you must keep:

* **Open-file limit.** launchd/cron shells start with 256 descriptors and DuckDB opens every parquet
  file behind a view. `paths.raise_fd_limit()` and `ulimit -n` in `update.sh` handle it. If you see
  "Too many open files", that is what broke.
* **Atomic rebuilds.** `well_monthly` is built in `_well_monthly_stage/` and swapped in on success. Keep
  that pattern for anything that deletes before it writes.

## 6. Sources that cannot be automated — and the rules

**Do not spoof browser headers, rotate IPs, or otherwise evade bot walls.** The owner has explicitly
ruled that out, and an automated safety check stopped a previous session that drifted that way. When a
site refuses scripted access, the connector makes one polite attempt, logs it, and falls back to a
manual drop-in. Ask the owner to download the file in a browser; do not work around it.

| Source | Situation | Manual path |
|---|---|---|
| **Alaska (`ak`)** | AOGCC refuses every scripted request (403 / connection reset, also from cloud fetchers). Data only exists behind the interactive Data Miner tool; there is no bulk file. | Export the Production and Well tables from AOGCC Data Miner as CSV into `data/raw/ak/` (`production*.csv`, `wells*.csv`); the parser is fuzzy on column names and **untested on real files** — expect a mapping fix the first time. Alaska production tax is net-profits based: `STATE_TAX['AK']` is 0 and PV10 there is not meaningful until modelled separately. |
| **FracFocus** | Resets TCP connections from the owner's home IP after the TLS handshake (works from other networks). | Download `FracFocusCSV.zip` in a browser into `data/raw/fracfocus/`. On a different network it may just work. |
| **Baker Hughes rig count** | Site blocks scripts. | Drop the weekly xlsx into `data/raw/bakerhughes/` (see `docs/sources.md`). |
| **AER ST37 (Alberta well list)** | `static.aer.ca` returned 403 to the pipeline on 2026-11-20 (fine in a browser). | Download `ST_37_Excel.zip` from the ST37 page into `data/raw/ab/`; the connector uses whatever file is present. |
| **Oklahoma (`ok`)** | Well-level production is Tax Commission PUN data, available only by request form. Connector loads wells + PUN crosswalk only. | Owner is handling OK separately. Do not scrape OCC. |
| **Louisiana (`la`)** | Bulk production is a paid DENR subscription; SONRIS terms forbid scraping. Connector loads wells + LUW crosswalk only. | Owner is handling LA separately. |
| **California (`ca`)** | Bulk production is by email request and ends 2021. Low priority per owner. | Wells only. |
| **Kansas (`ks`)** | Loaded, but KGS pre-1987 production is IHS-licensed: **do not redistribute** it. | — |
| **Pennsylvania (`pa`)** | Works, but the owner asked to ping it sparingly. | Leave the throttle alone. |
| **Alberta / Saskatchewan pre-2022** | Public Petrinex extracts start 2022-01; earlier well history is a paid AER / SK Ministry product. | Nothing to do unless the owner buys it. |

## 7. Data semantics you must not get wrong

* **Identifiers.** US wells: `api10` (10-digit API). Canadian wells: the 16-character **UWI is stored in
  `api10`** (and `well_id`), province code (`AB`, `SK`, `BC`) in `state`. Everything downstream joins on `api10`.
* **Texas is lease-level.** RRC publishes oil by lease, not by well. `well_monthly` allocates lease volumes
  to member wells (`alloc` = `direct` | `single` | `lease_alloc`; the last is an equal split and is excluded
  from type curves and analog searches by default). Kansas similar.
* **Ohio is quarterly/annual, PA conventional is annual.** Those well totals are spread evenly across their months
  in `well_monthly` (`alloc` = `q_split` | `a_split`) so the wells reach the PDP valuation and the map; they have no
  real decline shape, so they are excluded from type curves and analogs like `lease_alloc`.
* **Units.** Everything is bbl / mcf / days. Canadian metric volumes are converted (m3 × 6.28981, e3m3 × 35.3147);
  condensate is added to oil.
* **`history_truncated`.** A well whose first observed month is the first month of its jurisdiction's published
  history, and whose well file says it was spud much earlier, is flagged in `well_decline`: vintage comes from
  the well file and it is excluded from type curves/analogs (its early decline was never observed). This is
  most of Alberta/Saskatchewan and all pre-1985 West Virginia.
* **Gulf of Mexico is completion-level** — in `production_monthly` with `entity_type='completion'`, not in `well_monthly`.
* **Economics defaults** (`analytics/economics.py`) are screening placeholders: state severance taxes, Crown
  royalty approximations for Canada, per-state capex and price differentials. PV10 is not a reserve report.

## 8. Verification checklist after a rebuild

```bash
ogdata check                      # keys, drop-ins, last run per source, disk, fd limit
ogdata status                     # rows per table/source
ogdata sql "SELECT state, count(DISTINCT api10) wells, min(period) first, max(period) last FROM production_monthly GROUP BY 1 ORDER BY 1"
ogdata sql "SELECT state, count(*) wells, count(*) FILTER (WHERE status='active') active, count(*) FILTER (WHERE history_truncated) truncated FROM well_decline GROUP BY 1 ORDER BY 1"
ogdata sql "SELECT state, period, round(sum(oil_bbl)/day(last_day(period))/1e3) oil_kbpd FROM production_monthly WHERE period = (SELECT max(period) - INTERVAL 4 MONTH FROM production_monthly WHERE state='TX') GROUP BY 1,2 ORDER BY 3 DESC"
ogdata forecast --lat 32.30 --lon -103.60 --sims 2000 --html /tmp/lea.html      # Lea County NM: ~400 analogs, P50 EUR ~0.5 MMbbl
.venv/bin/python -m pytest -q
```

Sanity magnitudes (as of 2026-11): TX ~3.8 MMb/d oil in `production_monthly`; ND ~1.2; NM ~2.0; AB ~2.6
(includes in-situ bitumen); SK ~0.42; BC gas ~7.5 bcf/d; ~1.3M wells with decline fits; ~700k active.
ND 2018+ median oil EUR ≈ 385 Mbbl / IP90 ≈ 570 b/d is the reference check for the decline fits.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Could not set lock on file data/warehouse.duckdb` | another `ogdata` process holds the DuckDB file. Analytics and dashboard use in-memory connections to avoid this; `ogdata sql` opens read-only. Wait or kill the other process. |
| `Too many open files` | see §5. Run `ulimit -n 65536` in the shell or rely on `paths.raise_fd_limit()`. |
| `OutOfMemoryException` in type curves | lower `memory_limit`/`threads` in `wellcurves.build_all`, or restrict formations (already top-25 per state). |
| 403 / connection reset from a source | §6. One attempt, then manual drop-in. Do not add headers to look like a browser. |
| Petrinex month 404 | that production month is not published yet (released ~4 weeks after month end). Normal. |
| `ogdata update ab` says `unchanged` but you need to rebuild wells | `ogdata update ab --transform-only` re-parses the raw files already on disk. |
| Dashboard looks stale | check `logs/update-*.log` for the last nightly run; `ogdata analytics && ogdata dashboard` rebuilds in ~30 min. |

## 10. Conventions when you change things

* New source: copy `sources/mt.py` (smallest full example), implement `fetch()`/`transform()`, add to
  `registry.py`, document URLs and quirks in `docs/sources.md`, run `ogdata update <name> --limit-years 2` first.
* Never widen the canonical schema for one source; put extras in the `extra` JSON column.
* Keep partitions per source per year so an incremental run rewrites only what changed.
* Run `pytest` and `ogdata check` before handing back. Commit with a message that says which sources/tables changed.
* The dashboard follows the owner's TCRED design tokens (`analytics/assets/theme.css`); do not restyle it.
