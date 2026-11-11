"""NSE trading calendar: weekends plus the holidays the user keeps in data/holidays.csv.

Market hours themselves live in app/quality.py; this module only knows which
dates are trading days."""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from .quality import IST, MARKET_CLOSE, is_market_hours


class MarketCalendar:
    def __init__(self, holidays: dict[date, str] | None = None):
        self._lock = threading.Lock()
        self._holidays: dict[date, str] = dict(holidays or {})

    def set_holidays(self, holidays: dict[date, str]) -> None:
        with self._lock:
            self._holidays = dict(holidays)

    @property
    def holidays(self) -> frozenset[date]:
        with self._lock:
            return frozenset(self._holidays)

    def holiday_name(self, d: date) -> str | None:
        with self._lock:
            return self._holidays.get(d)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def previous_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def is_open(self, now_utc: datetime) -> bool:
        return is_market_hours(now_utc, self.holidays)

    def target_session(self, now_utc: datetime) -> date:
        """The trading day the regime and bias are for: today until the close on a
        trading day, otherwise the next trading day."""
        now = now_utc.astimezone(IST)
        today = now.date()
        if self.is_trading_day(today) and now.time() <= MARKET_CLOSE:
            return today
        return self.next_trading_day(today)
