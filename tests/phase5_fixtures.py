"""Shared fixtures for the phase 5 risk-engine tests: signals, a good account and market state, and
cost models whose charges are easy to calculate by hand."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.costs import CostBreakdown
from app.models import Signal
from app.options.payoff import OptionLeg
from app.risk.engine import MarketState
from app.risk.portfolio import AccountState, OpenPosition
from tests.fixture_settings import load_settings

T0 = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)          # 10:30 IST, a Monday
NOW = T0 + timedelta(minutes=1)
EXPIRY = date(2026, 11, 24)
SECTORS = {"NIFTY": "Index", "BANKNIFTY": "Index", "RELIANCE": "Oil Gas & Consumable Fuels",
           "ONGC": "Oil Gas & Consumable Fuels", "BPCL": "Oil Gas & Consumable Fuels",
           "HDFCBANK": "Financial Services", "ICICIBANK": "Financial Services", "INFY": "Information Technology",
           "TCS": "Information Technology"}


class ZeroCosts:
    """No charges and no slippage: the hand calculations show the sizing formula alone."""

    def require(self, *segments):
        pass

    def equity(self, segment, buy, sell):
        return CostBreakdown()

    def slip_pct(self, segment):
        return Decimal(0)

    def options(self, lots, buy, sell):
        return CostBreakdown()


class FlatCosts(ZeroCosts):
    """Options: Rs 20 brokerage per lot on each side (Rs 40 a lot per leg for the round trip).
    Equity: Rs 20 per round trip, no slippage."""

    def equity(self, segment, buy, sell):
        b = CostBreakdown()
        b.brokerage = Decimal(20)
        return b

    def slip_pct(self, segment):
        return Decimal(0)

    def options(self, lots, buy, sell):
        b = CostBreakdown()
        b.brokerage = Decimal(20) * lots * 2
        return b


def settings(capital: float = 200000.0, **risk):
    s = load_settings()
    raw = s.model_dump()
    raw["account"]["capital_inr"] = capital
    raw["risk"].update(risk)
    return type(s).model_validate(raw)


def cash_signal(symbol="RELIANCE", entry=100.0, stop=90.0, target=120.0, direction="LONG", horizon="INTRADAY",
                **kw) -> Signal:
    return Signal(signal_id=f"cash-{symbol}-{entry}-{stop}", created_at=T0, strategy_id="orb_v2",
                  strategy_version="test", symbol=symbol, asset_class="CASH_EQUITY", direction=direction,
                  horizon=horizon, entry=entry, stop=stop, target=target, valid_until=T0 + timedelta(hours=1),
                  regime="TRENDING_UP", why_now=("fixture",), data_as_of=T0, **kw)


def leg(action, right, strike, price, lots=1, lot_size=75, expiry=EXPIRY) -> OptionLeg:
    return OptionLeg(action, right, strike, expiry, lots, price, lot_size)


def option_signal(legs, entry, stop, target, symbol="NIFTY", asset="INDEX_OPTION", direction="SPREAD",
                  margin_per_lot=None, strategy="credit_spread") -> Signal:
    details = {"lot_size": legs[0].lot_size, "lots": legs[0].lots}
    if margin_per_lot is not None:
        details["margin_per_lot_inr"] = margin_per_lot
    return Signal(signal_id=f"opt-{symbol}-{entry}-{len(legs)}", created_at=T0, strategy_id=strategy,
                  strategy_version="test", symbol=symbol, asset_class=asset, direction=direction, horizon="MONTHLY",
                  entry=entry, stop=stop, target=target, valid_until=T0 + timedelta(hours=1), regime="TRENDING_UP",
                  why_now=("fixture",), data_as_of=T0, legs=tuple(legs), details=details)


def bull_put(symbol="NIFTY", sold=(24000, 60.0), bought=(23900, 30.0), lots=1, lot_size=75, margin_per_lot=40000.0,
             asset="INDEX_OPTION", strategy="credit_spread") -> Signal:
    """Credit 30, width 100: max loss (100 - 30) x lot size per lot before costs (5,250 at 75)."""
    legs = [leg("SELL", "PE", sold[0], sold[1], lots, lot_size), leg("BUY", "PE", bought[0], bought[1], lots, lot_size)]
    credit = sold[1] - bought[1]
    return option_signal(legs, credit, credit * 2, credit * 0.5, symbol, asset, "SPREAD", margin_per_lot, strategy)


def long_call(symbol="NIFTY", strike=24000, premium=100.0, lot_size=75) -> Signal:
    return option_signal([leg("BUY", "CE", strike, premium, 1, lot_size)], premium, premium * 0.5, premium * 2,
                         symbol, "INDEX_OPTION", "LONG", None, "long_option")


def pos(symbol, risk=1000.0, committed=10000.0, today=0.0, month=0.0, kind="CASH", pid=None) -> OpenPosition:
    return OpenPosition(pid or f"p-{symbol}-{risk}-{committed}", symbol, kind, risk, committed, today, month)


GOOD_ACCOUNT = AccountState(capital_inr=200000.0, positions=(), realised_today_inr=0.0, realised_month_inr=0.0,
                            equity_curve=((date(2026, 11, 13), 200000.0),), flows=(), history_verified=True)
GOOD_MARKET = MarketState(now=NOW, price_status="LIVE", regime="TRENDING_UP", market_open=True, index_status="LIVE",
                          quarantined_recent=0)


def account(**kw) -> AccountState:
    return replace(GOOD_ACCOUNT, **kw)


def market(**kw) -> MarketState:
    return replace(GOOD_MARKET, **kw)


def no_corr(a, b):
    return None
