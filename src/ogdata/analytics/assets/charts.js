/* Shared D3 chart helpers (D3 v7 global). Every chart: recessive grid, thin marks, legend for >=2 series, crosshair tooltip. */
const SERIES = ['#d95926', '#3987e5', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
const fmt = {
  num: (v, d = 0) => (v == null || !isFinite(v)) ? '–' : d3.format(`,.${d}f`)(v),
  big: v => (v == null || !isFinite(v)) ? '–' : (Math.abs(v) >= 1e9 ? d3.format('.2f')(v / 1e9) + 'B' : Math.abs(v) >= 1e6 ? d3.format('.1f')(v / 1e6) + 'M' : Math.abs(v) >= 1e3 ? d3.format('.0f')(v / 1e3) + 'k' : d3.format('.0f')(v)),
  usd: v => (v == null || !isFinite(v)) ? '–' : (v < 0 ? '-$' : '$') + fmt.big(Math.abs(v)),
  pct: (v, d = 1) => (v == null || !isFinite(v)) ? '–' : d3.format(`+.${d}f`)(v) + '%',
  date: d3.timeFormat('%d %b %y'), month: d3.timeFormat('%b %y'), ym: d3.timeFormat('%Y-%m'),
};
const parseD = d3.utcParse('%Y-%m-%d'), parseM = d3.utcParse('%Y-%m');
let TIP;
function tip() { if (!TIP) { TIP = d3.select('body').append('div').attr('class', 'tip'); } return TIP; }
function showTip(ev, html) { const t = tip().html(html).style('display', 'block'); const r = t.node().getBoundingClientRect();
  let x = ev.pageX + 14, y = ev.pageY + 14; if (x + r.width > window.scrollX + window.innerWidth - 8) x = ev.pageX - r.width - 14; if (y + r.height > window.scrollY + window.innerHeight - 8) y = ev.pageY - r.height - 10;
  t.style('left', x + 'px').style('top', y + 'px'); }
function hideTip() { tip().style('display', 'none'); }
function legend(el, items) { const l = d3.select(el).append('div').attr('class', 'legend');
  items.forEach(it => l.append('span').style('--c', it.color).text(it.label)); }

/* lineChart(el, {series:[{label, color, points:[{d:Date, v}]}], yFmt, height, yLabel, bands:[{from,to,label}], fill:boolean, markers}) */
function lineChart(el, o) {
  const root = d3.select(el).html(''); const W = Math.max(el.clientWidth || 600, 300), H = o.height || 190, m = { t: 10, r: 16, b: 24, l: o.ml || 48 };
  const svg = root.append('svg').attr('viewBox', `0 0 ${W} ${H}`); const g = svg.append('g');
  const all = o.series.flatMap(s => s.points.filter(p => p.v != null));
  if (!all.length) { root.append('div').attr('class', 'dim').text('no data'); return; }
  const x = (o.xType === 'linear' ? d3.scaleLinear() : d3.scaleUtc()).domain(d3.extent(all, p => p.d)).range([m.l, W - m.r]);
  let [y0, y1] = d3.extent(all, p => p.v); if (o.yZero) y0 = Math.min(0, y0); if (y0 === y1) { y0 -= 1; y1 += 1; }
  const y = (o.yLog ? d3.scaleLog().domain([Math.max(y0, o.yMin || 1), y1]) : d3.scaleLinear().domain([y0, y1]).nice()).range([H - m.b, m.t]);
  g.append('g').attr('class', 'gl').call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat('')).attr('transform', `translate(${m.l},0)`).select('.domain').remove();
  (o.bands || []).forEach(b => g.append('rect').attr('x', x(b.from)).attr('width', Math.max(x(b.to) - x(b.from), 1)).attr('y', m.t).attr('height', H - m.t - m.b).attr('fill', '#1a2332').attr('opacity', .6));
  if (o.yZero && y0 < 0) g.append('line').attr('x1', m.l).attr('x2', W - m.r).attr('y1', y(0)).attr('y2', y(0)).attr('stroke', '#3a3a3a');
  g.append('g').attr('class', 'ax').attr('transform', `translate(0,${H - m.b})`).call((o.xType === 'linear' ? d3.axisBottom(x).ticks(6).tickFormat(o.xFmt || (d => d)) : d3.axisBottom(x).ticks(Math.min(6, Math.floor(W / 90)))).tickSize(3));
  g.append('g').attr('class', 'ax').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(3).tickFormat(o.yFmt || fmt.big)).select('.domain').remove();
  const line = d3.line().defined(p => p.v != null).x(p => x(p.d)).y(p => y(p.v));
  o.series.forEach((s, i) => { const c = s.color || SERIES[i % 8];
    if (s.area) g.append('path').attr('class', 'area').attr('fill', c).attr('d', d3.area().defined(p => p.v != null).x(p => x(p.d)).y0(y(Math.max(y0, 0))).y1(p => y(p.v))(s.points));
    g.append('path').attr('class', 'ln' + (s.thick ? ' thick' : '')).attr('stroke', c).attr('stroke-dasharray', s.dash ? '4 3' : null).attr('d', line(s.points));
    const last = s.points.filter(p => p.v != null).at(-1); if (last && o.endDots !== false) g.append('circle').attr('cx', x(last.d)).attr('cy', y(last.v)).attr('r', 2.5).attr('fill', c); });
  if (o.series.length > 1 || o.legend) legend(el, o.series.map((s, i) => ({ label: s.label, color: s.color || SERIES[i % 8] })));
  /* crosshair */
  const cross = g.append('g').attr('class', 'cross').style('display', 'none'); cross.append('line').attr('y1', m.t).attr('y2', H - m.b);
  const bis = d3.bisector(p => p.d).center;
  svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', W - m.l - m.r).attr('height', H - m.t - m.b).attr('fill', 'transparent')
    .on('mousemove', ev => { const [mx] = d3.pointer(ev); const xd = x.invert(mx); cross.style('display', null).select('line').attr('x1', mx).attr('x2', mx);
      const rows = o.series.map((s, i) => { const pts = s.points.filter(p => p.v != null); if (!pts.length) return ''; const p = pts[bis(pts, xd)];
        return `<div class="r"><span><i class="sw" style="background:${s.color || SERIES[i % 8]}"></i>${s.label}</span><b>${(o.yFmt || fmt.big)(p.v)}</b></div>`; }).join('');
      showTip(ev, `<div class="t">${o.xType === 'linear' ? (o.xFmt || (d => d))(Math.round(xd)) : fmt.date(xd)}</div>${rows}`); })
    .on('mouseleave', () => { cross.style('display', 'none'); hideTip(); });
}

