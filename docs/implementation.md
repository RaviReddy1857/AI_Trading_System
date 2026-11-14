# Implementation plan

Nine phases. Build one phase per Claude Code session, in the **build order** below (not numeric order). Each phase lists what to build, the tracker tasks (mirrored in `Trading_System_Build_Tracker.xlsx` if you keep one; the tables here are the source), and acceptance checks. A phase is finished only when every acceptance check passes and `docs/AFTER_EACH_PHASE.md` has been followed.

**Build order (changed 2026-11-13): 1, 2, 3 (done), then 4, 5, 6, 9, 7, 8.** Stock option selling (phase 9) moves ahead of the journal (7) and the LLM layer (8) because the user trades monthly hedged stock option selling now. Phase numbers are unchanged; a phase may use only what earlier phases *in this order* have built (phase 9 relies on 4, 5 and 6, not on 7 or 8).

## How to start a phase

Open a terminal in the project folder, run `claude`, and type:

```
Read CLAUDE.md, docs/architecture.md, docs/implementation.md and docs/STATUS.md. Then build Phase N from docs/implementation.md. Build only that phase.
```

## Phase overview

| Build order | Phase | Name | What the user will see |
|---|---|---|---|
| 1 | 1 | Data feed and quality gate | Live price page; bad and stale data quarantined; 1-minute bars stored |
| 2 | 2 | Market-regime classifier | A 'today's market type' badge that updates during the day |
| 3 | 3 | Intraday and swing strategies with backtests | Per-strategy report: net result after costs, drawdown, trade count |
| 4 | 4 | Option strategies | Option signals with premium, margin and max loss shown; option backtests against a no-filter baseline |
| 5 | 5 | Risk engine | Quantity and rupee risk on every signal, or NO TRADE with a reason |
| 6 | 6 | Live signals and alerts (paper mode) | Real-time instruction cards; per-trade-type alert switches; exit calls for every open position |
| 7 | 9 | Stock option selling (signals always shown; capital check is advisory) | Daily shortlist, entry alerts for hedged stock option spreads with strikes, credit, max loss and margin; exit and roll alerts |
| 8 | 7 | Journal and weekly scorecard | Weekly table of which strategies earn after costs; losers cut back automatically |
| 9 | 8 | LLM layer and go-live gate | A short explanation on each card; a pass/fail decision per strategy |

---

## Phase 1: Data feed and quality gate

**Outcome:** Live price page; bad and stale data quarantined; 1-minute bars stored

**Status:** built and tested in simulated mode. Code is in `app/`, tests in `tests/`.

**Remaining (user tasks):** first live Breeze run; verify ICICI stock codes with `scripts/lookup_code.py`.

**What exists:** `app/config.py`, `app/models.py`, `app/quality.py`, `app/bars.py`, `app/store.py`, `app/hub.py`, `app/feeds/sim.py`, `app/feeds/breeze_feed.py`, `app/main.py`, `app/page.py`, `tests/test_step1.py`, `tests/test_safety.py`.

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P1.1 | Project skeleton, config.yaml with capital and % limits | Config loads; limits rescale with capital |
| P1.2 | Read-only Breeze feed and simulated feed | Sim prices show on page |
| P1.3 | Quality gate: bad price, crossed quote, jump, stale | Bad ticks never reach prices or bars |
| P1.4 | 1-minute bars saved to SQLite | Bars readable from /api/bars |
| P1.5 | Safety tests: no order code, no order routes, local host only | pytest passes |
| P1.6 | First live Breeze run on my laptop | Live NIFTY price shows with status LIVE |
| P1.7 | Verify ICICI stock codes with lookup_code.py | Every watchlist symbol subscribes without error |

---

## Phase 2: Market-regime classifier

**Outcome:** A 'today's market type' badge that updates during the day

### Scope

The market-regime classifier.

### Build steps

1. Add app/history.py: download daily and 5-minute bars for every watchlist symbol through BreezeFeed.historical() and store them in SQLite (tables bars_1d, bars_5m). Include a sim/fixture mode that generates history so tests run offline.
2. Add app/indicators.py: SMA, EMA, ATR, daily return, and market breadth (share of watchlist stocks above their 20-day average). Pure functions with unit tests.
3. Add India VIX to the watchlist (find the Breeze code with scripts/lookup_code.py) and store it like any other symbol.
4. Add app/regime.py with a versioned rule set that returns one of TRENDING_UP, TRENDING_DOWN, RANGE, VOLATILE, EVENT plus the reasons. Suggested rules: EVENT if today is in the event calendar; VOLATILE if VIX is above its 1-year 80th percentile or NIFTY's day range exceeds 1.5x ATR; TRENDING if NIFTY is above/below both 20 and 50-day averages and breadth agrees; otherwise RANGE. Put every threshold in config.yaml.
5. Add data/events.csv (date, type, note) for expiry days, RBI policy, Budget and results dates, with a loader.
6. Add a manual import for daily FII/DII cash figures (CSV I paste from the NSE site) and compute a next-day bias: BEARISH after 3 straight days of FII net selling, BULLISH after 3 of buying, else NEUTRAL.
7. Add GET /api/regime and show the regime badge, reasons and bias at the top of the dashboard.
8. Add data/holidays.csv (NSE trading holidays, which I will paste from the NSE site each year) and make the market-hours check use it.

### Acceptance checks

- Tests cover each regime with fixture data
- Same input always gives the same regime
- Missing VIX or history returns regime UNKNOWN, never a guess

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P2.1 | Historical data downloader (daily and 5-minute bars) | History stored for NIFTY, BANKNIFTY and watchlist |
| P2.2 | Indicators: trend, ATR, India VIX level, market breadth | Unit tests on known inputs |
| P2.3 | Regime rules: TRENDING_UP, TRENDING_DOWN, RANGE, VOLATILE, EVENT | Same input gives same regime |
| P2.4 | Event calendar (expiry days, RBI, Budget, results dates) | Event day forces EVENT regime |
| P2.5 | FII/DII daily flow import and next-day bias | Bias shown as BULLISH / BEARISH / NEUTRAL |
| P2.6 | Regime badge and history on the dashboard | Badge visible and updates |
| P2.7 | NSE holiday calendar so closed days are not flagged as stale | Holiday shows CLOSED |

---

## Phase 3: Intraday and swing strategies with backtests

**Outcome:** Per-strategy report: net result after costs, drawdown, trade count

### Scope

Intraday and swing strategies with cost-inclusive backtests.

### Build steps

