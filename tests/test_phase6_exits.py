"""Phase 6 M2, step 8 acceptance: for each trade type, a fixture open position produces each exit call (target,
stop, breakeven, trail, partial, time, entry invalidated, regime flip, kill switch) with an action and a reason,
and the call repeats until the position is marked closed. Stock option spreads add the phase 9 calls (strike
breached, trend flip, forced exit) and the settlement-readiness alert. A stale price never triggers and never
silences a call."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.journal import Leg, Position
from app.positions import ExitCallBook, ExitInputs, evaluate, evaluate_positions, settlement_call
from tests.phase6_fixtures import ZeroCosts, new_journal, settings

S = settings()
CAL = MarketCalendar()
NOW = datetime(2026, 11, 16, 5, 1, tzinfo=timezone.utc)                 # Monday 10:31 IST
ENTRY = NOW - timedelta(minutes=30)
FAR = date(2026, 12, 29)


def leg(a, r, k, p, lots=2, ls=75, exp=FAR):
    return Leg(a, r, k, exp, lots, ls, p)


def positions(expiry=FAR):
    """One open position per trade type (all bullish)."""
    return {
        "INTRADAY_STOCKS": Position("p1", "SIGNAL", "s1", "INTRADAY_STOCKS", "CASH_INDEX", "RELIANCE", "LONG", None, 10,
                                    100.0, ENTRY, stop=95.0, target=110.0, initial_stop=95.0, strategy_id="orb_v2",
                                    horizon="INTRADAY", regime_at_entry="TRENDING_UP",
                                    invalidation={"level": 100.0, "side": "below", "why": "entry level"}),
        "SWING_STOCKS": Position("p2", "MANUAL", None, "SWING_STOCKS", "CASH_INDEX", "INFY", "LONG", None, 10, 1000.0,
                                 ENTRY, stop=950.0, target=1100.0, initial_stop=950.0, strategy_id="swing_pullback_v2",
                                 horizon="SWING", regime_at_entry="TRENDING_UP",
                                 invalidation={"level": 1000.0, "side": "below", "why": "entry level"}),
        "INDEX_OPTION_BUYING": Position("p3", "SIGNAL", "s3", "INDEX_OPTION_BUYING", "CASH_INDEX", "NIFTY", "LONG",
                                        (leg("BUY", "CE", 24000, 100.0, exp=expiry),), 2, 100.0, ENTRY, stop=50.0,
                                        target=200.0, initial_stop=50.0, strategy_id="long_option", horizon="SWING",
                                        regime_at_entry="TRENDING_UP",
                                        invalidation={"level": 24000.0, "side": "below", "why": "trigger level"}),
        "INDEX_OPTION_SELLING": Position("p4", "SIGNAL", "s4", "INDEX_OPTION_SELLING", "CASH_INDEX", "NIFTY", "SPREAD",
                                         (leg("SELL", "PE", 24000, 60.0, exp=expiry), leg("BUY", "PE", 23900, 30.0, exp=expiry)),
                                         2, 30.0, ENTRY, stop=45.0, target=1.0, initial_stop=45.0,
                                         strategy_id="credit_spread", horizon="WEEKLY", regime_at_entry="TRENDING_UP",
                                         margin_inr=80000.0,
                                         invalidation={"level": 24000.0, "side": "below", "why": "the sold strike"}),
        "STOCK_OPTION_BUYING": Position("p5", "MANUAL", None, "STOCK_OPTION_BUYING", "CASH_INDEX", "RELIANCE", "LONG",
                                        (leg("BUY", "CE", 1300, 20.0, ls=500, exp=expiry),), 2, 20.0, ENTRY, stop=10.0,
                                        target=40.0, initial_stop=10.0, strategy_id="long_option", horizon="SWING",
                                        invalidation={"level": 1300.0, "side": "below", "why": "level I entered"}),
        "STOCK_OPTION_SELLING": Position("p6", "MANUAL", None, "STOCK_OPTION_SELLING", "STOCK_OPTIONS", "RELIANCE",
                                         "SPREAD", (leg("SELL", "PE", 1300, 12.0, ls=500, exp=expiry),
                                                    leg("BUY", "PE", 1260, 4.0, ls=500, exp=expiry)),
                                         2, 8.0, ENTRY, margin_inr=90000.0, horizon="MONTHLY",
                                         invalidation={"level": 1300.0, "side": "below", "why": "the sold strike"}),
    }


SPOT = {"RELIANCE": 1350.0, "INFY": 1000.0, "NIFTY": 24200.0}
# own prices (per leg) that are neutral: no stop, target or gain rule fires
NEUTRAL = {"INTRADAY_STOCKS": [101.0], "SWING_STOCKS": [1010.0], "INDEX_OPTION_BUYING": [110.0],
           "INDEX_OPTION_SELLING": [58.0, 29.0], "STOCK_OPTION_BUYING": [21.0], "STOCK_OPTION_SELLING": [11.0, 4.0]}
TARGET_PX = {"INTRADAY_STOCKS": [110.0], "SWING_STOCKS": [1100.0], "INDEX_OPTION_BUYING": [200.0],
             "INDEX_OPTION_SELLING": [1.0, 0.5], "STOCK_OPTION_BUYING": [40.0], "STOCK_OPTION_SELLING": [4.0, 0.5]}
STOP_PX = {"INTRADAY_STOCKS": [95.0], "SWING_STOCKS": [950.0], "INDEX_OPTION_BUYING": [50.0],
           "INDEX_OPTION_SELLING": [50.0, 5.0], "STOCK_OPTION_BUYING": [10.0], "STOCK_OPTION_SELLING": [18.0, 2.0]}
ONE_R = {"INTRADAY_STOCKS": [105.0], "SWING_STOCKS": [1050.0], "INDEX_OPTION_BUYING": [150.0],
         "INDEX_OPTION_SELLING": [20.0, 5.0], "STOCK_OPTION_BUYING": [30.0], "STOCK_OPTION_SELLING": [5.0, 5.0]}
TWO_R = {"INTRADAY_STOCKS": [110.0 - 0.01], "SWING_STOCKS": [1099.0], "INDEX_OPTION_BUYING": [199.0],
         "INDEX_OPTION_SELLING": [5.0, 5.0], "STOCK_OPTION_BUYING": [39.0], "STOCK_OPTION_SELLING": [4.5, 4.5]}


def run(tt, prices=None, spot=None, regime="TRENDING_UP", kill=(), now=NOW, pos=None, trend=None, close=None):
    p = pos or positions()[tt]
    x = ExitInputs(now=now, prices=prices if prices is not None else NEUTRAL[tt], spot=spot or SPOT[p.symbol],
                   regime=regime, kill_reasons=tuple(kill), stock_trend=trend, stock_close=close)
    calls, plan = evaluate(p, x, S, CAL, ZeroCosts())
    return {c["kind"]: c for c in calls}, plan


TYPES = list(positions())


@pytest.mark.parametrize("tt", TYPES)
def test_neutral_prices_give_no_call(tt):
    calls, _ = run(tt)
    assert calls == {}, calls


@pytest.mark.parametrize("tt", TYPES)
def test_target(tt):
    calls, _ = run(tt, TARGET_PX[tt])
    assert "TARGET" in calls and calls["TARGET"]["action"] and calls["TARGET"]["reason"]
    assert "Exit" in calls["TARGET"]["action"]


@pytest.mark.parametrize("tt", TYPES)
def test_stop(tt):
    calls, _ = run(tt, STOP_PX[tt])
    assert "STOP" in calls and "Exit" in calls["STOP"]["action"] and calls["STOP"]["reason"]


@pytest.mark.parametrize("tt", TYPES)
def test_breakeven_and_partial_at_one_r(tt):
    calls, plan = run(tt, ONE_R[tt])
    p = positions()[tt]
    assert "BREAKEVEN" in calls and calls["BREAKEVEN"]["action"].startswith("Move stop to")
    assert calls["BREAKEVEN"]["values"]["suggested_stop"] == round(p.entry_price, 2)
    assert "PARTIAL" in calls and ("1 lot of 2" in calls["PARTIAL"]["action"] or "5 shares of 10" in calls["PARTIAL"]["action"])
    assert plan["plan_stop"] == round(p.entry_price, 2)


@pytest.mark.parametrize("tt", TYPES)
def test_trail_beyond_one_and_a_half_r(tt):
    p = positions()[tt]
    if tt == "STOCK_OPTION_SELLING":
        # with the default phase 9 stop (buy back at 2x the credit) the risk R equals the credit, so the most a spread
        # can gain is 1R and no 1.5R trailing call can arise; with my own tighter stop (1.5x credit, R = 4) it does
        p = replace(p, stop=12.0, initial_stop=12.0)
    calls, plan = run(tt, TWO_R[tt], pos=p)
    assert "TRAIL" in calls and calls["TRAIL"]["action"].startswith("Trail the stop to")
    fav_up = not (p.is_option and p.selling)
    assert (plan["plan_stop"] > p.entry_price) if fav_up else (plan["plan_stop"] < p.entry_price)


def test_trail_numbers_by_hand():
    # long 100, stop 95 (R = 5); best 109.99 -> 1.998R; stop trails 1R behind: 104.99
    calls, plan = run("INTRADAY_STOCKS", [109.99])
    assert calls["TRAIL"]["values"]["suggested_stop"] == 104.99 and "Trail the stop to 104.99" == calls["TRAIL"]["action"]


@pytest.mark.parametrize("tt,now,pos_kw", [
    ("INTRADAY_STOCKS", datetime(2026, 11, 16, 9, 46, tzinfo=timezone.utc), {}),              # 15:16 IST
    ("SWING_STOCKS", NOW, {"entry_utc": NOW - timedelta(days=30)}),
    ("INDEX_OPTION_BUYING", datetime(2026, 11, 24, 9, 46, tzinfo=timezone.utc), {"expiry": date(2026, 11, 24)}),
    ("INDEX_OPTION_SELLING", datetime(2026, 11, 23, 9, 46, tzinfo=timezone.utc), {"expiry": date(2026, 11, 24)}),
    ("STOCK_OPTION_BUYING", NOW, {"expiry": date(2026, 11, 20)}),
    ("STOCK_OPTION_SELLING", NOW, {"expiry": date(2026, 11, 25)}),
])
def test_time_exit(tt, now, pos_kw):
    p = positions(pos_kw.get("expiry", FAR))[tt]
    if "entry_utc" in pos_kw:
        p = replace(p, entry_utc=pos_kw["entry_utc"])
    calls, _ = run(tt, now=now, pos=p)
    assert "TIME" in calls and "time exit" in calls["TIME"]["action"].lower() and calls["TIME"]["reason"]


@pytest.mark.parametrize("tt", TYPES)
def test_entry_invalidated(tt):
    p = positions()[tt]
    calls, _ = run(tt, spot=p.invalidation["level"] * 0.99)                   # 1% below the level (buffer 0.3%)
    assert "ENTRY_INVALIDATED" in calls and "no longer holds" in calls["ENTRY_INVALIDATED"]["action"]


def test_entry_invalidated_respects_the_buffer():
    # level 100, buffer 0.3%: 99.71 is inside the buffer, 99.69 beyond it
    assert "ENTRY_INVALIDATED" not in run("INTRADAY_STOCKS", [101.0], spot=99.71)[0]
    assert "ENTRY_INVALIDATED" in run("INTRADAY_STOCKS", [101.0], spot=99.69)[0]


@pytest.mark.parametrize("tt", TYPES)
def test_regime_flip(tt):
    calls, _ = run(tt, regime="TRENDING_DOWN")
    assert "REGIME_FLIP" in calls and "TRENDING_DOWN" in calls["REGIME_FLIP"]["reason"]


def test_unknown_regime_never_triggers():
    assert "REGIME_FLIP" not in run("SWING_STOCKS", regime="UNKNOWN")[0]


def test_selling_position_flips_on_volatile():
    assert "REGIME_FLIP" in run("INDEX_OPTION_SELLING", regime="VOLATILE")[0]
    manual = replace(positions()["SWING_STOCKS"], strategy_id=None)          # no strategy: only "against" counts
    assert "REGIME_FLIP" not in run("SWING_STOCKS", regime="VOLATILE", pos=manual)[0]
    # from a strategy allowed only in TRENDING_UP, VOLATILE is outside its regimes: a review call
    flip = run("SWING_STOCKS", regime="VOLATILE")[0]["REGIME_FLIP"]
    assert flip["action"].startswith("Review") and "outside swing_pullback_v2" in flip["reason"]


@pytest.mark.parametrize("tt", TYPES)
def test_kill_switch(tt):
    calls, _ = run(tt, kill=["NIFTY price is STALE, not LIVE"])
    assert "KILL_SWITCH" in calls and "kill switch on" in calls["KILL_SWITCH"]["action"]
    assert calls["KILL_SWITCH"]["reason"] == "NIFTY price is STALE, not LIVE"


def test_stock_spread_phase9_calls():
    p = positions(date(2026, 11, 20))["STOCK_OPTION_SELLING"]                  # 4 trading days left: forced exit
    calls, _ = run("STOCK_OPTION_SELLING", pos=p, trend="DOWN", close=1290.0)
    assert {"FORCED_EXPIRY_EXIT", "STRIKE_BREACHED", "TREND_FLIP"} <= set(calls)
    assert calls["FORCED_EXPIRY_EXIT"]["action"].startswith("Exit now: physical settlement")


def test_settlement_alert_for_a_sold_stock_leg():
    p = positions(date(2026, 11, 24))["STOCK_OPTION_SELLING"]                  # E-6, sold 1300 put with spot 1290: ITM
    c = settlement_call(p, 1290.0, NOW, S, CAL)
    assert c["kind"] == "SETTLEMENT" and c["level"] in ("WARNING", "URGENT") and c["action"].startswith("Exit the spread by")
    far = positions(FAR)["STOCK_OPTION_SELLING"]
    assert settlement_call(far, 1290.0, NOW, S, CAL) is None                   # before E-7: nothing
    assert settlement_call(positions()["INDEX_OPTION_SELLING"], 23000.0, NOW, S, CAL) is None   # index: cash-settled


# ---------------- repeats, resolution and stale prices ----------------
def _book():
    j, store = new_journal(S)
    return j, ExitCallBook(store, S)


def test_a_call_repeats_on_its_interval_until_the_position_is_closed():
    j, book = _book()
    pos = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                                 "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 950.0}, NOW)
    stop_call = [{"kind": "STOP", "action": "Exit all 10 shares now: stop reached", "reason": "price 949 <= 950",
                  "values": {}}]
    t = NOW
    due = book.apply(pos, stop_call, t)
    assert [(c["kind"], c["repeat_no"], c["key"]) for c in due] == [("STOP", 1, f"exit:{pos.position_id}:STOP:1")]
    assert book.apply(pos, stop_call, t + timedelta(minutes=5)) == []           # within the 15-minute interval
    # the price recovers above the stop: the call is not re-confirmed but STOP is sticky and repeats
    due = book.apply(pos, [], t + timedelta(minutes=15))
    assert [(c["kind"], c["repeat_no"]) for c in due] == [("STOP", 2)]
    due = book.apply(pos, [], t + timedelta(minutes=30))
    assert [(c["kind"], c["repeat_no"]) for c in due] == [("STOP", 3)]
    j.close_position(pos.position_id, {"exit_price": 949.0}, NOW, ZeroCosts())
    book.resolve_closed(pos.position_id, t + timedelta(minutes=31))
    assert book.calls(pos.position_id, active_only=True) == []
    assert len([x for x in book.log(pos.position_id) if x["kind"] == "STOP"]) == 3


def test_kill_switch_call_ends_when_the_switch_is_off():
    j, book = _book()
    pos = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                                 "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 950.0}, NOW)
    ks = [{"kind": "KILL_SWITCH", "action": "Review now", "reason": "NIFTY STALE", "values": {}}]
    book.apply(pos, ks, NOW)
    book.apply(pos, [], NOW + timedelta(minutes=5))
    assert book.calls(pos.position_id, active_only=True) == []


def test_breakeven_call_stays_until_my_stop_is_moved():
    j, book = _book()
    pos = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                                 "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 950.0}, NOW)
    calls, plan = evaluate(pos, ExitInputs(NOW, [1050.0], spot=1050.0, regime="TRENDING_UP"), S, CAL, ZeroCosts())
    be = [c for c in calls if c["kind"] == "BREAKEVEN"]
    book.apply(pos, be, NOW)
    book.apply(pos, [], NOW + timedelta(minutes=5))
    assert [c["kind"] for c in book.calls(pos.position_id, active_only=True)] == ["BREAKEVEN"]
    moved = j.set_stop(pos.position_id, {"stop": 1000.0}, NOW)
    book.apply(moved, [], NOW + timedelta(minutes=6))
    assert book.calls(pos.position_id, active_only=True) == []


def test_stale_price_never_triggers_and_never_silences():
    j, book = _book()
    pos = j.add_manual_position({"symbol": "INFY", "trade_type": "SWING_STOCKS", "direction": "LONG", "quantity": 10,
                                 "entry_price": 1000.0, "entry_date": "2026-11-16", "stop": 950.0}, NOW)
    # stale: no price -> no price call, the plan records since when
    calls, plan = evaluate(pos, ExitInputs(NOW, None, "INFY price STALE", spot=None, regime="TRENDING_UP"), S, CAL)
    assert calls == [] and plan["stale_since_utc"] == NOW.isoformat()
    # a STOP raised on a live price keeps repeating while the price is stale
    stop = [{"kind": "STOP", "action": "Exit", "reason": "stop", "values": {}}]
    book.apply(pos, stop, NOW)
    due = book.apply(pos, [], NOW + timedelta(minutes=15))                     # stale evaluation: no calls computed
    assert [c["kind"] for c in due] == ["STOP"]
    # the time rule still runs on a stale price
    old = replace(pos, entry_utc=NOW - timedelta(days=40), strategy_id="swing_pullback_v2")
    calls, _ = evaluate(old, ExitInputs(NOW, None, "stale", spot=None, regime="TRENDING_UP"), S, CAL)
    assert [c["kind"] for c in calls] == ["TIME"]


def test_evaluate_positions_stores_plan_and_settlement_level():
    j, book = _book()
    pos = j.add_manual_position({"symbol": "RELIANCE", "kind": "stock", "entry_date": "2026-11-16", "margin_inr": 90000,
                                 "legs": [{"action": "SELL", "right": "PE", "strike": 1300, "expiry": "2026-11-24",
                                           "lots": 1, "lot_size": 500, "price": 12.0},
                                          {"action": "BUY", "right": "PE", "strike": 1260, "expiry": "2026-11-24",
                                           "lots": 1, "lot_size": 500, "price": 4.0}]}, NOW)
    due = evaluate_positions(j, book, S, CAL, NOW,
                             lambda p: ExitInputs(NOW, [30.0, 6.0], spot=1290.0, regime="TRENDING_UP"), ZeroCosts())
    kinds = {c["kind"] for c in due}
    assert "SETTLEMENT" in kinds and "STOP" in kinds                     # 24 >= 2 x 8 credit; ITM at E-6
    assert j.position(pos.position_id).plan["settlement_level"] in ("WARNING", "URGENT")
    assert all(c["key"].startswith(f"exit:{pos.position_id}:") for c in due)
