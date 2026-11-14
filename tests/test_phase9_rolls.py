"""Phase 9: campaign tracking, roll rules, minimum net credit, stuck-position alert, no recovery sizing,
and the three exit policies compared on the same entries in the backtest."""
from datetime import date, datetime, timedelta

import pytest

from app.config import StockOptionSelling as SosParams
from app.models import unhedged_short_legs
from app.options.base import ExpiryInfo, OptionQuote
from app.options.campaign import NO_RECOVERY, CampaignPosition, after_roll, campaign_summary, campaigns_from_trades
from app.options.option_history import OptionHistory
from app.options.stock_backtest import policy_comparison, policy_runs
from app.options.stock_profile import ClosedTrade
from app.options.stock_selling import (STUCK_ACTION, SpreadPosition, build_stock_selling, position_alerts, roll_choices,
                                       stuck_position)
from app.quality import IST
from app.store import Store
from tests.test_phase3 import settings
from tests.test_phase9_core import (EXP, EXP_NEXT, LOT, NOW, chain, ctx_for, engine_for, ist_date, legs_of, stock_bars,
                                    stock_world)
from tests.test_phase9_rules import snap

ROLL = SosParams().roll


@pytest.fixture(scope="module")
def s():
    return settings()


def two_months(s, bars, **kw):
    spot = bars[-1].close
    both = {EXP: chain(spot), EXP_NEXT: chain(spot, expiry=EXP_NEXT)}
    return ctx_for(s, bars, both, expiries=(ExpiryInfo(EXP, "monthly", LOT), ExpiryInfo(EXP_NEXT, "monthly", LOT)), **kw)


HELD = SpreadPosition("RELIANCE", "PE", 1100.0, 1080.0, EXP, 4.0, LOT)       # nearer the money than the rules now give


# ---------------- 1. campaign tracking ----------------
def test_campaign_summary_by_hand_and_recovery_not_possible():
    """Original: sold 6.00, hedge 2.00 (credit 4.00) on 250 shares, closed at 11.00 / 1.00, charges 150:
    realised (4 - 10) x 250 - 150 = -1,650. Roll: sold 5.00, hedge 1.50, charges 70 so far, best case 760.
    Hedge cost (2.00 - 1.00) x 250 + 1.50 x 250 = 625. Charges 220. Best case -1,650 + 760 = -890."""
    first = CampaignPosition(0, "PE", 1100, 1080, EXP, 250, 6.0, 2.0, 150.0, None, 11.0, 1.0)
    roll = CampaignPosition(1, "PE", 1060, 1040, EXP_NEXT, 250, 5.0, 1.5, 70.0, 760.0)
    c = campaign_summary("sig-1", "RELIANCE", [roll, first])
    assert (c["realised_pnl_inr"], c["hedge_cost_inr"], c["charges_inr"]) == (-1650.0, 625.0, 220.0)
    assert c["best_possible_outcome_inr"] == -890.0 and c["recovery_possible"] is False
    assert c["message"] == NO_RECOVERY == "recovery not possible from this position"
    assert (c["rolls"], c["positions"], c["open"], c["side"]) == (1, 2, True, "PE")
    assert [x["roll_no"] for x in c["legs"]] == [0, 1] and c["legs"][0]["realised_pnl_inr"] == -1650.0
    assert c["campaign_id"] == "sig-1"                                # the roll stays linked to its original trade
    # a roll with a bigger best case: recovery is still possible, no message
    ok = campaign_summary("sig-1", "RELIANCE", [first, CampaignPosition(1, "PE", 1060, 1040, EXP_NEXT, 250, 9.0, 1.5, 70.0, 1700.0)])
    assert ok["best_possible_outcome_inr"] == 50.0 and ok["recovery_possible"] is True and ok["message"] == ""
    # everything closed: the best case is the result
    done = campaign_summary("sig-1", "RELIANCE", [first])
    assert done["open"] is False and done["best_possible_outcome_inr"] == -1650.0 and done["message"] == ""
    # an open position whose maximum profit is not known: unknown, never zero
    unknown = campaign_summary("x", "TCS", [CampaignPosition(0, "CE", 100, 110, EXP, 100, 3.0, 1.0, 50.0)])
    assert unknown["best_possible_outcome_inr"] is None and unknown["recovery_possible"] is None and "unknown" in unknown["message"]
    assert after_roll(-1000.0, -650.0, 760.0) == {"realised_pnl_inr": -1650.0, "best_possible_outcome_inr": -890.0,
                                                  "recovery_possible": False, "message": NO_RECOVERY}
    assert after_roll(0.0, -500.0, 760.0)["message"] == ""


