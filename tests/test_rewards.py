from datetime import datetime, timezone

import pytest

from pmscan.config import RewardsConfig
from pmscan.rewards import (
    Period,
    Program,
    RewardsPaper,
    best_quote,
    candidates,
    estimate,
    parse_program,
    score_side,
    spread_ok,
    window,
    yes_label,
)
from pmscan.web import Api, _State

H = 3600.0
RAW = {
    "marketSlug": "aec-nba-bos-nyk-2026-04-01",
    "category": "SPR", "subcategory": "BASKETBALL", "instrumentProduct": "aec",
    "eventStartTime": "2026-04-01 23:30:00+00", "instrumentState": "INSTRUMENT_STATE_OPEN",
    "timePeriods": [
        {"programId": "nba_t1_ml_day_of_20260328", "programType": "liquidityProgram", "start": "2026-03-28T04:00:00Z",
         "rewardPool": 300, "status": "active", "discountFactor": 0.4, "targetSize": 2000, "period": "day_of"},
        {"programId": "nba_t1_ml_live_20260328", "programType": "liquidityProgram", "start": "2026-03-28T04:00:00Z",
         "rewardPool": 900, "status": "active", "discountFactor": 0.3, "targetSize": 3000, "maxSpread": 0.035,
         "period": "live"},
        {"programId": "nba_volume", "programType": "volumeProgram", "rewardPool": 50, "status": "active"},
    ],
}


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def period(**kw) -> Period:
    base = dict(program_id="p_20260101", period="daily_event", pool=240.0, discount=0.5, target=100.0,
                max_spread=None, start=None, end=None, status="active")
    return Period(**{**base, **kw})


def test_parse_program_keeps_liquidity_periods():
    prog = parse_program(RAW)
    assert prog.slug == "aec-nba-bos-nyk-2026-04-01" and prog.open
    assert prog.event_start == ts("2026-04-01T23:30:00")
    assert [p.period for p in prog.periods] == ["day_of", "live"]
    live = prog.periods[1]
    assert live.max_spread == 0.035 and live.target == 3000 and live.discount == 0.3
    assert prog.periods[0].max_spread is None
    assert live.program == "nba_t1_ml_live"


def test_windows():
    prog = parse_program(RAW)
    es = prog.event_start
    day_of, live = prog.periods
    assert window(day_of, prog, es - 10 * H) == (es - 6 * H, es)
    assert window(live, prog, es) == (es, es + 2.5 * H)  # basketball
    daily = period()
    start, end = window(daily, prog, ts("2026-04-01T12:00:00"))
    assert end - start == 24 * H
    assert datetime.fromtimestamp(start, timezone.utc).hour == 4  # midnight Eastern (EDT)


def test_score_side_counts_only_up_to_target_size():
    bids = [(0.50, 60), (0.49, 60), (0.48, 1000)]
    s = score_side(bids, True, 0.01, 0.5, target=100)
    assert s.qualifies and s.landing == 0.49
    assert s.total == pytest.approx(60 + 0.5 * 60)  # the 48¢ level is past the target
    thin = score_side(bids[:1], True, 0.01, 0.5, target=100)
    assert not thin.qualifies and thin.share == 0


def test_a_big_order_at_the_best_price_pushes_others_out():
    bids = [(0.50, 40), (0.49, 500)]
    s = score_side(bids, True, 0.01, 0.5, target=100, ours=(0.50, 200))
    assert s.ours == 200 and s.total == 240  # 49¢ no longer within the target
    assert s.share == pytest.approx(200 / 240)


def test_asks_are_scored_from_the_lowest_price():
    asks = [(0.53, 50), (0.52, 50)]
    s = score_side(asks, False, 0.01, 0.5, target=100)
    assert s.landing == 0.53 and s.total == pytest.approx(50 + 25)


