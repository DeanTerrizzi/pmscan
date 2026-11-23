import { count, dateTime, html, pct, storage, truncate, usd, useState, usePoll } from "../lib.js";
import { StatTile } from "../charts.js";
import { Empty, ExtLink, Segmented, SortTable, StatusChip } from "../components.js";

const PERIOD = { early: "Pre-game", day_of: "Day of game", live: "Live", daily: "Daily", daily_event: "Daily" };
const STATUS = {
  quoting: ["good", "Quoting"],
  filled: ["warning", "Filled, holding"],
  settled: ["neutral", "Settled"],
  ended: ["muted", "Period ended"],
};
const money = (x, d = 2) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}$${Math.abs(x).toFixed(d)}`);
const ts = (s) => (s ? dateTime(s * 1000) : "–");

async function post(path) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Pmscan": "1" }, body: "{}" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 409) throw new Error(body.error || `${res.status}`);
}

function quoteText(e) {
  const q = e.quote;
  const yes = e.outcome ? `YES (${truncate(e.outcome, 40)})` : "YES";
  return `${q.side === "bid" ? "Buy" : "Sell"} ${yes} at ${(q.price * 100).toFixed(1)}¢`;
}

function title(row) {
  const t = truncate(row.title, 90);
  return row.url ? html`<${ExtLink} href=${row.url}>${t}<//>` : t;
}

const scanColumns = [
  { key: "title", label: "Market", text: true, get: (e) => e.title,
    render: (e) => html`<div>${title(e)}</div><div class="sub">${e.outcome ? `${truncate(e.outcome, 50)} · ` : ""}${e.program}</div>` },
  { key: "period", label: "Period", text: true, get: (e) => (e.paying_now ? 0 : 1),
    render: (e) => html`<div>${PERIOD[e.period] || e.period}</div><div class="sub">${e.paying_now ? `until ${ts(e.window_end)}` : `starts ${ts(e.window_start)}`}</div>` },
  { key: "pool", label: "This market's pool", num: true, get: (e) => e.market_pool,
    render: (e) => html`<div>${usd(e.market_pool, e.market_pool < 10 ? 2 : 0)}</div><div class="sub">${usd(e.pool)} ÷ ${count(e.program_markets)} markets</div>` },
  { key: "quote", label: "Quote", text: true, get: (e) => e.quote.price,
    render: (e) => html`<div>${quoteText(e)}</div><div class="sub">${count(Math.round(e.quote.size))} contracts${e.quote.ticks_behind ? ` · ${e.quote.ticks_behind} tick${e.quote.ticks_behind > 1 ? "s" : ""} back` : " · at the best price"}</div>` },
  { key: "share", label: "Share", num: true, get: (e) => e.quote.share, render: (e) => pct(e.quote.share, 0) },
  { key: "per_hour", label: "Per hour", num: true, get: (e) => e.per_hour, render: (e) => usd(e.per_hour, 2) },
  { key: "per_day", label: "Per day per $100", num: true, get: (e) => e.per_day_per_100, render: (e) => usd(e.per_day_per_100, 2) },
  { key: "risk", label: "At risk", num: true, get: (e) => e.quote.capital,
    render: (e) => html`<div>${usd(e.quote.capital)}</div><div class="sub">${e.quote.from_mid == null ? "" : `${(e.quote.from_mid * 100).toFixed(1)}¢ from mid`}</div>` },
];

const paperColumns = [
  { key: "title", label: "Market", text: true, get: (q) => q.title,
    render: (q) => html`<div>${title(q)}</div><div class="sub">${q.program} · ${PERIOD[q.period] || q.period}</div>` },
  { key: "status", label: "Status", text: true, get: (q) => q.status,
    render: (q) => {
      const [s, label] = STATUS[q.status] || ["neutral", q.status];
      return html`<${StatusChip} status=${s}>${label}<//>`;
    } },
  { key: "quote", label: "Quote", text: true, get: (q) => q.price,
    render: (q) => html`<div>${q.side === "bid" ? "Buy" : "Sell"} YES at ${(q.price * 100).toFixed(1)}¢</div>
      <div class="sub">${q.fill_price != null ? `filled ${ts(q.filled_at)}` : `since ${ts(q.placed_at)}`}</div>` },
  { key: "paid", label: "Time scoring", num: true, get: (q) => q.seconds_paid,
    render: (q) => html`<div>${(q.seconds_paid / 3600).toFixed(1)}h</div><div class="sub">of ${(q.seconds / 3600).toFixed(1)}h resting</div>` },
  { key: "accrued", label: "Rewards", num: true, get: (q) => q.accrued, render: (q) => usd(q.accrued, 2) },
  { key: "fill", label: "Fill P&L", num: true, get: (q) => (q.fill_price == null ? null : q.pnl - q.accrued),
    render: (q) => (q.fill_price == null ? html`<span class="muted">–</span>` : money(q.pnl - q.accrued)) },
  { key: "pnl", label: "Total", num: true, get: (q) => q.pnl, render: (q) => money(q.pnl) },
];

