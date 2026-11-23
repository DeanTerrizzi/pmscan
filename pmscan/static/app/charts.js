// SVG chart kit. Specs: 2px lines with a 10% area wash, >=8px end dots with a 2px
// surface ring, <=24px bars with 4px rounded data ends, hairline solid grid, one
// y-axis per chart, hover layer on every chart, and a table-view twin per card.
import { html, useEffect, useRef, useState } from "./lib.js";

// ---------- sizing & scales ----------
export function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

const linear = (d0, d1, r0, r1) => {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
};

export function niceTicks(min, max, n = 4, minStep = 0) {
  if (!(max > min)) max = min + (min === 0 ? 1 : Math.abs(min));
  const raw = (max - min) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const err = raw / mag;
  const step = Math.max(minStep, (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag);
  const out = [];
  for (let v = Math.floor(min / step) * step; v <= Math.ceil(max / step) * step + step / 2; v += step) {
    out.push(+v.toFixed(12));
  }
  return out;
}

const TIME_STEPS = [60e3, 300e3, 900e3, 1800e3, 3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 864e5, 2 * 864e5, 7 * 864e5, 30 * 864e5];
function timeTicks(t0, t1, maxTicks) {
  const step = TIME_STEPS.find((s) => (t1 - t0) / s <= maxTicks) || TIME_STEPS[TIME_STEPS.length - 1];
  const out = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return out;
}
export const centsTick = (v) => `${+(v * 100).toFixed(2)}¢`;
const timeLabel = (t, span) => {
  const d = new Date(t);
  return span > 2 * 864e5
    ? d.toLocaleDateString([], { month: "short", day: "numeric" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

function nearestIndex(sorted, value, get) {
  let lo = 0;
  let hi = sorted.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (get(sorted[mid]) < value) lo = mid;
    else hi = mid;
  }
  return Math.abs(get(sorted[lo]) - value) <= Math.abs(get(sorted[hi]) - value) ? lo : hi;
}

// ---------- tooltip ----------
// rows: [{ color, value, label }]: value leads (strong), label follows; keyed by a short line.
export function Tooltip({ x, y, width, title, rows }) {
  const left = x > width - 200 ? Math.max(0, x - 192) : x + 12;
  return html`<div class="tooltip" style=${`left:${left}px;top:${y}px`} role="status">
    ${title && html`<div class="tt-title">${title}</div>`}
    ${rows.map(
      (r) => html`<div class="tt-row">
        ${r.color && html`<span class="tt-key" style=${`background:${r.color}`}></span>`}
        <strong>${r.value}</strong>${r.label && html`<span class="tt-label">${r.label}</span>`}
      </div>`,
    )}
  </div>`;
}

// ---------- containers ----------
export function DataTable({ columns, rows, empty = "No data.", max = 500 }) {
  if (!rows || !rows.length) return html`<p class="empty">${empty}</p>`;
  return html`<div class="table-wrap"><table>
    <thead><tr>${columns.map((c) => html`<th class=${c.num ? "num" : ""}>${c.label}</th>`)}</tr></thead>
    <tbody>${rows.slice(0, max).map(
      (r) => html`<tr>${columns.map((c) => html`<td class=${c.num ? "num" : ""}>${
        c.render ? c.render(r) : c.format ? c.format(c.get(r)) : c.get(r)
      }</td>`)}</tr>`,
    )}</tbody>
  </table></div>`;
}

export function ChartCard({ title, subtitle, table, legend, children, className = "" }) {
  const [showTable, setShowTable] = useState(false);
  return html`<figure class=${`card chart-card ${className}`}>
    <figcaption class="card-head">
      <div class="card-titles"><h3>${title}</h3>${subtitle && html`<p class="sub">${subtitle}</p>`}</div>
      ${table && html`<button class="ghost small" aria-pressed=${showTable} onClick=${() => setShowTable(!showTable)}>
        ${showTable ? "Chart" : "Table"}</button>`}
    </figcaption>
    ${legend && !showTable && html`<${Legend} items=${legend} />`}
    ${showTable && table ? html`<${DataTable} ...${table} />` : children}
  </figure>`;
}

// items: [{ label, color, shape: "dot" | "line" | "rect" }]
export function Legend({ items }) {
  return html`<ul class="legend">${items.map(
    (it) => html`<li><span class=${`swatch ${it.shape || "dot"}`} style=${`background:${it.color}`}></span>${it.label}</li>`,
  )}</ul>`;
}

// ---------- line chart (time series, single series) ----------
export function LineChart({
  points, height = 200, yFormat = String, tickFormat, yDomain, color = "var(--series-1)", label = "Line chart",
  area = true, emptyText = "No data yet.", valueLabel = "", integer = false, zero = true,
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  const pts = (points || []).filter((p) => p.v != null && isFinite(p.v)).sort((a, b) => a.t - b.t);
  if (!pts.length) return html`<div ref=${ref} class="chart-empty" style=${`height:${height}px`}>${emptyText}</div>`;
  if (!width) return html`<div ref=${ref} class="chart" style=${`height:${height}px`}></div>`;

  const w = Math.max(width, 240);
  const M = { l: 56, r: 64, t: 14, b: 28 };
  const iw = w - M.l - M.r;
  const ih = height - M.t - M.b;
  const t0 = pts[0].t;
  const t1 = pts.length > 1 ? pts[pts.length - 1].t : t0 + 1;
  const vals = pts.map((p) => p.v);
  // zero=false (e.g. equity): scale to the data so small moves on a large balance stay visible.
  const vmin = Math.min(...vals);
  const vmax = Math.max(...vals);
  const pad = Math.max((vmax - vmin) * 0.1, Math.abs(vmax) * 0.002, 1e-9);
  const lo0 = yDomain ? yDomain[0] : zero ? Math.min(0, vmin) : vmin - pad;
  const hi0 = yDomain ? yDomain[1] : zero ? vmax : vmax + pad;
  const ticks = niceTicks(lo0, hi0, 4, integer ? 1 : 0);
  const tickFmt = tickFormat || yFormat;
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const x = linear(t0, t1, M.l, M.l + iw);
  const y = linear(lo, hi, M.t + ih, M.t);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const wash = `${line}L${x(pts[pts.length - 1].t).toFixed(1)},${y(lo)}L${x(t0).toFixed(1)},${y(lo)}Z`;
  const last = pts[pts.length - 1];
  const hp = hover != null ? pts[hover] : null;

  const onMove = (e) => {
    const px = e.clientX - e.currentTarget.getBoundingClientRect().left + M.l;
    const t = t0 + ((px - M.l) / iw) * (t1 - t0);
    setHover(nearestIndex(pts, t, (p) => p.t));
  };
  const onKey = (e) => {
    if (e.key === "ArrowLeft") setHover((i) => Math.max(0, (i ?? pts.length - 1) - 1));
    if (e.key === "ArrowRight") setHover((i) => Math.min(pts.length - 1, (i ?? pts.length - 1) + 1));
  };

  return html`<div class="chart" ref=${ref}>
    <svg width=${w} height=${height} role="img" aria-label=${label}>
      ${ticks.map((v) => html`<g>
        <line class="grid" x1=${M.l} x2=${M.l + iw} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${M.l - 8} y=${y(v)} dy="0.32em" text-anchor="end">${tickFmt(v)}</text></g>`)}
      <line class="axis" x1=${M.l} x2=${M.l + iw} y1=${y(lo)} y2=${y(lo)} />
      ${timeTicks(t0, t1, Math.max(2, Math.floor(iw / 90))).map((t) => html`
        <text class="tick" x=${x(t)} y=${height - 8} text-anchor="middle">${timeLabel(t, t1 - t0)}</text>`)}
      ${area && html`<path d=${wash} fill=${color} fill-opacity="0.1" stroke="none" />`}
      <path d=${line} fill="none" stroke=${color} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      <circle cx=${x(last.t)} cy=${y(last.v)} r="4" fill=${color} stroke="var(--surface)" stroke-width="2" />
      <text class="end-label" x=${x(last.t) + 9} y=${y(last.v)} dy="0.32em">${yFormat(last.v)}</text>
      ${hp && html`<g>
        <line class="crosshair" x1=${x(hp.t)} x2=${x(hp.t)} y1=${M.t} y2=${M.t + ih} />
        <circle cx=${x(hp.t)} cy=${y(hp.v)} r="4.5" fill=${color} stroke="var(--surface)" stroke-width="2" /></g>`}
      <rect x=${M.l} y=${M.t} width=${iw} height=${ih} fill="transparent" tabindex="0"
        aria-label=${`${label}: use arrow keys to read values`}
        onPointerMove=${onMove} onPointerLeave=${() => setHover(null)}
        onFocus=${() => setHover(pts.length - 1)} onBlur=${() => setHover(null)} onKeyDown=${onKey} />
    </svg>
    ${hp && html`<${Tooltip} x=${x(hp.t)} y=${M.t} width=${w} title=${dateTimeLabel(hp.t)}
      rows=${[{ color, value: yFormat(hp.v), label: valueLabel }]} />`}
  </div>`;
}
const dateTimeLabel = (t) =>
  new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

// ---------- sparkline & tiles ----------
export function Sparkline({ values, width = 112, height = 30 }) {
  const vs = (values || []).filter((v) => v != null && isFinite(v));
  if (vs.length < 2) return null;
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const x = linear(0, vs.length - 1, 4, width - 6);
  const y = linear(lo, hi === lo ? lo + 1 : hi, height - 5, 5);
  const d = vs.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return html`<svg class="spark" width=${width} height=${height} aria-hidden="true">
    <path d=${d} fill="none" stroke="var(--deemph)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
    <circle cx=${x(vs.length - 1)} cy=${y(vs[vs.length - 1])} r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" />
  </svg>`;
}

export function StatTile({ label, value, sub, spark }) {
  return html`<div class="tile">
    <div class="tile-label">${label}</div>
    <div class="tile-value">${value}</div>
    ${sub && html`<div class="tile-sub">${sub}</div>`}
    ${spark && html`<${Sparkline} values=${spark} />`}
  </div>`;
}

// Ratio against a limit: same-ramp track.
export function Meter({ value, max, label }) {
  const f = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return html`<div class="meter" role="meter" aria-label=${label} aria-valuemin="0" aria-valuemax=${max} aria-valuenow=${value}>
    <div class="meter-fill" style=${`width:${(f * 100).toFixed(2)}%`}></div></div>`;
}

// ---------- diverging horizontal bars around zero ----------
// rows: [{ label, value, href, detail }]. Positive bars wear the accent; negative recede.
export function DivergingBars({ rows, format, label = "Bar chart" }) {
  const [hover, setHover] = useState(null);
  if (!rows.length) return html`<p class="empty">No data.</p>`;
  const vals = rows.map((r) => r.value);
  const lo = Math.min(0, ...vals);
  const hi = Math.max(0, ...vals);
  const span = hi - lo || 1;
  const zero = ((0 - lo) / span) * 100;
  return html`<div class="dbars" role="list" aria-label=${label}>
    ${rows.map((r, i) => {
      const pos = r.value >= 0;
      const left = pos ? zero : ((r.value - lo) / span) * 100;
      const width = (Math.abs(r.value) / span) * 100;
      return html`<div class=${`dbar-row ${hover === i ? "hover" : ""}`} role="listitem" tabindex="0"
          onPointerEnter=${() => setHover(i)} onPointerLeave=${() => setHover(null)}
          onFocus=${() => setHover(i)} onBlur=${() => setHover(null)}>
        <div class="dbar-label" title=${r.label}>${
          r.href ? html`<a href=${r.href} target="_blank" rel="noopener">${r.label}</a>` : r.label
        }</div>
        <div class="dbar-track">
          <div class="dbar-zero" style=${`left:${zero}%`}></div>
          <div class=${`dbar ${pos ? "pos" : "neg"}`} style=${`left:${left}%;width:max(2px,${width}%)`}></div>
        </div>
        <div class="dbar-value num">${format(r.value)}</div>
        ${hover === i && r.detail && html`<div class="dbar-detail">${r.detail}</div>`}
      </div>`;
    })}
  </div>`;
}

// ---------- ladder range plot ----------
// rungs strictest -> loosest; each shows its bid-ask range. The out-of-order pair is accented.
export function LadderChart({ rungs, strictIndex, looseIndex, height = 250, format = String, tickFormat }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  if (!width) return html`<div ref=${ref} class="chart" style=${`height:${height}px`}></div>`;
  const tickFmt = tickFormat || format;
  const w = Math.max(width, 260);
  const M = { l: 52, r: 16, t: 18, b: 40 };
  const iw = w - M.l - M.r;
  const ih = height - M.t - M.b;
  const vals = rungs.flatMap((r) => [r.bid, r.ask]).filter((v) => v != null);
  const ticks = niceTicks(0, Math.max(0.01, ...vals), 4);
  const y = linear(0, ticks[ticks.length - 1], M.t + ih, M.t);
  const band = iw / rungs.length;
  const cx = (i) => M.l + band * (i + 0.5);
  const maxChars = Math.max(4, Math.floor(band / 6.5));
  const labelEvery = Math.max(1, Math.ceil(44 / band)); // thin labels on narrow charts; the pair is always labeled
  const accent = (i) => i === strictIndex || i === looseIndex;
  const s = rungs[strictIndex];
  const l = rungs[looseIndex];

  return html`<div class="chart" ref=${ref}>
    <svg width=${w} height=${height} role="img" aria-label="Ladder of nested markets, strictest to loosest">
      ${ticks.map((v) => html`<g>
        <line class="grid" x1=${M.l} x2=${M.l + iw} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${M.l - 8} y=${y(v)} dy="0.32em" text-anchor="end">${tickFmt(v)}</text></g>`)}
      <line class="axis" x1=${M.l} x2=${M.l + iw} y1=${y(0)} y2=${y(0)} />
      ${rungs.map((r, i) => {
        const color = accent(i) ? "var(--series-1)" : "var(--deemph)";
        const top = r.ask ?? r.bid;
        const bottom = r.bid ?? r.ask;
        return html`<g>
          ${r.bid != null && r.ask != null && html`<line x1=${cx(i)} x2=${cx(i)} y1=${y(bottom)} y2=${y(top)}
            stroke=${color} stroke-width="2" stroke-linecap="round" />`}
          ${r.bid != null && html`<circle cx=${cx(i)} cy=${y(r.bid)} r="4" fill=${color} stroke="var(--surface)" stroke-width="2" />`}
          ${r.ask != null && html`<circle cx=${cx(i)} cy=${y(r.ask)} r="4" fill="var(--surface)" stroke=${color} stroke-width="2" />`}
          ${(i % labelEvery === 0 || accent(i)) && html`<text class=${accent(i) ? "tick strong" : "tick"} x=${cx(i)}
            y=${height - 22} text-anchor="middle">${r.label.length > maxChars ? r.label.slice(0, maxChars - 1) + "…" : r.label}</text>`}
          <rect x=${M.l + band * i} y=${M.t} width=${band} height=${ih} fill="transparent" tabindex="0"
            onPointerEnter=${() => setHover(i)} onPointerLeave=${() => setHover(null)}
            onFocus=${() => setHover(i)} onBlur=${() => setHover(null)} />
        </g>`;
      })}
      ${s && s.bid != null && html`<text class="end-label" x=${cx(strictIndex) + 9} y=${y(s.bid)} dy="0.32em">bid ${format(s.bid)}</text>`}
      ${l && l.ask != null && html`<text class="end-label" x=${cx(looseIndex) + 9} y=${y(l.ask)} dy="0.32em">ask ${format(l.ask)}</text>`}
      <text class="axis-title" x=${M.l} y=${height - 4}>strictest</text>
      <text class="axis-title" x=${M.l + iw} y=${height - 4} text-anchor="end">loosest</text>
    </svg>
    ${hover != null && html`<${Tooltip} x=${cx(hover)} y=${M.t} width=${w} title=${rungs[hover].question || rungs[hover].label}
      rows=${[
        { value: format(rungs[hover].ask), label: "YES ask" },
        { value: format(rungs[hover].bid), label: "YES bid" },
      ]} />`}
  </div>`;
}

// ---------- scatter ----------
// points: [{ x, y, highlight, title, lines: [{value,label}], key }]
export function Scatter({
  points, height = 320, xDomain, yDomain, xFormat = String, yFormat = String, xLabel = "", yLabel = "",
  diagonal = false, onSelect, label = "Scatter plot", emptyText = "No data.",
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  if (!points.length) return html`<div ref=${ref} class="chart-empty" style=${`height:${height}px`}>${emptyText}</div>`;
  if (!width) return html`<div ref=${ref} class="chart" style=${`height:${height}px`}></div>`;
  const w = Math.max(width, 260);
  const M = { l: 56, r: 20, t: 14, b: 44 };
  const iw = w - M.l - M.r;
  const ih = height - M.t - M.b;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const xt = niceTicks(xDomain ? xDomain[0] : Math.min(...xs), xDomain ? xDomain[1] : Math.max(...xs), 5);
  const yt = niceTicks(yDomain ? yDomain[0] : Math.min(...ys), yDomain ? yDomain[1] : Math.max(...ys), 4);
  const x = linear(xt[0], xt[xt.length - 1], M.l, M.l + iw);
  const y = linear(yt[0], yt[yt.length - 1], M.t + ih, M.t);
  const ordered = [...points].sort((a, b) => (a.highlight ? 1 : 0) - (b.highlight ? 1 : 0));
  const hp = hover != null ? points[hover] : null;

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left + M.l;
    const py = e.clientY - r.top + M.t;
    let best = null;
    let bestD = 24 * 24; // nearest point within 24px
    points.forEach((p, i) => {
      const d = (x(p.x) - px) ** 2 + (y(p.y) - py) ** 2;
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    });
    setHover(best);
  };
  const lo = Math.max(xt[0], yt[0]);
  const hi = Math.min(xt[xt.length - 1], yt[yt.length - 1]);

  return html`<div class="chart" ref=${ref}>
    <svg width=${w} height=${height} role="img" aria-label=${label}>
      ${yt.map((v) => html`<g>
        <line class="grid" x1=${M.l} x2=${M.l + iw} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${M.l - 8} y=${y(v)} dy="0.32em" text-anchor="end">${yFormat(v)}</text></g>`)}
      ${xt.map((v) => html`<text class="tick" x=${x(v)} y=${M.t + ih + 16} text-anchor="middle">${xFormat(v)}</text>`)}
      <line class="axis" x1=${M.l} x2=${M.l + iw} y1=${M.t + ih} y2=${M.t + ih} />
      ${diagonal && hi > lo && html`<g>
        <line class="reference" x1=${x(lo)} y1=${y(lo)} x2=${x(hi)} y2=${y(hi)} />
        <text class="axis-title" x=${x(hi) - 4} y=${y(hi) + 14} text-anchor="end">same price</text></g>`}
      <text class="axis-title" x=${M.l + iw} y=${height - 4} text-anchor="end">${xLabel}</text>
      <text class="axis-title" x=${M.l} y=${M.t - 4}>${yLabel}</text>
      ${ordered.map((p) => html`<circle cx=${x(p.x)} cy=${y(p.y)} r="4"
        fill=${p.highlight ? "var(--series-1)" : "var(--deemph)"} stroke="var(--surface)" stroke-width="2" />`)}
      ${hp && html`<circle cx=${x(hp.x)} cy=${y(hp.y)} r="6" fill="none" stroke="var(--ink)" stroke-width="1.5" />`}
      <rect x=${M.l} y=${M.t} width=${iw} height=${ih} fill="transparent" style=${onSelect ? "cursor:pointer" : ""}
        onPointerMove=${onMove} onPointerLeave=${() => setHover(null)}
        onClick=${() => hp && onSelect && onSelect(hp)} />
    </svg>
    ${hp && html`<${Tooltip} x=${x(hp.x)} y=${Math.max(0, y(hp.y) - 30)} width=${w} title=${hp.title} rows=${hp.lines || []} />`}
  </div>`;
}

// ---------- order book depth ----------
// bids/asks: [[price, size]], best first. Cumulative shares within +/-20c of the spread.
export function DepthChart({ bids, asks, height = 240 }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  const bb = bids.length ? bids[0][0] : null;
  const ba = asks.length ? asks[0][0] : null;
  if (bb == null && ba == null) return html`<div ref=${ref} class="chart-empty" style=${`height:${height}px`}>Empty order book.</div>`;
  if (!width) return html`<div ref=${ref} class="chart" style=${`height:${height}px`}></div>`;
  // Window around the spread: wide enough for context, narrow enough that a 0.1c market isn't a flat line.
  const spread = bb != null && ba != null ? ba - bb : 0.05;
  const half = Math.max(0.02, Math.min(0.15, 10 * spread));
  const lo = Math.max(0, (bb ?? ba) - half);
  const hi = Math.min(1, (ba ?? bb) + half);
  const cum = (levels, keep) => {
    let c = 0;
    return levels.filter(([p]) => keep(p)).map(([p, s]) => [p, (c += s)]);
  };
  const cb = cum(bids, (p) => p >= lo);
  const ca = cum(asks, (p) => p <= hi);
  const ymax = Math.max(1, cb.length ? cb[cb.length - 1][1] : 0, ca.length ? ca[ca.length - 1][1] : 0);
  const w = Math.max(width, 260);
  const M = { l: 56, r: 20, t: 14, b: 30 };
  const iw = w - M.l - M.r;
  const ih = height - M.t - M.b;
  const yt = niceTicks(0, ymax, 4);
  const x = linear(lo, hi, M.l, M.l + iw);
  const y = linear(0, yt[yt.length - 1], M.t + ih, M.t);
  const stepPath = (levels, edge) => {
    if (!levels.length) return "";
    let d = `M${x(levels[0][0])},${y(0)}`;
    levels.forEach(([p, c], i) => {
      if (i) d += `H${x(p)}`;
      d += `V${y(c)}`;
    });
    return d + `H${x(edge)}`;
  };
  const bidLine = stepPath(cb, lo);
  const askLine = stepPath(ca, hi);
  const close = (d, edge) => (d ? `${d}V${y(0)}H${x(edge)}Z` : "");
  const at = (levels, price, side) => {
    // cumulative size available at `price` or better
    let c = 0;
    for (const [p, cumSize] of levels) if (side === "bid" ? p >= price : p <= price) c = cumSize;
    return c;
  };
  const hx = hover;
  const xt = niceTicks(lo, hi, 5).filter((v) => v >= lo - 1e-9 && v <= hi + 1e-9);
  const centsFmt = (v) => `${+(v * 100).toFixed(2)}¢`;

  return html`<div class="chart" ref=${ref}>
    <svg width=${w} height=${height} role="img" aria-label="Order book depth">
      ${yt.map((v) => html`<g>
        <line class="grid" x1=${M.l} x2=${M.l + iw} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${M.l - 8} y=${y(v)} dy="0.32em" text-anchor="end">${compactNum(v)}</text></g>`)}
      ${xt.map((v) => html`<text class="tick" x=${x(v)} y=${height - 8} text-anchor="middle">${centsFmt(v)}</text>`)}
      <line class="axis" x1=${M.l} x2=${M.l + iw} y1=${y(0)} y2=${y(0)} />
      <path d=${close(bidLine, bb ?? lo)} fill="var(--series-1)" fill-opacity="0.1" stroke="none" />
      <path d=${close(askLine, ba ?? hi)} fill="var(--series-2)" fill-opacity="0.1" stroke="none" />
      <path d=${bidLine} fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" />
      <path d=${askLine} fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round" />
      ${hx != null && html`<line class="crosshair" x1=${x(hx)} x2=${x(hx)} y1=${M.t} y2=${M.t + ih} />`}
      <rect x=${M.l} y=${M.t} width=${iw} height=${ih} fill="transparent"
        onPointerMove=${(e) => {
          const px = e.clientX - e.currentTarget.getBoundingClientRect().left;
          setHover(lo + (px / iw) * (hi - lo));
        }}
        onPointerLeave=${() => setHover(null)} />
    </svg>
    ${hx != null && html`<${Tooltip} x=${x(hx)} y=${M.t} width=${w} title=${`at ${(hx * 100).toFixed(1)}¢`} rows=${[
      { color: "var(--series-1)", value: compactNum(at(cb, hx, "bid")), label: "shares bid at or above" },
      { color: "var(--series-2)", value: compactNum(at(ca, hx, "ask")), label: "shares offered at or below" },
    ]} />`}
  </div>`;
}
const compactNum = (n) => new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
