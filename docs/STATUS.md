# Status

Claude updates this file at the end of every phase. It is the memory between sessions: read it first.

## Summary

| Phase | Name | State | Tests | Finished |
|---|---|---|---|---|
| 1 | Data feed and quality gate | Built; live Breeze run pending | 18 passing | 2026-11-11 |
| 2 | Market-regime classifier | Built and tested in sim mode; review 1 fixes, review 2 findings 1-3 and review 3 findings 1-2 applied; live history works (user); live subscriptions confirmed in the SDK log, live ticks not yet seen | 184 passing (whole suite) | 2026-11-11 |
| - | Nifty 50 watchlist (task before phase 3) | Built; 50 of 50 mapped; live history downloaded; 53 live subscriptions not yet seen | 212 passing (whole suite) | 2026-11-12 |
| 3 | Intraday and swing strategies with backtests | Built and tested; backtests run on real Breeze data (all five lose after costs); costs not yet verified against a contract note | 259 passing (whole suite) | 2026-11-12 |
| 4 | Option strategies | Not started | | |
| 5 | Risk engine | Not started | | |
| 6 | Live signals and alerts (paper mode) | Not started | | |
| 7 | Journal and weekly scorecard | Not started | | |
| 8 | LLM layer and go-live gate | Not started | | |
| 9 | Stock option selling | Not started | | |

## Phase 1

**Built:** config with % limits, simulated feed, read-only Breeze feed, quality gate, 1-minute bars in SQLite,
local price page, safety tests.

**Tested:** quality rules, bar building, config validation, Breeze message parsing, HTTP routes, safety checks.
App run end to end in simulated mode.

