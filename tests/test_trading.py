from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NOW, book

from pmscan.config import TradingConfig
from pmscan.models import Leg, Opportunity
from pmscan.trading import AutoTrader, Ledger, OrderResult


class FakeClient:
    def __init__(self, books):
        self.books = books

    def get_books(self, tokens):
        return {t: self.books[t] for t in tokens if t in self.books}


class FakeExchange:
    def __init__(self, statuses, sells="matched"):
        self.statuses = statuses  # one per leg: "matched", "rejected", "delayed"
        self.sell_status = sells
        self.submitted = []
        self.sold = []

    def submit(self, orders):
        self.submitted.append(list(orders))
        return [OrderResult(s != "rejected", s) for s in self.statuses]

    def sell(self, token_id, shares, min_price):
        self.sold.append((token_id, shares, min_price))
        return OrderResult(self.sell_status != "rejected", self.sell_status, shares=shares)


def leg(token, limit, **kw):
    return Leg(market=f"m{token}", outcome="Yes", token_id=token, shares=100, avg_price=limit,
               limit_price=limit, url="u", min_order_size=5.0, **kw)


def arb(legs=None, strategy="neg_risk_arb"):
    legs = legs or [leg("y0", 0.50), leg("y1", 0.40)]
    return Opportunity(strategy=strategy, kind="buy_all_yes", title="Event", subtitle="", url="https://x",
                       cost=90, payout=100, roi=0.11, annualized=None, days_to_end=10, risk="low",
                       notes=[], legs=legs, payout_per_share=1.0)


BOOKS = {"y0": book("y0", [(0.50, 100), (0.55, 100)]), "y1": book("y1", [(0.40, 100)])}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path)


def trader(ledger, books=BOOKS, exchange=None, **cfg):
    return AutoTrader(TradingConfig(**cfg), FakeClient(books), ledger, exchange)


def test_plan_sizes_to_budget_with_headroom_and_cent_amounts(ledger):
    plan = trader(ledger).plan(arb(), NOW)
    # $25 budget at $0.90 per basket = 27.78 baskets, minus ~1 share of rounding headroom
    assert plan.units == pytest.approx(26.75)
    assert [l.amount for l in plan.legs] == [Decimal("13.38"), Decimal("10.70")]
    assert [l.max_price for l in plan.legs] == [Decimal("0.5"), Decimal("0.4")]
    assert plan.value == pytest.approx(26.75)
    assert plan.edge == pytest.approx(26.75 - 24.08)


def test_plan_never_crosses_the_scanned_price(ledger):
    moved = {**BOOKS, "y0": book("y0", [(0.52, 100)])}
    assert trader(ledger, books=moved).plan(arb(), NOW) is None


def test_plan_rejects_edges_too_small_after_fees(ledger):
    tight = {"y0": book("y0", [(0.50, 100)]), "y1": book("y1", [(0.497, 100)])}
    assert trader(ledger, books=tight).plan(arb([leg("y0", 0.50), leg("y1", 0.497)]), NOW) is None


def test_daily_budget_counts_live_spend_only(ledger):
    ledger.record({"ts": NOW.isoformat(), "mode": "live", "spent": 90.0, "key": "k"})
    ledger.record({"ts": NOW.isoformat(), "mode": "dry_run", "spent": 500.0, "key": "k"})
    ledger.record({"ts": (NOW - timedelta(days=1)).isoformat(), "mode": "live", "spent": 500.0, "key": "k"})
    plan = trader(ledger).plan(arb(), NOW)
    assert plan.cost <= 10.0 + 0.05  # $100/day cap minus $90 spent today
    ledger.record({"ts": NOW.isoformat(), "mode": "live", "spent": 9.5, "key": "k"})
    assert trader(ledger).plan(arb(), NOW) is None


def test_value_bets_are_capped_at_fair_value(ledger):
    opp = replace(arb([leg("y0", 0.60)], strategy="sportsbook_value"), value_per_share=0.62)
    books = {"y0": book("y0", [(0.58, 10), (0.60, 100)])}
    plan = trader(ledger, books=books, strategies=["sportsbook_value"], max_usd_per_trade=1000).plan(opp, NOW)
    # The cap is fair / (1 + min_roi) = 0.617, so both levels qualify; the edge is valued at 62%.
    assert plan is not None and plan.value == pytest.approx(plan.units * 0.62)


