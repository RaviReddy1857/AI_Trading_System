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


# ---------------- 4. a pending paper trade past valid-until expires whatever the quote ----------------
def _trader():
    from tests.test_phase6_paper import trader
    return trader()


def test_4_past_valid_until_a_live_fresh_quote_does_not_fill():
    from tests.test_phase6_paper import q
    from tests.phase6_fixtures import approved
    p, j, m, _, _ = _trader()
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), sig.created_at)
    late = sig.valid_until + timedelta(minutes=30)                 # the laptop slept through the fill window
    m.cash_q["RELIANCE"] = q(100.3, 100.5, 100.4, late - timedelta(seconds=1))
    p.fill_pending(late, {sig.signal_id: sig})
    row = j.paper_trade(sig.signal_id)
    assert row["status"] == "NOT_FILLED" and "valid-until" in row["exit_reason"]


def test_4_after_restart_without_the_signal_or_outside_hours_it_still_expires():
    from tests.phase6_fixtures import approved
    p, j, m, _, _ = _trader()
    sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
    p.open(sig, approved(quantity=10), sig.created_at)
    late = sig.valid_until + timedelta(hours=12)                   # after the close: no fill pass runs
    assert p.expire_pending(late) == [sig.signal_id]               # the signal itself is not needed
    assert j.paper_trade(sig.signal_id)["status"] == "NOT_FILLED"


def test_4_engine_step_expires_pending_trades_outside_market_hours():
    from tests.test_phase6_engine import make
    from tests.phase6_fixtures import approved
    c, eng = make()
    with c:
        sig = cash_signal(entry=100.0, stop=95.0, target=110.0)
        eng.j.save_signal(sig, approved(quantity=10), sig.created_at)
        eng.paper.open(sig, approved(quantity=10), sig.created_at)
        saturday = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)
        eng.step(saturday)
        assert eng.j.paper_trade(sig.signal_id)["status"] == "NOT_FILLED"


# ---------------- 5. SENDING before the send; no blind resend after an uncertain delivery ----------------
class AcceptThenTimeout:
    """A Telegram transport that delivers the message, then times out before the reply arrives."""

    def __init__(self, times=1):
        self.delivered, self.times = [], times

    def __call__(self, url, body, timeout):
        self.delivered.append(json.loads(body)["text"])
        if self.times:
            self.times -= 1
            raise TimeoutError("The read operation timed out")
        return 200, b'{"ok": true}'


def _book(s=None):
    from app.alerts import AlertBook
    from app.store import Store
    s = s or settings()
    s = s.model_copy(update={"alerts": s.alerts.model_copy(update={"telegram_enabled": True})})
    store = Store(":memory:")
    return AlertBook(store, s), store, s


def _sender(transport):
    from app.telegram import CHAT_ENV, TOKEN_ENV, TelegramSender
    return TelegramSender(post=transport, env={TOKEN_ENV: "t0ken", CHAT_ENV: "42"})


def test_5_a_timeout_after_the_send_is_uncertain_and_an_information_alert_is_not_resent():
    from app.telegram import SendError
    tr = AcceptThenTimeout()
    try:
        _sender(tr).send("x")
        raise AssertionError("no error")
    except SendError as e:
        assert e.uncertain and "t0ken" not in str(e)
    book, _, _ = _book()
    tr = AcceptThenTimeout()
    book.enqueue("signal:abc", "SIGNAL", "SIGNAL - RELIANCE LONG", NOW)
    book.dispatch(NOW, _sender(tr))
    assert book.get("signal:abc")["status"] == "UNCERTAIN"
    for k in range(1, 6):
        book.dispatch(NOW + timedelta(minutes=k), _sender(tr))
    assert tr.delivered == ["SIGNAL - RELIANCE LONG"]                  # delivered once, never resent
    assert book.view()["uncertain"][0]["key"] == "signal:abc"


def test_5_an_exit_call_is_resent_with_its_alert_key_so_the_duplicate_is_recognisable():
    book, _, _ = _book()
    tr = AcceptThenTimeout()
    key = "exit:p1:STOP:1"
    book.enqueue(key, "EXIT_CALL", "EXIT CALL [STOP] RELIANCE", NOW, NOW + timedelta(minutes=15))
    book.dispatch(NOW, _sender(tr))
    assert book.get(key)["status"] == "PENDING"
    book.dispatch(NOW + timedelta(minutes=5), _sender(tr))
    assert book.get(key)["status"] == "SENT"
    assert len(tr.delivered) == 2 and all(f"Alert key: {key}" in t for t in tr.delivered)


