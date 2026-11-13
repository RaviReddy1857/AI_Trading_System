# Status

Claude updates this file at the end of every phase. It is the memory between sessions: read it first.

## Summary

**Build order (changed 2026-11-13): 1, 2, 3 done; next 4, 5, 6, then 9 (stock option selling), then 7, 8.**

| Phase | Name | State | Tests | Finished |
|---|---|---|---|---|
| 1 | Data feed and quality gate | Built; live Breeze run pending | 18 passing | 2026-11-11 |
| 2 | Market-regime classifier | Built and tested in sim mode; review 1 fixes, review 2 findings 1-3 and review 3 findings 1-2 applied; live history works (user); live subscriptions confirmed in the SDK log, live ticks not yet seen | 184 passing (whole suite) | 2026-11-11 |
| - | Nifty 50 watchlist (task before phase 3) | Built; 50 of 50 mapped; live history downloaded; 53 live subscriptions not yet seen | 212 passing (whole suite) | 2026-11-12 |
| 3 | Intraday and swing strategies with backtests | Built and tested; backtests run on real Breeze data (all five lose after costs); costs not yet verified against a contract note | 259 passing (whole suite) | 2026-11-12 |
| 3b | Strategy revision (v2 versions, cost-aware stops, longer swing history) | Built and tested; rerun at Prime 4999 rates 2026-11-13: all v1 and v2 still lose after costs, none beats random; all set to no alerts, paper only, "negative in backtests"; holdout not opened | 300 passing (whole suite) | 2026-11-13 |
| 4 | Option strategies | Not started | | |
| 5 | Risk engine | Not started | | |
| 6 | Live signals and alerts (paper mode) | Not started | | |
| 7 | Journal and weekly scorecard | Not started | | |
| 8 | LLM layer and go-live gate | Not started | | |
| 9 | Stock option selling (built after 6, before 7) | Not started; plan extended 2026-11-13 | | |

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

## Phase 3 diagnosis (2026-11-12)

Question: all five strategies had profit factor 0.25-0.41. Is that a bug? Development period only; holdout not opened;
no parameters changed. Tools: `python scripts/diagnostics/diag.py` (random baseline, sample trades, exit reasons,
R-multiples, MFE/MAE, mechanical checks) and `python scripts/diagnostics/fwd.py` (raw forward returns, no engine).
Output goes to `data/diagnostics/` (not tracked).

**Verdict: no bug found.** The engine reran all five backtests with identical trades and net P&L.

Profit factor by cost treatment, real vs random entries (20 seeds; same symbols, sessions, direction, stop and target
distances, exit rules; swing: random TRENDING_UP days):

(The intraday "Random" figures in this table came from a biased baseline and are superseded; see the withdrawn line
below and the phase 3b results.)

| Strategy | No costs | Slippage only | Charges only | Both | Random, no costs (min-max) |
|---|---|---|---|---|---|
| orb | 0.70 | 0.52 | 0.54 | 0.41 | 0.99 (0.80-1.17) |
| vwap_pullback | 0.92 | 0.51 | 0.55 | 0.31 | 1.24 (1.08-1.33) |
| range_reversion | 1.00 | 0.48 | 0.52 | 0.25 | 0.57 (0.50-0.65) |
| swing_pullback | 0.58 | 0.53 | 0.31 | 0.29 | 0.75 (0.53-0.91) |
| swing_breakout | 0.60 | 0.57 | 0.38 | 0.36 | 0.71 (0.46-1.06) |

- ~~Worse than every random seed: orb and vwap_pullback.~~ **Withdrawn 2026-11-12 (phase 3b):** the intraday random
  baseline used here reused each real trade's own session and direction. For a breakout strategy that leaks the future
  (the session was picked because price later broke out that way), which flattered the random entries; `fwd.py`'s
  baseline had the same flaw. With baselines that use only information available at the time (phase 3b results),
  orb, vwap_pullback and range_reversion are all **inside** the random range: no worse and no better than random entries.
  The "no bug" verdict and the mechanical checks are unaffected.
- range_reversion beats random clearly before costs, but its stops (median 0.20% of price) are smaller than costs can
  carry: costs take 0.93 of the risk per trade (vwap_pullback 0.65, orb 0.21, swings about 0.3).
