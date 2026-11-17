"""Phase 9: reversal detector - washed-out list, base forming, confirmation, the two candidate trades,
stall zones, the reversal alert for open positions, and the backtest of every confirmed reversal."""
import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.config import StockOptionSelling as SosParams
from app.history import daily_start
from app.models import Bar, unhedged_short_legs
from app.options.backtest import OptionEngine, model_prices
from app.options.base import ExpiryInfo
from app.options.option_history import OptionHistory
from app.options.payoff import InvalidStructure, debit_spread, pnl_at_expiry
from app.options.reversal import (REVERSAL_CONFIRMED, ReversalScanner, build_reversal_strategies, reversal_outcomes)
from app.options.shortlist import reversal_watch
from app.options.stock_backtest import run_reversal_reports
from app.options.stock_profile import DOWNTREND_BLOCK, alert_kinds
from app.options.stock_selling import SpreadPosition, build_stock_selling, position_alerts
from app.quality import IST
from app.store import Store
from tests.rev_fixture import washed_out_stock
from tests.test_phase3 import FixedRegime
from tests.test_phase9_core import settings, EXP, LOT, NOW, chain, ctx_for, ist_date, legs_of, weekdays
from tests.test_phase9_rules import snap

REV = settings().stock_option_selling.reversal
BASE_LOW = 540 * 0.98                                    # the fall's lowest close less its 2% daily range


def scan(**kw):
    bars, idx, marks = washed_out_stock(**kw)
    return ReversalScanner(bars, idx, REV, 200), bars, idx, marks


@pytest.fixture(scope="module")
def s():
    return settings()


# ---------------- 1-3. washed out, base forming, confirmed ----------------
def test_ACCEPTANCE_washed_out_then_base_forming_then_reversal_confirmed():
    sc, bars, _, m = scan()
    states = [sc.info(i)["state"] for i in range(len(bars))]
    first = {x: states.index(x) for x in ("WASHED_OUT", "BASE_FORMING", REVERSAL_CONFIRMED)}
    # below the 200-day average since the first leg down; 25% below the 52-week high (below 766.5) on session 388
    assert first["WASHED_OUT"] == 388 and states[387] is None
    assert sc.days[387].drawdown_pct < 25 <= sc.days[388].drawdown_pct
    day = sc.days[388]
    assert day.washed_out and day.high_52w == pytest.approx(1002 * 1.02) and not day.base_forming
    # with the drawdown already there, 63 sessions below the long average are needed too
    longer = REV.model_copy(update={"min_sessions_below_long": 120})
    late = [d.washed_out for d in ReversalScanner(bars, washed_out_stock()[1], longer, 200).days].index(True)
    assert late > 388
    # base forming: no new low for 15 sessions and the 10-session ATR below 0.8 x the 20-session ATR
    assert m["quiet_from"] < first["BASE_FORMING"] <= m["quiet_from"] + 6
    assert sc.days[first["BASE_FORMING"]].base_low == pytest.approx(BASE_LOW)
    assert all(not d.base_forming for d in sc.days[:m["quiet_from"]])           # wide ranges: not a base yet
    # confirmed on the first close above the last swing high (600), not on the two closes before it
    assert first[REVERSAL_CONFIRMED] == m["confirmed"] and [d.confirmed for d in sc.days].count(True) == 1
    info = sc.info(m["confirmed"])
    assert info["confirmed_today"] and info["lift_active"] and info["base_low"] == pytest.approx(BASE_LOW)
    assert info["confirmed_on"] == sc.dates[m["confirmed"]].isoformat() and len(info["reasons"]) == 5
    assert "above the 20-day average" in info["reasons"][0] and "last confirmed swing high 612.00" in info["reasons"][1]
    assert "volume" in info["reasons"][2] and "ahead of the index" in info["reasons"][3] and "has held" in info["reasons"][4]
    assert info["stall_zones"] == [784.0]                # the swing low the second leg down broke
    assert sc.info_on(sc.dates[m["confirmed"]]) == info and sc.info_on(date(1999, 1, 1))["state"] is None


