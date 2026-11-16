"""Shared fixtures for the phase 6 tests: a journal on an in-memory store, approved and NO_TRADE decisions,
a fake price source for open positions, and the app on a fake feed."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.journal import Journal
from app.models import RiskDecision
from app.store import Store
from tests.phase5_fixtures import (EXPIRY, NOW, SECTORS, T0, FlatCosts, ZeroCosts, bull_put, cash_signal, leg,
                                   long_call, option_signal, settings)

__all__ = ["EXPIRY", "NOW", "SECTORS", "T0", "FlatCosts", "ZeroCosts", "bull_put", "cash_signal", "leg", "long_call",
           "option_signal", "settings", "approved", "no_trade", "new_journal", "FakeMarks", "app_client", "IST_DAY"]

IST_DAY = date(2026, 11, 16)


def approved(quantity=10, lots=None, risk=100.0, margin=None, profit=200.0, costs=10.0) -> RiskDecision:
    return RiskDecision(status="APPROVED", reason_codes=(), quantity=quantity, lots=lots, risk_inr=risk, risk_pct=0.05,
                        est_profit_inr=profit, est_loss_inr=risk, costs_inr=costs, margin_inr=margin, capital_fit="FITS")


def no_trade(*codes) -> RiskDecision:
    codes = codes or ("DATA_STALE",)
    return RiskDecision(status="NO_TRADE", reason_codes=tuple(codes), quantity=None, lots=None, risk_inr=None,
                        risk_pct=None, est_profit_inr=None, est_loss_inr=None, costs_inr=None, margin_inr=None,
                        capital_fit=None, reasons=tuple(f"{c}: fixture" for c in codes))


def new_journal(s=None, path=":memory:") -> tuple[Journal, Store]:
    store = Store(path)
    return Journal(store, s or settings()), store


class FakeMarks:
    """marks(pos, now) -> (prices, why): a fixed closing price per symbol (cash) or per (strike, right) (options)."""

    def __init__(self, prices: dict | None = None):
        self.prices = dict(prices or {})

    def __call__(self, pos, now):
        if not pos.is_option:
            p = self.prices.get(pos.symbol)
            return ([p], "") if p is not None else (None, f"{pos.symbol} price STALE")
        out = []
        for x in pos.legs:
            p = self.prices.get((x.strike, x.right))
            if p is None:
                return None, f"{x.strike:g} {x.right}: quote STALE"
            out.append(p)
        return out, ""


def app_client(s=None, db_path=":memory:", now_fn=None):
    from app.main import create_app
    from tests.test_step1 import FakeFeed, sim_settings
    return TestClient(create_app(s or sim_settings(), feed=FakeFeed(), db_path=db_path, background=False),
                      base_url="http://127.0.0.1:8000")
