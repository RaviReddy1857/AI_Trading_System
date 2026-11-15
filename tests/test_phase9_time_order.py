"""Phase 9 time-ordering fixes (2026-11-15), each test failing on the engine before the fix:

1. The portfolio backtest runs every stock's events (entries, exits, rolls, pending entries, signals) in
   ONE queue in time order, so a decision sees the portfolio as it stands at that moment. Reversing the
   order of the stocks changes nothing, except between truly simultaneous events, where the documented
   priority (the order the streams are given: most liquid first) decides.
2. A roll decided inside a session uses correlations of completed sessions BEFORE that day only:
   changing only that day's closing prices does not change the decision.
"""
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime

import pytest

import app.options.backtest as bt
from app.history import daily_start
from app.options.backtest import DECIDE, FILL, SIGNAL, session_end
from app.options.portfolio import Correlations, PortfolioGate
from app.options.stock_backtest import variant
from app.quality import IST
from tests.test_phase9_core import NOW, ist_date
from tests.two_stock_world import A, B, add_five, at, dev, engine, two_settings, two_world, write_options

R, I = A, B                      # RELIANCE, INFY


def world(cfg=None, closes=None):
    cfg, store, base = two_world(closes, cfg)
    write_options(store, cfg)
    return cfg, store, base


UNTIL = date(2025, 7, 22)        # the tests of non-simultaneous events stop before the next same-close signals (25 July)


def run(cfg, store, base, streams, mode="intraday", prepare=None, until=None, **kw):
    """One portfolio run on a fresh engine and gate: (trades as dicts, skipped, gate, trace).
    `until`: test only the sessions before this date."""
    eng = engine(cfg, store, base)
    if prepare:
        prepare(eng)
    st = kw.pop("strategy", None) or variant(cfg)
    sk, trace = Counter(), []
    gate = PortfolioGate(cfg, eng)
    sessions = [d for d in dev(base) if until is None or d < until]
    trades = eng.run_portfolio(st, streams, sessions, sk, gate, mode, trace=trace, **kw)
    return [asdict(t) for t in trades], sk, gate, trace


def local(text):
    return datetime.fromisoformat(text).astimezone(IST)


def of(trades, sym):
    return [t for t in trades if t["symbol"] == sym]


# ---------------- 1. one queue of events in time order ----------------
def test_ACCEPTANCE_reversing_the_stock_order_does_not_change_a_synchronized_portfolio_run():
    """One spread may be open. INFY holds one and reaches its target on 9 July: decided at the 12:00 bar's
    close, filled at the 15:25 open. RELIANCE may signal from 9 July on, at that session's close (15:30):
    INFY's slot is free by then, whichever stock is listed first. Before the fix a stock's whole session
    was run before the next stock's, so with RELIANCE listed first its 15:30 signal was judged before
    INFY's 15:25 exit had been run, and was refused."""
    cfg, store, base = world(two_settings(max_open_spreads=1))
    days = dev(base)
    free_from = date(2025, 7, 9)

    def hold_back(eng):                       # no RELIANCE entry before 9 July (the engine's own exclusion list)
        eng.no_entry = {R: {d for d in days if d < free_from}}

    # the fixture's index is weak in early July; the market block is not what is tested here
    rules = lambda: variant(cfg, off=("market_block",))
    fwd, _, _, trace = run(cfg, store, base, [(R, "strategy"), (I, "strategy")], prepare=hold_back, until=UNTIL,
                           strategy=rules())
    rev, _, _, _ = run(cfg, store, base, [(I, "strategy"), (R, "strategy")], prepare=hold_back, until=UNTIL,
                       strategy=rules())
    assert fwd == rev                                                    # identical, trade for trade
    first_i = next(t for t in of(fwd, I) if t["expiry"] == "2025-07-29")      # INFY's July spread
    assert first_i["exit_reason"] == "TARGET" and local(first_i["exit_time"]) == local(at(free_from, 15, 25).isoformat())
    first_r = of(fwd, R)[0]
    assert ist_date(first_r["signal_time"]) == free_from                 # signalled at the close INFY left before
    assert ist_date(first_r["entry_time"]) == days[days.index(free_from) + 1]
    # every step ran in time order; on 9 July INFY's fill came before RELIANCE's signal although RELIANCE is listed first
    keys = [(t, phase) for t, phase, _ in trace]
    assert keys == sorted(keys)
    step = lambda moment, phase, key: trace.index((moment, phase, key))
    assert step(at(free_from, 15, 25), FILL, (I, "strategy")) < step(session_end(free_from), SIGNAL, (R, "strategy"))
    # at one instant: decisions, then signals, then fills
    assert (DECIDE, SIGNAL, FILL) == (0, 1, 2)


