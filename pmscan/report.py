"""Terminal output with rich."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .models import Opportunity
from .scanner import ScanResult

SORT_KEYS = {
    "score": lambda o: o.score,
    "profit": lambda o: o.profit,
    "roi": lambda o: o.roi,
    "annualized": lambda o: o.annualized or 0.0,
    "win": lambda o: o.win_probability or 0.0,
    "soonest": lambda o: -(o.days_to_end if o.days_to_end is not None else 1e9),
}


def _pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "-"
    if abs(x) >= 100:  # 10,000%+ annualized is noise; don't pretend to precision
        return f"{x * 100:,.0f}%"
    return f"{x * 100:.{digits}f}%"


def _days(d: float | None) -> str:
    if d is None:
        return "-"
    if d < 0:
        return f"ended {-d:.1f}d ago"
    if d < 1:
        return f"{d * 24:.1f}h"
    return f"{d:.1f}d"


def _link(text: str, url: str) -> str:
    return f"[link={url}]{escape(text)}[/link]"


def _table(title: str, columns: list[str]) -> Table:
    """First column is the flexible name column; the rest are compact and never wrap."""
    t = Table(title=title, title_justify="left", expand=True, pad_edge=False)
    t.add_column(columns[0], ratio=1, min_width=20, no_wrap=True, overflow="ellipsis")
    for col in columns[1:]:
        t.add_column(col, justify="right", no_wrap=True)
    return t


def _arb_table(opps: list[Opportunity]) -> Table:
    t = _table("Neg-risk arbitrage (payout fixed in advance)",
               ["Event", "Basket", "Cost", "Profit", "ROI", "Annual", "Ends", "Risk"])
    for o in opps:
        basket = f"{'YES' if o.kind == 'buy_all_yes' else 'NO'} x{len(o.legs)}"
        t.add_row(
            _link(o.title, o.url), basket, f"${o.cost:,.0f}", f"[green]${o.profit:,.2f}[/green]",
            _pct(o.roi, 2), _pct(o.annualized), _days(o.days_to_end), o.risk,
        )
    return t


def _hp_table(opps: list[Opportunity]) -> Table:
    title = ("Near-certain bets (one loss costs 30-100 wins)" if opps and opps[0].strategy == "near_certain"
             else "High-probability yield (NOT risk-free: price ~= implied win chance)")
    t = _table(title,
               ["Market", "Buy", "Price", "ROI if win", "Annual", "Ends", "Depth"])
    for o in opps:
        leg = o.legs[0]
        outcome = leg.outcome if len(leg.outcome) <= 12 else leg.outcome[:11] + "…"
        t.add_row(
            _link(o.title, o.url), escape(outcome), f"{leg.avg_price * 100:.1f}¢",
            _pct(o.roi, 2), _pct(o.annualized), _days(o.days_to_end), f"${o.cost:,.0f}",
        )
    return t


def _ladder_table(opps: list[Opportunity]) -> Table:
    t = _table("Ladder arbitrage (nested markets priced out of order; check the wording)",
               ["Event", "Buy", "Cost", "Profit", "ROI", "Annual", "Ends"])
    for o in opps:
        buy = f"{o.legs[1].outcome} + {o.legs[0].outcome}"
        t.add_row(
            _link(f"{o.title}: {o.subtitle}", o.url), escape(buy), f"${o.cost:,.0f}",
            f"[green]${o.profit:,.2f}[/green]", _pct(o.roi, 2), _pct(o.annualized), _days(o.days_to_end),
        )
    return t


def _value_table(opps: list[Opportunity]) -> Table:
    t = _table("Sportsbook value (model: bookmaker consensus vs Polymarket)",
               ["Game", "Bet", "Price", "Books", "EV", "Kelly", "Starts", "Depth"])
    for o in opps:
        d = o.details
        bet = d.get("bet", o.subtitle)
        bet = bet if len(bet) <= 22 else bet[:21] + "…"
        t.add_row(
            _link(o.title, o.url), escape(bet), f"{o.legs[0].avg_price * 100:.1f}¢",
            f"{d.get('fair', 0) * 100:.1f}%", f"[green]{d.get('ev', 0) * 100:+.1f}%[/green]",
            _pct(d.get("kelly"), 1), _days(o.days_to_end), f"${o.cost:,.0f}",
        )
    return t


def _none_message(name: str, s: dict) -> str:
    if name == "neg_risk_arb":
        msg = f"Neg-risk arbitrage: none right now across {s.get('neg_risk_groups', 0):,} groups."
        closest = s.get("neg_risk_closest") or []
        if closest:
            best = closest[0]
            msg += (f"\n  Closest at top of book: {_link(best['event'], best.get('url', ''))} "
                    f"({best['kind']}) at {best['roi'] * 100:+.2f}%; too thin or gone once depth was checked.")
        return msg
    if name == "ladder_arb":
        return f"Ladder arbitrage: none right now across {s.get('ladders', 0):,} ladders."
    if name == "sportsbook_value":
        status = s.get("sportsbook_status", "")
        if status.startswith("inactive"):
            return f"Sportsbook value: {escape(status)}."
        return (f"Sportsbook value: nothing above the edge threshold across {s.get('sportsbook_matched', 0):,} "
                f"matched games (Odds API credits left: {s.get('odds_api_requests_remaining') or '?'}).")
    if name == "near_certain":
        return "Near-certain bets: nothing at 97-99.5c ending within 3 days passed the filters."
    return "High-probability yield: nothing passed the filters."


TABLES = {"neg_risk_arb": _arb_table, "ladder_arb": _ladder_table, "high_prob": _hp_table,
          "near_certain": _hp_table, "sportsbook_value": _value_table}


def print_result(result: ScanResult, console: Console, top_n: int = 25, sort: str = "score") -> None:
    s = result.stats
    console.print(
        f"[bold]Scan {result.started_at:%Y-%m-%d %H:%M:%S} UTC[/bold] · {s.get('events', 0):,} events · "
        f"{s.get('tradeable_markets', 0):,} tradeable markets · {s.get('books_fetched', 0):,} books checked · "
        f"{result.duration_s:.1f}s"
    )
    key = SORT_KEYS.get(sort, SORT_KEYS["score"])
    for name, opps in result.opportunities.items():
        ranked = sorted(opps, key=key, reverse=True)[:top_n]
        if ranked and name in TABLES:
            console.print(TABLES[name](ranked))
        else:
            console.print(f"\n[dim]{_none_message(name, s)}[/dim]")
