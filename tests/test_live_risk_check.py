"""Approvals are checked on the LIVE real book at the moment of the check, never on a stored snapshot (18-Nov-2026).

The AXISBANK case: range_reversion_v2 (margin Rs 49,897.20) and range_reversion v1 both at 13:15 IST. The card of the
signal decided first keeps the capital snapshot of its issue time (Rs 0.00 committed); that snapshot is informational.
The second decision reads the journal again (open positions + approved signals awaiting my decision, every strategy and
version) and a second approval on the same symbol, direction and session is blocked (SAME_SYMBOL_ALREADY_APPROVED)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from app.cards import build_card
from app.journal import CASH_INDEX, paper_book, pool_account
from app.paper import PaperTrader
from app.risk.engine import decide
from app.risk.portfolio import (BEARISH, BULLISH, EXPOSURE_UNKNOWN, NEUTRAL, SAME_SYMBOL_ALREADY_APPROVED, AccountState,
                                OpenPosition, exposure_of, same_symbol_check)
from tests.phase5_fixtures import (GOOD_MARKET, SECTORS, FlatCosts, ZeroCosts, account, cash_signal, leg, long_call,
                                   option_signal)
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
    yesterday = replace(GOOD_MARKET, now=NOW - timedelta(days=1))           # approved in the previous session
    j.save_signal(old, decide(old, _live(s, j), yesterday, s, ZeroCosts(), low_corr, SECTORS_AXIS),
                  NOW - timedelta(days=1))
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


# ================= Codex review of 6dfdd7c =================
# 1. the session is the approval's, not the data's ------------------------------------------
def test_a_swing_signal_on_previous_session_data_approved_today_blocks_a_second_today():
    s, j = _journal()
    swing = _sig("axis-swing", strategy="swing_breakout", horizon="SWING")
    swing = replace(swing, created_at=swing.created_at - timedelta(days=1), data_as_of=swing.data_as_of - timedelta(days=1),
                    valid_until=NOW + timedelta(days=1))                     # built on yesterday's close
    first = _issue(s, j, swing)                                              # decided and stored today
    assert first.status == "APPROVED" and first.facts["decision_session"] == "2026-11-16"
    res = j.pending_reservations(CASH_INDEX, NOW)
    assert [(r["signal_id"], r["session"].isoformat()) for r in res] == [("axis-swing", "2026-11-16")]
    second = _decide(s, _live(s, j), _sig("axis-intraday", strategy="range_reversion"))
    assert SAME_SYMBOL_ALREADY_APPROVED in second.reason_codes, second.reasons


def test_a_stored_signal_without_the_decision_session_falls_back_to_its_stored_time_then_to_today():
    from datetime import date
    from app.journal import decision_session
    assert decision_session({"decision": {"facts": {"decision_session": "2026-11-16"}},
                             "stored_utc": "2026-11-13T05:00:00+00:00"}) == date(2026, 11, 16)
    # before 18-Nov-2026 no decision_session: the time it was saved (right after the decision), as an IST day
    assert decision_session({"decision": {"facts": {}}, "stored_utc": "2026-11-15T19:00:00+00:00"}) == date(2026, 11, 16)
    assert decision_session({"decision": None, "stored_utc": None}) is None
    # neither readable: pool_account counts it in the current session (unknown never lets a second one through)
    s, j = _journal()
    _issue(s, j, _sig("axis-old"))
    legacy = '{"status": "APPROVED", "risk_inr": 500.0, "margin_inr": 50000.0, "facts": {}}'
    with j._lock:
        j._db.execute("UPDATE signals SET decision_json=?, stored_utc=? WHERE signal_id=?",
                      (legacy, "not a time", "axis-old"))
        j._db.commit()
    acct = _live(s, j)
    assert acct.positions[0].session.isoformat() == "2026-11-16"
    assert SAME_SYMBOL_ALREADY_APPROVED in _decide(s, acct, _sig("axis-new")).reason_codes


# 2. exposure to the underlying, not the raw direction ---------------------------------------
def _call(k=24000):
    return [leg("BUY", "CE", k, 100.0)]


def _put(k=24000):
    return [leg("BUY", "PE", k, 100.0)]


def _bull_put():
    return [leg("SELL", "PE", 24000, 60.0), leg("BUY", "PE", 23900, 30.0)]


def _bull_call():
    return [leg("BUY", "CE", 24000, 100.0), leg("SELL", "CE", 24100, 60.0)]


def _bear_call():
    return [leg("SELL", "CE", 24000, 60.0), leg("BUY", "CE", 24100, 30.0)]


def _bear_put():
    return [leg("BUY", "PE", 24000, 100.0), leg("SELL", "PE", 23900, 60.0)]


def _condor(put_wing=100, call_wing=100):
    return [leg("SELL", "PE", 23900, 40.0), leg("BUY", "PE", 23900 - put_wing, 20.0),
            leg("SELL", "CE", 24100, 40.0), leg("BUY", "CE", 24100 + call_wing, 20.0)]


def _strangle():
    return [leg("SELL", "PE", 23800, 40.0), leg("SELL", "CE", 24200, 40.0)]


def test_exposure_classification():
    assert exposure_of("LONG", None) == BULLISH and exposure_of("SHORT", ()) == BEARISH
    assert exposure_of("SPREAD", None) is None                               # a cash direction that is not known
    assert exposure_of("LONG", _call()) == BULLISH
    assert exposure_of("LONG", _put()) == BEARISH
    assert exposure_of("SPREAD", _bull_put()) == BULLISH
    assert exposure_of("SPREAD", _bull_call()) == BULLISH                   # Position.view says BEARISH here
    assert exposure_of("SPREAD", _bear_call()) == BEARISH
    assert exposure_of("SPREAD", _bear_put()) == BEARISH
    assert exposure_of("SPREAD", _condor()) == NEUTRAL
    assert exposure_of("SPREAD", _condor(100, 200)) == NEUTRAL              # uneven wings: still a range bet
    assert exposure_of("SPREAD", _strangle()) == NEUTRAL
    stored = [{"action": "SELL", "right": "PE", "strike": 24000, "lots": 1},
              {"action": "BUY", "right": "PE", "strike": 23900, "lots": 1}]
    assert exposure_of("SPREAD", stored) == BULLISH                          # a stored signal's leg dicts
    assert exposure_of("SPREAD", [{"action": "BUY", "right": "XX", "strike": 24000, "lots": 1}]) is None
    assert exposure_of("SPREAD", [{"action": "BUY", "right": "CE", "strike": None, "lots": 1}]) is None


TODAY = NOW.date()


def _held(legs, sym="NIFTY", pid="pending:first"):
    return OpenPosition(pid, sym, "OTHER", 1000.0, 10000.0, 0.0, 0.0, origin="PENDING",
                        exposure=exposure_of("SPREAD", legs), session=TODAY)


@pytest.mark.parametrize("held, proposed, blocked", [
    (_call(), _put(), False),                        # bought call vs bought put: opposite exposure
    (_bull_put(), _call(), True),                    # bull put spread vs bought call: both bullish
    (_condor(), _condor(), True),                    # condor vs condor
    (_condor(), _call(), False),                     # condor (neutral) vs bought call (bullish)
    (_bear_put(), _bear_call(), True),
    (_bull_call(), _bull_put(), True),
])
def test_same_underlying_same_exposure_same_session(held, proposed, blocked):
    code, why = same_symbol_check("NIFTY", exposure_of("SPREAD", proposed), [_held(held)], TODAY)
    assert (code == SAME_SYMBOL_ALREADY_APPROVED) is blocked, why
    other_day = [replace(_held(held), session=TODAY - timedelta(days=1))]
    assert same_symbol_check("NIFTY", exposure_of("SPREAD", proposed), other_day, TODAY)[0] is None
    assert same_symbol_check("NIFTY", exposure_of("SPREAD", proposed), [_held(held, sym="BANKNIFTY")], TODAY)[0] is None


def test_unknown_exposure_is_blocked():
    assert same_symbol_check("NIFTY", None, [], TODAY)[0] == EXPOSURE_UNKNOWN                 # the proposed one
    unknown_held = replace(_held(_call()), exposure=None)
    assert same_symbol_check("NIFTY", BULLISH, [unknown_held], TODAY)[0] == EXPOSURE_UNKNOWN  # a held one


def test_the_engine_blocks_a_bought_call_after_a_bull_put_spread_on_the_same_index():
    held = _held(_bull_put())
    d = decide(long_call(), account(positions=(held,)), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert SAME_SYMBOL_ALREADY_APPROVED in d.reason_codes and d.facts["exposure"] == BULLISH, d.reasons
    put = option_signal(_put(), 100.0, 50.0, 200.0, direction="LONG", strategy="long_option")
    d = decide(put, account(positions=(held,)), GOOD_MARKET, settings(), FlatCosts(), low_corr, SECTORS)
    assert SAME_SYMBOL_ALREADY_APPROVED not in d.reason_codes and d.facts["exposure"] == BEARISH


# 3. config.yaml carries the switch; config.local.yaml can override it -------------------------
def test_the_switch_is_in_config_yaml_and_a_local_override_turns_it_off(tmp_path, monkeypatch):
    import app.config as config
    from app.config import NO_LOCAL_ENV, load_settings
    committed = load_settings(config.ROOT / "config.yaml")
    assert committed.risk.block_same_symbol_same_session is True
    monkeypatch.delenv(NO_LOCAL_ENV)
    local = tmp_path / "config.local.yaml"
    local.write_text("risk:\n  block_same_symbol_same_session: false\n", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_CONFIG", local)
    s = load_settings()
    assert s.risk.block_same_symbol_same_session is False
    assert s.risk.max_trades_per_sector == committed.risk.max_trades_per_sector    # sibling keys kept
    raw = s.model_dump()
    raw["account"]["capital_inr"] = 200000.0
    s = type(s).model_validate(raw)
    _, j = _journal(s)
    _issue(s, j, _sig("axis-v2"))
    assert SAME_SYMBOL_ALREADY_APPROVED not in _decide(s, _live(s, j), _sig("axis-v1")).reason_codes
    local.write_text("risk:\n  block_same_symbol_same_sesion: false\n", encoding="utf-8")     # a typo
    with pytest.raises(ValueError, match="block_same_symbol_same_sesion"):
        load_settings()


# 4. a signal blocked only by the same-symbol rule is still paper-traded -----------------------
def test_a_signal_blocked_only_by_the_same_symbol_rule_opens_a_flagged_paper_trade_in_its_own_book():
    s, j = _journal()
    _issue(s, j, _sig("axis-v2", strategy="range_reversion_v2"))
    d = _issue(s, j, _sig("axis-v1", strategy="range_reversion"))
    assert d.status == "NO_TRADE" and d.reason_codes == (SAME_SYMBOL_ALREADY_APPROVED,)
    row = j.paper_trade("axis-v1")
    assert row is not None and row["status"] == "PENDING" and row["strategy_id"] == "range_reversion"
    assert json.loads(row["note"])["same_symbol_blocked"] is True
    assert row["quantity"] == d.facts["would_be"]["quantity"] == 50 and row["margin_inr"] == 50000.0
    assert "same_symbol_blocked" not in json.loads(j.paper_trade("axis-v2")["note"])
    assert paper_book(j, s, CASH_INDEX, NOW, strategy_id="range_reversion")["committed_inr"] == 50000.0
    # my real book: only the approved one reserves
    assert [r["signal_id"] for r in j.pending_reservations(CASH_INDEX, NOW)] == ["axis-v2"]
    assert [p.position_id for p in _live(s, j).positions] == ["pending:axis-v2"]


def test_a_signal_with_any_other_block_reason_gets_no_paper_trade():
    s, j = _journal()
    _issue(s, j, _sig("axis-v2"))
    sig = _sig("axis-v1", strategy="range_reversion")
    d = decide(sig, _live(s, j), replace(GOOD_MARKET, price_status="STALE"), s, ZeroCosts(), low_corr, SECTORS_AXIS)
    assert set(d.reason_codes) == {SAME_SYMBOL_ALREADY_APPROVED, "DATA_STALE"}
    assert PaperTrader.pending_row(sig, d) is None
