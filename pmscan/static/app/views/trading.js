import { count, dateTime, href, html, truncate, useMemo, usePoll, usd } from "../lib.js";
import { ChartCard, DataTable, LineChart, StatTile } from "../charts.js";
import { Empty, ExtLink, StatusChip } from "../components.js";

const OUTCOME = {
  filled: ["good", "Filled"],
  partial: ["critical", "Partial fill"],
  not_filled: ["muted", "Not filled"],
  dry_run: ["neutral", "Dry run"],
};
const outcomeChip = (o) => {
  const [status, label] = OUTCOME[o] || ["neutral", o];
  return html`<${StatusChip} status=${status}>${label}<//>`;
};

function AccountCard() {
  const res = usePoll("/api/account", {}, 30000);
  const a = res.data;
  if (!a || !a.connected) return null;
  return html`<section class="tiles wide">
    <${StatTile} label="Account wallet" value=${`${a.wallet.slice(0, 6)}…${a.wallet.slice(-4)}`} sub=${a.wallet_type} />
    <${StatTile} label="Balance" value=${usd(a.collateral_usd, 2)} sub="pUSD available to trade" />
  </section>`;
}

export function Trading({ latest }) {
  const trades = usePoll("/api/trades", { limit: 5000 }, 15000);
  const t = (latest && latest.trading) || { mode: "off" };
  const entries = trades.data || [];
  const today = new Date().toISOString().slice(0, 10);

  const s = useMemo(() => {
    const live = entries.filter((e) => e.mode === "live");
    return {
      spentToday: live.filter((e) => (e.ts || "").slice(0, 10) === today).reduce((a, e) => a + (e.spent || 0), 0),
      filled: live.filter((e) => e.outcome === "filled"),
      partial: live.filter((e) => e.outcome === "partial").length,
      dry: entries.filter((e) => e.mode === "dry_run"),
    };
  }, [entries, today]);
  const curveSource = s.filled.length ? s.filled : s.dry;
  const curve = useMemo(() => {
    let c = 0;
    return curveSource.map((e) => ({ t: Date.parse(e.ts), v: (c += e.edge || 0) }));
  }, [curveSource]);

  const mode = t.mode === "off" ? "Off" : t.mode === "live" ? "Live" : "Dry run";
  return html`<div class="view">
    <header class="view-head">
      <h1>Trading</h1>
      <p class="sub">What the auto-trader did or would do. Start the server with <code>--trade</code> for a dry run, or <code>--trade --live</code> to place orders (see the README for setup and limits).</p>
    </header>

    ${t.mode === "live" && html`<${AccountCard} />`}
    ${t.halted && html`<div class="banner critical" role="alert"><${StatusChip} status="critical">Trading halted<//>
      <span>${t.halted}</span></div>`}

    <section class="tiles wide">
      <${StatTile} label="Mode" value=${mode}
        sub=${t.mode === "off" ? "not running in this server" : `${(t.strategies || []).join(", ")} · max ${usd(t.max_usd_per_trade)}/trade`} />
      <${StatTile} label="Spent today (live)" value=${usd(s.spentToday, 2)} sub=${t.max_usd_per_day ? `of ${usd(t.max_usd_per_day)} daily cap` : ""} />
      <${StatTile} label="Live trades filled" value=${count(s.filled.length)}
        sub=${`${usd(s.filled.reduce((a, e) => a + (e.edge || 0), 0), 2)} edge locked in`} />
      <${StatTile} label="Partial fills" value=${count(s.partial)} sub=${s.partial ? "each one halted trading" : "none"} />
      <${StatTile} label="Dry-run trades logged" value=${count(s.dry.length)}
        sub=${`${usd(s.dry.reduce((a, e) => a + (e.edge || 0), 0), 2)} edge if they had been real`} />
    </section>

    ${entries.length
      ? html`
        <${ChartCard} title=${s.filled.length ? "Cumulative edge locked in (live fills)" : "Cumulative edge of dry-run trades"}
          subtitle="Guaranteed payout minus cost, summed over time. For arbitrage this is profit fixed at entry."
          table=${{ columns: [
            { label: "Time", get: (p) => dateTime(p.t) },
            { label: "Cumulative edge", get: (p) => p.v, format: (v) => usd(v, 2), num: true },
          ], rows: [...curve].reverse() }}>
          <${LineChart} points=${curve} yFormat=${(v) => usd(v, 2)} label="Cumulative trading edge" valueLabel="cumulative edge" />
        <//>
        <section class="card">
          <div class="card-head"><div class="card-titles"><h3>Trade log</h3>
            <p class="sub">${count(entries.length)} attempts, newest first (data/trades.jsonl)</p></div></div>
          <${DataTable} rows=${[...entries].reverse()} max=${300} columns=${[
            { label: "Time", get: (e) => dateTime(e.ts) },
            { label: "Outcome", render: (e) => outcomeChip(e.outcome) },
            { label: "Opportunity", render: (e) => html`<${ExtLink} href=${e.url}>${truncate(e.title, 70)}<//>` },
            { label: "Strategy", get: (e) => e.strategy },
            { label: "Cost", get: (e) => e.cost, format: (v) => usd(v, 2), num: true },
            { label: "Edge", get: (e) => e.edge, format: (v) => usd(v, 2), num: true },
            { label: "Legs", render: (e) => html`<details><summary>${e.legs.length}</summary><ul class="legs">${e.legs.map(
              (l) => html`<li><a href=${href("markets", { token: l.token_id })}>${l.outcome} · ${truncate(l.market, 40)}</a>:
                $${l.amount} up to ${l.max_price}</li>`)}</ul></details>` },
          ]} />
        </section>`
      : html`<${Empty} title="No trades yet">
          ${t.mode === "off"
            ? html`This server isn't running the trader. Restart with <code>pmscan serve --trade</code> to log what it would buy.`
            : "The trader is running; nothing has qualified yet. Skipped opportunities and their reasons appear in Activity on the Overview."}<//>`}
  </div>`;
}
