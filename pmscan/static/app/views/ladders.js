import { cents, count, days, html, pct, strategyMap, usd } from "../lib.js";
import { centsTick, ChartCard, LadderChart } from "../charts.js";
import { Empty, ExtLink, Notes } from "../components.js";
import { LegsTable } from "./arbitrage.js";

const LEGEND = [
  { label: "The out-of-order pair", color: "var(--series-1)" },
  { label: "Other rungs", color: "var(--deemph)" },
];

function LadderCard({ o }) {
  const d = o.details || {};
  const rungs = d.rungs || [];
  return html`<${ChartCard} className="ladder-card" title=${o.title}
    subtitle=${o.live ? `${o.subtitle} · found by the live feed` : o.subtitle} legend=${LEGEND}
    table=${{
      columns: [
        { label: "Rung", get: (r) => r.question || r.label },
        { label: "YES bid", get: (r) => r.bid, format: cents, num: true },
        { label: "YES ask", get: (r) => r.ask, format: cents, num: true },
      ],
      rows: rungs,
    }}>
    <p class="chart-note">Each rung's YES price range (filled dot = bid, ring = ask), strictest on the left.
      Prices should rise to the right; the accented pair doesn't.</p>
    <${LadderChart} rungs=${rungs} strictIndex=${d.strict_index ?? 0} looseIndex=${d.loose_index ?? 1} format=${cents} tickFormat=${centsTick} />
    <div class="ladder-summary">
      <span>Pay <strong>${usd(o.cost, 2)}</strong></span>
      <span>receive at least <strong>${usd(o.payout, 2)}</strong></span>
      <span>profit <strong>${usd(o.profit, 2)}</strong> (${pct(o.roi, 2)})</span>
      <span>ends ${days(o.days_to_end)}</span>
      <${ExtLink} href=${o.url}>Open on Polymarket ↗<//>
    </div>
    <${LegsTable} legs=${o.legs} />
    <${Notes} notes=${o.notes} />
  <//>`;
}

export function Ladders({ latest }) {
  const result = latest && latest.result;
  const opps = strategyMap(result).ladder_arb;
  const stats = (result && result.stats) || {};
  return html`<div class="view">
    <header class="view-head">
      <h1>Ladder arbitrage</h1>
      <p class="sub">Nested markets: "above $110k" implies "above $100k", "by June" implies "by December". When the
        stricter market is priced above the looser one, YES on the looser plus NO on the stricter pays at least $1.
        Nesting is inferred from the wording, so check both markets' rules before trading.</p>
    </header>
    ${!opps
      ? html`<${Empty} title="Ladder arbitrage is not enabled">Enable <code>[ladder_arb]</code> in pmscan.toml.<//>`
      : opps.length
        ? opps.map((o) => html`<${LadderCard} o=${o} />`)
        : html`<${Empty} title="Nothing out of order right now">
            ${count(stats.ladders)} ladders checked this scan. Examples it understood:
            <ul class="examples">${(stats.ladder_examples || []).map((e) => html`<li><strong>${e.event}</strong> (${e.kind}):
              ${e.strict_to_loose.join(" → ")}</li>`)}</ul><//>`}
  </div>`;
}
