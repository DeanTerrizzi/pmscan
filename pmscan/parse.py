"""Convert raw Gamma / CLOB JSON into model objects.

Gamma encodes several list fields (``outcomes``, ``clobTokenIds``, ``outcomePrices``)
as JSON strings, and numeric fields are sometimes strings, so everything is parsed
defensively.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import NO_FEES, Event, FeeSchedule, Level, Market, OrderBook, Team


def _float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    value = value.replace("Z", "+00:00")
    if len(value) > 3 and value[-3] in "+-" and value[-2:].isdigit():
        value += ":00"  # "…+00" -> "…+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _price(value: Any) -> float | None:
    """A quoted price, or None when the side is empty (Gamma reports 0 or 1 then)."""
    p = _float(value)
    if p is None or p <= 0.0 or p >= 1.0:
        return None
    return p


def parse_fees(raw: dict) -> FeeSchedule:
    if not raw.get("feesEnabled"):
        return NO_FEES
    schedule = raw.get("feeSchedule") or {}
    rate = _float(schedule.get("rate"), 0.0) or 0.0
    exponent = _float(schedule.get("exponent"), 1.0) or 1.0
    return FeeSchedule(rate=rate, exponent=exponent) if rate > 0 else NO_FEES


def parse_market(raw: dict, event_slug: str = "", event_title: str = "") -> Market | None:
    token_ids = tuple(str(t) for t in _json_list(raw.get("clobTokenIds")))
    if len(token_ids) != 2:
        return None
    outcomes = tuple(str(o) for o in _json_list(raw.get("outcomes"))) or ("Yes", "No")
    tradeable = bool(
        raw.get("active")
        and not raw.get("closed")
        and raw.get("enableOrderBook", True)
        and raw.get("acceptingOrders", True)
    )
    return Market(
        id=str(raw.get("id", "")),
        condition_id=str(raw.get("conditionId", "")),
        question=str(raw.get("question", "")),
        slug=str(raw.get("slug", "")),
        group_title=str(raw.get("groupItemTitle") or ""),
        outcomes=outcomes,
        token_ids=token_ids,
        best_bid=_price(raw.get("bestBid")),
        best_ask=_price(raw.get("bestAsk")),
        liquidity=_float(raw.get("liquidityNum"), None) or _float(raw.get("liquidity"), 0.0) or 0.0,
        volume_24h=_float(raw.get("volume24hr"), 0.0) or 0.0,
        end_date=parse_datetime(raw.get("endDate")),
        neg_risk=bool(raw.get("negRisk")),
        neg_risk_id=raw.get("negRiskMarketID") or None,
        is_other=bool(raw.get("negRiskOther")),
        tradeable=tradeable,
        fees=parse_fees(raw),
        min_order_size=_float(raw.get("orderMinSize"), 0.0) or 0.0,
        game_start=parse_datetime(raw.get("gameStartTime")),
        sports_market_type=str(raw.get("sportsMarketType") or ""),
        version=str(raw.get("version") or "v1"),
        seconds_delay=_float(raw.get("secondsDelay"), 0.0) or 0.0,
        event_slug=event_slug,
        event_title=event_title,
    )


def _resolved_yes(raw: dict) -> bool:
    prices = [_float(p, 0.0) for p in _json_list(raw.get("outcomePrices"))]
    return bool(prices) and (prices[0] or 0.0) >= 0.99


def _teams(raw: dict) -> tuple[Team, ...]:
    teams = []
    for t in raw.get("teams") or []:
        if isinstance(t, dict) and t.get("name"):
            teams.append(Team(str(t["name"]), str(t.get("abbreviation") or ""), t.get("ordering") == "home"))
    return tuple(teams)


def parse_event(raw: dict) -> Event:
    slug = str(raw.get("slug", ""))
    title = str(raw.get("title", ""))
    markets: list[Market] = []
    resolved_yes: set[str] = set()
    for rm in raw.get("markets") or []:
        if rm.get("closed"):
            if rm.get("negRisk") and rm.get("negRiskMarketID") and _resolved_yes(rm):
                resolved_yes.add(rm["negRiskMarketID"])
            continue
        m = parse_market(rm, slug, title)
        if m is not None:
            markets.append(m)
    return Event(
        id=str(raw.get("id", "")),
        slug=slug,
        title=title,
        neg_risk=bool(raw.get("negRisk") or raw.get("enableNegRisk")),
        augmented=bool(raw.get("enableNegRisk") and raw.get("negRiskAugmented")),
        markets=markets,
        resolved_yes_groups=resolved_yes,
        tags=tuple(str(t.get("label", "")) for t in raw.get("tags") or [] if isinstance(t, dict)),
        live=bool(raw.get("live")),
        ended=bool(raw.get("ended")),
        sport=str((raw.get("sport") or {}).get("sport") or "") if isinstance(raw.get("sport"), dict) else "",
        teams=_teams(raw),
        start_time=parse_datetime(raw.get("startTime")),
    )


def _levels(raw_levels: Any, *, descending: bool) -> list[Level]:
    levels = []
    for lvl in raw_levels or []:
        price, size = _float(lvl.get("price")), _float(lvl.get("size"))
        if price is not None and size and size > 0 and 0.0 < price < 1.0:
            levels.append(Level(price, size))
    levels.sort(key=lambda l: l.price, reverse=descending)
    return levels


def parse_book(raw: dict) -> OrderBook:
    return OrderBook(
        token_id=str(raw.get("asset_id", "")),
        bids=_levels(raw.get("bids"), descending=True),
        asks=_levels(raw.get("asks"), descending=False),
        min_order_size=_float(raw.get("min_order_size"), 0.0) or 0.0,
    )
