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
| - | Phase 9 time-ordering fixes (events of all stocks in time order; intraday roll correlation from completed sessions) | Fixed and tested; stored-data rerun: one trade changed (30 stocks 2026 synchronized: -3,688 -> -7,584), everything else identical | 613 passing (whole suite) | 2026-11-15 |
| - | Review fixes (Codex review of 968d410..267f09b): reversal windows in market sessions, excluded across price breaks and missing sessions; every_session includes confirmations; scenario triggers bounded to [floor, as-of]; window accounting | Fixed and tested; stored-data rerun: confirmations identical, baselines slightly changed, scenario history unchanged; still LOW SAMPLE | 672 passing (whole suite) | 2026-11-16 |
| - | Phase 9 M5: reversal outcome study against matching baselines (washed out and not confirmed; every session) | Built and tested; on stored data LOW SAMPLE (26 cases), no conclusion | 664 passing (whole suite) | 2026-11-16 |
| - | Phase 9 M4: profiles and scenario history - exclusions counted, breach history from 2019-01-22 at every decision, VIX spikes from post-2019 VIX only | Built and tested; stored-data results unchanged | 661 passing (whole suite) | 2026-11-16 |
| - | Review fixes (Codex review of 2e3e2c4..42784c7): pending entries keep a risk reservation until they fill; settlement hedges allocated once across the position | Fixed and tested; stored-data rerun identical | 655 passing (whole suite) | 2026-11-16 |
| - | Phase 9 M3 follow-ups: settlement day count by the holiday calendar (conservative when it does not cover the expiry month); forced exit at E-5 in the backtest | Built and tested; stored-data strategy results unchanged | 647 passing (whole suite) | 2026-11-15 |
| - | Phase 9 M3: exits, rolls, campaigns (built earlier) and settlement readiness P9.29-P9.32 | Built and tested as rules and estimates; sending, repeats and marking a position closed wait for phase 6 | 638 passing (whole suite) | 2026-11-15 |
| 5 | Risk engine | Built and tested on fixtures: sizing (cash and options), capital pool, selling-spread count, sector and correlation limits, daily stop and monthly pause, drawdown halve/pause (deposits excluded), kill switch, reason codes, capital log, `/api/risk`. Live decisions are all NO_TRADE (PORTFOLIO_UNKNOWN) until the phase 6 journal records positions | 848 passing (whole suite) | 2026-11-16 |
| 6 | Live signals and alerts (paper mode) | Not started | | |
| 7 | Journal and weekly scorecard | Not started | | |
| 8 | LLM layer and go-live gate | Not started | | |
| 9 | Stock option selling: CORE built early (2026-11-14, before phases 5 and 6, by the user's instruction) | Core built and tested: rules, shortlist, exits and rolls, campaigns, falling-stock block, campaign limit, profile, scenario history, alerts, reversal detector, backtest with baseline. Option history partly downloaded (9 stocks, Jan-Jun 2026): results so far are LOW SAMPLE. Signal cards, Telegram alerts, manual position form, risk hooks and paper tracking wait for phase 6 | 526 passing (whole suite) | 2026-11-14 |
| - | Review fixes (Codex review of 782e870..2e3e2c4, 6 findings) | Fixed and tested: correlation unknown through unheld stocks, declared board-meetings coverage, synchronized marks, per-block completion (KOTAKBANK 2025 and ETERNAL 2020 no longer complete), repo prices out of the tracked docs, future-dated rows flagged; no backtest rerun | 595 passing (whole suite) | 2026-11-15 |

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
| 3 | Exit-policy comparison on synchronized five-minute bars | **Done (code and tests; no real-data result yet, see the rerun note).** The 5-minute execution was rebuilt to section 8.1: a spread is priced only from 5-minute bars in which every leg traded; entry at the first such bar of the session after the signal; stop and target on those bars' closes; every fill at the NEXT synchronized bar's open with slippage and costs; no stale leg price, no combining of separate highs and lows, no daily fallback. A campaign with a session that has no synchronized bar (or whose 5-minute bars are not stored) is data-incomplete and left out of every figure; the campaigns and the sessions are counted in the report. The three exit policies run on the same recorded entries under both executions (`stock_backtest.policy_comparison`). Tests: `test_ACCEPTANCE_a_spread_is_priced_only_from_bars_in_which_every_leg_traded`, `..._synchronized_stop_is_decided_on_a_bar_close_and_filled_at_the_next_synchronized_open`, `..._a_session_without_a_synchronized_bar_makes_the_campaign_data_incomplete_not_a_daily_fallback`, `..._report_compares_the_three_exit_policies_per_campaign_on_daily_and_synchronized_bars`. |
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
| 8.1 | Synchronized five-minute execution, data-incomplete campaigns excluded, no daily fallback | `backtest.synchronized_bars`, `sync_decision`, `_enter_sync`, `_manage_sync`, `_sync_exit` | test_phase9_core (4 tests) | C for credit spreads (correction 3). P: the hold-through-gap sensitivity analysis is not built; the debit spread candidate still runs on daily bars |
| 8.2 | No-filter baseline, one attempt per expiry | `OptionEngine.baseline_schedule` | test_phase4, test_review5_fixes, test_phase9_core | C |
| 8.3 | Isolated comparisons of each block; bypassed rules reported | `stock_backtest.rule_effects` | test_phase9_rules | P: blocks yes; score factors no; the list of bypassed rules is not in the report |
| 8.4 | Three exit policies on the same entries | `stock_backtest.policy_runs`, `policy_comparison` | test_phase9_rolls | C in code, on daily and synchronized 5-minute execution; no real-data result yet (no trades at the section 2 defaults and Rs 2,00,000) |
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
EVENT_COVERAGE_UNKNOWN and open-campaign reporting are not; synchronized 5-minute execution is in). M2 not started (score and
bands). M3 partial (campaigns, roll rules, stuck and open-position guidance and the exit-policy comparison on both executions
are done in code; no real-data comparison yet).
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

## Phase 9: the user's answers of 2026-11-14 (evening), the download, and M1 progress

No holdout opened, no default tuned, Phase 5 not started. Whole suite: 535 passing.

### Answers applied
| # | Answer | What was done |
|---|---|---|
| 1 | Capital Rs 10,00,000 for the Phase 9 backtests; `max_risk_per_position_pct` unchanged | `stock_option_selling.backtest.capital_inr: 1000000`; `stock_backtest.phase9_settings` runs every Phase 9 backtest at it (2.5% = Rs 25,000 a position). `account.capital_inr` is still Rs 2,00,000, so the research pages (shortlist, reversal watch) still measure against Rs 2,00,000: **say if they should use Rs 10,00,000 too.** |
| 2 | Strict data-incomplete reading stays primary; report the number and share excluded; above 30% add a labelled sensitivity run at the daily close | `stock_backtest.incomplete_sensitivity`; the report prints both counts and shares. On the stored data the share is 0% (below), so no sensitivity run was made. |
| 3 | Bull call candidate: 20-35 days to expiry | `reversal.bull_call.entry_max_dte: 35`. |
| 4 | EVENT_COVERAGE_UNKNOWN | Live and research candidates need a `COVERAGE` row in events.csv for the stock, retrieved within `event_coverage_max_age_days` (7) and covering the expiry. Without it there is no actionable signal; the research candidate stays listed with the reason. Backtests list the event check under "bypassed checks" as not available historically (every option report now has that list). |
| 5 | Report the credit / max-loss distribution, then stop | Done (below). `min_net_credit_pct_of_max_loss` is **0 (not applied)** until the user sets it. |
| 6 | Download after `--recheck` | Done (below). |

