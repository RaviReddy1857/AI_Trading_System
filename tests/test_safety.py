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


def test_live_db_never_receives_sim_rows():
    """The live SQLite file refuses sim rows, a sim feed cannot be paired with live
    mode, and the real live file on disk (if any) holds no sim rows."""
    import sqlite3

    import pytest

    from app.config import load_settings
    from app.models import Bar
    from app.store import SimDataInLiveStore, Store

    s = load_settings()
    assert s.storage.db_path_live != s.storage.db_path_sim
    live = Store(":memory:", live=True)
    with pytest.raises(SimDataInLiveStore):
        live.save_bar(Bar("NIFTY", __import__("datetime").datetime.now(), 1, 1, 1, 1), "sim")
    path = s.path(s.storage.db_path_live)
    if path.exists():
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for table in ("bars_1m", "bars_1d", "bars_5m", "quality_events"):
            n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE lower(source) LIKE 'sim%'").fetchone()[0]
            assert n == 0, f"{n} sim rows in live {table}"


def test_sdk_is_only_touched_in_one_file():
    users = [f.name for f in (ROOT / "app").rglob("*.py")
             if re.search(r"^\s*(from|import) breeze_connect", f.read_text(encoding="utf-8"), re.M)]
    assert users == ["breeze_feed.py"]
