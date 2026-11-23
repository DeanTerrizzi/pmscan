"""Polymarket US liquidity rewards: what a small resting quote would earn right now.

Polymarket US pays reward pools for resting limit orders (docs.polymarket.us/incentives/liquidity.md).
Every second the exchange scores each order on a market's book as

    DiscountFactor ** (ticks from the best price) * size

counting orders from the best price outward only until the side holds Target Size contracts.
Each side's scores are normalised to 1 per second, so an order's pay is its share of its side's
score times that side's slice of the period's pool. A second pays nobody when a side never reaches
Target Size, or (for programs with a Max Spread) when the two sides' Target-Size prices sit more
than 2 × Max Spread apart.

This module estimates, from the live books, what a quote of a fixed dollar budget would earn
per hour on each rewarded market, and what it puts at risk. The program list comes from the
authenticated incentives API, so it needs a Polymarket US API key.

A period's pool belongs to the whole program: "the total amount paid out for a time period, shared
across the program's markets — never summed per market" (polymarket.us/rewards). The API repeats the
program's pool on every market, so each market's slice is pool / (markets in the program). Each
market's second is normalised per side, so we assume every market gets an equal slice and each side
half of it; that split isn't documented.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import env
from .config import RewardsConfig

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PROGRAMS_TTL_S = 15 * 60
PAGE_SIZE = 500
DAY_OF_HOURS = 6.0  # "Day-of / Pre-game: from 6 hours before until the event starts"
DEFAULT_LIVE_HOURS = 3.0
# Rough length of the live period (event start to settlement) by sport; the API gives no end time.
LIVE_HOURS = {
    "FOOTBALL": 3.5, "BASKETBALL": 2.5, "SOCCER": 2.0, "HOCKEY": 2.5, "ICEHOCKEY": 2.5,
    "BASEBALL": 3.0, "TENNIS": 2.5, "TABLETENIS": 1.0, "EFOOTBALL": 0.25, "EBASKETBALL": 0.25,
    "MMA": 3.0, "BOXING": 3.0, "MOTORSPORT": 2.0, "GOLF": 5.0, "CRICKET": 4.0, "RUGBY": 2.0,
}
NON_EVENT_LIVE_HOURS = 24.0  # a "live" period on a market without a game (weather, …): assume a day
SIDE_SHARE = 0.5  # each second the bid and ask sides are each normalised to 1: assume an even split
CATEGORY_NAMES = {"SPR": "Sports", "POL": "Politics", "CRY": "Crypto", "CUL": "Culture", "CLI": "Climate",
                  "ECO": "Economics", "FIN": "Finance", "TEC": "Tech", "": "Other"}


# ---------- programs ----------

def _ts(value: Any) -> float | None:
    if not value:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(value).strip()).replace("Z", "+00:00")  # ns → µs
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("+00"):
        text += ":00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Period:
    program_id: str
    period: str  # early | day_of | live | daily | daily_event
    pool: float  # USD for the whole period
    discount: float  # DiscountFactor; 1 = no penalty for distance
    target: float  # Target Size per side, in contracts (0 = no cut-off)
    max_spread: float | None  # dollars from the midpoint; None = no check
    start: float | None
    end: float | None
    status: str

    @property
    def program(self) -> str:
        """The program without its date suffix (nba_regular_player_props_live_20261118 → …_live)."""
        head, _, tail = self.program_id.rpartition("_")
        return head if head and tail.isdigit() else self.program_id


@dataclass(slots=True)
class Program:
    slug: str
    category: str
    subcategory: str
    product: str
    event_start: float | None
    open: bool
    periods: list[Period]


def parse_program(raw: dict) -> Program:
    periods = []
    for t in raw.get("timePeriods") or []:
        if t.get("programType", "liquidityProgram") != "liquidityProgram":
            continue
        periods.append(Period(
            program_id=str(t.get("programId") or ""),
            period=str(t.get("period") or ""),
            pool=_num(t.get("rewardPool")) or 0.0,
            discount=_num(t.get("discountFactor")) or 1.0,
            target=_num(t.get("targetSize")) or 0.0,
            max_spread=_num(t.get("maxSpread")) or None,  # omitted (never 0) when there's none
            start=_ts(t.get("start")),
            end=_ts(t.get("end")),
            status=str(t.get("status") or ""),
        ))
    return Program(
        slug=str(raw.get("marketSlug") or ""),
        category=str(raw.get("category") or ""),
        subcategory=str(raw.get("subcategory") or ""),
        product=str(raw.get("instrumentProduct") or ""),
        event_start=_ts(raw.get("eventStartTime")),
        open=raw.get("instrumentState", "INSTRUMENT_STATE_OPEN") == "INSTRUMENT_STATE_OPEN",
        periods=periods,
    )


def fetch_programs(sdk_client: Any, statuses: Sequence[str] = ("active",), max_pages: int = 1000,
                   progress: Callable[[int], None] | None = None) -> list[dict]:
    """Every liquidity program from GET /v1/incentives (authenticated; 5 requests/s)."""
    programs: list[dict] = []
    token = None
    for _ in range(max_pages):
        query: dict[str, Any] = {"page_size": PAGE_SIZE, "statuses": list(statuses),
                                 "program_type": "liquidityProgram"}
        if token:
            query["page_token"] = token
        started = time.monotonic()
        data = sdk_client.get("/v1/incentives", query=query, authenticated=True) or {}
        programs.extend(data.get("programs") or [])
        if progress:
            progress(len(programs))
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(max(0.0, 0.21 - (time.monotonic() - started)))
    return programs


def fetch_earnings(sdk_client: Any) -> list[dict]:
    """Your own liquidity rewards by market and day (PAID / PENDING / SKIPPED)."""
    data = sdk_client.get("/v1/incentives/earnings", query={"program_type": "liquidityProgram"},
                          authenticated=True) or {}
    return list(data.get("rewards") or [])


def sdk_from_env() -> Any:
    """An authenticated Polymarket US SDK client from the US key in the environment."""
    from .us import UsExchange

    key_id, secret = env.get(env.US_KEY_ID) or "", env.get(env.US_SECRET_KEY) or ""
    if not key_id or not secret:
        raise RuntimeError("The rewards scanner reads Polymarket US's reward programs, which needs your API key "
                           f"({env.US_KEY_ID} and {env.US_SECRET_KEY}, or connect it in the Mac app).")
    return UsExchange.connect(key_id, secret).client


# ---------- when a period pays, and how fast ----------

def _day_start_et(now: float) -> float:
    d = datetime.fromtimestamp(now, ET)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def window(period: Period, program: Program, now: float) -> tuple[float, float] | None:
    """When the period's pool accrues (start, end) as Unix times; None when it can't be placed."""
    es = program.event_start
    if period.period in ("daily", "daily_event"):
        day = _day_start_et(now)
        nxt = (datetime.fromtimestamp(day, ET) + timedelta(days=1)).timestamp()
        return day, nxt
    if period.period == "live":
        if es is None:
            start = period.start or now
            return start, start + NON_EVENT_LIVE_HOURS * 3600
        hours = LIVE_HOURS.get(program.subcategory, DEFAULT_LIVE_HOURS) if program.category == "SPR" \
            else NON_EVENT_LIVE_HOURS
        return es, (period.end or es + hours * 3600)
    if es is None:
        return None
    if period.period == "day_of":
        return es - DAY_OF_HOURS * 3600, es
    if period.period == "early":
        end = es - DAY_OF_HOURS * 3600
        start = period.start or end - 86400
        return (start, end) if end - start >= 3600 else None
    return None


# ---------- scoring a side of the book ----------

@dataclass(slots=True)
class SideScore:
    qualifies: bool  # the side reaches Target Size
    ours: float  # our score
    total: float  # everyone's score, ours included
    landing: float | None  # price where the walk to Target Size lands
    depth: float  # contracts on the side, ours included

    @property
    def share(self) -> float:
        return self.ours / self.total if self.qualifies and self.total > 0 else 0.0


def score_side(levels: Sequence[tuple[float, float]], bid: bool, tick: float, discount: float, target: float,
               ours: tuple[float, float] | None = None) -> SideScore:
    """Score one side of a book (levels best first) with our order (price, size) added."""
    merged: dict[float, list[float]] = {}
    for price, qty in levels:
        if qty > 0:
            merged.setdefault(round(price, 6), [0.0, 0.0])[0] += qty
    if ours and ours[1] > 0:
        merged.setdefault(round(ours[0], 6), [0.0, 0.0])[1] += ours[1]
    if not merged:
        return SideScore(False, 0.0, 0.0, None, 0.0)
    prices = sorted(merged, reverse=bid)
    best, tick = prices[0], tick or 0.01
    mine = total = cum = 0.0
    landing = None
    for price in prices:
        others, us = merged[price]
        weight = discount ** round(abs(best - price) / tick)
        mine += weight * us
        total += weight * (others + us)
        cum += others + us
        if target > 0 and cum >= target - 1e-9:
            landing = price
            break
    qualifies = target <= 0 or landing is not None
    if target <= 0:
        landing = prices[-1]
    return SideScore(qualifies, mine, total, landing, cum)


def spread_ok(bid: SideScore, ask: SideScore, max_spread: float | None) -> bool:
    """Max Spread: both Target-Size prices within max_spread of their midpoint (or no check)."""
    if max_spread is None:
        return True
    if not (bid.qualifies and ask.qualifies) or bid.landing is None or ask.landing is None:
        return False
    return ask.landing - bid.landing <= 2 * max_spread + 1e-9


# ---------- the estimate ----------

@dataclass(slots=True)
class Quote:
    side: str  # "bid" (buy YES) or "ask" (sell YES = buy NO)
    price: float  # YES price of the order
    size: float  # contracts
    capital: float  # collateral it ties up = the most it can lose if filled
    ticks_behind: int  # 0 = at the best price
    from_mid: float | None  # distance from the midpoint, dollars
    share: float  # our share of the side's score
    per_hour: float  # USD per hour while the period pays
    competition: float  # others' score on this side, in best-price contracts


@dataclass(slots=True)
class Estimate:
    slug: str
    title: str
    outcome: str  # what YES means (a strike, a team), when the question alone doesn't say
    event_title: str
    category: str
    subcategory: str
    product: str
    program: str
    program_markets: int  # markets in this program (if the pool is shared, it's split across them)
    period: str
    pool: float
    window_start: float
    window_end: float
    paying_now: bool
    market_pool: float  # this market's slice of the program's pool (pool / program_markets)
    rate_per_hour: float  # that slice per hour of the period
    discount: float
    target: float
    max_spread: float | None
    bid_depth: float
    ask_depth: float
    qualifies: bool  # the book as it stands pays out each second
    reason: str  # why it doesn't, when it doesn't
    best_bid: float | None
    best_ask: float | None
    tick: float
    step: float  # smallest order size step
    quote: Quote | None
    per_hour: float  # what the best quote earns per hour while the period pays
    period_total: float  # the best quote held for the rest of the period
    url: str = ""

    @property
    def per_day_per_100(self) -> float:
        if not self.quote or self.quote.capital <= 0:
            return 0.0
        return self.per_hour * 24 * 100 / self.quote.capital


def _cost(side: str, price: float) -> float:
    return price if side == "bid" else 1.0 - price


def _floor(x: float, step: float) -> float:
    step = step or 0.01
    return math.floor(round(x / step, 9)) * step


def best_quote(bids: Sequence[tuple[float, float]], asks: Sequence[tuple[float, float]], period: Period,
               tick: float, step: float, budget: float, rate_per_hour: float, max_behind: int = 3,
               improve: bool = False, sides: Iterable[str] = ("bid", "ask")) -> tuple[Quote | None, str]:
    """The quote of about `budget` dollars that earns most per hour, and why none does."""
    tick = tick or 0.01
    base_bid = score_side(bids, True, tick, period.discount, period.target)
    base_ask = score_side(asks, False, tick, period.discount, period.target)
    best: Quote | None = None
    reasons: Counter[str] = Counter()
    bb = bids[0][0] if bids else None
    ba = asks[0][0] if asks else None
    mid = (bb + ba) / 2 if bb is not None and ba is not None else None
    for side in sides:
        levels, is_bid = (bids, True) if side == "bid" else (asks, False)
        if not levels:
            reasons["one side of the book is empty"] += 1
            continue
        top = levels[0][0]
        for k in range(-1 if improve else 0, max_behind + 1):
            price = round(top - k * tick if is_bid else top + k * tick, 6)
            if not tick - 1e-9 <= price <= 1 - tick + 1e-9:
                continue
            if k < 0 and ((is_bid and ba is not None and price >= ba - 1e-9) or
                          (not is_bid and bb is not None and price <= bb + 1e-9)):
                continue  # would cross the spread
            cost = _cost(side, price)
            size = _floor(budget / cost, step) if cost > 0 else 0.0
            if size <= 0:
                continue
            mine = score_side(levels, is_bid, tick, period.discount, period.target, (price, size))
            other = base_ask if is_bid else base_bid
            if not mine.qualifies:
                reasons["a side never reaches Target Size"] += 1
                continue
            pair = (mine, other) if is_bid else (other, mine)
            if not spread_ok(pair[0], pair[1], period.max_spread):
                reasons["the book is wider than the Max Spread"] += 1
                continue
            if mine.ours <= 0:
                reasons["beyond Target Size: the order wouldn't score"] += 1
                continue
            share = mine.share
            q = Quote(side=side, price=price, size=round(size, 4), capital=round(size * cost, 2), ticks_behind=k,
                      from_mid=(abs(price - mid) if mid is not None else None), share=share,
                      per_hour=rate_per_hour * SIDE_SHARE * share, competition=mine.total - mine.ours)
            if best is None or q.per_hour > best.per_hour + 1e-12:
                best = q
    reason = "" if best else (reasons.most_common(1)[0][0] if reasons else "no room for a quote")
    return best, reason


def book_levels(raw: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]], bool]:
    """(bids best first, asks best first, open) from a gateway /book response; YES prices."""
    data = raw.get("marketData") or raw

    def levels(rows: Any, descending: bool) -> list[tuple[float, float]]:
        out = []
        for r in rows or []:
            p = _num((r.get("px") or {}).get("value") if isinstance(r.get("px"), dict) else r.get("px"))
            q = _num(r.get("qty"))
            if p is not None and q:
                out.append((p, q))
        return sorted(out, key=lambda x: -x[0] if descending else x[0])

    return levels(data.get("bids"), True), levels(data.get("offers"), False), \
        data.get("state", "MARKET_STATE_OPEN") == "MARKET_STATE_OPEN"


def yes_label(info: dict) -> str:
    """What a YES position on the market is (its short title, or the first named outcome)."""
    title, question = str(info.get("title") or ""), str(info.get("question") or "")
    if title and title != question:
        return title
    try:
        outcomes = json.loads(info.get("outcomes") or "[]")
    except (TypeError, ValueError):
        outcomes = []
    return str(outcomes[0]) if outcomes and str(outcomes[0]).lower() != "yes" else ""


def estimate(program: Program, period: Period, raw_book: dict, info: dict, now: float, budget: float,
             program_markets: int, max_behind: int = 3, improve: bool = False) -> Estimate | None:
    win = window(period, program, now)
    if win is None:
        return None
    start, end = win
    hours = max((end - start) / 3600, 1 / 60)
    market_pool = period.pool / max(program_markets, 1)
    rate = market_pool / hours
    bids, asks, is_open = book_levels(raw_book)
    tick = _num(info.get("orderPriceMinTickSize")) or 0.01
    step = _num(info.get("minimumTradeQty")) or 0.01
    quote, reason = (best_quote(bids, asks, period, tick, step, budget, rate, max_behind, improve)
                     if is_open else (None, "the market isn't open for trading"))
    base_bid = score_side(bids, True, tick, period.discount, period.target)
    base_ask = score_side(asks, False, tick, period.discount, period.target)
    qualifies = base_bid.qualifies and base_ask.qualifies and spread_ok(base_bid, base_ask, period.max_spread) \
        if period.max_spread is not None else (base_bid.qualifies or base_ask.qualifies)
    per_hour = quote.per_hour if quote else 0.0
    remaining = max(0.0, end - max(now, start)) / 3600
    event_slug = ((info.get("events") or [{}])[0] or {}).get("slug") or info.get("eventSlug") or ""
    return Estimate(
        slug=program.slug, title=str(info.get("question") or info.get("title") or program.slug),
        outcome=yes_label(info),
        event_title=str(((info.get("events") or [{}])[0] or {}).get("title") or ""),
        category=program.category, subcategory=program.subcategory, product=program.product,
        program=period.program, program_markets=program_markets, period=period.period, pool=period.pool,
        window_start=start, window_end=end, paying_now=start <= now < end, market_pool=market_pool,
        rate_per_hour=rate,
        discount=period.discount, target=period.target, max_spread=period.max_spread,
        bid_depth=base_bid.depth, ask_depth=base_ask.depth, qualifies=qualifies,
        reason="" if qualifies else ("pays only with your quote added" if quote else
                                     (reason or "the book doesn't qualify")),
        best_bid=bids[0][0] if bids else None, best_ask=asks[0][0] if asks else None, tick=tick, step=step,
        quote=quote, per_hour=per_hour, period_total=per_hour * remaining,
        url=f"https://polymarket.us/event/{event_slug}" if event_slug else "",
    )


# ---------- the scan ----------

@dataclass
class ScanResult:
    generated_at: float
    settings: dict
    programs: int  # markets with an active liquidity program
    pools_total: float  # sum of the active programs' pools (each program counted once)
    candidates: int
    books_read: int
    estimates: list[dict]
    by_category: list[dict] = field(default_factory=list)
    earnings: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def program_sizes(programs: Sequence[Program]) -> Counter[str]:
    """How many markets share each program's pool (every market listed with an active period)."""
    sizes: Counter[str] = Counter()
    for prog in programs:
        for per in {p.program_id for p in prog.periods if p.status == "active"}:
            sizes[per] += 1
    return sizes


