"""Shared fixture data for phase 2 tests. Hand-built so expected results are obvious."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.calendar import MarketCalendar
from app.config import Settings
from tests.fixture_settings import load_settings
from app.history import daily_start
from app.models import Bar, Tick
from app.regime import DailyBar, RegimeInputs
from app.store import Store

T0 = datetime(2026, 11, 11, 5, 0, 0, tzinfo=timezone.utc)   # Wed 10:30 IST
SESSION = date(2026, 11, 11)
STOCKS = ["HDFCBANK", "ICICIBANK", "INFY", "RELIANCE", "TCS"]


EMPTY_CSV = {"events.csv": "date,type,note,symbol\n", "holidays.csv": "date,note\n",
             "fii_dii.csv": "date,fii_net_cr,dii_net_cr\n"}


def settings_with_tmp_data(tmp_path: Path, create_files: bool = True, **overrides) -> Settings:
    """Config pointing at CSV files in tmp_path. By default each file is created with
    just its header (an intentionally empty list); a missing file would mean UNKNOWN."""
    if create_files:
        for name, header in EMPTY_CSV.items():
            if not (tmp_path / name).exists():
                (tmp_path / name).write_text(header)
    raw = load_settings().model_dump()
    raw["calendar"] = {"events_csv": str(tmp_path / "events.csv"),
                       "holidays_csv": str(tmp_path / "holidays.csv"),
                       "fii_dii_csv": str(tmp_path / "fii_dii.csv")}
    raw["history"]["sim_seed_on_start"] = False
    raw["feed"]["mode"] = "sim"          # independent of the user's feed.mode
    for k, v in overrides.items():
        raw[k] = {**raw.get(k, {}), **v} if isinstance(v, dict) else v
    return Settings.model_validate(raw)


# ---------------- pure classify() inputs ----------------
def index_history(n: int = 60, start: float = 20000.0, step: float = 20.0) -> list[DailyBar]:
    """Steady trend; every bar's high-low is 100 and gaps are small, so ATR(14) = 100."""
    out, d = [], SESSION - timedelta(days=n + 30)
    c = start
    for _ in range(n):
        d += timedelta(days=1)
        c += step
        out.append(DailyBar(d, c - step / 2, c + 50, c - 50, c))
    return out


def vix_history(n: int = 250) -> list[float]:
    """Values 10.0 .. 19.9 repeating; 80th percentile is about 18."""
    return [10 + (i % 100) / 10 for i in range(n)]


def stock_data(n_above: int, n_total: int = 5, missing: int = 0) -> dict:
    closes = [100.0 + i for i in range(20)]          # 20-day average = 109.5
    out = {}
    for i, sym in enumerate(STOCKS[:n_total]):
        if i < missing:
            out[sym] = (closes, None)
        elif i < missing + n_above:
            out[sym] = (closes, 125.0)
        else:
            out[sym] = (closes, 90.0)
    return out


def make_inputs(trend: str = "up", **kw) -> RegimeInputs:
    if trend == "up":
        hist = index_history(step=20.0)
        price = hist[-1].close + 20
        stocks = stock_data(n_above=4)
    elif trend == "down":
        hist = index_history(start=22000.0, step=-20.0)
        price = hist[-1].close - 20
        stocks = stock_data(n_above=1)
    else:  # flat: price between the averages
        hist = index_history(step=20.0)
        sma20 = sum(b.close for b in hist[-20:]) / 20
        sma50 = sum(b.close for b in hist[-50:]) / 50
        price = (sma20 + sma50) / 2
        stocks = stock_data(n_above=3)
    base = dict(session_date=SESSION, index_symbol="NIFTY", index_history=hist,
                index_price=price, index_day_high=price + 40, index_day_low=price - 60,
                vix_history=vix_history(), vix_now=12.0, stocks=stocks)
    base.update(kw)
    return RegimeInputs(**base)


# ---------------- stored history + live ticks for RegimeService ----------------
def trading_days_before(cal: MarketCalendar, d: date, n: int) -> list[date]:
    out = []
    for _ in range(n):
        d = cal.previous_trading_day(d)
        out.append(d)
    return list(reversed(out))


def store_history(store: Store, cal: MarketCalendar, end_before: date, n: int = 260,
                  source: str = "breeze", trend: float = 10.0) -> None:
    """Daily bars for every watchlist symbol for n trading days before end_before."""
    days = trading_days_before(cal, end_before, n)
    plan = {"NIFTY": 20000.0, "BANKNIFTY": 45000.0}
    for sym in ["NIFTY", "BANKNIFTY", *STOCKS]:
        start = plan.get(sym, 1000.0)
        step = trend if sym in plan else trend / 20
        bars = []
        for i, d in enumerate(days):
            c = start + step * i
            bars.append(Bar(sym, daily_start(d), c - step / 2, c + 50 * (start / 20000), c - 50 * (start / 20000), c, None))
        store.save_history_bars("bars_1d", bars, source)
    vix = [Bar("INDIAVIX", daily_start(d), v, v + 0.5, v - 0.5, v, None)
           for d, v in zip(days, vix_history(len(days)))]
    store.save_history_bars("bars_1d", vix, source)


def live_ticks(hub, at: datetime, nifty: float, vix: float = 12.0, stock_price: float = 5000.0,
               skip: tuple[str, ...] = ()) -> None:
    for sym, px in [("NIFTY", nifty), ("BANKNIFTY", 47000.0), ("INDIAVIX", vix),
                    *[(s, stock_price) for s in STOCKS]]:
        if sym in skip:
            continue
        hub.on_tick(Tick(sym, px, received_at=at, source="breeze",
                         day_high=px * 1.001, day_low=px * 0.999))
