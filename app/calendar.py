"""NSE trading calendar: weekends plus the holidays the user keeps in data/holidays.csv.

Hours per segment and date live in app/session.py (closing auction from 3 Aug 2026); this module knows which dates
are trading days and asks the session calendar for the hours of a trading day."""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from .quality import IST, is_market_hours
from .session import CLOSED, Hours, calendar as session_calendar


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

    def is_open(self, now_utc: datetime, segment: str | None = None, continuous: bool = False) -> bool:
        """Some segment trading (no `segment`), or that segment open (`continuous`: in continuous trading only)."""
        return is_market_hours(now_utc, self.holidays, segment, continuous)

    def phase(self, segment: str, now_utc: datetime) -> str:
        """OPEN, AUCTION or CLOSED (app/session.py), CLOSED on weekends and holidays."""
        return session_calendar().phase(segment, now_utc, self.holidays)

    def hours(self, segment: str, day: date) -> Hours:
        return session_calendar().hours(segment, day)

    def last_close_utc(self, day: date) -> datetime:
        """The last close of any segment on `day` (UTC): the end of the market day."""
        from .session import at_utc
        return at_utc(day, session_calendar().latest_close(day))

    def target_session(self, now_utc: datetime) -> date:
        """The trading day the regime and bias are for: today until the last close on a
        trading day, otherwise the next trading day."""
        now = now_utc.astimezone(IST)
        today = now.date()
        if self.is_trading_day(today) and now.time() <= session_calendar().latest_close(today):
            return today
        return self.next_trading_day(today)
