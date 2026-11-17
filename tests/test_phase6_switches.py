"""Phase 6 M3 acceptance: with a type's switch off its signals are paper-traded and no alert is sent; with it on,
alerts follow `strategies.status` (a strategy with alerts: false sends none). Top signals per bar and the daily cap;
an expired signal is never sent; exit calls always go out (settlement alerts included, repeating by level); the kill
switch and a pool's daily stop are announced once; the pre-market plan and the end-of-day summary."""
from datetime import datetime, timedelta, timezone

from app.alerts import AlertBook, Notifier
from app.calendar import MarketCalendar
from app.store import Store
from tests.phase6_fixtures import new_journal, settings
from tests.test_phase6_engine import (BAR_END, BreakoutFixture, FixedRegime, pin_equity_start, seed,
                                     signals_at, tick)
from tests.phase6_fixtures import app_client
from tests.test_step1 import sim_settings

T = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)


def engine_app(status_alerts: bool, telegram: bool = True):
    s = sim_settings()
    raw = s.model_dump()
    raw["strategies"]["status"]["orb_v2"]["alerts"] = status_alerts
    raw["alerts"]["telegram_enabled"] = telegram
    c = app_client(type(s).model_validate(raw))
    eng = c.app.state.engine
    eng.cash, eng.swing, eng.regime = [BreakoutFixture()], [], FixedRegime()
    eng.desk = type("NoOptions", (), {"underlyings": lambda self: []})()
    pin_equity_start(eng)
    return c, eng


def run_bar(c, eng):
    last = seed(c.app)
    tick(c.app, BAR_END - timedelta(seconds=1), last)
    return eng.on_bar(BAR_END, BAR_END + timedelta(seconds=3))


def signal_alerts(c):
    return [a for a in c.app.state.alerts.rows() if a["kind"] == "SIGNAL"]


def test_switch_off_paper_only_no_alert():
    c, eng = engine_app(status_alerts=True)
    with c:
        out = run_bar(c, eng)
        assert out["approved"] == 1
        assert len(c.get("/api/paper").json()["open"]) == 1               # still paper-traded
        assert signal_alerts(c) == []


def test_switch_on_but_status_without_alerts_sends_none():
    c, eng = engine_app(status_alerts=False)
    with c:
        c.post("/journal/alert-switches", json={"type": "INTRADAY_STOCKS", "on": True})
        run_bar(c, eng)
        assert signal_alerts(c) == []
        card = signals_at(c)["cards"][0]
        assert card["alert"]["allowed"] is False and "does not allow alerts" in card["alert"]["why"]


def test_switch_on_and_status_allowing_alerts_sends_one():
    c, eng = engine_app(status_alerts=True)
    with c:
        c.post("/journal/alert-switches", json={"type": "INTRADAY_STOCKS", "on": True})
        run_bar(c, eng)
        a = signal_alerts(c)
        assert len(a) == 1 and a[0]["text"].startswith("SIGNAL - Intraday stocks: RELIANCE (cash) LONG")
        assert "Estimate if target is hit, not a forecast" in a[0]["text"]
        sw = c.get("/api/alert-switches").json()["switches"]
        intraday = next(x for x in sw if x["type"] == "INTRADAY_STOCKS")
        assert intraday["on"] and "track" in intraday and "backtest" in intraday["track"]


# ---------------- notifier rules ----------------
class FakeRisk:
    def __init__(self, accounts=None):
        self.accounts = accounts or {}

    def account_state(self, pool, now=None):
        from app.risk.portfolio import AccountState
        return self.accounts.get(pool) or AccountState(200000.0, None, None, None, None)


def notifier(cap=10, top=3, accounts=None):
    s = settings()
    raw = s.model_dump()
    raw["alerts"].update(daily_signal_cap=cap, top_per_bar=top, telegram_enabled=True)
    s = type(s).model_validate(raw)
    j, store = new_journal(s)
    return Notifier(s, AlertBook(store, s), j, FakeRisk(accounts), None, MarketCalendar(), store=store), s


def row(i, score, allowed=True, valid=T + timedelta(minutes=15)):
    card = {"trade_type_name": "Intraday stocks", "instrument": f"S{i}", "direction": "LONG", "entry_zone": [1, 1],
            "stop": 0.9, "target": 1.2, "quantity": 1, "lots": None, "risk_inr": 1.0, "estimate": {},
            "valid_until": "x", "strategy_id": "orb_v2", "strategy_version": "1", "why_now": [], "score": {"score": score}}
    return {"signal_id": f"s{i}", "score": score, "alert_allowed": allowed, "valid_until": valid, "card": card}


def test_top_per_bar_and_daily_cap():
    n, _ = notifier(cap=4, top=3)
    n.on_bar([row(1, 90), row(2, 80), row(3, 70), row(4, 60), row(5, 50, allowed=False)], [], T)
    assert sorted(a["key"] for a in n.book.rows()) == ["signal:s1", "signal:s2", "signal:s3"]
    n.on_bar([row(6, 95), row(7, 85)], [], T + timedelta(minutes=5))
    assert n.book.count_today("SIGNAL", T) == 4 and n.book.get("signal:s7") is None     # the cap of 4 per day


