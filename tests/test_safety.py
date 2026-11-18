"""Build fails if any order-related capability appears in the app."""
import pytest
import re
from pathlib import Path

from app.main import create_app
from tests.test_step1 import FakeFeed, sim_settings

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_CALLS = ["place_order", "modify_order", "cancel_order", "square_off", "gtt_",
                   "set_funds", "add_margin", "aggressive_limit_order", "preview_order",
                   "get_order_notification=True"]
FORBIDDEN_ROUTE_WORDS = ["order", "execute", "square", "submit", "trade/"]
# From phase 2 (architecture.md section 10): writes only for journal and manual data.
WRITE_ROUTE_ALLOWLIST = ("/journal/", "/data/import/", "/capital/")


def test_no_order_calls_in_source():
    hits = []
    for folder in ("app", "scripts"):
        for f in (ROOT / folder).rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            hits += [f"{f.name}: {w}" for w in FORBIDDEN_CALLS if w in text]
    assert hits == [], f"order-related code found: {hits}"


@pytest.mark.usefixtures("fixed_clock")
def test_no_order_routes():
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:")
    paths = [r.path.lower() for r in app.routes]
    bad = [p for p in paths if any(w in p for w in FORBIDDEN_ROUTE_WORDS)]
    assert bad == [], f"forbidden routes: {bad}"
    writes = [r.path for r in app.routes
              if getattr(r, "methods", None) and r.methods - {"GET", "HEAD", "OPTIONS"}]
    outside = [p for p in writes if not p.startswith(WRITE_ROUTE_ALLOWLIST)]
    assert outside == [], f"write routes outside {WRITE_ROUTE_ALLOWLIST}: {outside}"


def _sim_rows(path) -> dict:
    import sqlite3
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE lower(source) LIKE 'sim%'").fetchone()[0]
                for t in ("bars_1m", "bars_1d", "bars_5m", "quality_events")}
    finally:
        db.close()


def test_live_db_never_receives_sim_rows(tmp_path):
    """The live SQLite file refuses sim rows and holds none after a refused write. On a live-mode file in tmp_path:
    the suite never opens data/ (tests/conftest.py; the real file: `test_real_live_db_holds_no_sim_rows`, opt-in)."""
    from app.config import load_settings
    from app.models import Bar
    from app.store import SimDataInLiveStore, Store

    s = load_settings()
    assert s.storage.db_path_live != s.storage.db_path_sim
    path = tmp_path / "market_live.db"
    live = Store(path, live=True)
    for source in ("sim", "SIM:seed", " sim"):
        with pytest.raises(SimDataInLiveStore):
            live.save_bar(Bar("NIFTY", __import__("datetime").datetime.now(), 1, 1, 1, 1), source)
    live.save_bar(Bar("NIFTY", __import__("datetime").datetime(2026, 11, 16, 4, 0), 1, 1, 1, 1), "breeze")
    live.close()
    assert _sim_rows(path) == {"bars_1m": 0, "bars_1d": 0, "bars_5m": 0, "quality_events": 0}


@pytest.mark.skipif(__import__("os").environ.get("TRADING_AUDIT_REAL_DATA") != "1",
                    reason="opt-in audit of data/market_live.db (TRADING_AUDIT_REAL_DATA=1; only when nothing writes)")
def test_real_live_db_holds_no_sim_rows():
    from app.config import load_settings
    s = load_settings()
    path = s.path(s.storage.db_path_live)
    if path.exists():
        assert set(_sim_rows(path).values()) == {0}


def test_sdk_is_only_touched_in_one_file():
    users = [f.name for f in (ROOT / "app").rglob("*.py")
             if re.search(r"^\s*(from|import) breeze_connect", f.read_text(encoding="utf-8"), re.M)]
    assert users == ["breeze_feed.py"]