def test_each_confirmation_condition_is_needed():
    assert not any(d.confirmed for d in scan(breakout=False)[0].days)            # no close above the swing high
    assert not any(d.confirmed for d in scan(quiet_volume=True)[0].days)         # volume not above its average
    assert not any(d.confirmed for d in scan(index_growth=0.01)[0].days)         # NIFTY's 10 sessions are stronger
    # close above the swing high but the low broken first: the base is gone
    bars, idx, m = washed_out_stock()
    i = m["breakout"] - 2
    broken = bars[:i] + [replace(bars[i], low=500.0)] + bars[i + 1:]
    assert not any(d.confirmed for d in ReversalScanner(broken, idx, REV, 200).days)
    # a stock that is not below its 200-day average, or not down 30% for three months, is not washed out
    assert not any(d.washed_out for d in ReversalScanner(bars[:388], idx, REV, 200).days)
    shallow = REV.model_copy(update={"washed_out_drawdown_pct": 60.0})
    assert not any(d.washed_out for d in ReversalScanner(bars, idx, shallow, 200).days)
    # too little history is unknown, never a state
    assert all(sc_day.washed_out is False for sc_day in ReversalScanner(bars[:150], idx, REV, 200).days)


def test_the_state_of_a_day_never_uses_later_bars():
    full, bars, idx, m = scan(after=30)
    for cut in (430, 465, 480, m["confirmed"], m["confirmed"] + 1, m["confirmed"] + 12):
        part = ReversalScanner(bars[:cut + 1], idx, REV, 200)
        assert part.days == full.days[:cut + 1], cut
        assert part.info(cut) == full.info(cut), cut


def test_the_lift_lasts_lift_sessions_and_ends_if_the_base_low_breaks_and_one_confirmation_per_rearm_period():
    sc, bars, idx, m = scan(after=30)
    c = m["confirmed"]
    assert all(sc.days[i].lift_active for i in range(c, c + REV.lift_sessions + 1))
    assert not sc.days[c + REV.lift_sessions + 1].lift_active
    assert sc.info(c + 5)["state"] == REVERSAL_CONFIRMED and sc.info(c + 5)["confirmed_today"] is False
    assert sc.info(c + REV.lift_sessions + 1)["state"] != REVERSAL_CONFIRMED
    assert [d.confirmed for d in sc.days].count(True) == 1                       # the later higher closes do not re-confirm
    crash = bars[:c + 4] + [replace(bars[c + 4], low=500.0)] + bars[c + 5:]
    after = ReversalScanner(crash, idx, REV, 200)
    assert after.days[c + 3].lift_active and not after.days[c + 4].lift_active and not after.days[c + 9].lift_active
    # a price break (unadjusted split) starts the history afresh: nothing is known for a year after it
    gap = ReversalScanner(bars, idx, REV, 200, breaks=[400])
    assert not any(d.washed_out or d.confirmed for d in gap.days[400:]) and gap.days[399].washed_out


# ---------------- the block is lifted only for put spreads below the base low ----------------
def reversal_ctx(cfg, after=0, iv=0.6, confirmed_today=None, **kw):
    sc, bars, idx, m = scan(after=after)
    info = sc.info(len(bars) - 1)
    if confirmed_today is not None:
        info = {**info, "confirmed_today": confirmed_today}
    spot = bars[-1].close
    return ctx_for(cfg, bars, chain(spot, lo=350, hi=900, iv=iv), iv=iv, index_daily=tuple(idx),
                   market={"reversal": info, "breadth_pct": 60.0}, **kw), info


def test_ACCEPTANCE_reversal_lifts_the_falling_stock_block_only_for_a_put_spread_below_the_base_low():
    cfg = settings(stock_option_selling={"min_credit_to_max_loss": 0, "sma_short": 3, "sma_mid": 8})
    strat = build_stock_selling(cfg)
    ctx, info = reversal_ctx(cfg, after=2)
    plain = replace(ctx, market={})
    assert strat.evaluate(plain) == [] and any(r.startswith(DOWNTREND_BLOCK) for r in strat.last_no_trade)
    a = strat.assess(ctx)
    assert a.signal is not None, strat.last_no_trade
    short, long_ = legs_of(a.signal)
    assert short.right == "PE" and short.strike < BASE_LOW and long_.strike < short.strike
    assert any("REVERSAL_CONFIRMED" in r and "block is lifted" in r for r in a.reasons)
    assert a.signal.details["reversal"]["base_low"] == pytest.approx(BASE_LOW)
    assert a.signal.details["reversal"]["stall_zones"] == [784.0]
    # after the lift has run out the block is back
    late, info2 = reversal_ctx(cfg, after=REV.lift_sessions + 3)
    assert info2["lift_active"] is False
    assert strat.evaluate(late) == [] and any(r.startswith(DOWNTREND_BLOCK) for r in strat.last_no_trade)
    # switched off: no lift
    off = build_stock_selling(settings(stock_option_selling={
        "min_credit_to_max_loss": 0, "sma_short": 3, "sma_mid": 8,
        "reversal": {**REV.model_dump(), "enabled": False}}))
    assert off.evaluate(ctx) == [] and any(r.startswith(DOWNTREND_BLOCK) for r in off.last_no_trade)


