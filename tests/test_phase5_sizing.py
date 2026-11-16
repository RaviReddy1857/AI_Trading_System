"""Phase 5 sizing (P5.1): quantities and lots against hand calculations, with the exact boundary
cases (just under, exactly at, just over the limit)."""
from decimal import Decimal

import pytest

from app.risk.sizing import cash_quantity, lots_within, option_lots
from tests.phase5_fixtures import FlatCosts, ZeroCosts, leg

CAP = 200000.0     # capital
RISK = 1.0         # max_loss_per_trade_pct -> budget 2,000
POS_CAP = 25.0     # position value cap -> 50,000


# ---------------- cash: quantity = floor(capital x pct / (entry - stop)), capped ----------------
@pytest.mark.parametrize("entry, stop, expected, why", [
    # budget 200000 x 1% = 2,000
    (100.0, 90.0, 200, "2000 / 10 = 200.0 -> 200 shares; risk 200 x 10 = 2,000 = budget (exactly at)"),
    (100.0, 90.01, 200, "2000 / 9.99 = 200.2 -> 200; risk 1,998 (just under)"),
    (100.0, 89.99, 199, "2000 / 10.01 = 199.8 -> 199; 200 shares would risk 2,002 (just over)"),
    (100.0, 110.0, 200, "short: 2000 / |100 - 110| = 200"),
    (100.0, 100.0, 0, "zero distance: no size (ZERO_SIZE)"),
    (100.0, 0.0, 20, "2000 / 100 = 20"),
])
def test_cash_quantity_by_risk(entry, stop, expected, why):
    q, f = cash_quantity(CAP, RISK, POS_CAP, entry, stop)
    assert q == expected, why
    assert f["budget_inr"] == 2000.0


@pytest.mark.parametrize("capital, expected, why", [
    (200000.0, 200, "budget 2,000.00: 200 shares at Rs 10 a share = 2,000 (at)"),
    (199999.0, 199, "budget 1,999.99: 200 shares would be 2,000 > 1,999.99 (just over) -> 199"),
    (200001.0, 200, "budget 2,000.01: 201 shares would be 2,010 -> 200 (just under the next share)"),
])
def test_cash_quantity_budget_boundary(capital, expected, why):
    assert cash_quantity(capital, RISK, POS_CAP, 100.0, 90.0)[0] == expected, why


@pytest.mark.parametrize("entry, stop, expected, why", [
    # by risk: 2000 / 5 = 400 shares; cap 25% of 200000 = 50,000 of position value
    (250.0, 245.0, 200, "cap 50,000 / 250 = 200 shares exactly (value 50,000 = cap)"),
    (250.01, 245.01, 199, "50,000 / 250.01 = 199.99 -> 199 (200 would be 50,002 > cap)"),
    (249.99, 244.99, 200, "50,000 / 249.99 = 200.008 -> 200 (value 49,998)"),
])
def test_cash_quantity_position_value_cap(entry, stop, expected, why):
    assert cash_quantity(CAP, RISK, POS_CAP, entry, stop)[0] == expected, why


def test_cash_quantity_includes_costs():
    """Costs Rs 20 + Rs 0.10 a share. Largest q with 10q + 20 + 0.1q <= 2,000: 10.1q <= 1,980,
    q <= 196.04 -> 196. Check: 196 -> 1,960 + 20 + 19.60 = 1,999.60 <= 2,000; 197 -> 2,009.70 > 2,000."""
    cost = lambda q: Decimal(20) + Decimal("0.10") * q
    q, f = cash_quantity(CAP, RISK, POS_CAP, 100.0, 90.0, cost)
    assert q == 196 and f["by_formula"] == 200 and f["after_costs"] == 196


def test_cash_quantity_size_factor_halves():
    """Drawdown factor 0.5: budget 2,000 x 0.5 = 1,000 -> 1000 / 10 = 100 shares; the cap halves too
    (25,000 of position value at Rs 100 = 250 shares, not binding)."""
    assert cash_quantity(CAP, RISK, POS_CAP, 100.0, 90.0, size_factor=0.5)[0] == 100
    # cap binding: 25,000 / 250 = 100 shares at half size (200 at full size)
    assert cash_quantity(CAP, RISK, POS_CAP, 250.0, 245.0, size_factor=0.5)[0] == 100


def test_size_factor_can_only_reduce():
    with pytest.raises(ValueError):
        cash_quantity(CAP, RISK, POS_CAP, 100.0, 90.0, size_factor=1.5)


@pytest.mark.parametrize("held, expected, why", [
    (1000.0, 100, "budget 2,000 - 1,000 held in the correlated group = 1,000 -> 100 shares (combined 2,000, at)"),
    (1000.01, 99, "remaining 999.99 -> 99 shares (100 would make the group 2,000.01, just over)"),
    (2000.0, 0, "nothing left of the budget"),
])
def test_cash_quantity_correlated_group_counts_as_one(held, expected, why):
    assert cash_quantity(CAP, RISK, POS_CAP, 100.0, 90.0, held_risk_inr=held)[0] == expected, why


