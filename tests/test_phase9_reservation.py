"""Phase 9 review fix (2026-11-16): a pending entry keeps its risk reservation until it fills.

On synchronized 5-minute bars an accepted entry is filled at the first bar of the next session in which
every leg traded, which may be hours after 09:15. From the acceptance of its signal until that fill the
entry RESERVES its estimated maximum loss in the portfolio book; when it fills, the position carries the
maximum loss recomputed at the fill; when it is not made, the reservation is released. Every portfolio
gate reads the book, so a roll decided in between counts the reservation.

Before the fix the entry left the book at 09:15 of its session and came back only when it filled: a roll
decided in between was judged as if the entry did not exist, and the portfolio could end above its
total-risk limit.

The scene (two stocks, total risk limited to 18% of capital = Rs 36,000):
- RELIANCE holds a put spread since 25 June (maximum loss about Rs 17,000);
- INFY's entry is accepted at the close of 1 July (about Rs 17,000 more: about Rs 34,000, within the limit);
  on 2 July its legs first trade together at 12:00, so it fills at 12:00, not 09:15;
- RELIANCE is stopped at 10:00 on 2 July and its roll is decided at 10:05. The replacement risks 1.2 times
  the old spread (about Rs 20,400): with INFY's reservation about Rs 37,300, above the limit - REFUSED.
  Without the reservation the roll would be judged at Rs 20,400 alone and allowed.
"""
from collections import Counter
from dataclasses import replace
from datetime import date

import pytest

from app.options.backtest import DECIDE, FILL
from app.options.option_history import INTRADAY
from app.options.stock_backtest import variant
from tests.test_phase9_core import ist_date
from tests.test_phase9_time_order import (I, R, local, of, recorded_entries, roll_always_offered, roller, run, stop_at_ten,
                                          world)
from tests.two_stock_world import at, dev, engine, two_settings

SIGNAL_DAY, DAY = date(2025, 7, 1), date(2025, 7, 2)
KEYS = [(R, "baseline:PE"), (I, "strategy")]
LIMIT_PCT = 18.0                                   # of Rs 200,000 capital: Rs 36,000


def drop_five(store, sig, day, only=None):
    """Remove the 5-minute bars of `day` of both legs of the signal (`only`: just the bar at (hh, mm))."""
    for leg in sig.legs:
        q = ("DELETE FROM option_bars WHERE underlying=? AND expiry=? AND strike=? AND right=? AND interval=? AND source='sim'"
             " AND start_utc>=? AND start_utc<=?")
        lo, hi = (at(day, *only), at(day, *only)) if only else (at(day, 9, 15), at(day, 15, 25))
        store._db.execute(q, (sig.symbol, leg.expiry.isoformat(), float(leg.strike), leg.right, INTRADAY,
                              lo.isoformat(), hi.isoformat()))
    store._db.commit()


def scene(monkeypatch, limit_pct=LIMIT_PCT, infy="delayed"):
    """(settings, store, base, replay, RELIANCE's June signal, INFY's 1 July signal).
    `infy`: "delayed" - its legs have no 09:15 bar on 2 July (first synchronized bar 12:00);
    "never" - its legs have no 5-minute bar at all that day (the entry is not made); "on time" - untouched."""
    cfg, store, base = world(two_settings(max_total_risk_pct=limit_pct))
    held = recorded_entries(cfg, store, base, KEYS[:1])[KEYS[0]][0][1]          # RELIANCE, signalled 24 June
    eng = engine(cfg, store, base)
    eng.no_entry = {I: {d for d in dev(base) if d < SIGNAL_DAY}}                 # INFY: first signal at the close of 1 July
    rec = []
    eng.run_stream(variant(cfg, off=("market_block",)), I, dev(base), "strategy", Counter(), exit_check="intraday", record=rec)
    d0, new = rec[0]
    assert d0 == SIGNAL_DAY and ist_date(held.created_at.isoformat()) == date(2025, 6, 24)
    if infy != "on time":
        drop_five(store, new, DAY, only=(9, 15) if infy == "delayed" else None)
    stop_at_ten(store, held, DAY)                                                # RELIANCE: stop at 10:00, roll decided at 10:05
    bigger = replace(held, details={**held.details, "max_loss_inr": round(held.details["max_loss_inr"] * 1.2, 2)})
    roll_always_offered(monkeypatch, bigger)
    replay = {KEYS[0]: {date(2025, 6, 24): [held]}, KEYS[1]: {SIGNAL_DAY: [new]}}
    return cfg, store, base, replay, held, new, bigger


def risk(sig) -> float:
    return float(sig.details["max_loss_inr"])