# ---------------- 4. two candidate trades ----------------
def test_ACCEPTANCE_confirmation_gives_a_put_credit_spread_below_the_base_low(s):
    cfg = settings(stock_option_selling={"min_credit_to_max_loss": 0})
    put, _ = build_reversal_strategies(cfg)
    assert put.id == "reversal_put_spread" and put.alerts_enabled is False and "WATCH ONLY" in put.status_label
    ctx, info = reversal_ctx(cfg)
    sig = put.evaluate(ctx)
    assert sig, put.last_no_trade
    short, long_ = legs_of(sig[0])
    assert sig[0].strategy_id == "reversal_put_spread" and short.right == "PE"
    assert short.strike < BASE_LOW and unhedged_short_legs(sig[0].legs) == []
    assert abs(sig[0].details["legs"][0]["delta"]) <= cfg.stock_option_selling.short_delta_max
    assert sig[0].details["reversal"]["base_low"] == pytest.approx(BASE_LOW) and 20 <= sig[0].details["days_to_expiry"] <= 35
    assert "the base low" in " ".join(sig[0].why_now)
    # no confirmation today: no candidate. The up-trend and IV-rank entry rules do not gate this candidate
    quiet, _ = reversal_ctx(cfg, confirmed_today=False)
    assert put.evaluate(quiet) == [] and any(r.startswith("NO_REVERSAL") for r in put.last_no_trade)
    assert put.evaluate(replace(ctx, market={})) == []
    assert put.evaluate(replace(ctx, iv_rank=None))
    # structure rules still apply: margin, liquidity
    assert put.evaluate(replace(ctx, margin=lambda a, b: None)) == [] and any("MARGIN_UNKNOWN" in r for r in put.last_no_trade)
    # the minimum net credit applies with the default settings
    strict, _ = build_reversal_strategies(settings(stock_option_selling={"min_credit_to_max_loss": 0.95}))
    assert strict.evaluate(ctx) == [] and any(r.startswith("CREDIT_TOO_SMALL") for r in strict.last_no_trade)


def test_ACCEPTANCE_confirmation_gives_a_bull_call_spread_with_stall_zones(s):
    _, call = build_reversal_strategies(s)
    assert call.id == "reversal_bull_call_spread" and call.alerts_enabled is False and "WATCH ONLY" in call.status_label
    ctx, info = reversal_ctx(s)
    sig = call.evaluate(ctx)
    assert sig, call.last_no_trade
    x = sig[0]
    bought, sold = x.legs
    spot = ctx.spot
    assert (bought.action, sold.action, bought.right, sold.right) == ("BUY", "SELL", "CE", "CE")
    assert abs(bought.strike - spot) <= 5 and sold.strike - bought.strike == 20           # nearest the money; 2 strikes higher
    assert bought.expiry == sold.expiry == EXP and unhedged_short_legs(x.legs) == []
    d = x.details
    debit = bought.price - sold.price
    assert x.direction == "LONG" and x.asset_class == "STOCK_OPTION" and x.entry == pytest.approx(debit, abs=0.01)
    assert x.stop < x.entry < x.target
    assert x.stop == pytest.approx(debit * 0.5, abs=0.01) and x.target == pytest.approx(debit + (20 - debit) * 0.6, abs=0.01)
    n = d["lots"]
    assert n >= 1 and bought.lots == sold.lots == n
    assert d["structure"] == "DEBIT_SPREAD" and d["max_loss_inr"] == pytest.approx(debit * LOT * n + d["costs_inr"], abs=0.02)
    assert d["max_profit_inr"] == pytest.approx((20 - debit) * LOT * n - d["costs_inr"], abs=0.02)
    assert d["breakevens"][0] == pytest.approx(bought.strike + debit + d["costs_inr"] / (LOT * n), abs=0.01)
    assert d["stall_zones"] == [784.0] and d["base_low"] == pytest.approx(BASE_LOW) and "stall zones" in x.why_now[-1]
    assert d["max_loss_inr"] <= s.account.capital_inr * s.stock_option_selling.max_risk_per_position_pct / 100
    assert any("approve" in w for w in d["warnings"])
    # no confirmation: nothing. Baseline (no filter): the same structure without a reversal
    assert call.evaluate(replace(ctx, market={})) == [] and any(r.startswith("NO_REVERSAL") for r in call.last_no_trade)
    assert call.evaluate(replace(ctx, market={}, no_filter=True))
    small, = [build_reversal_strategies(settings(stock_option_selling={"max_risk_per_position_pct": 0.5}))[1]]
    assert small.evaluate(ctx) == [] and any(r.startswith("ZERO_SIZE") for r in small.last_no_trade)
    assert call.evaluate(replace(ctx, kind="index")) == []
    for bad in ({"reversal": {"bull_call": {"entry_min_dte": 70}}}, {"reversal": {"outcome_sessions": []}},
                {"reversal": {"washed_out_drawdown_pct": 0}}):
        with pytest.raises(Exception):
            settings(stock_option_selling=bad)


