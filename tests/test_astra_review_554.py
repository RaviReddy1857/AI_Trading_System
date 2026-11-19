"""Codex Astra review of 554c2d8/b365e67 (19-Nov-2026). Each test fails on the code before its fix."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Feed, HeldPriceNotice, load_settings

from app.options.settlement import NOTICE, PRICE_UNKNOWN, URGENT, WARNING
from tests.test_settlement_last_price import World, ist, tick


# ================= 1. HIGH: the settlement snapshot is authoritative, None included; one snapshot per symbol =================
def test_a_pre_open_tick_at_the_open_at_e6_is_no_valid_price_not_a_silent_far_otm():
    w = World(left=6)
    w.hub.on_tick(tick(ist(9, 14, 59), 1400.0))                            # pre-open: valid, but not continuous
    assert w.eng.m.spot("RELIANCE", ist(9, 15, 10)) == 1400.0              # the LIVE spot other exit rules use ...
    assert w.hub.settlement_snapshot("RELIANCE", ist(9, 15, 10))["price"] is None   # ... is not a settlement price
    due, _ = w.step(ist(9, 15, 10))
    assert [c["level"] for c in due] == [NOTICE]                           # E-6: the no-price assumption
    assert PRICE_UNKNOWN in due[0]["reason"] and due[0]["values"]["alert"]["price"]["assumed_near_money"] is True


def test_positions_on_the_same_underlying_see_one_snapshot_per_evaluation():
    w = World(left=6)
    exp = w.pos.legs[0].expiry
    w.j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
                             "legs": [{"action": "SELL", "right": "PE", "strike": 1380, "expiry": exp.isoformat(),
                                       "lots": 1, "lot_size": 500, "price": 12.0},
                                      {"action": "BUY", "right": "PE", "strike": 1340, "expiry": exp.isoformat(),
                                       "lots": 1, "lot_size": 500, "price": 4.0}]}, ist(9, 30))
    w.ticks(ist(10, 0), ist(10, 4, 50), 10, [1400.0])
    calls = []
    real = w.hub.settlement_snapshot

    def counting(sym, now, *a):
        calls.append(sym)
        return real(sym, now, *a)
    w.hub.settlement_snapshot = counting
    w.eng.evaluate_exits(ist(10, 5), "TRENDING_UP", five={}, daily={"RELIANCE": []})
    assert calls == ["RELIANCE"]                                           # two positions, one read
    alerts = [c["values"]["alert"] for c in w.book.calls() if c["kind"] == "SETTLEMENT"]
    assert alerts and len({(a["price"]["spot"], a["price"]["as_of"]) for a in alerts}) == 1


# ================= 2. HIGH: stored levels no current reason supports are flagged at start, never changed =================
def test_the_startup_check_flags_an_unsupported_stored_level_and_changes_nothing():
    w = World(left=9)                                                      # E-9, far OTM: no current alert at all
    w.j.update_plan(w.pos.position_id, {**w.pos.plan, "settlement_level": URGENT})
    w.ticks(ist(10, 0), ist(10, 4, 50), 10, [1400.0])
    restored = w.eng.restore(ist(10, 5))
    flags = restored["settlement_level_flags"]
    assert [(f["position_id"], f["stored_level"], f["current_level"]) for f in flags] == [(w.pos.position_id, URGENT, None)]
    assert w.j.position(w.pos.position_id).plan["settlement_level"] == URGENT        # unchanged
    assert any("settlement level check" in p for p in w.eng.problems)


def test_the_startup_check_leaves_a_supported_level_alone():
    w = World(left=7)
    w.ticks(ist(10, 0), ist(10, 4, 50), 10, [1290.0])                      # ITM at E-7: WARNING now
    w.j.update_plan(w.pos.position_id, {**w.pos.plan, "settlement_level": WARNING})
    assert w.eng.restore(ist(10, 5))["settlement_level_flags"] == []


# ================= 3. HIGH: overdue is URGENT before the alert is built and the plan saved =================
def test_a_far_otm_position_is_warning_at_the_planned_exit_then_urgent_once_overdue():
    w = World(left=5)                                                      # Monday is the forced-exit day
    w.ticks(ist(14, 50), ist(14, 59, 50), 10, [1400.0])                    # far out of the money
    due, _ = w.step(ist(15, 0))                                            # 15:00: the planned exit time has come
    assert [c["level"] for c in due] == [WARNING] and w.level() == WARNING
    due, _ = w.step(ist(15, 40))                                           # the options' close: overdue
    assert [(c["level"], c["overdue"]) for c in due] == [(URGENT, True)]
    assert w.level() == URGENT and w.call()["values"]["alert"]["level"] == URGENT


# ================= 4. MEDIUM: the notice expires at the continuous-session end; durable key; flag after enqueue =================
def _outage(w, now=ist(10, 0)):
    w.ticks(ist(9, 15), ist(9, 20), 10, [1400.0])                          # then nothing
    return w.step(now)[1]


def test_the_notice_expires_at_the_end_of_the_continuous_session():
    w = World(left=9)
    notes = _outage(w)
    assert len(notes) == 1
    row = w.alerts.get("price:RELIANCE:2026-11-16")
    assert row["kind"] == "PRICE_UNAVAILABLE" and row["expires_utc"] == ist(15, 15).isoformat()


def test_the_notice_is_deduplicated_on_its_durable_key_across_a_restart():
    w = World(left=9)
    assert len(_outage(w)) == 1
    again = World(left=9, store=w.store, journal=w.j, hub=w.hub)            # a restarted engine, same database
    assert again.step(ist(10, 5))[1] == []                                 # the key exists: not recorded again
    assert len(w.alerts.rows("kind=?", ("PRICE_UNAVAILABLE",))) == 1
    assert "RELIANCE" in again.eng._noticed                                 # done for the day


def test_the_day_flag_is_set_only_after_the_notice_is_recorded():
    w = World(left=9)
    real = w.alerts.enqueue
    state = {"fail": True}

    def flaky(*a, **k):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("database is locked")
        return real(*a, **k)
    w.alerts.enqueue = flaky
    assert _outage(w) == [] and "RELIANCE" not in w.eng._noticed           # failed: not marked, retried next bar
    assert len(w.step(ist(10, 5))[1]) == 1 and "RELIANCE" in w.eng._noticed
    assert w.step(ist(10, 10))[1] == []


def test_a_notice_whose_session_has_ended_is_never_recorded():
    w = World(left=9)
    n = {"symbol": "RELIANCE", "key": "price:RELIANCE:2026-11-16", "expires": ist(15, 15), "text": "x"}
    assert w.notifier.price_notices([n], ist(15, 15)) == {"done": [], "new": []}
    assert w.alerts.get(n["key"]) is None


# ================= 6. LOW: unknown config keys are errors; booleans are not notice thresholds =================
@pytest.mark.parametrize("field", ["no_valid_tick_minutes", "rejection_window_minutes", "rejection_share_pct",
                                   "min_ticks"])
def test_a_boolean_notice_threshold_is_rejected(field):
    with pytest.raises(ValidationError):
        HeldPriceNotice(**{field: True})


def test_an_unknown_config_key_is_an_error_and_the_shipped_config_loads():
    with pytest.raises(ValidationError):
        HeldPriceNotice(min_tick=10)                                       # a typo
    with pytest.raises(ValidationError):
        Feed(stale_after_seconds=30, max_jump_pct=5, rejected_quote_grace_minutes=5)
    assert load_settings().feed.held_price_notice.min_ticks == 10
