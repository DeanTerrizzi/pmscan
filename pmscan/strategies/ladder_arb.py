"""Ladder arbitrage: nested markets priced out of order.

Some events list markets that are logically nested:

* thresholds: "Will BTC be above $110k?" implies "... above $100k?"
* deadlines: "Will X happen by June 30?" implies "... by December 31?"
* totals: "O/U 3.5" Over implies "O/U 2.5" Over

If A implies B then P(A) <= P(B). When the market prices A above B (A's YES bid is
higher than B's YES ask), buy YES on B and NO on A:

| Outcome            | YES B | NO A | Total |
|--------------------|-------|------|-------|
| A happens (so B)   | 1     | 0    | 1     |
| B but not A        | 1     | 1    | 2     |
| neither            | 0     | 1    | 1     |

That pays at least $1 per pair for a cost of ask(B) + (1 - bid(A)) < $1.

The nesting is inferred from the question wording. Markets in one event are grouped
by their question with numbers and dates blanked out; a group qualifies only when the
questions differ in exactly one number or date and the wording around it states a
direction ("above", "dip to", "or more", "by <date>", ...). Anything ambiguous
("between", ranges, neg-risk brackets, unknown phrasing) is skipped. Always read the
two markets' rules before trading one of these.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..books import walk_basket
from ..models import Event, Leg, Market, Opportunity, OrderBook
from .base import Candidate, ScanContext, Strategy

_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_TOKEN = re.compile(
    rf"(?P<date>\b{_MONTHS}\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s+(?:19|20)\d{{2}})?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|(?<![$€£\d,.])\b(?:19|20)\d{2}\b(?![\d,%]))"
    r"|(?P<num>(?<![\w.])[$€£]?\d+(?:,\d{3})*(?:\.\d+)?"
    r"(?:\s*(?:k|m|b|t|bn|mn|thousand|million|billion|trillion)\b|%)?)",
    re.IGNORECASE,
)
_SUFFIX = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
           "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}

# Words just before / after the varying number that say which way the ladder runs.
# UP: a higher threshold is the stricter statement. DOWN: a lower one is.
_UP_BEFORE = re.compile(
    # "hit" is deliberately absent: "hit $80k" can mean a fall as easily as a rise.
    r"\b(above|over|greater than|more than|higher than|at least|reach(es)?|exceed(s)?|"
    r"surpass(es)?|o/u)\s*[$€£]?\s*$|[>≥]\s*$"
)
_DOWN_BEFORE = re.compile(
    r"\b(below|under|less than|fewer than|lower than|at most|(dip|dips|drop|drops|fall|falls|sink|sinks)"
    r"( to| below| under)?)\s*[$€£]?\s*$|[<≤]\s*$"
)
_UP_AFTER = re.compile(r"^\s*(\+|or more\b|or higher\b|or above\b|or greater\b)")
_DOWN_AFTER = re.compile(r"^\s*(or less\b|or lower\b|or below\b|or fewer\b)")
_BLOCK = re.compile(r"\b(between|exactly|range)\b|<n>\s*(-|–|to)\s*<n>")
_DEADLINE_BEFORE = re.compile(r"\b(by|before)(\s+(the\s+)?end\s+of)?\s*$")


def tokenize(question: str) -> tuple[str, list[tuple[str, str]]]:
    """Lowercased question with dates as <d> and numbers as <n>, plus the slots in order."""
    slots: list[tuple[str, str]] = []

    def sub(m: re.Match) -> str:
        kind = "d" if m.group("date") else "n"
        slots.append((kind, m.group(0).strip()))
        return f"<{kind}>"

    text = " ".join(question.lower().split())
    return _TOKEN.sub(sub, text), slots


def parse_number(raw: str) -> float | None:
    m = re.match(r"[$€£]?(\d+(?:,\d{3})*(?:\.\d+)?)\s*([a-z]+|%)?$", raw.strip().lower())
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    suffix = m.group(2)
    if suffix and suffix != "%":
        if suffix not in _SUFFIX:
            return None
        value *= _SUFFIX[suffix]
    return value


def _slot_context(skeleton: str, slot_index: int) -> tuple[str, str]:
    """Text before and after the slot_index-th placeholder in the skeleton."""
    positions = [m.start() for m in re.finditer(r"<[dn]>", skeleton)]
    start = positions[slot_index]
    return skeleton[:start], skeleton[start + 3 :]


def _mid(m: Market) -> float | None:
    if m.best_bid is None or m.best_ask is None:
        return None
    return (m.best_bid + m.best_ask) / 2


def prices_consistent(markets: Sequence[Market], tolerance: float = 0.02) -> bool:
    """Sanity check on the inferred direction: in a real ladder, most neighbouring
    rungs are priced in order. If many are inverted, the wording was misread."""
    mids = [x for m in markets if (x := _mid(m)) is not None]
    pairs = list(zip(mids, mids[1:], strict=False))  # neighbours
    if not pairs:
        return True
    inverted = sum(1 for strict, loose in pairs if strict > loose + tolerance)
    return inverted <= max(1, len(pairs) // 4)


@dataclass
class Ladder:
    """Markets ordered from the strictest statement to the loosest."""

    kind: str  # "threshold" or "deadline"
    markets: list[Market]


def _yes_like(m: Market) -> bool:
    outcomes = tuple(o.lower() for o in m.outcomes)
    return outcomes in (("yes", "no"), ("over", "under"))


def find_ladders(event: Event) -> list[Ladder]:
    groups: dict[str, list[tuple[Market, list[tuple[str, str]]]]] = defaultdict(list)
    for m in event.markets:
        if m.neg_risk or not m.tradeable or not _yes_like(m):
            continue  # neg-risk markets are mutually exclusive brackets, not nested
        skeleton, slots = tokenize(m.question)
        if slots:
            groups[skeleton].append((m, slots))

    ladders = []
    for skeleton, members in groups.items():
        if len(members) < 2 or _BLOCK.search(skeleton):
            continue
        n_slots = len(members[0][1])
        varying = [i for i in range(n_slots) if len({s[i][1] for _, s in members}) > 1]
        if len(varying) != 1:
            continue
        i = varying[0]
        kind = members[0][1][i][0]
        before, after = _slot_context(skeleton, i)
        markets = [m for m, _ in members]

        if kind == "n":
            up = bool(_UP_BEFORE.search(before) or _UP_AFTER.search(after))
            down = bool(_DOWN_BEFORE.search(before) or _DOWN_AFTER.search(after))
            if up == down:
                continue  # no direction, or contradictory wording
            values = [parse_number(s[i][1]) for _, s in members]
            if any(v is None for v in values) or len(set(values)) != len(values):
                continue
            ends = {m.end_date.date() if m.end_date else None for m in markets}
            if len(ends) != 1:
                continue  # thresholds must resolve on the same date
            order = [m for _, m in sorted(zip(values, markets, strict=True), key=lambda vm: vm[0], reverse=up)]
            arrows = {("↑" in m.group_title, "↓" in m.group_title) for m in markets}
            if len(arrows) > 1:
                continue  # mixed up/down targets in one wording group
            if prices_consistent(order):
                ladders.append(Ladder("threshold", order))
        else:
            if not _DEADLINE_BEFORE.search(before):
                continue  # "in June" isn't cumulative; only "by/before <date>" is
            if any(m.end_date is None for m in markets) or len({m.end_date for m in markets}) != len(markets):
                continue
            order = sorted(markets, key=lambda m: m.end_date)
            if prices_consistent(order):
                ladders.append(Ladder("deadline", order))
    return ladders


@dataclass
class _Pair:
    event: Event
    kind: str
    strict: Market  # A: buy NO
    loose: Market  # B: buy YES
    top_edge: float
    ladder: Ladder | None = None


class LadderArbitrage(Strategy):
    name = "ladder_arb"
    title = "Ladder arbitrage"

    def screen(self, events: Sequence[Event], ctx: ScanContext) -> list[Candidate]:
        cfg = ctx.config.ladder_arb
        out: list[Candidate] = []
        n_ladders = 0
        examples = []
        for event in events:
            for ladder in find_ladders(event):
                n_ladders += 1
                if len(examples) < 5:
                    examples.append({"event": event.title, "kind": ladder.kind,
                                     "strict_to_loose": [m.label for m in ladder.markets][:6]})
                pairs = []
                ms = ladder.markets
                for a in range(len(ms)):
                    for b in range(a + 1, len(ms)):
                        strict, loose = ms[a], ms[b]
                        if strict.best_bid is None or loose.best_ask is None:
                            continue
                        no_price = 1.0 - strict.best_bid
                        cost = (loose.best_ask + loose.fees.per_share(loose.best_ask)
                                + no_price + strict.fees.per_share(no_price))
                        edge = 1.0 - cost
                        ctx.watch(event.id, edge / max(cost, 1e-9))
                        if edge > -cfg.screen_slack:
                            pairs.append(_Pair(event, ladder.kind, strict, loose, edge, ladder))
                pairs.sort(key=lambda p: p.top_edge, reverse=True)
                for p in pairs[: cfg.max_pairs_per_ladder]:
                    out.append(Candidate([p.strict.token_ids[1], p.loose.token_ids[0]], p.top_edge, p))
        ctx.stats["ladders"] = n_ladders
        ctx.stats["ladder_examples"] = examples
        return out

    def evaluate(
        self, candidate: Candidate, books: Mapping[str, OrderBook], ctx: ScanContext
    ) -> Opportunity | None:
        cfg = ctx.config.ladder_arb
        pair: _Pair = candidate.payload
        no_book, yes_book = (books.get(t) for t in candidate.token_ids)
        if no_book is None or yes_book is None or not no_book.asks or not yes_book.asks:
            return None
        fill = walk_basket(
            [no_book.asks, yes_book.asks],
            [pair.strict.fees, pair.loose.fees],
            max_unit_cost=1.0 / (1.0 + cfg.min_roi),
            budget=cfg.max_capital_usd,
        )
        min_shares = max(pair.strict.min_order_size, pair.loose.min_order_size,
                         no_book.min_order_size, yes_book.min_order_size)
        if fill.units < max(min_shares, 1e-9):
            return None
        profit = fill.units - fill.cost
        if profit < cfg.min_profit_usd:
            return None
        roi = profit / fill.cost
        ends = [d for m in (pair.strict, pair.loose) if (d := m.days_to_end(ctx.now)) is not None]
        days = max(ends) if ends else None
        legs = [
            Leg.for_market(pair.strict, 1, shares=fill.units, avg_price=fill.avg_price(0),
                           limit_price=fill.limit_prices[0], label=pair.strict.question),
            Leg.for_market(pair.loose, 0, shares=fill.units, avg_price=fill.avg_price(1),
                           limit_price=fill.limit_prices[1], label=pair.loose.question),
        ]
        notes = [
            f"“{pair.strict.question}” implies “{pair.loose.question}”, "
            "so it can't be the more likely of the two, yet it's priced higher.",
            f"Buy {fill.units:,.1f} {legs[0].outcome} on the first and {legs[1].outcome} on the second: "
            "pays at least $1 per pair, $2 if only the second comes true.",
            "Detected from question wording: confirm both markets' rules really nest before trading.",
        ]
        return Opportunity(
            strategy=self.name,
            kind=f"ladder_{pair.kind}",
            title=pair.event.title or pair.loose.question,
            subtitle=f"{legs[1].outcome} {pair.loose.label} + {legs[0].outcome} {pair.strict.label}",
            url=pair.event.url,
            cost=fill.cost,
            payout=fill.units,
            roi=roi,
            annualized=ctx.annualize(roi, days),
            days_to_end=days,
            risk="check wording",
            notes=notes,
            legs=legs,
            score=profit,
            payout_per_share=1.0,
            details={
                "ladder_kind": pair.kind,
                # Top-of-book quotes for every rung, strictest first, for charting.
                "rungs": [{"label": m.label, "question": m.question, "bid": m.best_bid, "ask": m.best_ask,
                           "url": m.url} for m in (pair.ladder.markets if pair.ladder else [pair.strict, pair.loose])],
                "strict_index": pair.ladder.markets.index(pair.strict) if pair.ladder else 0,
                "loose_index": pair.ladder.markets.index(pair.loose) if pair.ladder else 1,
            },
        )