- Both swing strategies are within the random range; random long entries on TRENDING_UP days also lost in this period.
- Mechanical checks on every real and random trade (about 100,000), 0 failures: stop and target on the correct sides
  of the signal close and the raw entry; no entry beyond its stop; entry on the bar after the signal; slippage applied
  once per side, always adverse, at the configured rate; stop exits never better than the stop (gaps fill at the open);
  target exits exactly at the target; no bar between entry and exit touched the stop or target before the exit bar.
- Exits on the entry bar: orb 0, vwap_pullback 127, range_reversion 161, swings 3 each (random entries: about the same).
- Exit reasons: orb 59% end of day, 36% stop, 5% target; vwap 56/32/12 (stop/target/EOD); range 60/32/8;
  swing_pullback 53/11/37 (stop/target/time); swing_breakout 58/17/25.
- Mean R before costs: orb -0.15, vwap -0.05, range -0.02, swing_pullback -0.23, swing_breakout -0.29.
  MFE/MAE (R): orb 0.66/0.70, vwap 0.97/0.94, range 1.15/1.03, swing_pullback 0.84/0.94, swing_breakout 0.91/1.10.
- Data finding (does not affect backtests): Breeze's 15:20 five-minute bar is unreliable (zero volume in about half
  of sessions, open more than 0.5% from the previous close in about 2,000 sessions); the 15:15 bar is missing in about
  47% of sessions. Trades exit at the 15:10 bar's close and enter no later than 14:45, so they never use these bars.

## Phase 3b: strategy revision - plan (recorded 2026-11-12, before any phase 3b backtest was run)

v1 strategies stay unchanged (same ids and versions) for comparison. New versions are new strategy ids with
base version 2. Nothing below was chosen by looking at backtest results; no parameter is fitted.

**Round-trip cost estimate (rule 1).** Charges for one intraday round trip on a position at the position cap
(25% of capital = Rs 50,000; with a stop of about 1% the cap always binds, so this is the actual trade size), as a
% of price, plus slippage on both sides. Computed by `app/costs.py` from config.yaml at start-up, not hard-coded.
At current rates: charges 0.0874% + slippage 0.10% = **0.187%**. 5x = 0.94% of price; 2x = 0.37%.

1. **Cost-aware stops (all intraday v2):** a signal whose stop distance is below
   `min_stop_cost_mult` (5) x the round-trip cost estimate is skipped. Sizing is unchanged (1% risk at the stop,
   25% position cap).
2. **range_reversion_v2:** v1 entry logic (previous close outside the 20-bar 2-sd band, latest close back inside).
   Stop = whichever is further from entry: the v1 stop (3-bar extreme plus 0.10%) or 5x round-trip cost.
   Target = the band average (as v1). A signal whose target is less than `min_target_cost_mult` (2) x round-trip
   cost from entry is skipped. v1's reward/risk minimum is dropped (user decision, option 2 plus the target rule).
   RANGE regime only. Counts of signals removed by each filter are reported.
3. **vwap_pullback:** retired from cash trading. Code kept; listed in `strategies.inactive`, so it is not run by
   default (it can still be run by name).
4. **orb_v2:** 30-minute opening range (09:15-09:45). Signal on a 5-minute close beyond the range whose volume is
   above 1.5x the average volume of the same time-of-day bar over the previous 20 sessions (no signal until 20 prior
   sessions exist); the first such bar of the day only. Stop at the opposite side of the range; subject to rule 1.
   Carried over from v1 unchanged: long only on TRENDING_UP, short only on TRENDING_DOWN; target 2x risk; no signal
   after 11:30. Not carried over: v1's 0.05% breakout buffer and its 1.5% maximum range width (the request defines
   the entry as any close beyond the range, and the width cap would conflict with the 0.94% minimum stop).
5. **Swing:** download daily history as far back as Breeze provides (`history.daily_days` raised; a recorded floor
   stops repeat requests). **swing_pullback_v2 / swing_breakout_v2** = v1 plus a filter: NIFTY's close on the signal
   day above its 50-day average. v1 and v2 are both rerun on the longer history.
6. **15:15 and 15:20 five-minute bars** are ignored by the regime calculation (kept in storage). Note: the live
   regime (`RegimeEngine`) never reads stored 5-minute bars (it uses live ticks and daily bars); the only regime code
   that reads them is the backtest's `PointInTimeRegime.at_bar`, which now skips bars starting at the times in
   `regime.ignore_5m_bar_starts`. Trades already never used them, so backtest results do not change because of this.

