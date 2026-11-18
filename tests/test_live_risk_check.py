"""Approvals are checked on the LIVE real book at the moment of the check, never on a stored snapshot (18-Nov-2026).

The AXISBANK case: range_reversion_v2 (margin Rs 49,897.20) and range_reversion v1 both at 13:15 IST. The card of the
signal decided first keeps the capital snapshot of its issue time (Rs 0.00 committed); that snapshot is informational.
The second decision reads the journal again (open positions + approved signals awaiting my decision, every strategy and
version) and a second approval on the same symbol, direction and session is blocked (SAME_SYMBOL_ALREADY_APPROVED)."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.cards import build_card
from app.journal import CASH_INDEX, paper_book, pool_account
from app.paper import PaperTrader
from app.risk.engine import decide
from app.risk.portfolio import AccountState, SAME_SYMBOL_ALREADY_APPROVED
from tests.phase5_fixtures import GOOD_MARKET, SECTORS, ZeroCosts, cash_signal
from tests.phase6_fixtures import NOW, FakeMarks, new_journal, settings

pytestmark = pytest.mark.usefixtures("fixed_clock")

SECTORS_AXIS = {**SECTORS, "AXISBANK": "Financial Services"}


def low_corr(a, b):
    """Measured and low between different stocks: the correlation limit never interferes here."""
    return 1.0 if a == b else 0.1


def _journal(s=None):
    s = s or settings()
    j, _ = new_journal(s)
    j.ensure_equity_start(CASH_INDEX, NOW - timedelta(days=1), s.account.capital_inr)
    return s, j


def _sig(sid, symbol="AXISBANK", strategy="range_reversion_v2", version="2/test", direction="LONG", **kw):
    entry = 1000.0
    stop, target = (990.0, 1030.0) if direction == "LONG" else (1010.0, 970.0)
    return replace(cash_signal(symbol=symbol, entry=entry, stop=stop, target=target, direction=direction, **kw),
                   signal_id=sid, strategy_id=strategy, strategy_version=version)


def _live(s, j, marks=None):
    return pool_account(j, s, CASH_INDEX, NOW, marks or FakeMarks(), ZeroCosts())[0]


def _decide(s, acct, sig):
    return decide(sig, acct, GOOD_MARKET, s, ZeroCosts(), low_corr, SECTORS_AXIS)


def _issue(s, j, sig):
    """What the live engine does: decide on the live book, then store the signal with its paper trade."""
    d = _decide(s, _live(s, j), sig)
    j.save_signal(sig, d, NOW, paper=PaperTrader.pending_row(sig, d))
    return d


# (a) ---------------- one approval per symbol, direction and session ----------------
def test_second_same_symbol_signal_is_blocked_while_the_first_awaits_my_decision():
    s, j = _journal()
    first = _issue(s, j, _sig("axis-v2", strategy="range_reversion_v2", version="2/abc"))
    assert first.status == "APPROVED", first.reasons
    second = _issue(s, j, _sig("axis-v1", strategy="range_reversion", version="1/def"))
    assert second.status == "NO_TRADE"
    assert SAME_SYMBOL_ALREADY_APPROVED in second.reason_codes, second.reasons
    assert any("pending:axis-v2" in r for r in second.reasons)


def test_an_open_position_on_the_symbol_in_the_session_also_blocks():
    s, j = _journal()
    _issue(s, j, _sig("axis-1"))
    j.record_decision("axis-1", {"decision": "TOOK", "fill_price": 1000.0}, NOW)
    acct = _live(s, j, FakeMarks({"AXISBANK": 1000.0}))
    assert [p.origin for p in acct.positions] == ["SIGNAL"]
    d = _decide(s, acct, _sig("axis-2", strategy="range_reversion"))
    assert SAME_SYMBOL_ALREADY_APPROVED in d.reason_codes, d.reasons


def test_other_direction_other_symbol_and_skipped_signals_are_not_blocked_by_the_rule():
    s, j = _journal()
    _issue(s, j, _sig("axis-long"))
    assert SAME_SYMBOL_ALREADY_APPROVED not in _decide(s, _live(s, j), _sig("axis-short", direction="SHORT")).reason_codes
    assert SAME_SYMBOL_ALREADY_APPROVED not in _decide(s, _live(s, j), _sig("hdfc", symbol="HDFCBANK")).reason_codes
    j.record_decision("axis-long", {"decision": "SKIPPED"}, NOW)              # skipped: the reservation is released
    assert _decide(s, _live(s, j), _sig("axis-again")).status == "APPROVED"


def test_a_reservation_from_an_earlier_session_does_not_count_for_this_session():
    s, j = _journal()
    old = _sig("axis-swing", horizon="SWING")
    old = replace(old, created_at=old.created_at - timedelta(days=1), data_as_of=old.data_as_of - timedelta(days=1),
                  valid_until=NOW + timedelta(days=1))
    j.save_signal(old, _decide(s, _live(s, j), old), NOW - timedelta(days=1))
    acct = _live(s, j)
    assert [p.origin for p in acct.positions] == ["PENDING"]               # still reserves capital
    assert SAME_SYMBOL_ALREADY_APPROVED not in _decide(s, acct, _sig("axis-today")).reason_codes


def test_the_rule_can_be_switched_off():
    s, j = _journal(settings(block_same_symbol_same_session=False))
    _issue(s, j, _sig("axis-v2"))
    d = _decide(s, _live(s, j), _sig("axis-v1", strategy="range_reversion"))
    assert SAME_SYMBOL_ALREADY_APPROVED not in d.reason_codes
    assert d.facts["pool"]["committed_inr"] == 50000.0                       # the pending one still counts


# (b) ---------------- committed capital: open positions + every pending reservation ----------------
def test_committed_capital_includes_a_pending_reservation_from_another_strategy_version():
    s, j = _journal()
    first = _issue(s, j, _sig("axis-v2", strategy="range_reversion_v2", version="2/abc"))
    assert first.margin_inr == 50000.0
    d = _decide(s, _live(s, j), _sig("hdfc-v1", symbol="HDFCBANK", strategy="range_reversion", version="1/def"))
    pool = d.facts["pool"]
    assert pool["committed_inr"] == 50000.0 and pool["after_inr"] == 100000.0
    assert [(x["id"], x["origin"]) for x in pool["items"]] == [("pending:axis-v2", "PENDING")]
    res = j.pending_reservations(CASH_INDEX, NOW)
    assert [(r["strategy_id"], r["strategy_version"], r["direction"]) for r in res] == [
        ("range_reversion_v2", "2/abc", "LONG")]


# (c) ---------------- a stale snapshot never approves what the live check blocks ----------------
def test_stale_snapshot_does_not_allow_an_approval_the_live_check_blocks():
    s, j = _journal()
    stale = AccountState(**{**_live(s, j).__dict__})                         # the book before anything was issued
    for sid, sym in (("r", "RELIANCE"), ("h", "HDFCBANK"), ("i", "INFY")):   # Rs 50,000 each: Rs 1,50,000 = usable
        assert _issue(s, j, _sig(sid, symbol=sym)).status == "APPROVED"
    late = _sig("tcs", symbol="TCS")
    on_snapshot = _decide(s, stale, late)
    assert on_snapshot.status == "APPROVED" and on_snapshot.facts["pool"]["committed_inr"] == 0.0
    live = _decide(s, _live(s, j), late)
    assert live.status == "NO_TRADE" and "RISK_BUDGET_USED" in live.reason_codes, live.reasons
    assert live.facts["pool"]["committed_inr"] == 150000.0
    # the first card's snapshot still says Rs 0.00: shown as informational, at issue time
    first = j.signal("r")
    card = build_card(_sig("r", symbol="RELIANCE"), _decide(s, stale, _sig("r", symbol="RELIANCE")), score=None,
                      regime=None, bias=None, events_week=[], track=None, alert=None, now=NOW)
    assert first["decision"]["facts"]["pool"]["committed_inr"] == 0.0
    assert card["commitment"]["committed_inr"] == 0.0 and "informational" in card["commitment"]["as_of"]


def test_same_symbol_snapshot_with_zero_committed_is_blocked_live():
    """The AXISBANK case: the second signal decided against the issue-time book would pass (Rs 0.00 committed);
    on the live book it is blocked."""
    s, j = _journal()
    stale = _live(s, j)
    _issue(s, j, _sig("axis-v2"))
    v1 = _sig("axis-v1", strategy="range_reversion", version="1/def")
    assert _decide(s, stale, v1).status == "APPROVED"
    assert SAME_SYMBOL_ALREADY_APPROVED in _decide(s, _live(s, j), v1).reason_codes


# (d) ---------------- the paper book is unchanged ----------------
def test_paper_trades_never_count_for_the_same_symbol_rule_and_the_paper_book_is_unchanged():
    s, j = _journal()
    for sid in ("p1", "p2"):                                                  # two open paper AXISBANK trades
        j.upsert_paper({"signal_id": sid, "trade_type": "INTRADAY_STOCKS", "pool": CASH_INDEX, "symbol": "AXISBANK",
                        "direction": "LONG", "legs_json": None, "quantity": 50, "status": "OPEN",
                        "entry_utc": NOW.isoformat(), "entry_price": 1000.0, "stop": 990.0, "target": 1030.0,
                        "horizon": "INTRADAY", "strategy_id": "range_reversion_v2", "margin_inr": 50000.0,
                        "risk_inr": 500.0, "note": '{"valid_until_utc": "%s"}' % (NOW + timedelta(minutes=15)).isoformat()})
    before = paper_book(j, s, CASH_INDEX, NOW, strategy_id="range_reversion_v2")
    assert _live(s, j).positions == ()                                       # not in my real book
    d = _decide(s, _live(s, j), _sig("axis-real"))
    assert d.status == "APPROVED", d.reasons
    assert paper_book(j, s, CASH_INDEX, NOW, strategy_id="range_reversion_v2") == before
    assert before["committed_inr"] == 100000.0
