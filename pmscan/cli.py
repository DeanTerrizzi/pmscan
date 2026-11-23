"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

from . import env
from .alerts import Alert, AlertManager, notifiers_from_env
from .client import PolymarketClient
from .config import ScanConfig, load_config
from .paper import PaperTrader
from .report import SORT_KEYS, print_result
from .scanner import Scanner, ScanResult
from .strategies import ALL_STRATEGIES
from .trading import (
    AutoTrader,
    Ledger,
    SdkExchange,
    authorize_session_key,
    check_account,
    owner_client_from_env,
)

console = Console()
PostScan = Callable[[ScanResult], list[str]]


def _run_scan(scanner: Scanner, data_dir: Path | None) -> ScanResult:
    with console.status(f"Scanning {scanner.client.label}…") as status:
        result = scanner.scan(progress=lambda msg: status.update(msg))
    if data_dir is not None:
        result.save(data_dir)
    return result


def _client(args: argparse.Namespace) -> PolymarketClient:
    if args.venue == "us":
        from .us import UsClient

        return UsClient()
    return PolymarketClient()


def _exchange(args: argparse.Namespace) -> Any:
    if args.venue == "us":
        from .us import UsExchange

        return UsExchange.from_env()
    return SdkExchange.from_env()


def _build(args: argparse.Namespace) -> tuple[ScanConfig, PolymarketClient, Scanner, int]:
    config = load_config(args.config)
    strategies = args.strategy.split(",") if args.strategy else None
    client = _client(args)
    scanner = Scanner(client, config, strategies)
    return config, client, scanner, args.top or config.top_n


def _alert_manager(config: ScanConfig, args: argparse.Namespace) -> AlertManager | None:
    if getattr(args, "no_alerts", False):
        return None
    manager = AlertManager(config.alerts, notifiers_from_env())
    return manager if manager.active else None


def _trader(config: ScanConfig, client: PolymarketClient, args: argparse.Namespace,
            alerts: AlertManager | None) -> AutoTrader | None:
    if not getattr(args, "trade", False):
        if getattr(args, "live", False):
            raise ValueError("--live only applies together with --trade")
        return None
    exchange = _exchange(args) if args.live else None
    notify = (lambda title, lines, url: alerts.send(Alert(title, lines, url))) if alerts else None
    trader = AutoTrader(config.trading, client, Ledger(Path(args.data_dir)), exchange, notify)
    t = config.trading
    if trader.live:
        acct = exchange.account_summary()  # type: ignore[union-attr]
        console.print(
            f"[bold red]LIVE TRADING[/bold red] {acct['wallet']} ({acct['wallet_type']}), "
            f"balance {acct['collateral_balance']} · strategies {', '.join(t.strategies)} · "
            f"max ${t.max_usd_per_trade:,.0f}/trade, ${t.max_usd_per_day:,.0f}/day"
        )
    else:
        console.print(f"[yellow]Dry-run trading[/yellow] (add --live to place orders) · strategies "
                      f"{', '.join(t.strategies)} · max ${t.max_usd_per_trade:,.0f}/trade")
    return trader


def _paper(config: ScanConfig, client: PolymarketClient, args: argparse.Namespace) -> PaperTrader | None:
    if getattr(args, "no_paper", False) or not config.paper.enabled:
        return None
    paper = PaperTrader(config.paper, client, Path(args.data_dir))
    s = paper.summary()
    console.print(f"[cyan]Paper trading[/cyan] {len(paper.books)} strategies "
                  f"({', '.join(b['title'] for b in s['books'].values())}) · equity ${s['equity']:,.2f} · "
                  f"{s['open_positions']} open positions")
    return paper


# Scans and the live feed both act on what they find; never at the same time.
_ACTIONS = threading.Lock()