1. Add app/strategies/base.py: a Strategy interface with id, version, allowed_regimes and a method that takes bars plus regime and returns zero or more Signal records (symbol, direction, entry price or zone, stop, target, valid_until, reason, strategy_id, version).
2. Add app/costs.py: a cost model for ICICI Direct with brokerage, STT, exchange charges, SEBI fee, GST and stamp duty for intraday equity, delivery equity and options, plus a slippage setting. Rates live in config.yaml with an effective date. Leave the rates as clearly marked placeholders for me to fill from my own contract note; fail loudly if they are empty.
3. Implement five strategies, each in its own file with thresholds in config.yaml: opening-range breakout (first 15 minutes), VWAP pullback, range mean-reversion, swing trend pullback, swing volume breakout.
4. Add app/backtest.py: replays stored bars, enters at the NEXT bar's open after a signal, applies stop and target conservatively (if both are hit in one bar, assume the stop was hit first), closes intraday trades by 15:15 IST, applies costs.
5. Add walk-forward evaluation: fixed rules, rolling test windows, and a final untouched holdout period.
6. Add a report per strategy: number of trades, win rate, average win and loss, net P&L after costs, max drawdown, profit factor, and results split by regime. Save as JSON and show on a new dashboard page.

### Acceptance checks

- A test proves the backtest cannot use the signal bar's own close as a fill
- A fixture trade's net P&L reconciles to the paise
- A strategy returns no signal outside its allowed regimes
- Reports state clearly when there are fewer than 30 trades

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P3.1 | Strategy interface and signal record (entry, stop, target, valid-until) | Signal JSON schema validated |
| P3.2 | Cost model: ICICI brokerage plan, STT, exchange fees, GST, stamp duty, slippage | Costs match a real contract note |
| P3.3 | Opening-range breakout | Backtest report produced |
| P3.4 | VWAP pullback | Backtest report produced |
| P3.5 | Range mean-reversion | Backtest report produced |
| P3.6 | Swing: trend pullback and volume breakout | Backtest report produced |
| P3.7 | Backtest engine with next-bar entry and walk-forward split | Look-ahead test fails if same bar is used |
| P3.8 | Each strategy tagged with the regimes it is allowed in | Strategy is silent outside its regime |

---

## Phase 4: Option strategies

**Outcome:** Option signals with premium, margin and max loss shown

### Scope

Option strategies. Index options are cash-settled; stock options are physically settled, so in this phase stock options are BUY-ONLY and must carry an exit warning before expiry week (hedged stock option selling is added in phase 9, built right after phase 6; capital is advisory there, never a gate). No naked option selling of any kind.

Data rules added 2026-11-13: option backtests use Breeze's historical option prices if Breeze provides them (checked first, step 0); every option strategy backtest is compared with a no-filter baseline; Breeze stock history before 2019-01-22 is unreliable and is not used for tests (phase 3b finding: wrong-scale days and ~100x VIX values).

### Build steps

0. **Check Breeze historical option prices first.** With read-only data calls only (`get_historical_data_v2` with an options product type, expiry, strike and right), try a few known past contracts (one NIFTY weekly, one BANKNIFTY monthly, two stock monthlies; daily and 5-minute). Record in `docs/STATUS.md` what exists: which underlyings, intervals, how far back, whether expired contracts are available, and whether OI is included. If usable history exists, download it into an `option_bars` table (same pacing, retry, timeout and quality rules as stock history) and option backtests **must** price entries and exits from it. If it does not, option backtests price options from a model and every report and card says **"payoff simulation"**. A backtest never mixes the two silently: each trade records its price source.
1. Add app/options/chain.py: load option chains through BreezeFeed.option_chain() for NIFTY (weekly and monthly), BANKNIFTY (monthly only) and watchlist stocks (monthly). Store strike, right, expiry, bid, ask, last, OI and timestamp. Quarantine rows with missing or crossed bid/ask.
2. Read lot sizes and expiry dates from the broker's instrument list at runtime. Never hardcode a lot size.
3. Add app/options/payoff.py: pure functions for max loss, max profit and breakevens of a long option, a vertical credit spread and an iron condor, including costs. Property-based tests.
4. Implement three strategies using the phase 3 interface: directional option buying (triggered by an intraday or swing signal, picks a liquid near-the-money strike), credit spread (allowed in mild trend regimes), iron condor (RANGE regime only, never on EVENT days).
5. Margin: add a read-only margin lookup. If Breeze's margin calculator needs functions outside the allowed data calls, do not use it; instead add a manual margin entry field and mark the signal MARGIN_UNKNOWN until filled.
6. Every option signal shows premium, lot size, total cost, max loss in rupees and as % of capital, margin blocked, and breakevens.
7. Add app/options/analytics.py: implied volatility rank against the past year, put-call ratio, change in open interest, and the highest-OI call and put strikes as resistance and support. Show them on an 'Options view' page. Use them in strategy rules: buy options only when IV rank is low or moderate, sell spreads only when IV rank is high, and place sold strikes beyond the highest-OI strike.
8. Implied volatility and delta are computed by code (Black-Scholes on the option mid price, risk-free rate and dividend assumptions in config.yaml), never estimated by an LLM. Phase 9 uses the same functions.
9. **No-filter baseline for every option strategy backtest.** Next to each strategy's result, run the same structure (same strike-selection rule, width, expiry choice and exits) sold or bought on a fixed schedule with no entry rules: every month for monthly strategies, every week for weekly ones, on a fixed day. Report both side by side with the same costs and the same price source. The baseline makes one attempt per expiry (or month) on a date fixed by the calendar; an attempt that cannot be made or filled is recorded as skipped and never retried. A strategy that does not beat its baseline after costs is reported as "no edge vs baseline".
10. Backtests that use stock or index daily history start no earlier than `backtest.test_start` (2019-01-22): Breeze history before that is treated as unreliable.

### Acceptance checks

- No live signal if bid/ask, lot size or margin is unknown. Backtests on stored option history cannot check margin or bid/ask (past margins are unknown; history has traded prices only): they still need the lot size, and every report is labelled "excludes margin and bid/ask eligibility checks"
- A single short leg without its hedge is rejected: such a signal cannot be constructed, the backtest does not enter it and `Store.save_backtest()` does not save it
- Stock option selling returns NO_TRADE in this phase and that is tested
- Option backtests are labelled 'payoff simulation' unless real historical option quotes are stored, and each backtest trade records whether its prices came from stored option history or a model
- If Breeze historical option prices exist, a test shows the backtest uses them (a fixture option bar changes the result)
- Every option backtest report contains the no-filter baseline for the same period, costs and price source
- No option or stock backtest tests a session before 2019-01-22

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P4.1 | Option chain loader (NIFTY weekly and monthly, BANKNIFTY monthly, stock monthly) | Chain stored with bid, ask, OI |
| P4.2 | Lot sizes and expiry calendar from the instrument list, never hardcoded | Lot size matches exchange value |
| P4.3 | Directional option buying (index and stock) | Max loss = premium + costs shown |
| P4.4 | Credit spread (NIFTY, BANKNIFTY) | Max loss and margin shown |
| P4.5 | Iron condor (NIFTY weekly) | Max loss and margin shown |
| P4.6 | Margin figure fetched from ICICI or entered manually | No signal if margin is unknown |
| P4.7 | Stock options are buy-only in this phase, with expiry-week exit warning | Stock option selling arrives in phase 9 |
| P4.8 | Option intelligence: IV rank, put-call ratio, highest-OI strikes as support and resistance | Shown on dashboard and used by strategies |
| P4.9 | Check whether Breeze provides historical option prices (index and stock, intervals, depth, expired contracts) | Findings recorded in STATUS.md; option history stored if available, otherwise reports say "payoff simulation" |
| P4.10 | No-filter baseline in every option backtest (same spread every month or week, no entry rules) | Baseline shown beside each strategy with the same costs |
| P4.11 | Pre-2019 Breeze stock history treated as unreliable | No backtest tests a session before 2019-01-22 (test) |
| P4.12 | IV and delta computed by code (Black-Scholes on mid price) | Values match a hand calculation in a test |