def candidates(programs: Sequence[Program], now: float, horizon_hours: float, per_program: int,
               limit: int) -> tuple[list[tuple[Program, Period, float]], Counter[str]]:
    """(program, period, this market's pool per hour) for periods paying now or soon, best first.

    Also returns how many markets share each program's pool."""
    sizes = program_sizes(programs)
    rows = []
    for prog in programs:
        if not prog.open:
            continue
        for per in prog.periods:
            if per.status != "active" or per.pool <= 0:
                continue
            win = window(per, prog, now)
            if win is None or win[1] <= now or win[0] > now + horizon_hours * 3600:
                continue
            rate = per.pool / max(sizes[per.program_id], 1) / max((win[1] - win[0]) / 3600, 1 / 60)
            rows.append((prog, per, rate))
    rows.sort(key=lambda r: -r[2])
    taken: Counter[str] = Counter()
    picked, seen = [], set()
    for prog, per, rate in rows:
        key = (prog.slug, per.period)
        if key in seen or taken[per.program_id] >= per_program:
            continue
        seen.add(key)
        taken[per.program_id] += 1
        picked.append((prog, per, rate))
        if len(picked) >= limit:
            break
    return picked, sizes


class RewardsScanner:
    """Reads the reward programs (with your key) and the live books (public), and ranks the quotes."""

    def __init__(self, data_dir: Path, client: Any = None, sdk: Any = None) -> None:
        self.dir = Path(data_dir) / "rewards"
        self.client = client
        self._sdk = sdk

    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            self._sdk = sdk_from_env()
        return self._sdk

    def _client(self) -> Any:
        if self.client is None:
            from .us import UsClient

            self.client = UsClient(rate=15.0)
        return self.client

    def programs(self, refresh: bool = False, progress: Callable[[str], None] | None = None) -> list[dict]:
        path = self.dir / "programs.json"
        if not refresh and path.exists() and time.time() - path.stat().st_mtime < PROGRAMS_TTL_S:
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        raw = fetch_programs(self.sdk, progress=(lambda n: progress(f"Reading reward programs… {n:,}"))
                             if progress else None)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw))
        tmp.replace(path)
        return raw

    def market_info(self, slugs: Sequence[str]) -> dict[str, dict]:
        """Title, tick size and trade step for each market (public, 50 per request)."""
        client = self._client()
        out: dict[str, dict] = {}
        for i in range(0, len(slugs), 50):
            chunk = list(slugs[i:i + 50])
            try:
                data = client._get("/v1/markets", slug=chunk, limit=len(chunk))
            except httpx.HTTPError as exc:
                log.warning("market lookup failed: %s", exc)
                continue
            for m in data.get("markets") or []:
                out[str(m.get("slug"))] = m
        return out

    def books(self, slugs: Sequence[str], workers: int = 6) -> dict[str, dict]:
        from concurrent.futures import ThreadPoolExecutor

        client = self._client()

        def fetch(slug: str) -> tuple[str, dict | None]:
            try:
                return slug, client._get(f"/v1/markets/{slug}/book")
            except httpx.HTTPError as exc:
                log.warning("book for %s failed: %s", slug, exc)
                return slug, None

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(slugs)))) as pool:
            return {s: raw for s, raw in pool.map(fetch, slugs) if raw is not None}

    def scan(self, settings: RewardsConfig | None = None, refresh: bool = False,
             progress: Callable[[str], None] | None = None, now: float | None = None) -> ScanResult:
        settings = settings or RewardsConfig()
        now = now or time.time()
        say = progress or (lambda _m: None)
        raw = self.programs(refresh=refresh, progress=say)
        programs = [parse_program(r) for r in raw]
        pools = {p.program_id: p.pool for prog in programs for p in prog.periods if p.status == "active"}
        pools_total = sum(pools.values())
        self.sizes = sizes = program_sizes(programs)
        self.by_slug = {prog.slug: prog for prog in programs}
        picked, _ = candidates(programs, now, settings.horizon_hours, settings.per_program, settings.max_books)
        slugs = list(dict.fromkeys(prog.slug for prog, _, _ in picked))
        say(f"Reading {len(slugs)} order books…")
        info = self.market_info(slugs)
        books = self.books(slugs)
        estimates = []
        for prog, per, _rate in picked:
            if prog.slug not in books:
                continue
            e = estimate(prog, per, books[prog.slug], info.get(prog.slug, {}), now, settings.budget_usd,
                         sizes[per.program_id], settings.max_ticks_behind)
            if e:
                estimates.append(e)
        estimates.sort(key=lambda e: (not e.paying_now, -e.per_hour, -e.rate_per_hour))
        try:
            earnings = summarize_earnings(fetch_earnings(self.sdk))
        except Exception as exc:  # noqa: BLE001 - earnings are a nice-to-have
            log.warning("earnings lookup failed: %s", exc)
            earnings = {"error": str(exc)}
        result = ScanResult(
            generated_at=now, settings=asdict(settings), programs=len(programs), pools_total=round(pools_total, 2),
            candidates=len(picked), books_read=len(books), estimates=[to_row(e) for e in estimates],
            by_category=by_category(estimates), earnings=earnings,
            notes=[
                "Estimates assume the book stays as it is now. For periods that haven't started, the "
                "book then (especially during live games) may look very different.",
                "A program's pool is shared by all its markets (polymarket.us/rewards); each market is "
                "assumed to get an equal slice, and each side of its book half of that slice.",
                "A filled quote becomes a position you hold until you sell it or the market settles: the "
                "capital shown is the most it can lose.",
            ],
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "scan.tmp"
        tmp.write_text(json.dumps(asdict(result)))
        tmp.replace(self.dir / "scan.json")
        return result

    def latest(self) -> dict | None:
        path = self.dir / "scan.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None


def to_row(e: Estimate) -> dict:
    row = asdict(e)
    row["per_day_per_100"] = e.per_day_per_100
    return row


def by_category(estimates: Sequence[Estimate]) -> list[dict]:
    groups: dict[str, dict] = {}
    for e in estimates:
        g = groups.setdefault(e.category, {"category": e.category, "name": CATEGORY_NAMES.get(e.category, e.category),
                                           "markets": 0, "paying": 0, "best_per_hour": 0.0})
        g["markets"] += 1
        g["paying"] += 1 if e.per_hour > 0 else 0
        g["best_per_hour"] = max(g["best_per_hour"], e.per_hour)
    return sorted(groups.values(), key=lambda g: -g["best_per_hour"])


def summarize_earnings(rewards: Sequence[dict]) -> dict:
    total: Counter[str] = Counter()
    for r in rewards:
        total[str(r.get("status") or "")] += _num(r.get("reward")) or 0.0
    return {"paid": round(total["PAID"], 2), "pending": round(total["PENDING"], 2),
            "skipped": round(total["SKIPPED"], 2), "days": len({r.get("date") for r in rewards}),
            "markets": len({r.get("marketSlug") for r in rewards})}


# ---------- paper test ----------

@dataclass
class PaperQuote:
    """A simulated resting quote: it re-joins the best price each tick until it fills or its period ends."""

    id: str
    slug: str
    title: str
    url: str
    program: str
    program_id: str
    period: str
    side: str  # bid = buy YES, ask = sell YES (buy NO)
    price: float  # YES price, now
    size: float
    budget: float
    tick: float
    step: float
    ticks_behind: int
    discount: float
    target: float
    max_spread: float | None
    rate_per_hour: float  # this market's slice of the pool, per hour
    window_end: float
    placed_at: float
    last_tick: float
    status: str = "quoting"  # quoting → filled → settled, or quoting → ended
    accrued: float = 0.0  # estimated rewards
    seconds_paid: float = 0.0  # seconds the quote scored
    seconds: float = 0.0  # seconds it rested
    fill_price: float | None = None
    filled_at: float | None = None
    cost: float = 0.0  # what the fill paid (maker: no fee)
    mark: float | None = None  # current value of the filled position
    value: float | None = None  # settlement value
    closed_at: float | None = None
    note: str = ""
    by_day: dict[str, float] = field(default_factory=dict)  # ET date -> accrued that day

    @property
    def pnl(self) -> float:
        """Fill result (settled or marked) plus rewards."""
        if self.fill_price is None:
            return self.accrued
        held = self.value if self.value is not None else (self.mark if self.mark is not None else self.cost)
        return held - self.cost + self.accrued

    @property
    def payable(self) -> float:
        """Rewards that clear the $1 minimum payout (assumed per market per day)."""
        return sum(v for v in self.by_day.values() if v >= 1.0)


def _trade(raw: dict) -> tuple[float | None, float | None]:
    stats = (raw.get("marketData") or raw).get("stats") or {}
    px = stats.get("lastTradePx")
    return _num(px.get("value") if isinstance(px, dict) else px), _ts(stats.get("lastTradeSetTime"))


def _et_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


class RewardsPaper:
    """Paper-tests the scanner's best quotes: estimated rewards against what fills would have cost.

    Fills are judged pessimistically: a quote counts as filled in full as soon as a trade prints at
    or through its price, or the other side of the book reaches it. A filled quote is held until the
    market settles (no hedging), so the test shows the rewards and the fill risk side by side.
    """

    MAX_GAP_S = 300.0  # don't credit rewards across a gap longer than this (the app was closed)

    def __init__(self, data_dir: Path, config: RewardsConfig, scanner: RewardsScanner | None = None) -> None:
        self.dir = Path(data_dir) / "rewards"
        self.config = config
        self.scanner = scanner or RewardsScanner(data_dir)
        self.path = self.dir / "paper.json"
        self.started_at: float | None = None
        self.quotes: list[PaperQuote] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.started_at = data.get("started_at")
        self.quotes = [PaperQuote(**q) for q in data.get("quotes", [])]

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"started_at": self.started_at, "quotes": [asdict(q) for q in self.quotes]}))
        tmp.replace(self.path)

    def reset(self) -> None:
        if self.path.exists():
            self.path.rename(self.path.with_name(f"paper-{int(time.time())}.json"))
        self.started_at, self.quotes = None, []

    # -- opening quotes --

    def open_from(self, scan: dict, now: float) -> list[PaperQuote]:
        """Open quotes on the scan's best paying markets, up to max_quotes, one per program."""
        active = [q for q in self.quotes if q.status == "quoting"]
        used_slugs = {q.slug for q in self.quotes if q.status in ("quoting", "filled")}
        used_programs = {q.program_id for q in active}
        opened = []
        rows = sorted((e for e in scan.get("estimates") or [] if e.get("paying_now") and e.get("quote")),
                      key=lambda e: -e["per_hour"])
        for e in rows:
            if len(active) + len(opened) >= self.config.max_quotes:
                break
            pid = f"{e['program']}|{e['period']}"
            if e["slug"] in used_slugs or pid in used_programs or e["per_hour"] < self.config.min_per_hour:
                continue
            if e["period"] == "live" and not self.config.paper_live:
                continue  # a quote resting through live play is filled the moment the price jumps
            if any(k.lower() in e["program"].lower() for k in self.config.paper_skip_programs):
                continue  # tracks a fast-moving price: fills arrive just before it moves against you
            if e["window_end"] <= now:
                continue
            q = e["quote"]
            quote = PaperQuote(
                id=f"{e['slug']}:{q['side']}:{int(now)}", slug=e["slug"], title=e["title"], url=e.get("url", ""),
                program=e["program"], program_id=pid, period=e["period"], side=q["side"], price=q["price"],
                size=q["size"], budget=q["capital"], tick=e["tick"], step=e.get("step") or 0.01,
                ticks_behind=int(q.get("ticks_behind") or 0), discount=e["discount"],
                target=e["target"], max_spread=e["max_spread"], rate_per_hour=e["rate_per_hour"],
                window_end=e["window_end"], placed_at=now, last_tick=now)
            opened.append(quote)
            used_slugs.add(e["slug"])
            used_programs.add(pid)
        if opened:
            self.started_at = self.started_at or now
            self.quotes.extend(opened)
        return opened

    # -- each tick --

    def tick(self, books: dict[str, dict], now: float, resolutions: dict[str, float] | None = None) -> list[str]:
        """Advance every quote by one book snapshot; returns messages about fills and settlements."""
        messages = []
        for q in self.quotes:
            if q.status == "quoting":
                messages += self._tick_quote(q, books.get(q.slug), now)
            elif q.status == "filled":
                messages += self._tick_position(q, books.get(q.slug), now, (resolutions or {}).get(q.slug))
        return messages

    def _tick_quote(self, q: PaperQuote, raw: dict | None, now: float) -> list[str]:
        if raw is None:
            q.last_tick = now
            return []
        bids, asks, is_open = book_levels(raw)
        dt = min(max(0.0, now - q.last_tick), self.MAX_GAP_S)
        end = min(now, q.window_end)
        dt = min(dt, max(0.0, end - q.last_tick))
        is_bid = q.side == "bid"
        # Credit the interval just ended at the price the quote rested at.
        own, other = (bids, asks) if is_bid else (asks, bids)
        mine = score_side(own, is_bid, q.tick, q.discount, q.target, (q.price, q.size))
        rest = score_side(other, not is_bid, q.tick, q.discount, q.target)
        pair = (mine, rest) if is_bid else (rest, mine)
        if dt > 0:
            q.seconds += dt
            if mine.qualifies and spread_ok(pair[0], pair[1], q.max_spread) and mine.ours > 0:
                earned = q.rate_per_hour * SIDE_SHARE * mine.share * dt / 3600
                q.accrued += earned
                q.seconds_paid += dt
                day = _et_date(now)
                q.by_day[day] = q.by_day.get(day, 0.0) + earned
        # Did it fill?
        px, at = _trade(raw)
        traded_through = px is not None and at is not None and at > q.last_tick and \
            (px <= q.price + 1e-9 if is_bid else px >= q.price - 1e-9)
        crossed = (asks and asks[0][0] <= q.price + 1e-9) if is_bid else (bids and bids[0][0] >= q.price - 1e-9)
        q.last_tick = now
        if traded_through or crossed:
            q.status, q.fill_price, q.filled_at = "filled", q.price, now
            q.cost = q.size * (q.price if is_bid else 1 - q.price)
            q.mark = q.cost
            return [f"rewards paper: {q.side} on {q.title} filled at {q.price:.3f} ({q.size:g} contracts)"]
        if not is_open or now >= q.window_end:
            q.status, q.closed_at = "ended", now
            q.note = "period ended" if now >= q.window_end else "market closed"
            return []
        # Follow the best price at the same distance (without crossing), as a quoting bot would.
        if own:
            price = round(own[0][0] - q.ticks_behind * q.tick if is_bid else own[0][0] + q.ticks_behind * q.tick, 6)
            if q.tick - 1e-9 <= price <= 1 - q.tick + 1e-9:
                q.price = price
            cost = q.price if is_bid else 1 - q.price
            q.size = _floor(q.budget / cost, q.step) if cost > 0 else q.size
        return []

    def _tick_position(self, q: PaperQuote, raw: dict | None, now: float, settled_yes: float | None) -> list[str]:
        yes = q.side == "bid"
        if settled_yes is not None:
            q.value = q.size * (settled_yes if yes else 1 - settled_yes)
            q.mark, q.status, q.closed_at = q.value, "settled", now
            return [f"rewards paper: {q.title} settled, fill P&L ${q.value - q.cost:+.2f}, "
                    f"rewards ${q.accrued:.2f}"]
        if raw is not None:
            bids, asks, _ = book_levels(raw)
            if bids and asks:
                mid = (bids[0][0] + asks[0][0]) / 2
                q.mark = q.size * (mid if yes else 1 - mid)
        return []

    def slugs(self) -> list[str]:
        return [q.slug for q in self.quotes if q.status in ("quoting", "filled")]

    def summary(self) -> dict:
        quotes = sorted(self.quotes, key=lambda q: (q.status not in ("quoting", "filled"), -q.placed_at))
        filled = [q for q in self.quotes if q.fill_price is not None]
        rested = sum(q.seconds for q in self.quotes)
        return {
            "started_at": self.started_at,
            "rewards": round(sum(q.accrued for q in self.quotes), 4),
            "payable": round(sum(q.payable for q in self.quotes), 2),
            "fill_pnl": round(sum(q.pnl - q.accrued for q in filled), 4),
            "pnl": round(sum(q.pnl for q in self.quotes), 4),
            "capital": round(sum(q.budget for q in self.quotes if q.status in ("quoting", "filled")), 2),
            "quoting": sum(q.status == "quoting" for q in self.quotes),
            "holding": sum(q.status == "filled" for q in self.quotes),
            "fills": len(filled),
            "quotes": len(self.quotes),
            "hours_quoted": round(rested / 3600, 2),
            "paid_fraction": round(sum(q.seconds_paid for q in self.quotes) / rested, 3) if rested else None,
            "rows": [{**asdict(q), "pnl": q.pnl, "payable": q.payable} for q in quotes[:200]],
        }


