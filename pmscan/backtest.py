"""Backtest of the 90¢+ (high-probability) strategy on settled Polymarket US markets.

The question it answers: if you had bought every contract priced 90-99.5¢ some time before
the market ended, how often would it have paid, and would that have made money after fees?

How it works:

1. **Sample settled markets** in each group (game winners, soccer, season futures, other
   sports such as player props, crypto, politics, ...). Polymarket US has well over a million
   settled markets, mostly player props and 15-minute crypto markets, so each group is
   sampled at random offsets instead of read whole; otherwise props would drown out
   everything else.
2. **Fetch each market's hourly price history** (YES = best ask, NO = 1 - best bid, i.e.
   what you'd pay to buy either side) and keep only the points where the price changed.
3. **Simulate entries** at several horizons before the market's end: 1 hour, 6 hours, 1 day,
   3 days and 7 days. "End" is the game's start for game markets (the strategy doesn't buy
   during play) and the scheduled end otherwise, but never later than the last price change.
   After a market settles its book freezes, and a stale quote there would be a sure win that
   was never really for sale.
4. **Score** each entry with the result Polymarket recorded and today's US taker fee
   (theta x p x (1 - p) per contract). Results are per $1 staked.

Limits worth knowing: the prices are top-of-book display prices with no depth, so a real
$50-100 order could fill a little worse; settled markets that were voided (paid at a fair
price instead of $1/$0) are left out; and markets within one event are correlated (one game
has many props), so the true uncertainty is somewhat wider than the intervals shown.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .parse import _float, parse_datetime
from .us import UsClient

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# (key, label, gateway filter). Sports types get their own groups; "sports_other" is what's left.
GROUPS: list[tuple[str, str, dict[str, str]]] = [
    ("moneyline", "Sports: who wins the game", {"categories": "sports", "sportsMarketTypes": "SPORTS_MARKET_TYPE_MONEYLINE"}),
    ("soccer", "Sports: soccer win / draw / win", {"categories": "sports", "sportsMarketTypes": "SPORTS_MARKET_TYPE_DRAWABLE_OUTCOME"}),
    ("futures", "Sports: season futures", {"categories": "sports", "sportsMarketTypes": "SPORTS_MARKET_TYPE_FUTURE"}),
    ("sports_other", "Sports: player props and other", {"categories": "sports"}),
    ("crypto", "Crypto prices", {"categories": "crypto"}),
    ("politics", "Politics", {"categories": "politics"}),
    ("culture", "Culture and entertainment", {"categories": "culture"}),
    ("finance", "Finance and economy", {"categories": "finance"}),
    ("macro", "Macro (rates, inflation, jobs)", {"categories": "macro"}),
    ("climate", "Weather", {"categories": "climate"}),
]
GROUP_LABELS = {k: label for k, label, _ in GROUPS}
HORIZONS: list[tuple[str, float]] = [("1h", 3600.0), ("6h", 6 * 3600.0), ("1d", 86400.0), ("3d", 3 * 86400.0),
                                     ("7d", 7 * 86400.0)]
BUCKETS: list[tuple[float, float]] = [(0.90, 0.93), (0.93, 0.96), (0.96, 0.98), (0.98, 0.995)]
GAME_TYPES = {"moneyline", "drawable_outcome", "props", "spreads", "totals"}
DEFAULT_THETA = 0.095  # the lowest-volume US taker tier
PAGE = 50
PER_PAGE = 4  # markets taken from each random page, so samples spread across the range


@dataclass
class SampledMarket:
    slug: str
    group: str
    category: str
    market_type: str
    yes_settle: float  # 1.0 YES won, 0.0 NO won
    anchor: float  # unix seconds: game start, or scheduled end
    theta: float
    title: str = ""

    def to_json(self) -> dict[str, Any]:
        return self.__dict__


def _settled_yes(raw: dict) -> float | None:
    """YES's settlement value. For settled US markets outcomePrices lists YES (the long side) first."""
    if raw.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    try:
        prices = json.loads(raw.get("outcomePrices") or "[]")
        v = float(prices[0])
    except (ValueError, IndexError, TypeError):
        return None
    return v


