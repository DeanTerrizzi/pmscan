// Shared helpers: rendering, formatting, data fetching, routing.
// Relative paths, not an import map: WebKit before Safari 16.4 (e.g. macOS 12's) has no import maps.
import { h, render } from "../vendor/preact.module.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "../vendor/hooks.module.js";
import htm from "../vendor/htm.module.js";

export const html = htm.bind(h);
export { render, useCallback, useEffect, useMemo, useRef, useState };

// ---------- formatting ----------
const nf = (d) => ({ minimumFractionDigits: d, maximumFractionDigits: d });

export const usd = (x, d = 0) =>
  x == null || !isFinite(x) ? "–" : `${x < 0 ? "−" : ""}$${Math.abs(x).toLocaleString(undefined, nf(d))}`;

export const pct = (x, d = 1) => {
  if (x == null || !isFinite(x)) return "–";
  if (Math.abs(x) >= 100) return `${Math.round(x * 100).toLocaleString()}%`; // 10,000%+ is noise
  return `${(x * 100).toFixed(d)}%`;
};

export const signedPct = (x, d = 1) => (x == null ? "–" : `${x > 0 ? "+" : ""}${pct(x, d)}`);

export const cents = (p) => (p == null ? "–" : `${(p * 100).toFixed(p < 0.1 ? 2 : 1)}¢`);

export const compact = (n) =>
  n == null ? "–" : new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);

export const count = (n) => (n == null ? "–" : n.toLocaleString());

export const days = (d) => {
  if (d == null) return "–";
  if (d < 0) return `ended ${(-d).toFixed(1)}d ago`;
  if (d < 1) return `${(d * 24).toFixed(1)}h`;
  return `${d.toFixed(1)}d`;
};

export const clock = (ts) => new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
export const dateTime = (ts) =>
  new Date(ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

export const truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");

// ---------- data ----------
export async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v != null && v !== "") url.searchParams.set(k, v);
  const res = await fetch(url, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

// Poll an endpoint. Keeps the previous data while refetching (no flash, no layout jump).
export function usePoll(path, params = {}, intervalMs = 0) {
  const key = path ? path + JSON.stringify(params) : null;
  const [state, setState] = useState({ data: null, error: null, loading: !!path });
  useEffect(() => {
    if (!key) return undefined;
    let alive = true;
    const load = () => {
      setState((s) => ({ ...s, loading: true }));
      api(path, params)
        .then((data) => alive && setState({ data, error: null, loading: false }))
        .catch((error) => alive && setState((s) => ({ ...s, error, loading: false })));
    };
    load();
    const timer = intervalMs ? setInterval(load, intervalMs) : null;
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [key, intervalMs]);
  return state;
}

// ---------- routing: #/view?key=value ----------
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, query = ""] = raw.split("?");
  return { name: name || "overview", params: Object.fromEntries(new URLSearchParams(query)) };
}

export function useRoute() {
  const [route, setRoute] = useState(parseHash);
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

export const href = (name, params = {}) => {
  const q = new URLSearchParams(params).toString();
  return `#/${name}${q ? "?" + q : ""}`;
};

// ---------- small shared bits ----------
export function strategyMap(result) {
  const out = {};
  for (const s of (result && result.strategies) || []) out[s.name] = s.opportunities;
  return out;
}

export const storage = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* private mode etc. */
    }
  },
};

export function useDebounced(value, ms) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}
