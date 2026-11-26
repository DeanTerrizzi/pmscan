import json
from dataclasses import replace

from conftest import NOW, book, make_event, make_market

from pmscan.models import Leg, Opportunity
from pmscan.scanner import ScanResult
from pmscan.web import Api, MarketIndex, _State, _static_file


class FakeClient:
    def __init__(self):
        self.history_calls = 0

    def price_history(self, token, interval, fidelity):
        self.history_calls += 1
        return [{"t": 1, "p": 0.4}, {"t": 2, "p": 0.45}]

    def get_books(self, tokens):
        return {t: book(t, [(0.52, 10)], [(0.48, 20)]) for t in tokens}


def opp(strategy, profit=2.0):
    leg = Leg(market="m", outcome="Yes", token_id="y1", shares=10, avg_price=0.5, limit_price=0.5, url="u")
    return Opportunity(strategy=strategy, kind="k", title="T", subtitle="", url="https://x", cost=10.0,
                       payout=10.0 + profit, roi=profit / 10, annualized=None, days_to_end=1.0, risk="low",
                       notes=[], legs=[leg])


def result():
    btc = replace(make_market(1, bid=0.4, ask=0.42, group=None), question="Will Bitcoin hit $100k?", volume_24h=500)
    eth = replace(make_market(2, bid=0.1, ask=0.12, group=None), question="Will Ethereum flip Bitcoin?", volume_24h=900)
    return ScanResult(started_at=NOW, duration_s=12.3, stats={"events": 1, "tradeable_markets": 2, "books_fetched": 3},
                      opportunities={"neg_risk_arb": [opp("neg_risk_arb"), opp("neg_risk_arb", 1.0)],
                                     "high_prob": [opp("high_prob")]},
                      events=[make_event([btc, eth])])


def api(tmp_path, with_index=True):
    state = _State(120, None)
    r = result()
    state.result = r.to_dict()
    if with_index:
        state.index = MarketIndex(r.events)
    return Api(state, tmp_path, FakeClient())


def test_summary_and_save_split_history(tmp_path):
    r = result()
    s = r.summary()
    assert s["neg_risk_arb"] == {"count": 2, "profit": 3.0, "best_roi": 0.2, "best_score": 0.0}
    r.save(tmp_path)
    r.save(tmp_path)
    assert len((tmp_path / "scans.jsonl").read_text().splitlines()) == 2
    history = [json.loads(l) for l in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert {h["strategy"] for h in history} == {"neg_risk_arb"}  # high_prob rows stay out
    assert "events" not in json.loads((tmp_path / "latest.json").read_text())


def test_scans_history_and_trades_routes(tmp_path):
    a = api(tmp_path)
    result().save(tmp_path)
    status, scans = a.handle("/api/scans", {"limit": "5"})
    assert status == 200 and scans[0]["events"] == 1
    status, rows = a.handle("/api/history", {"strategy": "neg_risk_arb"})
    assert status == 200 and len(rows) == 2
    (tmp_path / "trades.jsonl").write_text('{"ts": "2026-01-01T00:00:00+00:00", "mode": "dry_run"}\n')
    assert a.handle("/api/trades", {})[1][0]["mode"] == "dry_run"
    status, snap = a.handle("/api/latest", {})
    assert status == 200 and snap["trading"] == {"mode": "off"} and snap["result"]["duration_s"] == 12.3


def test_market_search_and_lookup(tmp_path):
    a = api(tmp_path)
    status, hits = a.handle("/api/markets", {"q": "bitcoin"})
    assert [m["question"] for m in hits] == ["Will Ethereum flip Bitcoin?", "Will Bitcoin hit $100k?"]  # by volume
    assert [m["id"] for m in a.handle("/api/markets", {"q": "bitcoin 100k"})[1]] == ["1"]
    status, m = a.handle("/api/market", {"token": "n1"})
    assert status == 200 and m["token_ids"] == ["y1", "n1"]
    assert a.handle("/api/market", {"token": "nope"})[0] == 404
    assert api(tmp_path, with_index=False).handle("/api/markets", {"q": "x"})[0] == 503


def test_price_history_is_cached_and_validated(tmp_path):
    a = api(tmp_path)
    assert a.handle("/api/price-history", {"token": "y1", "interval": "1w"})[1][1]["p"] == 0.45
    a.handle("/api/price-history", {"token": "y1", "interval": "1w"})
    assert a.client.history_calls == 1
    assert a.handle("/api/price-history", {"token": "y1", "interval": "5y"})[0] == 400
    status, b = a.handle("/api/book", {"token": "y1"})
    assert status == 200 and b["asks"] == [[0.52, 10]] and b["bids"] == [[0.48, 20]]
    assert a.handle("/api/nope", {})[0] == 404


def test_static_files_cannot_escape_the_static_dir():
    assert _static_file("/static/app/main.js") is not None
    assert _static_file("/static/vendor/preact.module.js") is not None
    assert _static_file("/static/../web.py") is None
    assert _static_file("/static/%2e%2e/web.py") is None
    assert _static_file("/static/app/") is None


def test_paper_and_account_routes(tmp_path):
    a = api(tmp_path)
    status, snap = a.handle("/api/paper", {})
    assert status == 200 and snap["summary"]["equity"] == 50_000  # five strategies x $10,000
    assert snap["books"]["high_prob"]["open"] == []
    assert a.handle("/api/latest", {})[1]["paper"]["open_positions"] == 0
    assert a.handle("/api/account", {})[1] == {"connected": False}


def test_wait_or_wake_returns_early_after_sleep(monkeypatch):
    import threading

    from pmscan import web

    stop = threading.Event()
    assert web.wait_or_wake(stop, 0.05, step=0.01) is False  # no sleep: waits it out
    real = web.time.time
    calls = iter(range(10_000))
    monkeypatch.setattr(web.time, "time", lambda: real() + 120 * next(calls))  # wall clock races ahead
    assert web.wait_or_wake(stop, 60, step=0.01) is True
    stop.set()
    assert web.wait_or_wake(stop, 60) is False


def test_failed_scans_retry_sooner_and_back_off(monkeypatch, tmp_path):
    import threading

    from pmscan import web

    stop, waits = threading.Event(), []

    def fake_wait(_stop, seconds):
        waits.append(round(seconds))
        if len(waits) == 4:
            stop.set()
        return False

    class Broken:
        def scan(self):
            raise ConnectionError("offline")

    monkeypatch.setattr(web, "wait_or_wake", fake_wait)
    state = web._State(interval=120, trader=None)
    web._scan_loop(Broken(), state, tmp_path, stop, None)
    assert waits == [15, 30, 60, 120]
    assert state.error == "ConnectionError: offline"
