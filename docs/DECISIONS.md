# Technical decisions

Each entry covers the problem, what was chosen, and what it cost. Most of these changed after
looking at real data, so several record a first approach that turned out to be wrong.

---

## 1. Screen cheaply, then verify against real order books

**Problem.**
- A scan covers about 25,000 markets.
- Fetching every order book takes minutes and runs into rate limits.
- Top-of-book quotes from the event listing are free, but they're only the best price for a
  tiny size.

**Decision.** Split every strategy into two phases.
- `screen` uses only the listing's quotes and returns the tokens worth checking.
- The scanner merges the candidates from all strategies, removes duplicates, fetches those books
  in one parallel batch, and then calls `evaluate` with the real books.

**Trade-off.**
- A strategy's screen has to be generous enough that it never drops a real opportunity, and the
  evaluate step is the only place that decides.
- Two strategies that need the same book (90¢+ and near-certain overlap) share one fetch.

---

## 2. Price everything by walking the book, fees included

**Problem.**
- Midpoint or best-ask math overstates edges.
- A neg-risk basket that looks 3% cheap at the top of the book is often 0.2% cheap after 50
  shares, and negative after taker fees, which are a curve in p(1−p) and largest at 50¢.

**Decision.**
- `books.walk_basket` buys equal share counts of every leg, level by level, applying the fee
  per fill.
- It stops at the first unit that would no longer clear the target cost.
- Every reported opportunity carries the size it's good for, its worst price per leg, and its
  cost with fees.

**Trade-off.**
- The result is more conservative than other scanners. Many "arbitrages" seen elsewhere never
  show up here.
- That was the point: the paper trader and the live trader use the same function, so what's
  shown is what a real order would get.

---

## 3. One model for two venues

**Problem.**
- Polymarket US launched with a different API: different IDs, prices quoted as YES only, and
  NO bought as a short.
- It also has its own fee schedule and its own WebSocket protocol.

**Decision.**
- `us.py` adapts the US API into the same `Event`, `Market`, `OrderBook` and `FeeSchedule`
  types.
- A NO book is derived as the mirror of the YES book.
- Order placement sits behind a small exchange interface.
- Data directories are per venue.

**Trade-off.**
- A few places still branch on venue: categories, fee formulas, and US-only features such as
  liquidity rewards.
- In exchange, all five strategies, the backtester, paper trading and the dashboard worked on
  the new venue without changes.

---

## 4. Let the backtest decide what runs

**Problem.** Plausible strategies are easy to write and usually lose money after fees. Two
examples:
- **Long shots:** buying contracts under 20¢.
- **Bonds:** buying favorites at 90¢ or more.

**Decision.**
- Build `backtest.py` before trusting any statistical strategy. It samples 10,000 settled
  markets across 10 categories.
- It replays their hourly price history and enters at 1 hour, 6 hours, 1 day, 3 days and
  7 days before the end, using the price actually available then.
- It settles each entry with the recorded result and charges today's fees.
- Results carry Wilson 95% intervals. A category counts only when its interval clears the
  break-even win rate.

**What it changed.**
- Player props and game-winner markets were excluded from the 90¢+ strategy (−2% to −6% per
  dollar).
- The long-shot idea was dropped, at −50% to −70% per dollar.
- The near-certain strategy is limited to politics, the only category that cleared break-even.

**Trade-off and honesty.**
- 904 of the 1,000 politics markets in the sample settled on one election night, so they aren't
  independent.
- The dashboard says so next to the numbers, and the paper portfolio is treated as the real
  test.

---

## 5. Measure the spread at a real order size

**Problem.**
- The first 90¢+ paper portfolio lost $721 in its first days.
- About half was genuine price moves.
- The rest came from buying in thin, wide books, where a position is immediately worth far
  less than it cost.
- Re-running the backtest with the spread at entry showed it was the strongest single filter:
  - at a spread of 2¢ or less, bets won 98.7–99.4% (+1.3–1.9% per dollar);
  - above 5¢, they won 59–91% (−7% to −40% per dollar).

