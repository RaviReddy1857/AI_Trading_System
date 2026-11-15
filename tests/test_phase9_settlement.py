"""Phase 9 M3: settlement readiness for sold stock option legs (tasks P9.29-P9.31; the config is P9.32,
tests/test_settlement_config.py). Alerts and estimates only: nothing here closes or changes a position."""
import ast
import dataclasses
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

import app.options.settlement as sm
from functools import partial

from app.calendar import MarketCalendar
from app.config import DeliveryMargin, Settlement, load_settings
from app.options.settlement import (CALENDAR_COVERED, MARGIN_UNKNOWN, NOT_CONFIRMED, NOTICE, PRICE_UNKNOWN, URGENT, WARNING,
                                    HeldLeg, HeldPosition, day_count, delivery_estimate, delivery_margin, escalate,
                                    leg_level, moneyness, rules_view, uncovered_years)
from app.quality import IST

# A holiday calendar that covers 2026 and has no holiday near the test expiry: in the tests above the
# calendar section, trading days are therefore the weekdays. The calendar tests below use their own.
CAL = MarketCalendar({date(2026, 1, 26): "Republic Day"})
cutoffs, days_left, exit_deadline, overdue, settlement_alert = (
    partial(f, calendar=CAL) for f in (sm.cutoffs, sm.days_left, sm.exit_deadline, sm.overdue, sm.settlement_alert))

SOS = load_settings().stock_option_selling                 # the shipped rules: E-7, URGENT from E-5, 5% of spot
ST = SOS.settlement
EXP = date(2026, 12, 29)                                   # a Tuesday; no holidays in these tests
CAP = 1000000.0
LOT = 250


def day(n):
    """The session n trading days before expiry (weekdays)."""
    d = EXP
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def at(n, hh=10, mm=0):
    return datetime.combine(day(n), time(hh, mm), IST)


def put_spread(lots=2, short=1000.0, long_=980.0, expiry=EXP):
    return HeldPosition("RELIANCE", expiry, (HeldLeg("SELL", "PE", short, lots, LOT), HeldLeg("BUY", "PE", long_, lots, LOT)),
                        "from a card")


def call_spread(lots=2, short=1100.0, long_=1120.0):
    return HeldPosition("RELIANCE", EXP, (HeldLeg("SELL", "CE", short, lots, LOT), HeldLeg("BUY", "CE", long_, lots, LOT)))


NAKED_PUT = HeldPosition("RELIANCE", EXP, (HeldLeg("SELL", "PE", 1000.0, 1, LOT),), "UNHEDGED")
SOLD = put_spread().shorts[0]


def with_settlement(**over):
    return SOS.model_copy(update={"settlement": Settlement(**{**ST.model_dump(), **over})})


# ---------------- P9.29: escalating alerts ----------------
def test_days_left_are_trading_days_and_moneyness_by_hand():
    assert [days_left(day(n), EXP) for n in (7, 5, 1, 0)] == [7, 5, 1, 0]
    assert days_left(EXP - timedelta(days=1), EXP) == 1 and days_left(date(2026, 12, 26), EXP) == 2      # a Saturday
    assert days_left(EXP, None) is None and days_left(EXP + timedelta(days=1), EXP) == -1
    # sold 1000 put, spot 980: in the money by 20 a share = 2.04% of spot; 2 lots x 250 = 500 shares -> Rs 10,000
    m = moneyness(SOLD, 980.0, 5.0)
    assert (m["state"], m["distance_pct"], m["itm_inr_per_share"], m["itm_inr"]) == ("IN_THE_MONEY", 2.04, 20.0, 10000.0)
    # spot 1040: out of the money by 40 = 3.85% of spot, within 5%: near the money; spot 1060: 5.66%: out
    assert moneyness(SOLD, 1040.0, 5.0)["state"] == "NEAR_THE_MONEY" and moneyness(SOLD, 1040.0, 5.0)["itm_inr"] == 0.0
    assert moneyness(SOLD, 1060.0, 5.0)["state"] == "OUT_OF_THE_MONEY"
    assert moneyness(SOLD, None, 5.0)["state"] == PRICE_UNKNOWN and moneyness(SOLD, 0.0, 5.0)["state"] == PRICE_UNKNOWN
    call = call_spread().shorts[0]
    assert moneyness(call, 1120.0, 5.0)["state"] == "IN_THE_MONEY" and moneyness(call, 1050.0, 5.0)["state"] == "NEAR_THE_MONEY"


