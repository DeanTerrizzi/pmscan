"""Polymarket US (polymarket.us): market data and order placement.

Polymarket US is a separate, CFTC-regulated exchange with its own markets, prices and
accounts. Accounts are ordinary sign-ins verified with ID, there is no crypto wallet,
and the API uses a Key ID + Secret Key made at polymarket.us/developer.

How it maps onto the scanner's model:

* Each US market is **one instrument**, the YES side; NO is a short of it. The scanner
  still sees two tokens per market, ``<slug>`` (YES) and ``<slug>~no`` (NO), and the NO
  order book is built by mirroring the YES book (NO ask = 1 - YES bid).
* Fees: ``theta * contracts * p * (1 - p)`` for takers, with theta per market
  (``feeCoefficient``), which is the formula the scanner already uses.
* US events carry no "mutually exclusive" flag, so baskets that rely on at most one
  outcome winning are only built for events whose wording says so (see
  ``mutually_exclusive``), plus soccer's win / draw / win markets, which are also the
  only ones known to be exhaustive.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

import httpx

from . import env
from .client import PolymarketClient
from .models import NO_FEES, Event, FeeSchedule, Level, Market, OrderBook, Team
from .parse import _float, parse_datetime

log = logging.getLogger(__name__)

GATEWAY_URL = "https://gateway.polymarket.us"
SITE_URL = "https://polymarket.us"
NO_SUFFIX = "~no"
GAME_TYPES = {"moneyline", "drawable_outcome", "spreads", "totals", "props"}
PAGE_SIZE = 100


def yes_token(slug: str) -> str:
    return slug


def no_token(slug: str) -> str:
    return slug + NO_SUFFIX


def split_token(token: str) -> tuple[str, int]:
    """(market slug, side) with side 0 = YES, 1 = NO."""
    return (token[: -len(NO_SUFFIX)], 1) if token.endswith(NO_SUFFIX) else (token, 0)


def _amount(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    return _float(value)


def _quote(value: Any) -> float | None:
    p = _amount(value)
    return p if p is not None and 0.0 < p < 1.0 else None


# ---------- which events are mutually exclusive ----------

# Event titles that name a single winner ("... Champion", "NBA MVP").
_SINGLE_WINNER = re.compile(
    r"\b(champion|winner|win the|leader|mvp|most\b|highest|lowest|margin of victory|nominee\b|next\b|"
    r"prime minister|president\b|governor|mayor|speaker|chancellor|pope\b|control|largest|top finisher|"
    r"first\b|of the year|heisman|cy young|rookie)",
    re.I,
)
# Wording that allows several outcomes at once ("Oscar Nominees", "NBA Teams 50+ Wins").
_MULTI = re.compile(
    r"\b(nominees|nominated|nominations|which\b|how many|how high|how low|make the|qualif|advance|reach|"
    r"top \d|all\b|any\b|each\b|or more|teams|players|finishers|place|announce|meet|impeach|out of|"
    r"have a|person of the year|host|by\b)",
    re.I,
)
# Market titles that are thresholds or deadlines: nested, not exclusive (a ladder).
_THRESHOLD = re.compile(
    r"(\d[\d,.]*\s*[kmb%]?\s*\+|\bor (more|above|higher|over|less|fewer|below|lower)\b|\b(above|below|over|"
    r"under|at least|more than|less than|fewer than)\b|^\s*(by|before)\b|"
    r"^\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}\s*$)",
    re.I,
)
MAX_BID_SUM = 1.25  # an "exclusive" set whose YES bids add up past this is almost surely misread


def mutually_exclusive(raw: dict) -> bool:
    """True when at most one of the event's markets can resolve YES."""
    markets = [m for m in raw.get("markets") or [] if isinstance(m, dict)]
    if len(markets) < 2:
        return False
    types = {m.get("marketType") or "" for m in markets}
    if types == {"drawable_outcome"}:
        return True  # home win / draw / away win
    if types != {"futures"}:
        return False
    title = str(raw.get("title") or "")
    if _MULTI.search(title) or not _SINGLE_WINNER.search(title):
        return False
    if any(_THRESHOLD.search(str(m.get("title") or "")) for m in markets):
        return False
    bids = sum(_quote(m.get("bestBidQuote")) or 0.0 for m in markets)
    return bids <= MAX_BID_SUM


