"""Phase 9: the forced exit in the backtest. No stock option position is ever inside the last
`forced_exit_trading_days_before_expiry` (5) trading days. Normally the time exit (10 calendar days) ends a
late position first, so these tests switch the time exit off and show the forced exit firing at E-5 under
both executions (daily closes, and synchronized 5-minute bars)."""
from collections import Counter
from datetime import date, datetime, time

import pytest

from app.options.backtest import FORCED_EXPIRY_EXIT
from app.options.option_history import OptionHistory
from app.options.stock_backtest import variant
from app.options.stock_selling import trading_days_to
from app.quality import IST
from tests.test_phase9_core import BT_FIRST, engine_for, ist_date, stock_world

EXPIRY = date(2025, 7, 29)                    # a Tuesday; E-5 is Tuesday 22 July, E-6 Monday 21 July
E5, E6 = date(2025, 7, 22), date(2025, 7, 21)


@pytest.fixture(scope="module")
def late_world():
    """The stock falls 40 on 7 July 2025: the July put spread neither reaches its target nor its stop, and
    the stock stays above the sold strike, so only the calendar ends the position."""
    return stock_world(shocks={date(2025, 7, 7): -40.0})


def july_trade(w, mode, time_exit, holidays=None):
    cfg, store, base, _ = w
    base.calendar.set_holidays(holidays or {})
    try:
        eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
        eng.set_expiry_floor(BT_FIRST)
        dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
        st = variant(cfg)
        st.time_exit = time_exit
        trades = eng.run_stream(st, "RELIANCE", dev, "baseline:PE", Counter(), exit_check=mode)
    finally:
        base.calendar.set_holidays({})
    return next(t for t in trades if t.expiry == EXPIRY.isoformat()), trades, cfg


@pytest.mark.parametrize("mode", ["daily", "intraday"])
def test_ACCEPTANCE_with_the_time_exit_off_the_forced_exit_fires_at_e_minus_5(late_world, mode):
    t, trades, cfg = july_trade(late_world, mode, time_exit=False)
    assert cfg.stock_option_selling.forced_exit_trading_days_before_expiry == 5
    assert t.exit_reason == FORCED_EXPIRY_EXIT == "FORCED_EXPIRY_EXIT" and t.exit_basis == "rule at the close"
    # decided at the close of E-6 (Monday 21 July), filled at the first price of E-5 (Tuesday 22 July):
    # the session's open on daily bars, the first synchronized 5-minute bar (09:15 here) otherwise
    out = datetime.fromisoformat(t.exit_time).astimezone(IST)
    assert out == datetime.combine(E5, time(9, 15), IST)
    assert trading_days_to(out.date(), EXPIRY) == 5 and trading_days_to(E6, EXPIRY) == 6
    assert t.exit_check == mode and ist_date(t.entry_time) < E6
    # the position was open through the close of E-6 and not at the close of E-5
    assert [m[0] for m in t.marks][-1] == E6.isoformat()
    # no position of the run is ever inside the window
    for x in trades:
        if x.exit_reason != "OPEN_AT_CUTOFF":
            assert trading_days_to(ist_date(x.exit_time), date.fromisoformat(x.expiry)) >= 5


@pytest.mark.parametrize("mode", ["daily", "intraday"])
def test_the_time_exit_comes_first_when_it_is_on_and_the_forced_exit_follows_the_holiday_calendar(late_world, mode):
    # as shipped: the time exit (first session within 10 calendar days of expiry: Monday 21 July) ends it first
    t, _, _ = july_trade(late_world, mode, time_exit=True)
    assert t.exit_reason == "TIME" and ist_date(t.exit_time) == E6
    # a holiday on Friday 25 July: E-5 is then Monday 21 July, and the forced exit moves one session earlier
    h, _, _ = july_trade(late_world, mode, time_exit=False, holidays={date(2025, 7, 25): "test holiday"})
    assert h.exit_reason == FORCED_EXPIRY_EXIT and ist_date(h.exit_time) == E6