**events.csv coverage row format** (one row per stock; add the three columns to the header):
`date,type,note,symbol,source,retrieved_at,covered_through` with `type` = `COVERAGE`, the stock's `symbol`, `source` (where
the announcements were checked), `retrieved_at` (the day you checked; the `date` column may be left blank) and
`covered_through` (the last date the check covers; it must be on or after the option's expiry). Example:
`,COVERAGE,,RELIANCE,NSE corporate filings,2026-11-13,2026-12-31`.

### Download (2026-11-14)
After `--recheck`, the download used the rest of the day's budget: **3,500 of 3,500 requests used today**. It stopped at the
budget, not on an error; one dropped connection was retried and the run carried on. **Completely stored now: January to
5 June 2026 for 25 of the 30 priority stocks** (HDFCBANK, RELIANCE, ICICIBANK, SBIN, BHARTIARTL, INFY, BSE, LT, M&M, ETERNAL,
TCS, AXISBANK, BAJFINANCE, KOTAKBANK, INDIGO, SHRIRAMFIN, MARUTI, BEL, TATASTEEL, ITC, SUNPHARMA, HINDALCO, COALINDIA,
ADANIENT, ONGC). HCLTECH is partly stored; ADANIPORTS, NTPC, TITAN, ULTRACEMCO and all earlier years are not. Universe
selection: the 30 watchlist stocks with the highest median daily traded value over the 60 sessions before 2026-06-08
(the holdout start), selected on 2026-11-14 - a current liquidity-selected universe, not historically unbiased. The next
run needs a fresh Breeze login and a new day's budget; at roughly 100 requests per stock-year the remaining history is many
days of downloads.

### Net credit / max loss, without outcomes (answer 5) - the user sets the minimum from this
25 stocks, 2026-01-01 to 2026-06-05, one lot, capital Rs 10,00,000. Each observation is the first day a spread on an
expiry passed every other rule (the minimum-credit rule off; the campaign limit off because it depends on results). No
price after the decision day and no trade result was read (`python -m app.options.stock_backtest --credit-distribution`;
`data/diagnostics/credit_ratio_distribution.json`).

| Spreads | n | 25th pct | Median | 75th pct | Min | Max | >= 0.10 | >= 0.15 | >= 0.20 |
|---|---|---|---|---|---|---|---|---|---|
| Passing every other rule | 28 (LOW SAMPLE) | 0.044 | 0.100 | 0.135 | 0.009 | 0.186 | 50.0% | 14.3% | 0% |
| - put spreads | 7 | 0.052 | 0.147 | 0.172 | | | | | |
| - call spreads | 21 | 0.040 | 0.100 | 0.132 | | | | | |
| No-filter structure (baseline attempts), for reference | 58 | 0.097 | 0.123 | 0.147 | 0.000 | 0.189 | 72.4% | 24.1% | 0% |

No spread in the stored data reaches 0.20. Five months of one year and 28 observations: low sample.

### Report at Rs 10,00,000 (25 stocks, 2026-01-01 to 2026-06-05, one lot per trade, minimum credit not applied)
`data/diagnostics/stock_report_2026-11-14_10lakh.txt`. Run before the MARKET_WEAK block was added. Everything here is five
months of data: low sample, no conclusion. Whole-lot sizing is not built, so each trade is still one lot.

| | Execution | Trades | Win % | Net after costs (Rs) | Per trade (Rs) | PF |
|---|---|---|---|---|---|---|
| strategy | synchronized 5-minute | 22 (LOW SAMPLE) | 59.1 | -146 | -7 | 0.98 |
| baseline (both sides) | synchronized 5-minute | 53 | 66.0 | -4,759 | -90 | 0.86 |
| baseline bull put / bear call | synchronized 5-minute | 23 / 30 | 56.5 / 73.3 | -6,985 / +2,226 | | 0.63 / 1.16 |
| strategy | daily close | 22 (LOW SAMPLE) | 54.5 | -4,819 | -219 | 0.68 |
| baseline (both sides) | daily close | 52 | 59.6 | -26,842 | -516 | 0.52 |

- **Data-incomplete campaigns (answer 2):** strategy 0 of 22 (0%), baseline 0 of 53 (0%) left out; one baseline entry had
  no synchronized bar on its entry day and was not made. Under the research "roll every month" policy 1 of 53 baseline
  campaigns was left out. Below 30%, so no sensitivity run.
- **Baseline schedule:** 250 attempts, 53 entered. Skipped: 159 illiquid (open interest under 200 lots on a traded strike),
  15 no strike, 14 no priced hedge, 3 gapped past stop or target, 2 max loss above the Rs 25,000 budget, 2 too little
  history, 1 fill beyond the band, 1 no synchronized bar at entry.
- **Exit policies on the same entries, per campaign:**

| Entries | Execution | Stop | Roll by the rules | Roll every month (research only) |
|---|---|---|---|---|
| baseline, 53 | synchronized 5-minute | -4,759 | -4,759 (0 rolls) | +21,000 (0 rolls made; 52 campaigns, 1 left out; worst -13,015) |
| baseline, 52 | daily | -26,842 | -26,842 (0 rolls) | -27,763 (0 rolls made; worst -21,924) |
| strategy, 22 | synchronized 5-minute | -146 | -146 (0 rolls) | +2,071 (worst -4,913) |
| strategy, 22 | daily | -4,819 | -4,819 (0 rolls) | -2,212 |

  Roll by the rules made no roll. Refusals on the baseline (synchronized): IV rank unknown 6, IV rank too low 4, no trend
  2, entry rules now on the other side 2, strike not better 1, no strike 1, too little history 1. On the strategy entries:
  roll net credit too small 2, strike not better 2, illiquid 1, IV rank 2. "Roll every month" made no roll either (the next
  month's strikes were illiquid or unpriced on the day); its numbers differ from "stop" only because it has no stop.
- **Rule effects on the baseline (synchronized):** with the trend block 19 trades, +3,934 (PF 1.58); the campaign limit
  changed nothing; exiting on any alert +3,452 (18 alert exits, 16 of them the regime turning VOLATILE or EVENT).
  The strategy without the trend block: 35 trades, +2,850.
- **Reversal candidates:** 25 confirmations in 18 stocks (24 with a complete outcome window): 10 / 20 / 40 sessions later
  the median move was -0.7% / +1.4% / +5.7%, up in 42% / 58% / 58% of cases, none made a new low within 40 sessions (LOW
  SAMPLE). Neither candidate had a confirmation in the stored months. Baselines: put spread 22 trades, -16,959; bull call
  spread with the 20-35 day window 59 trades of 125 attempts, -116,041 (PF 0.36).

### M1 progress since the requirement table
| Gap | State |
|---|---|
| EVENT_COVERAGE_UNKNOWN (was a conflict: no events meant a pass) | **Done**: `stock_selling.event_coverage`, `manual_data` COVERAGE rows; tests `test_ACCEPTANCE_event_coverage_unknown_blocks_an_actionable_signal_and_keeps_the_research_candidate`, `test_events_csv_coverage_rows_parse_and_round_trip` |
| Synchronized 5-minute execution; exit policies on it | **Done** (correction 3) |
| MARKET_WEAK | **Done**: `stock_profile.market_weak`, `breadth_pct`, `OptionEngine.breadth_at`, `shortlist.breadth_on`; `market_block` in config (breadth of the watchlist stocks above their 50-session average, minimum 40%; NIFTY against its 200-day average). Tests `test_ACCEPTANCE_market_weak_blocks_put_selling_and_unknown_market_data_blocks_too`, `test_backtest_market_block_uses_breadth_known_at_each_close`. Also an open-position alert and rows in the rule-effect table |
| Bypassed checks listed in every option report | **Done** (`backtest.BYPASSED_CHECKS`) |
| Whole-lot sizing and ZERO_SIZE | Open |
| Confirmed pivots (5 bars each side) for support, resistance, swings, stall zones | Open |
| CORRELATED_LIMIT, CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN, max_open_spreads, max_total_risk_pct | Open |
| Research / actionable labels with the snapshot timestamp | Open |
| Open campaigns at the cutoff reported as open | Open |
| Config keys renamed to the specification's names; full parameter table in docs/implementation.md | Open |
| Monthly candidates and trades; hold-through-gap sensitivity; VIX spike and expected-move definitions of section 2 | Open |

## Phase 9 M1 completed in code (2026-11-15) and the end-of-M1 rerun

Decisions of 2026-11-14 applied: `min_credit_to_max_loss` 0.10 (chosen by the user from the pre-outcome distribution, not
from results); the research pages use the Phase 9 capital Rs 10,00,000; MARKET_WEAK applies to put selling only, with
MARKET_UNKNOWN for missing data (confirmed); downloads complete 2023-2026 for every priority stock before any earlier
year (`backtest.download_first_years_from: 2023`). Phase 5 not started; no holdout opened; no default tuned.

### What M1 now contains
| Item | Code | Tests |
|---|---|---|
| Whole-lot sizing within `max_risk_per_position_pct` (2.5% = Rs 25,000), rounded down; ZERO_SIZE; a roll never grows the position; margin for the whole position | `app/options/sizing.py`, `stock_selling.pick_hedge`, `reversal.BullCallSpread` | `test_ACCEPTANCE_sizing_is_whole_lots_within_the_risk_budget_rounded_down` |
| Confirmed 5-bar pivots for support, resistance, the last swing high, stall zones and the range profile; NO_CONFIRMED_LEVEL | `stock_profile.pivots`, `alternating`, `support_resistance`; `reversal.ReversalScanner` | `test_ACCEPTANCE_support_is_a_confirmed_pivot_used_only_after_its_five_later_sessions`, pivot tests |
| CORRELATED_LIMIT (6 spreads, 10% total risk, groups of same sector or correlation >= 0.7: 2 positions, 5% risk), CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN; in the backtest every stock runs side by side (`OptionEngine.run_portfolio`), entries and rolls are checked | `app/options/portfolio.py`, `backtest.iter_stream`/`run_portfolio` | `tests/test_phase9_portfolio.py` |
| Research candidates versus actionable signals: every shortlist row is a research candidate with its snapshot timestamps; actionable needs market hours, a LIVE price, a chain no older than 15 minutes with bid and ask on every leg, a margin for the complete spread, known positions within the portfolio limits | `app/options/actionable.py`, `shortlist.run_shortlist` | `test_ACCEPTANCE_a_signal_is_actionable_only_with_live_fresh_known_inputs`, `..._shortlist_rows_are_labelled_research...` |
| Positions open at the cutoff reported as open: marked after estimated liquidation costs; out of trade counts, win rate and profit factor; in equity and drawdown | `backtest.option_stats`, `OPEN_AT_CUTOFF` | `test_ACCEPTANCE_a_position_open_at_the_cutoff_is_reported_open_not_as_a_closed_trade` |
| Config keys renamed to the specification's names; full parameter table | `app/config.py`, `config.yaml`, docs/implementation.md | `test_shipped_defaults_are_the_section_2_values` |
| Monthly candidates after blocks and trades taken, shortages against 3-8 | `stock_backtest.monthly_candidates` | `test_ACCEPTANCE_monthly_candidates_after_blocks_and_trades_taken_are_reported` |
| Section 2 vix_spike (up 20% over 5 sessions or above the 80th percentile of 252 closes) and expected_move (spot x IV x sqrt(DTE/365)) | `stock_profile.vix_spike_at`, `expected_move` | VIX and expected-move tests in `test_phase9_rules.py` |

Correlated groups of the 25 tested stocks on 2026-06-05: sizes 13, 3, 3, 2, 1, 1, 1, 1 (sectors plus correlation >= 0.7;
the 13 is the financial-services group); no pair without a measurable relationship.

### End-of-M1 rerun with every block applied (25 stocks, 2026-01-01 to 2026-06-05, Rs 10,00,000, whole lots)
`data/diagnostics/stock_report_2026-11-15_M1.txt`. **This report is INCOMPLETE: the new strike rules (pivots, sizing) want
430 option contracts that are not stored.** The stored marks of "complete" were made under the earlier rules; the download
plan version is raised so the next `--download` checks every stock-year again. Five months of one year: LOW SAMPLE, no
conclusion.

| | Execution | Trades | Win % | Net after costs (Rs) | Per trade (Rs) | PF |
|---|---|---|---|---|---|---|
| strategy | synchronized 5-minute | 7 (LOW SAMPLE) | 71.4 | +405 | +58 | 1.07 |
| baseline (both sides) | synchronized 5-minute | 32 | 71.9 | +2,601 | +81 | 1.09 |
| baseline bull put / bear call | synchronized 5-minute | 13 / 19 | 76.9 / 68.4 | +9,058 / -6,458 | | 2.14 / 0.67 |
| strategy | daily close | 8 (LOW SAMPLE) | 62.5 | -5,532 | -692 | 0.55 |
| baseline | daily close | 27 (7 more open at the cutoff) | 66.7 | -17,760 | -658 | 0.62 |

- Verdict on the synchronized run: no edge versus the baseline (low sample).
- Data-incomplete campaigns: 0 of 7 (strategy) and 0 of 32 (baseline); under 30%, no sensitivity run.
- Portfolio limits on the strategy: 11 entries checked, none refused (one position at a time in a group of 1).
- Baseline schedule: 250 attempts, 32 entered; 125 illiquid, 40 too little history, 28 no strike, 10 no confirmed level,
  8 no priced hedge, 3 zero lots, 3 without 5-minute bars or a synchronized bar at entry, 1 gapped.
- Strategy blocks: up-trend block 50, too little history 48, market weak 44, illiquid 37, no strike 31, credit too small 26.
- Monthly candidates (3-8 is a reporting expectation only): 11 candidates and 7 trades in six months; January 0/0,
  February 1/1, March 3/1, April 6/4, May 0/0, June 1/1. Five months are short. No rule was loosened.
- Exit policies on the same entries (synchronized): stop and roll-by-rules identical (no roll passed: refusals IV rank
  unknown 3, IV rank too low 2, no trend 2, market weak 1, strike not better 1 on the baseline); roll every month (research
  only) +20,195 on 29 baseline campaigns with a worst campaign of -13,015, and 3 campaigns not closed or excluded.
- Rule effects on the baseline (synchronized): trend block 14 trades +4,873 (PF 1.59); minimum credit 24 trades +3,134;
  market block 19 trades -6,458; portfolio limits 18 trades +5,335; exit on any alert +11,506 (16 alert exits, 11 of them
  the regime turning VOLATILE or EVENT). Strategy without the market block: 9 trades +8,536. All low sample.
- Reversal study (49 watchlist stocks, 2020-2026 development period, pivots): 27 confirmations in 19 stocks; 10/20/40
  sessions later median -1.4% / +1.2% / +1.8%; 3.8% made a new low within 40 sessions (low sample). Candidates: no
  confirmation inside the stored months. Baselines: put spread below the base low 8 trades +551; bull call spread
  (20-35 days, whole lots) 47 trades, -2,36,340, PF 0.47.

### Decisions recorded for M1 (2026-11-15)
- Support and resistance: nearest confirmed 5-bar pivot low below / high above the close among the last 252 sessions
  (`pivot_lookback_sessions`, a new definition). A pivot uses the bar's low / high; the 52-week low uses closes, as section 2 says.
- The reversal "last swing high" is the newest confirmed pivot high (its high); stall zones are confirmed pivot lows of the
  52-week window above the price.
- Sizing: lots = floor(budget / one lot's cost-inclusive max loss), then reduced while the max loss of n lots (with n lots'
  charges) is above the budget. A roll's replacement is capped at the old position's lots.
- Portfolio: two positions in the same stock are two positions of one group. Sector from the watchlist; an unknown sector
  blocks (CORRELATION_UNKNOWN). Returns are not bridged over a missing session or a price break. Streams decide in
  liquidity order within a day. The baseline runs without the portfolio limits (shown as a separate row with them).
- Open at the cutoff: marked at the last tested close after estimated closing charges; the campaign's realised legs stay
  realised. A position with no price at the cutoff is counted as `valuation_incomplete`.
- Research pages: always research candidates; nothing is actionable until positions are recorded (PORTFOLIO_UNKNOWN).
- vix_spike: a part without enough history counts only if the other part says yes; otherwise the state is unknown and
  raises no alert. A scenario case is the first day of a spike episode.
- Score factors and bands are M2 (below / next).

## Phase 9 M2: the candidate score and score bands (2026-11-15)

Built and tested: `app/options/score.py` (six equal-weight factors, point rules written in the module and in
docs/implementation.md before any result), the score on every signal and assessment, the shortlist ranked by it with
each factor's points and contribution, score bands by each day's candidates, and each factor against the baseline.
`tests/test_phase9_score.py` (7 tests); whole suite 557 passing.

### Result on the stored data (same run as M1: 25 stocks, Jan-Jun 2026, INCOMPLETE - 430 contracts not stored)
`data/diagnostics/stock_report_2026-11-15_M2.txt`. **The cross-sections are too small for score bands to mean anything:**
22 decision days had candidates, 20 of them only one, 1 had two and 1 had three. So almost every trade falls in the
"small cross-section" band.

| Band (strategy trades without portfolio limits, synchronized execution) | Trades | Net after costs (Rs) |
|---|---|---|
| top third | 0 | - |
| middle third | 1 | -2,882 |
| bottom third | 1 | +1,361 |
| small cross-section (under 3 candidates that day) | 5 | +1,926 |

Baseline trades by score factor at entry (32 trades; fixed bins under 33.3 / 33.3-66.7 / 66.7 and more): every bin has
fewer than 30 trades, so no factor is evidence either way. Total score: high 4 trades (-52 per trade), middle 20 (+246),
unknown 8 (-265; IV history missing). Score was not used to select anything; nothing was tuned.

### Decisions recorded for M2
- Point rules as in docs/implementation.md ("Score point rules"); a 90th-percentile 21-session fall of zero or less gives
  that half of strike_distance in full (the stock never fell that far in its worst tenth of windows).
- A factor whose inputs are unknown has no points; the total is then "incomplete" and ranks after every scored candidate.
- Relative strength uses the sector peers among the watchlist stocks (median of their 63-session returns); a stock with
  no peer uses the index only.
- Score bands use the strategy's trades without the portfolio limits (each stock trades whenever flat), each placed in
  its decision day's cross-section of candidates (every block applied except the campaign limit and the portfolio
  limits, which depend on results and positions). Ties: by stock, then expiry, then side.

## Phase 9 download of 2026-11-15 and rerun on what is stored

Download (fresh Breeze login): the slow offline `--recheck` was stopped by the user's instruction after ~55 minutes
(it had confirmed 6 stock-years); then `--download`. Today's budget was raised **for this day only** to 4,500 requests
(user: not a trading day, live app not running; hard stop 4,800) through a one-off run setting, not in config.yaml, which
keeps 3,500. Two runs: 3,500 + 1,000 = 4,500 requests, both stopped on the budget; no authentication or memory error (one
DNS failure on a single request at 08:37 IST, recovered). 41 stock-years completed.

Complete now (plan version 6): **2026 for all 30 priority stocks; 2025 for 16** (HDFCBANK, RELIANCE, ICICIBANK, SBIN,
BHARTIARTL, INFY, BSE, LT, M&M, ETERNAL, TCS, AXISBANK, BAJFINANCE, KOTAKBANK, INDIGO, SHRIRAMFIN). MARUTI 2025 was cut
off by the budget. Still to do: 2025 for 14 stocks, then 2024 and 2023.

Rerun (offline, every block applied, Rs 10,00,000, whole lots). Both reports still list contracts not stored (46 and 20):
the download pass replays the main, rule-effect, roll-policy and reversal streams, but not the portfolio-gated runs,
score scans or exit-on-alert rows, which can want other strikes. Low sample throughout; nothing tuned.

| Report | Execution | Strategy trades | Strategy net (Rs) | Baseline trades | Baseline net (Rs) | Verdict |
|---|---|---|---|---|---|---|
| 16 stocks, 2025-01-01..2026-06-05 (351 sessions) | synchronized 5-minute | 19 | -16,085 (PF 0.48) | 91 | +2,382 (PF 1.03) | no edge vs baseline |
| same | daily close | 19 | -20,933 | 88 (+4 open at cutoff) | -16,107 | - |
| 30 stocks, 2026-01-01..2026-06-05 (103 sessions) | synchronized 5-minute | 12 | -7,779 (PF 0.53) | 50 | -15,951 (PF 0.71) | no edge vs baseline |
| same | daily close | 12 | -14,309 | 41 (+9 open at cutoff) | -79,291 | - |

CORRELATED_LIMIT: **refused no strategy entry** in either report (22 and 16 entries checked; one position at a time in a
group of 1 or 2). It blocked 1 entry in "strategy without the trend block" (16 stocks) and 1 in "strategy without the market
block" (30 stocks). On the no-filter baseline with the portfolio limits added it blocked 39 entries (16 stocks: 57 trades,
+12,830) and 30 (30 stocks: 21 trades, +3,457). Correlated groups on 2026-06-05: 12, 2, 1, 1 (16 stocks); 19, 3, 3, 1, 1, 1,
1, 1 (30 stocks).

Monthly candidates: 16 stocks 22 candidates / 21 trades in 18 months (16 short); 30 stocks 16 / 14 in 6 months (5 short).
Score bands: still almost all "small cross-section" (1 candidate on 38 of 42 and 29 of 34 decision days).
Reports: `data/diagnostics/stock_report_2026-11-15_16stocks_2025-2026.txt`, `..._30stocks_2026.txt`.

## Phase 9 PRE-REGISTRATION of four variants and the decision rule (2026-11-15)

Recorded by the user's instruction on 2026-11-15, **before any 2023 or 2024 option data is downloaded** (stored and
complete at this date: 2026 for 30 stocks, 2025 for 16; nothing of 2023-2024 has been backtested). No variant, default or
rule below may be changed until the comparison has been run and reported. The two readings marked (user) were confirmed by
the user on 2026-11-15 before this commit.

### The four variants (all with the defaults in config.yaml at commit time; capital Rs 10,00,000; whole lots)
| Variant | Entries | Rules applied |
|---|---|---|
| A | the strategy's signals | the full strategy as currently specified: every entry rule and block (trend, IV rank, falling-stock and rising-stock blocks, market block, per-stock campaign limit, minimum credit 0.10, hedge, sizing, liquidity) and the portfolio limits |
| B | the no-filter baseline's fixed schedule, both sides (put and call spreads) | the baseline + the trend block in **both directions** (DOWNTREND_BLOCK on puts, UPTREND_BLOCK on calls) (user) + the essentials: hedge (`hedge_strikes_out`, max loss within the limit), events, minimum credit (`min_credit_to_max_loss` 0.10), whole-lot sizing within `max_risk_per_position_pct` 2.5% (ZERO_SIZE) |
| C | as B | B + the portfolio limits: CORRELATED_LIMIT (max 6 open spreads, total risk 10% of capital, groups of same sector or correlation >= 0.7: at most 2 positions and 5% risk), CORRELATION_UNKNOWN, PORTFOLIO_UNKNOWN; all stocks side by side |
| D | the strategy's signals | A without MARKET_WEAK (the market block off; everything else as A, portfolio limits included) |

- Events: the event check is not available historically (BYPASSED_CHECKS), so in this backtest it applies to no variant;
  this is the same for all four and for the baseline.
- Hedge, liquidity, strike selection and sizing are already part of the no-filter baseline; B adds the trend block and the
  minimum credit to it. B and C as combined rows are not yet in the report; they will be built with existing switches
  (`baseline_rules = trend_block, min_credit`, plus the portfolio gate for C) before the comparison, without looking at any
  2023-2024 result and without changing any default.

### The comparison and the decision rule
- **When:** once 2023 and 2024 are completely downloaded for the 30 priority stocks (a stock without option history in a
  year contributes nothing and is listed).
- **Data:** 2023-2024 only (signals from the first 2023 session to the last 2024 session). Positions open at the last 2024
  session are marked there after estimated closing costs (OPEN_AT_CUTOFF); nothing of 2025 or later is used.
- **Execution:** synchronized five-minute bars, after brokerage, taxes and slippage; exit policy "stop" (the main result).
- **Baseline:** the no-filter baseline (both sides, no portfolio limits), same stocks, period and execution.
- **Measures (user):** net result and max drawdown INCLUDING positions open at the cutoff, marked as above (the equity figures
  `net_incl_open_inr`, `max_drawdown_incl_open_inr`); the trade count is closed trades.
- **A variant qualifies** when all three hold: net result above the baseline's; max drawdown below the baseline's; at least
  50 closed trades.
- **Preference:** the simplest qualifying variant, simplest first: **B, then C, then D, then A**. If none qualifies, no
  variant is preferred and nothing is changed.