# ---------------- options: lots = floor(budget / max loss of one lot) ----------------
def long_call(premium, lot_size=75):
    return [leg("BUY", "CE", 24000, premium, 1, lot_size)]


@pytest.mark.parametrize("premium, budget, lots, why", [
    (100.0, 7500.0, 1, "one lot 100 x 75 = 7,500 = budget (exactly at)"),
    (99.99, 7500.0, 1, "one lot 7,499.25 (just under)"),
    (100.01, 7500.0, 0, "one lot 7,500.75 > 7,500 (just over): ZERO_SIZE"),
    (100.0, 15000.0, 2, "15,000 / 7,500 = 2 lots exactly"),
    (100.0, 14999.99, 1, "just under two lots"),
    (100.0, 25000.0, 3, "25,000 / 7,500 = 3.33 -> 3"),
])
def test_long_option_lots(premium, budget, lots, why):
    n, pay, structure = option_lots(long_call(premium), ZeroCosts(), budget)
    assert (n, structure) == (lots, "LONG_OPTION"), why


def test_long_option_lots_with_costs():
    """Rs 40 a lot (Rs 20 each side): one lot 7,500 + 40 = 7,540. Budget 25,000: floor(25,000 / 7,540) = 3;
    3 lots = 22,500 + 120 = 22,620 <= 25,000. Budget 7,539.99: one lot does not fit."""
    n, pay, _ = option_lots(long_call(100.0), FlatCosts(), 25000.0)
    assert n == 3 and pay.max_loss_inr == Decimal("22620.00")
    assert option_lots(long_call(100.0), FlatCosts(), 7539.99)[0] == 0
    assert option_lots(long_call(100.0), FlatCosts(), 7540.0)[0] == 1


def bull_put_legs(lots=1):
    # sold 24000 PE at 60, bought 23900 PE at 30: credit 30, width 100 -> max loss (100 - 30) x 75 = 5,250 a lot
    return [leg("SELL", "PE", 24000, 60.0, lots), leg("BUY", "PE", 23900, 30.0, lots)]


@pytest.mark.parametrize("budget, lots, why", [
    (10500.0, 2, "10,500 / 5,250 = 2 lots exactly (max loss 10,500 = budget)"),
    (10499.99, 1, "just under two lots' 10,500"),
    (10500.01, 2, "just over: still 2 (3 lots = 15,750)"),
    (5250.0, 1, "one lot exactly"),
    (5249.99, 0, "one lot just over the budget: ZERO_SIZE"),
])
def test_credit_spread_lots(budget, lots, why):
    n, pay, structure = option_lots(bull_put_legs(), ZeroCosts(), budget)
    assert (n, structure) == (lots, "CREDIT_SPREAD"), why
    if n:
        assert pay.max_loss_inr == Decimal(5250) * n


def test_credit_spread_resized_from_one_lot():
    """A signal that came with 4 lots is sized again from one lot: budget 10,500 -> 2 lots."""
    assert option_lots(bull_put_legs(lots=4), ZeroCosts(), 10500.0)[0] == 2


def test_credit_spread_max_lots_cap():
    """Phase 9: never more lots than signalled. Budget 26,250 allows 5; capped at 3."""
    assert option_lots(bull_put_legs(lots=3), ZeroCosts(), 26250.0, max_lots=3)[0] == 3


def condor_legs():
    # put 23800/23700 for 40 - 20, call 24200/24300 for 45 - 25: credit 40, width 100 -> (100 - 40) x 75 = 4,500 a lot
    return [leg("SELL", "PE", 23800, 40.0), leg("BUY", "PE", 23700, 20.0),
            leg("SELL", "CE", 24200, 45.0), leg("BUY", "CE", 24300, 25.0)]


@pytest.mark.parametrize("budget, lots", [(9000.0, 2), (8999.99, 1), (9000.01, 2), (4499.99, 0)])
def test_iron_condor_lots(budget, lots):
    """One lot 4,500: 9,000 = 2 lots exactly; 8,999.99 -> 1; 4,499.99 -> 0."""
    n, pay, structure = option_lots(condor_legs(), ZeroCosts(), budget)
    assert (n, structure) == (lots, "IRON_CONDOR")


def test_lots_within_lowers_n_when_costs_grow():
    """Loss per n lots = 1,000 n + 10 n^2 (costs that grow faster than linear). Budget 3,000:
    floor(3,000 / 1,010) = 2; 2 lots = 2,040 <= 3,000 -> 2. Budget 2,039.99: 2 lots = 2,040 > budget -> 1."""
    loss = lambda n: Decimal(1000 * n + 10 * n * n)
    assert lots_within(3000, loss) == 2
    assert lots_within(2040, loss) == 2
    assert lots_within(2039.99, loss) == 1


def test_unknown_structure_and_lot_size_raise():
    with pytest.raises(ValueError):
        option_lots([leg("BUY", "CE", 24000, 100.0, 1, 0)], ZeroCosts(), 10000)
    with pytest.raises(ValueError):
        option_lots([leg("BUY", "CE", 24000, 100.0), leg("BUY", "PE", 23000, 90.0), leg("SELL", "CE", 24100, 50)],
                    ZeroCosts(), 10000)