@pytest.mark.parametrize("left, spot, want", [
    (8, 980.0, None),            # before E-7: nothing yet, even in the money
    (7, 1040.0, NOTICE),         # near the money, E-7
    (6, 1040.0, NOTICE),
    (7, 980.0, WARNING),         # in the money, E-7
    (6, 980.0, WARNING),
    (5, 1040.0, URGENT),         # from E-5: near or in the money
    (5, 980.0, URGENT),
    (0, 999.0, URGENT),
    (7, 1060.0, None),           # far out of the money: never
    (5, 1060.0, None),
    (0, 1060.0, None),
    (7, None, URGENT),           # price unknown: URGENT, never silence
    (2, None, URGENT),
    (None, 1040.0, URGENT),      # expiry unknown
])
def test_ACCEPTANCE_each_alert_level_fires_from_seven_trading_days_and_never_for_a_far_otm_leg(left, spot, want):
    level, why = leg_level(SOLD, spot, left, ST)
    assert level == want and why
    if want == URGENT and (spot is None or left is None):
        assert PRICE_UNKNOWN in why


def test_ACCEPTANCE_the_alert_names_the_leg_the_numbers_the_deadline_and_the_action():
    a = settlement_alert(put_spread(), 980.0, at(7), SOS, CAP)
    assert a["level"] == WARNING and a["trading_days_left"] == 7 and a["underlying"] == "RELIANCE"
    leg = a["legs"][0]
    assert (leg["leg"], leg["strike"], leg["spot"], leg["distance_pct"], leg["itm_inr"]) == ("sold 1000 PE", 1000.0, 980.0, 2.04, 10000.0)
    # the deadline: the close of the forced-exit day (E-5), stricter than every ICICI time
    assert a["deadline"] == datetime.combine(day(5), time(15, 30), IST).isoformat()
    assert a["action"] == f"Exit the spread by {day(5):%d-%b-%Y} 15:30" and a["overdue"] is None
    assert "never closes or changes" in a["note"] and a["estimate"]["position"]["legs"]
    # a far out-of-the-money leg: no alert at all before the deadline; a closed position: none ever
    assert settlement_alert(put_spread(), 1100.0, at(7), SOS, CAP) is None
    assert settlement_alert(put_spread(), 980.0, at(7), SOS, CAP, closed=True) is None
    # a position without a sold leg has nothing to settle against
    longs = HeldPosition("RELIANCE", EXP, (HeldLeg("BUY", "PE", 1000.0, 1, LOT),))
    assert settlement_alert(longs, 980.0, at(3), SOS, CAP) is None
    # manual and UNHEDGED positions get the same alerts
    n = settlement_alert(NAKED_PUT, 1040.0, at(6), SOS, CAP)
    assert n["level"] == NOTICE and n["label"] == "UNHEDGED" and n["action"].startswith("Exit the position by")


def test_ACCEPTANCE_the_level_only_rises_while_open_and_an_unknown_price_is_urgent():
    assert escalate(None, NOTICE) == NOTICE and escalate(WARNING, NOTICE) == WARNING and escalate(URGENT, None) == URGENT
    assert escalate(None, None) is None
    pos, level, seen = put_spread(), None, []
    # E-7 in the money, E-6 back out of the money (far), E-5 near the money, E-4 far again
    for n, spot in ((7, 980.0), (6, 1100.0), (5, 1040.0), (4, 1100.0)):
        a = settlement_alert(pos, spot, at(n), SOS, CAP, previous_level=level)
        level = a["level"]
        seen.append(level)
    assert seen == [WARNING, WARNING, URGENT, URGENT]                    # never back down
    a = settlement_alert(pos, None, at(7), SOS, CAP)
    assert a["level"] == URGENT and PRICE_UNKNOWN in a["legs"][0]["why"] and a["legs"][0]["spot"] is None
    unknown_expiry = dataclasses.replace(pos, expiry=None)
    a = settlement_alert(unknown_expiry, 1100.0, at(7), SOS, CAP)
    assert a["level"] == URGENT and a["deadline"] is None and a["overdue"]["colour"] == "red"
    assert "expiry is not known" in a["action"]
    # past an ICICI cut-off a near-the-money leg is URGENT even where the day count alone would not say so
    late_urgent = with_settlement(urgent_from_trading_days=5, alert_from_trading_days=7)
    assert leg_level(SOLD, 1040.0, 6, late_urgent.settlement, past_cutoff=True)[0] == URGENT
    assert leg_level(SOLD, 1100.0, 6, late_urgent.settlement, past_cutoff=True)[0] is None