**Holdout kept fixed.** With longer swing history, "the last 20% of sessions" would move the swing holdout back
over 2024-2026, which v1 was already developed on. So the holdout starts are frozen at today's values:
5-minute strategies from **2026-10-05**, daily strategies from **2026-06-08** (`backtest.holdout_start`). The
development period extends backward only. The holdout is still not simulated.

**Swing test period starts 2019-01-22 (added after the download, still before any phase 3b backtest).** Breeze's daily
history now reaches 2014-10-27 (4,400 calendar days requested; later listings start at their listing date). Checking it
before use found price errors:
- On 2015-10-01, 2015-11-11, 2016-01-19, 2016-12-20, 2017-07-10 and 2019-01-21 many stocks have one day at a different
  price scale that reverts the next day (e.g. TCS 1,292 -> 2,599 -> 1,350 around 2015-10-01). Most jumps are over 25% and
  become data breaks, but on 2017-07-10 a dozen are 4-10% (ASIANPAINT +9%, SHRIRAMFIN +10%, SBIN +7%), which the break
  rule cannot see and which would look like breakouts.
- INDIAVIX is about 100x off on 2015-11-11, 2015-11-13, 2016-02-25 and 2017-01-03.
- NIFTY shows no such errors (its large 2016 and 2020 moves are real).
So daily strategies are tested from 2019-01-22 (`backtest.test_start`), the day after the last bad date: about 7.4 years
of development period. Earlier bars stay stored and still feed the averages of early-2019 signals (the break rule
applies). Breaks found after 2019 are real corporate actions or single-stock errors (listed by `python -m app.history`).
History requests now time out after 60 s (`history.request_timeout_seconds`): one request hung for over 30 minutes.

**Same data for v1 and v2.** Holidays from `data/holidays.csv` (filled by the user; 2026 only) are loaded before
each backtest run, so v1 is rerun too. Older years have no holiday rows, so sessions after a 2015-2025 holiday still
get an UNKNOWN regime (no trades those days).

**Evaluation:** development-period backtests of v1 and v2, plus the 20-seed random baseline for every v2
(`scripts/diagnostics/diag.py`). For swing v2 the random days also satisfy the NIFTY filter.

## Phase 3b: results (2026-11-12; development period only, holdout not opened)

**Correction found while running this:** the random baseline of the phase 3 diagnosis was biased for intraday strategies
(see the withdrawn line there). Fixed before the table below: a random intraday entry is now a random stock, session and
bar in the signal window where the regime at that bar allows the strategy, in the regime's direction (RANGE: the real
long/short mix), with a real trade's stop and target distances. v1 intraday baselines were rerun. Swing baselines were not
affected (random allowed-regime days of the same stock, long only).

Round-trip cost estimate 0.187% of price (5x = 0.94%, 2x = 0.37%) **at the Prime 999 rates used for this run**; with Prime 4999 (2026-11-13) it is 0.159% (5x = 0.80%, 2x = 0.32%) and the results below are not yet rerun. Intraday period 2026-05-06..2026-10-01 (104 sessions).
Swing period 2019-01-22..2026-06-05 (1,817 sessions). v1 rerun on the same data (2026 holidays loaded; longer daily history).

