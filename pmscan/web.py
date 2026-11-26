"""Local dashboard server: rescans in a background thread, serves the app and a JSON API.

Routes
  /                     the app (pmscan/static/index.html)
  /static/...           app assets
  /api/latest           latest scan result, scanner status, trading status
  /api/scans            per-scan summaries (trend charts), ?limit=
  /api/history          arbitrage / value opportunities over time, ?limit=&strategy=
  /api/trades           the trade ledger, ?limit=
  /api/markets          search open markets from the latest scan, ?q=&limit=
  /api/market           one market by outcome token, ?token=
  /api/price-history    price history for a token, ?token=&interval=1h|6h|1d|1w|1m|max
  /api/book             current order book for a token, ?token=
  /api/paper            paper-trading portfolio, positions and equity curve
  /api/account          live account wallet and balance (when trading live)
  /api/backtest         the 90¢+ backtest on settled Polymarket US markets (POST /api/backtest/run starts one)
  /api/near-certain     how near-certain bets did in the backtest, by category and price
  /api/rewards          Polymarket US liquidity rewards: ranked quotes and the paper test
                        (POST /api/rewards/scan rescans now, POST /api/rewards/reset restarts the paper test)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .client import PolymarketClient
from .config import ScanConfig
from .fast import FastWatcher
from .models import Event, Market
from .paper import PaperTrader, paper_snapshot
from .scanner import Scanner, ScanResult
from .trading import AutoTrader, Ledger

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("pmscan").joinpath("static")))
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
    ".txt": "text/plain; charset=utf-8", ".ico": "image/x-icon", ".png": "image/png",
}
HISTORY_FIDELITY = {"1h": 1, "6h": 5, "1d": 10, "1w": 60, "1m": 240, "max": 1440}  # minutes per point


def market_json(m: Market) -> dict[str, Any]:
    return {
        "id": m.id, "question": m.question, "label": m.label, "event": m.event_title, "url": m.url,
        "outcomes": list(m.outcomes), "token_ids": list(m.token_ids), "bid": m.best_bid, "ask": m.best_ask,
        "liquidity": round(m.liquidity, 2), "volume_24h": round(m.volume_24h, 2),
        "end_date": m.end_date.isoformat() if m.end_date else None, "neg_risk": m.neg_risk,
        "fee_rate": m.fees.rate, "tradeable": m.tradeable,
    }


class MarketIndex:
    """Lookup and search over the markets of the latest scan."""

    def __init__(self, events: list[Event]) -> None:
        self.by_token: dict[str, Market] = {}
        self._search: list[tuple[str, Market]] = []
        for e in events:
            for m in e.markets:
                for t in m.token_ids:
                    self.by_token[t] = m
                if m.tradeable:
                    self._search.append((f"{m.question} {m.event_title} {m.group_title}".lower(), m))

    def search(self, query: str, limit: int = 50) -> list[Market]:
        words = query.lower().split()
        if not words:
            hits = [m for _, m in self._search]
        else:
            hits = [m for text, m in self._search if all(w in text for w in words)]
        hits.sort(key=lambda m: m.volume_24h, reverse=True)
        return hits[:limit]


class _State:
    def __init__(self, interval: float, trader: AutoTrader | None, fast: FastWatcher | None = None,
                 venue: str = "international") -> None:
        self.lock = threading.Lock()
        self.venue = venue
        self.interval = interval
        self.trader = trader
        self.fast = fast
        self.result: dict[str, Any] | None = None
        self.index: MarketIndex | None = None
        self.error: str | None = None
        self.scanning = False
        self.next_scan_at: float | None = None
        self.messages: list[str] = []  # alert / trade activity, newest last

    def _trading(self) -> dict[str, Any]:
        if self.trader is None:
            return {"mode": "off"}
        ledger = self.trader.ledger
        return {"mode": "live" if self.trader.live else "dry_run", "halted": ledger.halted(),
                "strategies": self.trader.config.strategies,
                "max_usd_per_trade": self.trader.config.max_usd_per_trade,
                "max_usd_per_day": self.trader.config.max_usd_per_day,
                "recent": ledger.entries()[-15:][::-1]}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "result": self.result,
                "status": {
                    "venue": self.venue,
                    "scanning": self.scanning,
                    "error": self.error,
                    "interval_s": self.interval,
                    "next_scan_in_s": None if self.next_scan_at is None else max(0.0, self.next_scan_at - time.time()),
                    "messages": self.messages[-20:],
                    "fast": dict(self.fast.stats) if self.fast else {"enabled": False},
                },
                "trading": self._trading(),
            }

    def merge_found(self, found: ScanResult, messages: list[str]) -> None:
        """Add opportunities the live feed found to the latest result, so every view shows them."""
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.messages.extend(f"{stamp} live feed: {m}" for m in messages)
            if not self.result:
                return
            by_name = {s["name"]: s for s in self.result.get("strategies", [])}
            for name, opps in found.opportunities.items():
                strategy = by_name.get(name)
                if strategy is None:
                    continue
                fresh = {o.key: {**o.to_dict(), "live": True, "found_at": found.started_at.isoformat()} for o in opps}
                kept = [d for d in strategy["opportunities"] if d.get("key") not in fresh]
                strategy["opportunities"] = sorted(kept + list(fresh.values()), key=lambda d: d.get("score", 0),
                                                   reverse=True)


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class BacktestRunner:
    """Runs the Polymarket US backtest in the background (it takes several minutes)."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir  # the US data folder; results go in <it>/backtest
        self.lock = threading.Lock()
        self.running = False
        self.progress: str | None = None
        self.error: str | None = None

    def start(self, per_group: int) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.error, self.progress = True, None, "Starting…"
        threading.Thread(target=self._run, args=(per_group,), name="pmscan-backtest", daemon=True).start()
        return True

    def _run(self, per_group: int) -> None:
        from .backtest import Backtest
        from .us import UsClient

        try:
            # A gentler request rate than the scanner's, so the two together stay under the API limit.
            Backtest(self.data_dir, UsClient(rate=6.0)).run(per_group, progress=self._progress)
        except Exception as exc:
            log.warning("backtest failed: %s", exc)
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.running, self.progress = False, None

    def _progress(self, message: str) -> None:
        self.progress = message

    def snapshot(self) -> dict[str, Any]:
        result = None
        path = self.data_dir / "backtest" / "result.json"
        if path.exists():
            try:
                result = json.loads(path.read_text())
            except json.JSONDecodeError:
                result = None
        cached = sum(1 for _ in open(self.data_dir / "backtest" / "markets.jsonl")) \
            if (self.data_dir / "backtest" / "markets.jsonl").exists() else 0
        return {"running": self.running, "progress": self.progress, "error": self.error, "result": result,
                "cached_markets": cached}