def test_debit_spread_payoff_by_hand_and_bounds():
    """Bull call 600/620, debit 8, lot 250, costs 150: paid 2,000; max loss 2,150; max profit (20-8)*250 - 150 = 2,850;
    breakeven 600 + 8 + 0.6 = 608.6."""
    p = debit_spread("CE", 600, 620, 8, 250, 1, 150)
    assert (p.net_premium_inr, p.max_loss_inr, p.max_profit_inr) == (Decimal("-2000.00"), Decimal("2150.00"), Decimal("2850.00"))
    assert float(p.breakevens[0]) == pytest.approx(608.6) and p.structure == "DEBIT_SPREAD"
    for bad in (("CE", 620, 600, 8), ("PE", 600, 620, 8), ("CE", 600, 620, 20), ("XX", 600, 620, 8)):
        with pytest.raises(InvalidStructure):
            debit_spread(*bad, 250)


@given(kl=st.integers(100, 5000), width=st.integers(1, 500), frac=st.floats(0.02, 0.98), lot=st.integers(1, 3000),
       costs=st.decimals(min_value=0, max_value=2000, places=2), spot=st.floats(1, 9000))
@hyp_settings(max_examples=150, deadline=None)
def test_bull_call_spread_payoff_is_bounded_at_every_expiry_price(kl, width, frac, lot, costs, spot):
    from app.options.payoff import OptionLeg
    debit = round(width * frac, 2)
    if not (0 < debit < width):
        return
    p = debit_spread("CE", kl, kl + width, debit, lot, 1, costs)
    legs = [OptionLeg("BUY", "CE", kl, EXP, 1, debit + 1.0, lot), OptionLeg("SELL", "CE", kl + width, EXP, 1, 1.0, lot)]
    pnl = pnl_at_expiry(legs, round(spot, 2), costs)
    assert -p.max_loss_inr - Decimal("0.02") <= pnl <= p.max_profit_inr + Decimal("0.02")
    assert unhedged_short_legs(legs) == []


# ---------------- 5. the reversal alert, only once approved ----------------
def test_ACCEPTANCE_reversal_alert_goes_to_every_open_position_only_after_approval(s):
    prev, now = snap(), snap(date(2026, 11, 13), reversal=True)
    kinds = lambda right, ok: [a["kind"] for a in alert_kinds(prev, now, right, ok)]
    assert kinds("PE", False) == [] and kinds("CE", False) == []                    # not approved: nothing
    assert kinds("PE", True) == ["REVERSAL_CONFIRMED"] and kinds("CE", True) == ["REVERSAL_CONFIRMED"]
    assert "against this bear call spread" in alert_kinds(prev, now, "CE", True)[0]["what_changed"]
    assert [a["kind"] for a in alert_kinds(snap(reversal=True), now, "PE", True)] == []   # only on the day it happens
    assert s.stock_option_selling.reversal.alerts_approved is False
    approved = settings(stock_option_selling={"reversal": {**REV.model_dump(), "alerts_approved": True}})
    ctx, _ = reversal_ctx(s)
    held = SpreadPosition("RELIANCE", "PE", 520.0, 500.0, EXP, 2.0, LOT)             # a position entered by hand is the same object
    assert position_alerts(build_stock_selling(s), ctx, held, 1.0, prev, now) == []
    got = position_alerts(build_stock_selling(approved), ctx, held, 1.0, prev, now)
    assert [a["kind"] for a in got] == ["REVERSAL_CONFIRMED"] and got[0]["exit"]["cost_to_close_inr"] == 1.0 * LOT
    call_pos = SpreadPosition("RELIANCE", "CE", 700.0, 720.0, EXP, 2.0, LOT)
    assert [a["kind"] for a in position_alerts(build_stock_selling(approved), ctx, call_pos, 1.0, prev, now)] == ["REVERSAL_CONFIRMED"]