| Strategy | Version | Trades | Win % | Gross incl. slippage (Rs) | Charges (Rs) | Net (Rs) | Max DD (Rs) | PF net | PF no costs | Random PF no costs, mean (min-max of 20) | Random PF net, mean (min-max) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| orb | 1/db0cb746 | 242 | 31.0 | -25,780 | 10,316 | -36,096 | 36,316 | 0.41 | 0.71 | 0.96 (0.68-1.16) | 0.54 (0.37-0.66) |
| orb_v2 | 2/5fda4369 | 449 | 42.5 | -15,640 | 19,108 | -34,748 | 39,603 | 0.67 | 1.07 | 0.93 (0.75-1.25) | 0.54 (0.44-0.72) |
| range_reversion | 1/bcf1f9a1 | 1,799 | 32.1 | -89,104 | 76,625 | -165,729 | 166,288 | 0.25 | 0.99 | 1.00 (0.91-1.11) | 0.25 (0.22-0.27) |
| range_reversion_v2 | 2/4aa26e3b | 803 | 45.2 | -46,760 | 34,236 | -80,997 | 81,206 | 0.40 | 0.92 | 0.98 (0.79-1.16) | 0.41 (0.33-0.49) |
| vwap_pullback (retired, rerun by name in the diagnosis only) | 1 | 2,852 | | | | | | 0.31 | 0.93 | 0.99 (0.92-1.14) | 0.32 (0.29-0.36) |
| swing_pullback | 1/70681558 | 1,316 | 40.2 | +149,645 | 451,724 | -302,079 | 319,544 | 0.75 | 1.22 | 1.18 (1.02-1.28) | 0.73 (0.63-0.80) |
| swing_pullback_v2 | 2/a9ac026c | 1,311 | 40.0 | +138,399 | 450,079 | -311,680 | 329,145 | 0.75 | 1.21 | 1.21 (1.09-1.35) | 0.75 (0.67-0.85) |
| swing_breakout | 1/dfa9ac55 | 999 | 39.2 | +13,797 | 330,611 | -316,814 | 316,814 | 0.70 | 1.07 | 1.22 (1.08-1.35) | 0.77 (0.68-0.86) |
| swing_breakout_v2 | 2/682e0a81 | 998 | 38.5 | -10,545 | 330,516 | -341,061 | 341,061 | 0.68 | 1.04 | 1.23 (1.08-1.39) | 0.77 (0.67-0.86) |

"PF no costs" = no slippage and no charges. Every trade, real and random, passed the mechanical checks (sides, slippage,
exits, next-bar entry).

**Signals removed by each filter (development period):**
- orb_v2, rule 1 (stop < 5x cost): **252 removed**; 449 traded. Median stop 1.41% (v1 0.90%); costs 0.13R per trade (v1 0.21R).
- range_reversion_v2, target rule (average closer than 2x cost): **2,146 removed**; 803 traded. Rule 1 removes none here
  because the stop is widened instead: 797 of the 803 trades had their stop widened to 5x cost. Costs 0.20R per trade
  (v1 0.93R).
- swing_pullback_v2 and swing_breakout_v2, NIFTY above its 50-day average: **8 removed each**. The TRENDING_UP regime both
  swing strategies require already needs NIFTY above its 20- and 50-day averages (regime rule 4), so the filter is almost
  entirely redundant; the 8 come from a one-day difference in the averaging window.

**What it shows:**
- Rule 1 cut costs as intended (orb 0.21R -> 0.13R per trade; range 0.93R -> 0.20R). Range reversion's loss roughly
  halved and orb's profit factor rose from 0.41 to 0.67. **No version is profitable after costs, and none is outside the
  random range before costs**: orb_v2's 1.07 is inside random's 0.75-1.25.
- range_reversion_v2 exits 45% at end of day, 38% at target, 18% at stop: the wider stop is rarely hit and the average
  often not reached.
- Swing over 7.4 years: both make money **before** costs (PF 1.04-1.22), but so do random long entries on the same regime
  days (1.18-1.23): the gain is the market's drift in TRENDING_UP periods, not the entry rule. swing_breakout v1 is below
  every random seed before costs (1.07 vs 1.08-1.35). Delivery costs (about 0.77% of price per round trip with slippage,
  about 0.23R per trade) make every swing variant lose.
- Exposure is still uncapped (phase 5): peak open positions reach 2.7-6.9x capital, so rupee totals overstate one account.
- Walk-forward windows: no intraday version had a profitable window; swing 18-20 of 60-62 windows profitable.
- Diagnosis output: `data/diagnostics/` (not tracked); rerun with `scripts/diagnostics/diag.py` and `fwd.py`.

## Costs update (2026-11-13)

- Plan changed to **ICICI Direct Prime 4999** (user): delivery 0.10% (was 0.22%), intraday 0.010% (was 0.022%), options
  Rs 19 per lot (was Rs 49), each charged on both sides (the engine already charges both legs).
- NSE charges set from **NSE circular NSE/FA/73061** (27 Feb 2026, effective 1 Mar 2026; as given by the user, not fetched
  here): total NSE outflow per side, IPFT included - equity cash Rs 307 per crore (0.00307%), equity options Rs 3,553 per crore
  of premium (0.03553%). The exchange charge carries the whole figure and IPFT is 0 in all three segments.
  The cash figure already matched (0.00307%), but a separate IPFT of 0.0000001% was being added on top: corrected.
  The options IPFT gap is closed, so no rate is empty.
