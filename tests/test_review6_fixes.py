"""Fixes of the Codex review of 782e870..2e3e2c4 (2026-11-15, six findings). Each test failed on the code
before the fix and passes after it."""
from datetime import date
from functools import partial

import pytest

from app.options.portfolio import CORRELATION_UNKNOWN, Holding, portfolio_check
from tests.test_phase9_core import settings

CFG = settings(account={"capital_inr": 1000000.0}).stock_option_selling.portfolio.model_copy(
    update={"max_total_risk_pct": 10.0, "group_max_risk_pct": 5.0})
CAP = 1000000.0
SECTORS = {"A": "Banks", "B": "Banks", "C": "IT", "D": "IT", "E": "Energy", "X": None}
pc = partial(portfolio_check, history_verified=True)


# ---------------- 1. unknown links through unheld stocks ----------------
def test_ACCEPTANCE_an_unknown_path_through_an_unheld_stock_that_could_join_held_groups_is_unknown():
    """A (held) -?- C (nobody holds it) -?- E (proposed): neither unknown link alone joins two held
    components, but together they could put A and E in one group. Unknown is never a pass."""
    unknown = {frozenset("AC"), frozenset("CE")}
    f = lambda a, b: None if frozenset((a, b)) in unknown else 0.1
    code, why, _ = pc("E", 5000, [Holding("A", 5000)], SECTORS, f, CFG, CAP, universe=["A", "C", "E"])
    assert code == CORRELATION_UNKNOWN and "A-C" in why and "C-E" in why
    # one unknown link to an unheld stock that leads nowhere else changes nothing
    g = lambda a, b: None if frozenset((a, b)) == frozenset("AC") else 0.1
    assert pc("E", 5000, [Holding("A", 5000)], SECTORS, g, CFG, CAP, universe=["A", "C", "E"])[0] is None
    # an unheld stock without a known sector may share a sector with both held stocks' groups
    code, why, _ = pc("E", 5000, [Holding("A", 5000)], SECTORS, lambda a, b: 0.1, CFG, CAP, universe=["A", "X", "E"])
    assert code == CORRELATION_UNKNOWN and "X: sector not known" in why
    # nothing else held: nothing to join
    assert pc("E", 5000, [], SECTORS, f, CFG, CAP, universe=["A", "C", "E"])[0] is None


# ---------------- 2. board meetings: coverage only from a declared, unfiltered all-companies export ----------------
def _board_world(tmp_path):
    from tests.test_data_import import manual_in
    s, store, m = manual_in(tmp_path)
    m.load_all()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return s, store, m, inbox


def _import(s, store, m, inbox, text, decl):
    import os
    from app.inbox import run_inbox
    from tests.test_data_import import NOW
    f = inbox / "board.csv"
    f.write_text(text)
    os.utime(f, (NOW.timestamp(), NOW.timestamp()))           # downloaded on 15 Nov 2026
    res = run_inbox(s, store, m, NOW, board_meetings=decl)[0]
    return res, [e for e in store.all_events() if e["type"] == "COVERAGE"]


def test_ACCEPTANCE_a_partial_board_meetings_export_gives_no_coverage(tmp_path):
    """A file filtered to a few companies (here: the two that have meetings) proves nothing about the other
    watchlist stocks. Before the fix it gave every watchlist stock coverage through its latest meeting date."""
    from datetime import date
    from app.inbox import BoardMeetingsDeclaration
    from tests.test_data_import import BOARD
    s, store, m, inbox = _board_world(tmp_path)
    res, cov = _import(s, store, m, inbox, BOARD, None)                            # no declaration: rejected
    assert res["ok"] is False and "declaration" in res["errors"][0] and cov == []
    ok = BoardMeetingsDeclaration(date(2026, 11, 15), date(2027, 1, 31), all_companies=True)
    res, cov = _import(s, store, m, inbox, BOARD, ok)                              # declared, but only 2 companies
    assert res["ok"] is False and "looks filtered" in res["errors"][0] and cov == []
    assert list((inbox / "rejected").glob("*.reason.txt"))


