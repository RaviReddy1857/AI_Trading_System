"""Review 7 (Codex review of 307b192..9dd57af): nine fixes, each with a test that failed before the fix.

1. pending paper trades reserve risk, margin, sector and correlation capacity until they fill or expire;
2. realised P&L today / this month uses the open P&L's basis (the previous closing mark) for older positions;
3. one deposit rule for equity and drawdown;
4. a pending paper trade past valid-until expires whatever the quote;
5. alerts: SENDING before the send, no blind resend of information alerts after an uncertain delivery;
6. a signal and its paper trade are saved together; restore re-creates a missing paper trade;
7. an exit call stays active while the regime is UNKNOWN or prices are missing;
8. pending fills use the first quote after the bar close, not the latest tick;
9. alert dispatch and the watchdog heartbeat do not wait for engine.step().
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.journal import CASH_INDEX, STOCK_OPTIONS, pool_account, record_marks
from app.risk.engine import decide
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, ZeroCosts, cash_signal, no_corr
from tests.phase6_fixtures import NOW, FakeMarks, new_journal, settings


def journal_with_start(s):
    j, store = new_journal(s)
    for pool, cap in ((CASH_INDEX, s.account.capital_inr), (STOCK_OPTIONS, s.stock_option_selling.backtest.capital_inr)):
        j.ensure_equity_start(pool, NOW, cap)
    return j, store


class _Paper:
    """The paper trader's `open` only (it writes the PENDING row from the approved decision)."""

    def __init__(self, s, j):
        from app.paper import PaperTrader
        self.p = PaperTrader(s, j, None, None, None, None, None)

    def open(self, sig, dec, now):
        return self.p.open(sig, dec, now)


# ---------------- 1. pending paper trades reserve capacity ----------------
def test_1_three_reliance_signals_before_any_fill_only_those_within_limits_are_approved():
    s = settings()                       # capital Rs 2,00,000: 1% risk = 2,000; position cap 25% = 50,000; 2 per sector
    j, _ = journal_with_start(s)
    paper = _Paper(s, j)
    out = []
    for k in range(3):
        sig = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id=f"rel-{k}")
        acct, _ = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())
        d = decide(sig, acct, GOOD_MARKET, s, ZeroCosts(), no_corr, SECTORS)
        out.append(d)
        j.save_signal(sig, d, NOW)
        paper.open(sig, d, NOW)                                    # PENDING: not filled yet
    assert [d.status for d in out] == ["APPROVED", "APPROVED", "NO_TRADE"], [d.reasons for d in out]
    assert "SECTOR_LIMIT" in out[2].reason_codes
    # the second is sized on the budget left in RELIANCE's group (the same symbol is one correlated group)
    held = out[1].facts["sizing"]["held_in_correlated_group_inr"]
    assert held == out[0].risk_inr
    acct, f = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())
    assert f["pending_reserved"] == 2 and len(acct.positions) == 2
    assert all(p.origin == "PENDING" and p.open_pnl_today_inr == 0.0 for p in acct.positions)


def test_1_a_pending_trade_stops_reserving_once_expired_filled_or_taken():
    s = settings()
    j, _ = journal_with_start(s)
    sig = replace(cash_signal(entry=1000.0, stop=990.0, target=1030.0), signal_id="rel-x")
    acct, _ = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())
    d = decide(sig, acct, GOOD_MARKET, s, ZeroCosts(), no_corr, SECTORS)
    j.save_signal(sig, d, NOW)
    _Paper(s, j).open(sig, d, NOW)
    assert len(pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts())[0].positions) == 1
    after = sig.valid_until + timedelta(seconds=1)
    assert pool_account(j, s, CASH_INDEX, after, FakeMarks(), ZeroCosts())[0].positions == ()
    j.record_decision(sig.signal_id, {"decision": "TOOK", "fill_price": 1000.5}, NOW)
    acct, _ = pool_account(j, s, CASH_INDEX, NOW, FakeMarks({"RELIANCE": 1000.0}), ZeroCosts())
    assert [p.origin for p in acct.positions] == ["SIGNAL"]           # my position, not also the pending twin


# ---------------- 2. realised today / this month on the open P&L's basis ----------------
def _old_long(j, qty):
    """A swing position bought at 1,000 on Thursday 12 November (before today, this month)."""
    return j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": qty,
                                  "entry_price": 1000.0, "entry_date": "2026-11-12", "stop": 900.0}, NOW)


def test_2_review_case_1000_1100_1050_keeps_daily_stop_after_closing():
    s = settings()                                  # daily stop 2% of Rs 2,00,000 = Rs 4,000
    j, _ = journal_with_start(s)
    p = _old_long(j, 80)
    friday = NOW - timedelta(days=3)
    record_marks(j, CASH_INDEX, friday, FakeMarks({"INFY": 1100.0}), ZeroCosts())    # previous close: 1,100
    today_marks = FakeMarks({"INFY": 1050.0})
    sig = cash_signal(symbol="TCS", entry=100.0, stop=95.0, target=110.0)

    def daily_stop():
        acct, f = pool_account(j, s, CASH_INDEX, NOW, today_marks, ZeroCosts())
        return "DAILY_STOP" in decide(sig, acct, GOOD_MARKET, s, ZeroCosts(), no_corr, SECTORS).reason_codes, f

    held, _ = daily_stop()
    assert held                                                   # open: (1,050 - 1,100) x 80 = -4,000 today
    j.close_position(p.position_id, {"exit_price": 1050.0}, NOW, ZeroCosts())
    closed, f = daily_stop()
    assert f["realised_today_inr"] == -4000.0                    # not +4,000 (1,050 - 1,000 since entry)
    assert closed                                                 # the stop stays on after closing
    assert j.realised(CASH_INDEX) == Decimal("4000.00")          # all-time realised is still since entry


