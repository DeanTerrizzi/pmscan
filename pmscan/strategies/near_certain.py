"""Near-certain bets: buy the side of an outcome that almost never happens, close to the end.

The same mechanics as high_prob, with narrower settings (see NearCertainConfig): positions
priced 97-99.5c that resolve within 3 days, in politics only by default. Each bet returns
about 0.5-3%; one loss costs the whole stake, so it takes roughly 30-100 wins to cover it.
The dashboard pairs each row with how its category and price did in the backtest.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..backtest import history_group, price_bucket
from ..models import Opportunity, OrderBook
from .base import Candidate, ScanContext
from .high_prob import HighProbability, _Pick


class NearCertain(HighProbability):
    name = "near_certain"
    title = "Near-certain bets"

    def evaluate(
        self, candidate: Candidate, books: Mapping[str, OrderBook], ctx: ScanContext
    ) -> Opportunity | None:
        opp = super().evaluate(candidate, books, ctx)
        if opp is None:
            return None
        m = candidate.payload.market if isinstance(candidate.payload, _Pick) else None
        price = opp.legs[0].avg_price
        if opp.profit > 0:
            opp.notes.insert(1, f"One loss costs as much as about {opp.cost / opp.profit:,.0f} wins.")
        opp.details = {
            "group": history_group(m.category, m.market_type) if m else "",
            "bucket": price_bucket(price),
        }
        return opp