# ---------------- P9.30: delivery and margin estimate ----------------
def test_ACCEPTANCE_delivery_estimate_of_a_short_put_matches_a_hand_calculation():
    """Sold 1000 put, bought 980 put, 2 lots of 250 = 500 shares, capital Rs 10,00,000.
    If assigned: buy 500 shares at 1000 = Rs 5,00,000 (50% of capital); one lot: 250 shares, Rs 2,50,000.
    Spot 990: the sold put settles (in the money), the bought 980 put does not: 500 shares to buy,
    Rs 5,00,000, worth 500 x 990 = Rs 4,95,000 at the last price.
    Spot 970: both settle: bought at 1000, sold at 980: no shares left, Rs 20 x 500 = Rs 10,000 paid."""
    pos = put_spread()
    e = delivery_estimate(pos, 990.0, 3, SOS, CAP)
    leg = e["position"]["legs"][0]
    assert leg["if_assigned"] == {"shares_to_buy": 500, "cash_needed_inr": 500000.0, "cash_pct_of_capital": 50.0}
    assert leg["at_last_price"] == {"settles": True, "shares_to_buy": 500, "netted_with_bought_leg_shares": 0,
                                    "net_cash_paid_on_netted_shares_inr": 0.0, "delivery_value_inr": 500000.0,
                                    "delivery_value_pct_of_capital": 50.0, "shares_worth_at_spot_inr": 495000.0}
    one = e["one_lot"]["legs"][0]
    assert one["if_assigned"]["shares_to_buy"] == 250 and one["if_assigned"]["cash_needed_inr"] == 250000.0
    assert one["at_last_price"]["delivery_value_inr"] == 250000.0
    both = delivery_estimate(pos, 970.0, 3, SOS, CAP)["position"]["legs"][0]["at_last_price"]
    assert (both["shares_to_buy"], both["netted_with_bought_leg_shares"], both["net_cash_paid_on_netted_shares_inr"],
            both["delivery_value_inr"]) == (0, 500, 10000.0, 0.0)
    assert delivery_estimate(pos, 970.0, 3, SOS, CAP)["one_lot"]["legs"][0]["at_last_price"]["net_cash_paid_on_netted_shares_inr"] == 5000.0
    # out of the money at the last price: no delivery; price unknown: unknown, never "no delivery"
    assert delivery_estimate(pos, 1010.0, 3, SOS, CAP)["position"]["legs"][0]["at_last_price"]["settles"] is False
    unknown = delivery_estimate(pos, None, 3, SOS, CAP)["position"]["legs"][0]
    assert unknown["at_last_price"] is None and unknown["if_assigned"]["cash_needed_inr"] == 500000.0
    # an UNHEDGED short put: nothing to net with
    naked = delivery_estimate(NAKED_PUT, 970.0, 3, SOS, CAP)["position"]["legs"][0]["at_last_price"]
    assert naked["shares_to_buy"] == 250 and naked["delivery_value_inr"] == 250000.0


def test_ACCEPTANCE_delivery_estimate_of_a_short_call_matches_a_hand_calculation():
    """Sold 1100 call, bought 1120 call, 500 shares. If assigned: deliver 500 shares for Rs 5,50,000.
    Spot 1110: only the sold call settles: 500 shares to deliver; buying them at the last price costs
    500 x 1110 = Rs 5,55,000 (55.5% of capital) if they are not held.
    Spot 1130: both settle: bought at 1120, delivered at 1100: no shares, Rs 20 x 500 = Rs 10,000 paid."""
    pos = call_spread()
    leg = delivery_estimate(pos, 1110.0, 3, SOS, CAP)["position"]["legs"][0]
    assert leg["if_assigned"] == {"shares_to_deliver": 500, "cash_received_inr": 550000.0, "cash_pct_of_capital": 55.0}
    assert leg["at_last_price"] == {"settles": True, "shares_to_deliver": 500, "netted_with_bought_leg_shares": 0,
                                    "net_cash_paid_on_netted_shares_inr": 0.0, "delivery_value_inr": 550000.0,
                                    "delivery_value_pct_of_capital": 55.0,
                                    "cost_to_buy_shares_at_spot_if_not_held_inr": 555000.0, "cost_pct_of_capital": 55.5}
    both = delivery_estimate(pos, 1130.0, 3, SOS, CAP)["position"]["legs"][0]["at_last_price"]
    assert (both["shares_to_deliver"], both["netted_with_bought_leg_shares"], both["net_cash_paid_on_netted_shares_inr"],
            both["cost_to_buy_shares_at_spot_if_not_held_inr"]) == (0, 500, 10000.0, 0.0)


