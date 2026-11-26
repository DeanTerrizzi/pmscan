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
    cfg = PaperConfig(strategies=["neg_risk_arb"], basket_stake_usd=100, probe_delays_s=[])
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


def test_caps_money_per_league(tmp_path):
    def sports(i, slug):
        return replace(market(i), category="sports", event_slug=slug)
    ms = [sports(i, f"cfb-event{i}-2026") for i in range(1, 6)] + [sports(6, "nba-cup-2026"),
                                                                    replace(market(7), category="politics")]
    books = {f"n{i}": book(f"n{i}", [(0.95, 1000)]) for i in range(1, 8)}
    # $50 a bet, 15% of $1,000 = $150: three bets per league.
    trader = PaperTrader(replace(CFG, starting_balance=1000, max_league_share=0.15), FakeClient(books), tmp_path)
    hp = trader.books["high_prob"]
    trader.process(scan(ms, [high_prob(m) for m in ms]), NOW)
    leagues = sorted(p.league for p in hp.open.values())
    assert leagues == ["politics", "sports:cfb", "sports:cfb", "sports:cfb", "sports:nba"]
    # Held positions count on later scans, including ones saved before the league was stored.
    for p in hp.open.values():
        p.league = ""
    trader.process(scan(ms, [high_prob(m) for m in ms]), NOW)
    assert len(hp.open) == 5
    # polymarket.com markets have no category: only the per-event cap applies.
    t2 = PaperTrader(replace(CFG, starting_balance=1000, max_league_share=0.15), FakeClient(books), tmp_path / "intl")
    plain = [replace(market(i), event_slug=f"e{i}") for i in range(1, 6)]
    t2.process(scan(plain, [high_prob(m) for m in plain]), NOW)
    assert len(t2.books["high_prob"].open) == 5


def test_probes_record_whether_a_slower_order_would_still_fill(tmp_path):
    ms = [market(i, bid=None, ask=0.30, group="g") for i in (1, 2, 3)]
    legs = [Leg.for_market(m, 0, shares=100, avg_price=0.30, limit_price=0.30) for m in ms]
    arb = Opportunity(strategy="neg_risk_arb", kind="buy_all_yes", title="Who wins?", subtitle="", url="u",
                      cost=90, payout=100, roi=0.11, annualized=None, days_to_end=1, risk="low", notes=[],
                      legs=legs, score=10.0)
    client = FakeClient(books={f"y{i}": book(f"y{i}", [(0.30, 1000)]) for i in (1, 2, 3)})
    nr = PaperTrader(PaperConfig(strategies=["neg_risk_arb"], basket_stake_usd=100, probe_delays_s=[]),
                     client, tmp_path).books["neg_risk_arb"]
    nr.config = replace(nr.config, probe_delays_s=[1.0, 3.0, 10.0])
    plan = nr._planner(100)._plan(arb, NOW)
    # The edge shrinks between checks: half the depth after 1s, gone after 3s, the network fails at 10s.
    states = iter([{"y1": book("y1", [(0.30, 1000)]), "y2": book("y2", [(0.30, 1000)]),
                    "y3": book("y3", [(0.30, 1000), (0.40, 1000)])}, None, "boom"])

    def step(_seconds):
        state = next(states)
        if state is None:
            client.books["y1"] = book("y1", [(0.45, 1000)])
        elif state == "boom":
            client.get_books = lambda tokens: (_ for _ in ()).throw(ConnectionError("offline"))
        else:
            client.books.update(state)
            client.books["y2"] = book("y2", [(0.30, plan.units / 2 + 5)])

    nr._run_probes(arb, plan, "b1", 100, sleep=step)
    [row] = [json.loads(line) for line in nr.probes_path.read_text().splitlines()]
    first, second, third = row["checks"]
    assert first["ok"] is True and 0.4 < first["share"] < 0.6 and first["profit"] > 0
    assert second == {"delay_s": 3.0, "ok": False, "share": 0.0, "profit": 0.0,
                      "reason": second["reason"]} and "asks" in second["reason"]
    assert third["ok"] is None  # unknown, not counted as a miss
    s = nr.probe_summary()
    assert s["baskets"] == 1 and [d["delay_s"] for d in s["delays"]] == [1.0, 3.0]
    assert s["delays"][0]["still_profitable"] == 1 and s["delays"][1]["still_profitable"] == 0
