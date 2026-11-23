"""Run a full scan: fetch every open event, screen, verify against order books, rank."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import PolymarketClient
from .config import ScanConfig
from .models import Event, Opportunity
from .strategies import ALL_STRATEGIES, Candidate, ScanContext, Strategy

Progress = Callable[[str], None]
HISTORY_STRATEGIES = frozenset({"neg_risk_arb", "ladder_arb", "sportsbook_value"})


@dataclass
class ScanResult:
    started_at: datetime
    duration_s: float
    stats: dict[str, Any]
    opportunities: dict[str, list[Opportunity]]  # strategy name -> ranked list
    strategy_titles: dict[str, str] = field(default_factory=dict)
    # Parsed events, kept for the dashboard's market explorer. Not serialized.
    events: list[Event] = field(default_factory=list, repr=False)

    def summary(self) -> dict[str, Any]:
        """One compact line per scan, for trend charts."""
        s = self.stats
        out: dict[str, Any] = {
            "ts": self.started_at.isoformat(), "duration_s": round(self.duration_s, 1),
            "events": s.get("events"), "tradeable_markets": s.get("tradeable_markets"),
            "books_fetched": s.get("books_fetched"),
        }
        for name, opps in self.opportunities.items():
            out[name] = {
                "count": len(opps),
                "profit": round(sum(o.profit for o in opps), 4),
                "best_roi": round(max((o.roi for o in opps), default=0.0), 5),
                "best_score": round(max((o.score for o in opps), default=0.0), 5),
            }
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "duration_s": round(self.duration_s, 2),
            "stats": self.stats,
            "strategies": [
                {"name": name, "title": self.strategy_titles.get(name, name), "opportunities": [o.to_dict() for o in opps]}
                for name, opps in self.opportunities.items()
            ],
        }

    def save(self, data_dir: Path) -> Path:
        """Write latest.json, append this scan's summary to scans.jsonl, and append one
        line per arbitrage / value opportunity to history.jsonl. (High-probability rows
        number in the hundreds per scan, so they're counted in scans.jsonl instead.)"""
        data_dir.mkdir(parents=True, exist_ok=True)
        latest = data_dir / "latest.json"
        tmp = latest.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1))
        tmp.replace(latest)
        with open(data_dir / "scans.jsonl", "a") as fh:
            fh.write(json.dumps(self.summary()) + "\n")
        with open(data_dir / "history.jsonl", "a") as fh:
            ts = self.started_at.isoformat()
            for name, opps in self.opportunities.items():
                if name not in HISTORY_STRATEGIES:
                    continue
                for o in opps:
                    fh.write(json.dumps({
                        "ts": ts, "strategy": name, "kind": o.kind, "title": o.title, "url": o.url,
                        "cost": round(o.cost, 2), "profit": round(o.profit, 2), "roi": round(o.roi, 5),
                        "annualized": None if o.annualized is None else round(o.annualized, 4),
                    }) + "\n")
        return latest


class Scanner:
    def __init__(
        self,
        client: PolymarketClient,
        config: ScanConfig,
        strategies: Sequence[str] | None = None,
    ) -> None:
        self.client = client
        self.config = config
        names = list(strategies) if strategies else [
            n for n in ALL_STRATEGIES if getattr(getattr(config, n, None), "enabled", True)
        ]
        unknown = [n for n in names if n not in ALL_STRATEGIES]
        if unknown:
            raise ValueError(f"unknown strategy: {', '.join(unknown)} (choose from {', '.join(ALL_STRATEGIES)})")
        self.strategies: list[Strategy] = [ALL_STRATEGIES[n]() for n in names]

    def fetch_events(self, progress: Progress | None = None) -> list[Event]:
        events: dict[str, Event] = {}
        markets = 0
        for page_no, page in enumerate(self.client.iter_events(), start=1):
            for event in page:
                if event.id not in events:
                    events[event.id] = event
                    markets += len(event.markets)
            if progress and page_no % 10 == 0:
                progress(f"Fetched {len(events):,} events / {markets:,} open markets")
        return list(events.values())

    def scan(self, progress: Progress | None = None) -> ScanResult:
        t0 = time.monotonic()
        started = datetime.now(timezone.utc)
        if progress:
            progress(f"Fetching events from {self.client.label}")
        events = self.fetch_events(progress)
        t_fetch = time.monotonic() - t0

        ctx = ScanContext(config=self.config, now=datetime.now(timezone.utc), venue=self.client.venue)
        screened: list[tuple[Strategy, Candidate]] = []
        for strategy in self.strategies:
            screened.extend((strategy, c) for c in strategy.screen(events, ctx))

        tokens = list(dict.fromkeys(t for _, c in screened for t in c.token_ids))
        if progress:
            progress(f"Verifying {len(screened):,} candidates against {len(tokens):,} order books")
        books = self.client.get_books(tokens)

        results: dict[str, list[Opportunity]] = {s.name: [] for s in self.strategies}
        for strategy, candidate in screened:
            opp = strategy.evaluate(candidate, books, ctx)
            if opp is not None:
                results[strategy.name].append(opp)
        for opps in results.values():
            opps.sort(key=lambda o: o.score, reverse=True)

        ctx.stats.update(
            events=len(events),
            open_markets=sum(len(e.markets) for e in events),
            tradeable_markets=sum(1 for e in events for m in e.markets if m.tradeable),
            candidates=len(screened),
            books_fetched=len(books),
            fetch_s=round(t_fetch, 1),
        )
        ctx.stats["found"] = {name: len(opps) for name, opps in results.items()}
        watch = ctx.stats.get("watch", {})
        ctx.stats["watch"] = dict(sorted(watch.items(), key=lambda kv: kv[1], reverse=True)[:100])
        return ScanResult(
            started_at=started,
            duration_s=time.monotonic() - t0,
            stats=ctx.stats,
            opportunities=results,
            strategy_titles={s.name: s.title for s in self.strategies},
            events=events,
        )