---

## Phase 5: Risk engine

**Outcome:** Quantity and rupee risk on every signal, or NO TRADE with a reason

### Scope

The risk engine. It is the only component allowed to approve a signal, and it can always say NO TRADE.

### Build steps

1. Add app/risk/sizing.py: quantity = (capital x max_loss_per_trade_pct) / (entry - stop), rounded down, with a cap on position value; for options, lots = floor(max_loss_per_option / max loss per lot); zero lots means NO TRADE.
2. Add app/risk/portfolio.py: track open paper and manual positions, capital committed, margin blocked and cash free. Enforce uncommitted_cash_pct and max_open_selling_spreads.
3. Add sector tags to the watchlist and enforce: max two open trades per sector, and treat symbols with 60-day return correlation above 0.8 as one position.
4. Enforce daily_loss_stop_pct and monthly_loss_pause_pct using realised plus open P&L.
5. Drawdown from peak equity (deposits excluded): halve all sizes at drawdown_halve_pct, pause new signals at drawdown_pause_pct.
6. Kill switch: stop all signals if the feed is STALE for the index, if quarantined ticks spike, or if the clock is outside market hours.
7. Every decision returns APPROVED with quantity and rupee risk, or NO_TRADE with reason codes (DATA_STALE, RISK_BUDGET_USED, SECTOR_LIMIT, DAILY_STOP, DRAWDOWN_PAUSE, MARGIN_UNKNOWN, ZERO_SIZE, OUTSIDE_REGIME).
8. Changing capital_inr in config.yaml must rescale everything; deposits are logged separately so they never count as profit.

### Acceptance checks

- Table-driven tests for each rule
- A test that no code path can raise risk above the configured percentages
- Unknown inputs always produce NO_TRADE

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P5.1 | Volatility-based position sizing from ATR and max loss % | Quantity matches hand calculation |
| P5.2 | Shared capital pool and uncommitted cash rule | Signal blocked when pool is used up |
| P5.3 | Sector and correlation limits | Third trade in same sector blocked |
| P5.4 | Daily loss stop and monthly pause | No new signals after limit is hit |
| P5.5 | Drawdown rules: halve sizes at 10%, pause at 15% | Sizes change automatically |
| P5.6 | Kill switch on stale feed or abnormal prices | All signals stop |
| P5.7 | NO TRADE reason codes | Every blocked signal has a code |

---

## Phase 6: Live signals and alerts (paper mode)

**Outcome:** Real-time instruction cards on the dashboard and Telegram

### Scope

Live signals in paper mode.

### Build steps

1. Add app/engine.py: on every closed 1-minute or 5-minute bar, compute the regime, run the allowed strategies, pass each signal through the risk engine, and store the result (approved or NO_TRADE with reasons) in a signals table. Nothing is ever sent to the broker.
2. Signal card on the dashboard: instrument, direction, entry zone, stop, target, quantity or lots, valid-until time, strategy id and version, regime and data timestamp. Show NO TRADE cards too, collapsed.
2a. Every approved card has a 'Why now' section built by code from the rule conditions that fired, in plain words (for example: 'Price broke above the first 15-minute high on 2.1x average volume; market regime is TRENDING_UP; FII bias is BULLISH; no results due this week').
2b. Every approved card has an 'Estimate' section in rupees: profit if target is hit (for option selling: maximum profit = premium collected), loss if stop is hit (for spreads: maximum loss), risk-reward ratio, costs, and breakeven(s). Label it 'Estimate if target is hit, not a forecast'.
2c. Show the strategy's historical hit rate and average result from backtest and paper trades with the number of trades behind it; mark it 'low sample' under 30 trades. Never show a probability of profit that is not backed by recorded trades.
3. Add Telegram alerts through a bot token in .env; send approved signals only, plus a message when the daily stop or kill switch triggers. If Telegram fails, log it and carry on.
4. Add app/paper.py: every approved signal is paper-traded automatically at the next bar's open and tracked to stop, target or time exit, with costs.
5. Add two buttons per card, 'I took this trade' and 'I skipped it', with an optional note and my actual fill price. These only write to the journal.
6. Signals expire at valid-until and cannot be acted on afterwards.
7. Signal score (0-100) computed by code from confluence: agreement between 5-minute, 15-minute and daily trend, volume versus average, relative strength versus NIFTY and sector, fit with the regime, FII bias, and distance to the nearest support or resistance. Rank signals by score, send Telegram alerts only for the top ones, and cap alerts per day (config). The score is a ranking aid, never shown as a probability.
8. **Exit calls for every open position**, of every trade type (intraday stocks, swing stocks, index option buying, index option selling, stock option buying, stock option selling), whether I marked it as taken from a card or entered it manually (step 12). The engine re-evaluates each open position on every closed bar and issues an exit call when any of these fires: target hit; stop hit; move stop to breakeven (after a set gain); trail the stop; book partial profit; time exit; entry reason invalidated; regime flip; kill switch. Each call states the **action** (for example "Exit all 2 lots now", "Move stop to 1,245.50") and the **reason** with the values that triggered it. A call repeats (interval in config.yaml) until I mark the position closed under /journal/. Option positions are judged on the option or spread price as well as the underlying. Exit calls are alerts only; nothing is sent to the broker.
9. Pre-market plan at 9:00 IST (regime, bias, events, watchlist levels) and an end-of-day summary at 15:45 (signals given, taken, paper and real P&L, open positions and their stops).
10. Heartbeat: a small watchdog that sends a Telegram alert if the feed is STALE or the engine has not processed a bar for two minutes during market hours.
11. **Trade-type selector.** Six switches: intraday stocks, swing stocks, index option buying, index option selling, stock option buying, stock option selling. Each switch controls alerts (dashboard highlight and Telegram) for that type only; every strategy keeps paper-trading whatever its switch says. Beside each switch show the type's track record (paper and backtest trades, win rate, net result after costs, with the number of trades; "low sample" under 30) and its label: **UNPROVEN** (no track record yet), **"negative in backtests"** (from `strategies.status`, phase 3b), or the strategy's other status label. An alert is sent only when the type's switch is on **and** the strategy's `strategies.status` entry allows alerts, so a strategy labelled "negative in backtests" stays silent until its status is changed in config.yaml. The switch positions are stored in the database (table `alert_switches`), changed only through `POST /journal/alert-switches` (JSON body: type, on/off; local Host/Origin rules apply) and default to off. Each change is logged with time and old/new value. Exit calls for positions I hold are always sent, whatever the switch.
12. **Manual position entry** under /journal/ (`POST /journal/positions`, `POST /journal/positions/{id}/close`): positions I opened outside a signal (instrument or option legs, quantity or lots, entry price, date, optional stop and target). They get the same exit calls as positions taken from cards. Phase 9 extends this to stock option spreads and unhedged positions.

