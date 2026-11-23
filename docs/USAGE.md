# pmscan

Scans every open Polymarket market (about 18,000 events and 170,000 tradeable markets) every
couple of minutes. It lists the contracts with a measurable edge, each one checked against the
live order book with taker fees included. It can also alert you to new opportunities and trade
them automatically within limits you set.

It works with either Polymarket exchange:

- **Polymarket US** (polymarket.us): the CFTC-regulated exchange for US residents. You sign in
  with Apple, Google or email, your ID is verified, and there's no crypto wallet. About 5,300
  events and 25,000 markets. Use `--venue us`, or pick it in the Mac app.
- **Polymarket international** (polymarket.com): the original crypto-based exchange, not open
  to US residents. This is the default for the command line.

They're separate exchanges with their own markets, prices and accounts. A polymarket.us account
can't trade on polymarket.com, or the reverse. See [Polymarket US](#polymarket-us).

## Quick start

```bash
uv sync                        # add --extra trade for live trading
cp .env.example .env           # optional: Odds API key, alert channels, wallet
uv run pmscan scan             # one scan, printed as tables
uv run pmscan watch            # rescan every 120s in the terminal
uv run pmscan serve --open     # same, with the dashboard app at http://127.0.0.1:8765
```

Useful flags:
- `--strategy neg_risk_arb,ladder_arb`
- `--top 50`
- `--sort roi|annualized|win|profit|soonest`
- `--interval 300`
- `--json out.json`
- `--config my.toml`

`watch` and `serve` also take:
- `--trade` (dry run)
- `--trade --live`
- `--no-alerts`

Each scan writes `data/latest.json` and appends to `data/history.jsonl`. Trade attempts go to
`data/trades.jsonl`.

| Command | What it does |
|---|---|
| `pmscan scan` | One scan |
| `pmscan watch` / `serve` | Repeated scans: terminal or dashboard, alerts, optional trading |
| `pmscan alerts test` | Send a test message to your configured channels |
| `pmscan trade setup --yes` | One-time wallet approvals for live trading |
| `pmscan trade status` | Wallet, balance, today's spend, halt state |
| `pmscan trade log` | Recent trade attempts |
| `pmscan trade resume` | Clear a halt after a partial fill |

## Mac app

A native macOS app (macOS 12+) runs the scanner for you, so there's no terminal or browser to
manage:

```bash
uv sync                       # once: the app runs the scanner from this project's .venv
macos/build.sh --install      # builds dist/pmscan.app and copies it to ~/Applications
```

Then open **pmscan** from Launchpad, Spotlight or `~/Applications`. Building needs only the Xcode
Command Line Tools (`xcode-select --install`), not Xcode.

What the app adds on top of the dashboard:
- **Keeps running (Settings › General).**
  - Open at login, in the background: menu-bar icon only, with no window or Dock icon.
  - Keep running in the menu bar when the window is closed. The Dock icon goes away too, and the
    menu-bar icon shows the number of open arbitrage opportunities. Choose **Open pmscan** in its
    menu, or open the app again, to bring the window back; **Quit pmscan** there stops it.
  - Keep the Mac awake while the bot runs. The display can still sleep, but a laptop with its lid
    closed sleeps unless it's on power with an external display.
- **Exchange (Settings › General or Account).** Polymarket US (the default) or polymarket.com.
  Switching restarts the scanner, and each exchange keeps its own results and paper portfolios.
  The sidebar shows which one is active.
- **Account and trading settings (⌘,).** Connect your account: a Polymarket US API key, or a
  polymarket.com wallet with a session key. Switch automatic trading between Off, Dry run and
  Live, with a confirmation before going live. Paper trading can be switched on or off here too
  (see below).
- **Native window.** A sidebar with live opportunity counts and a toolbar with scan status, reload
  and open-in-browser buttons.
- **Menus and shortcuts.** ⌘1–⌘7 switch pages, ⌘R reloads, ⇧⌘R restarts the scanner.
- **Scanner menu.** Toggle dry-run trading, show the data folder, show the scanner log.
- **Dock badge.** Shows the number of open arbitrage baskets.
- **Notifications.** A macOS notification for each new arbitrage, and when trading halts. Click
  one to jump to it; macOS asks permission the first time.
