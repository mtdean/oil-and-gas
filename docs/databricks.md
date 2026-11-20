# Databricks as the back end

Status: **planned, not implemented**. The settings are reserved in `.env.example` so the work-machine setup is
a copy-and-fill; the code still runs entirely on local parquet + DuckDB (`OGDATA_BACKEND=duckdb`, the default).
This note is the design to implement when the switch happens, written so an agent can do it without guessing.

## What stays local, what moves

The pipeline has three layers and only the first two need Databricks:

| Layer | Today | On Databricks |
|---|---|---|
| Raw downloads (`data/raw/`, ~10 GB, regulator files) | local disk | local disk (or a scratch volume); never queried directly |
| Lake (`data/parquet/<table>/source=<src>/*.parquet`, ~4 GB) | DuckDB views over the files | same files mirrored to a Unity Catalog **volume**, registered as external tables |
| Analytics (`data/parquet/analytics/*.parquet`: well_monthly, well_decline, type_curves, well_value, …) | built by DuckDB in-process | still built locally in DuckDB (vectorised numpy fits, ~25 min), then mirrored to the same volume and registered |

Keeping the analytics step in DuckDB is deliberate: the Arps grid fit is numpy over (wells × months) arrays
and the type-curve digests are tuned for a single machine; porting them to Spark is a project, not a port.
Databricks is the shared, governed query surface, and the place other tools (notebooks, SQL dashboards,
BI) read from.

## Settings (`.env`)

| Variable | Meaning |
|---|---|
| `OGDATA_BACKEND` | `duckdb` (default) or `databricks` |
| `DATABRICKS_HOST` | workspace URL |
| `DATABRICKS_TOKEN` **or** `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` | PAT or OAuth service principal; a `DATABRICKS_CONFIG_PROFILE` from `~/.databrickscfg` works instead |
| `DATABRICKS_HTTP_PATH` | SQL warehouse path for `ogdata sql` |
| `DATABRICKS_CATALOG`, `DATABRICKS_SCHEMA` | tables land as `<catalog>.<schema>.<table>` |
| `DATABRICKS_VOLUME` | `/Volumes/<catalog>/<schema>/<volume>` that mirrors `data/parquet/` |

The `databricks-sdk` and `databricks-sql-connector` packages read `DATABRICKS_HOST` / `DATABRICKS_TOKEN` /
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` / `DATABRICKS_CONFIG_PROFILE` from the environment by
themselves, which is why the names above follow their convention.

## Implementation plan (for whoever does it)

1. **Dependencies**: add an optional extra in `pyproject.toml`: `databricks = ["databricks-sdk>=0.40", "databricks-sql-connector>=3.4"]`.
2. **`ogdata publish`** (new command, `src/ogdata/backends/databricks.py`): walk `data/parquet/` and
   `data/parquet/analytics/`, upload changed files to `DATABRICKS_VOLUME` with the Files API
   (`WorkspaceClient().files.upload`), keeping the `table/source=<src>/<partition>.parquet` layout; skip files whose
   size + mtime (or sha256 from `data/manifest.json`) are unchanged. Then run, through the SQL warehouse,
   `CREATE TABLE IF NOT EXISTS <catalog>.<schema>.<table> USING PARQUET LOCATION '<volume>/<table>'` for every
   canonical table and analytics output, followed by `REFRESH TABLE` / `MSCK REPAIR TABLE` so new partitions
   are seen. Hive-style `source=` directories become a partition column, matching the DuckDB views.
3. **`ogdata sql`**: when `OGDATA_BACKEND=databricks`, open `databricks.sql.connect(server_hostname, http_path,
   access_token)` and run the query there, default catalog/schema set with `USE CATALOG` / `USE SCHEMA`; keep the
   DuckDB path for `--local`.
4. **Derived views**: recreate `production_annual`, `production_by_state_month`, `well_locations`, `macro_latest`
   (see `storage._build_derived`) as Databricks views in the schema; the SQL is standard enough to reuse with
   `arg_min/arg_max` replaced by `min_by/max_by`.
5. **Scheduling**: `scripts/update.sh` gains a final `ogdata publish` step when the backend is databricks. A
   Databricks Job can also run the whole thing on a small single-node cluster (clone repo, `pip install -e .`,
   `./scripts/update.sh`), with `OGDATA_DATA` on local disk or a scratch volume; memory limit in
   `wellcurves.build_all` assumes ~16 GB RAM.
6. **Governance**: the Kansas pre-1987 production is IHS-licensed (see `sources.md`); either leave `source='ks'`
   rows before 1987 out of the published table or restrict the schema's grants accordingly.

## Verification once implemented

```sql
SELECT state, count(DISTINCT api10) wells, min(period) first, max(period) last FROM energy.ogdata.production_monthly GROUP BY 1 ORDER BY 1;
SELECT state, count(*) wells, sum(pv10_usd)/1e9 pv10_bn FROM energy.ogdata.well_value GROUP BY 1 ORDER BY 3 DESC;
```
Numbers must match `ogdata sql` locally (the AGENTS.md §8 checklist) because the files are the same bytes.