def test_ACCEPTANCE_reversing_the_stock_order_does_not_change_a_daily_portfolio_run():
    """Daily execution: INFY's target is decided at the close of Friday 27 June and filled at Monday's
    open (09:15). RELIANCE may signal from Monday on, at Monday's close: the slot is free in either
    order. Before the fix, RELIANCE listed first was refused on Monday and entered a day later."""
    cfg, store, base = world(two_settings(max_open_spreads=1))
    days = dev(base)
    fri, mon = date(2025, 6, 27), date(2025, 6, 30)
    plain, _, _, _ = run(cfg, store, base, [(I, "strategy")], mode="daily")
    held = plain[0]
    assert ist_date(held["entry_time"]) < fri < ist_date(held["exit_time"])
    for leg, px in zip(held["legs"], (0.5, 0.3)):                        # the spread is nearly worthless at Friday's close
        store.save_option_bars("sim", I, date.fromisoformat(held["expiry"]), leg["strike"], leg["right"], "1day",
                               [(daily_start(fri).isoformat(), px, px, px, px, 1000.0, 500000.0)], NOW)

    def hold_back(eng):
        eng.no_entry = {R: {d for d in days if d < mon}}

    fwd, _, _, trace = run(cfg, store, base, [(R, "strategy"), (I, "strategy")], mode="daily", prepare=hold_back, until=UNTIL)
    rev, _, _, _ = run(cfg, store, base, [(I, "strategy"), (R, "strategy")], mode="daily", prepare=hold_back, until=UNTIL)
    assert fwd == rev
    out = of(fwd, I)[0]
    assert out["exit_reason"] == "TARGET" and local(out["exit_time"]) == local(daily_start(mon).isoformat())
    assert ist_date(of(fwd, R)[0]["signal_time"]) == mon
    keys = [(t, phase) for t, phase, _ in trace]
    assert keys == sorted(keys)


def test_ACCEPTANCE_truly_simultaneous_signals_follow_the_documented_priority():
    """Both stocks signal at the same close (24 June, 15:30) and one spread may be open: the stream
    listed first gets it. This is the only way the order of the stocks can matter."""
    cfg, store, base = world(two_settings(max_open_spreads=1))
    day = date(2025, 6, 24)
    fwd, sk, _, _ = run(cfg, store, base, [(R, "strategy"), (I, "strategy")])
    rev, _, _, _ = run(cfg, store, base, [(I, "strategy"), (R, "strategy")])
    first = lambda ts: min(ts, key=lambda t: t["entry_time"])
    assert first(fwd)["symbol"] == R and first(rev)["symbol"] == I
    assert ist_date(first(fwd)["signal_time"]) == ist_date(first(rev)["signal_time"]) == day
    assert sk["NO_SIGNAL:CORRELATED_LIMIT"] >= 1
    # the same run twice is identical
    assert run(cfg, store, base, [(R, "strategy"), (I, "strategy")])[0] == fwd


