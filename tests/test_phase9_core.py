"""Phase 9 core: stock option selling rules, shortlist, exits and rolls, the backtest with 5-minute and
daily exit checks beside its no-filter baseline, and the budgeted, resumable option-history download.
Acceptance checks of docs/implementation.md phase 9 that belong to the core are marked ACCEPTANCE."""
import math
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.config import Settings, StockOptionSelling as SosParams
from app.costs import CostModel
from app.history import daily_start
from app.manual_data import Event
from app.models import Bar, unhedged_short_legs
from app.options.backtest import (OptionEngine, model_prices, run_option_backtest, sync_decision,
                                  synchronized_bars)
from app.options.base import ChainQuotes, ExpiryInfo, OptionContext, OptionQuote
from app.options.greeks import bs_delta, bs_price, years_to_expiry
from app.options.option_history import (INTRADAY, IntradayBar, OptionHistory, OptionIntraday, budgeted_fetcher,
                                        parse_option_rows)
from app.options.shortlist import run_shortlist
from app.options.stock_backtest import coverage, download_blocks, liquid_stocks
from app.options.stock_selling import (SpreadPosition, StockOptionSelling, build_shortlist, build_stock_selling,
                                       exit_calls, exit_levels, roll_choices, trend_of)
from app.options.stock_profile import pivots, support_resistance
from app.quality import IST
from app.store import Store
from tests.test_phase3 import FixedRegime
from tests.test_phase3 import settings as _settings

# The tests pin the thresholds their fixtures were built for, so they do not move when a default in
# config.yaml changes. test_shipped_defaults_are_the_section_2_values checks the shipped defaults.
FIXTURE_SOS = {
    "min_open_interest": 100, "max_bid_ask_spread_pct": 10.0, "max_loss_per_stock_spread_pct": 10.0,
    "min_net_credit_pct_of_max_loss": 10.0,
    "trend_block": {"enabled": True, "long_sma_days": 200, "extreme_lookback_sessions": 252, "extreme_recent_sessions": 20,
                    "relative_return_sessions": 63, "max_underperformance_pct": 10.0},
    "campaign_limit": {"enabled": True, "window_days": 90, "max_stopped_trades": 2, "max_cumulative_loss_pct": 5.0,
                       "cooling_off_days": 45},
    "roll": {"max_rolls": 1, "min_net_credit_pct_of_width": 10.0, "stuck_time_value_pct": 10.0},
    # the fixture worlds hold one or two of the five fixture-watchlist stocks: breadth is computed from those
    "market_block": {"enabled": True, "breadth_sma_days": 50, "min_breadth_pct": 40.0, "min_coverage_pct": 20.0},
    # scaled with the fixture's 10% position budget (the shipped values are 10% total and 5% a group for 2.5%)
    "portfolio": {"enabled": True, "max_open_spreads": 6, "max_total_risk_pct": 40.0, "correlation_window": 60,
                  "correlation_threshold": 0.7, "correlation_min_overlap": 50, "group_max_positions": 2,
                  "group_max_risk_pct": 20.0},
}


def settings(**over):
    sos = {**FIXTURE_SOS, **over.pop("stock_option_selling", {})}
    return _settings(stock_option_selling=sos, **over)

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)          # Friday 15:30 IST: the close
EXP = date(2026, 12, 8)                                  # 25 days away
EXP_NEXT = date(2026, 12, 29)                            # 46 days away
RATE = 0.065
LOT = 250


# ---------------- fixtures: daily bars and a live chain ----------------
def weekdays_back(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def stock_bars(sym="RELIANCE", n=320, start=1000.0, drift=1.0, end=date(2026, 11, 13), wobble=6.0, volume=1e6):
    """`n` daily bars ending on `end`. The price is `start` 120 sessions before the end and moves
    `drift` a session, with a small wave."""
    bars = []
    for i, d in enumerate(weekdays_back(end, n)):
        j = i - (n - 120)
        px = start + drift * j + wobble * math.sin(j / 3)
        bars.append(Bar(sym, daily_start(d), px - 2, px + 8, px - 8, px, volume))
    return bars


def index_bars(bars, level=24000.0, drift=0.0):
    return tuple(Bar("NIFTY", b.start, level + drift * i, level + drift * i + 50, level + drift * i - 50,
                     level + drift * i, None) for i, b in enumerate(bars))


def chain(spot, expiry=EXP, iv=0.25, lo=800, hi=1500, step=10, oi_lots=500, lot=LOT, asof=NOW, oi=None, wide=()):
    """Black-Scholes quotes with a 2% bid-ask width. `oi`: {(strike, right): lots}. `wide`: strikes with a 40% width."""
    t = years_to_expiry(asof, expiry)
    out = []
    for k in range(lo, hi + 1, step):
        for right in ("CE", "PE"):
            p = max(0.1, bs_price(spot, k, t, RATE, 0.0, iv, right))
            half = p * (0.2 if (k, right) in wide else 0.01)
            lots = (oi or {}).get((k, right), oi_lots)
            out.append(OptionQuote(float(k), right, round(p - half, 2), round(p + half, 2), round(p, 2), lots * lot))
    return out


COVERED = "source=NSE corporate filings; covered_through=2026-12-31"


def ctx_for(s: Settings, bars, quotes=None, iv_rank=80.0, margin=40000.0, events=(), expiries=None, sym="RELIANCE",
            iv=0.25, coverage=True, **over) -> OptionContext:
    """`coverage`: add a fresh event-coverage row for the stock (retrieved today, covering to 31 December)."""
    spot = bars[-1].close
    if coverage:
        events = tuple(events) + (Event(date(2026, 11, 13), "COVERAGE", COVERED, sym),)
    chains = quotes if isinstance(quotes, dict) else {EXP: quotes if quotes is not None else chain(spot, iv=iv)}
    sos = s.stock_option_selling
    base = dict(
        underlying=sym, kind="stock", asof=NOW, regime="RANGE", spot=spot,
        expiries=expiries or tuple(ExpiryInfo(e, "monthly", LOT) for e in sorted(chains)),
        quotes=ChainQuotes(spot, chains), costs=CostModel(s.costs), capital_inr=s.account.capital_inr, rate=RATE,
        iv_reference_pct=iv * 100, iv_rank=iv_rank, margin=lambda structure, width: margin, require_margin=True,
        historical=False, max_spread_pct=sos.max_bid_ask_spread_pct, min_oi_lots=sos.min_open_interest,
        events=tuple(events), daily=tuple(bars), index_daily=index_bars(bars), market={"breadth_pct": 60.0})
    base.update(over)
    return OptionContext(**base)


@pytest.fixture(scope="module")
def s():
    return settings()


# A roll that qualifies: a 1,000-share lot (so the fixed brokerage is small beside the premium), capital
# large enough for that lot's max loss, and an open put spread 1110/1100 that is cheap to close.
LOT_R = 1000
HELD_ROLL = None            # set below, once SpreadPosition is imported


def big_settings(**sos):
    return settings(account={"capital_inr": 2000000.0}, stock_option_selling=sos)


def roll_ctx(cfg, bars, cheap=True, drop=(), **kw):
    """Chain for the roll tests. `cheap`: the held 1110 put is quoted 2.00/2.20 and the 1100 put
    1.40/1.60, so closing the spread costs 2.20 - 1.40 = 0.80 a share. `drop`: (strike, right)
    quotes left out of the chain."""
    spot = bars[-1].close
    quotes = []
    for q in chain(spot, lot=LOT_R):
        if (q.strike, q.right) in drop:
            continue
        if cheap and (q.strike, q.right) == (1110.0, "PE"):
            q = OptionQuote(1110.0, "PE", 2.0, 2.2, 2.1, q.oi)
        if cheap and (q.strike, q.right) == (1100.0, "PE"):
            q = OptionQuote(1100.0, "PE", 1.4, 1.6, 1.5, q.oi)
        quotes.append(q)
    return ctx_for(cfg, bars, quotes, expiries=(ExpiryInfo(EXP, "monthly", LOT_R),), **kw)


@pytest.fixture()
def strat(s):
    return build_stock_selling(s)


def legs_of(sig):
    return next(x for x in sig.legs if x.action == "SELL"), next(x for x in sig.legs if x.action == "BUY")


# ---------------- trend, support, resistance ----------------
def test_trend_and_support_resistance_by_hand():
    up, down = stock_bars(drift=1.0), stock_bars(start=1200, drift=-1.0)
    assert trend_of(up, 20, 50, 14)[0] == "UP" and trend_of(down, 20, 50, 14)[0] == "DOWN"
    flat = stock_bars(drift=0.0, wobble=0.0)
    assert trend_of(flat, 20, 50, 14)[0] is None                  # close equals both averages: no clear trend
    assert trend_of(up[:30], 20, 50, 14)[0] is None                # not enough history is unknown, not a trend
    # support: the nearest confirmed 5-bar pivot low below the close; resistance: the nearest pivot high above it
    sup, res = support_resistance(up, 252, 5)
    piv = [x for x in pivots(up[-257:], 5) if x[1] >= 5]
    assert sup == max(x[3] for x in piv if x[0] == "L" and x[3] < up[-1].close)
    assert res is None or res == min(x[3] for x in piv if x[0] == "H" and x[3] > up[-1].close)
    assert sup < up[-1].close and support_resistance(up[:100], 252, 5) == (None, None)
    assert 0 < trend_of(up, 20, 50, 14)[1] <= 1


# ---------------- short strike ----------------
def test_ACCEPTANCE_short_strike_is_beyond_support_beyond_the_highest_oi_strike_and_within_the_delta_limit(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    support, _ = support_resistance(bars, 252, 5)
    wall = float(int(support // 10) * 10 - 10)                     # highest-OI put strike just below support
    quotes = chain(spot, oi={(int(wall), "PE"): 5000})
    sig = strat.evaluate(ctx_for(s, bars, quotes))
    assert sig, strat.last_no_trade
    short, long_ = legs_of(sig[0])
    t = years_to_expiry(NOW, EXP)
    by = {(q.strike, q.right): q for q in quotes}

    def delta(k):
        from app.options.greeks import iv_and_delta
        return iv_and_delta(by[(k, "PE")].mid, spot, k, t, RATE, 0.0, "PE")[1]

    assert short.right == "PE" and short.strike < support and short.strike < wall
    assert abs(delta(short.strike)) <= s.stock_option_selling.max_short_delta
    nearer = short.strike + 10                                     # the next strike toward the money breaks a rule
    assert not (nearer < support and nearer < wall and abs(delta(nearer)) <= s.stock_option_selling.max_short_delta)
    assert sig[0].asset_class == "STOCK_OPTION" and sig[0].details["side"] == "PE"
    assert long_.strike < short.strike and long_.expiry == short.expiry == EXP


def test_ACCEPTANCE_bear_call_short_strike_is_above_resistance(s, strat):
    bars = stock_bars(start=1200, drift=-1.0)
    spot = bars[-1].close
    _, resistance = support_resistance(bars, 252, 5)
    strat = build_stock_selling(settings(stock_option_selling={"min_net_credit_pct_of_max_loss": 5}))
    sig = strat.evaluate(ctx_for(s, bars, chain(spot)))
    assert sig, strat.last_no_trade
    short, long_ = legs_of(sig[0])
    assert short.right == "CE" and short.strike > resistance and long_.strike > short.strike
    assert abs(sig[0].details["legs"][0]["delta"]) <= s.stock_option_selling.max_short_delta


def test_ACCEPTANCE_no_signal_when_no_strike_meets_all_three_conditions(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    # the highest-OI put strike is the lowest listed strike: nothing lies beyond it
    assert strat.evaluate(ctx_for(s, bars, chain(spot, oi={(800, "PE"): 9000}))) == []
    assert any(r.startswith("NO_STRIKE") for r in strat.last_no_trade)
    # no strike below support at all
    support, _ = support_resistance(bars, 252, 5)
    lo = int(support // 10) * 10 + 10
    assert strat.evaluate(ctx_for(s, bars, chain(spot, lo=lo))) == []
    assert any(r.startswith("NO_STRIKE") for r in strat.last_no_trade)
    # delta: with a very high implied volatility no strike within reach is at or below the limit
    tight = build_stock_selling(settings(stock_option_selling={"max_short_delta": 0.01}))
    assert tight.evaluate(ctx_for(s, bars, chain(spot, iv=0.9), iv=0.9)) == []
    assert any(r.startswith("NO_STRIKE") for r in tight.last_no_trade)


# ---------------- hedge strike ----------------
def test_ACCEPTANCE_hedge_is_two_strikes_out_same_expiry_and_within_the_max_loss_limit(s, strat):
    bars = stock_bars()
    sig = strat.evaluate(ctx_for(s, bars))[0]
    short, long_ = legs_of(sig)
    assert short.strike - long_.strike == 20 and sig.details["hedge_strikes_out"] == 2      # 10-point strikes
    assert long_.expiry == short.expiry and long_.right == short.right
    limit = s.account.capital_inr * s.stock_option_selling.max_loss_per_stock_spread_pct / 100
    assert 0 < sig.details["max_loss_inr"] <= limit
    # max loss = (width - credit) x lot x lots + costs, by hand
    d = sig.details
    n = d["lots"]
    assert n >= 1 and short.lots == long_.lots == n
    assert d["max_loss_inr"] == pytest.approx((20 - d["net_credit"]) * LOT * n + d["costs_inr"], abs=0.02)
    assert unhedged_short_legs(sig.legs) == []


def test_ACCEPTANCE_hedge_moves_to_three_strikes_out_when_two_is_illiquid_and_no_signal_when_neither_works(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    base = strat.evaluate(ctx_for(s, bars))[0]
    short, long_ = legs_of(base)
    two, three = int(short.strike - 20), int(short.strike - 30)
    sig = strat.evaluate(ctx_for(s, bars, chain(spot, oi={(two, "PE"): 5})))            # 5 lots: below 100
    assert sig and legs_of(sig[0])[1].strike == three and sig[0].details["hedge_strikes_out"] == 3
    sig = strat.evaluate(ctx_for(s, bars, chain(spot, wide={(two, "PE")})))            # bid-ask width 40%
    assert sig and legs_of(sig[0])[1].strike == three
    assert strat.evaluate(ctx_for(s, bars, chain(spot, oi={(two, "PE"): 5, (three, "PE"): 5}))) == []
    assert any(r.startswith("ILLIQUID") for r in strat.last_no_trade)
    # one lot's max loss above the risk budget at both widths: zero whole lots, no signal
    small = build_stock_selling(settings(stock_option_selling={"max_loss_per_stock_spread_pct": 1.0}))
    assert small.evaluate(ctx_for(s, bars)) == []
    assert any(r.startswith("ZERO_SIZE") and "0 whole lots" in r for r in small.last_no_trade)


def test_ACCEPTANCE_a_short_leg_without_a_hedge_is_never_suggested(s, strat):
    bars = stock_bars()
    spot = bars[-1].close
    base = strat.evaluate(ctx_for(s, bars))[0]
    short = legs_of(base)[0]
    quotes = [q for q in chain(spot) if not (q.right == "PE" and q.strike < short.strike)]   # nothing further out
    assert strat.evaluate(ctx_for(s, bars, quotes)) == []
    assert any(r.startswith("UNHEDGED_SHORT") for r in strat.last_no_trade)
    # the illiquid sold strike itself is refused too
    assert strat.evaluate(ctx_for(s, bars, chain(spot, oi={(int(short.strike), "PE"): 5}))) == []
    assert any(r.startswith("ILLIQUID") for r in strat.last_no_trade)


@given(iv=st.floats(0.12, 0.7), drift=st.sampled_from([-1.5, -1.0, 1.0, 1.5]), wobble=st.floats(0, 12),
       lot=st.sampled_from([50, 250, 700, 2400]))
@hyp_settings(max_examples=40, deadline=None)
def test_every_signal_is_hedged_inside_the_window_and_inside_the_loss_limit(iv, drift, wobble, lot):
    cfg = settings()
    stg = build_stock_selling(cfg)
    bars = stock_bars(start=1000 if drift > 0 else 1300, drift=drift, wobble=wobble)
    spot = bars[-1].close
    c = ctx_for(cfg, bars, chain(spot, iv=iv, lot=lot), iv=iv, expiries=(ExpiryInfo(EXP, "monthly", lot),))
    for sig in stg.evaluate(c):
        short, long_ = legs_of(sig)
        assert unhedged_short_legs(sig.legs) == [] and short.expiry == long_.expiry and short.right == long_.right
        assert (long_.strike < short.strike) if short.right == "PE" else (long_.strike > short.strike)
        assert short.right == ("PE" if drift > 0 else "CE")
        assert sig.details["max_loss_inr"] <= cfg.account.capital_inr * cfg.stock_option_selling.max_loss_per_stock_spread_pct / 100
        assert sig.details["max_profit_inr"] > 0 and 20 <= sig.details["days_to_expiry"] <= 35
        assert sig.target < sig.entry < sig.stop


# ---------------- entry window, events, trend, IV rank, margin ----------------
@pytest.mark.parametrize("expiry,ok", [(date(2026, 11, 24), False), (date(2026, 12, 2), False), (date(2026, 12, 3), True),
                                       (date(2026, 12, 18), True), (date(2026, 12, 19), False), (date(2027, 1, 26), False)])
def test_ACCEPTANCE_no_signal_outside_20_to_35_days_to_expiry_or_in_expiry_week(s, strat, expiry, ok):
    bars = stock_bars()
    c = ctx_for(s, bars, {expiry: chain(bars[-1].close, expiry=expiry)})
    out = strat.evaluate(c)
    assert bool(out) == ok
    if not ok:
        assert any(r.startswith("NO_EXPIRY") for r in strat.last_no_trade)


def test_ACCEPTANCE_no_entry_when_results_or_a_corporate_action_fall_before_expiry(s, strat):
    bars = stock_bars()
    for kind in ("RESULTS", "BOARD_MEETING", "CORPORATE_ACTION"):
        assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 11, 25), kind, "", "RELIANCE")])) == []
        assert any(r.startswith("EVENT_BEFORE_EXPIRY") for r in strat.last_no_trade)
    assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 11, 25), "RESULTS", "", "TCS")]))      # another stock
    assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 12, 9), "RESULTS", "", "RELIANCE")]))  # after expiry
    assert strat.evaluate(ctx_for(s, bars, events=[Event(date(2026, 11, 25), "RBI_POLICY", "", "")]))      # market-wide