def test_board_meetings_declaration_and_sanity_checks(tmp_path):
    from datetime import date
    from app.inbox import BoardMeetingsDeclaration as D
    from tests.test_data_import import full_board
    s, store, m, inbox = _board_world(tmp_path)
    full = full_board(days=70)                                       # meetings 2026-11-16 .. 2027-01-25, 152 companies
    cases = [
        (D(date(2026, 11, 15), date(2027, 1, 31), all_companies=False), "did not confirm"),
        (D(date(2026, 11, 15), date(2027, 1, 10), True), "outside the declared range"),     # file runs past the end
        (D(date(2026, 11, 15), date(2027, 3, 31), True), "does not span"),                  # no meetings near the end
        (D(date(2026, 10, 1), date(2027, 1, 31), True), "does not span"),                   # none near the start
        (D(date(2026, 11, 20), date(2027, 1, 31), True), "after the file was downloaded"),
        (D(date(2027, 1, 31), date(2026, 11, 15), True), "ends before it starts"),
    ]
    for decl, why in cases:
        res, cov = _import(s, store, m, inbox, full, decl)
        assert res["ok"] is False and why in res["errors"][0] and cov == [], (decl, res)
    res, cov = _import(s, store, m, inbox, full, D(date(2026, 11, 15), date(2027, 1, 31), True))
    assert res["ok"] is not False and cov and all("covered_through=2027-01-31" in e["note"] for e in cov)


def test_inbox_route_and_cli_take_the_declaration(tmp_path):
    from app.inbox import BoardMeetingsDeclaration, declaration, main
    import pytest
    from datetime import date
    assert declaration("", "", False) is None
    assert declaration("2026-11-15", "2027-01-31", True) == BoardMeetingsDeclaration(date(2026, 11, 15),
                                                                                     date(2027, 1, 31), True)
    assert declaration("2026-11-15", "2027-01-31", "yes").all_companies is False      # only a real True confirms
    with pytest.raises(ValueError):
        declaration("2026-11-15", "", True)
    with pytest.raises(SystemExit):
        main(["--covered-through", "2027-01-31"])                                       # the old flag is gone


# ---------------- 3. five-minute runs are marked at the last synchronized bar ----------------
def _bump(store, pick, strike, interval, start, by):
    store._db.execute("UPDATE option_bars SET close = close + ?, high = MAX(high, close + ?) WHERE underlying='RELIANCE' "
                      "AND expiry=? AND strike=? AND right='PE' AND interval=? AND start_utc=?",
                      (by, by, pick["expiry"], strike, interval, start))
    store._db.commit()


def test_ACCEPTANCE_five_minute_marks_use_the_last_synchronized_bar_never_the_daily_leg_closes():
    from datetime import datetime, time, timedelta
    from app.history import daily_start
    from app.options.option_history import INTRADAY
    from app.quality import IST
    from tests.test_phase9_core import UTC, ist_date, run_bt, stock_world
    from tests.test_phase9_review_fixes import baseline_pick, legs
    w = stock_world()
    store = w[1]
    before = run_bt(w)
    pick = baseline_pick(before)
    sell, _ = legs(pick)
    day = date.fromisoformat(pick["marks"][1][0])                    # a session the position is held through
    assert ist_date(pick["entry_time"]) < day < ist_date(pick["exit_time"])
    mark = lambda res: next(m for m in next(t for t in res["trades"] if t["signal_id"] == pick["signal_id"]
                                            and t["exit_check"] == "intraday")["marks"] if m[0] == day.isoformat())
    m0 = mark(before)
    # the sold leg's DAILY close of that day moves: the five-minute run's mark does not (before the fix it did)
    _bump(store, pick, sell["strike"], "1day", daily_start(day).isoformat(), 1.0)
    assert mark(run_bt(w))[1] == m0[1]
    assert datetime.fromisoformat(m0[2]).astimezone(IST) == datetime.combine(day, time(15, 30), IST)  # 15:25 bar's end
    # the sold leg's LAST SYNCHRONIZED 5-minute bar moves: the mark follows it (dearer to buy back = lower value)
    last = datetime.combine(day, time(15, 25), IST).astimezone(UTC).isoformat()
    _bump(store, pick, sell["strike"], INTRADAY, last, 1.0)
    assert mark(run_bt(w))[1] < m0[1]