def exhaustive(raw: dict) -> bool:
    """True when one of the listed outcomes must win (so buying every YES pays $1)."""
    markets = raw.get("markets") or []
    return len(markets) == 3 and all(m.get("marketType") == "drawable_outcome" for m in markets)


# ---------- parsing ----------

def _sides(rm: dict) -> tuple[str, str]:
    """Outcome names (YES side first). Moneylines use full team names, so they match bookmakers'."""
    long_ = short = None
    for side in rm.get("marketSides") or []:
        name = side.get("description")
        team = (side.get("team") or {}).get("name") if isinstance(side.get("team"), dict) else None
        if rm.get("marketType") == "moneyline" and team:
            name = team
        if side.get("long"):
            long_ = name
        else:
            short = name
    return (str(long_ or "Yes"), str(short or "No"))


def _label(rm: dict) -> str:
    title = str(rm.get("title") or "")
    if rm.get("marketType") == "drawable_outcome":
        title = title.replace(" (Reg. Time)", "").strip()
        if title.lower() in {"tie", "draw"}:
            return "Draw"
    return title


def _end_date(rm: dict, event_raw: dict, mtype: str) -> datetime | None:
    """A game market's end date is its settlement deadline (two weeks out); the game ends with the event."""
    end = parse_datetime(rm.get("endDate"))
    if mtype in GAME_TYPES:
        event_end = parse_datetime(event_raw.get("endDate"))
        if event_end is not None and (end is None or event_end < end):
            return event_end
    return end


def parse_market(rm: dict, event_raw: dict, group_id: str | None) -> Market | None:
    slug = str(rm.get("slug") or "")
    if not slug:
        return None
    event_title = str(event_raw.get("title") or "")
    mtype = str(rm.get("marketType") or "")
    single = len(event_raw.get("markets") or []) == 1
    question = str(rm.get("question") or "")
    if mtype == "moneyline" or not question or question == event_title:
        question = str(rm.get("title") or event_title) if single else f"{event_title}: {rm.get('title') or ''}"
    theta = _float(rm.get("feeCoefficient"), 0.0) or 0.0
    step = _float(rm.get("minimumTradeQty"), 1.0) or 1.0
    tradeable = bool(rm.get("active") and not rm.get("closed") and rm.get("status", "MARKET_STATUS_OPEN")
                     == "MARKET_STATUS_OPEN" and not rm.get("hidden"))
    return Market(
        id=slug,
        condition_id=str(rm.get("id") or ""),
        question=question,
        slug=slug,
        group_title="" if single else _label(rm),
        outcomes=_sides(rm),
        token_ids=(yes_token(slug), no_token(slug)),
        best_bid=_quote(rm.get("bestBidQuote")),
        best_ask=_quote(rm.get("bestAskQuote")),
        liquidity=0.0,
        volume_24h=0.0,
        end_date=_end_date(rm, event_raw, mtype),
        neg_risk=group_id is not None,
        neg_risk_id=group_id,
        is_other=False,
        tradeable=tradeable,
        fees=FeeSchedule(theta, 1.0) if theta > 0 else NO_FEES,
        min_order_size=step,
        game_start=parse_datetime(rm.get("gameStartTime")) if mtype in GAME_TYPES else None,
        # The sportsbook strategy reads soccer's win / draw / win markets as moneylines too.
        sports_market_type="moneyline" if mtype in {"moneyline", "drawable_outcome"} else mtype,
        event_slug=str(event_raw.get("slug") or ""),
        event_title=event_title,
        venue="us",
        stats_known=False,
        market_type=mtype,
        category=str(rm.get("category") or event_raw.get("category") or "").lower(),
        tick_size=_float(rm.get("orderPriceMinTickSize"), 0.01) or 0.01,
        size_step=step,
    )