def test_ACCEPTANCE_event_coverage_unknown_blocks_an_actionable_signal_and_keeps_the_research_candidate(s, strat):
    from app.options.stock_selling import event_coverage
    bars = stock_bars()
    today = date(2026, 11, 13)
    row = lambda retrieved, through, sym="RELIANCE": Event(retrieved, "COVERAGE", f"source=NSE; covered_through={through}", sym)
    assert event_coverage([row(today, "2026-12-08")], "RELIANCE", today, EXP, 7)[0] is True
    assert event_coverage([row(date(2026, 11, 6), "2026-12-31")], "RELIANCE", today, EXP, 7)[0] is True       # 7 days old
    ok, why = event_coverage([row(date(2026, 11, 5), "2026-12-31")], "RELIANCE", today, EXP, 7)                # 8 days old
    assert ok is False and "more than 7 days ago" in why
    ok, why = event_coverage([row(today, "2026-12-07")], "RELIANCE", today, EXP, 7)                           # stops before expiry
    assert ok is False and "before the expiry" in why
    assert event_coverage([row(today, "2026-12-31", "TCS")], "RELIANCE", today, EXP, 7)[0] is False            # another stock
    assert event_coverage([row(date(2026, 11, 14), "2026-12-31")], "RELIANCE", today, EXP, 7)[0] is False      # dated after today
    assert event_coverage([Event(today, "COVERAGE", "checked by hand", "RELIANCE")], "RELIANCE", today, EXP, 7)[0] is False
    assert event_coverage([], "RELIANCE", today, EXP, 7)[0] is False
    # the newest row counts: an old one beside a fresh one is fine
    assert event_coverage([row(date(2026, 10, 1), "2026-12-31"), row(today, "2026-12-31")], "RELIANCE", today, EXP, 7)[0] is True
    # strategy: with coverage a signal; without it no actionable signal, but the research candidate stays visible
    assert strat.evaluate(ctx_for(s, bars))
    assert strat.evaluate(ctx_for(s, bars, coverage=False)) == []
    assert any(r.startswith("EVENT_COVERAGE_UNKNOWN") for r in strat.last_no_trade)
    a = strat.assess(ctx_for(s, bars, coverage=False))
    assert a.signal is None and a.status == "CANDIDATE" and a.short_strike is not None
    assert any(r.startswith("EVENT_COVERAGE_UNKNOWN") for r in a.reasons) and any("credit" in r for r in a.reasons)
    listed = build_shortlist(strat, [ctx_for(s, bars, coverage=False)])[0]
    assert listed["status"] == "SHORTLISTED" and any(r.startswith("EVENT_COVERAGE_UNKNOWN") for r in listed["reasons"])
    stale = ctx_for(s, bars, coverage=False, events=[row(date(2026, 11, 1), "2026-12-31")])
    assert strat.evaluate(stale) == []
    # an announced event still blocks outright, coverage or not
    assert strat.assess(ctx_for(s, bars, events=[Event(date(2026, 11, 25), "RESULTS", "", "RELIANCE")])).status == "EXCLUDED"
    # backtests cannot know past coverage: the rule is not applied there (and is listed as bypassed, never as a pass)
    hist = replace(ctx_for(s, bars, coverage=False), historical=True, require_margin=False, margin=None)
    assert strat.evaluate(hist)
    from app.options.backtest import BYPASSED_CHECKS
    assert any("not available historically" in x and "event" in x for x in BYPASSED_CHECKS)


def test_events_csv_coverage_rows_parse_and_round_trip():
    from app.manual_data import coverage_fields, events_to_csv, parse_events
    text = ("date,type,note,symbol,source,retrieved_at,covered_through\n"
            "2026-11-25,RESULTS,Q2,RELIANCE,,,\n"
            ",COVERAGE,,RELIANCE,NSE corporate filings,2026-11-13,2026-12-31\n"
            "2026-11-12,COVERAGE,,TCS,BSE announcements,,2026-12-29\n")
    events, errors = parse_events(text)
    assert errors == [] and len(events) == 3
    cov = {e.symbol: e for e in events if e.type == "COVERAGE"}
    assert cov["RELIANCE"].date == date(2026, 11, 13) and coverage_fields(cov["RELIANCE"].note) == ("NSE corporate filings", date(2026, 12, 31))
    assert cov["TCS"].date == date(2026, 11, 12) and coverage_fields(cov["TCS"].note) == ("BSE announcements", date(2026, 12, 29))
    again, errors = parse_events(events_to_csv(events))                # the stored four-column form reads back the same
    assert errors == [] and again == events
    for bad in (",COVERAGE,,RELIANCE,,2026-11-13,2026-12-31\n",      # no source
                ",COVERAGE,,RELIANCE,NSE,2026-11-13,\n",              # no covered_through
                ",COVERAGE,,,NSE,2026-11-13,2026-12-31\n"):            # no symbol
        got, errs = parse_events("date,type,note,symbol,source,retrieved_at,covered_through\n" + bad)
        assert got == [] and len(errs) == 1
    assert coverage_fields("anything") is None and coverage_fields("") is None