def test_max_spread_needs_both_sides_close_to_the_mid():
    bid = score_side([(0.49, 100)], True, 0.01, 0.5, 100)
    ask_ok = score_side([(0.55, 100)], False, 0.01, 0.5, 100)
    ask_wide = score_side([(0.57, 100)], False, 0.01, 0.5, 100)
    assert spread_ok(bid, ask_ok, 0.03)
    assert not spread_ok(bid, ask_wide, 0.03)
    assert spread_ok(bid, ask_wide, None)


def test_best_quote_prefers_the_cheaper_side_for_the_same_budget():
    # A 10¢ market: $10 buys 100 contracts on the bid but only ~11 on the ask (it costs 90¢ to sell YES).
    bids, asks = [(0.10, 100)], [(0.11, 100)]
    q, reason = best_quote(bids, asks, period(target=50), 0.01, 0.01, 10.0, rate_per_hour=10.0)
    assert reason == "" and q.side == "bid" and q.ticks_behind == 0
    assert q.size == pytest.approx(100) and q.capital == pytest.approx(10)
    assert q.share == pytest.approx(0.5)
    assert q.per_hour == pytest.approx(10 * 0.5 * 0.5)


def test_best_quote_explains_when_nothing_pays():
    q, reason = best_quote([(0.40, 10)], [(0.60, 10)], period(target=1_000_000), 0.01, 0.01, 5.0, 1.0)
    assert q is None and "Target Size" in reason


def test_pool_is_split_across_the_programs_markets():
    prog = Program("m1", "CRY", "BTC", "cpc", None, True, [period(pool=240.0)])
    book = {"marketData": {"bids": [{"px": {"value": "0.40"}, "qty": "100"}],
                           "offers": [{"px": {"value": "0.42"}, "qty": "100"}]}}
    now = ts("2026-04-01T12:00:00")
    e = estimate(prog, prog.periods[0], book, {"question": "Q?", "orderPriceMinTickSize": 0.01}, now, 40.0,
                 program_markets=4)
    assert e.market_pool == 60 and e.rate_per_hour == pytest.approx(60 / 24)
    assert e.paying_now and e.quote is not None
    assert e.per_hour == pytest.approx(e.rate_per_hour * 0.5 * e.quote.share)


def test_candidates_rank_by_each_markets_slice():
    now = ts("2026-04-01T12:00:00")
    big_program = [Program(f"b{i}", "SPR", "X", "", None, True, [period(program_id="big", pool=1000.0)])
                   for i in range(100)]
    solo = Program("solo", "FIN", "", "", None, True, [period(program_id="solo", pool=50.0)])
    picked, sizes = candidates([*big_program, solo], now, 24, per_program=5, limit=3)
    assert sizes["big"] == 100 and picked[0][0].slug == "solo"  # $50 alone beats $1,000 / 100
    assert len(picked) == 3


def test_yes_label():
    assert yes_label({"question": "How high will BTC get?", "title": "Reaches $87,500"}) == "Reaches $87,500"
    assert yes_label({"question": "Who wins?", "outcomes": '["Celtics","Knicks"]'}) == "Celtics"
    assert yes_label({"question": "Will it?", "title": "Will it?", "outcomes": '["Yes","No"]'}) == ""


def scan_row(**kw):
    row = {"slug": "m1", "title": "Gold up?", "url": "", "program": "gold", "period": "daily_event", "paying_now": True,
           "window_end": 10_000.0, "tick": 0.01, "step": 0.01, "discount": 0.5, "target": 100.0, "max_spread": None,
           "rate_per_hour": 3600.0, "per_hour": 900.0,
           "quote": {"side": "bid", "price": 0.40, "size": 100.0, "capital": 40.0, "ticks_behind": 0}}
    return {**row, **kw}


def raw_book(bid=0.40, ask=0.42, qty=100, trade=None, trade_at=None):
    stats = {}
    if trade is not None:
        stats = {"lastTradePx": {"value": str(trade)},
                 "lastTradeSetTime": datetime.fromtimestamp(trade_at, timezone.utc).isoformat()}
    return {"marketData": {"bids": [{"px": {"value": str(bid)}, "qty": str(qty)}],
                           "offers": [{"px": {"value": str(ask)}, "qty": str(qty)}], "stats": stats}}