# ---------------- rolls decided inside a session ----------------
def recorded_entries(cfg, store, base, keys):
    """The entry signals of each stream run on its own: {stream key: [(signal day, signal)]}."""
    eng = engine(cfg, store, base)
    out = {}
    for key in keys:
        rec = []
        eng.run_stream(variant(cfg), key[0], dev(base), key[1], Counter(), exit_check="intraday", record=rec)
        out[key] = rec
    return out


def stop_at_ten(store, sig, day):
    """Both legs of the spread trade at 10:00 and 10:05 on `day`, the sold one far above the stop: the
    stop is decided at the 10:00 bar's close (10:05) and filled at the 10:05 bar's open."""
    sold, bought = sig.legs
    add_five(store, sig.symbol, sold.expiry, sold.strike, sold.right,
             [(day, 10, 0, round(sig.stop + 6, 2)), (day, 10, 5, round(sig.stop + 6.5, 2))])
    add_five(store, sig.symbol, bought.expiry, bought.strike, bought.right, [(day, 10, 0, 1.0), (day, 10, 5, 1.1)])


def roll_always_offered(monkeypatch, replacement):
    """The roll rules themselves are not under test here: every stopped spread is offered `replacement`
    with enough roll credit, so the decision is left to the portfolio gate alone."""
    monkeypatch.setattr(bt, "roll_choices", lambda *a, **k: {"roll_signal": replacement, "refused_by": None})
    monkeypatch.setattr(bt, "roll_money", lambda *a, **k: {"roll_net_credit_pct_of_width": 50.0})


def roller(cfg):
    st = variant(cfg)
    st.exit_policy = "roll_rules"
    return st


def test_ACCEPTANCE_a_roll_decided_inside_the_session_sees_the_portfolio_as_it_stands_then(monkeypatch):
    """One spread may be open; two were entered earlier (replayed entries, as the exit-policy comparison
    does). On 9 July RELIANCE is stopped at 10:00 and its roll is decided at 10:05, while INFY's spread
    is still open (it leaves at 15:25): the roll is refused. Before the fix, with INFY listed first its
    whole session - the 15:25 exit included - had already been run, and the roll was allowed."""
    cfg, store, base = world(two_settings(max_open_spreads=1))
    keys = [(R, "baseline:PE"), (I, "baseline:PE")]
    rec = recorded_entries(cfg, store, base, keys)
    day = date(2025, 7, 9)
    first = rec[keys[0]][0][1]
    stop_at_ten(store, first, day)
    roll_always_offered(monkeypatch, first)
    replay = {k: {} for k in keys}
    for k in keys:
        d0, sig = rec[k][0]
        replay[k][d0] = [sig]                                            # the June entry of each stock, nothing later
    fwd, _, gate_f, trace = run(cfg, store, base, keys, strategy=roller(cfg), replay=replay)
    rev, _, gate_r, _ = run(cfg, store, base, keys[::-1], strategy=roller(cfg), replay=replay)
    assert fwd == rev
    stopped = of(fwd, R)[0]
    assert stopped["exit_reason"] == "STOP" and local(stopped["exit_time"]) == local(at(day, 10, 5).isoformat())
    assert stopped["roll_refused"] == "CORRELATED_LIMIT"                 # INFY's spread was open at 10:05
    assert local(of(fwd, I)[0]["exit_time"]) == local(at(day, 15, 25).isoformat())
    assert gate_f.summary()["refused"] == gate_r.summary()["refused"] == {"CORRELATED_LIMIT": 1}
    # the decision (10:05) ran before INFY's fill (15:25), and before RELIANCE's own fill at the 10:05 open
    step = lambda moment, phase, key: trace.index((moment, phase, key))
    assert step(at(day, 10, 5), DECIDE, keys[0]) < step(at(day, 10, 5), FILL, keys[0]) < step(at(day, 15, 25), FILL, keys[1])


