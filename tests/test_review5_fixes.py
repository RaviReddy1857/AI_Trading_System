"""Phase 4 review (commit 033782e): findings 1-5. Each test fails on the code before the fix.
Finding 6 (weak loops and the exit-date boundary) is fixed in tests/test_phase4.py itself."""
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.history import daily_start
from app.models import Bar, Signal
from app.options.backtest import OptionEngine
from app.options.greeks import bs_price, years_to_expiry
from app.options.option_history import OptionHistory
from app.options.payoff import OptionLeg, iron_condor, pnl_at_expiry
from app.quality import IST
from app.store import Store
from tests.test_phase3 import FixedRegime, settings
from tests.test_phase4 import EXP_M, EXP_W, NOW, RATE, leg, make_ctx, materialise, run, strat, weekdays, world


# ---------------- finding 1: the hedge rule at every boundary ----------------
def spread_signal(legs, **over):
    base = dict(signal_id="s1", created_at=NOW, strategy_id="credit_spread", strategy_version="1/x", symbol="NIFTY",
                asset_class="INDEX_OPTION", direction="SPREAD", horizon="MONTHLY", entry=30.0, stop=1e6, target=0.01,
                valid_until=NOW + timedelta(days=400), regime="TRENDING_UP", why_now=("fixture",), data_as_of=NOW,
                legs=tuple(legs), details={"structure": "CREDIT_SPREAD", "width": 1e6})
    base.update(over)
    return Signal(**base)


SHORT = leg("SELL", "PE", 24000, 50)


@pytest.mark.parametrize("legs", [
    [SHORT],                                                       # a sold option alone
    [SHORT, leg("BUY", "CE", 23800, 20)],                          # bought option of the other right
    [SHORT, leg("BUY", "PE", 23800, 20, expiry=EXP_W)],            # bought option of another expiry
    [SHORT, leg("BUY", "PE", 24100, 20)],                          # not further out of the money
    [leg("SELL", "PE", 24000, 50, lots=2), leg("BUY", "PE", 23800, 20)],          # hedge too small
    [SHORT, leg("SELL", "PE", 23900, 40), leg("BUY", "PE", 23700, 15)],           # one hedge, two sold options
    [SHORT, leg("BUY", "PE", 23800, 20), leg("SELL", "CE", 24600, 30)],           # condor missing its call hedge
])
def test_finding1_an_unhedged_option_signal_cannot_be_constructed(legs):
    with pytest.raises(ValueError, match="UNHEDGED_SHORT"):
        spread_signal(legs)
    with pytest.raises(ValueError, match="UNHEDGED_SHORT"):        # a stock option signal too
        spread_signal(legs, asset_class="STOCK_OPTION", symbol="RELIANCE")


def test_finding1_hedged_signals_still_construct():
    assert spread_signal([SHORT, leg("BUY", "PE", 23800, 20)]).legs[0].action == "SELL"
    condor = [SHORT, leg("BUY", "PE", 23800, 20), leg("SELL", "CE", 24600, 30), leg("BUY", "CE", 24800, 10)]
    assert len(spread_signal(condor).legs) == 4
    assert spread_signal([leg("BUY", "CE", 24000, 100)], direction="LONG", entry=100.0, stop=60.0, target=180.0)