def test_a_session_without_a_synchronized_bar_carries_the_previous_mark_and_its_age():
    from datetime import datetime, time, timedelta, timezone
    from types import SimpleNamespace
    from app.options.backtest import OptionEngine, mtm_equity
    from app.quality import IST
    d0, d1, d2 = date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 3)
    at0 = datetime.combine(d0, time(15, 30), IST).astimezone(timezone.utc)
    eng = OptionEngine.__new__(OptionEngine)
    eng._sync_bars = lambda sym, legs, d: []                          # the legs never traded in the same 5 minutes
    pos = SimpleNamespace(legs=(), marks=[(d0, -400.0, at0)])
    OptionEngine._mark(eng, pos, "RELIANCE", d1, None, sync=True)
    assert pos.marks[-1] == (d1, -400.0, at0)                         # carried, with the time of its prices
    empty = SimpleNamespace(legs=(), marks=[])
    OptionEngine._mark(eng, empty, "RELIANCE", d1, None, sync=True)
    assert empty.marks == []                                          # nothing to carry: no mark, nothing invented
    t = SimpleNamespace(entry_time=at0.isoformat(), exit_time=datetime.combine(d2, time(12, 0), IST).isoformat(),
                        net_pnl=100.0, marks=[[m[0].isoformat(), m[1], m[2].isoformat()] for m in pos.marks])
    m = mtm_equity([t])
    assert m["stale_position_days"] == 1 and m["max_mark_age_minutes"] == 24 * 60   # d1 shows d0's 15:30 prices


# ---------------- 4. completion is judged on each block's own requests ----------------
def test_ACCEPTANCE_two_blocks_wanting_the_same_unavailable_contract_are_both_incomplete(monkeypatch):
    """Block 2026 and block 2025 (which runs on to the end, so it covers 2026 too) both want one contract
    that is not stored. Before the fix the 2025 block added nothing NEW to the shared not-stored set and was
    marked complete."""
    from types import SimpleNamespace
    import app.options.stock_backtest as sb
    from app.options.option_history import OptionHistory
    from app.options.stock_selling import build_stock_selling
    from app.store import Store
    cfg = settings()
    hist = OptionHistory(Store(":memory:"), "sim")                  # nothing stored, nothing downloadable
    asked = []

    def run_stream(st, sym, block, name, counter, exit_check="daily"):
        asked.append(block[0].year)
        hist.contract(sym, date(2026, 1, 27), 1000.0, "PE")        # the same missing contract in every block

    eng = SimpleNamespace(prices=hist, intraday=None, base=SimpleNamespace(daily=lambda s: [object()]),
                          last_session=None, set_expiry_floor=lambda d: None, run_stream=run_stream)
    monkeypatch.setattr(sb, "policy_runs", lambda *a, **k: None)
    monkeypatch.setattr(sb, "build_reversal_strategies", lambda s: [])
    dev = [date(2025, 6, 2), date(2025, 12, 1), date(2026, 1, 5), date(2026, 3, 2)]
    status = {}
    sb.download_blocks(cfg, eng, build_stock_selling(cfg), ["RELIANCE"], dev, status, progress=lambda x: None,
                       offline=True)
    assert set(asked) == {2025, 2026} and len(hist.not_stored) == 1
    assert status["complete"] == {}                                  # neither year is complete
    # a block that wants nothing missing is still complete
    hist2 = OptionHistory(Store(":memory:"), "sim")
    eng.prices, eng.run_stream = hist2, lambda *a, **k: None
    status2 = {}
    sb.download_blocks(cfg, eng, build_stock_selling(cfg), ["RELIANCE"], dev, status2, progress=lambda x: None,
                       offline=True)
    assert status2["complete"] == {"RELIANCE": [2025, 2026]}


