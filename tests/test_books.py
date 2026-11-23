import pytest

from pmscan.books import walk_basket
from pmscan.models import NO_FEES, FeeSchedule, Level


def test_fee_curve_matches_documented_formula():
    # docs: 100 shares at $0.50 with rate 0.07 -> $1.75
    assert 100 * FeeSchedule(rate=0.07).per_share(0.5) == pytest.approx(1.75)
    assert FeeSchedule(rate=0.04).per_share(0.9) == pytest.approx(0.04 * 0.9 * 0.1)
    assert NO_FEES.per_share(0.5) == 0.0
    assert FeeSchedule(rate=0.04).per_share(1.0) == 0.0


def test_walk_stops_when_basket_cost_exceeds_limit():
    a = [Level(0.30, 10), Level(0.40, 100)]
    b = [Level(0.60, 50)]
    fill = walk_basket([a, b], [NO_FEES, NO_FEES], max_unit_cost=0.95)
    # 10 baskets at 0.90; the next level would cost 1.00 > 0.95
    assert fill.units == pytest.approx(10)
    assert fill.cost == pytest.approx(9.0)
    assert fill.limit_prices == [0.30, 0.60]


def test_walk_consumes_multiple_levels_and_tracks_vwap():
    a = [Level(0.30, 10), Level(0.32, 10)]
    b = [Level(0.60, 15), Level(0.61, 100)]
    fill = walk_basket([a, b], [NO_FEES, NO_FEES], max_unit_cost=0.95)
    assert fill.units == pytest.approx(20)
    assert fill.cost == pytest.approx(10 * 0.90 + 5 * 0.92 + 5 * 0.93)
    assert fill.avg_price(0) == pytest.approx(0.31)
    assert fill.limit_prices == [0.32, 0.61]


def test_walk_respects_budget_and_max_price():
    asks = [Level(0.90, 100), Level(0.97, 100)]
    fill = walk_basket([asks], [NO_FEES], max_unit_cost=1.0, budget=45.0)
    assert fill.cost == pytest.approx(45.0)
    assert fill.units == pytest.approx(50)

    fill = walk_basket([asks], [NO_FEES], max_unit_cost=1.0, max_price=0.95)
    assert fill.units == pytest.approx(100)


def test_walk_includes_fees_in_unit_cost():
    fees = FeeSchedule(rate=0.04)
    fill = walk_basket([[Level(0.5, 10)]], [fees], max_unit_cost=1.0)
    assert fill.fees == pytest.approx(10 * 0.01)
    assert fill.cost == pytest.approx(10 * 0.51)
    assert walk_basket([[Level(0.5, 10)]], [fees], max_unit_cost=0.505).units == 0


def test_walk_handles_empty_ladder():
    assert walk_basket([[Level(0.5, 10)], []], [NO_FEES, NO_FEES], max_unit_cost=1.0).units == 0
