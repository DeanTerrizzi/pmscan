import { cents, count, dateTime, days, href, html, pct, storage, truncate, useMemo, usePoll, useState, usd } from "../lib.js";
import { ChartCard, DataTable, LineChart, StatTile } from "../charts.js";
import { Empty, ExtLink, Segmented, SortTable, StatusChip } from "../components.js";

const signedUsd = (x) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${usd(Math.abs(x), 2)}`);
const untilEnd = (iso) => (iso ? days((Date.parse(iso) - Date.now()) / 864e5) : "–");
const winRate = (s) => (s.win_rate == null ? "–"
  : s.implied_win_rate == null ? pct(s.win_rate) : `${pct(s.win_rate)} vs ${pct(s.implied_win_rate)} implied`);

export function Paper({ latest }) {
  const res = usePoll("/api/paper", {}, 15000);
  const [only, setOnly] = useState(() => storage.get("paper.strategy", "all"));
  const d = res.data;
  const enabled = latest && latest.paper ? latest.paper.enabled : true;
  const books = d ? Object.entries(d.books) : [];
  const rows = useMemo(() => {
    const pick = (key) => books.filter(([n]) => only === "all" || n === only)
      .flatMap(([, b]) => b[key].map((p) => ({ ...p, _title: b.summary.title })));
    return { open: pick("open"), closed: pick("closed") };
  }, [d, only]);
  const head = html`<header class="view-head">
    <h1>Paper trading</h1>
    <p class="sub">Simulated portfolios, one per strategy, each starting with the same balance so they can be compared
      over the same period. Every fill is priced off the live order book with fees, positions are valued at the best bid,
      and they settle at Polymarket's final resolution. Nothing real is bought.</p>
  </header>`;

  if (!d) return html`<div class="view">${head}<p class="empty">${res.error ? res.error.message : "Loading…"}</p></div>`;
  if (!enabled && !d.summary.open_positions) {
    return html`<div class="view">${head}<${Empty} title="Paper trading is off">
      Enable <code>[paper]</code> in pmscan.toml, or start without <code>--no-paper</code>.<//></div>`;
  }
  const s = d.summary;
  const filterOptions = [{ value: "all", label: "All" }, ...books.map(([n, b]) => ({ value: n, label: b.summary.title }))];

  return html`<div class="view">${head}
    <section class="tiles wide">
      <${StatTile} label="Total equity" value=${usd(s.equity, 2)} sub=${`${signedUsd(s.pnl)} across ${books.length} strategies`} />
      ${books.map(([, b]) => html`<${StatTile} label=${b.summary.title} value=${signedUsd(b.summary.pnl)}
        sub=${`${pct(b.summary.return, 2)} · ${count(b.summary.open_bets)} open · ${count(b.summary.resolved)} resolved`} />`)}
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Strategies compared</h3>
        <p class="sub">A bet counts as resolved once all its positions have settled. For 90¢+ positions the win rate is
          shown against the rate the prices implied, which is the real test of that strategy.</p></div></div>
      <${DataTable} rows=${books.map(([, b]) => b.summary)} columns=${[
        { label: "Strategy", get: (b) => b.title },
        { label: "Equity", get: (b) => b.equity, format: (v) => usd(v, 2), num: true },
        { label: "P&L", get: (b) => b.pnl, format: signedUsd, num: true },
        { label: "Return", get: (b) => b.return, format: (v) => pct(v, 2), num: true },
        { label: "Realized", get: (b) => b.realized_pnl, format: signedUsd, num: true },
        { label: "Open bets", get: (b) => b.open_bets, format: count, num: true },
        { label: "Resolved", get: (b) => b.resolved, format: count, num: true },
        { label: "Win rate", render: (b) => winRate(b) },
        { label: "Stake", get: (b) => b.stake_usd, format: (v) => usd(v), num: true },
      ]} />
    </section>

    <${ArbProbes} books=${books} />

    <h2 class="section-title">Equity over time</h2>
    <div class="grid-2">
      ${books.map(([, b]) => {
        const curve = (b.equity || []).map((e) => ({ t: Date.parse(e.ts), v: e.equity }));
        return html`<${ChartCard} title=${b.summary.title}
          table=${{ columns: [
            { label: "Time", get: (p) => dateTime(p.t) },
            { label: "Equity", get: (p) => p.v, format: (v) => usd(v, 2), num: true },
          ], rows: [...curve].reverse() }}>
          <${LineChart} points=${curve} yFormat=${(v) => usd(v, 0)} label=${`${b.summary.title} equity`}
            valueLabel="equity" area=${false} zero=${false} height=${170}
            emptyText="The curve starts after the first scan." />
        <//>`;
      })}
    </div>

    <div class="filters">
      <${Segmented} label="Strategy" options=${filterOptions} value=${only}
        onChange=${(v) => (setOnly(v), storage.set("paper.strategy", v))} />
    </div>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Open positions</h3>
        <p class="sub">${count(rows.open.length)} held · valued at the current best bid</p></div></div>
      <${SortTable} rows=${rows.open} rowKey=${(p) => p.basket + p.token_id} initialSort=${{ key: "opened", dir: -1 }}
        empty="No open positions."
        columns=${[
          { key: "market", label: "Market", text: true, get: (p) => p.market,
            render: (p) => html`<a href=${href("markets", { token: p.token_id })}>${truncate(p.market, 64)}</a>
              ${p.bet && html`<div class="sub">${truncate(p.bet, 60)}</div>`}` },
          { key: "strategy", label: "Strategy", text: true, get: (p) => p._title },
          { key: "outcome", label: "Buy", text: true, get: (p) => p.outcome },
          { key: "entry", label: "Entry", num: true, get: (p) => p.avg_price, render: (p) => cents(p.avg_price) },
          { key: "mark", label: "Now", num: true, get: (p) => p.mark, render: (p) => cents(p.mark) },
          { key: "cost", label: "Cost", num: true, get: (p) => p.cost, render: (p) => usd(p.cost, 2) },
          { key: "pnl", label: "Unrealized", num: true, get: (p) => p.shares * (p.mark ?? p.avg_price) - p.cost,
            render: (p) => signedUsd(p.shares * (p.mark ?? p.avg_price) - p.cost) },
          { key: "ends", label: "Ends", num: true, get: (p) => (p.end_date ? Date.parse(p.end_date) : 0), render: (p) => untilEnd(p.end_date) },
          { key: "opened", label: "Opened", num: true, get: (p) => Date.parse(p.opened_at), render: (p) => dateTime(p.opened_at) },
        ]} />
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Resolved positions</h3>
        <p class="sub">Settled at Polymarket's final price, newest first. In an arbitrage basket most legs expire
          worthless by design; the basket's total is what counts.</p></div></div>
      <${SortTable} rows=${rows.closed} rowKey=${(p) => p.basket + p.token_id + p.closed_at} initialSort=${{ key: "closed", dir: -1 }}
        empty="Nothing has resolved yet. Short-dated sports and crypto markets usually settle within a day."
        columns=${[
          { key: "market", label: "Market", text: true, get: (p) => p.market,
            render: (p) => html`<${ExtLink} href=${p.url}>${truncate(p.market, 64)}<//>` },
          { key: "strategy", label: "Strategy", text: true, get: (p) => p._title },
          { key: "outcome", label: "Bought", text: true, get: (p) => p.outcome },
          { key: "entry", label: "Entry", num: true, get: (p) => p.avg_price, render: (p) => cents(p.avg_price) },
          { key: "result", label: "Result", text: true, get: (p) => ((p.payout || 0) > p.cost ? "won" : "lost"),
            render: (p) => ((p.payout || 0) > p.cost
              ? html`<${StatusChip} status="good">Paid out<//>` : html`<${StatusChip} status="critical">Expired worthless<//>`) },
          { key: "cost", label: "Cost", num: true, get: (p) => p.cost, render: (p) => usd(p.cost, 2) },
          { key: "payout", label: "Payout", num: true, get: (p) => p.payout, render: (p) => usd(p.payout, 2) },
          { key: "pnl", label: "P&L", num: true, get: (p) => (p.payout || 0) - p.cost, render: (p) => signedUsd((p.payout || 0) - p.cost) },
          { key: "closed", label: "Resolved", num: true, get: (p) => Date.parse(p.closed_at), render: (p) => dateTime(p.closed_at) },
        ]} />
    </section>
  </div>`;
}

// After each arbitrage entry the paper trader re-checks the same basket on fresh books a few seconds
// later: would a real order, which lands later than the paper fill, still have got it?
function ArbProbes({ books }) {
  const probed = books.filter(([, b]) => b.probes && b.probes.baskets);
  if (!probed.length) return null;
  const rows = probed.flatMap(([, b]) => b.probes.delays.map((d) => ({ ...d, title: b.summary.title, baskets: b.probes.baskets })));
  const recent = probed.flatMap(([, b]) => b.probes.recent.map((r) => ({ ...r, strategy: b.summary.title })))
    .sort((a, b) => (a.ts < b.ts ? 1 : -1)).slice(0, 20);
  const delays = [...new Set(recent.flatMap((r) => r.checks.map((c) => c.delay_s)))].sort((a, b) => a - b);
  const checkText = (c) => (!c ? "–" : c.ok == null ? "unknown" : c.ok ? `${pct(c.share, 0)} · ${signedUsd(c.profit)}` : "gone");
  return html`<section class="card">
    <div class="card-head"><div class="card-titles"><h3>Would a slower order still fill?</h3>
      <p class="sub">A paper arbitrage fills on order books read a fraction of a second after the edge appeared. A real
        order lands later and races other traders. So after each paper entry the same basket is re-checked on fresh
        books a few seconds later, with the live trader's own rules. If the edge is usually gone after a second,
        the paper profit isn't reachable for real.</p></div></div>
    <${DataTable} rows=${rows} columns=${[
      { label: "Strategy", get: (r) => r.title },
      { label: "Checked after", get: (r) => `${r.delay_s}s` },
      { label: "Still profitable", render: (r) => `${count(r.still_profitable)} of ${count(r.checked)}` },
      { label: "Size still available", get: (r) => r.avg_size_share, format: (v) => pct(v, 0), num: true },
      { label: "Profit kept", get: (r) => r.profit_kept, format: (v) => pct(v, 0), num: true },
    ]} />
    <h4 class="sub-head">Latest baskets</h4>
    <${DataTable} rows=${recent} columns=${[
      { label: "Bought", get: (r) => dateTime(r.ts) },
      { label: "Basket", get: (r) => truncate(r.title, 60) },
      { label: "Paper profit", get: (r) => r.entered.profit, format: signedUsd, num: true },
      ...delays.map((d) => ({ label: `After ${d}s`, render: (r) => checkText(r.checks.find((c) => c.delay_s === d)) })),
    ]} />
  </section>`;
}
