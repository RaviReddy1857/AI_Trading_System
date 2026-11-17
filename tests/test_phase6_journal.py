"""Phase 6 M1: the journal - stored signals (never duplicated or edited), 'I took this trade' / 'I skipped it',
manual positions (cash and option legs, UNHEDGED flag), closing all or part of a position with the realised P&L
computed by code, stop moves, and the trade-type switches (default off, logged, kept across a restart)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.journal import (CASH_INDEX, STOCK_OPTIONS, JournalError, invalidation_of, max_loss_inr, parse_manual,
                         pool_of_trade_type, trade_type_of)
from tests.phase6_fixtures import (NOW, T0, FlatCosts, ZeroCosts, app_client, approved, bull_put, cash_signal,
                                   long_call, new_journal, no_trade, settings)


# ---------------- trade types and pools ----------------
@pytest.mark.parametrize("asset,horizon,actions,tt,pool", [
    ("CASH_EQUITY", "INTRADAY", [], "INTRADAY_STOCKS", CASH_INDEX),
    ("CASH_EQUITY", "SWING", [], "SWING_STOCKS", CASH_INDEX),
    ("INDEX_OPTION", "WEEKLY", ["BUY"], "INDEX_OPTION_BUYING", CASH_INDEX),
    ("INDEX_OPTION", "WEEKLY", ["SELL", "BUY"], "INDEX_OPTION_SELLING", CASH_INDEX),
    ("STOCK_OPTION", "MONTHLY", ["BUY"], "STOCK_OPTION_BUYING", CASH_INDEX),
    ("STOCK_OPTION", "MONTHLY", ["SELL", "BUY"], "STOCK_OPTION_SELLING", STOCK_OPTIONS),
    ("STOCK_OPTION", "MONTHLY", ["SELL"], "STOCK_OPTION_SELLING", STOCK_OPTIONS),
])
def test_trade_type_and_pool(asset, horizon, actions, tt, pool):
    legs = [{"action": a} for a in actions]
    assert trade_type_of(asset, horizon, legs) == tt
    assert pool_of_trade_type(tt) == pool


# ---------------- signals ----------------
def test_signal_stored_once_and_never_edited():
    j, _ = new_journal()
    sig = cash_signal()
    assert j.save_signal(sig, approved(), NOW, score=55.0) is True
    assert j.save_signal(sig, no_trade(), NOW, score=10.0) is False          # same id again (restart, same bar)
    row = j.signal(sig.signal_id)
    assert row["status"] == "APPROVED" and row["score"] == 55.0 and row["trade_type"] == "INTRADAY_STOCKS"
    assert len(j.signals_since(T0 - timedelta(days=1))) == 1


# ---------------- decisions ----------------
def test_took_opens_a_position_with_my_fill():
    j, _ = new_journal()
    sig = cash_signal(entry=100.0, stop=90.0, target=120.0)
    j.save_signal(sig, approved(quantity=10), NOW)
    r = j.record_decision(sig.signal_id, {"decision": "TOOK", "fill_price": 100.4, "note": "filled a bit higher"}, NOW)
    pos = j.position(r["position_id"])
    assert (pos.origin, pos.quantity, pos.entry_price, pos.stop, pos.target) == ("SIGNAL", 10, 100.4, 90.0, 120.0)
    assert pos.fill_estimated is False and pos.pool == CASH_INDEX and pos.strategy_id == "orb_v2"
    assert pos.invalidation == {"level": 100.0, "side": "below", "why": "back below the signal bar's close"}
    d = j.decision(sig.signal_id)
    assert d["decision"] == "TOOK" and d["fill_price"] == 100.4 and d["position_id"] == pos.position_id


def test_took_without_fill_uses_signal_price_marked_estimated():
    j, _ = new_journal()
    sig = cash_signal()
    j.save_signal(sig, approved(quantity=7), NOW)
    pos = j.position(j.record_decision(sig.signal_id, {"decision": "TOOK"}, NOW)["position_id"])
    assert pos.entry_price == sig.entry and pos.fill_estimated is True and pos.quantity == 7


def test_skipped_stores_no_position():
    j, _ = new_journal()
    sig = cash_signal()
    j.save_signal(sig, approved(), NOW)
    assert j.record_decision(sig.signal_id, {"decision": "SKIPPED", "note": "too late"}, NOW)["position_id"] is None
    assert j.positions() == []


@pytest.mark.parametrize("setup,payload,status", [
    ("none", {"decision": "TOOK"}, 404),
    ("approved", {"decision": "MAYBE"}, 400),
    ("no_trade", {"decision": "TOOK"}, 409),
    ("expired", {"decision": "TOOK"}, 409),
    ("approved", {"decision": "TOOK", "fill_price": -1}, 400),
    ("approved", {"decision": "TOOK", "quantity": 0}, 400),
    ("approved", {"decision": "TOOK", "fill_time": "2099-01-01T10:00:00+05:30"}, 400),
])
def test_bad_decisions_refused(setup, payload, status):
    j, _ = new_journal()
    sig = cash_signal()
    if setup != "none":
        j.save_signal(sig, no_trade() if setup == "no_trade" else approved(), NOW)
    now = sig.valid_until if setup == "expired" else NOW
    with pytest.raises(JournalError) as e:
        j.record_decision(sig.signal_id, payload, now)
    assert e.value.status == status
    assert j.positions() == []


def test_one_decision_per_signal():
    j, _ = new_journal()
    sig = cash_signal()
    j.save_signal(sig, approved(), NOW)
    j.record_decision(sig.signal_id, {"decision": "SKIPPED"}, NOW)
    with pytest.raises(JournalError) as e:
        j.record_decision(sig.signal_id, {"decision": "TOOK"}, NOW)
    assert e.value.status == 409


def test_skipping_an_expired_signal_is_allowed():
    j, _ = new_journal()
    sig = cash_signal()
    j.save_signal(sig, approved(), NOW)
    assert j.record_decision(sig.signal_id, {"decision": "SKIPPED"}, sig.valid_until + timedelta(hours=1))["ok"]


def test_took_option_spread_with_fills_per_leg():
    j, _ = new_journal()
    sig = bull_put(margin_per_lot=40000.0)
    j.save_signal(sig, approved(quantity=150, lots=2, margin=80000.0), NOW)
    r = j.record_decision(sig.signal_id, {"decision": "TOOK", "fills": [59.5, 30.5]}, NOW)
    pos = j.position(r["position_id"])
    assert pos.quantity == 2 and [x.lots for x in pos.legs] == [2, 2]
    assert [x.price for x in pos.legs] == [59.5, 30.5] and pos.entry_price == 29.0      # net credit per share
    assert pos.margin_inr == 80000.0 and pos.hedged and pos.trade_type == "INDEX_OPTION_SELLING"
    assert pos.invalidation["level"] == 24000 and pos.invalidation["side"] == "below"
    with pytest.raises(JournalError):                                             # one price for two legs
        j2, _ = new_journal()
        j2.save_signal(sig, approved(quantity=150, lots=2), NOW)
        j2.record_decision(sig.signal_id, {"decision": "TOOK", "fill_price": 29.0}, NOW)


# ---------------- manual positions ----------------
def test_manual_cash_position():
    j, _ = new_journal()
    pos = j.add_manual_position({"symbol": "infy", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 5,
                                 "entry_price": 1500.0, "entry_date": "2026-11-12", "stop": 1450.0, "target": 1600.0},
                                NOW)
    assert (pos.symbol, pos.origin, pos.pool, pos.horizon) == ("INFY", "MANUAL", CASH_INDEX, "SWING")
    assert j.positions()[0].position_id == pos.position_id


@pytest.mark.parametrize("bad", [
    {"trade_type": "SWING_STOCKS", "direction": "UP"},
    {"trade_type": "SWING_STOCKS", "direction": "SHORT"},                    # delivery cannot be short
    {"entry_date": "2099-01-01"},
    {"quantity": 2.5},
    {"stop": 1600.0, "target": 1450.0},                                      # wrong sides for a long
    {"entry_price": float("nan")},
])
def test_manual_cash_position_refused(bad):
    j, _ = new_journal()
    body = {"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 5, "entry_price": 1500.0,
            "entry_date": "2026-11-12", **bad}
    with pytest.raises(JournalError):
        j.add_manual_position(body, NOW)
    assert j.positions() == []


def _legs(*specs):
    return [{"action": a, "right": r, "strike": k, "expiry": "2026-11-24", "lots": 1, "lot_size": 500, "price": p}
            for a, r, k, p in specs]


def test_manual_stock_spread_is_hedged_and_in_the_stock_pool():
    pos = parse_manual({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-10", "margin_inr": 90000,
                        "legs": _legs(("SELL", "PE", 1300, 12.0), ("BUY", "PE", 1260, 4.0))}, NOW)
    assert pos.hedged and pos.trade_type == "STOCK_OPTION_SELLING" and pos.pool == STOCK_OPTIONS
    assert pos.direction == "SPREAD" and pos.entry_price == 8.0 and pos.view == "BULLISH"


def test_manual_unhedged_short_call_is_stored_and_flagged():
    j, _ = new_journal()
    pos = j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-10",
                                 "legs": _legs(("SELL", "CE", 1400, 10.0))}, NOW)
    v = j.position(pos.position_id).to_view()
    assert v["hedged"] is False and v["flag"] == "UNHEDGED" and v["view"] == "BEARISH"
    assert max_loss_inr(j.position(pos.position_id), ZeroCosts()) is None            # no limit: unknown, never zero


def test_unhedged_short_put_max_loss_is_strike_based():
    pos = parse_manual({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-10",
                        "legs": _legs(("SELL", "PE", 1300, 10.0))}, NOW)
    assert not pos.hedged
    assert max_loss_inr(pos, ZeroCosts()) == Decimal("645000.00")                    # (1300 - 10) x 500


def test_manual_option_position_refused_for_bad_legs():
    with pytest.raises(JournalError):
        parse_manual({"symbol": "NIFTY", "kind": "index", "entry_date": "2026-11-10",
                      "legs": [{"action": "SELL", "right": "XX", "strike": 1, "expiry": "2026-11-24", "lots": 1,
                                "lot_size": 75, "price": 1}]}, NOW)
    with pytest.raises(JournalError):                                           # different lots per leg
        legs = _legs(("SELL", "PE", 24000, 60.0), ("BUY", "PE", 23900, 30.0))
        legs[1]["lots"] = 2
        parse_manual({"symbol": "NIFTY", "kind": "index", "entry_date": "2026-11-10", "legs": legs}, NOW)


# ---------------- closing ----------------
def test_partial_then_full_close_realised_pnl_by_hand():
    j, _ = new_journal()
    pos = j.add_manual_position({"symbol": "INFY", "trade_type": "INTRADAY_STOCKS", "direction": "LONG", "quantity": 10,
                                 "entry_price": 100.0, "entry_date": "2026-11-16", "stop": 95.0}, NOW)
    r = j.close_position(pos.position_id, {"exit_price": 104.0, "quantity": 4}, NOW, FlatCosts())
    # 4 x (104 - 100) = 16.00 gross; FlatCosts equity round trip Rs 20 -> net -4.00
    assert (r["gross_inr"], r["estimated_charges_inr"], r["net_inr"], r["remaining"]) == (16.0, 20.0, -4.0, 6)
    assert j.position(pos.position_id).status == "OPEN" and j.position(pos.position_id).plan["partial_booked"]
    r = j.close_position(pos.position_id, {"exit_price": 98.5}, NOW, FlatCosts())
    assert (r["gross_inr"], r["net_inr"], r["status"]) == (-9.0, -29.0, "CLOSED")    # 6 x -1.5 - 20
    assert j.realised(CASH_INDEX) == Decimal("-33.00")
    with pytest.raises(JournalError) as e:
        j.close_position(pos.position_id, {"exit_price": 99.0}, NOW, FlatCosts())
    assert e.value.status == 409


def test_close_spread_by_leg_prices():
    j, _ = new_journal()
    pos = j.add_manual_position({"symbol": "NIFTY", "kind": "index", "entry_date": "2026-11-16", "margin_inr": 40000,
                                 "legs": [{"action": "SELL", "right": "PE", "strike": 24000, "expiry": "2026-11-24",
                                           "lots": 1, "lot_size": 75, "price": 60.0},
                                          {"action": "BUY", "right": "PE", "strike": 23900, "expiry": "2026-11-24",
                                           "lots": 1, "lot_size": 75, "price": 30.0}]}, NOW)
    r = j.close_position(pos.position_id, {"exit_prices": [40.0, 22.0]}, NOW, ZeroCosts())
    # sold leg: (60 - 40) x 75 = 1,500; bought leg: (22 - 30) x 75 = -600
    assert r["gross_inr"] == 900.0 and r["net_inr"] == 900.0
    with pytest.raises(JournalError):
        j.close_position(pos.position_id, {"exit_price": 18.0}, NOW, ZeroCosts())


def test_stop_move_is_journal_only():
    j, _ = new_journal()
    pos = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 5,
                                 "entry_price": 1500.0, "entry_date": "2026-11-12", "stop": 1450.0}, NOW)
    p2 = j.set_stop(pos.position_id, {"stop": 1500.0}, NOW)
    assert p2.stop == 1500.0 and p2.initial_stop == 1450.0 and p2.plan["plan_stop"] == 1500.0


def test_invalidation_level_from_entry_zone():
    sig = cash_signal(direction="SHORT", entry=100.0, stop=105.0, target=90.0, entry_zone=(99.5, 100.5))
    assert invalidation_of(sig.to_dict())["level"] == 100.5


# ---------------- switches ----------------
def test_switches_default_off_change_logged_and_kept_across_restart(tmp_path):
    db = tmp_path / "j.db"
    j, store = new_journal(path=db)
    assert all(not v["on"] for v in j.switches().values()) and len(j.switches()) == 6
    j.set_switch("INDEX_OPTION_SELLING", True, NOW)
    j.set_switch("INDEX_OPTION_SELLING", False, NOW + timedelta(minutes=1))
    j.set_switch("SWING_STOCKS", True, NOW + timedelta(minutes=2))
    store._db.close()
    j2, _ = new_journal(path=db)                                                 # a restart: same file
    sw = j2.switches()
    assert sw["SWING_STOCKS"]["on"] and not sw["INDEX_OPTION_SELLING"]["on"]
    log = j2.switch_log()
    assert [(x["type"], x["old"], x["new"]) for x in log] == [
        ("SWING_STOCKS", False, True), ("INDEX_OPTION_SELLING", True, False), ("INDEX_OPTION_SELLING", False, True)]
    with pytest.raises(JournalError):
        j2.set_switch("FUTURES", True, NOW)


# ---------------- routes ----------------
@pytest.mark.usefixtures("fixed_clock")
def test_journal_routes_end_to_end():
    with app_client() as c:
        r = c.post("/journal/alert-switches", json={"type": "SWING_STOCKS", "on": True})
        assert r.status_code == 200 and r.json()["new"] is True
        assert c.post("/journal/alert-switches", json={"type": "SWING_STOCKS", "on": "yes"}).status_code == 400
        sw = {x["type"]: x["on"] for x in c.get("/api/alert-switches").json()["switches"]}
        assert sw["SWING_STOCKS"] and not sw["INTRADAY_STOCKS"]
        r = c.post("/journal/positions", json={"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG",
                                               "quantity": 3, "entry_price": 1500.0, "entry_date": "2026-11-12"})
        assert r.status_code == 200
        pid = r.json()["position"]["position_id"]
        assert [p["position_id"] for p in c.get("/api/positions").json()["open"]] == [pid]
        assert c.post(f"/journal/positions/{pid}/close", json={"exit_price": 1510.0}).json()["status"] == "CLOSED"
        assert c.get("/api/positions").json()["open"] == []
        assert c.post("/journal/positions/nope/close", json={"exit_price": 1.0}).status_code == 404
        assert c.post("/journal/nope/decision", json={"decision": "TOOK"}).status_code == 404
        # writes need the local host and JSON
        assert c.post("/journal/alert-switches", json={"type": "SWING_STOCKS", "on": False},
                      headers={"host": "evil.example"}).status_code == 403
        assert c.post("/journal/alert-switches", content="type=x",
                      headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
