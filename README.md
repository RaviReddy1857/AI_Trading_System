# Trading Research System - Phases 1-5 and the core of phase 9: data feed, quality gate, market regime, strategies, backtests, options, risk engine and stock option selling

Research and suggestions only. **This app cannot place, change or close orders**,
and a test fails the build if such code is ever added. You trade manually in ICICI Direct.

## What you get
- Live price table at http://127.0.0.1:8000 (local machine only)
- Two feed modes: `sim` (fake prices, no login needed) and `breeze` (live ICICI data, read-only)
- Quality gate: bad prices, crossed bid/ask, sudden jumps and stale data are quarantined
- 1-minute bars and history saved to SQLite: `data/market_sim.db` in sim mode, `data/market_live.db` in breeze mode
  (chosen from `feed.mode`; the live file refuses simulated rows). The old `data/market.db` held only sim data and is no longer used.
- `config.yaml`: capital and every risk limit as a % of capital
- `config.yaml` -> `stock_option_selling.settlement.broker`: ICICI Direct's physical-settlement rules (delivery margin,
  auto close, fresh-position block, obligation), each with `confirmed`; set a flag to `true` only after confirming the
  rule with ICICI (sources in docs/STATUS.md)
- **Phase 2:** a "Today's market type" badge at the top of the page: `TRENDING_UP`, `TRENDING_DOWN`,
  `RANGE`, `VOLATILE`, `EVENT` or `UNKNOWN`, with the reasons in plain words, the FII next-day bias
  (`BULLISH` / `BEARISH` / `NEUTRAL` / `UNKNOWN`) and a history of changes. It refreshes every 15 seconds.

## Run it (Windows, Python 3.11+)
```
cd AI_Trading_System
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q          # should say 1,174 passed, whatever the date or time
python -m app.main           # then open http://127.0.0.1:8000
```
The tests never depend on the machine's clock: a test that reads the date or time uses the shared `fixed_clock`
fixture (Mon 16-Nov-2026 10:30 IST, `tests/clock.py`). To check, run the suite as if the clock read another time:
`python -m pytest -q --fake-clock=2026-11-21T23:59+05:30`; `--clock-report=<file>` lists any test that read the clock
without the fixed clock (it should be empty).
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

### Data status page, inbox importers and the one-time external import
**Data status** (`/data-status`, and a green / amber / red strip at the top of the main page): one row per data source
with when it was last updated, its coverage, its status (OK, STALE, MISSING, PARTIAL or INFO), what it blocks, and exactly
what to do. A STALE, MISSING or PARTIAL source is never shown as OK. Rows dated after today are never used to judge
freshness: a source holding any is shown as DATA_ERROR (red) with the rows listed and its status without them. The page
ends with the daily and weekly routine.
The Breeze session is confirmed only by data that arrived today (live prices or a download); the token in `.env` is
never read. Times are shown in IST ("15 Nov 2026, 10:30 AM"). The option-history row shows today's actual
request budget: `python -m app.options.stock_backtest --download --day-budget 4500` raises it for today only
(recorded in the store; `config.yaml` keeps its default).

**Inbox** (`data/inbox/`, gitignored): put the files you download by hand there and press **Import inbox files** on the
Data status page (or run `python -m app.inbox`). Processed files move to `data/inbox/done/`, rejected ones to
`data/inbox/rejected/` with a `.reason.txt`. Duplicates are skipped and no row you entered is ever replaced.
| File | Where | Columns read | What it adds |
|---|---|---|---|
| NSE board meetings | nseindia.com > Companies listing > Corporate filings > Board meetings: a date range from today through at least the next monthly expiry, **no symbol, index or purpose filter**, Download (.csv) | `SYMBOL`, `PURPOSE`, a meeting-date column (`MEETING DATE` / `BOARD MEETING DATE`), optional `DETAILS` | RESULTS events (purpose mentions results); a COVERAGE row for every watchlist stock through the END date you declare (see below) |
| NSE corporate actions | nseindia.com > Companies listing > Corporate filings > Corporate actions (Equity), Download (.csv) | `SYMBOL`, `PURPOSE`, `EX-DATE`, optional `SERIES`, `RECORD DATE` | splits, bonuses, demergers, rights, dividends (table `corporate_actions`, source `nse_inbox`) |
| NSE FII/DII activity | nseindia.com > Reports > FII/DII trading activity, Download (.csv) | `Category`, `Date`, `Buy Value`, `Sell Value`, `Net Value` (or `date,fii_net_cr,dii_net_cr`) | new days in `fii_dii.csv` |
| RBI decisions | rbi.org.in (monetary policy statements) | `date`, `decision` (`scheduled` for a planned meeting) | RBI_POLICY rows in `events.csv` (or use the RBI form on the page) |

**Board meetings need your declaration.** A filtered list proves nothing about the stocks it leaves out, so a
board-meetings file is imported only when you give the export's date range and confirm that it is the unfiltered
all-companies list: on the Data status page fill in *from* and *through* and tick the confirmation, or run
`python -m app.inbox --board-from 2026-11-16 --board-through 2026-12-31 --all-companies`. The file is then rejected
(moved to `rejected/` with the reason) when it lists fewer than `data_status.board_meetings_min_symbols` companies
(default 100), has a meeting date outside the declared range, or has no meeting within
`data_status.board_meetings_edge_days` (default 7) of either end. Coverage runs through the declared end date.

**Corporate-action adjustment:** splits, bonuses and demergers adjust the daily history used for the trend, 52-week low,
range and profile (`app/corporate_actions.py`), point in time (an action counts from its announcement date if known,
else its ex-date) and only where the raw Breeze prices show the gap. Raw prices are never changed; option strikes and
option prices are never adjusted.