def parse_event(raw: dict) -> Event:
    eid = str(raw.get("id") or "")
    group = f"us-{eid}" if mutually_exclusive(raw) else None
    markets = [m for rm in raw.get("markets") or [] if not rm.get("closed")
               if (m := parse_market(rm, raw, group)) is not None]
    teams = tuple(Team(str(t["name"]), str(t.get("abbreviation") or ""), t.get("ordering") == "home")
                  for t in raw.get("teams") or [] if isinstance(t, dict) and t.get("name"))
    league = next((str(t.get("league")) for t in raw.get("teams") or [] if isinstance(t, dict) and t.get("league")), "")
    is_game = any(m.game_start is not None for m in markets)
    return Event(
        id=eid,
        slug=str(raw.get("slug") or ""),
        title=str(raw.get("title") or ""),
        neg_risk=group is not None,
        augmented=not exhaustive(raw),  # blocks "buy every YES" unless the outcomes are known to be complete
        markets=markets,
        tags=tuple(str(t.get("label") or t.get("slug") or "") for t in raw.get("tags") or [] if isinstance(t, dict)),
        live=bool(raw.get("live")),
        ended=bool(raw.get("ended")) or str(raw.get("period") or "").upper() in {"F", "FT", "FINAL", "ENDED"},
        sport=(league or str(raw.get("seriesSlug") or "").split("-")[0]).lower() if is_game else "",
        teams=teams if is_game else (),
        start_time=parse_datetime(raw.get("startTime")) if is_game else None,
        venue="us",
    )


def _levels(raw: Any, *, descending: bool, mirror: bool = False) -> list[Level]:
    out = []
    for lvl in raw or []:
        price, size = _amount(lvl.get("px")), _float(lvl.get("qty"))
        if price is None or not size or size <= 0:
            continue
        if mirror:
            price = round(1.0 - price, 6)
        if 0.0 < price < 1.0:
            out.append(Level(price, size))
    out.sort(key=lambda lv: lv.price, reverse=descending)
    return out


def parse_book(slug: str, raw: dict, min_size: float = 0.0) -> tuple[OrderBook, OrderBook]:
    """YES and NO books from one market's book (selling YES at p = buying NO at 1 - p)."""
    data = raw.get("marketData") or raw
    bids, offers = data.get("bids"), data.get("offers")
    yes = OrderBook(yes_token(slug), _levels(bids, descending=True), _levels(offers, descending=False), min_size)
    no = OrderBook(no_token(slug), _levels(offers, descending=True, mirror=True),
                   _levels(bids, descending=False, mirror=True), min_size)
    return yes, no


# ---------- public market data ----------

