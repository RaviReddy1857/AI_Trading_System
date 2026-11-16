"""Phase 5 routes: GET /api/risk (limits in rupees, kill switch, what is not known yet) and
POST /capital/log (deposits and withdrawals, logged separately, never profit)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Tick
from app.risk.engine import RiskService, decide
from app.store import Store
from tests.phase5_fixtures import SECTORS, ZeroCosts, cash_signal, no_corr
from tests.test_step1 import FakeFeed, sim_settings


def client(capital=None):
    s = sim_settings()
    if capital:
        raw = s.model_dump()
        raw["account"]["capital_inr"] = capital
        s = Settings.model_validate(raw)
    return TestClient(create_app(s, feed=FakeFeed(), db_path=":memory:", background=False),
                      base_url="http://127.0.0.1:8000")


def test_api_risk_limits_and_unknown_portfolio():
    with client() as c:
        r = c.get("/api/risk").json()
    assert r["capital_inr"] == 200000
    lim = r["limits_inr"]
    assert (lim["max_loss_per_trade"], lim["max_loss_per_option"], lim["daily_loss_stop"], lim["monthly_loss_pause"],
            lim["position_value_cap"], lim["capital_pool_usable"]) == (2000, 5000, 4000, 12000, 50000, 150000)
    assert r["portfolio"]["known"] is False and r["portfolio"]["problems"]
    assert "KILL_SWITCH" in r["reason_codes"] and "ZERO_SIZE" in r["reason_codes"]
    assert isinstance(r["kill_switch"]["on"], bool)


def test_api_risk_rescales_with_capital():
    with client(400000) as c:
        lim = c.get("/api/risk").json()["limits_inr"]
    assert (lim["max_loss_per_trade"], lim["daily_loss_stop"], lim["capital_pool_usable"]) == (4000, 8000, 300000)


def test_capital_log_deposit_is_stored_and_shown():
    with client() as c:
        r = c.post("/capital/log", json={"date": "2026-11-02", "type": "DEPOSIT", "amount": 50000, "note": "top-up"})
        assert r.status_code == 200 and r.json()["ok"]
        c.post("/capital/log", json={"date": "2026-11-03", "type": "WITHDRAWAL", "amount": 10000})
        log = c.get("/api/risk").json()["capital_log"]
    assert (log["entries"], log["deposits_inr"], log["withdrawals_inr"]) == (2, 50000, 10000)


def test_capital_log_rejects_bad_entries():
    with client() as c:
        for body in ({"date": "2026-11-02", "type": "PROFIT", "amount": 1},
                     {"date": "2026-11-02", "type": "DEPOSIT", "amount": -5},
                     {"date": "2026-11-02", "type": "DEPOSIT", "amount": "1000"},
                     {"date": "2026-11-02", "type": "DEPOSIT", "amount": True},
                     {"date": "02-11-2026", "type": "DEPOSIT", "amount": 1},
                     {"date": "2999-01-01", "type": "DEPOSIT", "amount": 1}):
            r = c.post("/capital/log", json=body)
            assert r.status_code == 400 and not r.json()["ok"], body
        assert c.post("/capital/log", json={"date": "2026-11-02", "type": "DEPOSIT", "amount": 1},
                      headers={"Host": "evil.example:8000"}).status_code == 403
        assert c.get("/api/risk").json()["capital_log"]["entries"] == 0


class FakeHub:
    def __init__(self, statuses):
        self.statuses = statuses

    def snapshot(self, now):
        return [{"symbol": k, "status": v} for k, v in self.statuses.items()]


class Cal:
    holidays = frozenset()


def test_service_kill_switch_from_quarantine_window():
    """20 quarantined ticks inside the 5-minute window turn the kill switch on; 19 do not; older ones
    do not count."""
    s = sim_settings()
    st = Store(":memory:")
    now = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)              # 10:30 IST, Monday
    for i in range(19):
        st.save_quality_event(now - timedelta(minutes=1), "NIFTY", "PRICE_JUMP", 1.0, None, None, "sim")
    for i in range(30):
        st.save_quality_event(now - timedelta(minutes=6), "NIFTY", "PRICE_JUMP", 1.0, None, None, "sim")
    svc = RiskService(s, st, FakeHub({"NIFTY": "LIVE", "RELIANCE": "LIVE"}), Cal())
    assert svc.market_state("RELIANCE", now).quarantined_recent == 19
    assert svc.status(now)["kill_switch"]["on"] is False
    st.save_quality_event(now - timedelta(seconds=10), "NIFTY", "PRICE_JUMP", 1.0, None, None, "sim")
    ks = svc.status(now)["kill_switch"]
    assert ks["on"] is True and "20 ticks quarantined" in ks["reasons"][0]
    # outside market hours and a stale index both turn it on
    assert svc.status(now + timedelta(hours=8))["kill_switch"]["on"] is True
    svc2 = RiskService(s, Store(":memory:"), FakeHub({"NIFTY": "STALE"}), Cal())
    assert svc2.status(now)["kill_switch"]["on"] is True


def test_live_decision_is_no_trade_until_positions_are_recorded():
    """Phase 5 alone: open positions and P&L are not known yet (phase 6 journal), so every live decision
    is NO_TRADE with PORTFOLIO_UNKNOWN, even with fresh prices in market hours."""
    s = sim_settings()
    st = Store(":memory:")
    now = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)
    svc = RiskService(s, st, FakeHub({"NIFTY": "LIVE", "RELIANCE": "LIVE"}), Cal())
    sig = cash_signal()
    d = decide(sig, svc.account_state(), svc.market_state("RELIANCE", now, "TRENDING_UP"), s, ZeroCosts(), no_corr,
               SECTORS)
    assert d.status == "NO_TRADE" and "PORTFOLIO_UNKNOWN" in d.reason_codes and "KILL_SWITCH" not in d.reason_codes


def test_dashboard_has_risk_strip():
    with client() as c:
        html = c.get("/").text
    assert 'id="risk"' in html and "/api/risk" in html


def test_service_correlations_use_the_phase5_window():
    s = sim_settings()
    c = RiskService(s, Store(":memory:"), FakeHub({}), Cal()).correlations("sim")
    assert (c.window, c.min_overlap) == (s.risk.correlation_window, s.risk.correlation_min_overlap) == (60, 50)
    assert c.at(datetime(2026, 11, 13).date(), "RELIANCE", "INFY") is None      # no history: unknown, never 0


def test_risk_config_is_validated():
    import pytest
    from pydantic import ValidationError
    raw = sim_settings().model_dump()
    for bad in ({"drawdown_halve_pct": 15.0, "drawdown_pause_pct": 15.0},
                {"correlation_min_overlap": 70, "correlation_window": 60},
                {"correlation_threshold": 1.0}, {"max_trades_per_sector": 0},
                {"kill_switch": {"quarantine_window_minutes": 5, "quarantine_max_events": 0}}):
        r = {**raw, "risk": {**raw["risk"], **bad}}
        with pytest.raises(ValidationError):
            Settings.model_validate(r)