def test_ACCEPTANCE_expiry_week_margin_follows_the_schedule_and_an_empty_rule_is_margin_unknown_never_zero():
    pos = put_spread()
    # ICICI's schedule: 10, 25, 45, 70, 100% of VaR + ELM + adhoc on E-4 .. E. Base Rs 80,000 for this position.
    got = {n: delivery_margin(pos, 990.0, n, SOS, base_margin_inr=80000.0, capital=CAP) for n in (6, 4, 3, 2, 1, 0)}
    assert [got[n]["pct_of_base"] for n in (4, 3, 2, 1, 0)] == [10, 25, 45, 70, 100]
    assert [got[n]["margin_inr"] for n in (4, 3, 2, 1, 0)] == [8000.0, 20000.0, 36000.0, 56000.0, 80000.0]
    assert got[2]["margin_pct_of_capital"] == 3.6 and got[2]["status"] == "CHARGED" and got[2]["confirmed"] is True
    assert got[6]["status"] == "NOT_STARTED" and "starts on E-4" in got[6]["why"]
    # the schedule's share is known but the rupee base is not entered: the amount is unknown, not zero
    m = delivery_margin(pos, 990.0, 2, SOS, capital=CAP)
    assert m["pct_of_base"] == 45 and m["margin_inr"] is None and m["status"] == MARGIN_UNKNOWN and m["colour"] == "red"
    # only for a leg in the money or within 5% of spot (CTM)
    assert delivery_margin(pos, 1100.0, 2, SOS, 80000.0, CAP)["status"] == "NOT_APPLICABLE"
    # an empty broker rule, an unknown price, an unknown expiry: "margin unknown" in red, never zero
    empty = with_settlement(broker={**ST.broker.model_dump(), "delivery_margin": DeliveryMargin().model_dump()})
    for m in (delivery_margin(pos, 990.0, 2, empty, 80000.0, CAP), delivery_margin(pos, None, 2, SOS, 80000.0, CAP),
              delivery_margin(pos, 990.0, None, SOS, 80000.0, CAP)):
        assert m["status"] == MARGIN_UNKNOWN and m["colour"] == "red" and m["margin_inr"] is None and m["pct_of_base"] is None
    a = settlement_alert(pos, 990.0, at(2), empty, CAP)
    assert a["estimate"]["margin"]["status"] == MARGIN_UNKNOWN and f"{NOT_CONFIRMED}: delivery margin" in a["not_confirmed"]


# ---------------- P9.31: the planned-exit deadline ----------------
def test_deadline_is_the_stricter_of_the_forced_exit_and_icicis_times():
    cs = cutoffs(put_spread(), SOS)
    assert [c["rule"] for c in cs] == ["forced exit", "delivery margin", "auto close"]
    assert [c["at"] for c in cs] == [datetime.combine(day(5), time(15, 30), IST), datetime.combine(day(4), time(15, 15), IST),
                                     datetime.combine(EXP, time(12, 0), IST)]            # a spread: the bought leg may go at 12:00
    assert exit_deadline(put_spread(), SOS)["rule"] == "forced exit"
    # a short-only position: ICICI's earliest stated time is 13:30; a sold call adds the 11:00 shares deadline
    assert [c["at"].time() for c in cutoffs(NAKED_PUT, SOS) if c["rule"] == "auto close"] == [time(13, 30)]
    assert [(c["rule"], c["at"].time(), c["confirmed"]) for c in cutoffs(call_spread(), SOS)][-2:] == [
        ("call shares deadline", time(11, 0), False), ("auto close", time(12, 0), False)]
    # were the forced exit only one trading day before expiry, ICICI's margin start would be the stricter deadline
    loose = SOS.model_copy(update={"forced_exit_trading_days_before_expiry": 1})
    assert exit_deadline(put_spread(), loose)["rule"] == "delivery margin"
    assert exit_deadline(dataclasses.replace(put_spread(), expiry=None), SOS) is None