**One-time import** from `aswinv90/indian-market-historical-data` (`python -m app.external_data`): **licence: personal,
non-commercial research use only, no redistribution - never use this data in anything published.** Clone it outside
the project (`../external_data/`; a sparse clone of `data/CORPORATE_ACTIONS`, `data/FLOWS`,
`data/MACRO/rbi_policy_rates.*` and the F&O stocks' `data/NSE/*_NS.parquet` is enough); only its data files are read,
none of its scripts is run. Imported rows are tagged `external_repo` with the commit and import date, kept in the SQLite
store and in the gitignored overlays `data/external/events_external.csv` and `fii_dii_external.csv` (merged on load; your
own rows win). Breeze data always comes first. Needs `pyarrow` (in requirements.txt).
Tracked files (docs, README) must never carry a value of the repo's rows: give factors, methods and aggregates, and keep
raw values in the gitignored `data/diagnostics/`. `python -m app.external_data --check-docs` lists any line that does
(a test runs the same check). The GitHub repo is private; its history still holds such values in older commits of
`docs/STATUS.md` and must be cleaned before the repo is ever made public (see docs/STATUS.md).

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

Outside market hours on weekdays, and on holidays in `holidays.csv`, symbols show `CLOSED`.

### Market hours: the closing auction from 3 Aug 2026 (`session` in config.yaml)
Hours are dated, per segment (`app/session.py`; live code and backtests read the same calendar):

| From | F&O stocks (cash) | Other stocks | Index values | Stock and index options |
|---|---|---|---|---|
| before 3 Aug 2026 | 09:15-15:30 | 09:15-15:30 | 09:15-15:30 | 09:15-15:30 |
| 3 Aug 2026 | 09:15-15:15, then closing auction 15:15-15:35 (official close) | 09:15-15:30 | indicative 15:15-15:35 | 09:15-15:40 |

- During the auction an F&O stock's ticks are auction indications (the index: indicative values), shown as **AUCTION**
  on the dashboard. They are never a signal, a paper fill, an exit trigger, a bar or a quality event (they no longer
  trip the kill switch). No new signal of any kind is made after 15:15; exit calls and paper exits for options carry
  on until 15:40.
- Cash intraday: from 3 Aug 2026 no new entry from, and out by, **15:00** (the `cash_intraday_cutoff` of the
  3-Aug period in `session.periods`; before 3 Aug the configured 15:15 exit applies). ICICI Direct's own
  intraday square-off time after the auction is **not confirmed**: confirm it and set
  `cash_intraday_cutoff_confirmed: true` (the exit call says "not confirmed" until then).
- Every Nifty 50 stock is an F&O stock; give a watchlist line `fno: false` for a cash-only stock.
- Stock option spreads: on the forced-exit day (E-5) the exit is due by **15:00**, while the stock still trades
  continuously (`stock_option_selling.settlement.forced_exit_by`); the red, URGENT overdue alert starts at the
  options' close, 15:40.
- Telegram: the 15:45 end-of-day summary covers signals, paper P&L and positions; the stock option shortlist
  comes as its own message when the after-close job completes (or at 17:30 if it has not).
- When NSE changes the hours again, add a dated period under `session.periods`.

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
- No trade if the entry fill **after slippage** (or the raw open) is at or beyond the stop or the target; the report counts
  these as `FILL_BEYOND_STOP_OR_TARGET` / `GAPPED_PAST_STOP_OR_TARGET` under skipped signals.
- An intraday session is traded only if it has **every 5-minute bar from 09:15 to the 15:10 bar** (the bar that ends at the
  15:15 exit; Breeze's 15:15 and 15:20 bars are not needed). A trade that would cross a missing bar is dropped, never closed
  at an invented end-of-day price. The report's `data_exclusions` (and the backtests page) show how many symbol-sessions
  and trades this removed.
- ORB `range_minutes` must be a multiple of 5 (whole 5-minute bars); any other value stops config loading with an error.
- Intraday trades close at 15:15 IST; no new entries after 14:45. Swing trades exit at stop, target, or after 10 sessions.
  From 3 Aug 2026 no trade enters or exits on an F&O stock's bar inside the closing auction (15:15-15:35); with these
  times no stored result changes (docs/STATUS.md has the proof). The live paper trades and exit calls use the earlier
  15:00 intraday cutoff, so live intraday exits are 15 minutes earlier than the backtest's.
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
random entries** (rerun at Prime 4999 rates on 2026-11-13: still true); nothing has been tested on the holdout.

**Strategy status** (`config.yaml` -> `strategies.status`): every phase 3 cash strategy, v1 and v2, is set to
**no alerts, paper only**, labelled **"negative in backtests"**. The label shows on the backtests page and in each report.
Phase 6 alerts will only use strategies with `alerts: true`; none has it. Change an entry only after a strategy shows an
edge after costs and against random entries.

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

## Phase 4: options
Open http://127.0.0.1:8000/options ("Options view" at the top of the main page). Pick an underlying to see:
- **Lot size and expiries**, read from ICICI's instrument list (never typed into the code or the config).
- **Option chain** near the money: bid, ask, open interest and its change, and **implied volatility and delta computed by
  code** (Black-Scholes on the bid/ask mid price; the 6.5% risk-free rate in `config.yaml` -> `options` is an assumption).
- **Put-call ratio, support and resistance** (the highest-OI put and call strikes) and **IV rank** against the past year
  (India VIX for NIFTY and BANKNIFTY; stored at-the-money implied volatility for a stock).
- **Option ideas** from three strategies, each either a SIGNAL with every figure, or NO TRADE with the reason:

| Strategy | Where | Rules in short |
|---|---|---|
| `long_option` | NIFTY, BANKNIFTY, stocks | Buy a near-the-money call/put when an underlying strategy signals; only when IV rank is 60 or lower; stop and target as % of premium. **Stock options are buy-only** and carry "exit before expiry week" (physical settlement) |
| `credit_spread` | NIFTY, BANKNIFTY monthly | TRENDING_UP: bull put spread; TRENDING_DOWN: bear call spread. Only when IV rank is 50 or higher; sold strike at delta 0.20 or less and beyond the highest-OI strike; bought strike about 1% further out; 20-35 days to expiry |
| `iron_condor` | NIFTY weekly | RANGE regime only (never an EVENT day); both sides at delta 0.15 or less, each hedged; 3-7 days to expiry |

A signal shows premium (or net credit), lot size, total cost, **max loss in rupees and as % of capital**, max profit,
breakeven(s), estimated costs and the margin blocked, all for **one lot** (the risk engine in phase 5 decides the quantity).
There is **no signal** when the price is not LIVE, the option chain is older than 15 minutes, a strike has no bid/ask, the
lot size is not in the instrument list, or the margin is not known. A sold option without its bought hedge is always
rejected (`UNHEDGED_SHORT`), and selling stock options returns NO TRADE in this phase.
`long_option` shows `NO_TRIGGER` until the live signal loop exists (phase 6). All three are set to **no alerts, paper only**.

**Margin** is not fetched from ICICI (its margin calculator is not one of the read-only data calls this app allows itself).
Copy the margin for ONE lot of the spread from ICICI Direct's margin calculator and paste it on the Options page, or keep
it in `data/margins.csv`:
```
underlying,structure,width,margin_per_lot_inr,as_of
NIFTY,CREDIT_SPREAD,250,38500,2026-11-13
```
`structure` is `CREDIT_SPREAD` or `IRON_CONDOR`; `width` is the distance between the sold and bought strike (the NO TRADE
reason names the width it needs). An entry older than 7 days counts as unknown. Do not use the example figure: it is made up.

**Chains.** In breeze mode the NIFTY and BANKNIFTY chains reload every 5 minutes during market hours. Stock chains load on request:
```
python -m app.options.chain                # NIFTY, BANKNIFTY now
python -m app.options.chain RELIANCE TCS   # these stocks
```
Rows with a missing or crossed bid/ask are quarantined (listed under Data quality as `BAD_OPTION_QUOTE`) and never used.
In sim mode the chain, lot sizes and expiries are FAKE (built from the simulated price) and labelled so.

**Historical option prices: Breeze has them** (checked 2026-11-13 with `python scripts/check_option_history.py`): single
contracts including expired ones, NIFTY, BANKNIFTY and stocks, back to at least 2017, as daily, 5-minute and 1-minute bars
with volume and open interest. They are **traded prices only: there is no bid/ask in history**. So option backtests on your
real data use these stored prices; only `--source sim` uses model prices and is then labelled **"payoff simulation"**.
Every backtest trade records its price source.

**Option backtests**, each beside a **no-filter baseline** (the same structure, strike rule, width, expiry choice, exits and
costs, entered on a fixed schedule with no entry rules: every monthly expiry for spreads, every weekly expiry for the
condor, the first session of each month for option buying; a credit spread has a bull-put and a bear-call baseline):
```
python -m app.options.backtest --download            # first time: fetches the option contracts it needs from Breeze (read-only)
python -m app.options.backtest                       # later runs: stored prices only, no login needed
python -m app.options.backtest --strategy credit_spread --underlyings NIFTY
python -m app.options.backtest --source sim          # fake data, labelled SIMULATED and "payoff simulation"
python -m app.options.option_history                 # what option history is stored
python -m app.options.option_history --iv-history RELIANCE TCS   # at-the-money history so a stock gets an IV rank
```
Results show on http://127.0.0.1:8000/backtests with the price source, the baseline rows and a verdict: a strategy that
does not beat its baseline per trade after costs (or loses money) is marked **"no edge vs baseline"**. Every option
report is also labelled **"excludes margin and bid/ask eligibility checks"**: history has no bid/ask and past margins are
unknown, so backtests cannot apply those two rules. Both stay mandatory for live signals.
How these backtests work:
- **The baseline makes one attempt per expiry (or month) on a date fixed by the calendar**: for spreads and condors the
  first market session on which the expiry is inside the entry window, for option buying the first market session of the
  month. If it cannot enter that day (no valid strike, no price, no fill at the next open) the attempt is recorded as
  skipped with its reason and is never retried. Baseline positions of different expiries may overlap.
- The 31 index contracts the fixed schedule wanted were downloaded on 2026-11-14 and the index backtests rerun
  (docs/STATUS.md, issue 54). A report says INCOMPLETE whenever contracts it wanted are not stored.
- A signal comes from a day's close; every leg is entered at the **next session's open**, with 0.5% slippage per leg.
- Spreads are judged on closing prices (cost to buy back): target at 50% of the credit kept, stop at 2x the credit, and out
  before expiry. The exit is filled at the next session's open, so a stop can fill well past 2x. Nothing is held to settlement.
- A bought option uses its own daily high/low (stop first), with a time exit after 10 sessions.
- **One lot per trade at today's lot size** (past lot sizes are not in the instrument list). At Rs 2,00,000 capital one lot
  of most index spreads risks more than the 2.5% per-option limit, so phase 5 would size many of them to zero lots.
- `--download` asks Breeze once per contract and remembers the answer, also when nothing came back; `--max-requests` (default
  1500) limits one run and a later run resumes. A report says INCOMPLETE if contracts it wanted are not stored.
- A trade is dropped, never given an invented price, if a leg has no bar for a session while it is open; an entry is skipped
  if a leg has no bar that day, if the next day is not a market session, or if the opening credit is already outside the
  stop/target band. The report counts each.
- In backtests the highest-OI rule and the margin rule are not applied (history has open interest per contract only, and
  past margins are unknown). The IV rank of a stock uses the stored at-the-money calls, point in time: a call is used
  only from the day its strike was chosen, so a day's IV never depends on a later close.
- **A sold option without its hedge is refused everywhere**: the signal cannot be constructed, the backtest will not enter
  it and a backtest run containing one is not saved (`UNHEDGED_SHORT`). The hedge must be a bought option of the same
  underlying, right and expiry, further out of the money, with at least as many shares.
- No session before 2019-01-22 is tested, and the holdout (from 2026-06-08) is not opened.

**Swing backtests (issue 42).** A daily-bar trade is now dropped if the stock has no daily bar for a session the index
traded while the trade was open (its stop or target may have been hit that day). On your data this removed no trade.

## Phase 9 core: stock option selling (monthly hedged spreads on stocks)

Research only, no alerts yet. Signal cards, Telegram alerts and the manual position form come after phase 6.

**See it:** `python -m app.main`, then http://127.0.0.1:8000/stock-option-selling : the daily shortlist, the reversal
watch list ("watch only, never a signal"), campaigns, and a per-stock profile with scenario history ("history, not a
forecast"). Each scenario shows its cases and how many trigger dates were not counted (before 2019-01-22, no stored
price near the date, the same starting day as another trigger, or its 30 days not over yet). Backtest results are on
http://127.0.0.1:8000/backtests. The reversal study there sets every confirmed reversal against the washed-out days
of the same stocks that had no confirmation (and against every day), measured the same way. A horizon is counted in
market sessions from `data/holidays.csv`; a window with a session the stock has no price for, or with an unadjusted
split or bonus inside it, is left out, and the printout of `python -m app.options.stock_backtest` shows, for every
group and horizon, the starting days, the completed cases and what was left out and why. Years that `holidays.csv`
has no holidays for use the NIFTY sessions instead (add the NSE holiday lists for earlier years to close that gap).

**Settlement readiness (phase 9 M3).** The same page has a "Settlement readiness" card (and
`GET /api/stock-option-selling/settlement`): ICICI's physical-settlement rules as you entered them in `config.yaml`
(`stock_option_selling.settlement.broker`), each marked confirmed or "ICICI rule not confirmed". The rules behind it
(`app/options/settlement.py`) give, for every open position with a sold stock option leg: NOTICE / WARNING from 7
trading days before expiry and URGENT from 5 (or when the price is unknown), the delivery obligation and expiry-week
margin estimate, and a red alert when the position is not marked closed by its exit deadline. Trading days are counted
with `data/holidays.csv`; unless a COVERAGE row (`COVERAGE through YYYY-MM-DD`, optionally followed by its source)
verifies the holiday list for the expiry month, the count is made one session earlier, the card says so in red, and
the deadline is marked "deadline unverified: holiday calendar missing; confirm before relying on it" on the alert, the
API and the Telegram text (one session earlier is not enough if two holidays are missing). A stock quote that arrives
but is rejected (crossed, or a last price that is not a finite number above 0) makes the alert URGENT at any distance
to expiry; no quote at all stays silent before E-7. In the delivery
estimate each bought share nets one sold share at most (the most exposed sold legs first); sold quantity without a
bought share in the money is shown as fully deliverable. They are alerts and
estimates only; nothing closes or changes a position. No position is recorded before phase 6, so no alert is shown or
sent yet. To do: confirm the auto-close time, the fresh-position block and the 11:00 shares deadline with ICICI and set
their `confirmed` flags. For a rupee margin figure the VaR + ELM + adhoc margin of the position must be entered (phase 6
form); without it the estimate shows the schedule's percentage and "margin unknown".

**Commands**
```
python -m app.options.stock_backtest --status      # liquidity ranking, today's request budget, what is completely downloaded
python -m app.options.stock_backtest --download    # fetch stock option history within the daily budget, then report
python -m app.options.stock_backtest               # report from stored prices only (no login needed)
python -m app.options.stock_backtest --recheck --no-report   # after an interrupted download: mark what is completely stored
python -m app.options.stock_backtest --recheck --verify-complete --no-report   # judge every complete mark again (no requests)
python -m app.options.stock_backtest --credit-distribution   # net credit / max loss of spreads passing every other rule (no outcomes)
python -m app.options.shortlist                    # after the close: load stock option chains, build the day's shortlist
python -m app.history                              # daily bars must be up to date for the shortlist, profile and watch list
```
- The download needs a fresh Breeze session in `.env`. It uses at most `options.backtest.daily_request_budget` (3,500)
  requests a day, the 30 most liquid stocks first, newest year first, and skips everything already stored. Run it once a
  day until `--status` shows every year complete; a Breeze error stops it cleanly and the next run carries on.
- A report covers only the stocks and years that are completely downloaded (`--all-data` shows everything, marked INCOMPLETE).
- Phase 9 backtests run at `stock_option_selling.backtest.capital_inr` (Rs 10,00,000); `account.capital_inr` is not changed.

**What a candidate needs (M1).** Whole lots within 2.5% of capital per position (rounded down; 0 lots = ZERO_SIZE);
support and resistance from confirmed 5-bar pivots; the falling-stock, market, per-stock loss and portfolio limits (6
spreads, 10% total risk, at most 2 positions and 5% risk per group of linked stocks: same sector or return correlation 0.7
or more). Every row on the page is a **research candidate** from the last session's close; an **actionable** signal also
needs market hours, a live price, a fresh chain with bid and ask, a current margin and your open positions known - so
nothing is actionable until positions are recorded (after phase 6).

**The score (M2).** Each candidate gets 0-100 points from six equal-weight factors - IV rank, IV falling from its 20-session
peak, relative strength against NIFTY and its sector, strike distance against the expected move and the 90th-percentile
21-session fall, the past breach rate at that distance, and liquidity - with each factor's contribution shown. It ranks
candidates; it is not a probability. The point rules are written in `app/options/score.py`.

**What the report shows:** the strategy beside its no-filter baseline (same spread on a fixed schedule), executed on
synchronized 5-minute bars of every leg (a campaign with a session lacking such a bar is left out as data-incomplete and
counted) and, for comparison, on daily closes; three exit policies on the same entries under both executions (stop, roll
by the rules, roll every month, the last one research only); positions still open at the end reported as open; monthly
candidates and trades; score bands (top, middle and bottom third of each day's candidates) and each score factor against
the baseline; what each rule and each alert does to the result; baseline trades split by what was known at
entry; and every confirmed reversal with its 10, 20 and 40-session outcome and both candidate trades.

**Files you fill for the scenario history** (until then those scenarios say "no data"; nothing is invented):
- `data/fii_dii.csv`: `date,fii_net_cr,dii_net_cr`, one row per session from 2019-01-22 (Rs crore, selling negative).
  Source: NSE "FII/DII trading activity" (recent days); for older days NSE's archive or NSDL's FPI daily reports
  (fpi.nsdl.co.in). Use one source throughout.
- `data/events.csv`: `date,type,note,symbol`. Add `RBI_POLICY` rows (symbol blank) for past rate-decision days, from the
  RBI website, and `RESULTS` rows with the stock's symbol for past and coming results days, from NSE corporate filings.
  `BOARD_MEETING` and `CORPORATE_ACTION` rows with a symbol also keep that stock off the shortlist until they pass.
- **Event coverage (needed before any stock candidate can become a signal):** one `COVERAGE` row per stock in
  `data/events.csv`, with three more columns in the header: `date,type,note,symbol,source,retrieved_at,covered_through`.
  Example: `,COVERAGE,,RELIANCE,NSE corporate filings,2026-11-13,2026-12-31` = "I checked RELIANCE's announcements at NSE
  on 13 November and they are known through 31 December". The row must be at most 7 days old and cover the option's
  expiry; otherwise the candidate shows `EVENT_COVERAGE_UNKNOWN` and gives no signal.

**Thresholds** are all in `config.yaml` under `stock_option_selling`, named as in section 2 of the Phase 9 specification; docs/implementation.md has the full table (value, unit, lookback, comparison, what happens when data is missing).
Reversal candidates stay silent until you set `stock_option_selling.reversal.alerts_approved: true`.

## Phase 5: risk engine (sizes every signal or says NO TRADE)

The risk engine (`app/risk/`) is the only part allowed to approve a signal. For each signal it returns **APPROVED**
with the quantity (cash) or lots (options), the rupee risk including charges and slippage, the estimated profit at the
target, the loss at the stop (spreads: the maximum loss), the costs, the cash or margin needed and a capital-fit label;
or **NO_TRADE** with every reason code that applies. All numbers are computed by code.

| Rule | Setting in `config.yaml` (`risk`) | At Rs 2,00,000 |
|---|---|---|
| Cash: shares = floor(capital x 1% / (entry - stop)), position value at most 25% of capital, charges included | `max_loss_per_trade_pct`, `backtest.position_cap_pct` | Rs 2,000 risk, Rs 50,000 position |
| Options: lots = floor(capital x 2.5% / max loss of one lot), cost-inclusive; 0 lots = ZERO_SIZE | `max_loss_per_option_pct` | Rs 5,000 |
| Index spreads: if one lot is above that limit (after any drawdown halving), narrower widths down to 100 points are tried and the widest that fits one lot is used; none fits = NO TRADE (the limit is never raised) | `strategies.credit_spread` / `iron_condor` `min_hedge_width_points` | - |
| Capital pool: 25% of capital stays uncommitted | `uncommitted_cash_pct` | Rs 1,50,000 usable |
| At most 2 open index selling spreads | `max_open_selling_spreads` | - |
| At most 2 open trades per sector (watchlist sector tags; NIFTY and BANKNIFTY are sector "Index") | `max_trades_per_sector` | - |
| Correlation of 60-session daily returns above 0.8: one position (their risk together within one budget) | `correlation_window`, `correlation_threshold`, `correlation_min_overlap` | - |
| Daily stop and monthly pause on realised + open P&L | `daily_loss_stop_pct`, `monthly_loss_pause_pct` | Rs 4,000 / Rs 12,000 |
| Drawdown from peak equity (deposits excluded): sizes halved at 10%, new signals paused at 15% | `drawdown_halve_pct`, `drawdown_pause_pct` | - |
| Kill switch: NIFTY not LIVE, outside market hours, or 20+ quarantined ticks in 5 minutes | `kill_switch` | - |

A limit is reached when the value equals it (exactly 2% lost today = stopped). Correlation needs to be strictly above
0.8. Change `account.capital_inr` and every rupee limit and size rescales.

**Checked on the live book, one approval per symbol (18-Nov-2026).** Every decision reads the real book again at that
moment: open positions plus every approved signal still awaiting your decision, from every strategy and version. The
"commitment" on a card is a snapshot taken when that signal was issued (informational only, never used for a later
approval). A signal whose underlying already has an approved signal awaiting your decision or an open position with
the same exposure - BULLISH (cash long, bought call, bull put or bull call spread), BEARISH (cash short, bought put,
bear call or bear put spread) or NEUTRAL (iron condor, strangle) - approved or opened in the same session (the IST day
of the approval, not of the signal's data: a morning swing signal is built on yesterday's close), is NO_TRADE with
`SAME_SYMBOL_ALREADY_APPROVED`; an exposure that cannot be classified is `EXPOSURE_UNKNOWN` (unknown never passes). The
default is on: `risk.block_same_symbol_same_session: true` in config.yaml; set it to false in config.local.yaml to
switch it off. A signal blocked ONLY by this rule is still paper-traded in its own strategy's paper book (flagged
`same_symbol_blocked`), so strategy versions are compared on the same signals; it reserves nothing in your real book.

**Unknown is never assumed.** Unknown capital, open positions, P&L, equity history, margin, lot size, cost rates,
sector, correlation or kill-switch input gives NO_TRADE with a code (`CAPITAL_UNKNOWN`, `PORTFOLIO_UNKNOWN`,
`MARGIN_UNKNOWN`, `LOT_SIZE_UNKNOWN`, `DATA_MISSING`, `CORRELATION_UNKNOWN`, `KILL_SWITCH`, ...).
Open positions, P&L and equity come from the phase 6 journal, per capital pool (below). Phase 5 itself issues no
signals and sends nothing.

Stock option spreads (phase 9) keep their own rules and their own capital: every rupee figure is a % of
`stock_option_selling.backtest.capital_inr` (Rs 10,00,000), not `account.capital_inr` - so their risk limit is
Rs 25,000 a position and their daily stop Rs 20,000. Capital BLOCKS your real book since 18-Nov-2026 (your policy;
advisory before): committing the 25% reserve is `RESERVE_BREACH`, margin above the capital `MARGIN_EXCEEDS_CAPITAL`
(with the shortfall); a signal blocked only by these is still paper-traded in its strategy's paper book. Size within `stock_option_selling.max_risk_per_position_pct` and never more lots than
the signal, the phase 9 portfolio limits; unknown data or margin, the kill switch, the loss stops and the drawdown pause
still block them.

See it: start the app and open http://127.0.0.1:8000 - the "Risk engine" card shows the kill switch, whether positions
are known, the usable pool, the position value cap and logged deposits. The JSON is at `GET /api/risk`.

**Deposits and withdrawals** are logged separately and never count as profit or loss (they are taken out of the
drawdown calculation). From the command line (Windows PowerShell, app running):
```
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/capital/log -ContentType application/json `
  -Body '{"date": "2026-11-16", "type": "DEPOSIT", "amount": 50000, "note": "top-up"}'
```

## Phase 6: live signals and alerts, paper mode

Research and suggestions only: nothing is ever sent to the broker. Every write below is a journal entry.

**Two capital pools.** Stock option selling (any position with a sold stock option leg) uses
`stock_option_selling.backtest.capital_inr` (Rs 10,00,000); everything else - intraday and swing stocks, index options,
stock option buying - uses `account.capital_inr` (Rs 2,00,000). Each pool has its own realised and open P&L and its own
equity history, so a loss in one pool never stops or halves the other. Deposits (`/capital/log`) belong to the cash and
index pool: they are part of its equity (capital + deposits since the journal began + realised + open P&L) but never
profit, so a deposit or withdrawal alone never moves the drawdown. Today's and this month's P&L of a position held
from an earlier day count from its previous closing mark, whether it is still open or closed. An approved paper trade
that has not filled yet already counts against the risk, capital, sector and correlation limits until it fills or
expires. See both pools at `GET /api/positions` (`pools`) and `GET /api/risk` (`pools`).

**Journal routes** (JSON body, from this computer only):
| What | Route | Body |
|---|---|---|
| I took / skipped a signal | `POST /journal/{signal_id}/decision` | `{"decision": "TOOK", "fill_price": 101.5, "note": "..."}` (options: `"fills": [price per leg]`; optional `"quantity"`/`"lots"`, `"fill_time"`). Refused after the signal's valid-until |
| A position I opened myself | `POST /journal/positions` | cash: `{"symbol", "trade_type": "INTRADAY_STOCKS"/"SWING_STOCKS", "direction": "LONG"/"SHORT", "quantity", "entry_price", "entry_date", "stop"?, "target"?}`; options: `{"symbol", "kind": "index"/"stock", "entry_date", "legs": [{"action", "right", "strike", "expiry", "lots", "lot_size", "price"}], "margin_inr"?, "stop"?, "target"?}` |
| I closed it (all or part) | `POST /journal/positions/{id}/close` | `{"exit_price": x}` or `{"exit_prices": [per leg]}`, optional `"quantity"`/`"lots"` |
| I moved my stop | `POST /journal/positions/{id}/stop` | `{"stop": x}` |
| Alerts for one trade type on/off | `POST /journal/alert-switches` | `{"type": "SWING_STOCKS", "on": true}` |

**The live loop (paper mode).** While the app runs in market hours, every closed 5-minute bar runs the strategies,
the risk engine sizes each signal on its own pool, and a card is stored (APPROVED, or NO TRADE with the reasons).
Every approved signal is paper-traded at the next bar's first live quote (the first quote after the bar close, kept
when it arrives, never a later tick) and followed to its stop, target or time exit; one not filled by its valid-until
is NOT_FILLED, even after a restart or a sleep. Open **http://127.0.0.1:8000/signals** for the cards ("I took this trade" / "I skipped it"), your open
positions with their exit calls ("I closed it", "I moved my stop"), the form for positions you opened yourself, the
alert switches and the slippage report (CSV: `/api/slippage.csv`). The score on a card is a ranking aid, never a
probability; the rupee estimate is labelled "Estimate if target is hit, not a forecast".

**Exit calls** (every trade type, taken or entered by you): target, stop, move stop to breakeven, trail the stop,
book partial profit, time exit, entry reason invalidated, regime flip, kill switch; stock option spreads also strike
breached, trend flip, forced exit before expiry week and the settlement-readiness alert. Each says what to do and
why, with the numbers, and repeats every 15 minutes (`alerts.exit_call_repeat_minutes`) until you mark the position
closed. Thresholds are in `exits` in config.yaml. A stale price pauses the price checks; it never triggers or
silences a call.

**Telegram alerts are OFF until you switch them on.** To switch on: put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
(your own chat with the bot) in `.env`, set `alerts.telegram_enabled: true` in `config.local.yaml` (your own
overrides of config.yaml, never committed; the tests ignore it) and restart. The app only
sends to that chat and never reads incoming messages. Then, per trade type, switch alerts on at `/signals` (or
`POST /journal/alert-switches`). A signal alert also needs its strategy's `strategies.status` entry to say
`alerts: true`; today every strategy says `false` (their backtests show no edge), so only exit calls, settlement
alerts, the kill switch, the daily stop, the pre-market plan (09:00) and the end-of-day summary (15:45) are sent.
Every alert and its delivery state is listed under "Alerts" on `/signals` (`GET /api/alerts`); a failed send is
retried and shown as "not delivered" if it expires first. If Telegram may have received a message but did not
confirm it (a timeout after sending), a signal, pre-market or end-of-day alert is not sent again (shown as UNCERTAIN);
an exit call or urgent alert is sent again, and these always end with "Alert key: ...", so you can tell a repeat
from a new alert. Alerts and the heartbeat run on their own threads, so a stuck signal loop still raises its alert.

**When something fails** the app stays safe and tells you: a stale or dropped feed gives no signals, a red banner
"feed stale since hh:mm", one heartbeat alert and an automatic reconnect; an expired Breeze login shows red on
`/signals` and the Data status page with one alert and the re-login steps; a rejected import keeps the last good data
and shows an amber "Rejected import" row; after a restart the banner says "restarted at hh:mm, data from hh:mm" and
open positions are checked on the next bar; after the laptop sleeps you get one "system was asleep from - to" alert,
prices count as stale until fresh ticks arrive, and expired signals are never sent.

A sold option without a bought option of the same right and expiry is stored and flagged **UNHEDGED** (its maximum
loss is shown as not known for a call, strike-based for a put). The six alert switches start OFF.

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
- `app/data_status.py`, `app/inbox.py`, `app/external_data.py`, `app/corporate_actions.py` - the Data status page, the
  inbox importers, the one-time external-repo import, and the point-in-time corporate-action adjustment of daily history
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
- `app/options/` - phase 4: `instruments.py` (lot sizes, expiries, strikes from ICICI's list), `chain.py` (option chains
  and their quality gate), `greeks.py` (Black-Scholes price, implied volatility, delta, IV rank), `payoff.py` (max loss,
  max profit, breakevens, hedge check), `margin.py` (your margin entries), `base.py` (option context and strike rules),
  `long_option.py`, `credit_spread.py`, `iron_condor.py`, `option_history.py` (stored Breeze option prices, expiry
  discovery), `backtest.py` (option backtests with the baseline), `analytics.py` (put-call ratio, highest-OI strikes, the Options page data)
- `scripts/check_option_history.py` - the check of what option history Breeze provides
- `app/options/sizing.py`, `portfolio.py`, `actionable.py`, `score.py` - phase 9 M1/M2: whole-lot sizing, portfolio and
  correlation limits, research versus actionable, the candidate score
- `app/options/settlement.py` - phase 9 M3: settlement-readiness alerts, delivery and margin estimate, exit deadline
- `app/risk/` - phase 5: `sizing.py` (shares and lots; reuses `app/backtest.py`'s formula and `app/options/sizing.py`),
  `portfolio.py` (positions, capital pool, capital fit, selling-spread count), `limits.py` (sector, correlation, loss
  stops, drawdown, kill switch), `engine.py` (`decide()` -> RiskDecision, and the live inputs for `/api/risk`)
- `app/options/stock_selling.py`, `stock_profile.py`, `campaign.py`, `reversal.py`, `shortlist.py`, `stock_backtest.py` -
  phase 9 core: stock option selling rules, rolls and alerts; profile, scenario history and blocks; campaign tracking;
  reversal detector and its two candidates; the shortlist and views; the download and the report
- `app/main.py` - web server and API: `/health`, `/api/prices`, `/api/quality`, `/api/bars/{symbol}`,
  `/api/config`, `/api/regime`, and `POST /data/import/{fii_dii|events|holidays}` (writes only your data files;
  JSON body, local Host/Origin only), `/api/backtests`, `/api/backtests/{run_id}` and the page `/backtests`, `/api/options`, `/api/options/{underlying}` and
  the page `/options`; `POST /data/import/margins` for your margin entries; `GET /api/risk` and
  `POST /capital/log` (phase 5)
- `app/engine.py`, `app/cards.py`, `app/scoring.py`, `app/track.py`, `app/paper.py`, `app/execution.py`,
  `app/positions.py` - phase 6: the live loop, signal cards, the score, track records, the paper trader, execution
  records and the slippage report, exit calls
- `app/recovery.py` - phase 6: heartbeat, feed reconnect, sleep detection, expired login, restart banner
- `app/telegram.py`, `app/alerts.py`, `app/schedule.py` - phase 6: the send-only Telegram client, the alert log
  and what is sent, the pre-market plan and end-of-day summary
- `app/journal.py`, `app/pricing.py`, `app/switches.py` - phase 6: signals, decisions, positions, paper trades and the
  account state of each capital pool; quotes with their status; the trade-type alert switches
- `tests/test_safety.py` - fails if order code, order routes, write routes outside
  `/journal/`, `/data/import/`, `/capital/`, or a public host appear

- Test time limit: every test fails after `TEST_TIMEOUT_SECONDS` (default 300) instead of hanging the
  suite (`tests/conftest.py`; a test may set `@pytest.mark.timeout(n)`).
- The suite never reads your `data/`: every `data/...` path points at a temporary copy of `tests/fixtures/data/`
  (frozen calendar CSVs, header-only external overlays), so your imports never change a test result.
- The suite never opens a database under `data/` (a test that tries fails with RealDataAccess), so running it cannot
  lock `data/market_live.db` while the app or a download writes. The audits of the real files are opt-in: run them only
  when nothing writes, with `TRADING_AUDIT_REAL_DATA=1` (PowerShell: `$env:TRADING_AUDIT_REAL_DATA="1"; python -m
  pytest -q tests/test_safety.py tests/test_review6_fixes.py`). `--data-access-report=FILE` lists other files a test
  read under `data/`.
- Several processes share the database file (the app, the option download, the after-close job): the store uses WAL
  and a 30-second busy timeout, so a second writer waits for the first instead of failing with "database is locked".

## Not yet tested
- **Daily shortlist** (docs/STATUS.md): the app captures the priority stocks' chains from 14:50 IST (live
  bid/ask, each chain stamped with its own fetch time) and stops at 15:15, the end of continuous trading (chains not
  fetched by then are reported MISSED); from 15:41, after every segment has closed, it updates the history and builds
  the shortlist for the chains' session only, waiting for Breeze's daily bar (the official closing-auction close)
  until 17:30 (the last attempt runs at 17:30 itself) - only then is a missing close built from 5-minute bars,
  labelled APPROXIMATE (about 226 Breeze calls
  a day). Tested with fake Breeze sources; not yet run live at 14:50.
- **Closing auction handling** (docs/STATUS.md): tested on fixtures only; not yet seen on a live day (AUCTION on the
  dashboard from 15:15, no crossed-quote events, option exits to 15:40, the session close waiting for the daily bar).
- **/stock-option-selling page** (docs/STATUS.md): the section-by-section loading is tested by running the page
  script in Node (skipped without Node), not in a real browser.
- **Two books** (docs/STATUS.md): the real book (my positions and approved signals awaiting my decision) and
  the paper books, one per strategy and pool (`paper.max_open_trades` in config.yaml), are tested on fixtures,
  not over a live day.
- **Event coverage wording** (docs/STATUS.md): the plan and the cards say "EVENT COVERAGE MISSING: results dates
  unknown" until every stock has a current COVERAGE row; tested on fixtures, not yet seen in a live pre-market plan.
- **Review 8 fixes** (docs/STATUS.md): first-quote captures across a restart are tested on a fixture hub, not on a
  restart of the live app; a late cash fill is caught up on stored 1-minute bars only (the minute still forming is
  checked later); the Telegram reply check is tested with fake replies.
- **Review 7 fixes** (docs/STATUS.md): the uncertain-delivery path is tested with a fake Telegram transport, not a real
  timeout; the separate alert and watchdog threads are tested with a blocked step, not over a market day.
- **Phase 6 live.** No real Telegram message has been sent (alerts are off until you switch them on); no full market
  day has run with live Breeze data; a real feed drop, the Breeze reconnect and the expired-login message are tested on
  a fake SDK only; market depth is not recorded. Every strategy's status has `alerts: false`, so no signal alert is sent
  until you change it in config.yaml.
- **Risk engine on real positions.** Phase 5 is tested on fixtures only. Open positions, P&L and the equity history come
  from the phase 6 journal, so no live signal has been sized for real yet (every live decision is NO_TRADE until then).
  The kill switch's quarantine spike threshold (20 in 5 minutes) is a starting value, not tuned on live ticks.
- **Stock option selling.** Only Jan-Jun 2026 of 9 stocks is downloaded, so every result is low sample. The shortlist
  command and the views have not been run against live Breeze chains. See docs/STATUS.md, "Phase 9 core".
- **Options, live.** The option chain loader, the 5-minute background refresh and live option signals have not been seen
  running in market hours (the code was written after the close on 2026-11-13); one manual chain request returned 60
  strikes per side with bid, ask and OI. Spread signals also need your margin entries.
- **Margin and max loss against ICICI.** Check one spread's max loss and margin by hand against ICICI's margin calculator.
- **Option backtest evidence is thin for the filtered strategies** (few trades: see docs/STATUS.md, phase 4), the
  highest-OI rule is not part of the backtests, lot sizes are today's, and intraday-triggered option buying is not backtested.
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
