# Implementation plan

Nine phases. Build one phase per Claude Code session, in order. Each phase lists what to build, the tasks tracked in `Trading_System_Build_Tracker.xlsx`, and acceptance checks. A phase is finished only when every acceptance check passes and `docs/AFTER_EACH_PHASE.md` has been followed.

## How to start a phase

Open a terminal in the project folder, run `claude`, and type:

```
Read CLAUDE.md, docs/architecture.md, docs/implementation.md and docs/STATUS.md. Then build Phase N from docs/implementation.md. Build only that phase.
```

## Phase overview

| Phase | Name | What the user will see |
|---|---|---|
| 1 | Data feed and quality gate | Live price page; bad and stale data quarantined; 1-minute bars stored |
| 2 | Market-regime classifier | A 'today's market type' badge that updates during the day |
| 3 | Intraday and swing strategies with backtests | Per-strategy report: net result after costs, drawdown, trade count |
| 4 | Option strategies | Option signals with premium, margin and max loss shown |
| 5 | Risk engine | Quantity and rupee risk on every signal, or NO TRADE with a reason |
| 6 | Live signals and alerts (paper mode) | Real-time instruction cards on the dashboard and Telegram |
| 7 | Journal and weekly scorecard | Weekly table of which strategies earn after costs; losers cut back automatically |
| 8 | LLM layer and go-live gate | A short explanation on each card; a pass/fail decision per strategy |
| 9 | Stock option selling (signals always shown; capital check is advisory) | Entry alerts for hedged stock option spreads with reason, estimated profit and capital needed |

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

Option strategies. Index options are cash-settled; stock options are physically settled, so in this phase stock options are BUY-ONLY and must carry an exit warning before expiry week (hedged stock option selling is added in phase 9 behind a capital gate). No naked option selling of any kind.

### Build steps

1. Add app/options/chain.py: load option chains through BreezeFeed.option_chain() for NIFTY (weekly and monthly), BANKNIFTY (monthly only) and watchlist stocks (monthly). Store strike, right, expiry, bid, ask, last, OI and timestamp. Quarantine rows with missing or crossed bid/ask.
2. Read lot sizes and expiry dates from the broker's instrument list at runtime. Never hardcode a lot size.
3. Add app/options/payoff.py: pure functions for max loss, max profit and breakevens of a long option, a vertical credit spread and an iron condor, including costs. Property-based tests.
4. Implement three strategies using the phase 3 interface: directional option buying (triggered by an intraday or swing signal, picks a liquid near-the-money strike), credit spread (allowed in mild trend regimes), iron condor (RANGE regime only, never on EVENT days).
5. Margin: add a read-only margin lookup. If Breeze's margin calculator needs functions outside the allowed data calls, do not use it; instead add a manual margin entry field and mark the signal MARGIN_UNKNOWN until filled.
6. Every option signal shows premium, lot size, total cost, max loss in rupees and as % of capital, margin blocked, and breakevens.
7. Add app/options/analytics.py: implied volatility rank against the past year, put-call ratio, change in open interest, and the highest-OI call and put strikes as resistance and support. Show them on an 'Options view' page. Use them in strategy rules: buy options only when IV rank is low or moderate, sell spreads only when IV rank is high, and place sold strikes beyond the highest-OI strike.

### Acceptance checks

- No signal if bid/ask, lot size or margin is unknown
- A single short leg without its hedge is rejected
- Stock option selling returns NO_TRADE in this phase and that is tested
- Option backtests are labelled 'payoff simulation' unless real historical option quotes are stored

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
8. Position management: for every position I marked as taken, keep watching it and alert me to move the stop to breakeven after a set gain, trail the stop, book partial profit at the first target, exit on a time stop, or exit immediately if the regime flips, the kill switch fires or the reason for entry is invalidated. Each alert says what to do and why.
9. Pre-market plan at 9:00 IST (regime, bias, events, watchlist levels) and an end-of-day summary at 15:45 (signals given, taken, paper and real P&L, open positions and their stops).
10. Heartbeat: a small watchdog that sends a Telegram alert if the feed is STALE or the engine has not processed a bar for two minutes during market hours.

### Acceptance checks

- An end-to-end test from fixture bars to a stored signal card
- The safety tests still pass
- Write routes exist only under /journal/, /data/import/ and /capital/, and no path contains order, execute, square, submit or trade/

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
| P6.10 | Position management alerts after entry: move stop to breakeven, trail, book partial, exit now | Alert fires for each open position event |
| P6.11 | Pre-market plan and end-of-day summary | Both arrive on Telegram |
| P6.12 | Heartbeat: alert me if the system or feed stops during market hours | Alert within 2 minutes |

---

## Phase 7: Journal and weekly scorecard

**Outcome:** Weekly table of which strategies earn after costs; losers cut back automatically

### Scope

Journal, scorecard and automatic cut-back.

### Build steps

