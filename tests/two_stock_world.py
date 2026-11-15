"""A fixture world with TWO stocks of different sectors (RELIANCE, INFY) whose daily returns are not
correlated, each with stored daily and 5-minute option history: for the tests of the portfolio run
(events of every stock in time order, correlations of completed sessions only)."""
import math
from datetime import date, datetime, time, timedelta

from app.backtest import Engine
from app.calendar import MarketCalendar
from app.history import daily_start
from app.models import Bar
from app.options.backtest import OptionEngine, model_prices
from app.options.option_history import INTRADAY, OptionHistory, OptionIntraday
from app.quality import IST
from app.store import Store
from tests.test_phase3 import FixedRegime
from tests.test_phase9_core import BT_FIRST, BT_LAST, NOW, UTC, settings, weekdays

A, B = "RELIANCE", "INFY"
LOTS = {A: 500, B: 500}
DEV_FROM, DEV_TO = date(2025, 6, 2), date(2025, 10, 1)


def portfolio(**over) -> dict:
    return {"enabled": True, "max_open_spreads": 6, "max_total_risk_pct": 100.0, "correlation_window": 60,
            "correlation_threshold": 0.7, "correlation_min_overlap": 50, "group_max_positions": 2,
            "group_max_risk_pct": 100.0, **over}


def two_settings(**limits):
    return settings(backtest={"test_start": {"1day": DEV_FROM}, "holdout_start": {"1day": DEV_TO}},
                    options={"iv_rank_min_history_days": 20, "iv_rank_lookback_days": 60},
                    stock_option_selling={"portfolio": portfolio(**limits)})


def level(sym: str, i: int) -> float:
    """RELIANCE and INFY rise alike, with waves a quarter turn apart: their returns are not correlated."""
    wave = math.sin(i / 4) if sym == A else math.cos(i / 4)
    return 1000 + 0.5 * i + 8 * wave


def two_world(closes: dict | None = None, cfg=None):
    """(settings, store, base engine). `closes`: {(stock, date): close} - that session's CLOSING price
    only; its open, high and low and every other bar stay as they are."""
    cfg = cfg or two_settings()
    store = Store(":memory:")
    bars = []
    for i, d in enumerate(weekdays(date(2024, 1, 1), date(2025, 12, 31))):
        px = 24000 + 60 * math.sin(i / 5)
        bars.append(Bar("NIFTY", daily_start(d), px - 10, px + 45, px - 45, px, None))
        v = 24 + i * 0.01
        bars.append(Bar("INDIAVIX", daily_start(d), v, v + 0.2, v - 0.2, v, None))
        for sym in (A, B):
            sp = level(sym, i)
            bars.append(Bar(sym, daily_start(d), sp - 1, sp + 6, sp - 6, (closes or {}).get((sym, d), sp), 2e6))
    store.save_history_bars("bars_1d", bars, "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    return cfg, store, base


def write_options(store, cfg, lo=900, hi=1500, step=10):
    """Option history of both stocks from the model on the UNCHANGED price path (a second world), so a
    changed daily close never changes an option price: daily bars, and three 5-minute bars a day
    (09:15, 12:00, 15:25) inside the 5-minute window."""
    _, _, plain = two_world(cfg=cfg)
    model = model_prices(cfg, plain)
    for exp, _ in model.expiries(A):
        store.save_option_expiry("sim", "NIFTY", exp, "monthly", NOW)       # stocks expire with the index monthly
        if not (BT_FIRST <= exp <= BT_LAST):
            continue
        w0, w1 = OptionIntraday.window(exp)
        for sym in (A, B):
            for k in range(lo, hi + 1, step):
                for right in ("CE", "PE"):
                    rows, five = [], []
                    for d in weekdays(exp - timedelta(days=60), exp):
                        b = model.bar(sym, exp, k, right, d)
                        if b is None:
                            continue
                        rows.append((daily_start(d).isoformat(), b.open, b.high, b.low, b.close, 1000.0, 500000.0))
                        if w0 <= d <= w1:
                            for hh, mm, px in ((9, 15, b.open), (12, 0, round((b.open + b.close) / 2, 2)), (15, 25, b.close)):
                                five.append((at(d, hh, mm).isoformat(), px, px, px, px, 100.0, 500000.0))
                    store.save_option_bars("sim", sym, exp, k, right, "1day", rows, NOW)
                    store.save_option_bars("sim", sym, exp, k, right, INTRADAY, five, NOW)


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), IST).astimezone(UTC)


def engine(cfg, store, base) -> OptionEngine:
    hist = OptionHistory(store, "sim")
    eng = OptionEngine(cfg, store, "sim", hist, base, lot_sizes=LOTS, intraday=OptionIntraday(hist))
    eng.set_expiry_floor(BT_FIRST)
    return eng


def dev(base) -> list[date]:
    return [d for d in base.session_dates() if DEV_FROM <= d < DEV_TO]


def add_five(store, sym: str, expiry, strike: float, right: str, rows) -> None:
    """Extra 5-minute bars of one contract: rows of (day, hh, mm, price)."""
    store.save_option_bars("sim", sym, expiry if isinstance(expiry, date) else date.fromisoformat(expiry), strike, right,
                           INTRADAY, [(at(d, h, m).isoformat(), px, px, px, px, 1.0, 1.0) for d, h, m, px in rows], NOW)
