"""Automatic execution of scanner opportunities.

Safety model, in order:

1. Dry run unless started with ``--live``; live mode also needs a private key.
2. Only strategies listed in [trading].strategies are traded (default: neg-risk
   arbitrage only), within per-trade and per-day USD caps.
3. Right before trading, the order books are fetched again and the basket is
   re-sized. No leg may cross the price the scan evaluated, and the fresh basket
   must still clear min_profit_usd / min_roi after fees.
4. Every leg is signed first, then all legs are posted together as Fill-or-Kill
   market buys with a max price, so each leg fills completely or not at all.
5. If only some legs fill (the book moved between legs), trading halts until you
   run ``pmscan trade resume``. The filled legs stay in your wallet for you to
   sell or complete by hand.

Every attempt, live or dry run, is appended to data/trades.jsonl.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from . import env
from .books import cost_for_shares, walk_basket
from .client import PolymarketClient
from .config import TradingConfig
from .models import Level, Opportunity

log = logging.getLogger(__name__)

# Contract addresses from docs.polymarket.com/trading/wallets-auth (Neg Risk Adapter approvals).
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
BATCH_LIMIT = 15  # post_orders accepts 1-15 signed orders per call
COLLATERAL_DECIMALS = 6  # pUSD base units, as in the SDK


@dataclass
class LegOrder:
    market: str
    outcome: str
    token_id: str
    amount: Decimal  # pre-fee USD to spend
    max_price: Decimal  # never fill above this
    shares: float  # expected
    tick_size: float = 0.01  # price grid (Polymarket US markets can be 0.001)


@dataclass
class OrderResult:
    ok: bool
    status: str  # matched | delayed | live | rejected | error
    order_id: str | None = None
    message: str | None = None
    shares: float | None = None  # shares actually traded, when the exchange reports it

    @property
    def filled(self) -> bool:
        return self.ok and self.status == "matched"


@dataclass
class TradePlan:
    opportunity: Opportunity
    units: float
    legs: list[LegOrder]
    cost: float  # amounts + estimated taker fees
    value: float  # guaranteed payout (arbs) or expected value at the model's probability

    @property
    def edge(self) -> float:
        return self.value - self.cost


class Exchange(Protocol):
    def submit(self, orders: Sequence[LegOrder]) -> list[OrderResult]: ...

    def sell(self, token_id: str, shares: float, min_price: float) -> OrderResult: ...


class SdkExchange:
    """Live order placement through the official SDK (pip install 'pmscan[trade]')."""

    def __init__(self, client: Any) -> None:
        self.client = client

    @classmethod
    def from_env(cls) -> SdkExchange:
        try:
            from polymarket import RelayerApiKey, SecureClient
        except ImportError as exc:
            raise RuntimeError("Live trading needs the SDK: uv sync --extra trade") from exc
        key = env.get(env.PRIVATE_KEY)
        if not key:
            raise RuntimeError(f"Live trading needs {env.PRIVATE_KEY} (and usually {env.WALLET_ADDRESS}).")
        relayer_key, relayer_addr = env.get(env.RELAYER_API_KEY), env.get(env.RELAYER_API_KEY_ADDRESS)
        api_key = RelayerApiKey(key=relayer_key, address=relayer_addr) if relayer_key and relayer_addr else None
        return cls(SecureClient.create(private_key=key, wallet=env.get(env.WALLET_ADDRESS), api_key=api_key))

    def submit(self, orders: Sequence[LegOrder]) -> list[OrderResult]:
        # Sign every leg before posting any, so a signing error leaves nothing half-done.
        try:
            signed = [
                self.client.create_market_order(
                    token_id=o.token_id, side="BUY", amount=str(o.amount),
                    max_price=str(o.max_price), order_type="FOK",
                )
                for o in orders
            ]
        except Exception as exc:  # SDK input/metadata errors
            return [OrderResult(False, "error", message=f"signing failed: {exc}") for _ in orders]
        results: list[OrderResult] = []
        for i in range(0, len(signed), BATCH_LIMIT):
            try:
                responses = self.client.post_orders(signed[i : i + BATCH_LIMIT])
            except Exception as exc:
                results.extend(OrderResult(False, "error", message=str(exc)) for _ in signed[i : i + BATCH_LIMIT])
                continue
            for r in responses:
                ok = bool(getattr(r, "ok", False))
                results.append(OrderResult(
                    ok=ok,
                    status=str(getattr(r, "status", "rejected") if ok else "rejected"),
                    order_id=getattr(r, "order_id", None),
                    message=getattr(r, "message", None),
                ))
        return results

    def sell(self, token_id: str, shares: float, min_price: float) -> OrderResult:
        """Fill-and-kill market sell with a price floor (used to unwind a partial basket)."""
        try:
            signed = self.client.create_market_order(token_id=token_id, side="SELL", shares=f"{shares:.2f}",
                                                     min_price=f"{min_price:.2f}", order_type="FAK")
            r = self.client.post_order(signed)
        except Exception as exc:
            return OrderResult(False, "error", message=str(exc))
        ok = bool(getattr(r, "ok", False))
        try:
            traded = float(getattr(r, "making_amount", None))  # a SELL gives shares
        except (TypeError, ValueError):
            traded = None
        return OrderResult(ok, str(getattr(r, "status", "rejected") if ok else "rejected"),
                           getattr(r, "order_id", None), getattr(r, "message", None), traded)

    def account_summary(self) -> dict[str, Any]:
        balance = self.client.get_balance_allowance(asset_type="COLLATERAL")
        raw = int(getattr(balance, "balance", 0) or 0)
        return {"wallet": str(self.client.wallet), "wallet_type": str(self.client.wallet_type),
                "collateral_balance": f"${raw / 10**COLLATERAL_DECIMALS:,.2f} pUSD",
                "collateral_usd": raw / 10**COLLATERAL_DECIMALS}

    def setup_approvals(self) -> list[str]:
        """Exchange approvals plus the Neg Risk Adapter approvals neg-risk trading needs."""
        done = []
        for label, call in [
            ("exchange approvals", lambda: self.client.setup_trading_approvals()),
            ("pUSD -> Neg Risk Adapter", lambda: self.client.approve_erc20(
                token_address=PUSD, spender_address=NEG_RISK_ADAPTER, amount="max")),
            ("outcome tokens -> Neg Risk Adapter", lambda: self.client.approve_erc1155_for_all(
                token_address=CONDITIONAL_TOKENS, operator_address=NEG_RISK_ADAPTER, approved=True)),
        ]:
            handle = call()
            wait = getattr(handle, "wait", None)
            if callable(wait):
                wait()
            done.append(label)
        return done


def owner_client_from_env() -> Any:
    """The account owner's client, for one-off wallet actions (session keys, approvals)."""
    try:
        from polymarket import RelayerApiKey, SecureClient
    except ImportError as exc:
        raise RuntimeError("This needs the SDK: uv sync --extra trade") from exc
    key = env.get(env.OWNER_PRIVATE_KEY)
    wallet = env.get(env.WALLET_ADDRESS)
    relayer_key, relayer_addr = env.get(env.RELAYER_API_KEY), env.get(env.RELAYER_API_KEY_ADDRESS)
    missing = [n for n, v in [(env.OWNER_PRIVATE_KEY, key), (env.WALLET_ADDRESS, wallet),
                              (env.RELAYER_API_KEY, relayer_key), (env.RELAYER_API_KEY_ADDRESS, relayer_addr)] if not v]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)}.")
    validate_credentials(wallet, key)
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", relayer_addr.strip()):
        raise ValueError("The relayer key address should start with 0x followed by 40 letters and numbers: it's the "
                         "Signer Address shown next to the key under polymarket.com › Settings › API Keys.")
    return SecureClient.create(private_key=key, wallet=wallet,
                               api_key=RelayerApiKey(key=relayer_key, address=relayer_addr))