def test_verify_complete_takes_the_old_marks_off_for_a_recheck():
    from app.options.stock_backtest import verify_complete_marks
    st = {"complete": {"KOTAKBANK": [2025], "RELIANCE": [2025, 2026]}}
    assert verify_complete_marks(st, [2025]) == {("KOTAKBANK", 2025), ("RELIANCE", 2025)}
    assert st["complete"] == {"RELIANCE": [2026]}
    assert verify_complete_marks(st) == {("RELIANCE", 2026)} and st["complete"] == {}


# ---------------- 6. rows dated after today never make a source look fresh ----------------
def test_ACCEPTANCE_future_dated_rows_are_ignored_for_freshness_and_flagged_on_every_source(tmp_path):
    from datetime import datetime, timedelta
    from app.calendar import MarketCalendar
    from app.data_status import data_status
    from app.history import daily_start
    from app.models import Bar
    from tests.test_data_import import NOW, act, manual_in
    later = NOW + timedelta(days=5)
    s, store, m = manual_in(tmp_path, fii="date,fii_net_cr,dii_net_cr\n2026-11-10,1,1\n2026-11-20,1,1\n",
                            events="date,type,note,symbol\n"
                                   "2026-11-20,COVERAGE,source=x; covered_through=2027-01-31,RELIANCE\n"
                                   "2026-11-01,RBI_POLICY,SCHEDULED MPC decision,\n"
                                   "2026-11-18,RBI_POLICY,RBI decision: hold,\n"
                                   "2026-12-05,RBI_POLICY,SCHEDULED MPC decision,\n"
                                   "2027-01-20,RESULTS,Board meeting results,RELIANCE\n")
    m.load_all()
    store.save_corporate_actions([{**act("INFY", date(2026, 12, 10), "BONUS", 0.5), "imported_utc": later.isoformat()}])
    store.log_import("corporate_actions", "external_repo", "abc", later, 1, "test")
    stock = next(i.symbol for i in s.watchlist if i.kind == "stock")
    store.save_history_bars("bars_1d", [Bar(stock, daily_start(date(2026, 11, 13)), 1, 1, 1, 1, 1.0),
                                        Bar(stock, daily_start(date(2026, 11, 20)), 1, 1, 1, 1, 1.0)], "sim")
    store.save_option_bars("breeze", stock, date(2026, 11, 26), 1000.0, "PE", "1day",
                           [(daily_start(date(2026, 11, 13)).isoformat(), 1, 1, 1, 1, 1.0, 1.0)], later)
    rows = {r["source"]: r for r in data_status(s, store, MarketCalendar(), "sim", manual=m, now=NOW)["rows"]}
    want = {"FII/DII cash flows": ("2026-11-20", "STALE"),          # 10-Nov is three trading days behind
            "Results dates and event coverage": ("RELIANCE 2026-11-20", "MISSING"),
            "RBI policy dates": ("2026-11-18", "STALE"),            # the 1-Nov meeting has no decision on or before today
            "Corporate actions": (later.isoformat(), "MISSING"),
            "External repo import (aswinv90/indian-market-historical-data)": ("corporate_actions", "INFO"),
            "Stock daily history (Breeze)": (f"{stock} 2026-11-20", None),
            "Breeze session": (later.isoformat(), "STALE")}
    for source, (item, without) in want.items():
        r = rows[source]
        assert r["status"] == "DATA_ERROR" and r["colour"] == "red", (source, r)
        assert any(item in x for x in r["detail"]["future_dated"]), (source, r["detail"])
        assert without is None or r["detail"]["status_without_them"] == without, (source, r["detail"])
        assert "dated after today" in r["what_to_do"]
    assert rows["FII/DII cash flows"]["last_updated_iso"] == "2026-11-10"
    assert rows["Stock daily history (Breeze)"]["detail"]["last_date_per_stock"][stock] == "2026-11-13"
    # dates that lie ahead by nature are not errors: results meetings, ex-dates, covered_through, scheduled meetings
    (tmp_path / "b").mkdir()
    s2, store2, m2 = manual_in(tmp_path / "b", events="date,type,note,symbol\n2026-12-05,RBI_POLICY,SCHEDULED MPC,\n"
                                                      "2027-01-20,RESULTS,Board meeting results,RELIANCE\n")
    m2.load_all()
    store2.save_corporate_actions([act("INFY", date(2026, 12, 10), "BONUS", 0.5)])
    ok = {r["source"]: r for r in data_status(s2, store2, MarketCalendar(), "sim", manual=m2, now=NOW)["rows"]}
    assert not [r for r in ok.values() if r["status"] == "DATA_ERROR"]