def test_paper_accrues_rewards_then_fills_and_settles(tmp_path):
    paper = RewardsPaper(tmp_path, RewardsConfig(max_quotes=2))
    opened = paper.open_from({"estimates": [scan_row(), scan_row(slug="m2", program="gold")]}, now=1000.0)
    assert len(opened) == 1  # one quote per program
    q = opened[0]
    paper.tick({"m1": raw_book()}, now=1060.0)
    # 60s at $3,600/h, half to the bid side, share 100/(100+100) at 40¢
    assert q.accrued == pytest.approx(3600 * 0.5 * 0.5 * 60 / 3600)
    assert q.status == "quoting"
    paper.tick({"m1": raw_book(trade=0.39, trade_at=1100.0)}, now=1120.0)
    assert q.status == "filled" and q.fill_price == 0.40 and q.cost == pytest.approx(40.0)
    paper.tick({}, now=1200.0, resolutions={"m1": 1.0})
    assert q.status == "settled" and q.value == pytest.approx(100.0)
    assert q.pnl == pytest.approx(60.0 + q.accrued)
    s = paper.summary()
    assert s["fills"] == 1 and s["fill_pnl"] == pytest.approx(60.0)
    assert s["payable"] == pytest.approx(q.accrued)  # cleared the $1 minimum on that day
    paper.save()
    assert RewardsPaper(tmp_path, RewardsConfig()).summary()["rewards"] == pytest.approx(s["rewards"])


def test_paper_skips_live_games_unless_asked(tmp_path):
    live = scan_row(period="live")
    assert RewardsPaper(tmp_path, RewardsConfig()).open_from({"estimates": [live]}, now=1000.0) == []
    assert len(RewardsPaper(tmp_path, RewardsConfig(paper_live=True)).open_from({"estimates": [live]}, now=1000.0)) == 1


def test_paper_skips_fast_moving_programs(tmp_path):
    paper = RewardsPaper(tmp_path, RewardsConfig(max_quotes=5))
    rows = [scan_row(slug="a", program="crypto_alt_updown_1d"), scan_row(slug="b", program="metals_gold_updown_1d"),
            scan_row(slug="c", program="usgas_daily_ladder"), scan_row(slug="d", program="finance_nvda_kimpow")]
    assert [q.slug for q in paper.open_from({"estimates": rows}, now=1000.0)] == ["d"]


def test_paper_follows_the_best_price_and_stops_when_the_period_ends(tmp_path):
    paper = RewardsPaper(tmp_path, RewardsConfig())
    q = paper.open_from({"estimates": [scan_row(window_end=2000.0)]}, now=1000.0)[0]
    paper.tick({"m1": raw_book(bid=0.38, ask=0.41)}, now=1060.0)
    assert q.price == 0.38 and q.size == pytest.approx(40 / 0.38, abs=0.01)
    paper.tick({"m1": raw_book(bid=0.38, ask=0.41)}, now=2100.0)
    assert q.status == "ended"
    assert q.seconds == pytest.approx(60 + 300)  # long gaps aren't credited in full


def test_small_rewards_are_not_payable(tmp_path):
    paper = RewardsPaper(tmp_path, RewardsConfig())
    q = paper.open_from({"estimates": [scan_row(rate_per_hour=10.0)]}, now=1000.0)[0]
    paper.tick({"m1": raw_book()}, now=1060.0)
    assert 0 < q.accrued < 1 and q.payable == 0


def test_rewards_routes_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)

    class Client:
        venue = "us"

    api = Api(_State(60, None), tmp_path, Client())
    status, snap = api.handle("/api/rewards", {})
    assert status == 200 and snap["has_key"] is False and snap["result"] is None
    assert api.post("/api/rewards/scan", {})[0] == 400


def test_rewards_command_defaults_to_us(monkeypatch):
    from pmscan import cli

    monkeypatch.delenv("PMSCAN_VENUE", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "cmd_rewards", lambda a: seen.setdefault("venue", a.venue) and 0)
    cli.main(["rewards", "status"])
    assert seen == {"venue": "us"}
