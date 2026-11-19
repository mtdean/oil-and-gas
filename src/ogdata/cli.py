"""Command line: python -m ogdata <command> or `ogdata <command>` once installed."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

import pandas as pd

from . import registry, storage
from .paths import LOGS, ensure_dirs
from .sources.base import RunContext


def _setup_logging(verbose: bool) -> None:
    ensure_dirs()
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stderr), logging.FileHandler(LOGS / f"ogdata-{datetime.now():%Y%m%d}.log")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_list(args):
    rows = [s.describe() for s in registry.all_sources()]
    df = pd.DataFrame(rows)
    with pd.option_context("display.max_columns", None, "display.width", 250, "display.max_colwidth", 60):
        print(df.drop(columns=["url"]).to_string(index=False))


def cmd_update(args):
    names = args.sources or [s.name for s in registry.all_sources() if s.status == "active"]
    ctx = RunContext(force=args.force, fetch_only=args.fetch_only, transform_only=args.transform_only, limit_years=args.limit_years)
    results = []
    for name in names:
        src = registry.get(name)
        if src.requires_key and not __import__("os").environ.get(src.requires_key):
            print(f"skip {name}: set {src.requires_key} in .env", file=sys.stderr)
            results.append({"source": name, "status": "skipped-no-key"})
            continue
        if src.status == "planned" and name not in (args.sources or []):
            continue
        results.append(src.run(ctx))
    if not args.fetch_only:
        _build_views_retry()
    print(pd.DataFrame(results).to_string(index=False))
    if any(r["status"] == "error" for r in results):
        sys.exit(1)


def _build_views_retry(tries: int = 6) -> None:
    import time
    for i in range(tries):
        try:
            storage.build_views()
            return
        except Exception as e:  # another ogdata process holds the DuckDB write lock
            if i == tries - 1:
                print(f"warning: could not rebuild DuckDB views ({e}); run `ogdata build-db` later", file=sys.stderr)
                return
            time.sleep(5 * (i + 1))


def cmd_status(args):
    df = storage.table_stats()
    if df.empty:
        print("no data yet. run: ogdata update")
        return
    print(df.to_string(index=False))
    print(f"\ntotal rows: {df.rows.sum():,}   size: {df.mb.sum():,.0f} MB")


def cmd_build_db(args):
    built = storage.build_views()
    print("views:", ", ".join(built) or "(none)")


def cmd_sql(args):
    con = storage.connect(read_only=True)
    q = args.query if args.query != "-" else sys.stdin.read()
    df = con.execute(q).df()
    with pd.option_context("display.max_columns", None, "display.width", 250, "display.max_rows", args.max_rows):
        print(df.to_string(index=False) if not args.csv else df.to_csv(index=False))


def main(argv=None):
    p = argparse.ArgumentParser(prog="ogdata", description="US oil & gas well + macro data pipeline")
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("list", help="list data sources").set_defaults(fn=cmd_list)

    u = sp.add_parser("update", help="fetch + transform sources (default: all active)")
    u.add_argument("sources", nargs="*")
    u.add_argument("--force", action="store_true", help="refetch and rebuild from scratch")
    u.add_argument("--fetch-only", action="store_true")
    u.add_argument("--transform-only", action="store_true", help="rebuild parquet from already-downloaded raw files")
    u.add_argument("--limit-years", type=int, help="only the most recent N years (smoke tests)")
    u.set_defaults(fn=cmd_update)

    sp.add_parser("status", help="row counts per table/source").set_defaults(fn=cmd_status)
    sp.add_parser("build-db", help="rebuild DuckDB views over parquet").set_defaults(fn=cmd_build_db)

    q = sp.add_parser("sql", help="run SQL against the warehouse ('-' reads stdin)")
    q.add_argument("query")
    q.add_argument("--csv", action="store_true")
    q.add_argument("--max-rows", type=int, default=200)
    q.set_defaults(fn=cmd_sql)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
