"""Bookmaker odds from The Odds API (https://the-odds-api.com), turned into fair probabilities.

Each call costs one credit per region per market, so responses are cached for
``refresh_minutes`` and only sports with upcoming Polymarket games are fetched. When the
API reports how many credits are left this month, refreshes also slow down so they last
until the month resets (the free plan has 500).
"""

from __future__ import annotations

import logging
import re
import statistics
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import httpx

from .parse import parse_datetime

log = logging.getLogger(__name__)

ODDS_API_URL = "https://api.the-odds-api.com/v4"

# Polymarket league code -> Odds API sport key. Override or extend with
# [sportsbook_value].sport_keys in pmscan.toml; GET /v4/sports lists valid keys.
SPORT_KEYS: dict[str, str] = {
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "cbb": "basketball_ncaab",
    "ncaab": "basketball_ncaab",
    "euroleague": "basketball_euroleague",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "epl": "soccer_epl",
    "lal": "soccer_spain_la_liga",
    "bun": "soccer_germany_bundesliga",
    "sea": "soccer_italy_serie_a",
    "fl1": "soccer_france_ligue_one",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "mls": "soccer_usa_mls",
    "ere": "soccer_netherlands_eredivisie",
    "elc": "soccer_efl_champ",
    "mex": "soccer_mexico_ligamx",
    "ufc": "mma_mixed_martial_arts",
    # Polymarket US league codes that differ from polymarket.com's
    "wcbb": "basketball_wncaab",
    "eurolg": "basketball_euroleague",
    "eflch": "soccer_efl_champ",
    "efl1": "soccer_england_league1",
    "efl2": "soccer_england_league2",
    "lg1": "soccer_france_ligue_one",
    "bra": "soccer_brazil_campeonato",
    "j1": "soccer_japan_j_league",
    "uecl": "soccer_uefa_europa_conference_league",
    "ahl": "icehockey_ahl",
    "boxing": "boxing_boxing",
}

DRAW = "draw"


@dataclass
class BookEvent:
    id: str
    sport_key: str
    commence: datetime | None
    home: str
    away: str
    # bookmaker key -> {outcome name: de-vigged probability}
    books: dict[str, dict[str, float]] = field(default_factory=dict)

    def fair(self, bookmakers: list[str] | None = None, min_books: int = 1) -> dict[str, float] | None:
        """Median de-vigged probability per outcome across bookmakers, renormalized."""
        books = {k: v for k, v in self.books.items() if not bookmakers or k in bookmakers}
        if len(books) < min_books:
            return None
        names = set.intersection(*(set(v) for v in books.values())) if books else set()
        if len(names) < 2:
            return None
        med = {n: statistics.median(b[n] for b in books.values()) for n in names}
        total = sum(med.values())
        return {n: p / total for n, p in med.items()}


def devig(prices: dict[str, float]) -> dict[str, float] | None:
    """Decimal odds -> probabilities with the bookmaker margin removed proportionally."""
    implied = {n: 1.0 / p for n, p in prices.items() if p and p > 1.0}
    total = sum(implied.values())
    if len(implied) < 2 or total <= 0:
        return None
    return {n: q / total for n, q in implied.items()}


def parse_odds_events(raw_events: list[dict]) -> list[BookEvent]:
    events = []
    for raw in raw_events:
        ev = BookEvent(
            id=str(raw.get("id", "")),
            sport_key=str(raw.get("sport_key", "")),
            commence=parse_datetime(raw.get("commence_time")),
            home=str(raw.get("home_team", "")),
            away=str(raw.get("away_team", "")),
        )
        for bm in raw.get("bookmakers") or []:
            for market in bm.get("markets") or []:
                if market.get("key") != "h2h":
                    continue  # h2h_lay (exchange lay prices) is a different quantity
                prices = {str(o.get("name")): float(o.get("price") or 0) for o in market.get("outcomes") or []}
                probs = devig(prices)
                if probs:
                    ev.books[str(bm.get("key"))] = probs
        events.append(ev)
    return events


_STOP = {"fc", "sc", "cf", "afc", "ac", "club", "the", "de", "cd", "ud", "sd", "fk", "sk", "if", "bk", "ssc", "as"}


def _norm_tokens(name: str) -> set[str]:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return (tokens - _STOP) or tokens


def same_team(a: str, b: str) -> bool:
    """Loose team-name match: 'Texans' ~ 'Houston Texans', 'Paris FC' ~ 'Paris FC'."""
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    return SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() >= 0.85


class OddsApiClient:
    def __init__(self, api_key: str, regions: str = "us", refresh_minutes: float = 60.0,
                 http: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.regions = regions
        self.ttl = refresh_minutes * 60.0
        self.min_reserve = 10  # credits kept back so a manual check still works
        self._http = http or httpx.Client(timeout=20.0, headers={"User-Agent": "pmscan"})
        self._cache: dict[str, tuple[float, list[BookEvent]]] = {}
        self.requests_remaining: str | None = None

    def ttl_for(self, active_sports: int, now: datetime | None = None) -> float:
        """Seconds between refreshes: refresh_minutes, or slower if credits wouldn't last the month."""
        try:
            remaining = float(self.requests_remaining) if self.requests_remaining is not None else None
        except ValueError:
            remaining = None
        if remaining is None:
            return self.ttl
        now = now or datetime.now(timezone.utc)
        month_end = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        left = (month_end - now).total_seconds()
        usable = remaining - self.min_reserve
        if usable <= 0:
            return left  # out of credits: wait for the reset
        return max(self.ttl, left * max(active_sports, 1) / usable)

    def odds(self, sport_key: str, active_sports: int = 1) -> list[BookEvent]:
        cached = self._cache.get(sport_key)
        if cached and time.monotonic() - cached[0] < self.ttl_for(active_sports):
            return cached[1]
        try:
            resp = self._http.get(
                f"{ODDS_API_URL}/sports/{sport_key}/odds/",
                params={"apiKey": self.api_key, "regions": self.regions, "markets": "h2h",
                        "oddsFormat": "decimal", "dateFormat": "iso"},
            )
            self.requests_remaining = resp.headers.get("x-requests-remaining", self.requests_remaining)
            if resp.status_code == 404:  # unknown or out-of-season sport key
                log.info("Odds API has no sport %s", sport_key)
                events: list[BookEvent] = []
            else:
                resp.raise_for_status()
                events = parse_odds_events(resp.json())
        except httpx.HTTPError as exc:
            log.warning("Odds API request for %s failed: %s", sport_key, exc)
            return cached[1] if cached else []
        self._cache[sport_key] = (time.monotonic(), events)
        return events
