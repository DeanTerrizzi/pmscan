import { compact, count, dateTime, href, html, storage, strategyMap, useMemo, usePoll, useState, usd } from "../lib.js";
import { ChartCard, LineChart, StatTile } from "../charts.js";
import { Segmented } from "../components.js";

const RANGES = [
  { value: "1h", label: "1 hour", ms: 3600e3 },
  { value: "6h", label: "6 hours", ms: 6 * 3600e3 },
  { value: "24h", label: "24 hours", ms: 864e5 },
  { value: "7d", label: "7 days", ms: 7 * 864e5 },
  { value: "all", label: "All", ms: Infinity },
];

const STRATEGIES = [
  { name: "neg_risk_arb", title: "Neg-risk arbitrage", view: "arbitrage" },
  { name: "ladder_arb", title: "Ladder arbitrage", view: "ladders" },
  { name: "sportsbook_value", title: "Sportsbook value bets", view: "sportsbook" },
  { name: "high_prob", title: "High-probability positions", view: "high-prob" },
  { name: "near_certain", title: "Near-certain bets", view: "near-certain" },
];

const arbProfit = (s) => ((s.neg_risk_arb && s.neg_risk_arb.profit) || 0) + ((s.ladder_arb && s.ladder_arb.profit) || 0);

export function Overview({ latest }) {
  const [range, setRange] = useState(() => storage.get("overview.range", "24h"));
  const scans = usePoll("/api/scans", { limit: 20000 }, 60000);
  const result = latest && latest.result;
  const opps = strategyMap(result);
  const stats = (result && result.stats) || {};

  const series = useMemo(() => {
    const ms = (RANGES.find((r) => r.value === range) || RANGES[2]).ms;
    const cutoff = Date.now() - ms;
    return (scans.data || []).map((s) => ({ ...s, t: Date.parse(s.ts) })).filter((s) => s.t >= cutoff);
  }, [scans.data, range]);
  const recent = (scans.data || []).slice(-24);

  const arbs = [...(opps.neg_risk_arb || []), ...(opps.ladder_arb || [])];
  const total = arbs.reduce((a, o) => a + o.profit, 0);
  const sportsInactive = (stats.sportsbook_status || "").startsWith("inactive");

  const tile = (s) => {
    const list = opps[s.name];
    const sub = s.name === "sportsbook_value" && sportsInactive
      ? "inactive: add an Odds API key"
      : list ? html`<a href=${href(s.view)}>View list →</a>` : "not enabled";
    return html`<${StatTile} label=${s.title} value=${list ? count(list.length) : "–"} sub=${sub}
      spark=${recent.map((r) => (r[s.name] ? r[s.name].count : null))} />`;
  };

  return html`<div class="view">
    <section class="hero-row">
      <div class="card hero">
        <div class="hero-label">Guaranteed arbitrage profit available now</div>
        <div class="hero-value">${usd(total, 2)}</div>
        <div class="sub">${count(arbs.length)} basket${arbs.length === 1 ? "" : "s"} (neg-risk and ladder), sized
          against live order books after fees${arbs.length ? html` · capital needed ${usd(arbs.reduce((a, o) => a + o.cost, 0))}` : ""}</div>
      </div>
      <div class="tiles">
        ${STRATEGIES.map(tile)}
        <${StatTile} label="Markets scanned" value=${compact(stats.tradeable_markets)}
          sub=${`${count(stats.events)} events · ${count(stats.books_fetched)} books checked`} />
        <${StatTile} label="Scan time" value=${result ? `${Math.round(result.duration_s)}s` : "–"}
          sub=${result ? `last scan ${dateTime(result.started_at)}` : "waiting for first scan"}
          spark=${recent.map((r) => r.duration_s)} />
      </div>
    </section>

    <div class="filters">
      <${Segmented} label="Time range" options=${RANGES} value=${range}
        onChange=${(v) => (setRange(v), storage.set("overview.range", v))} />
      <span class="sub">${count(series.length)} scans in range</span>
    </div>

    <${ChartCard} title="Arbitrage profit available"
      subtitle="Guaranteed profit across every arbitrage basket found, per scan"
      table=${{
        columns: [
          { label: "Scan", get: (s) => dateTime(s.t) },
          { label: "Profit available", get: arbProfit, format: (v) => usd(v, 2), num: true },
          { label: "Neg-risk", get: (s) => (s.neg_risk_arb ? s.neg_risk_arb.count : "–"), num: true },
          { label: "Ladder", get: (s) => (s.ladder_arb ? s.ladder_arb.count : "–"), num: true },
        ],
        rows: [...series].reverse(),
      }}>
      <${LineChart} points=${series.map((s) => ({ t: s.t, v: arbProfit(s) }))} yFormat=${(v) => usd(v, v < 10 ? 2 : 0)}
        label="Arbitrage profit available over time" valueLabel="guaranteed profit" height=${220}
        emptyText=${scans.loading ? "Loading…" : "No scans in this range yet. Leave pmscan serve running to build history."} />
    <//>

    <h2 class="section-title">Opportunities found per scan</h2>
    <div class="grid-2">
      ${STRATEGIES.map((s) => html`<${ChartCard} title=${s.title}
        table=${{
          columns: [
            { label: "Scan", get: (r) => dateTime(r.t) },
            { label: "Found", get: (r) => (r[s.name] ? r[s.name].count : null), format: count, num: true },
          ],
          rows: [...series].reverse(),
        }}>
        <${LineChart} integer
          points=${s.name === "sportsbook_value" && sportsInactive ? [] : series.filter((r) => r[s.name]).map((r) => ({ t: r.t, v: r[s.name].count }))}
          yFormat=${(v) => count(Math.round(v))} label=${`${s.title} per scan`} valueLabel="found" height=${160}
          emptyText=${s.name === "sportsbook_value" && sportsInactive ? "Inactive: add an Odds API key." : "No scans in this range yet."} />
      <//>`)}
    </div>

    <${Activity} latest=${latest} />
  </div>`;
}

function feedStatus(f) {
  if (!f || f.enabled === false) return f && f.error ? `Live feed off (${f.error}).` : "Live feed off.";
  if (!f.connected) return f.error ? `Live feed reconnecting (${f.error}).` : "Live feed starting.";
  return `Live feed on: watching ${count(f.watching_markets)} markets in the ${count(f.watching_events)} events closest to `
    + `an arbitrage · ${count(f.updates)} price updates · ${count(f.found)} found between scans.`;
}

function Activity({ latest }) {
  const messages = ((latest && latest.status && latest.status.messages) || []).slice().reverse();
  const trading = (latest && latest.trading) || { mode: "off" };
  const feed = latest && latest.status && latest.status.fast;
  return html`<section class="card">
    <div class="card-head"><div class="card-titles"><h3>Activity</h3>
      <p class="sub">Alerts, trades, paper trades and skipped trades from this session · trading ${trading.mode === "off" ? "off" : trading.mode === "live" ? "live" : "dry run"}</p>
      <p class="sub">${feedStatus(feed)}</p></div></div>
    ${messages.length
      ? html`<ul class="activity">${messages.map((m) => html`<li>${m}</li>`)}</ul>`
      : html`<p class="empty">Nothing yet. Start the server with <code>--trade</code> to see what the trader would do.</p>`}
  </section>`;
}