- **Scanner lifecycle.** The app starts the scanner on launch and stops it on quit. If the app
  crashes, the scanner exits within seconds.

The pages inside the window are the dashboard described below, rendered by the system WebKit.

Notes:
- The app runs `python -m pmscan.cli serve` from this project folder and uses its `.env`,
  `pmscan.toml` and `data/`. If you move the folder, rebuild.
- The trading key (or Polymarket US secret) is stored in the macOS Keychain, not in any file. After you rebuild the app,
  macOS may ask once whether pmscan may read it. Choose *Always Allow*.
- The scanner log is `~/Library/Logs/pmscan/scanner.log`.

## Dashboard app

`pmscan serve` runs the scanner in the background and serves a dashboard app on your machine. It
has no build step and needs no npm: the app is plain ES modules in `pmscan/static/app/`, and it
works in Safari 15 and later. Preact
and htm (about 10 KB) are vendored under `pmscan/static/vendor/` and checked against npm's
integrity hashes. Nothing loads from a CDN.

| View | What it shows |
|---|---|
| **Overview** | Guaranteed arbitrage profit available now, plus a count per strategy with sparklines. Trend charts (1h / 6h / 24h / 7d / all) of profit available and opportunities per scan, and an activity feed (alerts, trades, skipped trades and why) |
| **Neg-risk** | Baskets available now; click a row to see its legs and a cost-vs-payout meter. Also a chart of how close the 40 nearest neg-risk groups are to break-even |
| **Ladders** | Each ladder arbitrage drawn as a range plot of every rung, with the out-of-order pair highlighted |
| **Sportsbook** | Polymarket price plotted against bookmaker probability for every compared bet, with value bets highlighted, plus the value-bet table |
| **High-probability** | Filters, a scatter of price against days to resolution (click a dot for its market), and the table |
| **Near-certain** | 97–99.5¢ bets ending within 3 days, each with how its category and price did in the backtest, the paper portfolio's results, and the full track record by category |
| **Trading** | Mode and halt status, spend against caps, cumulative edge, and the trade log with each order's amount and price cap |
| **Markets** | Search any open market, then view its price history (1d / 1w / 1m / all) and live order-book depth |

Every chart has a **Table** toggle, hover and keyboard readouts, and light and dark themes. The
theme follows your system by default; switch it with the header button. Scan history is stored in
`data/scans.jsonl`, so the trend charts fill in the longer `serve` runs.

The app reads a small JSON API, which is also usable from scripts. Every endpoint is a GET:

| Endpoint | Returns |
|---|---|
| `/api/latest` | The latest scan result, scanner status, and trading status |
| `/api/scans?limit=` | Per-scan summaries |
| `/api/history?strategy=&limit=` | Arbitrage and value opportunities over time |
| `/api/trades?limit=` | The trade ledger |
| `/api/markets?q=&limit=` | Search across the latest scan's markets |
| `/api/market?token=` | One market, looked up by outcome token |
| `/api/price-history?token=&interval=1d\|1w\|1m\|max` | Price history, proxied from the CLOB and cached for 60s |
| `/api/book?token=` | The current order book, cached for 10s |

## How a scan works

1. **Fetch.** Every open event and its markets come from the Gamma API (`/events/keyset`), in
   roughly 15–30 seconds.
2. **Screen.** Each strategy checks Gamma's best bid and ask for every market and picks out the
   candidates.
3. **Verify.** Order books for the candidates come from the CLOB `/books` endpoint (500 tokens per
   request). The strategy walks the real ask ladders, including fees, to work out how much it
   could actually buy and what it would earn.

## Strategies

| Strategy | Payout | Risk |
|---|---|---|
| `neg_risk_arb` | Fixed in advance | Execution only |
| `ladder_arb` | At least $1 per pair | Execution, plus misreading the wording |
| `sportsbook_value` | Positive expected value if the bookmakers are right | Model: single bets lose often |
| `high_prob` | Pays if a heavy favorite wins | Tail: a 96¢ contract loses about 4% of the time |
| `near_certain` | Pays 0.5–3% if something very unlikely doesn't happen | Tail: one loss costs 30–100 wins |

### Neg-risk arbitrage

