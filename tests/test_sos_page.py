"""The /stock-option-selling page on a live database without a stored shortlist (2026-11-17): every section was empty.

- The shortlist API builds the newest session's assessment in memory when none is stored for it (nothing written),
  so every stock is listed with why it is not on the list - NO_CHAIN and, without event coverage,
  EVENT_COVERAGE_UNKNOWN.
- The page loads each section on its own: one failing call shows its error in its own section and never empties the
  shortlist, the "not on the list" rows, the settlement rules or the stock list (run in Node when it is installed).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest

from app.history import daily_start
from app.models import Bar
from app.options.instruments import ContractSpec, InstrumentBook
from tests.test_phase9_core import EXP, LOT, stock_bars, weekdays_back

pytestmark = pytest.mark.usefixtures("fixed_clock")

SESSION = date(2026, 11, 13)
AS_OF = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)


def _app():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    return app, TestClient(app)


def _seed(store, coverage_for=()):
    idx = [Bar("NIFTY", daily_start(d), 24000, 24050, 23950, 24000, None) for d in weekdays_back(SESSION, 5)]
    store.save_history_bars("bars_1d", idx + stock_bars("RELIANCE") + stock_bars("TCS"), "sim")
    strikes = tuple(float(k) for k in range(800, 1501, 10))
    InstrumentBook(store, "sim").save([ContractSpec(s, EXP, LOT, "monthly", strikes, AS_OF) for s in ("RELIANCE", "TCS")])
    store.replace_events([("2026-11-12", "COVERAGE", s, f"source=NSE board meetings; covered_through={EXP + timedelta(days=5)}")
                          for s in coverage_for])


def test_without_a_stored_shortlist_every_stock_is_listed_with_every_reason():
    app, c = _app()
    with c:
        store = app.state.store
        _seed(store, coverage_for=("TCS",))
        r = c.get("/api/stock-option-selling/shortlist").json()
        assert r["computed"] is True and r["session_date"] == "2026-11-13" and "Not stored" in r["computed_note"]
        by = {x["stock"]: [y.split(":")[0] for y in x["reasons"]] for x in r["rows"]}
        assert by == {"RELIANCE": ["NO_CHAIN", "EVENT_COVERAGE_UNKNOWN"], "TCS": ["NO_CHAIN"]}
        assert all(x["status"] == "EXCLUDED" for x in r["rows"])
        assert store.shortlist()["rows"] == []                                 # a GET never stores it


def test_a_stored_shortlist_of_the_newest_session_is_served_as_stored():
    app, c = _app()
    with c:
        store = app.state.store
        _seed(store)
        store.save_shortlist(SESSION, [{"stock": "TCS", "status": "EXCLUDED", "reasons": ["NO_CHAIN: x"]}], AS_OF)
        r = c.get("/api/stock-option-selling/shortlist").json()
        assert r["computed"] is False and [x["stock"] for x in r["rows"]] == ["TCS"]


# ---------------- the page script, in Node ----------------
NODE = shutil.which("node")


def _run_page(responses: dict, page: str) -> dict:
    """Run the page's script with a DOM of the page's own ids only (getElementById of any other id is null, as in
    a browser) and fetch answering from `responses` (url -> (status, body)). Returns id -> filled html/text."""
    script = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
    ids = sorted(set(re.findall(r'id="([^"]+)"', page.replace(script, ""))))
    harness = """
const responses = %s, ids = %s, els = {};
for (const id of ids) els[id] = {id, textContent: "", innerHTML: "", className: "", value: ""};
global.document = {getElementById: id => els[id] || null};
global.console.error = () => {};
global.fetch = async url => { const [status, body] = responses[url] || [404, {detail: "Not Found"}];
  return {ok: status < 400, status, json: async () => body}; };
process.on("unhandledRejection", e => { console.log(JSON.stringify({UNHANDLED: String(e)})); });
%s
setTimeout(() => console.log(JSON.stringify(Object.fromEntries(Object.entries(els).map(([k, v]) => [k, v.innerHTML || v.textContent])))), 200);
""" % (json.dumps(responses), json.dumps(ids), script)
    r = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        out.update(json.loads(line))
    return out


@pytest.mark.skipif(NODE is None, reason="Node is not installed: the page script cannot be run here")
def test_one_failing_call_never_empties_the_other_sections():
    app, c = _app()
    with c:
        _seed(app.state.store)
        page = c.get("/stock-option-selling").text
        responses = {f"/api/stock-option-selling/{n}": [200, c.get(f"/api/stock-option-selling/{n}").json()]
                     for n in ("shortlist", "campaigns", "settlement")}
    responses["/api/stock-option-selling/reversals"] = [500, {"detail": "Internal Server Error"}]
    got = _run_page(responses, page)
    assert "UNHANDLED" not in got
    assert "Could not load" in got["rev"] and "HTTP 500" in got["rev"]
    assert "RELIANCE" in got["rest"] and "EVENT_COVERAGE_UNKNOWN" in got["rest"] and "NO_CHAIN" in got["rest"]
    assert "delivery margin" in got["setrules"]                                # the settlement rules
    assert "<option>RELIANCE</option>" in got["psym"]                          # the stock list
    assert "Not stored" in got["asof"]


@pytest.mark.skipif(NODE is None, reason="Node is not installed: the page script cannot be run here")
def test_page_fills_every_section_when_every_call_succeeds():
    app, c = _app()
    with c:
        _seed(app.state.store)
        page = c.get("/stock-option-selling").text
        responses = {f"/api/stock-option-selling/{n}": [200, c.get(f"/api/stock-option-selling/{n}").json()]
                     for n in ("shortlist", "reversals", "campaigns", "settlement")}
    got = _run_page(responses, page)
    assert "UNHANDLED" not in got and not any("Could not load" in v for v in got.values())
    for k in ("rest", "setrules", "psym", "camps", "rev"):
        assert got[k], k
