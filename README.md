# Trading Research System - Phases 1-3: data feed, quality gate, market regime, strategies and backtests

Research and suggestions only. **This app cannot place, change or close orders**,
and a test fails the build if such code is ever added. You trade manually in ICICI Direct.

## What you get
- Live price table at http://127.0.0.1:8000 (local machine only)
- Two feed modes: `sim` (fake prices, no login needed) and `breeze` (live ICICI data, read-only)
- Quality gate: bad prices, crossed bid/ask, sudden jumps and stale data are quarantined
- 1-minute bars and history saved to SQLite: `data/market_sim.db` in sim mode, `data/market_live.db` in breeze mode
  (chosen from `feed.mode`; the live file refuses simulated rows). The old `data/market.db` held only sim data and is no longer used.
- `config.yaml`: capital and every risk limit as a % of capital
- **Phase 2:** a "Today's market type" badge at the top of the page: `TRENDING_UP`, `TRENDING_DOWN`,
  `RANGE`, `VOLATILE`, `EVENT` or `UNKNOWN`, with the reasons in plain words, the FII next-day bias
  (`BULLISH` / `BEARISH` / `NEUTRAL` / `UNKNOWN`) and a history of changes. It refreshes every 15 seconds.

## Run it (Windows, Python 3.11+)
```
cd AI_Trading_System
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q          # should say 296 passed
python -m app.main           # then open http://127.0.0.1:8000
```
It starts in `sim` mode with moving fake prices and **fake history** (generated, stored separately
from real data), so the regime badge works straight away. Sim results mean nothing about the real market.

## How the regime is decided (rules `regime_v1`, thresholds in `config.yaml` -> `regime`)
1. **UNKNOWN** if anything needed is missing or not LIVE: NIFTY or India VIX price, NIFTY day high/low,
   enough daily history (50 days for NIFTY, 200 for VIX), history that is up to date, breadth for at
   least 80% of the watchlist stocks, or a readable event calendar. The system never guesses.
2. **EVENT** if today has a market-wide event of a type in `regime.event_types` (EXPIRY, RBI_POLICY,
   BUDGET, MARKET_EVENT). A stock's own RESULTS row does not change the market regime.
3. **VOLATILE** if India VIX is above its 80th percentile of the last year, or NIFTY's day range is
   more than 1.5x its 14-day ATR.
4. **TRENDING_UP / TRENDING_DOWN** if NIFTY is above / below both its 20- and 50-day averages and
   breadth agrees (60% or more / 40% or less of watchlist stocks above their 20-day average).
5. **RANGE** otherwise.

Averages, ATR and percentiles use only completed days before today. Outside market hours the badge
is based on the last close and is labelled for the next trading session ("basis LAST_CLOSE").
Each result carries a rules version such as `regime_v1/dcbbd0b4`; the suffix changes when you change a threshold.

## Your data files (in `data/`)
| File | What to put in it | How |
|---|---|---|
| `holidays.csv` | NSE trading holidays for the year (`date,note`) | Paste NSE's list once a year. Holidays show `CLOSED` and are skipped when finding "the previous session" |
| `events.csv` | Expiry days, RBI policy, Budget, results (`date,type,note,symbol`) | Leave `symbol` blank for market-wide events |
| `fii_dii.csv` | Daily FII/DII cash figures in Rs crore (`date,fii_net_cr,dii_net_cr`) | Paste daily from NSE |

The files start empty: **no dates are filled in for you.** Until you fill in holidays, a holiday is treated as a missing day,
so the regime shows UNKNOWN with a note telling you to add it.

Paste data on the dashboard ("Paste FII/DII figures, events or holidays"), or edit the CSV files and restart.
- FII/DII accepts our layout or the NSE table copied as CSV (`Category,Date,Buy Value,Sell Value,Net Value`,
  with one FII/FPI row and one DII row per day). New days are **added** to the file.
- Events and holidays: the pasted list **replaces** the file, so paste the full list.
- If any row is invalid, nothing is saved and the errors are shown. An invalid file on disk is not loaded:
  the last good copy stays in use, the page shows a red warning, and the regime is UNKNOWN with the file and row named
  until you fix it. If `fii_dii.csv` itself is broken, fix it in a text editor and restart, because new days cannot be merged into it.
- The import route only accepts JSON (`{"text": "<csv>"}`) sent from this machine (127.0.0.1 or localhost), so other websites cannot post to it.