def test_ACCEPTANCE_a_position_not_closed_by_its_deadline_gets_a_red_alert_until_it_is_marked_closed():
    pos = put_spread()
    assert overdue(pos, at(5, 15, 29), False, SOS) is None                # one minute before the deadline
    red = [overdue(pos, when, False, SOS) for when in (at(5, 15, 30), at(4, 9, 15), at(4, 9, 20), at(1), at(0, 14, 0))]
    assert all(r and r["kind"] == "EXIT_OVERDUE" and r["colour"] == "red" and r["repeat"] is True for r in red)
    assert "NOT marked closed" in red[0]["text"] and "mark it closed in the journal" in red[0]["text"]
    assert overdue(pos, at(1), True, SOS) is None                         # marked closed: it stops
    # whatever the moneyness: a far out-of-the-money spread left open past the deadline is still alerted, in red
    a = settlement_alert(pos, 1100.0, at(3), SOS, CAP)
    assert a is not None and a["level"] is None and a["colour"] == "red" and a["overdue"]["kind"] == "EXIT_OVERDUE"
    assert settlement_alert(pos, 1100.0, at(3), SOS, CAP, closed=True) is None
    assert settlement_alert(pos, 980.0, at(3), SOS, CAP)["level"] == URGENT


# ---------------- P9.32: unconfirmed ICICI rules are named ----------------
def test_ACCEPTANCE_an_unconfirmed_icici_rule_is_named_on_every_alert_that_uses_it():
    a = settlement_alert(put_spread(), 980.0, at(6), SOS, CAP)
    assert f"{NOT_CONFIRMED}: auto close" in a["not_confirmed"]                         # as shipped: not confirmed
    assert not any("delivery margin" in x or "settlement obligation" in x for x in a["not_confirmed"])   # confirmed ones are not
    assert any("fresh-position block" in x for x in a["not_confirmed"])
    c = settlement_alert(call_spread(), 1110.0, at(6), SOS, CAP)
    assert f"{NOT_CONFIRMED}: call shares deadline" in c["not_confirmed"]
    assert f"{NOT_CONFIRMED}: call shares deadline" not in a["not_confirmed"]           # a put spread does not use it
    # flip a flag: delivery margin unconfirmed -> named; auto close confirmed -> no longer named
    b = ST.broker.model_dump()
    b["delivery_margin"]["confirmed"], b["auto_close"]["confirmed"] = False, True
    flipped = settlement_alert(put_spread(), 980.0, at(6), with_settlement(broker=b), CAP)
    assert f"{NOT_CONFIRMED}: delivery margin" in flipped["not_confirmed"]
    assert f"{NOT_CONFIRMED}: auto close" not in flipped["not_confirmed"]
    view = rules_view(SOS)
    labels = {r["rule"]: r["label"] for r in view["rules"]}
    assert labels["auto close"] == f"{NOT_CONFIRMED}: auto close" and labels["delivery margin"] == ""
    assert labels["fresh-position block in the last days"].startswith(NOT_CONFIRMED)
    assert next(r for r in view["rules"] if r["rule"].startswith("fresh"))["value"] == "not known"