class Api:
    """JSON routes, separate from HTTP plumbing so they can be tested directly."""

    def __init__(self, state: _State, data_dir: Path, client: PolymarketClient,
                 config: ScanConfig | None = None) -> None:
        self.state = state
        self.data_dir = data_dir
        self.client = client
        self.config = config or ScanConfig()
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        us_dir = data_dir if getattr(client, "venue", "international") == "us" else data_dir / "us"
        self.us_dir = us_dir
        self.backtest = BacktestRunner(us_dir)
        from .rewards import RewardsService

        self.rewards = RewardsService(us_dir, self.config.rewards)

    def _cached(self, key: tuple, ttl: float, fetch: Callable[[], Any]) -> Any:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = fetch()
        with self._cache_lock:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[key] = (time.monotonic(), value)
        return value

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if path == "/api/backtest/run":
            per_group = max(50, min(int(body.get("per_group") or 1000), 5000))
            started = self.backtest.start(per_group)
            return (202 if started else 409), self.backtest.snapshot()
        if path == "/api/rewards/scan":
            if not self.rewards.has_key():
                return 400, {"error": "Connect your Polymarket US API key first."}
            started = self.rewards.scan_now()
            return (202 if started else 409), self.rewards_snapshot()
        if path == "/api/rewards/reset":
            self.rewards.reset_paper()
            return 200, self.rewards_snapshot()
        return 404, {"error": "not found"}

    def rewards_snapshot(self) -> dict[str, Any]:
        """The rewards scan and paper test, with links from the latest market scan where it knows them."""
        snap = self.rewards.snapshot()
        index = self.state.index
        if index is not None:
            rows = ((snap.get("result") or {}).get("estimates") or []) + (snap["paper"].get("rows") or [])
            for row in rows:
                market = index.by_token.get(row.get("slug", ""))
                if market is not None and not row.get("url"):
                    row["url"] = market.url
        return snap

    def handle(self, path: str, query: dict[str, str]) -> tuple[int, Any]:
        limit = max(1, min(int(query.get("limit", "0") or 0) or 1000, 20_000))
        if path == "/api/latest":
            snap = self.state.snapshot()
            paper = PaperTrader(self.config.paper, self.client, self.data_dir).summary()
            snap["paper"] = {"enabled": self.config.paper.enabled, "open_positions": paper["open_positions"],
                             "equity": paper["equity"], "pnl": paper["pnl"]}
            return 200, snap
        if path == "/api/paper":
            return 200, paper_snapshot(self.data_dir, self.config.paper)
        if path == "/api/backtest":
            return 200, self.backtest.snapshot()
        if path == "/api/rewards":
            return 200, self.rewards_snapshot()
        if path == "/api/near-certain":
            from .backtest import near_certain_record

            cfg = self.config.near_certain
            book = PaperTrader(self.config.paper, None, self.data_dir).books.get("near_certain")
            return 200, {"record": near_certain_record(self.us_dir), "paper": book.summary() if book else None,
                         "settings": {"min_price": cfg.min_price, "max_price": cfg.max_price, "max_days": cfg.max_days,
                                      "include_us": cfg.include_us, "stake_usd": self.config.paper.near_certain_stake_usd,
                                      "max_bets_per_event": self.config.paper.max_bets_per_event}}
        if path == "/api/account":
            trader = self.state.trader
            if trader is None or not trader.live:
                return 200, {"connected": False}
            summary = self._cached(("account",), 30, trader.exchange.account_summary)
            return 200, {"connected": True, **summary}
        if path == "/api/scans":
            return 200, _tail_jsonl(self.data_dir / "scans.jsonl", limit)
        if path == "/api/history":
            rows = _tail_jsonl(self.data_dir / "history.jsonl", 50_000)
            if query.get("strategy"):
                rows = [r for r in rows if r.get("strategy") == query["strategy"]]
            return 200, rows[-limit:]
        if path == "/api/trades":
            return 200, Ledger(self.data_dir).entries()[-limit:]
        if path in ("/api/markets", "/api/market"):
            index = self.state.index
            if index is None:
                return 503, {"error": "no scan yet"}
            if path == "/api/markets":
                return 200, [market_json(m) for m in index.search(query.get("q", ""), min(limit, 100))]
            m = index.by_token.get(query.get("token", ""))
            return (200, market_json(m)) if m else (404, {"error": "unknown token"})
        if path == "/api/price-history":
            token, interval = query.get("token", ""), query.get("interval", "1w")
            if not token or interval not in HISTORY_FIDELITY:
                return 400, {"error": "token and a valid interval are required"}
            history = self._cached(("hist", token, interval), 60,
                                   lambda: self.client.price_history(token, interval, HISTORY_FIDELITY[interval]))
            return 200, history
        if path == "/api/book":
            token = query.get("token", "")
            if not token:
                return 400, {"error": "token is required"}
            book = self._cached(("book", token), 10, lambda: self.client.get_books([token]).get(token))
            if book is None:
                return 404, {"error": "no order book"}
            return 200, {"token_id": token, "bids": [[l.price, l.size] for l in book.bids],
                         "asks": [[l.price, l.size] for l in book.asks]}
        return 404, {"error": "not found"}