- **Reporting:** all four variants and the baseline are reported whatever the outcome (trades, win rate, net, max drawdown,
  profit factor, per trade, open at the cutoff, data-incomplete campaigns). If more than 30% of a variant's campaigns are
  data-incomplete, the sensitivity run is reported too; the decision uses the main run.
- Adopting the preferred variant as the strategy is a change of the specification and needs the user's approval.

## Phase 9 review fixes (Codex re-review of 782e870), 2026-11-15

Correctness fixes, not tuning: no variant definition (pre-registration 6fa73df) and no default changed; variants A-D will
be evaluated on this engine. Each fix has a test that fails on the old engine and passes now (checked by running the new
and corrected tests against the stashed old `app/`). Whole suite: **567 passing**. M3 and Phase 5 not started.

| # | Fix | Code | Test |
|---|---|---|---|
| 1 | A leg without a 5-minute bar while the position is open (other legs trading at that moment) makes the campaign data-incomplete; an exit decided and filled on bars before that moment still stands (`DATA_INCOMPLETE:MISSING_LEG_BAR`; the sensitivity run judges such a session at the daily closes) | `OptionEngine._leg_gaps`, `_manage_sync` | corrected `test_ACCEPTANCE_synchronized_stop_is_decided_on_a_bar_close...` |
| 2 | The entry bar's own close is checked (was: only later bars); a stop there exits at the next synchronized open; the same for a roll's replacement | `_manage_sync` | `test_ACCEPTANCE_a_stop_on_the_entry_bars_close_exits_at_the_next_synchronized_open` |
| 3 | After the actual slipped entry fill the cost-inclusive max loss is recomputed; above the 2.5% budget the lots are cut (whole lots), zero lots = not entered (`ZERO_SIZE_AT_FILL`); the trade and the portfolio book carry the recomputed max loss (pending signals: the estimate); rolls too | `OptionEngine._sized`, `publish`, `book_holdings` | `test_ACCEPTANCE_max_loss_is_recomputed_from_the_slipped_fill...`, `test_the_portfolio_book_carries_the_recomputed_max_loss...` |
| 4 | PORTFOLIO_UNKNOWN also when the closed and manual-trade history is not verified (`history_verified`); a backtest's simulated history is verified; live it stays unknown until the phase 6 journal | `portfolio_check`, `PortfolioGate.history_verified`, `actionable_check` | `test_ACCEPTANCE_portfolio_unknown_without_verified_closed_and_manual_history`, actionable test |
| 5 | Correlated groups are connected components of the whole universe (backtest: every tested stock; live: the watchlist stocks), the same groups as displayed; an unknown sector or link counts only where it could change a group holding positions | `portfolio_check(universe=)`, `_components`, `run_portfolio` | `test_ACCEPTANCE_groups_are_components_of_the_whole_universe...` |
| 6 | MARKET_UNKNOWN blocks put and call spreads; MARKET_WEAK stays put-only | `StockOptionSelling.assess` | market tests in `test_phase9_rules.py` (the backtest test used to assert calls passed) |
| 7 | Closing charges on each closing leg's own premium (buy back the sold leg, sell the bought leg), never on the net spread value; unknown leg price = unknown charges | `closing_charges`, `roll_choices`, `exit_calls(costs=, leg_px=)` | `test_ACCEPTANCE_closing_charges_are_on_each_legs_premium...` |
| 8 | Entry time = the actual first synchronized entry bar, for entries and roll replacements | `_Position.entry_bar`, `_close` | `test_ACCEPTANCE_entry_time_is_the_first_synchronized_bar_actually_used` |
| 9 | Drawdown (`max_drawdown_incl_open_inr`, the pre-registered measure) from a daily mark-to-market portfolio equity series (every open position marked at each session's leg closes after exit slippage and charges; a missing mark carries the last one and is counted as stale); open at the cutoff: `mark_age_minutes` apart from `holding_days` | `OptionEngine._mark`, `mtm_equity`, `option_stats` | `test_ACCEPTANCE_drawdown_is_from_the_daily_mark_to_market_equity`, `..._mark_age_apart_from_holding_days` |
| 10 | The 90th-percentile fall reads every daily close since 2019-01-22 up to the decision day (was: the last 1,300 bars), windows across a price break left out; backtest and live shortlist alike | `score.history_fall_pct`, `score_inputs`, `score_inputs_from_store` | `test_ACCEPTANCE_fall_percentile_uses_all_daily_history...` |
| 11 | The synchronized-roll test first asserts that a roll occurred | `tests/test_phase9_rolls.py` | the test itself |

Why the test of item 1 was changed: it asserted that a 10:00 bar of the sold leg alone changed nothing. That treated
a moment at which the spread had no price as harmless; the stop could have been reached then. Unknown is never a pass,
so the campaign is now left out of the primary results and counted.

### Rerun on stored data after the fixes (same runs as before; low sample; nothing tuned)
| Run | Execution | Strategy before -> after | Baseline before -> after |
|---|---|---|---|
| 16 stocks, 2025-01..2026-06 | synchronized | 19 trades -16,085 -> **17 trades -19,050** (PF 0.39) | 91 trades +2,382 -> **73 trades -11,283** (PF 0.82) |
| same | daily | 19 trades -20,933 -> 19 trades -20,933 | 88 trades -16,107 -> 88 trades -13,152 |
| 30 stocks, 2026-01..2026-06 | synchronized | 12 trades -7,779 -> **11 trades -9,145** (PF 0.45) | 50 trades -15,951 -> **50 trades -13,350** (PF 0.74) |
| same | daily | 12 trades -14,309 -> 12 trades -14,309 | 41 trades -79,291 -> 41 trades -76,118 |

- What changed most: item 1. Data-incomplete campaigns: 16 stocks strategy 0 -> 2 of 21 (9.5%), baseline 1 -> 19 of 92
  (20.7%; under 30%, no sensitivity run); 30 stocks strategy 0 -> 1 of 14, baseline 0. The baseline's synchronized
  result went from +2,382 to -11,283 because the 18 campaigns now left out had been priced across moments without a
  price for a leg. Item 3 cut lots at the fill in the baseline (RESIZED_AT_FILL 1 in each synchronized run). The daily
  baselines changed with the same trade counts (-16,107 -> -13,152; -79,291 -> -76,118); lots cut at the fill (item 3)
  are the likely cause, not checked trade by trade.
- Verdicts unchanged: no edge versus the baseline (low sample).
- CORRELATED_LIMIT: still no strategy entry refused (22 and 16 checks). Baseline with the portfolio limits: 40 blocked
  (16 stocks; 43 trades -1,508) and 30 (30 stocks; 21 trades +3,361). Groups unchanged: 12, 2, 1, 1 and 19, 3, 3, 1, 1, 1, 1, 1.
- Strategy without the market block: 16 stocks 19 trades -25,189; 30 stocks 14 trades +260.
- Reports: `data/diagnostics/stock_report_2026-11-15_review_fixes_fix_2025_2026.txt`, `..._fix_2026.txt`.

## Data import, inbox importers and the Data status page (2026-11-15)

### Licence of the external repo (aswinv90/indian-market-historical-data)
**Personal, non-commercial research use only; no redistribution.** This data is never used in anything published. It is
never committed: imported rows live in the SQLite store (gitignored) and in the gitignored overlays
`data/external/events_external.csv` and `data/external/fii_dii_external.csv` (decision of the user, 2026-11-15:
`events.csv` and `fii_dii.csv` are tracked in git and the repo has a GitHub remote, so the imported rows do not go into
them). The clone is outside the project (`../external_data/indian-market-historical-data`, a sparse clone of only the
needed data files); none of its scripts was run. Commit imported: `1f4aff7c3e53a9f2bfb6a167b0d80f4d0c8f8bf9`
(2026-11-12T22:29:14Z). Every row is tagged source `external_repo`, the commit and the import time.

### What was imported (one run, 2026-11-15)
| Dataset | Rows | Where | Notes |
|---|---|---|---|
| Corporate actions since 2019-01-01 | 11,967 (dividends 10,991, bonuses 321, splits 309, rights 250, demergers 96) | table `corporate_actions` | 8 split/bonus ratios unreadable (no factor); bonus of preference shares (e.g. TVSMOTOR NCRPS) kept without a price factor |
| RBI policy dates since 2019-01-22 | 22 (2019-02-07 .. 2026-08-05) | `data/external/events_external.csv` (RBI_POLICY) | the source's dates are policy EFFECTIVE dates and include some holds, not every MPC meeting |
| FII/DII daily cash | 190 days (2026-01-14 .. 2026-11-12) | `data/external/fii_dii_external.csv` | the repo has nothing before 2026-01-14; none of your days was replaced (your file had none) |
| Positioning (participant OI, FII index options, FII long ratio) | 705 days (2024-01-01 .. 2026-11-12) | table `positioning_daily` | no rule uses it yet |
| Daily OHLCV since 2019-01-22, F&O stocks without Breeze bars | 167 stocks, 296,106 rows | table `external_bars_1d` | F&O list: 218 stocks in ICICI's public F&O file today (current list only: survivorship); 50 already have Breeze bars; ICICILIFE not in the repo. Price basis: the source's Close, adjusted by the source for splits and bonuses (not raw). Rows with a missing price are left out. No rule reads these bars yet |
| Cross-check of daily closes, 30 priority stocks | 13,578 days over 0.5% | table `price_crosscheck` | see below |

**Cross-check (Breeze vs the repo, 30 priority stocks, about 1,923 days each):** 13,578 day-stock pairs differ by more
than 0.5%. 8,087 are explained by a later split, bonus or demerger that the Breeze prices show (the source publishes
adjusted closes); 3,635 are possibly explained by a later demerger or rights issue (the source's own adjustment, factor
not known here: ITC 1,467, RELIANCE 1,108, BHARTIARTL 658, SHRIRAMFIN 355, ADANIENT 47); **1,856 are unexplained**.
Most unexplained: BSE 673 (up to 89%; its 2022 bonus is not seen in the Breeze prices), TATASTEEL 638 (up to 90%; Breeze
already adjusted for the 2022 split, and has one-day price breaks on 2021-08-02, 2022-03-29/30 and 2022-06-14/15),
ETERNAL 114 (up to 6.6%), M&M 72 (up to 3.8%), SHRIRAMFIN 45, INDIGO 35, ADANIPORTS 32; the other stocks 4 to 22 days
each, mostly 1-3%. Breeze data is not changed by any of this.

**INSUFFICIENT_HISTORY resolved by the adjustment** (trend block needs 252 sessions; decision sessions 2019-01-22 to
today on the 30 priority stocks): **1,709 sessions in 7 stocks** - RELIANCE 251 (demerger 2023-07-20, bonus 2024-10-28),
BAJFINANCE 251 (bonus + split 2025-06-16, applied together), SHRIRAMFIN 251 (split 2025-01-10), BEL 251 (bonus
2022-09-15), KOTAKBANK 203 (split 2026-01-14), HDFCBANK 251 (bonus 2025-08-26), BSE 251 (bonus 2025-05-23). Not
resolved: the first 251 sessions of each stock's history (start of the data, not a break; HDFCBANK, BSE, ETERNAL,
HCLTECH); TATASTEEL 718 (its breaks are one-day bad bars, not an action). Not applied because the Breeze prices do not
show them (the history is already adjusted there): HDFCBANK split 2019-09-19, BSE bonus 2022-03-21, TATASTEEL split
2022-07-28, HCLTECH bonus 2019-12-05.

**Effect on backtests:** the trend, 52-week low, range and profile now read the adjusted history for these 7 stocks (and
any stock with a confirmed action), so backtest results on them change. The backtest was not rerun in this task.

### Built
- `app/corporate_actions.py`: factors from the stated ratios (split old:new face value; bonus new:held; preference-share
  bonuses excluded), same-day actions as one price change, a factor applied only when the raw opening gap shows it
  (within 25%), demerger factor = ex-date open / previous close, point in time (announcement date if known, else
  ex-date), an explained break no longer cuts the history. Raw prices, option strikes and option prices are never adjusted.
  Used by the backtest engine (`OptionEngine.daily_window`, `stock_backtest` scans) and the live shortlist (`live_daily`).
- `app/external_data.py`: the one-time import and the INSUFFICIENT_HISTORY report (`--history-report`); results in
  `data/diagnostics/external_import.json` and `insufficient_history_report.json`.
- `app/inbox.py`: inbox importers (board meetings -> RESULTS + COVERAGE; corporate actions; FII/DII; RBI decisions),
  `data/inbox/` -> `done/` or `rejected/` + `.reason.txt`; duplicates skipped (`event_key`); your rows never replaced.
- `app/manual_data.py`: overlays merged on load (your row wins on the same date / type / symbol); `merge_into_file`;
  FII/DII rows keep an optional `source` column.
- `app/data_status.py`, `/data-status`, `/api/data-status`, the strip on the main page; write routes
  `/data/import/inbox` and `/data/import/rbi-decision` (JSON only, 127.0.0.1/localhost only, as the other write routes).
- Store tables: `corporate_actions`, `positioning_daily`, `external_bars_1d`, `price_crosscheck`, `data_imports`.
- Config: `external_data`, `data_status` (limits), calendar overlay paths. Dependency: `pyarrow`.
- Tests: `tests/test_data_import.py` (9). Whole suite **576 passing**.

### Decisions recorded
- Overlays instead of the tracked CSV files (user, 2026-11-15).
- The repo has no announcement dates: every action counts from its ex-date.
- An action the Breeze prices do not show is not applied (adjusting already adjusted history would create a false break).
- Board meetings: COVERAGE rows for every watchlist stock through the latest meeting date in the file (the NSE list
  covers every company for its period), or the date you give (`--covered-through` / the field on the page).
  **Superseded 2026-11-15 (review fix 2):** only with your declared range and all-companies confirmation, after the
  sanity checks; coverage through the declared end date.
- RBI: a decision row "scheduled" marks a planned meeting; amber when a scheduled meeting is 3 days past without a decision,
  and also when no coming meeting is entered (a missed decision could not be noticed otherwise).
- Breeze session: OK only when live prices or a download with data arrived today; the token is never read.

### Not tested here
- The live NSE download files: their column names were not checked against today's NSE pages (no NSE access from here);
  the importers match the columns listed in README and reject a file whose columns differ, naming the columns found.
- The Data status page in a browser (the API and the HTML are tested).

### Still MISSING after the import
- Results dates and event coverage (no board-meetings file imported yet): every priority stock is without coverage, so
  actionable stock option signals stay blocked (EVENT_COVERAGE_UNKNOWN).
- FII/DII before 2026-01-14 (the repo has none): the FII scenarios before that date show "no data".
- RBI: no coming meeting entered as "scheduled".
- Daily bars of ICICILIFE (not in the repo).

## Data quality: BSE and TATASTEEL investigated; PRE-REGISTERED stock-year exclusion rule (2026-11-15)

Decided before any 2023-2024 option backtest (none has been run; no 2023-2024 option data is downloaded).