A neg-risk event is a set of mutually exclusive markets, and at most one of them resolves YES
("Who wins?", sentencing brackets, and so on).

- **Buy every YES.** One YES in each outcome pays $1 if exactly one wins, so the basket is
  profitable when the YES asks plus fees sum to less than $1.
  - The scanner skips this basket when any outcome can't be traded.
  - By default it also skips *augmented* events, where Polymarket can add outcomes later.
- **Buy every NO.** k NO shares pay at least k − 1 dollars, whichever outcome wins. This basket
  needs no assumption about the outcome list being complete.

A single YES/NO market can't be arbitraged this way, because Polymarket's YES and NO books mirror
each other (NO ask = 1 − YES bid).

### Ladder arbitrage

Some markets are nested: "above $110k" implies "above $100k", "by June" implies "by December",
and Over 3.5 implies Over 2.5. If A implies B, then A can't be more likely than B. When the market
prices A higher anyway (A's YES bid is above B's YES ask), buying YES on B and NO on A pays at
least $1 per pair. It pays $2 when B happens but A doesn't.

The nesting is inferred from the wording, and the parser is strict about it:
- Markets in one event are grouped by question, with numbers and dates blanked out.
- A group must differ in exactly one number or date, and carry a direction word such as
  "above", "dip to", "or more", "O/U" or "by <date>".
- These cases are skipped:
  - neg-risk brackets
  - "between" questions and ranges
  - "hit" (it can mean up or down)
  - "in <month>", which isn't cumulative
  - mixed ↑/↓ targets
  - ladders whose prices are mostly inverted, which suggests a misread

Even so, **read both markets' rules before trading one.**

### Sportsbook value