class _RateLimiter:
    """At most `rate` calls per second across threads (the public API allows 25/s per IP)."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


class UsClient(PolymarketClient):
    """Read-only Polymarket US market data (no key needed)."""

    venue = "us"
    label = "Polymarket US"

    def __init__(self, gateway_url: str = GATEWAY_URL, timeout: float = 30.0, max_retries: int = 4,
                 rate: float = 18.0) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
        self.gateway_url = gateway_url.rstrip("/")
        self._limit = _RateLimiter(rate)
        self._step: dict[str, float] = {}  # slug -> size step, learned from event pages

    def _get(self, path: str, **params: Any) -> Any:
        self._limit.wait()
        return self._request("GET", f"{self.gateway_url}{path}", params=params or None)

    def iter_event_pages(self, page_size: int = PAGE_SIZE) -> Iterator[list[dict]]:
        offset, batch = 0, 4  # a few pages at a time; a short page means the end
        with ThreadPoolExecutor(max_workers=batch) as pool:
            while True:
                offsets = [offset + i * page_size for i in range(batch)]
                pages = list(pool.map(lambda o: (self._get("/v1/events", limit=page_size, offset=o, active="true",
                                                           closed="false").get("events") or []), offsets))
                for page in pages:
                    if page:
                        yield page
                    if len(page) < page_size:
                        return
                offset += batch * page_size

    def iter_events(self) -> Iterator[list[Event]]:
        for page in self.iter_event_pages():
            events = [parse_event(raw) for raw in page]
            for e in events:
                for m in e.markets:
                    self._step[m.slug] = m.size_step
            yield events

    def get_books(self, token_ids: Iterable[str], workers: int = 6) -> dict[str, OrderBook]:
        slugs = list(dict.fromkeys(split_token(t)[0] for t in token_ids))
        books: dict[str, OrderBook] = {}
        if not slugs:
            return books

        def fetch(slug: str) -> tuple[str, dict | None]:
            try:
                return slug, self._get(f"/v1/markets/{slug}/book")
            except httpx.HTTPError as exc:
                log.warning("book for %s failed: %s", slug, exc)
                return slug, None

        with ThreadPoolExecutor(max_workers=min(workers, len(slugs))) as pool:
            for slug, raw in pool.map(fetch, slugs):
                if raw is None:
                    continue
                state = (raw.get("marketData") or {}).get("state", "MARKET_STATE_OPEN")
                if state != "MARKET_STATE_OPEN":
                    continue  # suspended / halted / closed books can't be traded
                yes, no = parse_book(slug, raw, self._step.get(slug, 0.0))
                books[yes.token_id], books[no.token_id] = yes, no
        return books

    def price_history(self, token_id: str, interval: str = "1w", fidelity: int = 60) -> list[dict]:
        slug, side = split_token(token_id)
        fixed = {"1h": "INTERVAL_1H", "6h": "INTERVAL_6H", "1d": "INTERVAL_1D", "1w": "INTERVAL_1W",
                 "1m": "INTERVAL_1M", "max": "INTERVAL_ALL"}.get(interval, "INTERVAL_1W")
        data = self._get("/v1/price-history", symbol=slug, fixedInterval=fixed, fidelity=fidelity)
        key = "shortPrice" if side else "longPrice"
        return [{"t": h["timestamp"], "p": h[key]} for h in data.get("history") or []
                if h.get("timestamp") is not None and h.get(key) is not None]

    def resolutions(self, market_ids: list[str]) -> dict[str, list[float]]:
        """Settled markets' payout per share [YES, NO]. US market ids are their slugs."""

        def fetch(slug: str) -> tuple[str, float | None]:
            try:
                return slug, _float(self._get(f"/v1/markets/{slug}/settlement").get("settlement"))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (400, 404):
                    log.warning("settlement for %s failed: %s", slug, exc)
                return slug, None  # not settled yet
            except httpx.HTTPError as exc:
                log.warning("settlement for %s failed: %s", slug, exc)
                return slug, None

        out: dict[str, list[float]] = {}
        if not market_ids:
            return out
        with ThreadPoolExecutor(max_workers=min(6, len(market_ids))) as pool:
            for slug, s in pool.map(fetch, market_ids):
                if s is not None and 0.0 <= s <= 1.0:
                    out[slug] = [s, 1.0 - s]
        return out


# ---------- trading ----------

def _sdk() -> Any:
    try:
        import polymarket_us
    except ImportError as exc:
        raise RuntimeError("Polymarket US trading needs its SDK: uv sync --extra trade") from exc
    return polymarket_us


def validate_keys(key_id: str, secret: str) -> None:
    """Catch the common copy-paste mistakes before talking to Polymarket US."""
    import base64
    import binascii

    if not key_id.strip():
        raise ValueError("Enter the Key ID from polymarket.us/developer.")
    if len(key_id.strip()) < 8 or any(c.isspace() for c in key_id.strip()):
        raise ValueError("That Key ID doesn't look right: copy the Key ID shown next to your key at "
                         "polymarket.us/developer.")
    try:
        raw = base64.b64decode(secret.strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) not in (32, 64):
        raise ValueError("That Secret Key doesn't look right. It's the long code shown once when you created "
                         "the key at polymarket.us/developer. If you didn't save it, delete that key and "
                         "create a new one.")


