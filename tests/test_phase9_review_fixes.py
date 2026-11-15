"""Phase 9 review fixes of 2026-11-15 (Codex re-review of 782e870). Correctness fixes, not tuning:
each test failed on the engine before the fix and passes after it. Items 1, 4, 5, 6 and 11 are tested
in test_phase9_core.py, test_phase9_portfolio.py, test_phase9_rules.py and test_phase9_rolls.py."""
import math
from collections import Counter
from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import Engine
from app.costs import CostModel
from app.history import daily_start
from app.models import Bar
from app.options.backtest import OPEN_AT_CUTOFF, OptionEngine, OptionTrade, option_stats
from app.options.option_history import INTRADAY, OptionHistory
from app.options.score import fall_percentile
from app.options.stock_backtest import variant
from app.options.stock_selling import exit_calls, roll_choices, build_stock_selling
from app.quality import IST
from app.store import Store
from tests.test_phase9_core import (HELD_ROLL, LOT_R, NOW, POS, UTC, big_settings, engine_for, ist_date, roll_ctx, run_bt,
                                    settings, stock_bars, stock_world, weekdays)


def at(d, hh, mm):
    return datetime.combine(d, time(hh, mm), IST).astimezone(UTC)


def baseline_pick(res):
    return next(t for t in res["trades"] if t["exit_check"] == "intraday" and t["variant"] == "baseline:PE"
                and (ist_date(t["exit_time"]) - ist_date(t["entry_time"])).days >= 5)


def legs(t):
    return (next(x for x in t["legs"] if x["action"] == a) for a in ("SELL", "BUY"))


# ---------------- 2. the entry bar's own close ----------------
def test_ACCEPTANCE_a_stop_on_the_entry_bars_close_exits_at_the_next_synchronized_open():
    w = stock_world()
    cfg, store, _, _ = w
    before = run_bt(w)
    pick = baseline_pick(before)
    sell, buy = legs(pick)
    day = ist_date(pick["entry_time"])
    exp = date.fromisoformat(pick["expiry"])
    open_px = sell["entry_price"] / (1 - cfg.costs.slippage_pct.options / 100)      # the 09:15 open it was sold at
    # the sold leg opens as before but closes the 09:15 bar far above the stop
    store.save_option_bars("sim", "RELIANCE", exp, sell["strike"], "PE", INTRADAY,
                           [(at(day, 9, 15).isoformat(), round(open_px, 2), 999.0, round(open_px, 2), 500.0, 1.0, 1.0)], NOW)
    after = run_bt(w)
    hit = next(t for t in after["trades"] if t["exit_check"] == "intraday" and t["signal_id"] == pick["signal_id"])
    assert hit["exit_reason"] == "STOP" and hit["exit_basis"] == "synchronized 5-minute bar"
    # decided at the entry bar's 09:15 close, filled at the next synchronized bar's open (the fixture's 12:00 bar)
    assert datetime.fromisoformat(hit["exit_time"]).astimezone(IST) == datetime.combine(day, time(12, 0), IST)
    assert ist_date(hit["entry_time"]) == day


# ---------------- 8. the actual entry bar's timestamp ----------------
def test_ACCEPTANCE_entry_time_is_the_first_synchronized_bar_actually_used():
    w = stock_world()
    cfg, store, _, _ = w
    pick = baseline_pick(run_bt(w))
    sell, buy = legs(pick)
    day = ist_date(pick["entry_time"])
    store._db.execute("DELETE FROM option_bars WHERE underlying='RELIANCE' AND expiry=? AND strike=? AND right='PE' "
                      "AND interval=? AND start_utc=?", (pick["expiry"], buy["strike"], INTRADAY, at(day, 9, 15).isoformat()))
    store._db.commit()                                   # the bought leg did not trade at 09:15 on the entry day
    t = next(x for x in run_bt(w)["trades"] if x["exit_check"] == "intraday" and x["signal_id"] == pick["signal_id"])
    assert datetime.fromisoformat(t["entry_time"]).astimezone(IST) == datetime.combine(day, time(12, 0), IST)
    # the daily execution still enters at the session's open
    d = next(x for x in run_bt(w)["trades"] if x["exit_check"] == "daily" and x["signal_id"] == pick["signal_id"])
    assert datetime.fromisoformat(d["entry_time"]).astimezone(IST).time() == time(9, 15)


