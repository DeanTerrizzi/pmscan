import { cents, count, days, href, html, pct, signedPct, strategyMap, usd, usePoll } from "../lib.js";
import { StatTile } from "../charts.js";
import { ExtLink, Notes, SortTable } from "../components.js";

const BUCKETS = [0.97, 0.98, 0.99];
const bucketOf = (p) => (p < 0.98 ? 0.97 : p < 0.99 ? 0.98 : 0.99);
const money = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}$${Math.abs(x).toFixed(2)}`);
// "sports:futures" in the settings is the backtest's "futures" group; other entries are categories.
const groupOf = (c) => (c === "sports:futures" ? "futures" : c.split(":")[0]);

function recordText(r) {
  if (!r) return html`<span class="muted">no history</span>`;
  return html`<div>${count(r.won)} of ${count(r.n)} won</div><div class="sub">${signedPct(r["return"], 2)} per $1</div>`;
}

export function NearCertain({ latest }) {
  const res = usePoll("/api/near-certain", {}, 60000);
  const d = res.data || {};
  const s = d.settings || {};
  const p = d.paper;
  const rows = strategyMap(latest && latest.result).near_certain || [];
  const record = (d.record && d.record.rows) || [];
  const cell = (group, bucket) => record.find((r) => r.group === group && Math.abs(r.bucket - bucket) < 1e-9);
  const included = new Set((s.include_us || []).map(groupOf));
  const groups = [...new Set(record.map((r) => r.group))]
    .filter((g) => (cell(g, 0) || {}).n >= 20)
    .sort((a, b) => cell(b, 0).n - cell(a, 0).n);
  const historyOf = (o) => cell((o.details || {}).group, bucketOf(o.legs[0].avg_price));

  return html`<div class="view">
    <header class="view-head">
      <h1>Near-certain bets</h1>
      <p class="sub">Buy the "it won't happen" side of things that almost never happen, at ${cents(s.min_price || 0.97)}–${cents(s.max_price || 0.995)},
        within ${s.max_days || 3} days of the end. Each bet makes only 0.5–3% and one surprise loses the whole stake, so
        it only works where things priced this way really do almost never happen. In the backtest that was politics and
        nothing else.</p>
    </header>

    <section class="tiles wide">
      <${StatTile} label="Found now" value=${count(rows.length)}
        sub=${`${(s.include_us || []).join(", ") || "every category"} · ending within ${s.max_days || 3} days`} />
      <${StatTile} label="Paper bets open" value=${p ? count(p.open_bets) : "–"}
        sub=${p ? `${usd(p.stake_usd)} each · since ${new Date(p.started_at).toLocaleDateString()}` : "paper trading off"} />
      <${StatTile} label="Paper results" value=${p && p.resolved ? `${count(p.won)} of ${count(p.resolved)} won` : "none settled yet"}
        sub=${p ? `${money(p.realized_pnl)} realized` : ""} />
      <${StatTile} label="Paper P&L" value=${p ? money(p.pnl) : "–"} sub=${p ? "open bets valued at the bid" : ""} />
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Available now</h3>
        <p class="sub">Checked against live order books after fees. "History" is how settled markets in the same category
          at the same price did.</p></div></div>
      <${SortTable} rows=${rows} rowKey=${(o) => o.legs[0].token_id} initialSort=${{ key: "annualized", dir: -1 }}
        empty=${latest && latest.result
          ? "Nothing right now. These come up mostly in the last few days before political markets end."
          : "Waiting for the first scan."}
        detail=${(o) => html`<div class="detail"><${Notes} notes=${o.notes} />
          <p><a href=${href("markets", { token: o.legs[0].token_id })}>Price history and order book</a> ·
          <${ExtLink} href=${o.url}>Open on Polymarket ↗<//></p></div>`}
        columns=${[
          { key: "title", label: "Market", text: true, get: (o) => o.title, render: (o) => html`<${ExtLink} href=${o.url}>${o.title}<//>` },
          { key: "outcome", label: "Buy", text: true, get: (o) => o.legs[0].outcome },
          { key: "price", label: "Price", num: true, get: (o) => o.legs[0].avg_price, render: (o) => cents(o.legs[0].avg_price) },
          { key: "history", label: "History", num: true, get: (o) => (historyOf(o) || {})["return"],
            render: (o) => recordText(historyOf(o)) },
          { key: "roi", label: "If right", num: true, get: (o) => o.roi, render: (o) => pct(o.roi, 2) },
          { key: "loss", label: "1 loss =", num: true, get: (o) => (o.profit > 0 ? o.cost / o.profit : null),
            render: (o) => (o.profit > 0 ? `${count(Math.round(o.cost / o.profit))} wins` : "–") },
          { key: "annualized", label: "Annualized", num: true, get: (o) => o.annualized, render: (o) => pct(o.annualized) },
          { key: "days", label: "Ends", num: true, get: (o) => o.days_to_end, render: (o) => days(o.days_to_end) },
          { key: "depth", label: "Depth", num: true, get: (o) => o.cost, render: (o) => usd(o.cost) },
        ]} />
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Track record by category</h3>
        <p class="sub">${count(d.record && d.record.markets)} settled Polymarket US markets from the backtest. Each counts
          once: its favorite bought 1 hour to 3 days before the end, after taker fees. A 99¢ bet has to win more than
          99.1% of the time just to break even.</p></div></div>
      ${groups.length ? html`<div class="table-wrap"><table class="data">
        <thead><tr><th>Category</th>${BUCKETS.map((b) => html`<th class="num">${Math.round(b * 100)}¢</th>`)}
          <th class="num">All</th><th>Bought here</th></tr></thead>
        <tbody>${groups.map((g) => html`<tr>
          <td>${(d.record.groups || {})[g] || g}</td>
          ${[...BUCKETS, 0].map((b) => html`<td class="num">${recordText(cell(g, b))}</td>`)}
          <td>${included.has(g) ? "Yes" : html`<span class="muted">No</span>`}</td>
        </tr>`)}</tbody>
      </table></div>` : html`<p class="empty">${res.loading ? "Loading…" : "No backtest data yet: run one from the Backtest tab."}</p>`}
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>How to read this</h3></div></div>
      <ul class="notes">
        <li>The paper portfolio buys every bet listed above with ${usd(s.stake_usd || 200)}${s.max_bets_per_event
          ? `, at most ${s.max_bets_per_event} per event,` : ""} and holds it to settlement. It is
          kept separate from the 90¢+ portfolio, which also holds some of the same positions.</li>
        <li>Treat the politics record with care: almost all of it is one night, the November 2026 midterms (safe House
          and Senate seats priced at 98¢), so its markets aren't independent tests. If the real loss rate were 1 in 100
          rather than 4 in 823, 99¢ bets would lose money. The paper portfolio is the real test.</li>
        <li>Most political markets run for months. Expect few bets until a batch nears its end date, for example the
          many that end December 31.</li>
        <li>Recent news didn't help in the backtest: long shots whose price had just jumped came true less often, not more.
          A market past its end date at these prices may be in a dispute; read its rules before trusting it.</li>
      </ul>
    </section>
  </div>`;
}
