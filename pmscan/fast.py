"""Live order-book feed for the markets closest to an arbitrage.

A full scan takes 20-40 seconds and runs every couple of minutes, while arbitrage
usually lasts seconds. After each scan, the watcher subscribes to Polymarket's
real-time market feed for the events nearest to an edge (from the arbitrage
strategies' own screening). Every price change updates those markets' quotes in
place; when one moves into an edge at the top of the book, the event's order books
are fetched and the normal strategy evaluation runs, so a found opportunity is
exactly what a scan would have produced, just sooner.

Needs the official SDK (uv sync --extra trade) for the streaming client. On Polymarket US
the market stream also needs the account's API key.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .models import Event, Market, Opportunity
from .parse import _price
from .scanner import Scanner, ScanResult
from .strategies import ARBITRAGE, ScanContext

log = logging.getLogger(__name__)

OnFound = Callable[[ScanResult], None]
US_SUBSCRIPTION_LIMIT = 100  # markets per Polymarket US subscription


def _amount(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else value


class FastWatcher:
    def __init__(self, scanner: Scanner, on_found: OnFound, max_events: int = 40,
                 debounce_s: float = 0.5, repeat_after_s: float = 1800.0, repeat_if_better: float = 1.25) -> None:
        self.scanner = scanner
        self.strategies = [s for s in scanner.strategies if s.name in ARBITRAGE]
        self.on_found = on_found
        self.max_events = max_events
        self.debounce_s = debounce_s
        self.repeat_after_s = repeat_after_s
        self.repeat_if_better = repeat_if_better
        self._lock = threading.Lock()
        self._events: dict[str, Event] = {}
        self._index: dict[str, tuple[Market, int, str]] = {}  # token -> (market, side, event id)
        self._tokens: list[str] = []
        self._dirty: set[str] = set()
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._reported: dict[str, tuple[float, float]] = {}  # opportunity key -> (when, profit)
        self.stats: dict[str, Any] = {"enabled": True, "connected": False, "watching_events": 0,
                                      "watching_markets": 0, "updates": 0, "checks": 0, "found": 0,
                                      "last_update": None, "error": None}

    # ---------- watch list (called from the scan thread) ----------
    def update(self, result: ScanResult) -> None:
        closeness: dict[str, float] = result.stats.get("watch", {})
        ids = set(sorted(closeness, key=lambda i: closeness[i], reverse=True)[: self.max_events])
        events = {e.id: e for e in result.events if e.id in ids}
        index: dict[str, tuple[Market, int, str]] = {}
        tokens = []
        for eid, e in events.items():
            for m in e.markets:
                if not m.tradeable:
                    continue
                for side, t in enumerate(m.token_ids):
                    index[t] = (m, side, eid)
                tokens.append(m.token_ids[0])  # one side is enough: the books mirror each other
        now = time.monotonic()
        with self._lock:
            # What the scan already found isn't news: only report it again if it gets better.
            for name in ARBITRAGE:
                for o in result.opportunities.get(name, []):
                    self._reported[o.key] = (now, o.profit)
            changed = set(tokens) != set(self._tokens)
            self._events, self._index, self._tokens = events, index, tokens
            self.stats.update(watching_events=len(events), watching_markets=len(tokens))
        if changed:
            self._changed.set()

    def start(self) -> None:
        threading.Thread(target=self._run, name="pmscan-fast", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._changed.set()

    # ---------- streaming ----------
    @property
    def venue(self) -> str:
        return getattr(self.scanner.client, "venue", "international")

    def _run(self) -> None:
        if self.venue == "us":
            try:
                import polymarket_us  # noqa: F401
            except ImportError:
                self.stats.update(enabled=False, error="the live feed needs the SDK: uv sync --extra trade")
                return
            asyncio.run(self._main_us())
            return
        try:
            import polymarket  # noqa: F401
        except ImportError:
            self.stats.update(enabled=False, error="the live feed needs the SDK: uv sync --extra trade")
            return
        asyncio.run(self._main())

    async def _main_us(self) -> None:
        """Polymarket US: its market stream needs the account's API key (best bid / ask per market)."""
        from polymarket_us import PolymarketUS

        from . import env

        key_id, secret = env.get(env.US_KEY_ID), env.get(env.US_SECRET_KEY)
        if not key_id or not secret:
            self.stats.update(enabled=False, error="the Polymarket US live feed needs your API key")
            return
        loop = asyncio.get_running_loop()
        delay = 2.0
        while not self._stop.is_set():
            with self._lock:
                slugs = list(self._tokens)  # YES tokens are the market slugs
            self._changed.clear()
            if not slugs:
                await asyncio.sleep(2)
                continue
            ws = PolymarketUS(key_id=key_id, secret_key=secret).ws.markets()
            closed = asyncio.Event()
            errors: list[str] = []
            ws.on("market_data_lite", self._apply_us)
            ws.on("close", lambda *_, closed=closed: closed.set())
            ws.on("error", lambda e, errors=errors: errors.append(str(getattr(e, "message", None) or e)))
            try:
                await ws.connect()
                for i in range(0, len(slugs), US_SUBSCRIPTION_LIMIT):
                    await ws.subscribe_market_data_lite(f"pmscan-{i}", slugs[i : i + US_SUBSCRIPTION_LIMIT])
                self.stats.update(connected=True, error=None)
                delay = 2.0
                last_check = 0.0
                while not (self._stop.is_set() or self._changed.is_set() or closed.is_set()):
                    await asyncio.sleep(0.1)
                    if errors:
                        self.stats["error"] = errors[-1]
                    if self._dirty and time.monotonic() - last_check >= self.debounce_s:
                        last_check = time.monotonic()
                        await loop.run_in_executor(None, self._check)
            except Exception as exc:  # network or auth trouble: back off and reconnect
                self.stats["error"] = f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"
                log.warning("Polymarket US live feed error: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            finally:
                self.stats["connected"] = False
                with contextlib.suppress(Exception):
                    await ws.close()

    def _apply_us(self, message: dict) -> None:
        data = message.get("marketDataLite") or {}
        slug = data.get("marketSlug")
        if slug:
            self._quote(slug, _amount(data.get("bestBid")), _amount(data.get("bestAsk")))

    async def _main(self) -> None:
        from polymarket import AsyncPublicClient
        from polymarket.streams import MarketSpec

        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            with self._lock:
                tokens = list(self._tokens)
            self._changed.clear()
            if not tokens:
                await asyncio.sleep(2)
                continue
            try:
                async with AsyncPublicClient() as client:
                    spec = MarketSpec(asset_ids=tokens, custom_feature_enabled=True)
                    async with await client.subscribe([spec]) as stream:
                        self.stats.update(connected=True, error=None)
                        last_check = 0.0
                        while not self._stop.is_set() and not self._changed.is_set():
                            try:
                                self._apply(await asyncio.wait_for(stream.__anext__(), timeout=1.0))
                            except asyncio.TimeoutError:
                                pass
                            if self._dirty and time.monotonic() - last_check >= self.debounce_s:
                                last_check = time.monotonic()
                                await loop.run_in_executor(None, self._check)
            except StopAsyncIteration:
                pass
            except Exception as exc:  # network trouble: wait and reconnect
                self.stats["error"] = f"{type(exc).__name__}: {exc}"
                log.warning("live feed error: %s", exc)
                await asyncio.sleep(5)
            finally:
                self.stats["connected"] = False

    def _apply(self, ev: Any) -> None:
        p = ev.payload
        if ev.type == "best_bid_ask":
            self._quote(p.asset_id, p.best_bid, p.best_ask)
        elif ev.type == "price_change":
            for c in p.price_changes:
                self._quote(c.asset_id, c.best_bid, c.best_ask)
        elif ev.type == "book":
            bids = [lv.price for lv in p.bids]
            asks = [lv.price for lv in p.asks]
            self._quote(p.asset_id, max(bids) if bids else None, min(asks) if asks else None)

    def _quote(self, token: Any, bid: Any, ask: Any) -> None:
        with self._lock:
            hit = self._index.get(str(token))
            if not hit:
                return
            m, side, eid = hit
            bid, ask = _price(bid), _price(ask)
            if side == 0:
                m.best_bid, m.best_ask = bid, ask
            else:  # a NO quote mirrors the YES side
                m.best_bid = None if ask is None else 1.0 - ask
                m.best_ask = None if bid is None else 1.0 - bid
            self._dirty.add(eid)
            self.stats["updates"] += 1
            self.stats["last_update"] = datetime.now(timezone.utc).isoformat()

    # ---------- evaluation (worker thread) ----------
    def _check(self) -> None:
        with self._lock:
            events = [self._events[i] for i in self._dirty if i in self._events]
            self._dirty.clear()
        if not events:
            return
        self.stats["checks"] += 1
        ctx = ScanContext(config=self.scanner.config, now=datetime.now(timezone.utc), venue=getattr(self.scanner.client, "venue", "international"))
        # Only an edge at the top of the book is worth fetching full books for.
        candidates = [(s, c) for s in self.strategies for c in s.screen(events, ctx) if c.priority > 0]
        if not candidates:
            return
        books = self.scanner.client.get_books({t for _, c in candidates for t in c.token_ids})
        found: dict[str, list[Opportunity]] = {}
        now = time.monotonic()
        for s, c in candidates:
            o = s.evaluate(c, books, ctx)
            if o is None:
                continue
            when, profit = self._reported.get(o.key, (-1e9, 0.0))
            if now - when < self.repeat_after_s and o.profit < profit * self.repeat_if_better:
                continue
            self._reported[o.key] = (now, o.profit)
            found.setdefault(s.name, []).append(o)
        if found:
            self.stats["found"] += sum(len(v) for v in found.values())
            self.on_found(ScanResult(started_at=datetime.now(timezone.utc), duration_s=0.0,
                                     stats={"source": "live feed"}, opportunities=found, events=events))
