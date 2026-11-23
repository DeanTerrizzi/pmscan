import base64
import json
from decimal import Decimal

import pytest
from conftest import NOW

from pmscan import cli
from pmscan.config import ScanConfig
from pmscan.strategies import ScanContext
from pmscan.strategies.neg_risk_arb import NegRiskArbitrage
from pmscan.trading import LegOrder
from pmscan.us import (
    UsExchange,
    mutually_exclusive,
    no_token,
    parse_book,
    parse_event,
    split_token,
    validate_keys,
)


def q(v):
    return {"value": str(v), "currency": "USD"}


def market(slug, title, mtype="futures", bid=0.2, ask=0.22, sides=("Yes", "No"), **kw):
    return {
        "id": "1", "slug": slug, "title": title, "question": kw.pop("question", "Event title"), "marketType": mtype,
        "active": True, "closed": False, "status": "MARKET_STATUS_OPEN", "feeCoefficient": 0.095,
        "minimumTradeQty": kw.pop("step", 0.01), "orderPriceMinTickSize": kw.pop("tick", 0.01),
        "bestBidQuote": q(bid), "bestAskQuote": q(ask), "endDate": "2026-12-01T00:00:00Z",
        "gameStartTime": kw.pop("start", "2026-01-02T00:00:00Z"),
        "marketSides": [{"description": sides[0], "long": True}, {"description": sides[1], "long": False}], **kw,
    }


def event(title, markets, **kw):
    return {"id": "9", "slug": "ev-slug", "title": title, "markets": markets, **kw}


def test_tokens_round_trip():
    assert split_token("abc") == ("abc", 0)
    assert split_token(no_token("abc")) == ("abc", 1)


def test_no_book_mirrors_yes_book():
    raw = {"marketData": {"bids": [{"px": q(0.40), "qty": "10"}, {"px": q(0.39), "qty": "5"}],
                          "offers": [{"px": q(0.42), "qty": "7"}]}}
    yes, no = parse_book("s", raw, 0.01)
    assert [(lv.price, lv.size) for lv in yes.asks] == [(0.42, 7.0)]
    assert [(lv.price, lv.size) for lv in no.asks] == [(0.60, 10.0), (0.61, 5.0)]  # 1 - YES bids
    assert [(lv.price, lv.size) for lv in no.bids] == [(0.58, 7.0)]
    assert no.token_id == "s~no"


@pytest.mark.parametrize("title, titles, expected", [
    ("English Premier League Champion", ["Arsenal", "Chelsea", "Liverpool"], True),
    ("NBA MVP", ["A", "B"], True),
    ("Oscar Nominees: Best Casting", ["A", "B", "C"], False),
    ("NBA Teams 50+ Regular Season Wins", ["A", "B"], False),
    ("DK Metcalf Regular Season Receiving Yards", ["600.5+ Yards", "800.5+ Yards"], False),
    ("Newsom Announces a Presidential Run?", ["By March 31, 2027", "By June 30, 2027"], False),
    ("GTA VI Album Winner", ["50k+", "100k+"], False),
    ("Who will host SNL in December?", ["A", "B"], False),
])
def test_mutual_exclusivity_is_read_conservatively(title, titles, expected):
    raw = event(title, [market(f"m{i}", t, bid=0.1) for i, t in enumerate(titles)])
    assert mutually_exclusive(raw) is expected


def test_soccer_three_way_is_exclusive_and_exhaustive():
    raw = event("A vs. B", [market("a", "A (Reg. Time)", "drawable_outcome"),
                            market("d", "Tie (Reg. Time)", "drawable_outcome"),
                            market("b", "B (Reg. Time)", "drawable_outcome")],
                teams=[{"name": "A", "league": "epl"}, {"name": "B", "league": "epl"}],
                startTime="2026-01-02T00:00:00Z")
    e = parse_event(raw)
    assert e.neg_risk and not e.augmented
    assert [m.group_title for m in e.markets] == ["A", "Draw", "B"]
    assert {m.sports_market_type for m in e.markets} == {"moneyline"}
    assert e.sport == "epl" and e.venue == "us"
    assert e.url == "https://polymarket.us/event/ev-slug"


def test_too_many_yes_bids_means_not_exclusive():
    raw = event("NBA MVP", [market("a", "A", bid=0.7), market("b", "B", bid=0.7)])
    assert not mutually_exclusive(raw)