def _post_scan(alerts: AlertManager | None, trader: AutoTrader | None, paper: PaperTrader | None = None) -> PostScan:
    def run(result: ScanResult) -> list[str]:
        with _ACTIONS:
            return _act(result)

    def _act(result: ScanResult) -> list[str]:
        messages = []
        if trader is not None:
            for e in trader.process(result.opportunities):
                what = "dry run, would buy" if e["mode"] == "dry_run" else e["outcome"].replace("_", " ")
                messages.append(f"{what}: {e['title']} · cost ${e['cost']:,.2f} · edge ${e['edge']:,.2f}")
            skipped = dict.fromkeys(f"skipped {o.title}: {r}" for o, r in trader.skipped if r != "traded recently")
            messages.extend(skipped)
            halted = trader.ledger.halted()
            if halted:
                messages.append(f"TRADING HALTED: {halted}")
        if paper is not None:
            try:
                messages.extend(paper.process(result))
            except Exception as exc:  # a paper-trading hiccup must never stop the scanner
                messages.append(f"paper trading failed this scan: {exc}")
        if alerts is not None:
            sent = alerts.process(result)
            if sent:
                messages.append(f"Sent {len(sent)} alert{'s' if len(sent) != 1 else ''}")
        return messages
    return run


def _on_found(alerts: AlertManager | None, trader: AutoTrader | None, paper: PaperTrader | None) -> PostScan:
    """What to do when the live feed finds an opportunity between scans."""
    def run(result: ScanResult) -> list[str]:
        messages: list[str] = []
        with _ACTIONS:
            if trader is not None:
                for e in trader.process(result.opportunities):
                    what = "dry run, would buy" if e["mode"] == "dry_run" else e["outcome"].replace("_", " ")
                    messages.append(f"{what}: {e['title']} · cost ${e['cost']:,.2f} · edge ${e['edge']:,.2f}")
            if paper is not None:
                try:
                    messages.extend(paper.enter_only(result))
                except Exception as exc:
                    messages.append(f"paper trading failed: {exc}")
            if alerts is not None:
                alerts.process(result)
        return messages
    return run


def cmd_scan(args: argparse.Namespace) -> int:
    _, client, scanner, top_n = _build(args)
    with client:
        result = _run_scan(scanner, None if args.no_save else Path(args.data_dir))
    print_result(result, console, top_n=top_n, sort=args.sort)
    if args.json:
        Path(args.json).write_text(json.dumps(result.to_dict(), indent=1))
        console.print(f"[dim]Wrote {args.json}[/dim]")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    config, client, scanner, top_n = _build(args)
    alerts = _alert_manager(config, args)
    post = _post_scan(alerts, _trader(config, client, args, alerts), _paper(config, client, args))
    with client:
        while True:
            started = time.monotonic()
            try:
                result = _run_scan(scanner, None if args.no_save else Path(args.data_dir))
                messages = post(result)
            except Exception as exc:  # keep watching through transient API failures
                console.print(f"[red]Scan failed: {exc}[/red]")
            else:
                console.clear()
                print_result(result, console, top_n=top_n, sort=args.sort)
                if any(result.opportunities.get(n) for n in ("neg_risk_arb", "ladder_arb")):
                    console.bell()
                for m in messages:
                    console.print(f"[bold]{m}[/bold]")
            wait = max(0.0, args.interval - (time.monotonic() - started))
            console.print(f"[dim]Next scan in {wait:.0f}s (Ctrl-C to stop)[/dim]")
            time.sleep(wait)


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    config, client, scanner, _ = _build(args)
    alerts = _alert_manager(config, args)
    trader = _trader(config, client, args, alerts)
    paper = _paper(config, client, args)
    fast = None
    us_without_key = args.venue == "us" and not (env.get(env.US_KEY_ID) and env.get(env.US_SECRET_KEY))
    if config.fast.enabled and not args.no_fast and us_without_key:
        console.print("[dim]Live feed: Polymarket US streams prices only to connected accounts; add your API "
                      f"key to turn it on. Scanning every {args.interval:.0f}s.[/dim]")
    elif config.fast.enabled and not args.no_fast:
        from .fast import FastWatcher

        fast = FastWatcher(scanner, on_found=lambda r: None, max_events=config.fast.watch_events,
                           debounce_s=config.fast.debounce_seconds)
        console.print(f"[cyan]Live feed[/cyan] on for the {config.fast.watch_events} events closest to an arbitrage")
    if args.open:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [f"http://{args.host}:{args.port}/"]).start()
    if args.exit_with_parent:
        _exit_when_parent_dies()
    with client:
        serve(scanner, host=args.host, port=args.port, interval=args.interval, data_dir=Path(args.data_dir),
              on_result=_post_scan(alerts, trader, paper), trader=trader, config=config,
              fast=fast, on_found=_on_found(alerts, trader, paper))
    return 0


