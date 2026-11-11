"""Build fails if any order-related capability appears in the app."""
import re
from pathlib import Path

from app.main import create_app
from tests.test_step1 import FakeFeed

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_CALLS = ["place_order", "modify_order", "cancel_order", "square_off", "gtt_",
                   "set_funds", "add_margin", "aggressive_limit_order", "preview_order",
                   "get_order_notification=True"]
FORBIDDEN_ROUTE_WORDS = ["order", "execute", "square", "submit", "trade/"]


def test_no_order_calls_in_source():
    hits = []
    for folder in ("app", "scripts"):
        for f in (ROOT / folder).rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            hits += [f"{f.name}: {w}" for w in FORBIDDEN_CALLS if w in text]
    assert hits == [], f"order-related code found: {hits}"


def test_no_order_routes():
    app = create_app(feed=FakeFeed(), db_path=":memory:")
    paths = [r.path.lower() for r in app.routes]
    bad = [p for p in paths if any(w in p for w in FORBIDDEN_ROUTE_WORDS)]
    assert bad == [], f"forbidden routes: {bad}"
    writes = [r.path for r in app.routes
              if getattr(r, "methods", None) and r.methods - {"GET", "HEAD", "OPTIONS"}]
    assert writes == [], f"step 1 must be read-only, found write routes: {writes}"


def test_sdk_is_only_touched_in_one_file():
    users = [f.name for f in (ROOT / "app").rglob("*.py")
             if re.search(r"^\s*(from|import) breeze_connect", f.read_text(encoding="utf-8"), re.M)]
    assert users == ["breeze_feed.py"]