### 1. BSE and TATASTEEL mismatches: Breeze errors (a partly adjusted history), not the repo
Both stocks show the same pattern in the Breeze daily bars: the history from **2021-08-02** onwards is adjusted for a
corporate action that happened LATER, while the history before 2021-08-02 is not. 2021-08-02 is not an action date for
either stock, so Breeze shows a one-day fall there that never happened. The repo's series is smooth across that date and
is consistent with the actions (repo / Breeze = the product of the later actions' factors).
(Raw prices removed 2026-11-15: this file is tracked and the repo's licence forbids redistribution. Factors, method and aggregates only; the raw values are in the gitignored `data/diagnostics/status_raw_values_2026-11-15.md`.)
- **BSE** (bonus 2:1 on 2022-03-21 and 2:1 on 2025-05-23, factor 1/3 each):
  - From 2021-07-30 to 2021-08-02 the Breeze close falls 65.9% in one day; the repo's close rises 2.3%.
  - Before 2021-08-02 repo / Breeze = 0.1111 (= 1/9, both bonuses) on 620 days; from 2021-08-02 Breeze is already
    adjusted for the 2022 bonus, so the 2022-03-21 ex-date shows no gap in Breeze (+0.3%) - which is why that bonus is
    "not seen in the prices" and is not applied. The 2025 bonus is raw in Breeze (a -65.0% gap on its ex-date, factor
    1/3) and is applied.
  - Remaining unexplained BSE days after 2022: short runs where repo / Breeze is 0.329-0.337 instead of 0.3333 (the two
    closes differ by 1-1.5% on those days); which side is right is not known.
- **TATASTEEL** (split 10:1 on 2022-07-28):
  - From 2021-07-30 to 2021-08-02 the Breeze close falls 90.1%; the repo's close falls 1.7%. Before 2021-08-02
    repo / Breeze = 0.1000 on 620 days; from 2021-08-02 to the split Breeze is already adjusted, so the 2022-07-28
    ex-date shows no gap (+4.3%).
  - Two single unadjusted bars inside the adjusted stretch (2022-03-29 and 2022-06-14): each about 10x the repo's close
    (repo / Breeze 0.100), +896% / +901% and back -90% the next day.
  - 2019-01-21 Breeze bar about 1/10 of its neighbours (-92.2%, then +1,138.9% on 2019-01-22).
- The repo had no one-day close move over 20% for either stock since 2019. Conclusion: Breeze errors (a vendor
  adjustment boundary at 2021-08-02 and isolated bad bars); not unadjusted actions in the usual sense, and not repo errors.
  Breeze data is not changed; the stock-years are excluded by the rule below.

### 2. PRE-REGISTERED RULE (2026-11-15; `data_quality` in config.yaml; `app/data_quality.py`)
A stock-year is **excluded from every Phase 9 backtest** - the strategy, every variant (A-D) and the baseline alike - when
- its **unexplained** Breeze-vs-repo close mismatches (over 0.5%, no explanation; "possibly explained" by a later demerger
  or rights issue does not count) exceed **5% of the year's Breeze sessions**, or
- its Breeze daily closes contain **any one-day move over 20%** (up or down) on a session that is not the ex-date of a
  split, bonus or demerger,

**unless resolved**: a stock-year is kept only when it is listed in `data_quality.resolved_stock_years` with the
resolution recorded here. Exclusion means no entry (no signal, no scheduled baseline attempt) in that stock-year or in the
45 calendar days before it; scheduled baseline attempts there are counted as EXCLUDED_STOCK_YEAR. A year with no external
closes to compare is judged on the jump rule alone. Whether the repo shows the same move is recorded as evidence only.

### Stock-years this excludes now (30 priority stocks, years from 2019; Phase 9 tests start 2019-01-22)
27 stock-years. "(repo: same move)" = the repo's close moves by the same amount that day (evidence that the move was
real); the rule excludes these too, unless you resolve them.
| Stock | Years | Reason |
|---|---|---|
| BSE | 2019, 2020, 2021, 2022, 2023 | unexplained mismatches 93%, 100%, 61%, 7.3%, 10.6%; 2021-08-02 -65.9% (Breeze error) |
| TATASTEEL | 2019, 2020, 2021, 2022 | mismatches 93%, 100%, 58%; jumps 2019-01-21/22, 2021-08-02, 2022-03-29/30, 2022-06-14/15 (Breeze errors) |
| ETERNAL | 2021, 2022, 2023, 2024 | unexplained mismatches 18.0%, 14.5%, 16.7%, 5.7%; 2022-01-24 -20.2% (repo: same move) |
| ADANIENT | 2019, 2020, 2023, 2024 | jumps only: 2019-05-20 +27.4%, 2020-08-25 +23.7%, 2023-02-01 -26.7%, 2023-02-02 -28.2%, 2023-02-08 +22.1%, 2024-11-21 -22.6% (all: repo same move) |
| ADANIPORTS | 2023, 2024 | 2023: unexplained mismatches 6.9%; 2024: 2024-06-04 -21.1% (repo: same move) |
| HCLTECH, HDFCBANK | 2019 | a bad Breeze bar on 2019-01-21 (about 2x its neighbours; repo differs), the move back lands on 2019-01-22 |
| AXISBANK, BAJFINANCE, SHRIRAMFIN | 2020 | 2020-03-23 -27.9%, -23.2%, -22.2% (repo: same move) |
| M&M | 2020 | unexplained mismatches 15.6% |
| SHRIRAMFIN | 2022 | unexplained mismatches 9.3% |

- In **2023-2024** (the pre-registered comparison's period) this excludes **7 stock-years**: ADANIENT 2023 and 2024,
  ADANIPORTS 2023 and 2024, BSE 2023, ETERNAL 2023 and 2024.
- In **2025-2026** it excludes **none**, so the stored-data backtest is not affected by it.
- Candidates for a resolution by you (jump-only years where the repo shows the same move, i.e. most likely real market
  moves): ADANIENT 2019, 2020, 2023, 2024; ADANIPORTS 2024; AXISBANK 2020; BAJFINANCE 2020; SHRIRAMFIN 2020. Nothing is
  resolved now.
- Earlier years (2014-2017) have many more bad Breeze bars (pairs of +100% / -50% days on the same dates across stocks,
  e.g. 2015-10-01/06, 2016-01-19/20); they are before the Phase 9 floor and not used.

### 3. Rerun on stored data after the corporate-action adjustment and the exclusion rule (open issue 79), 2026-11-15
Same two runs as before (Rs 10,00,000, whole lots, every block, nothing tuned; low sample). The exclusion rule removes no
stock-year in 2025-2026, so every change below comes from the adjustment of the daily history (more decision days have
enough history, and the trend, 52-week, range and profile read adjusted prices).

| Run | Execution | Strategy before -> after | Baseline before -> after |
|---|---|---|---|
| 16 stocks, 2025-01..2026-06 | synchronized | 17 trades -19,050 -> **18 trades -17,490** (PF 0.44) | 73 trades -11,283 -> **79 trades -19,916** (PF 0.74) |
| same | daily | 19 trades -20,933 -> 21 trades -22,026 | 88 trades -13,152 -> 94 trades -25,050 |
| 30 stocks, 2026-01..2026-06 | synchronized | 11 trades -9,145 -> **12 trades -3,688** (PF 0.73) | 50 trades -13,350 -> **56 trades -21,983** (PF 0.65) |
| same | daily | 12 trades -14,309 -> 15 trades -8,947 | 41 trades -76,118 -> 47 trades -88,016 |

- Verdicts: no edge versus the baseline in the 16-stock run; in the 30-stock run the strategy loses less per trade than the
  baseline (-307 against -393) but loses money: no evidence of an edge (low sample).
- **The reports are now much more INCOMPLETE: 874 contracts (16-stock run) and 133 (30-stock run) are wanted but not
  stored** (before: 46 and 20). The new decision days want contracts the download never fetched (the "complete" marks were
  made before the adjustment). The download plan version is raised to 7, so the next `--download` checks every stock-year
  again and fetches what is now wanted. Until then these results understate the trades the rules would take.
- CORRELATED_LIMIT now refuses strategy entries for the first time: 3 of 28 checks (16 stocks) and 5 of 24 (30 stocks), as
  more stocks are tradable at the same time. Baseline with the portfolio limits: 46 and 37 blocked.
- Data-incomplete campaigns: strategy 3 of 23 (13.0%) and 3 of 17 (17.6%); baseline 19 of 98 (19.4%) and 0 of 56.
- Strategy without the market block: 20 trades -23,629 (16 stocks); 14 trades +459 (30 stocks).
- The exclusion list printed by the run covers every ranked watchlist stock (the reversal study uses them), 33 stock-years
  from 2019 including non-priority stocks (e.g. BAJAJFINSV 2020).
- Reports: `data/diagnostics/stock_report_2026-11-15_dataquality_dq_2025_2026.txt`, `..._dq_2026.txt`.

## Data status page: IST times and today's actual request budget (2026-11-15)
- Every time on the Data status page is shown in IST as "15 Nov 2026, 10:30 AM" (a date alone as "13 Nov 2026");
  the API keeps the ISO value in `last_updated_iso` (`app/data_status.py: fmt_ist`).
- The option-history row shows today's actual request budget. A budget raised for one day is now recorded in the store
  (`request_budget`, kind `option_history:budget`; `Store.set_day_budget` / `day_budget`) and used by every download that
  day (`option_history.todays_budget`); `python -m app.options.stock_backtest --download --day-budget N` sets it.
  config.yaml keeps its default (3,500). Today's raise to 4,500 (the user's instruction earlier today) was made with a
  one-off run setting and not stored at the time; it is now recorded for 2026-11-15, so the row reads
  "4,500 of 4,500 (set for today only; default 3,500)".
- Test: `test_times_are_shown_in_ist_and_the_days_actual_budget_is_used`. Whole suite 580 passing.

## AMENDED data-quality rule (2026-11-15, before any 2023-2024 option data is downloaded)

This replaces the exclusion rule pre-registered earlier today (section "Data quality: BSE and TATASTEEL investigated").
Decided by the user in three steps today, on price data only (no option result was looked at): rule
`dq-2026-11-15-amended`, `app/data_quality.py`, run with `python -m app.data_quality` (verdicts and corrections stored in
the tables `data_quality_years` and `price_corrections`). The same verdicts apply to the strategy, every variant (A-D)
and the baseline; an excluded stock-year gets no entry in it or in the 45 calendar days before it.

1. **Exact corrections.** A Breeze bar whose close, against the repo's, is off by exactly the factor of a later split or
   bonus that the rest of the Breeze history already reflects (the action shows no gap in Breeze) is a Breeze error and
   is corrected in the adjusted series by that factor (tolerance 2%); the stock-year is kept. A bar with no repo bar
   (a repo filler day) between two bars corrected by the same factor gets the same factor. The raw store is not changed.
2. **One-day jumps.** A (corrected) Breeze close move over 20% on a session with no split, bonus or demerger is a real
   market move when the repo confirms it (same date, same direction, within 5 percentage points): the stock-year is
   NOT excluded. An unconfirmed jump excludes it.
3. **Unexplained mismatches** (over 0.5% after the later applied factors; before a later demerger or rights issue a
   mismatch counts as possibly explained, not unexplained): the stock-year is tested for a smooth adjustment. The
   ratio Breeze / repo may step on an ex-date of any corporate action or dividend. A step **over 2%** on a day with no
   corporate action or dividend is **erratic**, unless the stock moved **more than 8%** that day in either source
   (closing-price definition noise on a volatile day). Any erratic day excludes the stock-year; otherwise it is kept as
   the repo's own adjustment. (The literal 0.2% day-to-day test would have excluded 115 of the 131 stock-years with
   unexplained mismatches: in 2021-2024 the two sources' closes differ by 0.1-0.15% on a typical day, a closing-price
   definition difference; e.g. ICICIBANK 2022-06-08 differs by 0.34%.)
4. **Repo filler bars** (open = high = low = close = the previous close; 213 since 2019 on the 30 priority stocks, e.g.
   2025-03-18 for every stock) are repo errors and are left out of every comparison.

### BSE factor check (why 1/3 now and 1/9 earlier)
(Raw prices removed 2026-11-15: this file is tracked and the repo's licence forbids redistribution. Factors, method and aggregates only; the raw values are in the gitignored `data/diagnostics/status_raw_values_2026-11-15.md`.)
BSE bonuses: 2:1 with ex-date 2022-03-21 and 2:1 with ex-date 2025-05-23, each a price factor of 1/3.
| Days | Repo / Breeze | What it means |
|---|---|---|
| to 2021-07-30 (620 days) | 0.1111 = 1/9 | Breeze raw for BOTH bonuses; the repo adjusted for both |
| from 2021-08-02 (no action that day) | 0.3333 = 1/3 | Breeze already adjusted for the 2022 bonus, raw for the 2025 one |
Method: the ratio repo close / Breeze close on each day, compared with the products of the later actions' factors
(1/3 x 1/3 = 1/9 before the boundary, 1/3 after it), tolerance 2%. The 1/9 in the earlier cross-check was the product
of both bonuses. In the Breeze history the 2022 bonus shows no gap on its ex-date (+0.3% from the session before),
because Breeze already applied it from 2021-08-02; the 2025 bonus shows its gap (-65.0%, factor 1/3) and is applied as
usual. So the Breeze bars before 2021-08-02 are not adjusted for the 2022 bonus only: correction x1/3, after which
repo / corrected = 0.3333 (= the 2025 bonus) on every corrected bar, consistent with every later bar. Corrected: 244
bars in 2019, 250 in 2020, 143 in 2021 (to 2021-07-30); BSE 2019-2023 are kept.

### Other corrections (exact factor)
- TATASTEEL: 250 bars in 2020 and 143 in 2021 (before 2021-08-02) and 2 bars in 2022 (2022-03-29 and 2022-06-14)
  x 1/10 for the split of 2022-07-28; after the correction each is within 0.01% of the repo's close.
- HCLTECH 2019-01-21: the Breeze close is about twice both neighbouring closes; x 0.5 for the bonus of 2019-12-05
  brings it within 0.001% of the repo's close: **confirmed corrected; HCLTECH 2019 kept**.
- HDFCBANK 2019-01-21 x 0.5 (split 2019-09-19); EICHERMOT 2019-01-21 x 0.1 (split 2020-08-24); POWERGRID 2019-01-21 and
  2021-04-23 x 0.75 (bonus 2021-07-29). (Older bars, 2015-2017, on the same dates are corrected too; they are before the
  Phase 9 floor.)
- Not correctable: TATASTEEL 2019-01-21, a Breeze close 19-22% below both corrected neighbours and 21.9% below the
  repo's close: no factor explains it, so the move to 2019-01-22 (+23.9%, repo -3.2%) is an unconfirmed jump.

### Stock-years under the amended rule (from 2019)
**30 priority stocks - excluded (8):** ETERNAL 2021, 2022, 2023, INDIGO 2021, M&M 2020, SHRIRAMFIN 2022, TITAN 2022
(erratic ratio days, as reported to the user before applying), and TATASTEEL 2019 (the uncorrectable 2019-01-21 bar).
Kept with an explanation: BSE 2019-2023 and TATASTEEL 2020-2022 (exact corrections), HCLTECH 2019 and HDFCBANK 2019
(one bar corrected), ADANIENT 2019, 2020, 2023, 2024, ADANIPORTS 2024, AXISBANK, BAJFINANCE, SHRIRAMFIN 2020 (jumps
confirmed by the repo), ADANIPORTS 2023 (its one 2% step is on a -8.9% day), RELIANCE, ITC, BHARTIARTL and the rest
(no unexplained mismatch, or a ratio that steps only on ex-dates).
**Other watchlist stocks - excluded (10):** DRREDDY 2021, JIOFIN 2023, MAXHEALTH 2020, 2022, 2023, SBILIFE 2021, 2022,
TRENT 2019, 2020, 2023 (erratic ratio days; they matter for the reversal study, which uses every ranked stock).
**In 2023-2024:** ETERNAL 2023 (priority) and JIOFIN 2023, MAXHEALTH 2023, TRENT 2023. **In 2025-2026: none.**
No stock-year is resolved by hand (`data_quality.resolved_stock_years` is empty).

### The fast version-7 recheck
`python -m app.options.stock_backtest --recheck --changed-only`: the plan-7 change only alters the daily history, so a
complete mark can only become wrong where a decision's adjusted or corrected window differs from the raw window it was
made with. That is found offline in one pass over the stored daily bars (`changed_stock_years`): **0.2 s**, 14 of the 46
complete marks (BAJFINANCE 2025-2026, BEL 2026, BSE 2025-2026, HDFCBANK 2025-2026, ITC 2026, KOTAKBANK 2026, RELIANCE
2025-2026, SHRIRAMFIN 2025-2026, TATASTEEL 2026); every other mark is kept. Rechecking only those 14 took **98 s** (100 s
wall time in all; the earlier full recheck of every stock-year was stopped after about 55 minutes). Result: RELIANCE 2026,
SHRIRAMFIN 2026, BEL 2026, TATASTEEL 2026, ITC 2026 complete again; HDFCBANK, BSE, BAJFINANCE, KOTAKBANK 2026 and
HDFCBANK, RELIANCE, BSE, BAJFINANCE, SHRIRAMFIN 2025 incomplete (they want contracts that are not stored; the next
`--download` fetches them). Complete now: 2026 for 26 of 30 priority stocks, 2025 for 11.

### Rerun on stored data (same stocks and years as the previous runs; INCOMPLETE where contracts are missing)
**Unchanged, figure for figure,** from the rerun before (commit bc9490b): 16 stocks 2025-2026 synchronized strategy 18
trades -17,490, baseline 79 trades -19,916; 30 stocks 2026 strategy 12 trades -3,688, baseline 56 trades -21,983; daily,
rule-effect, CORRELATED_LIMIT (3 and 5 strategy refusals) and data-incomplete figures identical; contracts not stored
874 and 133. Why: the amended rule excludes nothing in 2025-2026, and the corrections only change bars of 2021 and
earlier, which do not change any 2025-2026 decision (the windows differ, the outcomes of the rules do not). Reports:
`data/diagnostics/stock_report_2026-11-15_amended_am_16.txt`, `..._am_30.txt`.

## Review fixes (Codex review of 782e870..2e3e2c4, six findings), 2026-11-15

Correctness and licence fixes, not tuning: no strategy, variant or default changed. Each fix has a test in
`tests/test_review6_fixes.py` (11 tests) that fails on the code before the fix and passes now (checked by running it
against the stashed old file). Whole suite: **595 passing**. Batch B, M3 and Phase 5 not started.

| # | Fix | Code | Test |
|---|---|---|---|
| 1 (HIGH) | CORRELATION_UNKNOWN when an unknown link through ANY stock of the universe, held or not, could join two groups that hold positions: the known groups are joined along every possible link (an unmeasurable correlation, or any link of a stock whose sector is unknown) and every unknown link inside a possible group with two or more held groups counts. Was: only an unknown link directly between two held groups | `portfolio._unknown_joins` | `test_ACCEPTANCE_an_unknown_path_through_an_unheld_stock_that_could_join_held_groups_is_unknown` |
| 2 (HIGH) | A board-meetings file gives coverage only with your declaration (date range + confirmation that it is the unfiltered all-companies export: page fields or `--board-from / --board-through / --all-companies`) and a sanity check: at least `board_meetings_min_symbols` (100) distinct companies, every meeting date inside the range, meetings within `board_meetings_edge_days` (7) of both ends, the range starting no later than the download day. Otherwise the file is rejected. Coverage runs through the declared END date (was: the latest meeting date, for every watchlist stock, from any file). `--covered-through` and the "cover through" field are gone | `inbox.check_board_meetings`, `BoardMeetingsDeclaration`, `declaration`; route `/data/import/inbox`; Data status page; config `data_status` | `test_ACCEPTANCE_a_partial_board_meetings_export_gives_no_coverage`, `test_board_meetings_declaration_and_sanity_checks`, `test_inbox_route_and_cli_take_the_declaration`; the inbox test in `test_data_import.py` no longer endorses blanket coverage from a two-company file |
| 3 | Five-minute (synchronized) runs mark open positions at the LAST synchronized 5-minute bar of each session, never at the legs' independent daily closes; a session without one carries the previous mark with its own price time (counted stale; `max_mark_age_minutes` in the report). Daily runs keep the daily closes. Marks are stored as [session, value, price time] | `OptionEngine._mark(sync=)`, `mtm_equity`, `option_stats` | `test_ACCEPTANCE_five_minute_marks_use_the_last_synchronized_bar_never_the_daily_leg_closes`, `test_a_session_without_a_synchronized_bar_carries_the_previous_mark_and_its_age` |
| 4 | A (stock, year) block is complete only when the block itself made no request for a contract that is not stored (`misses`, counted on every request). Was: no growth of the shared not-stored set, so a block wanting a contract an earlier block had already wanted looked complete | `OptionHistory/OptionIntraday._miss`, `download_blocks.missing` | `test_ACCEPTANCE_two_blocks_wanting_the_same_unavailable_contract_are_both_incomplete` |
| 5 | External-repo prices removed from this file (sections "BSE and TATASTEEL investigated" and "AMENDED data-quality rule": 20 lines); replaced by factors, method and aggregates. Raw values only in the gitignored `data/diagnostics/status_raw_values_2026-11-15.md`. A scanner checks every tracked doc against the repo's rows held here (overlays in `data/external/`, the store's external tables, the clone) and for a price written after the word "repo" | `external_data.doc_leaks`, `external_values`, `tracked_docs`; `python -m app.external_data --check-docs` | `test_ACCEPTANCE_tracked_docs_carry_no_row_of_the_external_repo`, `test_doc_scanner_finds_repo_values_next_to_their_dates_and_after_the_word_repo` |
| 6 | Rows dated after today never judge freshness, on every source of the Data status page (FII/DII rows, COVERAGE rows by retrieved date, RBI decisions, daily bars, corporate-action and external import times, the download log, the download progress time); a source holding any is DATA_ERROR (red) with the rows listed and its status without them. Dates that may lie ahead by nature are not flagged (RESULTS meetings, ex-dates, covered_through, scheduled RBI meetings) | `data_status._flag_future`, every row | `test_ACCEPTANCE_future_dated_rows_are_ignored_for_freshness_and_flagged_on_every_source` |

