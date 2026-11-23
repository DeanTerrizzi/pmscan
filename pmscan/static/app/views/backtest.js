import { count, dateTime, html, pct, storage, truncate, useState, usePoll } from "../lib.js";
import { ChartCard, StatTile, Tooltip, niceTicks, useWidth } from "../charts.js";
import { Empty, Segmented, StatusChip } from "../components.js";

const HORIZON_LABELS = { "1h": "1 hour", "6h": "6 hours", "1d": "1 day", "3d": "3 days", "7d": "7 days" };
const VERDICT = {
  profitable: ["good", "Made money"],
  "loses money": ["critical", "Lost money"],
  unclear: ["neutral", "Can't tell yet"],
};
const signedPct = (x, d = 2) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${pct(Math.abs(x), d)}`);

function verdictChip(s) {
  if (!s || !s.n) return html`<span class="muted">–</span>`;
  const [status, label] = VERDICT[s.verdict] || VERDICT.unclear;
  return html`<${StatusChip} status=${status}>${label}<//>`;
}

async function startRun(perGroup) {
  const res = await fetch("/api/backtest/run", {
    method: "POST", headers: { "Content-Type": "application/json", "X-Pmscan": "1" },
    body: JSON.stringify({ per_group: perGroup }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 409) throw new Error(body.error || `${res.status}`);
}

// One row per group: actual win rate (dot) with its 95% range (line), and the
// win rate needed to break even after fees (tick). Right of the tick = made money.
function WinRateChart({ rows }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  const ROW = 34;
  const M = { l: Math.min(230, Math.max(140, width * 0.32)), r: 24, t: 8, b: 40 };
  const height = M.t + M.b + rows.length * ROW;
  if (!width) return html`<div ref=${ref} class="chart" style=${`height:${height}px`}></div>`;
  const lowest = Math.min(...rows.map((r) => Math.min(r.s.ci_low, r.s.breakeven)));
  const ticks = niceTicks(Math.max(0, Math.floor(lowest * 50) / 50), 1, 5);
  const x0 = ticks[0];
  const x1 = ticks[ticks.length - 1];
  const iw = width - M.l - M.r;
  const x = (v) => M.l + ((v - x0) / (x1 - x0 || 1)) * iw;
  const y = (i) => M.t + i * ROW + ROW / 2;
  const h = hover != null ? rows[hover] : null;
  return html`<div class="chart" ref=${ref}>
    <svg width=${width} height=${height} role="img" aria-label="Actual win rate against the win rate needed to break even, by group">
      ${ticks.map((v) => html`<g>
        <line class="grid" x1=${x(v)} x2=${x(v)} y1=${M.t} y2=${height - M.b} />
        <text class="tick" x=${x(v)} y=${height - M.b + 16} text-anchor="middle">${pct(v, 0)}</text></g>`)}
      <text class="axis-title" x=${M.l + iw} y=${height - 4} text-anchor="end">win rate →</text>
      ${rows.map((r, i) => html`<g>
        <text class=${`tick ${r.key === "all" ? "strong" : ""}`} x=${M.l - 10} y=${y(i)} dy="0.32em" text-anchor="end">
          ${truncate(r.label, Math.floor(M.l / 6.6))}</text>
        <line x1=${x(r.s.ci_low)} x2=${x(r.s.ci_high)} y1=${y(i)} y2=${y(i)} stroke="var(--series-1)" stroke-width="2" stroke-linecap="round" />
        <line x1=${x(r.s.breakeven)} x2=${x(r.s.breakeven)} y1=${y(i) - 8} y2=${y(i) + 8} stroke="var(--ink-2)" stroke-width="2" />
        <circle cx=${x(r.s.win_rate)} cy=${y(i)} r="4.5" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" />
        ${hover === i && html`<circle cx=${x(r.s.win_rate)} cy=${y(i)} r="7" fill="none" stroke="var(--ink)" stroke-width="1.5" />`}
        <rect x="0" y=${y(i) - ROW / 2} width=${width} height=${ROW} fill="transparent"
          onPointerEnter=${() => setHover(i)} onPointerLeave=${() => setHover(null)} />
      </g>`)}
    </svg>
    ${h && html`<${Tooltip} x=${x(h.s.win_rate)} y=${Math.max(0, y(hover) - 70)} width=${width} title=${h.label} rows=${[
      { value: pct(h.s.win_rate, 1), label: `won (95% range ${pct(h.s.ci_low, 1)}–${pct(h.s.ci_high, 1)})`, color: "var(--series-1)" },
      { value: pct(h.s.breakeven, 1), label: "needed to break even", color: "var(--ink-2)" },
      { value: signedPct(h.s.roi), label: `per $1 after fees · ${count(h.s.n)} bets` },
    ]} />`}
  </div>`;
}

export function Backtest() {
  const [horizon, setHorizon] = useState(() => storage.get("backtest.horizon", "1d"));
  const [perGroup, setPerGroup] = useState(1000);
  const [startError, setStartError] = useState(null);
  const [kick, setKick] = useState(0);
  const res = usePoll("/api/backtest", { kick }, 5000);
  const d = res.data;
  const r = d && d.result;
  const running = d && d.running;

  const head = html`<header class="view-head">
    <h1>Backtest: the 90¢+ strategy</h1>
    <p class="sub">What would have happened if you had bought every Polymarket US contract priced 90–99.5¢ some time
      before its market ended. It replays settled markets' real price history, settles each bet the way Polymarket did,
      and charges today's US taker fee. Results are per $1 staked.</p>
  </header>`;

  const run = async () => {
    setStartError(null);
    try {
      await startRun(perGroup);
      setKick((k) => k + 1);
    } catch (e) {
      setStartError(e.message);
    }
  };
  const controls = html`<section class="card run-bar">
    <div style="flex:1;min-width:220px">
      ${running ? html`<strong>Running…</strong> <span class="sub">${d.progress || ""}</span>`
        : r ? html`<span>Last run ${dateTime(r.generated_at)} · ${count(r.markets_sampled)} settled markets · ${count(r.bets)} simulated bets</span>`
        : html`<span>No backtest yet. A run samples settled markets in every category and takes 10–20 minutes.</span>`}
      ${d && d.error && html`<div class="sub" style="color:var(--critical)">Last run failed: ${d.error}</div>`}
      ${startError && html`<div class="sub" style="color:var(--critical)">${startError}</div>`}
    </div>
    <${Segmented} label="Markets per group" value=${perGroup} onChange=${setPerGroup}
      options=${[500, 1000, 2000, 4000].map((n) => ({ value: n, label: `${count(n)} / group` }))} />
    <button class="ghost" disabled=${running} onClick=${run}>${r ? "Run again" : "Run backtest"}</button>
  </section>`;

  if (!d) return html`<div class="view">${head}<p class="empty">${res.error ? res.error.message : "Loading…"}</p></div>`;
  if (!r) return html`<div class="view">${head}${controls}<${Empty} title="Nothing to show yet">
    Start a run above, or run <code>uv run pmscan backtest</code> in the project folder.<//></div>`;

  const groups = r.groups.filter((g) => g.horizons[horizon] && g.horizons[horizon].n);
  const all = r.groups.find((g) => g.key === "all").horizons[horizon];
  const chartRows = groups.filter((g) => g.horizons[horizon].n >= 10).map((g) => ({ key: g.key, label: g.label, s: g.horizons[horizon] }));
  const check = r.settlement_check || {};

  return html`<div class="view">${head}${controls}
    <div class="filters">
      <${Segmented} label="Bought this long before the end" value=${horizon}
        onChange=${(v) => (setHorizon(v), storage.set("backtest.horizon", v))}
        options=${r.horizons.map((h) => ({ value: h, label: `${HORIZON_LABELS[h] || h} before` }))} />
    </div>

    ${all && all.n ? html`<section class="tiles wide">
      <${StatTile} label="Return per $1, after fees" value=${signedPct(all.roi)} sub=${`${signedPct(all.roi_no_fees)} before fees`} />
      <${StatTile} label="Won" value=${pct(all.win_rate, 1)} sub=${`95% range ${pct(all.ci_low, 1)}–${pct(all.ci_high, 1)}`} />
      <${StatTile} label="Needed to break even" value=${pct(all.breakeven, 1)} sub=${`prices implied ${pct(all.implied, 1)}`} />
      <${StatTile} label="Simulated bets" value=${count(all.n)} sub=${verdictText(all)} />
    </section>` : html`<p class="empty">No bets at this horizon.</p>`}

    <${ChartCard} title="Did it win often enough to beat the fees?"
      subtitle="Dot: how often the bets won, with the line showing the 95% range. Tick: the win rate needed to break even after fees. A dot right of its tick made money; when the range crosses the tick, there isn't enough data to tell."
      legend=${[{ label: "Actual win rate (95% range)", color: "var(--series-1)" }, { label: "Break-even win rate", color: "var(--ink-2)", shape: "line" }]}
      table=${{ columns: groupColumns, rows: groups.map((g) => ({ label: g.label, ...g.horizons[horizon] })) }}>
      ${chartRows.length ? html`<${WinRateChart} rows=${chartRows} />` : html`<p class="empty">Too few bets per group at this horizon.</p>`}
    <//>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Every group and horizon</h3>
        <p class="sub">Return per $1 after fees. Game markets are bought before the game starts; fewer of them are listed
          days ahead, so the longer horizons have fewer bets.</p></div></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Group</th>${r.horizons.map((h) => html`<th class="num">${HORIZON_LABELS[h]} before</th>`)}</tr></thead>
        <tbody>${r.groups.filter((g) => g.sampled).map((g) => html`<tr>
          <td>${g.key === "all" ? html`<strong>${g.label}</strong>` : g.label}</td>
          ${r.horizons.map((h) => {
            const s = g.horizons[h];
            return html`<td class="num">${s && s.n ? html`<div>${signedPct(s.roi)}</div>
              <div class="sub">${verdictChip(s)} · ${count(s.n)}</div>` : html`<span class="muted">–</span>`}</td>`;
          })}
        </tr>`)}</tbody>
      </table></div>
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>By price</h3>
        <p class="sub">All groups, ${HORIZON_LABELS[horizon]} before the end. Fees are smallest near $1, but so is the
          room for a loss to be made back.</p></div></div>
      <${DataTableLite} rows=${r.buckets.map((b) => ({ label: `${(b.low * 100).toFixed(0)}–${(b.high * 100).toFixed(1)}¢`, ...b.horizons[horizon] }))} />
    </section>

    ${r.worst_losses && r.worst_losses.length ? html`<section class="card">
      <div class="card-head"><div class="card-titles"><h3>Losses at the highest prices (1 day before)</h3>
        <p class="sub">The bets that lost while priced closest to certain: what the strategy's risk looks like.</p></div></div>
      <ul class="notes">${r.worst_losses.map((l) => html`<li>${(l.price * 100).toFixed(1)}¢ on ${l.side === 0 ? "YES" : "NO"}:
        ${truncate(l.title || l.slug, 110)}</li>`)}</ul>
    </section>` : null}

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>How to read this</h3></div></div>
      <ul class="notes">
        <li>Prices are the top of the book with no depth, so a real $50–100 order could fill slightly worse.</li>
        <li>Markets in one event move together (one game has many player props), so the real uncertainty is somewhat
          wider than the ranges shown.</li>
        <li>Fees are today's lowest-volume US rate (theta ${0.095} × p × (1 − p) per contract). Voided markets are left out.</li>
        <li>${`Settled results were cross-checked against Polymarket's settlement records for ${check.agree ?? "–"} of ${check.checked ?? "–"} sampled markets.`}</li>
        <li>Each run adds to a cached sample, so running again with more markets per group narrows the ranges.</li>
      </ul>
    </section>
  </div>`;
}

function verdictText(s) {
  const [, label] = VERDICT[s.verdict] || VERDICT.unclear;
  return label;
}

const groupColumns = [
  { label: "Group", get: (g) => g.label },
  { label: "Bets", get: (g) => g.n, format: count, num: true },
  { label: "Won", get: (g) => g.win_rate, format: (v) => pct(v, 1), num: true },
  { label: "95% range", render: (g) => `${pct(g.ci_low, 1)}–${pct(g.ci_high, 1)}` },
  { label: "Break-even", get: (g) => g.breakeven, format: (v) => pct(v, 1), num: true },
  { label: "Per $1 after fees", get: (g) => g.roi, format: (v) => signedPct(v), num: true },
  { label: "Verdict", render: (g) => verdictChip(g) },
];

function DataTableLite({ rows }) {
  const shown = rows.filter((r) => r.n);
  if (!shown.length) return html`<p class="empty">No bets at this horizon.</p>`;
  return html`<div class="table-wrap"><table>
    <thead><tr><th>Price</th>${groupColumns.slice(1).map((c) => html`<th class=${c.num ? "num" : ""}>${c.label}</th>`)}</tr></thead>
    <tbody>${shown.map((r) => html`<tr><td>${r.label}</td>${groupColumns.slice(1).map((c) => html`<td class=${c.num ? "num" : ""}>${
      c.render ? c.render(r) : c.format(c.get(r))}</td>`)}</tr>`)}</tbody>
  </table></div>`;
}
