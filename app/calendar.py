"""NSE trading calendar: weekends plus the holidays the user keeps in data/holidays.csv.

Hours per segment and date live in app/session.py (closing auction from 3 Aug 2026); this module knows which dates
are trading days and asks the session calendar for the hours of a trading day."""
from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta

from .quality import IST, is_market_hours
from .session import CLOSED, Hours, calendar as session_calendar


COVERAGE = "COVERAGE"
# the whole description: "COVERAGE through YYYY-MM-DD" from its start, then optionally a source after a space
# ("COVERAGE through 2027-12-31 NSE circular 123"). Anything else ("COVERAGE NOT VERIFIED through ...") is no coverage.
_THROUGH = re.compile(r"COVERAGE\s+through\s+(\d{4}-\d{2}-\d{2})(?:\s+\S.*)?", re.I | re.S)


def coverage_through(note: str | None) -> date | None:
    """The end date of a COVERAGE row's description, which must read "COVERAGE through YYYY-MM-DD" from its start
    with an optional trailing source; None for anything else (the list is then not verified complete)."""
    m = _THROUGH.fullmatch((note or "").strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def is_coverage_row(note: str | None) -> bool:
    return (note or "").strip().upper().startswith(COVERAGE)


class MarketCalendar:
    """Holidays and the VERIFIED coverage of the holiday list, kept apart. A row of data/holidays.csv whose description
    starts with COVERAGE is not a holiday: it declares that the list is complete from its date through the date in its
    note ("COVERAGE through 2027-12-31 NSE circular ..."). Holiday rows alone never prove coverage: a year with a few
    holidays entered may still miss one."""

    def __init__(self, holidays: dict[date, str] | None = None):
        self._lock = threading.Lock()
        self._holidays: dict[date, str] = {}
        self._coverage: list[tuple[date, date]] = []
        self.set_holidays(holidays or {})

    def set_holidays(self, holidays: dict[date, str]) -> None:
        real, cov = {}, []
        for d, note in dict(holidays).items():
            if is_coverage_row(note):
                end = coverage_through(note)
                if end is not None and end >= d:
                    cov.append((d, end))
            else:
                real[d] = note
        with self._lock:
            self._holidays, self._coverage = real, sorted(cov)

    @property
    def coverage(self) -> tuple[tuple[date, date], ...]:
        with self._lock:
            return tuple(self._coverage)

    def covers(self, first: date, last: date) -> bool:
        """Every day from `first` to `last` lies inside a declared COVERAGE range (ranges may join up)."""
        if last < first:
            return True
        d = first
        for a, b in self.coverage:
            if a <= d <= b:
                d = b + timedelta(days=1)
                if d > last:
                    return True
        return False

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
