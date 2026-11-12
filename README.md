# Trading Research System - Phases 1 and 2: data feed, quality gate, market regime

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
python -m pytest -q          # should say 212 passed
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
- `app/watchlist.py` - Nifty 50 file -> Breeze codes and sectors -> the `nifty50` block of config.yaml (pure functions)
- `scripts/update_watchlist.py` - refresh the Nifty 50 watchlist from a new `data/nifty50.csv`
- `app/indicators.py` - SMA, EMA, ATR, daily return, percentile, breadth (pure functions)
- `app/regime.py` - regime rules, FII bias, and the service that feeds them
- `app/manual_data.py` - events, holidays and FII/DII CSV loading and import
- `app/main.py` - web server and API: `/health`, `/api/prices`, `/api/quality`, `/api/bars/{symbol}`,
  `/api/config`, `/api/regime`, and `POST /data/import/{fii_dii|events|holidays}` (writes only your data files;
  JSON body, local Host/Origin only)
- `tests/test_safety.py` - fails if order code, order routes, write routes outside
  `/journal/`, `/data/import/`, `/capital/`, or a public host appear

## Not yet tested
- **Live subscriptions for all 53 symbols.** The paced subscription and the startup summary are tested against a fake SDK only.
  When the 50 stocks were added, an app was already running on port 8000, so a second live Breeze connection was not opened.
  On your next start in breeze mode, look for `subscribed 53 of 53 symbols` in the console.
- The tick's day `high`/`low` fields are assumed. Live history works: the 50 new stocks were downloaded on 2026-11-12,
  with no failures and no empty replies.