- Still labelled "standard rates, not yet verified against a contract note".
- Effect: intraday round trip on Rs 50,000 = Rs 29.55 in charges (was Rs 43.71); round-trip cost estimate with slippage
  0.159% of price (was 0.187%); delivery 0.606% (was about 0.77%). One option lot bought and sold (Rs 10,000 premium each
  side) = Rs 68.54. v1 strategy versions are unchanged; orb_v2 and range_reversion_v2 versions changed (2/5d1213de,
  2/e859fa76) because their cost floor moved. The backtests were not rerun.

## Rerun at Prime 4999 rates and strategy status (2026-11-13)

Development period only; holdout not opened; nothing tuned. Same data and rules as the phase 3b run; only the cost rates
changed (Prime 4999 brokerage; NSE charges per NSE/FA/73061). Round-trip cost estimate 0.159% of price (5x = 0.80%,
2x = 0.32%), so orb_v2 and range_reversion_v2 have new versions and different trade counts. Random baseline: 20 seeds,
information available at the time only (see phase 3b).

| Strategy | Version | Trades | Win % | Gross incl. slippage (Rs) | Charges (Rs) | Net (Rs) | Max DD (Rs) | PF net | PF no costs | Random PF no costs, mean (min-max) | Random PF net, mean (min-max) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| orb | 1/db0cb746 | 242 | 31.8 | -25,780 | 6,972 | -32,752 | 33,042 | 0.45 | 0.71 | 0.96 (0.68-1.16) | 0.59 (0.41-0.72) |
| orb_v2 | 2/5d1213de | 542 | 42.6 | -24,633 | 15,586 | -40,219 | 46,332 | 0.68 | 1.02 | 0.93 (0.72-1.14) | 0.58 (0.44-0.71) |
| range_reversion | 1/bcf1f9a1 | 1,799 | 33.7 | -89,104 | 51,794 | -140,898 | 141,499 | 0.31 | 0.99 | 1.00 (0.91-1.11) | 0.31 (0.28-0.34) |
| range_reversion_v2 | 2/e859fa76 | 1,027 | 46.7 | -61,613 | 29,575 | -91,188 | 91,511 | 0.43 | 0.90 | 0.96 (0.78-1.09) | 0.44 (0.36-0.51) |
| swing_pullback | 1/70681558 | 1,316 | 42.2 | +149,645 | 290,809 | -141,165 | 200,832 | 0.88 | 1.22 | 1.18 (1.02-1.28) | 0.85 (0.74-0.93) |
| swing_pullback_v2 | 2/a9ac026c | 1,311 | 42.0 | +138,399 | 289,754 | -151,355 | 211,023 | 0.87 | 1.21 | 1.21 (1.09-1.35) | 0.87 (0.78-0.98) |
| swing_breakout | 1/dfa9ac55 | 999 | 40.6 | +13,797 | 213,041 | -199,244 | 199,244 | 0.80 | 1.07 | 1.22 (1.08-1.35) | 0.89 (0.78-0.99) |
| swing_breakout_v2 | 2/682e0a81 | 998 | 39.9 | -10,545 | 212,980 | -223,526 | 223,526 | 0.78 | 1.04 | 1.23 (1.08-1.39) | 0.89 (0.78-1.00) |

- **Every version still loses after costs.** Lower brokerage cut charges by about a third (swing: Rs 452k -> 291k for
  swing_pullback) and raised every profit factor, but none reaches 1.0 after costs.
- Before costs: intraday versions are inside the random range (orb_v2 1.02 vs 0.72-1.14). swing_pullback v1/v2 equal random
  long entries on the same regime days (market drift, not the entry rule). swing_breakout v1 and v2 are **below every random
  seed** before costs (1.07 / 1.04 vs 1.08 minimum).
- Filters at these rates: orb_v2 rule 1 removed 159 signals (542 traded); range_reversion_v2 target rule removed 1,823
  (1,027 traded; 1,017 stops widened); swing v2 NIFTY filter removed 8 each.
- Mechanical checks: no problems on any real or random trade. Peak exposure 2.7-7.4x capital (no portfolio limits yet).
- Walk-forward: intraday 0-1 of 4-5 windows profitable; swing 20-23 of 60-62.