def _anchor(raw: dict) -> float | None:
    mtype = raw.get("marketType") or ""
    start = parse_datetime(raw.get("gameStartTime"))
    end = parse_datetime(raw.get("endDate"))
    if mtype in GAME_TYPES and start is not None:
        return start.timestamp()
    return end.timestamp() if end else None


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def compress(history: Iterable[dict]) -> list[list[float]]:
    """Keep only the points where either price changed: [[ts, long, short], ...]."""
    out: list[list[float]] = []
    for h in history:
        ts, lp, sp = h.get("timestamp"), h.get("longPrice"), h.get("shortPrice")
        if ts is None or lp is None or sp is None:
            continue
        point = [float(ts), float(lp), float(sp)]
        if not out or out[-1][1:] != point[1:]:
            out.append(point)
    return out


def entry(points: list[list[float]], anchor: float, horizon_s: float, band: tuple[float, float]
          ) -> tuple[int, float] | None:
    """(side, price) bought `horizon_s` before the end, if either side was priced in the band."""
    if len(points) < 2:
        return None
    end = min(anchor, points[-1][0])  # the last change: after it the book is frozen (settled)
    t = end - horizon_s
    if t < points[0][0]:
        return None  # the market wasn't listed yet
    i = max(j for j, p in enumerate(points) if p[0] <= t)
    _, yes, no = points[i]
    if not (0 < yes < 1 and 0 < no < 1) or not 1.0 - 1e-9 <= yes + no <= 1.3:
        return None  # empty, crossed or very wide book
    lo, hi = band
    if lo <= yes <= hi:
        return 0, yes
    if lo <= no <= hi:
        return 1, no
    return None


def per_dollar(price: float, won: bool, theta: float) -> float:
    """Profit per $1 staked: $1 buys 1 / (p + fee) contracts, each paying $1 if it wins."""
    unit = price + theta * price * (1 - price)
    return (1.0 / unit if won else 0.0) - 1.0


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class Backtest:
    def __init__(self, data_dir: Path, client: UsClient | None = None, seed: int | None = None) -> None:
        self.dir = data_dir / "backtest"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client or UsClient()
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        self.markets_path = self.dir / "markets.jsonl"
        self.history_path = self.dir / "history.jsonl"
        self.totals_path = self.dir / "totals.json"
        self.result_path = self.dir / "result.json"

    # ---------- cache ----------
    def _read_jsonl(self, path: Path) -> list[dict]:
        return read_jsonl(path)

    def _append(self, path: Path, rows: list[dict]) -> None:
        if not rows:
            return
        with self._lock, open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    # ---------- sampling ----------
    def _page(self, filters: dict[str, str], offset: int, limit: int = PAGE) -> list[dict]:
        return self.client._get("/v1/markets", limit=limit, offset=offset, closed="true", **filters).get("markets") or []

    def group_size(self, filters: dict[str, str]) -> int:
        """Approximate number of settled markets matching a filter (the API has no count)."""
        if not self._page(filters, 0, 1):
            return 0
        hi = 1000
        while self._page(filters, hi, 1):
            hi *= 2
        lo = hi // 2 if hi > 1000 else 0
        while hi - lo > max(100, hi // 50):
            mid = (lo + hi) // 2
            if self._page(filters, mid, 1):
                lo = mid
            else:
                hi = mid
        return lo + 1

    def totals(self, refresh: bool = False) -> dict[str, int]:
        if self.totals_path.exists() and not refresh:
            cached = json.loads(self.totals_path.read_text())
            if time.time() - cached.get("at", 0) < 7 * 86400:
                return cached["totals"]
        totals = {key: self.group_size(f) for key, _, f in GROUPS}
        self.totals_path.write_text(json.dumps({"at": time.time(), "totals": totals}))
        return totals

    def _group_of(self, key: str, raw: dict) -> str | None:
        """Keep each sampled market in exactly one group."""
        mtype = raw.get("marketType") or ""
        if key == "sports_other":
            return None if mtype in {"moneyline", "drawable_outcome", "futures"} else key
        if key == "moneyline" and mtype != "moneyline":
            return None
        if key == "soccer" and mtype != "drawable_outcome":
            return None
        if key == "futures" and mtype != "futures":
            return None
        return key

    def sample(self, per_group: int, progress: Progress | None = None) -> list[SampledMarket]:
        """Grow the cached sample to `per_group` settled markets per group (where that many exist)."""
        have = [SampledMarket(**m) for m in self._read_jsonl(self.markets_path)]
        seen = {m.slug for m in have}
        counts: dict[str, int] = defaultdict(int)
        for m in have:
            counts[m.group] += 1
        totals = self.totals()
        for key, label, filters in GROUPS:
            total = totals.get(key, 0)
            need = per_group - counts[key]
            if total <= 0 or need <= 0:
                continue
            if progress:
                progress(f"Sampling {label}: {counts[key]} of {per_group}")
            tries = 0
            while need > 0 and tries < max(20, 3 * per_group // PER_PAGE):
                tries += 1
                batch = min(6, max(1, need // PER_PAGE + 1))
                offsets = [self.rng.randrange(0, max(1, total - PAGE + 1)) if total > PAGE else 0 for _ in range(batch)]
                with ThreadPoolExecutor(max_workers=batch) as pool:
                    pages = list(pool.map(lambda o, f=filters: self._safe_page(f, o), offsets))
                new: list[SampledMarket] = []
                for page in pages:
                    self.rng.shuffle(page)
                    taken = 0
                    for raw in page:
                        if taken >= PER_PAGE or need - len(new) <= 0:
                            break
                        slug = raw.get("slug")
                        group = self._group_of(key, raw)
                        settle, anchor = _settled_yes(raw), _anchor(raw)
                        if not slug or slug in seen or group is None or anchor is None:
                            continue
                        if settle not in (0.0, 1.0):
                            continue  # voided / split payouts: not a clean win or loss
                        seen.add(slug)
                        taken += 1
                        new.append(SampledMarket(
                            slug=slug, group=group, category=str(raw.get("category") or ""),
                            market_type=str(raw.get("marketType") or ""), yes_settle=settle, anchor=anchor,
                            theta=_float(raw.get("feeCoefficient"), DEFAULT_THETA) or DEFAULT_THETA,
                            title=str(raw.get("question") or raw.get("title") or "")[:120]))
                self._append(self.markets_path, [m.to_json() for m in new])
                have.extend(new)
                need -= len(new)
                if total <= per_group and tries > total // PAGE + 2:
                    break  # a small group: take what exists
        return have

    def _safe_page(self, filters: dict[str, str], offset: int) -> list[dict]:
        try:
            return self._page(filters, offset)
        except httpx.HTTPError as exc:
            log.warning("market page failed: %s", exc)
            return []

    # ---------- price histories ----------
    def histories(self, markets: list[SampledMarket], progress: Progress | None = None) -> dict[str, list[list[float]]]:
        cached = {h["slug"]: h["points"] for h in self._read_jsonl(self.history_path)}
        todo = [m.slug for m in markets if m.slug not in cached]
        done = 0

        def fetch(slug: str) -> dict | None:
            try:
                data = self.client._get("/v1/price-history", symbol=slug, fixedInterval="INTERVAL_ALL", fidelity=60)
            except httpx.HTTPError as exc:
                log.warning("history for %s failed: %s", slug, exc)
                return None
            return {"slug": slug, "points": compress(data.get("history") or [])}

        with ThreadPoolExecutor(max_workers=6) as pool:
            for i in range(0, len(todo), 60):
                rows = [r for r in pool.map(fetch, todo[i : i + 60]) if r is not None]
                self._append(self.history_path, rows)
                cached.update({r["slug"]: r["points"] for r in rows})
                done += len(rows)
                if progress:
                    progress(f"Price histories: {len(cached):,} of {len(markets):,} markets")
        return cached

    def check_settlements(self, markets: list[SampledMarket], n: int = 12) -> dict[str, int]:
        """Cross-check the YES-first reading of outcomePrices against the settlement endpoint."""
        picks = self.rng.sample(markets, min(n, len(markets)))
        res = self.client.resolutions([m.slug for m in picks])
        agree = sum(1 for m in picks if m.slug in res and abs(res[m.slug][0] - m.yes_settle) < 1e-9)
        return {"checked": len(res), "agree": agree}

    # ---------- the backtest ----------
    def run(self, per_group: int = 600, band: tuple[float, float] = (0.90, 0.995),
            progress: Progress | None = None) -> dict[str, Any]:
        t0 = time.monotonic()
        markets = self.sample(per_group, progress)
        if progress:
            progress(f"Sampled {len(markets):,} settled markets; fetching price histories")
        histories = self.histories(markets, progress)
        check = self.check_settlements(markets)
        bets: list[dict[str, Any]] = []
        for m in markets:
            points = histories.get(m.slug)
            if not points:
                continue
            for hname, hsec in HORIZONS:
                e = entry(points, m.anchor, hsec, band)
                if e is None:
                    continue
                side, price = e
                won = (m.yes_settle == 1.0) == (side == 0)
                bets.append({"group": m.group, "horizon": hname, "side": side, "price": price, "won": won,
                             "pnl": per_dollar(price, won, m.theta), "pnl_no_fees": per_dollar(price, won, 0.0),
                             "fee_unit": m.theta * price * (1 - price), "slug": m.slug, "title": m.title})
        result = summarize(bets, markets, band)
        result.update({
            "generated_at": datetime.now(timezone.utc).isoformat(), "duration_s": round(time.monotonic() - t0, 1),
            "markets_sampled": len(markets), "with_history": sum(1 for m in markets if histories.get(m.slug)),
            "per_group": per_group, "settlement_check": check, "totals": self.totals(),
        })
        tmp = self.result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result))
        tmp.replace(self.result_path)
        return result

    def latest(self) -> dict[str, Any] | None:
        if not self.result_path.exists():
            return None
        try:
            return json.loads(self.result_path.read_text())
        except json.JSONDecodeError:
            return None


def _stats(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in rows if r["won"])
    implied = sum(r["price"] for r in rows) / n
    breakeven = sum(r["price"] + r["fee_unit"] for r in rows) / n  # win rate needed to break even after fees
    lo, hi = wilson(wins, n)
    roi = sum(r["pnl"] for r in rows) / n
    if lo > breakeven:
        verdict = "profitable"
    elif hi < breakeven:
        verdict = "loses money"
    else:
        verdict = "unclear"
    return {"n": n, "wins": wins, "win_rate": wins / n, "ci_low": lo, "ci_high": hi, "implied": implied,
            "breakeven": breakeven, "roi": roi, "roi_no_fees": sum(r["pnl_no_fees"] for r in rows) / n,
            "verdict": verdict}


def summarize(bets: list[dict], markets: list[SampledMarket], band: tuple[float, float]) -> dict[str, Any]:
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for b in bets:
        by[(b["group"], b["horizon"])].append(b)
        by[("all", b["horizon"])].append(b)
    groups = []
    for key in [g for g, _, _ in GROUPS] + ["all"]:
        sampled = sum(1 for m in markets if m.group == key) if key != "all" else len(markets)
        groups.append({"key": key, "label": GROUP_LABELS.get(key, "All groups"), "sampled": sampled,
                       "horizons": {h: _stats(by[(key, h)]) for h, _ in HORIZONS}})
    buckets = []
    for lo, hi in BUCKETS:
        rows = {h: [b for b in bets if b["horizon"] == h and lo <= b["price"] < hi + (1e-9 if hi == band[1] else 0)]
                for h, _ in HORIZONS}
        buckets.append({"low": lo, "high": hi, "horizons": {h: _stats(r) for h, r in rows.items()}})
    losses = sorted((b for b in bets if not b["won"] and b["horizon"] == "1d"), key=lambda b: -b["price"])[:15]
    return {"band": list(band), "horizons": [h for h, _ in HORIZONS], "groups": groups, "buckets": buckets,
            "bets": len(bets), "worst_losses": [{k: b[k] for k in ("title", "slug", "price", "side", "group")}
                                                for b in losses]}


# ---------- near-certain record ----------
NEAR_CERTAIN_BUCKETS: list[tuple[float, float]] = [(0.97, 0.98), (0.98, 0.99), (0.99, 0.995)]
NEAR_CERTAIN_HORIZONS = (3 * 86400.0, 86400.0, 6 * 3600.0, 3600.0)  # earliest first
_record_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def history_group(category: str, market_type: str) -> str:
    """The backtest group a live Polymarket US market belongs to."""
    if category == "sports":
        return {"moneyline": "moneyline", "drawable_outcome": "soccer", "futures": "futures"}.get(
            market_type, "sports_other")
    return category


def price_bucket(price: float) -> float | None:
    for lo, hi in NEAR_CERTAIN_BUCKETS:
        if lo <= price < hi or (hi == NEAR_CERTAIN_BUCKETS[-1][1] and price == hi):
            return lo
    return None


def near_certain_record(data_dir: Path) -> dict[str, Any]:
    """How 97-99.5c favorites did when bought 1 hour to 3 days before the end, from the backtest cache.

    Each settled market counts once, at the earliest of 3d / 1d / 6h / 1h before its end where its
    favorite was priced in the band. Grouped by backtest group and by price (97, 98, 99c).
    """
    bt = data_dir / "backtest"
    mpath, hpath = bt / "markets.jsonl", bt / "history.jsonl"
    if not mpath.exists() or not hpath.exists():
        return {"markets": 0, "rows": [], "groups": GROUP_LABELS}
    stamp = max(mpath.stat().st_mtime, hpath.stat().st_mtime)
    hit = _record_cache.get(str(bt))
    if hit and hit[0] == stamp:
        return hit[1]
    markets = [SampledMarket(**m) for m in read_jsonl(mpath)]
    points = {h["slug"]: h["points"] for h in read_jsonl(hpath) if h.get("slug")}
    cells: dict[tuple[str, float], list[float]] = defaultdict(lambda: [0, 0, 0.0])
    band = (NEAR_CERTAIN_BUCKETS[0][0], NEAR_CERTAIN_BUCKETS[-1][1])
    for m in markets:
        pts = points.get(m.slug)
        if not pts:
            continue
        for h in NEAR_CERTAIN_HORIZONS:
            e = entry(pts, m.anchor, h, band)
            if e is None:
                continue
            side, price = e
            bucket = price_bucket(price)
            if bucket is None:
                break
            won = (m.yes_settle == 1.0) == (side == 0)
            for key in ((m.group, bucket), (m.group, 0.0)):  # 0.0 = every price in the band
                c = cells[key]
                c[0] += 1
                c[1] += won
                c[2] += per_dollar(price, won, m.theta)
            break
    rows = [{"group": g, "label": GROUP_LABELS.get(g, g), "bucket": b, "n": int(c[0]), "won": int(c[1]),
             "return": c[2] / c[0]} for (g, b), c in sorted(cells.items())]
    record = {"markets": len(markets), "rows": rows, "groups": GROUP_LABELS}
    _record_cache[str(bt)] = (stamp, record)
    return record
