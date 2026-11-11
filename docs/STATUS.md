# Status

Claude updates this file at the end of every phase. It is the memory between sessions: read it first.

## Summary

| Phase | Name | State | Tests | Finished |
|---|---|---|---|---|
| 1 | Data feed and quality gate | Built; live Breeze run pending | 18 passing | 2026-11-11 |
| 2 | Market-regime classifier | Built and tested in sim mode; review fixes applied; live Breeze history/VIX pending | 133 passing (whole suite) | 2026-11-11 |
| 3 | Intraday and swing strategies with backtests | Not started | | |
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
- `regime_log` is written only when the session, regime, bias or basis changes. A background thread re-evaluates every 60 s.
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

## Open issues

| # | Found in phase | Issue | State |
|---|---|---|---|
| 1 | 1 | Live Breeze connection never run | Waiting for user |
| 2 | 1 | Watchlist Breeze codes unverified | Waiting for user |
| 3 | 2 | India VIX Breeze code `INDVIX` unverified (`python scripts/lookup_code.py INDIAVIX`) | Waiting for user |
| 4 | 2 | Live history download never run (`python -m app.history`) | Waiting for user |
| 5 | 2 | holidays.csv / events.csv / fii_dii.csv need real data from NSE, RBI and companies | Waiting for user |
| 6 | 1 | Review: NaN/inf last, bid or ask passed the quality gate (and history parsing) | Fixed 2026-11-11 |
| 7 | 2 | Review: an invalid holidays.csv emptied the calendar; invalid events/fii_dii files did not make the regime UNKNOWN | Fixed 2026-11-11 |
| 8 | 1 | Review: sim and live 1-minute bars shared one table keyed by symbol and minute | Fixed 2026-11-11 (separate files; old market.db holds sim data only, kept) |
| 9 | 2 | Review: zero or missing ATR caused a division error in the regime | Fixed 2026-11-11 |
| 10 | 2 | Review: write route open to cross-site requests | Fixed 2026-11-11 (Host/Origin check, JSON only) |
| 11 | 2 | Old `data/market.db` (sim data only) is no longer used | Waiting for user: delete it if you wish |

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
| 2026-11-11 | An invalid CSV file keeps the last good copy and makes the regime UNKNOWN; an FII/DII paste is refused while fii_dii.csv itself is invalid (fix the file) |
| 2026-11-11 | Write requests need local Host/Origin and JSON; GET and HEAD are unaffected |
