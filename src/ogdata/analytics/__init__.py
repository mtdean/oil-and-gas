"""Analytics layer: per-well monthly series, Arps decline fits, type curves, lat/lon forecasts, Monte Carlo economics, dashboard.

Outputs live in data/parquet/analytics/<table>.parquet and are exposed as DuckDB views
(well_monthly, well_decline, type_curves) alongside the canonical tables.
"""