### Recheck of the complete marks under fix 4 (offline, no requests; 285 s)
`python -m app.options.stock_backtest --recheck --verify-complete --no-report` judged every one of the 38 complete
marks again: **36 kept, 2 no longer complete: KOTAKBANK 2025 and ETERNAL 2020** (both blocks run on into later years
and want contracts that are not stored, which an earlier block had already wanted). Complete now: 2026 for 26 of 30
priority stocks, 2025 for 10 (was 11), 2020 for none. Recorded in `data/option_download_status.json`
(`verify_complete_2026_11_15`); the next `--download` fetches what they want. No backtest was rerun in this task. The
16-stock 2025-2026 runs above include KOTAKBANK and were already labelled INCOMPLETE (874 contracts wanted but not
stored); ETERNAL 2020 is outside every reported run.

### Git history must be cleaned before the repo is ever made public
The GitHub repo is private. Its history still holds external-repo prices: `docs/STATUS.md` in commits 90ce771 and
2e3e2c4 (and any later commit until this one). History was NOT rewritten (your instruction). **Before the repo is
ever made public, rewrite the history to remove those values (for example with git filter-repo on `docs/STATUS.md`)
and run `python -m app.external_data --check-docs` on every commit that stays.**

### Not tested here
- The real NSE board-meetings export against the new sanity checks (no NSE access from here): a real all-companies
  export for a short range could list fewer than 100 companies; lower `data_status.board_meetings_min_symbols` only
  after checking such a file.
- The Data status page form in a browser (the HTML fields and the route are tested).
- The future-dated download-progress time (it is read from `data/option_download_status.json`; the code path is
  small and untested).

## ICICI Direct physical-settlement rules entered (Phase 9 M3 config), 2026-11-15

Config and validation only (`stock_option_selling.settlement` in config.yaml; `Settlement`, `BrokerSettlement` and
their parts in `app/config.py`); the alerts and estimates that read it are not built (M3, needs phase 6 to send).
Tests: `tests/test_settlement_config.py` (12). Whole suite: **607 passing**.

