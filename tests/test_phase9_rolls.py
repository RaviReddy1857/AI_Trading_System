"""Phase 9: campaign tracking, roll rules, minimum net credit, stuck-position alert, no recovery sizing,
and the three exit policies compared on the same entries in the backtest."""
from dataclasses import replace
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
from tests.test_phase9_core import (HELD_ROLL, LOT_R, big_settings, roll_ctx, settings, EXP, EXP_NEXT, LOT, NOW, chain, ctx_for, engine_for, ist_date, legs_of, stock_bars,
                                    stock_world)
from tests.test_phase9_rules import snap

SOS = settings().stock_option_selling
ROLL = SOS.roll


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
def test_ACCEPTANCE_roll_net_credit_is_new_credit_less_the_debit_to_close_less_all_roll_charges():
    """By hand, live quotes (sell at the bid, buy at the ask; no further slippage), 1,000 shares:
    close the old spread: buy back 1110 PE at 2.20, sell 1100 PE at 1.40 -> debit 0.80 x 1000 = 800.
    open the replacement at its bid and ask. Charges: four executions (two legs closed, two opened)."""
    from app.costs import CostModel
    cfg = big_settings()
    strat = build_stock_selling(cfg)
    c = roll_ctx(cfg, stock_bars())
    out = roll_choices(strat, c, HELD_ROLL, 0.8)
    assert out["action"] == "ROLL" and out["roll_note"] == "" and out["refused_by"] is None
    roll = out["roll"]
    sold, bought = roll["legs"]
    assert (sold["action"], bought["action"]) == ("SELL", "BUY") and sold["lots"] == bought["lots"] == 1
    new_credit = (sold["price"] - bought["price"]) * LOT_R
    costs = CostModel(cfg.costs)
    charges = float(costs.options(1, 2.2 * LOT_R, 1.4 * LOT_R).total
                    + costs.options(1, bought["price"] * LOT_R, sold["price"] * LOT_R).total)
    assert roll["close_debit_inr"] == pytest.approx(800.0, abs=0.01)
    assert roll["new_credit_inr"] == pytest.approx(new_credit, abs=0.01)
    assert roll["roll_charges_inr"] == pytest.approx(charges, abs=0.01) and charges > 4 * 49      # four brokerage legs and more
    net = new_credit - 800.0 - charges
    assert roll["roll_net_credit_inr"] == pytest.approx(net, abs=0.02)
    width = abs(sold["strike"] - bought["strike"]) * LOT_R
    assert roll["replacement_width_inr"] == width
    assert roll["roll_net_credit_pct_of_width"] == pytest.approx(net / width * 100, abs=0.01)
    assert roll["roll_net_credit_pct_of_width"] > ROLL.min_net_credit_pct_of_width
    assert sold["strike"] < HELD_ROLL.short_strike and roll["expiry"] == EXP.isoformat() and roll["roll_no"] == 1
    assert unhedged_short_legs(roll["legs"]) == []
    after = roll["campaign_after_roll"]
    assert after["realised_pnl_inr"] == out["exit"]["realised_pnl_inr"] and after["recovery_possible"] is True
    # history (last traded prices): each leg is moved against us by the options slippage exactly once
    from app.options.stock_selling import roll_net_credit
    hist = replace(c, historical=True, require_margin=False, margin=None)
    sig = strat.assess(hist).signal
    m = roll_net_credit(hist, HELD_ROLL, sig)
    s_new, b_new = (next(x for x in sig.legs if x.action == k) for k in ("SELL", "BUY"))
    slip = cfg.costs.slippage_pct.options / 100
    exp_close = (2.1 * (1 + slip) - 1.5 * (1 - slip)) * LOT_R                # last prices 2.10 and 1.50
    exp_new = (s_new.price * (1 - slip) - b_new.price * (1 + slip)) * LOT_R
    assert m["close_debit_inr"] == pytest.approx(exp_close, abs=11) and m["new_credit_inr"] == pytest.approx(exp_new, abs=11)
    assert m["roll_net_credit_inr"] == pytest.approx(m["new_credit_inr"] - m["close_debit_inr"] - m["roll_charges_inr"], abs=0.02)


