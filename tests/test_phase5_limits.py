"""Phase 5 limits (P5.2 - P5.6), table-driven, each with its hand calculation and the exact boundary
(just under, exactly at, just over)."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.risk.limits import (correlated_group, drawdown_state, kill_switch, loss_stops, sector_check)
from app.risk.portfolio import (FITS, NEEDS_MORE_CAPITAL, TIGHT, capital_fit, pool_check, position_problems,
                                selling_spread_check)
from tests.phase5_fixtures import SECTORS, pos

KS = SimpleNamespace(quarantine_window_minutes=5, quarantine_max_events=20)
D1, D2, D3 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)


# ---------------- P5.2 capital pool: uncommitted_cash_pct ----------------
@pytest.mark.parametrize("new, code, label, why", [
    # capital 200,000, 25% uncommitted -> usable 150,000; 100,000 already committed
    (50000.0, None, FITS, "100,000 + 50,000 = 150,000 = usable (exactly at)"),
    (49999.99, None, FITS, "149,999.99 (just under)"),
    (50000.01, "RISK_BUDGET_USED", TIGHT, "150,000.01 (just over): blocked"),
    (100000.01, "RISK_BUDGET_USED", NEEDS_MORE_CAPITAL, "above capital itself"),
])
def test_pool(new, code, label, why):
    got, text, facts = pool_check(200000.0, [pos("RELIANCE", committed=60000.0), pos("INFY", committed=40000.0)],
                                  new, 25.0)
    assert (got, facts["capital_fit"]) == (code, label), why
    assert facts["usable_inr"] == 150000.0 and facts["cash_free_inr"] == 100000.0


@pytest.mark.parametrize("capital, margin, label, shortfall", [
    # phase 9 acceptance: the same setup, margin Rs 2,30,000
    (200000.0, 230000.0, NEEDS_MORE_CAPITAL, 30000.0),   # 2,30,000 - 2,00,000 free = 30,000 short
    (1000000.0, 230000.0, FITS, 0.0),                    # usable 7,50,000
    (300000.0, 230000.0, TIGHT, 0.0),                    # usable 2,25,000 < 2,30,000 <= 3,00,000
    (300000.0, 225000.0, FITS, 0.0),                     # exactly at usable
    (230000.0, 230000.0, TIGHT, 0.0),                    # exactly at capital
    (229999.99, 230000.0, NEEDS_MORE_CAPITAL, 0.01),     # just over capital
])
def test_capital_fit(capital, margin, label, shortfall):
    got, short, _ = capital_fit(capital, Decimal(0), margin, 25.0)
    assert (got, short) == (label, shortfall)


@pytest.mark.parametrize("open_spreads, max_open, code", [
    (0, 2, None), (1, 2, None),            # 1 + 1 = 2 = max (at)
    (2, 2, "RISK_BUDGET_USED"),            # 2 + 1 = 3 > 2 (over)
    (0, 0, "RISK_BUDGET_USED"),            # selling switched off
])
def test_max_open_selling_spreads(open_spreads, max_open, code):
    held = [pos("NIFTY", kind="INDEX_SELLING_SPREAD", pid=f"s{i}") for i in range(open_spreads)]
    held.append(pos("NIFTY", kind="OPTION_BUY", pid="long"))       # a bought option is not a selling spread
    assert selling_spread_check(held, True, max_open)[0] == code
    assert selling_spread_check(held, False, max_open)[0] is None


# ---------------- P5.3 sector and correlation ----------------
@pytest.mark.parametrize("held, code", [
    ([], None),
    (["ONGC"], None),                     # 1 + 1 = 2 = max (at)
    (["ONGC", "BPCL"], "SECTOR_LIMIT"),   # third trade in the same sector (over)
    (["ONGC", "INFY", "TCS"], None),      # other sectors do not count
])
def test_sector_limit(held, code):
    assert sector_check("RELIANCE", [pos(s) for s in held], SECTORS, 2)[0] == code


def test_sector_unknown_blocks():
    assert sector_check("XYZ", [], SECTORS, 2)[0] == "DATA_MISSING"
    assert sector_check("RELIANCE", [pos("XYZ")], SECTORS, 2)[0] == "DATA_MISSING"


def corr_table(table):
    return lambda a, b: table.get(tuple(sorted((a, b))))


@pytest.mark.parametrize("c, held_risk", [
    (0.8, Decimal(0)),                  # exactly 0.8 is not ABOVE 0.8: separate positions
    (0.7999, Decimal(0)),               # just under
    (0.8001, Decimal("1500.00")),       # just over: one position, ONGC's risk counts against RELIANCE's budget
])
def test_correlation_threshold(c, held_risk):
    code, _, group, held = correlated_group("RELIANCE", [pos("ONGC", risk=1500.0)],
                                            corr_table({("ONGC", "RELIANCE"): c}), 0.8)
    assert code is None and held == held_risk


def test_same_symbol_is_one_position():
    _, _, group, held = correlated_group("RELIANCE", [pos("RELIANCE", risk=700.0)], corr_table({}), 0.8)
    assert group == {"RELIANCE"} and held == Decimal("700.00")


def test_correlation_unknown_blocks_only_when_it_could_join():
    # RELIANCE-ONGC not measurable: blocks
    assert correlated_group("RELIANCE", [pos("ONGC")], corr_table({}), 0.8)[0] == "CORRELATION_UNKNOWN"
    # INFY-TCS (both held) not measurable, RELIANCE clearly unrelated to both: no block
    t = {("INFY", "RELIANCE"): 0.1, ("RELIANCE", "TCS"): 0.2}
    assert correlated_group("RELIANCE", [pos("INFY"), pos("TCS")], corr_table(t), 0.8)[0] is None
    # RELIANCE linked to INFY (0.9), INFY-TCS unknown: TCS could be in RELIANCE's group -> blocks
    t = {("INFY", "RELIANCE"): 0.9, ("RELIANCE", "TCS"): 0.1}
    assert correlated_group("RELIANCE", [pos("INFY"), pos("TCS")], corr_table(t), 0.8)[0] == "CORRELATION_UNKNOWN"


def test_correlation_groups_are_transitive():
    # RELIANCE-INFY 0.85, INFY-TCS 0.9, RELIANCE-TCS 0.5: all three are one position
    t = {("INFY", "RELIANCE"): 0.85, ("INFY", "TCS"): 0.9, ("RELIANCE", "TCS"): 0.5}
    code, _, group, held = correlated_group("RELIANCE", [pos("INFY", risk=300.0), pos("TCS", risk=200.0)],
                                            corr_table(t), 0.8)
    assert code is None and group == {"RELIANCE", "INFY", "TCS"} and held == Decimal("500.00")


# ---------------- P5.4 daily loss stop and monthly pause ----------------
@pytest.mark.parametrize("open_today, codes", [
    # capital 200,000: daily stop 2% = 4,000. Realised today -3,000.
    (-999.99, []),                       # loss 3,999.99 (just under)
    (-1000.0, ["DAILY_STOP"]),           # 4,000.00 (at)
    (-1000.01, ["DAILY_STOP"]),          # 4,000.01 (over)
    (+500.0, []),                        # open profit offsets: loss 2,500
])
def test_daily_stop(open_today, codes):
    got, facts = loss_stops(200000.0, -3000.0, -3000.0, [pos("RELIANCE", today=open_today, month=0.0)], 2.0, 6.0)
    assert [c for c, _ in got] == codes
    assert facts["daily_stop_inr"] == 4000.0


@pytest.mark.parametrize("realised_month, codes", [
    # monthly pause 6% = 12,000; open P&L this month -2,000
    (-9999.99, []), (-10000.0, ["MONTHLY_PAUSE"]), (-10000.01, ["MONTHLY_PAUSE"]),
])
def test_monthly_pause(realised_month, codes):
    got, facts = loss_stops(200000.0, 0.0, realised_month, [pos("RELIANCE", today=0.0, month=-2000.0)], 2.0, 6.0)
    assert [c for c, _ in got] == codes and facts["monthly_pause_inr"] == 12000.0


@pytest.mark.parametrize("realised_today, positions", [
    (None, []), (float("nan"), []), (0.0, None), (0.0, [pos("RELIANCE", today=None)]),
    (0.0, [pos("RELIANCE", month=None)]),
])
def test_loss_stops_unknown_pnl_blocks(realised_today, positions):
    got, _ = loss_stops(200000.0, realised_today, 0.0, positions, 2.0, 6.0)
    assert [c for c, _ in got] == ["PORTFOLIO_UNKNOWN"]


def test_loss_limits_rescale_with_capital():
    """Rs 4,000 is the daily stop at 2,00,000 and only half of it at 4,00,000 (8,000)."""
    assert loss_stops(200000.0, -4000.0, 0.0, [], 2.0, 6.0)[0][0][0] == "DAILY_STOP"
    assert loss_stops(400000.0, -4000.0, 0.0, [], 2.0, 6.0)[0] == []
    assert loss_stops(400000.0, -8000.0, 0.0, [], 2.0, 6.0)[0][0][0] == "DAILY_STOP"


# ---------------- P5.5 drawdown ----------------
@pytest.mark.parametrize("now_equity, factor, code", [
    # peak 1,00,000 -> drawdown = (1 - equity / 1,00,000) x 100
    (90010.0, 1.0, None),                # 9.99% (just under 10)
    (90000.0, 0.5, None),                # 10.00% (at): sizes halved
    (89990.0, 0.5, None),                # 10.01%
    (85010.0, 0.5, None),                # 14.99% (just under 15)
    (85000.0, 0.5, "DRAWDOWN_PAUSE"),    # 15.00% (at): paused
    (84990.0, 0.5, "DRAWDOWN_PAUSE"),    # 15.01%
])
def test_drawdown_thresholds(now_equity, factor, code):
    dd, f, c, facts = drawdown_state(((D1, 100000.0), (D2, now_equity)), (), 10.0, 15.0)
    assert (f, c) == (factor, code)
    assert dd == (Decimal(100000) - Decimal(str(now_equity))) / Decimal(100000) * 100


def test_drawdown_from_peak_not_start():
    """1,00,000 -> 1,20,000 -> 1,08,000: index 1.2 then 1.08, drawdown (1.2 - 1.08) / 1.2 = 10%."""
    dd, f, c, _ = drawdown_state(((D1, 100000.0), (D2, 120000.0), (D3, 108000.0)), (), 10.0, 15.0)
    assert dd == Decimal(10) and f == 0.5 and c is None


def test_deposit_never_counts_as_profit_or_hides_a_drawdown():
    """1,00,000 -> 1,40,000 with a 50,000 deposit on D2: index = (1,40,000 - 50,000) / 1,00,000 = 0.9:
    a 10% drawdown, and the result excluding deposits is -10,000 (not +40,000)."""
    dd, f, c, facts = drawdown_state(((D1, 100000.0), (D2, 140000.0)), ((D2, 50000.0),), 10.0, 15.0)
    assert dd == Decimal(10) and f == 0.5
    assert facts["profit_excluding_deposits_inr"] == -10000.0


def test_withdrawal_is_not_a_loss():
    """1,00,000 -> 50,000 after withdrawing 50,000: index (50,000 + 50,000) / 1,00,000 = 1: no drawdown."""
    dd, f, c, facts = drawdown_state(((D1, 100000.0), (D2, 50000.0)), ((D2, -50000.0),), 10.0, 15.0)
    assert dd == 0 and f == 1.0 and facts["profit_excluding_deposits_inr"] == 0.0


def test_flow_before_first_point_is_in_the_start():
    dd, f, c, _ = drawdown_state(((D2, 100000.0), (D3, 100000.0)), ((D1, 50000.0), (D2, 30000.0)), 10.0, 15.0)
    assert dd == 0


@pytest.mark.parametrize("curve, flows", [
    (None, ()), ((), ()), (((D1, 0.0),), ()), (((D1, float("nan")),), ()), (((D1, 1.0), (D1, 2.0)), ()),
    (((D1, 100.0),), ((D1, None),)),
])
def test_drawdown_unknown_blocks(curve, flows):
    assert drawdown_state(curve, flows, 10.0, 15.0)[2] == "PORTFOLIO_UNKNOWN"


# ---------------- P5.6 kill switch ----------------
@pytest.mark.parametrize("index_status, quarantined, market_open, on", [
    ("LIVE", 0, True, False),
    ("LIVE", 19, True, False),           # just under the spike
    ("LIVE", 20, True, True),            # at
    ("LIVE", 21, True, True),            # over
    ("STALE", 0, True, True), ("NO_DATA", 0, True, True), ("CLOSED", 0, True, True), (None, 0, True, True),
    ("LIVE", 0, False, True),            # outside market hours
    ("LIVE", None, True, True), ("LIVE", 0, None, True),     # unknown inputs
])
def test_kill_switch(index_status, quarantined, market_open, on):
    assert bool(kill_switch("NIFTY", index_status, quarantined, market_open, KS)) is on


# ---------------- positions known ----------------
def test_position_problems():
    assert position_problems(None)
    assert position_problems([pos("RELIANCE", committed=None)])
    assert position_problems([pos("RELIANCE", risk=float("inf"))])
    assert position_problems([pos("RELIANCE")]) == []
