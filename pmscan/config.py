"""Scanner settings, with optional overrides from a TOML file (see pmscan.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class NegRiskArbConfig:
    enabled: bool = True
    min_roi: float = 0.003  # require >= 0.3% return after fees on every basket bought
    min_profit_usd: float = 1.0  # ignore opportunities smaller than this
    max_capital_usd: float = 10_000.0  # stop walking the books past this spend
    screen_slack: float = 0.02  # how close top-of-book must be to an edge to fetch books
    allow_augmented_buy_yes: bool = False  # see README: outcome set may be incomplete


@dataclass
class HighProbConfig:
    enabled: bool = True
    min_price: float = 0.90  # only outcomes the market already prices as >= 90% likely
    max_price: float = 0.995
    min_roi: float = 0.005  # >= 0.5% return if it wins, after fees
    max_days: float = 30.0  # resolves within this many days
    max_days_past_end: float = 7.0  # include markets past their end date awaiting resolution
    min_liquidity: float = 1_000.0
    min_volume_24h: float = 500.0
    min_depth_usd: float = 50.0  # must be able to deploy at least this much at >= min_roi
    max_position_usd: float = 5_000.0
    max_candidates: int = 1_500  # order books verified per scan
    max_candidates_us: int = 300  # on Polymarket US, which serves one book per request
    # Skip books wider than this (ask - bid). In the backtest, 90c+ politics and futures bought with a
    # spread of 2c or less won 98.7-99.4% (+1.3-1.9% per dollar at 1-21 days out); above 5c the ask
    # was a stale leftover order, not a price: they won 59-91% and lost 7-40% per dollar.
    max_spread: float = 0.02
    # Polymarket US market types to skip. The backtest (pmscan backtest) found 90c+ player props
    # and game-winner markets lost money after fees: they won 92-95% of the time but needed
    # ~95-97%. Politics, futures and weather held up. Set to [] to buy everything.
    exclude_market_types_us: list[str] = field(default_factory=lambda: ["props", "moneyline"])
    # Polymarket US: buy only these. "politics" is a category; "sports:futures" is a category and market
    # type (season futures; every non-game market there is typed "futures"). [] = every category.
    include_us: list[str] = field(default_factory=lambda: ["politics", "sports:futures"])
    # In-play sports prices track the live score and move faster than a scan cycle.
    include_live_games: bool = False
    exclude_tags: list[str] = field(default_factory=list)  # e.g. ["Sports", "Crypto"]
    exclude_keywords: list[str] = field(default_factory=list)  # skip slugs containing any of these


@dataclass
class NearCertainConfig(HighProbConfig):
    """Near-certain bets: the 97-99.5c side of things that almost never happen, close to the end.

    The backtest (10,000 settled Polymarket US markets) found these paid only in politics bought
    1 hour to 3 days before the end: 819 of 823 won, +0.8% per dollar after fees. Weather, culture,
    economic data and season futures were break-even or worse at these prices, player props and
    game winners clearly worse, and a week or more out even politics stopped paying. Almost all of
    the politics record is the November 2026 midterms, so it is one night more than 823 tests.
    """

    min_price: float = 0.97
    max_price: float = 0.995
    min_roi: float = 0.003  # a 99c contract returns about 0.9% after fees
    max_days: float = 3.0
    max_days_past_end: float = 1.0
    include_us: list[str] = field(default_factory=lambda: ["politics"])


@dataclass
class LadderArbConfig:
    enabled: bool = True
    min_roi: float = 0.003
    min_profit_usd: float = 1.0
    max_capital_usd: float = 10_000.0
    screen_slack: float = 0.01
    max_pairs_per_ladder: int = 3  # pairs in one ladder share liquidity; report the best few


@dataclass
class SportsbookConfig:
    enabled: bool = True  # active only when PMSCAN_ODDS_API_KEY is set
    # Polymarket league codes to compare (see pmscan/odds.py for the Odds API mapping).
    sports: list[str] = field(default_factory=lambda: ["nfl", "nba", "nhl", "cfb", "cbb", "epl", "ucl"])
    sport_keys: dict[str, str] = field(default_factory=dict)  # extra/override code -> Odds API key
    regions: str = "us"  # each region costs one Odds API credit per sport per refresh
    bookmakers: list[str] = field(default_factory=list)  # restrict fair value to these books
    min_books: int = 3
    min_edge: float = 0.03  # expected value per $1 staked, after fees
    min_fair_prob: float = 0.10  # skip longshots, where bookmaker prices are least reliable
    max_hours_ahead: float = 48.0
    max_start_diff_minutes: float = 90.0  # kickoff times must agree this closely to match
    refresh_minutes: float = 60.0  # odds are cached this long to save API credits
    max_position_usd: float = 1_000.0
    min_depth_usd: float = 20.0


@dataclass
class AlertsConfig:
    enabled: bool = True  # sends only when a channel is configured (see README)
    strategies: list[str] = field(default_factory=lambda: ["neg_risk_arb", "ladder_arb", "sportsbook_value"])
    min_profit_usd: float = 2.0  # for arbitrage
    min_edge: float = 0.05  # for sportsbook value (expected value per $1)
    cooldown_minutes: float = 120.0  # don't repeat the same opportunity within this window
    max_per_scan: int = 5


@dataclass
class TradingConfig:
    # Only these strategies are executed automatically. Arbitrage has a fixed payout;
    # adding high_prob or sportsbook_value means the bot takes real directional risk.
    strategies: list[str] = field(default_factory=lambda: ["neg_risk_arb"])
    max_usd_per_trade: float = 25.0
    max_usd_per_day: float = 100.0
    min_profit_usd: float = 0.50  # after re-checking fresh order books
    min_roi: float = 0.005
    min_leg_usd: float = 1.0  # skip baskets with a leg too small to place reliably
    max_legs: int = 15  # one batch request; all legs are submitted together
    retrade_cooldown_minutes: float = 10.0
    allow_delayed_markets: bool = False  # markets with a matching delay can't fill atomically
    # If only some legs of a basket fill, sell the filled legs straight back (fill-and-kill, no lower
    # than this much below cost). Trading halts only if that doesn't fully work.
    unwind_partial_fills: bool = True
    unwind_max_loss_pct: float = 0.10


@dataclass
class PaperConfig:
    """Simulated trading: see pmscan/paper.py."""

    enabled: bool = True
    # One simulated portfolio per strategy, each with its own starting balance.
    strategies: list[str] = field(default_factory=lambda: ["high_prob", "near_certain", "neg_risk_arb", "ladder_arb",
                                                           "sportsbook_value"])
    min_price: float = 0.90  # high_prob: enter every listed position priced at least this...
    max_price: float = 0.995  # ...and at most this (above it the return isn't worth the risk)
    stake_usd: float = 50.0  # per single-position bet (high_prob, sportsbook_value)
    near_certain_min_price: float = 0.97  # near_certain: enter every listed position priced at least this...
    near_certain_stake_usd: float = 200.0  # ...with this much (each bet returns only 0.5-3%)
    basket_stake_usd: float = 200.0  # most to put into one arbitrage basket
    min_edge_usd: float = 0.05  # baskets/value bets: skip if the re-checked edge is below this
    min_roi: float = 0.002
    starting_balance: float = 10_000.0  # per strategy
    max_open_positions: int = 500  # per strategy
    # high_prob / near_certain: most bets held in one event at once (0 = no limit). Markets in one
    # event often go wrong together (CFP rankings, conference qualifiers), so one surprise can't sink many bets.
    max_bets_per_event: int = 3
    # high_prob / near_certain: most of the starting balance held in one league (Polymarket US
    # sports: college football, NFL, NBA, a golf tour, ...; other markets: their category) at once
    # (0 = no limit). Events in one league ride on the same games, which the per-event cap can't see.
    max_league_share: float = 0.20


@dataclass
class FastConfig:
    """Live order-book feed for the events closest to an arbitrage (see pmscan/fast.py)."""

    enabled: bool = True
    watch_events: int = 40
    debounce_seconds: float = 0.5


@dataclass
class RewardsConfig:
    """Polymarket US liquidity rewards: scan and paper-test resting quotes (see pmscan/rewards.py)."""

    enabled: bool = True  # active only on Polymarket US with an API key (the program list needs it)
    budget_usd: float = 50.0  # collateral behind each quote, which is also the most a fill can lose
    max_quotes: int = 5  # paper quotes held at once, at most one per program
    horizon_hours: float = 24.0  # the scan includes periods starting within this long
    max_books: int = 400  # order books read per scan
    per_program: int = 25  # candidates from one program per scan (its markets are near-copies)
    max_ticks_behind: int = 3  # also try quotes this many ticks behind the best price
    min_per_hour: float = 0.05  # paper: only quote where the estimate is at least this
    paper_live: bool = False  # paper: also quote during live games (prices jump; fills get picked off)
    # paper: skip programs whose name contains any of these. Markets that track a fast-moving price
    # (crypto, metals, fuel) filled within minutes and lost far more than they earned in the first test.
    paper_skip_programs: list[str] = field(default_factory=lambda: ["crypto", "metals", "usgas", "gas_state", "diesel"])
    tick_seconds: float = 60.0  # paper: re-check the quotes' books this often
    rescan_minutes: float = 15.0  # paper: look for new quotes this often


@dataclass
class ScanConfig:
    resolution_lag_days: float = 1.0  # added to time-to-end: UMA settlement isn't instant
    top_n: int = 25
    neg_risk_arb: NegRiskArbConfig = field(default_factory=NegRiskArbConfig)
    ladder_arb: LadderArbConfig = field(default_factory=LadderArbConfig)
    high_prob: HighProbConfig = field(default_factory=HighProbConfig)
    near_certain: NearCertainConfig = field(default_factory=NearCertainConfig)
    sportsbook_value: SportsbookConfig = field(default_factory=SportsbookConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    fast: FastConfig = field(default_factory=FastConfig)
    rewards: RewardsConfig = field(default_factory=RewardsConfig)


def _apply(obj: Any, overrides: dict[str, Any], path: str = "") -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in overrides.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config section {path}{key} must be a table")
            _apply(current, value, f"{path}{key}.")
        elif isinstance(current, bool) or not isinstance(current, (int, float)):
            setattr(obj, key, value)
        else:
            setattr(obj, key, type(current)(value))  # allow `max_days = 30` for a float field


def load_config(path: str | Path | None = None) -> ScanConfig:
    """Defaults, overridden by ``path`` or by ./pmscan.toml when it exists."""
    config = ScanConfig()
    candidate = Path(path) if path else Path("pmscan.toml")
    if path or candidate.exists():
        with open(candidate, "rb") as fh:
            _apply(config, tomllib.load(fh))
    return config