def test_parse_moneyline_uses_team_sides():
    raw = event("POR vs. MIL", [market("aec-nba", "Portland vs Milwaukee", "moneyline", bid=0.68, ask=0.69,
                                       sides=("Trail Blazers", "Bucks"), question="Who will win ...?")],
                teams=[{"name": "Portland Trail Blazers", "league": "nba"}, {"name": "Milwaukee Bucks", "league": "nba"}],
                startTime="2026-01-02T00:00:00Z", live=True)
    e = parse_event(raw)
    [m] = e.markets
    assert m.outcomes == ("Trail Blazers", "Bucks")
    assert m.token_ids == ("aec-nba", "aec-nba~no")
    assert m.question == "Portland vs Milwaukee" and m.group_title == ""
    assert m.fees.per_share(0.5) == pytest.approx(0.095 * 0.25)
    assert e.live and e.sport == "nba" and len(e.teams) == 2
    assert not e.neg_risk


def test_futures_are_not_in_play():
    raw = event("NBA MVP", [market("a", "A", bid=0.1, question="NBA MVP"), market("b", "B", bid=0.1)])
    e = parse_event(raw)
    assert all(m.game_start is None for m in e.markets) and e.start_time is None
    assert e.markets[0].question == "NBA MVP: A"
    assert not e.markets[0].stats_known


def test_buy_every_no_arbitrage_on_us_event():
    # Four exclusive outcomes whose YES bids sum past $1: buying every NO pays $3 for less.
    ms = [market(f"m{i}", n, bid=0.30, ask=0.32) for i, n in enumerate(["A", "B", "C", "D"])]
    e = parse_event(event("Big Ten Champion", ms))
    assert e.neg_risk
    ctx = ScanContext(config=ScanConfig(), now=NOW, venue="us")
    strat = NegRiskArbitrage()
    [cand] = [c for c in strat.screen([e], ctx) if c.payload.kind == "buy_all_no"]
    books = {}
    for m in e.markets:
        yes, no = parse_book(m.slug, {"marketData": {"bids": [{"px": q(0.30), "qty": "500"}],
                                                     "offers": [{"px": q(0.32), "qty": "500"}]}})
        books[yes.token_id], books[no.token_id] = yes, no
    opp = strat.evaluate(cand, books, ctx)
    assert opp is not None and opp.profit > 0
    assert all(leg.token_id.endswith("~no") for leg in opp.legs)
    assert opp.url.startswith("https://polymarket.us/")


def test_validate_keys():
    secret = base64.b64encode(bytes(range(32))).decode()
    validate_keys("6f1c2d3e-key-id", secret)
    with pytest.raises(ValueError, match="Secret Key"):
        validate_keys("6f1c2d3e-key-id", "not-a-secret")
    with pytest.raises(ValueError, match="Key ID"):
        validate_keys("", secret)


class FakeOrders:
    def __init__(self, fill=True):
        self.fill = fill
        self.created = []

    def create(self, params):
        self.created.append(params)
        qty = params["quantity"]
        if not self.fill:
            return {"id": "o1", "executions": [{"type": "EXECUTION_TYPE_CANCELED",
                                                 "order": {"state": "ORDER_STATE_CANCELED", "cumQuantity": 0}}]}
        return {"id": "o1", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": str(qty),
                                             "order": {"state": "ORDER_STATE_FILLED", "cumQuantity": qty}}]}


class FakeSdk:
    def __init__(self, fill=True):
        self.orders = FakeOrders(fill)


def order(token, price, shares=10.0, tick=0.01):
    return LegOrder("m", "Yes", token, Decimal("5"), Decimal(str(price)), shares, tick)


def test_us_exchange_buys_yes_long_and_no_as_short_at_the_yes_price():
    sdk = FakeSdk()
    results = UsExchange(sdk).submit([order("mkt-a", 0.42), order("mkt-b~no", 0.61, tick=0.001)])
    assert [r.filled for r in results] == [True, True]
    a, b = sdk.orders.created
    assert (a["marketSlug"], a["intent"], a["price"]["value"], a["tif"]) == (
        "mkt-a", "ORDER_INTENT_BUY_LONG", "0.42", "TIME_IN_FORCE_FILL_OR_KILL")
    # Buying NO at up to 0.61 = selling YES at no less than 0.39.
    assert (b["marketSlug"], b["intent"], b["price"]["value"]) == ("mkt-b", "ORDER_INTENT_BUY_SHORT", "0.39")
    assert all(o["synchronousExecution"] for o in sdk.orders.created)