/* fanChart: p10/p50/p90 band + optional overlay lines. series: [{label, color, t:[], p10:[], p50:[], p90:[]}] */
function fanChart(el, o) {
  const root = d3.select(el).html(''); const W = Math.max(el.clientWidth || 600, 300), H = o.height || 200, m = { t: 10, r: 16, b: 24, l: 52 };
  const svg = root.append('svg').attr('viewBox', `0 0 ${W} ${H}`); const g = svg.append('g');
  const tmax = d3.max(o.series, s => d3.max(s.t)), vmax = d3.max(o.series, s => d3.max(s.p90 || s.p50)), vmin = o.yLog ? Math.max(1, d3.min(o.series, s => d3.min((s.p10 || s.p50).filter(v => v > 0)))) : 0;
  const x = d3.scaleLinear().domain([0, tmax]).range([m.l, W - m.r]);
  const y = (o.yLog ? d3.scaleLog().domain([vmin, vmax]) : d3.scaleLinear().domain([0, vmax]).nice()).range([H - m.b, m.t]);
  g.append('g').attr('class', 'gl').call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat('')).attr('transform', `translate(${m.l},0)`).select('.domain').remove();
  g.append('g').attr('class', 'ax').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(8).tickSize(3));
  g.append('g').attr('class', 'ax').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4, o.yLog ? '~s' : undefined).tickSize(3).tickFormat(fmt.big)).select('.domain').remove();
  svg.append('text').attr('class', 'lbl sec').attr('x', W - m.r).attr('y', H - 2).attr('text-anchor', 'end').text(o.xLabel || 'months on production');
  o.series.forEach((s, i) => { const c = s.color || SERIES[i % 8];
    if (s.p10 && s.p90) g.append('path').attr('class', 'area').attr('fill', c).attr('d', d3.area().defined((_, k) => s.p10[k] != null && s.p90[k] != null).x((_, k) => x(s.t[k])).y0((_, k) => y(Math.max(s.p10[k], vmin || 0))).y1((_, k) => y(Math.max(s.p90[k], vmin || 0)))(s.t));
    g.append('path').attr('class', 'ln thick').attr('stroke', c).attr('stroke-dasharray', s.dash ? '4 3' : null).attr('d', d3.line().defined((_, k) => s.p50[k] != null && s.p50[k] > (vmin ? 0 : -1)).x((_, k) => x(s.t[k])).y((_, k) => y(Math.max(s.p50[k], vmin || 0)))(s.t)); });
  legend(el, o.series.map((s, i) => ({ label: s.label + (s.p10 ? ' (P10–P90 band, P50 line)' : ''), color: s.color || SERIES[i % 8] })));
  svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', W - m.l - m.r).attr('height', H - m.t - m.b).attr('fill', 'transparent')
    .on('mousemove', ev => { const [mx] = d3.pointer(ev); const t = Math.round(x.invert(mx));
      const rows = o.series.map((s, i) => { const k = s.t.indexOf(t); if (k < 0) return ''; const c = s.color || SERIES[i % 8];
        return `<div class="r"><span><i class="sw" style="background:${c}"></i>${s.label}</span><b>${s.p10 ? fmt.big(s.p10[k]) + ' / ' : ''}${fmt.big(s.p50[k])}${s.p90 ? ' / ' + fmt.big(s.p90[k]) : ''}</b></div>`; }).join('');
      showTip(ev, `<div class="t">month ${t}${o.series[0].p10 ? ' · P10 / P50 / P90' : ''}</div>${rows}`); }).on('mouseleave', hideTip);
}

