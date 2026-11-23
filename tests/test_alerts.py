import json
from dataclasses import replace

import httpx
from conftest import NOW

from pmscan import env
from pmscan.alerts import Alert, AlertManager, DiscordNotifier, TelegramNotifier, notifiers_from_env
from pmscan.config import AlertsConfig
from pmscan.models import Leg, Opportunity
from pmscan.scanner import ScanResult


class Collect:
    name = "collect"

    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)


def opp(strategy="neg_risk_arb", payout=105.0, token="t1", **kw):
    leg = Leg(market="m", outcome="Yes", token_id=token, shares=100, avg_price=0.5, limit_price=0.5, url="u")
    return Opportunity(strategy=strategy, kind="k", title="Who wins?", subtitle="Buy every YES", url="https://x",
                       cost=100.0, payout=payout, roi=(payout - 100) / 100, annualized=None, days_to_end=3.0,
                       risk="low", notes=[], legs=[leg], **kw)


def result(*opps):
    by = {}
    for o in opps:
        by.setdefault(o.strategy, []).append(o)
    return ScanResult(started_at=NOW, duration_s=1.0, stats={}, opportunities=by)


def test_alerts_once_per_cooldown_and_respect_thresholds():
    sink = Collect()
    mgr = AlertManager(AlertsConfig(), [sink])
    small = opp(payout=101.0, token="t2")  # $1 profit < $2 threshold
    weak = opp("sportsbook_value", token="t3", details={"ev": 0.02})
    strong = opp("sportsbook_value", token="t4", details={"ev": 0.08, "fair": 0.6})
    not_alerted = opp("high_prob", token="t5")
    sent = mgr.process(result(opp(), small, weak, strong, not_alerted))
    assert [o.legs[0].token_id for o in sent] == ["t1", "t4"]  # arbitrage first
    assert sink.alerts[0].title.startswith("Arbitrage: $5.00")
    assert mgr.process(result(opp(), strong)) == []  # same opportunities: cooldown


def test_inactive_without_notifiers():
    assert not AlertManager(AlertsConfig(), []).active
    assert AlertManager(AlertsConfig(), []).process(result(opp())) == []


def test_max_per_scan():
    sink = Collect()
    mgr = AlertManager(replace(AlertsConfig(), max_per_scan=2), [sink])
    mgr.process(result(*[opp(token=f"t{i}") for i in range(5)]))
    assert len(sink.alerts) == 2


def capture():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    return requests, httpx.Client(transport=httpx.MockTransport(handler))


def test_telegram_payload_escapes_html():
    requests, http = capture()
    TelegramNotifier("TOKEN", "42", http).send(Alert("A <b> & C", ["x < y"], "https://p.com/?a=1&b=2"))
    [req] = requests
    assert req.url == "https://api.telegram.org/botTOKEN/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert "<b>A &lt;b&gt; &amp; C</b>" in body["text"] and "x &lt; y" in body["text"]
    assert 'href="https://p.com/?a=1&amp;b=2"' in body["text"]


def test_discord_payload():
    requests, http = capture()
    DiscordNotifier("https://discord.test/hook", http).send(Alert("Title", ["line"], "https://p.com"))
    assert json.loads(requests[0].content) == {"content": "**Title**\nline\n<https://p.com>"}


def test_notifiers_from_env(monkeypatch):
    for name in (env.TELEGRAM_BOT_TOKEN, env.TELEGRAM_CHAT_ID, env.DISCORD_WEBHOOK_URL):
        monkeypatch.delenv(name, raising=False)
    assert notifiers_from_env() == []
    monkeypatch.setenv(env.TELEGRAM_BOT_TOKEN, "t")
    assert notifiers_from_env() == []  # needs the chat id too
    monkeypatch.setenv(env.TELEGRAM_CHAT_ID, "1")
    monkeypatch.setenv(env.DISCORD_WEBHOOK_URL, "https://d")
    assert [n.name for n in notifiers_from_env()] == ["telegram", "discord"]


def test_load_dotenv(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# comment\nexport PMSCAN_A="quoted value"\nPMSCAN_B=plain\nPMSCAN_C=keep-me\n')
    monkeypatch.delenv("PMSCAN_A", raising=False)
    monkeypatch.delenv("PMSCAN_B", raising=False)
    monkeypatch.setenv("PMSCAN_C", "already-set")
    env.load_dotenv(f)
    assert env.get("PMSCAN_A") == "quoted value" and env.get("PMSCAN_B") == "plain"
    assert env.get("PMSCAN_C") == "already-set"