def test_ACCEPTANCE_a_positive_new_premium_alone_does_not_qualify_and_each_rule_can_refuse_a_roll():
    cfg = big_settings()
    strat = build_stock_selling(cfg)
    bars = stock_bars()
    # the open spread is dear to close: new credit is positive, roll net credit is negative -> exit
    dear = roll_choices(strat, roll_ctx(cfg, bars, cheap=False), HELD_ROLL, 7.0)
    assert dear["action"] == "EXIT" and dear["refused_by"] == "ROLL_NET_CREDIT_TOO_SMALL"
    assert "new credit Rs" in dear["roll_note"] and "not above 10%" in dear["roll_note"]
    fresh = strat.assess(roll_ctx(cfg, bars, cheap=False)).signal
    assert fresh is not None and fresh.details["net_credit_after_costs_inr"] > 0        # the premium alone was positive
    # exactly at the limit is not enough: the ratio must be ABOVE min_roll_net_credit_pct_of_width
    ok = roll_choices(strat, roll_ctx(cfg, bars), HELD_ROLL, 0.8)["roll"]["roll_net_credit_pct_of_width"]
    at = build_stock_selling(big_settings(roll={**ROLL.model_dump(), "min_net_credit_pct_of_width": ok}))
    assert roll_choices(at, roll_ctx(cfg, bars), HELD_ROLL, 0.8)["action"] == "EXIT"
    # the campaign has already been rolled max_rolls times
    again = roll_choices(strat, roll_ctx(cfg, bars), HELD_ROLL, 0.8, rolls_so_far=1)
    assert again["action"] == "EXIT" and again["refused_by"] == "MAX_ROLLS" and "already been rolled 1 time" in again["roll_note"]
    never = build_stock_selling(big_settings(roll={**ROLL.model_dump(), "max_rolls": 0}))
    assert roll_choices(never, roll_ctx(cfg, bars), HELD_ROLL, 0.8)["action"] == "EXIT"
    # the sold strike must be strictly lower for a put spread: the same or a higher strike is never a roll
    low = SpreadPosition("RELIANCE", "PE", 1000.0, 980.0, EXP, 4.0, LOT_R)
    out = roll_choices(strat, roll_ctx(cfg, bars), low, 0.8)
    assert out["action"] == "EXIT" and out["refused_by"] == "STRIKE_NOT_BETTER" and "not below the current 1000" in out["roll_note"]
    same_k = legs_of(strat.evaluate(roll_ctx(cfg, bars))[0])[0].strike
    same = SpreadPosition("RELIANCE", "PE", same_k, same_k - 20, EXP, 4.0, LOT_R)
    assert roll_choices(strat, roll_ctx(cfg, bars), same, 0.8)["refused_by"] == "STRIKE_NOT_BETTER"
    # the replacement must pass every entry rule a new trade would: IV rank, events, margin, the campaign limit
    assert roll_choices(strat, roll_ctx(cfg, bars, iv_rank=10.0), HELD_ROLL, 0.8)["refused_by"] == "IV_RANK_TOO_LOW"
    assert roll_choices(strat, roll_ctx(cfg, bars, margin=None), HELD_ROLL, 0.8)["refused_by"] == "MARGIN_UNKNOWN"
    from app.manual_data import Event
    ev = roll_choices(strat, roll_ctx(cfg, bars, events=(Event(date(2026, 11, 25), "RESULTS", "", "RELIANCE"),)), HELD_ROLL, 0.8)
    assert ev["refused_by"] == "EVENT_BEFORE_EXPIRY"
    stops = (ClosedTrade(date(2026, 9, 20), True, -4000.0), ClosedTrade(date(2026, 10, 30), True, -4000.0))
    assert roll_choices(strat, roll_ctx(cfg, bars, closed_trades=stops), HELD_ROLL, 0.8)["refused_by"] == "STOCK_LOSS_LIMIT"
    # an earlier expiry is never a roll target; a leg of the open spread without a price means the roll is unknown
    later = SpreadPosition("RELIANCE", "PE", 1110.0, 1100.0, date(2026, 12, 15), 4.0, LOT_R)
    out = roll_choices(strat, roll_ctx(cfg, bars), later, 0.8)
    assert out["action"] == "EXIT" and out["refused_by"] == "NO_EXPIRY"
    gone = roll_choices(strat, roll_ctx(cfg, bars, drop={(1100.0, "PE")}), HELD_ROLL, 0.8)
    assert gone["action"] == "EXIT" and gone["refused_by"] == "ROLL_PRICE_UNKNOWN" and gone["exit"]["cost_to_close_inr"] == 800.0


