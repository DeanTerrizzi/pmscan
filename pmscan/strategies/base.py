"""Strategy interface.

A scan runs in two phases so that only promising markets cost an order-book fetch:

1. ``screen`` looks at Gamma's top-of-book quotes for every market and returns
   cheap ``Candidate`` objects naming the tokens whose books need checking.
2. ``evaluate`` receives the real CLOB order books and returns a sized, fee-adjusted
   ``Opportunity`` (or None if the edge isn't real).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import ScanConfig
from ..models import Event, Opportunity, OrderBook


@dataclass
class ScanContext:
    config: ScanConfig
    now: datetime
    stats: dict[str, Any] = field(default_factory=dict)
    venue: str = "international"  # "us" for Polymarket US

    def watch(self, event_id: str, closeness: float) -> None:
        """Note how close an event is to an edge (top-of-book return; above 0 is an edge), for the live feed."""
        watch = self.stats.setdefault("watch", {})
        if closeness > watch.get(event_id, float("-inf")):
            watch[event_id] = closeness

    def lock_days(self, days_to_end: float | None) -> float | None:
        """How long capital is tied up: time to end date plus settlement lag."""
        if days_to_end is None:
            return None
        return max(days_to_end, 0.0) + self.config.resolution_lag_days

    def annualize(self, roi: float, days_to_end: float | None) -> float | None:
        lock = self.lock_days(days_to_end)
        if lock is None or lock <= 0:
            return None
        return roi * 365.0 / lock


@dataclass
class Candidate:
    token_ids: list[str]
    priority: float  # higher = verify first
    payload: Any


class Strategy(ABC):
    name: str = ""
    title: str = ""

    @abstractmethod
    def screen(self, events: Sequence[Event], ctx: ScanContext) -> list[Candidate]: ...

    @abstractmethod
    def evaluate(
        self, candidate: Candidate, books: Mapping[str, OrderBook], ctx: ScanContext
    ) -> Opportunity | None: ...