**Strategy status (config.yaml -> `strategies.status`; shown on the backtests page and in every report):**
every phase 3 cash strategy, v1 and v2 (orb, vwap_pullback, range_reversion, swing_pullback, swing_breakout, orb_v2,
range_reversion_v2, swing_pullback_v2, swing_breakout_v2) is set to `alerts: false`, `mode: paper_only`,
label **"negative in backtests"**. None turned positive after costs, so the alternative label "no edge vs random" was not
needed. `Strategies.alert_ids()` returns no strategy; phase 6 alerts must read it. Strategies without an entry also default
to no alerts and paper only.

**Other changes in this step:** backtests read `holidays.csv` directly and no longer write it to the database (the first
rerun failed with "database is locked" because the running app held the write lock); an invalid file falls back to the
holidays the app stored. A leftover background wait-loop from phase 3 (it waited for "no Python process", which never
happens while the app runs) was stopped.

## Plan change (2026-11-13, documents only, no code)

Requested by the user; `docs/implementation.md` and `docs/architecture.md` updated with matching tracker tasks.

- **Build order:** 4, 5, 6, 9, 7, 8. The user trades monthly hedged stock option selling now, so phase 9 comes before the
  journal/scorecard (7) and LLM (8) phases. Phase 9 uses only phases 4, 5 and 6; phase 7 will cover phase 9 spreads.
- **Phase 4 additions** (P4.9-P4.12): first check whether Breeze provides historical option prices and record the answer
  here; if it does, option backtests must use them, otherwise reports say "payoff simulation". Every option strategy
  backtest has a no-filter baseline (the same spread every month or week, no entry rules). Pre-2019 Breeze stock history is
  treated as unreliable (no session before 2019-01-22 is tested). IV and delta are computed by code.
- **Phase 6 additions** (P6.10 rewritten, P6.13-P6.16): six trade-type alert switches (intraday stocks, swing stocks,
  index option buying, index option selling, stock option buying, stock option selling), stored in the database, changed via
  `POST /journal/alert-switches`, default off; switched-off types still paper-trade; each switch shows the type's track
  record and label (UNPROVEN, "negative in backtests"). Exit calls for every open position of every type, taken or entered
  manually (target, stop, breakeven, trailing stop, partial, time exit, entry invalidated, regime flip, kill switch), each
  with action and reason, repeating until marked closed. Manual position entry under /journal/.
- **Phase 9 extensions** (P9.1-P9.11, all thresholds in `stock_option_selling` in config.yaml): universe = all NSE F&O stocks
  from the instrument list filtered by option liquidity; daily shortlist after the close (IV rank, trend clarity, distance to
  support/resistance, liquidity, no events before expiry) on a "Stock option selling" page; short strike beyond support or
  resistance, beyond the highest-OI strike and at or below delta 0.20; hedge 2-3 strikes out, same expiry, within
  max_loss_per_stock_spread_pct, liquid; entry 20-35 days to expiry, never expiry week; exits at 50% of credit, stop at 2x
  credit, breach or trend-flip alerts with exit and roll numbers, forced exit before expiry week; manual spread entry with
  UNHEDGED flag (never suggested by the system); capital advisory only (capital-fit label).

**Interpretations to confirm (recorded as defaults in architecture.md section 15):**
1. A switched-on trade type sends alerts only for strategies whose `strategies.status` allows alerts. So with today's
   statuses, switching on "intraday stocks" or "swing stocks" sends nothing ("negative in backtests"). Alternative: the
   switch alone decides.