export function Rewards() {
  const [scope, setScope] = useState(() => storage.get("rewards.scope", "now"));
  const [kick, setKick] = useState(0);
  const [actionError, setActionError] = useState(null);
  const res = usePoll("/api/rewards", { kick }, 10000);
  const d = res.data;

  const head = html`<header class="view-head">
    <h1>Liquidity rewards</h1>
    <p class="sub">Polymarket US pays reward pools for keeping limit orders on the book, filled or not. This ranks what a
      small resting quote would earn on each rewarded market right now, from the live order books, and paper-tests the best
      ones: rewards earned against what fills would have cost.</p>
  </header>`;
  if (!d) return html`<div class="view">${head}<p class="empty">${res.error ? res.error.message : "Loading…"}</p></div>`;
  if (!d.has_key) return html`<div class="view">${head}<${Empty} title="Connect your Polymarket US API key">
    The reward programs come from Polymarket US's incentives API, which needs your key. Connect it in Settings →
    Account (or set POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY). The key is only used to read; nothing is traded.<//></div>`;

  const act = async (path) => {
    setActionError(null);
    try {
      await post(path);
      setKick((k) => k + 1);
    } catch (e) {
      setActionError(e.message);
    }
  };
  const r = d.result;
  const p = d.paper || {};
  const s = d.settings || {};
  const bar = html`<section class="card run-bar">
    <div style="flex:1;min-width:220px">
      ${d.scanning ? html`<strong>Scanning…</strong> <span class="sub">${d.message || ""}</span>`
        : r ? html`<span>Last scan ${ts(r.generated_at)} · ${count(r.books_read)} order books · ${count(r.programs)} markets in reward programs</span>`
        : html`<span>No scan yet. The first one starts shortly after the app opens and takes about a minute.</span>`}
      ${d.error && html`<div class="sub" style="color:var(--critical)">Last scan failed: ${d.error}</div>`}
      ${actionError && html`<div class="sub" style="color:var(--critical)">${actionError}</div>`}
    </div>
    <button class="ghost" disabled=${d.scanning} onClick=${() => act("/api/rewards/scan")}>Scan now</button>
  </section>`;

  const rows = r ? r.estimates.filter((e) => e.quote && (scope === "all" || e.paying_now)) : [];
  const earned = (r && r.earnings) || {};
  return html`<div class="view">${head}${bar}
    <section class="tiles wide">
      <${StatTile} label="Paper rewards (estimated)" value=${usd(p.rewards, 2)}
        sub=${`${usd(p.payable, 2)} clears the $1 minimum payout`} />
      <${StatTile} label="Paper fills" value=${money(p.fill_pnl)} sub=${`${count(p.fills || 0)} of ${count(p.quotes || 0)} quotes filled`} />
      <${StatTile} label="Paper total" value=${money(p.pnl)}
        sub=${p.paid_fraction == null ? "no quote time yet" : `scored ${pct(p.paid_fraction, 0)} of ${p.hours_quoted}h quoted`} />
      <${StatTile} label="Your real rewards" value=${usd((earned.paid || 0) + (earned.pending || 0), 2)}
        sub=${`${usd(earned.paid || 0, 2)} paid · ${usd(earned.pending || 0, 2)} pending`} />
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>Paper test</h3>
        <p class="sub">Up to ${s.max_quotes} quotes of ${usd(s.budget_usd)} at a time, one per program, on the best markets
          paying now${s.paper_live ? "" : " (not during live games, where a resting quote is picked off as soon as the price jumps)"}. Each follows the best price until it fills; a filled quote is held until its market settles. Fills are
          judged pessimistically (any trade at or through the price fills it in full). Started ${ts(p.started_at)}.</p></div>
        ${p.quotes ? html`<button class="ghost small" onClick=${() => confirm("Archive the rewards paper test and start again?") && act("/api/rewards/reset")}>Reset</button>` : null}
      </div>
      <${SortTable} columns=${paperColumns} rows=${p.rows || []} rowKey=${(q) => q.id}
        initialSort=${{ key: "status", dir: 1 }} empty="No paper quotes yet: they open after the next scan finds a paying market." />
    </section>

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>What a ${usd(s.budget_usd)} quote would earn</h3>
        <p class="sub">The best quote on each rewarded market given its book right now: price, your share of that side's
          score, and pay while the period runs. A program's pool is shared by all its markets.</p></div>
        <${Segmented} label="Which periods" value=${scope} onChange=${(v) => (setScope(v), storage.set("rewards.scope", v))}
          options=${[{ value: "now", label: "Paying now" }, { value: "all", label: "Next 24 hours" }]} />
      </div>
      ${r ? html`<${SortTable} columns=${scanColumns} rows=${rows} rowKey=${(e) => `${e.slug}:${e.period}`}
        initialSort=${{ key: "per_hour", dir: -1 }} limit=${100} empty="No quote earns anything on the books read." />`
        : html`<p class="empty">Waiting for the first scan.</p>`}
    </section>

    ${d.messages && d.messages.length ? html`<section class="card">
      <div class="card-head"><div class="card-titles"><h3>Recent activity</h3></div></div>
      <ul class="notes">${[...d.messages].reverse().map((m) => html`<li>${m}</li>`)}</ul>
    </section>` : null}

    <section class="card">
      <div class="card-head"><div class="card-titles"><h3>How to read this</h3></div></div>
      <ul class="notes">
        <li>Each second Polymarket US scores every resting order as discount factor<sup>ticks from the best price</sup> ×
          size, counting from the best price out only until the side holds the program's target size. Your pay is your share
          of that score. A second pays nobody if a side never reaches the target, or (with a max spread) if the book is too wide.</li>
        <li>${(r && r.notes) ? r.notes.join(" ") : ""}</li>
        <li>Rewards are calculated within 5 business days after a period ends and paid 2 business days later; amounts under
          $1 aren't paid. Nothing here places orders.</li>
      </ul>
    </section>
  </div>`;
}