def test_2_month_boundary_uses_the_last_mark_before_the_month():
    s = settings()
    j, _ = journal_with_start(s)
    p = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                               "entry_price": 1000.0, "entry_date": "2026-10-28", "stop": 900.0}, NOW)
    record_marks(j, CASH_INDEX, datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc), FakeMarks({"INFY": 1200.0}),
                 ZeroCosts())                                     # October's last close: 1,200
    record_marks(j, CASH_INDEX, NOW - timedelta(days=3), FakeMarks({"INFY": 1150.0}), ZeroCosts())
    j.close_position(p.position_id, {"exit_price": 1100.0}, NOW, ZeroCosts())
    assert j.realised(CASH_INDEX, since=date(2026, 11, 1)) == Decimal("-1000.00")   # (1,100 - 1,200) x 10
    assert j.realised(CASH_INDEX, since=date(2026, 11, 16)) == Decimal("-500.00")   # (1,100 - 1,150) x 10
    assert j.realised(CASH_INDEX) == Decimal("1000.00")


def test_2_a_partial_close_and_a_position_opened_today_and_a_missing_mark():
    s = settings()
    j, _ = journal_with_start(s)
    p = _old_long(j, 10)
    record_marks(j, CASH_INDEX, NOW - timedelta(days=3), FakeMarks({"INFY": 1100.0}), ZeroCosts())
    j.close_position(p.position_id, {"exit_price": 1050.0, "quantity": 4}, NOW, ZeroCosts())
    assert j.realised(CASH_INDEX, since=date(2026, 11, 16)) == Decimal("-200.00")    # 4 x (1,050 - 1,100)
    q = j.add_manual_position({"symbol": "TCS", "trade_type": "INTRADAY_STOCKS", "direction": "LONG", "quantity": 5,
                               "entry_price": 100.0, "entry_date": "2026-11-16", "stop": 90.0}, NOW)
    j.close_position(q.position_id, {"exit_price": 104.0}, NOW, ZeroCosts())        # opened today: since entry
    assert j.realised(CASH_INDEX, since=date(2026, 11, 16)) == Decimal("-180.00")
    r = _old_long(j, 3)                                                              # no mark at an earlier close
    j.close_position(r.position_id, {"exit_price": 1010.0}, NOW, ZeroCosts())
    assert j.realised(CASH_INDEX, since=date(2026, 11, 16)) is None                  # not known: never assumed


# ---------------- 3. one deposit rule for equity and drawdown ----------------
def _dd(acct, s):
    from app.risk.limits import drawdown_state
    dd, factor, code, f = drawdown_state(acct.equity_curve, acct.flows, s.risk.drawdown_halve_pct, s.risk.drawdown_pause_pct)
    return f["drawdown_pct"], code, factor


def test_3_a_deposit_alone_gives_zero_drawdown():
    s = settings()
    j, _ = journal_with_start(s)                                   # start: Rs 2,00,000 at the close of 15 November
    flows = ((date(2026, 11, 16), 50000.0),)
    acct, f = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts(), flows)
    assert f["equity_inr"] == 250000.0                             # equity includes the money in the account
    assert _dd(acct, s) == (0.0, None, 1.0)


def test_3_a_withdrawal_alone_gives_zero_drawdown_and_lower_equity():
    from app.journal import close_equity_day
    s = settings()
    j, _ = journal_with_start(s)
    flows = ((date(2026, 11, 16), -50000.0),)
    acct, f = pool_account(j, s, CASH_INDEX, NOW, FakeMarks(), ZeroCosts(), flows)
    assert f["equity_inr"] == 150000.0 and _dd(acct, s)[0] == 0.0
    after_close = NOW.replace(hour=11)                             # 16:30 IST
    assert close_equity_day(j, s, CASH_INDEX, after_close, flows)["equity_inr"] == 150000.0
    tuesday = NOW + timedelta(days=1)
    acct, f = pool_account(j, s, CASH_INDEX, tuesday, FakeMarks(), ZeroCosts(), flows)
    assert f["equity_inr"] == 150000.0 and _dd(acct, s)[0] == 0.0


def test_3_a_trading_loss_after_a_deposit_gives_the_hand_calculated_drawdown():
    from app.journal import close_equity_day
    s = settings()
    j, _ = journal_with_start(s)
    flows = ((date(2026, 11, 16), 100000.0),)                      # Monday: Rs 1,00,000 deposited
    close_equity_day(j, s, CASH_INDEX, NOW.replace(hour=11), flows)    # Monday close: 2,00,000 + 1,00,000 = 3,00,000
    assert j.equity_rows(CASH_INDEX)[-1] == (date(2026, 11, 16), 300000.0)
    tuesday = NOW + timedelta(days=1)
    p = j.add_manual_position({"symbol": "INFY", "trade_type": "INTRADAY_STOCKS", "direction": "LONG", "quantity": 100,
                               "entry_price": 1000.0, "entry_date": "2026-11-17", "stop": 800.0}, tuesday)
    j.close_position(p.position_id, {"exit_price": 850.0}, tuesday, ZeroCosts())      # Tuesday: -15,000
    acct, f = pool_account(j, s, CASH_INDEX, tuesday, FakeMarks(), ZeroCosts(), flows)
    assert f["equity_inr"] == 285000.0
    # by hand: the deposit is not profit; peak 3,00,000 (after the deposit), now 2,85,000 -> 15,000 / 3,00,000 = 5%
    assert _dd(acct, s) == (5.0, None, 1.0)
