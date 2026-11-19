# Example queries (DuckDB, via `ogdata sql "..."` or `duckdb data/warehouse.duckdb`)

```sql
-- 1. Monthly production history for one well (any state that reports per well)
SELECT period, oil_bbl, gas_mcf, water_bbl, days_on, operator
FROM production_monthly WHERE api10 = '3305303901' ORDER BY period;

-- 2. All wells within ~5 km of a point (Permian example), with cumulative oil since 2019
WITH w AS (
  SELECT api10, state, well_name, operator, lat, lon,
         6371 * acos(cos(radians(31.90)) * cos(radians(lat)) * cos(radians(lon) - radians(-103.20)) + sin(radians(31.90)) * sin(radians(lat))) AS km
  FROM well_locations)
SELECT w.*, p.oil
FROM w LEFT JOIN (SELECT api10, sum(oil_bbl) oil FROM production_monthly WHERE period >= '2019-01-01' GROUP BY 1) p USING (api10)
WHERE km < 5 ORDER BY km;

-- 3. Texas: allocate lease oil to member wells by equal share (replace with a better weight, e.g. FracFocus water volume)
WITH members AS (
  SELECT entity_id, api10, count(*) OVER (PARTITION BY entity_id) AS n FROM entity_wells WHERE source = 'tx')
SELECT m.api10, p.period, p.oil_bbl / m.n AS oil_alloc_bbl, p.gas_mcf / m.n AS gas_alloc_mcf
FROM production_monthly p JOIN members m USING (entity_id)
WHERE p.source = 'tx' AND p.entity_type = 'lease' AND p.period >= '2024-01-01';

-- 4. Type curve: average oil by month-on-production for wells first producing in 2023 in the Bakken pool
WITH first AS (SELECT api14, min(period) FILTER (WHERE oil_bbl > 0) AS fp FROM production_monthly WHERE source = 'nd' AND formation = 'BAKKEN' GROUP BY 1)
SELECT date_diff('month', f.fp, p.period) AS mop, count(*) wells, avg(oil_bbl) avg_oil_bbl, median(oil_bbl) med_oil_bbl
FROM production_monthly p JOIN first f USING (api14)
WHERE f.fp BETWEEN '2023-01-01' AND '2023-12-31' AND p.oil_bbl IS NOT NULL
GROUP BY 1 ORDER BY 1;

-- 5. Operator exposure: current-year production by operator across states
SELECT operator, state, sum(oil_bbl) oil, sum(gas_mcf) gas, count(DISTINCT coalesce(api10, entity_id)) entities
FROM production_monthly WHERE period >= date_trunc('year', current_date) AND coalesce(period_months,1) = 1
GROUP BY ALL ORDER BY oil DESC LIMIT 50;

-- 6. Macro: WTI vs HY spread vs US rig count, monthly
SELECT date_trunc('month', period) m,
       avg(value) FILTER (WHERE series_id = 'eia:PET.RWTC.D') wti,
       avg(value) FILTER (WHERE series_id = 'fred:BAMLH0A0HYM2') hy_oas,
       avg(value) FILTER (WHERE series_id = 'eia:PET.E_ERTRR0_XR0_NUS_C.M') rigs
FROM macro_series WHERE period >= '2015-01-01' GROUP BY 1 ORDER BY 1;

-- 7. Permian DUC inventory and rigs (EIA STEO, history + forecast)
SELECT period, max(value) FILTER (WHERE series_id = 'eia:STEO.DUCSPM.M') ducs, max(value) FILTER (WHERE series_id = 'eia:STEO.RIGSPM.M') rigs
FROM macro_series GROUP BY 1 HAVING ducs IS NOT NULL ORDER BY 1;

-- 8. Frac intensity for a well (FracFocus) joined to its first-12-month production
SELECT c.api10, c.job_end, c.base_water_gal, c.tvd_ft, sum(p.oil_bbl) oil_12m
FROM completions c JOIN production_monthly p USING (api10)
WHERE p.period BETWEEN c.job_end AND c.job_end + INTERVAL 12 MONTH
GROUP BY ALL;
```
