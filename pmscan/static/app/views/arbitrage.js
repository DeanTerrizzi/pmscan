import { cents, count, dateTime, days, href, html, pct, strategyMap, truncate, usd } from "../lib.js";
import { ChartCard, DataTable, DivergingBars, Meter } from "../charts.js";
import { ExtLink, Notes, SortTable, StatusChip } from "../components.js";

export function LegsTable({ legs }) {
  return html`<${DataTable} columns=${[
    { label: "Outcome", get: (l) => l.outcome },
    { label: "Market", render: (l) => html`<a href=${href("markets", { token: l.token_id })}>${truncate(l.market, 80)}</a>` },
    { label: "Shares", get: (l) => l.shares, format: (v) => v.toLocaleString(undefined, { maximumFractionDigits: 1 }), num: true },
    { label: "Avg price", get: (l) => l.avg_price, format: cents, num: true },
    { label: "Limit price", get: (l) => l.limit_price, format: cents, num: true },
  ]} rows=${legs} />`;
}

export function BasketDetail({ o }) {
  return html`<div class="detail">
    <div class="detail-meter">
      <div class="meter-labels"><span>Pay <strong>${usd(o.cost, 2)}</strong></span>
        <span>receive <strong>${usd(o.payout, 2)}</strong> ${o.strategy === "high_prob" ? "if it wins" : "guaranteed"}</span></div>
      <${Meter} value=${o.cost} max=${o.payout} label="Cost as a share of payout" />
      <div class="sub">Profit ${usd(o.profit, 2)} · ${pct(o.roi, 2)} · ${o.annualized != null ? `${pct(o.annualized)} annualized` : ""}</div>
    </div>
    <${LegsTable} legs=${o.legs} />
    <${Notes} notes=${o.notes} />
    <p><${ExtLink} href=${o.url}>Open on Polymarket ↗<//></p>
  </div>`;
}

export const riskChip = (risk) =>
  risk === "low" ? html`<${StatusChip} status="good">low<//>` : html`<${StatusChip} status="warning">${risk}<//>`;

export function Arbitrage({ latest }) {
  const result = latest && latest.result;
  const opps = strategyMap(result).neg_risk_arb;
  const stats = (result && result.stats) || {};
  const closest = stats.neg_risk_closest || [];

  return html`<div class="view">
    <header class="view-head">
      <h1>Neg-risk arbitrage</h1>
      <p class="sub">In an event where at most one outcome can win, buying YES on every outcome for under $1,
        or NO on k outcomes for under k − 1 dollars, locks in a profit. Each basket below is sized against live
        order books after taker fees.</p>
    </header>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Baskets available now</h3>
        <p class="sub">${opps ? `${count(opps.length)} found across ${count(stats.neg_risk_groups)} neg-risk groups · click a row for its legs` : "Strategy not enabled"}</p></div></div>
      <${SortTable} rows=${opps || []} rowKey=${(o) => o.url + o.kind} initialSort=${{ key: "profit", dir: -1 }}
        empty="None right now. They're rare and get taken quickly; the chart below shows how close each group is."
        detail=${(o) => html`<${BasketDetail} o=${o} />`}
        columns=${[
          { key: "title", label: "Event", text: true, get: (o) => o.title, render: (o) => html`<${ExtLink} href=${o.url}>${o.title}<//>
            ${o.live && html` <span class="badge live" title=${`Found by the live feed at ${dateTime(o.found_at)}`}>live feed</span>`}` },
          { key: "basket", label: "Basket", get: (o) => o.legs.length, render: (o) => `${o.kind === "buy_all_yes" ? "YES" : "NO"} × ${o.legs.length}` },
          { key: "cost", label: "Cost", num: true, get: (o) => o.cost, render: (o) => usd(o.cost) },
          { key: "profit", label: "Profit", num: true, get: (o) => o.profit, render: (o) => html`<strong>${usd(o.profit, 2)}</strong>` },
          { key: "roi", label: "ROI", num: true, get: (o) => o.roi, render: (o) => pct(o.roi, 2) },
          { key: "annualized", label: "Annualized", num: true, get: (o) => o.annualized, render: (o) => pct(o.annualized) },
          { key: "days", label: "Ends", num: true, get: (o) => o.days_to_end, render: (o) => days(o.days_to_end) },
          { key: "risk", label: "Risk", text: true, get: (o) => o.risk, render: (o) => riskChip(o.risk) },
        ]} />
    </section>

    <${ChartCard} title="How close each group is to arbitrage"
      subtitle="Top-of-book return of the best basket in each neg-risk group, before checking depth. Above zero is an edge; most of those turn out too thin once the full book is walked."
      table=${{
        columns: [
          { label: "Event", render: (c) => html`<${ExtLink} href=${c.url}>${c.event}<//>` },
          { label: "Basket", get: (c) => `${c.kind === "buy_all_yes" ? "YES" : "NO"} × ${c.legs ?? "?"}` },
          { label: "Top-of-book return", get: (c) => c.roi, format: (v) => pct(v, 2), num: true },
        ],
        rows: closest,
      }}>
      <${DivergingBars} label="Top-of-book return by neg-risk group" format=${(v) => `${v > 0 ? "+" : ""}${pct(v, 2)}`}
        rows=${closest.map((c) => ({
          label: c.event, value: c.roi, href: c.url,
          detail: `${c.kind === "buy_all_yes" ? "Buy every YES" : "Buy every NO"}${c.legs ? ` · ${c.legs} legs` : ""}`,
        }))} />
    <//>
  </div>`;
}