**Sources (as given by you; not checked from here: no access to ICICI's pages):**
| Rule | Value | Source | Confirmed |
|---|---|---|---|
| Delivery margin | Near-month ITM and CTM stock options (CTM = strikes within 5% of spot), from E-4: 10% (E-4), 25% (E-3), 45% (E-2), 70% (E-1), 100% (E) of VaR + ELM + adhoc margin; collected at 15:15 on E-4 to E-2, hourly on E-1 and E | ICICI Direct articles on physical delivery of stock options | yes |
| Auto close on expiry day (ICICI's own square-off) | Statement 1: options 13:30, futures 14:30 (best effort). Statement 2: long options 12:00, short options 14:30. Both stored; alert deadlines use the earliest time for the legs held: 12:00 for a spread, 13:30 for a short-only position | Statement 1: an ICICI Direct article; statement 2: another ICICI note you found | **no** (the statements differ) |
| Fresh-position block in the last days | not known | - | **no** |
| Settlement obligation | Short put: strike x lot size per lot in cash; short call: the shares in demat | ICICI Direct articles on physical delivery of stock options | yes |
| Short call shares deadline | in demat before 11:00 on expiry day | as above | **no** |

**Alert levels (your decision):** URGENT from E-5 for any short stock option ITM or within 5% of spot, the same day as
the forced exit (`forced_exit_trading_days_before_expiry: 5`); config refuses an URGENT start later than the forced
exit. NOTICE (near the money) and WARNING (in the money) cover E-7 and E-6.

**Waiting for you:** confirm the auto-close time with ICICI (which statement holds, for long and short options), whether
ICICI blocks fresh positions in the last days, and the 11:00 deadline for shares; then set the `confirmed` flags.

## Phase 9 time-ordering fixes and the rerun on stored data (2026-11-15)

Two HIGH findings, fixed before M3. Correctness fixes, not tuning: no rule, default or variant (A-D, pre-registered)
changed. Tests: `tests/test_phase9_time_order.py` (6) with a two-stock fixture (`tests/two_stock_world.py`).

| # | Fix | Code | Test |
|---|---|---|---|
| 1 (HIGH) | The portfolio backtest ran each stock's whole session before the next stock's same session, so a decision could see another stock's LATER fills, exits and signals of that session, or miss its earlier ones. Every stock's steps (entries, exits, rolls, pending entries, new signals) now go through one queue in time order. At one instant: decisions on a bar's close and position management at the session's close, then new signals, then fills at a bar's open. Only truly simultaneous events of different stocks follow the order of the stocks (most liquid first) | `OptionEngine.run_portfolio`, `iter_stream`, `_manage_sync`, `_sync_exit`, `_sync_entry` (generators that announce the moment of each step before taking it) | `test_ACCEPTANCE_reversing_the_stock_order_does_not_change_a_synchronized_portfolio_run`, `..._a_daily_portfolio_run`, `..._a_roll_decided_inside_the_session_sees_the_portfolio_as_it_stands_then`, `..._truly_simultaneous_signals_follow_the_documented_priority` |
| 2 (HIGH) | The portfolio gate of a roll decided inside a session read daily returns through that same day, whose close had not happened. An intraday decision now uses correlations of completed sessions before that day; a decision at the close (a new signal, a roll by a rule at the close) may use the day | `Correlations.at(include_day=)`, `PortfolioGate.__call__(intraday=)`, `OptionEngine._roll_gate`, `_sync_roll_decision` | `test_ACCEPTANCE_changing_only_that_days_closing_prices_does_not_change_an_intraday_roll_decision` (with a control: the same change one session earlier does refuse the roll), `test_correlation_inside_a_session_equals_the_previous_sessions_and_the_gate_asks_for_it` |

**Fail before, pass after:** run against the engine of commit ce61be5, the three reversal tests fail because the trades
differ when the stock order is reversed, and the closing-price test fails because the roll is refused
(`CORRELATED_LIMIT`) once that day's closes are changed. Those runs used a small import shim, because the tests use
names the old engine lacks (`trace`, the phase constants, `include_day`); the unit test of the new argument fails there
only for that reason. The priority test passes on both engines: it documents the one order-dependent case. The reversal
tests stop before 22 July 2025 of the fixture, the next close at which both stocks signal together.

**Test fixtures:** in the roll tests the roll rules themselves are replaced (every stopped spread is offered a
replacement with enough credit), so that the decision is left to the portfolio gate; on real data no roll by the rules
has passed so far.

### Rerun on stored data (offline; the same two runs as before; low sample; nothing tuned)
Method: the strategy's portfolio run on the old engine (ce61be5) and on the new one, same data, compared trade by
trade; then the two full reports on the new engine. The old engine reproduced the stored figures exactly, so the review
fix of the correlation groups (review 6, fix 1), which had not been rerun, changed nothing in these runs.

| Run | Execution | Strategy before -> after | Baseline before -> after |
|---|---|---|---|
| 16 stocks, 2025-01..2026-06 | synchronized | 18 trades -17,490 -> 18 trades -17,490 (identical trades) | 79 trades -19,916 -> unchanged |
| same | daily | 21 trades -22,026 -> identical | 94 trades -25,050 -> unchanged |
| 30 stocks, 2026-01..2026-06 | synchronized | 12 trades -3,688 (PF 0.73) -> **12 trades -7,584** (PF 0.54) | 56 trades -21,983 -> unchanged |
| same | daily | 15 trades -8,947 -> identical | 47 trades -88,016 -> unchanged |

- **One trade changed, in one run.** 30 stocks, synchronized: BHARTIARTL's signal of 24 April 2026 was refused by
  CORRELATED_LIMIT on the old engine (it judged that close before another stock's exit of the same session had been
  run) and the stock entered four days later, on the 28 April signal, reaching its target (+1,015). In time order the
  slot was free on 24 April: the spread was entered on 27 April and stopped on 29 April (-2,882). Net effect -3,897.
  CORRELATED_LIMIT refusals 5 -> 4 of 24 -> 23 checks. Net including the open positions -2,813 -> -6,710; drawdown of the
  marked equity 8,209 -> 10,139.
- With that trade the strategy under the portfolio limits now equals the strategy without them in the 30-stock run
  (12 trades, -7,584 both): the limits changed which day was traded before, not whether.
- The baselines do not run under the portfolio limits, so they cannot change; "baseline + portfolio limits" is unchanged
  too (43 trades -12,447 and 20 trades -8,761).
- Rule-effect rows that run under the limits moved with the same cause: 30 stocks, strategy without the minimum credit
  -2,874 -> -6,770; without the trend block 15 trades -11,486 -> 16 trades -17,919; without the market block 14 trades
  +459 -> 15 trades -2,077; roll every month (research only) +10,313 -> +9,911. 16 stocks: only "without the trend block"
  22 trades -32,097 -> 23 trades -34,634.
- No roll by the rules was made before or after (every stopped strategy spread refused by a roll rule), so fix 2 changed
  no trade in these runs.
- Verdicts unchanged: no edge versus the baseline (low sample). Contracts wanted but not stored: 874 -> 891 and
  133 -> 142 (the changed decisions want other contracts); the reports stay INCOMPLETE.
- Reports: `data/diagnostics/stock_report_2026-11-15_timeorder_16.txt`, `..._timeorder_30.txt` (stocks and first
  session given explicitly, because the "complete" marks changed after the per-block recheck; the reversal study, which
  uses single streams, was not rerun).

### Not tested here
- A roll by the rules actually passing on real data (none has), so the intraday correlation rule has only been
  exercised on fixtures.
- Baseline streams of one stock still run one after another when they are run without the portfolio limits: they share
  no state, so their order cannot matter.

## Phase 9 M3: exits, rolls, campaigns and settlement readiness (2026-11-15)

M3 is the "manage an open position" part of Phase 9 (build steps 6 and 16-22 of docs/implementation.md and the
settlement-readiness addition, tasks P9.29-P9.32). Phase 5 not started; variants A-D and every default unchanged.
Whole suite: **638 passing**.

### What M3 contains
| Item | Code | Tests | State |
|---|---|---|---|
| Exit calls: target, stop, strike breached, trend flip, time exit, forced exit before expiry week | `stock_selling.exit_calls` | `test_phase9_core.py` | built earlier |
| Campaign tracking, "recovery not possible from this position" | `campaign.py`, store `campaigns` | `test_phase9_rolls.py` | built earlier |
| Roll rules (a)-(d), one roll, never larger; stuck position; no recovery sizing | `stock_selling.roll_choices`, `stuck_position` | `test_phase9_rolls.py` | built earlier |
| Open-position alerts with exit and roll numbers | `stock_selling.position_alerts`, `stock_profile.alert_kinds` | `test_phase9_rules.py` | built earlier |
| Exit-policy comparison (stop, roll by rules, roll every month) on the same entries, both executions | `stock_backtest.policy_runs`, `policy_comparison` | `test_phase9_rolls.py` | built earlier; now on the time-ordered engine |
| **P9.29** escalating settlement alerts | `settlement.leg_level`, `escalate`, `settlement_alert` | `test_phase9_settlement.py` (14 parameter cases + 3 tests) | **new** |
| **P9.30** delivery obligation and expiry-week margin estimate | `settlement.delivery_estimate`, `delivery_margin` | hand calculations for a short put and a short call, with and without the bought leg in the money | **new** |
| **P9.31** planned-exit deadline and the red alert after it | `settlement.cutoffs`, `exit_deadline`, `overdue` | deadline and repeat tests | **new** |
| **P9.32** "ICICI rule not confirmed" on every alert that uses an unconfirmed rule, and on the page | `settlement.not_confirmed`, `rules_view`; route `/api/stock-option-selling/settlement`; "Settlement readiness" card | flag tests, route and page test | **new** (config was built before) |

### Settlement readiness as built (`app/options/settlement.py`; pure functions, nothing is stored or sent)
- **Applies to** every open position with a sold stock option leg (from a card, manual, UNHEDGED), as `HeldPosition`.
- **Levels** per sold leg, by trading days to expiry: nothing before E-7 and never for a leg more than 5% of spot out of
  the money; NOTICE (near the money) and WARNING (in the money) on E-7 and E-6; URGENT from E-5, or once an ICICI cut-off
  has passed; URGENT "price unknown" when the stock's price or the expiry is not known. The position's level is the
  highest of its legs and never drops while it is open (the caller passes the previous level).
- **Each alert** names the leg, spot, strike, moneyness in % of spot and in rupees, trading days left, the estimate, the
  deadline and the action ("Exit the spread by 22-Dec-2026 15:30").
- **Delivery estimate**, whole position and one lot: what the sold leg obliges if assigned (a put: shares to buy and the
  cash, strike x shares; a call: shares to deliver), and what settling at the last price would mean, netted with a
  bought leg of the same right only when that leg would settle in the money too. Rupee figures also as % of capital.
- **Expiry-week margin**: ICICI's schedule (10, 25, 45, 70, 100% of VaR + ELM + adhoc margin on E-4 to E) for a leg in the
  money or within 5% of spot. The rupee amount needs that margin base for the position, which the system does not have
  (it is ICICI's figure): without it the percentage is shown and the amount is "margin unknown" in red. An empty rule,
  an unknown price or an unknown expiry is "margin unknown" too. Never zero.
- **Deadline**: the earliest of the forced exit (the close, 15:30, of E-5), the start of ICICI's delivery margin (E-4,
  15:15), ICICI's auto close on expiry day (12:00 for a spread, 13:30 short only) and, for a sold call, the 11:00 shares
  deadline. With the shipped config it is always the forced exit. After it, every evaluation of a position that is not
  marked closed returns a red `EXIT_OVERDUE` alert, whatever its moneyness.
- **No code path closes or changes a position**: positions are frozen objects, the module imports no store, feed or SDK
  (tested), and tests/test_safety.py passes.

### Tested
P9.29-P9.32 acceptance checks of docs/implementation.md, each with a test: every level on its days and none for a far
out-of-the-money leg; the level only rises; unknown price and unknown expiry are URGENT; the two hand calculations;
"margin unknown" for an empty rule; the red alert on every call after the deadline until closed; the unconfirmed label
appearing and disappearing with a rule's flag; positions unchanged. The app was started in simulated mode (port 8011):
`/api/stock-option-selling/settlement` returns the rules and the page shows the "Settlement readiness" card.

### Not tested / not built here
- **Sending and repeating the alerts** (Telegram, repeat interval per level) and **marking a position closed**: phase 6.
  No position is recorded before phase 6, so the route returns no alert and nothing is shown at the top of the dashboard yet.
- **The holiday calendar in the day count**: see the follow-ups below.
- **ICICI's rules themselves** are as you entered them; none was checked from here (issue 85).
- (Superseded by the follow-ups below: the backtest now applies the forced exit, and the day count uses the calendar.)

## Phase 9 M3 follow-ups: holiday calendar in the settlement day count; forced exit in the backtest (2026-11-15)

Two follow-ups asked for after M3. No default changed; variants A-D unchanged; "nothing before E-7 for an unknown
price" kept as built. Whole suite: **647 passing**.

| # | Change | Code | Tests |
|---|---|---|---|
| 1 | Settlement alerts count trading days with the holiday calendar (holidays.csv), not weekdays: the day count, the alert levels, the exit deadline, ICICI's cut-off days and the expiry-week margin day all follow it. When the calendar has no holiday row for a year the expiry month touches (the 31 days up to expiry), or there is no calendar, the count is weekdays LESS ONE: alerts, URGENT and the deadline come one session earlier, never later, and the alert carries `calendar: {covered: false, note, colour: red}`. The page card and the route show the same note for expiries in the next 70 days | `settlement.DayCount`, `day_count`, `uncovered_years`, `days_left`, `cutoffs`, `settlement_alert`, `rules_view(calendar, today)`; route and page card | `test_ACCEPTANCE_trading_days_use_the_holiday_calendar_not_weekdays` (a 25 December holiday: E-5 is the 21st, not the 22nd), `test_ACCEPTANCE_a_calendar_that_does_not_cover_the_expiry_month_counts_one_day_earlier_and_says_so` (no calendar, empty file, last year's list), `test_coverage_is_judged_on_the_expiry_month_and_shown_on_the_page_card` |
| 2 | The backtest now applies the forced exit: at a close, a credit spread whose next session is inside the last `forced_exit_trading_days_before_expiry` (5) trading days (by the run's holiday calendar) is closed at that next session (`FORCED_EXPIRY_EXIT`), under both executions. The time exit is still checked first. `strategy.time_exit = False` switches the time exit off for tests and comparisons; the forced exit cannot be switched off | `OptionEngine._late_exit`, `FORCED_EXPIRY_EXIT`; `StockOptionSelling.time_exit` | `tests/test_phase9_forced_exit.py`: with the time exit off the July spread of a fixture leaves at 09:15 on E-5 (22 July, expiry 29 July) on daily bars and on synchronized 5-minute bars, decided at the close of E-6; with it on, the time exit ends it on 21 July; with a holiday in the window the forced exit moves a session earlier |

- **Stored-data results unchanged.** With the time exit on, the forced exit can only fire when two or more holidays
  fall in the last fortnight before an expiry. The strategy's portfolio run on stored data (16 stocks 2025-2026 and 30
  stocks 2026, both executions) gives the same trades, net and drawdown as the rerun above. The full reports and the
  baselines were not rerun.
- **What "covered" means:** NSE publishes a holiday list every year, so a year with no row in holidays.csv is treated as
  not entered. A year with some rows but a missing holiday cannot be detected.
- **Not tested here:** the live path (phase 6 must pass the app's calendar to `settlement_alert`; the route already
  does for the page note). holidays.csv has rows for 2026 only, so every expiry from January 2027 on is counted
  conservatively until the 2027 list is added; the page card says so from 70 days before.

## Review fixes (Codex review of 2e3e2c4..42784c7, two HIGH findings), 2026-11-16

No default changed; variants A-D unchanged. Whole suite: **655 passing**. Each new test fails on the code before the fix.

| # | Finding | Fix | Code | Tests |
|---|---|---|---|---|
| 1 | HIGH: on synchronized 5-minute bars an accepted entry left the portfolio book at 09:15 of its session and came back only when it filled (its first synchronized bar may be hours later). A roll decided in between was judged without it | An accepted entry RESERVES its estimated maximum loss from the acceptance of its signal until it fills (the position then carries the maximum loss recomputed at the fill) or is not made (released). Every gate reads the book, so new signals and roll decisions count the reservations | `OptionEngine.iter_stream` (`entering`, `publish`) | `tests/test_phase9_reservation.py`: RELIANCE holds a spread, INFY's entry accepted on 1 July fills at 12:00 on 2 July, RELIANCE's roll is decided at 10:05 with the total-risk limit at 18% of capital: refused (`CORRELATED_LIMIT`), in either stock order; controls: allowed with room, and allowed when INFY's entry is not made (released at 09:15); the book holds the estimate until the fill and the recomputed maximum loss after it |
| 2 | HIGH: the delivery estimate reused the same bought leg for every sold leg, and computed cash from the first bought leg only | Bought legs that would settle in the money are allocated ONCE across the whole position (same underlying, right and expiry): sold legs most exposed first (highest strike for puts, lowest for calls), bought legs deepest in the money first. Shares and cash are computed for every allocated portion (`netted_with`); sold quantity without a bought share is fully deliverable. Bought quantity in the money that nets nothing is listed on its own (`bought_in_the_money_not_netted`) | `settlement.allocate_hedges`, `_delivery` | `tests/test_phase9_settlement.py`, hand calculations in each test: two sold puts and one hedge (100 shares remain deliverable; before: 0); one sold leg and two hedges (200 netted, Rs 2,500 net paid, Rs 17,500 received from both; before: Rs 1,000); an uneven manual position with puts and calls; an unhedged short put and call |

- **Rerun on stored data: no change.** The full Phase 9 report (16 stocks 2025-2026 and 30 stocks 2026, both
  executions, with baselines, the exit-policy comparison and the score bands) is identical, value for value, to the
  rerun after the time-ordering fixes. This also covers the forced exit added in the M3 follow-ups, whose baselines and
  full reports had not been rerun. In the stored data no roll or signal is decided while another stock's accepted entry
  is waiting for its first synchronized bar near a limit.
- **Not tested here:** the live path (no positions are recorded before phase 6).

## Phase 9 M4 and M5: requirement-to-code/test table, and M4 gaps built (2026-11-16)

Phase 5 not started; variants A-D and every default unchanged; no holdout opened. Requirements from docs/implementation.md
(Phase 9 core additions 14, 15 and 23, the parameter table, P9.16, P9.17, P9.25, P9.26) and the specification rows 0,
5.1-5.3, 7, 8.6 and 8.7 of the table above. C = compliant before this work, B = gap built now.

### M4: range and speed profiles, scenario history
| Requirement | Code | Tests | State |
|---|---|---|---|
| Profile per stock over 2 and 5 years, data from 2019-01-22 only | `stock_profile.range_speed_profile` (`profile.windows_years`), `shortlist.stock_view` (floor, after the last price break) | `test_range_speed_profile_windows_floor_and_implied_move` | C |
| Typical monthly high-low range; sessions swing low to swing high (confirmed 5-bar pivots); ranging or trending now | `range_speed_profile`, `pivots`, `alternating`, `state_now` | `test_swings_state_and_breach_frequency_by_hand`, `test_range_speed_profile_...` | C (pivots since M1) |
| How often a strike at the proposed distance was passed within the holding period, beside the option-implied move (expected_move = spot x IV x sqrt(DTE/365)) | `breach_frequency`, `expected_move` | as above | C |
| Shown on the stock option page; route read-only | `/api/stock-option-selling/profile/{stock}`, page card | `test_profile_route_is_read_only_and_unknown_stock_is_404` | C |
| Breach history at a backtest decision: point in time and nothing before 2019-01-22 | was `closes[-504:]` of whatever the context held (bars before 2019-01-22 for decisions before 2021) | - | **B**: `stock_profile.stats_closes` (the last 504 closes from the statistics floor) in `score.candidate_score` (range_profile) and the signal's `breach_history_pct`; `test_ACCEPTANCE_a_decision_in_2020_reads_no_breach_history_before_2019_01_22` |
| Scenario history: 30-day moves after FII selling streaks, India VIX spikes, RBI decisions, own results | `scenario_history`, `shortlist.scenario_triggers`, `fii_streak_starts`, `vix_spike_dates` | `test_scenario_history_...`, `test_fii_streaks_...`, `test_scenarios_use_the_stored_...` | C |
| Point in time: only cases whose horizon had passed; the move starts at the close of the trigger day (the newest price known when the trigger is known) | `scenario_history` | `test_scenario_history_counts_only_completed_cases...` | C (the starting price is now documented in the function) |
| Case counts; under 10 LOW SAMPLE; missing inputs "no data" naming the file; "history, not a forecast" | `scenario_history`, `DATA_NEEDED` | `test_ACCEPTANCE_scenarios_without_input_data_say_no_data...` | C |
| Exclusions counted (spec 8.7) | trigger dates not counted were dropped silently; breach start days without a complete holding period were not reported | - | **B**: `excluded` by reason and `triggers` on every scenario, the count in its note and a column on the page; `not_yet_complete` on the breach history; `test_ACCEPTANCE_every_trigger_that_is_not_a_case_is_counted_by_reason`, `test_the_breach_history_counts_the_start_days...`, page test |
| VIX spike (section 2) from usable data | the spike series read the whole stored VIX history, including the wrong-scale Breeze bars before 2019-01-22 (issue 30): the floor's first session looked like a +10,000% rise | - | **B**: VIX bars from the floor on only, for the scenario triggers and the backtest's open-position alert (`OptionEngine.market_at`); the first year after the floor has no known non-spike day, so no episode starts in it; `test_ACCEPTANCE_vix_spike_triggers_come_from_vix_bars_on_or_after_the_floor_only`, `test_the_backtest_engine_reads_vix_spikes_from_the_floor_on_too` |

### M5: reversal detector and study
| Requirement | Code | Tests | State |
|---|---|---|---|
| Washed-out list: 25% below the 52-week high, 63 sessions in a row below sma_long; shown as "watch", never a signal | `reversal.ReversalScanner`, `shortlist.reversal_watch`, page card | `test_ACCEPTANCE_washed_out_then_base_forming_then_reversal_confirmed`, `test_ACCEPTANCE_washed_out_stocks_are_shown_as_watch_never_as_a_signal` | C |
| Base forming: 52-week low at least 15 sessions old, ATR(10) < 0.8 x ATR(20) | `ReversalScanner._scan` | as above, `test_each_confirmation_condition_is_needed` | C |
| REVERSAL_CONFIRMED: close above the 20-day average and the last swing high (newest CONFIRMED 5-bar pivot high, usable only after its 5 later sessions), volume above its 20-day average, 10-day return ahead of NIFTY; point in time | `ReversalScanner` (pivots per unbroken stretch, `by_confirm`) | `test_each_confirmation_condition_is_needed`, `test_the_state_of_a_day_never_uses_later_bars` | C (pivots since M1) |
| Lifts the falling-stock block only for put spreads below the base low | `stock_selling.assess` | `test_ACCEPTANCE_reversal_lifts_the_falling_stock_block_only_for_a_put_spread_below_the_base_low` | C |
| Two research candidates (bull call spread; put credit spread sold below the base low) with stall zones (confirmed pivot lows of the 52-week window above the price) | `BullCallSpread`, `ReversalPutSpread`, `ReversalScanner.stall_zones` | `test_ACCEPTANCE_confirmation_gives_a_put_credit_spread_below_the_base_low`, `..._a_bull_call_spread_with_stall_zones` | C |
| Nothing alerts until approved; the reversal alert goes to every open position once approved | `reversal.alerts_approved`, `alert_kinds` | `test_ACCEPTANCE_reversal_alert_goes_to_every_open_position_only_after_approval` | C |
| Study: 10/20/40-session outcome of every confirmation, the share making a new low, LOW SAMPLE | `reversal_outcomes`, `run_reversal_reports` | `test_reversal_outcomes_by_hand_and_low_sample` | C |
| Both candidates against their no-filter baselines (same structure, same stocks and period; the put candidate against baseline:PE, the bull call against the bull call schedule) | `run_reversal_reports` | `test_ACCEPTANCE_backtest_reports_every_reversal_and_both_candidates_against_the_baseline` | C |
| The outcome study against matching baselines | the outcomes had no baseline: a median move after a confirmation could not be told apart from what washed-out stocks did anyway | - | **B** (M5 below): `reversal.reversal_outcomes` `baselines`, `versus_matching_baseline`; printout and backtests page; `tests/test_phase9_m5.py` |

### M4 result on stored data
The strategy's portfolio runs on stored data (16 stocks 2025-2026 and 30 stocks 2026, both executions) give the same
trades, statistics and skipped counts as before: every decision there is after 2021, so the floor on the breach history
changes nothing, and the VIX change only touches 2019. Whole suite: **661 passing**.

## Phase 9 M5: the reversal outcome study against matching baselines (2026-11-16)

The only M5 gap (table above). Defined in `reversal.reversal_outcomes` before any result was looked at; the same stocks,
the same tested sessions and the same measurement as the confirmations (close to the close h sessions later, the whole
horizon inside the period; a new low = a later low under the reference low within 40 sessions):
- **matching baseline** (`washed_out_not_confirmed`): every session on which the stock was washed out and no reversal was
  confirmed; reference low = the lowest low of that day's 52-week window (a confirmation's base low is that window's low
  on its base day);