def _err(exc: Exception) -> str:
    return str(getattr(exc, "message", None) or exc) or type(exc).__name__


class UsExchange:
    """Order placement on Polymarket US through its official SDK (pip install polymarket-us)."""

    def __init__(self, client: Any, key_id: str = "") -> None:
        self.client = client
        self.key_id = key_id

    @classmethod
    def from_env(cls) -> UsExchange:
        key_id, secret = env.get(env.US_KEY_ID) or "", env.get(env.US_SECRET_KEY) or ""
        if not key_id or not secret:
            raise RuntimeError(f"Live trading on Polymarket US needs {env.US_KEY_ID} and {env.US_SECRET_KEY} "
                               "(made at polymarket.us/developer).")
        return cls.connect(key_id, secret)

    @classmethod
    def connect(cls, key_id: str, secret: str) -> UsExchange:
        validate_keys(key_id, secret)
        sdk = _sdk()
        return cls(sdk.PolymarketUS(key_id=key_id.strip(), secret_key=secret.strip()), key_id.strip())

    @staticmethod
    def _price(price: Decimal | float, tick: float) -> str:
        """A price on the market's tick grid, as the API wants it (a decimal string)."""
        t = Decimal(str(tick or 0.01))
        q = (Decimal(str(price)) / t).to_integral_value(rounding=ROUND_DOWN) * t
        return format(q.normalize(), "f")

    def _create(self, slug: str, intent: str, yes_price: str, qty: float, tif: str) -> dict:
        return self.client.orders.create({
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": yes_price, "currency": "USD"},
            "quantity": qty,
            "tif": tif,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "3",
        })

    @staticmethod
    def _result(resp: dict, wanted: float) -> Any:
        from .trading import OrderResult

        executions = resp.get("executions") or []
        filled = 0.0
        reject = None
        state = ""
        for ex in executions:
            filled += _float(ex.get("lastShares"), 0.0) or 0.0
            order = ex.get("order") or {}
            state = str(order.get("state") or state)
            filled = max(filled, _float(order.get("cumQuantity"), 0.0) or 0.0)
            if ex.get("orderRejectReason") or str(ex.get("type", "")).endswith("REJECTED"):
                reject = ex.get("text") or str(ex.get("orderRejectReason") or "rejected")
        oid = resp.get("id")
        if filled >= wanted * 0.999 and filled > 0:
            return OrderResult(True, "matched", oid, None, filled)
        if filled > 0:
            return OrderResult(True, "partial", oid, f"{filled:g} of {wanted:g} filled", filled)
        if reject or state in ("ORDER_STATE_REJECTED", "ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED"):
            return OrderResult(False, "rejected", oid, reject or state, 0.0)
        return OrderResult(True, "unknown", oid, "no execution report yet", None)

    def submit(self, orders: Sequence[Any]) -> list[Any]:
        """Every leg as a fill-or-kill limit buy, all sent at once."""
        from .trading import OrderResult

        def one(o: Any) -> Any:
            slug, side = split_token(o.token_id)
            tick = getattr(o, "tick_size", 0.01)
            # Prices always refer to the YES side: buying NO at p is selling YES at 1 - p.
            if side == 0:
                intent, price = "ORDER_INTENT_BUY_LONG", self._price(o.max_price, tick)
            else:
                yes = Decimal(1) - Decimal(str(o.max_price))
                t = Decimal(str(tick))
                yes = (yes / t).to_integral_value(rounding="ROUND_UP") * t  # never a worse NO price
                intent, price = "ORDER_INTENT_BUY_SHORT", format(yes.normalize(), "f")
            try:
                return self._result(self._create(slug, intent, price, o.shares, "TIME_IN_FORCE_FILL_OR_KILL"),
                                    o.shares)
            except Exception as exc:
                return OrderResult(False, "error", message=_err(exc))

        with ThreadPoolExecutor(max_workers=max(1, min(len(orders), 8))) as pool:
            return list(pool.map(one, orders))

    def sell(self, token_id: str, shares: float, min_price: float) -> Any:
        """Immediate-or-cancel sell with a price floor (used to unwind a partial basket)."""
        from .trading import OrderResult

        slug, side = split_token(token_id)
        if side == 0:
            intent, price = "ORDER_INTENT_SELL_LONG", self._price(min_price, 0.01)
        else:  # selling NO at >= p is buying YES back at <= 1 - p
            intent, price = "ORDER_INTENT_SELL_SHORT", self._price(Decimal(1) - Decimal(str(min_price)), 0.01)
        try:
            return self._result(self._create(slug, intent, price, shares, "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"),
                                shares)
        except Exception as exc:
            return OrderResult(False, "error", message=_err(exc))

    def account_summary(self) -> dict[str, Any]:
        b = self.client.account.balances()
        b = b.get("balances", [b])[0] if isinstance(b.get("balances"), list) and b["balances"] else b
        power = _float(b.get("buyingPower"), 0.0) or 0.0
        cash = _float(b.get("currentBalance"), 0.0) or 0.0
        masked = f"{self.key_id[:4]}…{self.key_id[-4:]}" if len(self.key_id) > 10 else self.key_id
        return {
            "venue": "us",
            "wallet": f"Polymarket US (key {masked})",
            "wallet_type": "Polymarket US",
            "wallet_type_label": "Polymarket US account",
            "collateral_balance": f"${power:,.2f} buying power",
            "collateral_usd": power,
            "cash_usd": cash,
            "positions_usd": _float(b.get("assetNotional"), 0.0) or 0.0,
            "open_orders_usd": _float(b.get("openOrders"), 0.0) or 0.0,
        }


    def position(self, slug: str) -> float | None:
        """Net contracts held in a market: positive = YES (long), negative = NO (short)."""
        try:
            data = self.client.portfolio.positions({"market": slug})
        except Exception as exc:
            log.warning("positions for %s failed: %s", slug, exc)
            return None
        pos = (data.get("positions") or {}).get(slug)
        if not pos:
            return 0.0
        return _float(pos.get("netPositionDecimal"), None) if pos.get("netPositionDecimal") is not None \
            else _float(pos.get("netPosition"), 0.0)


