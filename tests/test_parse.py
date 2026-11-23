from datetime import datetime, timezone

from pmscan.parse import parse_book, parse_event, parse_market

RAW_MARKET = {
    "id": "123", "conditionId": "0xabc", "question": "Will it rain?", "slug": "rain",
    "groupItemTitle": "Rain", "outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]',
    "outcomePrices": '["0.4", "0.6"]', "bestBid": 0.39, "bestAsk": "0.41", "liquidityNum": 5000,
    "volume24hr": 1200.5, "endDate": "2026-12-01T00:00:00Z", "negRisk": True, "negRiskMarketID": "0xg",
    "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
    "feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True},
    "orderMinSize": 5, "gameStartTime": "2026-11-19 17:45:00+00",
}


def test_parse_market_decodes_json_string_fields():
    m = parse_market(RAW_MARKET, "ev", "Event")
    assert m is not None
    assert m.token_ids == ("111", "222")
    assert m.outcomes == ("Yes", "No")
    assert (m.best_bid, m.best_ask) == (0.39, 0.41)
    assert m.end_date == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert m.game_start == datetime(2026, 11, 19, 17, 45, tzinfo=timezone.utc)
    assert m.fees.rate == 0.04
    assert m.tradeable
    assert m.url == "https://polymarket.com/event/ev/rain"


def test_parse_market_treats_empty_sides_and_fee_flags():
    m = parse_market({**RAW_MARKET, "bestBid": 0, "bestAsk": 1, "feesEnabled": False}, "ev", "Event")
    assert m.best_bid is None and m.best_ask is None
    assert m.fees.rate == 0.0
    assert parse_market({**RAW_MARKET, "clobTokenIds": "[]"}) is None
    assert not parse_market({**RAW_MARKET, "acceptingOrders": False}).tradeable


def test_parse_event_tracks_already_resolved_neg_risk_groups():
    winner = {**RAW_MARKET, "id": "9", "closed": True, "outcomePrices": '["1", "0"]'}
    loser = {**RAW_MARKET, "id": "8", "closed": True, "outcomePrices": '["0", "1"]'}
    ev = parse_event({"id": "1", "slug": "ev", "title": "T", "markets": [RAW_MARKET, winner, loser],
                      "tags": [{"label": "Sports"}], "live": True})
    assert [m.id for m in ev.markets] == ["123"]
    assert ev.resolved_yes_groups == {"0xg"}
    assert ev.tags == ("Sports",) and ev.live


def test_parse_book_sorts_best_first_and_drops_bad_levels():
    b = parse_book({
        "asset_id": "111", "min_order_size": "5",
        "bids": [{"price": "0.01", "size": "10"}, {"price": "0.40", "size": "5"}, {"price": "0.2", "size": "0"}],
        "asks": [{"price": "0.99", "size": "10"}, {"price": "0.42", "size": "7"}],
    })
    assert b.best_bid == 0.40 and b.best_ask == 0.42
    assert [l.price for l in b.bids] == [0.40, 0.01]
    assert [l.price for l in b.asks] == [0.42, 0.99]