def test_expired_signal_is_never_sent():
    n, _ = notifier()
    n.on_bar([row(1, 90, valid=T)], [], T)
    assert n.book.rows() == []


def test_exit_calls_always_sent_and_system_alerts_once():
    n, _ = notifier()
    call = {"kind": "STOP", "action": "Exit all 2 lots now", "reason": "x", "repeat_no": 1, "key": "exit:p:STOP:1",
            "symbol": "NIFTY", "trade_type": "INDEX_OPTION_SELLING", "hedged": False}
    n.on_bar([], [call], T, kill_reasons=("NIFTY price is STALE, not LIVE",))
    n.on_bar([], [], T + timedelta(minutes=5), kill_reasons=("NIFTY price is STALE, not LIVE",))
    kinds = [a["kind"] for a in n.book.rows()]
    assert kinds.count("EXIT_CALL") == 1 and kinds.count("KILL_SWITCH") == 1
    assert "UNHEDGED" in n.book.get("exit:p:STOP:1")["text"]
    n.on_bar([], [], T + timedelta(minutes=10))                                   # switch off ...
    n.on_bar([], [], T + timedelta(minutes=15), kill_reasons=("spike",))           # ... and on again: a new alert
    assert [a["kind"] for a in n.book.rows()].count("KILL_SWITCH") == 2


def test_daily_stop_announced_once_per_pool():
    from app.risk.portfolio import AccountState
    loss = AccountState(200000.0, (), -4000.0, -4000.0, None)
    n, _ = notifier(accounts={"CASH_INDEX": loss})
    n.on_bar([], [], T)
    n.on_bar([], [], T + timedelta(minutes=5))
    rows = [a for a in n.book.rows() if a["kind"] == "DAILY_STOP"]
    assert len(rows) == 1 and rows[0]["key"] == "dailystop:CASH_INDEX:2026-11-16" and "cash index pool" in rows[0]["text"]


def test_settlement_alert_sent_and_repeated_by_level():
    from app.positions import ExitCallBook, ExitInputs, evaluate_positions
    from tests.phase5_fixtures import ZeroCosts
    n, s = notifier()
    pos = n.j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
                                   "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": "2026-11-23",
                                             "lots": 1, "lot_size": 500, "price": 12.0},
                                            {"action": "BUY", "right": "PE", "strike": 1260, "expiry": "2026-11-23",
                                             "lots": 1, "lot_size": 500, "price": 4.0}]}, T)
    book = ExitCallBook(n.store, s)
    inputs = lambda p: ExitInputs(T, [11.0, 4.0], spot=1295.0, regime="RANGE")   # in the money, E-5: URGENT
    due = evaluate_positions(n.j, book, s, MarketCalendar(), T, inputs, ZeroCosts())
    n.on_bar([], due, T)
    sent = [a for a in n.book.rows() if "SETTLEMENT" in a["text"]]
    assert len(sent) == 1 and "URGENT" in sent[0]["text"] and "Exit the spread by" in sent[0]["text"]
    # URGENT repeats every alerts.settlement_repeat_minutes.URGENT (15)
    t2 = T + timedelta(minutes=s.alerts.settlement_repeat_minutes.URGENT)
    due = evaluate_positions(n.j, book, s, MarketCalendar(), t2, lambda p: ExitInputs(t2, [11.0, 4.0], spot=1295.0,
                                                                                      regime="RANGE"), ZeroCosts())
    n.on_bar([], due, t2)
    assert len([a for a in n.book.rows() if "SETTLEMENT" in a["text"]]) == 2
    assert pos.position_id in sent[0]["key"]


# ---------------- scheduled messages ----------------
def test_premarket_plan_and_end_of_day_summary_once_a_day():
    from app.schedule import scheduled
    n, s = notifier()
    n.regime = FixedRegime()
    pre = datetime(2026, 11, 16, 3, 31, tzinfo=timezone.utc)                     # 09:01 IST
    scheduled(n, pre)
    scheduled(n, pre + timedelta(minutes=2))
    plan = n.book.get("premarket:2026-11-16")
    assert plan and plan["text"].startswith("PRE-MARKET PLAN Mon 16-Nov-2026") and "Regime: TRENDING_UP" in plan["text"]
    assert len([a for a in n.book.rows() if a["kind"] == "PREMARKET"]) == 1
    n2, _ = notifier()
    n2.regime = FixedRegime()
    scheduled(n2, datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc))             # 09:30: market open, no plan
    assert n2.book.get("premarket:2026-11-16") is None
    eod = datetime(2026, 11, 16, 10, 16, tzinfo=timezone.utc)                    # 15:46 IST
    scheduled(n, eod)
    e = n.book.get("eod:2026-11-16")
    assert e["text"].startswith("END OF DAY") and "Signals: 0" in e["text"] and "Open positions: 0" in e["text"]