**Not tested:** live Breeze login, subscription and tick flow (needs the user's credentials).

**Known gaps:** ICICI stock codes in `config.yaml` are unverified. The holiday calendar gap is closed in phase 2.

## Phase 2

**Built:**
- `app/history.py`: daily and 5-minute history for every watchlist symbol into `bars_1d` / `bars_5m`
  (with a `source` column, so sim and Breeze data never mix). Breeze source via `BreezeFeed.historical()`.
  Deterministic sim source. Bad rows go to `quality_events` as `BAD_BAR`. Incomplete bars (today before the close,
  a forming 5-minute bar) and holiday-dated bars are not stored. Requests are chunked, updates are incremental,
  one failing symbol does not stop the others. Runs in the background at start in breeze mode, or by hand with `python -m app.history`.
- `app/indicators.py`: SMA, EMA, true range, ATR (Wilder), daily returns, percentile, breadth.
- India VIX added to the watchlist as `INDIAVIX` (Breeze code `INDVIX`, unverified).
- `app/regime.py`: `classify()` (pure, versioned `regime_v1/<threshold hash>`), `fii_bias()`, and `RegimeService`,
  which gathers inputs from the store, the hub (LIVE prices only) and the calendar. All thresholds are in `config.yaml` -> `regime`.
  Outside market hours it classifies on the last close for the next trading session.
- `app/calendar.py` and `data/holidays.csv`: holidays make the hub show `CLOSED` and are skipped when finding previous sessions.
- `app/manual_data.py` and `data/events.csv`, `data/fii_dii.csv`: all-or-nothing CSV parsing. The NSE FII/DII and holiday
  table layouts are accepted.
- `GET /api/regime` (regime, reasons, bias, values, history) and `POST /data/import/{fii_dii|events|holidays}`.
- Dashboard: regime and bias badges, reasons, history table, and a CSV paste box.
- `regime_log` is written only when the regime, bias, basis or rules version differs from the last stored entry for that session
  (compared with the database, so a restart adds no duplicate). A background thread re-evaluates every 60 s.
- `tests/test_safety.py`: the "no write routes" check was replaced with the allowlist `/journal/`, `/data/import/`, `/capital/`
  (the one change permitted by architecture.md section 10). All other safety checks are unchanged.

**Tested (offline):**
- Indicators against hand-calculated values.
- Each regime from fixture data: TRENDING_UP, TRENDING_DOWN, RANGE (two ways), VOLATILE (VIX and day range, plus the exact 1.5x boundary), EVENT.
- An event day forces EVENT; a stock-only RESULTS row does not.
- UNKNOWN for each missing input: VIX price, VIX history, NIFTY history, NIFTY price, day range, breadth coverage,
  unreadable events file, outdated history, stale ticks, no history at all.
- Determinism: repeated and deep-copied inputs give identical results.
- FII bias: bearish, bullish, neutral, missing days, and holidays skipped.
- History: IST to UTC parsing, bad-row quarantine, completeness, chunking, incremental update, sim determinism.
- Manual data: both CSV layouts, failed imports changing nothing, FII merge.
- Holidays show CLOSED.
- API routes, the dashboard badge, and the safety allowlist.
- App started with `python -m app.main` in sim mode: `/api/regime` returned RANGE with reasons, and the page shows the badge.

**Not tested (needs the user's Breeze login):**
- `BreezeFeed.historical()` against the real API, including the row format (assumed `datetime` as IST
  `YYYY-MM-DD HH:MM:SS`; open/high/low/close/volume) and the candle limit per call (chunks are 10 days for 5-minute bars).
- India VIX live subscription with code `INDVIX`.
- The live tick's day `high`/`low` fields. If they are absent, the day range falls back to ticks seen since the app started,
  and the badge says so.

**Known gaps:**
- `data/holidays.csv`, `data/events.csv` and `data/fii_dii.csv` are empty. No real dates were invented. Until holidays are
  filled in, the session after a holiday shows UNKNOWN ("history ends ...; add the holiday").
- Expiry dates are not generated automatically. The user adds them to `events.csv`, and phase 4 reads expiries from the instrument list.
- Special sessions (for example Muhurat trading) are not modelled.
- `fii_bias` is shown but not yet used by any strategy (phase 3+).

## Review fixes (independent review of phases 1-2, 2026-11-11)

All five findings were confirmed and fixed, with regression tests in `tests/test_review_fixes.py` and `tests/test_safety.py`.
- **Non-finite prices:** the quality gate rejects a tick whose last, bid or ask is NaN or ±inf.
  History rows, Breeze tick parsing, `prev_close` and the feed's day high/low use the same rule.
- **Invalid CSV files:** a file that fails to parse is not applied. The last good copy in SQLite stays in force; holidays are never emptied.
  The regime is UNKNOWN, with a reason naming the file and row, until the file is fixed (edit it and restart, or re-import events/holidays).
- **One SQLite file per feed mode:** `data/market_sim.db` and `data/market_live.db`, chosen from `feed.mode`.
  The live store refuses any row whose source is `sim`, and a sim feed cannot start with `feed.mode: breeze`. `python -m app.history` always writes to the live file.
  The old `data/market.db` holds only sim data (2,288 daily and 4,200 five-minute sim bars, 1 regime row, no 1-minute bars, no live rows).
  It is no longer used and was not deleted.
- **Division safety:** a zero, missing or non-finite ATR, average or VIX percentile gives UNKNOWN. Non-finite or non-positive regime inputs,
  day high below day low, and bad history values give UNKNOWN. Breadth and daily returns treat non-finite values as missing.
  Every other division was audited and is guarded.
- **Write routes:** every non-GET/HEAD request needs Host `127.0.0.1:<port>` or `localhost:<port>`. A matching Origin is required if one is sent,
  and Content-Type must be `application/json`. Otherwise the reply is 403 or 415. `POST /data/import/{kind}` now takes `{"text": "<csv>"}`, and the dashboard paste box sends JSON.

**Not verified:** finding 1 was reproduced before fixing. The other new tests were not run against the pre-fix code, because there is no git snapshot.
They fail on it by construction: they use names that did not exist (`SimDataInLiveStore`, `write_request_problem`, `Settings.db_path()`), or the old behaviour
they check against (empty calendar, ZeroDivisionError, `text/csv` accepted).

## Review 2 fixes (2026-11-11)

Findings 1-3 were confirmed and fixed (open issues 14-16), with tests in `tests/test_review2_fixes.py`.
Before/after check: the new tests were run against a snapshot of the code taken before these fixes (the git commit plus the
uncommitted Breeze subscription fix). 22 of 43 cases failed there and all pass now. The 21 that already passed are NaN/inf
cases already caught by range limits, kept as regression coverage, and two guards against over-correction (a header-only CSV
is a valid empty list; zero or missing quotes are "no quote").
Findings 4-6 arrived as unfilled placeholders, so nothing was done for them.

## Review 3 fixes (2026-11-11)

Findings 1 and 2 were confirmed and fixed (open issues 18-19), with tests in `tests/test_review3_fixes.py`.
Before/after check: the new tests were run against the previous commit (`1b4e9f6`) in a temporary worktree. 3 of 4 failed there
and all pass now. The one that passed is a guard against over-correction (a real regime change after a restart is still logged).
- **SDK log files in git:** `logs/apiLogs.log` and `logs/websocketLogs.log` (written by breeze-connect) were committed in `1b4e9f6`.
  They were removed from tracking with `git rm --cached` and kept on disk; `.gitignore` now has `logs/` and `*.log`.
  They contain no API key, secret or session token (checked without printing values): only quote and history responses,
  instrument lookups (`isec_token` is an instrument code) and "subscribed" messages. The SDK's header logging is commented out.
  The log folder is hard-coded in the SDK as the relative path `logs` (current directory), with no option to change it;
  started from the project root it is already the git-ignored `logs/`. The files remain in the history of commit `1b4e9f6`.
- **Duplicate regime entries on restart:** the "last logged" check lived only in memory. It is now seeded from the newest stored
  entry for the session (`Store.last_regime`), and the rules version is part of the comparison.
- **FII/DII import (review comment, not changed):** a paste is still refused while `fii_dii.csv` itself is invalid. This is intended:
  the user repairs a broken file by hand, so a paste never builds on rows the app could not read.
- Finding 4 arrived as an unfilled placeholder, so nothing was done for it.

## Nifty 50 watchlist (2026-11-12, small task before phase 3)

**Built:**
- `app/watchlist.py` (pure functions): reads the niftyindices.com constituents file and maps each NSE symbol to its Breeze code.
  A symbol is mapped only when ICICI's NSE instrument list has exactly one series-EQ row with that symbol and the same ISIN.
  Otherwise it is listed as unmapped and left out. It writes only the lines between the `nifty50` marker comments in `config.yaml`,
  keeps the file's line endings, and validates the new config before writing it.
- `app/feeds/breeze_feed.py`: `nse_scrip_master()` reads the instrument list that breeze-connect downloads on import
  (`SecurityMaster.zip` -> `NSEScripMaster.txt`, the same file `get_names()` uses). No login and no order call.
  The SDK is still imported only in this file.
- `scripts/update_watchlist.py [--dry-run] [--csv ...]`: redoes the mapping and the sectors from a new `data/nifty50.csv`.
- `config.yaml`: 53 symbols. NIFTY, BANKNIFTY and INDIAVIX are unchanged; the 50 stocks have `sector` from the Industry column.
  **All 50 mapped; none left out.** The 5 stocks you verified earlier mapped to the same codes.
  Some codes keep a company's old name: ETERNAL -> `ZOMLIM`, TMPV -> `TATMOT`, TATACONSUM -> `TATGLO`, SHRIRAMFIN -> `SHRTRA`,
  INDIGO -> `INTAVI`. All of these were matched by ISIN.
- Config: `Instrument.sector` (optional) and checks that reject duplicate watchlist symbols or codes. New settings `feed.subscribe_pause_seconds` (0.25),
  `history.request_pause_seconds` (0.7), `history.empty_retries` (2) and `history.retry_pause_seconds` (3).
- Live feed: subscribes one symbol at a time with a pause between them. A token lookup failure, a subscribe that raises, or an SDK
  error string ("Exception while subscribing ...") counts as a failure, and the other symbols carry on. At startup it logs
  `Breeze live feed: subscribed N of 53 symbols` plus a line listing each failure with its code. The result is kept in `BreezeFeed.subscription_summary`.
- History: Breeze requests are paced, and an empty reply is retried, then reported as `empty` (not an error). A symbol/interval whose
  newest complete bar is already stored is skipped without a request; 5-minute bars are never skipped while the market is open.
  `python -m app.history [SYMBOL ...]` ends with a summary. The sim source is not paced (sim startup is not slowed).
- Breadth: the regime already used every `kind: stock` watchlist entry. That is now the 50 mapped stocks. The 60% / 40% / 80%-coverage thresholds are unchanged.
- Tests use `tests/fixture_settings.py`: the real config with a fixed 8-symbol watchlist. No test depends on the Nifty 50 list,
  except one that checks the real config's stocks all have a sector and that the 3 indices are present.

**Tested (offline):** CSV parsing (BOM, missing columns, duplicates, empty industry); mapping (EQ row chosen over a warrant row
with the same symbol, ISIN mismatch, ambiguous code, symbol not listed); ICICI header layout; YAML round trip of `M&M` and
`Metals & Mining`; block rewrite keeps the indices and the rest of the file, is idempotent, dry run writes nothing, an invalid
result (duplicate symbol) is refused and the file is untouched; paced subscriptions (sleep between, not before), summary log, three
kinds of failure with the others continuing, 53 subscriptions; history pacing, retry then success, empty after all retries,
skip when stored, new symbol fetched while stored ones are skipped, daily due again the next day; breadth over 10 fixture stocks at
exactly 60% gives TRENDING_UP and at 50% gives RANGE.

**Run here:**
- `python scripts/update_watchlist.py` against ICICI's live instrument list (public file): 50 of 50 mapped.
- `python -m app.history` (live Breeze, 2026-11-12 13:2x IST): 106 downloads, 36,340 bars saved, 8 daily downloads skipped as already
  stored, 0 failed, 0 empty. Every new stock got 268 daily bars. 5-minute bars were refetched for the original symbols because the market was open.
- App in sim mode with the 53-symbol config (port 8001, because port 8000 was in use): `/api/prices` lists 53 symbols; `/api/regime` gives
  RANGE with "breadth 50% of 50 stocks", coverage 100%; the page loads. Sim startup now takes about 23 s (fake history for 53 symbols).

**Not tested:**
- Live subscription of all 53 symbols. An app was already listening on port 8000, so a second live Breeze connection was not opened.
  Check the console for `subscribed 53 of 53 symbols` on your next breeze-mode start.
- Whether Breeze limits the number of symbols per websocket. 53 is assumed to be within its limit; any refusal will now show in the summary.

## Phase 3 (2026-11-12)

**Built:**
- **History (step 0):** 5-minute bars for at least 6 months (`history.intraday_days: 190`) and daily bars for 800 calendar days,
  in 10-day / 900-day chunks, with the same pause and retry rules. Stored data is one block per symbol, so only the part
  older than the first stored bar (backfill) and from the last stored day onward are fetched. If the source has nothing older,
  a `history_floor` row stops it being asked again. `python -m app.history` prints how far back each symbol goes and the data breaks.
  Breeze returns pre-open (09:00-09:10) and post-close (15:30-15:35) 5-minute bars: they are now dropped, and 13,788 stored ones were purged.
- **Data actually stored (live, 2026-11-12):** all 53 symbols have 5-minute bars from **2026-05-06 to 2026-11-12 (131 sessions)**
  and daily bars from **2024-09-04 to 2026-11-11 (540 sessions)**. No symbol is shorter. 0 failed, 0 empty requests.
- `app/models.py`: `Signal` (architecture contract plus `entry_zone`), validated on creation (`signal_problems`: enumerations,
  aware datetimes, finite prices, stop/target on the right side, `data_as_of` not after `created_at`). Ids are deterministic (uuid5).
- `app/strategies/base.py`: `Strategy` (id, version = base + hash of thresholds, horizon, interval, allowed_regimes) and
  `MarketContext`. The base refuses a non-LIVE status, a regime outside `allowed_regimes`, an index, and raises on a bar after `asof`.
- Strategies: `orb`, `vwap_pullback`, `range_reversion` (5-minute, intraday, long and short); `swing_pullback`,
  `swing_breakout` (daily, long only, delivery). Thresholds in `config.yaml` -> `strategies`.
- `app/costs.py` + `config.yaml` -> `costs`: Prime 999 brokerage and statutory charges for intraday equity, delivery equity
  and options, with slippage. Each rate has a source and an effective date. Decimal arithmetic, each charge rounded to the paise
  per leg. Empty rates raise `MissingCostRates`, naming them. The label "standard rates, not yet verified against a contract note"
  is on every report and on the page.
- `PointInTimeRegime` (`app/regime.py`): the regime at a bar's close from stored bars only (daily bars before the session,
  5-minute bars that had ended); `at_close(d)` for swing signals.
- `app/backtest.py`: next-bar-open entry, stop first, gaps through the stop fill at the open, 15:15 intraday exit, no entries
  after 14:45, risk sizing (1% at the stop, 25% position cap), costs per trade, a swing trade exiting on its entry day charged as
  intraday, walk-forward (fixed rules; last 20% of sessions an untouched holdout; ~1-month windows), reports to SQLite
  (`backtest_runs`, `backtest_trades`) and `data/backtests/*.json`.
- Report fields: period, trades, win rate, average win/loss, gross, costs, net P&L, max drawdown (Rs and % of capital), profit factor,
  by regime, by window, holdout, skipped-signal counts, peak exposure, data breaks, LOW SAMPLE below 30 trades (overall, per regime, per window).
- `GET /api/backtests`, `GET /api/backtests/{run_id}`, page `/backtests` (linked from the main page). All GET; the safety test passes.
- **Unadjusted corporate actions:** Breeze daily history is not adjusted. An overnight stock gap above 25%
  (`history.max_overnight_gap_pct`) is a break. Averages, ATR, breakouts and breadth (backtest **and the live regime**) use
  only bars after it. A swing trade open across a break is dropped and counted.
  Breaks found: RELIANCE and DRREDDY 2024-10-28, SHRIRAMFIN 2025-01-10, BSE 2025-05-23, BAJFINANCE 2025-06-16,
  NESTLEIND 2025-08-08, HDFCBANK 2025-08-26, TMPV 2025-10-14 (demerger), KOTAKBANK 2026-01-14, TRENT 2026-06-04
  (TRENT -34%: not confirmed as a corporate action; excluded anyway).
- `Store` waits up to 30 s for a lock (the running app writes to the same file).

**Cost rates used (Prime 999; checked 2026-11-12):**

| Item | Intraday | Delivery | Options | Source |
|---|---|---|---|---|
| Brokerage | 0.022% each leg | 0.22% each leg, no minimum | Rs 49 per lot each leg | icicidirect.com/brokerage, secure.icicidirect.com/customer/brokerageplans |
| STT | 0.025% sell | 0.1% buy and sell | 0.15% of premium, sell | Zerodha, Angel One, Navia (Finance Act 2026, from 2026-04-01) |
| NSE transaction | 0.00307% | 0.00307% | 0.03553% of premium | Zerodha, Angel One (NSE page did not load; ICICI page shows an older 0.00297%) |
| SEBI fee | Rs 10/crore | Rs 10/crore | Rs 10/crore | Zerodha, Angel One |
| Stamp duty (buy) | 0.003% | 0.015% | 0.003% | Zerodha, Angel One |
| IPFT | Rs 0.01/crore | Rs 0.01/crore | **empty** (sources disagree) | Zerodha, Angel One |
| GST | 18% on brokerage, NSE, SEBI, IPFT, DP | | | Zerodha, Angel One |
| DP charge | - | Rs 20 + GST per scrip sold | - | icicidirect.com/brokerage |
| Slippage (setting) | 0.05% per side | 0.05% per side | 0.5% per side | not a published rate |

ICICI's own statutory table is out of date (it still shows options STT 0.05% and futures 0.01%), so it was not used for statutory rates.

**Backtest results on real data (development period only; holdout untouched):**

| Strategy | Period | Trades | Win % | Net after costs (Rs) | Costs (Rs) | Max DD (Rs) | PF | Peak exposure |
|---|---|---|---|---|---|---|---|---|
| orb | 2026-05-06..2026-10-01 | 240 | 30.8 | -36,218 | 10,231 | 36,438 | 0.41 | 270% of capital |
| vwap_pullback | same | 2,789 | 34.5 | -274,478 | 118,720 | 276,346 | 0.31 | 582% |
| range_reversion | same | 1,666 | 32.1 | -152,623 | 70,960 | 153,182 | 0.25 | 663% |
| swing_pullback | 2024-09-04..2026-06-05 | 133 | 24.8 | -97,671 | 49,261 | 100,333 | 0.29 | 516% |
| swing_breakout | same | 76 | 23.7 | -58,967 | 27,747 | 58,967 | 0.36 | 370% |

All five lose before and after costs. The intraday strategies have tight stops (median 0.3-0.9%), so every position hits the
25% cap (Rs 50,000) and risks only about Rs 170, while charges (about Rs 43) and slippage (about Rs 50) take roughly half of that on every trade.
The rupee totals are inflated by overlapping trades (no portfolio limit until phase 5). The runs are deterministic: the same command gave identical results.
The point-in-time regime was UNKNOWN on 7 of 131 sessions (6 after market holidays missing from holidays.csv), so no trades on those days.

**Tested (offline, `tests/test_phase3.py`, 47 tests):**
- ACCEPTANCE: the signal bar's close is never the fill (fill = next open + slippage; no next bar or a gap = no trade; the strategy never
  sees an unfinished bar; a future bar in the context raises).