WALLET_TYPES = {
    "DEPOSIT_WALLET": "Deposit Wallet",
    "POLY_PROXY": "Proxy Wallet (older email or Google account)",
    "GNOSIS_SAFE": "Safe Wallet (older crypto-wallet account)",
    "EOA": "plain wallet (EOA)",
}


def validate_credentials(wallet: str, key: str) -> None:
    """Catch the common copy-paste mistakes before talking to Polymarket."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet.strip()):
        raise ValueError("That wallet address doesn't look right. It should start with 0x followed by 40 "
                         "letters and numbers: copy it from the profile menu on polymarket.com.")
    if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", key.strip()):
        raise ValueError("That private key doesn't look right. It should be 64 letters and numbers (0-9, a-f), "
                         "optionally starting with 0x. Copy it again from reveal.magic.link/polymarket.")


def check_account(wallet: str, key: str) -> dict[str, Any]:
    """Connect with the given key and report what kind of account it is."""
    validate_credentials(wallet, key)
    try:
        from eth_account import Account
        from polymarket import SecureClient
    except ImportError as exc:
        raise RuntimeError("This needs the SDK: uv sync --extra trade") from exc
    signer = Account.from_key(key.strip()).address
    try:
        client = SecureClient.create(private_key=key.strip(), wallet=wallet.strip())
    except Exception as exc:
        raise RuntimeError(f"Polymarket didn't accept that wallet and key together ({exc}). Check that the key "
                           "belongs to this account: export it while signed in with the same email or Google "
                           "login.") from exc
    summary = SdkExchange(client).account_summary()
    return {**summary, "signer": signer,
            "wallet_type_label": WALLET_TYPES.get(summary["wallet_type"], summary["wallet_type"]),
            "session_keys_supported": summary["wallet_type"] == "DEPOSIT_WALLET"}


def authorize_session_key(owner: Any, setup_approvals: bool = True) -> dict[str, Any]:
    """Create a fresh signer and authorize it to trade (CLOB only) for the owner's Deposit Wallet.

    A session key can place orders but cannot withdraw funds, expires after 180 days and can
    be revoked, so it's what the bot should hold instead of the owner's key.
    """
    from eth_account import Account
    from polymarket import SessionKeyKnownScope

    if str(owner.wallet_type) != "DEPOSIT_WALLET":
        raise RuntimeError(f"Session keys need a Polymarket Deposit Wallet; this account's wallet is "
                           f"{owner.wallet_type}. Use the account's own key as {env.PRIVATE_KEY} instead.")
    approvals = SdkExchange(owner).setup_approvals() if setup_approvals else []
    signer = Account.create()
    result = owner.authorize_session_key(address=signer.address, scopes=(SessionKeyKnownScope.CLOB,))
    wait = getattr(getattr(result, "transaction", None), "wait", None)
    if callable(wait):
        wait()
    session = getattr(result, "session_key", None)
    return {
        "session_private_key": "0x" + signer.key.hex().removeprefix("0x"),
        "session_address": signer.address,
        "wallet": str(owner.wallet),
        "wallet_type": str(owner.wallet_type),
        "valid_until": str(getattr(session, "valid_until", "")),
        "approvals": approvals,
    }


class Ledger:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "trades.jsonl"
        self.halt_path = data_dir / "TRADING_HALTED"
        data_dir.mkdir(parents=True, exist_ok=True)

    def record(self, entry: dict[str, Any]) -> None:
        with open(self.path, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def spent_today(self, now: datetime) -> float:
        day = now.date().isoformat()
        return sum(e.get("spent", 0.0) for e in self.entries() if e.get("mode") == "live" and e.get("ts", "")[:10] == day)

    def last_attempt(self, key: str) -> datetime | None:
        times = [e["ts"] for e in self.entries() if e.get("key") == key and "ts" in e]
        return datetime.fromisoformat(max(times)) if times else None

    def halted(self) -> str | None:
        return self.halt_path.read_text().strip() if self.halt_path.exists() else None

    def halt(self, reason: str) -> None:
        self.halt_path.write_text(reason + "\n")

    def resume(self) -> bool:
        if self.halt_path.exists():
            self.halt_path.unlink()
            return True
        return False


class Skip(Exception):
    """An opportunity that won't be traded, with the reason."""