- **reference** (`every_session`): every session of the same stocks, for the drift of the period.
`versus_matching_baseline` gives the confirmations' median and up-share minus the matching baseline's per horizon, and the
new-low share minus its, LOW SAMPLE when either side has under 30 cases. Baseline sessions overlap (consecutive days share
most of a horizon), so their cases are not independent. Tests: `tests/test_phase9_m5.py` (by hand). Whole suite:
**664 passing**.

### Result on stored daily data (50 watchlist stocks, development period 2019-01-22 .. 2026-06-05; no option prices needed)
| | Cases (10 / 20 / 40) | Median move 10 / 20 / 40 sessions | Up in % of cases | New low within 40 sessions |
|---|---|---|---|---|
| Confirmed reversals (27 in 19 stocks) | 26 / 26 / 26 (LOW SAMPLE) | -1.43% / +1.23% / +1.80% | 38.5 / 57.7 / 53.8 | 3.8% |
| Matching baseline: washed out, not confirmed | 4,456 / 4,383 / 4,286 sessions | +1.46% / +3.15% / +5.11% | 61.2 / 65.6 / 68.6 | 16.8% |
| Reference: every session | 88,189 / 87,689 / 86,689 sessions | +0.65% / +1.38% / +2.73% | 55.3 / 57.5 / 60.3 | 11.6% |
| Confirmed minus matching | - | -2.89 / -1.92 / -3.31 points | -22.7 / -7.9 / -14.8 points | -13.0 points |

- **History, not a forecast; LOW SAMPLE (26 cases): no conclusion.** On these numbers a confirmation was not followed by a
  better price move than the average washed-out day; fewer confirmations went on to a new low (1 of 26 against 17% of
  washed-out sessions). Nothing was changed: the detector stays watch only, and no threshold was tuned.
- The two candidate trades against their no-filter baselines are unchanged from the M1 report (no confirmation inside the
  stored option months).
- Not tested here: nothing new; the study reads stored daily bars only.

## Review fixes: Codex review of 968d410..267f09b (2026-11-16)