def test_campaigns_are_stored_and_served_read_only():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    store = Store(":memory:")
    first = CampaignPosition(0, "PE", 1100, 1080, EXP, 250, 6.0, 2.0, 150.0, None, 11.0, 1.0)
    roll = CampaignPosition(1, "PE", 1060, 1040, EXP_NEXT, 250, 5.0, 1.5, 70.0, 760.0)
    store.save_campaign(campaign_summary("sig-1", "RELIANCE", [first]), NOW)
    store.save_campaign(campaign_summary("sig-1", "RELIANCE", [first, roll]), NOW + timedelta(hours=1))     # the roll updates it
    store.save_campaign(campaign_summary("sig-2", "TCS", [first]), NOW)
    got = store.campaigns()
    assert [c["campaign_id"] for c in got] == ["sig-1", "sig-2"] and got[0]["rolls"] == 1 and got[0]["message"] == NO_RECOVERY
    assert [c["campaign_id"] for c in store.campaigns("TCS")] == ["sig-2"]
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        assert c.get("/api/stock-option-selling/campaigns").json()["campaigns"] == []
        app.state.store.save_campaign(campaign_summary("sig-1", "RELIANCE", [first, roll]), NOW)
        j = c.get("/api/stock-option-selling/campaigns").json()["campaigns"][0]
        assert j["best_possible_outcome_inr"] == -890.0 and j["hedge_cost_inr"] == 625.0 and j["message"] == NO_RECOVERY
        assert "Campaigns" in c.get("/stock-option-selling").text


# ---------------- 2. roll rules ----------------
def test_ACCEPTANCE_a_roll_is_suggested_only_when_every_rule_holds(s):
    strat = build_stock_selling(s)
    bars = stock_bars()
    out = roll_choices(strat, two_months(s, bars), HELD, 7.0)
    assert out["action"] == "ROLL" and out["roll_note"] == ""
    roll = out["roll"]
    width = abs(roll["legs"][0]["strike"] - roll["legs"][1]["strike"])
    assert roll["legs"][0]["strike"] < HELD.short_strike                           # (b) lower for a put: the way the market went
    assert roll["new_net_credit_after_costs_inr"] > 0                              # (a) after the hedge and all charges
    assert roll["new_net_credit_after_costs_inr"] >= ROLL.min_net_credit_pct_of_width / 100 * width * LOT
    assert roll["roll_no"] == 1 and unhedged_short_legs(roll["legs"]) == []
    assert all(leg["lots"] == 1 for leg in roll["legs"])                           # a roll never raises the size
    after = roll["campaign_after_roll"]
    assert after["realised_pnl_inr"] == out["exit"]["realised_pnl_inr"]
    assert after["best_possible_outcome_inr"] == pytest.approx(after["realised_pnl_inr"] + roll["new_net_credit_after_costs_inr"], abs=0.02)
    # (d) the campaign has already been rolled once
    again = roll_choices(strat, two_months(s, bars), HELD, 7.0, rolls_so_far=1)
    assert again["action"] == "EXIT" and again["roll"] is None and "already been rolled 1 time" in again["roll_note"]
    never = build_stock_selling(settings(stock_option_selling={"roll": {**ROLL.model_dump(), "max_rolls": 0}}))
    assert roll_choices(never, two_months(s, bars), HELD, 7.0)["action"] == "EXIT"
    # (a) the new credit is too small a share of the width
    thin = build_stock_selling(settings(stock_option_selling={"roll": {**ROLL.model_dump(), "min_net_credit_pct_of_width": 60}}))
    out = roll_choices(thin, two_months(s, bars), HELD, 7.0)
    assert out["action"] == "EXIT" and "under 60% of the width" in out["roll_note"]
    # (b) the rules give a strike that is not below the one held: the same or a worse strike is never a roll
    low = SpreadPosition("RELIANCE", "PE", 1000.0, 980.0, EXP, 4.0, LOT)
    out = roll_choices(strat, two_months(s, bars), low, 7.0)
    assert out["action"] == "EXIT" and "not below the current 1000" in out["roll_note"]
    same_k = legs_of(strat.evaluate(ctx_for(s, bars))[0])[0].strike
    same = SpreadPosition("RELIANCE", "PE", same_k, same_k - 20, EXP, 4.0, LOT)
    assert roll_choices(strat, ctx_for(s, bars), same, 7.0)["action"] == "EXIT"      # only this month: the same strike
    nxt = roll_choices(strat, two_months(s, bars), same, 7.0)                         # next month the rules sit lower
    assert nxt["action"] == "ROLL" and nxt["roll"]["legs"][0]["strike"] < same_k and nxt["roll"]["expiry"] == EXP_NEXT.isoformat()
    # (c) the stock does not pass the falling-stock block
    down = stock_bars(start=1200, drift=-1.0)
    out = roll_choices(strat, two_months(s, down), HELD, 7.0)
    assert out["action"] == "EXIT" and out["roll_note"].startswith("exit: DOWNTREND_BLOCK")
    # the entry trend and IV-rank rules do not gate a roll, the roll rules do
    assert roll_choices(strat, two_months(s, bars, iv_rank=10.0), HELD, 7.0)["action"] == "ROLL"


