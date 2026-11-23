import json
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, book, make_event, make_market

from pmscan.client import PolymarketClient
from pmscan.config import PaperConfig
from pmscan.models import Leg, Opportunity
from pmscan.paper import PaperTrader, paper_snapshot
from pmscan.scanner import ScanResult


class FakeClient:
    def __init__(self, books=None, resolved=None):
        self.books = books or {}
        self.resolved = resolved or {}
        self.lookups = []

    def get_books(self, tokens):
        return {t: self.books[t] for t in tokens if t in self.books}

    def markets_by_ids(self, ids, closed=None):
        self.lookups.append(list(ids))
        return [self.resolved[i] for i in ids if i in self.resolved]

    resolutions = PolymarketClient.resolutions


def market(n=1, bid=0.04, ask=0.05, group=None):
    return replace(make_market(n, bid=bid, ask=ask, group=group), question=f"Will thing {n} happen?")


def high_prob(m, side=1, price=0.95, limit=0.96):
    leg = Leg.for_market(m, side, shares=1000, avg_price=price, limit_price=limit, label=m.question)
    return Opportunity(strategy="high_prob", kind="buy_no", title=m.question, subtitle="", url=m.url, cost=950,
                       payout=1000, roi=0.05, annualized=1.0, days_to_end=3, risk="", notes=[], legs=[leg],
                       win_probability=price, score=1.0)


def scan(markets, opps):
    by = {}
    for o in opps:
        by.setdefault(o.strategy, []).append(o)
    return ScanResult(started_at=NOW, duration_s=1, stats={}, opportunities=by, events=[make_event(markets)])


CFG = PaperConfig(stake_usd=50, strategies=["high_prob"])


@pytest.fixture
def setup(tmp_path):
    m = market()
    client = FakeClient(books={"n1": book("n1", [(0.95, 30), (0.96, 100)])})
    trader = PaperTrader(CFG, client, tmp_path)
    return trader, trader.books["high_prob"], client, m, tmp_path


def test_enters_from_the_order_book_once(setup):
    trader, hp, client, m, _ = setup
    msgs = trader.process(scan([m], [high_prob(m)]), NOW)
    [p] = hp.open.values()
    assert p.side == 1 and p.outcome == "No" and p.market_id == "1"
    assert p.cost == pytest.approx(50)
    assert p.shares == pytest.approx(30 + (50 - 30 * 0.95) / 0.96)  # walked two levels
    assert hp.cash == pytest.approx(10_000 - 50)
    assert msgs == ["paper 90¢+ positions: bought 1 bet for $50.00"]
    trader.process(scan([m], [high_prob(m)]), NOW)
    assert len(hp.open) == 1  # never doubles up on a held position


def test_respects_price_band_cash_and_book_cap(tmp_path):
    ms = [market(i) for i in range(1, 5)]
    books = {f"n{i}": book(f"n{i}", [(0.95, 1000)]) for i in range(1, 5)}
    trader = PaperTrader(replace(CFG, starting_balance=120), FakeClient(books), tmp_path)
    opps = [high_prob(ms[0], price=0.85), high_prob(ms[1]), high_prob(ms[2]), high_prob(ms[3])]
    trader.process(scan(ms, opps), NOW)
    assert sorted(p.token_id for p in trader.books["high_prob"].open.values()) == ["n2", "n3"]
    t2 = PaperTrader(CFG, FakeClient({"n1": book("n1", [(0.97, 1000)])}), tmp_path / "other")
    t2.process(scan([ms[0]], [high_prob(ms[0])]), NOW)  # 0.97 is above the scanned limit of 0.96
    assert t2.books["high_prob"].open == {}


def test_marks_to_market_and_settles_wins_and_losses(setup):
    trader, hp, client, m, _ = setup
    other = market(2)
    client.books["n2"] = book("n2", [(0.92, 1000)])
    trader.process(scan([m, other], [high_prob(m), high_prob(other, price=0.92, limit=0.93)]), NOW)
    trader.process(scan([replace(m, best_bid=0.02, best_ask=0.03), other], []), NOW)
    assert next(p for p in hp.open.values() if p.token_id == "n1").mark == pytest.approx(0.97)

    client.resolved = {"1": {"id": "1", "closed": True, "outcomePrices": '["0", "1"]'},   # NO wins
                       "2": {"id": "2", "closed": True, "outcomePrices": '["1", "0"]'}}   # NO loses
    shares = next(p for p in hp.open.values() if p.token_id == "n1").shares
    msgs = trader.process(scan([], []), NOW + timedelta(days=3))
    assert hp.open == {}
    won, lost = sorted(hp.closed, key=lambda p: p.market_id)
    assert won.payout == pytest.approx(shares) and lost.payout == 0
    assert hp.cash == pytest.approx(10_000 - 100 + shares)
    s = hp.summary()
    assert s["won"] == 1 and s["win_rate"] == 0.5
    assert s["implied_win_rate"] == pytest.approx((won.avg_price + lost.avg_price) / 2)
    assert "2 positions resolved" in msgs[0]