| # | Finding | Fix | Tests (fail on 267f09b, pass now) |
|---|---|---|---|
| 1 HIGH | Outcome windows counted bars, not sessions: a window could cross an unadjusted price break (a 2-for-1 split read as -50%) and a missing session stretched it | `reversal.market_sessions`, `_window`: a horizon is h market sessions (the holiday calendar's trading days; the NIFTY sessions in a year holidays.csv has no holidays for); every session in the window needs a price and no break (`ReversalScanner.breaks`) may lie inside; otherwise excluded by reason. Same rule for the confirmations, both baselines and the new-low window | `tests/test_phase9_m5_review.py`: `..._2_for_1_split_is_excluded`, `..._missing_session_is_excluded_not_stretched`, `test_horizons_are_counted_in_market_sessions_from_the_holiday_calendar` |
| 2 MEDIUM | `every_session` left out the confirmation dates | confirmation dates are in `every_session` (reference low: the 52-week low, like every other session); left out of `washed_out_not_confirmed` only | `test_ACCEPTANCE_every_session_includes_the_confirmation_dates`; `tests/test_phase9_m5.py` corrected (every session 8 cases, new low 2 of 8 = 25%) |
| 3 MEDIUM | Scenario triggers read FII rows before the floor (a floor-date streak from pre-floor selling) and FII/VIX/index rows after the as-of date | `shortlist.scenario_triggers`: index sessions, FII rows and VIX bars restricted to [floor, as-of] before any streak or spike | `..._fii_selling_before_the_floor_makes_no_streak_on_the_floor_date`, `..._triggers_as_of_a_date_contain_nothing_later` |
| 4 LOW | No count of the windows left out | `windows`: for every group and horizon, eligible starts, completed cases and exclusions by reason, asserted to add up; on the printout | `test_every_group_and_horizon_reports_eligible_completed_and_excluded`, `test_the_printout_shows_the_window_accounting` |

Baseline definitions unchanged. Whole suite: **672 passing**.

### Rerun on stored data (50 stocks, 2019-01-22 .. 2026-06-05), old (267f09b) -> new
| | Cases 10 / 20 / 40 | Median 10 / 20 / 40 | Up % 10 / 20 / 40 | New low within 40 |
|---|---|---|---|---|
| Confirmed (27) | 26 / 26 / 26 -> same | -1.43 / +1.23 / +1.80 -> same | 38.5 / 57.7 / 53.8 -> same | 3.8% -> 3.8% |
| Matching baseline | 4,456 / 4,383 / 4,286 -> 4,454 / 4,381 / 4,259 | +1.46 / +3.15 / +5.11 -> +1.46 / +3.15 / +5.20 | 61.2 / 65.6 / 68.6 -> 61.2 / 65.6 / 69.0 | 16.8% -> 16.2% |
| Every session | 88,189 / 87,689 / 86,689 -> 88,040 / 87,370 / 86,030 | +0.65 / +1.38 / +2.73 -> +0.66 / +1.41 / +2.82 | 55.3 / 57.5 / 60.3 -> 55.3 / 57.7 / 60.7 | 11.6% -> 11.0% |
| Confirmed minus matching | | -2.89 / -1.92 / -3.31 -> -2.89 / -1.92 / -3.40 | -22.7 / -7.9 / -14.8 -> -22.7 / -7.9 / -15.2 | -13.0 -> -12.4 points |

Exclusions (new): confirmations 1 of 27 (horizon past the period, every horizon); matching baseline 4,546 starts, at 40
sessions 260 past the period and 27 across a price break; every session 88,715 starts (the 27 confirmation days now
included), at 40 sessions 2,000 past the period and 685 across a price break. No window was excluded for a missing
session: on stored data every stock has a bar on every NIFTY session of the period, and in 2026 every holiday-calendar
trading day has a NIFTY bar. Holidays.csv covers 2026 only, so for 2019-2025 the NIFTY sessions are the market sessions
(a day missing from NIFTY and the stock alike would go unnoticed there). Still LOW SAMPLE (26 cases): no conclusion,
nothing tuned.

Scenario history (50 stocks, summed): unchanged - FII streak 840 cases of 850 triggers, India VIX spike 2,010 of 2,600,
RBI decision 823 of 1,100, own results no data. The stored FII rows start 2026-01-14 (none before the floor), and no
FII row or VIX spike lies after a stock's last bar, so the bound changes nothing on this data.

## Phase 5: risk engine (2026-11-16)


### Requirement-to-code/test table (written before the code)

| Req | Requirement (docs/implementation.md phase 5) | Code | Test (`tests/test_phase5_*.py`) |
|---|---|---|---|
| S1 / P5.1 | Cash quantity = floor(capital x max_loss_per_trade_pct / (entry - stop)), capped at position value `backtest.position_cap_pct`, charges at the stop included | `app/risk/sizing.py` `cash_quantity` (reuses `app.backtest.position_size` for the formula and the cap) | `test_phase5_sizing.py`: hand calculations; just under / at / over the budget and the cap |
| S1 | Options: lots = floor(max_loss_per_option / max loss per lot), cost-inclusive; zero lots = NO_TRADE (ZERO_SIZE) | `app/risk/sizing.py` `option_lots` (two-leg spreads through `app.options.sizing.size_spread`; long options and condors with the same floor-then-check rule on `app.options.payoff`) | `test_phase5_sizing.py`: one lot's loss just under / at / over the budget, 2 lots exactly at the budget |
| S2 / P5.2 | Track open paper and manual positions, capital committed, margin blocked, cash free; enforce `uncommitted_cash_pct` | `app/risk/portfolio.py` `OpenPosition`, `AccountState`, `pool_check` | `test_phase5_limits.py`: committed + new just under / at / over capital x (1 - 25%) |
| S2 | Enforce `max_open_selling_spreads` (index option selling) | `app/risk/portfolio.py` `selling_spread_check` | `test_phase5_limits.py`: 1 open -> allowed, 2 open -> blocked; limit 0 |
| S3 / P5.3 | Sector tags on the watchlist; at most `max_trades_per_sector` (2) open trades per sector | `config.yaml` (sector on NIFTY, BANKNIFTY), `app/risk/limits.py` `sector_check` | `test_phase5_limits.py`: 1 -> second allowed, 2 -> third blocked (SECTOR_LIMIT); unknown sector blocks |
| S3 | Symbols with 60-day return correlation above 0.8 count as one position (their risk together within one position's budget) | `app/risk/limits.py` `correlation_check` (reuses `app.options.portfolio` correlation, groups and unknown-link logic) | `test_phase5_limits.py`: 0.8 exactly not linked, 0.8001 linked; combined risk just under / at / over; unmeasurable -> CORRELATION_UNKNOWN |
| S4 / P5.4 | `daily_loss_stop_pct` and `monthly_loss_pause_pct` on realised + open P&L | `app/risk/limits.py` `loss_stops` | `test_phase5_limits.py`: loss just under / at / over 2% (DAILY_STOP) and 6% (MONTHLY_PAUSE); unknown P&L blocks |
| S5 / P5.5 | Drawdown from peak equity, deposits excluded; halve sizes at 10%, pause at 15% | `app/risk/limits.py` `drawdown_state` (deposit-neutral equity index) | `test_phase5_limits.py`: 9.99 / 10 / 10.01 and 14.99 / 15 / 15.01 %; a deposit never hides a drawdown; halved quantities by hand |
| S6 / P5.6 | Kill switch: index feed STALE, quarantined ticks spike, outside market hours | `app/risk/limits.py` `kill_switch` (`risk.kill_switch` in config) | `test_phase5_limits.py`: each trigger; spike count just under / at / over |
| S7 / P5.7 | APPROVED with quantity and rupee risk, or NO_TRADE with reason codes | `app/models.py` `RiskDecision`; `app/risk/engine.py` `decide` | `test_phase5_engine.py`: every code produced in a table; every NO_TRADE has a code |
| S8 | Changing capital_inr rescales everything; deposits logged separately, never profit | `app/risk/engine.py` (all rupee limits from capital at call time); `capital_log` table, `POST /capital/log` | `test_phase5_engine.py`: capital doubled -> every limit and quantity rescales; `test_phase5_api.py`: deposit stored, not counted as profit |
| A1 | Table-driven tests for each rule | - | each rule above |
| A2 | No code path can raise risk above the configured percentages | `decide` (risk only reduced) | `test_phase5_engine.py`: property test (hypothesis) over random signals and states |
| A3 | Unknown inputs always produce NO_TRADE | `decide` | `test_phase5_engine.py`: one row per unknown input (capital, positions, P&L, equity, margin, lot size, costs, price status, regime, sector, correlation, kill-switch inputs) |
| U1 | User: unknown capital, margin or position data blocks the signal | `decide` | as A3 |
| U2 | User: reuse app/options/sizing.py and portfolio.py; do not change the Phase 9 backtest loop, variants A-D or any Phase 9 default | imports only; `store_correlations` gets optional window arguments (defaults unchanged) | whole suite unchanged; `test_phase5_engine.py` checks the Phase 9 defaults |
| P9-8 | Phase 9 step 8 (risk engine for stock option spreads): capital advisory (capital-fit label), blocks on unknown data or margin, portfolio limits of the Phase 9 specification | `decide` stock-spread path: `capital_fit`, `app.options.portfolio.portfolio_check` | `test_phase5_engine.py`: NEEDS_MORE_CAPITAL at Rs 2,00,000 with the shortfall, FITS at Rs 10,00,000, TIGHT between; never hidden for capital |
| R1 | `/api/risk` (architecture section 10) and a risk strip on the dashboard | `app/main.py`, `app/page.py`, `app/risk/engine.py` `RiskService` | `test_phase5_api.py` |

### Built
- `app/risk/sizing.py`: `cash_quantity` (the phase 3 formula `app.backtest.position_size` with its position value cap,
  then the largest quantity whose loss at the stop plus estimated charges and slippage stays within the budget, exact in
  Decimal), `option_lots` and `lots_within` (floor(budget / one lot's cost-inclusive max loss), then lowered while the
  n-lot loss with n lots' charges is above the budget; two-leg spreads through `app.options.sizing.size_spread`, long
  options and iron condors through `app.options.payoff`).
- `app/risk/portfolio.py`: `OpenPosition` (paper or manual: risk, capital committed, open P&L today and this month),
  `AccountState`, `position_problems`, `committed_inr`, `capital_fit` (FITS / TIGHT / NEEDS_MORE_CAPITAL with the
  shortfall), `pool_check` (uncommitted_cash_pct), `selling_spread_check` (max_open_selling_spreads, index selling).
- `app/risk/limits.py`: `sector_check`, `correlated_group` (connected components with `app.options.portfolio`'s
  `_components`; a link needs correlation strictly above 0.8; an unmeasurable link blocks only if it could join the
  proposed symbol to a held one), `loss_stops`, `drawdown_state` (deposit-neutral equity index), `kill_switch`.
- `app/risk/engine.py`: `decide(signal, account, market, ...)` -> `RiskDecision` (every failing check adds its code; a
  final guard refuses any approval whose risk is above the budget) and `RiskService` (live inputs: capital from
  config.yaml, kill switch from the hub and `quality_events`, deposits from `capital_log`; positions NOT KNOWN).
- `app/models.py`: `RiskDecision` (architecture contract, plus `reasons`, `size_factor`, `facts`); a NO_TRADE without a
  code or an APPROVED without a positive size and risk cannot be constructed.
- `config.yaml` / `app/config.py`: `risk.max_trades_per_sector` 2, `correlation_window` 60, `correlation_threshold` 0.8,
  `correlation_min_overlap` 50, `kill_switch` (5 minutes, 20 events); validated (halve below pause, overlap within the
  window). NIFTY and BANKNIFTY tagged sector "Index".
- `app/store.py`: `capital_log` table (date, DEPOSIT/WITHDRAWAL, amount, note), `quality_event_count_since`.
- `GET /api/risk`, `POST /capital/log` (local Host/Origin and JSON rules as every write), a "Risk engine" card on the
  dashboard.
- `app/options/shortlist.py` `store_correlations`: optional window and minimum-overlap arguments (defaults unchanged).
- Not changed: the Phase 9 backtest loop, variants A-D (pre-registration), every Phase 9 default, `app/backtest.py`.

### Tested (176 new tests in `tests/test_phase5_sizing.py`, `test_phase5_limits.py`, `test_phase5_engine.py`, `test_phase5_api.py`; whole suite 848 passing)
- Every sizing, limit and loss-stop rule with its hand calculation written in the test and the boundary just under,
  exactly at and just over: cash budget and position cap, costs included, size factor; long option, credit spread and
  iron condor lots; capital pool; capital fit (the phase 9 acceptance: NEEDS_MORE_CAPITAL with Rs 30,000 short at
  Rs 2,00,000, FITS at Rs 10,00,000, TIGHT between); selling spreads; sector (third trade blocked); correlation 0.7999 /
  0.8 / 0.8001 and the correlated budget; daily stop at 3,999.99 / 4,000 / 4,000.01; monthly pause; drawdown 9.99 / 10 /
  10.01 and 14.99 / 15 / 15.01 %; kill-switch spike 19 / 20 / 21.
- Every reason code produced by `decide` in a table, each with its text; all codes reported together.
- Unknown inputs: capital (None, NaN, 0), positions, a position's risk / margin / open P&L, realised P&L, equity history,
  a deposit amount, price status, decision time, regime, index status, market hours, quarantine count, cost rates,
  sector, correlation, margin, lot size: each gives NO_TRADE.
- Property tests (hypothesis, 500 random cases a run, hundreds of approvals across cash, index and stock options, with
  zero, flat and the config.yaml cost rates): an approval never carries more risk than the configured percentage (with
  the correlated group), never breaks the position cap or the capital pool, and the drawdown rule never adds size.
- Capital doubled: every limit, share quantity and lot count doubles; the daily stop moves with it.
- Deposits: never profit, never hide a drawdown; a withdrawal is not a loss. `/capital/log` stores entries and rejects
  bad ones (type, negative, text, boolean, date format, future date, foreign Host).
- The live service: quarantine window count, kill switch on at 20 in 5 minutes, outside market hours, stale index; a live
  decision is NO_TRADE (PORTFOLIO_UNKNOWN) without positions.
- Determinism; Phase 9 defaults unchanged; safety tests pass (the new write route is under `/capital/`).
- App run in simulated mode (port 8765, separate database): `/api/risk` returned the limits at Rs 2,00,000, kill switch
  off at 11:39 IST, portfolio not known; `POST /capital/log` stored a deposit; the dashboard shows the Risk engine card.

### Not tested / known gaps
- No real position has been sized: open positions, P&L and the equity history come from the phase 6 journal and paper
  trader. Until then `RiskService` reports them as not known and every live decision is NO_TRADE.
- The engine is not yet called by anything that produces live signals (phase 6 wires strategies -> risk -> cards).
- The quarantine spike threshold (20 ticks in 5 minutes) is a starting value, not measured on live ticks.
- Cost-inclusive cash sizing uses the cost model's estimate at the stop; real fills and contract notes may differ.
- Margin for spreads must still be entered by hand (issue 46); without it every spread is MARGIN_UNKNOWN.

### Decisions made in this phase (yours to change)
- Cash risk includes the estimated round-trip charges and slippage at the stop, so the rupee risk never exceeds the
  limit (the formula of the specification gives an upper bound and is applied first).
- "At the limit" counts as reached: exactly 2% lost today stops, exactly 10% drawdown halves, exactly 15% pauses, the
  20th quarantined tick in the window trips the kill switch. The capital pool allows committing exactly 75%.
- "Correlation above 0.8 = one position": the held risk of the proposed symbol's correlated group is taken off the
  proposed trade's budget (a correlated pair together risks no more than one trade); the same symbol always counts as
  one position. Correlation exactly 0.8 is not linked.
- NIFTY and BANKNIFTY share the sector "Index", so at most two open index trades at a time (any type). Change the tag in
  config.yaml if you want each index counted on its own.
- Halving sizes halves the risk budget and the position value cap (capital x 0.5 for sizing).
- Stock option spreads (phase 9) skip the phase 5 capital pool, sector and correlation rules (the Phase 9
  specification's capital-fit label and portfolio limits apply instead), but the kill switch, the daily stop, the monthly
  pause and the drawdown pause and halving apply to them, because each only reduces risk.
- Drawdown excludes deposits with a deposit-neutral index: a flow dated after one close and on or before the next is
  taken out of that period's change.
- The kill switch checks market hours in sim mode too (the hub does not); outside 09:15-15:30 IST it is on.

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
| 63 | 9 | Section 9.1: the stock-option capital for Phase 9 backtests was received as a placeholder | Closed 2026-11-14: Rs 10,00,000 (`stock_option_selling.backtest.capital_inr`) |
| 64 | 9 | With the section 2 defaults and Rs 2,00,000 capital the stored period has no trade (illiquid 45, max loss 27 of 90 baseline attempts) | Reported; no default changed |
| 65 | 9 | Events: with no verified event source a stock passes the event rule; the specification requires EVENT_COVERAGE_UNKNOWN to block | Fixed 2026-11-14 for live and research candidates; backtests list it as a bypassed check |
| 66 | 9 | 5-minute backtest execution conflicts with section 8.1 (stale leg price, daily entry and fallback); exit-policy comparison not yet on 5-minute bars | Fixed 2026-11-14: synchronized execution; exit policies run on both. Hold-through-gap sensitivity analysis still to build |
| 67 | 9 | Open campaigns at the cutoff are closed and counted as trades; the specification wants them reported as open | Open (M1) |
| 68 | 9 | Bull call candidate's 20-65 day window is not in section 2 and its baseline almost never enters | Closed 2026-11-14: 20-35 days; the baseline now enters (59 of 125 attempts) |
| 69 | 9 | `min_credit_to_max_loss`: no stored spread reaches 0.20 (median 0.10, 75th percentile 0.135, n 28) | Closed 2026-11-14: set to 0.10 by the user |
| 70 | 9 | The research pages still use `account.capital_inr` Rs 2,00,000; only the Phase 9 backtests use Rs 10,00,000 | Closed 2026-11-14: research pages use Rs 10,00,000 |
| 71 | 9 | MARKET_WEAK is applied to put selling only (a weak market is not held against a bear call spread); unknown market data gives MARKET_UNKNOWN | Closed 2026-11-14: confirmed by the user |
| 72 | 9 | Open interest minimum of 200 lots skips 159 of 250 baseline attempts in the stored period | Reported; no default changed |
| 73 | 9 | After the M1 rule changes the report wants 430 contracts that are not stored | 2026-11-15: 2026 complete for 30 stocks, 2025 for 16; reports still miss 46 / 20 contracts from runs the download pass does not replay (portfolio-gated, score scans, alert exits) |
| 74 | 9 | Bull call spread baseline with whole-lot sizing: 47 trades, -2,36,340 (PF 0.47) in five months | Reported; candidate stays watch only |
| 75 | 9 | Score bands need several candidates a day; the stored months give 1 on 20 of 22 days | Open: needs more stocks and years downloaded |
| 76 | 9 | Pre-registered comparison of variants A-D on 2023-2024 (2026-11-15) | Open: waits for 2023-2024 to be completely downloaded for the 30 priority stocks; B and C rows to be built first |
| 77 | data | 1,856 unexplained daily-close mismatches Breeze vs the external repo (BSE 673, TATASTEEL 638, ...) | BSE, TATASTEEL investigated 2026-11-15: Breeze errors (adjustment boundary 2021-08-02, bad bars); the stock-years are excluded by the pre-registered rule |
| 78 | data | TATASTEEL Breeze history has one-day price breaks (2021-08-02, 2022-03-29/30, 2022-06-14/15): 718 decision sessions short of history | Open |
| 79 | data | The corporate-action adjustment changes the daily history of 7 priority stocks: backtest results on them change | Rerun 2026-11-15 (see Data quality, section 3); 874 / 133 contracts now wanted, plan version 7 |
| 80 | data | NSE board-meetings and corporate-actions CSV columns not verified against the live pages | Open: first real download will confirm or be rejected with the columns named |
| 82 | review | Correlation unknown through unheld stocks; board-meetings blanket coverage; daily-close marks in five-minute runs; shared-set completion; repo prices in STATUS.md; future-dated rows judged fresh | Fixed 2026-11-15 (Review fixes, Codex review of 782e870..2e3e2c4) |
| 83 | review | Git history (private repo) holds external-repo prices in older commits of docs/STATUS.md | Open: clean the history before the repo is ever made public |
| 84 | review | KOTAKBANK 2025 and ETERNAL 2020 no longer complete after the per-block recheck | Open: the next `--download` fetches what they want |
| 85 | 9 M3 | ICICI auto-close time (two differing statements), fresh-position block and the 11:00 shares deadline are unconfirmed | Waiting for user: confirm with ICICI, then set `confirmed: true` |
| 88 | review | Backtest ran each stock's whole session before the next stock's; intraday roll gate read that day's close | Fixed 2026-11-15 (time-ordering fixes); rerun: one trade changed |
| 86 | 9 M3 | The rupee amount of the expiry-week margin needs ICICI's VaR + ELM + adhoc margin for the position; the system does not have it | Open: shown as "margin unknown" with the schedule's percentage until the phase 6 position form takes the figure |
| 87 | 9 M3 | Settlement day counts use weekdays in the tests; phase 6 must pass the holiday calendar | Fixed 2026-11-15: counted with the holiday calendar; a calendar that does not cover the expiry month counts one session earlier and says so. Phase 6 still has to pass the calendar when it sends alerts |
| 90 | review | Pending entries dropped out of the portfolio book until their first synchronized bar; settlement estimate reused one hedge for several sold legs and counted cash from the first hedge only | Fixed 2026-11-16; rerun on stored data identical |
| 89 | data | holidays.csv has 2026 only: settlement alerts for expiries from January 2027 are counted conservatively (one session earlier) | Waiting for user: add the 2027 NSE holidays when published |
| 81 | data | Stock-year exclusion, amended 2026-11-15 (exact corrections, repo-confirmed jumps, smooth-ratio test): 8 priority stock-years excluded from 2019 (1 in 2023-2024), 10 other watchlist ones | Applied |
| 49 | 4 | At Rs 2,00,000 capital one lot of most index spreads risks more than 2.5% of capital (for example a 200-point NIFTY spread: about Rs 10,000 to 11,000) | Confirmed by phase 5 (2026-11-16): such a spread gets ZERO_SIZE at Rs 2,00,000. Waiting for user: narrower width, lower limit or more capital |
| 91 | 5 | Every live risk decision is NO_TRADE (PORTFOLIO_UNKNOWN) because open positions, P&L and equity history are recorded by the phase 6 journal | Open: phase 6 |
| 92 | 5 | NIFTY and BANKNIFTY share the sector tag "Index": at most two open index trades together | Waiting for user: keep, or give each index its own tag |
| 93 | 5 | Kill-switch quarantine spike (20 ticks in 5 minutes) is a starting value | Open: check against a live market day (phase 6) |
| 94 | 5 | Loss stops, drawdown pause and kill switch also stop stock option spread signals (phase 9 capital stays advisory) | Waiting for user: confirm |
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
| 2026-11-14 | Synchronized 5-minute execution (section 8.1), as built: a "synchronized bar" is a 5-minute bar in which every leg of the spread traded. A "required leg bar is missing" when a session of the holding period has no synchronized bar at all, or a leg's 5-minute bars for that session are not stored; the campaign is then data-incomplete and excluded. An entry session without a synchronized bar gives no entry. Rules decided at the session's close (time exit, strike breach, alert) are filled at the next session's first synchronized bar. User to confirm this reading, because thinly traded hedge strikes will exclude many campaigns |
| 2026-11-14 | Roll decision inside a session (stop reached at a synchronized close): the replacement is chosen and judged by the entry rules on the last completed session's data; the roll net credit is priced from that bar's closes of all four legs; a replacement leg with no bar there leaves the roll unpriced and the answer is exit. The roll fills at the first later bar in which all four legs traded, else the spread is just closed (`ROLL_NOT_FILLED`) |
| 2026-11-14 | The stale-leg setting `intraday_leg_max_age_minutes` is removed. 5-minute bars are still stored only from 40 to 5 days before each expiry: a roll into a contract more than 40 days out cannot be filled on synchronized bars and is recorded as not made |
| 2026-11-14 | Phase 9 backtests run at Rs 10,00,000 (`stock_option_selling.backtest.capital_inr`); the account capital in config.yaml is unchanged |
| 2026-11-14 | Event coverage: a COVERAGE row in events.csv (symbol, source, retrieved_at, covered_through), at most 7 days old and covering expiry, is required for a live or research candidate; without it the candidate is listed with EVENT_COVERAGE_UNKNOWN and gives no signal. Stored in the events table with the note `source=...; covered_through=...` |
| 2026-11-14 | `min_credit_to_max_loss` is not applied (0) until the user sets it from the distribution |
| 2026-11-14 | MARKET_WEAK: breadth = share of the watchlist stocks that traded that day and closed above their 50-session average; unknown when under 80% of them have 50 closes. Applied to put selling only. New reason code MARKET_UNKNOWN for missing market data |
| 2026-11-14 | Credit / max-loss distribution: one observation per stock, expiry and side, on the first day the spread passes every other rule; campaign limit off because it depends on results |
| 2026-11-14 | Sensitivity run for data-incomplete campaigns: a session without a synchronized bar is judged on the legs' daily closes; an exit decided there still fills at the next session's first synchronized bar; made only when more than 30% of the strategy's or the baseline's campaigns are excluded |
| 2026-11-15 | Board-meetings coverage only from a declared, unfiltered all-companies export that passes the sanity checks (100 companies, dates inside and spanning the declared range within 7 days); coverage through the declared end date (review fix 2) |
| 2026-11-15 | Five-minute runs: marks at the last synchronized 5-minute bar of each session; none that day = the previous mark carried with its age (review fix 3) |
| 2026-11-15 | Tracked files never carry values of the external repo's rows (factors, methods, aggregates only); history is not rewritten while the repo is private, but must be cleaned before it is ever made public (review fix 5) |
| 2026-11-15 | Rows dated after today are a data error on the Data status page and never count as fresh (review fix 6) |
| 2026-11-15 | Settlement alerts: URGENT from E-5 (same day as the forced exit) for a short stock option ITM or within 5% of spot; ICICI rules stored with per-rule `confirmed` flags; alert deadlines use the earliest stated auto-close time for the legs held |
| 2026-11-15 | Settlement readiness: near the money = strike within 5% of spot and not in the money; a leg further out never alerts. Before E-7 nothing is raised, even with an unknown price; from E-7 an unknown price is URGENT; an unknown expiry is always URGENT |
| 2026-11-15 | Settlement deadline = the earliest of the close (15:30) of the forced-exit day (E-5) and ICICI's times (delivery margin start, auto close, call shares deadline); after it a position not marked closed is alerted in red on every evaluation, whatever its moneyness |
| 2026-11-15 | Delivery estimate nets a sold leg only with a bought leg of the same right that is in the money at the last price too; the expiry-week margin is the schedule's percentage of a margin base entered by the user, never computed or assumed by the system |
| 2026-11-15 | Portfolio backtest: one queue of every stock's events in time order; at one instant decisions, then new signals, then fills; only truly simultaneous events of different stocks follow the stock order (most liquid first) |
| 2026-11-15 | A decision inside a session uses correlations of completed sessions before that day; a decision at the session's close may use that day |
| 2026-11-15 | Settlement trading days come from the holiday calendar; a year of the expiry month (31 days to expiry) without any holiday row, or no calendar, means weekdays less one: one session earlier, shown in red on the card (user) |
| 2026-11-15 | Backtest: forced exit at the first session inside the last 5 trading days (FORCED_EXPIRY_EXIT), after the time exit; the time exit can be switched off on the strategy object for tests, the forced exit cannot |
| 2026-11-16 | Backtest: an accepted entry reserves its estimated maximum loss in the portfolio book until it fills (then the maximum loss recomputed at the fill) or is not made (released); every gate counts reservations |
| 2026-11-16 | Delivery estimate: bought legs in the money are allocated once across the position, to the most exposed sold legs first (puts: highest strike; calls: lowest), deepest bought leg first; unallocated sold quantity is fully deliverable (user) |
| 2026-11-16 | Scenario history: every trigger date that is not a case is counted by reason (before the floor, no session within 5 days, same starting session, horizon not passed); the breach history counts its incomplete start days |
| 2026-11-16 | Statistics floor: the breach history a decision reads is the last 504 closes on or after 2019-01-22; India VIX spikes are computed from VIX bars on or after 2019-01-22 only (older Breeze VIX is wrong-scale) |
| 2026-11-16 | Reversal study baselines: the matching baseline is every washed-out session without a confirmation (same stocks, same sessions, same measurement; reference low = that day's 52-week low); every session is shown as a reference; differences are evidence only when neither side is LOW SAMPLE |
| 2026-11-16 | Phase 5: cash risk includes estimated charges and slippage at the stop; a limit is reached at equality; correlation strictly above 0.8 links symbols, and a correlated group shares one position's risk budget; sizes halved = budget and position cap halved |
| 2026-11-16 | Phase 5: NIFTY and BANKNIFTY tagged sector "Index"; unknown sector, correlation, positions, P&L, equity, margin, lot size, costs or kill-switch input = NO_TRADE |
| 2026-11-16 | Phase 5: stock option spreads keep the phase 9 rules (capital advisory, phase 9 portfolio limits, never more lots than signalled) and also stop on the kill switch, loss stops and drawdown pause |
| 2026-11-16 | Phase 5: deposits and withdrawals in `capital_log` (`POST /capital/log`); drawdown uses a deposit-neutral equity index |
| 2026-11-16 | Reversal study windows: a horizon is h market sessions from the holiday calendar (the NIFTY sessions in a year holidays.csv has no holidays for, since only 2026 is entered); a window with a session the stock has no price for, or an unadjusted price break inside it, is excluded by reason, for the confirmations and both baselines alike; every_session includes the confirmation dates. Scenario triggers read inputs inside [floor, as-of date] only |