**FII bias:** BEARISH after 3 straight sessions of FII net selling, BULLISH after 3 of net buying, otherwise
NEUTRAL. It is UNKNOWN if any of the last 3 sessions' figures are missing.

## Switch to live ICICI data
1. Create an app at https://api.icicidirect.com/apiuser/home to get an API key and secret.
2. Copy `.env.example` to `.env` and fill in the key and secret.
3. Each morning, log in at `https://api.icicidirect.com/apiuser/login?api_key=YOUR_KEY`,
   copy the session token from the address bar into `.env` (`BREEZE_SESSION_TOKEN`).
4. In `config.yaml` set `feed.mode: breeze`, then run `python -m app.main`.
   In breeze mode the app downloads daily (400 days) and 5-minute (10 days) history in the background at start.
   You can also run the download on its own: `python -m app.history`.
   Run these from the project folder: the Breeze SDK writes its own log files to `logs/` in the current folder
   (this cannot be changed in the SDK). `logs/` is git-ignored.

Outside 09:15-15:30 IST on weekdays, and on holidays in `holidays.csv`, symbols show `CLOSED`.

## Watchlist: NIFTY, BANKNIFTY, INDIAVIX and the Nifty 50 stocks (53 symbols)
ICICI uses its own short codes (`RELIND` for Reliance, `CNXBAN` for Bank Nifty). The three index codes were
verified by you on 2026-11-11. The 50 stock lines in `config.yaml` (between the `nifty50` marker comments)
are generated from `data/nifty50.csv`. Each one has a `sector` taken from the file's Industry column.

**Refresh after an index change.** Download the new constituents file from niftyindices.com, save it as
`data/nifty50.csv`, then run:
```
python scripts/update_watchlist.py --dry-run   # shows mapped / not mapped / added / removed, writes nothing
python scripts/update_watchlist.py             # rewrites only the nifty50 block in config.yaml
python -m app.history                          # downloads history for new symbols; stored ones are skipped
```
The script maps each NSE symbol to its Breeze code with ICICI's own instrument list (the SecurityMaster file
that breeze-connect downloads; no login needed). It needs the series-EQ row for that symbol, and the ISIN must match the file.
Symbols that cannot be mapped are listed and left out; codes are never guessed. If a symbol you already had gets a different
code, it prints a WARNING. The new config is validated before it is written. The index lines are not touched.
Check a single code by hand with `python scripts/lookup_code.py RELIANCE` (needs your login).

**Market breadth** in the regime uses every stock in the watchlist, so all 50 now. The thresholds are unchanged:
60% or more above their 20-day average for TRENDING_UP, 40% or less for TRENDING_DOWN, and usable data for at least 80% of the
stocks (40 of 50), otherwise UNKNOWN.

**Live feed start.** In breeze mode the app subscribes the 53 symbols one by one, pausing `feed.subscribe_pause_seconds`
(0.25 s) between them. It then logs `Breeze live feed: subscribed N of 53 symbols` and, if any failed, a line naming each
one and its code. A failed symbol shows `NO_DATA`; the others carry on.

**History download pacing** (`config.yaml` -> `history`): a pause of `request_pause_seconds` (0.7 s) between Breeze requests,
up to `empty_retries` (2) more tries `retry_pause_seconds` (3 s) apart when a reply is empty, and no request at all for a symbol
whose newest complete bar is already stored. `python -m app.history` ends with a summary
(bars saved, skipped, failed, empty). `python -m app.history RELIANCE TCS` downloads only those symbols.

## Phase 3: strategies and backtests
Five rule-based strategies, each in `app/strategies/` with its thresholds in `config.yaml` -> `strategies`:

| Strategy | Bars | Regimes | Idea |
|---|---|---|---|
| `orb` | 5-minute | TRENDING_UP (long), TRENDING_DOWN (short) | First close beyond the 09:15-09:30 range, with volume; stop at the other side |
| `vwap_pullback` | 5-minute | TRENDING_UP (long), TRENDING_DOWN (short) | Pullback to VWAP after 6 bars on the trend side |
| `range_reversion` | 5-minute | RANGE | Close back inside the 20-bar 2-sd band; target the average |
| `swing_pullback` | daily | TRENDING_UP | Up day after a pullback to the 20-day average, above the 50-day; long only |
| `swing_breakout` | daily | TRENDING_UP | Close above the 20-day high on 1.5x volume; long only |