### Acceptance checks

- An end-to-end test from fixture bars to a stored signal card
- The safety tests still pass
- Write routes exist only under /journal/, /data/import/ and /capital/, and no path contains order, execute, square, submit or trade/
- With a type's switch off, its signals are paper-traded and no alert is sent; with it on, alerts follow `strategies.status` (a strategy with `alerts: false` sends none)
- Switch positions survive a restart (database) and each change is logged
- For each trade type, a fixture open position produces each exit call (target, stop, breakeven, trail, partial, time, entry invalidated, regime flip, kill switch) with an action and a reason, and the call repeats until the position is marked closed

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P6.1 | Live signal loop: bars -> regime -> strategies -> risk -> card | Card appears within one bar |
| P6.2 | Signal card: instrument, entry, stop, target, quantity, valid-until | All fields present |
| P6.3 | Card explains WHY NOW in plain words and shows estimated profit, max loss and risk-reward in rupees | Every approved card has them |
| P6.4 | Card shows the strategy's past hit rate with its sample size | Marked 'low sample' under 30 trades |
| P6.5 | Telegram bot alerts | Message arrives on phone |
| P6.6 | Paper trade simulator tracks each signal to exit | Paper P&L after costs recorded |
| P6.7 | 'I took this trade' / 'I skipped' buttons (journal only) | Decision stored |
| P6.8 | Signals expire after valid-until | Expired card greyed out |
| P6.9 | Signal score from confluence (timeframes, volume, relative strength, regime, bias); only top-ranked alerts sent | Daily alert cap respected |
| P6.10 | Exit calls for every open position of every trade type (taken or entered manually): target, stop, breakeven, trailing stop, partial booking, time exit, entry reason invalidated, regime flip, kill switch | Each call states action and reason and repeats until the position is marked closed (test per trade type) |
| P6.11 | Pre-market plan and end-of-day summary | Both arrive on Telegram |
| P6.12 | Heartbeat: alert me if the system or feed stops during market hours | Alert within 2 minutes |
| P6.13 | Trade-type selector: six switches controlling alerts per type; switched-off types still paper-trade | Switch off = paper only, no alert; switch on = alerts only for strategies whose status allows them |
| P6.14 | Track record and label beside each switch (UNPROVEN, "negative in backtests") | Shown with trade count; "low sample" under 30 |
| P6.15 | Switch choices stored in the database, changed through POST /journal/alert-switches, changes logged | Survives restart; safety test passes |
| P6.16 | Manual position entry under /journal/ for any trade type | Manually entered position receives exit calls |

---

## Phase 7: Journal and weekly scorecard

**Outcome:** Weekly table of which strategies earn after costs; losers cut back automatically

### Scope

Journal, scorecard and automatic cut-back. Built after phase 9 (build order 4, 5, 6, 9, 7, 8), so it covers every trade type, including the stock option spreads of phase 9 and manually entered positions, and builds on the journal routes and tables phase 6 already created.

### Build steps

1. Journal: for every signal store the paper result and, if I took it, my real fill and exit. Report slippage (my fill minus signal price) per strategy.
2. Weekly scorecard (runs Saturday, also on demand): per strategy and per regime show trades, win rate, net P&L after costs, max drawdown, profit factor, and paper versus real results side by side. Export as CSV and show on the dashboard.
3. Automatic cut-back rules, all of which only reduce risk: halve a strategy's size the week after it breaches its weekly loss limit; switch a strategy off in a regime after it is net negative over its last 20 trades there, but keep it running on paper; flag it for my approval when the paper version turns positive over 20 trades. Nothing is ever re-enabled or sized up without my approval recorded in the journal.
4. Champion and challenger: allow two versions of a strategy to run, one live and one paper-only, and compare them in the scorecard.
5. Capital ladder: show whether the step-up conditions are met (3 months live, net positive, drawdown under 10%, at least 50 closed trades) and never change capital automatically.
6. Track deposits and withdrawals separately so reported return is on money deposited.
7. **Scorecard by stock and by entry conditions (added 2026-11-14).** Beside the per-strategy and per-regime tables, show
   the same figures per stock and per entry condition recorded on the signal: the stock's trend state (TRENDING_UP,
   TRENDING_DOWN, RANGING), IV rank band and FII bias at the signal. Each table compares the signals I took with the ones I
   skipped (paper result of both, my real result for the ones taken), so it shows whether my choosing adds or loses money.
   The entry conditions are already stored on every stock option selling signal (`details.entry_conditions`, phase 9).

### Acceptance checks

- The by-stock and by-entry-condition tables add up to the per-strategy totals, and taken and skipped signals are shown
  side by side with their counts (low sample marked)
- Tests for each cut-back rule
- A test that no automatic rule can increase size
- Scorecard marks any figure based on fewer than 30 trades as 'low sample'

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P7.1 | Trade journal: signal price vs my actual fill (slippage) | Slippage per strategy reported |
| P7.2 | Weekly scorecard per strategy and per regime | Report generated every weekend |
| P7.3 | Auto cut-back: halve size after weekly limit, switch off after 20 losing trades in a regime | Rule fires in test |
| P7.4 | Champion vs challenger: new rule versions run on paper beside live | Both versions tracked |
| P7.5 | Capital ladder check: conditions for step-up shown | Step-up eligible / not eligible |
| P7.6 | Deposits tracked separately from profits | Return on deposited money shown |
| P7.7 | Scorecard by stock and by entry conditions (trend state, IV rank, FII bias), signals taken vs skipped | Tables add up to the strategy totals; low sample marked |

---

## Phase 8: LLM layer and go-live gate

**Outcome:** A short explanation on each card; a pass/fail decision per strategy

### Scope

The LLM layer and the go-live gate.

### Build steps