def test_5_urgent_system_alerts_resend_with_the_key_and_a_clear_refusal_is_a_plain_retry():
    book, _, _ = _book()
    tr = AcceptThenTimeout()
    book.enqueue("kill:2026-11-16:10:31", "KILL_SWITCH", "KILL SWITCH ON", NOW)
    book.dispatch(NOW, _sender(tr))
    book.dispatch(NOW + timedelta(minutes=5), _sender(tr))
    assert book.get("kill:2026-11-16:10:31")["status"] == "SENT" and len(tr.delivered) == 2
    assert all("Alert key: kill:2026-11-16:10:31" in t for t in tr.delivered)

    def refused(url, body, timeout):
        raise OSError("connection refused")                          # nothing reached Telegram
    book.enqueue("signal:def", "SIGNAL", "SIGNAL - TCS", NOW)
    book.dispatch(NOW, _sender(refused))
    assert book.get("signal:def")["status"] == "PENDING"            # a certain failure is retried


def test_5_sending_is_recorded_before_the_send_and_a_crash_mid_send_is_uncertain_at_restart():
    from app.alerts import AlertBook
    book, store, s = _book()
    book.enqueue("signal:ghi", "SIGNAL", "SIGNAL - INFY", NOW)
    book.enqueue("exit:p2:STOP:1", "EXIT_CALL", "EXIT CALL [STOP] INFY", NOW, NOW + timedelta(minutes=15))
    seen = []

    class Crash:
        def send(self, text):
            seen.append({r["key"]: r["status"] for r in book.rows()})
            raise KeyboardInterrupt                                  # the process dies during the send

    try:
        book.dispatch(NOW, Crash())
    except KeyboardInterrupt:
        pass
    assert "SENDING" in seen[0].values()
    book2 = AlertBook(store, s)                                      # restart
    assert book2.get("signal:ghi")["status"] in ("UNCERTAIN", "PENDING")
    statuses = {book2.get(k)["status"] for k in ("signal:ghi", "exit:p2:STOP:1")}
    assert "SENDING" not in statuses
    crashed = next(k for k, v in seen[0].items() if v == "SENDING")
    assert book2.get(crashed)["status"] == ("UNCERTAIN" if crashed.startswith("signal:") else "PENDING")


# ---------------- 6. the signal and its paper trade in one transaction; restore re-creates a missing one ----------------
def test_6_a_crash_right_after_saving_the_signal_leaves_its_paper_trade_saved(tmp_path):
    import pytest
    from tests.test_phase6_engine import BAR_END, make, seed, tick
    db = str(tmp_path / "crash.db")
    c, eng = make(db)
    with c:
        last = seed(c.app)
        now = BAR_END + timedelta(seconds=3)
        tick(c.app, now - timedelta(seconds=1), last)

        def crash(*a, **k):
            raise RuntimeError("process killed")
        eng.x.record_quotes = crash                     # the first step after save_signal
        with pytest.raises(RuntimeError):
            eng.on_bar(BAR_END, now)
        rows = eng.j.signals_since(BAR_END - timedelta(hours=1))
        assert len(rows) == 1 and rows[0]["status"] == "APPROVED"
        assert eng.j.paper_trade(rows[0]["signal_id"])["status"] == "PENDING"


def test_6_restart_creates_the_missing_paper_trade_of_an_unexpired_approved_signal(tmp_path):
    from tests.test_phase6_engine import BAR_END, make, seed, tick
    db = str(tmp_path / "restore.db")
    c, eng = make(db)
    with c:
        last = seed(c.app)
        now = BAR_END + timedelta(seconds=3)
        tick(c.app, now - timedelta(seconds=1), last)
        sid = eng.on_bar(BAR_END, now)["stored"][0]["signal_id"]
        approved_row = eng.j.paper_trade(sid)
        with eng.j._lock:                               # a database from before the fix: the paper row was lost
            eng.j._db.execute("DELETE FROM paper_trades WHERE signal_id=?", (sid,))
            eng.j._db.commit()
    c2, eng2 = make(db)                                 # restart
    with c2:
        out = eng2.restore(now)
        assert out["paper_trades_recreated"] == 1 and out["pending_paper"] == 1
        row = eng2.j.paper_trade(sid)
        for k in ("status", "quantity", "risk_inr", "margin_inr", "planned_fill_utc", "stop", "target", "note"):
            assert row[k] == approved_row[k], k
    c3, eng3 = make(db)                                 # past valid-until: nothing is re-created
    with c3:
        with eng3.j._lock:
            eng3.j._db.execute("DELETE FROM paper_trades WHERE signal_id=?", (sid,))
            eng3.j._db.commit()
        assert eng3.restore(BAR_END + timedelta(hours=2))["paper_trades_recreated"] == 0