def test_a_roll_that_cannot_recover_the_campaign_says_so():
    cfg = big_settings()
    out = roll_choices(build_stock_selling(cfg), roll_ctx(cfg, stock_bars()), HELD_ROLL, 0.8, realised_so_far_inr=-200000.0)
    after = out["roll"]["campaign_after_roll"]
    assert out["action"] == "ROLL" and after["recovery_possible"] is False
    assert after["message"] == "recovery not possible from this position" and after["best_possible_outcome_inr"] < 0
    assert out["exit"]["campaign_pnl_inr"] == pytest.approx(-200000.0 + out["exit"]["realised_pnl_inr"], abs=0.02)


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
    why = stuck_position(pos, 42.0, 1060.0, today, SOS)
    assert why and "in the money" in why and "4.8%" in why
    assert stuck_position(pos, 50.0, 1060.0, today, SOS) is None        # time value 20%
    assert stuck_position(pos, 44.4, 1060.0, today, SOS) is not None    # 9.9%
    assert stuck_position(pos, 44.5, 1060.0, today, SOS) is None        # 10.1%
    assert stuck_position(pos, 3.0, 1120.0, today, SOS) is None         # out of the money
    assert stuck_position(pos, None, 1060.0, today, SOS) is None        # unknown price: nothing is claimed
    call = SpreadPosition("RELIANCE", "CE", 1100.0, 1120.0, EXP, 4.0, LOT)
    assert stuck_position(call, 41.0, 1140.0, today, SOS) is not None and stuck_position(call, 41.0, 1090.0, today, SOS) is None
    # inside the pre-expiry window, whatever the price
    assert "forced-exit window" in stuck_position(pos, 3.0, 1120.0, date(2026, 12, 1), SOS)      # 5 trading days left
    assert stuck_position(pos, 3.0, 1120.0, date(2026, 11, 30), SOS) is None                        # 6 trading days left
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


REFUSALS = {"STUCK_POSITION", "MAX_ROLLS", "OTHER_SIDE", "STRIKE_NOT_BETTER", "ROLL_PRICE_UNKNOWN",
            "ROLL_NET_CREDIT_TOO_SMALL", "NO_EXPIRY", "ROLL_NOT_FILLED", "NO_TREND", "IV_RANK_UNKNOWN", "IV_RANK_TOO_LOW",
            "DOWNTREND_BLOCK", "UPTREND_BLOCK", "STOCK_LOSS_LIMIT", "NO_STRIKE", "ILLIQUID", "UNHEDGED_SHORT",
            "CREDIT_TOO_SMALL", "MAX_LOSS_ABOVE_LIMIT", "INSUFFICIENT_HISTORY", "NO_CREDIT", "NO_CREDIT_AFTER_COSTS"}


def test_roll_policy_refuses_a_roll_that_does_not_pay_for_closing_the_old_spread_and_records_the_rule(drop_world):
    """A stopped spread costs at least twice its credit to close, so a replacement at the rules'
    strikes cannot give a positive roll net credit: the roll policy exits, as the stop policy does,
    and every refusal is recorded with the rule that made it."""
    cfg, eng, dev = setup_policy(drop_world)
    got = policy_runs(cfg, eng, "RELIANCE", dev)["baseline"]
    stopped = [t for t in got["stop"] if t.exit_reason == "STOP" and t.variant == "baseline:PE"]
    assert len(stopped) == 1 and stopped[0].roll_refused == ""           # the stop policy never considers a roll
    same = [t for t in got["roll_rules"] if t.signal_id == stopped[0].signal_id]
    assert len(same) == 1 and same[0].exit_reason == "STOP" and same[0].roll_no == 0
    assert same[0].net_pnl == stopped[0].net_pnl and same[0].exit_time == stopped[0].exit_time
    assert same[0].roll_refused in REFUSALS and same[0].roll_refused != ""
    assert all(t.roll_no == 0 and t.exit_reason != "ROLLED" for t in got["roll_rules"])
    considered = [t for t in got["roll_rules"] if t.exit_reason in ("STOP", "STRIKE_BREACHED")]
    assert considered and all(t.roll_refused in REFUSALS for t in considered)
    assert all(t.roll_refused == "" for t in got["roll_rules"] if t.exit_reason in ("TARGET", "TIME", "PERIOD_END"))


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
        assert (date.fromisoformat(t.expiry) - ist_date(t.exit_time)).days <= 10    # at the time exit, 10 days before expiry
        assert unhedged_short_legs(child.legs, child.expiry, child.lots, child.lot_size) == []
        assert datetime.fromisoformat(t.exit_time).astimezone(IST).hour == 9
        # the roll is linked to its original trade, and the campaign's figures add up
        assert child.campaign_id == t.campaign_id == next(x.signal_id for x in always if x.campaign_id == t.campaign_id and x.roll_no == 0)
        assert child.lots == 1 and ist_date(child.signal_time) < ist_date(child.entry_time)
    camps = {c["campaign_id"]: c for c in campaigns_from_trades(always)}
    first = rolled[0]
    legs = [x for x in always if x.campaign_id == first.campaign_id]
    c = camps[first.campaign_id]
    assert c["rolls"] == len(legs) - 1 >= 1 and c["positions"] == len(legs) and c["open"] is False
    assert c["realised_pnl_inr"] == pytest.approx(sum(x.net_pnl for x in legs), abs=0.01)
    assert c["charges_inr"] == pytest.approx(sum(x.costs["total"] for x in legs), abs=0.01)
    assert c["hedge_cost_inr"] == pytest.approx(sum((y["entry_price"] - y["exit_price"]) * y["lots"] * y["lot_size"]
                                                    for x in legs for y in x.legs if y["action"] == "BUY"), abs=0.01)
    # no expired contract is ever held: every position is closed on or before its expiry
    assert all(ist_date(x.exit_time) <= date.fromisoformat(x.expiry) for x in always)