# ---------------- 6. backtest: every confirmed reversal ----------------
def test_reversal_outcomes_by_hand_and_low_sample():
    sc, bars, idx, m = scan(after=45)
    c = m["confirmed"]
    sessions = sc.dates
    out = reversal_outcomes({"RELIANCE": sc}, sessions, [10, 20, 40], 30)
    assert out["label"] == "history, not a forecast" and out["confirmations"] == 1 and out["stocks"] == 1
    assert out["events"] == [{"stock": "RELIANCE", "date": sc.dates[c].isoformat(), "close": 620.0, "base_low": pytest.approx(BASE_LOW)}]
    for row, h in zip(out["outcomes"], (10, 20, 40)):
        assert row["sessions"] == h and row["cases"] == 1 and row["low_sample"] is True
        assert row["median_pct"] == pytest.approx(((620 + 2 * h) / 620 - 1) * 100, abs=0.01) and row["up_pct_of_cases"] == 100.0
    assert out["new_low_afterwards"] == {"within_sessions": 40, "cases": 1, "share_pct": 0.0, "low_sample": True}
    # a horizon that runs past the tested period is not a case; nothing is filled in
    short = reversal_outcomes({"RELIANCE": sc}, sessions[:c + 25], [10, 20, 40], 30)
    assert [r["cases"] for r in short["outcomes"]] == [1, 1, 0] and short["outcomes"][2]["median_pct"] is None
    assert short["new_low_afterwards"]["cases"] == 0 and short["new_low_afterwards"]["share_pct"] is None
    assert reversal_outcomes({"RELIANCE": sc}, sessions[:c], [10], 30)["confirmations"] == 0      # outside the period
    # a later low under the base low is counted
    bars2 = bars[:c + 12] + [replace(bars[c + 12], low=500.0)] + bars[c + 13:]
    again = reversal_outcomes({"RELIANCE": ReversalScanner(bars2, idx, REV, 200)}, sessions, [10, 20, 40], 30)
    assert again["new_low_afterwards"]["share_pct"] == 100.0
    assert reversal_outcomes({"A": sc, "B": sc}, sessions, [10], 1)["outcomes"][0]["low_sample"] is False


