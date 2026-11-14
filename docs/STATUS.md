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
| - | Backtest engine review fixes (Codex review of phase 3) | Fixed and tested; dev backtests and random baselines rerun; no conclusion changes; holdout not opened | 327 passing (whole suite) | 2026-11-13 |
| 4 | Option strategies | Built and tested; Breeze has historical option prices, so option backtests use real stored prices; no option strategy shows an edge vs its no-filter baseline (selling strategies: 7 trades in total, low sample); live chain refresh and live signals not yet seen in market hours | 410 passing (whole suite) | 2026-11-13 |
| - | Phase 4 review fixes (review of commit 033782e, 6 findings) | Fixed and tested; phase 4 backtests rerun: strategy results identical, baselines changed (fixed schedule), no verdict changes; 31 baseline contracts not stored yet | 437 passing (whole suite) | 2026-11-13 |
| 5 | Risk engine | Not started | | |
| 6 | Live signals and alerts (paper mode) | Not started | | |
| 7 | Journal and weekly scorecard | Not started | | |
| 8 | LLM layer and go-live gate | Not started | | |
| 9 | Stock option selling: CORE built early (2026-11-14, before phases 5 and 6, by the user's instruction) | Core built and tested: rules, shortlist, exits and rolls, campaigns, falling-stock block, campaign limit, profile, scenario history, alerts, reversal detector, backtest with baseline. Option history partly downloaded (9 stocks, Jan-Jun 2026): results so far are LOW SAMPLE. Signal cards, Telegram alerts, manual position form, risk hooks and paper tracking wait for phase 6 | 526 passing (whole suite) | 2026-11-14 |

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

## Backtest engine review fixes (2026-11-13, before phase 4)

A Codex review of the phase 3 engine found three problems. All three were confirmed in the code and fixed, because
phases 4 and 9 reuse the engine. Development period only; the holdout was not opened; no strategy or parameter changed.

1. **Entry validated on the raw open, not the slipped fill** (`Engine._open`). Slippage is adverse, so it moves the fill
   toward the target: an open just inside the target could fill beyond it, and the trade then "hit the target" on its
   entry bar at a loss. Now both the raw open and the fill after slippage must lie strictly between stop and target;
   otherwise no trade, counted as `FILL_BEYOND_STOP_OR_TARGET` in `skipped_signals` (a raw open beyond stays
   `GAPPED_PAST_STOP_OR_TARGET`). **Real-data impact: none** - no trade in the previous run (intraday or swing) had a
   fill at or beyond its stop or target, and the rerun skipped none for this reason.
2. **Incomplete intraday sessions were traded**, and two fallbacks invented exits: "the exit bar is missing: leave at the
   first price after it" and "data ended: EOD at the last bar's close". Now an intraday session is used only if it has
   every 5-minute bar from 09:15 to the bar ending at `backtest.intraday_exit_time` (09:15..15:10, 72 bars; the 15:15
   and 15:20 bars are not required). Both fallbacks are gone: a trade whose holding period crosses a missing bar, or
   whose session ends before the exit bar, is dropped (a guard; it cannot happen once sessions are complete). Counts are
   in the report's new `data_exclusions` section, on the backtests page and in the CLI output.
   **Real-data impact:** 50 of 5,200 development symbol-sessions removed - all on 2026-05-06, the first day of 5-minute
   history, where every stock lacks the 09:15 and 09:20 bars. Trades removed: range_reversion 1 (1,799 -> 1,798); no
   other active strategy traded that day (retired vwap_pullback had 29 there; not rerun). Trades dropped by the
   missing-bar guard: 0. In the previous run no trade crossed a missing bar and no EOD exit came from a truncated session.
   The random baseline (`diag.py`) now draws only complete sessions, so it is compared on the same footing.
3. **ORB `range_minutes`** accepted values that are not whole 5-minute bars (17 silently became 15). `OrbParams` and
   `OrbV2Params` now reject them at config load ("must be a multiple of 5"). Current values (15, 30) are unaffected.

**Tests** (`tests/test_review4_fixes.py`, 27 tests): fill past / exactly at the target, long and short; fill inside
still trades; raw gap still a gap; report separates skipped signals from data exclusions; required bars 09:15..15:10;
truncated session, missing midday bar, missing exit bar and empty symbol-session are not traded; a session without
15:15/15:20 bars is used and exits EOD at 15:15; the three drop guards (with the completeness check bypassed); config
rejects 17/22/33 for orb and orb_v2 and accepts multiples of 5; the backtests page shows the exclusions. Run against
the pre-fix engine, 18 of them fail and 8 pass (the 8 check behaviour that was already right; the three guard tests
fail there at the missing `session_usable` hook rather than at their assertion). Seven existing engine tests used 3-4
bar sessions; they now pad to a full session (`full_day` in test_phase3.py) with unchanged assertions, and the two that
tested "no next bar" / "gap in bars" now expect `SESSIONS_INCOMPLETE`.

**Rerun (Prime 4999 rates, same data, 20 random seeds):**

| Strategy | Trades before -> after | Net (Rs) before -> after | PF net | PF no costs | Random PF no costs before -> after, mean (min-max) | Random PF net before -> after, mean (min-max) |
|---|---|---|---|---|---|---|
| orb | 242 -> 242 | -32,752 (same) | 0.45 | 0.705 | 0.96 (0.68-1.16) -> 0.91 (0.72-1.09) | 0.59 (0.41-0.72) -> 0.56 (0.43-0.67) |
| orb_v2 | 542 -> 542 | -40,219 (same) | 0.68 | 1.02 | 0.93 (0.72-1.14) -> 0.88 (0.72-1.11) | 0.58 (0.44-0.71) -> 0.55 (0.44-0.68) |
| range_reversion | 1,799 -> 1,798 | -140,898 -> -140,886 | 0.31 | 0.99 | 1.00 (0.91-1.11) -> 1.02 (0.90-1.12) | 0.31 (0.28-0.34) -> 0.31 (0.27-0.34) |
| range_reversion_v2 | 1,027 -> 1,027 | -91,188 (same) | 0.43 | 0.90 | 0.96 (0.78-1.09) -> 0.96 (0.82-1.03) | 0.44 (0.36-0.51) -> 0.44 (0.38-0.48) |
| swing_pullback | 1,316 -> 1,316 | -141,165 (same) | 0.88 | 1.22 | 1.18 (1.02-1.28), same | 0.85 (0.74-0.93), same |
| swing_pullback_v2 | 1,311 -> 1,311 | -151,355 (same) | 0.87 | 1.21 | 1.21 (1.09-1.35), same | 0.87 (0.78-0.98), same |
| swing_breakout | 999 -> 999 | -199,244 (same) | 0.80 | 1.07 | 1.22 (1.08-1.35), same | 0.89 (0.78-0.99), same |
| swing_breakout_v2 | 998 -> 998 | -223,526 (same) | 0.78 | 1.04 | 1.23 (1.08-1.39), same | 0.89 (0.776-1.00), same |

**Does any conclusion change? No.** Every version still loses after costs; none beats random entries; all stay
"negative in backtests", no alerts, paper only. Details:
- Swing results and swing baselines are identical to the last run (finding 2 is intraday only; finding 1 never occurred).
- The intraday random ranges moved because random draws now skip 2026-05-06, which changes the random sequence. That is
  seed-to-seed sampling noise, not an engine effect. Side effect: orb v1's PF before costs (0.705, unchanged) is now
  just below every random seed (minimum 0.719; before 0.677). With 20 seeds that margin is within noise, so it is not
  treated as a new finding; orb v1 was already the weakest intraday strategy.
- swing_breakout_v2's PF after costs (0.775) was already just below every seed (0.776) in the last run; the earlier
  table rounded both to 0.78.
- Mechanical checks: no problems on any real or random trade.

Output: `data/diagnostics/bt_review4.txt`, `data/diagnostics/diag_review4.txt` and the JSON reports in `data/backtests/`.

**Not tested:** the app was not started for this fix (the backtests page is covered by a TestClient test). Swing trades
across a stock's missing daily bar are not checked (outside this review; open issue 42).

## Phase 4: option strategies (2026-11-13)

### Step 0: does Breeze provide historical option prices? Yes

Checked with read-only calls (`get_historical_data_v2`, product type options) by `python scripts/check_option_history.py`
(559 requests; output in `data/diagnostics/option_history_probe.json`, not tracked).

| Question | Finding |
|---|---|
| Underlyings | NIFTY, BANKNIFTY and stocks (RELIANCE, HDFCBANK, INFY, SBIN tried): all return data |
| Expired contracts | Available. The oldest month tried was June 2017: NIFTY, BANKNIFTY and SBIN have it; RELIANCE from June 2018, INFY from June 2019, HDFCBANK from June 2020 in this probe (older misses are probably my strike guess, not missing data) |
| Intervals | Daily, 5-minute and 1-minute all work, also for 2017-2020 contracts |
| How much of a contract | Daily bars for about 3 months before a monthly expiry (62 bars), about 4-5 weeks for a weekly |
| Fields | open, high, low, close, volume, open_interest (present on nearly every row: 2,967 of 3,074 daily rows) |
| Bid/ask | **Not available. History is traded prices only.** |
| Weekly contracts | NIFTY weekly contracts are available (the 2026-10-13 weekly returned 20 daily and 265 five-minute bars) |
| Expiry dates | Not listed anywhere for expired contracts. A wrong date returns nothing (or, once, a single stale bar), so the real date is found by trying: Thursdays until mid-2025, Tuesdays since, earlier on holidays |

Consequence: option backtests on real data **must and do** use stored option prices (`option_bars`); each trade records
`price_source: option_history`. Only the sim source uses Black-Scholes prices, and those reports say "payoff simulation".

### Built
- `app/feeds/breeze_feed.py`: `option_historical_raw` / `option_historical` (one contract's bars) and
  `nfo_scrip_master_lines` (ICICI's F&O instrument list from the public SecurityMaster file). Data calls only; the SDK is
  still imported only here. `option_chain` now logs in first.
- `app/options/instruments.py`: lot size, expiries (weekly/monthly) and listed strikes per underlying from the instrument
  list, stored in `instruments`. An expiry whose rows disagree on the lot size, a symbol not in the list, or a list older
  than 5 days is UNKNOWN. Today's list: NIFTY lot 65, BANKNIFTY 30 (163 contract expiries for the 53 watchlist symbols).
- `app/options/chain.py`: chain loader for NIFTY (two nearest expiries plus two nearest monthly), BANKNIFTY (monthly) and
  stocks (monthly, on request). Rows with a missing, zero, negative or crossed bid/ask are quarantined
  (`BAD_OPTION_QUOTE` in `quality_events`) and never stored. A snapshot older than 15 minutes is stale.
- `app/options/greeks.py`: Black-Scholes price and delta, implied volatility by bisection, IV rank. Unknown inputs give None.
- `app/options/payoff.py`: max loss, max profit and breakevens including costs for a long option, a credit spread and an
  iron condor; `check_hedged` rejects any sold option without a bought option of the same expiry and right further out.
- `app/options/margin.py`: margins entered by the user (`data/margins.csv`, or pasted on the Options page through
  `POST /data/import/margins`). No entry, or one older than 7 days: `MARGIN_UNKNOWN`, no signal.
- Strategies on the phase 3 interface (id, version from thresholds, status): `long_option`, `credit_spread`,
  `iron_condor` (rules in README and in each file). Stock option selling returns NO TRADE
  (`STOCK_OPTION_SELLING_NOT_ALLOWED`). Every signal carries, for one lot: premium or net credit, lot size, total cost, max
  loss in rupees and % of capital, max profit, breakevens, costs, margin, IV rank, legs with IV and delta, warnings.
- `app/options/option_history.py`: per-contract download into `option_bars` (same pacing, timeout and bar-quality rules as
  stock history; an error reply is never recorded as "no data"; every contract asked for is remembered), expiry discovery
  into `option_expiries`, model prices for the sim source, at-the-money IV history for stocks.
- `app/options/backtest.py`: option backtests with the **no-filter baseline** (P4.10), reports saved like the others and
  shown on `/backtests` with the price source, baseline rows and verdict.
- `app/options/analytics.py`: put-call ratio, change in OI, highest-OI strikes, chain table with IV and delta, and
  `OptionDesk` behind `GET /api/options`, `GET /api/options/{underlying}` and the page `/options`.
- App: instrument list loaded daily, index chains refreshed every 5 minutes in market hours (breeze) or continuously (sim,
  fake chain), option-chain snapshots older than 2 days deleted.
- Config: `options` section and `strategies.long_option / credit_spread / iron_condor`, all validated.
- **Issue 42**: see the open-issues table; `Engine.session_dates / sessions_between / next_session` are shared by the
  option engine.

### Option history stored (live file)
NIFTY 1,764 contracts (70,020 daily bars), BANKNIFTY 807 (38,091), RELIANCE 229 (9,820), HDFCBANK 133 (5,792); expiries
2019-01-31 to 2026-06-30. Expiries found: NIFTY 90 monthly and 99 weekly (2024 onward; one week, 2026-06-15, not found),
BANKNIFTY 90 monthly. No bar was rejected by the quality rules. About 3,950 Breeze requests were used today in total.

### Results on real data (development period; holdout from 2026-06-08 not opened; nothing tuned)
Prime 4999 costs, options slippage 0.5% per leg per side, one lot per trade at today's lot size (NIFTY 65, BANKNIFTY 30).
Final run from stored prices only, no contract missing for these three reports.

| Strategy | Underlyings, period | | Trades | Win % | Net after costs (Rs) | Costs (Rs) | Max DD (Rs) | PF | Per trade (Rs) |
|---|---|---|---|---|---|---|---|---|---|
| credit_spread 1/cff9bd39 | NIFTY, BANKNIFTY monthly, 2019-01-22..2026-06-05 | strategy | 6 | 50.0 | -2,307 | 682 | 2,941 | 0.51 | -385 |
| | | baseline (both sides) | 289 | 64.4 | -125,848 | 33,051 | 130,849 | 0.59 | -435 |
| | | baseline bull put | 144 | 68.8 | -46,748 | | | 0.66 | |
| | | baseline bear call | 145 | 60.0 | -79,099 | | | 0.53 | |
| iron_condor 1/4b4dadcc | NIFTY weekly, 2024-01-01..2026-06-05 | strategy | 1 | 100 | +673 | 187 | 0 | - | +673 |
| | | baseline | 110 | 69.1 | -16,771 | 21,375 | 25,762 | 0.77 | -152 |
| long_option 1/556436ff | NIFTY, BANKNIFTY, 2019-01-22..2026-06-05 | strategy (trigger swing_pullback_v2) | 81 | 35.8 | -3,759 | 7,101 | 79,312 | 0.99 | -46 |
| | | baseline (ATM call, first session of each month) | 153 | 39.2 | -1,011 | 15,142 | 272,448 | 1.00 | -7 |

Verdicts: credit_spread **no edge vs baseline (LOW SAMPLE)**; iron_condor "beats baseline" on **one trade**, which is no
evidence (labelled UNPROVEN); long_option **no edge vs baseline**. All three: no alerts, paper only (`strategies.status`).

What it shows:
- **The selling filters almost never fire** (issue 43): 6 credit spreads in 7.4 years and 1 condor in 2.4 years.
- **The unfiltered spreads lose** (issue 44). Win rates are 64-69%, but a winner keeps about half the credit and a stopped
  trade is bought back at 2.6-2.8x the credit on average (NIFTY: credit 25, buy-back 68), because the stop is decided at a
  close and filled at the next open (worst cases: the June 2024 election gap and March 2026). Costs take Rs 106-134 per
  spread, about a quarter of the baseline loss. By year the credit spread baseline lost most in 2022-2024 and 2026.
- Option buying on the indices is about break-even before costs with or without the swing trigger; costs make both negative.
- Skipped baseline entries (one attempt per expiry): credit spread 31 with the opening credit outside the stop/target band
  and 27 with a non-session next day, of about 360 scheduled; 7 trades dropped for a missing leg bar.
- **Stocks (incomplete, not a result)**: RELIANCE and HDFCBANK with the history stored so far gave strategy 35 trades,
  -25,676, against baseline 81 trades, +130,546, but 149 contracts were still missing when the request limit was reached,
  and INFY and SBIN were not downloaded (issue 45). Stocks were therefore taken out of the default run.

### Tested (offline, `tests/test_phase4.py`, 83 tests; whole suite 410 passing)
- ACCEPTANCE: no signal if bid/ask, lot size or margin is unknown (each strategy).
- ACCEPTANCE: a single short leg without its hedge is rejected (wrong right, other expiry, not further out, too few lots).
- ACCEPTANCE: stock option selling returns NO_TRADE; stock option buying carries the expiry-week warning and is not
  bought inside expiry week.
- ACCEPTANCE: model-priced backtests are labelled "payoff simulation"; every trade records its price source; never mixed.
- ACCEPTANCE: with stored option history the backtest uses it: changing one fixture option bar changes the fill and the result.
- ACCEPTANCE: every option report has the no-filter baseline for the same period, costs and price source.
- ACCEPTANCE: no option backtest tests a session before 2019-01-22.
- ACCEPTANCE: IV and delta match a hand calculation; lot sizes and expiries come from the instrument list (a revised
  list changes the lot size; no lot-size or expiry literal in `app/options`).
- Also: property-based payoff bounds (credit spread, condor, long option) and implied-volatility inversion; entry at the
  next session's open, never the signal day; a spread trade reconciled to the paise by independent arithmetic; trades
  crossing a missing option bar dropped; unknown lot size or unstored contracts give no trades and are counted;
  chain quarantine and loader; stale chain, stale instrument list, missing spot; margin parsing, age and import;
  option history fetch, cache, pacing, error and request-limit handling; expiry discovery with a holiday-shifted expiry;
  iron condor only in RANGE; regime, IV-rank, liquidity and credit rules; config validation; routes, page and the margin
  import route (safety tests pass); issue 42 (four cases).

### Run here
- `python scripts/check_option_history.py` (live, read-only), `python -m app.options.backtest --download` in four parts and
  a final `python -m app.options.backtest` from stored data; `python -m app.options.backtest --source sim`.
- Swing backtests rerun after the issue 42 fix: identical trades and net P&L for all four; 0 trades dropped.
- App in sim mode on port 8001 (8000 was in use): `/options` 200, `/api/options` lists 52 underlyings,
  `/api/options/NIFTY` showed spot LIVE, lot size and 7 expiries (fake sim contracts), three fresh chains with put-call
  ratio, highest-OI strikes and ATM IV, IV rank 36, and three NO TRADE ideas with reasons (regime RANGE; IV rank below 50;
  no trigger). `/backtests` 200. A bad margin paste was refused (400).
- One live `get_option_chain_quotes` call after the close: 60 strikes per side with bid, ask, last and OI.

### Not tested / known gaps
- Live: background chain refresh, live IV rank, live ideas and the `BAD_OPTION_QUOTE` count in market hours (issue 47).
- No margin has been entered, and max loss / margin have not been compared with ICICI's calculator (issue 46).
- `long_option` needs a live trigger signal: it shows NO_TRIGGER until phase 6. Intraday-triggered buying is not backtested
  (it needs 5-minute option bars). Put buying is not backtested (the daily trigger is long-only).
- Backtests: highest-OI rule and margin rule not applied; today's lot sizes; daily bars only (the opening prices of two
  legs are not simultaneous); BANKNIFTY strikes assumed on a 500-point grid and NIFTY on 100; stock IV rank from a fixed
  set of at-the-money calls; one entry attempt per expiry.
- Stock option backtests incomplete (issue 45). The combined sim-source option reports are payoff simulations on fake data.
- `data/backtests/*_latest.json` holds the last run of each strategy, whatever its source.

## Phase 4 review fixes (2026-11-13, review of commit 033782e)

All six findings were confirmed in the code and fixed. New tests: `tests/test_review5_fixes.py` (26 tests). Run against
the pre-fix commit in a temporary worktree, **24 fail and 2 pass** (the 2 are guards that hedged signals and trades are
still accepted); all pass now. Development period only; the holdout was not opened; no strategy threshold changed.

1. **HIGH - hedge rule enforced at every boundary.** The rule lives in `app/models.py` (`unhedged_short_legs`): a sold
   option is covered only by a bought option of the same right and expiry, further out of the money, with at least as
   many shares; one bought option covers one sold option; a leg that cannot be read counts as uncovered. All legs of a
   signal or trade belong to one underlying (the signal's symbol).
   - `Signal` construction fails (`InvalidSignal`, text `UNHEDGED_SHORT`) for index and stock option signals.
   - `OptionEngine._enter` refuses such a signal even if the object got past its own validation (`UNHEDGED_SHORT` in
     `skipped_signals`).
   - `Store.save_backtest()` raises `UnhedgedShort` and saves nothing (run and trades) if any option trade has an
     uncovered sold leg. Backtest trade legs now carry their own expiry, lots and lot size so the check is real.
2. **HIGH - stock IV history is point in time.** Before: the at-the-money calls struck 35 and 15 days before an expiry
   were pooled, and each day used whichever was nearest the money, so the call struck on a LATER close could set an
   earlier day's IV (and IV rank). Now a call is used only from the day its strike was chosen
   (`option_history.point_in_time_atm_contracts`, `stock_iv_series`); the backtest engine and the live Options page use
   the same function. Test: changing the close 15 days before expiry leaves `iv_at(stock, D)` unchanged for every earlier D.
   Index IV rank (India VIX) was not affected. Real-data impact: none on the reported phase 4 results (they are index
   only); the incomplete stock run in the phase 4 notes used the old series.
3. **payoff.py - condor breakevens.** With unequal wings the credit after costs can exceed the narrower wing; that side
   never loses, so it has no breakeven. `iron_condor` now returns only prices where the P&L is zero (property test with
   asymmetric wings evaluates the P&L at every reported breakeven). Equal-width condors (what the strategy builds) are unchanged.
4. **Baseline - one fixed scheduled attempt per expiry.** The schedule is computed from the calendar alone
   (`OptionEngine.baseline_schedule`): spreads and condors on the first market session on which the expiry is inside the
   entry window; option buying on the first market session of each month. One attempt: no valid strikes or prices that
   day, or no fill at the next open, and the expiry is recorded as skipped (`baseline.schedule.skipped`, with the date
   and reason) and never retried. Before, a failed day was retried on the following days, and an attempt was delayed
   while the previous month's baseline position was still open. Baseline positions of different expiries may now overlap.
5. **Margin and bid/ask.** Still required for live signals: a live `OptionContext` with `require_margin=False` now
   raises. Backtests cannot check them, so every option report carries `eligibility_label`
   **"excludes margin and bid/ask eligibility checks"** (report, CLI, backtests page), and the acceptance check in
   implementation.md is scoped to live signals.
6. **Weak tests in test_phase4.py.** Every loop over trades first asserts there are trades. Two tests turned out to
   check nothing: the option-buying strategy had no trades in its fixture (now a rising market with falling VIX), and the
   exit check allowed `EXPIRY_EXIT`. It now asserts the exact boundary (a time exit fills at 09:15 of the first session
   within `exit_days_before_expiry` of expiry; no trade exits later), with a new test in which every spread leaves by the
   time rule. A vacuous `>= 0` assertion was removed.

**Phase 4 backtests rerun from stored prices (same data, costs and rules; `data/diagnostics/option_bt_review5.txt`):**

| Strategy | | Before | After |
|---|---|---|---|
| credit_spread | strategy | 6 trades, -2,307, PF 0.51 | identical |
| | baseline (both) | 289 trades, 64.4%, -125,848, PF 0.59, -435 per trade | 265 trades, 64.2%, -126,716, PF 0.56, -478 per trade |
| | baseline bull put | 144 trades, -46,748, PF 0.66 | 133 trades, -46,120, PF 0.64 |
| | baseline bear call | 145 trades, -79,099, PF 0.53 | 132 trades, -80,596, PF 0.50 |
| iron_condor | strategy | 1 trade, +673 | identical |
| | baseline | 110 trades, 69.1%, -16,771, PF 0.77, -152 per trade | 105 trades, 69.5%, -14,598, PF 0.79, -139 per trade |
| long_option | strategy | 81 trades, -3,759, PF 0.99, -46 per trade | identical |
| | baseline | 153 trades, -1,011, PF 1.00, -7 per trade | 151 trades, +5,817, PF 1.01, +39 per trade |

**Does any result change?** The three strategy results are identical (findings 1-3 and 5 change no trade; the strategy
path of finding 4 is unchanged). Every baseline changed because of finding 4 (fixed dates, no retries, overlapping
expiries). **No verdict changes:** credit_spread "no edge vs baseline (LOW SAMPLE)", iron_condor "beats baseline" on one
trade (no evidence), long_option "no edge vs baseline". The option-buying baseline moved from about zero to slightly
positive (+39 per trade), so the strategy is a little further below it.

Baseline schedule accounting: credit spread 356 attempts, 272 entered, 84 skipped (28 opening credit outside the
stop/target band, 22 next weekday not a session, 15 no strike, 9 illiquid, 8 no hedge strike, 1 no credit, 1 fill beyond);
condor 127 / 106 / 21; option buying 178 / 153 / 25. (Entered attempts can still be dropped later for a missing bar, so
trades are fewer than entries.)

**Not complete:** the fixed schedule asks for contracts on different days than before; **31 contracts it wanted are not
stored**, so some of the "no strike / no hedge" skips above are missing data, not real skips. They need one
`python -m app.options.backtest --download` run (a few dozen requests) and the baseline rows above will move slightly.

## Phase 9 core: stock option selling (2026-11-14, built before phases 5 and 6)

Built on the user's instruction to do the core of phase 9 now. Phase 5 was NOT started. No holdout was opened
(development period only, to 2026-06-05). Nothing here produces alerts.

### Built
- `app/options/stock_selling.py`: the strategy `stock_option_selling` (id, version from its thresholds, status "UNPROVEN").
  Entry: stock in a clear trend (swing trend rule) -> bull put spread (up) or bear call spread (down); IV rank at or above
  50; no results / board meeting / corporate action before expiry; monthly expiry 20-35 days away; sold strike the first
  strike beyond support (20-session low) or resistance with |delta| <= 0.20 and, live, beyond the highest-OI strike; bought
  strike 2 strikes further out (3 if 2 does not qualify), liquid, max loss within `max_loss_per_stock_spread_pct`; net
  credit after hedge and charges at least 10% of max loss (`CREDIT_TOO_SMALL`); falling-stock block (`DOWNTREND_BLOCK`,
  mirror `UPTREND_BLOCK`); per-stock campaign limit (`STOCK_LOSS_LIMIT`); margin known for a live signal. Also: daily
  shortlist scoring, exit calls (target, stop, strike breached, trend flip, forced exit), roll rules, stuck-position check,
  open-position alerts with exit and roll numbers.
- `app/options/stock_profile.py`: falling-stock block, campaign limit, range and speed profile, scenario history, alert
  detection. `app/options/campaign.py`: campaign tracking. `app/options/reversal.py`: reversal detector, outcome study, and
  the two candidates (`reversal_put_spread`, `reversal_bull_call_spread`; both "WATCH ONLY: awaiting your approval").
- `app/options/stock_backtest.py`: liquidity ranking, the budgeted resumable download, the report (5-minute and daily exit
  checks, three exit policies on the same entries, the effect of each rule and alert, splits by entry condition, reversal
  study). `app/options/shortlist.py`: the daily shortlist, the per-stock profile view and the reversal watch list.
- Engine (`app/options/backtest.py`): 5-minute exit check, strike-breach exit, alert exits, rolls with campaign links,
  recorded and replayed entries, debit spreads, entry conditions on every trade. `option_history.py`: 5-minute option bars
  (paged), the daily request budget, retries for a request with no reply.
- Store: tables `shortlist`, `campaigns`, `request_budget`. Routes (all read-only): `/api/stock-option-selling/shortlist`,
  `/profile/{stock}`, `/reversals`, `/campaigns`, and the page `/stock-option-selling`. `/backtests` shows the new tables.
- Config: `stock_option_selling` (validated) and `options.backtest.daily_request_budget`; two event types added
  (`BOARD_MEETING`, `CORPORATE_ACTION`).

### Downloads done today (2026-11-14) and what the data shows
- **Issue 54 closed:** the 31 index contracts needed 12 requests; none is missing now. Index backtests rerun from stored
  prices: long_option and credit_spread unchanged from the review-fix table; iron_condor baseline now 107 trades,
  -18,111, PF 0.75, -169 per trade (was 105, -14,598). No verdict changed.
- **Stock option history:** 1,588 of today's 3,500 requests were used. Completely stored: **Jan-Jun 2026 for 9 stocks**
  (HDFCBANK, RELIANCE, ICICIBANK, SBIN, BHARTIARTL, INFY, BSE, LT, M&M); ETERNAL partly. A stock-year costs roughly
  80-250 requests, so 30 stocks x 7.4 years needs many more days at 3,500 a day. The last run was stopped by the system
  (machine low on memory), not by Breeze; an earlier run stopped cleanly on a 60-second timeout. Nothing fetched was lost.
- **Report on those 9 stocks, 2026-01-01 to 2026-06-05, one lot each, Prime 4999 costs** (`data/diagnostics/stock_report_2026-11-14.txt`).
  This is five months of one year: **every figure is LOW SAMPLE or close to it and proves nothing yet.**

| | Exit check | Trades | Win % | Net after costs (Rs) | Per trade (Rs) | PF |
|---|---|---|---|---|---|---|
| strategy | 5-minute | 8 | 75.0 | +1,253 | +157 | 1.23 |
| baseline (both sides) | 5-minute | 41 | 68.3 | -2,118 | -52 | 0.89 |
| baseline bull put / bear call | 5-minute | 20 / 21 | 45.0 / 90.5 | -11,469 / +9,351 | | 0.39 / 9.17 |
| strategy | daily close | 8 | 62.5 | -6,108 | -764 | 0.52 |
| baseline (both sides) | daily close | 41 | 61.0 | -8,553 | -209 | 0.70 |

  - Checking on 5-minute bars instead of daily closes moved the baseline from -209 to -52 per trade (the daily check fills a
    stop at the next day's open, usually much worse).
  - Exit policies on the same 41 baseline entries (daily check): stop -8,553; roll by the rules -8,553 (no roll passed the
    rules in this period); roll every month -39,206 with a worst campaign of -15,793. On the 8 strategy entries rolling
    every month happened to show +7,219 against -6,108: 8 trades, no evidence.
  - Rule effects on the baseline (5-minute check): adding the trend block left 15 of 41 trades, +5,854 (PF 3.96); adding
    the minimum net credit left 27, -3,783; the campaign limit blocked 1 attempt and changed nothing.
  - Baseline attempts: 90 scheduled, 41 entered; skipped 25 illiquid, 13 no priced hedge strike, 7 no strike, 3 gapped,
    1 max loss above the limit.
  - Reversal detector, all 49 watchlist stocks with enough history, 2020-2026 development period: **37 confirmations in 22
    stocks**; 10 / 20 / 40 sessions later the median move was -0.3% / +1.1% / +0.3% (mean +1.8% / +6.0% / +6.9%), up in
    49% / 54% / 51% of cases; none made a new low within 40 sessions. Neither candidate trade had a confirmation inside the
    downloaded months, so there is no option result for them yet. The bull call baseline entered nothing: its scheduled
    day (65 days before expiry) had no traded strike in 35 of 36 attempts.

### Tested (offline; 83 test functions in `tests/test_phase9_core.py`, `test_phase9_rules.py`, `test_phase9_rolls.py`, `test_phase9_reversal.py`; whole suite 526 passing)
- ACCEPTANCE: sold strike beyond support/resistance, beyond the highest-OI strike and within the delta limit; no signal
  when no strike meets all three. Hedge 2-3 strikes out, same expiry, liquid, max loss within the limit; otherwise no signal.
- ACCEPTANCE: no signal outside 20-35 days to expiry or in expiry week; no entry with results or a corporate action before
  expiry. A sold leg without a hedge is never suggested, entry or roll (also a property test over many inputs).
- ACCEPTANCE: target, stop, threat and forced-exit calls fire; threat shows exit and roll with numbers.
- ACCEPTANCE: the shortlist excludes illiquid stocks and stocks with events and gives a reason per candidate.
- ACCEPTANCE: backtest shows strategy and baseline side by side under both exit checks; every trade is hedged, entered at
  the next session and flat before expiry week; deterministic. A stop touched only inside the day is caught by the 5-minute
  check and not by the daily one; a stale leg price is not used.
- Download: fills an empty store and the result then matches a full store; stops on an error reply and resumes without
  asking again; stops at the budget; the daily budget is counted across runs; timeouts are retried, error replies are not.
- Each of the six rule additions, the roll rules (a)-(d), campaign arithmetic by hand, stuck positions, no recovery
  sizing, the three exit policies on identical entries, and the reversal detector (states by day, no use of later bars,
  both candidates, alert only after approval, outcome study by hand). Safety tests pass (no write route added).
- The new pages and routes were exercised through the test client in sim settings only; the app was not started by hand after these changes.

### Not tested / known gaps
- Live: the shortlist command (`python -m app.options.shortlist`) loading stock chains from Breeze after the close, the
  profile and reversal views with a live chain. Tested only with fixture chains.
- The option history is far from complete (see above), so no result can be relied on yet.
- Backtests: strike spacing of expired stock contracts is a guess (a round step of about 1% of the price); a strike that
  does not exist costs a request and is skipped, and "2 strikes out" counts strikes that have a price. The highest-OI rule,
  margin and bid/ask cannot be checked on history. Past results dates are not known, so the event rule is not applied in
  backtests. Lot sizes are today's. The three exit policies are compared on the daily exit check only.
- Scenario history: only the India VIX scenario has data. FII, RBI and results scenarios say "no data" until the files below are filled.
- Stocks whose daily history has an unadjusted price break in the last year (issue 25) give INSUFFICIENT_HISTORY for the
  trend block, and their profile windows are shorter than asked (for example RELIANCE starts 2024-10-28).
- Deferred to after phase 6: signal cards with the capital-fit label, Telegram alerts and repeats, the manual position
  form, risk engine hooks, automatic paper tracking and the UNPROVEN banner clearing; the universe beyond the watchlist.

### Data files to fill for the scenario history (nothing was invented)
| File | Columns | What to enter | Where to get it |
|---|---|---|---|
| `data/fii_dii.csv` | `date,fii_net_cr,dii_net_cr` | One row per session from 2019-01-22: FII/FPI and DII net cash-market figures in Rs crore, selling negative | NSE website, "FII/DII trading activity" report (it shows recent days; older days need NSE's archive or NSDL's FPI daily reports at fpi.nsdl.co.in, which are a slightly different series: use one source throughout) |
| `data/events.csv` | `date,type,note,symbol` | `RBI_POLICY` rows, symbol blank: the day each monetary policy decision was announced | RBI website (rbi.org.in), monetary policy press releases |
| `data/events.csv` | `date,type,note,symbol` | `RESULTS` rows with the stock's symbol: the day each quarterly result was announced (also future dates, for the entry rule) | NSE corporate filings (financial results / board meetings) or the company's announcements on BSE |

Both files can also be pasted through `POST /data/import/fii_dii` and `/data/import/events`. Restart the app (or rerun the
command) afterwards.

## Phase 9 final specification (received 2026-11-14): corrections and reconciliation

The consolidated Phase 9 specification replaces all earlier Phase 9 instructions. Phase 5 is not started; no holdout was
opened; no threshold was tuned. This section records the five corrections asked for first, and the requirement-to-code
table of section 10. **Waiting for the user: the stock-option capital for section 9.1 arrived as a placeholder
(`<YOUR STOCK-OPTION CAPITAL>`); `account.capital_inr` is still Rs 2,00,000 and was not changed.**

### Corrections

| # | Correction | State |
|---|---|---|
| 1 | Roll net credit = new spread credit - debit to close the old spread - all roll charges (four executions), each leg priced once with adverse slippage; ratio to replacement width x lot x lots must be ABOVE `min_net_credit_pct_of_width`. The replacement must also pass every entry rule a new trade would (trend, IV rank, events, blocks, campaign limit, strikes, hedge, risk budget, liquidity, 20-35 day window, minimum credit, margin), same side, same or later expiry, strike strictly lower (puts) / higher (calls). | **Done.** `stock_selling.roll_net_credit`, `roll_choices`; tests `test_ACCEPTANCE_roll_net_credit_is_new_credit_less_the_debit_to_close_less_all_roll_charges` (by hand, live and history) and `..._a_positive_new_premium_alone_does_not_qualify_and_each_rule_can_refuse_a_roll`. The earlier relaxed roll mode (no trend/IV rule, expiry up to 65 days) is removed. Every refusal is recorded on the trade (`roll_refused`) and counted in the report. |
| 2 | Section 2 defaults replace the assumed thresholds | **Done for the parameters that exist** (table below). Tests pin their fixtures' thresholds explicitly; `test_shipped_defaults_are_the_section_2_values` checks config.yaml. Parameters whose rule is not built yet are listed as gaps. |
| 3 | Exit-policy comparison on synchronized five-minute bars | **Not done yet.** The comparison still uses the daily exit check. The present 5-minute check also conflicts with section 8.1 (see table): it prices a leg that did not trade in a bar at its last trade (up to 30 minutes old) and falls back to the daily close when a day's bars are not stored. To be rebuilt in M1. |
| 4 | Widened hedge rule accepted | **Done** (already in code): a bought option of the same underlying, right and expiry at a different strike with at least the sold quantity. Issue 59 closed. |
| 5 | Report the bull call entry-window problem and trace the "no roll passed" result | **Done**, below. |

**Rerun of the exit-policy comparison (correction 1):** with the section 2 defaults and capital Rs 2,00,000 the stored
period (9 stocks, Jan-Jun 2026) has **no trade at all**, so there is nothing to compare yet. Of 90 scheduled baseline
attempts: 45 `ILLIQUID` (open interest under 200 lots on a traded strike), 27 `MAX_LOSS_ABOVE_LIMIT` (one lot's max loss
above 2.5% of capital = Rs 5,000), 11 no priced hedge strike, 7 no strike. The strategy stream: 40 max-loss, 35
`UPTREND_BLOCK`, 22 illiquid, 15 `INSUFFICIENT_HISTORY`. No default was changed to get trades. The comparison will be
rerun when the capital is set and more history is stored. (The run also wanted 198 contracts that are not stored: the new
thresholds pick other strikes.)

**Why the earlier run made no roll (traced with the earlier commit's code and thresholds, 9 stocks, Jan-Jun 2026).** The
roll policy took 15 roll decisions (the 13 stops and 2 strike breaches of the baseline):

| Decisions | Outcome | Rule |
|---|---|---|
| 7 | exit | `DOWNTREND_BLOCK` (stock below its 200-day average; RELIANCE x3, ICICIBANK, BHARTIARTL, INFY x2) |
| 4 | exit | `INSUFFICIENT_HISTORY`: fewer than 252 daily bars since an unadjusted price break (HDFCBANK x3, BSE) - issue 25 |
| 2 | exit | no replacement passed: `CREDIT_TOO_SMALL` (RELIANCE call spread), no expiry 20-65 days away (ICICIBANK) |
| 2 | roll approved, not made | BHARTIARTL (2026-04-30) and INFY (2026-06-02) call spreads: the replacement could not be entered at the next open (entry rules at the fill), so the spread was simply closed |

Under the corrected rule a roll is rarer still: closing a stopped spread costs at least twice its credit, and a new spread
at the rules' strikes pays about one credit, so the roll net credit is negative unless the open spread is cheap to close.

**Bull call spread candidate: entry-window problem.** Its no-filter baseline makes one attempt per expiry on the first
session the expiry is inside its 20-65 day window, that is about 65 days before expiry. Stock options hardly trade that far
out: 35 of 36 attempts found no priced strike. The candidate itself (entered on a confirmation day) picks the nearest
expiry at least 20 days away, so it is not affected the same way, but its baseline is not a fair comparison as built. The
window (20-65 days) is not in section 2; it needs the user's decision (for example the same 20-35 days as the put spread).

**Other finding to report (not acted on):** with a 0.20 delta limit and the bought strike 2 strikes out, a spread's credit
after charges is roughly 13-19% of its max loss in the fixtures and in the stored data, so `min_credit_to_max_loss` 0.20
blocks most otherwise valid setups. Together with the 200-lot open-interest minimum and the 2.5% risk budget, section 2
may produce very few candidates. This is reported as an outcome; no default was changed.

### Section 2 parameters: what config.yaml holds now

| Specification name | Value | config.yaml key (`stock_option_selling.`) | Built |
|---|---|---|---|
| sma_long / sma_mid / sma_short | 200 / 50 / 20 | `trend_block.long_sma_days`, `trend_slow_sma`, `trend_fast_sma` | yes |
| new_low_lookback; 52-week low = lowest close over 252 | 20; 252 | `trend_block.extreme_recent_sessions`, `extreme_lookback_sessions` | yes (changed from lowest low to lowest close) |
| rel_strength_lookback / max_lag_pct | 63 / 5 | `trend_block.relative_return_sessions`, `max_underperformance_pct` | yes (was 10) |
| stock_loss_window_days / max_stops / max_pct_capital / cooloff | 90 / 2 / 1.5 / 30 | `campaign_limit.*` | yes (was 5% and 45 days) |
| max_risk_per_position_pct | 2.5 | `max_loss_per_stock_spread_pct` | yes (was 10); whole-lot sizing not built |
| min_credit_to_max_loss | 0.20 | `min_net_credit_pct_of_max_loss` = 20 | yes (was 10) |
| short_delta_max; hedge_strikes_out | 0.20; 2 (2-3) | `max_short_delta`, `hedge_strikes_out` | yes |
| entry_dte_min / max | 20 / 35 | `entry_min_days_to_expiry`, `entry_max_days_to_expiry` | yes |
| profit_target_pct_of_credit; stop_spread_value_multiple | 50; 2.0 | `profit_target_pct_of_credit`, `stop_multiple_of_credit` | yes |
| time_exit_dte; forced_exit_trading_days_before_expiry | 10; 5 | `exit_days_before_expiry`, `forced_exit_trading_days_before_expiry` | yes (was 7 calendar days) |
| max_rolls; min_roll_net_credit_pct_of_width | 1; 10 | `roll.max_rolls`, `roll.min_net_credit_pct_of_width` | yes |
| stuck_time_value_pct | 20 | `roll.stuck_time_value_pct` | yes (was 10; the stuck window is now the forced-exit window) |
| min_open_interest_lots; max_bid_ask_pct_of_mid | 200; 5 | `min_open_interest`, `max_bid_ask_spread_pct` | yes (were 100 and 10) |
| washed_out_drawdown_pct; 63 sessions below sma_long | 25; 63 | `reversal.drawdown_pct`, `reversal.min_sessions_below_long` | yes |
| base_no_new_low_sessions; base_range_contraction | 15; ATR(10) < 0.8 x ATR(20) | `reversal.base_no_new_low_sessions`, `range_sessions`, `range_compare_sessions`, `range_shrink_ratio` | yes |
| fii_streak_sessions | 3 | `bias.streak_days` (existing) | yes |
| iv_rank_lookback | 252 | `options.iv_rank_lookback_days` (existing) | yes |
| market_breadth_min_pct | 40 | - | **no** (MARKET_WEAK not built) |
| max_open_spreads; max_total_risk_pct | 6; 10 | - | **no** (portfolio limits not built) |
| correlation_window / threshold / min_overlap; group_max_positions / risk | 60 / 0.7 / 50; 2 / 5 | - | **no** (CORRELATED_LIMIT, CORRELATION_UNKNOWN not built) |
| iv_falling | IV <= 90% of its 20-session maximum | - | **no** (score factor) |
| expected_move | spot x IV x sqrt(DTE/365) | - | **no** (the profile shows IV x sqrt(holding sessions/252) instead) |
| hist_fall_percentile | 90th percentile of 21-session falls | - | **no** |
| swing_pivot_bars | 5 each side, usable after 5 confirming sessions | - | **no** (support = 20-session low; swings by a % zigzag) |
| vix_spike | up 20% over 5 sessions or above its 80th percentile | `profile.vix_spike_pct` = 15% in one session | **no, differs** |

The config keys keep their earlier names; renaming them to the specification's names, and the full parameter table in
docs/implementation.md (unit, lookback, comparison, missing-data behaviour), are part of M1.

### Requirement-to-code/test table (section 10)

C = compliant, P = partial, G = gap (not built), X = conflict with the specification as built.

| Spec | Requirement | Code | Tests | State |
|---|---|---|---|---|
| 0 | No order code, routes or SDK calls | whole app | `tests/test_safety.py` | C |
| 0 | Sold option needs an adequate hedge to construct, enter or save (hedge further out or nearer the money) | `models.unhedged_short_legs`, `Signal`, `OptionEngine._enter`, `Store.save_backtest` | test_phase4, test_review5_fixes, test_phase9_core (property test), test_phase9_reversal | C |
| 0 | Unknown never passes, never becomes zero | throughout | many | P: holds for what is built; see X rows (events, 5-minute fallback) |
| 0 | No history before 2019-01-22 in tested outcomes or statistics | `backtest.test_start`, reversal scanner, profile view | test_phase4, test_phase9_rules, test_phase9_reversal | P: averages, 52-week levels and the 2-year breach history at decisions in 2019-2020 still read bars before 2019-01-22 |
| 0 | Nothing alerts until approved; paper tracking | `strategies.status`, `reversal.alerts_approved` | test_phase9_core, test_phase9_reversal | C for alerts; paper tracking **not available** (phase 6) |
| 1.1 | Universe: F&O stocks with price history here, filtered by option liquidity | `stock_backtest.liquid_stocks`, `shortlist.universe`, `liquidity_problem` | test_phase9_core | C (the watchlist stocks are the ones with price history) |
| 1.2 | Research candidates and actionable signals kept separate | `shortlist.run_shortlist` (session snapshot) vs `StockOptionSelling.evaluate` on a live context | test_phase9_core | P: the snapshot timestamp is the session date only; the shortlist context is marked LIVE internally; no live stock loop (phase 6) |
| 1.3 | Sold strike beyond the nearest CONFIRMED support/resistance, beyond highest OI, within delta | `stock_selling.assess`, `base.pick_short_strike` | test_phase9_core (3 acceptance tests) | P: support/resistance is the 20-session low/high, not a confirmed pivot |
| 1.4 | Hedge 2-3 strikes out, liquid, within the risk budget | `stock_selling.pick_hedge` | test_phase9_core | C |
| 1.5 | Entry 20-35 days to expiry, never in expiry week | `CreditSpread.pick` | test_phase9_core | C |
| 1.6 | Every candidate: net credit after hedge, charges and slippage; max loss; breakevens; margin; capital-fit label; why-now with values | `assess`, `CreditSpread.finish` | test_phase9_rolls, test_phase9_core | P: capital-fit label not built; in history the credit on the signal is not slipped (fills are) |
| 1.7 | Sizing: risk budget, whole lots, round down, ZERO_SIZE | - | - | G: one lot always; a lot above the budget is `MAX_LOSS_ABOVE_LIMIT` |
| 2 | Parameter table in docs/implementation.md | - | - | G (table above is the start) |
| 3 | UNHEDGED_SHORT, EVENT_BEFORE_EXPIRY, DOWNTREND/UPTREND_BLOCK, STOCK_LOSS_LIMIT, CREDIT_TOO_SMALL, DATA_STALE, MARGIN_UNKNOWN | `stock_selling`, `stock_profile` | test_phase9_core, test_phase9_rules, test_phase9_rolls | C |
| 3 | EVENT_COVERAGE_UNKNOWN | - | - | **X: with no events file a stock passes the event rule today.** Must block actionable signals until a verified, fresh event source covers the stock to expiry |
| 3 | MARKET_WEAK | - | - | G |
| 3 | CORRELATED_LIMIT, CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN, max_open_spreads, max_total_risk_pct | - | - | G (needs positions: phase 6 for live; buildable now for the backtest) |
| 3 | REVERSAL_CONFIRMED lifts only DOWNTREND_BLOCK, only for a put spread below the base low | `stock_selling.assess` | test_phase9_reversal | C |
| 4 | Score 0-100, six equal-weight factors, contributions shown, blocked candidates listed separately | `stock_selling.weighted_score` (four factors, 0-1) | test_phase9_core | G: to be rebuilt in M2 |
| 5.1 | Range and speed profile, 2 and 5 years | `stock_profile.range_speed_profile`, `shortlist.stock_view` | test_phase9_rules | P: swings by a % zigzag not 5-bar pivots; implied move not by the section 2 formula |
| 5.2 | Scenario history, point in time, low sample, "history, not a forecast" | `stock_profile.scenario_history` | test_phase9_rules | P: VIX spike definition differs from section 2 |
| 5.3 | Missing FII/DII, RBI, results: fixtures, "no data", files reported | `shortlist.scenario_triggers`, README, this file | test_phase9_rules | C |
| 6.1 | Exit at the first of target, stop, time exit, forced exit | `exit_calls`, engine `_manage` | test_phase9_core | P: the backtest applies the time exit (10 days); the forced exit (5 trading days) exists in `exit_calls` only |
| 6.2 | Campaigns: realised P&L, hedge cost, charges, best possible outcome, "recovery not possible" | `campaign.py`, store `campaigns` | test_phase9_rolls | C (live recording waits for phase 6) |
| 6.3 | Roll rules | `roll_choices`, `roll_net_credit` | test_phase9_rolls, test_phase9_core | C (correction 1) |
| 6.4 | Stuck position: "exit; rolling earns little" | `stuck_position` | test_phase9_rolls | C |
| 6.5 | No recovery sizing | `assess` (one lot, `sizing_note`) | test_phase9_rolls | C |
| 6.6 | Open-position guidance shown locally; unknown roll inputs never hide the exit | `position_alerts`, `alert_kinds` | test_phase9_rules, test_phase9_reversal | P: MARKET_WEAK missing; guidance is computed but no page shows it (no positions before phase 6) |
| 7 | Washed-out list, base forming, REVERSAL_CONFIRMED, two research candidates, stall zones | `reversal.py`, `shortlist.reversal_watch` | test_phase9_reversal | P: swing high and stall zones by a 5% zigzag, not 5-bar pivots |
| 8.1 | Synchronized five-minute execution, data-incomplete campaigns excluded, no daily fallback | `backtest.intraday_spread_exit` | test_phase9_core | **X: uses a leg's last trade up to 30 minutes old, enters at daily opens, and falls back to the daily close (counted)** |
| 8.2 | No-filter baseline, one attempt per expiry | `OptionEngine.baseline_schedule` | test_phase4, test_review5_fixes, test_phase9_core | C |
| 8.3 | Isolated comparisons of each block; bypassed rules reported | `stock_backtest.rule_effects` | test_phase9_rules | P: blocks yes; score factors no; the list of bypassed rules is not in the report |
| 8.4 | Three exit policies on the same entries | `stock_backtest.policy_runs`, `policy_comparison` | test_phase9_rolls | P: daily check only (correction 3 open) |
| 8.5 | Score bands by the candidates available at each decision time | - | - | G (M2) |
| 8.6 | Reversal analysis | `reversal.reversal_outcomes`, `run_reversal_reports` | test_phase9_reversal | C |
| 8.7 | Point-in-time outcome statistics; exclusions counted | `scenario_history`, `breach_frequency` | test_phase9_rules | P: exclusions are not counted in the output |
| 8.8 | Monthly candidates after blocks and trades taken | - | - | G |
| 8.9 | Open campaigns at the cutoff reported as open, with unrealised P&L kept apart | engine `PERIOD_END` | - | **X: an open spread is closed at the cutoff session's close and counted as a closed trade** |
| 8.10 | Counts, win rate, average win/loss, net, drawdown, profit factor, low sample under 30 | `backtest.stats` | test_phase3, test_phase9_* | C |
| 9.1 | Stock-option capital for Phase 9 backtests | - | - | **Waiting for the user (placeholder received)** |
| 9.2 | Budgeted, resumable download; `--recheck`; universe-selection date and method | `stock_backtest`, `option_history` | test_phase9_core | P: the selection method is recorded; the selection date is not stored with the status |
| 9.3 | Authentication failure stops cleanly | `OptionHistory.error`, `download_blocks` | test_phase9_core | C |
| 9.4 | Standalone / depends on phases 5-6 / research-only list | below | - | C (documentation) |
| 9.5 | Phase 6 and 7 documentation | docs/implementation.md | - | P: Phase 7 addition is there; "followed signal / broke rule" classification and the Phase 6 notes to be added |

**Milestones:** M1 partial (universe, strikes, hedge, entry window, existing blocks, exits, baseline, isolated block
reporting are in; sizing, research/actionable labels, confirmed pivots, MARKET_WEAK, correlation and portfolio blocks,
EVENT_COVERAGE_UNKNOWN, synchronized 5-minute execution and open-campaign reporting are not). M2 not started (score and
bands). M3 partial (campaigns, roll rules, stuck and open-position guidance done; 5-minute exit-policy comparison not).
M4 partial (profiles and scenarios done to the earlier definitions). M5 partial (detector and research comparisons done;
pivots not).

### What Phase 9 does standalone now, and what waits for phases 5 and 6
- **Standalone now:** research candidates after the close (shortlist, profile, scenario history, reversal watch), every
  entry rule and block listed as C above, exit and roll decisions as functions, campaign arithmetic, the backtests and the
  download.
- **Depends on phase 5:** position sizing in whole lots, portfolio risk limits (they can be built for the backtest first).
- **Depends on phase 6:** actionable live signals for stocks (live price, live chain, margin recheck), signal cards with
  the capital-fit label, Telegram alerts, repeating exit calls, manual positions under /journal/, paper tracking.
- **Therefore research-only or unavailable today:** every Phase 9 output. **No paper trading is running.** No alert is sent.

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
| 38 | plan | Whether Breeze provides historical option prices | Fixed 2026-11-13: yes (expired contracts too, to at least 2017, daily/5-minute/1-minute, volume and OI; trades only, no bid/ask). Option backtests use them |
| 39 | review | Engine validated the raw open, not the slipped fill, against stop/target | Fixed 2026-11-13: fill after slippage checked (`FILL_BEYOND_STOP_OR_TARGET`); no real trade affected |
| 40 | review | Incomplete intraday sessions were traded; a missing exit bar or truncated data gave invented EOD exits | Fixed 2026-11-13: sessions need every bar 09:15..15:10; trades crossing a missing bar are dropped; 50 symbol-sessions (2026-05-06) and 1 trade removed |
| 41 | review | ORB range_minutes not a multiple of 5 was silently rounded down | Fixed 2026-11-13: rejected at config load |
| 42 | review | Swing backtests do not check for a stock's missing daily bar inside a holding period (a day the index traded but the stock has no bar) | Fixed 2026-11-13 (phase 4): such a trade is dropped and counted (`DROPPED_MISSING_DAILY_BAR_DURING_TRADE`); a signal is entered only on the next market session. Real data: 0 trades affected, all four swing results identical. Option backtests use the same rule for every leg |
| 43 | 4 | The filtered selling strategies almost never trade in backtests (credit spread 6 trades in 7.4 years, iron condor 1 in 2.4 years): "IV rank at or above 50" rarely coincides with a trending or RANGE regime, because a high VIX usually makes the regime VOLATILE | Waiting for user: decide whether to change the rule (any change is a new version and needs its own backtest) |
| 44 | 4 | The no-filter spread baselines lose after costs (credit spread PF 0.59, condor PF 0.77): stops decided at the close fill at the next open at about 2.6-2.8x the credit, not 2x | Open: phase 9 uses the same exits; consider intraday stop checks on 5-minute option bars (Breeze has them) |
| 45 | 4 | Stock option history only partly stored (RELIANCE, HDFCBANK partial; INFY, SBIN none): about 3,950 Breeze requests were used on 2026-11-13 and the download was stopped there | Waiting for user: add the stocks to `options.backtest.underlyings.long_option` and run `python -m app.options.backtest --strategy long_option --download` on another day |
| 46 | 4 | Margin for spreads must be entered by hand (Breeze's margin calculator is not a data call); none entered yet, so every live spread idea shows MARGIN_UNKNOWN | Waiting for user |
| 47 | 4 | Live option chain refresh, live IV rank and live option ideas not yet seen in market hours (built after the close) | Waiting for user: restart the app in breeze mode and open /options |
| 48 | 4 | Risk-free rate 6.5% for IV/delta is an assumption; dividend yield 0 | Waiting for user (low impact on short-dated options) |
| 50 | review | Phase 4 review: unhedged option signal could be constructed, entered and saved | Fixed 2026-11-13: refused by the Signal model, the backtest entry and Store.save_backtest() |
| 51 | review | Phase 4 review: stock IV history used contracts chosen from later closes | Fixed 2026-11-13: point-in-time contract selection, shared by backtest and Options page |
| 52 | review | Phase 4 review: unequal-width condor reported a breakeven that is not real | Fixed 2026-11-13 |
| 53 | review | Phase 4 review: baseline retried failed days and waited for the previous position | Fixed 2026-11-13: one fixed scheduled attempt per expiry; baselines rerun (no verdict change) |
| 54 | review | After the schedule fix the phase 4 baselines want 31 contracts that are not stored | Fixed 2026-11-14: downloaded (12 requests), index backtests rerun, no verdict changed |
| 55 | 9 | Stock option history is only partly downloaded (9 stocks, Jan-Jun 2026); every stock result is LOW SAMPLE | Open: run `python -m app.options.stock_backtest --download` each day with a fresh Breeze login (3,500 requests a day) |
| 56 | 9 | Today's last download run was stopped by the system because the machine was low on memory; about 1,900 of the day's requests were not used | Waiting for user: rerun the download command (close other programs first) |
| 57 | 9 | Scenario history has no FII/DII, RBI or results dates | Waiting for user: fill `data/fii_dii.csv` and `data/events.csv` (table above) |
| 58 | 9 | The roll-by-rules policy made no roll in the downloaded period; not yet checked which rule refused each of the 13 stopped baseline trades | Closed 2026-11-14: traced (7 falling-stock block, 4 too little history after a price break, 2 no qualifying replacement, 2 approved but not fillable); refusals are now recorded on every trade |
| 59 | 9 | Hedge rule widened: a bought option nearer the money now also covers a sold option (needed for the bull call spread) | Closed 2026-11-14: accepted by the user (specification section 0) |
| 60 | 9 | Bull call spread baseline almost never enters: 65 days before expiry stock options have no traded strike | Open: the entry window of that candidate needs a decision |
| 61 | 9 | Stock daily bars stop at 2026-11-12 while the index has 2026-11-13, so the reversal watch list and profile views show no stock as of the latest session | Waiting for user: run `python -m app.history` |
| 62 | 9 | Assumed thresholds | Closed 2026-11-14: replaced by the section 2 defaults |
| 63 | 9 | Section 9.1: the stock-option capital for Phase 9 backtests was received as a placeholder | Waiting for user: give the rupee amount |
| 64 | 9 | With the section 2 defaults and Rs 2,00,000 capital the stored period has no trade (illiquid 45, max loss 27 of 90 baseline attempts) | Reported; no default changed |
| 65 | 9 | Events: with no verified event source a stock passes the event rule; the specification requires EVENT_COVERAGE_UNKNOWN to block | Open (M1) |
| 66 | 9 | 5-minute backtest execution conflicts with section 8.1 (stale leg price, daily entry and fallback); exit-policy comparison not yet on 5-minute bars | Open (M1 / M3) |
| 67 | 9 | Open campaigns at the cutoff are closed and counted as trades; the specification wants them reported as open | Open (M1) |
| 68 | 9 | Bull call candidate's 20-65 day window is not in section 2 and its baseline almost never enters | Waiting for user: decide the window |
| 49 | 4 | At Rs 2,00,000 capital one lot of most index spreads risks more than 2.5% of capital (for example a 200-point NIFTY spread: about Rs 10,000 to 11,000) | For phase 5: sizing will give zero lots unless the width or the limit changes |
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
| 2026-11-13 | Backtest entries need both the raw open and the slipped fill strictly between stop and target |
| 2026-11-13 | An intraday session is backtested only if it has every 5-minute bar 09:15..15:10 (to the exit bar); trades crossing a missing bar are dropped, never given an invented exit |
| 2026-11-13 | ORB range_minutes must be a multiple of 5 |
| 2026-11-13 | Phase 4: option backtests on the breeze source use stored Breeze option bars (traded prices); model prices only for the sim source, labelled "payoff simulation" |
| 2026-11-13 | Phase 4: expiry dates of expired contracts are found from the option history itself (a date is an expiry if the at-the-money call with that expiry traded on that date); stock monthly expiries follow the index monthly expiry |
| 2026-11-13 | Phase 4: backtest trades are one lot at today's lot size from the instrument list (past lot sizes are not in the list); sizing is phase 5 |
| 2026-11-13 | Phase 4: IV rank = position of the current value between the past year's low and high; India VIX for indices, stored at-the-money implied volatility for stocks; unknown = no signal |
| 2026-11-13 | Phase 4: margins are entered by the user (`data/margins.csv` or the Options page); Breeze's margin calculator is not used; unknown or older than 7 days = MARGIN_UNKNOWN |
| 2026-11-13 | Phase 4: spread exits are decided at a close and filled at the next session's open; nothing is held to settlement; one entry attempt per expiry per stream |
| 2026-11-13 | Phase 4: in backtests the highest-OI rule and the margin rule are not applied (not knowable for past dates); both are enforced live |
| 2026-11-13 | Issue 42: a daily-bar trade open across a session with no bar for that stock is dropped, never given an exit from a later bar |
| 2026-11-13 | NSE charges per circular NSE/FA/73061 (effective 2026-03-01, user): cash Rs 307/crore and options Rs 3,553/crore of premium each side, IPFT included; IPFT rates set to 0. Cash was 0.00307% + a separate 0.0000001% IPFT before (IPFT counted twice); corrected |
| 2026-11-13 | Review fix: the hedge rule (same right, same expiry, further out, enough shares) is enforced by the Signal model, the option backtest entry and Store.save_backtest(), not only by the strategies |
| 2026-11-13 | Review fix: a stock's implied-volatility history uses a contract only from the day its strike was chosen |
| 2026-11-13 | Review fix: no-filter baselines make one attempt per expiry (or month) on a date fixed by the calendar; skipped attempts are recorded and never retried; baseline positions of different expiries may overlap |
| 2026-11-13 | Review fix: option backtest reports are labelled "excludes margin and bid/ask eligibility checks"; both stay mandatory for live signals (a live context cannot switch the margin rule off) |
| 2026-11-14 | Phase 9 core built before phases 5 and 6 (user's instruction); cards, Telegram alerts and the manual position form wait for phase 6 |
| 2026-11-14 | Universe for now: the watchlist stocks (Nifty 50) with option contracts, because other F&O stocks have no price history here. Download and backtest priority: the 30 with the highest median daily traded value (close x volume) over the last 60 development sessions. This picks stocks by today's liquidity (a selection bias to keep in mind) |
| 2026-11-14 | Support = lowest low and resistance = highest high of the last 20 sessions. Shortlist "distance" score: more room between the price and that level scores higher |
| 2026-11-14 | Forced exit for stock spreads: at the open of the first session within 7 days of expiry, so always before expiry week |
| 2026-11-14 | 5-minute exit check: a leg that did not trade in a bar is priced at its last trade if at most 30 minutes old, otherwise the cost is unknown and nothing is decided; the fill is the next 5-minute bar's open. Entries are the same in both exit checks |
| 2026-11-14 | Backtest closes a spread at the next open when the stock closes beyond the sold strike (`exit_on_strike_breach`); a trend flip is an alert only |
| 2026-11-14 | Download order: newest year first, most liquid stock first; a report covers only stocks and years that are completely stored |
| 2026-11-14 | A request with no reply (timeout) is repeated twice; an error reply from Breeze (session, login, rate limit) stops the download at once |
| 2026-11-14 | Mirror of the falling-stock block for calls uses the reason code UPTREND_BLOCK (DOWNTREND_BLOCK for puts) |
| 2026-11-14 | Campaign limit: a stopped trade is one closed at the stop; the cumulative loss limit is a % of capital (5%) |
| 2026-11-14 | Roll rule (a) is the NEW spread's credit after its hedge and charges; the cost of closing the old spread is shown beside it and in the campaign's best possible outcome. Rule (b) "toward the market" is read as: lower strike for puts, higher for calls. A roll does not need the entry trend and IV-rank rules; it may use the next monthly expiry up to 65 days away; it is always one lot |
| 2026-11-14 | Exit-policy comparison uses the daily exit check for all three policies (rolls need both spreads priced at the same open). "Roll every month": no stop, and a losing spread is rolled into the next month at the forced exit; a spread in profit there is closed |
| 2026-11-14 | Hedge rule widened for debit spreads: a sold option is covered by a bought option of the same right and expiry at any different strike with at least as many shares. Two review tests were changed from "not further out" to "same strike". User to confirm |
| 2026-11-14 | Reversal: "washed out" needs every session of the last 3 months more than 30% below the 52-week high and the close below the 200-day average; "shrinking ranges" compares the last 10 sessions with the 30 before; the scanner uses bars from 2019-01-22 only. Candidates are shown for 20 sessions after confirmation while the base low holds. Nothing alerts until `reversal.alerts_approved` is set |
| 2026-11-14 | Scenario history: a move runs from the close of the trigger day to the close 30 calendar days later; only cases whose 30 days had passed are counted |
| 2026-11-14 | Final Phase 9 specification received; it replaces earlier Phase 9 instructions. Corrections 1, 2, 4 and 5 done; correction 3 open |
| 2026-11-14 | A roll's replacement is judged by the full entry rules, on the same side; the relaxed roll mode and the 65-day roll window are removed (specification section 6) |
| 2026-11-14 | In history the four roll legs are priced at the last trade moved against us by the options slippage once; live they are the bid and the ask with no further slippage |
| 2026-11-14 | "Washed out": 25% or more below the 52-week high (highest high of 252 sessions) and at least 63 sessions IN A ROW closing below the 200-day average; "base range contraction": mean true range of the last 10 sessions below 0.8 x that of the last 20 |
| 2026-11-14 | Stuck window = the forced-exit window (5 trading days, counted as weekdays unless a calendar is passed). Time exit: 10 calendar days before expiry |
| 2026-11-14 | Phase 9 tests pin their fixtures' thresholds (tests/test_phase9_core.py `FIXTURE_SOS`) so a default change does not move them; one test checks the shipped defaults against section 2. Reason for the test changes: the specification changed the roll rule, the exit windows and the defaults |