def test_a_roll_that_cannot_recover_the_campaign_says_so(s):
    strat = build_stock_selling(s)
    out = roll_choices(strat, two_months(s, stock_bars()), HELD, 7.0, realised_so_far_inr=-20000.0)
    after = out["roll"]["campaign_after_roll"]
    assert out["action"] == "ROLL" and after["recovery_possible"] is False
    assert after["message"] == "recovery not possible from this position" and after["best_possible_outcome_inr"] < 0
    assert out["exit"]["campaign_pnl_inr"] == pytest.approx(-20000.0 + out["exit"]["realised_pnl_inr"], abs=0.02)


def test_a_roll_may_use_the_next_month_but_no_expiry_beyond_the_roll_limit(s):
    strat = build_stock_selling(s)
    bars = stock_bars()
    spot = bars[-1].close
    near, far = date(2026, 11, 24), date(2026, 12, 29)                 # 11 and 46 days away
    held = SpreadPosition("RELIANCE", "PE", 1100.0, 1080.0, near, 4.0, LOT)
    c = ctx_for(s, bars, {near: chain(spot, expiry=near), far: chain(spot, expiry=far)},
                expiries=(ExpiryInfo(near, "monthly", LOT), ExpiryInfo(far, "monthly", LOT)))
    out = roll_choices(strat, c, held, 7.0)
    assert out["action"] == "ROLL" and out["roll"]["expiry"] == far.isoformat()        # 46 days: fine for a roll
    assert strat.evaluate(ctx_for(s, bars, {far: chain(spot, expiry=far)})) == []      # ... but not for a new entry
    too_far = date(2027, 1, 26)                                        # 74 days: beyond roll.max_days_to_expiry
    c2 = ctx_for(s, bars, {near: chain(spot, expiry=near), too_far: chain(spot, expiry=too_far)},
                 expiries=(ExpiryInfo(near, "monthly", LOT), ExpiryInfo(too_far, "monthly", LOT)))
    out = roll_choices(strat, c2, held, 7.0)
    assert out["action"] == "EXIT" and "no roll passes the rules" in out["roll_note"]


