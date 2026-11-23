"""Push notifications for new opportunities and trades: Telegram and/or Discord.

Configure channels with environment variables (or .env):

* Telegram: PMSCAN_TELEGRAM_BOT_TOKEN and PMSCAN_TELEGRAM_CHAT_ID
* Discord:  PMSCAN_DISCORD_WEBHOOK_URL
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field

import httpx

from . import env
from .config import AlertsConfig
from .models import Opportunity
from .scanner import ScanResult
from .strategies import ARBITRAGE

log = logging.getLogger(__name__)


@dataclass
class Alert:
    title: str
    lines: list[str] = field(default_factory=list)
    url: str | None = None


class Notifier:
    name = ""

    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, http: httpx.Client) -> None:
        self.token, self.chat_id, self.http = token, chat_id, http

    def send(self, alert: Alert) -> None:
        parts = [f"<b>{html.escape(alert.title)}</b>", *(html.escape(l) for l in alert.lines)]
        if alert.url:
            parts.append(f'<a href="{html.escape(alert.url, quote=True)}">Open on Polymarket</a>')
        resp = self.http.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": "\n".join(parts)[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
        )
        resp.raise_for_status()


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, webhook_url: str, http: httpx.Client) -> None:
        self.webhook_url, self.http = webhook_url, http

    def send(self, alert: Alert) -> None:
        parts = [f"**{alert.title}**", *alert.lines]
        if alert.url:
            parts.append(f"<{alert.url}>")  # angle brackets suppress the link preview
        resp = self.http.post(self.webhook_url, json={"content": "\n".join(parts)[:2000]})
        resp.raise_for_status()


def notifiers_from_env(http: httpx.Client | None = None) -> list[Notifier]:
    http = http or httpx.Client(timeout=15.0)
    out: list[Notifier] = []
    token, chat = env.get(env.TELEGRAM_BOT_TOKEN), env.get(env.TELEGRAM_CHAT_ID)
    if token and chat:
        out.append(TelegramNotifier(token, chat, http))
    hook = env.get(env.DISCORD_WEBHOOK_URL)
    if hook:
        out.append(DiscordNotifier(hook, http))
    return out


def format_opportunity(o: Opportunity) -> Alert:
    if o.strategy in ARBITRAGE:
        title = f"Arbitrage: ${o.profit:,.2f} profit ({o.roi * 100:.2f}%)"
        lines = [o.title, o.subtitle, f"Cost ${o.cost:,.2f} -> guaranteed ${o.payout:,.2f}"]
    elif o.strategy == "sportsbook_value":
        d = o.details
        title = f"Value bet: {d.get('ev', o.score) * 100:+.1f}% EV"
        lines = [o.title, f"{o.subtitle} @ {o.legs[0].avg_price * 100:.1f}¢ "
                 f"(books: {d.get('fair', 0) * 100:.1f}%)", f"Depth ${o.cost:,.0f}"]
    else:
        title = f"{o.strategy}: {o.roi * 100:.2f}% if it wins"
        lines = [o.title, o.subtitle]
    if o.days_to_end is not None:
        lines.append(f"Ends in {o.days_to_end:,.1f} days" if o.days_to_end >= 1 else f"Ends in {o.days_to_end * 24:.1f} hours")
    return Alert(title, lines, o.url)


class AlertManager:
    """Sends each qualifying opportunity once per cooldown window."""

    def __init__(self, config: AlertsConfig, notifiers: list[Notifier]) -> None:
        self.config = config
        self.notifiers = notifiers
        self._sent: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return self.config.enabled and bool(self.notifiers)

    def qualifies(self, o: Opportunity) -> bool:
        if o.strategy not in self.config.strategies:
            return False
        if o.strategy in ARBITRAGE:
            return o.profit >= self.config.min_profit_usd
        if o.strategy == "sportsbook_value":
            return o.details.get("ev", o.score) >= self.config.min_edge
        return True

    def send(self, alert: Alert) -> bool:
        ok = False
        for n in self.notifiers:
            try:
                n.send(alert)
                ok = True
            except httpx.HTTPError as exc:
                log.warning("%s alert failed: %s", n.name, exc)
        return ok

    def process(self, result: ScanResult) -> list[Opportunity]:
        if not self.active:
            return []
        now = time.monotonic()
        window = self.config.cooldown_minutes * 60
        self._sent = {k: t for k, t in self._sent.items() if now - t < window}
        fresh = [o for opps in result.opportunities.values() for o in opps
                 if self.qualifies(o) and o.key not in self._sent]
        # Arbitrage first (largest profit first), then everything else by its own score.
        fresh.sort(key=lambda o: (o.strategy in ARBITRAGE, o.profit if o.strategy in ARBITRAGE else o.score),
                   reverse=True)
        sent = []
        for o in fresh[: self.config.max_per_scan]:
            if self.send(format_opportunity(o)):
                self._sent[o.key] = now
                sent.append(o)
        return sent
