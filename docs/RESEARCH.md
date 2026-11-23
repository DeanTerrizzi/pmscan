# Research notes

These are findings from backtesting and live measurement on Polymarket US, November 2026. All
returns are per $1 staked, after taker fees, unless noted.

## Method

- **Sample:** 10,000 settled markets across 10 groups:
  - sports: game winners, soccer three-way, season futures, props and other;
  - politics, crypto, culture, finance, macro, and weather.
  - Each group was sampled at random offsets, because props and 15-minute crypto markets
    would otherwise make up most of the more than a million settled markets.
- **Prices:** hourly price history for each market. The YES entry price is the ask; the NO
  entry price is 1 minus the bid. Only points where the price changed are kept.
- **Entries:** 1 hour, 6 hours, 1 day, 3 days and 7 days before the end.
  - For game markets, the end is the game's start, since the strategy doesn't buy during play.
  - The end is never later than the last price change: after settlement a frozen quote looks
    like a free win that was never really for sale.
- **Statistics:** Wilson 95% intervals on win rates, compared with the break-even win rate
  implied by price plus fee. Where the interval crosses break-even, the result is reported as
  "can't tell yet" rather than as a win or a loss.
- **Checks:** splitting the sample into two time halves, and keeping each market only once,
  so one market can't count several times at different horizons.

## Findings

### Favorite–long-shot bias is strong, and fees take the rest

| Entry price | Result per $1 |
|---|---|
| Long shots, 1–15¢ | −50% to −70%, in nearly every category |
| Taker, 20–80¢ | −2% to −20% (fee plus spread) |
| Favorites, 90–99.5¢ | about 0 overall (+0.24% at 1 day before the end) |

Fading long shots (buying the favorite against them) is the same trade as buying 90¢+
favorites, and only politics held up.

### 90¢+ by category

- **Politics:** +0.9% to +1.5% per dollar when bought 1 hour to 3 days before the end.
- **Season futures:** +0.3% to +0.9%.
- **Player props:** −2% to −4%.
- **Game winners:** −5.6% at 1 hour before the game.

**Action taken:** props and game winners were excluded, and the live 90¢+ list was narrowed to
politics and season futures.

### Near-certain bets (97–99.5¢, final 3 days)

- **Politics:** 819 of 823 won, +0.84% per dollar.
- **Every other group:** about 0 or negative; props and game winners −6% to −9%.

**Caveat:** 904 of the 1,000 politics markets settled around one election night, so they aren't
independent trials. If the true loss rate were 1 in 100 rather than 4 in 823, 99¢ bets would lose
money. This is shown on the dashboard next to the numbers.

### Spread at entry is the strongest filter

Politics and futures entries, 1 to 21 days before the end:

| Spread at entry | Won | Result per $1 |
|---|---|---|
| 2¢ or less | 98.7–99.4% | +1.3% to +1.9% |
| More than 5¢ | 59–91% | −7% to −40% |

**Action taken:** 90¢+ and near-certain bets require a spread of 2¢ or less, measured at a
$50 order size (see [decision 5](DECISIONS.md#5-measure-the-spread-at-a-real-order-size)).

### Things that didn't work

- **Resting limit orders (maker).** Fills suffer adverse selection: filled bids won 89% of the
  time against 98% for taker entries at 1 hour. They were only slightly better for politics and
  futures 1–3 days out.
- **Momentum and reversal.** No edge after recent price moves in either direction. Long shots
  that had just risen were *less* likely to come true, so a "news is moving it" filter would
  have hurt.
- **Cross-venue arbitrage.** The median price gap was 0.5¢ between Polymarket US and
  polymarket.com, and 0.8¢ against Kalshi. There were essentially no arbitrages after fees on
  both sides.
- **Calibration rules per cell.** Rules fitted per category and price bucket overfit: almost
  none held up on the other time half.
- **Crypto daily markets.** These were within about 2¢ of a lognormal model, and the deviations
  look like crash skew rather than mispricing.

### Liquidity rewards

Polymarket US pays reward pools for keeping orders on the book.

- **The pools are smaller than the API suggests.** The API repeats each program's pool on every
  market. The pool is actually shared across the program, so the real per-market amount is
  much smaller.
- **Paper test:** resting $50 quotes, followed against real trades.
  - A quote on a live tennis match lost $17 against $0.01 of rewards, picked off as soon as the
    price jumped.
- **Action taken:** live-game periods and markets that track fast-moving prices (crypto, metals,
  fuel) are excluded by default.

## How the conclusions are being tested

Six paper portfolios run continuously with realistic fills:
- 90¢+ positions;
- near-certain bets;
- neg-risk arbitrage;
- ladder arbitrage;
- sportsbook value;
- the liquidity-rewards quotes.

Each one reports its realized win rate against the rate the entry prices implied. A strategy has
an edge only if it beats that implied rate over many independent bets. The backtest alone
can't show this, because so much of its politics data is one election.