# ---------------- 3. net credit after hedge and charges ----------------
def test_ACCEPTANCE_every_signal_shows_net_credit_after_hedge_and_charges_and_a_small_one_is_skipped(s):
    strat = build_stock_selling(s)
    bars = stock_bars()
    d = strat.evaluate(ctx_for(s, bars))[0].details
    sold, hedge = d["legs"][0]["price"], d["legs"][1]["price"]
    assert d["sold_premium_inr"] == pytest.approx(sold * LOT, abs=0.01) and d["hedge_cost_inr"] == pytest.approx(hedge * LOT, abs=0.01)
    assert d["net_credit_after_costs_inr"] == pytest.approx((sold - hedge) * LOT - d["costs_inr"], abs=0.02)
    assert d["net_credit_after_costs_inr"] == d["max_profit_inr"]
    assert d["net_credit_after_costs_per_share"] == pytest.approx(d["net_credit_after_costs_inr"] / LOT, abs=0.01)
    assert d["net_credit_pct_of_max_loss"] == pytest.approx(d["net_credit_after_costs_inr"] / d["max_loss_inr"] * 100, abs=0.01)
    assert d["net_credit_pct_of_max_loss"] >= s.stock_option_selling.min_net_credit_pct_of_max_loss
    strict = build_stock_selling(settings(stock_option_selling={"min_net_credit_pct_of_max_loss": 60}))
    assert strict.evaluate(ctx_for(s, bars)) == []
    assert any(r.startswith("CREDIT_TOO_SMALL") and "under 60%" in r for r in strict.last_no_trade)
    off = build_stock_selling(settings(stock_option_selling={"min_net_credit_pct_of_max_loss": 0}))
    assert off.evaluate(ctx_for(s, bars))
    # the no-filter baseline does not apply it unless the comparison asks for it
    base = dict(no_filter=True, baseline_right="PE")
    assert strict.evaluate(ctx_for(s, bars, **base))
    strict.baseline_rules = frozenset({"min_credit"})
    assert strict.evaluate(ctx_for(s, bars, **base)) == []


# ---------------- 4. stuck position ----------------
def test_ACCEPTANCE_stuck_position_alert_is_exit_rolling_earns_little(s):
    pos = SpreadPosition("RELIANCE", "PE", 1100.0, 1080.0, EXP, 4.0, LOT)
    today = date(2026, 11, 13)                                           # 25 days to expiry
    # in the money by 40 with a price of 42: time value 2 / 42 = 4.8%, under 10%
    why = stuck_position(pos, 42.0, 1060.0, today, ROLL)
    assert why and "in the money" in why and "4.8%" in why
    assert stuck_position(pos, 50.0, 1060.0, today, ROLL) is None        # time value 20%
    assert stuck_position(pos, 44.4, 1060.0, today, ROLL) is not None    # 9.9%
    assert stuck_position(pos, 44.5, 1060.0, today, ROLL) is None        # 10.1%
    assert stuck_position(pos, 3.0, 1120.0, today, ROLL) is None         # out of the money
    assert stuck_position(pos, None, 1060.0, today, ROLL) is None        # unknown price: nothing is claimed
    call = SpreadPosition("RELIANCE", "CE", 1100.0, 1120.0, EXP, 4.0, LOT)
    assert stuck_position(call, 41.0, 1140.0, today, ROLL) is not None and stuck_position(call, 41.0, 1090.0, today, ROLL) is None
    # inside the pre-expiry window, whatever the price
    assert "pre-expiry window" in stuck_position(pos, 3.0, 1120.0, date(2026, 11, 28), ROLL)        # 10 days
    assert stuck_position(pos, 3.0, 1120.0, date(2026, 11, 27), ROLL) is None                        # 11 days
    # in a roll decision and in the alerts: exit, and no roll is offered
    strat = build_stock_selling(s)
    bars = stock_bars(start=930.0)                                       # the stock is near 1049: the 1100 put is deep in the money
    spot = bars[-1].close
    quotes = [q if not (q.strike == 1100.0 and q.right == "PE") else
              OptionQuote(1100.0, "PE", round(1100 - spot + 1.0, 2), round(1100 - spot + 1.4, 2), round(1100 - spot + 1.2, 2), q.oi)
              for q in chain(spot)]
    c = ctx_for(s, bars, quotes)
    out = roll_choices(strat, c, pos, 15.0)
    assert out["action"] == "EXIT" and out["roll"] is None and out["roll_note"].startswith(STUCK_ACTION)
    assert STUCK_ACTION == "exit; rolling earns little"
    alerts = position_alerts(strat, c, pos, 15.0, snap(), snap(date(2026, 11, 13)))
    assert [a["kind"] for a in alerts] == ["STUCK_POSITION"] and alerts[0]["action"] == "EXIT"
    assert alerts[0]["roll_note"].startswith("exit; rolling earns little") and "roll_signal" not in alerts[0]
    assert alerts[0]["exit"]["cost_to_close_inr"] == 15.0 * LOT