1. Add app/llm/base.py with a ResearchModel interface, and app/llm/claude_cli.py that runs `claude -p` as a subprocess with --output-format json and --json-schema, tools disabled, a timeout, and a daily call budget from config.yaml. Never send broker credentials, account numbers or personal details in a prompt.
2. Pre-market briefing at 8:45 IST: summarise overnight global cues, FII/DII bias, today's events and the expected regime, from data I provide or the system has stored. Mark it clearly as commentary.
3. For each approved signal, ask for a bull case, a bear case, what would invalidate the trade, and an event-risk tag (NONE, LOW, HIGH). Show these on the card. The model may not change any number on the card; reject any reply that contains a price, quantity or strike not present in the input.
4. News scan every 15-30 minutes in market hours on headlines I supply through a simple feed or paste box: the model can tag a symbol EVENT_RISK, which pauses new signals there for a set time. It can never create or approve a trade.
5. Treat all news text and model output as untrusted data: no instructions inside them are followed.
6. If the CLI fails, times out or the budget is used up, signals continue with 'no AI commentary' shown.
7. Go-live gate report: after at least 8 weeks of paper trading, list each strategy with trades, net result after costs, drawdown and a PASS / FAIL / NOT ENOUGH DATA verdict against thresholds in config.yaml. Only I can switch account.mode to live_manual.

### Acceptance checks

- Tests with a fake model for schema failure, invented numbers, prompt injection in a headline, timeout and budget exhaustion
- Safety tests still pass

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P8.1 | ResearchModel interface with Claude CLI backend (claude -p, JSON schema) | Schema-valid JSON returned |
| P8.2 | Pre-market briefing: global cues, FII/DII, events | Briefing on dashboard by 9:00 |
| P8.3 | Per-signal bull case, bear case and event-risk tag | Shown on card |
| P8.4 | News scan can pause a symbol but never create a trade | Tested |
| P8.5 | LLM failure falls back to rules-only | Signals still work with CLI offline |
| P8.6 | Go-live gate report after 8-12 weeks of paper trading | Pass/fail per strategy |

---

## Phase 9: Stock option selling (signals always shown; capital check is advisory)

**Outcome:** A daily shortlist of stocks for monthly hedged option selling, entry alerts with both strikes, credit, max loss, breakeven and margin, and exit and roll alerts until each position is closed

**Core built early (2026-11-14, by the user's instruction, before phases 5 and 6):** universe and liquidity filter, daily
shortlist, strike rules, entry window, exit and adjustment rules, the backtest with its no-filter baseline, and the
additions in "Core additions" below. **Still to build after phase 6:** signal cards with the capital-fit label
(P9.1, P9.9), Telegram alerts and repeating exit calls (the alert part of P9.7), the manual position form (P9.8), the risk
engine hooks (step 8) and automatic paper tracking (P9.11).

**Built after phase 6** (build order 4, 5, 6, 9, 7, 8). Uses phase 4 (option chains, lot sizes, IV and delta, payoff, analytics, option history check), phase 5 (risk engine) and phase 6 (alerts, trade-type selector, exit calls, manual positions, paper trading). Does not depend on phases 7 or 8.

### Scope

Monthly hedged stock option selling (bull put spreads and bear call spreads). Capital never blocks a signal in this phase. Every valid setup is shown to me, and I decide whether to trade it. Instead of a gate, each card carries a capital-fit label computed from margin required versus capital_inr in config.yaml: FITS (margin plus the uncommitted-cash rule is satisfied), TIGHT (margin fits but breaks the uncommitted-cash rule), or NEEDS MORE CAPITAL (margin exceeds free capital, with the shortfall in rupees). The recommended capital for this strategy (recommended_capital_inr, default 1000000) is shown as guidance only.

Hard rules: hedged spreads only (every sold option is paired with a bought option of the same expiry); the system never suggests an unhedged position, and a single uncovered short stock option is rejected (`UNHEDGED_SHORT`); no position may be open in expiry week because stock options are physically settled. Every number on a card (strikes, delta, credit, max loss, breakeven, margin) is computed by code.

### Configuration (`stock_option_selling` in config.yaml, each with a validated field)

enabled (true), recommended_capital_inr (1000000), max_open_stock_spreads, max_loss_per_stock_spread_pct, min_open_interest, max_bid_ask_spread_pct, max_short_delta (0.20), hedge_strikes_out (2-3, i.e. min 2, max 3), entry_min_days_to_expiry (20), entry_max_days_to_expiry (35), profit_target_pct_of_credit (50), stop_multiple_of_credit (2.0), exit_days_before_expiry (start of expiry week), shortlist_size, ranking weights (IV rank, trend clarity, distance to support/resistance, liquidity), lookback for support/resistance, alert repeat interval, risk-free rate.

### Build steps

1. **Universe.** All NSE F&O stocks from the broker's instrument list (not only the watchlist), refreshed daily; lot sizes and expiries from the same list. A stock is eligible only if the strikes to be traded pass the liquidity filters (open interest at least min_open_interest, bid-ask width at most max_bid_ask_spread_pct of mid) in the stored option chain. Chain requests are paced like history requests; failures make that stock ineligible for the day, never guessed.
2. **Daily shortlist after the close.** For each eligible stock and each eligible expiry, score: IV rank (high is better for selling), trend clarity (from the swing trend rules; a clear up trend for bull put spreads, a clear down trend for bear call spreads), distance from spot to the relevant support or resistance, and liquidity. Exclude any stock with results, a board meeting or a corporate action before expiry. Show the top `shortlist_size` candidates with the reason for each score on a read-only **"Stock option selling"** page (`/stock-option-selling`, `GET /api/stock-option-selling/shortlist`), stored daily in a `shortlist` table.
3. **Short strike.** For puts: the first strike below the nearest support, below the highest-OI put strike, with |delta| at or below max_short_delta. For calls: the first strike above the nearest resistance, above the highest-OI call strike, with delta at or below max_short_delta. If no strike meets all three conditions: no signal, with the reason shown.
4. **Hedge strike.** Same expiry, hedge_strikes_out strikes further out (try 2, then 3), choosing the nearest one that keeps max loss (width x lot size - net credit, plus costs) within max_loss_per_stock_spread_pct of capital and passes the liquidity filters. If none qualifies: no signal. The card shows net credit, max loss, breakeven, margin blocked (phase 4 margin rule; MARGIN_UNKNOWN until known), return on margin at target, costs and the capital-fit label.
5. **Entry window.** Signals only when days to expiry is between entry_min_days_to_expiry and entry_max_days_to_expiry (default 20-35), never in expiry week.
6. **Exit and adjustment alerts** (through the phase 6 exit calls; each repeats until I mark the position closed):
   - Profit target: spread can be bought back for (100 - profit_target_pct_of_credit)% of the credit, i.e. 50% of the credit kept.
   - Stop: the cost to close the spread reaches stop_multiple_of_credit x the credit received (default 2x, a loss of about 1x the credit before costs).
   - Threat: the stock closes beyond the short strike, or the trend rules flip against the position. The alert shows two choices with numbers: **Exit** (cost to close now, realised P&L) and **Roll** (close this spread and open the same structure at the next valid strikes and/or the next month, with the net debit or credit of the roll, the new max loss, the new breakeven and margin). Roll choices obey all entry rules; if no valid roll exists, it says so.
   - Forced exit: before expiry week (exit_days_before_expiry), "Exit now: physical settlement", repeating until closed.
7. **Manual positions** under /journal/ (extends phase 6 step 12): enter spreads I already hold, leg by leg (stock, expiry, strike, right, buy/sell, lots, price). A position with a short leg not covered by a bought leg of the same expiry and right is stored and flagged **UNHEDGED** in red on every view and alert, with max loss shown as unlimited (calls) or strike-based (puts). It still receives every exit call. The system never generates an unhedged suggestion, including in rolls.
8. **Risk engine:** counts margin blocked against the capital pool and reports the capital-fit label; it warns (does not block) on max_open_stock_spreads and the sector limit; it still blocks on missing or stale data, unknown margin, a missing hedge leg, illiquid strikes, or results before expiry, because those make the numbers on the card unreliable. Capital stays advisory: capital-fit label only.
9. **Evidence:** backtests use stored option history if phase 4 found it, otherwise they are labelled "payoff simulation"; each includes the phase 4 no-filter baseline (the same spread sold every month at a fixed days-to-expiry with no entry rules); no session before 2019-01-22. Every signal is paper-traded automatically from day one; until the strategy has 8 weeks of paper results the card and the trade-type switch show **UNPROVEN: no track record yet**. Alerts for this type follow the phase 6 switch "stock option selling" and `strategies.status`.

### Core additions (2026-11-14; every threshold is in `stock_option_selling` in config.yaml)

10. **Backtest exit check.** Target and stop are checked on the legs' 5-minute option bars (filled at the next 5-minute
    bar's open) and, for comparison, on daily closes filled at the next session's open. Every report shows the strategy and
    its baseline both ways. A day without stored 5-minute bars is checked on the daily close and counted.
11. **Option-history download.** Within a daily request budget (`options.backtest.daily_request_budget`, default 3,500,
    counted in the store per day across runs), the 30 most liquid watchlist F&O stocks first, newest year first, resumable,
    skipping everything already stored or asked for. A Breeze error reply stops it at once and records the progress.
12. **Falling-stock block** (`trend_block`): no put-selling signal when the stock is below its 200-day average, or made a
    52-week low in the last 20 sessions, or its 3-month return trails NIFTY by more than a set margin
    (`DOWNTREND_BLOCK`). Mirror rule for call selling in a strong uptrend (`UPTREND_BLOCK`).
13. **Per-stock campaign limit** (`campaign_limit`): after 2 stopped trades or a set cumulative loss in one stock within 90
    days, no new signal in that stock for a cooling-off period (`STOCK_LOSS_LIMIT`). Closed manual positions count too.
14. **Range and speed profile** per stock (2 and 5 years, data from 2019-01-22 only): typical monthly high-low range,
    average sessions from swing low to swing high, ranging or trending now, and how often a strike at the proposed distance
    was passed within the holding period, shown beside the move implied by current option prices.
15. **Scenario history** per stock: the distribution of 30-day moves after past FII selling streaks, India VIX spikes, RBI
    rate decisions and its own results, using only data available at the time. The number of cases is shown; under 10 is
    LOW SAMPLE; a scenario whose inputs are not in the data files says "no data". Labelled "history, not a forecast".
16. **Open-position alerts** (sent through Telegram from phase 6): the stock closes below support or a 52-week low, breaks
    its 200-day average, an FII selling streak begins, India VIX spikes, the regime turns VOLATILE or EVENT, or news tags
    the stock EVENT_RISK (mirrored for bear call spreads). Each alert says what changed and shows exit and roll with numbers.
17. **Campaign tracking.** Every roll is linked to its original trade. Stored and shown: cumulative realised P&L, total
    hedge cost paid, total charges, and the best possible remaining outcome (the open position's max profit plus the P&L so
    far). If the best case is still a loss: "recovery not possible from this position".
18. **Roll rules** (`roll`). A roll is suggested only if (a) the new credit after hedge cost and charges is positive and at
    least a minimum share of the new spread's width, (b) the sold strike moves the way the market has gone (lower for puts,
    higher for calls), never the same or a worse strike, (c) the stock passes the falling-stock block, and (d) the campaign
    has had fewer than `max_rolls` rolls (default 1). Otherwise the alert is "exit". A roll is one lot.