/* stackedArea(el, {x:[labels], series:[{label, color, values:[]}], yFmt}) */
function stackedArea(el, o) {
  const root = d3.select(el).html(''); const W = Math.max(el.clientWidth || 600, 300), H = o.height || 220, m = { t: 10, r: 16, b: 24, l: 56 };
  const svg = root.append('svg').attr('viewBox', `0 0 ${W} ${H}`); const g = svg.append('g');
  const keys = o.series.map(s => s.label); const data = o.x.map((xx, i) => Object.fromEntries([['x', xx], ...o.series.map(s => [s.label, s.values[i] || 0])]));
  const st = d3.stack().keys(keys).offset(d3.stackOffsetDiverging)(data);
  const x = o.xType === 'band' ? d3.scaleBand().domain(o.x).range([m.l, W - m.r]).padding(.15) : d3.scaleUtc().domain(d3.extent(o.x)).range([m.l, W - m.r]);
  const y = d3.scaleLinear().domain([Math.min(0, d3.min(st, s => d3.min(s, d => d[0]))), d3.max(st, s => d3.max(s, d => d[1]))]).nice().range([H - m.b, m.t]);
  g.append('g').attr('class', 'gl').call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat('')).attr('transform', `translate(${m.l},0)`).select('.domain').remove();
  g.append('g').attr('class', 'ax').attr('transform', `translate(0,${H - m.b})`).call((o.xType === 'band' ? d3.axisBottom(x).tickValues(o.x.filter((_, i) => i % Math.ceil(o.x.length / 10) === 0)) : d3.axisBottom(x).ticks(6)).tickSize(3));
  g.append('g').attr('class', 'ax').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(3).tickFormat(o.yFmt || fmt.big)).select('.domain').remove();
  st.forEach((s, i) => { const c = o.series[i].color || SERIES[i % 8];
    if (o.xType === 'band') g.selectAll(null).data(s).join('rect').attr('x', d => x(d.data.x)).attr('width', x.bandwidth()).attr('y', d => y(Math.max(d[0], d[1])) + 1).attr('height', d => Math.max(Math.abs(y(d[0]) - y(d[1])) - 2, 0)).attr('fill', c)
      .on('mousemove', (ev, d) => showTip(ev, `<div class="t">${d.data.x}</div>` + keys.map((k, j) => `<div class="r"><span><i class="sw" style="background:${o.series[j].color || SERIES[j % 8]}"></i>${k}</span><b>${(o.yFmt || fmt.big)(d.data[k])}</b></div>`).join('') + `<div class="r"><span>total</span><b>${(o.yFmt || fmt.big)(d3.sum(keys, k => d.data[k]))}</b></div>`)).on('mouseleave', hideTip);
    else g.append('path').attr('fill', c).attr('opacity', .85).attr('stroke', '#111').attr('stroke-width', .8).attr('d', d3.area().x(d => x(d.data.x)).y0(d => y(d[0])).y1(d => y(d[1]))(s)); });
  if (o.xType !== 'band') { const bis = d3.bisector(d => d.x).center;
    svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', W - m.l - m.r).attr('height', H - m.t - m.b).attr('fill', 'transparent')
      .on('mousemove', ev => { const [mx] = d3.pointer(ev); const d = data[bis(data, x.invert(mx))];
        showTip(ev, `<div class="t">${fmt.month(d.x)}</div>` + keys.map((k, j) => `<div class="r"><span><i class="sw" style="background:${o.series[j].color || SERIES[j % 8]}"></i>${k}</span><b>${(o.yFmt || fmt.big)(d[k])}</b></div>`).join('') + `<div class="r"><span>total</span><b>${(o.yFmt || fmt.big)(d3.sum(keys, k => d[k]))}</b></div>`); }).on('mouseleave', hideTip); }
  legend(el, o.series.map((s, i) => ({ label: s.label, color: s.color || SERIES[i % 8] })));
}

