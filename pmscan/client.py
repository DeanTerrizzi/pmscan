"""HTTP client for Polymarket's public Gamma (metadata) and CLOB (order book) APIs.

No API key is needed: everything used here is public, read-only market data.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from . import __version__
from .models import Event, OrderBook
from .parse import _json_list, parse_book, parse_event

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
BOOKS_BATCH_LIMIT = 500  # the /books endpoint rejects larger payloads
_RETRY_STATUS = {429, 500, 502, 503, 504}


class PolymarketClient:
    venue = "international"
    label = "Polymarket"

    def __init__(
        self,
        gamma_url: str = GAMMA_URL,
        clob_url: str = CLOB_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.max_retries = max_retries
        # A non-default User-Agent matters: Cloudflare blocks some library defaults.
        # Gamma requires gzip for event pages above 50 items; httpx decodes it.
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": f"pmscan/{__version__}", "Accept-Encoding": "gzip"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PolymarketClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("%s %s failed (%s); retrying in %.0fs", method, url, exc, delay)
            else:
                if resp.status_code not in _RETRY_STATUS or attempt == self.max_retries:
                    resp.raise_for_status()
                    return resp.json()
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                log.warning("%s %s -> %s; retrying in %.0fs", method, url, resp.status_code, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise RuntimeError("unreachable")

    def iter_event_pages(self, page_size: int = 100) -> Iterator[list[dict]]:
        """Yield pages of every open event (with nested markets) via keyset pagination."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"active": "true", "closed": "false", "limit": page_size}
            if cursor:
                params["after_cursor"] = cursor
            data = self._request("GET", f"{self.gamma_url}/events/keyset", params=params)
            events = data.get("events") or []
            if events:
                yield events
            cursor = data.get("next_cursor")
            if not cursor or not events:
                return

    def iter_events(self) -> Iterator[list[Event]]:
        """Pages of parsed open events."""
        for page in self.iter_event_pages():
            yield [parse_event(raw) for raw in page]

    def resolutions(self, market_ids: list[str]) -> dict[str, list[float]]:
        """Final payout per share [YES, NO] for the given markets that have resolved."""
        out: dict[str, list[float]] = {}
        for i in range(0, len(market_ids), 40):
            for raw in self.markets_by_ids(market_ids[i : i + 40], closed=True):
                prices = resolution_prices(raw)
                if prices is not None:
                    out[str(raw.get("id"))] = prices
        return out

    def markets_by_ids(self, ids: list[str], closed: bool | None = None) -> list[dict]:
        """Raw Gamma market objects by id (up to ~50 per call)."""
        params: list[tuple[str, Any]] = [("id", i) for i in ids] + [("limit", len(ids))]
        if closed is not None:
            params.append(("closed", "true" if closed else "false"))
        return self._request("GET", f"{self.gamma_url}/markets", params=params) or []

    def price_history(self, token_id: str, interval: str = "1w", fidelity: int = 60) -> list[dict]:
        """[{t: unix seconds, p: price}] for one token. interval: 1h, 6h, 1d, 1w, 1m, max."""
        data = self._request("GET", f"{self.clob_url}/prices-history",
                             params={"market": token_id, "interval": interval, "fidelity": fidelity})
        return data.get("history") or []

    def get_books(self, token_ids: Iterable[str], workers: int = 4) -> dict[str, OrderBook]:
        """Fetch order books for many tokens, batched and in parallel."""
        unique = list(dict.fromkeys(token_ids))
        chunks = [unique[i : i + BOOKS_BATCH_LIMIT] for i in range(0, len(unique), BOOKS_BATCH_LIMIT)]

        def fetch(chunk: list[str]) -> list[dict]:
            payload = [{"token_id": t} for t in chunk]
            return self._request("POST", f"{self.clob_url}/books", json=payload) or []

        books: dict[str, OrderBook] = {}
        if not chunks:
            return books
        with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
            for raw_books in pool.map(fetch, chunks):
                for raw in raw_books:
                    book = parse_book(raw)
                    books[book.token_id] = book
        return books


def resolution_prices(raw: dict) -> list[float] | None:
    """Final payout per share for each outcome of a Gamma market, or None if not resolved yet."""
    if not raw.get("closed"):
        return None
    prices = []
    for p in _json_list(raw.get("outcomePrices")):
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            return None
    if len(prices) != 2:
        return None
    settled = all(p in (0.0, 1.0) for p in prices) or raw.get("umaResolutionStatus") == "resolved"
    return prices if settled else None