def test_unresolved_closed_market_stays_open(setup):
    trader, hp, client, m, _ = setup
    trader.process(scan([m], [high_prob(m)]), NOW)
    client.resolved = {"1": {"id": "1", "closed": True, "outcomePrices": '["0.5", "0.5"]'}}
    trader.process(scan([], []), NOW)
    assert len(hp.open) == 1 and client.lookups == [["1"]]


def test_arbitrage_baskets_are_bought_whole_and_settle_as_one_bet(tmp_path):
    ms = [market(i, bid=None, ask=0.30, group="g") for i in (1, 2, 3)]  # YES asks sum to 0.90
    legs = [Leg.for_market(m, 0, shares=100, avg_price=0.30, limit_price=0.30) for m in ms]
    arb = Opportunity(strategy="neg_risk_arb", kind="buy_all_yes", title="Who wins?", subtitle="", url="u",
                      cost=90, payout=100, roi=0.11, annualized=None, days_to_end=5, risk="low", notes=[],
                      legs=legs, score=10.0)
    client = FakeClient(books={f"y{i}": book(f"y{i}", [(0.30, 1000)]) for i in (1, 2, 3)})
    cfg = PaperConfig(strategies=["neg_risk_arb"], basket_stake_usd=100)
    trader = PaperTrader(cfg, client, tmp_path)
    trader.process(scan(ms, [arb]), NOW)
    nr = trader.books["neg_risk_arb"]
    assert len(nr.open) == 3 and len({p.basket for p in nr.open.values()}) == 1
    shares = next(iter(nr.open.values())).shares
    cost = 10_000 - nr.cash
    assert all(p.shares == shares for p in nr.open.values())
    assert cost == pytest.approx(shares * 0.90, rel=0.02)  # three legs at 30c, cents rounding
    trader.process(scan(ms, [arb]), NOW)
    assert len(nr.open) == 3  # same basket isn't bought twice

    client.resolved = {str(i): {"id": str(i), "closed": True, "outcomePrices": '["1", "0"]' if i == 2 else '["0", "1"]'}
                       for i in (1, 2, 3)}
    trader.process(scan([], []), NOW + timedelta(days=5))
    s = nr.summary()
    assert s["resolved"] == 1 and s["won"] == 1 and s["implied_win_rate"] is None
    assert s["realized_pnl"] == pytest.approx(shares - cost)  # exactly one leg pays $1 a share


def test_old_single_portfolio_is_migrated_and_snapshot_has_every_book(tmp_path):
    old = tmp_path / "paper"
    old.mkdir()
    (old / "portfolio.json").write_text(json.dumps({"cash": 9_000, "open": [], "closed": []}))
    (old / "equity.jsonl").write_text('{"ts": "2026-01-01T00:00:00+00:00", "equity": 9000}\n')
    snap = paper_snapshot(tmp_path, PaperConfig())
    assert snap["books"]["high_prob"]["summary"]["cash"] == 9_000
    assert len(snap["books"]["high_prob"]["equity"]) == 1
    assert set(snap["books"]) == {"high_prob", "near_certain", "neg_risk_arb", "ladder_arb", "sportsbook_value"}
    assert snap["summary"]["equity"] == pytest.approx(9_000 + 4 * 10_000)


def test_state_persists_and_reset_archives(setup):
    trader, hp, client, m, data = setup
    trader.process(scan([m], [high_prob(m)]), NOW)
    again = PaperTrader(CFG, client, data)
    assert len(again.books["high_prob"].open) == 1
    [archive] = again.reset("high_prob")
    assert (archive / "portfolio.json").exists()
    assert PaperTrader(CFG, client, data).books["high_prob"].open == {}


def test_caps_bets_per_event(tmp_path):
    ms = [replace(market(i), event_slug="golf" if i <= 4 else "cfp") for i in range(1, 7)]
    books = {f"n{i}": book(f"n{i}", [(0.95, 1000)]) for i in range(1, 7)}
    trader = PaperTrader(replace(CFG, max_bets_per_event=2), FakeClient(books), tmp_path)
    hp = trader.books["high_prob"]
    trader.process(scan(ms, [high_prob(m) for m in ms]), NOW)
    assert sorted(p.token_id for p in hp.open.values()) == ["n1", "n2", "n5", "n6"]
    assert {p.event for p in hp.open.values()} == {"golf", "cfp"}
    # Held bets count toward the cap on later scans, including ones saved before the event was stored.
    next(p for p in hp.open.values() if p.token_id == "n1").event = ""
    trader.process(scan(ms, [high_prob(m) for m in ms]), NOW)
    assert len(hp.open) == 4
    t2 = PaperTrader(replace(CFG, max_bets_per_event=0), FakeClient(books), tmp_path / "nocap")
    t2.process(scan(ms, [high_prob(m) for m in ms]), NOW)
    assert len(t2.books["high_prob"].open) == 6