**First fix, and why it failed.**
- Requiring a top-of-book spread of 2¢ or less let through 26 of 118 new positions that were
  6–9¢ under water.
- Their books were empty except for dust orders of a few cents at 1¢ and 99¢, which made the
  spread look tight.

**Decision.** The spread is measured at a $50 order size: the worst bid and ask price reached
when trading $50 each way (`_tight_book`).

**Lesson.** A filter has to be measured the way a trade would experience it, not from the
cheapest number the API offers.

---

## 6. Cap bets per event

**Problem.** One event can list dozens of related markets: playoff qualifiers, a golf
tournament's winner, a conference championship. One surprise, such as a rankings upset, can
sink several bets at once, so 30 bets in one event don't spread the risk 30 ways.

**Decision.** At most 3 open bets per event (`max_bets_per_event`), keeping the best-scoring
ones. Positions record their event, and older positions fall back to a lookup.

**Trade-off.**
- It's conservative for winner-takes-one events, where "No" on 30 golfers can lose at most
  one bet.
- A single simple rule was preferred to modelling correlation per event type.

---

## 7. A separate paper portfolio per strategy

**Problem.**
- A single shared paper account hides which strategy made or lost money.
- Optimistic fills, such as at the midpoint or the last price, make every strategy look good.

**Decision.**
- Each strategy gets its own $10,000 book and its own equity curve.
- Fills walk the live book with fees.
- Positions are valued at the best bid and settle at the official resolution.
- Arbitrage baskets are counted as one bet.
- Resets archive the old portfolio rather than deleting it.

**Trade-off.** Valuing at the bid makes every new position start slightly negative. The
dashboard explains this, and the win rate is shown against the rate the prices implied, which is
the real measure.

---

## 8. Layered safety for live trading

**Problem.** Arbitrage legs are separate orders. If the book moves between legs, the bot holds a
naked position instead of a hedge.

**Decision.**
- Dry run by default, opt-in strategies, and per-trade and per-day caps.
- The books are re-fetched and re-sized right before trading.
- All legs are signed first and sent together as Fill-or-Kill orders with a maximum price.
- If only some legs fill, they're sold back automatically, and trading halts until a person
  resumes it if that fails.
- Every attempt is appended to a ledger.
- Each rule has its own test.

**Trade-off.** Some real opportunities are skipped because the re-check is strict. Missing a
trade costs nothing, while a half-filled one can lose money.

---

## 9. Credentials

**Decision.**
- Secrets come only from environment variables or a git-ignored `.env`, never from
  `pmscan.toml`.
- The Mac app keeps them in the macOS Keychain and passes them to the child process through its
  environment.
- On polymarket.com the bot is meant to use a **session key**, a separate key the account
  authorizes and can revoke, never the wallet's main key.
- Read-only features work without any key.

**Trade-off.** Setting up a session key takes an extra step, which the app guides the user
through.

---

## 10. No build step, almost no dependencies

**Decision.**
- **Runtime:** `httpx` and `rich`.
- **Server:** Python's `ThreadingHTTPServer`.
- **Dashboard:** Preact and `htm`, vendored as ES modules, with hand-written SVG charts.
- **Mac app:** built with `swiftc` from a shell script.

**Why.** The tool runs unattended on a laptop. Fewer moving parts means fewer things break on an
OS or package update, and there's no node toolchain to keep in step.

**Trade-off.**
- No type checking for the JavaScript, and no hot reload.
- Charts are about 450 lines of custom code instead of a library, but they match the design
  exactly, support dark mode, and have table views.

---

## 11. Watch live only where it matters

**Problem.** A full scan every couple of minutes is too slow for arbitrage that lasts seconds,
but streaming every market is wasteful.

**Decision.**
- During screening, the arbitrage strategies report how close each event is to an edge.
- After each scan, the live feed subscribes to the 40 closest events only.
- When a streamed quote crosses into an edge, that event goes through the normal book fetch and
  evaluate path.

**Trade-off.**
- An event far from an edge can only be caught by the next scan.
- But no second pricing path exists that could disagree with the scanner.
