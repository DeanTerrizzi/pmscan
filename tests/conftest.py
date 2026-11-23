from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pmscan.config import ScanConfig
from pmscan.models import NO_FEES, Event, FeeSchedule, Level, Market, OrderBook
from pmscan.strategies import ScanContext

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_market(
    n: int,
    *,
    bid: float | None,
    ask: float | None,
    group: str = "g1",
    days: float = 10.0,
    fees: FeeSchedule = NO_FEES,
    tradeable: bool = True,
    liquidity: float = 10_000.0,
    volume: float = 10_000.0,
) -> Market:
    return Market(
        id=str(n), condition_id=f"c{n}", question=f"Outcome {n}?", slug=f"m{n}", group_title=f"Outcome {n}",
        outcomes=("Yes", "No"), token_ids=(f"y{n}", f"n{n}"), best_bid=bid, best_ask=ask,
        liquidity=liquidity, volume_24h=volume, end_date=NOW + timedelta(days=days),
        neg_risk=group is not None, neg_risk_id=group, is_other=False, tradeable=tradeable,
        fees=fees, min_order_size=5.0, event_slug="ev", event_title="Event",
    )


def make_event(markets: list[Market], *, augmented: bool = False) -> Event:
    return Event(id="e1", slug="ev", title="Event", neg_risk=True, augmented=augmented, markets=markets)


def book(token: str, asks: list[tuple[float, float]], bids: list[tuple[float, float]] = ()) -> OrderBook:
    return OrderBook(
        token_id=token,
        bids=[Level(p, s) for p, s in sorted(bids, reverse=True)],
        asks=[Level(p, s) for p, s in sorted(asks)],
        min_order_size=5.0,
    )


@pytest.fixture
def ctx() -> ScanContext:
    return ScanContext(config=ScanConfig(), now=NOW)