OnResult = Callable[[ScanResult], list[str]]


def _scan_loop(scanner: Scanner, state: _State, data_dir: Path, stop: threading.Event,
               on_result: OnResult | None, fast: FastWatcher | None = None) -> None:
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        with state.lock:
            state.scanning = True
        payload = index = None
        try:
            result = scanner.scan()
            result.save(data_dir)
            payload, index, error = result.to_dict(), MarketIndex(result.events), None
            if on_result is not None:
                stamp = time.strftime("%H:%M:%S")
                new = [f"{stamp} {m}" for m in on_result(result)]
                with state.lock:
                    state.messages.extend(new)
            if fast is not None:
                fast.update(result)
        except Exception as exc:  # keep serving the last good result
            log.exception("scan failed")
            error = f"{type(exc).__name__}: {exc}"
        failures = 0 if error is None else failures + 1
        wait = max(5.0, state.interval - (time.monotonic() - started))
        if failures:  # e.g. offline or just woken: retry sooner, backing off to the normal interval
            wait = min(wait, RETRY_AFTER_FAILURE_S * 2 ** (failures - 1))
        with state.lock:
            state.scanning = False
            state.error = error
            if payload is not None:
                state.result, state.index = payload, index
            state.next_scan_at = time.time() + wait
        if wait_or_wake(stop, wait):
            log.info("woke from sleep: scanning now")