- ACCEPTANCE: a fixture trade reconciles to the paise (hand calculation in the test's docstring: net Rs 887.14).
- ACCEPTANCE: every strategy is silent in every regime outside its list, when not LIVE, and for an index (contexts found in sim
  data; a hand-built context for the volume breakout because simulated volume never spikes).
- ACCEPTANCE: the backtest regime uses only past data (rewriting every later bar leaves it identical; changing an earlier bar changes it).
- ACCEPTANCE: LOW SAMPLE below 30 trades; empty rates fail loudly and are named.
- Also: Signal schema, delivery and option charges, sizing, stop-first, gap through stop, short trades, 15:15 exit, last entry time,
  swing next-day entry and time exit, holdout untouched and saved reports, determinism, routes and page, backfill and floor,
  coverage report, out-of-session bars, data breaks (breadth and swing), exposure.

**Run here:** `python -m app.history` (live), `python -m app.backtest` (live data, twice: identical results; 30 s),
`python -m app.backtest --source sim`, and the app in sim mode on port 8001 (`/backtests` 200, `/api/backtests` lists runs, main page links to it).

**Not tested / known gaps:**
- Costs not compared with a real contract note (tracker P3.2 "Costs match a real contract note" is not done).
  Whether ICICI charges Prime intraday/option brokerage on both legs is assumed (the page does not say). Rounding per order vs per leg not checked.
- Options IPFT rate empty (conflicting sources); option backtests will refuse to run until it is filled.
- No corporate-action adjustment; history across a gap above 25% is not used. A real 25%+ move would also be excluded.
- No portfolio limits in backtests (phase 5); rupee totals overstate what one account could do.
- The holdout (2026-10-05..2026-11-12) has not been opened. It includes today's session, which was still partial when downloaded.
- Holidays in `holidays.csv` are not filled, so sessions after a holiday have an UNKNOWN regime (no trades).
- The app on port 8000 was running the old code during this work; restart it to get the new routes and the data-break rule in the live regime.
- Sim mode startup is slower (fake 190-day 5-minute history for 53 symbols: over 2 minutes the first time).

## Open issues

| # | Found in phase | Issue | State |
|---|---|---|---|
| 1 | 1 | Live Breeze connection never run | Waiting for user |
| 2 | 1 | Watchlist Breeze codes unverified | Fixed 2026-11-11: verified by the user against ICICI |
| 3 | 2 | India VIX Breeze code `INDVIX` unverified (`python scripts/lookup_code.py INDIAVIX`) | Fixed 2026-11-11: verified by the user |
| 4 | 2 | Live history download never run (`python -m app.history`) | Waiting for user |
| 5 | 2 | holidays.csv / events.csv / fii_dii.csv need real data from NSE, RBI and companies | Waiting for user |
| 6 | 1 | Review: NaN/inf last, bid or ask passed the quality gate (and history parsing) | Fixed 2026-11-11 |
| 7 | 2 | Review: an invalid holidays.csv emptied the calendar; invalid events/fii_dii files did not make the regime UNKNOWN | Fixed 2026-11-11 |
| 8 | 1 | Review: sim and live 1-minute bars shared one table keyed by symbol and minute | Fixed 2026-11-11 (separate files; old market.db holds sim data only, kept) |
| 9 | 2 | Review: zero or missing ATR caused a division error in the regime | Fixed 2026-11-11 |
| 10 | 2 | Review: write route open to cross-site requests | Fixed 2026-11-11 (Host/Origin check, JSON only) |
| 11 | 2 | Old `data/market.db` (sim data only) is no longer used | Waiting for user: delete it if you wish |
| 12 | 1 | Live run: every symbol logged "Unknown Breeze code ... skipped". In breeze-connect 1.0.69, `get_stock_token_value()` reads `self.interval`, which only `subscribe_feeds()` sets, and returns the AttributeError instead of a token | Fixed 2026-11-11: `BreezeFeed.start()` sets `interval = ""` before lookups and logs the SDK's actual return value on failure; tested with a fake SDK (`tests/test_breeze_feed.py`). The user's live run at 16:48 IST on 2026-11-11 logged "subscribed" for all 8 symbols (SDK log); ticks during market hours not yet seen |
| 13 | 2 | Tests that build the app read the real config.yaml, so they failed when `feed.mode` was switched to breeze | Fixed 2026-11-11: app tests pin sim mode |
| 14 | 1 | Review 2: config accepted +inf in fields with only a lower bound (`account.capital_inr`, `feed.max_jump_pct`); no capital ceiling | Fixed 2026-11-11: every config model rejects NaN/inf (`allow_inf_nan=False`); `capital_inr` <= Rs 100 crore |
| 15 | 2 | Review 2: a missing (or 0-byte) events/holidays/fii_dii CSV erased the last good data | Fixed 2026-11-11: treated as unloadable; last good copy kept; regime UNKNOWN naming the file ("file not found" / "no header row"). A header-only file is still a valid empty list. An FII/DII paste with the file missing merges into the stored days |
| 16 | 1 | Review 2: negative bid, ask, cumulative volume or previous close passed the quality gate; a negative bid with a zero ask was hidden by the Breeze parser as "no quote" | Fixed 2026-11-11: quarantined as BAD_PRICE / BAD_VOLUME / BAD_PREV_CLOSE (non-finite values too); the parser passes negatives through to the gate |
| 17 | 2 | Review 2: findings 4-6 were placeholders in the request, not real findings | Waiting for user: paste the text |
| 18 | 1 | Review 3: breeze-connect log files (`logs/apiLogs.log`, `logs/websocketLogs.log`) were tracked in git | Fixed 2026-11-11: untracked (kept on disk), `logs/` and `*.log` ignored; no credentials in them |
| 19 | 2 | Review 3: restarting the app added a duplicate `regime_log` entry | Fixed 2026-11-11: compared with the last stored entry for the session |
| 20 | 2 | Review 3: finding 4 was a placeholder in the request | Waiting for user: paste the text |
| 21 | - | Live start with 53 subscriptions not yet seen (port 8000 was busy during the change) | Waiting for user: restart the app in breeze mode and check the summary line |
| 22 | 2 | Sim startup is slower with 53 symbols (over 2 minutes the first time with 190 days of fake 5-minute history) | Open, low priority |
| 23 | 3 | Cost rates are standard published rates, not verified against a contract note | Waiting for user: compare one contract note |
| 24 | 3 | Options IPFT rate empty (sources disagree) | Waiting for user: fill from a contract note before phase 4 |
| 25 | 3 | Breeze history is not adjusted for corporate actions; gaps above 25% are treated as data breaks | Open: an adjustment source would let older history be used |
| 26 | 3 | All five strategies lose after costs on real data (development period) | Waiting for user: decide what to change before opening the holdout |
| 27 | 3 | holidays.csv has no rows: sessions after a holiday get an UNKNOWN regime | Waiting for user |

## Decisions log

| Date | Decision |
|---|---|
| 2026-11-11 | Manual execution only; no order code |
| 2026-11-11 | Option selling is hedged spreads only, for indices and stocks |
| 2026-11-11 | Capital does not block stock option selling signals; capital-fit label instead |
| 2026-11-11 | Starting capital Rs 2,00,000; all limits as % of capital |
| 2026-11-11 | Regime rule order: missing data (UNKNOWN) > EVENT > VOLATILE > TRENDING > RANGE. Missing data wins even on an event day |
| 2026-11-11 | Only market-wide events (blank symbol) of types in `regime.event_types` force EVENT; a stock's own results do not |
| 2026-11-11 | Breadth needs usable data for at least 80% of watchlist stocks (`breadth_min_coverage_pct`), else UNKNOWN |
| 2026-11-11 | After the close, the regime and bias are for the next trading session, based on the last close |
| 2026-11-11 | Manual CSV imports are all-or-nothing; FII/DII merges by date, events and holidays replace the list |
| 2026-11-11 | History tables carry a `source` column; sim history is never used in breeze mode |
| 2026-11-11 | Separate SQLite files per feed mode; the live file refuses sim rows (review finding 3) |
| 2026-11-11 | An invalid CSV file keeps the last good copy and makes the regime UNKNOWN; an FII/DII paste is refused while fii_dii.csv itself is invalid (fix the file). Kept on purpose after review 3 |
| 2026-11-11 | Write requests need local Host/Origin and JSON; GET and HEAD are unaffected |
| 2026-11-12 | Watchlist = NIFTY, BANKNIFTY, INDIAVIX + Nifty 50 stocks mapped by NSE symbol and ISIN from ICICI's instrument list; unmappable symbols are left out, never guessed |
| 2026-11-12 | Breadth uses every watchlist stock (now the Nifty 50); thresholds unchanged |
| 2026-11-12 | Brokerage plan: ICICI Direct Prime 999 (user). Statutory rates from Zerodha/Angel One/Navia because ICICI's statutory table is outdated and NSE's page did not load |
| 2026-11-12 | Stock overnight gaps above 25% are data breaks (no adjustment factors guessed), also in the live regime's breadth |
| 2026-11-12 | Pre-open and post-close 5-minute bars are not part of the session and are not stored |
| 2026-11-12 | The holdout is not simulated unless --holdout is given |