# ---------------- 5. no recovery sizing ----------------
def test_ACCEPTANCE_a_signal_after_a_losing_campaign_uses_the_normal_rules_and_size(s):
    strat = build_stock_selling(s)
    bars = stock_bars()
    fresh = strat.evaluate(ctx_for(s, bars))[0]
    lost = (ClosedTrade(date(2026, 10, 20), True, -9000.0), )            # one stop, a loss under the campaign limit
    after = strat.evaluate(ctx_for(s, bars, closed_trades=lost))[0]
    assert after.to_dict() == fresh.to_dict()                            # same strikes, same credit, same everything
    assert all(leg.lots == 1 for leg in after.legs) and after.details["lots"] == 1
    assert "never raise the size" in after.details["sizing_note"]
    big = strat.evaluate(ctx_for(s, bars, closed_trades=(ClosedTrade(date(2026, 3, 1), True, -500000.0),)))[0]
    assert big.to_dict() == fresh.to_dict()                              # even a huge old loss changes nothing


# ---------------- 6. three exit policies on the same entries ----------------
def setup_policy(world):
    cfg, store, base, _ = world
    eng = engine_for(cfg, store, base, OptionHistory(store, "sim"))
    eng.set_expiry_floor(date(2025, 3, 1))
    dev = [d for d in base.session_dates() if date(2025, 6, 2) <= d < date(2025, 10, 1)]
    return cfg, eng, dev


@pytest.fixture(scope="module")
def drop_world():
    """A rising stock that falls 80 on 10 July 2025, while a put spread is open: the spread is stopped,
    and the stock still passes the falling-stock block."""
    return stock_world(shocks={date(2025, 7, 10): -80.0}, drift=1.0, hi=1750)


@pytest.fixture(scope="module")
def slide_world():
    """Two falls (1 and 15 July 2025): the put spread is still losing at its forced exit."""
    return stock_world(shocks={date(2025, 7, 1): -60.0, date(2025, 7, 15): -40.0}, drift=1.0, hi=1750)


def originals(trades):
    return sorted((t.variant, t.signal_id, t.entry_time, t.legs[0]["strike"], t.expiry) for t in trades if t.roll_no == 0)


def test_ACCEPTANCE_the_three_exit_policies_use_exactly_the_same_entries(drop_world):
    cfg, eng, dev = setup_policy(drop_world)
    got = policy_runs(cfg, eng, "RELIANCE", dev)
    for group in ("strategy", "baseline"):
        assert set(got[group]) == {"stop", "roll_rules", "roll_always"}
        ref = originals(got[group]["stop"])
        assert ref and originals(got[group]["roll_rules"]) == ref and originals(got[group]["roll_always"]) == ref
    assert all(t.roll_no == 0 and t.exit_reason != "ROLLED" for t in got["baseline"]["stop"])


def test_roll_policy_links_the_roll_to_its_original_trade_and_obeys_the_roll_rules(drop_world):
    cfg, eng, dev = setup_policy(drop_world)
    got = policy_runs(cfg, eng, "RELIANCE", dev)["baseline"]
    stopped = [t for t in got["stop"] if t.exit_reason == "STOP" and t.variant == "baseline:PE"]
    assert len(stopped) == 1
    rolled = [t for t in got["roll_rules"] if t.exit_reason == "ROLLED"]
    assert len(rolled) == 1 and rolled[0].signal_id == stopped[0].signal_id
    assert rolled[0].net_pnl == stopped[0].net_pnl and rolled[0].exit_time == stopped[0].exit_time     # closed at the same open
    child = [t for t in got["roll_rules"] if t.roll_no == 1]
    assert len(child) == 1 and child[0].campaign_id == rolled[0].campaign_id == rolled[0].signal_id
    assert child[0].entry_time[:10] == rolled[0].exit_time[:10]                    # opened at the open the old one closed at
    assert ist_date(child[0].signal_time) < ist_date(child[0].entry_time)          # decided at the previous close
    sell = lambda t: next(x for x in t.legs if x["action"] == "SELL")
    assert sell(child[0])["strike"] < sell(rolled[0])["strike"] and sell(child[0])["right"] == "PE"
    assert unhedged_short_legs(child[0].legs, child[0].expiry, child[0].lots, child[0].lot_size) == [] and child[0].lots == 1
    assert max(t.roll_no for t in got["roll_rules"]) == 1                          # max_rolls
    assert child[0].max_profit_inr > 0
    width = abs(child[0].legs[0]["strike"] - child[0].legs[1]["strike"])
    assert child[0].max_profit_inr >= ROLL.min_net_credit_pct_of_width / 100 * width * child[0].lot_size
    camps = {c["campaign_id"]: c for c in campaigns_from_trades(got["roll_rules"])}
    c = camps[rolled[0].campaign_id]
    assert c["rolls"] == 1 and c["positions"] == 2 and c["open"] is False
    assert c["realised_pnl_inr"] == pytest.approx(rolled[0].net_pnl + child[0].net_pnl, abs=0.01)
    assert c["charges_inr"] == pytest.approx(rolled[0].costs["total"] + child[0].costs["total"], abs=0.01)
    assert c["hedge_cost_inr"] == pytest.approx(sum(
        (x["entry_price"] - x["exit_price"]) * x["lots"] * x["lot_size"]
        for t in (rolled[0], child[0]) for x in t.legs if x["action"] == "BUY"), abs=0.01)
    # with max_rolls 0 the same threat is just an exit
    none = settings(backtest=cfg.backtest.model_dump(), options=cfg.options.model_dump(),
                    stock_option_selling={"roll": {**ROLL.model_dump(), "max_rolls": 0}})
    again = policy_runs(none, eng, "RELIANCE", dev)["baseline"]["roll_rules"]
    assert all(t.roll_no == 0 and t.exit_reason != "ROLLED" for t in again)


