"""Self-contained HTML report for one `ogdata forecast` run (same design system as the dashboard)."""
from __future__ import annotations

import json
from pathlib import Path

from .dashboard import ASSETS, _clean
from .forecast import ForecastResult

TEMPLATE = """<title>Well Forecast {loc}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap">
<style>
/*__THEME_CSS__*/
</style>
<div class="topbar"><span class="topbar-title">&lt;OGDATA&gt; FORECAST</span><span class="topbar-meta" id="meta"></span></div>
<div class="wrap">
 <div class="grid">
  <div class="panel span12"><div class="hd"><h2 id="title"></h2><span class="sub" id="sub"></span></div><div class="bd kpi" id="kpi"></div></div>
  <div class="panel span8"><div class="hd"><h2>Production forecast</h2><span class="sub">analog P10–P90 band and P50 (solid) vs Monte Carlo P10/P50/P90 (dashed) · volume per month</span>
    <div class="ctl"><div class="seg" id="prod-seg"><button data-k="oil" class="on">oil</button><button data-k="gas">gas</button></div><div class="seg" id="scale-seg"><button data-k="lin" class="on">lin</button><button data-k="log">log</button></div></div></div>
    <div class="bd"><div class="chart" id="prod"></div></div></div>
  <div class="panel span4"><div class="hd"><h2>NPV10 distribution</h2><span class="sub" id="npv-sub"></span></div><div class="bd"><div class="chart" id="hist"></div></div></div>
  <div class="panel span6"><div class="hd"><h2>Cash flow, P50 profile at base deck</h2><span class="sub">monthly net cash flow and cumulative</span></div><div class="bd"><div class="chart" id="cf"></div></div></div>
  <div class="panel span6"><div class="hd"><h2>Sensitivities</h2><span class="sub">change in NPV10 vs P50 / base deck, $</span></div><div class="bd"><div class="chart" id="tornado"></div></div></div>
  <div class="panel span6"><div class="hd"><h2>Scenario grid</h2><span class="sub">NPV10 $MM · production bear/base/bull (P10/P50/P90 EUR) × price deck</span></div><div class="bd" id="scen"></div></div>
  <div class="panel span6"><div class="hd"><h2>Analog wells</h2><span class="sub" id="an-sub"></span></div><div class="bd"><div class="chart" id="anmap"></div></div></div>
  <div class="panel span12"><div class="hd"><h2>Analog list</h2><span class="sub">nearest first · EURs from each well's own decline fit</span></div><div class="bd" id="antable"></div></div>
  <div class="panel span12"><div class="hd"><h2>Assumptions</h2><span class="sub">override with ogdata forecast flags</span></div><div class="bd" id="assump"></div></div>
 </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script>
const R = /*__JSON__*/;
/*__CHARTS_JS__*/
const m = R.metrics, inp = R.inputs;
document.getElementById('meta').textContent = `generated ${inp.generated} · ${inp.sims} simulations`;
document.getElementById('title').textContent = inp.api10 ? `API ${inp.api10} · PDP forecast` : `${inp.lat.toFixed(4)}, ${inp.lon.toFixed(4)} · ${inp.new_well ? 'new well' : 'PDP'} forecast`;
document.getElementById('sub').textContent = `${m.state} · ${m.product} · ${m.n_analogs} analogs within ${m.radius_mi.toFixed(0)} mi · vintage ≥ ${inp.spec.vintage_min}${inp.spec.formation ? ' · formation ~ ' + inp.spec.formation : ''}`;
const K = d3.select('#kpi'); const kpi = (l, v, s) => K.append('div').attr('class', 'k').html(`<div class="l">${l}</div><div class="v">${v}</div><div class="s">${s || ''}</div>`);
const p = (d, k = 'p50') => d && d[k] != null ? d[k] : null;
const pdp = !!inp.api10;
kpi(pdp ? 'Remaining oil P50' : 'EUR oil P50', fmt.big(p(pdp ? m.remaining_oil_bbl : m.eur_oil_bbl)) + ' bbl', pdp ? `cum to date ${fmt.big(m.cum_oil_bbl)} · P10 ${fmt.big(p(m.remaining_oil_bbl, 'p10'))} · P90 ${fmt.big(p(m.remaining_oil_bbl, 'p90'))}` : `P10 ${fmt.big(p(m.eur_oil_bbl, 'p10'))} · P90 ${fmt.big(p(m.eur_oil_bbl, 'p90'))}`);
kpi(pdp ? 'Remaining gas P50' : 'EUR gas P50', fmt.big(p(pdp ? m.remaining_gas_mcf : m.eur_gas_mcf)) + ' mcf', pdp ? `cum to date ${fmt.big(m.cum_gas_mcf)}` : `P10 ${fmt.big(p(m.eur_gas_mcf, 'p10'))} · P90 ${fmt.big(p(m.eur_gas_mcf, 'p90'))}`);
kpi(pdp ? 'Current oil rate P50' : 'IP90 oil P50', fmt.num(p(m.ip90_oil_bpd)) + ' b/d', `gas ${fmt.num(p(m.ip90_gas_mcfd))} mcf/d`);
kpi('NPV10 P50', fmt.usd(p(m.npv10_usd)), `P10 ${fmt.usd(p(m.npv10_usd, 'p10'))} · P90 ${fmt.usd(p(m.npv10_usd, 'p90'))} · P(>0) ${(m.prob_npv10_positive * 100).toFixed(0)}%`);
kpi(inp.new_well ? 'IRR P50' : 'PV10 operating P50', inp.new_well ? (p(m.irr) == null ? '–' : (p(m.irr) * 100).toFixed(0) + '%') : fmt.usd(p(m.pv10_operating_usd)), inp.new_well ? `P10 ${p(m.irr, 'p10') == null ? '–' : (p(m.irr, 'p10') * 100).toFixed(0) + '%'} · P90 ${p(m.irr, 'p90') == null ? '–' : (p(m.irr, 'p90') * 100).toFixed(0) + '%'}` : '');
kpi('Payout P50', p(m.payout_months) == null ? '–' : fmt.num(p(m.payout_months)) + ' mo', `economic life P50 ${fmt.num(p(m.life_months))} mo`);
if (m.breakeven_wti_p50) kpi('Breakeven WTI', '$' + fmt.num(m.breakeven_wti_p50) + '/bbl', 'flat price, P50 profile, NPV10 = 0');
kpi('12-mo operating CF P50', fmt.usd(p(m.operating_cf_12m_usd)), `revenue ${fmt.usd(p(m.revenue_12m_usd))}`);
let prod = (R.inputs.product === 'gas') ? 'gas' : 'oil', scale = 'lin'; d3.selectAll('#prod-seg button').classed('on', function () { return this.dataset.k === prod; });
function drawProd() { const c = R.curves; const t = c.t.slice(0, 180);
  const series = [{ label: `analogs ${prod}`, color: SERIES[prod === 'oil' ? 0 : 2], t, p10: c[prod + '_p10'].slice(0, 180), p50: c[prod + '_p50'].slice(0, 180), p90: c[prod + '_p90'].slice(0, 180) }];
  if (R.mc && R.mc[prod]) series.push({ label: `Monte Carlo ${prod} P50`, color: SERIES[1], dash: true, t, p50: R.mc[prod].p50.slice(0, 180), p10: R.mc[prod].p10.slice(0, 180), p90: R.mc[prod].p90.slice(0, 180) });
  fanChart(document.getElementById('prod'), { series, height: 300, yLog: scale === 'log' }); }
d3.select('#prod-seg').on('click', e => { const b = e.target.closest('button'); if (!b) return; d3.selectAll('#prod-seg button').classed('on', function () { return this === b; }); prod = b.dataset.k; drawProd(); });
d3.select('#scale-seg').on('click', e => { const b = e.target.closest('button'); if (!b) return; d3.selectAll('#scale-seg button').classed('on', function () { return this === b; }); scale = b.dataset.k; drawProd(); });
drawProd();
/* NPV histogram */
(function () { const h = R.npv_hist; document.getElementById('npv-sub').textContent = `${inp.sims} trials · bins $`;
  barChart(document.getElementById('hist'), { height: 300, items: h.edges.slice(0, -1).map((e, i) => ({ label: fmt.usd(e), value: h.counts[i], color: e >= 0 ? '#199e70' : '#e66767' })), yFmt: v => fmt.num(v), valueLabel: 'trials' }); })();
/* cash flow */
(function () { const c = R.cashflow_p50; const d = c.month.map(parseM);
  lineChart(document.getElementById('cf'), { height: 240, yZero: true, yFmt: fmt.usd, series: [{ label: 'net cash flow / month', color: SERIES[0], points: d.map((x, i) => ({ d: x, v: c.net_cf[i] })), area: true }, { label: 'cumulative', color: SERIES[1], thick: true, points: d.map((x, i) => ({ d: x, v: c.cum_net_cf[i] })) }] }); })();
barChart(document.getElementById('tornado'), { horizontal: true, ml: 170, rowH: 24, items: R.sensitivities.map(s => ({ label: s.driver, low: s.low, high: s.high, value: 0 })), yFmt: fmt.usd });
/* scenario grid */
(function () { const s = R.scenarios; const keys = ['price_-25%', 'price_base', 'price_+25%'];
  const rows = Object.entries(s).map(([k, v]) => ({ scenario: k, eur: `${fmt.big(v.eur_oil_bbl)} bbl / ${fmt.big(v.eur_gas_mcf)} mcf`, ...Object.fromEntries(keys.map(kk => [kk, v[kk].npv10])) }));
  table(document.getElementById('scen'), { rows, cols: [{ key: 'scenario', label: 'production' }, { key: 'eur', label: 'EUR oil / gas' }, ...keys.map(kk => ({ key: kk, label: kk.replace('price_', 'deck '), num: true, fmt: fmt.usd }))] }); })();
/* analog map */
(function () { const el = document.getElementById('anmap'); const W = Math.max(el.clientWidth || 500, 300), H = 300; const svg = d3.select(el).append('svg').attr('viewBox', `0 0 ${W} ${H}`);
  const an = R.analogs; const lat0 = inp.lat, lon0 = inp.lon; const kx = Math.cos(lat0 * Math.PI / 180) * 69.17, ky = 69.0;
  const pts = an.map(a => ({ ...a, x: (a.lon - lon0) * kx, y: (a.lat - lat0) * ky })); const r = d3.max(pts, p => Math.max(Math.abs(p.x), Math.abs(p.y))) * 1.1 || 1;
  const sx = d3.scaleLinear().domain([-r, r]).range([30, W - 10]), sy = d3.scaleLinear().domain([-r, r]).range([H - 24, 8]);
  svg.append('g').attr('class', 'ax').attr('transform', `translate(0,${H - 24})`).call(d3.axisBottom(sx).ticks(6).tickSize(3).tickFormat(v => v + ' mi'));
  svg.append('g').attr('class', 'ax').attr('transform', 'translate(30,0)').call(d3.axisLeft(sy).ticks(5).tickSize(3).tickFormat(v => v + '')).select('.domain').remove();
  const vmax = d3.max(pts, p => p.oil_eur || p.gas_eur / 6 || 0) || 1; const col = d3.scaleSequential().domain([0, vmax]).interpolator(d3.interpolateRgb.gamma(1.6)('#3a2008', '#ffb347'));
  svg.selectAll('circle.a').data(pts).join('circle').attr('class', 'a').attr('cx', p => sx(p.x)).attr('cy', p => sy(p.y)).attr('r', 4).attr('fill', p => col(p.oil_eur || (p.gas_eur || 0) / 6)).attr('stroke', '#111')
    .on('mousemove', (ev, a) => showTip(ev, `<div class="t">${a.api10} · ${a.operator || ''}</div><div class="r"><span>vintage</span><b>${a.vintage}</b></div><div class="r"><span>formation</span><b>${a.formation || '–'}</b></div><div class="r"><span>distance</span><b>${a.dist.toFixed(1)} mi</b></div><div class="r"><span>oil EUR</span><b>${fmt.big(a.oil_eur)} bbl</b></div><div class="r"><span>gas EUR</span><b>${fmt.big(a.gas_eur)} mcf</b></div><div class="r"><span>IP90 oil</span><b>${fmt.num(a.oil_ip90_pd)} b/d</b></div>`)).on('mouseleave', hideTip);
  svg.append('path').attr('class', 'pin').attr('d', d3.symbol(d3.symbolCross, 90)()).attr('transform', `translate(${sx(0)},${sy(0)}) rotate(45)`);
  document.getElementById('an-sub').textContent = `${an.length} wells · colour = oil EUR (boe for gas wells) · cross = target`; })();
table(document.getElementById('antable'), { rows: R.analogs, cols: [{ key: 'api10', label: 'api10' }, { key: 'dist', label: 'mi', num: true, fmt: v => fmt.num(v, 1) }, { key: 'operator', label: 'operator' }, { key: 'county', label: 'county' }, { key: 'formation', label: 'formation' }, { key: 'vintage', label: 'vintage' },
  { key: R.inputs.product === 'gas' ? 'gas_ip90_pd' : 'oil_ip90_pd', label: R.inputs.product === 'gas' ? 'IP90 gas mcf/d' : 'IP90 oil b/d', num: true, fmt: v => fmt.num(v) }, { key: 'oil_eur', label: 'oil EUR bbl', num: true, fmt: fmt.big }, { key: 'gas_eur', label: 'gas EUR mcf', num: true, fmt: fmt.big }, { key: R.inputs.product === 'gas' ? 'gas_b' : 'oil_b', label: (R.inputs.product === 'gas' ? 'gas ' : 'oil ') + 'b', num: true, fmt: v => fmt.num(v, 1) }, { key: R.inputs.product === 'gas' ? 'gas_di' : 'oil_di', label: 'Di /mo', num: true, fmt: v => fmt.num(v, 3) }, { key: 'alloc', label: 'alloc' }, { key: 'status', label: 'status' }] });
(function () { const a = R.assumptions; const rows = Object.entries(a).filter(([k]) => k !== 'sev_tax_note').map(([k, v]) => ({ k, v: typeof v === 'number' ? (Math.abs(v) < 1 ? v.toString() : fmt.num(v, 2)) : String(v) }));
  rows.push({ k: 'severance note', v: a.sev_tax_note }); rows.push({ k: 'price deck', v: `${R.deck.source}; vol oil ${(R.deck.vol_oil * 100).toFixed(0)}% gas ${(R.deck.vol_gas * 100).toFixed(0)}% ρ ${R.deck.rho.toFixed(2)}; first months WTI ${R.deck.wti.slice(0, 3).map(x => '$' + x.toFixed(0)).join(' ')} HH ${R.deck.hh.slice(0, 3).map(x => '$' + x.toFixed(2)).join(' ')}` });
  table(document.getElementById('assump'), { rows, cols: [{ key: 'k', label: 'parameter' }, { key: 'v', label: 'value' }] }); })();
</script>
"""


def write_well_report(r: ForecastResult, out: str | Path) -> Path:
    payload = json.loads(r.to_json())
    payload["mc"] = r.mc_curves if hasattr(r, "mc_curves") else None
    payload["npv_hist"] = r.npv_hist if hasattr(r, "npv_hist") else {"edges": [], "counts": []}
    loc = f"{r.inputs['api10']}" if r.inputs.get("api10") else f"{r.inputs['lat']:.3f}, {r.inputs['lon']:.3f}"
    html = TEMPLATE.replace("{loc}", loc).replace("/*__THEME_CSS__*/", (ASSETS / "theme.css").read_text()).replace("/*__CHARTS_JS__*/", (ASSETS / "charts.js").read_text())
    html = html.replace("/*__JSON__*/", json.dumps(_clean(payload), separators=(",", ":")))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out
