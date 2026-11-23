import json

import pytest

from pmscan.backtest import (
    SampledMarket,
    _anchor,
    _settled_yes,
    compress,
    entry,
    per_dollar,
    summarize,
    wilson,
)
from pmscan.web import Api, _State

H = 3600.0
BAND = (0.90, 0.995)


def test_compress_keeps_only_changes():
    hist = [{"timestamp": t, "longPrice": lp, "shortPrice": sp}
            for t, lp, sp in [(0, 0.5, 0.52), (1, 0.5, 0.52), (2, 0.6, 0.42), (3, 0.6, 0.42), (4, None, 0.1)]]
    assert compress(hist) == [[0, 0.5, 0.52], [2, 0.6, 0.42]]


def test_entry_buys_the_side_in_the_band_before_the_end():
    # YES ask 0.95 (NO ask 0.06) from t=0; the game starts at t=10h; the book changes at t=20h.
    points = [[0, 0.50, 0.52], [2 * H, 0.95, 0.06], [20 * H, 0.0, 1.0]]
    assert entry(points, anchor=10 * H, horizon_s=H, band=BAND) == (0, 0.95)
    assert entry(points, anchor=10 * H, horizon_s=9 * H, band=BAND) is None  # 50c then: not in the band


def test_entry_never_uses_the_frozen_book_after_settlement():
    # The last change is at t=5h (settlement); afterwards the stale quote 0.92 would be a "sure win".
    points = [[0, 0.40, 0.62], [5 * H, 0.92, 0.01]]
    assert entry(points, anchor=100 * H, horizon_s=H, band=BAND) is None


def test_entry_skips_markets_not_listed_yet_and_broken_books():
    points = [[10 * H, 0.95, 0.06], [12 * H, 0.96, 0.05]]
    assert entry(points, anchor=11 * H, horizon_s=5 * H, band=BAND) is None
    crossed = [[0, 0.95, 0.02], [5 * H, 0.5, 0.5]]  # 0.95 + 0.02 < 1: a crossed / empty book
    assert entry(crossed, anchor=10 * H, horizon_s=6 * H, band=BAND) is None


def test_no_side_is_bought_at_the_short_price():
    points = [[0, 0.07, 0.94], [10 * H, 0.0, 1.0]]
    assert entry(points, anchor=5 * H, horizon_s=H, band=BAND) == (1, 0.94)


def test_per_dollar_includes_the_fee():
    assert per_dollar(0.95, True, 0.0) == pytest.approx(1 / 0.95 - 1)
    unit = 0.95 + 0.095 * 0.95 * 0.05
    assert per_dollar(0.95, True, 0.095) == pytest.approx(1 / unit - 1)
    assert per_dollar(0.95, False, 0.095) == -1.0


def test_wilson_interval():
    lo, hi = wilson(95, 100)
    assert 0.88 < lo < 0.95 < hi < 0.98
    assert wilson(0, 0) == (0.0, 1.0)


def test_settlement_and_anchor_parsing():
    resolved = {"status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["0","1"]'}
    assert _settled_yes(resolved) == 0.0
    assert _settled_yes({"status": "MARKET_STATUS_OPEN", "outcomePrices": '["1","0"]'}) is None
    game = {"marketType": "moneyline", "gameStartTime": "2026-01-01T00:00:00Z", "endDate": "2026-01-15T00:00:00Z"}
    assert _anchor(game) == pytest.approx(1767225600)
    future = {"marketType": "futures", "gameStartTime": "2026-01-01T00:00:00Z", "endDate": "2026-01-15T00:00:00Z"}
    assert _anchor(future) == pytest.approx(1767225600 + 14 * 86400)


def bet(group, price, won, horizon="1d"):
    return {"group": group, "horizon": horizon, "side": 0, "price": price, "won": won,
            "pnl": per_dollar(price, won, 0.095), "pnl_no_fees": per_dollar(price, won, 0.0),
            "fee_unit": 0.095 * price * (1 - price), "slug": "s", "title": "t"}


def test_summary_verdicts():
    markets = [SampledMarket("s", "crypto", "crypto", "", 1.0, 0.0, 0.095)]
    always = [bet("crypto", 0.92, True) for _ in range(400)]
    often_wrong = [bet("politics", 0.95, i % 5 != 0) for i in range(400)]  # wins 80% at 95c
    r = summarize(always + often_wrong, markets, BAND)
    groups = {g["key"]: g["horizons"]["1d"] for g in r["groups"]}
    assert groups["crypto"]["verdict"] == "profitable"
    assert groups["politics"]["verdict"] == "loses money"
    assert groups["all"]["n"] == 800
    assert groups["moneyline"]["n"] == 0
    assert r["worst_losses"][0]["price"] == 0.95


def test_dashboard_routes(tmp_path):
    class Client:
        venue = "us"

    api = Api(_State(60, None), tmp_path, Client())
    status, snap = api.handle("/api/backtest", {})
    assert status == 200 and snap["result"] is None and not snap["running"]
    (tmp_path / "backtest").mkdir()
    (tmp_path / "backtest" / "result.json").write_text(json.dumps({"bets": 3}))
    assert api.handle("/api/backtest", {})[1]["result"] == {"bets": 3}
    started = []
    api.backtest.start = lambda n: started.append(n) or True
    status, _ = api.post("/api/backtest/run", {"per_group": 999999})
    assert status == 202 and started == [5000]
    assert api.post("/api/nope", {})[0] == 404


def test_backtest_folder_is_under_us_for_an_international_server(tmp_path):
    class Client:
        venue = "international"

    api = Api(_State(60, None), tmp_path, Client())
    assert api.backtest.data_dir == tmp_path / "us"


def test_backtest_defaults_to_us_without_changing_other_commands(monkeypatch):
    from pmscan import cli

    monkeypatch.delenv("PMSCAN_VENUE", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "cmd_backtest", lambda a: seen.setdefault("backtest", a.venue) and 0)
    monkeypatch.setattr(cli, "cmd_paper", lambda a: seen.setdefault("paper", a.venue) and 0)
    cli.main(["backtest"])
    cli.main(["paper", "status"])
    assert seen == {"backtest": "us", "paper": "international"}