def check_keys(key_id: str, secret: str) -> dict[str, Any]:
    """Connect with the given keys and report the account's balance."""
    exchange = UsExchange.connect(key_id, secret)
    sdk = _sdk()
    try:
        return exchange.account_summary()
    except sdk.AuthenticationError as exc:
        raise RuntimeError("Polymarket US didn't accept that Key ID and Secret Key. Check you copied both from "
                           "the same key at polymarket.us/developer, and that your identity verification in "
                           f"the app is complete ({_err(exc)}).") from exc
    except Exception as exc:
        raise RuntimeError(f"Couldn't reach your Polymarket US account: {_err(exc)}") from exc


def floor_step(x: float, step: float) -> float:
    step = step or 0.01
    return math.floor(round(x / step, 9)) * step


# ---------- a $1 round trip to prove the order path ----------

def pick_test_market(client: UsClient, events: Sequence[Event] | None = None) -> tuple[Market, OrderBook] | None:
    """A busy, two-sided market near 50¢ with a one-tick spread and depth on both sides."""
    if events is None:
        events = [e for page in client.iter_events() for e in page]
    picks = []
    for e in events:
        if e.live:
            continue
        for m in e.markets:
            if not m.tradeable or m.best_bid is None or m.best_ask is None or m.size_step > 0.01:
                continue
            if 0.25 <= m.best_bid and m.best_ask <= 0.75 and m.best_ask - m.best_bid <= max(m.tick_size, 0.01) + 1e-9:
                picks.append(m)
    picks.sort(key=lambda m: abs((m.best_bid + m.best_ask) / 2 - 0.5))
    for m in picks[:15]:
        books = client.get_books([m.token_ids[0]])
        yes = books.get(m.token_ids[0])
        if yes and yes.bids and yes.asks and yes.bids[0].size >= 20 and yes.asks[0].size >= 20:
            return m, yes
    return None