# ---------------- 3. maximum loss recomputed after the actual fill ----------------
def test_ACCEPTANCE_max_loss_is_recomputed_from_the_slipped_fill_lots_cut_or_zero_size():
    from app.options.backtest import _Position
    from app.options.payoff import OptionLeg
    from app.options.sizing import size_spread
    w = stock_world()
    cfg, store, base, _ = w
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(date(2025, 3, 1))
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    rec = []
    eng.run_stream(variant(cfg), "RELIANCE", dev, "baseline:PE", Counter(), record=rec)
    sig = rec[0][1]
    budget = cfg.account.capital_inr * cfg.stock_option_selling.max_risk_per_position_pct / 100
    lots = sig.legs[0].lots
    # a fill that keeps almost nothing of the credit: the maximum loss of the signalled lots is above the budget
    from decimal import Decimal
    sold, bought = sig.legs
    fills = [Decimal(str(round(bought.price + 0.05, 2))), Decimal(str(bought.price))]
    one = [OptionLeg(x.action, x.right, x.strike, x.expiry, 1, float(f), x.lot_size) for x, f in zip(sig.legs, fills)]
    n, _, pay = size_spread("credit", one, sold.lot_size, eng.costs, budget, max_lots=lots)
    pos = eng._sized(_Position(sig, list(sig.legs), fills, dev[0], 0, "baseline:PE"), Counter())
    if n == 0:
        assert pos is None
    else:
        assert pos.legs[0].lots == pos.legs[1].lots == n <= lots and pos.max_loss_inr == pytest.approx(float(pay.max_loss_inr))
        assert pos.max_loss_inr <= budget
    # a tiny capital: zero lots after the fill -> not entered, ZERO_SIZE
    tiny = settings(account={"capital_inr": 1000.0}, backtest=cfg.backtest.model_dump(), options=cfg.options.model_dump())
    sk = Counter()
    assert engine_for(tiny, store, base, OptionHistory(store, "sim"))._sized(
        _Position(sig, list(sig.legs), fills, dev[0], 0, "baseline:PE"), sk) is None
    assert sk["ZERO_SIZE_AT_FILL"] == 1
    # every backtest trade carries the maximum loss of its actual fill, which slippage makes larger than the estimate
    res = run_bt(w)
    for t in res["trades"]:
        assert t["max_loss_inr"] <= budget + 0.01
    est = {s.signal_id: s.details["max_loss_inr"] for _, s in rec}
    matched = [t for t in res["trades"] if t["signal_id"] in est and t["exit_check"] == "daily"]
    assert matched and all(t["max_loss_inr"] > est[t["signal_id"]] for t in matched)


def test_the_portfolio_book_carries_the_recomputed_max_loss_of_open_positions():
    w = stock_world()
    cfg, store, base, _ = w
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(date(2025, 3, 1))
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    key = ("RELIANCE", "baseline:PE")
    eng._book, seen = {}, {}
    g = eng.iter_stream(variant(cfg), "RELIANCE", dev, "baseline:PE", Counter(), key=key)
    try:
        while True:
            next(g)                                        # the book as another stream would see it, day by day
            for s, risk in eng._book.get(key, []):
                seen.setdefault(s.signal_id, set()).add(round(risk, 2))
    except StopIteration as stop:
        trades = stop.value
    finally:
        eng._book = None
    assert trades
    for t in trades:
        assert round(t.max_loss_inr, 2) in seen[t.signal_id]   # while open: the maximum loss of the actual fill
    sig_est = {s for t in trades for s in seen[t.signal_id] if s != round(t.max_loss_inr, 2)}
    assert sig_est                                          # while pending: the signal's estimate, a different figure


