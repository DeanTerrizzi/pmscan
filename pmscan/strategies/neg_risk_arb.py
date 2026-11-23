"""Neg-risk basket arbitrage.

A neg-risk event groups mutually exclusive markets: at most one can resolve YES
("Who wins the election?" with one market per candidate). That gives two baskets
whose payout is known in advance:

* **Buy every YES.** If exactly one outcome wins, one share of each YES pays $1.
  Profitable when the YES asks (plus fees) sum to less than $1. This needs the
  listed outcomes to be exhaustive, so it is skipped when any outcome can't be
  traded and, by default, for "augmented" events where new outcomes can be added.

* **Buy every NO.** With k markets and at most one YES, k NO shares pay at least
  k - 1 dollars. Profitable when the NO asks (plus fees) sum to less than k - 1,
  i.e. when the YES bids sum to more than $1. This holds for any subset of the
  group, so it doesn't need the outcome list to be complete.

Note that a single binary market can't be arbitraged this way on Polymarket: the
YES and NO books are mirrors of one another (NO ask = 1 - YES bid), so YES ask +
NO ask is always at least $1.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..books import walk_basket
from ..models import Event, Leg, Market, Opportunity, OrderBook
from .base import Candidate, ScanContext, Strategy

BUY_YES = "buy_all_yes"
BUY_NO = "buy_all_no"


@dataclass
class _Basket:
    event: Event
    kind: str
    markets: list[Market]
    side: int  # token index: 0 = YES, 1 = NO
    payout_per_unit: float
    top_of_book_edge: float  # payout - cost per unit, from Gamma quotes


class NegRiskArbitrage(Strategy):
    name = "neg_risk_arb"
    title = "Neg-risk arbitrage"

    def screen(self, events: Sequence[Event], ctx: ScanContext) -> list[Candidate]:
        cfg = ctx.config.neg_risk_arb
        out: list[Candidate] = []
        near: list[tuple[float, str, str, str, int]] = []
        groups_seen = 0

        for event in events:
            groups: dict[str, list[Market]] = defaultdict(list)
            for m in event.markets:
                if m.neg_risk and m.neg_risk_id:
                    groups[m.neg_risk_id].append(m)

            for gid, members in groups.items():
                if gid in event.resolved_yes_groups:
                    continue  # one outcome already won; nothing left to arbitrage
                legs = [m for m in members if m.tradeable]
                if len(legs) < 2:
                    continue
                groups_seen += 1

                # Buy every YES: needs every outcome tradeable with an ask.
                complete = len(legs) == len(members)
                yes_ok = complete and (cfg.allow_augmented_buy_yes or not event.augmented)
                if yes_ok and all(m.best_ask is not None for m in legs):
                    cost = sum(m.best_ask + m.fees.per_share(m.best_ask) for m in legs)
                    edge = 1.0 - cost
                    near.append((edge / max(cost, 1e-9), BUY_YES, event.title, event.url, len(legs)))
                    ctx.watch(event.id, edge / max(cost, 1e-9))
                    if edge > -cfg.screen_slack:
                        out.append(self._candidate(_Basket(event, BUY_YES, legs, 0, 1.0, edge)))

                # Buy every NO: NO ask mirrors 1 - YES bid; skip outcomes with no bids.
                no_legs = [m for m in legs if m.best_bid is not None]
                if len(no_legs) >= 2:
                    cost = sum((1 - m.best_bid) + m.fees.per_share(1 - m.best_bid) for m in no_legs)
                    payout = len(no_legs) - 1.0
                    edge = payout - cost
                    near.append((edge / max(cost, 1e-9), BUY_NO, event.title, event.url, len(no_legs)))
                    ctx.watch(event.id, edge / max(cost, 1e-9))
                    if edge > -cfg.screen_slack:
                        out.append(self._candidate(_Basket(event, BUY_NO, no_legs, 1, payout, edge)))

        near.sort(key=lambda x: x[0], reverse=True)
        ctx.stats["neg_risk_groups"] = groups_seen
        # Top-of-book ROI of the groups closest to (or past) break-even, for monitoring.
        # A positive value that isn't listed as an opportunity was too thin to clear
        # min_profit_usd once the real order-book depth was walked.
        ctx.stats["neg_risk_closest"] = [
            {"roi": round(e, 5), "kind": k, "event": t, "url": u, "legs": n} for e, k, t, u, n in near[:40]
        ]
        return out

    @staticmethod
    def _candidate(basket: _Basket) -> Candidate:
        return Candidate(
            token_ids=[m.token_ids[basket.side] for m in basket.markets],
            priority=basket.top_of_book_edge,
            payload=basket,
        )

    def evaluate(
        self, candidate: Candidate, books: Mapping[str, OrderBook], ctx: ScanContext
    ) -> Opportunity | None:
        cfg = ctx.config.neg_risk_arb
        basket: _Basket = candidate.payload
        book_list = [books.get(t) for t in candidate.token_ids]
        if any(b is None or not b.asks for b in book_list):
            return None

        fill = walk_basket(
            [b.asks for b in book_list],
            [m.fees for m in basket.markets],
            max_unit_cost=basket.payout_per_unit / (1.0 + cfg.min_roi),
            budget=cfg.max_capital_usd,
        )
        min_shares = max(max(b.min_order_size, m.min_order_size) for b, m in zip(book_list, basket.markets, strict=True))
        if fill.units < max(min_shares, 1e-9):
            return None
        payout = fill.units * basket.payout_per_unit
        profit = payout - fill.cost
        if profit < cfg.min_profit_usd:
            return None

        roi = profit / fill.cost
        ends = [d for m in basket.markets if (d := m.days_to_end(ctx.now)) is not None]
        days = max(ends) if ends else None
        side = "YES" if basket.side == 0 else "NO"
        notes = [
            f"Buy {fill.units:,.1f} {side} shares in each of {len(basket.markets)} outcomes "
            f"(fees ${fill.fees:,.2f}).",
            "Prices move between orders: place every leg quickly, or the basket may stop being profitable.",
        ]
        if basket.kind == BUY_YES:
            risk = "low"
            notes.append("Only pays if one listed outcome resolves YES; check the rules for a 'none of these' case.")
            if basket.event.augmented:
                risk = "medium"
                notes.append("Augmented neg-risk event: outcomes may be added later, so the listed set can be incomplete.")
        else:
            risk = "low"
            notes.append(f"Pays at least ${basket.payout_per_unit:,.0f} per basket whichever outcome wins.")

        legs = [
            Leg.for_market(m, basket.side, shares=fill.units, avg_price=fill.avg_price(i),
                           limit_price=fill.limit_prices[i])
            for i, m in enumerate(basket.markets)
        ]
        annualized = ctx.annualize(roi, days)
        if annualized is not None and annualized < 0.05 and days is not None:
            notes.append(
                f"Capital is locked ~{days:,.0f} days, so this is only {annualized * 100:.1f}%/yr; "
                "a risk-free rate may beat it."
            )
        return Opportunity(
            strategy=self.name,
            kind=basket.kind,
            title=basket.event.title,
            subtitle=f"{'Buy every YES' if basket.side == 0 else 'Buy every NO'} · {len(legs)} legs",
            url=basket.event.url,
            cost=fill.cost,
            payout=payout,
            roi=roi,
            annualized=annualized,
            days_to_end=days,
            risk=risk,
            notes=notes,
            legs=legs,
            score=profit,
            payout_per_share=basket.payout_per_unit,
        )