def _cents_up(x: float) -> Decimal:
    # round() first so float noise (26.75 * 0.4 = 10.700000000000001) doesn't add a cent
    return Decimal(str(round(x, 9))).quantize(Decimal("0.01"), rounding=ROUND_UP)


class AutoTrader:
    def __init__(
        self,
        config: TradingConfig,
        client: PolymarketClient,
        ledger: Ledger,
        exchange: Exchange | None = None,
        notify: Any = None,  # callable(title, lines, url) for alerts
    ) -> None:
        self.config = config
        self.client = client
        self.ledger = ledger
        self.exchange = exchange
        self.notify = notify
        self.skipped: list[tuple[Opportunity, str]] = []  # from the latest process() call

    @property
    def live(self) -> bool:
        return self.exchange is not None

    def ineligible(self, opp: Opportunity, now: datetime) -> str | None:
        cfg = self.config
        if opp.strategy not in cfg.strategies:
            return "strategy not enabled for trading"
        if len(opp.legs) > cfg.max_legs:
            return f"{len(opp.legs)} legs > max_legs"
        if any(l.version != "v1" for l in opp.legs):
            return "Protocol V2 market (not supported yet)"
        if not cfg.allow_delayed_markets and any(l.seconds_delay > 0 for l in opp.legs):
            return "market has a matching delay"
        last = self.ledger.last_attempt(opp.key)
        if last and now - last < timedelta(minutes=cfg.retrade_cooldown_minutes):
            return "traded recently"
        return None

    def plan(self, opp: Opportunity, now: datetime) -> TradePlan | None:
        try:
            return self._plan(opp, now)
        except Skip:
            return None

    def _plan(self, opp: Opportunity, now: datetime) -> TradePlan:
        """Re-size the opportunity against fresh order books, within budget. Raises Skip."""
        cfg = self.config
        budget = min(cfg.max_usd_per_trade, cfg.max_usd_per_day - self.ledger.spent_today(now))
        if budget < cfg.min_leg_usd * len(opp.legs):
            raise Skip(f"budget left today (${max(budget, 0):,.2f}) is below ${cfg.min_leg_usd:,.2f} per leg")
        books = self.client.get_books([l.token_id for l in opp.legs])
        ladders: list[list[Level]] = []
        for leg in opp.legs:
            book = books.get(leg.token_id)
            # Never pay more than the scan evaluated, even if the book has moved.
            ladder = [lv for lv in (book.asks if book else []) if lv.price <= leg.limit_price + 1e-9]
            if not ladder:
                raise Skip(f"no asks left at or below {leg.limit_price} for {leg.market!r}")
            ladders.append(ladder)

        worth = opp.value_per_share or opp.payout_per_share
        fees = [l.fees for l in opp.legs]
        fill = walk_basket(ladders, fees, max_unit_cost=worth / (1.0 + cfg.min_roi), budget=budget)
        if fill.units <= 0:
            raise Skip("no longer profitable on the fresh order books")
        # Leave headroom so rounding each leg's USD amount up to the cent can't run
        # past the depth we measured (one cent buys 1/price shares).
        headroom = max(0.01 * fill.units, 1.0 + max(0.01 / lp for lp in fill.limit_prices if lp > 0))
        step = max(l.size_step for l in opp.legs) or 0.01  # e.g. whole contracts on some US markets
        units = math.floor(round((fill.units - headroom) / step, 9)) * step
        units = round(units, 2)
        min_shares = max(l.min_order_size for l in opp.legs)
        if units <= 0 or units < min_shares:
            raise Skip(f"only {max(units, 0):,.2f} shares fit the budget and depth (minimum {min_shares:g})")

        legs, cost = [], 0.0
        for leg, ladder, limit in zip(opp.legs, ladders, fill.limit_prices, strict=True):
            priced = cost_for_shares(ladder, units, leg.fees)
            if priced is None:
                raise Skip(f"not enough depth for {leg.market!r}")
            notional, fee = priced
            amount = _cents_up(notional)
            if amount < Decimal(str(cfg.min_leg_usd)):
                raise Skip(f"the {leg.market!r} leg would be ${amount}, under the ${cfg.min_leg_usd:,.2f} "
                           f"minimum order (budget ${budget:,.0f})")
            legs.append(LegOrder(leg.market, leg.outcome, leg.token_id, amount, Decimal(str(limit)), units,
                                 leg.tick_size))
            cost += float(amount) + fee
        plan = TradePlan(opp, units, legs, cost, units * worth)
        if plan.edge < cfg.min_profit_usd or plan.edge / plan.cost < cfg.min_roi:
            raise Skip(f"edge ${plan.edge:,.2f} ({plan.edge / plan.cost * 100:.2f}%) after resizing is below "
                       f"the ${cfg.min_profit_usd:,.2f} / {cfg.min_roi * 100:.2f}% minimum")
        return plan

    def _entry(self, plan: TradePlan, now: datetime, mode: str) -> dict[str, Any]:
        o = plan.opportunity
        return {
            "ts": now.isoformat(), "mode": mode, "strategy": o.strategy, "kind": o.kind, "key": o.key,
            "title": o.title, "url": o.url, "units": plan.units, "cost": round(plan.cost, 4),
            "value": round(plan.value, 4), "edge": round(plan.edge, 4),
            "legs": [{**asdict(l), "amount": str(l.amount), "max_price": str(l.max_price)} for l in plan.legs],
            "spent": 0.0,
        }

    def execute(self, plan: TradePlan, now: datetime) -> dict[str, Any]:
        entry = self._entry(plan, now, "live" if self.live else "dry_run")
        if not self.live:
            entry["outcome"] = "dry_run"
            self.ledger.record(entry)
            return entry

        results = self.exchange.submit(plan.legs)  # type: ignore[union-attr]
        if len(results) != len(plan.legs):  # can't tell which legs filled: assume the worst
            results = results[: len(plan.legs)] + [OrderResult(True, "unknown")] * (len(plan.legs) - len(results))
        entry["results"] = [asdict(r) for r in results]
        filled = [r.filled for r in results]
        pending = [r.ok and r.status in ("delayed", "live", "unknown") for r in results]
        if results and all(filled):
            entry["outcome"] = "filled"
            entry["spent"] = round(plan.cost, 4)
        elif not any(filled) and not any(pending):
            entry["outcome"] = "not_filled"
        else:
            entry["outcome"] = "partial"
            entry["spent"] = round(sum(float(l.amount) for l, r in zip(plan.legs, results, strict=True) if r.filled or r.ok), 4)
            problem = f"{sum(filled)}/{len(results)} legs matched, {sum(pending)} pending"
            if self.config.unwind_partial_fills and not any(pending):
                unwound, detail = self._unwind(plan, results)
                entry["unwinds"] = detail
                if unwound:
                    entry["outcome"] = "partial_unwound"
                else:
                    problem += "; selling the filled legs back didn't fully work"
            if entry["outcome"] == "partial":
                self.ledger.halt(f"{now.isoformat()} partial fill on {plan.opportunity.title!r}: {problem}. "
                                 "Check your positions, then run `pmscan trade resume`.")
        self.ledger.record(entry)
        self._notify(entry)
        return entry

    def _unwind(self, plan: TradePlan, results: list[OrderResult]) -> tuple[bool, list[dict[str, Any]]]:
        """Sell back every leg that filled, so a half-built basket doesn't stay open.

        Each sell is fill-and-kill with a floor of `unwind_max_loss_pct` below what the leg cost.
        Returns whether every filled leg was fully sold.
        """
        detail, ok = [], True
        for leg, r in zip(plan.legs, results, strict=True):
            if not r.filled:
                continue
            shares = math.floor(leg.shares * 100) / 100  # we bought at least this many
            paid = float(leg.amount) / leg.shares
            floor = max(0.01, math.floor(paid * (1 - self.config.unwind_max_loss_pct) * 100) / 100)
            sold = self.exchange.sell(leg.token_id, shares, floor)  # type: ignore[union-attr]
            complete = sold.filled and (sold.shares is None or sold.shares >= 0.99 * shares)
            ok = ok and complete
            detail.append({"market": leg.market, "token_id": leg.token_id, "shares": shares, "min_price": floor,
                           **asdict(sold), "complete": complete})
        return ok, detail

    def _notify(self, entry: dict[str, Any]) -> None:
        if not self.notify:
            return
        outcome = entry["outcome"]
        if outcome == "filled":
            title = f"Trade filled: {entry['title']}"
        elif outcome == "partial":
            title = f"PARTIAL FILL, trading halted: {entry['title']}"
        elif outcome == "partial_unwound":
            title = f"Partial fill sold back, trading continues: {entry['title']}"
        else:
            title = f"Trade not filled: {entry['title']}"
        self.notify(title, [f"Cost ${entry['cost']:,.2f} for ${entry['value']:,.2f} ({entry['strategy']})"], entry["url"])

    def process(self, opportunities: dict[str, list[Opportunity]], now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        if self.ledger.halted():
            log.warning("Trading halted: %s", self.ledger.halted())
            return []
        done = []
        self.skipped = []
        for name in self.config.strategies:
            for opp in opportunities.get(name, []):
                reason = self.ineligible(opp, now)
                if reason:
                    self.skipped.append((opp, reason))
                    continue
                try:
                    plan = self._plan(opp, now)
                except Skip as skip:
                    self.skipped.append((opp, str(skip)))
                    continue
                done.append(self.execute(plan, now))
                if self.ledger.halted():
                    return done
        return done