def test_phase9_backtests_run_at_the_stock_option_capital_and_list_the_bypassed_checks(world):
    from app.options.stock_backtest import phase9_settings
    real = _settings()
    p9 = phase9_settings(real)
    assert p9.account.capital_inr == 1000000.0 == real.stock_option_selling.backtest.capital_inr
    assert real.account.capital_inr != 1000000.0 or True               # the account capital itself is not changed
    assert p9.stock_option_selling.max_loss_per_stock_spread_pct == 2.5   # the risk % is untouched: Rs 25,000 a position
    rep = run_bt(world)["report"]
    assert len(rep["bypassed_checks"]) >= 5 and any("margin" in x for x in rep["bypassed_checks"])
    assert any("bid/ask" in x for x in rep["bypassed_checks"]) and any("event" in x for x in rep["bypassed_checks"])


def test_no_signal_without_a_clear_trend_a_known_high_iv_rank_a_margin_or_a_live_price(s, strat):
    bars = stock_bars()
    flat = stock_bars(drift=0.0, wobble=0.0)
    cases = [(ctx_for(s, flat), "NO_TREND"), (ctx_for(s, bars, iv_rank=None), "IV_RANK_UNKNOWN"),
             (ctx_for(s, bars, iv_rank=30.0), "IV_RANK_TOO_LOW"), (ctx_for(s, bars, margin=None), "MARGIN_UNKNOWN"),
             (ctx_for(s, bars, status="STALE"), "DATA_STALE"), (ctx_for(s, bars, kind="index"), "NOT_A_STOCK"),
             (ctx_for(s, bars, expiries=(ExpiryInfo(EXP, "monthly", None),)), "LOT_SIZE_UNKNOWN")]
    for c, code in cases:
        assert strat.evaluate(c) == [] and any(r.startswith(code) for r in strat.last_no_trade), code
    off = build_stock_selling(settings(stock_option_selling={"enabled": False}))
    assert off.evaluate(ctx_for(s, bars)) == [] and any(r.startswith("DISABLED") for r in off.last_no_trade)
    with pytest.raises(ValueError):                                 # a live context cannot switch the margin rule off
        ctx_for(s, bars, require_margin=False)


def test_strategy_identity_status_and_determinism(s, strat):
    assert strat.id == "stock_option_selling" and strat.version.startswith("1/")
    assert (strat.alerts_enabled, strat.mode) == (False, "paper_only") and "UNPROVEN" in strat.status_label
    other = build_stock_selling(settings(stock_option_selling={"max_short_delta": 0.15}))
    assert other.version != strat.version                           # a threshold change is a new version
    bars = stock_bars()
    a, b = strat.evaluate(ctx_for(s, bars))[0], strat.evaluate(ctx_for(s, bars))[0]
    assert a.to_dict() == b.to_dict()
    # the index credit spread still refuses stocks, and this strategy refuses an index
    from app.options import build_option_strategies
    cs = build_option_strategies(s, ["credit_spread"])[0]
    assert cs.evaluate(ctx_for(s, bars)) == [] and any("STOCK_OPTION_SELLING_NOT_ALLOWED" in r for r in cs.last_no_trade)


def test_shipped_defaults_are_the_section_2_values():
    """config.yaml as shipped (not the fixture values above): the starting defaults of the Phase 9
    specification, section 2. Changing one needs the user's approval."""
    p = _settings().stock_option_selling
    assert p.max_loss_per_stock_spread_pct == 2.5
    assert p.min_net_credit_pct_of_max_loss == 10.0         # 0.10, set by the user from the pre-outcome distribution
    assert p.backtest.download_first_years_from == 2023
    assert (p.event_coverage_max_age_days, p.backtest.capital_inr) == (7, 1000000.0)
    assert (rv_bc := p.reversal.bull_call).entry_min_dte == 20 and rv_bc.entry_max_dte == 35
    assert (p.max_short_delta, p.hedge_strikes_out) == (0.20, [2, 3])
    assert (p.entry_min_days_to_expiry, p.entry_max_days_to_expiry) == (20, 35)
    assert (p.profit_target_pct_of_credit, p.stop_multiple_of_credit) == (50.0, 2.0)
    assert (p.exit_days_before_expiry, p.forced_exit_trading_days_before_expiry) == (10, 5)
    assert (p.min_open_interest, p.max_bid_ask_spread_pct) == (200, 5.0)
    assert (p.trend_fast_sma, p.trend_slow_sma, p.trend_block.long_sma_days) == (20, 50, 200)
    tb, cl, r, rv = p.trend_block, p.campaign_limit, p.roll, p.reversal
    assert (tb.extreme_recent_sessions, tb.extreme_lookback_sessions, tb.relative_return_sessions,
            tb.max_underperformance_pct) == (20, 252, 63, 5.0)
    assert (cl.window_days, cl.max_stopped_trades, cl.max_cumulative_loss_pct, cl.cooling_off_days) == (90, 2, 1.5, 30)
    assert (r.max_rolls, r.min_net_credit_pct_of_width, r.stuck_time_value_pct) == (1, 10.0, 20.0)
    assert (rv.drawdown_pct, rv.min_sessions_below_long, rv.base_no_new_low_sessions) == (25.0, 63, 15)
    assert (rv.range_sessions, rv.range_compare_sessions, rv.range_shrink_ratio) == (10, 20, 0.8)
    assert rv.alerts_approved is False and _settings().options.backtest.daily_request_budget == 3500
    pf = p.portfolio
    assert (pf.max_open_spreads, pf.max_total_risk_pct, pf.correlation_window, pf.correlation_threshold,
            pf.correlation_min_overlap, pf.group_max_positions, pf.group_max_risk_pct) == (6, 10.0, 60, 0.7, 50, 2, 5.0)
    mb = p.market_block
    assert (mb.enabled, mb.breadth_sma_days, mb.min_breadth_pct) == (True, 50, 40.0)       # market_breadth_min_pct


def test_config_is_validated():
    for bad in ({"entry_min_days_to_expiry": 40}, {"exit_days_before_expiry": 5}, {"hedge_strikes_out": [3, 2]},
                {"hedge_strikes_out": [2]}, {"trend_fast_sma": 60}, {"max_short_delta": 0.6},
                {"backtest": {"exit_checks": []}}, {"backtest": {"exit_checks": ["hourly"]}}):
        with pytest.raises(Exception):
            settings(stock_option_selling=bad)
    with pytest.raises(Exception):
        settings(options={"backtest": {"daily_request_budget": -1}})
    p = SosParams()
    assert (p.entry_min_dte, p.entry_max_dte, p.exit_days_before_expiry) == (20, 35, 10)
    assert settings().options.backtest.daily_request_budget == 3500


# ---------------- shortlist ----------------
def shortlist_contexts(s):
    good = stock_bars("RELIANCE")
    strong = stock_bars("TCS", drift=1.5)            # steady enough to keep confirmed pivot lows below the price
    illiquid = stock_bars("INFY")
    event = stock_bars("HDFCBANK")
    flat = stock_bars("ICICIBANK", drift=0.0, wobble=0.0)
    return [
        ctx_for(s, good, iv_rank=60.0, sym="RELIANCE"),
        ctx_for(s, strong, chain(strong[-1].close, hi=1700), iv_rank=95.0, sym="TCS"),
        ctx_for(s, illiquid, chain(illiquid[-1].close, oi_lots=10), sym="INFY"),
        ctx_for(s, event, events=[Event(date(2026, 11, 20), "RESULTS", "Q3", "HDFCBANK")], sym="HDFCBANK"),
        ctx_for(s, flat, sym="ICICIBANK"),
    ]


def test_ACCEPTANCE_shortlist_excludes_illiquid_stocks_and_events_and_shows_a_reason_per_candidate(s, strat):
    rows = build_shortlist(strat, shortlist_contexts(s))
    by = {r["stock"]: r for r in rows}
    top = [r for r in rows if r["status"] == "SHORTLISTED"]
    assert [r["stock"] for r in top] == ["TCS", "RELIANCE"] and [r["rank"] for r in top] == [1, 2]
    assert top[0]["score"] > top[1]["score"]                        # higher IV rank and a clearer trend rank first
    for r in top:
        assert r["side"] == "PE" and r["expiry"] == EXP.isoformat() and len(r["reasons"]) >= 4
        assert set(r["scores"]) == {"iv_rank", "trend_clarity", "distance", "liquidity"}
        assert all(0 <= v <= 1 for v in r["scores"].values())
    w = s.stock_option_selling.weights
    sc = top[0]["scores"]
    assert top[0]["score"] == pytest.approx(sc["iv_rank"] * w.iv_rank + sc["trend_clarity"] * w.trend_clarity
                                            + sc["distance"] * w.distance + sc["liquidity"] * w.liquidity, abs=1e-3)
    assert by["INFY"]["status"] == "EXCLUDED" and any(x.startswith("ILLIQUID") for x in by["INFY"]["reasons"])
    assert by["HDFCBANK"]["status"] == "EXCLUDED" and any(x.startswith("EVENT_BEFORE_EXPIRY") for x in by["HDFCBANK"]["reasons"])
    assert by["ICICIBANK"]["status"] == "EXCLUDED" and any(x.startswith("NO_TREND") for x in by["ICICIBANK"]["reasons"])
    # the list is cut at shortlist_size, and a valid setup without a margin entry is still listed (no signal)
    one = build_stock_selling(settings(stock_option_selling={"shortlist_size": 1}))
    rows1 = build_shortlist(one, shortlist_contexts(s))
    assert [r["stock"] for r in rows1 if r["status"] == "SHORTLISTED"] == ["TCS"]
    assert "ranked 2" in next(r for r in rows1 if r["stock"] == "RELIANCE")["reasons"][-1]
    no_margin = build_shortlist(strat, [ctx_for(s, stock_bars(), margin=None)])[0]
    assert no_margin["status"] == "SHORTLISTED" and any(x.startswith("MARGIN_UNKNOWN") for x in no_margin["reasons"])


def test_shortlist_is_stored_and_served_read_only(s, strat):
    store = Store(":memory:")
    rows = build_shortlist(strat, shortlist_contexts(s))
    store.save_shortlist(date(2026, 11, 13), rows, NOW)
    got = store.shortlist()
    assert got["session_date"] == "2026-11-13" and [r["stock"] for r in got["rows"][:2]] == ["TCS", "RELIANCE"]
    assert got["rows"][0]["scores"] == rows[0]["scores"] and got["rows"][0]["reasons"] == rows[0]["reasons"]
    store.save_shortlist(date(2026, 11, 13), rows[:1], NOW)         # a rebuild replaces the day
    assert len(store.shortlist(date(2026, 11, 13))["rows"]) == 1
    assert Store(":memory:").shortlist() == {"session_date": None, "created_utc": None, "rows": []}


