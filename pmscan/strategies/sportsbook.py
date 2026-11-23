"""Sportsbook value: Polymarket game prices that are cheap against bookmaker consensus.

Bookmaker odds for the same game are converted to probabilities, the bookmaker
margin is removed, and the median across books becomes the "fair" probability.
A Polymarket outcome is listed when buying it has positive expected value at
that probability: fair / (price + fee) - 1 >= min_edge.

This is a model, not an arbitrage. It assumes the books' consensus is closer to
the truth than Polymarket's price. Sharp books (Pinnacle, exchanges) tend to be,
but a single game is still a coin flip with a slight tilt. Size accordingly.

Needs an Odds API key in PMSCAN_ODDS_API_KEY; without it the strategy is inactive.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import env
from ..books import walk_basket
from ..models import Event, Leg, Market, Opportunity, OrderBook
from ..odds import DRAW, SPORT_KEYS, BookEvent, OddsApiClient, same_team
from .base import Candidate, ScanContext, Strategy


def match_event(event: Event, book_events: Sequence[BookEvent], max_diff_minutes: float) -> BookEvent | None:
    if event.start_time is None or len(event.teams) != 2:
        return None
    a, b = (t.name for t in event.teams)
    window = timedelta(minutes=max_diff_minutes)
    for be in book_events:
        if be.commence is None or abs(be.commence - event.start_time) > window:
            continue
        if (same_team(a, be.home) and same_team(b, be.away)) or (same_team(a, be.away) and same_team(b, be.home)):
            return be
    return None


def _fair_for(name: str, fair: Mapping[str, float]) -> float | None:
    if name.lower().startswith(DRAW):
        hits = [p for k, p in fair.items() if k.lower() == DRAW]
    else:
        hits = [p for k, p in fair.items() if k.lower() != DRAW and same_team(name, k)]
    return hits[0] if len(hits) == 1 else None


@dataclass
class _Bet:
    event: Event
    market: Market
    side: int
    fair: float
    label: str
    n_books: int


def moneyline_bets(event: Event, fair: Mapping[str, float], n_books: int) -> Iterator[_Bet]:
    """Every Polymarket moneyline position in the game with its fair probability."""
    for m in event.markets:
        if not m.tradeable or m.sports_market_type != "moneyline":
            continue
        outcomes = tuple(o.lower() for o in m.outcomes)
        if outcomes == ("yes", "no"):
            # Soccer style: one Yes/No market per result ("Paris FC", "Draw (…)").
            p = _fair_for(m.group_title, fair)
            if p is None:
                continue
            is_draw = m.group_title.lower().startswith(DRAW)
            yield _Bet(event, m, 0, p, "Draw" if is_draw else f"{m.group_title} to win", n_books)
            yield _Bet(event, m, 1, 1.0 - p, "No draw" if is_draw else f"{m.group_title} not to win", n_books)
        else:
            # US style: one market whose two outcomes are the teams.
            for side, name in enumerate(m.outcomes):
                p = _fair_for(name, fair)
                if p is not None:
                    yield _Bet(event, m, side, p, f"{name} to win", n_books)


class SportsbookValue(Strategy):
    name = "sportsbook_value"
    title = "Sportsbook value"

    def __init__(self, odds_client: OddsApiClient | None = None) -> None:
        self._odds = odds_client
        self._resolved = odds_client is not None

    def _client(self, ctx: ScanContext) -> OddsApiClient | None:
        if not self._resolved:
            self._resolved = True
            key = env.get(env.ODDS_API_KEY)
            if key:
                cfg = ctx.config.sportsbook_value
                self._odds = OddsApiClient(key, cfg.regions, cfg.refresh_minutes)
        return self._odds

    def screen(self, events: Sequence[Event], ctx: ScanContext) -> list[Candidate]:
        cfg = ctx.config.sportsbook_value
        client = self._client(ctx)
        if client is None:
            ctx.stats["sportsbook_status"] = f"inactive: set {env.ODDS_API_KEY} (see README)"
            return []
        keys = {**SPORT_KEYS, **cfg.sport_keys}
        by_sport: dict[str, list[Event]] = defaultdict(list)
        for e in events:
            if e.sport not in cfg.sports or e.sport not in keys or e.start_time is None or e.live or e.ended:
                continue
            hours = (e.start_time - ctx.now).total_seconds() / 3600.0
            if 0 <= hours <= cfg.max_hours_ahead:
                by_sport[keys[e.sport]].append(e)

        out: list[Candidate] = []
        comparisons: list[dict] = []
        matched = 0
        for sport_key, games in by_sport.items():
            book_events = client.odds(sport_key, active_sports=len(by_sport))
            for e in games:
                be = match_event(e, book_events, cfg.max_start_diff_minutes)
                fair = be.fair(cfg.bookmakers or None, cfg.min_books) if be else None
                if fair is None:
                    continue
                matched += 1
                n_books = len([k for k in be.books if not cfg.bookmakers or k in cfg.bookmakers])
                for bet in moneyline_bets(e, fair, n_books):
                    m = bet.market
                    price = m.best_ask if bet.side == 0 else (None if m.best_bid is None else 1.0 - m.best_bid)
                    if price is None or bet.fair < cfg.min_fair_prob:
                        continue
                    ev = bet.fair / (price + m.fees.per_share(price)) - 1.0
                    if len(comparisons) < 600:
                        comparisons.append({"game": e.title, "bet": bet.label, "fair": round(bet.fair, 4),
                                            "price": round(price, 4), "ev": round(ev, 4), "url": m.url})
                    if ev >= cfg.min_edge - 0.02:  # small margin: Gamma quotes can lag the book
                        out.append(Candidate([m.token_ids[bet.side]], ev, bet))

        ctx.stats["sportsbook_status"] = "active"
        ctx.stats["sportsbook_games"] = sum(len(g) for g in by_sport.values())
        ctx.stats["sportsbook_matched"] = matched
        ctx.stats["sportsbook_comparisons"] = comparisons
        ctx.stats["odds_api_requests_remaining"] = client.requests_remaining
        ctx.stats["odds_refresh_minutes"] = round(client.ttl_for(len(by_sport)) / 60.0, 1)
        return out

    def evaluate(
        self, candidate: Candidate, books: Mapping[str, OrderBook], ctx: ScanContext
    ) -> Opportunity | None:
        cfg = ctx.config.sportsbook_value
        bet: _Bet = candidate.payload
        m = bet.market
        book = books.get(candidate.token_ids[0])
        if book is None or not book.asks:
            return None
        fill = walk_basket([book.asks], [m.fees], max_unit_cost=bet.fair / (1.0 + cfg.min_edge),
                           budget=cfg.max_position_usd)
        if fill.cost < cfg.min_depth_usd or fill.units < max(book.min_order_size, m.min_order_size):
            return None

        unit = fill.unit_cost
        ev = bet.fair / unit - 1.0
        kelly = (bet.fair - unit) / (1.0 - unit)
        expected_profit = fill.units * bet.fair - fill.cost
        start: datetime = bet.event.start_time  # type: ignore[assignment]
        days = (start - ctx.now).total_seconds() / 86400.0
        avg = fill.avg_price(0)
        notes = [
            f"Bookmaker consensus ({bet.n_books} books, margin removed): {bet.fair * 100:.1f}% "
            f"vs Polymarket {avg * 100:.1f}¢ plus fees.",
            f"Expected value {ev * 100:+.1f}% if the books are right. Full Kelly stake is "
            f"{kelly * 100:.1f}% of bankroll; most bettors use a quarter of that or less.",
            "Books and Polymarket can settle differently (overtime, postponement, retirement): "
            "check the market rules.",
        ]
        return Opportunity(
            strategy=self.name,
            kind="value_bet",
            title=bet.event.title,
            subtitle=bet.label,
            url=m.url,
            cost=fill.cost,
            payout=fill.units,
            roi=(fill.units - fill.cost) / fill.cost,
            annualized=None,
            days_to_end=days,
            risk="model",
            notes=notes,
            legs=[Leg.for_market(m, bet.side, shares=fill.units, avg_price=avg,
                                 limit_price=fill.limit_prices[0], label=m.question)],
            win_probability=bet.fair,
            score=ev,
            value_per_share=bet.fair,
            details={"fair": bet.fair, "ev": ev, "kelly": kelly, "books": bet.n_books,
                     "expected_profit": expected_profit, "bet": bet.label},
        )