def _exit_when_parent_dies() -> None:
    """For the Mac app: if the app that launched us dies, don't linger as an orphan."""
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def cmd_alerts_test(args: argparse.Namespace) -> int:
    notifiers = notifiers_from_env()
    if not notifiers:
        console.print(f"[red]No alert channel configured.[/red] Set {env.TELEGRAM_BOT_TOKEN} + "
                      f"{env.TELEGRAM_CHAT_ID}, or {env.DISCORD_WEBHOOK_URL} (in the environment or .env).")
        return 1
    manager = AlertManager(load_config(args.config).alerts, notifiers)
    ok = manager.send(Alert("pmscan test alert", ["Alerts are working."], "https://polymarket.com"))
    names = ", ".join(n.name for n in notifiers)
    console.print(f"[green]Sent to {names}[/green]" if ok else f"[red]Sending to {names} failed (see log)[/red]")
    return 0 if ok else 1


def cmd_trade(args: argparse.Namespace) -> int:
    if not getattr(args, "json", False):
        return _cmd_trade(args)
    # For the Mac app: always answer with one JSON line, never a traceback.
    try:
        return _cmd_trade(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__}))
        return 1


def _cmd_trade(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.data_dir))
    if args.action == "check-account" and args.venue == "us":
        from .us import check_keys

        result = check_keys(env.get(env.US_KEY_ID) or "", env.get(env.US_SECRET_KEY) or "")
        if args.json:
            print(json.dumps(result))
        else:
            console.print(f"{result['wallet']}: {result['collateral_balance']} (cash ${result['cash_usd']:,.2f})")
        return 0
    if args.action == "test-order":
        return _test_order(args, ledger)
    if args.venue == "us" and args.action in ("setup", "authorize-session", "revoke-session"):
        raise ValueError(f"`trade {args.action}` is for polymarket.com wallets; Polymarket US accounts only need "
                         "an API key from polymarket.us/developer.")
    if args.action == "check-account":
        result = check_account(env.get(env.WALLET_ADDRESS) or "", env.get(env.PRIVATE_KEY) or "")
        if args.json:
            print(json.dumps(result))
        else:
            console.print(f"{result['wallet']}: {result['wallet_type_label']} · balance {result['collateral_balance']}")
            console.print("Session keys: " + ("supported" if result["session_keys_supported"] else
                                              "not available for this wallet type"))
        return 0
    if args.action == "resume":
        console.print("Trading resumed." if ledger.resume() else "Trading was not halted.")
        return 0
    if args.action == "log":
        for e in ledger.entries()[-args.n:]:
            console.print(f"{e['ts'][:19]} {e['mode']:7} {e.get('outcome', ''):10} ${e['cost']:>8,.2f} "
                          f"edge ${e['edge']:>6,.2f}  {e['title'][:70]}")
        return 0
    if args.action == "authorize-session":
        return _authorize_session(args)
    if args.action == "revoke-session":
        if not args.address:
            raise ValueError("revoke-session needs --address (the session key's public address)")
        owner_client_from_env().revoke_session_key(address=args.address)
        console.print(f"Revoked session key {args.address}.")
        return 0
    exchange = _exchange(args)
    if args.action == "status":
        acct = exchange.account_summary()
        if args.json:
            print(json.dumps(acct))
            return 0
        console.print(f"{acct['wallet']} ({acct['wallet_type']}) · balance {acct['collateral_balance']}")
        console.print(f"Spent today (live): ${ledger.spent_today(datetime.now(timezone.utc)):,.2f}")
        halted = ledger.halted()
        console.print(f"[red]Halted: {halted}[/red]" if halted else "Not halted.")
        return 0
    if args.action == "setup":
        if not args.yes:
            console.print("This submits token approvals from your wallet so Polymarket's exchange and the Neg Risk "
                          "Adapter can spend pUSD and outcome tokens for your orders. Re-run with --yes to proceed.")
            return 1
        for label in exchange.setup_approvals():
            console.print(f"[green]done[/green] {label}")
        return 0
    return 2