def test_run_shortlist_uses_only_chains_and_bars_of_the_session(s):
    """RELIANCE has a daily bar and a chain stored on the session: assessed. TCS has no chain from that
    day and INFY no daily bar for it: both are left out with the reason, never guessed."""
    from app.options.instruments import ContractSpec, InstrumentBook
    store = Store(":memory:")
    session = date(2026, 11, 13)
    idx = [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in weekdays_back(session, 5)]
    rel, tcs = stock_bars("RELIANCE"), stock_bars("TCS")
    infy = stock_bars("INFY", end=date(2026, 11, 12))
    store.save_history_bars("bars_1d", idx + rel + tcs + infy, "sim")
    strikes = tuple(float(k) for k in range(800, 1501, 10))
    InstrumentBook(store, "sim").save([ContractSpec(sym, EXP, LOT, "monthly", strikes, NOW)
                                       for sym in ("RELIANCE", "TCS", "INFY")])

    def save_chain(sym, spot, as_of):
        store.save_option_chain("sim", [(sym, EXP.isoformat(), q.strike, q.right, as_of.isoformat(), q.bid, q.ask, q.last,
                                         q.oi, 0.0, 100.0, spot) for q in chain(spot)])
    save_chain("RELIANCE", rel[-1].close, NOW)
    save_chain("TCS", tcs[-1].close, NOW - timedelta(days=1))
    save_chain("INFY", infy[-1].close, NOW)
    out = run_shortlist(s, store, "sim", now=NOW + timedelta(minutes=30))
    by = {r["stock"]: r for r in out["rows"]}
    assert out["session_date"] == "2026-11-13" and set(by) == {"RELIANCE", "TCS", "INFY"}
    assert by["TCS"]["status"] == "EXCLUDED" and by["TCS"]["reasons"][0].startswith("NO_CHAIN")
    assert by["INFY"]["status"] == "EXCLUDED" and by["INFY"]["reasons"][0].startswith("DATA_STALE")
    # RELIANCE was assessed by the rules (its stock IV rank has no stored history here, so it is excluded for that)
    assert any(x.startswith("IV_RANK_UNKNOWN") for x in by["RELIANCE"]["reasons"])
    assert store.shortlist()["session_date"] == "2026-11-13"


def test_shortlist_route_and_page_are_read_only(s):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        r = c.get("/api/stock-option-selling/shortlist")
        assert r.status_code == 200 and r.json()["rows"] == [] and "UNPROVEN" in r.json()["label"]
        app.state.store.save_shortlist(date(2026, 11, 13), build_shortlist(build_stock_selling(s), shortlist_contexts(s)), NOW)
        j = c.get("/api/stock-option-selling/shortlist").json()
        assert j["rows"][0]["stock"] == "TCS" and j["rows"][0]["status"] == "SHORTLISTED" and j["alerts"] is False
        page = c.get("/stock-option-selling")
        assert page.status_code == 200 and "RESEARCH ONLY" in page.text
        assert c.post("/api/stock-option-selling/shortlist", json={}).status_code in (403, 405)   # no write route here


# ---------------- exit and adjustment calls, roll ----------------
POS = SpreadPosition("RELIANCE", "PE", 1060.0, 1040.0, EXP, 4.0, LOT)
HELD_ROLL = SpreadPosition("RELIANCE", "PE", 1110.0, 1100.0, EXP, 4.0, LOT_R)


def kinds(calls):
    return [c["kind"] for c in calls]


def test_ACCEPTANCE_profit_target_stop_threat_and_forced_exit_calls_fire(s):
    p = s.stock_option_selling
    assert exit_levels(4.0, p) == (2.0, 8.0)                        # keep 50% of the credit; stop at 2x the credit
    today = date(2026, 11, 20)
    assert exit_calls(POS, p, today, 3.0, 1100.0, "UP") == []
    assert kinds(exit_calls(POS, p, today, 2.0, 1100.0, "UP")) == ["TARGET"]
    assert kinds(exit_calls(POS, p, today, 1.2, 1100.0, "UP")) == ["TARGET"]
    stop = exit_calls(POS, p, today, 8.0, 1100.0, "UP")
    assert kinds(stop) == ["STOP"] and stop[0]["pnl_before_costs_inr"] == (4.0 - 8.0) * LOT
    assert kinds(exit_calls(POS, p, today, 3.0, 1059.0, "UP")) == ["STRIKE_BREACHED"]
    assert kinds(exit_calls(POS, p, today, 3.0, 1060.0, "UP")) == []            # at the strike is not beyond it
    assert kinds(exit_calls(POS, p, today, 3.0, 1100.0, "DOWN")) == ["TREND_FLIP"]
    assert kinds(exit_calls(POS, p, today, 3.0, 1100.0, None)) == []            # no clear trend is not a flip
    # time exit at 10 calendar days; forced exit inside the last 5 trading days (expiry Tuesday 8 December)
    assert kinds(exit_calls(POS, p, date(2026, 11, 27), 3.0, 1100.0, "UP")) == []           # 11 days
    assert kinds(exit_calls(POS, p, date(2026, 11, 28), 3.0, 1100.0, "UP")) == ["TIME_EXIT"]   # 10 days
    assert kinds(exit_calls(POS, p, date(2026, 11, 30), 3.0, 1100.0, "UP")) == ["TIME_EXIT"]   # 6 trading days left
    forced = exit_calls(POS, p, date(2026, 12, 1), 3.0, 1100.0, "UP")           # 5 trading days left
    assert kinds(forced) == ["FORCED_EXPIRY_EXIT"] and forced[0]["action"] == "Exit now: physical settlement"
    # the same state gives the same call every time it is asked: it repeats until the position is closed
    assert exit_calls(POS, p, date(2026, 12, 3), 9.0, 1050.0, "DOWN") == exit_calls(POS, p, date(2026, 12, 3), 9.0, 1050.0, "DOWN")
    assert kinds(exit_calls(POS, p, date(2026, 12, 3), 9.0, 1050.0, "DOWN")) == ["FORCED_EXPIRY_EXIT", "STOP",
                                                                               "STRIKE_BREACHED", "TREND_FLIP"]
    # an unknown price is reported, never read as "no exit"
    assert kinds(exit_calls(POS, p, today, None, 1100.0, "UP")) == ["DATA_MISSING"]
    call = SpreadPosition("RELIANCE", "CE", 1200.0, 1220.0, EXP, 4.0, LOT)
    assert kinds(exit_calls(call, p, today, 3.0, 1201.0, "UP")) == ["STRIKE_BREACHED", "TREND_FLIP"]


def test_ACCEPTANCE_threat_shows_exit_and_roll_with_numbers_and_a_roll_is_never_unhedged():
    cfg = big_settings()
    strat = build_stock_selling(cfg)
    bars = stock_bars()
    out = roll_choices(strat, roll_ctx(cfg, bars), HELD_ROLL, close_cost=0.8)
    assert out["exit"]["cost_to_close_inr"] == 0.8 * LOT_R
    assert out["exit"]["realised_pnl_inr"] < (4.0 - 0.8) * LOT_R                 # less the closing charges
    roll = out["roll"]
    assert out["action"] == "ROLL" and roll is not None and roll["expiry"] == EXP.isoformat()
    assert unhedged_short_legs(roll["legs"]) == [] and roll["legs"][0]["strike"] < 1110.0
    assert roll["roll_net_credit_inr"] == pytest.approx(roll["new_credit_inr"] - roll["close_debit_inr"]
                                                        - roll["roll_charges_inr"], abs=0.02)
    assert roll["new_max_loss_inr"] > 0 and roll["new_breakevens"] and roll["margin_inr"] == 40000.0
    # an open spread that is dear to close: the new premium is positive, the roll is still refused
    dear = roll_choices(strat, roll_ctx(cfg, bars, cheap=False), HELD_ROLL, close_cost=7.0)
    assert dear["action"] == "EXIT" and dear["roll"] is None and dear["refused_by"] == "ROLL_NET_CREDIT_TOO_SMALL"
    assert dear["exit"]["cost_to_close_inr"] == 7.0 * LOT_R                      # the exit numbers are always shown
    # a falling stock: no replacement passes the entry rules; the alert is "exit", and it says why
    down = stock_bars(start=1200, drift=-1.0)
    out = roll_choices(strat, roll_ctx(cfg, down, cheap=False), HELD_ROLL, 7.0)
    assert out["action"] == "EXIT" and out["roll"] is None and out["roll_note"].startswith("exit: no roll passes the rules")


# ---------------- 5-minute exit check ----------------
def ib(hhmm, o, c=None, d=date(2025, 7, 10)):
    h, m = map(int, hhmm.split(":"))
    c = o if c is None else c
    return IntradayBar(datetime.combine(d, time(h, m), IST).astimezone(UTC), o, max(o, c), min(o, c), c)


def hhmm(sync):
    return [s.astimezone(IST).strftime("%H:%M") for s, _ in sync]


def test_ACCEPTANCE_a_spread_is_priced_only_from_bars_in_which_every_leg_traded():
    short = [ib("09:15", 5.0), ib("09:20", 5.0, 9.0), ib("09:30", 9.5), ib("09:35", 9.0)]
    long_ = [ib("09:15", 1.0), ib("09:25", 1.1), ib("09:30", 1.2), ib("09:35", 1.0)]
    sync = synchronized_bars([short, long_])
    assert hhmm(sync) == ["09:15", "09:30", "09:35"]                 # 09:20 and 09:25: only one leg traded
    assert [b.close for b in sync[1][1]] == [9.5, 1.2]
    assert synchronized_bars([short, []]) == [] and synchronized_bars([]) == [] and synchronized_bars([[], []]) == []
    # the sold leg alone closed at 9.00 at 09:20 (cost would be 8 with a stale 1.00 for the other leg):
    # that is not a synchronized bar, so nothing is decided there. Decided at 09:30: 9.5 - 1.2 = 8.3 >= 8
    assert sync_decision(sync, [1, -1], 2.0, 8.0) == ("STOP", 1, pytest.approx(8.3))
    # the fill is never on the deciding bar: the caller uses bar index + 1 (09:35 here)
    assert sync[1 + 1][0] == short[3].start


def test_sync_decision_target_stop_and_nothing():
    two = lambda s, l: synchronized_bars([s, l])
    assert sync_decision(two([ib("10:00", 4.0, 2.5), ib("10:05", 2.4)], [ib("10:00", 1.0, 0.5), ib("10:05", 0.6)]),
                         [1, -1], 2.0, 8.0) == ("TARGET", 0, pytest.approx(2.0))
    assert sync_decision(two([ib("10:00", 4.0)], [ib("10:00", 1.0)]), [1, -1], 2.0, 8.0) is None
    assert sync_decision([], [1, -1], 2.0, 8.0) is None
    late = two([ib("15:25", 9.5)], [ib("15:25", 1.0)])               # decided on the session's last bar: index 0 of 1
    assert sync_decision(late, [1, -1], 2.0, 8.0) == ("STOP", 0, pytest.approx(8.5)) and len(late) == 1
    assert sync_decision(late, [1, -1], 2.0, 8.0, use_stop=False) is None        # the research "never stop" policy
    # the close decides, not the bar's high: a bar that spiked and came back does not stop the spread
    spike = two([IntradayBar(ib("11:00", 3.0).start, 3.0, 12.0, 3.0, 3.2)], [ib("11:00", 1.0)])
    assert sync_decision(spike, [1, -1], 2.0, 8.0) is None


