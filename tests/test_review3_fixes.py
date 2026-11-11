"""Regression tests for the third independent review (findings 1 and 2).
Each test fails on the code before the fix and passes after it."""
from datetime import timedelta
from pathlib import Path

from app.calendar import MarketCalendar
from app.hub import MarketHub
from app.manual_data import ManualData
from app.regime import RegimeService
from app.store import Store
from tests.phase2_fixtures import SESSION, T0, live_ticks, settings_with_tmp_data, store_history

ROOT = Path(__file__).resolve().parents[1]


# ---------------- 1. broker SDK log files stay out of git ----------------
def test_sdk_log_files_are_git_ignored():
    lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "logs/" in lines and "*.log" in lines


# ---------------- 2. a restart does not duplicate the regime history ----------------
def start_app(tmp_path, db):
    """One 'run' of the app: a fresh Store, hub and RegimeService on the same SQLite file."""
    s = settings_with_tmp_data(tmp_path)
    store, cal = Store(str(db)), MarketCalendar()
    m = ManualData(s, store, cal)
    m.load_all()
    hub = MarketHub(s, store, "breeze", cal)
    return store, hub, RegimeService(s, store, hub, cal, m)


def test_restart_does_not_duplicate_regime_entry(tmp_path):
    db = tmp_path / "market_live.db"
    store, hub, svc = start_app(tmp_path, db)
    store_history(store, MarketCalendar(), SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    first = svc.evaluate(T0 + timedelta(seconds=5))
    svc.evaluate(T0 + timedelta(seconds=10))                  # same run, same result
    assert len(store.regime_history(50)) == 1

    for restart in range(2):                                  # restart twice, same market
        store, hub, svc = start_app(tmp_path, db)
        live_ticks(hub, T0 + timedelta(minutes=1 + restart), nifty=22700.0)
        again = svc.evaluate(T0 + timedelta(minutes=1 + restart, seconds=5))
        assert (again["regime"], again["bias"]) == (first["regime"], first["bias"])
        assert len(store.regime_history(50)) == 1, store.regime_history(50)


def test_change_after_restart_is_still_logged(tmp_path):
    db = tmp_path / "market_live.db"
    store, hub, svc = start_app(tmp_path, db)
    store_history(store, MarketCalendar(), SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    first = svc.evaluate(T0 + timedelta(seconds=5))["regime"]

    store, hub, svc = start_app(tmp_path, db)                 # restart, then INDIAVIX goes missing
    live_ticks(hub, T0 + timedelta(minutes=1), nifty=22700.0, skip=("INDIAVIX",))
    second = svc.evaluate(T0 + timedelta(minutes=1, seconds=5))["regime"]
    assert second == "UNKNOWN" != first
    assert [r["regime"] for r in store.regime_history(50)] == [second, first]


def test_last_regime_is_per_session(tmp_path):
    store = Store(str(tmp_path / "x.db"))
    store.save_regime(T0, SESSION, "RANGE", "NEUTRAL", {}, "regime_v1/abc", "LIVE")
    store.save_regime(T0, SESSION - timedelta(days=1), "VOLATILE", "BEARISH", {}, "regime_v1/abc", "LIVE")
    assert store.last_regime(SESSION)["regime"] == "RANGE"
    assert store.last_regime(SESSION - timedelta(days=2)) is None
