from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, book, make_event, make_market

from pmscan.strategies import LadderArbitrage
from pmscan.strategies.ladder_arb import find_ladders, parse_number, tokenize


def q(m, question, **kw):
    return replace(m, question=question, **kw)


def market(n, question, bid, ask, **kw):
    return q(make_market(n, bid=bid, ask=ask, group=None), question, **kw)


def test_tokenize_and_parse_numbers():
    sk, slots = tokenize("Will Bitcoin be above $100,000 on November 30, 2026?")
    assert sk == "will bitcoin be above <n> on <d>?"
    assert slots == [("n", "$100,000"), ("d", "november 30, 2026")]
    assert parse_number("$1.5b") == 1.5e9
    assert parse_number("250k") == 250_000
    assert parse_number("3.5%") == 3.5
    assert parse_number("abc") is None


def test_threshold_ladder_up_orders_strict_first():
    ev = make_event([
        market(1, "Will BTC be above $100k on Dec 31?", 0.40, 0.42),
        market(2, "Will BTC be above $120k on Dec 31?", 0.10, 0.12),
        market(3, "Will BTC be above $110k on Dec 31?", 0.25, 0.27),
    ])
    [ladder] = find_ladders(ev)
    assert ladder.kind == "threshold"
    assert [m.id for m in ladder.markets] == ["2", "3", "1"]  # 120k implies 110k implies 100k


def test_threshold_ladder_down_and_plus_suffix():
    down = make_event([
        market(1, "Will Bitcoin dip to $80,000 in November?", 0.20, 0.22),
        market(2, "Will Bitcoin dip to $70,000 in November?", 0.05, 0.06),
    ])
    assert [m.id for m in find_ladders(down)[0].markets] == ["2", "1"]  # dipping to 70k implies 80k

    plus = make_event([
        market(1, "Will Jalen Hurts have 3,499.5+ passing yards?", 0.30, 0.32),
        market(2, "Will Jalen Hurts have 3,999.5+ passing yards?", 0.08, 0.09),
    ])
    assert [m.id for m in find_ladders(plus)[0].markets] == ["2", "1"]


def test_over_under_totals_ladder():
    ms = [q(make_market(i, bid=b, ask=b + 0.02, group=None), f"A vs. B: O/U {line}", outcomes=("Over", "Under"))
          for i, (line, b) in enumerate([(2.5, 0.50), (3.5, 0.30), (1.5, 0.75)])]
    assert [m.id for m in find_ladders(make_event(ms))[0].markets] == ["1", "0", "2"]


def test_deadline_ladder_uses_end_dates():
    ev = make_event([
        market(1, "Kraken IPO by December 31, 2026?", 0.50, 0.52, end_date=NOW + timedelta(days=300)),
        market(2, "Kraken IPO by June 30, 2026?", 0.20, 0.22, end_date=NOW + timedelta(days=120)),
    ])
    [ladder] = find_ladders(ev)
    assert ladder.kind == "deadline"
    assert [m.id for m in ladder.markets] == ["2", "1"]


@pytest.mark.parametrize("questions", [
    ["Will X happen in June?", "Will X happen in July?"],  # not cumulative
    ["Will BTC be between $90k and $100k?", "Will BTC be between $100k and $110k?"],
    ["Will Elon tweet 100-119 times?", "Will Elon tweet 120-139 times?"],
    ["Will BTC hit $80k?", "Will BTC hit $120k?"],  # "hit" can mean up or down
    ["Will Team A score 2 goals?", "Will Team A score 3 goals?"],  # no direction word
])
def test_ambiguous_wording_is_skipped(questions):
    ev = make_event([market(i, text, 0.3, 0.32, end_date=NOW + timedelta(days=10 + i)) for i, text in enumerate(questions)])
    assert find_ladders(ev) == []


def test_neg_risk_brackets_and_mixed_arrows_are_skipped():
    ms = [q(make_market(i, bid=0.3, ask=0.32, group="g"), f"Will BTC be above ${p}k?") for i, p in enumerate((100, 110))]
    assert find_ladders(make_event(ms)) == []
    mixed = [market(1, "Will BTC reach $120k?", 0.1, 0.12, group_title="↑ 120,000"),
             market(2, "Will BTC reach $80k?", 0.2, 0.22, group_title="↓ 80,000")]
    assert find_ladders(make_event(mixed)) == []


def test_systematically_inverted_prices_mean_misparse():
    ms = [market(i, f"Will GDP growth be above {x}%?", p, p + 0.01) for i, (x, p) in
          enumerate([(1, 0.10), (2, 0.30), (3, 0.50), (4, 0.70)])]
    assert find_ladders(make_event(ms)) == []


def test_ladder_arbitrage_evaluates_both_legs(ctx):
    strict = market(1, "Will BTC be above $120k on Dec 31?", bid=0.30, ask=0.32)  # priced too high
    loose = market(2, "Will BTC be above $100k on Dec 31?", bid=0.24, ask=0.26)
    books = {"n1": book("n1", [(0.70, 100)]), "y2": book("y2", [(0.26, 60), (0.29, 100)])}
    [opp] = [o for c in LadderArbitrage().screen([make_event([strict, loose])], ctx)
             if (o := LadderArbitrage().evaluate(c, books, ctx))]
    # 60 pairs at 0.96, 40 at 0.99 (still >= 0.3% ROI), then NO book runs out
    assert opp.legs[0].outcome == "No" and opp.legs[1].outcome == "Yes"
    assert opp.payout == pytest.approx(100)
    assert opp.cost == pytest.approx(60 * 0.96 + 40 * 0.99)
    assert opp.payout_per_share == 1.0