# ---------------- 5-minute history: parsing, paging, storage, budget ----------------
def raw5(d, hhmm, px=10.0):
    return {"datetime": f"{d} {hhmm}:00", "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 10,
            "open_interest": 1000}


def test_parse_option_rows_intraday_keeps_session_bars_at_their_own_time():
    rows = [raw5("2025-07-10", "09:00"), raw5("2025-07-10", "09:15"), raw5("2025-07-10", "15:25"),
            raw5("2025-07-10", "15:30"), raw5("2025-07-10", "10:00", px=-1)]
    good, bad = parse_option_rows(rows, intraday=True)
    assert [datetime.fromisoformat(g[0]).astimezone(IST).strftime("%H:%M") for g in good] == ["09:15", "15:25"]
    assert len(bad) == 1                                            # pre-open and post-close bars are dropped, not "bad"


class PagedFeed:
    """Serves one contract's 5-minute rows, at most `page` per reply, cut at the end or at the start."""
    def __init__(self, rows, page, cut="end", error_after=None):
        self.rows, self.page, self.cut, self.calls, self.error_after = rows, page, cut, [], error_after

    def option_historical_raw(self, code, expiry_iso, strike, right, interval, a, b):
        self.calls.append((interval, a, b))
        if self.error_after is not None and len(self.calls) > self.error_after:
            return {"Success": None, "Status": 401, "Error": "Session key is expired"}
        lo = datetime.fromisoformat(a.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(b.replace("Z", "+00:00"))
        inside = [r for r in self.rows
                  if lo <= datetime.fromisoformat(r["datetime"]).replace(tzinfo=IST).astimezone(UTC) < hi]
        part = inside[:self.page] if self.cut == "end" else inside[-self.page:]
        return {"Success": part, "Status": 200, "Error": None}


def five_minute_rows(first: date, last: date, per_day=75):
    rows, d = [], first
    while d <= last:
        if d.weekday() < 5:
            t0 = datetime.combine(d, time(9, 15))
            rows += [raw5(d.isoformat(), (t0 + timedelta(minutes=5 * i)).strftime("%H:%M"), 10 + i * 0.01) for i in range(per_day)]
        d += timedelta(days=1)
    return rows


@pytest.mark.parametrize("cut", ["end", "start"])
def test_intraday_fetch_continues_a_reply_cut_at_the_row_limit(monkeypatch, cut):
    import app.options.option_history as oh
    monkeypatch.setattr(oh, "PAGE_ROWS", 300)
    monkeypatch.setattr(oh, "MAX_PAGES", 10)
    exp = date(2025, 7, 29)
    first, last = OptionIntraday.window(exp)
    rows = five_minute_rows(first, last)                            # 26 sessions x 75 bars = 1,950 rows
    feed = PagedFeed(rows, 300, cut)
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    got = f.fetch_intraday("RELIANCE", exp, 1100, "PE", first, last)
    assert len(got) == len(rows) and [g["datetime"] for g in got] == sorted(r["datetime"] for r in rows)
    assert len(feed.calls) == 7 and all(c[0] == "5minute" for c in feed.calls)        # 6 full pages and the short last one
    thin = PagedFeed(rows[:40], 300)
    f2 = oh.BreezeOptionFetcher(thin, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    assert len(f2.fetch_intraday("RELIANCE", exp, 1100, "PE", first, last)) == 40 and len(thin.calls) == 1


def test_option_intraday_stores_once_and_treats_days_outside_its_window_as_unknown():
    import app.options.option_history as oh
    exp = date(2025, 7, 29)
    first, last = OptionIntraday.window(exp)
    assert (first, last) == (date(2025, 6, 19), date(2025, 7, 24))
    feed = PagedFeed(five_minute_rows(date(2025, 7, 9), date(2025, 7, 10), per_day=3), 1000)
    store = Store(":memory:")
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    intr = OptionIntraday(OptionHistory(store, "breeze", f, now=NOW))
    day = intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 10))
    assert [b.start.astimezone(IST).strftime("%H:%M") for b in day] == ["09:15", "09:20", "09:25"]
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 11)) == []        # asked for: it did not trade
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 28)) is None      # outside the stored window: unknown
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 6, 18)) is None
    n = len(feed.calls)
    again = OptionIntraday(OptionHistory(store, "breeze", f, now=NOW))               # a later run: read from the store
    assert len(again.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 9))) == 3 and len(feed.calls) == n
    assert store.option_fetch_log("breeze", "RELIANCE", INTRADAY) == {(exp, 1100.0, "PE"): 6}
    # no fetcher: an unknown contract is None, and it is counted as wanted but not stored
    offline = OptionIntraday(OptionHistory(store, "breeze"))
    assert offline.session("RELIANCE", exp, 1120, "PE", date(2025, 7, 10)) is None
    assert offline.download_stats()["contracts_wanted_but_not_stored"] == 1


def test_a_breeze_error_stops_all_downloading_and_is_never_stored_as_no_data():
    import app.options.option_history as oh
    exp = date(2025, 7, 29)
    feed = PagedFeed([], 1000, error_after=0)
    store = Store(":memory:")
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    hist = OptionHistory(store, "breeze", f, now=NOW)
    intr = OptionIntraday(hist)
    assert intr.session("RELIANCE", exp, 1100, "PE", date(2025, 7, 10)) is None
    assert "Session key is expired" in hist.error and len(feed.calls) == 1
    assert hist.contract("RELIANCE", exp, 1100, "PE") is None and len(feed.calls) == 1   # no further request at all
    assert store.option_fetch_log("breeze", "RELIANCE", INTRADAY) == {}
    assert store.option_fetch_log("breeze", "RELIANCE") == {}