Polymarket game moneylines are compared with bookmaker odds from [The Odds API](https://the-odds-api.com).

- Each book's odds have the bookmaker margin removed, and the median across books is used as the
  fair probability.
- A Polymarket outcome is listed when `fair / (price + fee) − 1 ≥ min_edge` (default 3%).
- The list also shows the Kelly stake.
- Games are matched on team names and kickoff time (within 90 minutes).
- It handles both US-style markets (one market with the two teams as outcomes) and soccer-style
  ones (separate win/draw/win markets).

This is a model, not an arbitrage. Sharp books are usually closer to the truth, but any single
game is close to a coin flip with a slight tilt.

To turn it on, set `PMSCAN_ODDS_API_KEY`, or paste the key into the Mac app (Settings › General ›
Sportsbook comparison), which checks it and keeps it in the Keychain.

Credits:
- Each refresh costs one credit per sport per region.
- Odds are cached for `refresh_minutes` (default 60), and only sports with games in the next
  48 hours are fetched.
- Once the API reports the credits left, refreshes slow down so they last until the month
  resets. On the free plan (500 a month) that's roughly one refresh per sport every few hours.

It works on both exchanges. On Polymarket US the moneylines use full team names, and US league
codes map to Odds API sports. The defaults (`sports` in `[sportsbook_value]`) are NFL, NBA,
NHL, college football and basketball, the Premier League and the Champions League. Add `wcbb`
(women's college basketball), `uel` or others there.

### High-probability yield

Outcomes priced at 90–99.5¢ that resolve within 30 days, ranked by the return if they win,
annualized over the lock-up period.

The price is the market's own estimate of your chance of winning. The case for this strategy is
the favorite-longshot bias, which is a tendency, not a guarantee. In-play games are skipped by
default. Markets past their end date are kept, but flagged.

On Polymarket US, the strategy buys only politics and season futures
(`include_us = ["politics", "sports:futures"]`). Those were the groups that made money in the
[backtest](#backtest); most others broke even or lost after fees. An entry is a category, or a
category and market type separated by a colon: every non-game market on Polymarket US has the
type "futures", so season futures are `sports:futures`. Player props and game winners are also
skipped (`exclude_market_types_us = ["props", "moneyline"]`). Set both to `[]` to buy everything.

Markets whose bid and ask are more than 2¢ apart are skipped (`max_spread = 0.02`; `0` turns it
off). In a wide book the ask is a leftover order, not the market's estimate. In the backtest, 90¢+
politics and season futures bought with a spread of 2¢ or less won 98.7–99.4% of the time
(+1.3–1.9% per dollar, 1 to 21 days out). Those bought in books more than 5¢ wide won only
59–91% and lost 7–40% per dollar.

### Near-certain bets

The "it won't happen" side of things that almost never happen: positions priced 97–99.5¢ that
end within 3 days, politics only by default (`[near_certain]` in the config takes the same keys
as `[high_prob]`). Each bet returns 0.5–3% after fees, and one loss costs the whole stake, so a
99¢ bet has to win more than 99.1% of the time just to break even.

In the backtest (each settled market counted once, bought 1 hour to 3 days before the end),
politics won 819 of 823 for +0.8% per dollar. Weather, culture, economic data and season futures
were break-even or worse at these prices; player props and game winners lost 6–9%. A week or more
before the end, even politics stopped paying.

Two caveats:

- **The politics record is mostly one night.** Almost all of it is from the November 2026
  midterms, so its markets aren't independent. The paper portfolio is the real test.
- **News doesn't help.** Buying YES on long shots at 1–15¢ lost 50–70% per dollar in nearly every
  category. Long shots whose price had just jumped came true *less* often, so the strategy has no
  "skip it if the news is moving it" filter.

The strategy finds bets only when political markets near their end, so expect long quiet
stretches. Its paper portfolio ($200 a bet, `near_certain_stake_usd`) is kept apart from the 90¢+
one, which also holds some of the same positions.

## Alerts

Set `PMSCAN_TELEGRAM_BOT_TOKEN` + `PMSCAN_TELEGRAM_CHAT_ID` and/or `PMSCAN_DISCORD_WEBHOOK_URL`,
then run `pmscan alerts test`.

- `watch` and `serve` then send new arbitrage (≥ $2 profit) and value bets (≥ 5% EV).
- Each opportunity is sent once per 2-hour cooldown, with at most 5 per scan.
- Trade fills, misses and halts are sent too.

All of these thresholds are under `[alerts]` in the config.

## Paper trading

The paper trader runs simulated portfolios, on by default in `watch`, `serve` and the Mac app.
There's **one portfolio per strategy**, each starting with $10,000, so the strategies can be
compared over the same period. Nothing real is bought.

| Portfolio | Buys |
|---|---|
| 90¢+ positions | Every listed position priced 90–99.5¢, $50 each (the high-probability list: resolves within 30 days, liquid, not already in play) |
| Near-certain bets | Every near-certain bet (97–99.5¢, ends within 3 days), $200 each |
| Neg-risk arbitrage | Every arbitrage basket, all legs together, up to $200, sized like the live trader |
| Ladder arbitrage | The same, for ladder pairs |
| Sportsbook value | Each value bet, $50, never above the bookmakers' fair price |

How positions are handled:
- **Entry** walks the live order book with taker fees, so fills cost what a real order would.
- **At most 3 bets per event** for 90¢+ and near-certain (`max_bets_per_event`; `0` = no limit),
  best-scoring first. Markets in one event often go wrong together (a rankings surprise knocks out
  several "qualifies" bets at once), so one surprise can't sink a pile of bets.
- **Valuation** uses each position's best bid. The bid/ask spread therefore shows up as an
  immediate small loss.
- **Settlement** uses Polymarket's final resolution.
- **A bet counts as resolved** once all its positions have settled. In an arbitrage basket most
  legs expire worthless by design; what matters is the basket's total.
- **For 90¢+ positions**, the realized win rate is shown against the win rate the prices implied.
  If the actual rate beats the implied one over many bets, buying favorites has an edge.

Settings live under `[paper]` in `pmscan.toml`. The files are in `data/paper/<strategy>/`.

```bash
uv run pmscan paper status                           # each portfolio: equity, P&L, win rate
uv run pmscan paper reset --yes [--strategy NAME]    # archive and start over
```

The Paper page compares the portfolios in a table, shows an equity chart for each, and lists open
and resolved positions with a strategy filter.

## Backtest

```bash
uv run pmscan backtest                    # ~1,000 settled markets per group, 15-20 minutes
uv run pmscan backtest --per-group 3000   # grows the cached sample: narrower ranges
```

It tests the 90¢+ strategy on settled Polymarket US markets: if you had bought every contract
priced 90–99.5¢ some time before the market ended, would it have made money after fees? The
Mac app and dashboard have a **Backtest** page that shows the results and can start a run.

How it works:
1. **Sample.** It samples settled markets in each group at random: game winners, soccer, season
   futures, player props, crypto, politics, culture, finance, macro and weather. Polymarket US
   has more than a million settled markets, mostly props and 15-minute crypto markets, so
   reading them all would drown out everything else.
2. **Replay.** It replays each market's hourly price history. YES costs the best ask; NO costs
   1 − the best bid.
3. **Buy.** It buys whichever side was priced in the band 1 hour, 6 hours, 1 day, 3 days and
   7 days before the end:
   - For game markets, the end is the start of the game; the strategy doesn't buy during play.
   - Prices are never taken after a market's book froze at settlement. A stale quote there
     would be a sure win that was never really for sale.
4. **Score.** It scores each bet with the result Polymarket recorded, cross-checked against its
   settlement endpoint, minus today's lowest-tier US taker fee.

**Results.** These are from 10,000 markets and 12,157 bets on November 19, 2026, as return
per $1 after fees:

| Group | Result | Verdict |
|---|---|---|
| Politics | +0.9% to +1.5% (1 hour to 3 days before) | made money |
| Weather | +1.2% (1 hour before) | made money at 1 hour only |
| Season futures | +0.3% to +0.9% | positive, not proven |
| Culture, macro, finance | −2% to +2% | can't tell |
| Soccer win / draw / win | +0.6% to +2.6%; −7.5% at 7 days | can't tell |
| Player props | −2% to −4% | lost money (won 92–95%, needed ~96.5%) |
| Game winners | −5.6% (1 hour before) | lost money close to the game |
| **All groups** | **−0.2% to +0.7%** | about break-even |

Across everything, 90¢+ contracts won about as often as their prices implied. Buying all of
them is roughly break-even after fees; the edge, if any, is in which markets you pick.

Caveats:
- Prices are the top of the book with no depth, so a real $50–100 order could fill a little
  worse.
- Markets within one event move together, so the real uncertainty is wider than the 95%
  ranges shown.
- Voided markets are left out.

Results and the cached sample live in `data/us/backtest/`.

## Liquidity rewards (Polymarket US)

Polymarket US pays reward pools for resting limit orders, whether they fill or not
([how it works](https://docs.polymarket.us/incentives/liquidity.md),
[live schedule](https://polymarket.us/rewards)). Each second the exchange scores every order
on a market's book as `discount_factor ^ ticks_from_best × size`. It counts from the best price
outward only until the side holds the program's **target size**, and pays each order its share
of that side's score. A second pays nobody if a side never reaches the target, or, for programs
with a **max spread**, if the book is too wide.

A program's pool is **shared by every market in it**. Polymarket's page puts it as "the
total amount paid out for a time period, shared across the program's markets — never summed per
market". The API repeats the program's pool on each market, so "NFL Spreads Live $6,300" means
about $5.50 for each of its 1,148 lines, not $6,300 each. The scanner uses pool ÷ markets in
the program. Small programs pay the most per market: a single-market gold up/down daily
program pays $1,000 a day, while the 2,600 NBA player props share $700 per game.

```bash
uv run pmscan rewards                 # rank quotes on markets paying now ($50 each)
uv run pmscan rewards --all --budget 25
uv run pmscan rewards paper           # paper-test the best quotes in the terminal
uv run pmscan rewards status          # paper results so far
```

All of these need your US API key, because the program list comes from the authenticated
`/v1/incentives` API. The key is only used to read; nothing is traded. In the dashboard and the
Mac app, the **Rewards** page shows the same ranking, and the server paper-tests it in the
background.

For each market paying now (or within `horizon_hours`), the scanner:
- reads the live book;
- tries a quote of `budget_usd` at the best price and up to `max_ticks_behind` ticks back, on
  either side (buying YES costs p per contract, selling YES costs 1 − p);
- reports the best quote's share, pay per hour and pay per day per $100 at risk.

**The paper test** keeps up to `max_quotes` quotes, one per program, on the best markets. It
leaves out two kinds of market where quoting lost money in the first runs:
- **live-game periods** (unless `paper_live = true`): a quote resting through a live tennis
  match filled at once and lost $17 against a cent of rewards;
- **programs that track a fast-moving price** (`paper_skip_programs`: crypto, metals, gasoline
  and diesel). Over the first 18 hours, the crypto up/down bids filled within minutes and lost
  $45–48 of each $50, while the paper test earned $10.83 in rewards across all its quotes.
  Slow, news-driven markets (such as the NVIDIA ones) earned their rewards with almost no fills.

Every
`tick_seconds` it:
- credits each quote's estimated rewards from the live book;
- moves the quote with the best price;
- marks the quote filled in full as soon as a trade prints at or through its price, or the
  other side of the book reaches it.

A filled quote is held until its market settles, so rewards and fill losses show side by side.
Rewards under $1 per market per day count as not payable, since Polymarket doesn't pay them.

Caveats:
- Pay depends on other quoters, and books change: during live games in particular, a book can
  look nothing like it did before the game.
- How each market's second splits between the bid and ask sides isn't documented. The scanner
  assumes half each.
- The biggest pay per dollar is often a quote right at the best price in a market that moves
  all day (crypto and metals up/down). That is exactly where fills, and losses on them, are most
  likely. Let the paper test run for a few days before trusting the per-hour figures.

Scans and the paper ledger live in `data/us/rewards/`; settings are in `[rewards]`.

## Live feed

A full scan takes 20–40 seconds and runs every couple of minutes, but arbitrage often lasts only
seconds. So `serve` and the Mac app also keep a **live order-book feed** open for the 40 events
closest to an arbitrage. Those are chosen after every scan from the strategies' own screening, and
cover several hundred markets.

When a price update moves one of them into an edge at the top of the book, the bot:
1. Fetches that event's order books.
2. Runs the normal strategy evaluation, within about a second.
3. Sends anything it finds down the same path as a scan: trading, alerts, paper trading, and the
   dashboard, tagged "live feed".

An opportunity the scan already found isn't reported again unless its profit grows by more than
25%. The feed needs the SDK (`uv sync --extra trade`). Turn it off with `--no-fast` or
`[fast] enabled = false`.

## Polymarket US

```bash
uv run pmscan scan --venue us                # one scan of polymarket.us
uv run pmscan serve --venue us --trade       # dashboard + dry-run trading
export PMSCAN_VENUE=us                       # or make US the default for every command
```

US results, paper portfolios and the trade log live in `data/us/`, apart from polymarket.com's.

**Connecting your account.** Polymarket US accounts have no wallet or private key. The bot uses
an API key:
1. Finish identity verification in the Polymarket US app. Keys only work once it's approved.
2. Sign in at [polymarket.us/developer](https://polymarket.us/developer) the same way you sign
   into the app, and create a key. If it asks for an invite code, email support@polymarket.us.
3. Copy the **Key ID** and **Secret Key**. The secret is shown only once.
4. Put them in the Mac app (Settings › Account), or in `.env` as `POLYMARKET_US_KEY_ID` and
   `POLYMARKET_US_SECRET_KEY`. Then run `uv run pmscan trade check-account --venue us`.

The US API has endpoints for orders, positions and balances, and none for withdrawals. Delete
the key at polymarket.us/developer to cut the bot off.

**How US markets map onto the scanner:**
- **YES and NO.** Each US market is one instrument; buying NO is shorting it. The scanner still
  shows a YES and a NO side (tokens `<slug>` and `<slug>~no`), and builds the NO book by mirroring
  YES (NO ask = 1 − YES bid).
- **Orders.** Each leg is a fill-or-kill limit order sent with synchronous execution, so the
  fill comes back in the response. Prices on the US API always refer to the YES side: buying NO
  at 40¢ is sent as `ORDER_INTENT_BUY_SHORT` at 60¢.
- **Fees.** `theta × contracts × p × (1 − p)`. Theta is 0.095 at the lowest volume tier, read
  per market, with a lower rate for a new account's first 7 days. That's well above most
  polymarket.com markets, so US arbitrage needs bigger gaps.
- **Arbitrage needs mutually exclusive outcomes, and US events don't say which are.** The
  scanner only builds "buy every NO" baskets for events whose title names a single winner
  ("… Champion", "NBA MVP", "Winner", "Margin of Victory", …). It excludes threshold and deadline
  ladders, plural wording ("Nominees", "Teams", "Which …"), and sets whose YES bids add up past
  $1.25. "Buy every YES" also needs the outcomes to be exhaustive, so it's limited to soccer's
  win / draw / win markets.
- **Rate limit.** The US API serves one order book per request and allows 25 requests a second.
  A scan takes about 35 seconds, and the high-probability strategy checks at most
  `max_candidates_us` (300) books.
- **Live feed.** Polymarket US streams prices only to connected accounts. With your API key set,
  the live feed subscribes to best bid / ask for the markets closest to an arbitrage, up to 100
  per subscription. Without the key, scans run every 2 minutes. The Mac app passes the key to the
  scanner whenever the account is connected; that doesn't place orders, which still need Live
  mode.

**Test trade.** Before going live, prove the order path with real money:

```bash
uv run pmscan trade test-order --venue us --yes
```

It picks a busy market near 50¢ and runs a round trip:
1. Buys about $1 of YES and sells it straight back.
2. Buys about $1 of NO and sells it straight back.

After each step it checks the fill and your position, and reports what happened. It costs the
spread plus fees, a few cents. If the NO order doesn't fill, that means Polymarket US reads NO
prices differently than pmscan sends them. Fix that before trading NO legs. The Mac app has the
same test under Settings › Account › *Run $1 Test Trade*.

## Automated trading

```bash
uv run pmscan serve --trade             # dry run: logs what it would buy, places nothing
uv sync --extra trade                   # installs the official SDK (polymarket-client)
uv run pmscan trade authorize-session   # connect your account with a session key (below)
uv run pmscan serve --trade --live      # real orders
```

In the Mac app, all of this lives in **Settings (⌘,)**: connect the account under Account, then
pick Off / Dry run / Live under Trading. Going live asks for confirmation and shows your limits.

### Connecting your account: session keys

Don't give the bot your main key. Give it a **session key**: a separate key that your account
authorizes to place orders.

- It **can't withdraw funds**.
- It's limited to order-book trading.
- It expires after 180 days.
- You can revoke it at any time.

Your main (owner) key is used **once** to authorize it, and the bot never stores the owner key.

The Mac app walks you through this in **Settings › Account**:

1. **Wallet address.** Click your profile picture on polymarket.com; the address under your name
   starts with 0x.
2. **Private key.** For email or Google sign-ins, Polymarket keeps it at Magic. Open
   [reveal.magic.link/polymarket](https://reveal.magic.link/polymarket), sign in with the same
   email or Google account, and reveal it.
3. **Check Account.** The app detects your wallet type and balance. Nothing is saved yet.
4. **The next step depends on your account type.**
   - **Newer accounts (Deposit Wallet, created on or after May 4, 2026)** get a session key. That
     needs a Relayer API key: create one at polymarket.com › Settings › API Keys › Relayer API
     Keys, and copy the API Key and the Signer Address shown with it.
   - **Older email/Google accounts (Proxy Wallet)** can't create session keys, so the bot must use
     the account's own key.

Connection failures are shown in plain language. They're also logged, with keys removed, to
`~/Library/Logs/pmscan/account.log`.

For the CLI you need the same values: the wallet address, the owner private key, and the Relayer
API key with its address. `pmscan trade check-account` reports the wallet type.

Then connect in one of two ways:
- **Mac app:** Settings › Account › *Create a session key*. The session key goes into the macOS
  Keychain; the owner key isn't saved anywhere.
- **CLI:** put `POLYMARKET_OWNER_PRIVATE_KEY`, `POLYMARKET_WALLET_ADDRESS` and the two
  `POLYMARKET_RELAYER_*` values in `.env`, then run `pmscan trade authorize-session`. It writes the
  session key into `.env` (file mode 600) and never prints it. Afterwards, delete the owner key
  from `.env`.

Both ways also set up the one-time trading approvals; skip them with `--no-approvals`. To check the
connection, run `pmscan trade status` or use *Test Connection* in the app. To revoke a session key,
run `pmscan trade revoke-session --address <session address>` with the owner key set.

Session keys need a Polymarket **Deposit Wallet**, which is the default for current accounts. Older
proxy or Safe wallets have to give the bot the account's own key instead: use *Use an existing key*
in the app, or `POLYMARKET_PRIVATE_KEY` in `.env`. That key can move funds, so **use a dedicated
wallet holding only what you're prepared to lose.**

How it protects you:

1. **Arbitrage only by default.** `[trading].strategies = ["neg_risk_arb"]`. Adding `ladder_arb`
   brings in wording risk, and `sportsbook_value` or `high_prob` bring in directional risk.
2. **Caps.** $25 per trade and $100 per day by default. Only live spend counts toward the daily
   cap.
3. **Fresh check before every trade.** The order books are fetched again and the basket is
   re-sized.
   - No leg may fill above the price the scan evaluated.
   - The basket must still clear `min_profit_usd` / `min_roi` after fees.
   - Legs under the $1 minimum order are skipped, and the reason is shown.
4. **All legs at once.** Every leg is signed first, then all of them are posted in a single batch
   as Fill-or-Kill market buys with a `max_price`. Each leg fills completely or not at all.
5. **Partial fills are unwound.** If some legs fill and others don't, the bot immediately sells the
   filled legs back. These are fill-and-kill sells, never more than 10% below cost
   (`unwind_max_loss_pct`).
   - If the unwind fully works, trading continues and you get a notification.
   - If it doesn't, or a leg is still pending, trading halts and alerts you. Check your positions,
     then run `pmscan trade resume`.
6. **Unsupported markets are skipped.** That covers markets with a matching delay and Protocol V2
   markets.

The trading code has been tested offline against a fake exchange. **It has not placed a real
order yet.** To go live safely:
1. Run in dry run for a while.
2. Run `pmscan trade status` to check the connection.
3. Go live with the default $25 / $100 caps.
4. Check `pmscan trade log` against your Polymarket history before raising the caps.

Polymarket marks most markets as restricted. Whether you may trade depends on where you live, so
check their terms first.

## Fees

Taker fee = `shares × rate × p × (1 − p)`. Each market reports its own rate, and the scanner reads
it live. That includes the new rates that take effect on November 20, 2026 (for example 0.05 for
sports and 0.01 for most crypto). Makers pay nothing.

## Configuration

- Copy `pmscan.example.toml` to `pmscan.toml`; it's picked up automatically.
- Put secrets in `.env`, never in the TOML.
- An unknown key in the config is an error, so typos don't pass silently.

## Layout

```
pmscan/
  client.py            Gamma + CLOB HTTP client (retries, keyset paging, batched books)
  parse.py, models.py  raw JSON -> Market / Event / OrderBook / Opportunity
  books.py             order-book math: sized fills across one or more books
  strategies/          neg_risk_arb, ladder_arb, high_prob, near_certain, sportsbook (screen -> evaluate)
  odds.py              The Odds API client, de-vigging, team matching
  alerts.py            Telegram / Discord notifications with cooldowns
  trading.py           AutoTrader, ledger, SDK exchange wrapper
  scanner.py           one full scan
  cli.py, report.py    commands and terminal tables
  rewards.py           Polymarket US liquidity rewards: scoring, ranking, paper test
  web.py               dashboard server + JSON API
  static/app/          dashboard app (Preact + htm ES modules, hand-built SVG charts)
  static/vendor/       vendored Preact and htm
macos/                 native Mac app (SwiftUI shell, build.sh, icon)
tests/                 offline tests (uv run pytest)
```

To add a strategy, subclass `Strategy`, implement `screen` and `evaluate`, register it in
`ALL_STRATEGIES`, and add a config section with the same name.

## Ideas for next steps

- **WebSocket order books** for markets near an edge, so the bot reacts in seconds rather than a
  full scan cycle.
- **Auto-unwind** of a partial fill: sell the filled legs back with a price floor, instead of
  only halting.
- **Spread ladders.** "Colts −6.5" implies "Colts −3.5". This is like the totals ladders, but the
  team side has to be matched to the outcome.
- **Track record.** Use `history.jsonl` to measure how long edges last, and which strategies
  would actually have paid.