def _test_order(args: argparse.Namespace, ledger: Ledger) -> int:
    if args.venue != "us":
        raise ValueError("`trade test-order` is for Polymarket US (--venue us).")
    if not args.yes:
        console.print("This places real orders: it buys about $1 of YES in a busy market and sells it straight "
                      "back, then does the same with NO. It costs the spread plus fees, a few cents. Re-run with "
                      "--yes to go ahead.")
        return 1
    from .us import UsClient, UsExchange, run_test_trade

    result = run_test_trade(UsExchange.from_env(), UsClient())
    ledger.record({"ts": datetime.now(timezone.utc).isoformat(), "mode": "test", "outcome": "test_order",
                   "title": f"Test order: {result['market']}", "url": result["url"], "cost": 0.0, "edge": 0.0,
                   "spent": 0.0, "result": result})
    if args.json:
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    console.print(f"Market: {result['market']} ({result['url']})")
    for s in result["steps"]:
        mark = "[green]ok[/green]" if s["ok"] else "[red]failed[/red]"
        console.print(f"  {mark} {s['step']} · {s['status']}" + (f" · {s['message']}" if s["message"] else ""))
    console.print(("[green]" if result["ok"] else "[red]") + result["verdict"] + ("[/green]" if result["ok"] else "[/red]"))
    if result["position_left"] not in (None, result["position_before"]):
        console.print(f"[yellow]Position left in this market: {result['position_left']:g} contracts.[/yellow]")
    return 0 if result["ok"] else 1


