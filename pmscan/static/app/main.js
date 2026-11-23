import { href, html, render, storage, strategyMap, useEffect, usePoll, useRoute, useState } from "./lib.js";
import { Overview } from "./views/overview.js";
import { Arbitrage } from "./views/arbitrage.js";
import { Ladders } from "./views/ladders.js";
import { Sportsbook } from "./views/sportsbook.js";
import { HighProb } from "./views/highprob.js";
import { NearCertain } from "./views/nearcertain.js";
import { Trading } from "./views/trading.js";
import { Paper } from "./views/paper.js";
import { Markets } from "./views/markets.js";
import { Backtest } from "./views/backtest.js";
import { Rewards } from "./views/rewards.js";

const VIEWS = [
  { name: "overview", label: "Overview", component: Overview },
  { name: "arbitrage", label: "Neg-risk", component: Arbitrage, count: "neg_risk_arb" },
  { name: "ladders", label: "Ladders", component: Ladders, count: "ladder_arb" },
  { name: "sportsbook", label: "Sportsbook", component: Sportsbook, count: "sportsbook_value" },
  { name: "high-prob", label: "High-probability", component: HighProb, count: "high_prob" },
  { name: "near-certain", label: "Near-certain", component: NearCertain, count: "near_certain" },
  { name: "paper", label: "Paper", component: Paper },
  { name: "rewards", label: "Rewards", component: Rewards },
  { name: "backtest", label: "Backtest", component: Backtest },
  { name: "trading", label: "Trading", component: Trading },
  { name: "markets", label: "Markets", component: Markets },
];

function ScanStatus({ latest }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  if (latest.error && !latest.data) return html`<span class="pill error">server unreachable</span>`;
  const st = latest.data && latest.data.status;
  if (!st) return html`<span class="pill">connecting…</span>`;
  if (st.scanning) return html`<span class="pill active">scanning…</span>`;
  if (st.error) return html`<span class="pill error" title=${st.error}>last scan failed</span>`;
  const fetchedAt = latest.fetchedAt || Date.now();
  const left = st.next_scan_in_s == null ? null : Math.max(0, Math.round(st.next_scan_in_s - (Date.now() - fetchedAt) / 1000));
  return html`<span class="pill">${left == null ? "idle" : `next scan in ${left}s`}</span>`;
}

// Inside the Mac app (?embedded=1) the native sidebar and toolbar replace the web header,
// the theme follows macOS, and route changes are reported to the app's sidebar.
export const EMBEDDED = new URLSearchParams(location.search).has("embedded");
if (EMBEDDED) document.documentElement.classList.add("embedded");
const tellApp = (msg) => {
  try {
    window.webkit.messageHandlers.pmscan.postMessage(msg);
  } catch {
    /* not inside the app */
  }
};

const THEMES = ["system", "light", "dark"];
function ThemeToggle() {
  const [theme, setTheme] = useState(() => storage.get("theme", "system"));
  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    storage.set("theme", theme);
  }, [theme]);
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  return html`<button class="ghost small" onClick=${() => setTheme(next)} title="Switch theme">Theme: ${theme}</button>`;
}

function useLatest() {
  const latest = usePoll("/api/latest", {}, 10000);
  const [fetchedAt, setFetchedAt] = useState(Date.now());
  useEffect(() => setFetchedAt(Date.now()), [latest.data]);
  return { ...latest, fetchedAt };
}

function App() {
  const route = useRoute();
  const latest = useLatest();
  const data = latest.data;
  const opps = strategyMap(data && data.result);
  const view = VIEWS.find((v) => v.name === route.name) || VIEWS[0];
  const nav = (name, params) => (location.hash = href(name, params));
  useEffect(() => {
    document.title = `${view.label} · pmscan`;
    window.scrollTo(0, 0);
    if (EMBEDDED) tellApp({ type: "route", name: view.name });
  }, [view.name]);
  const View = view.component;

  return html`
    ${!EMBEDDED && html`<header class="topbar">
      <div class="brand"><span class="logo" aria-hidden="true"></span>pmscan</div>
      <nav aria-label="Views">${VIEWS.map((v) => {
        const n = v.name === "paper" ? (data && data.paper ? data.paper.open_positions : null)
          : v.count && opps[v.count] ? opps[v.count].length : null;
        return html`<a href=${href(v.name)} aria-current=${v.name === view.name ? "page" : null}>
          ${v.label}${n != null && html`<span class="badge">${n.toLocaleString()}</span>`}</a>`;
      })}</nav>
      <div class="top-right"><${ScanStatus} latest=${latest} /><${ThemeToggle} /></div>
    </header>`}
    ${data && data.trading && data.trading.halted && view.name !== "trading" && html`<div class="banner critical slim" role="alert">
      Trading is halted after a partial fill. <a href=${href("trading")}>See Trading</a></div>`}
    <main class=${latest.loading && data ? "refreshing-soft" : ""}>
      ${data || view.name === "markets"
        ? html`<${View} latest=${data} route=${route} nav=${nav} />`
        : html`<div class="view"><p class="empty">${latest.error ? `Can't reach the pmscan server: ${latest.error.message}` : "Loading…"}</p></div>`}
    </main>
    <footer>${data && data.status && data.status.venue === "us"
      ? "Data from Polymarket US's public market API (polymarket.us)."
      : "Data from Polymarket's public Gamma and CLOB APIs (polymarket.com)."} Prices move constantly; check the order book before you trade.</footer>`;
}

render(html`<${App} />`, document.getElementById("app"));