def test_ACCEPTANCE_report_compares_the_three_exit_policies_per_campaign_on_daily_and_synchronized_bars(drop_world):
    cfg, eng, dev = setup_policy(drop_world)
    both = policy_comparison(cfg, eng, ["RELIANCE"], dev)["exit_policy_comparison"]
    assert list(both) == ["intraday", "daily"]
    for mode, out in both.items():
        assert out["exit_check"] == mode and "same signals" in out["note"] and "research only" in out["note"]
        for group in ("strategy", "baseline"):
            rows = {r["policy"]: r for r in out[group]}
            assert list(rows) == ["stop", "roll_rules", "roll_always"]
            assert rows["stop"]["rolls"] == 0 and rows["stop"]["positions"] == rows["stop"]["campaigns"] > 0
            for r in rows.values():
                assert r["positions"] == r["campaigns"] + r["rolls"] and r["low_sample"] is True      # under 30 campaigns
                assert r["worst_campaign_inr"] <= r["per_campaign_inr"] and r["hedge_cost_inr"] > 0
                assert r["avg_win_inr"] is None or r["avg_win_inr"] > 0
                assert r["campaigns"] + r["data_incomplete_campaigns"] + r["entries_without_synchronized_bar"] >= rows["stop"]["campaigns"]
        base = {r["policy"]: r for r in out["baseline"]}
        assert base["roll_rules"]["rolls"] == 0 and base["roll_rules"]["net_pnl_inr"] == base["stop"]["net_pnl_inr"]
        assert base["roll_rules"]["roll_refusals"] and set(base["roll_rules"]["roll_refusals"]) <= REFUSALS
        assert base["stop"]["roll_refusals"] == {}
    assert "synchronized" in both["intraday"]["note"].lower() and "daily closes" in both["daily"]["note"]
    assert both["daily"]["baseline"][0]["data_incomplete_campaigns"] == 0
    got = policy_runs(cfg, eng, "RELIANCE", dev, "intraday")["baseline"]
    assert {r["policy"]: r for r in both["intraday"]["baseline"]}["stop"]["net_pnl_inr"] == pytest.approx(
        sum(t.net_pnl for t in got["stop"]), abs=0.02)
    assert all(t.exit_check == "intraday" for t in got["stop"])
    # the same signals are entered under every policy in the synchronized run too
    assert originals(got["roll_rules"]) == originals(got["stop"])
    # deterministic
    assert policy_comparison(cfg, eng, ["RELIANCE"], dev)["exit_policy_comparison"] == both


def test_synchronized_roll_every_month_fills_all_four_legs_in_one_bar_or_just_exits(slide_world):
    cfg, eng, dev = setup_policy(slide_world)
    always = policy_runs(cfg, eng, "RELIANCE", dev, "intraday")["baseline"]["roll_always"]
    assert always and all(t.exit_reason not in ("STOP", "STRIKE_BREACHED") for t in always)
    for t in always:
        assert ist_date(t.exit_time) <= date.fromisoformat(t.expiry)                 # never held past expiry
        if t.exit_reason == "ROLLED":
            child = next(x for x in always if x.campaign_id == t.campaign_id and x.roll_no == t.roll_no + 1)
            assert child.entry_time == t.exit_time                                   # the same synchronized bar
            assert child.expiry > t.expiry and t.net_pnl < 0
        elif t.roll_refused:
            assert t.roll_refused in REFUSALS                                        # not made: recorded why, and closed
