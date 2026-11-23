"""High-probability ("bond") positions.

Find outcomes the market already prices as very likely (for example 90-99.5 cents)
that resolve soon, and rank them by the return you earn if they pay out, annualized
over the time your capital is locked up.

This is **not** risk-free. At a price of 0.96 the market itself estimates about a
4% chance of losing the whole stake. The case for the strategy is that prediction
markets have tended to slightly underprice heavy favorites (the favorite-longshot
bias) and that short-dated favorites tie up capital only briefly. Size positions
so that one loss doesn't wipe out many wins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..books import walk_basket
from ..config import HighProbConfig
from ..models import Event, Leg, Level, Market, Opportunity, OrderBook
from .base import Candidate, ScanContext, Strategy


@dataclass
class _Pick:
    market: Market
    side: int  # 0 = first outcome (YES), 1 = second (NO)
    days_to_end: float


def _tight(bid: float | None, ask: float | None, max_spread: float) -> bool:
    """Both sides quoted, at most max_spread apart (0 or less turns the check off)."""
    if max_spread <= 0:
        return True
    return bid is not None and ask is not None and ask - bid <= max_spread + 1e-9


def _price_for(levels: Sequence[Level], usd: float) -> float | None:
    """The worst price reached trading `usd` against these levels (best first), or None if too thin."""
    spent = 0.0
    for lv in levels:
        spent += lv.price * lv.size
        if spent >= usd - 1e-9:
            return lv.price
    return None


def _tight_book(book: OrderBook, max_spread: float, usd: float) -> bool:
    """The spread for a real order: what `usd` costs to buy against what it fetches when sold.

    A few dust contracts at the top can make an empty book (real orders at 1c and 99c) look 1c wide.
    """
    if max_spread <= 0:
        return True
    return _tight(_price_for(book.bids, usd), _price_for(book.asks, usd), max_spread)


class HighProbability(Strategy):
    name = "high_prob"
    title = "High-probability yield"

    def settings(self, ctx: ScanContext) -> HighProbConfig:
        return getattr(ctx.config, self.name)

    def screen(self, events: Sequence[Event], ctx: ScanContext) -> list[Candidate]:
        cfg = self.settings(ctx)
        keywords = [k.lower() for k in cfg.exclude_keywords]
        excluded_tags = {t.lower() for t in cfg.exclude_tags}
        out: list[Candidate] = []
        live_skipped = 0
        type_skipped = category_skipped = spread_skipped = 0
        skip_types = set(cfg.exclude_market_types_us) if ctx.venue == "us" else set()
        only = [e.lower().split(":", 1) for e in cfg.include_us] if ctx.venue == "us" else []
        for event in events:
            if excluded_tags and excluded_tags.intersection(t.lower() for t in event.tags):
                continue
            for m in event.markets:
                if not m.tradeable:
                    continue
                if m.market_type in skip_types:
                    type_skipped += 1
                    continue
                if only and not any(m.category == c[0] and (len(c) == 1 or m.market_type == c[1]) for c in only):
                    category_skipped += 1
                    continue
                # Where listings don't report liquidity, min_depth_usd still checks the real book.
                if m.stats_known and (m.liquidity < cfg.min_liquidity or m.volume_24h < cfg.min_volume_24h):
                    continue
                if not cfg.include_live_games and event.in_play(m, ctx.now):
                    live_skipped += 1
                    continue
                days = m.days_to_end(ctx.now)
                if days is None or days > cfg.max_days or days < -cfg.max_days_past_end:
                    continue
                if not _tight(m.best_bid, m.best_ask, cfg.max_spread):
                    spread_skipped += 1
                    continue
                if keywords and any(k in m.slug.lower() or k in event.slug.lower() for k in keywords):
                    continue
                # YES costs its ask; NO costs 1 - YES bid (the books are mirrored).
                quotes = (m.best_ask, None if m.best_bid is None else 1.0 - m.best_bid)
                for side, price in enumerate(quotes):
                    if price is None or not cfg.min_price <= price <= cfg.max_price:
                        continue
                    unit = price + m.fees.per_share(price)
                    est = ctx.annualize((1.0 - unit) / unit, days) or 0.0
                    out.append(Candidate([m.token_ids[side]], est, _Pick(m, side, days)))

        out.sort(key=lambda c: c.priority, reverse=True)
        ctx.stats[f"{self.name}_screened"] = len(out)
        ctx.stats[f"{self.name}_live_skipped"] = live_skipped
        ctx.stats[f"{self.name}_type_skipped"] = type_skipped
        ctx.stats[f"{self.name}_category_skipped"] = category_skipped
        ctx.stats[f"{self.name}_spread_skipped"] = spread_skipped
        # Polymarket US serves one order book per request (25 requests/s), so verify fewer there.
        limit = cfg.max_candidates if ctx.venue != "us" else min(cfg.max_candidates, cfg.max_candidates_us)
        return out[:limit]

    def evaluate(
        self, candidate: Candidate, books: Mapping[str, OrderBook], ctx: ScanContext
    ) -> Opportunity | None:
        cfg = self.settings(ctx)
        pick: _Pick = candidate.payload
        m = pick.market
        book = books.get(candidate.token_ids[0])
        if book is None or not book.asks or not _tight_book(book, cfg.max_spread, cfg.min_depth_usd):
            return None
        fill = walk_basket(
            [book.asks],
            [m.fees],
            max_unit_cost=1.0 / (1.0 + cfg.min_roi),
            max_price=cfg.max_price,
            budget=cfg.max_position_usd,
        )
        if fill.cost < cfg.min_depth_usd or fill.units < max(book.min_order_size, m.min_order_size):
            return None
        avg = fill.avg_price(0)
        if avg < cfg.min_price:
            return None

        roi = (fill.units - fill.cost) / fill.cost
        days = pick.days_to_end
        outcome = m.outcomes[pick.side] if len(m.outcomes) > pick.side else ("Yes", "No")[pick.side]
        notes = [f"Market-implied chance of losing the stake: {(1 - avg) * 100:.1f}%."]
        if days < 0:
            notes.append(f"End date passed {-days:.1f} days ago; awaiting resolution (check for disputes).")
        elif days < 1:
            notes.append(f"Ends in {days * 24:.1f} hours.")
        if fill.cost >= cfg.max_position_usd - 1:
            notes.append(f"Depth exceeds the ${cfg.max_position_usd:,.0f} position cap.")

        leg = Leg.for_market(m, pick.side, shares=fill.units, avg_price=avg,
                             limit_price=fill.limit_prices[0], label=m.question)
        annualized = ctx.annualize(roi, days)
        return Opportunity(
            strategy=self.name,
            kind=f"buy_{outcome.lower()}",
            title=m.question,
            subtitle=f"Buy {outcome} @ {avg:.3f}",
            url=m.url,
            cost=fill.cost,
            payout=fill.units,
            roi=roi,
            annualized=annualized,
            days_to_end=days,
            risk=f"{(1 - avg) * 100:.1f}% loss",
            notes=notes,
            legs=[leg],
            win_probability=avg,
            score=annualized or 0.0,
        )