def test_us_exchange_reports_unfilled_fok_as_rejected():
    [r] = UsExchange(FakeSdk(fill=False)).submit([order("mkt-a", 0.42)])
    assert not r.ok and r.status == "rejected"


def test_us_exchange_sell_floor():
    sdk = FakeSdk()
    r = UsExchange(sdk).sell("mkt-b~no", 10.0, 0.55)
    assert r.filled and r.shares == 10.0
    [o] = sdk.orders.created
    assert (o["intent"], o["price"]["value"], o["tif"]) == (
        "ORDER_INTENT_SELL_SHORT", "0.45", "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL")


def test_check_account_us_json_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "abc")
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", "nope")
    code = cli.main(["trade", "check-account", "--venue", "us", "--json", "--data-dir", str(tmp_path)])
    assert code == 1
    assert "Key ID" in json.loads(capsys.readouterr().out)["error"]


def test_us_venue_keeps_its_own_data_dir(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "cmd_paper", lambda args: seen.setdefault("dir", args.data_dir) and 0)
    cli.main(["paper", "status", "--venue", "us", "--data-dir", str(tmp_path)])
    assert seen["dir"] == str(tmp_path / "us")


def test_sportsbook_matches_a_us_moneyline():
    from datetime import timedelta

    from pmscan.odds import parse_odds_events
    from pmscan.strategies.sportsbook import SportsbookValue

    kickoff = NOW + timedelta(hours=5)
    side = lambda name, long: {"description": name.split()[-1], "long": long, "team": {"name": name}}  # noqa: E731
    rm = market("aec-nba-por-mil", "Portland vs Milwaukee", "moneyline", bid=0.53, ask=0.55,
                start=kickoff.isoformat())
    rm["marketSides"] = [side("Portland Trail Blazers", True), side("Milwaukee Bucks", False)]
    e = parse_event(event("POR vs. MIL", [rm], startTime=kickoff.isoformat(), endDate=(kickoff + timedelta(hours=4)).isoformat(),
                          teams=[{"name": "Portland Trail Blazers", "league": "nba"},
                                 {"name": "Milwaukee Bucks", "league": "nba"}]))
    assert e.markets[0].outcomes == ("Portland Trail Blazers", "Milwaukee Bucks")
    assert e.markets[0].end_date == kickoff + timedelta(hours=4)  # the game, not the settlement deadline
    raw_odds = {"id": "x", "sport_key": "basketball_nba", "commence_time": kickoff.isoformat(),
                "home_team": "Milwaukee Bucks", "away_team": "Portland Trail Blazers",
                "bookmakers": [{"key": f"b{i}", "title": "b", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Portland Trail Blazers", "price": 1.55}, {"name": "Milwaukee Bucks", "price": 2.6}]}]}
                               for i in range(3)]}

    class Odds:
        requests_remaining = "400"

        def odds(self, key, active_sports=1):
            assert key == "basketball_nba"
            return parse_odds_events([raw_odds])

        def ttl_for(self, n, now=None):
            return 3600.0

    ctx = ScanContext(config=ScanConfig(), now=NOW, venue="us")
    strat = SportsbookValue(Odds())
    cands = strat.screen([e], ctx)
    assert ctx.stats["sportsbook_matched"] == 1
    yes, no = parse_book("aec-nba-por-mil", {"marketData": {"bids": [{"px": q(0.53), "qty": "300"}],
                                                            "offers": [{"px": q(0.55), "qty": "300"}]}})
    opps = [o for c in cands if (o := strat.evaluate(c, {yes.token_id: yes, no.token_id: no}, ctx))]
    [opp] = opps
    assert opp.subtitle == "Portland Trail Blazers to win" and opp.legs[0].token_id == "aec-nba-por-mil"


class SimExchange:
    """Fills everything and tracks the net position, like a well-behaved Polymarket US."""

    def __init__(self, no_fills=True):
        self.net = 0.0
        self.no_fills = no_fills
        self.orders = []

    def submit(self, orders):
        from pmscan.trading import OrderResult

        out = []
        for o in orders:
            side = split_token(o.token_id)[1]
            self.orders.append(("buy", o.token_id, float(o.max_price), o.shares))
            if side == 1 and not self.no_fills:
                out.append(OrderResult(False, "rejected", message="no fill"))
                continue
            self.net += o.shares if side == 0 else -o.shares
            out.append(OrderResult(True, "matched", shares=o.shares))
        return out

    def sell(self, token_id, shares, min_price):
        from pmscan.trading import OrderResult

        side = split_token(token_id)[1]
        self.orders.append(("sell", token_id, min_price, shares))
        self.net += -shares if side == 0 else shares
        return OrderResult(True, "matched", shares=shares)

    def position(self, slug):
        return round(self.net, 2)


class BookClient:
    def get_books(self, tokens):
        out = {}
        for t in tokens:
            yes, no = parse_book(split_token(t)[0], {"marketData": {"bids": [{"px": q(0.49), "qty": "500"}],
                                                                    "offers": [{"px": q(0.50), "qty": "500"}]}})
            out[yes.token_id], out[no.token_id] = yes, no
        return out


def test_round_trip_test_trade():
    from pmscan.us import run_test_trade

    e = parse_event(event("Big game", [market("busy", "Busy market", bid=0.49, ask=0.50, start=None)]))
    sim = SimExchange()
    result = run_test_trade(sim, BookClient(), events=[e])
    assert result["ok"], result
    assert [o[0] for o in sim.orders] == ["buy", "sell", "buy", "sell"]
    assert sim.orders[2][1] == "busy~no" and sim.orders[2][2] == pytest.approx(0.51)  # NO ask = 1 - YES bid
    assert result["position_left"] == 0


def test_round_trip_reports_a_no_order_that_does_not_fill():
    from pmscan.us import run_test_trade

    e = parse_event(event("Big game", [market("busy", "Busy market", bid=0.49, ask=0.50, start=None)]))
    result = run_test_trade(SimExchange(no_fills=False), BookClient(), events=[e])
    assert not result["ok"] and "NO order" in result["verdict"]


def test_high_prob_buys_only_politics_and_season_futures_on_us_by_default():
    from pmscan.strategies.high_prob import HighProbability

    soon = (NOW.replace(day=3)).isoformat()
    ms = [market("pol", "Will X win?", category="politics", bid=0.94, ask=0.95),
          market("fut", "Champion: A", category="sports", bid=0.94, ask=0.95),
          market("cry", "BTC above 90k?", category="crypto", bid=0.94, ask=0.95),
          market("cul", "Album #1?", category="culture", bid=0.94, ask=0.95)]
    for m in ms:
        m["endDate"] = soon
    events = [parse_event(event(f"E{i}", [m])) for i, m in enumerate(ms)]
    ctx = ScanContext(config=ScanConfig(), now=NOW, venue="us")
    assert {c.payload.market.slug for c in HighProbability().screen(events, ctx)} == {"pol", "fut"}
    assert ctx.stats["high_prob_category_skipped"] == 2
    assert events[0].markets[0].category == "politics"
    cfg = ScanConfig()
    cfg.high_prob.include_us = ["crypto"]
    assert {c.payload.market.slug for c in HighProbability().screen(events, ScanContext(config=cfg, now=NOW, venue="us"))} \
        == {"cry"}


def test_high_prob_skips_us_props_and_game_winners_by_default():
    from pmscan.strategies.high_prob import HighProbability

    soon = (NOW.replace(day=3)).isoformat()
    ms = [market("prop", "Player 2+ hits", "props", bid=0.94, ask=0.95, start=soon),
          market("game", "A vs B", "moneyline", bid=0.94, ask=0.95, start=soon),
          market("fut", "Champion: A", "futures", bid=0.94, ask=0.95)]
    for m in ms:
        m["endDate"] = soon
    events = [parse_event(event(f"E{i}", [m], startTime=soon)) for i, m in enumerate(ms)]
    cfg = ScanConfig()
    ctx = ScanContext(config=cfg, now=NOW, venue="us")
    cfg.high_prob.include_us = []
    picked = {c.payload.market.slug for c in HighProbability().screen(events, ctx)}
    assert picked == {"fut"} and ctx.stats["high_prob_type_skipped"] == 2
    cfg.high_prob.exclude_market_types_us = []
    assert {c.payload.market.slug for c in HighProbability().screen(events, ScanContext(config=cfg, now=NOW, venue="us"))} \
        == {"prop", "game", "fut"}
    # polymarket.com markets are unaffected
    intl = ScanContext(config=ScanConfig(), now=NOW)
    assert len(HighProbability().screen(events, intl)) == 3
