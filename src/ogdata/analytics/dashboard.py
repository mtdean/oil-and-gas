"""Build the self-contained HTML monitor (dashboards/og_monitor.html).

All numbers are pre-aggregated here with DuckDB and embedded as JSON; the page renders them with
D3 (loaded from cdnjs). Styling follows the TCRED / situation-monitor terminal design system.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from ..paths import ROOT
from .forecast import analytics_connection

log = logging.getLogger("ogdata.analytics.dashboard")
ASSETS = Path(__file__).parent / "assets"
CELL = 0.2  # degrees
BASINS = {"PM": "Permian", "BK": "Bakken", "EF": "Eagle Ford", "AP": "Appalachia", "HA": "Haynesville", "R48": "Rest of L48"}
STATES = {"TX": "Texas", "NM": "New Mexico", "ND": "North Dakota", "CO": "Colorado", "WY": "Wyoming", "UT": "Utah", "MT": "Montana", "KS": "Kansas",
          "WV": "West Virginia", "PA": "Pennsylvania", "OH": "Ohio", "GOM": "Gulf of Mexico", "OK": "Oklahoma", "LA": "Louisiana", "CA": "California"}


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, np.ndarray)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(o) else round(float(o), 6)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.strftime("%Y-%m-%d")
    if o is pd.NaT:
        return None
    return o


def _series(con, sid: str, since: str | None = None, weekly: bool = False) -> dict:
    where = f"AND period >= '{since}'" if since else ""
    if weekly:
        df = con.execute(f"SELECT date_trunc('week', period) d, avg(value) v FROM macro_series WHERE series_id = ? AND value IS NOT NULL {where} GROUP BY 1 ORDER BY 1", [sid]).df()
    else:
        df = con.execute(f"SELECT period d, value v FROM macro_series WHERE series_id = ? AND value IS NOT NULL {where} ORDER BY 1", [sid]).df()
    return {"d": [x.strftime("%Y-%m-%d") for x in df.d], "v": [None if pd.isna(v) else float(v) for v in df.v]}


def _latest(con, sid: str):
    df = con.execute("SELECT period, value FROM macro_series WHERE series_id = ? AND value IS NOT NULL ORDER BY period", [sid]).df()
    if df.empty:
        return None
    df = df.set_index("period").value
    last = df.index[-1]
    def at(delta):
        idx = df.index[df.index <= last - delta]
        return float(df[idx[-1]]) if len(idx) else None
    v = float(df.iloc[-1])
    return {"value": v, "asof": last.strftime("%Y-%m-%d"), "prev": at(pd.Timedelta(days=1)), "m1": at(pd.DateOffset(months=1)), "y1": at(pd.DateOffset(years=1))}


def ticker(con) -> list[dict]:
    items = []
    for label, sid, unit, fmt in (("WTI", "eia:PET.RWTC.D", "$/bbl", "2"), ("BRENT", "eia:PET.RBRTE.D", "$/bbl", "2"), ("HENRY HUB", "eia:NG.RNGWHHD.D", "$/mmbtu", "2"),
                                  ("US RIGS", "eia:PET.E_ERTRR0_XR0_NUS_C.M", "rigs", "0"), ("US CRUDE", "eia:PET.MCRFPUS2.M", "kb/d", "0"),
                                  ("CRUDE STOCKS", "eia:PET.WCESTUS1.W", "kbbl", "0"), ("GAS STORAGE", "eia:NG.NW2_EPG0_SWO_R48_BCF.W", "bcf", "0"),
                                  ("PERMIAN DUCs", "eia:STEO.DUCSPM.M", "wells", "0"), ("PERMIAN RIGS", "eia:STEO.RIGSPM.M", "rigs", "0")):
        l = _latest(con, sid)
        if l:
            items.append({"label": label, "unit": unit, "fmt": fmt, **l})
    # CFTC managed money net length
    for label, code in (("WTI MM NET", "067651"), ("NG MM NET", "023651")):
        df = con.execute(f"""
            SELECT l.period, l.value - s.value AS v FROM macro_series l JOIN macro_series s USING (period)
            WHERE l.series_id = 'cftc:{code}:M_Money_Positions_Long_All' AND s.series_id = 'cftc:{code}:M_Money_Positions_Short_All' ORDER BY 1""").df()
        if not df.empty:
            s = df.set_index("period").v
            last = s.index[-1]
            m1 = s[s.index <= last - pd.DateOffset(months=1)]
            y1 = s[s.index <= last - pd.DateOffset(years=1)]
            items.append({"label": label, "unit": "contracts", "fmt": "0", "value": float(s.iloc[-1]), "asof": last.strftime("%Y-%m-%d"),
                          "prev": float(s.iloc[-2]) if len(s) > 1 else None, "m1": float(m1.iloc[-1]) if len(m1) else None, "y1": float(y1.iloc[-1]) if len(y1) else None})
    return items


def indicators(con) -> dict:
    since3 = (pd.Timestamp.today() - pd.DateOffset(years=3)).strftime("%Y-%m-%d")
    since10 = (pd.Timestamp.today() - pd.DateOffset(years=10)).strftime("%Y-%m-%d")
    out = {
        "spot": {"wti": _series(con, "eia:PET.RWTC.D", since3, weekly=True), "brent": _series(con, "eia:PET.RBRTE.D", since3, weekly=True), "hh": _series(con, "eia:NG.RNGWHHD.D", since3, weekly=True)},
        "steo": {"wti": _series(con, "eia:STEO.WTIPUUS.M", since3), "hh": _series(con, "eia:STEO.NGHHUUS.M", since3),
                 "us_crude": _series(con, "eia:STEO.COPRPUS.M", since10), "us_gas": _series(con, "eia:STEO.NGPRPUS.M", since10)},
        "us_crude_actual": _series(con, "eia:PET.MCRFPUS2.M", since10),
        "rigs": {name: _series(con, f"eia:STEO.RIGS{code}.M", since10) for code, name in BASINS.items()},
        "ducs": {name: _series(con, f"eia:STEO.DUCS{code}.M", since10) for code, name in BASINS.items()},
        "newwell_per_rig_oil": {name: _series(con, f"eia:STEO.CONWR{code}.M", since10) for code, name in BASINS.items()},
        "legacy_decline_oil": {name: _series(con, f"eia:STEO.COEOP{code}.M", since10) for code, name in BASINS.items()},
        "stocks": {"crude_ex_spr": _series(con, "eia:PET.WCESTUS1.W", since3), "gas_storage": _series(con, "eia:NG.NW2_EPG0_SWO_R48_BCF.W", since3)},
        "dallas_fed": {k: _series(con, f"dfed:all_firms:{c}") for k, c in (("business_activity", "qbac"), ("capex", "qcexp"), ("company_outlook", "qolk"), ("uncertainty", "quncr"), ("employment", "qemp"))},
        "cftc": {},
        "state_gas_withdrawals": {st: _series(con, f"eia:NG.N9010{st}2.M", since10) for st in ("TX", "PA", "LA", "WV", "OH", "NM", "OK", "CO", "ND")},
        "state_crude_prod": {},
    }
    for label, code in (("WTI (NYMEX)", "067651"), ("Natural gas (NYMEX)", "023651")):
        df = con.execute(f"""
            SELECT l.period d, l.value - s.value AS v FROM macro_series l JOIN macro_series s USING (period)
            WHERE l.series_id = 'cftc:{code}:M_Money_Positions_Long_All' AND s.series_id = 'cftc:{code}:M_Money_Positions_Short_All' AND l.period >= '{since3}' ORDER BY 1""").df()
        out["cftc"][label] = {"d": [x.strftime("%Y-%m-%d") for x in df.d], "v": [float(v) for v in df.v]}
    for st, code in (("TX", "TX1"), ("NM", "NM1"), ("ND", "ND1"), ("CO", "CO1"), ("OK", "OK1"), ("WY", "WY1"), ("GOM", "FX1"), ("LA", "LA1"), ("AK", "AK1"), ("CA", "CA1"), ("UT", "UT1")):
        s = _series(con, f"eia:PET.MCRFP{code}.M", since10)
        if s["d"]:
            out["state_crude_prod"][st] = s
    return out


def state_production(con) -> dict:
    df = con.execute("""
        SELECT state, period, sum(oil_bbl) oil, sum(gas_mcf) gas
        FROM production_monthly WHERE coalesce(period_months,1) = 1 AND period >= '2005-01-01' AND period <= current_date - INTERVAL 4 MONTH
        GROUP BY 1, 2 ORDER BY 2
    """).df()
    months = sorted(df.period.unique())
    piv_o = df.pivot(index="period", columns="state", values="oil").reindex(months)
    piv_g = df.pivot(index="period", columns="state", values="gas").reindex(months)
    days = pd.Series(pd.DatetimeIndex(months).days_in_month, index=months)
    out = {"months": [m.strftime("%Y-%m") for m in months], "oil_bpd": {}, "gas_mcfd": {}}
    for st in piv_o.columns:
        out["oil_bpd"][st] = [None if pd.isna(v) else round(float(v), 0) for v in (piv_o[st] / days)]
        out["gas_mcfd"][st] = [None if pd.isna(v) else round(float(v), 0) for v in (piv_g[st] / days)]
    out["last_full_month"] = {st: con.execute("SELECT max(period) FROM production_monthly WHERE state = ?", [st]).fetchone()[0].strftime("%Y-%m") for st in piv_o.columns}
    return out


def hex_cells(con) -> list[dict]:
    df = con.execute(f"""
        WITH w AS (
            SELECT d.api10, d.state, d.county, d.vintage, d.status, d.primary_product, d.alloc,
                   d.oil_cum12, d.gas_cum12, d.oil_eur, d.gas_eur, d.boe_eur, d.oil_ip90_pd, d.gas_ip90_pd,
                   v.pv10_usd, v.cf_next12_usd, v.oil_remaining_bbl, v.gas_remaining_mcf, v.oil_rate_bpd, v.gas_rate_mcfd,
                   floor(l.lat / {CELL}) * {CELL} AS clat, floor(l.lon / {CELL}) * {CELL} AS clon
            FROM well_decline d JOIN well_locations l USING (api10) LEFT JOIN well_value v USING (api10)
            WHERE l.lat BETWEEN 24 AND 50 AND l.lon BETWEEN -125 AND -66
        )
        SELECT clat, clon, mode(state) st, mode(county) county, count(*) n,
               count(*) FILTER (WHERE vintage >= 2015) n15, count(*) FILTER (WHERE status = 'active') n_active,
               count(*) FILTER (WHERE vintage >= 2018 AND alloc <> 'lease_alloc') n18,
               quantile_cont(oil_cum12, 0.5) FILTER (WHERE vintage >= 2018 AND alloc <> 'lease_alloc' AND primary_product = 'oil') cum12_oil,
               quantile_cont(gas_cum12, 0.5) FILTER (WHERE vintage >= 2018 AND alloc <> 'lease_alloc' AND primary_product = 'gas') cum12_gas,
               quantile_cont(oil_eur, 0.5) FILTER (WHERE vintage >= 2018 AND alloc <> 'lease_alloc' AND primary_product = 'oil') eur_oil,
               quantile_cont(gas_eur, 0.5) FILTER (WHERE vintage >= 2018 AND alloc <> 'lease_alloc' AND primary_product = 'gas') eur_gas,
               quantile_cont(boe_eur, 0.5) FILTER (WHERE vintage >= 2018 AND alloc <> 'lease_alloc') eur_boe,
               sum(pv10_usd) pv10, sum(cf_next12_usd) cf12, sum(oil_remaining_bbl + gas_remaining_mcf / 6) rem_boe,
               sum(oil_rate_bpd + gas_rate_mcfd / 6) boed, sum(oil_rate_bpd) bopd, sum(gas_rate_mcfd) mcfd,
               quantile_cont(pv10_usd, 0.5) FILTER (WHERE status = 'active') pv10_med
        FROM w GROUP BY 1, 2 HAVING count(*) >= 3
    """).df()
    cells = []
    for r in df.itertuples(index=False):
        cells.append({"lat": round(r.clat, 3), "lon": round(r.clon, 3), "st": r.st, "county": r.county, "n": int(r.n), "n15": int(r.n15), "n_active": int(r.n_active), "n18": int(r.n18),
                      "cum12_oil": r.cum12_oil, "cum12_gas": r.cum12_gas, "eur_oil": r.eur_oil, "eur_gas": r.eur_gas, "eur_boe": r.eur_boe,
                      "pv10": r.pv10, "cf12": r.cf12, "rem_boe": r.rem_boe, "boed": r.boed, "bopd": r.bopd, "mcfd": r.mcfd, "pv10_med": r.pv10_med})
    return _clean(cells)


def type_curves(con) -> dict:
    out = {}
    groups = con.execute("""
        SELECT group_type, state, key, vintage, max(n_wells) nw FROM type_curves
        WHERE t = 6 AND vintage IN ('2015-19', '2020-22', '2023+') AND (group_type = 'state' OR group_type = 'state_formation')
        GROUP BY ALL HAVING max(n_wells) >= 30 ORDER BY state, group_type, nw DESC
    """).df()
    keep = []
    for (st, gt), g in groups.groupby(["state", "group_type"]):
        if gt == "state":
            keep += [(st, gt, k) for k in g.key.unique()]
        else:
            top = g.groupby("key").nw.max().sort_values(ascending=False).head(3).index
            keep += [(st, gt, k) for k in top]
    for st, gt, key in keep:
        rows = con.execute("""
            SELECT vintage, t, n_wells, oil_p10, oil_p50, oil_p90, oil_mean, gas_p10, gas_p50, gas_p90, gas_mean FROM type_curves
            WHERE state = ? AND group_type = ? AND coalesce(key, '') = coalesce(?::VARCHAR, '') AND t < 72 AND vintage IN ('2015-19', '2020-22', '2023+') ORDER BY vintage, t
        """, [st, gt, key]).df()
        label = st if gt == "state" else f"{st} · {key}"
        out[label] = {"state": st, "group_type": gt, "key": key, "vintages": {}}
        for v, gg in rows.groupby("vintage"):
            out[label]["vintages"][v] = {c: gg[c].tolist() for c in ("t", "n_wells", "oil_p10", "oil_p50", "oil_p90", "gas_p10", "gas_p50", "gas_p90")}
    return _clean(out)


def vintage_table(con) -> list[dict]:
    df = con.execute("""
        SELECT state, vintage, count(*) n, count(*) FILTER (WHERE primary_product='oil') n_oil,
               quantile_cont(oil_cum12, 0.5) FILTER (WHERE primary_product='oil') cum12_oil_med,
               quantile_cont(gas_cum12, 0.5) FILTER (WHERE primary_product='gas') cum12_gas_med,
               quantile_cont(oil_eur, 0.5) FILTER (WHERE primary_product='oil' AND oil_fit_ok) eur_oil_med,
               quantile_cont(gas_eur, 0.5) FILTER (WHERE primary_product='gas' AND gas_fit_ok) eur_gas_med,
               quantile_cont(oil_ip90_pd, 0.5) FILTER (WHERE primary_product='oil') ip90_oil_med,
               quantile_cont(oil_d1_eff, 0.5) FILTER (WHERE primary_product='oil' AND oil_fit_ok) d1_oil_med
        FROM well_decline WHERE vintage BETWEEN 2008 AND year(current_date) - 1 AND alloc <> 'lease_alloc'
        GROUP BY 1, 2 HAVING count(*) >= 20 ORDER BY 1, 2
    """).df()
    return _clean(df.to_dict(orient="records"))


def cashflow(con) -> dict:
    df = con.execute("""
        SELECT state, deck, year(month) yr, sum(operating_cf_usd) cf FROM state_cashflow GROUP BY ALL ORDER BY 3
    """).df()
    deck = con.execute("SELECT month, wti, hh, vol_oil, vol_gas, rho FROM price_deck ORDER BY month").df()
    out = {"years": sorted(df.yr.unique().tolist()), "decks": {}, "price_deck": {"months": [m.strftime("%Y-%m") for m in deck.month[:120]], "wti": deck.wti[:120].tolist(), "hh": deck.hh[:120].tolist(),
           "vol_oil": float(deck.vol_oil[0]), "vol_gas": float(deck.vol_gas[0]), "rho": float(deck.rho[0])}}
    for d, g in df.groupby("deck"):
        piv = g.pivot(index="yr", columns="state", values="cf").reindex(out["years"]).fillna(0)
        out["decks"][d] = {st: piv[st].tolist() for st in piv.columns}
    return _clean(out)


def tables(con) -> dict:
    counties = con.execute("""
        SELECT state, county, count(*) wells, sum(pv10_usd) pv10, sum(cf_next12_usd) cf12, sum(oil_remaining_bbl) rem_oil, sum(gas_remaining_mcf) rem_gas,
               sum(oil_rate_bpd) bopd, sum(gas_rate_mcfd) mcfd
        FROM well_value WHERE county IS NOT NULL GROUP BY 1, 2 ORDER BY pv10 DESC LIMIT 40
    """).df()
    ops = con.execute("""
        SELECT operator, count(*) wells, count(DISTINCT state) states, sum(pv10_usd) pv10, sum(cf_next12_usd) cf12, sum(oil_rate_bpd) bopd, sum(gas_rate_mcfd) mcfd,
               quantile_cont(vintage, 0.5) med_vintage
        FROM well_value WHERE operator IS NOT NULL GROUP BY 1 ORDER BY pv10 DESC LIMIT 40
    """).df()
    totals = con.execute("""
        SELECT state, count(*) wells, sum(pv10_usd) pv10, sum(pv10_bear_usd) pv10_bear, sum(pv10_bull_usd) pv10_bull, sum(cf_next12_usd) cf12,
               sum(oil_rate_bpd) bopd, sum(gas_rate_mcfd) mcfd, sum(oil_remaining_bbl) rem_oil, sum(gas_remaining_mcf) rem_gas
        FROM well_value GROUP BY 1 ORDER BY pv10 DESC
    """).df()
    return _clean({"counties": counties.to_dict(orient="records"), "operators": ops.to_dict(orient="records"), "states": totals.to_dict(orient="records")})


def coverage(con) -> list[dict]:
    df = con.execute("""
        SELECT state, count(DISTINCT api10) wells, count(*) AS well_months, min(period) AS first, max(period) AS last,
               count(DISTINCT api10) FILTER (WHERE alloc = 'lease_alloc') lease_alloc_wells
        FROM well_monthly GROUP BY 1 ORDER BY 1
    """).df()
    fits = con.execute("SELECT state, count(*) n, count(*) FILTER (WHERE oil_fit_ok OR gas_fit_ok) fitted, count(*) FILTER (WHERE status='active') active FROM well_decline GROUP BY 1").df()
    df = df.merge(fits, on="state", how="left")
    return _clean(df.to_dict(orient="records"))


def build(out: str | None = None) -> Path:
    con = analytics_connection()
    data = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "ticker": ticker(con), "indicators": indicators(con), "state_production": state_production(con), "hex": hex_cells(con),
        "type_curves": type_curves(con), "vintages": vintage_table(con), "cashflow": cashflow(con), "tables": tables(con), "coverage": coverage(con),
    }
    con.close()
    tpl = (ASSETS / "dashboard_template.html").read_text()
    topo = (ASSETS / "states-10m.json").read_text()
    html = tpl.replace("/*__DATA__*/", "const DATA = " + json.dumps(_clean(data), separators=(",", ":")) + ";\nconst US_TOPO = " + topo + ";")
    html = html.replace("/*__THEME_CSS__*/", (ASSETS / "theme.css").read_text()).replace("/*__CHARTS_JS__*/", (ASSETS / "charts.js").read_text())
    dest = Path(out) if out else ROOT / "dashboards" / "og_monitor.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html)
    log.info("dashboard %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest
