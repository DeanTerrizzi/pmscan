"""Paper trading: simulated portfolios, one per strategy, run side by side.

Each strategy gets its own book with its own starting balance, so their results can be
compared over the same period:

* **high_prob** buys every listed position priced 90-99.5c with a fixed stake.
* **near_certain** does the same for 97-99.5c politics positions ending within 3 days, with a
  bigger stake since each bet returns only 0.5-3%.
* **neg_risk_arb / ladder_arb** buy each arbitrage basket (every leg together), sized
  against fresh order books exactly as the live trader would, within a basket stake.
* **sportsbook_value** buys each value bet, never above its fair price.

Every fill walks the live order book including taker fees, so it costs what a real order
of that size would. Open positions are valued at their current best bid after each scan,
and settle at Polymarket's final resolution price (normally $1 or $0 per share).

State lives in data/paper/<strategy>/: portfolio.json and equity.jsonl.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .books import walk_basket
from .client import PolymarketClient
from .config import PaperConfig, TradingConfig
from .models import Market, Opportunity
from .scanner import ScanResult
from .trading import AutoTrader, Ledger, Skip

TITLES = {
    "high_prob": "90¢+ positions",
    "near_certain": "Near-certain bets",
    "neg_risk_arb": "Neg-risk arbitrage",
    "ladder_arb": "Ladder arbitrage",
    "sportsbook_value": "Sportsbook value",
}

BANDED = {"high_prob", "near_certain"}  # single positions bought inside a price band
PROBED = {"neg_risk_arb", "ladder_arb"}  # baskets re-checked after entry (see PaperBook._run_probes)
_probe_lock = threading.Lock()


@dataclass
class PaperPosition:
    token_id: str
    market_id: str
    market: str
    outcome: str
    side: int
    url: str
    strategy: str
    shares: float
    cost: float  # USD paid including taker fees
    avg_price: float
    opened_at: str
    end_date: str | None = None
    mark: float | None = None  # latest best bid
    closed_at: str | None = None
    payout: float | None = None  # USD received at resolution
    basket: str = ""  # positions bought together share one id (one bet)
    bet: str = ""  # what the whole bet was, e.g. the event title
    event: str = ""  # the market's event (slug), for the per-event cap
    league: str = ""  # e.g. "sports:cfb" or "politics", for the per-league cap ("" = unknown)

    @property
    def id(self) -> str:
        return f"{self.basket}|{self.token_id}"

    @property
    def value(self) -> float:
        return self.shares * (self.mark if self.mark is not None else self.avg_price)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperPosition:
        known = {f.name for f in fields(cls)}
        p = cls(**{k: v for k, v in d.items() if k in known})
        if not p.basket:  # portfolios from before baskets existed
            p.basket = p.token_id
        return p


def _event_key(market: Market) -> str:
    return market.event_slug or market.event_title or market.id


def _league_key(market: Market) -> str:
    """Markets that ride on the same games or news. Polymarket US sports event slugs start with the
    league ("cfb-cfp-2026-12-06-playoffq", "nfl-wins-ou-..."); other markets group by category.
    polymarket.com has no categories, so its markets aren't grouped (only the per-event cap applies)."""
    category = getattr(market, "category", "") or ""
    if category == "sports" and market.event_slug:
        return f"sports:{market.event_slug.split('-')[0]}"
    return category


def _now() -> datetime:
    return datetime.now(timezone.utc)


Index = dict[str, tuple[Market, int]]


class PaperBook:
    """One strategy's simulated portfolio."""

    def __init__(self, strategy: str, config: PaperConfig, client: PolymarketClient | None, root: Path) -> None:
        self.strategy = strategy
        self.config = config
        self.client = client
        self.dir = root / strategy
        self.state_path = self.dir / "portfolio.json"
        self.equity_path = self.dir / "equity.jsonl"
        self.starting_balance = config.starting_balance
        self.cash = config.starting_balance
        self.started_at = _now().isoformat()
        self.open: dict[str, PaperPosition] = {}
        self.closed: list[PaperPosition] = []
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text())
        self.cash = state["cash"]
        self.started_at = state.get("started_at", self.started_at)
        self.starting_balance = state.get("starting_balance", self.starting_balance)
        for d in state.get("open", []):
            p = PaperPosition.from_dict(d)
            self.open[p.id] = p
        self.closed = [PaperPosition.from_dict(p) for p in state.get("closed", [])]

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        state = {
            "strategy": self.strategy,
            "started_at": self.started_at,
            "starting_balance": self.starting_balance,
            "cash": self.cash,
            "open": [asdict(p) for p in self.open.values()],
            "closed": [asdict(p) for p in self.closed],
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)

    # ---------- the per-scan cycle ----------
    def settle(self, resolved: dict[str, list[float]], now: datetime) -> list[PaperPosition]:
        settled = []
        for pid, p in list(self.open.items()):
            prices = resolved.get(p.market_id)
            if prices is None:
                continue
            p.payout = p.shares * prices[p.side]
            p.mark = prices[p.side]
            p.closed_at = now.isoformat()
            self.cash += p.payout
            self.closed.append(p)
            del self.open[pid]
            settled.append(p)
        return settled

    def mark(self, index: Index) -> None:
        for p in self.open.values():
            hit = index.get(p.token_id)
            if not hit:
                continue
            m, side = hit
            bid = m.best_bid if side == 0 else (None if m.best_ask is None else 1.0 - m.best_ask)
            if bid is not None:
                p.mark = bid

    def enter(self, opportunities: list[Opportunity], index: Index, now: datetime) -> list[PaperPosition]:
        if self.strategy in BANDED:
            return self._enter_band(opportunities, index, now)
        return self._enter_baskets(opportunities, index, now)

    @property
    def stake(self) -> float:
        if self.strategy == "near_certain":
            return self.config.near_certain_stake_usd
        if self.strategy in ("high_prob", "sportsbook_value"):
            return self.config.stake_usd
        return self.config.basket_stake_usd

    @property
    def band_min(self) -> float:
        return self.config.near_certain_min_price if self.strategy == "near_certain" else self.config.min_price

    def _enter_band(self, opps: list[Opportunity], index: Index, now: datetime) -> list[PaperPosition]:
        """Every single-leg position priced inside [min_price, max_price], one stake each."""
        cfg = self.config
        min_price, stake = self.band_min, self.stake
        held = {p.token_id for p in self.open.values()}
        candidates = [o for o in opps if len(o.legs) == 1 and o.legs[0].token_id not in held
                      and min_price <= o.legs[0].avg_price <= cfg.max_price and o.legs[0].token_id in index]
        candidates.sort(key=lambda o: o.score, reverse=True)
        # At most max_bets_per_event per event: markets in one event often go wrong together.
        per_event = Counter(self._event_of(p, index) for p in self.open.values())
        # ...and at most max_league_share of the balance per league: one weekend's games can move
        # every college football market at once, across many events.
        per_league: Counter[str] = Counter()
        for p in self.open.values():
            per_league[self._league_of(p, index)] += p.cost
        league_cap = cfg.max_league_share * self.starting_balance
        picks = []
        for o in candidates:
            market = index[o.legs[0].token_id][0]
            event, league = _event_key(market), _league_key(market)
            if cfg.max_bets_per_event > 0 and per_event[event] >= cfg.max_bets_per_event:
                continue
            if cfg.max_league_share > 0 and league and per_league[league] + stake > league_cap + 1e-9:
                continue
            per_event[event] += 1
            per_league[league] += stake
            picks.append(o)
        room = min(cfg.max_open_positions - len(self.open), int(self.cash // stake))
        picks = picks[: max(0, room)]
        if not picks or self.client is None:
            return []
        books = self.client.get_books([o.legs[0].token_id for o in picks])
        entered = []
        for o in picks:
            leg = o.legs[0]
            book = books.get(leg.token_id)
            if book is None or self.cash < stake:
                continue
            cap = min(cfg.max_price, leg.limit_price)
            ladder = [lv for lv in book.asks if min_price <= lv.price <= cap + 1e-9]
            fill = walk_basket([ladder], [leg.fees], max_unit_cost=1.0, budget=stake)
            if fill.units <= 0 or fill.units < max(leg.min_order_size, book.min_order_size):
                continue
            market, side = index[leg.token_id]
            entered.append(self._position(o, market, side, leg.outcome, leg.url, fill.units, fill.cost,
                                          fill.avg_price(0), now, basket=leg.token_id, mark=book.best_bid))
        return entered

    @staticmethod
    def _event_of(p: PaperPosition, index: Index) -> str:
        if p.event:
            return p.event
        if p.token_id in index:  # positions from before the event was stored
            return _event_key(index[p.token_id][0])
        return p.market_id

    @staticmethod
    def _league_of(p: PaperPosition, index: Index) -> str:
        if p.league:
            return p.league
        if p.token_id in index:  # positions from before the league was stored
            return _league_key(index[p.token_id][0])
        return ""

    def _enter_baskets(self, opps: list[Opportunity], index: Index, now: datetime) -> list[PaperPosition]:
        """Each arbitrage basket / value bet, sized against fresh books like the live trader."""
        if self.client is None:
            return []
        cfg = self.config
        held = {p.basket.split("@")[0] for p in self.open.values()}
        entered = []
        for o in sorted(opps, key=lambda o: o.score, reverse=True):
            if o.key in held or len(self.open) >= cfg.max_open_positions or any(l.token_id not in index for l in o.legs):
                continue
            stake = min(cfg.basket_stake_usd if len(o.legs) > 1 else cfg.stake_usd, self.cash)
            if stake < 1.0:
                break
            planner = self._planner(stake)
            try:
                plan = planner._plan(o, now)
            except Skip:
                continue
            notional = sum(float(l.amount) for l in plan.legs) or 1.0
            fees = plan.cost - notional
            basket = f"{o.key}@{now.isoformat()}"
            for leg_order, leg in zip(plan.legs, o.legs, strict=True):
                market, side = index[leg.token_id]
                amount = float(leg_order.amount)
                entered.append(self._position(o, market, side, leg.outcome, leg.url, plan.units,
                                              amount + fees * amount / notional, amount / plan.units, now,
                                              basket=basket, mark=None))
            held.add(o.key)
            if self.strategy in PROBED and cfg.probe_delays_s:
                threading.Thread(target=self._run_probes, args=(o, plan, basket, stake), daemon=True,
                                 name=f"paper-probe-{self.strategy}").start()
        return entered

    def _planner(self, stake: float) -> AutoTrader:
        """The live trader's own sizing and checks, used to price paper entries like real orders."""
        cfg = self.config
        return AutoTrader(
            TradingConfig(strategies=[self.strategy], max_usd_per_trade=stake, max_usd_per_day=float("inf"),
                          min_profit_usd=cfg.min_edge_usd, min_roi=cfg.min_roi, max_legs=10_000,
                          allow_delayed_markets=True),
            self.client, Ledger(self.dir / "planner"),
        )

    @property
    def probes_path(self) -> Path:
        return self.dir / "probes.jsonl"

    def _run_probes(self, o: Opportunity, plan: Any, basket: str, stake: float, sleep=time.sleep) -> None:
        """Would a slower order have got this basket too? Re-plan it on fresh books after each delay.

        A paper entry fills on books fetched a fraction of a second after the edge was seen; a real
        order lands later and races other traders. This records, per delay, how much of the entry
        size would still have filled and for what profit. It never changes the portfolio.
        """
        checks, start = [], time.monotonic()
        for delay in sorted(self.config.probe_delays_s):
            sleep(max(0.0, delay - (time.monotonic() - start)))
            check: dict[str, Any] = {"delay_s": delay}
            try:
                again = self._planner(stake)._plan(o, _now())
                check.update(ok=True, units=round(again.units, 4), share=round(min(1.0, again.units / plan.units), 4),
                             profit=round(again.edge, 4))
            except Skip as exc:
                check.update(ok=False, share=0.0, profit=0.0, reason=str(exc))
            except Exception as exc:  # network trouble: unknown, not a miss
                check.update(ok=None, reason=f"{type(exc).__name__}: {exc}")
            checks.append(check)
        row = {"ts": _now().isoformat(), "basket": basket, "title": o.title, "kind": o.kind,
               "entered": {"units": round(plan.units, 4), "cost": round(plan.cost, 4), "profit": round(plan.edge, 4)},
               "checks": checks}
        try:
            with _probe_lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with open(self.probes_path, "a") as fh:
                    fh.write(json.dumps(row) + "\n")
        except OSError:
            pass  # e.g. the portfolio was reset meanwhile

    def probe_summary(self) -> dict[str, Any] | None:
        """Per delay: how many entries were still profitable, the share of the size and profit kept."""
        if self.strategy not in PROBED or not self.probes_path.exists():
            return None
        rows = []
        for line in self.probes_path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        delays: dict[float, dict[str, float]] = {}
        for r in rows:
            for c in r["checks"]:
                if c.get("ok") is None:
                    continue
                d = delays.setdefault(c["delay_s"], {"n": 0, "still": 0, "share": 0.0, "profit": 0.0, "entered": 0.0})
                d["n"] += 1
                d["still"] += bool(c["ok"])
                d["share"] += c.get("share", 0.0)
                d["profit"] += c.get("profit", 0.0)
                d["entered"] += r["entered"]["profit"]
        return {
            "baskets": len(rows),
            "delays": [{"delay_s": k, "checked": v["n"], "still_profitable": v["still"],
                        "avg_size_share": v["share"] / v["n"] if v["n"] else None,
                        "profit_kept": v["profit"] / v["entered"] if v["entered"] else None}
                       for k, v in sorted(delays.items())],
            "recent": rows[-20:][::-1],
        }

    def _position(self, o: Opportunity, market: Market, side: int, outcome: str, url: str, shares: float,
                  cost: float, avg: float, now: datetime, basket: str, mark: float | None) -> PaperPosition:
        p = PaperPosition(
            token_id=market.token_ids[side], market_id=market.id, market=market.question, outcome=outcome,
            side=side, url=url, strategy=self.strategy, shares=shares, cost=cost, avg_price=avg,
            opened_at=now.isoformat(), end_date=market.end_date.isoformat() if market.end_date else None,
            mark=mark if mark is not None else avg, basket=basket, bet=o.title if len(o.legs) > 1 else "",
            event=_event_key(market), league=_league_key(market),
        )
        self.cash -= cost
        self.open[p.id] = p
        return p

    def record_equity(self, now: datetime) -> None:
        s = self.summary()
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.equity_path, "a") as fh:
            fh.write(json.dumps({"ts": now.isoformat(), "equity": round(s["equity"], 4), "cash": round(self.cash, 4),
                                 "open": s["open_positions"], "realized": round(s["realized_pnl"], 4)}) + "\n")

    def reset(self) -> Path | None:
        archive = None
        if self.dir.exists() and any(self.dir.iterdir()):
            archive = self.dir.parent / f"{self.strategy}-archive-{_now():%Y%m%d-%H%M%S}"
            shutil.move(str(self.dir), archive)
        self.cash = self.starting_balance = self.config.starting_balance
        self.started_at = _now().isoformat()
        self.open, self.closed = {}, []
        self.save()
        return archive

    # ---------- reporting ----------
    def summary(self) -> dict[str, Any]:
        value = sum(p.value for p in self.open.values())
        invested = sum(p.cost for p in self.open.values())
        realized = sum((p.payout or 0) - p.cost for p in self.closed)
        # A bet (one position, or a whole basket) counts once all of its positions have resolved.
        open_baskets = {p.basket for p in self.open.values()}
        bets: dict[str, list[PaperPosition]] = defaultdict(list)
        for p in self.closed:
            if p.basket not in open_baskets:
                bets[p.basket].append(p)
        won = [b for b in bets.values() if sum(p.payout or 0 for p in b) > sum(p.cost for p in b)]
        singles = [b[0] for b in bets.values() if len(b) == 1]
        closed_cost = sum(p.cost for p in self.closed)
        equity = self.cash + value
        return {
            "strategy": self.strategy,
            "title": TITLES.get(self.strategy, self.strategy),
            "started_at": self.started_at,
            "starting_balance": self.starting_balance,
            "cash": self.cash,
            "equity": equity,
            "pnl": equity - self.starting_balance,
            "return": equity / self.starting_balance - 1 if self.starting_balance else None,
            "open_positions": len(self.open),
            "open_bets": len(open_baskets),
            "invested": invested,
            "unrealized_pnl": value - invested,
            "realized_pnl": realized,
            "resolved": len(bets),
            "won": len(won),
            "win_rate": len(won) / len(bets) if bets else None,
            # For single-position bets: the win rate the entry prices implied.
            "implied_win_rate": (sum(p.avg_price for p in singles) / len(singles)
                                 if singles and len(singles) == len(bets) else None),
            "resolved_roi": realized / closed_cost if closed_cost else None,
            "stake_usd": self.stake,
            "min_price": self.band_min,
        }


class PaperTrader:
    """All the paper books, run together after each scan."""

    def __init__(self, config: PaperConfig, client: PolymarketClient | None, data_dir: Path) -> None:
        self.config = config
        self.client = client
        self.root = data_dir / "paper"
        self._migrate()
        self.books = {s: PaperBook(s, config, client, self.root) for s in config.strategies}

    def _migrate(self) -> None:
        """Before per-strategy books, the 90c portfolio lived directly in data/paper/."""
        if (self.root / "portfolio.json").exists():
            target = self.root / "high_prob"
            target.mkdir(parents=True, exist_ok=True)
            for name in ("portfolio.json", "equity.jsonl"):
                if (self.root / name).exists() and not (target / name).exists():
                    shutil.move(str(self.root / name), target / name)

    def process(self, result: ScanResult, now: datetime | None = None) -> list[str]:
        now = now or _now()
        index: Index = {t: (m, side) for e in result.events for m in e.markets for side, t in enumerate(m.token_ids)}
        resolved = self._resolutions(index)
        messages = []
        for name, book in self.books.items():
            settled = book.settle(resolved, now)
            book.mark(index)
            entered = book.enter(result.opportunities.get(name, []), index, now)
            book.record_equity(now)
            book.save()
            label = TITLES.get(name, name)
            if entered:
                bets = len({p.basket for p in entered})
                messages.append(f"paper {label}: bought {bets} bet{'s' if bets != 1 else ''} "
                                f"for ${sum(p.cost for p in entered):,.2f}")
            if settled:
                pnl = sum((p.payout or 0) - p.cost for p in settled)
                messages.append(f"paper {label}: {len(settled)} position{'s' if len(settled) != 1 else ''} resolved, "
                                f"P&L {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}")
        return messages

    def enter_only(self, result: ScanResult, now: datetime | None = None) -> list[str]:
        """Take positions in newly found opportunities (from the live feed) without a full cycle."""
        now = now or _now()
        index: Index = {t: (m, side) for e in result.events for m in e.markets for side, t in enumerate(m.token_ids)}
        messages = []
        for name, opps in result.opportunities.items():
            book = self.books.get(name)
            if book is None:
                continue
            entered = book.enter(opps, index, now)
            if entered:
                book.save()
                messages.append(f"paper {TITLES.get(name, name)}: bought {len({p.basket for p in entered})} bet(s) "
                                f"for ${sum(p.cost for p in entered):,.2f}")
        return messages

    def _resolutions(self, index: Index) -> dict[str, list[float]]:
        # A market still listed as open can't have resolved; look up the rest.
        gone = sorted({p.market_id for b in self.books.values() for p in b.open.values() if p.token_id not in index})
        if not gone or self.client is None:
            return {}
        return self.client.resolutions(gone)

    def reset(self, strategy: str | None = None) -> list[Path]:
        return [a for name, b in self.books.items() if strategy in (None, name) if (a := b.reset())]

    def summary(self) -> dict[str, Any]:
        books = [b.summary() for b in self.books.values()]
        start = sum(b["starting_balance"] for b in books)
        equity = sum(b["equity"] for b in books)
        return {
            "starting_balance": start,
            "equity": equity,
            "pnl": equity - start,
            "open_positions": sum(b["open_positions"] for b in books),
            "books": {b["strategy"]: b for b in books},
        }


def paper_snapshot(data_dir: Path, config: PaperConfig, equity_limit: int = 5000) -> dict[str, Any]:
    """Read-only view for the dashboard (reads the files the scanner thread writes)."""
    trader = PaperTrader(config, client=None, data_dir=data_dir)
    books = {}
    for name, book in trader.books.items():
        equity = []
        if book.equity_path.exists():
            for line in book.equity_path.read_text().splitlines()[-equity_limit:]:
                try:
                    equity.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        books[name] = {
            "summary": book.summary(),
            "open": sorted((asdict(p) for p in book.open.values()), key=lambda p: p["opened_at"], reverse=True),
            "closed": [asdict(p) for p in reversed(book.closed[-500:])],
            "equity": equity,
            "probes": book.probe_summary(),
        }
    return {"enabled": config.enabled, "summary": trader.summary(), "books": books}