/* barChart(el, {items:[{label, value, color}], horizontal:true, yFmt}) — single hue unless colors given */
function barChart(el, o) {
  const root = d3.select(el).html(''); const W = Math.max(el.clientWidth || 600, 300); const n = o.items.length;
  if (o.horizontal) { const rowH = o.rowH || 18, m = { t: 4, r: 60, b: 20, l: o.ml || 150 }, H = m.t + m.b + n * rowH;
    const svg = root.append('svg').attr('viewBox', `0 0 ${W} ${H}`); const g = svg.append('g');
    const lo = Math.min(0, d3.min(o.items, d => Math.min(d.value, d.low ?? d.value))), hi = Math.max(0, d3.max(o.items, d => Math.max(d.value, d.high ?? d.value)));
    const x = d3.scaleLinear().domain([lo, hi]).nice().range([m.l, W - m.r]); const y = d3.scaleBand().domain(o.items.map(d => d.label)).range([m.t, H - m.b]).padding(.25);
    g.append('g').attr('class', 'ax').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(5).tickSize(3).tickFormat(o.yFmt || fmt.big));
    g.append('g').attr('class', 'ax').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).tickSize(0)).select('.domain').remove();
    g.append('line').attr('x1', x(0)).attr('x2', x(0)).attr('y1', m.t).attr('y2', H - m.b).attr('stroke', '#3a3a3a');
    o.items.forEach((d, i) => { const c = d.color || o.color || SERIES[0];
      if (d.low != null) { /* tornado: two bars */
        g.append('rect').attr('x', x(Math.min(0, d.low))).attr('width', Math.abs(x(d.low) - x(0))).attr('y', y(d.label)).attr('height', y.bandwidth()).attr('fill', '#e66767');
        g.append('rect').attr('x', x(Math.min(0, d.high))).attr('width', Math.abs(x(d.high) - x(0))).attr('y', y(d.label)).attr('height', y.bandwidth()).attr('fill', '#199e70');
        g.append('text').attr('class', 'lbl sec').attr('x', x(Math.min(d.low, d.high)) - 4).attr('y', y(d.label) + y.bandwidth() / 2 + 3).attr('text-anchor', 'end').text((o.yFmt || fmt.big)(Math.min(d.low, d.high)));
        g.append('text').attr('class', 'lbl sec').attr('x', x(Math.max(d.low, d.high)) + 4).attr('y', y(d.label) + y.bandwidth() / 2 + 3).text((o.yFmt || fmt.big)(Math.max(d.low, d.high)));
      } else { g.append('rect').attr('x', x(Math.min(0, d.value))).attr('width', Math.abs(x(d.value) - x(0))).attr('y', y(d.label)).attr('height', y.bandwidth()).attr('fill', c).attr('rx', 1);
        g.append('text').attr('class', 'lbl').attr('x', x(Math.max(0, d.value)) + 4).attr('y', y(d.label) + y.bandwidth() / 2 + 3).text((o.yFmt || fmt.big)(d.value)); } });
  } else { const H = o.height || 180, m = { t: 8, r: 10, b: 26, l: 48 };
    const svg = root.append('svg').attr('viewBox', `0 0 ${W} ${H}`); const g = svg.append('g');
    const x = d3.scaleBand().domain(o.items.map(d => d.label)).range([m.l, W - m.r]).padding(.2); const y = d3.scaleLinear().domain([0, d3.max(o.items, d => d.value) || 1]).nice().range([H - m.b, m.t]);
    g.append('g').attr('class', 'gl').call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat('')).attr('transform', `translate(${m.l},0)`).select('.domain').remove();
    g.append('g').attr('class', 'ax').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).tickSize(3).tickValues(x.domain().filter((_, i) => i % Math.ceil(n / 12) === 0)));
    g.append('g').attr('class', 'ax').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(3).tickFormat(o.yFmt || fmt.big)).select('.domain').remove();
    g.selectAll(null).data(o.items).join('rect').attr('x', d => x(d.label)).attr('width', x.bandwidth()).attr('y', d => y(d.value)).attr('height', d => H - m.b - y(d.value)).attr('fill', d => d.color || o.color || SERIES[0]).attr('rx', 1)
      .on('mousemove', (ev, d) => showTip(ev, `<div class="t">${d.label}</div><div class="r"><span>${o.valueLabel || 'value'}</span><b>${(o.yFmt || fmt.big)(d.value)}</b></div>${d.extra ? `<div class="r"><span>${d.extra}</span></div>` : ''}`)).on('mouseleave', hideTip); }
}

/* table(el, {cols:[{key,label,fmt,num}], rows}) */
function table(el, o) {
  const root = d3.select(el).html('').append('div').attr('class', 'tbwrap'); const t = root.append('table').attr('class', 'tb');
  t.append('thead').append('tr').selectAll('th').data(o.cols).join('th').attr('class', c => c.num ? 'num' : null).text(c => c.label);
  t.append('tbody').selectAll('tr').data(o.rows).join('tr').selectAll('td').data(r => o.cols.map(c => ({ c, v: r[c.key] }))).join('td').attr('class', d => d.c.num ? 'num' : null).text(d => d.c.fmt ? d.c.fmt(d.v) : (d.v ?? '–'));
}