# ---------------- 5. tracked docs carry no rows copied from the external repo ----------------
def test_doc_scanner_finds_repo_values_next_to_their_dates_and_after_the_word_repo():
    from app.external_data import doc_leaks
    vals = {date(2021, 7, 30): {138.76, 1248.8}, date(2019, 1, 21): {482.875}}
    files = {"a.md": "| 2021-07-30 | 1,248.80 | 138.76 | 0.1111 = 1/9 |\n"            # a copied table row
                     "BSE 2021-07-30: repo / Breeze = 0.1111 (1/9), -65.9% in one day\n"  # factors and percentages
                     "HCLTECH 2019-01-21 x 0.5 = 482.88 against repo 482.875\n"
                     "RBI 2019-02-07: repo 6.25% (policy rate)\n"
                     "no date here, repo close 141.93\n"}
    got = doc_leaks(files, lambda wanted: {d: v for d, v in vals.items() if d in wanted})
    assert [g.split(":")[1] for g in got] == ["1", "3", "3", "5"]
    assert "1248.8" in got[0] and "138.76" in got[0] and any("482.875" in g for g in got[1:3])


def test_ACCEPTANCE_tracked_docs_carry_no_repo_price_pattern():
    """Every tracked doc (git ls-files *.md *.txt *.csv): the pattern check ("repo" followed by a price), which needs
    none of the machine's data. The suite never opens data/ (tests/conftest.py): the check against the repo's rows held
    on this machine is `test_ACCEPTANCE_tracked_docs_carry_no_row_of_the_external_repo` (opt-in)."""
    from app.config import load_settings
    from app.external_data import doc_leaks, tracked_docs
    s = load_settings()
    docs = tracked_docs(s.path("."))
    assert "docs/STATUS.md" in docs and "README.md" in docs
    leaks = doc_leaks(docs, lambda wanted: {})
    assert leaks == [], "\n".join(leaks)


@pytest.mark.skipif(__import__("os").environ.get("TRADING_AUDIT_REAL_DATA") != "1",
                    reason="opt-in audit against data/ (TRADING_AUDIT_REAL_DATA=1; only when nothing writes)")
def test_ACCEPTANCE_tracked_docs_carry_no_row_of_the_external_repo():
    """Every tracked doc (git ls-files *.md *.txt *.csv) against the repo's rows held on this machine: the
    gitignored overlays in data/external/, the store's external tables (opened read-only) and the clone.
    The pattern check ("repo" followed by a price) runs even where none of that data exists."""
    import sqlite3
    import threading
    from types import SimpleNamespace
    from app.config import load_settings
    from app.external_data import doc_leaks, external_values, tracked_docs
    s = load_settings()
    db = s.path(s.storage.db_path_live)
    store = None
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='external_bars_1d'").fetchone():
            store = SimpleNamespace(_db=con, _lock=threading.Lock())
    docs = tracked_docs(s.path("."))
    assert "docs/STATUS.md" in docs and "README.md" in docs
    leaks = doc_leaks(docs, lambda wanted: external_values(s, store, wanted))
    assert leaks == [], "\n".join(leaks)
