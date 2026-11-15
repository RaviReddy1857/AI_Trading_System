"""Phase 9 M3 config: ICICI Direct's physical-settlement rules (stock_option_selling.settlement), entered by the user
on 2026-11-15. Config and validation only; the alerts and estimates that read it are not built yet."""
from datetime import time

import pytest
from pydantic import ValidationError

from app.config import AutoClose, DeliveryMargin, Settlement, StockOptionSelling, load_settings


def test_ACCEPTANCE_config_yaml_holds_the_icici_settlement_rules_as_entered():
    st = load_settings().stock_option_selling.settlement
    assert (st.alert_from_trading_days, st.urgent_from_trading_days, st.near_money_pct) == (7, 5, 5.0)
    dm = st.broker.delivery_margin
    assert dm.confirmed and dm.near_month_only and dm.ctm_within_pct_of_spot == 5 and dm.base == "VaR + ELM + adhoc margin"
    assert dm.schedule_pct == {4: 10, 3: 25, 2: 45, 1: 70, 0: 100}
    assert (dm.collected_daily_at, dm.collected_daily_on_days, dm.collected_hourly_on_days) == ("15:15", [4, 3, 2], [1, 0])
    ac = st.broker.auto_close
    assert ac.confirmed is False and len(ac.sources) == 2                        # both statements kept, unconfirmed
    assert st.broker.fresh_position_block.confirmed is False and st.broker.fresh_position_block.rule == ""
    ob = st.broker.obligation
    assert ob.confirmed and ob.call_shares_in_demat_by == "11:00" and ob.call_shares_deadline_confirmed is False
    assert all(x.source for x in (dm, ob)) and all(x.source for x in ac.sources)


def test_alert_deadline_is_the_earliest_stated_time_for_the_legs_held():
    ac = load_settings().stock_option_selling.settlement.broker.auto_close
    assert ac.deadline(has_long=True, has_short=True) == time(12, 0)       # a spread: the bought leg may go at 12:00
    assert ac.deadline(has_long=False, has_short=True) == time(13, 30)     # short only: 13:30 beats 14:30
    assert ac.deadline(has_long=True, has_short=False) == time(12, 0)
    assert AutoClose().deadline(True, True) is None                        # nothing stated: no deadline invented


def test_urgent_is_never_later_than_the_forced_exit():
    with pytest.raises(ValidationError, match="forced_exit"):
        StockOptionSelling(settlement=Settlement(urgent_from_trading_days=4))
    with pytest.raises(ValidationError, match="cannot exceed"):
        Settlement(alert_from_trading_days=5, urgent_from_trading_days=6)


@pytest.mark.parametrize("bad, why", [
    ({"schedule_pct": {4: 10, 2: 45, 0: 100}}, "one entry per trading day"),
    ({"schedule_pct": {2: 50, 1: 40, 0: 100}}, "rising"),
    ({"schedule_pct": {1: 70, 0: 120}}, "rising"),
    ({"schedule_pct": {1: 70, 0: 100}, "collected_daily_on_days": [3], "collected_daily_at": "15:15"}, "not in schedule"),
    ({"schedule_pct": {1: 70, 0: 100}, "collected_daily_on_days": [1]}, "needs collected_daily_at"),
    ({"schedule_pct": {1: 70, 0: 100}, "collected_daily_on_days": [1], "collected_hourly_on_days": [1],
      "collected_daily_at": "15:15"}, "not both"),
    ({"confirmed": True}, "confirmed needs"),
    ({"collected_daily_at": "16:00"}, "outside market hours"),
])
def test_delivery_margin_rules_are_validated(bad, why):
    with pytest.raises(ValidationError, match=why):
        DeliveryMargin(**bad)


def test_unset_rules_default_to_unknown_never_to_a_value():
    b = Settlement().broker
    assert b.delivery_margin.schedule_pct == {} and not b.delivery_margin.confirmed
    assert b.auto_close.sources == [] and not b.obligation.confirmed and b.obligation.call_shares_in_demat_by is None