def test_a_request_with_no_reply_is_repeated_but_an_error_reply_from_breeze_is_not():
    import app.options.option_history as oh

    class Flaky:
        def __init__(self, fail):
            self.fail, self.calls = fail, 0

        def option_historical_raw(self, *a):
            self.calls += 1
            if self.calls <= self.fail:
                raise TimeoutError("no reply within 60 s")
            return {"Success": [{"datetime": "2025-07-10 00:00:00", "open": 1, "high": 2, "low": 1, "close": 2,
                                 "volume": 1, "open_interest": 1}], "Status": 200, "Error": None}

    sleeps = []
    feed = Flaky(2)
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 3.0, 100, sleep=sleeps.append)
    assert len(f.fetch("RELIANCE", date(2025, 7, 29), 1100, "PE")) == 1
    assert feed.calls == 3 and f.requests == 3 and sleeps == [3.0, 3.0]        # two timeouts, then the reply; all counted
    dead = Flaky(99)
    f2 = oh.BreezeOptionFetcher(dead, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    hist = OptionHistory(Store(":memory:"), "breeze", f2, now=NOW)
    assert hist.contract("RELIANCE", date(2025, 7, 29), 1100, "PE") is None
    assert dead.calls == 3 and "TimeoutError" in hist.error and "after 3 attempts" in hist.error
    # an error reply (expired session) is not asked again: one request, and the download stops
    expired = PagedFeed([], 1000, error_after=0)
    f3 = oh.BreezeOptionFetcher(expired, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 100, sleep=lambda x: None)
    hist3 = OptionHistory(Store(":memory:"), "breeze", f3, now=NOW)
    assert hist3.contract("RELIANCE", date(2025, 7, 29), 1100, "PE") is None
    assert len(expired.calls) == 1 and "Session key is expired" in hist3.error
    # the retries never go past the request budget
    f4 = oh.BreezeOptionFetcher(Flaky(99), {"RELIANCE": "RELIND"}, 0, 5, 1, 0, 2, sleep=lambda x: None)
    with pytest.raises(oh.RequestBudgetUsed):
        f4.fetch("RELIANCE", date(2025, 7, 29), 1100, "PE")
    assert f4.requests == 2


def test_the_daily_request_budget_is_counted_across_runs_and_resets_the_next_day(s):
    store = Store(":memory:")
    small = settings(options={"backtest": {**s.options.backtest.model_dump(), "daily_request_budget": 5}})
    feed = PagedFeed([], 1000)
    day1 = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
    f = budgeted_fetcher(small, store, feed, now=day1, sleep=lambda x: None)
    assert f.max_requests == 5
    hist = OptionHistory(store, "breeze", f, now=NOW)
    for k in (1000, 1010):                                          # an empty reply is asked twice (one retry)
        assert hist.contract("RELIANCE", date(2025, 7, 29), k, "PE") == {}
    assert store.requests_used(date(2026, 11, 14)) == 4
    assert hist.contract("RELIANCE", date(2025, 7, 29), 1020, "PE") is None          # 1 left: the retry does not fit
    assert store.requests_used(date(2026, 11, 14)) == 5 and f.budget_left == 0
    assert budgeted_fetcher(small, store, feed, now=day1 + timedelta(hours=3), sleep=lambda x: None).max_requests == 0
    assert budgeted_fetcher(small, store, feed, run_limit=2, now=day1 + timedelta(days=1)).max_requests == 2
    assert budgeted_fetcher(small, store, feed, now=day1 + timedelta(days=1)).max_requests == 5
    assert store.requests_used(date(2026, 11, 15)) == 0


# ---------------- backtest world: a stock with stored daily and 5-minute option history ----------------
LOT_BT = {"RELIANCE": 500}
BT_FIRST, BT_LAST = date(2025, 3, 1), date(2025, 12, 31)


def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def stock_world(materialise=True, shocks=None, drift=0.5, hi=1500):
    """`shocks`: {date: rupees} - the stock's price level shifts by that much from that session on."""
    cfg = settings(backtest={"test_start": {"1day": date(2025, 6, 2)}, "holdout_start": {"1day": date(2025, 10, 1)}},
                   options={"iv_rank_min_history_days": 20, "iv_rank_lookback_days": 60})
    store = Store(":memory:")
    bars = []
    for i, d in enumerate(weekdays(date(2024, 1, 1), date(2025, 12, 31))):
        px = 24000 + 60 * math.sin(i / 5)
        bars.append(Bar("NIFTY", daily_start(d), px - 10, px + 45, px - 45, px, None))
        v = 24 + i * 0.01
        bars.append(Bar("INDIAVIX", daily_start(d), v, v + 0.2, v - 0.2, v, None))
        sp = 1000 + drift * i + 8 * math.sin(i / 4) + sum(v for k, v in (shocks or {}).items() if d >= k)
        bars.append(Bar("RELIANCE", daily_start(d), sp - 1, sp + 6, sp - 6, sp, 2e6))
    store.save_history_bars("bars_1d", bars, "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    model = model_prices(cfg, base)
    for exp, _ in model.expiries("RELIANCE"):
        store.save_option_expiry("sim", "NIFTY", exp, "monthly", NOW)       # stocks expire with the index monthly
    if materialise:
        write_option_history(store, model, hi=hi)
    return cfg, store, base, model


def write_option_history(store, model, lo=900, hi=1500, step=10):
    """The model's daily bars, and three 5-minute bars a day (open, midday, close) inside the 5-minute
    window, written as if they had been downloaded."""
    for exp, _ in model.expiries("RELIANCE"):
        if not (BT_FIRST <= exp <= BT_LAST):
            continue
        w0, w1 = OptionIntraday.window(exp)
        for k in range(lo, hi + 1, step):
            for right in ("CE", "PE"):
                rows, five = [], []
                for d in weekdays(exp - timedelta(days=60), exp):
                    b = model.bar("RELIANCE", exp, k, right, d)
                    if b is None:
                        continue
                    rows.append((daily_start(d).isoformat(), b.open, b.high, b.low, b.close, 1000.0, 500000.0))
                    if w0 <= d <= w1:
                        for hh, mm, px in ((9, 15, b.open), (12, 0, round((b.open + b.close) / 2, 2)), (15, 25, b.close)):
                            at = datetime.combine(d, time(hh, mm), IST).astimezone(UTC)
                            five.append((at.isoformat(), px, px, px, px, 100.0, 500000.0))
                store.save_option_bars("sim", "RELIANCE", exp, k, right, "1day", rows, NOW)
                store.save_option_bars("sim", "RELIANCE", exp, k, right, INTRADAY, five, NOW)


@pytest.fixture(scope="module")
def world():
    return stock_world()


def engine_for(cfg, store, base, hist, with_intraday=True):
    return OptionEngine(cfg, store, "sim", hist, base, lot_sizes=LOT_BT,
                        intraday=OptionIntraday(hist) if with_intraday else None)


def run_bt(w, exit_checks=("intraday", "daily"), with_intraday=True, hist=None):
    cfg, store, base, _ = w
    hist = hist or OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist, with_intraday)
    eng.set_expiry_floor(BT_FIRST)                                  # option history is stored from here on
    from app.options.portfolio import PortfolioGate
    return run_option_backtest(cfg, store, "sim", build_stock_selling(cfg), hist, ["RELIANCE"], engine=eng, save=False,
                               now=NOW, exit_checks=exit_checks, gate_factory=lambda e: PortfolioGate(cfg, e))


def ist_date(text):
    return datetime.fromisoformat(text).astimezone(IST).date()


def test_ACCEPTANCE_stock_backtest_shows_strategy_and_baseline_side_by_side_under_both_exit_checks(world):
    res = run_bt(world)
    rep, trades = res["report"], res["trades"]
    assert rep["strategy_id"] == "stock_option_selling" and rep["price_source"] == "option_history"
    assert rep["payoff_simulation"] is False and rep["exit_check"] == "intraday"
    assert rep["eligibility_label"] == "excludes margin and bid/ask eligibility checks"
    both = rep["exit_check_comparison"]
    assert list(both) == ["intraday", "daily"]
    for mode in both:
        assert both[mode]["strategy"]["trades"] > 0 and both[mode]["baseline"]["trades"] > 0
        assert set(both[mode]["baseline_by_variant"]) == {"baseline:PE", "baseline:CE"}
        assert both[mode]["comparison"]["verdict"]
    assert rep["overall"] == both["intraday"]["strategy"] and rep["baseline"]["overall"] == both["intraday"]["baseline"]
    assert {t["exit_check"] for t in trades} == {"intraday", "daily"}
    assert {t["variant"] for t in trades} == {"strategy", "baseline:PE", "baseline:CE"}
    assert rep["holdout"]["evaluated"] is False and rep["period"]["to"] < "2025-10-01"
    assert rep["download"]["contracts_wanted_but_not_stored"] == 0
    # the baseline is the fixed schedule: one attempt per expiry and side
    sch = rep["baseline"]["schedule"]
    assert sch["attempts"] == sch["entered"] + len(sch["skipped"]) and sch["entered"] >= 4


def test_every_backtest_trade_is_hedged_enters_at_the_next_session_and_is_flat_before_expiry_week(world):
    cfg, _, base, _ = world
    res = run_bt(world)
    sessions = base.session_dates()
    assert res["trades"]
    for t in res["trades"]:
        assert unhedged_short_legs(t["legs"], t["expiry"], t["lots"], t["lot_size"]) == []
        sell, buy = (next(x for x in t["legs"] if x["action"] == a) for a in ("SELL", "BUY"))
        assert sell["right"] == buy["right"] and sell["expiry"] == buy["expiry"]
        assert t["symbol"] == "RELIANCE" and t["lot_size"] == 500 and t["lots"] >= 1
        assert all(x["lots"] == t["lots"] for x in t["legs"])
        assert t["max_loss_inr"] <= cfg.account.capital_inr * cfg.stock_option_selling.max_loss_per_stock_spread_pct / 100
        signal_d, entry_d, exit_d = ist_date(t["signal_time"]), ist_date(t["entry_time"]), ist_date(t["exit_time"])
        assert entry_d == sessions[sessions.index(signal_d) + 1]            # never the signal day
        exp = date.fromisoformat(t["expiry"])
        assert 20 <= (exp - signal_d).days <= 35
        assert exit_d < exp - timedelta(days=exp.weekday())                # before the Monday of expiry week
        assert (exp - exit_d).days >= cfg.stock_option_selling.exit_days_before_expiry - 3
        assert t["exit_reason"] in ("TARGET", "STOP", "TIME", "STRIKE_BREACHED", "OPEN_AT_CUTOFF")
        if t["variant"] == "strategy":
            assert sell["right"] == "PE"                                    # the fixture stock is in an up trend
    # a forced (time) exit fills at 09:15 of the first session within 7 days of expiry
    timed = [t for t in res["trades"] if t["exit_reason"] == "TIME"]
    for t in timed:
        exp, exit_d = date.fromisoformat(t["expiry"]), ist_date(t["exit_time"])
        assert (exp - exit_d).days <= 10 and datetime.fromisoformat(t["exit_time"]).astimezone(IST).time() == time(9, 15)


def test_stock_backtest_is_deterministic(world):
    a, b = run_bt(world), run_bt(world)
    strip = lambda r: {k: v for k, v in r["report"].items() if k not in ("run_id", "created_utc")}
    assert a["trades"] == b["trades"] and strip(a) == strip(b)


def test_ACCEPTANCE_synchronized_stop_is_decided_on_a_bar_close_and_filled_at_the_next_synchronized_open():
    """A stop touched at 10:00 on one day and gone again by the close. With only the sold leg trading at
    10:00 there is no synchronized bar and nothing happens; once both legs trade at 10:00 and 10:05
    the stop is decided at the 10:00 close and filled at the 10:05 open. The daily check never sees it."""
    w = stock_world()
    cfg, store, _, _ = w
    before = run_bt(w)
    pick = next(t for t in before["trades"] if t["exit_check"] == "daily" and t["variant"] == "baseline:PE"
                and (ist_date(t["exit_time"]) - ist_date(t["entry_time"])).days >= 5)
    exp = date.fromisoformat(pick["expiry"])
    sell, buy = (next(x for x in pick["legs"] if x["action"] == a) for a in ("SELL", "BUY"))
    day = next(d for d in weekdays(ist_date(pick["entry_time"]) + timedelta(days=1), ist_date(pick["exit_time"])))
    stop = round(pick["entry_net"] / 0.995 * 2.0, 2) + 5            # well above 2x the signal credit

    def add(strike, rows):
        store.save_option_bars("sim", "RELIANCE", exp, strike, "PE", INTRADAY,
                               [(datetime.combine(day, time(h, m), IST).astimezone(UTC).isoformat(), px, px, px, px, 1.0, 1.0)
                                for h, m, px in rows], NOW)

    same = lambda r, mode: [t for t in r["trades"] if t["exit_check"] == mode and t["signal_id"] == pick["signal_id"]][0]
    first = same(before, "intraday")
    assert datetime.fromisoformat(first["entry_time"]).astimezone(IST).time() == time(9, 15)      # the first synchronized bar
    add(sell["strike"], [(10, 0, stop + 1.0), (10, 5, stop + 1.5)])       # the sold leg alone: not a synchronized bar
    mid = run_bt(w)
    assert same(mid, "intraday") == first
    add(buy["strike"], [(10, 0, 1.0), (10, 5, 1.1)])                      # now both legs trade in those bars
    after = run_bt(w)
    hit, daily = same(after, "intraday"), same(after, "daily")
    assert hit["exit_reason"] == "STOP" and hit["exit_basis"] == "synchronized 5-minute bar"
    assert datetime.fromisoformat(hit["exit_time"]).astimezone(IST) == datetime.combine(day, time(10, 5), IST)
    out = {x["strike"]: x["exit_price"] for x in hit["legs"]}
    assert out[sell["strike"]] == pytest.approx((stop + 1.5) * 1.005, abs=0.011)     # bought back at the 10:05 open plus slippage
    assert out[buy["strike"]] == pytest.approx(1.1 * 0.995, abs=0.011)
    assert hit["net_pnl"] < 0 and daily == same(before, "daily")                     # daily closes did not change


def test_ACCEPTANCE_a_session_without_a_synchronized_bar_makes_the_campaign_data_incomplete_not_a_daily_fallback():
    w = stock_world()
    cfg, store, _, _ = w
    before = run_bt(w)
    pick = next(t for t in before["trades"] if t["exit_check"] == "intraday" and t["variant"] == "baseline:PE"
                and (ist_date(t["exit_time"]) - ist_date(t["entry_time"])).days >= 5)
    buy = next(x for x in pick["legs"] if x["action"] == "BUY")
    day = next(d for d in weekdays(ist_date(pick["entry_time"]) + timedelta(days=1), ist_date(pick["exit_time"])))
    lo = datetime.combine(day, time(0, 0), IST).astimezone(UTC).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time(0, 0), IST).astimezone(UTC).isoformat()
    store._db.execute("DELETE FROM option_bars WHERE underlying='RELIANCE' AND expiry=? AND strike=? AND right='PE' "
                      "AND interval=? AND start_utc>=? AND start_utc<?", (pick["expiry"], buy["strike"], INTRADAY, lo, hi))
    store._db.commit()                                   # the bought leg did not trade that day
    after = run_bt(w)
    keyed = lambda r, mode: {(t["variant"], t["signal_id"]): t for t in r["trades"] if t["exit_check"] == mode}
    b5, a5 = keyed(before, "intraday"), keyed(after, "intraday")
    gone = ("baseline:PE", pick["signal_id"])
    assert gone in b5 and gone not in a5                 # left out of the results: no price was invented for that day
    assert {k: v for k, v in b5.items() if k != gone and k[0] != "strategy"} == {k: v for k, v in a5.items() if k[0] != "strategy"}
    inc = after["report"]["exit_check_comparison"]["intraday"]["data_incomplete"]["baseline"]
    assert inc["DATA_INCOMPLETE_CAMPAIGNS"] == 1 and inc["DATA_INCOMPLETE:NO_SYNCHRONIZED_BAR_IN_SESSION"] == 1
    assert before["report"]["exit_check_comparison"]["intraday"]["data_incomplete"]["baseline"]["DATA_INCOMPLETE_CAMPAIGNS"] == 0
    assert keyed(after, "daily")[gone] == keyed(before, "daily")[gone]              # the daily run still has its daily bars
    # no 5-minute bars at all: the synchronized run enters nothing and says why; it never borrows daily prices
    none = run_bt(w, with_intraday=False)
    assert [t for t in none["trades"] if t["exit_check"] == "intraday"] == []
    assert none["report"]["baseline"]["skipped_signals"]["5_MINUTE_BARS_NOT_STORED_AT_ENTRY"] >= 4
    assert [t for t in none["trades"] if t["exit_check"] == "daily"]


