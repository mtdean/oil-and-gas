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


def cmd_analytics(args):
    from .analytics import wellcurves
    wellcurves.build_all(tuple(args.only) if args.only else ("well_monthly", "well_decline", "type_curves", "well_value"))
    _build_views_retry()


def cmd_forecast(args):
    from .analytics import economics, forecast
    if args.api10 is None and (args.lat is None or args.lon is None):
        raise SystemExit("give --lat/--lon or --api10")
    spec = forecast.AnalogSpec(radius_mi=args.radius, min_wells=args.min_wells, vintage_min=args.vintage_min, formation=args.formation,
                               product=args.product, include_lease_alloc=args.include_lease_alloc)
    overrides = {k: v for k, v in {"nri": args.nri, "capex_mm": args.capex_mm, "loe_fixed_month": args.loe_fixed, "loe_var_per_boe": args.loe_var,
                                   "oil_diff": args.oil_diff, "gas_diff": args.gas_diff, "discount_rate": args.discount, "ngl_yield_bbl_per_mmcf": args.ngl_yield}.items() if v is not None}
    con = forecast.analytics_connection()
    assumptions = None
    if overrides:
        # state is resolved from the analogs; patch after the first pass
        probe = forecast.find_analogs(con, args.lat, args.lon, spec) if args.api10 is None else None
        st = probe.state.mode().iat[0] if probe is not None and not probe.empty else "TX"
        assumptions = economics.Assumptions(state=st, **overrides)
    r = forecast.run(args.lat, args.lon, spec, assumptions, sims=args.sims, seed=args.seed, con=con, new_well=not args.pdp, api10=args.api10)
    print(forecast.summary_text(r))
    if args.json:
        from pathlib import Path
        Path(args.json).write_text(r.to_json())
        print(f"\nwrote {args.json}")
    if args.html:
        from .analytics import report
        report.write_well_report(r, args.html)
        print(f"wrote {args.html}")


def cmd_dashboard(args):
    from .analytics import dashboard
    out = dashboard.build(args.out)
    print(f"wrote {out}")


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

    an = sp.add_parser("analytics", help="build well_monthly / well_decline / type_curves (data/parquet/analytics)")
    an.add_argument("--only", nargs="*", choices=["well_monthly", "well_decline", "type_curves", "well_value"], default=None)
    an.set_defaults(fn=cmd_analytics)

    fc = sp.add_parser("forecast", help="lat/lon (or --api10) production forecast with Monte Carlo economics")
    fc.add_argument("--lat", type=float); fc.add_argument("--lon", type=float)
    fc.add_argument("--api10", help="existing well: PDP forecast from its own decline (no capex)")
    fc.add_argument("--radius", type=float, default=10.0, help="analog search radius, miles (expands if too few wells)")
    fc.add_argument("--min-wells", type=int, default=25)
    fc.add_argument("--vintage-min", type=int, default=2014)
    fc.add_argument("--formation", help="substring filter on reported formation/pool/field")
    fc.add_argument("--product", choices=["oil", "gas"])
    fc.add_argument("--include-lease-alloc", action="store_true", help="allow TX/KS multi-well lease allocations as analogs")
    fc.add_argument("--sims", type=int, default=2000)
    fc.add_argument("--seed", type=int, default=7)
    fc.add_argument("--pdp", action="store_true", help="no capex (existing production)")
    fc.add_argument("--nri", type=float); fc.add_argument("--capex-mm", type=float); fc.add_argument("--loe-fixed", type=float); fc.add_argument("--loe-var", type=float)
    fc.add_argument("--oil-diff", type=float); fc.add_argument("--gas-diff", type=float); fc.add_argument("--discount", type=float)
    fc.add_argument("--ngl-yield", type=float, help="bbl NGL per mmcf wellhead gas")
    fc.add_argument("--json", help="write full result JSON here")
    fc.add_argument("--html", help="write a self-contained HTML report here")
    fc.set_defaults(fn=cmd_forecast)

    db = sp.add_parser("dashboard", help="build the HTML dashboard (dashboards/og_monitor.html)")
    db.add_argument("--out", default=None)
    db.set_defaults(fn=cmd_dashboard)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