# ---------------- 7. an exit call is not resolved by an UNKNOWN regime or missing prices ----------------
def _regime_flip_setup():
    from app.calendar import MarketCalendar
    from app.positions import ExitCallBook, ExitInputs, evaluate_positions
    s = settings()
    j, store = new_journal(s)
    j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                           "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 900.0, "target": 1200.0}, NOW)
    book = ExitCallBook(store, s)

    def run(at, regime, prices):
        def inputs_for(pos):
            return ExitInputs(now=at, prices=prices, price_why="" if prices else "INFY price STALE",
                              spot=prices[0] if prices else None, regime=regime)
        evaluate_positions(j, book, s, MarketCalendar(), at, inputs_for, ZeroCosts())
        return {c["kind"]: c["active"] for c in book.calls()}
    return run


def test_7_regime_flip_stays_active_while_the_regime_is_unknown_or_prices_are_missing():
    run = _regime_flip_setup()
    assert run(NOW, "TRENDING_DOWN", [1010.0])["REGIME_FLIP"] is True          # a bullish position in a downtrend
    assert run(NOW + timedelta(minutes=5), "UNKNOWN", [1010.0])["REGIME_FLIP"] is True
    assert run(NOW + timedelta(minutes=10), None, [1010.0])["REGIME_FLIP"] is True
    assert run(NOW + timedelta(minutes=15), "TRENDING_UP", None)["REGIME_FLIP"] is True   # prices missing
    assert run(NOW + timedelta(minutes=20), "TRENDING_UP", [1010.0])["REGIME_FLIP"] is False   # known, no longer holds


