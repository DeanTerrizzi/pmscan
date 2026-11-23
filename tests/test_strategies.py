import pytest
from conftest import book, make_event, make_market

from pmscan.strategies import HighProbability, NegRiskArbitrage
from pmscan.strategies.neg_risk_arb import BUY_NO, BUY_YES


def run(strategy, events, books, ctx):
    return [o for c in strategy.screen(events, ctx) if (o := strategy.evaluate(c, books, ctx))]


def test_buy_all_yes_arbitrage_is_sized_from_the_books(ctx):
    markets = [make_market(i, bid=p - 0.01, ask=p) for i, p in enumerate([0.50, 0.30, 0.15])]  # sums to 0.95
    books = {
        "y0": book("y0", [(0.50, 100), (0.60, 100)]),
        "y1": book("y1", [(0.30, 40), (0.31, 100)]),
        "y2": book("y2", [(0.15, 100)]),
    }
    [opp] = [o for o in run(NegRiskArbitrage(), [make_event(markets)], books, ctx) if o.kind == BUY_YES]
    # 40 baskets at 0.95, then 60 at 0.96; the next level (0.60) costs 1.06
    assert opp.legs[0].shares == pytest.approx(100)
    assert opp.cost == pytest.approx(40 * 0.95 + 60 * 0.96)
    assert opp.profit == pytest.approx(100 - opp.cost)
    assert opp.risk == "low"
    assert [l.limit_price for l in opp.legs] == [0.50, 0.31, 0.15]


def test_buy_all_yes_requires_complete_non_augmented_group(ctx):
    markets = [make_market(i, bid=None, ask=0.3) for i in range(3)]  # sums to 0.90
    books = {f"y{i}": book(f"y{i}", [(0.3, 100)]) for i in range(3)}
    assert run(NegRiskArbitrage(), [make_event(markets)], books, ctx)

    incomplete = markets[:2] + [make_market(2, bid=None, ask=0.3, tradeable=False)]
    assert not run(NegRiskArbitrage(), [make_event(incomplete)], books, ctx)
    assert not run(NegRiskArbitrage(), [make_event(markets, augmented=True)], books, ctx)


def test_buy_all_no_arbitrage_when_yes_bids_exceed_one(ctx):
    # YES bids sum to 1.08, so NO asks (1 - bid) sum to 1.92 for a guaranteed payout of 2
    markets = [make_market(i, bid=b, ask=b + 0.02) for i, b in enumerate([0.55, 0.33, 0.20])]
    books = {f"n{i}": book(f"n{i}", [(round(1 - m.best_bid, 2), 50)]) for i, m in enumerate(markets)}
    [opp] = [o for o in run(NegRiskArbitrage(), [make_event(markets)], books, ctx) if o.kind == BUY_NO]
    assert opp.payout == pytest.approx(50 * 2)
    assert opp.cost == pytest.approx(50 * 1.92)
    assert all(l.outcome == "No" for l in opp.legs)


def test_no_arbitrage_when_books_are_fair(ctx):
    markets = [make_market(i, bid=p - 0.01, ask=p + 0.01) for i, p in enumerate([0.5, 0.3, 0.2])]
    books = {}
    for i, m in enumerate(markets):
        books[f"y{i}"] = book(f"y{i}", [(m.best_ask, 100)])
        books[f"n{i}"] = book(f"n{i}", [(round(1 - m.best_bid, 2), 100)])
    assert run(NegRiskArbitrage(), [make_event(markets)], books, ctx) == []


def test_high_prob_picks_the_favored_side_and_annualizes(ctx):
    m = make_market(1, bid=0.03, ask=0.05, group=None, days=9.0)  # NO costs 0.97
    books = {"n1": book("n1", [(0.97, 200), (0.999, 1000)], [(0.95, 100)])}
    [opp] = run(HighProbability(), [make_event([m])], books, ctx)
    assert opp.legs[0].outcome == "No"
    assert opp.cost == pytest.approx(200 * 0.97)  # 0.999 level is above max_price
    assert opp.roi == pytest.approx(0.03 / 0.97)
    # locked for 9 days to end + 1 day settlement lag
    assert opp.annualized == pytest.approx(opp.roi * 365 / 10)
    assert opp.win_probability == pytest.approx(0.97)


def test_high_prob_filters(ctx):
    books = {"y1": book("y1", [(0.95, 500)], [(0.94, 500)])}
    too_far = make_market(1, bid=0.94, ask=0.95, group=None, days=60)
    thin = make_market(1, bid=0.94, ask=0.95, group=None, liquidity=10)
    assert run(HighProbability(), [make_event([too_far])], books, ctx) == []
    assert run(HighProbability(), [make_event([thin])], books, ctx) == []

    ok = make_market(1, bid=0.94, ask=0.95, group=None)
    live = make_event([ok]); live.live = True
    assert run(HighProbability(), [live], books, ctx) == []
    assert len(run(HighProbability(), [make_event([ok])], books, ctx)) == 1


def test_high_prob_skips_wide_books(ctx):
    wide_quote = make_market(1, bid=0.80, ask=0.95, group=None)  # the listing's quotes are 15c apart
    assert run(HighProbability(), [make_event([wide_quote])], {"y1": book("y1", [(0.95, 500)], [(0.94, 500)])},
               ctx) == []
    assert ctx.stats["high_prob_spread_skipped"] == 1
    ok = make_market(1, bid=0.94, ask=0.95, group=None)
    for bids in ([(0.90, 500)], []):  # the fetched book is wide, or has no bids at all
        assert run(HighProbability(), [make_event([ok])], {"y1": book("y1", [(0.95, 500)], bids)}, ctx) == []
    # Dust on top of an empty book: 1c apart for 4 contracts, 98c apart for a real order.
    dust = book("y1", [(0.95, 4), (0.99, 9000)], [(0.94, 5), (0.01, 2500)])
    assert run(HighProbability(), [make_event([ok])], {"y1": dust}, ctx) == []
    ctx.config.high_prob.max_spread = 0  # off
    assert len(run(HighProbability(), [make_event([wide_quote])],
                   {"y1": book("y1", [(0.95, 500)], [(0.80, 500)])}, ctx)) == 1