def test_finding1_backtest_entry_refuses_an_unhedged_signal():
    """Even a Signal object that got past its own validation is not entered by the backtest."""
    s, store, base, model = world()
    eng = OptionEngine(s, store, "sim", model, base, lot_sizes={"NIFTY": 50})
    d = next(x for x in base.session_dates() if x >= date(2025, 7, 1))
    i = [b.start for b in base.daily("NIFTY")].index(daily_start(d))
    exp = next(e for e, _ in model.expiries("NIFTY") if (e - d).days >= 20)
    legs = [OptionLeg("SELL", "PE", 23500.0, exp, 1, 50.0, 50), OptionLeg("BUY", "PE", 23300.0, exp, 1, 20.0, 50)]
    sig = spread_signal(legs)
    skipped = Counter()
    assert eng._enter(sig, "NIFTY", d, i, "strategy", skipped) is not None and not skipped      # hedged: entered
    object.__setattr__(sig, "legs", (legs[0],))                    # bypass the model: the hedge is gone
    assert eng._enter(sig, "NIFTY", d, i, "strategy", skipped) is None
    assert skipped == Counter({"UNHEDGED_SHORT": 1})
    object.__setattr__(sig, "legs", (legs[0], OptionLeg("BUY", "CE", 23300.0, exp, 1, 20.0, 50)))
    assert eng._enter(sig, "NIFTY", d, i, "strategy", skipped) is None and skipped["UNHEDGED_SHORT"] == 2


def stored_trade(legs, **over):
    return {"symbol": "NIFTY", "structure": "CREDIT_SPREAD", "expiry": "2026-12-08", "lots": 1, "lot_size": 65,
            "legs": legs, "net_pnl": 1.0, **over}


@pytest.mark.parametrize("legs", [
    [{"action": "SELL", "right": "PE", "strike": 24000.0}],
    [{"action": "SELL", "right": "PE", "strike": 24000.0}, {"action": "BUY", "right": "CE", "strike": 23800.0}],
    [{"action": "SELL", "right": "PE", "strike": 24000.0},
     {"action": "BUY", "right": "PE", "strike": 23800.0, "expiry": "2026-11-17"}],
    [{"action": "SELL", "right": "PE", "strike": 24000.0, "lots": 2}, {"action": "BUY", "right": "PE", "strike": 23800.0}],
    [{"action": "SELL", "right": "PE"}, {"action": "BUY", "right": "PE", "strike": 23800.0}],     # unreadable: not hedged
])
def test_finding1_save_backtest_refuses_an_unhedged_option_trade_and_saves_nothing(legs):
    store = Store(":memory:")
    good = stored_trade([{"action": "SELL", "right": "PE", "strike": 24000.0},
                         {"action": "BUY", "right": "PE", "strike": 23800.0}])
    with pytest.raises(ValueError, match="UNHEDGED_SHORT"):
        store.save_backtest("run1", NOW, "credit_spread", "1/x", {}, {"k": 1}, [good, stored_trade(legs)])
    assert store.backtest_run("run1") is None and store.backtest_runs() == []      # all or nothing


def test_finding1_save_backtest_accepts_hedged_and_cash_trades():
    store = Store(":memory:")
    spread = stored_trade([{"action": "SELL", "right": "PE", "strike": 24000.0},
                           {"action": "BUY", "right": "PE", "strike": 23800.0}])
    long_call = stored_trade([{"action": "BUY", "right": "CE", "strike": 24000.0}], structure="LONG_OPTION")
    cash = {"symbol": "TCS", "direction": "LONG", "qty": 10, "net_pnl": 5.0}
    store.save_backtest("run2", NOW, "credit_spread", "1/x", {}, {"k": 1}, [spread, long_call, cash])
    assert len(store.backtest_run("run2")["trades"]) == 3


def test_finding1_every_real_backtest_trade_carries_expiry_and_size_on_each_leg():
    s, store, base, model = world()
    res = run(s, store, base, model, "credit_spread")
    assert res["trades"]
    for t in res["trades"]:
        assert all(x["expiry"] == t["expiry"] and x["lots"] == 1 and x["lot_size"] == 50 for x in t["legs"])