class RewardsService:
    """Background scans and paper ticks for the dashboard (Polymarket US with an API key only)."""

    def __init__(self, data_dir: Path, config: RewardsConfig, client: Any = None) -> None:
        self.data_dir = data_dir
        self.config = config
        if client is None:
            from .us import UsClient

            client = UsClient(rate=6.0)  # gentle: it shares the 25 requests/s limit with the main scanner
        self.scanner = RewardsScanner(data_dir, client)
        self.paper = RewardsPaper(data_dir, config, self.scanner)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.scanning = False
        self.message = ""
        self.error = ""
        self.messages: list[str] = []
        self.last_scan_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def has_key() -> bool:
        return bool(env.get(env.US_KEY_ID) and env.get(env.US_SECRET_KEY))

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="pmscan-rewards", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def scan_now(self) -> bool:
        if self.scanning:
            return False
        self.last_scan_at = 0.0
        self.start()
        self._wake.set()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            if self.has_key() and now - self.last_scan_at >= self.config.rescan_minutes * 60:
                self._scan()
            if self.has_key():
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.warning("rewards paper tick failed: %s", exc)
            self._wake.wait(self.config.tick_seconds)
            self._wake.clear()

    def _scan(self) -> None:
        self.scanning, self.error = True, ""
        try:
            result = self.scanner.scan(self.config, progress=self._progress)
            with self._lock:
                opened = self.paper.open_from(asdict(result), time.time())
                self.paper.save()
            self._note([f"rewards paper: quoting {q.side} on {q.title} at {q.price:.3f}" for q in opened])
        except Exception as exc:  # noqa: BLE001 - shown in the dashboard
            log.warning("rewards scan failed: %s", exc)
            self.error = str(exc)
        finally:
            self.scanning, self.message = False, ""
            self.last_scan_at = time.time()

    def _tick(self) -> None:
        slugs = self.paper.slugs()
        if not slugs:
            return
        books = self.scanner.books(slugs, workers=2)
        holding = [q.slug for q in self.paper.quotes if q.status == "filled"]
        resolutions: dict[str, float] = {}
        if holding:
            try:
                for slug, prices in (self.scanner._client().resolutions(holding) or {}).items():
                    if prices:
                        resolutions[slug] = float(prices[0])
            except Exception as exc:  # noqa: BLE001
                log.warning("rewards paper settlement check failed: %s", exc)
        with self._lock:
            messages = self.paper.tick(books, time.time(), resolutions)
            self.paper.save()
        self._note(messages)

    def _note(self, messages: list[str]) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.messages = (self.messages + [f"{stamp} {m}" for m in messages])[-50:]

    def _progress(self, message: str) -> None:
        self.message = message

    def reset_paper(self) -> None:
        with self._lock:
            self.paper.reset()
            self.paper.save()

    def snapshot(self) -> dict:
        with self._lock:
            paper = self.paper.summary()
        return {"has_key": self.has_key(), "scanning": self.scanning, "message": self.message, "error": self.error,
                "next_scan_at": (self.last_scan_at + self.config.rescan_minutes * 60) if self.last_scan_at else None,
                "messages": self.messages[-20:], "settings": asdict(self.config),
                "result": self.scanner.latest(), "paper": paper}
