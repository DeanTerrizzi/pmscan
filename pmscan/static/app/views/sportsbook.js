import { cents, count, days, href, html, pct, signedPct, strategyMap, usd } from "../lib.js";
import { ChartCard, Scatter } from "../charts.js";
import { Empty, ExtLink, Notes, SortTable } from "../components.js";
import { LegsTable } from "./arbitrage.js";

const LEGEND = [
  { label: "Listed as a value bet", color: "var(--series-1)" },
  { label: "Other outcomes compared", color: "var(--deemph)" },
];

export function Sportsbook({ latest }) {
  const result = latest && latest.result;
  const opps = strategyMap(result).sportsbook_value;
  const stats = (result && result.stats) || {};
  const status = stats.sportsbook_status || "";

  const head = html`<header class="view-head">
    <h1>Sportsbook value</h1>
    <p class="sub">Polymarket game prices against the bookmakers' consensus probability (median across books,
      bookmaker margin removed). Below the diagonal, Polymarket is cheaper than the books. This is a model: positive
      expected value if the books are right, and any single game can still lose.</p>
  </header>`;

  if (!opps || status.startsWith("inactive")) {
    return html`<div class="view">${head}
      <${Empty} title="Inactive: no Odds API key">
        Get a key at <${ExtLink} href="https://the-odds-api.com">the-odds-api.com<//>, put <code>PMSCAN_ODDS_API_KEY=…</code> in <code>.env</code>, and restart <code>pmscan serve</code>.
        Each refresh costs one credit per sport per region; odds are cached for an hour by default.<//>
    </div>`;
  }

  const listed = new Set(opps.map((o) => `${o.url}|${o.details.bet}`));
  const comparisons = stats.sportsbook_comparisons || [];
  const points = comparisons.map((c) => ({
    x: c.fair, y: c.price, highlight: listed.has(`${c.url}|${c.bet}`),
    title: `${c.game}: ${c.bet}`,
    lines: [
      { value: cents(c.price), label: "Polymarket price" },
      { value: pct(c.fair), label: "bookmaker consensus" },
      { value: signedPct(c.ev), label: "expected value after fees" },
    ],
  }));

  return html`<div class="view">${head}
    <${ChartCard} title="Polymarket price vs bookmaker consensus" legend=${LEGEND}
      subtitle=${`${count(stats.sportsbook_matched)} games matched to bookmaker odds · Odds API credits left: ${stats.odds_api_requests_remaining ?? "?"}`}
      table=${{
        columns: [
          { label: "Game", get: (c) => c.game },
          { label: "Bet", get: (c) => c.bet },
          { label: "Books", get: (c) => c.fair, format: (v) => pct(v), num: true },
          { label: "Polymarket", get: (c) => c.price, format: cents, num: true },
          { label: "EV", get: (c) => c.ev, format: (v) => signedPct(v), num: true },
        ],
        rows: [...comparisons].sort((a, b) => b.ev - a.ev),
      }}>
      <${Scatter} points=${points} xDomain=${[0, 1]} yDomain=${[0, 1]} diagonal
        xFormat=${(v) => `${Math.round(v * 100)}%`} yFormat=${(v) => `${Math.round(v * 100)}¢`}
        xLabel="bookmaker consensus probability →" yLabel="Polymarket price" label="Polymarket price against bookmaker probability"
        emptyText="No games matched to bookmaker odds in this scan." />
    <//>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Value bets</h3>
        <p class="sub">${count(opps.length)} above the edge threshold · sized to the depth available at that edge</p></div></div>
      <${SortTable} rows=${opps} rowKey=${(o) => o.url + o.subtitle} initialSort=${{ key: "ev", dir: -1 }}
        empty="Nothing above the edge threshold right now."
        detail=${(o) => html`<div class="detail"><${LegsTable} legs=${o.legs} /><${Notes} notes=${o.notes} /></div>`}
        columns=${[
          { key: "title", label: "Game", text: true, get: (o) => o.title, render: (o) => html`<${ExtLink} href=${o.url}>${o.title}<//>` },
          { key: "bet", label: "Bet", text: true, get: (o) => o.subtitle },
          { key: "price", label: "Price", num: true, get: (o) => o.legs[0].avg_price, render: (o) => cents(o.legs[0].avg_price) },
          { key: "fair", label: "Books", num: true, get: (o) => o.details.fair, render: (o) => pct(o.details.fair) },
          { key: "ev", label: "EV", num: true, get: (o) => o.details.ev, render: (o) => html`<strong>${signedPct(o.details.ev)}</strong>` },
          { key: "kelly", label: "Kelly", num: true, get: (o) => o.details.kelly, render: (o) => pct(o.details.kelly) },
          { key: "starts", label: "Starts", num: true, get: (o) => o.days_to_end, render: (o) => days(o.days_to_end) },
          { key: "depth", label: "Depth", num: true, get: (o) => o.cost, render: (o) => usd(o.cost) },
          { key: "chart", label: "", get: () => 0, render: (o) => html`<a href=${href("markets", { token: o.legs[0].token_id })}>chart</a>` },
        ]} />
    </section>
  </div>`;
}