def run_test_trade(exchange: UsExchange, client: UsClient, budget: float = 1.0,
                   events: Sequence[Event] | None = None) -> dict[str, Any]:
    """Buy about $1 of YES and sell it back, then the same with NO, checking fills and positions.

    Costs the spread plus fees (a few cents). If Polymarket US read NO prices differently from
    how pmscan sends them, the NO order would simply not fill, and that's reported.
    """
    from .trading import LegOrder

    pick = pick_test_market(client, events)
    if pick is None:
        raise RuntimeError("Couldn't find a busy enough market for the test right now. Try again in a few minutes.")
    m, book = pick
    steps: list[dict[str, Any]] = []
    start = exchange.position(m.slug) or 0.0

    def step(name: str, result: Any, expect: float | None) -> bool:
        held = None
        for _ in range(6):  # positions can trail the fill by a moment
            held = exchange.position(m.slug)
            if expect is None or held is None or abs(held - expect) < 0.011 or not result.filled:
                break
            time.sleep(0.5)
        ok = bool(result.filled) and (expect is None or held is None or abs(held - expect) < 0.011)
        steps.append({"step": name, "ok": ok, "status": result.status, "filled": result.shares,
                      "message": result.message, "position_after": held, "expected_position": expect})
        return ok

    yes_ask, yes_bid = book.asks[0].price, book.bids[0].price
    qty = max(1.0, floor_step(budget / yes_ask, 0.01))
    qty = round(qty, 2)
    buy_yes = exchange.submit([LegOrder(m.label, m.outcomes[0], m.token_ids[0], Decimal(str(budget)),
                                        Decimal(str(yes_ask)), qty, m.tick_size)])[0]
    if step(f"Buy {qty:g} YES at {yes_ask * 100:.1f}¢", buy_yes, start + qty):
        floor = max(0.01, round(yes_bid - 0.02, 2))  # a little room so the sell-back fills
        step(f"Sell {qty:g} YES back (at least {floor * 100:.0f}¢)",
             exchange.sell(m.token_ids[0], qty, floor), start)

    fresh = client.get_books([m.token_ids[1]]).get(m.token_ids[1])
    if fresh and fresh.asks and fresh.bids and steps and all(s["ok"] for s in steps):
        no_ask, no_bid = fresh.asks[0].price, fresh.bids[0].price
        qty_no = round(max(1.0, floor_step(budget / no_ask, 0.01)), 2)
        buy_no = exchange.submit([LegOrder(m.label, m.outcomes[1], m.token_ids[1], Decimal(str(budget)),
                                           Decimal(str(no_ask)), qty_no, m.tick_size)])[0]
        if step(f"Buy {qty_no:g} NO at {no_ask * 100:.1f}¢ (sent as YES at {(1 - no_ask) * 100:.1f}¢)", buy_no,
                start - qty_no):
            floor = max(0.01, round(no_bid - 0.02, 2))
            step(f"Sell {qty_no:g} NO back (at least {floor * 100:.0f}¢)",
                 exchange.sell(m.token_ids[1], qty_no, floor), start)

    ok = len(steps) == 4 and all(s["ok"] for s in steps)
    if ok:
        verdict = "Orders work: YES and NO both filled at the expected prices and were sold back."
    elif any(s["step"].startswith("Buy") and "NO" in s["step"] and not s["ok"] for s in steps):
        verdict = ("The NO order didn't fill as expected. Live trading of NO legs isn't safe until this is "
                   "understood; send me the details.")
    else:
        verdict = "The test didn't complete. Check your Polymarket US positions, and send me the details."
    leftover = exchange.position(m.slug)
    return {"ok": ok, "verdict": verdict, "market": m.label or m.question, "market_slug": m.slug, "url": m.url,
            "steps": steps, "position_left": leftover, "position_before": start}
