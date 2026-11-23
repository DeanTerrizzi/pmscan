from decimal import Decimal
from types import SimpleNamespace

from conftest import NOW, book, make_event, make_market

from pmscan.config import ScanConfig
from pmscan.fast import FastWatcher
from pmscan.scanner import ScanResult
from pmscan.strategies import LadderArbitrage, NegRiskArbitrage
from pmscan.web import _State


class FakeClient:
    def __init__(self, books):
        self.books = books
        self.calls = 0

    def get_books(self, tokens):
        self.calls += 1
        return {t: self.books[t] for t in tokens if t in self.books}


def setup(books):
    markets = [make_market(i, bid=None, ask=0.34, group="g") for i in range(3)]  # asks sum to 1.02: no edge
    event = make_event(markets)
    scanner = SimpleNamespace(config=ScanConfig(), strategies=[NegRiskArbitrage(), LadderArbitrage()],
                              client=FakeClient(books))
    found = []
    watcher = FastWatcher(scanner, on_found=found.append)
    result = ScanResult(started_at=NOW, duration_s=1, stats={"watch": {event.id: -0.02}}, opportunities={},
                        events=[event])
    watcher.update(result)
    return watcher, markets, found, scanner


def quote(asset, bid, ask):
    return SimpleNamespace(type="best_bid_ask", payload=SimpleNamespace(asset_id=asset, best_bid=bid, best_ask=ask))


def test_watches_the_closest_events_one_token_per_market():
    watcher, *_ = setup({})
    assert watcher.stats["watching_events"] == 1 and sorted(watcher._tokens) == ["y0", "y1", "y2"]


def test_price_move_into_an_edge_is_verified_and_reported_once():
    books = {"y0": book("y0", [(0.25, 100)]), "y1": book("y1", [(0.34, 100)]), "y2": book("y2", [(0.34, 100)])}
    watcher, markets, found, scanner = setup(books)
    watcher._apply(quote("y1", Decimal("0.33"), Decimal("0.34")))  # still no edge: books not fetched
    watcher._check()
    assert scanner.client.calls == 0 and found == []

    watcher._apply(quote("y0", Decimal("0.24"), Decimal("0.25")))  # asks now sum to 0.93
    assert markets[0].best_ask == 0.25
    watcher._check()
    [result] = found
    [opp] = result.opportunities["neg_risk_arb"]
    assert opp.kind == "buy_all_yes" and opp.profit > 0

    watcher._apply(quote("y0", Decimal("0.24"), Decimal("0.25")))
    watcher._check()
    assert len(found) == 1  # the same opportunity isn't reported again unless it gets better


def test_opportunities_the_scan_already_found_are_not_news():
    books = {"y0": book("y0", [(0.25, 100)]), "y1": book("y1", [(0.34, 100)]), "y2": book("y2", [(0.34, 100)])}
    watcher, markets, found, scanner = setup(books)
    watcher._apply(quote("y0", Decimal("0.24"), Decimal("0.25")))
    watcher._check()
    [first] = found
    # the next scan lists it too: re-seeding means the feed stays quiet about it
    event = next(iter(watcher._events.values()))
    watcher.update(ScanResult(started_at=NOW, duration_s=1, stats={"watch": {event.id: 0.05}},
                              opportunities=first.opportunities, events=[event]))
    found.clear()
    watcher._apply(quote("y2", Decimal("0.33"), Decimal("0.34")))
    watcher._check()
    assert found == []


def test_no_side_quotes_mirror_and_empty_sides_clear():
    watcher, markets, *_ = setup({})
    watcher._apply(quote("n2", Decimal("0.60"), Decimal("0.62")))  # NO bid/ask -> YES ask/bid
    assert (markets[2].best_bid, markets[2].best_ask) == (0.38, 0.40)
    watcher._apply(quote("y1", Decimal("0"), Decimal("1")))
    assert markets[1].best_bid is None and markets[1].best_ask is None


def test_found_opportunities_are_merged_into_the_latest_result():
    from pmscan.models import Leg, Opportunity

    leg = Leg(market="m", outcome="Yes", token_id="y0", shares=1, avg_price=0.3, limit_price=0.3, url="u")
    opp = Opportunity(strategy="neg_risk_arb", kind="buy_all_yes", title="Who wins?", subtitle="", url="u",
                      cost=9, payout=10, roi=0.1, annualized=None, days_to_end=1, risk="low", notes=[], legs=[leg],
                      score=1.0)
    state = _State(120, None)
    state.result = {"strategies": [{"name": "neg_risk_arb", "opportunities": [{"key": opp.key, "score": 0.5}]}]}
    state.merge_found(ScanResult(started_at=NOW, duration_s=0, stats={}, opportunities={"neg_risk_arb": [opp]}),
                      ["found one"])
    [merged] = state.result["strategies"][0]["opportunities"]
    assert merged["live"] and merged["score"] == 1.0
    assert state.messages[-1].endswith("live feed: found one")


def test_polymarket_us_stream_messages_update_quotes():
    watcher, markets, _, scanner = setup({})
    scanner.client.venue = "us"
    watcher._apply_us({"marketDataLite": {"marketSlug": "y1", "bestBid": {"value": "0.31", "currency": "USD"},
                                          "bestAsk": {"value": "0.33", "currency": "USD"}}})
    assert (markets[1].best_bid, markets[1].best_ask) == (0.31, 0.33)
    assert watcher._dirty == {"e1"} and watcher.venue == "us"


def test_polymarket_us_feed_needs_the_api_key(monkeypatch):
    import asyncio

    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)
    watcher, _, _, scanner = setup({})
    scanner.client.venue = "us"
    asyncio.run(watcher._main_us())
    assert watcher.stats["enabled"] is False and "API key" in watcher.stats["error"]
