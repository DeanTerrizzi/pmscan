import json
from datetime import timedelta

import pytest
from conftest import NOW, book
from test_paper import FakeClient, market, scan
from test_us import event
from test_us import market as us_market

from pmscan.backtest import history_group, near_certain_record, price_bucket
from pmscan.config import PaperConfig, ScanConfig
from pmscan.models import Leg, Opportunity
from pmscan.paper import PaperTrader
from pmscan.strategies import NearCertain, ScanContext
from pmscan.us import parse_event
from pmscan.web import Api, _State

D = 86400


def us_events():
    soon, far = (NOW + timedelta(days=2)).isoformat(), (NOW + timedelta(days=10)).isoformat()
    ms = [us_market("pol", "Will X resign?", category="politics", bid=0.01, ask=0.02, start=None, endDate=soon),
          us_market("far", "Will Y resign?", category="politics", bid=0.01, ask=0.02, start=None, endDate=far),
          us_market("cul", "Album #1?", category="culture", bid=0.01, ask=0.02, start=None, endDate=soon),
          us_market("p95", "Will Z resign?", category="politics", bid=0.05, ask=0.06, start=None, endDate=soon)]
    return [parse_event(event(f"E{i}", [m])) for i, m in enumerate(ms)]


def test_screens_only_politics_at_97c_plus_ending_soon():
    ctx = ScanContext(config=ScanConfig(), now=NOW, venue="us")
    picked = NearCertain().screen(us_events(), ctx)
    assert [(c.payload.market.slug, c.payload.side) for c in picked] == [("pol", 1)]  # NO at 99c
    assert ctx.stats["near_certain_category_skipped"] == 1


def test_evaluate_tags_the_history_group_and_the_cost_of_a_loss():
    ctx = ScanContext(config=ScanConfig(), now=NOW, venue="us")
    [cand] = NearCertain().screen(us_events(), ctx)
    token = cand.token_ids[0]
    opp = NearCertain().evaluate(cand, {token: book(token, [(0.99, 1000)], [(0.98, 1000)])}, ctx)
    assert opp.strategy == "near_certain" and opp.legs[0].avg_price == pytest.approx(0.99)
    assert opp.details == {"group": "politics", "bucket": 0.99}
    assert any("about 109 wins" in n for n in opp.notes)  # $0.9909 at risk for $0.0091


def test_history_groups_and_price_buckets():
    assert history_group("sports", "futures") == "futures"
    assert history_group("sports", "props") == "sports_other"
    assert history_group("politics", "futures") == "politics"
    assert price_bucket(0.975) == 0.97 and price_bucket(0.99) == 0.99 and price_bucket(0.995) == 0.99
    assert price_bucket(0.96) is None


def write_backtest(root):
    a = 1_800_000_000.0
    markets = [
        {"slug": "a", "group": "politics", "category": "politics", "market_type": "futures", "yes_settle": 0.0,
         "anchor": a, "theta": 0.095},
        {"slug": "b", "group": "politics", "category": "politics", "market_type": "futures", "yes_settle": 0.0,
         "anchor": a, "theta": 0.095},
        {"slug": "c", "group": "culture", "category": "culture", "market_type": "futures", "yes_settle": 1.0,
         "anchor": a, "theta": 0.095},
    ]
    history = [
        {"slug": "a", "points": [[a - 5 * D, 0.03, 0.98], [a, 0.001, 0.999]]},  # NO at 98c, 3 days out: won
        {"slug": "b", "points": [[a - 2 * D, 0.99, 0.02], [a, 0.01, 0.99]]},  # YES at 99c, 1 day out: lost
        {"slug": "c", "points": [[a - 5 * D, 0.97, 0.04], [a, 0.999, 0.001]]},
    ]
    bt = root / "backtest"
    bt.mkdir()
    (bt / "markets.jsonl").write_text("".join(json.dumps(m) + "\n" for m in markets))
    (bt / "history.jsonl").write_text("".join(json.dumps(h) + "\n" for h in history))


def test_record_counts_each_market_once_at_its_earliest_entry(tmp_path):
    write_backtest(tmp_path)
    rec = near_certain_record(tmp_path)
    cells = {(r["group"], r["bucket"]): r for r in rec["rows"]}
    assert rec["markets"] == 3
    assert (cells["politics", 0.98]["n"], cells["politics", 0.98]["won"]) == (1, 1)
    assert (cells["politics", 0.99]["n"], cells["politics", 0.99]["won"]) == (1, 0)
    assert cells["politics", 0.99]["return"] == -1.0
    assert (cells["politics", 0.0]["n"], cells["politics", 0.0]["won"]) == (2, 1)
    assert cells["culture", 0.97]["won"] == 1


def test_paper_buys_near_certain_bets_with_their_own_stake_and_floor(tmp_path):
    m = market()
    client = FakeClient(books={"n1": book("n1", [(0.95, 1000), (0.98, 1000)])})
    trader = PaperTrader(PaperConfig(strategies=["near_certain"]), client, tmp_path)
    leg = Leg.for_market(m, 1, shares=1000, avg_price=0.98, limit_price=0.98, label=m.question)
    opp = Opportunity(strategy="near_certain", kind="buy_no", title=m.question, subtitle="", url=m.url, cost=980,
                      payout=1000, roi=0.02, annualized=2.0, days_to_end=2, risk="", notes=[], legs=[leg], score=1.0)
    trader.process(scan([m], [opp]), NOW)
    [p] = trader.books["near_certain"].open.values()
    assert p.cost == pytest.approx(200) and p.avg_price == pytest.approx(0.98)  # skipped the 95c ask
    s = trader.books["near_certain"].summary()
    assert s["title"] == "Near-certain bets" and s["stake_usd"] == 200 and s["min_price"] == 0.97


def test_near_certain_route(tmp_path):
    write_backtest(tmp_path)

    class Client:
        venue = "us"

    status, body = Api(_State(60, None), tmp_path, Client()).handle("/api/near-certain", {})
    assert status == 200 and body["record"]["markets"] == 3
    assert body["settings"]["include_us"] == ["politics"] and body["settings"]["stake_usd"] == 200
    assert body["paper"]["strategy"] == "near_certain"