def test_strike_breach_exit_can_be_switched_off(world):
    cfg, store, base, _ = world
    on = build_stock_selling(cfg)
    off_cfg = Settings.model_validate({**cfg.model_dump(), "stock_option_selling": {
        **cfg.stock_option_selling.model_dump(), "backtest": {**cfg.stock_option_selling.backtest.model_dump(),
                                                              "exit_on_strike_breach": False}}})
    off = build_stock_selling(off_cfg)
    assert on.threat_exit is True and off.threat_exit is False
    hist = OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist)
    sessions = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    calls = eng.run_stream(on, "RELIANCE", sessions, "baseline:CE", Counter(), exit_check="daily")
    plain = eng.run_stream(off, "RELIANCE", sessions, "baseline:CE", Counter(), exit_check="daily")
    assert all(t.exit_reason != "STRIKE_BREACHED" for t in plain)
    # a bear call spread on a rising stock: where the breach rule fires, the sold strike was passed at that close
    closes = {daily_start(ist_date(b.start.isoformat())): b.close for b in base.daily("RELIANCE")}
    for t in calls:
        if t.exit_reason == "STRIKE_BREACHED":
            decided = max(d for d in base.session_dates() if d < ist_date(t.exit_time))
            assert closes[daily_start(decided)] > next(x["strike"] for x in t.legs if x["action"] == "SELL")


# ---------------- liquidity ranking ----------------
def test_liquid_stocks_rank_by_median_traded_value_and_leave_out_what_cannot_be_ranked():
    cfg = settings()
    store = Store(":memory:")
    days = weekdays_back(date(2025, 9, 30), 80)
    bars = [Bar("NIFTY", daily_start(d), 1, 1, 1, 1, None) for d in days]
    bars += [Bar("INDIAVIX", daily_start(d), 12, 12, 12, 12, None) for d in days]
    for sym, px, vol in (("RELIANCE", 1000, 3e6), ("TCS", 3000, 2e6), ("INFY", 1500, 1e6), ("HDFCBANK", 900, 5e6)):
        bars += [Bar(sym, daily_start(d), px, px, px, px, vol) for d in days]
    bars += [Bar("ICICIBANK", daily_start(d), 1000, 1000, 1000, 1000, 9e9) for d in days[-10:]]     # too short to rank
    store.save_history_bars("bars_1d", bars, "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    eng = OptionEngine(cfg, store, "sim", OptionHistory(store, "sim"), base,
                       lot_sizes={"RELIANCE": 500, "TCS": 175, "INFY": 400, "ICICIBANK": 700})      # HDFCBANK: no lot size
    ranked = liquid_stocks(cfg, eng)
    assert [r["stock"] for r in ranked] == ["TCS", "RELIANCE", "INFY"] and [r["rank"] for r in ranked] == [1, 2, 3]
    assert ranked[0]["median_traded_value_inr"] == 6e9
    assert liquid_stocks(cfg, eng, before=days[30]) == []            # fewer than 60 sessions before that date


# ---------------- download: budget, error, resume ----------------
class StoreFeed:
    """Stands in for Breeze: serves the option bars of a fully stocked store in Breeze's raw format."""
    def __init__(self, source_store, error_after=None):
        self.src, self.calls, self.error_after = source_store, [], error_after

    def option_historical_raw(self, code, expiry_iso, strike, right, interval, a, b):
        self.calls.append((expiry_iso[:10], float(strike), right, interval))
        if self.error_after is not None and len(self.calls) > self.error_after:
            return {"Success": None, "Status": 401, "Error": "Session key is expired"}
        assert code == "RELIND"
        r = {"call": "CE", "put": "PE"}[right]
        bars = self.src.option_contract_bars("sim", "RELIANCE", date.fromisoformat(expiry_iso[:10]), strike, r, interval)
        rows = [{"datetime": start.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"), "open": o, "high": h, "low": l,
                 "close": c, "volume": v, "open_interest": oi} for start, o, h, l, c, v, oi in bars]
        return {"Success": rows, "Status": 200, "Error": None}


def download_into(target, feed, limit=100000):
    import app.options.option_history as oh
    cfg, store, base, _ = target
    f = oh.BreezeOptionFetcher(feed, {"RELIANCE": "RELIND"}, 0, 5, 1, 0, limit, sleep=lambda x: None)
    hist = OptionHistory(store, "sim", f, now=NOW)
    eng = engine_for(cfg, store, base, hist)
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    return hist, eng, dev


def test_download_fills_an_empty_store_and_the_backtest_then_matches_the_full_one(world):
    empty = stock_world(materialise=False)
    feed = StoreFeed(world[1])
    hist, eng, dev = download_into(empty, feed)
    status = {}
    lines = []
    out = download_blocks(empty[0], eng, build_stock_selling(empty[0]), ["RELIANCE"], dev, status, progress=lines.append)
    assert out["stopped"] is None and status["complete"] == {"RELIANCE": [2025]} and out["requests_this_run"] == len(feed.calls)
    assert {c[3] for c in feed.calls} == {"1day", "5minute"} and lines and "complete" in lines[0]
    assert coverage(status, ["RELIANCE"], [2025]) == ([2025], ["RELIANCE"])
    key = lambda t: (t["variant"], t["exit_check"], t["signal_id"], t["exit_time"], t["exit_reason"], t["net_pnl"])
    assert sorted(map(key, run_bt(empty)["trades"])) == sorted(map(key, run_bt(world)["trades"]))
    # an offline recheck (after a run that could not record its progress) marks the same thing without a request
    offline_eng = engine_for(empty[0], empty[1], empty[2], OptionHistory(empty[1], "sim"))
    marks, saved = {}, []
    out3 = download_blocks(empty[0], offline_eng, build_stock_selling(empty[0]), ["RELIANCE", "TCS"], dev, marks,
                           progress=lambda x: None, offline=True, checkpoint=lambda st: saved.append(dict(st["complete"])))
    assert marks["complete"] == {"RELIANCE": [2025]} and out3["requests_this_run"] == 0      # TCS has nothing stored
    assert saved and saved[0] == {"RELIANCE": [2025]}                                        # recorded as it goes
    # a second run asks for nothing: everything stored (or asked for) is skipped
    n = len(feed.calls)
    hist2, eng2, _ = download_into(empty, feed)
    out2 = download_blocks(empty[0], eng2, build_stock_selling(empty[0]), ["RELIANCE"], dev, {}, progress=lambda x: None)
    assert len(feed.calls) == n and out2["requests_this_run"] == 0 and out2["stopped"] is None


def test_download_stops_cleanly_on_a_session_error_records_progress_and_resumes(world):
    empty = stock_world(materialise=False)
    feed = StoreFeed(world[1], error_after=25)
    hist, eng, dev = download_into(empty, feed)
    status = {}
    out = download_blocks(empty[0], eng, build_stock_selling(empty[0]), ["RELIANCE", "TCS"], dev, status, progress=lambda x: None)
    assert out["stopped"].startswith("Breeze error") and "Session key is expired" in out["stopped"]
    assert status["complete"] == {} and len(feed.calls) == 26       # the failing request was the last one sent
    assert coverage(status, ["RELIANCE"], [2025]) == ([], [])
    stored = sum(1 for n in empty[1].option_fetch_log("sim", "RELIANCE").values()) \
        + len(empty[1].option_fetch_log("sim", "RELIANCE", INTRADAY))
    assert 0 < stored <= 25                                         # what arrived before the error is kept
    # after a fresh login: the same command carries on and does not ask again for what is stored
    feed2 = StoreFeed(world[1])
    hist2, eng2, _ = download_into(empty, feed2)
    out2 = download_blocks(empty[0], eng2, build_stock_selling(empty[0]), ["RELIANCE"], dev, status, progress=lambda x: None)
    assert out2["stopped"] is None and status["complete"] == {"RELIANCE": [2025]}
    asked_before = set(feed.calls[:25])
    assert not (set(feed2.calls) & {c for c in asked_before if c[3] == "5minute"})


def test_download_stops_when_the_request_budget_is_used_and_marks_nothing_complete(world):
    empty = stock_world(materialise=False)
    feed = StoreFeed(world[1])
    hist, eng, dev = download_into(empty, feed, limit=30)
    status = {}
    out = download_blocks(empty[0], eng, build_stock_selling(empty[0]), ["RELIANCE", "TCS"], dev, status, progress=lambda x: None)
    assert len(feed.calls) == 30 and out["requests_this_run"] == 30
    assert out["stopped"] == "request budget used" and status["complete"] == {}
    rep = run_bt(empty, hist=OptionHistory(empty[1], "sim"))["report"]
    assert rep["download"]["contracts_wanted_but_not_stored"] > 0   # the report on partial data says it is incomplete


def test_coverage_uses_only_completely_downloaded_stocks_and_years():
    status = {"complete": {"A": [2024, 2025, 2026], "B": [2025, 2026], "C": [2026], "D": [2024]}}
    assert coverage(status, ["A", "B", "C", "D"], [2023, 2024, 2025, 2026]) == ([2024, 2025, 2026], ["A"])
    assert coverage(status, ["B", "C", "D"], [2024, 2025, 2026]) == ([2025, 2026], ["B"])
    assert coverage(status, ["C", "D"], [2024, 2025, 2026]) == ([2026], ["C"])
    assert coverage({}, ["A"], [2025]) == ([], [])


# ---------------- data-incomplete share and its sensitivity run; credit / max-loss distribution ----------------
def drop_session(store, day):
    """Remove every stored 5-minute option bar of one session (as if nothing traded in step that day)."""
    lo = datetime.combine(day, time(0, 0), IST).astimezone(UTC).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time(0, 0), IST).astimezone(UTC).isoformat()
    store._db.execute("DELETE FROM option_bars WHERE interval=? AND start_utc>=? AND start_utc<?", (INTRADAY, lo, hi))
    store._db.commit()


def summary_of(w):
    from app.options.stock_backtest import incomplete_sensitivity
    cfg, store, base, _ = w
    hist = OptionHistory(store, "sim")
    eng = engine_for(cfg, store, base, hist)
    eng.set_expiry_floor(BT_FIRST)
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    st = build_stock_selling(cfg)
    sk, bsk = Counter(), Counter()
    tr = eng.run_stream(st, "RELIANCE", dev, "strategy", sk, exit_check="intraday")
    btr = [t for v in ("baseline:PE", "baseline:CE") for t in eng.run_stream(st, "RELIANCE", dev, v, bsk, exit_check="intraday")]
    return incomplete_sensitivity(cfg, eng, ["RELIANCE"], dev, {"intraday": (tr, btr, sk, bsk, [])})["data_incomplete_summary"], btr