# ---------------- finding 2: stock IV history is point in time ----------------
def iv_world(close_on_strike_day_2: float):
    """RELIANCE with one monthly expiry (2025-08-28) and two stored at-the-money calls with different
    implied volatility: strike 1000 (20.0-20.6%, varying by day) and strike 1040 (30%). The close 35 days before expiry is
    1,000; the close 15 days before expiry is `close_on_strike_day_2`."""
    s = settings(backtest={"test_start": {"1day": date(2025, 6, 2)}, "holdout_start": {"1day": date(2025, 12, 1)}},
                 options={"iv_rank_lookback_days": 30, "iv_rank_min_history_days": 20})
    store = Store(":memory:")
    exp = date(2025, 8, 28)
    days = weekdays(date(2025, 6, 2), exp)
    later = date(2025, 8, 13)                                      # 15 days before expiry
    closes = {}
    for d in days:
        closes[d] = 1000.0 if d < date(2025, 7, 28) else 1035.0
    closes[later] = close_on_strike_day_2
    bars = []
    for d in days:
        c = closes[d]
        bars.append(Bar("RELIANCE", daily_start(d), c, c + 5, c - 5, c, 1e6))
        bars.append(Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None))
        bars.append(Bar("INDIAVIX", daily_start(d), 14, 14.2, 13.8, 14, None))
    store.save_history_bars("bars_1d", bars, "sim")
    prev = date(2025, 7, 31)                                       # an earlier expiry, so the IV rank has history
    for e in (prev, exp):
        store.save_option_expiry("sim", "NIFTY", e, "monthly", NOW)
    for e, strike, vol0 in ((prev, 1000.0, 0.20), (exp, 1000.0, 0.20), (exp, 1040.0, 0.30)):
        rows = []
        for n, d in enumerate(days):
            t = years_to_expiry(datetime.combine(d, time(15, 30), IST), e)
            if t:
                vol = vol0 + (0.001 * (n % 7) if strike == 1000.0 else 0.0)
                p = round(bs_price(closes[d], strike, t, s.options.risk_free_rate_pct / 100, 0.0, vol, "CE"), 4)
                rows.append((daily_start(d).isoformat(), p, p, p, p, 1000.0, 50000.0))
        store.save_option_bars("sim", "RELIANCE", e, strike, "CE", "1day", rows, NOW)
    base = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime("TRENDING_UP"))
    return OptionEngine(s, store, "sim", OptionHistory(store, "sim"), base, lot_sizes={"RELIANCE": 500})


def test_finding2_a_later_close_cannot_change_an_earlier_days_iv():
    earlier = date(2025, 8, 5)                                     # before the second strike is chosen
    a = iv_world(close_on_strike_day_2=1040.0)                     # the later close makes strike 1040 the ATM pick
    b = iv_world(close_on_strike_day_2=1000.0)                     # a different later close: strike 1000 again
    iv_a, rank_a = a.iv_at("RELIANCE", earlier)
    iv_b, rank_b = b.iv_at("RELIANCE", earlier)
    assert iv_a is not None and rank_a is not None
    assert (iv_a, rank_a) == (iv_b, rank_b)
    # on that day only the strike chosen on or before it (1000, 20% vol) may be used, although the
    # 1040 call (30% vol) is nearer the money
    assert 19.95 <= iv_a <= 20.65
    for d in weekdays(date(2025, 7, 24), date(2025, 8, 12)):
        assert a.iv_at("RELIANCE", d) == b.iv_at("RELIANCE", d)
    # from the day its strike is chosen the 1040 call may be used
    assert a.iv_at("RELIANCE", date(2025, 8, 14))[0] == pytest.approx(30.0, abs=0.05)
    assert 19.95 <= b.iv_at("RELIANCE", date(2025, 8, 14))[0] <= 20.65


def test_finding2_the_live_desk_uses_the_same_point_in_time_series():
    from app.options.analytics import OptionDesk
    out = []
    for later_close in (1040.0, 1000.0):
        eng = iv_world(close_on_strike_day_2=later_close)
        desk = OptionDesk(eng.s, eng.store, "sim", {}.get, lambda: "RANGE", lambda: NOW)
        out.append(desk.iv_measure("RELIANCE", date(2025, 8, 13), 25.0))     # history = days before 13-Aug
    assert out[0] == out[1] and out[0]["history_days"] >= 10
    assert out[0]["rank"] == 100.0             # 25% is above every earlier day (20.0-20.6%): the 30% call is unused