def test_ACCEPTANCE_no_code_path_closes_or_changes_a_position():
    pos = put_spread()
    before = dataclasses.asdict(pos)
    for n, spot in ((7, 980.0), (3, None), (0, 1040.0)):
        settlement_alert(pos, spot, at(n), SOS, CAP)
    assert dataclasses.asdict(pos) == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        pos.expiry = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        pos.legs[0].lots = 0
    # the module reads config and time only: no store, no feed, no broker SDK, no network, no file access
    src = Path("app/options/settlement.py").read_text(encoding="utf-8")
    imported = {(n.module or "") if isinstance(n, ast.ImportFrom) else a.name
                for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert imported <= {"__future__", "collections.abc", "dataclasses", "datetime", "config", "quality", "stock_selling"}
    calls = {n.func.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"execute", "commit", "save", "write", "write_text", "open", "post", "send"}


def test_settlement_route_and_page_are_read_only():
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        j = c.get("/api/stock-option-selling/settlement").json()
        assert j["alerts"] == [] and "never closes or changes" in j["note"]
        assert set(j["calendar"]) == {"covered", "colour", "note"} and isinstance(j["calendar"]["covered"], bool)
        assert j["alerts_from"] == {"from_trading_days": 7, "urgent_from_trading_days": 5, "near_money_pct": 5.0,
                                    "forced_exit_trading_days": 5}
        assert any(r["label"] == f"{NOT_CONFIRMED}: auto close" for r in j["rules"])
        assert "Settlement readiness" in c.get("/stock-option-selling").text
        assert c.post("/api/stock-option-selling/settlement", json={}).status_code in (403, 405)



# ---------------- trading days by the holiday calendar ----------------
XMAS = MarketCalendar({date(2026, 1, 26): "Republic Day", date(2026, 12, 25): "Christmas"})


def on(d, hh=10, mm=0):
    return datetime.combine(d, time(hh, mm), IST)


def test_ACCEPTANCE_trading_days_use_the_holiday_calendar_not_weekdays():
    """Expiry Tuesday 29 December 2026 with Friday 25 December a holiday. Sessions before expiry: 28 (E-1),
    24 (E-2), 23 (E-3), 22 (E-4), 21 (E-5), 18 (E-6), 17 (E-7). By weekdays alone E-5 would be the 22nd."""
    left = lambda d: sm.days_left(d, EXP, XMAS)
    assert [left(date(2026, 12, n)) for n in (16, 17, 18, 21, 22, 23, 24, 28, 29)] == [8, 7, 6, 5, 4, 3, 2, 1, 0]
    assert days_left(date(2026, 12, 21), EXP) == 6                        # the same day without the holiday: E-6
    dc = day_count(EXP, XMAS)
    assert dc.covered and dc.note == CALENDAR_COVERED
    assert [dc.session(n) for n in (0, 1, 2, 5, 7)] == [EXP, date(2026, 12, 28), date(2026, 12, 24), date(2026, 12, 21),
                                                       date(2026, 12, 17)]
    pos = put_spread()
    # Monday 21 December is E-5: an in-the-money leg is URGENT (WARNING if the holiday were not counted)
    a = sm.settlement_alert(pos, 980.0, on(date(2026, 12, 21)), SOS, CAP, calendar=XMAS)
    assert a["level"] == URGENT and a["trading_days_left"] == 5 and a["calendar"] == {"covered": True, "note": CALENDAR_COVERED,
                                                                                      "colour": None}
    assert settlement_alert(pos, 980.0, on(date(2026, 12, 21)), SOS, CAP)["level"] == WARNING
    # alerts start on E-7 = Thursday 17 December; nothing on the 16th
    assert sm.settlement_alert(pos, 980.0, on(date(2026, 12, 17)), SOS, CAP, calendar=XMAS)["level"] == WARNING
    assert sm.settlement_alert(pos, 980.0, on(date(2026, 12, 16)), SOS, CAP, calendar=XMAS) is None
    # the deadline and ICICI's margin start move with the holiday: forced exit at the close of the 21st, margin on the 22nd
    cs = {c["rule"]: c["at"] for c in sm.cutoffs(pos, SOS, XMAS)}
    assert cs["forced exit"] == on(date(2026, 12, 21), 15, 30) and cs["delivery margin"] == on(date(2026, 12, 22), 15, 15)
    assert a["action"] == "Exit the spread by 21-Dec-2026 15:30"
    assert sm.overdue(pos, on(date(2026, 12, 21), 15, 30), False, SOS, XMAS)["colour"] == "red"
    assert sm.overdue(pos, on(date(2026, 12, 21), 15, 29), False, SOS, XMAS) is None
    # the expiry-week margin follows the calendar's day count: 22 December is E-4 (10%), 24 December E-2 (45%)
    m = lambda d: sm.settlement_alert(pos, 990.0, on(d), SOS, CAP, calendar=XMAS, base_margin_inr=80000.0)["estimate"]["margin"]
    assert m(date(2026, 12, 22))["pct_of_base"] == 10 and m(date(2026, 12, 24))["margin_inr"] == 36000.0


@pytest.mark.parametrize("calendar, names", [
    (None, "no holiday calendar"),                                                    # no calendar at all
    (MarketCalendar(), "holidays.csv has no holidays for 2026"),                      # an empty file
    (MarketCalendar({date(2025, 12, 25): "Christmas"}), "holidays.csv has no holidays for 2026"),   # last year's list only
])
def test_ACCEPTANCE_a_calendar_that_does_not_cover_the_expiry_month_counts_one_day_earlier_and_says_so(calendar, names):
    """Not covered: trading days = weekdays LESS ONE. A holiday that is not in the file (25 December here)
    then cannot make an alert or the deadline late."""
    dc = day_count(EXP, calendar)
    assert dc.covered is False and names in dc.note and "one session earlier" in dc.note and "data/holidays.csv" in dc.note
    left = lambda d: sm.days_left(d, EXP, calendar)
    # weekdays to expiry on 16, 17, 18, 21, 22, 28, 29 December: 9, 8, 7, 6, 5, 1, 0
    assert [left(date(2026, 12, n)) for n in (16, 17, 18, 21, 22, 28, 29)] == [8, 7, 6, 5, 4, 0, 0]
    pos = put_spread()
    alert = lambda d, spot=980.0: sm.settlement_alert(pos, spot, on(d), SOS, CAP, calendar=calendar)
    assert alert(date(2026, 12, 15)) is None                                          # weekday E-10: counted E-9, still nothing
    assert alert(date(2026, 12, 17))["level"] == WARNING                              # weekday E-8: alerts start a day early
    assert alert(date(2026, 12, 21))["level"] == URGENT                               # weekday E-6: URGENT a day early
    a = alert(date(2026, 12, 21))
    # the card says so, in red
    assert a["calendar"]["covered"] is False and a["calendar"]["colour"] == "red" and names in a["calendar"]["note"]
    # the deadline is one session earlier too: the close of Monday 21 December (weekday E-6), not Tuesday 22
    assert a["deadline"] == on(date(2026, 12, 21), 15, 30).isoformat()
    assert sm.overdue(pos, on(date(2026, 12, 21), 15, 30), False, SOS, calendar)["kind"] == "EXIT_OVERDUE"
    # never later than the count with the true holiday: on every day it is at or below the covered count
    for n in range(10, 30):
        d = date(2026, 12, n)
        if d.weekday() < 5 and d != date(2026, 12, 25):
            assert left(d) <= sm.days_left(d, EXP, XMAS)


def test_coverage_is_judged_on_the_expiry_month_and_shown_on_the_page_card():
    only_2026 = MarketCalendar({date(2026, 1, 26): "Republic Day"})
    assert uncovered_years(only_2026, date(2026, 11, 28), EXP) == []
    assert uncovered_years(only_2026, date(2026, 12, 20), date(2027, 1, 20)) == [2027]
    assert uncovered_years(None, date(2026, 12, 20), date(2027, 1, 20)) == [2026, 2027]
    assert day_count(EXP, only_2026).covered is True
    # an expiry early in January: the 31 days before it reach into the old year, so both years are needed
    jan = date(2027, 1, 5)
    assert day_count(jan, only_2026).covered is False and "2027" in day_count(jan, only_2026).note
    assert day_count(jan, MarketCalendar({date(2027, 1, 26): "Republic Day"})).covered is False       # 2026 missing
    both = MarketCalendar({date(2026, 1, 26): "x", date(2027, 1, 26): "y"})
    assert day_count(jan, both).covered is True
    # an unknown expiry: no count at all (the alert is URGENT "price unknown")
    assert day_count(None, both).left(EXP) is None and day_count(None, both).covered is False
    # after expiry the count is negative, covered or not
    assert sm.days_left(EXP + timedelta(days=1), EXP, None) == -1
    # the page card: covered or, in red, what is missing
    ok = rules_view(SOS, only_2026, date(2026, 6, 1))["calendar"]
    assert ok == {"covered": True, "colour": None, "note": CALENDAR_COVERED}
    late = rules_view(SOS, only_2026, date(2026, 11, 15))["calendar"]                 # 70 days ahead reaches January 2027
    assert late["covered"] is False and late["colour"] == "red" and "2027" in late["note"] and "one session earlier" in late["note"]
    assert rules_view(SOS)["calendar"] is None                                        # no date given: nothing claimed
    from app.page import SHORTLIST_HTML
    assert "sr.calendar.note" in SHORTLIST_HTML and "CALENDAR:" in SHORTLIST_HTML