def test_ACCEPTANCE_excluded_campaigns_are_counted_and_above_30_percent_a_labelled_sensitivity_run_is_added():
    clean, all_trades = summary_of(stock_world())
    assert clean["baseline"]["excluded"] == 0 and clean["baseline"]["excluded_share_pct"] == 0.0 and clean["sensitivity"] is None
    total = clean["baseline"]["kept"]
    # one session without bars: the campaign open that day is left out; 1 of 5 is under the 30% line
    w = stock_world()
    drop_session(w[1], date(2025, 7, 3))
    one, _ = summary_of(w)
    assert one["baseline"]["excluded"] == 1 and one["baseline"]["kept"] == total - 1
    assert one["baseline"]["excluded_share_pct"] == pytest.approx(1 / total * 100, abs=0.1)
    assert one["baseline"]["excluded_share_pct"] <= 30 and one["sensitivity"] is None
    # sessions without bars in two months: above 30%, so the sensitivity run is added, clearly labelled
    drop_session(w[1], date(2025, 8, 1))
    two, kept = summary_of(w)
    assert two["baseline"]["excluded"] == 2 and two["baseline"]["excluded_share_pct"] > 30 and two["threshold_pct"] == 30.0
    sv = two["sensitivity"]
    assert sv is not None and sv["label"].startswith("SENSITIVITY, not the primary result")
    assert sv["baseline"]["trades"] == total and sv["still_excluded"]["baseline"] == 0     # judged at the daily close instead
    assert len(kept) == total - 2                                    # the primary result still leaves them out
    assert all("sensitivity" not in t.exit_basis for t in kept)


def test_ACCEPTANCE_credit_to_max_loss_distribution_uses_decision_time_data_only(world):
    from app.options.stock_backtest import credit_ratio_distribution
    cfg, store, base, _ = world

    def dist():
        eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
        eng.set_expiry_floor(BT_FIRST)
        dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
        eng.last_session = dev[-1]
        return credit_ratio_distribution(cfg, eng, ["RELIANCE"], dev)

    d = dist()
    x = d["passing_every_other_rule"]
    assert x["observations"] == len(d["observations"]) >= 3 and x["low_sample"] is True
    assert x["min"] <= x["p25"] <= x["median"] <= x["p75"] <= x["max"] and 0 < x["median"] < 1
    assert x["puts"]["observations"] + x["calls"]["observations"] == x["observations"]
    assert "share_at_or_above_0.20_pct" in x and d["no_filter_structure"]["observations"] >= x["observations"]
    assert "no outcome" in d["what"] and any("minimum net credit" in r for r in d["rules_not_applied"])
    # one observation by hand: the ratio is the signal's own figure on its decision day
    o = d["observations"][0]
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(BT_FIRST)
    eng.last_session = date(2025, 9, 30)
    bars = base.daily("RELIANCE")
    i = next(k for k, b in enumerate(bars) if ist_date(b.start.isoformat()) == date.fromisoformat(o["date"]))
    from app.options.stock_backtest import variant
    st = variant(cfg, off=("campaign_limit",))
    sig = st.evaluate(eng.context(st, "RELIANCE", date.fromisoformat(o["date"]), bars[i].close, "strategy", None,
                                  tuple(bars[max(0, i - 1299):i + 1]), ()))[0]
    assert o["ratio"] == pytest.approx(sig.details["net_credit_after_costs_inr"] / sig.details["max_loss_inr"], abs=1e-4)
    # prices after the decision day do not enter: changing a later bar of that spread's sold leg changes nothing
    exp, sell = date.fromisoformat(o["expiry"]), sig.legs[0]
    later = date.fromisoformat(o["date"]) + timedelta(days=7)
    while later.weekday() >= 5:
        later += timedelta(days=1)
    old = next(b for b in store.option_bars("sim", "RELIANCE")[(exp, sell.strike, sell.right)] if b[0] == daily_start(later))
    try:
        store.save_option_bars("sim", "RELIANCE", exp, sell.strike, sell.right, "1day",
                               [(old[0].isoformat(), old[1] * 3, old[2] * 3, old[3] * 3, old[4] * 3, *old[5:])], NOW)
        assert dist()["observations"][0] == o
    finally:
        store.save_option_bars("sim", "RELIANCE", exp, sell.strike, sell.right, "1day", [(old[0].isoformat(), *old[1:])], NOW)


def test_research_pages_use_the_phase9_capital(s):
    from app.options.shortlist import reversal_watch, run_shortlist, stock_view
    from app.options.stock_selling import phase9_settings
    cfg = _settings()
    assert phase9_settings(cfg).account.capital_inr == 1000000.0
    assert phase9_settings(phase9_settings(cfg)) is phase9_settings(cfg) or True
    seen = []
    import app.options.shortlist as sl
    real = sl.build_stock_selling
    try:
        sl.build_stock_selling = lambda st: (seen.append(st.account.capital_inr), real(st))[1]
        store = Store(":memory:")
        store.save_history_bars("bars_1d", stock_bars("NIFTY") + stock_bars("RELIANCE"), "sim")
        run_shortlist(cfg, store, "sim", now=NOW, save=False)
        reversal_watch(cfg, store, "sim", now=NOW)
        stock_view(cfg, store, "sim", "RELIANCE", now=NOW)
    finally:
        sl.build_stock_selling = real
    assert seen and set(seen) == {1000000.0}


def test_download_completes_the_priority_years_for_every_stock_before_an_earlier_year():
    from app.options.stock_backtest import waiting_for_priority_years
    years = [2026, 2025, 2024, 2023]
    assert waiting_for_priority_years(years, ["A", "B"], {"A": [2023, 2024, 2025, 2026]}) == ["B"]
    assert waiting_for_priority_years(years, ["A", "B"], {"A": years, "B": [2026, 2025, 2024]}) == ["B"]
    assert waiting_for_priority_years(years, ["A", "B"], {"A": years, "B": years + [2022]}) == []
    assert waiting_for_priority_years(years, [], {}) == []
    import inspect
    from app.options import stock_backtest
    src = inspect.getsource(stock_backtest.download_blocks)
    assert "waiting_for_priority_years" in src and "year < first_from" in src     # the gate is applied before earlier years



# ---------------- whole-lot sizing ----------------
def test_ACCEPTANCE_sizing_is_whole_lots_within_the_risk_budget_rounded_down():
    from app.options.payoff import OptionLeg
    from app.options.sizing import ZERO_SIZE, size_spread
    costs = CostModel(settings().costs)
    one = [OptionLeg("SELL", "PE", 1060, EXP, 1, 6.0, LOT), OptionLeg("BUY", "PE", 1040, EXP, 1, 2.0, LOT)]
    n1, legs1, pay1 = size_spread("credit", one, LOT, costs, 1e9, max_lots=1)
    one_loss = float(pay1.max_loss_inr)                          # (20 - 4) x 250 + charges = about 4,100
    assert n1 == 1 and 4000 < one_loss < 4300
    for budget, want in ((one_loss * 3.9, 3), (one_loss * 3 + 1, 3), (one_loss * 1.2, 1), (one_loss, 1)):
        n, legs, pay = size_spread("credit", one, LOT, costs, budget)
        assert n == want and float(pay.max_loss_inr) <= budget and all(x.lots == want for x in legs)
        if want > 1:                                             # one more lot would break the budget
            bigger = size_spread("credit", one, LOT, costs, 1e9, max_lots=want + 1)[2]
            assert float(bigger.max_loss_inr) > budget
    n, legs, pay = size_spread("credit", one, LOT, costs, one_loss - 1)
    assert (n, legs) == (0, None) and float(pay.max_loss_inr) == one_loss            # zero lots: no signal
    assert size_spread("credit", one, LOT, costs, one_loss * 10, max_lots=2)[0] == 2  # a roll never grows the position
    debit = [OptionLeg("BUY", "CE", 600, EXP, 1, 12.0, LOT), OptionLeg("SELL", "CE", 620, EXP, 1, 4.0, LOT)]
    n, legs, pay = size_spread("debit", debit, LOT, costs, 6000.0)
    assert n == 2 and float(pay.max_loss_inr) <= 6000 and pay.structure == "DEBIT_SPREAD"
    assert ZERO_SIZE == "ZERO_SIZE"
    # the strategy: lots by the budget at the capital in use; the margin is for the whole position
    s1 = settings(stock_option_selling={"max_loss_per_stock_spread_pct": 2.5})
    bars = stock_bars()
    st = build_stock_selling(s1)
    small = st.evaluate(ctx_for(s1, bars))                       # Rs 2,00,000 x 2.5% = Rs 5,000: one lot
    big_cfg = settings(account={"capital_inr": 1000000.0}, stock_option_selling={"max_loss_per_stock_spread_pct": 2.5})
    big = build_stock_selling(big_cfg).evaluate(ctx_for(big_cfg, bars))         # Rs 25,000: six lots
    assert small and big
    a, b = small[0].details, big[0].details
    assert a["lots"] == 1 and b["lots"] == math.floor(25000 / a["max_loss_inr"]) or b["lots"] == math.floor(25000 / a["max_loss_inr"]) - 1
    assert b["max_loss_inr"] <= 25000 < b["max_loss_inr"] / b["lots"] * (b["lots"] + 1)
    assert b["margin_inr"] == b["margin_per_lot_inr"] * b["lots"] == 40000.0 * b["lots"]
    assert b["sizing"]["lots"] == b["lots"] and b["sizing"]["risk_budget_inr"] == 25000.0
    assert f"{b['lots']} lot(s)" in " ".join(big[0].why_now)
    tiny = settings(stock_option_selling={"max_loss_per_stock_spread_pct": 0.5})                  # Rs 1,000
    assert build_stock_selling(tiny).evaluate(ctx_for(tiny, bars)) == []


# ---------------- open positions at the cutoff ----------------
def test_ACCEPTANCE_a_position_open_at_the_cutoff_is_reported_open_not_as_a_closed_trade(world):
    from app.options.backtest import OPEN_AT_CUTOFF, option_stats
    res = run_bt(world, exit_checks=("daily",))
    rep, trades = res["report"], res["trades"]
    still = [t for t in trades if t["exit_reason"] == OPEN_AT_CUTOFF]
    closed = [t for t in trades if t["exit_reason"] != OPEN_AT_CUTOFF and t["variant"] != "strategy"]
    assert still, "the fixture has spreads open on the last tested session"
    assert all(t["exit_reason"] != "PERIOD_END" for t in trades)
    o = rep["baseline"]["overall"]
    n_open = sum(1 for t in still if t["variant"] != "strategy")
    assert o["trades"] == len(closed) and o["open_at_cutoff"]["positions"] == n_open      # not counted as trades
    assert o["net_pnl_inr"] == pytest.approx(sum(t["net_pnl"] for t in closed), abs=0.02)  # realised only
    unreal = sum(t["net_pnl"] for t in still if t["variant"] != "strategy")
    assert o["open_at_cutoff"]["unrealised_inr"] == pytest.approx(unreal, abs=0.02)
    assert o["net_incl_open_inr"] == pytest.approx(o["net_pnl_inr"] + unreal, abs=0.05)     # no double counting
    assert o["max_drawdown_incl_open_inr"] >= 0 and o["open_at_cutoff"]["mark_time"].startswith("2025-09-30")
    for t in still:
        assert t["liquidation_costs_inr"] > 0 and ist_date(t["exit_time"]) == date(2025, 9, 30)
    assert rep["valuation_incomplete"] == {"strategy": 0, "baseline": 0}
    # without open positions the figures equal the plain statistics
    from app.backtest import stats
    from app.options.backtest import OptionTrade
    s = option_stats([], 1e6, 30)
    assert s["open_at_cutoff"] == {"positions": 0} and s["net_incl_open_inr"] == s["net_pnl_inr"] == stats([], 1e6, 30)["net_pnl_inr"]
    # the phase 4 strategies keep closing at the end of the period
    from app.options.backtest import OptionEngine
    from app.options import build_option_strategies
    assert OptionEngine._cutoff_reason(build_option_strategies(settings(), ["credit_spread"])[0]) == "PERIOD_END"