# ---------------- finding 3: condor breakevens with unequal wings ----------------
def condor_legs(ps, pl, cs, cl, credit, lot):
    """Leg prices whose net premium is exactly `credit`."""
    return [OptionLeg("SELL", "PE", ps, EXP_M, 1, float(credit) + 1.0, lot), OptionLeg("BUY", "PE", pl, EXP_M, 1, 1.0, lot),
            OptionLeg("SELL", "CE", cs, EXP_M, 1, 1.0, lot), OptionLeg("BUY", "CE", cl, EXP_M, 1, 1.0, lot)]


def test_finding3_asymmetric_condor_reports_only_real_breakevens():
    """Put wing 100 wide, call wing 300 wide, lot 65, costs 300 (4.6154 per share).
    Credit 60: both sides can lose -> 23800 - 60 + 4.6154 = 23,744.62 and 24600 + 60 - 4.6154 = 24,655.38.
    Credit 150: the put side loses at most 100 < 150 - 4.62, so it never loses: ONE breakeven,
    24600 + 150 - 4.6154 = 24,745.38 (the old code also reported 23,654.62, where the P&L is +2,950)."""
    tol = Decimal("0.70")                                          # a paise of rounding per share, 65 shares
    p = iron_condor(23800, 23700, 24600, 24900, 60, 65, 1, 300)
    assert [round(float(b), 2) for b in p.breakevens] == [23744.62, 24655.38]
    for b in p.breakevens:
        assert abs(pnl_at_expiry(condor_legs(23800, 23700, 24600, 24900, 60, 65), b, 300)) <= tol
    p = iron_condor(23800, 23700, 24600, 24900, 150, 65, 1, 300)
    legs = condor_legs(23800, 23700, 24600, 24900, 150, 65)
    assert [round(float(b), 2) for b in p.breakevens] == [24745.38]
    assert abs(pnl_at_expiry(legs, p.breakevens[0], 300)) <= tol
    assert pnl_at_expiry(legs, 23000, 300) == Decimal("2950.00")   # below the put wing it still makes money
    assert p.max_loss_inr == Decimal("10050.00")                   # (300 - 150) x 65 + 300, on the call side
    # mirror image: the narrow wing on the call side
    p = iron_condor(23800, 23500, 24600, 24700, 150, 65, 1, 300)
    legs = condor_legs(23800, 23500, 24600, 24700, 150, 65)
    assert [round(float(b), 2) for b in p.breakevens] == [23654.62]
    assert abs(pnl_at_expiry(legs, p.breakevens[0], 300)) <= tol
    assert pnl_at_expiry(legs, 26000, 300) > 0


@given(mid=st.integers(1000, 50000), gap=st.integers(10, 2000), wp=st.integers(5, 1000), wc=st.integers(5, 1000),
       frac=st.floats(0.01, 0.99), lot=st.integers(1, 2000), costs=st.decimals(min_value=0, max_value=3000, places=2))
@hyp_settings(max_examples=300, deadline=None)
def test_finding3_pnl_is_zero_at_every_reported_condor_breakeven(mid, gap, wp, wc, frac, lot, costs):
    ps, cs = mid - gap, mid + gap
    pl, cl = ps - wp, cs + wc
    if pl <= 0:
        return
    credit = round(max(wp, wc) * frac, 2)                          # may exceed the narrower wing
    if credit <= 0 or credit >= max(wp, wc):
        return
    p = iron_condor(ps, pl, cs, cl, credit, lot, 1, costs)
    legs = condor_legs(ps, pl, cs, cl, credit, lot)
    tol = Decimal("0.02") + Decimal(lot) * Decimal("0.01")
    for b in p.breakevens:
        assert pl <= b <= cl
        assert abs(pnl_at_expiry(legs, b, costs)) <= tol
    # completeness: a side that loses money at its bought strike has a breakeven, one that does not has none
    lows = [b for b in p.breakevens if b < mid]
    highs = [b for b in p.breakevens if b > mid]
    at_pl, at_cl = pnl_at_expiry(legs, pl, costs), pnl_at_expiry(legs, cl, costs)
    if p.max_profit_inr > 0:
        if at_pl < -tol:
            assert len(lows) == 1
        if at_pl > tol:
            assert lows == []
        if at_cl < -tol:
            assert len(highs) == 1
        if at_cl > tol:
            assert highs == []
    else:
        assert p.breakevens == ()