# ---------------- 7. closing charges on each leg's premium ----------------
def test_ACCEPTANCE_closing_charges_are_on_each_legs_premium_never_the_net_spread_value():
    cfg = big_settings()
    strat = build_stock_selling(cfg)
    c = roll_ctx(cfg, stock_bars())
    costs = CostModel(cfg.costs)
    out = roll_choices(strat, c, HELD_ROLL, 0.8)
    qs = c.quotes.quote(HELD_ROLL.expiry, HELD_ROLL.short_strike, "PE").price(False)
    ql = c.quotes.quote(HELD_ROLL.expiry, HELD_ROLL.long_strike, "PE").price(False)
    sh = HELD_ROLL.shares
    want = float(costs.options(HELD_ROLL.lots, qs * sh, 0).total + costs.options(HELD_ROLL.lots, 0, ql * sh).total)
    assert out["exit"]["closing_charges_inr"] == pytest.approx(want, abs=0.01)
    assert out["exit"]["realised_pnl_inr"] == pytest.approx((HELD_ROLL.credit - 0.8) * sh - want, abs=0.02)
    on_net = float(costs.options(HELD_ROLL.lots, 0.8 * sh, 0.8 * sh).total) / 2      # the old way: the net value as one leg
    assert abs(want - on_net) > 0.5
    # a leg without a price: the charges are unknown, never estimated from the net value
    gone = roll_choices(strat, roll_ctx(cfg, stock_bars(), drop={(1100.0, "PE")}), HELD_ROLL, 0.8)
    assert gone["exit"]["closing_charges_inr"] is None and gone["exit"]["realised_pnl_inr"] is None
    # exit guidance: a stop shows the closing charges per leg and the P&L after them
    p = settings().stock_option_selling
    stop = next(x for x in exit_calls(POS, p, date(2026, 11, 13), 8.0, 1100.0, "UP", costs=costs, leg_px=(10.0, 2.0))
                if x["kind"] == "STOP")
    per_leg = float(costs.options(POS.lots, 10.0 * POS.shares, 0).total + costs.options(POS.lots, 0, 2.0 * POS.shares).total)
    assert stop["closing_charges_inr"] == pytest.approx(per_leg, abs=0.01)
    assert stop["pnl_after_closing_charges_inr"] == pytest.approx(stop["pnl_before_costs_inr"] - per_leg, abs=0.01)


# ---------------- 9. drawdown from the daily mark-to-market equity; mark age apart from holding ----------------
def mtm_equity(trades):
    from app.options.backtest import mtm_equity as f
    return f(trades)


def history_fall_pct(*a):
    from app.options.score import history_fall_pct as f
    return f(*a)


def trade(entry, exit_, net, marks=(), reason="TARGET", mark_time=None):
    return OptionTrade("s", "1", "strategy", "X", "CREDIT_SPREAD", "2025-08-28", [], 1, 1, "id" + entry.isoformat(),
                       at(entry, 9, 15).isoformat(), "RANGE", at(entry, 9, 15).isoformat(), 1.0,
                       at(exit_, 9, 15).isoformat(), 0.0, reason, net, {"total": 0.0}, net, "option_history",
                       marks=[[d.isoformat(), v] for d, v in marks], mark_time=mark_time)


def test_ACCEPTANCE_drawdown_is_from_the_daily_mark_to_market_equity():
    d = [date(2025, 7, 1) + timedelta(days=k) for k in range(6)]
    # deep under water while open, closed at a small profit: the closed-trade sequence has no drawdown
    t = trade(d[0], d[5], 1000.0, marks=[(d[0], -100.0), (d[1], -2000.0), (d[2], -5000.0), (d[3], -1000.0)])
    s = option_stats([t], 1e6, 30)
    assert s["max_drawdown_inr"] == 0.0 and s["max_drawdown_incl_open_inr"] == pytest.approx(5000.0)
    assert "mark-to-market" in s["equity_basis"]["drawdown"]
    # a day without a mark for an open position carries its last one and is counted as stale (d[4]: another
    # position is marked that day, this one is not)
    v = trade(d[4], d[5], 0.0, marks=[(d[4], 0.0)])
    m = mtm_equity([t, v])
    assert m["max_drawdown_inr"] == 5000.0 and m["stale_position_days"] == 1
    # two positions at once: their marks add up on the same day
    u = trade(d[1], d[3], -500.0, marks=[(d[1], -3000.0), (d[2], -3000.0)])
    assert mtm_equity([t, u])["max_drawdown_inr"] == pytest.approx(8000.0)