A strategy gives no signal outside its regimes, for an index, or when the price is not LIVE. Each has an id and a version;
the version changes when you change any of its thresholds.

**Run the backtests** (after `python -m app.history` has downloaded data):
```
python -m app.backtest                       # all five, on your real Breeze data (data/market_live.db)
python -m app.backtest --strategy orb        # one strategy
python -m app.backtest --symbols RELIANCE TCS
python -m app.backtest --source sim          # on fake data (labelled SIMULATED)
python -m app.backtest --holdout             # also opens the final holdout - see below
```
Then open http://127.0.0.1:8000/backtests (link "Backtest reports" at the top of the main page). Reports are also saved as
JSON in `data/backtests/` (`<strategy>_latest.json` plus one file per run).

How the backtest works:
- Entry at the **next** bar's open after the signal bar closed, plus slippage. The signal bar's close is never a fill.
- If one bar touches both stop and target, the **stop** is assumed hit first. A bar that opens beyond the stop fills at its open.
- Intraday trades close at 15:15 IST; no new entries after 14:45. Swing trades exit at stop, target, or after 10 sessions.
- Size: 1% of capital at risk to the stop, capped at 25% of capital per position. Each trade is sized on its own: there are
  no portfolio limits yet (phase 5), so the report shows **peak exposure**, which can be several times your capital.
- The market regime at each moment is recomputed from data available at that moment only.
- **Walk-forward:** nothing is fitted. The last 20% of sessions is a **holdout** that is not simulated at all until you run
  with `--holdout`. Do that once, when you have finished changing a strategy; after that the holdout is no longer untouched.
  The rest is reported overall and in ~1-month windows.
- Each report: period, trades, win rate, average win and loss, net P&L after costs, costs, max drawdown, profit factor,
  results by regime and by window. Anything under 30 trades is marked **LOW SAMPLE**.

**Costs** (`config.yaml` -> `costs`) are your ICICI Direct **Prime 4999** brokerage (delivery 0.10%, intraday 0.010%,
options Rs 19 per lot, each side) plus statutory charges, each with its source and effective date. NSE charges follow NSE
circular NSE/FA/73061 (effective 2026-03-01): total NSE outflow per side, IPFT included, is Rs 307 per crore for equity cash
and Rs 3,553 per crore of premium for equity options, so the IPFT rate is 0 (it is inside the exchange charge, not counted
twice). They are **standard rates, not yet verified against a contract note**, and every report says so.
To verify: take one real contract note and compare brokerage, STT, transaction charges, SEBI fee, stamp duty and GST line by line;
fix any rate, then set `verified_against_contract_note: true`. No rate is empty now, so option backtests (phase 4) can run.

**Unadjusted history.** Breeze daily history is not adjusted for splits, bonuses or demergers (for example RELIANCE "falls 50%"
on 2024-10-28, its 1:1 bonus). An overnight gap of more than 25% (`history.max_overnight_gap_pct`) is treated as a break:
averages, ATR, breakouts and the regime's breadth only use bars after it, and a swing trade open across one is dropped.
`python -m app.history` lists the breaks it finds; each report lists them under `data_breaks`.

## Phase 3b: strategy revision (v2 versions)
v1 strategies are kept unchanged for comparison; the new versions have their own ids. Results and the comparison with
random entries are in `docs/STATUS.md` (phase 3b). In short: **no version is profitable after costs yet, and none beats
random entries**; nothing has been tested on the holdout.

| Strategy | What changed from v1 |
|---|---|
| `orb_v2` | 30-minute opening range; volume above 1.5x the same time-of-day bar's 20-session average; stop at the other side of the range; skipped if the stop is closer than 5x the round-trip cost |
| `range_reversion_v2` | Same entry; stop at the 3-bar extreme or 5x round-trip cost, whichever is further; target the band average; skipped if the average is closer than 2x the round-trip cost |
| `swing_pullback_v2`, `swing_breakout_v2` | Only while NIFTY closes above its 50-day average (nearly always true already in a TRENDING_UP regime) |
| `vwap_pullback` | Retired from cash trading: listed in `strategies.inactive`, so not run by default (`--strategy vwap_pullback` still works) |