# ---------------- finding 4: the baseline's fixed schedule ----------------
def expected_schedule(base, store, min_dte=20, max_dte=35):
    """expiry -> its scheduled session, computed here from the calendar alone."""
    out = {}
    for e, kind in store.option_expiries("sim", "NIFTY"):
        if kind != "monthly":
            continue
        d = next((x for x in base.session_dates() if (e - x).days <= max_dte), None)
        if d is not None and (e - d).days >= min_dte:
            out[e] = d
    return out


@pytest.fixture(scope="module")
def schedule_world():
    s, store, base, model = world()
    materialise(store, model, "NIFTY", date(2025, 6, 1), date(2025, 10, 31))
    return s, store, base, model


def test_finding4_baseline_enters_each_expiry_on_its_exact_scheduled_day(schedule_world):
    s, store, base, _ = schedule_world
    quiet = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    res = run(s, store, quiet, OptionHistory(store, "sim"), "credit_spread")
    want = expected_schedule(base, store)
    lo, hi = date.fromisoformat(res["report"]["period"]["from"]), date.fromisoformat(res["report"]["period"]["to"])
    in_period = {e: d for e, d in want.items() if lo <= d < hi}
    assert len(in_period) >= 3
    for v in ("baseline:PE", "baseline:CE"):
        got = {date.fromisoformat(t["expiry"]): datetime.fromisoformat(t["signal_time"]).astimezone(IST).date()
               for t in res["trades"] if t["variant"] == v}
        assert got and all(in_period[e] == d for e, d in got.items())          # the exact date, nothing else
        skipped = {date.fromisoformat(r["expiry"]) for r in res["report"]["baseline"]["schedule"]["skipped"]
                   if r["variant"] == v}
        assert set(got) | skipped == set(in_period) and not (set(got) & skipped)   # every expiry: traded or skipped
        # positions of different expiries may overlap: a still-open trade never delays the next expiry
    sch = res["report"]["baseline"]["schedule"]
    assert sch["attempts"] == 2 * len(in_period) and sch["entered"] == len([t for t in res["trades"]])


def test_finding4_a_scheduled_day_without_prices_skips_that_expiry_and_never_retries():
    s, store, base, model = world()
    materialise(store, model, "NIFTY", date(2025, 6, 1), date(2025, 10, 31))
    quiet = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    want = expected_schedule(base, store)
    r1 = run(s, store, quiet, OptionHistory(store, "sim"), "credit_spread")
    traded = sorted({date.fromisoformat(t["expiry"]) for t in r1["trades"]})
    exp = traded[1]
    day = want[exp]
    # no option of that expiry has a price on the scheduled day (the days after it are untouched)
    store._db.execute("DELETE FROM option_bars WHERE underlying='NIFTY' AND expiry=? AND start_utc=?",
                      (exp.isoformat(), daily_start(day).isoformat()))
    r2 = run(s, store, quiet, OptionHistory(store, "sim"), "credit_spread")
    assert exp.isoformat() not in {t["expiry"] for t in r2["trades"]}          # not entered on a later day either
    rows = [r for r in r2["report"]["baseline"]["schedule"]["skipped"] if r["expiry"] == exp.isoformat()]
    assert {r["variant"] for r in rows} == {"baseline:PE", "baseline:CE"}
    assert all(r["scheduled_day"] == day.isoformat() and r["result"] == "skipped" and r["reason"] for r in rows)
    others = lambda res: sorted((t["variant"], t["expiry"], t["signal_time"]) for t in res["trades"]      # noqa: E731
                                if t["expiry"] != exp.isoformat())
    assert others(r1) == others(r2)                                # the other expiries are unaffected


