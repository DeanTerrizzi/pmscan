import { cents, compact, days, href, html, truncate, useDebounced, usePoll, useState, usd } from "../lib.js";
import { centsTick, ChartCard, DepthChart, LineChart, StatTile } from "../charts.js";
import { Empty, ExtLink, Segmented } from "../components.js";

const INTERVALS = [
  { value: "1d", label: "1 day" }, { value: "1w", label: "1 week" }, { value: "1m", label: "1 month" }, { value: "max", label: "All" },
];

export function Markets({ route }) {
  return route.params.token ? html`<${MarketDetail} token=${route.params.token} />` : html`<${MarketSearch} />`;
}

function MarketSearch() {
  const [q, setQ] = useState("");
  const query = useDebounced(q, 250);
  const res = usePoll("/api/markets", { q: query, limit: 50 });
  const rows = res.data || [];
  return html`<div class="view">
    <header class="view-head">
      <h1>Markets</h1>
      <p class="sub">Search every open market from the latest scan, then open its price history and order book.</p>
    </header>
    <div class="filters">
      <label class="search wide"><span class="visually-hidden">Search markets</span>
        <input type="search" autofocus placeholder="Search, e.g. bitcoin november, fed, lakers" value=${q}
          onInput=${(e) => setQ(e.target.value)} /></label>
      <span class="sub">${res.error ? res.error.message : `${rows.length} shown, by 24h volume`}</span>
    </div>
    <section class=${`card ${res.loading ? "refreshing" : ""}`}>
      ${rows.length
        ? html`<div class="table-wrap"><table class="data"><thead><tr>
            <th>Market</th><th class="num">YES bid</th><th class="num">YES ask</th><th class="num">24h volume</th>
            <th class="num">Liquidity</th><th class="num">Ends</th></tr></thead>
            <tbody>${rows.map((m) => html`<tr>
              <td><a href=${href("markets", { token: m.token_ids[0] })}>${truncate(m.question, 110)}</a>
                ${m.event && m.event !== m.question && html`<div class="sub">${truncate(m.event, 90)}</div>`}</td>
              <td class="num">${cents(m.bid)}</td><td class="num">${cents(m.ask)}</td>
              <td class="num">${usd(m.volume_24h)}</td><td class="num">${usd(m.liquidity)}</td>
              <td class="num">${days(m.end_date ? (Date.parse(m.end_date) - Date.now()) / 864e5 : null)}</td></tr>`)}
            </tbody></table></div>`
        : html`<p class="empty">${res.loading ? "Searching…" : "No markets match."}</p>`}
    </section>
  </div>`;
}

function MarketDetail({ token }) {
  const market = usePoll("/api/market", { token });
  const [interval, setRange] = useState("1w");
  const m = market.data;
  const side = m ? Math.max(0, m.token_ids.indexOf(token)) : 0;
  const hist = usePoll("/api/price-history", { token, interval });
  const book = usePoll("/api/book", { token }, 15000);

  if (market.error) {
    return html`<div class="view"><p><a href=${href("markets")}>← All markets</a></p>
      <${Empty} title="Market not found">${market.error.message}. It may have closed since the last scan.<//></div>`;
  }
  if (!m) return html`<div class="view"><p class="empty">Loading…</p></div>`;

  // Gamma quotes are for the first outcome; the second mirrors them.
  const bid = side === 0 ? m.bid : m.ask != null ? 1 - m.ask : null;
  const ask = side === 0 ? m.ask : m.bid != null ? 1 - m.bid : null;
  const points = (hist.data || []).map((p) => ({ t: p.t * 1000, v: p.p }));
  const b = book.data;

  return html`<div class="view">
    <p><a href=${href("markets")}>← All markets</a></p>
    <header class="view-head">
      <h1>${m.question}</h1>
      <p class="sub">${m.event !== m.question ? `${m.event} · ` : ""}<${ExtLink} href=${m.url}>Open on Polymarket ↗<//></p>
    </header>

    <div class="filters">
      <${Segmented} label="Outcome" value=${token}
        onChange=${(t) => (location.hash = href("markets", { token: t }))}
        options=${m.token_ids.map((t, i) => ({ value: t, label: m.outcomes[i] || (i ? "No" : "Yes") }))} />
      <${Segmented} label="Price history range" options=${INTERVALS} value=${interval} onChange=${setRange} />
    </div>

    <section class="tiles wide">
      <${StatTile} label=${`${m.outcomes[side] || "Outcome"} bid`} value=${cents(bid)} />
      <${StatTile} label=${`${m.outcomes[side] || "Outcome"} ask`} value=${cents(ask)}
        sub=${bid != null && ask != null ? `spread ${((ask - bid) * 100).toFixed(1)}¢` : ""} />
      <${StatTile} label="24h volume" value=${usd(m.volume_24h)} />
      <${StatTile} label="Liquidity" value=${usd(m.liquidity)} />
      <${StatTile} label="Ends" value=${days(m.end_date ? (Date.parse(m.end_date) - Date.now()) / 864e5 : null)}
        sub=${m.fee_rate ? `taker fee rate ${m.fee_rate}` : "no taker fee"} />
    </section>

    <${ChartCard} title=${`${m.outcomes[side] || "Outcome"} price history`}
      subtitle="Price of one share; it pays $1 if this outcome wins"
      table=${{ columns: [
        { label: "Time", get: (p) => new Date(p.t).toLocaleString() },
        { label: "Price", get: (p) => p.v, format: cents, num: true },
      ], rows: [...points].reverse() }}>
      <div class=${hist.loading ? "refreshing" : ""}>
        <${LineChart} points=${points} yFormat=${cents} tickFormat=${centsTick} label="Price history" valueLabel="price" height=${240}
          emptyText=${hist.loading ? "Loading…" : hist.error ? hist.error.message : "No trades in this range."} />
      </div>
    <//>

    <${ChartCard} title="Order book depth"
      subtitle=${`Shares available at each price or better, around the spread · best bid ${cents(bid)}, best ask ${cents(ask)} · refreshes every 15s`}
      legend=${[{ label: "Bids (buyers)", color: "var(--series-1)", shape: "line" }, { label: "Asks (sellers)", color: "var(--series-2)", shape: "line" }]}
      table=${b ? { columns: [
        { label: "Side", get: (r) => r.side },
        { label: "Price", get: (r) => r.price, format: cents, num: true },
        { label: "Shares", get: (r) => r.size, format: compact, num: true },
      ], rows: [
        ...b.asks.slice(0, 15).reverse().map(([price, size]) => ({ side: "Ask", price, size })),
        ...b.bids.slice(0, 15).map(([price, size]) => ({ side: "Bid", price, size })),
      ] } : null}>
      ${b ? html`<${DepthChart} bids=${b.bids} asks=${b.asks} />`
        : html`<p class="empty">${book.error ? book.error.message : "Loading…"}</p>`}
    <//>
  </div>`;
}
