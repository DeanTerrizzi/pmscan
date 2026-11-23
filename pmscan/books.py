"""Order-book math: how much of a basket can be bought below a target unit cost."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import FeeSchedule, Level

_EPS = 1e-9


@dataclass(slots=True)
class BasketFill:
    """Result of buying equal share counts of every leg in a basket."""

    units: float = 0.0  # shares bought of each leg
    cost: float = 0.0  # total USDC including taker fees
    fees: float = 0.0
    spent: list[float] = field(default_factory=list)  # per-leg USDC excluding fees
    limit_prices: list[float] = field(default_factory=list)  # worst price touched per leg

    @property
    def unit_cost(self) -> float:
        return self.cost / self.units if self.units else 0.0

    def avg_price(self, leg: int) -> float:
        return self.spent[leg] / self.units if self.units else 0.0


def cost_for_shares(ladder: Sequence[Level], shares: float, fees: FeeSchedule) -> tuple[float, float] | None:
    """(notional, taker fees) to buy ``shares`` from an ask ladder, or None if too thin."""
    notional = fee_total = 0.0
    left = shares
    for level in ladder:
        take = min(left, level.size)
        notional += take * level.price
        fee_total += take * fees.per_share(level.price)
        left -= take
        if left <= _EPS:
            return notional, fee_total
    return None


def walk_basket(
    ladders: Sequence[Sequence[Level]],
    fees: Sequence[FeeSchedule],
    *,
    max_unit_cost: float,
    max_price: float | None = None,
    budget: float | None = None,
) -> BasketFill:
    """Buy one share of every leg at a time, walking each ask ladder, while the
    marginal cost of one more full basket (prices plus taker fees) stays at or below
    ``max_unit_cost``.

    ``ladders`` are ask levels sorted best (lowest) first. ``max_price`` caps the
    price of any single level; ``budget`` caps total USDC spent.
    """
    n = len(ladders)
    fill = BasketFill(spent=[0.0] * n, limit_prices=[0.0] * n)
    if n == 0 or any(not ladder for ladder in ladders):
        return fill

    idx = [0] * n
    remaining = [ladder[0].size for ladder in ladders]
    while True:
        levels = [ladders[i][idx[i]] for i in range(n)]
        if max_price is not None and any(l.price > max_price + _EPS for l in levels):
            break
        unit_fees = [fees[i].per_share(levels[i].price) for i in range(n)]
        unit = sum(l.price for l in levels) + sum(unit_fees)
        if unit > max_unit_cost + _EPS:
            break

        qty = min(remaining)
        budget_bound = False
        if budget is not None:
            affordable = (budget - fill.cost) / unit
            if affordable <= _EPS:
                break
            if affordable < qty:
                qty, budget_bound = affordable, True

        fill.units += qty
        fill.cost += qty * unit
        fill.fees += qty * sum(unit_fees)
        for i in range(n):
            fill.spent[i] += qty * levels[i].price
            fill.limit_prices[i] = max(fill.limit_prices[i], levels[i].price)
            remaining[i] -= qty
        if budget_bound:
            break

        for i in range(n):
            if remaining[i] <= _EPS:
                idx[i] += 1
                if idx[i] >= len(ladders[i]):
                    return fill
                remaining[i] = ladders[i][idx[i]].size
    return fill