def test_ineligible_reasons(ledger):
    t = trader(ledger)
    assert t.ineligible(arb(strategy="high_prob"), NOW) == "strategy not enabled for trading"
    assert "V2" in t.ineligible(arb([leg("y0", 0.5, version="v2"), leg("y1", 0.4)]), NOW)
    assert "delay" in t.ineligible(arb([leg("y0", 0.5, seconds_delay=3), leg("y1", 0.4)]), NOW)
    assert t.ineligible(arb(), NOW) is None
    t.execute(t.plan(arb(), NOW), NOW)  # dry run still counts as an attempt
    assert t.ineligible(arb(), NOW + timedelta(minutes=5)) == "traded recently"
    assert t.ineligible(arb(), NOW + timedelta(minutes=11)) is None


def test_dry_run_records_without_spending(ledger):
    [entry] = trader(ledger).process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["mode"] == "dry_run" and entry["outcome"] == "dry_run" and entry["spent"] == 0.0
    assert ledger.entries()[-1]["key"] == arb().key
    assert ledger.halted() is None


def test_live_full_fill(ledger):
    ex = FakeExchange(["matched", "matched"])
    [entry] = trader(ledger, exchange=ex).process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "filled"
    assert entry["spent"] == pytest.approx(entry["cost"])
    assert len(ex.submitted) == 1 and len(ex.submitted[0]) == 2  # both legs in one batch
    assert ledger.spent_today(NOW) == pytest.approx(entry["cost"])


def test_partial_fill_is_sold_back_and_trading_continues(ledger):
    ex = FakeExchange(["matched", "rejected"])
    [entry] = trader(ledger, exchange=ex).process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "partial_unwound" and ledger.halted() is None
    [(token, shares, floor)] = ex.sold
    assert token == "y0" and shares == pytest.approx(26.75)
    assert floor == pytest.approx(0.45)  # paid ~50c: never sell more than 10% below that
    assert entry["unwinds"][0]["complete"]


def test_failed_unwind_halts(ledger):
    [entry] = trader(ledger, exchange=FakeExchange(["matched", "rejected"], sells="rejected")).process(
        {"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "partial" and "didn't fully work" in ledger.halted()


def test_live_partial_fill_halts_trading_when_unwinding_is_off(ledger):
    ex = FakeExchange(["matched", "rejected"])
    t = trader(ledger, exchange=ex, unwind_partial_fills=False)
    [entry] = t.process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "partial"
    assert entry["spent"] == pytest.approx(13.38)
    assert "partial fill" in ledger.halted()
    assert t.process({"neg_risk_arb": [arb()]}, NOW + timedelta(hours=1)) == []
    assert ledger.resume() and ledger.halted() is None


def test_live_nothing_filled_is_harmless(ledger):
    [entry] = trader(ledger, exchange=FakeExchange(["rejected", "rejected"])).process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "not_filled" and entry["spent"] == 0.0
    assert ledger.halted() is None


def test_delayed_leg_counts_as_partial_and_is_not_unwound(ledger):
    ex = FakeExchange(["matched", "delayed"])
    [entry] = trader(ledger, exchange=ex).process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "partial" and ledger.halted() and ex.sold == []


def test_missing_order_results_are_treated_as_unknown(ledger):
    class Short(FakeExchange):
        def submit(self, orders):
            return [OrderResult(True, "matched")]  # one response for two legs

    [entry] = trader(ledger, exchange=Short([])).process({"neg_risk_arb": [arb()]}, NOW)
    assert entry["outcome"] == "partial" and ledger.halted()


def test_skip_reasons_are_reported(ledger):
    t = trader(ledger, max_usd_per_trade=2.5)  # $2.50 split over two legs leaves one under $1
    assert t.process({"neg_risk_arb": [arb()]}, NOW) == []
    [(opp, reason)] = t.skipped
    assert "minimum" in reason