def test_finding4_an_entry_that_cannot_fill_the_next_morning_is_skipped_not_retried():
    s, store, base, model = world()
    materialise(store, model, "NIFTY", date(2025, 6, 1), date(2025, 10, 31))
    quiet = Engine(s, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    want = expected_schedule(base, store)
    r1 = run(s, store, quiet, OptionHistory(store, "sim"), "credit_spread")
    t = next(x for x in r1["trades"] if x["variant"] == "baseline:PE")
    exp = date.fromisoformat(t["expiry"])
    entry_day = base.next_session(want[exp])
    hedge = next(x for x in t["legs"] if x["action"] == "BUY")
    store._db.execute("DELETE FROM option_bars WHERE underlying='NIFTY' AND expiry=? AND strike=? AND right='PE' AND start_utc=?",
                      (exp.isoformat(), hedge["strike"], daily_start(entry_day).isoformat()))
    r2 = run(s, store, quiet, OptionHistory(store, "sim"), "credit_spread")
    assert not [x for x in r2["trades"] if x["variant"] == "baseline:PE" and x["expiry"] == t["expiry"]]
    row = next(r for r in r2["report"]["baseline"]["schedule"]["skipped"]
               if r["variant"] == "baseline:PE" and r["expiry"] == t["expiry"])
    assert row["scheduled_day"] == want[exp].isoformat() and row["reason"] == "NO_OPTION_BAR_AT_ENTRY"


def test_finding4_option_buying_baseline_is_the_first_market_session_of_each_month_only():
    # the tested period starts mid-June: June's first session is before it, so June has no attempt
    s = settings(backtest={"test_start": {"1day": date(2025, 6, 10)}, "holdout_start": {"1day": date(2025, 10, 1)}})
    s, store, base, model = world(s=s)
    res = run(s, store, base, model, "long_option")
    sessions = base.session_dates()
    firsts = {min(d for d in sessions if (d.year, d.month) == (y, m)) for y, m in {(d.year, d.month) for d in sessions}}
    days = [datetime.fromisoformat(t["signal_time"]).astimezone(IST).date()
            for t in res["trades"] if t["variant"] == "baseline"]
    assert days == [date(2025, 7, 1), date(2025, 8, 1), date(2025, 9, 1)] and set(days) <= firsts


# ---------------- finding 5: margin and bid/ask stay required live; backtests say what they exclude ----------------
LABEL = "excludes margin and bid/ask eligibility checks"


def test_finding5_backtest_reports_are_labelled(schedule_world):
    s, store, base, model = schedule_world
    for sid, prices in (("credit_spread", OptionHistory(store, "sim")), ("iron_condor", model), ("long_option", model)):
        rep = run(s, store, base, prices, sid)["report"]
        assert rep["eligibility_label"] == LABEL
        assert any("margin and bid/ask eligibility" in n for n in rep["notes"])
    from app.page import BACKTEST_HTML
    assert "eligibility_label" in BACKTEST_HTML


def test_finding5_a_live_context_cannot_switch_the_margin_rule_off():
    with pytest.raises(ValueError, match="margin"):
        make_ctx(require_margin=False)                             # live (historical=False)
    cs = strat("credit_spread")
    assert cs.evaluate(make_ctx(margin=None)) == [] and cs.last_no_trade[0].startswith("MARGIN_UNKNOWN")
    from dataclasses import replace
    from tests.test_phase4 import bs_chain
    no_quotes = {EXP_M: [replace(q, bid=None, ask=None) for q in bs_chain()]}
    assert cs.evaluate(make_ctx(chains=no_quotes)) == []           # live: no bid/ask, no signal
    assert RATE > 0