19. **Net credit after hedge and charges** on every signal; skipped when it is below a minimum share of the maximum loss
    (`CREDIT_TOO_SMALL`).
20. **Stuck-position alert**: the sold option is in the money with time value below a set share of its price, or the
    position is inside the pre-expiry window: "exit; rolling earns little".
21. **No recovery sizing.** A new signal after a closed campaign uses the normal rules and the normal size only.
22. **Exit-policy comparison in the backtest**, on exactly the same entries: stop at 2x credit, roll by the roll rules, and
    roll every month without limit. All three are reported per campaign.
23. **Reversal detector** (`reversal`; watch list and candidates only, nothing alerts until the user approves it):
    - Washed-out list: more than a set % below the 52-week high for at least a set number of months, and below the 200-day
      average. Shown on the stock option page as "watch", never as a signal.
    - Base forming: no new low for a set number of sessions, and shrinking daily ranges.
    - Reversal confirmed (`REVERSAL_CONFIRMED`): close above the 20-day average and above the last swing high, volume above
      its 20-day average, and the 10-day return ahead of NIFTY. This lifts the falling-stock block only for put spreads
      placed below the base low.
    - On confirmation two candidate trades: a bull call spread, and a put credit spread with the sold strike below the base
      low. Prior breakdown levels are shown as stall zones.
    - The same reversal alert goes to every open or manually entered position in that stock (once approved).
    - Backtest on data from 2019-01-22: for every confirmed reversal the 10, 20 and 40-session outcome, the share that made
      a new low afterwards, and both candidate trades against the no-filter baseline, with LOW SAMPLE where it applies.
24. **Effect of every rule and alert in the backtest**: the strategy with one rule removed, the baseline with one rule or
    one alert-exit added, and the baseline's trades split by what was known at entry.

### Final specification (2026-11-14) and its parameter table

The consolidated Phase 9 specification of 2026-11-14 replaces the build steps above where they differ. Its state against
the code and the milestones M1-M5 are tracked in docs/STATUS.md ("Phase 9 final specification" and after).