def reversal_world():
    """The washed-out stock confirmed on 1 July 2025, with stored option history (model prices) around it."""
    cfg = settings(backtest={"test_start": {"1day": date(2023, 6, 1)}, "holdout_start": {"1day": date(2025, 10, 1)}},
                   options={"iv_rank_min_history_days": 20, "iv_rank_lookback_days": 60},
                   stock_option_selling={"min_credit_to_max_loss": 0})
    end = date(2025, 9, 30)
    after = len(weekdays(date(2025, 7, 2), end))
    bars, idx, m = washed_out_stock(end=end, after=after)
    store = Store(":memory:")
    vix = [Bar("INDIAVIX", b.start, 45.0, 45.2, 44.8, 45.0 + 0.001 * i, None) for i, b in enumerate(idx)]
    store.save_history_bars("bars_1d", bars + idx + vix, "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    model = model_prices(cfg, base)
    for exp, _ in model.expiries("RELIANCE"):
        store.save_option_expiry("sim", "NIFTY", exp, "monthly", NOW)
        if not (date(2025, 6, 1) <= exp <= date(2025, 11, 30)):
            continue
        for k in range(400, 801, 5):
            for right in ("CE", "PE"):
                rows = []
                for d in weekdays(exp - timedelta(days=80), exp):      # stored from before the 65-day entry limit
                    b = model.bar("RELIANCE", exp, k, right, d)
                    if b is not None:
                        rows.append((daily_start(d).isoformat(), b.open, b.high, b.low, b.close, 1000.0, 500000.0))
                store.save_option_bars("sim", "RELIANCE", exp, k, right, "1day", rows, NOW)
    hist = OptionHistory(store, "sim")
    eng = OptionEngine(cfg, store, "sim", hist, base, lot_sizes={"RELIANCE": 250})
    eng.set_expiry_floor(date(2025, 6, 1))
    return cfg, store, base, eng, ist_date(bars[m["confirmed"]].start.isoformat())


@pytest.mark.usefixtures("fixed_clock")
def test_ACCEPTANCE_backtest_reports_every_reversal_and_both_candidates_against_the_baseline():
    cfg, store, base, eng, confirmed = reversal_world()
    assert confirmed == date(2025, 7, 1)
    dev = [d for d in base.session_dates() if d < date(2025, 10, 1)]
    out = run_reversal_reports(cfg, store, eng, ["RELIANCE"], ["RELIANCE"], dev, date(2025, 6, 2), "")
    o = out["outcomes"]
    assert o["confirmations"] == 1 and o["events"][0]["date"] == "2025-07-01" and o["label"] == "history, not a forecast"
    assert [r["sessions"] for r in o["outcomes"]] == [10, 20, 40] and all(r["cases"] == 1 and r["low_sample"] for r in o["outcomes"])
    assert o["new_low_afterwards"]["cases"] == 1 and o["new_low_afterwards"]["low_sample"] is True
    assert set(out["reports"]) == {"reversal_put_spread", "reversal_bull_call_spread"}
    for sid, rep in out["reports"].items():
        assert rep["reversal_outcomes"] == o and rep["exit_check"] == "daily" and rep["overall"]["low_sample"] is True
        assert rep["strategy_status"]["alerts"] is False and "WATCH ONLY" in rep["strategy_status"]["label"]
        assert rep["baseline"]["overall"]["trades"] > 0 and rep["comparison"]["verdict"].endswith("(LOW SAMPLE)")
        run = store.backtest_run(rep["run_id"])                                   # saved: the hedge check accepted every trade
        trades = [t for t in run["trades"] if t["variant"] == "strategy"]
        assert len(trades) == 1 and ist_date(trades[0]["signal_time"]) == confirmed
        t = trades[0]
        assert ist_date(t["entry_time"]) == date(2025, 7, 2)                      # the next session's open
        assert unhedged_short_legs(t["legs"], t["expiry"], t["lots"], t["lot_size"]) == []
        exp = date.fromisoformat(t["expiry"])
        assert ist_date(t["exit_time"]) < exp - timedelta(days=exp.weekday())     # flat before expiry week
        if sid == "reversal_put_spread":
            sell = next(x for x in t["legs"] if x["action"] == "SELL")
            assert sell["right"] == "PE" and sell["strike"] < BASE_LOW and t["structure"] == "CREDIT_SPREAD"
            assert set(rep["baseline"]["by_variant"]) == {"baseline:PE"}
        else:
            buy, sell = (next(x for x in t["legs"] if x["action"] == a) for a in ("BUY", "SELL"))
            assert buy["right"] == sell["right"] == "CE" and buy["strike"] < sell["strike"] and t["structure"] == "DEBIT_SPREAD"
            assert t["exit_reason"] in ("TARGET", "STOP", "TIME", "PERIOD_END")
            # by hand: gross P&L = (sold back - paid) on the bought call plus (sold - bought back) on the sold call
            gross = ((buy["exit_price"] - buy["entry_price"]) + (sell["entry_price"] - sell["exit_price"])) * 250 * t["lots"]
            assert t["gross_pnl"] == pytest.approx(gross, abs=0.03) and t["net_pnl"] == pytest.approx(gross - t["costs"]["total"], abs=0.03)
            assert set(rep["baseline"]["by_variant"]) == {"baseline"}
    # the engine gives the strategies the same point-in-time reversal state as the scanner
    assert eng.reversal_scanner("RELIANCE").info_on(date(2025, 7, 1))["confirmed_today"] is True
    assert eng.reversal_scanner("RELIANCE").info_on(date(2025, 6, 30))["confirmed_today"] is False


# ---------------- the watch list on the page ----------------
def watch_store(with_chain: bool):
    bars, idx, m = washed_out_stock()
    store = Store(":memory:")
    days = [b.start for b in idx]
    extra = [Bar("TCS", t, 3000 + i, 3030 + i, 2970 + i, 3000 + i, 1e6) for i, t in enumerate(days)]      # a healthy stock
    vix = [Bar("INDIAVIX", t, 14, 14, 14, 14, None) for t in days]
    store.save_history_bars("bars_1d", bars + idx + extra + vix, "sim")
    if with_chain:
        from app.options.instruments import ContractSpec, InstrumentBook
        spot = bars[-1].close
        InstrumentBook(store, "sim").save([ContractSpec("RELIANCE", EXP, LOT, "monthly",
                                                        tuple(float(k) for k in range(350, 901, 10)), NOW)])
        store.save_option_chain("sim", [("RELIANCE", EXP.isoformat(), q.strike, q.right, NOW.isoformat(), q.bid, q.ask, q.last,
                                         q.oi, 0.0, 100.0, spot) for q in chain(spot, lo=350, hi=900, iv=0.6)])
        store.replace_margins([("RELIANCE", "CREDIT_SPREAD", w, 30000.0, "2026-11-13") for w in (10.0, 20.0, 30.0)])
        store.replace_events([("2026-11-13", "COVERAGE", "RELIANCE", "source=NSE corporate filings; covered_through=2026-12-31")])
    return store


@pytest.mark.usefixtures("fixed_clock")
def test_ACCEPTANCE_washed_out_stocks_are_shown_as_watch_never_as_a_signal(s):
    store = watch_store(with_chain=False)
    w = reversal_watch(s, store, "sim", now=NOW + timedelta(minutes=30))
    assert w["session_date"] == "2026-11-13" and "never a signal" in w["note"] and w["alerts_approved"] is False
    assert [r["stock"] for r in w["rows"]] == ["RELIANCE"]                       # the healthy stock is not listed
    r = w["rows"][0]
    assert r["state"] == "REVERSAL_CONFIRMED" and r["label"] == "watch" and r["confirmed_on"] == "2026-11-13"
    assert r["base_low"] == pytest.approx(BASE_LOW) and r["stall_zones"] == [784.0] and r["drawdown_pct"] > 30
    assert [c["candidate"] for c in r["candidates"]] == ["bull call spread", "put credit spread below the base low"]
    assert all(c["status"] == "NO_TRADE" and "NO_CHAIN" in c["reasons"][0] for c in r["candidates"])   # no chain: said, not guessed
    # earlier sessions: washed out, then base forming, both "watch"
    bars = store.history_bars("bars_1d", "RELIANCE", "sim")
    for cut, state in ((465, "WASHED_OUT"), (480, "BASE_FORMING")):
        st2 = Store(":memory:")
        keep = {b.start for b in bars[:cut + 1]}
        st2.save_history_bars("bars_1d", [b for sym in ("RELIANCE", "NIFTY", "TCS", "INDIAVIX")
                                          for b in store.history_bars("bars_1d", sym, "sim") if b.start in keep], "sim")
        rows = reversal_watch(s, st2, "sim")["rows"]
        assert [(x["stock"], x["state"], x["label"], x["candidates"]) for x in rows] == [("RELIANCE", state, "watch", [])]
    # with a chain stored that session the two candidates are worked out, still watch only
    cfg = settings(stock_option_selling={"min_credit_to_max_loss": 0})
    full = reversal_watch(cfg, watch_store(with_chain=True), "sim", now=NOW + timedelta(minutes=30))["rows"][0]
    by = {c["candidate"]: c for c in full["candidates"]}
    assert by["bull call spread"]["status"] == "CANDIDATE" and by["bull call spread"]["trade"]["direction"] == "LONG"
    put = by["put credit spread below the base low"]
    assert put["status"] == "CANDIDATE" and put["trade"]["legs"][0]["strike"] < BASE_LOW and "WATCH ONLY" in put["label"]
    off = settings(stock_option_selling={"reversal": {**REV.model_dump(), "enabled": False}})
    assert reversal_watch(off, store, "sim")["rows"] == []


@pytest.mark.usefixtures("fixed_clock")
def test_reversal_route_and_page_are_read_only():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        r = c.get("/api/stock-option-selling/reversals")
        assert r.status_code == 200 and "never a signal" in r.json()["note"] and r.json()["alerts_approved"] is False
        page = c.get("/stock-option-selling").text
        assert "Reversal watch" in page and "watch only, never a signal" in page
        assert c.post("/api/stock-option-selling/reversals", json={}).status_code in (403, 405)
    paths = [x.path for x in app.routes]
    assert not [p for p in paths if "reversal" in p and any(w in p for w in ("order", "execute", "submit"))]
    assert math.isfinite(1.0) and datetime.now(timezone.utc) and time(9, 15) and IST and ExpiryInfo