def test_ACCEPTANCE_open_at_cutoff_reports_mark_age_apart_from_holding_days():
    cut = date(2025, 9, 30)
    o = trade(cut - timedelta(days=10), cut, -700.0, marks=[(cut - timedelta(days=1), -500.0)], reason=OPEN_AT_CUTOFF,
              mark_time=at(cut, 15, 10).isoformat())
    oc = option_stats([o], 1e6, 30)["open_at_cutoff"]
    assert oc["mark_age_minutes"] == [20] and oc["holding_days"] == [10] and "age_days" not in oc
    # the backtest records daily marks for every position held overnight
    res = run_bt(stock_world())
    held = [t for t in res["trades"] if ist_date(t["exit_time"]) > ist_date(t["entry_time"]) + timedelta(days=3)]
    assert held and all(t["marks"] for t in held)
    for t in held:
        days = [date.fromisoformat(m[0]) for m in t["marks"]]
        assert days == sorted(days) and ist_date(t["entry_time"]) <= days[0] and days[-1] < ist_date(t["exit_time"])


# ---------------- 10. the fall percentile reads all history since 2019-01-22 ----------------
def test_ACCEPTANCE_fall_percentile_uses_all_daily_history_since_the_floor_not_the_last_1300_bars():
    cfg = settings(backtest={"test_start": {"1day": date(2019, 1, 22)}})
    days = weekdays(date(2019, 1, 22), date(2026, 6, 5))
    n = len(days)
    assert n > 1300 + 600
    # the first 600 sessions fall 0.5% a session, the rest rise gently: only the full history shows the falls
    closes = [1000.0 * (0.995 ** k) for k in range(600)]
    closes += [closes[-1] * (1.0005 ** k) for k in range(1, n - 600 + 1)]
    bars = [Bar("RELIANCE", daily_start(d), c, c, c, c, 1e6) for d, c in zip(days, closes)]
    idx = [Bar("NIFTY", daily_start(d), 100, 100, 100, 100, None) for d in days]
    vix = [Bar("INDIAVIX", daily_start(d), 14, 14, 14, 14, None) for d in days]
    store = Store(":memory:")
    store.save_history_bars("bars_1d", bars + idx + vix, "sim")
    from app.calendar import MarketCalendar
    from tests.test_phase9_core import FixedRegime
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    eng = OptionEngine(cfg, store, "sim", OptionHistory(store, "sim"), base, lot_sizes={"RELIANCE": 500})
    got = eng.score_inputs("RELIANCE", days[-1])["hist_fall_pct"]["PE"]
    last_1300 = fall_percentile(closes[-1300:], 21, 90, "PE")
    assert last_1300 < 0 < got and got == pytest.approx(100 * (1 - 0.995 ** 21), abs=0.2)
    assert got == history_fall_pct(bars, days[-1], date(2019, 1, 22), [], cfg.stock_option_selling.score)["PE"]
    # nothing after the decision day is read
    assert eng.score_inputs("RELIANCE", days[700])["hist_fall_pct"]["PE"] == history_fall_pct(
        bars[:701], days[700], date(2019, 1, 22), [], cfg.stock_option_selling.score)["PE"]
    # a window across a price break is left out
    assert fall_percentile([100.0] * 40 + [50.0] * 40, 21, 90, "PE") > 0
    assert fall_percentile([100.0] * 40 + [50.0] * 40, 21, 90, "PE", breaks=[40]) == 0.0
