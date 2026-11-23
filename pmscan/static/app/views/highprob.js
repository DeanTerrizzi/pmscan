import { cents, count, days, href, html, pct, storage, strategyMap, useMemo, useState, usd } from "../lib.js";
import { ChartCard, Scatter } from "../charts.js";
import { ExtLink, Notes, Segmented, SortTable } from "../components.js";

const PRICE = [
  { value: 0, label: "Any price" }, { value: 0.93, label: "≥ 93¢" }, { value: 0.95, label: "≥ 95¢" }, { value: 0.97, label: "≥ 97¢" },
];
const ENDS = [
  { value: 1e9, label: "Any time" }, { value: 1, label: "1 day" }, { value: 7, label: "7 days" }, { value: 14, label: "14 days" },
];

export function HighProb({ latest, nav }) {
  const opps = strategyMap(latest && latest.result).high_prob || [];
  const [f, setF] = useState(() => storage.get("hp.filters", { q: "", minPrice: 0, maxDays: 1e9, hideEnded: false }));
  const update = (patch) => {
    const next = { ...f, ...patch };
    setF(next);
    storage.set("hp.filters", next);
  };
  const rows = useMemo(() => {
    const q = f.q.trim().toLowerCase();
    return opps.filter((o) => {
      const leg = o.legs[0];
      if (leg.avg_price < f.minPrice) return false;
      if (o.days_to_end != null && o.days_to_end > f.maxDays) return false;
      if (f.hideEnded && o.days_to_end != null && o.days_to_end < 0) return false;
      return !q || o.title.toLowerCase().includes(q) || leg.outcome.toLowerCase().includes(q);
    });
  }, [opps, f]);

  const points = rows.map((o) => ({
    x: Math.max(-1, o.days_to_end ?? 0), y: o.legs[0].avg_price, highlight: true, token: o.legs[0].token_id,
    title: `${o.title} · buy ${o.legs[0].outcome}`,
    lines: [
      { value: cents(o.legs[0].avg_price), label: "price ≈ implied chance of winning" },
      { value: pct(o.roi, 2), label: "return if it wins" },
      { value: pct(o.annualized), label: "annualized" },
      { value: days(o.days_to_end), label: "until end date" },
    ],
  }));

  return html`<div class="view">
    <header class="view-head">
      <h1>High-probability positions</h1>
      <p class="sub">Outcomes the market prices at 90–99.5¢ that resolve within 30 days: the contracts most likely to pay. <strong>Not risk-free:</strong> the price is roughly the market's own estimate of your chance of winning, so a 96¢
        contract loses the whole stake about 4% of the time.</p>
    </header>

    <div class="filters">
      <label class="search"><span class="visually-hidden">Search</span>
        <input type="search" placeholder="Search market or outcome" value=${f.q} onInput=${(e) => update({ q: e.target.value })} /></label>
      <${Segmented} label="Minimum price" options=${PRICE} value=${f.minPrice} onChange=${(v) => update({ minPrice: v })} />
      <${Segmented} label="Ends within" options=${ENDS} value=${f.maxDays} onChange=${(v) => update({ maxDays: v })} />
      <label class="check"><input type="checkbox" checked=${f.hideEnded} onChange=${(e) => update({ hideEnded: e.target.checked })} /> Hide past end date</label>
      <span class="sub">${count(rows.length)} of ${count(opps.length)}</span>
    </div>

    <${ChartCard} title="Price against time to resolution"
      subtitle="Higher and further left = more likely and sooner. Click a dot to open its price history."
      table=${{
        columns: [
          { label: "Market", get: (o) => o.title },
          { label: "Buy", get: (o) => o.legs[0].outcome },
          { label: "Price", get: (o) => o.legs[0].avg_price, format: cents, num: true },
          { label: "Ends", get: (o) => o.days_to_end, format: days, num: true },
        ],
        rows,
      }}>
      <${Scatter} points=${points} yDomain=${[0.9, 1]} xFormat=${(v) => `${v}d`} yFormat=${(v) => `${Math.round(v * 100)}¢`}
        xLabel="days until end date →" yLabel="price" label="High-probability positions by price and time to end"
        onSelect=${(p) => nav("markets", { token: p.token })} emptyText="Nothing matches these filters." />
    <//>

    <section class="card">
      <${SortTable} rows=${rows} rowKey=${(o) => o.legs[0].token_id} initialSort=${{ key: "annualized", dir: -1 }}
        empty="Nothing matches these filters."
        detail=${(o) => html`<div class="detail"><${Notes} notes=${o.notes} />
          <p><a href=${href("markets", { token: o.legs[0].token_id })}>Price history and order book</a> ·
          <${ExtLink} href=${o.url}>Open on Polymarket ↗<//></p></div>`}
        columns=${[
          { key: "title", label: "Market", text: true, get: (o) => o.title, render: (o) => html`<${ExtLink} href=${o.url}>${o.title}<//>` },
          { key: "outcome", label: "Buy", text: true, get: (o) => o.legs[0].outcome },
          { key: "price", label: "Price", num: true, get: (o) => o.legs[0].avg_price, render: (o) => cents(o.legs[0].avg_price) },
          { key: "roi", label: "ROI if win", num: true, get: (o) => o.roi, render: (o) => pct(o.roi, 2) },
          { key: "annualized", label: "Annualized", num: true, get: (o) => o.annualized, render: (o) => pct(o.annualized) },
          { key: "days", label: "Ends", num: true, get: (o) => o.days_to_end, render: (o) => days(o.days_to_end) },
          { key: "depth", label: "Depth", num: true, get: (o) => o.cost, render: (o) => usd(o.cost) },
        ]} />
    </section>
  </div>`;
}