- **Round-trip cost estimate** (rule 1): charges for an intraday round trip at the position cap plus slippage on both sides,
  computed from `config.yaml` costs (0.159% of price with Prime 4999; it was 0.187% with Prime 999). It is part of each v2 version, so changing a rate
  changes the version.
- **Fixed holdout**: `backtest.holdout_start` - 5-minute strategies from 2026-10-05, daily strategies from 2026-06-08.
  Downloading more history never moves the holdout back over data already used.
- **Swing test period starts 2019-01-22** (`backtest.test_start`): Breeze's daily history goes back to 2014, but before
  2019-01-22 it has days at the wrong price scale and VIX values about 100x off. The older bars stay stored.
- Backtests load `holidays.csv`, `events.csv` and `fii_dii.csv` before running, as the app does.
- History requests time out after 60 s (`history.request_timeout_seconds`); the symbol is reported failed and a rerun resumes.
- **Diagnostics** (rerun after any strategy change; development period only):
  ```
  python scripts/diagnostics/diag.py --strategy orb_v2 range_reversion_v2   # random baseline, 10 sample trades, checks
  python scripts/diagnostics/fwd.py --strategy orb_v2                       # raw forward returns vs a baseline
  ```
  Output goes to `data/diagnostics/`.

## Status values
| Status | Meaning |
|---|---|
| LIVE | Fresh, passed all checks; usable by strategies |
| STALE | No good tick for `stale_after_seconds`; strategies must say NO TRADE |
| NO_DATA | Nothing received yet |
| CLOSED | Outside market hours or an NSE holiday |

## Files
- `app/feeds/breeze_feed.py` - the only file that touches the broker SDK (data calls only)
- `app/quality.py` - quality rules and market hours
- `app/hub.py` - tick -> quality gate -> bars -> price table
- `app/calendar.py` - trading days and holidays
- `app/history.py` - daily and 5-minute history download (Breeze or sim), paced, retries empty replies, skips stored data
- `app/models.py` - Tick, Bar, and the Signal contract with its schema check
- `app/strategies/` - `base.py` (Strategy, MarketContext) and the five strategies
- `app/costs.py` - ICICI cost model (Decimal, to the paise); empty rates stop a backtest and are named
- `app/backtest.py` - backtest engine, walk-forward split, reports (`python -m app.backtest`)
- `app/watchlist.py` - Nifty 50 file -> Breeze codes and sectors -> the `nifty50` block of config.yaml (pure functions)
- `scripts/update_watchlist.py` - refresh the Nifty 50 watchlist from a new `data/nifty50.csv`
- `app/indicators.py` - SMA, EMA, ATR, daily return, percentile, breadth (pure functions)
- `app/regime.py` - regime rules, FII bias, and the service that feeds them
- `app/manual_data.py` - events, holidays and FII/DII CSV loading and import
- `app/main.py` - web server and API: `/health`, `/api/prices`, `/api/quality`, `/api/bars/{symbol}`,
  `/api/config`, `/api/regime`, and `POST /data/import/{fii_dii|events|holidays}` (writes only your data files;
  JSON body, local Host/Origin only), `/api/backtests`, `/api/backtests/{run_id}` and the page `/backtests`
- `tests/test_safety.py` - fails if order code, order routes, write routes outside
  `/journal/`, `/data/import/`, `/capital/`, or a public host appear

## Not yet tested
- **No strategy has an edge yet.** Every v1 and v2 version loses after costs on the development period and none is
  better than random entries (docs/STATUS.md, phase 3b). The holdout has not been opened.
- Breeze daily history before 2019-01-22 has price errors; swing tests skip it. Holidays before 2026 are not in
  `holidays.csv`, so sessions after those holidays get an UNKNOWN regime in swing tests.
- **Costs against a real contract note.** The rates are published standard rates, not checked against your fills.
  Which legs ICICI charges Prime brokerage on (assumed: both legs, intraday and options) is not stated on its page.
- Corporate actions: no adjustment factors are applied; history across a big overnight gap is simply not used.
- **Live subscriptions for all 53 symbols.** The paced subscription and the startup summary are tested against a fake SDK only.
  When the 50 stocks were added, an app was already running on port 8000, so a second live Breeze connection was not opened.
  On your next start in breeze mode, look for `subscribed 53 of 53 symbols` in the console.
- The tick's day `high`/`low` fields are assumed. Live history works: the 50 new stocks were downloaded on 2026-11-12,
  with no failures and no empty replies.
