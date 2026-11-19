"""Parquet lake + DuckDB views.

Layout: data/parquet/<table>/source=<source>/<partition>.parquet
Each source owns its partition directory; a (re)load replaces files it writes
and can delete stale partitions. DuckDB views union everything by name, so
adding a source never requires a schema migration.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .paths import DB, PARQUET, RUNS
from .schema import TABLES, conform

log = logging.getLogger(__name__)


def partition_dir(table: str, source: str) -> Path:
    return PARQUET / table / f"source={source}"


def write_table(table: str, source: str, df: pd.DataFrame, partition: str = "data") -> Path:
    """Conform df to the canonical schema and write one parquet file for (table, source, partition)."""
    if table not in TABLES:
        raise KeyError(f"unknown table {table}")
    df = conform(df, table)
    df["source"] = source
    pdir = partition_dir(table, source)
    pdir.mkdir(parents=True, exist_ok=True)
    out = pdir / f"{partition}.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    tbl = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(tbl, tmp, compression="zstd", coerce_timestamps="us", allow_truncated_timestamps=True)
    tmp.replace(out)
    log.info("wrote %s rows -> %s", f"{len(df):,}", out.relative_to(PARQUET))
    return out


DUCK_TYPES = {"string": "VARCHAR", "Float64": "DOUBLE", "datetime64[ns]": "TIMESTAMP", "boolean": "BOOLEAN"}


def write_sql(table: str, source: str, con: duckdb.DuckDBPyConnection, select_sql: str, partition: str = "data") -> Path:
    """Like write_table but lets DuckDB stream a query straight to parquet (for sources too big for pandas)."""
    spec = TABLES[table]
    present = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM ({select_sql}) q").fetchall()}
    exprs = []
    for col, dt in spec.items():
        t = DUCK_TYPES[dt]
        if col == "source":
            exprs.append(f"'{source}'::VARCHAR AS source")
        elif col in present:
            exprs.append(f'TRY_CAST("{col}" AS {t}) AS "{col}"')
        else:
            exprs.append(f'NULL::{t} AS "{col}"')
    pdir = partition_dir(table, source)
    pdir.mkdir(parents=True, exist_ok=True)
    out = pdir / f"{partition}.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    con.execute(f"COPY (SELECT {', '.join(exprs)} FROM ({select_sql}) q) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    tmp.replace(out)
    n = pq.read_metadata(out).num_rows
    log.info("wrote %s rows -> %s", f"{n:,}", out.relative_to(PARQUET))
    return out


def clear_source(table: str, source: str) -> None:
    pdir = partition_dir(table, source)
    if pdir.exists():
        shutil.rmtree(pdir)


def list_partitions(table: str, source: str) -> list[Path]:
    pdir = partition_dir(table, source)
    return sorted(pdir.glob("*.parquet")) if pdir.exists() else []


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB), read_only=read_only)
    return con


def build_views(con: duckdb.DuckDBPyConnection | None = None) -> list[str]:
    """(Re)create one view per canonical table over its parquet files."""
    own = con is None
    con = con or connect()
    built = []
    for table in TABLES:
        tdir = PARQUET / table
        files = list(tdir.glob("source=*/*.parquet")) if tdir.exists() else []
        if not files:
            con.execute(f"DROP VIEW IF EXISTS {table}")
            continue
        glob = str(tdir / "source=*" / "*.parquet").replace("'", "''")
        src = f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        present = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        cols = []
        for col, dt in TABLES[table].items():
            if col == "source":
                cols.append("source::VARCHAR AS source")
            elif col in present:
                cols.append(f'"{col}"')
            else:
                cols.append(f'NULL::{DUCK_TYPES[dt]} AS "{col}"')
        con.execute(f"CREATE OR REPLACE VIEW {table} AS SELECT {', '.join(cols)} FROM {src}")
        built.append(table)
    _build_derived(con, built)
    if own:
        con.close()
    return built


def _build_derived(con, built: list[str]) -> None:
    """Convenience views on top of the raw tables."""
    if "production_monthly" in built:
        con.execute(
            """
            CREATE OR REPLACE VIEW production_annual AS
            SELECT entity_id, entity_type, api14, api10, state, source,
                   date_trunc('year', period) AS year,
                   sum(oil_bbl) AS oil_bbl, sum(gas_mcf) AS gas_mcf, sum(water_bbl) AS water_bbl,
                   sum(days_on) AS days_on, count(*) AS months_reported
            FROM production_monthly GROUP BY ALL
            """
        )
        con.execute(
            """
            CREATE OR REPLACE VIEW production_by_state_month AS
            SELECT state, source, period,
                   sum(oil_bbl) AS oil_bbl, sum(gas_mcf) AS gas_mcf, sum(water_bbl) AS water_bbl,
                   count(DISTINCT coalesce(api10, entity_id)) AS producing_wells
            FROM production_monthly WHERE coalesce(period_months, 1) = 1 GROUP BY ALL
            """
        )
    if "wells" in built:
        # Best-available location per API10 across sources (prefer state regulator over FracFocus).
        con.execute(
            """
            CREATE OR REPLACE VIEW well_locations AS
            SELECT api10, any_value(state) AS state,
                   arg_min(lat, src_rank) AS lat, arg_min(lon, src_rank) AS lon,
                   arg_min(source, src_rank) AS location_source,
                   any_value(well_name) AS well_name, any_value(operator) AS operator
            FROM (SELECT *, CASE WHEN source = 'fracfocus' THEN 2 ELSE 1 END AS src_rank
                  FROM wells WHERE lat IS NOT NULL AND lon IS NOT NULL AND api10 IS NOT NULL)
            GROUP BY api10
            """
        )
    if "macro_series" in built:
        con.execute(
            """
            CREATE OR REPLACE VIEW macro_latest AS
            SELECT series_id, source, name, units, frequency, region,
                   arg_max(value, period) AS latest_value, max(period) AS latest_period,
                   min(period) AS first_period, count(*) AS n
            FROM macro_series GROUP BY ALL
            """
        )


def table_stats(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    rows = []
    for table in TABLES:
        tdir = PARQUET / table
        if not tdir.exists():
            continue
        for sdir in sorted(tdir.glob("source=*")):
            files = list(sdir.glob("*.parquet"))
            if not files:
                continue
            n = sum(pq.read_metadata(f).num_rows for f in files)
            size = sum(f.stat().st_size for f in files)
            rows.append({"table": table, "source": sdir.name.split("=", 1)[1], "files": len(files), "rows": n, "mb": round(size / 1e6, 1)})
    if own:
        con.close()
    return pd.DataFrame(rows)


def log_run(source: str, status: str, **info) -> None:
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "source": source, "status": status, **info}
    with RUNS.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