@pytest.mark.parametrize("order", [KEYS, KEYS[::-1]])
def test_ACCEPTANCE_a_delayed_entry_keeps_its_reservation_through_an_intervening_roll_decision(monkeypatch, order):
    cfg, store, base, replay, held, new, bigger = scene(monkeypatch)
    limit = cfg.account.capital_inr * LIMIT_PCT / 100.0
    # hand check of the scene: the two spreads fit, the replacement alone fits, the replacement with INFY's entry does not
    assert limit == 36000.0
    assert risk(held) + risk(new) < limit and risk(bigger) < limit < risk(bigger) + risk(new)
    trades, _, gate, trace = run(cfg, store, base, order, strategy=roller(cfg), replay=replay, until=date(2025, 7, 22))
    stopped = of(trades, R)[0]
    assert stopped["exit_reason"] == "STOP" and local(stopped["exit_time"]) == local(at(DAY, 10, 5).isoformat())
    # the roll decided at 10:05 counted INFY's entry, which filled only at 12:00
    assert stopped["roll_refused"] == "CORRELATED_LIMIT"
    assert gate.summary()["refused"] == {"CORRELATED_LIMIT": 1}
    entered = of(trades, I)[0]
    assert entered["signal_id"] == new.signal_id and local(entered["entry_time"]) == local(at(DAY, 12, 0).isoformat())
    step = lambda moment, phase, key: trace.index((moment, phase, key))
    assert step(at(DAY, 10, 5), DECIDE, KEYS[0]) < step(at(DAY, 12, 0), FILL, KEYS[1])
    # the total risk carried never exceeded the limit: RELIANCE's spread, then INFY's alone
    assert stopped["max_loss_inr"] + risk(new) <= limit and entered["max_loss_inr"] <= limit


def test_controls_the_roll_is_allowed_with_room_and_a_rejected_entry_releases_its_reservation(monkeypatch):
    # room for both (25% = Rs 50,000): the same roll passes the gate (it then finds no fill at 10:05)
    cfg, store, base, replay, *_ = scene(monkeypatch, limit_pct=25.0)
    trades, _, gate, _ = run(cfg, store, base, KEYS, strategy=roller(cfg), replay=replay, until=date(2025, 7, 22))
    assert of(trades, R)[0]["roll_refused"] == "ROLL_NOT_FILLED" and gate.summary()["refused"] == {}
    # INFY's entry is not made (its legs have no 5-minute bar on 2 July: known at 09:15): the reservation is
    # released, and the roll at 10:05 is judged without it
    cfg, store, base, replay, held, new, _ = scene(monkeypatch, infy="never")
    trades, sk, gate, _ = run(cfg, store, base, KEYS, strategy=roller(cfg), replay=replay, until=date(2025, 7, 22))
    assert not of(trades, I) and sk["NO_SYNCHRONIZED_BAR_AT_ENTRY"] + sk["5_MINUTE_BARS_NOT_STORED_AT_ENTRY"] == 1
    assert of(trades, R)[0]["roll_refused"] == "ROLL_NOT_FILLED" and gate.summary()["refused"] == {}


def test_the_book_holds_the_estimate_until_the_fill_and_the_recomputed_maximum_loss_after_it(monkeypatch):
    """INFY's stream stepped on its own: what the portfolio book holds for it before every step."""
    cfg, store, base, replay, _, new, _ = scene(monkeypatch)
    eng = engine(cfg, store, base)
    eng._book = {}
    seen = []                                         # (moment, phase, [(signal id, risk)]) before each step
    try:
        g = eng.iter_stream(variant(cfg), I, [d for d in dev(base) if d < date(2025, 7, 22)], "strategy", Counter(), None,
                            "intraday", None, replay[KEYS[1]], KEYS[1])
        while True:
            try:
                moment, phase = next(g)
            except StopIteration as stop:
                trades = stop.value
                break
            seen.append((moment, phase, [(s.signal_id, r) for s, r in eng._book[KEYS[1]]]))
    finally:
        eng._book = None
    fill = at(DAY, 12, 0)
    during = [book for moment, phase, book in seen if at(SIGNAL_DAY, 15, 30) < moment <= fill]
    # from acceptance (the close of 1 July) up to and including the step that fills it: reserved at the estimate
    assert len(during) >= 2 and all(book == [(new.signal_id, risk(new))] for book in during)
    assert (fill, FILL) in [(m, p) for m, p, _ in seen]
    # after the fill: the position's maximum loss recomputed at the actual fill prices
    t = trades[0]
    after = next(book for moment, _, book in seen if moment > fill)
    assert after == [(new.signal_id, t.max_loss_inr)] and t.max_loss_inr != risk(new)
    # nothing before the signal, nothing once the position is closed
    assert all(book == [] for moment, _, book in seen if moment <= at(SIGNAL_DAY, 15, 30))
    assert seen[-1][2] == [] or t.exit_reason == "OPEN_AT_CUTOFF"