2. "Stop at 2x credit" = close when the cost to buy back the spread reaches 2x the credit received (a loss of about 1x
   the credit before costs). Alternative: close when the loss reaches 2x the credit (buy-back cost 3x credit).

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
| 24 | 3 | Options IPFT rate empty (sources disagree) | Fixed 2026-11-13: NSE circular NSE/FA/73061 (user) - Rs 3,553/crore of premium each side including IPFT; IPFT set to 0 so it is not counted twice |
| 34 | 3b | Phase 3b results were computed with Prime 999 rates; the plan is Prime 4999 (lower brokerage) | Fixed 2026-11-13: rerun at Prime 4999 rates; all still negative after costs |
| 35 | 3b | Backtest start-up wrote holidays to the database and failed while the app held the write lock | Fixed 2026-11-13: holidays read from the CSV, nothing written |
| 36 | plan | Alert rule when a trade-type switch is on (switch AND strategy status, or switch alone) | Waiting for user (default: both) |
| 37 | plan | Meaning of "stop at 2x credit" for stock spreads | Waiting for user (default: buy-back cost reaches 2x credit) |
| 38 | plan | Whether Breeze provides historical option prices | Open: first task of phase 4 (P4.9) |
| 25 | 3 | Breeze history is not adjusted for corporate actions; gaps above 25% are treated as data breaks | Open: an adjustment source would let older history be used |
| 26 | 3 | All five strategies lose after costs on real data (development period) | Waiting for user: decide what to change before opening the holdout |
| 27 | 3 | holidays.csv has no rows: sessions after a holiday get an UNKNOWN regime | Waiting for user |
| 28 | 3 | Breeze's 15:20 five-minute bar is unreliable and the 15:15 bar is often missing | Fixed in 3b: the backtest regime ignores them (`regime.ignore_5m_bar_starts`); trades never used them; the live regime uses ticks, not these bars |
| 29 | 3 | The phase 3 diagnosis's intraday random baseline reused each real trade's session and direction (leaks the future for breakouts) | Fixed 2026-11-12: corrected baseline; "orb and vwap worse than random" withdrawn |
| 30 | 3b | Breeze daily history before 2019-01-22 has wrong-scale days (stocks, several dates) and ~100x VIX values | Open: swing tests start 2019-01-22 (`backtest.test_start`); older bars kept |
| 31 | 3b | A Breeze history request once hung for over 30 minutes (no SDK timeout) | Fixed: 60 s timeout per request; that symbol fails and a rerun resumes |
| 32 | 3b | No v2 strategy is profitable after costs or better than random entries | All phase 3 cash strategies set to no alerts, paper only, "negative in backtests" (2026-11-13). Waiting for user: what to try next |
| 33 | 3b | holidays.csv covers 2026 only: sessions after 2019-2025 holidays get an UNKNOWN regime in swing tests | Waiting for user (add earlier years if wanted) |

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
| 2026-11-12 | Phase 3b: v2 strategies are new ids; v1 unchanged. vwap_pullback retired (`strategies.inactive`) |
| 2026-11-12 | Rule 1: intraday stop >= 5x estimated round-trip cost (charges at the position cap + slippage both sides) |
| 2026-11-12 | range_reversion_v2: stop = further of 3-bar extreme and 5x cost; target = band average; skip if target < 2x cost (user) |
| 2026-11-12 | Holdout starts frozen: 5-minute 2026-10-05, daily 2026-06-08 (adding older history never moves the holdout) |
| 2026-11-12 | Swing backtests start 2019-01-22 because of price errors in older Breeze daily history |
| 2026-11-12 | Backtests load the CSV files (holidays, events, FII/DII) first, as the app does |
| 2026-11-12 | Random baseline uses only information available at the random entry's time |
| 2026-11-13 | Brokerage plan: ICICI Direct Prime 4999 (user): delivery 0.10%, intraday 0.010%, options Rs 19/lot, each side |
| 2026-11-13 | All phase 3 cash strategies (v1 and v2): no alerts, paper only, labelled "negative in backtests" (`strategies.status`) |
| 2026-11-13 | Backtests read holidays.csv without writing to the database |
| 2026-11-13 | Build order 4, 5, 6, 9, 7, 8: stock option selling before journal and LLM (user trades it now) |
| 2026-11-13 | Option backtests use Breeze option history if it exists, else "payoff simulation"; always with a no-filter baseline |
| 2026-11-13 | Per-trade-type alert switches stored in the database (POST /journal/alert-switches); switched-off types paper-trade |
| 2026-11-13 | Exit calls for every open position, taken or manual, repeat until closed; always sent regardless of switches |
| 2026-11-13 | Phase 9 rules: F&O universe by liquidity, daily shortlist, short strike beyond S/R and max-OI with delta <= 0.20, hedge 2-3 strikes out, 20-35 DTE, 50% target, 2x credit stop, exit/roll alerts, manual UNHEDGED positions flagged, capital advisory |
| 2026-11-13 | NSE charges per circular NSE/FA/73061 (effective 2026-03-01, user): cash Rs 307/crore and options Rs 3,553/crore of premium each side, IPFT included; IPFT rates set to 0. Cash was 0.00307% + a separate 0.0000001% IPFT before (IPFT counted twice); corrected |