RETRY_AFTER_FAILURE_S = 15.0
SLEEP_JUMP_S = 30.0


def wait_or_wake(stop: threading.Event, seconds: float, step: float = 5.0) -> bool:
    """Wait up to `seconds`, returning early (True) if the computer slept in the meantime.

    On macOS the monotonic clock stops during sleep while the wall clock keeps going, so a
    wall-clock jump well past the monotonic one means the machine was asleep: the last scan
    is stale and the next one shouldn't wait out the rest of the interval.
    """
    end = time.monotonic() + seconds
    while not stop.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return False
        wall, mono = time.time(), time.monotonic()
        if stop.wait(min(step, left)):
            return False
        if (time.time() - wall) - (time.monotonic() - mono) > SLEEP_JUMP_S:
            return True
    return False


def _static_file(path: str) -> Path | None:
    """Resolve /static/<rel> inside STATIC_DIR, refusing anything that escapes it."""
    rel = path.removeprefix("/static/")
    target = (STATIC_DIR / rel).resolve()
    if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
        return None
    return target


def _make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), CONTENT_TYPES[".html"])
            elif path.startswith("/static/"):
                f = _static_file(path)
                if f is None:
                    self._send(404, b"not found", "text/plain")
                else:
                    self._send(200, f.read_bytes(), CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
            elif path.startswith("/api/"):
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                try:
                    status, payload = api.handle(path, query)
                except Exception as exc:  # upstream API failures shouldn't kill the request thread
                    log.warning("%s failed: %s", path, exc)
                    status, payload = 502, {"error": str(exc)}
                self._send(status, json.dumps(payload, default=str).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            # A custom header can't be sent cross-origin without a CORS preflight, which this
            # server never approves, so other websites can't trigger actions here.
            if self.headers.get("X-Pmscan") != "1":
                self._send(403, b'{"error": "forbidden"}', "application/json")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}
            try:
                status, payload = api.post(path, body if isinstance(body, dict) else {})
            except Exception as exc:
                log.warning("%s failed: %s", path, exc)
                status, payload = 500, {"error": str(exc)}
            self._send(status, json.dumps(payload, default=str).encode(), "application/json")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(scanner: Scanner, host: str, port: int, interval: float, data_dir: Path,
          on_result: OnResult | None = None, trader: AutoTrader | None = None,
          config: ScanConfig | None = None, fast: FastWatcher | None = None,
          on_found: OnResult | None = None) -> None:
    state = _State(interval, trader, fast, scanner.client.venue)
    if fast is not None:
        def found(result: ScanResult) -> None:
            messages = on_found(result) if on_found else []
            for name, opps in result.opportunities.items():
                messages += [f"{name.replace('_', ' ')}: {o.title} · profit ${o.profit:,.2f}" for o in opps]
            state.merge_found(result, messages)
        fast.on_found = found
        fast.start()
    latest = data_dir / "latest.json"
    if latest.exists():  # show the previous result while the first scan runs
        try:
            state.result = json.loads(latest.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    stop = threading.Event()
    worker = threading.Thread(target=_scan_loop, args=(scanner, state, data_dir, stop, on_result, fast), daemon=True)
    worker.start()

    api = Api(state, data_dir, scanner.client, config or scanner.config)
    if scanner.client.venue == "us" and api.config.rewards.enabled:
        api.rewards.start()  # paper-tests reward quotes in the background (needs the US API key)
    server = ThreadingHTTPServer((host, port), _make_handler(api))
    print(f"pmscan dashboard on http://{host}:{port}  (rescanning every {interval:.0f}s, Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        api.rewards.stop()
        if fast is not None:
            fast.stop()
        server.server_close()