def _authorize_session(args: argparse.Namespace) -> int:
    owner = owner_client_from_env()
    result = authorize_session_key(owner, setup_approvals=not args.no_approvals)
    if args.json:  # for the Mac app, which stores the key in the Keychain
        print(json.dumps(result))
        return 0
    env.update_dotenv({env.PRIVATE_KEY: result["session_private_key"], env.WALLET_ADDRESS: result["wallet"]})
    console.print(f"[green]Session key {result['session_address']} authorized[/green] for {result['wallet']} "
                  f"(trading only, valid until {result['valid_until']}).")
    console.print(f"Saved it to .env as {env.PRIVATE_KEY}. The bot no longer needs {env.OWNER_PRIVATE_KEY}: "
                  "remove it from .env if you put it there.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from rich.table import Table

    from .backtest import HORIZONS, Backtest

    if args.venue != "us":
        raise ValueError("The backtest uses Polymarket US's public price history; run it with --venue us.")
    with console.status("Backtesting on settled Polymarket US markets…") as status:
        result = Backtest(Path(args.data_dir), seed=args.seed).run(
            args.per_group, band=(args.min_price, args.max_price), progress=lambda m: status.update(m))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    colour = {"profitable": "green", "loses money": "red", "unclear": "yellow"}

    def cell(s: dict) -> str:
        if not s.get("n"):
            return "[dim]–[/dim]"
        return (f"[{colour[s['verdict']]}]{s['roi'] * 100:+.2f}%[/] n={s['n']}\n"
                f"won {pct(s['win_rate'])} need {pct(s['breakeven'])}")

    table = Table(title=f"Buying every contract priced {args.min_price * 100:.0f}–{args.max_price * 100:.1f}¢: "
                        "return per $1 after fees, by how long before the end it was bought",
                  show_lines=True)
    table.add_column("Group", overflow="fold")
    for h, _ in HORIZONS:
        table.add_column(f"{h} before", justify="right")
    for g in result["groups"]:
        table.add_row(f"{'[bold]' if g['key'] == 'all' else ''}{g['label']}", *[cell(g["horizons"][h]) for h, _ in HORIZONS])
    console.print(table)
    buckets = Table(title="By price (all groups)", show_lines=True)
    buckets.add_column("Price")
    for h, _ in HORIZONS:
        buckets.add_column(f"{h} before", justify="right")
    for b in result["buckets"]:
        buckets.add_row(f"{b['low'] * 100:.0f}–{b['high'] * 100:.1f}¢", *[cell(b["horizons"][h]) for h, _ in HORIZONS])
    console.print(buckets)
    check = result["settlement_check"]
    console.print(f"[dim]{result['markets_sampled']:,} settled markets sampled, {result['bets']:,} simulated bets. "
                  f"Green = the 95% range of the win rate is above break-even, red = below, yellow = can't tell "
                  f"yet. Results cross-checked with Polymarket's settlement for {check['agree']}/{check['checked']} "
                  f"markets. Re-run with a larger --per-group to narrow the ranges (the sample is cached).[/dim]")
    return 0


def _period_label(period: str) -> str:
    return {"early": "pre-game", "day_of": "day of game", "live": "live", "daily": "daily",
            "daily_event": "daily"}.get(period, period)


def cmd_rewards(args: argparse.Namespace) -> int:
    from rich.table import Table

    from .rewards import RewardsPaper, RewardsScanner, RewardsService

    if args.venue != "us":
        raise ValueError("Liquidity rewards are a Polymarket US program; run it with --venue us.")
    config = load_config(args.config).rewards
    if args.budget:
        config.budget_usd = args.budget
    data_dir = Path(args.data_dir)
    money = lambda x: f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"  # noqa: E731

    if args.action == "reset":
        if not args.yes:
            console.print("This archives the rewards paper test and starts again. Re-run with --yes to proceed.")
            return 1
        paper = RewardsPaper(data_dir, config)
        paper.reset()
        paper.save()
        console.print("Rewards paper test reset.")
        return 0

    if args.action == "status":
        s = RewardsPaper(data_dir, config).summary()
        if not s["quotes"]:
            console.print("No paper quotes yet. Start the dashboard (pmscan serve --venue us) or run "
                          "`pmscan rewards paper --venue us`.")
            return 0
        console.print(f"[bold]Rewards paper test[/bold]: rewards {money(s['rewards'])} (payable {money(s['payable'])}) · "
                      f"fills {money(s['fill_pnl'])} · total {money(s['pnl'])} · {s['quoting']} quoting, "
                      f"{s['holding']} filled and held · {s['hours_quoted']:.1f} quote-hours")
        table = Table(show_lines=False)
        for col in ("Market", "Side", "Price", "Status", "Rewards", "P&L"):
            table.add_column(col, justify="right" if col in ("Price", "Rewards", "P&L") else "left",
                             overflow="fold")
        for r in s["rows"][:args.top]:
            table.add_row(r["title"][:60], r["side"], f"{r['price']:.3f}", r["status"], f"${r['accrued']:.2f}",
                          money(r["pnl"]))
        console.print(table)
        return 0

    if args.action == "paper":
        service = RewardsService(data_dir, config)
        console.print(f"Paper-testing up to {config.max_quotes} quotes of ${config.budget_usd:,.0f}; "
                      f"re-checking every {config.tick_seconds:.0f}s. Ctrl-C to stop.")
        while True:
            if time.time() - service.last_scan_at >= config.rescan_minutes * 60:
                with console.status("Scanning reward programs…") as st:
                    service.scanner.scan(config, progress=lambda m: st.update(m))
                    service.last_scan_at = time.time()
                opened = service.paper.open_from(service.scanner.latest() or {}, time.time())
                for q in opened:
                    console.print(f"quoting {q.side} on {q.title} at {q.price:.3f} (${q.budget:,.0f})")
                service.paper.save()
            service._tick()
            for m in service.messages:
                console.print(m)
            service.messages.clear()
            s = service.paper.summary()
            console.print(f"[dim]{time.strftime('%H:%M:%S')} rewards {money(s['rewards'])} · fills "
                          f"{money(s['fill_pnl'])} · {s['quoting']} quoting, {s['holding']} held[/dim]")
            time.sleep(config.tick_seconds)

    with console.status("Reading Polymarket US reward programs…") as status:
        result = RewardsScanner(data_dir).scan(config, refresh=args.refresh, progress=lambda m: status.update(m))
    if args.json:
        Path(args.json).write_text(json.dumps(result.__dict__, indent=1, default=str))
    rows = [e for e in result.estimates if e["quote"] and (args.all or e["paying_now"])]
    table = Table(title=f"Liquidity rewards for a ${config.budget_usd:,.0f} quote, best first"
                        f"{'' if args.all else ' (paying now; --all adds upcoming periods)'}", show_lines=True)
    for col in ("Market", "Period", "Market's pool", "Quote", "Share", "Per hour", "Per day / $100", "Risk"):
        table.add_column(col, justify="left" if col in ("Market", "Period", "Quote") else "right", overflow="fold")
    for e in rows[:args.top]:
        q = e["quote"]
        yes = f"YES ({e['outcome']})" if e.get("outcome") else "YES"
        verb = f"buy {yes} at" if q["side"] == "bid" else f"sell {yes} at"
        when = _period_label(e["period"]) + ("" if e["paying_now"] else " (later)")
        table.add_row(
            f"{e['title'][:70]}\n[dim]{e['program']}[/dim]", when,
            f"${e['market_pool']:,.2f}\n[dim]${e['pool']:,.0f} / {e['program_markets']} mkts[/dim]",
            f"{verb} {q['price']:.3f}\n[dim]{q['size']:g} contracts, {q['ticks_behind']} tick{'s' if q['ticks_behind'] != 1 else ''} back[/dim]",
            f"{q['share'] * 100:.0f}%", f"${e['per_hour']:.2f}", f"${e['per_day_per_100']:.2f}",
            f"${q['capital']:,.0f}\n[dim]{(q['from_mid'] or 0) * 100:.1f}¢ from mid[/dim]")
    console.print(table)
    earned = result.earnings
    console.print(f"[dim]{result.programs:,} markets in reward programs; {result.books_read} order books read. "
                  f"Your rewards so far: paid ${earned.get('paid', 0):,.2f}, pending ${earned.get('pending', 0):,.2f}."
                  "[/dim]")
    for note in result.notes:
        console.print(f"[dim]· {note}[/dim]")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    paper = PaperTrader(config.paper, _client(args), Path(args.data_dir))
    if args.action == "reset":
        which = args.strategy or "every"
        if not args.yes:
            console.print(f"This archives the {which} paper portfolio and starts again with "
                          f"${config.paper.starting_balance:,.0f}. Re-run with --yes to proceed.")
            return 1
        for archive in paper.reset(args.strategy):
            console.print(f"Archived to {archive}")
        console.print("Paper portfolio reset.")
        return 0
    rate = lambda x: "–" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    money = lambda x: f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"  # noqa: E731
    for s in paper.summary()["books"].values():
        implied = f" vs {rate(s['implied_win_rate'])} implied" if s["implied_win_rate"] is not None else ""
        console.print(f"[bold]{s['title']}[/bold]: equity ${s['equity']:,.2f} ({money(s['pnl'])}) · "
                      f"{s['open_bets']} open bets · {s['resolved']} resolved, won {s['won']} "
                      f"({rate(s['win_rate'])}{implied}) · realized {money(s['realized_pnl'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pmscan", description="Scan Polymarket for profitable contracts.")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="TOML config file (default: ./pmscan.toml if present)")
    common.add_argument("--data-dir", default="data", help="where results, history and the trade log go")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--venue", choices=["international", "us"], default=os.environ.get(env.VENUE) or None,
                        help="international = polymarket.com (default), us = Polymarket US (polymarket.us); "
                             "US results, paper portfolios and trades are kept in DATA_DIR/us")

    scan_opts = argparse.ArgumentParser(add_help=False, parents=[common])
    scan_opts.add_argument("--strategy", help=f"comma-separated subset of: {', '.join(ALL_STRATEGIES)}")
    scan_opts.add_argument("--top", type=int, help="rows per strategy")
    scan_opts.add_argument("--sort", choices=sorted(SORT_KEYS), default="score",
                           help="score = profit for arbitrage, annualized for high-prob, EV for value bets")
    scan_opts.add_argument("--no-save", action="store_true", help="don't write results to --data-dir")

    loop_opts = argparse.ArgumentParser(add_help=False, parents=[scan_opts])
    loop_opts.add_argument("--interval", type=float, default=120, help="seconds between scan starts")
    loop_opts.add_argument("--no-alerts", action="store_true", help="don't send Telegram/Discord alerts")
    loop_opts.add_argument("--trade", action="store_true", help="auto-trade qualifying opportunities (dry run)")
    loop_opts.add_argument("--live", action="store_true", help="with --trade: place real orders")
    loop_opts.add_argument("--no-paper", action="store_true", help="don't run the paper trader")
    loop_opts.add_argument("--no-fast", action="store_true", help="serve: don't use the live order-book feed")

    p = sub.add_parser("scan", parents=[scan_opts], help="run one scan and print results")
    p.add_argument("--json", help="also write the full result to this file")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("watch", parents=[loop_opts], help="rescan on an interval in the terminal")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("serve", parents=[loop_opts], help="rescan on an interval and serve the dashboard app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p.add_argument("--exit-with-parent", action="store_true", help=argparse.SUPPRESS)  # used by the Mac app
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("alerts", parents=[common], help="alert channel tools")
    p.add_argument("action", choices=["test"])
    p.set_defaults(func=cmd_alerts_test)

    p = sub.add_parser("trade", parents=[common], help="account connection, status and the trade log")
    p.add_argument("action", choices=["status", "setup", "resume", "log", "check-account", "authorize-session",
                                      "revoke-session", "test-order"])
    p.add_argument("--yes", action="store_true", help="confirm `setup` approvals or the `test-order` round trip")
    p.add_argument("-n", type=int, default=20, help="entries to show for `log`")
    p.add_argument("--json", action="store_true", help="machine-readable output (status, authorize-session)")
    p.add_argument("--no-approvals", action="store_true", help="authorize-session: skip the approval transactions")
    p.add_argument("--address", help="revoke-session: the session key's public address")
    p.set_defaults(func=cmd_trade)

    p = sub.add_parser("backtest", parents=[common],
                       help="backtest the 90¢+ strategy on settled Polymarket US markets")
    p.add_argument("--per-group", type=int, default=1000,
                   help="settled markets to sample per group (cached; a bigger number adds to the sample)")
    p.add_argument("--min-price", type=float, default=0.90)
    p.add_argument("--max-price", type=float, default=0.995)
    p.add_argument("--seed", type=int, help="random seed for sampling")
    p.add_argument("--json", help="also write the full result to this file")
    p.set_defaults(func=cmd_backtest)  # Polymarket US only (see main): its price history is public

    p = sub.add_parser("rewards", parents=[common],
                       help="Polymarket US liquidity rewards: rank resting quotes and paper-test them")
    p.add_argument("action", nargs="?", default="scan", choices=["scan", "paper", "status", "reset"],
                   help="scan = rank quotes now (default); paper = run the paper test here; status; reset")
    p.add_argument("--budget", type=float, help="dollars behind each quote (default: [rewards] budget_usd)")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--all", action="store_true", help="scan: include periods that haven't started")
    p.add_argument("--refresh", action="store_true", help="scan: re-read the program list now")
    p.add_argument("--yes", action="store_true", help="confirm `reset`")
    p.add_argument("--json", help="scan: also write the full result to this file")
    p.set_defaults(func=cmd_rewards)  # Polymarket US only (see main)

    p = sub.add_parser("paper", parents=[common], help="paper-trading portfolios")
    p.add_argument("action", choices=["status", "reset"])
    p.add_argument("--yes", action="store_true", help="confirm `reset`")
    p.add_argument("--strategy", help="reset only this strategy's portfolio")
    p.set_defaults(func=cmd_paper)
    return parser


def main(argv: list[str] | None = None) -> int:
    env.load_dotenv()
    args = build_parser().parse_args(argv)
    if hasattr(args, "venue"):
        if getattr(args, "func", None) in (cmd_backtest, cmd_rewards) and not args.venue:
            args.venue = "us"  # not set_defaults: the shared --venue option would change for every command
        args.venue = args.venue or os.environ.get(env.VENUE) or "international"
        if args.venue not in ("international", "us"):
            raise SystemExit(f"{env.VENUE} must be 'international' or 'us'")
        if args.venue == "us":  # keep US results, paper portfolios and trades apart
            args.data_dir = str(Path(args.data_dir) / "us")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
