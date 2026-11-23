"""Secrets come from environment variables, optionally loaded from a local .env file.

Keep secrets out of pmscan.toml: that file is configuration, .env is credentials,
and .gitignore excludes .env.
"""

from __future__ import annotations

import os
from pathlib import Path

ODDS_API_KEY = "PMSCAN_ODDS_API_KEY"
TELEGRAM_BOT_TOKEN = "PMSCAN_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "PMSCAN_TELEGRAM_CHAT_ID"
DISCORD_WEBHOOK_URL = "PMSCAN_DISCORD_WEBHOOK_URL"
PRIVATE_KEY = "POLYMARKET_PRIVATE_KEY"  # the key the bot signs orders with (ideally a session key)
OWNER_PRIVATE_KEY = "POLYMARKET_OWNER_PRIVATE_KEY"  # only for creating/revoking session keys
WALLET_ADDRESS = "POLYMARKET_WALLET_ADDRESS"
RELAYER_API_KEY = "POLYMARKET_RELAYER_API_KEY"
RELAYER_API_KEY_ADDRESS = "POLYMARKET_RELAYER_API_KEY_ADDRESS"
# Polymarket US (polymarket.us) API key, made at polymarket.us/developer
US_KEY_ID = "POLYMARKET_US_KEY_ID"
US_SECRET_KEY = "POLYMARKET_US_SECRET_KEY"
VENUE = "PMSCAN_VENUE"  # "international" (polymarket.com) or "us" (polymarket.us)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables already set."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def update_dotenv(values: dict[str, str], path: str | Path = ".env") -> None:
    """Set KEY=VALUE lines in .env, replacing existing ones and keeping everything else."""
    p = Path(path)
    lines = p.read_text().splitlines() if p.is_file() else []
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].removeprefix("export ").strip()
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    p.write_text("\n".join(lines) + "\n")
    p.chmod(0o600)


def get(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