**Parameter table** (config.yaml `stock_option_selling.`, validated in `app/config.py`; starting defaults, not tuned; a
change needs the user's approval). Sessions are NSE trading sessions; days are calendar days. "Missing" says what happens
when the input is not known: it is never treated as a pass and never as zero.

| Name (config key) | Value | Unit, lookback, comparison | Missing data |
|---|---|---|---|
| `sma_short`, `sma_mid`, `sma_long` | 20, 50, 200 | sessions; simple averages of closes including the decision day | fewer bars: INSUFFICIENT_HISTORY (trend block), no trend (NO_TREND), MARKET_UNKNOWN (market block) |
| `trend_block.new_low_lookback`, `low_52w_sessions` | 20, 252 | DOWNTREND_BLOCK when the lowest close of the last 252 sessions was made in the last 20 (UPTREND_BLOCK: highest close) | fewer than 252 bars: INSUFFICIENT_HISTORY |
| `trend_block.rel_strength_lookback`, `rel_strength_max_lag_pct` | 63, 5 | sessions; stock return minus NIFTY return, points; below -5 blocks puts, above +5 blocks calls | no NIFTY close on either day: INSUFFICIENT_HISTORY |
| `market_block.market_breadth_min_pct` | 40 | % of watchlist stocks closing above their sma_mid; below 40 or NIFTY below its sma_long gives MARKET_WEAK (puts only) | under `min_coverage_pct` (80%) of stocks usable, or under 200 NIFTY bars: MARKET_UNKNOWN |
| `campaign_limit.stock_loss_window_days`, `stock_loss_max_stops`, `stock_loss_max_pct_capital`, `stock_cooloff_days` | 90, 2, 1.5, 30 | days; stops counted and net loss summed over closed trades in the window; % of capital; cooling-off days from the triggering exit | live: positions not recorded yet, PORTFOLIO_UNKNOWN |
| `max_risk_per_position_pct` | 2.5 | % of capital; cost-inclusive max loss of the whole position; whole lots, rounded down | 0 lots: ZERO_SIZE |
| `portfolio.max_open_spreads`, `max_total_risk_pct` | 6, 10 | positions (each counted, the proposed one included); % of capital | positions unknown: PORTFOLIO_UNKNOWN |
| `portfolio.correlation_window`, `correlation_threshold`, `correlation_min_overlap` | 60, 0.7, 50 | sessions of close-to-close returns up to the decision day; linked when correlation >= 0.7 or same sector; groups are connected components | sector unknown, or under 50 common returns between different sectors: CORRELATION_UNKNOWN |
| `portfolio.group_max_positions`, `group_max_risk_pct` | 2, 5 | positions per group, proposed one included; % of capital | as above |
| `min_credit_to_max_loss` | 0.10 | net credit after hedge, charges and slippage / cost-inclusive max loss, rupees; below gives CREDIT_TOO_SMALL. Set by the user from the pre-outcome distribution | a price unknown: no setup |
| `short_delta_max` | 0.20 | absolute delta of the sold strike from its own price (Black-Scholes) | delta not computable: that strike is not used |
| `hedge_strikes_out` | 2 (2-3) | priced strikes further out than the sold one | no priced strike: UNHEDGED_SHORT, no signal |
| `entry_dte_min`, `entry_dte_max` | 20, 35 | calendar days to the monthly expiry, inclusive | no expiry in the window: NO_EXPIRY |
| `profit_target_pct_of_credit` | 50 | % of the entry credit kept: exit when buy-back value <= 50% of the credit | leg price unknown: exit guidance says so |
| `stop_spread_value_multiple` | 2.0 | exit when the buy-back value >= 2 x the entry credit | as above |
| `time_exit_dte` | 10 | calendar days before expiry: exit at the next open | - |
| `forced_exit_trading_days_before_expiry` | 5 | trading days before expiry: no position inside this window | - |
| `roll.max_rolls` | 1 | completed rolls per campaign | - |
| `roll.min_roll_net_credit_pct_of_width` | 10 | (new credit - debit to close the old spread - all four legs' charges) / (replacement width x lot x lots), must be above 10% | a leg of the open spread unpriced: ROLL_PRICE_UNKNOWN, exit |
| `roll.stuck_time_value_pct` | 20 | sold option in the money with time value under 20% of its price, or inside the forced-exit window: "exit; rolling earns little" | price unknown: not claimed stuck |
| `options.iv_rank_lookback_days` (iv_rank_lookback) | 252 | sessions of the stock's at-the-money IV (point in time) | under 200 days: IV_RANK_UNKNOWN |
| expected_move (`stock_profile.expected_move`) | formula | spot x IV x sqrt(DTE / 365), IV annualized; a one-standard-deviation model estimate, not a guaranteed range | IV or DTE unknown: shown as unknown |
| `swing_pivot_bars`, `pivot_lookback_sessions` | 5, 252 | pivot low/high: below/above the 5 sessions before, at or below/above the 5 after; usable only after the 5 later sessions completed; support = nearest pivot low below the close, resistance = nearest pivot high above it, among the last 252 sessions (pivot_lookback_sessions is a new definition, 2026-11-15) | none: NO_CONFIRMED_LEVEL, no signal |
| `min_open_interest_lots`, `max_bid_ask_pct_of_mid` | 200, 5 | open interest in lots on each traded strike; live bid-ask width, % of mid | OI unknown live: ILLIQUID; no bid/ask: DATA_STALE (not actionable) |
| `bias.streak_days` (fii_streak_sessions) | 3 | straight sessions of FII net selling (BEARISH) in data/fii_dii.csv | figures missing: bias unknown, scenario "no data" |
| `profile.vix_spike_rise_pct`, `vix_spike_sessions`, `vix_spike_percentile`, `vix_percentile_lookback` (vix_spike) | 20, 5, 80, 252 | India VIX up >= 20% over 5 sessions, or closing above the 80th percentile of the previous 252 closes | a part without enough history counts only if the other part says yes; otherwise unknown |
| `reversal.washed_out_drawdown_pct`, `min_sessions_below_long` | 25, 63 | close >= 25% below the 52-week high (highest high of 252 sessions), with 63 sessions in a row below sma_long | under 252 bars: not assessed |
| `reversal.base_no_new_low_sessions`, `range_sessions`, `range_compare_sessions`, `range_shrink_ratio` (base_range_contraction) | 15, 10, 20, 0.8 | the 52-week low at least 15 sessions old, and the 10-session ATR below 0.8 x the 20-session ATR | as above |
| `reversal.bull_call.entry_min_dte`, `entry_max_dte` | 20, 35 | calendar days (user, 2026-11-14) | no expiry: no candidate |
| `event_coverage_max_age_days` | 7 | days since the COVERAGE row's retrieved_at; covered_through must be on or after expiry | EVENT_COVERAGE_UNKNOWN: no actionable signal |
| `backtest.capital_inr` | 10,00,000 | rupees: the capital of every Phase 9 backtest and research page | - |
| `score.weights` | 1 each | equal weights of the six factors: iv_rank, iv_falling, rel_strength, strike_distance, range_profile, liquidity | a factor unknown: score incomplete, ranked after the scored candidates |
| `score.iv_peak_sessions`, `iv_falling_ratio` (iv_falling) | 20, 0.9 | IV at or below 90% of its 20-session peak: full points; at the peak: none; linear between | no IV history: factor unknown |
| `score.hist_fall_sessions`, `hist_fall_percentile` (hist_fall_percentile) | 21, 90 | the 90th percentile of 21-session falls (rises for calls) since 2019-01-22, windows ended before the decision | under 30 windows: factor unknown |

**Score point rules (M2, written before any result).** Each factor 0-100 points, combined as the weighted mean:
IV rank as it is; IV falling as above; relative strength 50 + 5 per point of (63-session return minus NIFTY's, averaged with
minus the sector peers' median) for put spreads, 50 - 5 for call spreads, clamped; strike distance 50 points per yardstick
(the expected move to expiry and the 90th-percentile fall) at 1.5x or more of it, linearly from 0 (a 90th-percentile fall
of zero or less gives that half in full); range profile 100 - 2 x the past breach rate in %; liquidity log10(open interest
in lots / minimum) x 100. **Score bands:** each day's candidates ranked by score (ties by stock, expiry, side); top third
= 3 x position < n, bottom third = 3 x position >= 2n, else middle; fewer than 3 candidates that day = "small
cross-section"; trades used are the strategy's without the portfolio limits.

### Acceptance checks

- Tests that a valid setup produces a signal at Rs 2,00,000 capital labelled NEEDS MORE CAPITAL with the correct shortfall, and FITS at Rs 10,00,000
- Short-strike tests: the chosen strike is beyond support/resistance, beyond the highest-OI strike and at or below the delta limit; when no strike meets all three, there is no signal
- Hedge tests: the hedge is 2-3 strikes out, same expiry, liquid, and max loss stays within max_loss_per_stock_spread_pct; otherwise no signal
- No signal outside 20-35 days to expiry or in expiry week; no entry when results or a corporate action fall before expiry
- A short leg without its hedge is rejected; a manually entered unhedged position is stored and flagged UNHEDGED; no suggestion (entry or roll) is ever unhedged
- Profit-target, stop, threat (exit and roll with numbers) and forced-exit alerts fire in tests and repeat until the position is closed
- The shortlist excludes illiquid stocks and stocks with events before expiry, and shows a reason per candidate
- Safety tests still pass (new writes only under /journal/)

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P9.1 | Capital-fit label on every signal: FITS / TIGHT / NEEDS MORE CAPITAL, with margin required | Signal is shown in all three cases |
| P9.2 | Universe: all NSE F&O stocks from the instrument list, filtered by OI and bid-ask width on the strikes to be traded | Illiquid stocks and strikes rejected (test) |
| P9.3 | Daily shortlist after the close ranked by IV rank, trend clarity, distance to support/resistance, liquidity; events before expiry excluded | "Stock option selling" page shows top candidates with reasons |
| P9.4 | Short strike: beyond nearest support/resistance, beyond highest-OI strike, delta at or below 0.20 (config) | No signal when no strike meets all three (test) |
| P9.5 | Hedge strike: same expiry, 2-3 strikes out (config), max loss within max_loss_per_stock_spread_pct, liquid | Card shows net credit, max loss, breakeven and margin |
| P9.6 | Entry window 20-35 days to expiry (config), never in expiry week | No signal outside the window (test) |
| P9.7 | Exit and adjustment alerts: 50% profit target, 2x credit stop, short strike breached or trend flip with exit and roll numbers, forced exit before expiry week | Each alert fires in a test and repeats until closed |
| P9.8 | Manual spread entry under /journal/, unhedged positions flagged UNHEDGED; system never suggests an unhedged position | UNHEDGED flag shown; no unhedged suggestion in tests |
| P9.9 | Capital stays advisory: capital-fit label only | Signal never hidden for capital (test) |
| P9.10 | Backtest with option history if available (else "payoff simulation") and the no-filter monthly baseline, from 2019-01-22 | Report shows strategy and baseline side by side |
| P9.11 | Automatic paper tracking from day one; UNPROVEN banner until 8 weeks of results | Banner shows and clears correctly |
| P9.12 | Backtest exits checked on 5-minute option bars and on daily closes | Report shows strategy and baseline both ways (built) |
| P9.13 | Option-history download within a daily request budget, most liquid stocks first, resumable | Stops at the budget or on a Breeze error and carries on the next day (built) |
| P9.14 | Falling-stock block and its mirror (DOWNTREND_BLOCK / UPTREND_BLOCK) | No put signal in a falling stock (test); effect shown against the baseline (built) |
| P9.15 | Per-stock campaign limit (STOCK_LOSS_LIMIT), manual positions included | No signal during the cooling-off period (test) (built) |
| P9.16 | Range and speed profile per stock, 2 and 5 years, with the option-implied move | Shown on the stock option page (built) |
| P9.17 | Scenario history: FII streaks, VIX spikes, RBI decisions, own results; "history, not a forecast" | Cases counted, under 10 LOW SAMPLE, missing inputs "no data" (built; needs the user's data files) |
| P9.18 | Open-position alerts with exit and roll numbers | Each alert fires once in a test (built); Telegram and repeats in phase 6 |
| P9.19 | Campaign tracking: rolls linked to the original trade, hedge cost, charges, best possible outcome | "recovery not possible from this position" shown when the best case is a loss (built) |
| P9.20 | Roll rules (a)-(d), max_rolls 1; otherwise "exit" | Each rule refuses a roll in a test (built) |
| P9.21 | Net credit after hedge and charges on every signal; CREDIT_TOO_SMALL | Small credit skipped (test) (built) |
| P9.22 | Stuck-position alert: "exit; rolling earns little" | Fires for an in-the-money option with little time value and inside the pre-expiry window (built) |
| P9.23 | No recovery sizing | A signal after a losing campaign is identical to a fresh one (test) (built) |
| P9.24 | Backtest: stop vs roll by rules vs roll every month, on the same entries | All three reported per campaign (built) |
| P9.25 | Reversal detector: washed-out watch list, base forming, REVERSAL_CONFIRMED, two candidate trades, stall zones | Shown as "watch"; nothing alerts until approved (built) |
| P9.26 | Reversal backtest: 10/20/40-session outcomes, new-low share, both candidates vs baseline | Reported with LOW SAMPLE where it applies (built) |
| P9.27 | M1: whole-lot sizing and ZERO_SIZE; confirmed 5-bar pivots; portfolio and correlation limits; research versus actionable; open positions at the cutoff; spec names; monthly counts; section 2 vix_spike and expected_move | Built and tested (2026-11-15) |
| P9.28 | M2: 0-100 score, six equal-weight factors with contributions; score bands by each day's candidates; each factor against the baseline | Built and tested (2026-11-15) |