# ---------------- 2. correlations of completed sessions only ----------------
def spike(day):
    """Both stocks close 10% higher on `day` (closing prices only): enough to correlate their returns."""
    plain = two_world()[2]
    close = {sym: next(b.close for b in plain.daily(sym) if ist_date(b.start.isoformat()) == day) for sym in (R, I)}
    return {(sym, day): round(px * 1.10, 2) for sym, px in close.items()}


def roll_decision_on(day, closes, monkeypatch):
    """RELIANCE's put spread is stopped at 10:00 on `day`, while INFY holds one; one position is allowed
    per correlated group. Returns (the stopped trade, the gate, the engine's base)."""
    cfg, store, base = world(two_settings(group_max_positions=1), closes)
    keys = [(R, "baseline:PE"), (I, "baseline:PE")]
    first = recorded_entries(cfg, store, base, keys[:1])[keys[0]][0][1]
    stop_at_ten(store, first, day)
    roll_always_offered(monkeypatch, first)
    trades, _, gate, _ = run(cfg, store, base, keys, strategy=roller(cfg))
    stopped = of(trades, R)[0]
    assert stopped["signal_id"] == first.signal_id and local(stopped["exit_time"]) == local(at(day, 10, 5).isoformat())
    assert any(ist_date(t["entry_time"]) < day < ist_date(t["exit_time"]) for t in of(trades, I))   # INFY was open
    return stopped, gate, base


def test_ACCEPTANCE_changing_only_that_days_closing_prices_does_not_change_an_intraday_roll_decision(monkeypatch):
    day, before = date(2025, 7, 1), date(2025, 6, 30)
    decision = lambda t: (t["exit_reason"], t["roll_refused"], t["exit_time"], t["net_pnl"])
    plain, gate, base = roll_decision_on(day, None, monkeypatch)
    # the two stocks are not correlated: the roll is allowed (it then finds no fill at 10:05 and the spread is closed)
    assert plain["exit_reason"] == "STOP" and plain["roll_refused"] == "ROLL_NOT_FILLED"
    assert gate.summary()["refused"] == {}
    # only that day's closing prices changed: with them the 60-session correlation would be above the threshold ...
    changed, gate_c, base_c = roll_decision_on(day, spike(day), monkeypatch)
    cfg_p = two_settings().stock_option_selling.portfolio
    corr = Correlations(base_c.daily, base_c.session_dates(), base_c.breaks, cfg_p.correlation_window,
                        cfg_p.correlation_min_overlap)
    assert corr.at(day, R, I) >= cfg_p.correlation_threshold > corr.at(day, R, I, include_day=False)
    # ... but 10:05 is before that close: the decision is the same
    assert decision(changed) == decision(plain) and changed["roll_refused"] != "CORRELATED_LIMIT"
    # control: the same change one session EARLIER is known at 10:05, and the roll is refused
    earlier, gate_e, _ = roll_decision_on(day, spike(before), monkeypatch)
    assert earlier["roll_refused"] == "CORRELATED_LIMIT" and gate_e.summary()["refused"]["CORRELATED_LIMIT"] >= 1


def test_correlation_inside_a_session_equals_the_previous_sessions_and_the_gate_asks_for_it():
    cfg, store, base = two_world()
    days = base.session_dates()
    p = cfg.stock_option_selling.portfolio
    corr = Correlations(base.daily, days, base.breaks, p.correlation_window, p.correlation_min_overlap)
    d = days[200]
    assert corr.at(d, R, I, include_day=False) == corr.at(days[199], R, I)
    assert corr.at(d, R, I) != corr.at(d, R, I, include_day=False)
    # a decision at the day's close may use the day; a new signal is such a decision
    seen = []

    class Spy:
        universe = [R, I]

        def __call__(self, sym, d, sig, replacing, intraday=False):
            seen.append(intraday)
            return None

    eng = engine(cfg, store, base)
    eng._gate = Spy()
    try:
        assert eng._roll_gate(R, d, None, None) is None and eng._roll_gate(R, d, None, None, intraday=True) is None
    finally:
        eng._gate = None
    assert seen == [False, True]