def test_7_kill_switch_call_is_not_resolved_while_prices_are_missing():
    from app.calendar import MarketCalendar
    from app.positions import ExitCallBook, ExitInputs, evaluate_positions
    s = settings()
    j, store = new_journal(s)
    j.add_manual_position({"symbol": "INFY", "trade_type": "INTRADAY_STOCKS", "direction": "LONG", "quantity": 10,
                           "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 900.0}, NOW)
    book = ExitCallBook(store, s)

    def run(at, kill, prices):
        f = lambda pos: ExitInputs(now=at, prices=prices, spot=prices[0] if prices else None, regime="TRENDING_UP",
                                   kill_reasons=kill)
        evaluate_positions(j, book, s, MarketCalendar(), at, f, ZeroCosts())
        return {c["kind"]: c["active"] for c in book.calls()}
    assert run(NOW, ("NIFTY price is STALE, not LIVE",), [1010.0])["KILL_SWITCH"] is True
    assert run(NOW + timedelta(minutes=5), (), None)["KILL_SWITCH"] is True
    assert run(NOW + timedelta(minutes=10), (), [1010.0])["KILL_SWITCH"] is False


# ---------------- 8. fill from the first quote after the bar close, not the latest tick ----------------
def test_8_two_ticks_before_the_poll_fill_at_the_first(tmp_path):
    import pytest
    from tests.test_phase6_engine import BAR_END, make, seed, tick
    c, eng = make(str(tmp_path / "first.db"))
    with c:
        last = seed(c.app)
        now = BAR_END + timedelta(seconds=3)
        tick(c.app, BAR_END - timedelta(seconds=1), last)                  # the signal bar's last quote
        sid = eng.on_bar(BAR_END, now)["stored"][0]["signal_id"]
        tick(c.app, BAR_END + timedelta(seconds=10), last + 1.0)           # first quote of the next bar: ask last + 1.1
        tick(c.app, BAR_END + timedelta(seconds=20), last + 3.0)           # a later tick, before the poll
        eng.paper.fill_pending(BAR_END + timedelta(seconds=25), eng.signals)
        row = eng.j.paper_trade(sid)
        assert row["status"] == "OPEN" and row["entry_price"] == pytest.approx(last + 1.1)
        assert row["entry_utc"] == (BAR_END + timedelta(seconds=25)).isoformat()


def test_8_hub_keeps_the_first_good_tick_of_each_minute():
    from app.hub import MarketHub
    from app.models import Tick
    from app.store import Store
    from tests.test_step1 import sim_settings
    s = sim_settings()
    hub = MarketHub(s, Store(":memory:"), "sim")
    t0 = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)
    hub.on_tick(Tick("RELIANCE", 100.0, bid=99.9, ask=100.1, received_at=t0 - timedelta(seconds=2)))
    hub.on_tick(Tick("RELIANCE", 100.4, bid=100.3, ask=100.5, received_at=t0 + timedelta(seconds=4)))
    hub.on_tick(Tick("RELIANCE", 100.9, bid=100.8, ask=101.0, received_at=t0 + timedelta(seconds=9)))
    q = hub.first_quote("RELIANCE", t0)
    assert q["ask"] == 100.5 and q["at"] == t0 + timedelta(seconds=4) and q["status"] == "LIVE"
    assert hub.first_quote("RELIANCE", t0 + timedelta(minutes=1)) is None
    assert hub.first_quote("TCS", t0) is None


# ---------------- 9. dispatch and the watchdog run independently of engine.step() ----------------
def test_9_a_blocked_engine_step_still_raises_and_sends_the_heartbeat_alert():
    import threading
    import time as _time
    from app.main import loop_threads
    from tests.phase6_fixtures import app_client
    from tests.test_step1 import sim_settings
    raw = sim_settings().model_dump()
    raw["alerts"]["telegram_enabled"] = True
    from app.config import Settings
    s = Settings.model_validate(raw)
    bar = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)                   # the 10:30 IST bar closed ...
    frozen = bar + timedelta(seconds=s.alerts.heartbeat_bar_gap_seconds + 60)  # ... and was never processed
    c = app_client(s)
    with c:
        eng, notifier, wd = c.app.state.engine, c.app.state.notifier, c.app.state.watchdog
        release, entered = threading.Event(), threading.Event()

        def stuck_step(now=None):
            entered.set()
            release.wait(10)                                                   # the engine hangs
        eng.step = stuck_step
        sent = []
        notifier.sender = type("S", (), {"send": lambda self, text: sent.append(text)})()
        stop = threading.Event()
        threads = loop_threads(eng, notifier, wd, stop, poll_seconds=0.05, watchdog_seconds=0.05,
                               now_fn=lambda: frozen)
        for t in threads:
            t.start()
        try:
            deadline = _time.monotonic() + 5
            while _time.monotonic() < deadline and not any("HEARTBEAT: the signal loop" in x for x in sent):
                _time.sleep(0.05)
            assert entered.is_set()                                            # step() is blocked right now
            assert any("HEARTBEAT: the signal loop" in x for x in sent), sent
        finally:
            stop.set()
            release.set()
            for t in threads:
                t.join(2)


# ---------------- issue 108: the Phase 4 index reports of the download command use account.capital_inr ----------------
def test_108_index_option_reports_from_the_download_command_use_the_cash_and_index_capital(monkeypatch):
    from app.options import stock_backtest as sb
    from app.options.stock_selling import phase9_settings
    from app.store import Store
    from tests.test_step1 import sim_settings
    user = sim_settings()
    assert user.account.capital_inr != user.stock_option_selling.backtest.capital_inr     # the fixture tells them apart
    seen = {}

    class Hist:
        error, not_stored = None, []

    def fake_prepare(settings, store, base, hist, strategies, symbols):
        seen["prepare"] = settings.account.capital_inr
        seen["base"] = base.s.account.capital_inr if hasattr(base, "s") else base.settings.account.capital_inr
        return []

    def fake_run(settings, store, source, st, hist, symbols, engine=None, save=True):
        seen.setdefault("runs", []).append((st.id, settings.account.capital_inr, engine.s.account.capital_inr))
        return {"report": {}}

    monkeypatch.setattr(sb, "prepare_history", fake_prepare)
    monkeypatch.setattr(sb, "run_option_backtest", fake_run)
    monkeypatch.setattr(sb, "print_report", lambda *a, **k: None)
    sb.run_index_reports(user, Store(":memory:"), Hist(), calendar=None)
    cap = user.account.capital_inr
    assert seen["prepare"] == cap and seen["base"] == cap
    assert seen["runs"] and all(c == cap and e == cap for _, c, e in seen["runs"]), seen["runs"]
    assert phase9_settings(user).account.capital_inr == user.stock_option_selling.backtest.capital_inr