1. Journal: for every signal store the paper result and, if I took it, my real fill and exit. Report slippage (my fill minus signal price) per strategy.
2. Weekly scorecard (runs Saturday, also on demand): per strategy and per regime show trades, win rate, net P&L after costs, max drawdown, profit factor, and paper versus real results side by side. Export as CSV and show on the dashboard.
3. Automatic cut-back rules, all of which only reduce risk: halve a strategy's size the week after it breaches its weekly loss limit; switch a strategy off in a regime after it is net negative over its last 20 trades there, but keep it running on paper; flag it for my approval when the paper version turns positive over 20 trades. Nothing is ever re-enabled or sized up without my approval recorded in the journal.
4. Champion and challenger: allow two versions of a strategy to run, one live and one paper-only, and compare them in the scorecard.
5. Capital ladder: show whether the step-up conditions are met (3 months live, net positive, drawdown under 10%, at least 50 closed trades) and never change capital automatically.
6. Track deposits and withdrawals separately so reported return is on money deposited.

### Acceptance checks

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

**Outcome:** Entry alerts for hedged stock option spreads with reason, estimated profit and capital needed

### Scope

Stock option selling. Capital never blocks a signal in this phase. Every valid setup is shown to me, and I decide whether to trade it. Instead of a gate, each card carries a capital-fit label computed from margin required versus capital_inr in config.yaml: FITS (margin plus the uncommitted-cash rule is satisfied), TIGHT (margin fits but breaks the uncommitted-cash rule), or NEEDS MORE CAPITAL (margin exceeds free capital, with the shortfall in rupees). The recommended capital for this strategy (recommended_capital_inr, default 1000000) is shown as guidance only.

### Build steps

Hard rules: hedged spreads only (every sold option is paired with a bought option of the same expiry); a single uncovered short stock option is rejected; no position may be open in expiry week because stock options are physically settled.
1. Add stock_option_selling to config.yaml: enabled (true), recommended_capital_inr (1000000), max_open_stock_spreads, max_loss_per_stock_spread_pct, min_open_interest, max_bid_ask_spread_pct, exit_days_before_expiry.
2. Eligible list: only F&O stocks that pass liquidity filters on the strikes to be traded (open interest and bid/ask width from the stored option chain). Read lot sizes from the instrument list.
3. Strategy 'stock_credit_spread_v1' using the phase 3 interface. Entry conditions, all thresholds in config: (a) stock trend from the swing rules is up for a bull put spread or down for a bear call spread; (b) the short strike sits beyond a recent support or resistance level and at least a set distance from spot; (c) the stock's implied volatility is high against its own past year, so the premium collected is worthwhile; (d) no results, board meeting or corporate action for that stock before expiry; (e) market regime is not VOLATILE or EVENT.
4. Card content: the 'Why now' section lists which of the conditions above fired with their actual values. The 'Estimate' section shows premium collected (maximum profit), maximum loss, breakevens, margin blocked, return on margin if held to target, and costs. Label it 'Estimate, not a forecast'.
5. Exit rules: profit target at a set share of the premium collected, stop when the spread value reaches a set multiple of the premium, and a forced exit alert exit_days_before_expiry days ahead of expiry that repeats until I mark the position closed.
6. Risk engine: counts margin blocked against the capital pool and reports the capital-fit label; it warns (does not block) on max_open_stock_spreads and the sector limit; it still blocks on missing or stale data, unknown margin, a missing hedge leg, or results before expiry, because those make the numbers on the card unreliable.
7. Backtests are labelled 'payoff simulation' unless real historical stock option quotes are stored. Every signal is paper-traded automatically from day one; until the strategy has 8 weeks of paper results the card shows the banner 'UNPROVEN: no track record yet'.

### Acceptance checks

- Tests that a valid setup produces a signal at Rs 2,00,000 capital labelled NEEDS MORE CAPITAL with the correct shortfall, and FITS at Rs 10,00,000
- A short leg without its hedge is rejected
- No entry is allowed when results fall before expiry
- The forced-exit alert fires
- Safety tests still pass

### Tracker tasks

| ID | Task | Done when |
|---|---|---|
| P9.1 | Capital-fit label on every signal: FITS / TIGHT / NEEDS MORE CAPITAL, with margin required | Signal is shown in all three cases |
| P9.2 | Eligible stock list: most liquid F&O stocks only, with OI and spread filters | Illiquid stocks rejected |
| P9.3 | Hedged credit spread strategy on stocks (bull put / bear call) | Max loss and margin shown |
| P9.4 | Entry timing rules: trend, support/resistance, volatility rank, no results before expiry | Why-now text on the card |
| P9.5 | Estimated profit, max loss, breakevens and margin on the card | All in rupees |
| P9.6 | Forced exit alert before expiry week (physical settlement) | Alert fires in test |
| P9.7 | Automatic paper tracking from day one; UNPROVEN banner until 8 weeks of results | Banner shows and clears correctly |