def test_roll_always_never_stops_and_rolls_a_losing_spread_into_the_next_month(slide_world):
    cfg, eng, dev = setup_policy(slide_world)
    got = policy_runs(cfg, eng, "RELIANCE", dev)["baseline"]
    assert any(t.exit_reason == "STOP" for t in got["stop"])
    always = got["roll_always"]
    assert all(t.exit_reason not in ("STOP", "STRIKE_BREACHED") for t in always)
    rolled = [t for t in always if t.exit_reason == "ROLLED"]
    assert rolled and all(t.net_pnl < 0 for t in rolled)                            # only a losing spread is rolled
    for t in rolled:
        child = next(x for x in always if x.campaign_id == t.campaign_id and x.roll_no == t.roll_no + 1)
        assert child.expiry > t.expiry and child.entry_time[:10] == t.exit_time[:10]
        assert (date.fromisoformat(t.expiry) - ist_date(t.exit_time)).days <= 7     # at the forced exit before expiry week
        assert unhedged_short_legs(child.legs, child.expiry, child.lots, child.lot_size) == []
        assert datetime.fromisoformat(t.exit_time).astimezone(IST).hour == 9


def test_ACCEPTANCE_report_compares_the_three_exit_policies_per_campaign(drop_world):
    cfg, eng, dev = setup_policy(drop_world)
    out = policy_comparison(cfg, eng, ["RELIANCE"], dev)["exit_policy_comparison"]
    assert out["exit_check"] == "daily" and "same signals" in out["note"]
    for group in ("strategy", "baseline"):
        rows = {r["policy"]: r for r in out[group]}
        assert list(rows) == ["stop", "roll_rules", "roll_always"]
        assert len({r["campaigns"] for r in rows.values()}) == 1 and rows["stop"]["campaigns"] > 0     # the same entries
        assert rows["stop"]["rolls"] == 0 and rows["stop"]["positions"] == rows["stop"]["campaigns"]
        for r in rows.values():
            assert r["positions"] == r["campaigns"] + r["rolls"] and r["low_sample"] is True          # under 30 campaigns
            assert r["worst_campaign_inr"] <= r["per_campaign_inr"] and r["hedge_cost_inr"] > 0
    base = {r["policy"]: r for r in out["baseline"]}
    assert base["roll_rules"]["rolls"] == 1 and base["roll_rules"]["max_rolls_in_a_campaign"] == 1
    assert base["roll_rules"]["net_pnl_inr"] != base["stop"]["net_pnl_inr"]
    got = policy_runs(cfg, eng, "RELIANCE", dev)["baseline"]
    assert base["roll_rules"]["net_pnl_inr"] == pytest.approx(sum(t.net_pnl for t in got["roll_rules"]), abs=0.02)
    # deterministic
    assert policy_comparison(cfg, eng, ["RELIANCE"], dev)["exit_policy_comparison"] == out
