"""Core data types shared across the scanner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Polymarket taker fee curve.

    Polymarket documents the fee as ``fee = shares * rate * p * (1 - p)``, charged
    in USDC to the taker. Markets also carry an ``exponent`` that is applied to the
    price component of the curve; with the common exponent of 1 this reduces to the
    documented formula. Makers pay nothing.
    """

    rate: float = 0.0
    exponent: float = 1.0

    def per_share(self, price: float) -> float:
        if self.rate <= 0.0 or not 0.0 < price < 1.0:
            return 0.0
        return self.rate * (price * (1.0 - price)) ** self.exponent


NO_FEES = FeeSchedule()


@dataclass(slots=True)
class Market:
    """One binary market (a YES and a NO token) as reported by the Gamma API."""

    id: str
    condition_id: str
    question: str
    slug: str
    group_title: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    best_bid: float | None  # best bid on the first (YES) token
    best_ask: float | None  # best ask on the first (YES) token
    liquidity: float
    volume_24h: float
    end_date: datetime | None
    neg_risk: bool
    neg_risk_id: str | None
    is_other: bool
    tradeable: bool
    fees: FeeSchedule
    min_order_size: float
    game_start: datetime | None = None  # sports markets only
    sports_market_type: str = ""  # "moneyline", "spreads", "totals", ...
    version: str = "v1"  # "v2" = Polymarket Protocol V2 (position IDs, not CTF token IDs)
    seconds_delay: float = 0.0  # matching delay some markets impose on marketable orders
    event_slug: str = ""
    event_title: str = ""
    venue: str = "international"  # or "us" (Polymarket US, polymarket.us)
    tick_size: float = 0.01  # smallest price increment for orders
    size_step: float = 0.01  # orders are a whole number of these shares
    stats_known: bool = True  # liquidity / volume reported (Polymarket US listings don't)
    market_type: str = ""  # the venue's own market type, e.g. Polymarket US "props", "moneyline", "futures"
    category: str = ""  # Polymarket US: "politics", "sports", "crypto", "culture", ...

    @property
    def url(self) -> str:
        if self.venue == "us":  # polymarket.us has event pages only
            return f"https://polymarket.us/event/{self.event_slug or self.slug}"
        if self.event_slug:
            return f"https://polymarket.com/event/{self.event_slug}/{self.slug}"
        return f"https://polymarket.com/market/{self.slug}"

    @property
    def label(self) -> str:
        """Short name: the outcome label inside a grouped event, else the question."""
        return self.group_title or self.question

    def days_to_end(self, now: datetime) -> float | None:
        if self.end_date is None:
            return None
        return (self.end_date - now).total_seconds() / 86400.0


@dataclass(frozen=True, slots=True)
class Team:
    name: str
    abbreviation: str = ""
    home: bool = False


@dataclass(slots=True)
class Event:
    id: str
    slug: str
    title: str
    neg_risk: bool
    augmented: bool  # augmented neg-risk: outcomes can be added later
    markets: list[Market]  # open (not closed) markets only
    resolved_yes_groups: set[str] = field(default_factory=set)  # neg-risk ids already won by a closed market
    tags: tuple[str, ...] = ()
    live: bool = False  # a sports game in progress
    ended: bool = False  # the game is over (market may still await resolution)
    sport: str = ""  # Polymarket league code for games, e.g. "nfl", "epl"
    teams: tuple[Team, ...] = ()
    start_time: datetime | None = None  # scheduled game start
    venue: str = "international"

    def in_play(self, market: Market, now: datetime) -> bool:
        """True while a game is underway, when prices track the live score."""
        return self.live or (market.game_start is not None and market.game_start <= now and not self.ended)

    @property
    def url(self) -> str:
        site = "https://polymarket.us" if self.venue == "us" else "https://polymarket.com"
        return f"{site}/event/{self.slug}"


@dataclass(slots=True)
class Level:
    price: float
    size: float  # shares


@dataclass(slots=True)
class OrderBook:
    token_id: str
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    min_order_size: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


@dataclass(slots=True)
class Leg:
    """One order needed to enter an opportunity."""

    market: str
    outcome: str
    token_id: str
    shares: float
    avg_price: float
    limit_price: float  # worst price level the fill touches; use as the order's limit
    url: str
    market_slug: str = ""
    fee_rate: float = 0.0
    fee_exponent: float = 1.0
    min_order_size: float = 0.0
    version: str = "v1"
    seconds_delay: float = 0.0
    tick_size: float = 0.01
    size_step: float = 0.01

    @property
    def fees(self) -> FeeSchedule:
        return FeeSchedule(self.fee_rate, self.fee_exponent)

    @classmethod
    def for_market(cls, market: Market, side: int, *, shares: float, avg_price: float,
                   limit_price: float, label: str | None = None) -> Leg:
        outcome = market.outcomes[side] if len(market.outcomes) > side else ("Yes", "No")[side]
        return cls(
            market=label or market.label, outcome=outcome, token_id=market.token_ids[side],
            shares=shares, avg_price=avg_price, limit_price=limit_price, url=market.url,
            market_slug=market.slug, fee_rate=market.fees.rate, fee_exponent=market.fees.exponent,
            min_order_size=market.min_order_size, version=market.version,
            seconds_delay=market.seconds_delay, tick_size=market.tick_size, size_step=market.size_step,
        )


@dataclass(slots=True)
class Opportunity:
    strategy: str
    kind: str
    title: str
    subtitle: str
    url: str
    cost: float  # USDC to enter, including taker fees
    payout: float  # guaranteed (arb) or if-win (high_prob) USDC at resolution
    roi: float  # (payout - cost) / cost
    annualized: float | None  # simple annualized roi over the lock-up period
    days_to_end: float | None
    risk: str
    notes: list[str]
    legs: list[Leg]
    win_probability: float | None = None  # market-implied (or model) chance the position pays
    score: float = 0.0
    payout_per_share: float = 1.0  # guaranteed payout per basket unit (arbs) or per share if it wins
    # What one unit is worth when that differs from its payout, e.g. a model's fair
    # probability for a value bet. The trader never pays more than this per unit.
    value_per_share: float | None = None
    details: dict = field(default_factory=dict)  # strategy-specific extras for display

    @property
    def key(self) -> str:
        """Stable identity across scans: what is bought, not at what price."""
        return f"{self.strategy}|{self.kind}|" + ",".join(sorted(l.token_id for l in self.legs))

    @property
    def profit(self) -> float:
        return self.payout - self.cost

    def to_dict(self) -> dict:
        d = asdict(self)
        d["profit"] = self.profit
        d["key"] = self.key
        return d
