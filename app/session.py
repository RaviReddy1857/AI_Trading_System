"""The dated NSE session calendar per segment (IST). Live code and backtests both read it.

Segments:
- CASH_FNO: F&O-eligible stocks in the cash segment (`watchlist[].fno`, every Nifty 50 stock). From 3 Aug 2026
  continuous trading ends 15:15 and a Closing Auction Session (CAS) runs 15:15-15:35 and sets the official close.
- CASH_OTHER: stocks outside F&O: continuous to 15:30.
- INDEX: index values; from 3 Aug 2026 indicative only during CAS (15:15-15:35).
- DERIVATIVES: stock and index options: until 15:40 from 3 Aug 2026.
Before 3 Aug 2026 every segment ran 09:15-15:30. The times are `session.periods` in config.yaml (one period per date
the rules changed; a day before the first period uses the first). Holidays and weekends come from the caller
(`app/calendar.py`).

Phases: OPEN (continuous trading: prices are trades), AUCTION (the closing auction: F&O stock prices are auction
indications and index values are indicative, NOT trades: never a signal, a paper fill, an exit trigger or a quality
event), CLOSED. A segment without an auction is OPEN from its open to its close inclusive (the rule before 3 Aug 2026);
with one, OPEN until (not including) the end of continuous trading and AUCTION from then to the close inclusive.

The calendar in use is set from config.yaml by `app.config.load_settings` (`configure`); until then it holds the
defaults of `app.config.SessionCfg`, which are the same times.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .config import SessionCfg, hhmm

IST = timezone(timedelta(hours=5, minutes=30))

CASH_FNO, CASH_OTHER, INDEX, DERIVATIVES = "CASH_FNO", "CASH_OTHER", "INDEX", "DERIVATIVES"
SEGMENTS = (CASH_FNO, CASH_OTHER, INDEX, DERIVATIVES)
OPEN, AUCTION, CLOSED = "OPEN", "AUCTION", "CLOSED"


def at_utc(day: date, t: time) -> datetime:
    return datetime.combine(day, t, IST).astimezone(timezone.utc)


@dataclass(frozen=True)
class Hours:
    """One segment's hours on one day (IST clock times)."""
    segment: str
    day: date
    open: time
    continuous_end: time
    close: time
    period_from: date

    @property
    def has_auction(self) -> bool:
        return self.close > self.continuous_end

    @property
    def open_utc(self) -> datetime:
        return at_utc(self.day, self.open)

    @property
    def continuous_end_utc(self) -> datetime:
        return at_utc(self.day, self.continuous_end)

    @property
    def close_utc(self) -> datetime:
        return at_utc(self.day, self.close)

    def phase_at(self, t: time) -> str:
        """OPEN, AUCTION or CLOSED at the IST clock time `t` of this day (weekends and holidays not checked)."""
        if t < self.open or t > self.close:
            return CLOSED
        if self.has_auction and t >= self.continuous_end:
            return AUCTION
        return OPEN

    def bar_in_continuous(self, start_utc: datetime, minutes: int = 5) -> bool:
        """True when a bar [start, start + minutes) lies inside continuous trading (a trade bar, not an auction
        or indicative one)."""
        return self.open_utc <= start_utc and start_utc + timedelta(minutes=minutes) <= self.continuous_end_utc

    def describe(self) -> str:
        if self.has_auction:
            return (f"{self.open:%H:%M}-{self.continuous_end:%H:%M} continuous, closing auction "
                    f"{self.continuous_end:%H:%M}-{self.close:%H:%M}")
        return f"{self.open:%H:%M}-{self.close:%H:%M}"


class SessionCalendar:
    def __init__(self, cfg: SessionCfg | None = None):
        self.cfg = cfg or SessionCfg()
        self._periods = sorted(self.cfg.periods, key=lambda p: p.from_date)

    def period(self, day: date):
        out = self._periods[0]
        for p in self._periods:
            if p.from_date <= day:
                out = p
        return out

    def hours(self, segment: str, day: date) -> Hours:
        p = self.period(day)
        o = hhmm(p.open)
        if segment == CASH_FNO:
            ce, cl = hhmm(p.fno_continuous_end), hhmm(p.fno_close)
        elif segment == CASH_OTHER:
            ce = cl = hhmm(p.other_close)
        elif segment == INDEX:
            ce, cl = hhmm(p.index_continuous_end), hhmm(p.index_close)
        elif segment == DERIVATIVES:
            ce = cl = hhmm(p.derivatives_close)
        else:
            raise ValueError(f"unknown segment {segment!r}")
        return Hours(segment, day, o, ce, cl, p.from_date)

    def phase(self, segment: str, now_utc: datetime, holidays: Collection[date] = ()) -> str:
        now = now_utc.astimezone(IST)
        if now.weekday() >= 5 or now.date() in holidays:
            return CLOSED
        return self.hours(segment, now.date()).phase_at(now.time())

    def open_time(self, day: date) -> time:
        return hhmm(self.period(day).open)

    def latest_close(self, day: date) -> time:
        """The last close of any segment on `day` (15:40 from 3 Aug 2026: options; 15:30 before)."""
        return max(self.hours(s, day).close for s in SEGMENTS)

    def cash_close(self, day: date) -> time:
        """The last close of the cash market on `day`: the official close of every stock is set by then (15:35
        from 3 Aug 2026, the end of the closing auction; 15:30 before)."""
        return max(self.hours(CASH_FNO, day).close, self.hours(CASH_OTHER, day).close)

    def any_open(self, now_utc: datetime, holidays: Collection[date] = ()) -> bool:
        """Some segment is trading or in its auction (the market day is not over)."""
        now = now_utc.astimezone(IST)
        if now.weekday() >= 5 or now.date() in holidays:
            return False
        return self.open_time(now.date()) <= now.time() <= self.latest_close(now.date())

    @property
    def intraday_cutoff(self) -> time:
        return hhmm(self.cfg.cash_intraday_cutoff)

    @property
    def cutoff_confirmed(self) -> bool:
        return self.cfg.cash_intraday_cutoff_confirmed


_current = SessionCalendar()


def calendar() -> SessionCalendar:
    """The calendar in use (config.yaml's `session`, once `app.config.load_settings` has run)."""
    return _current


def configure(cfg: SessionCfg) -> SessionCalendar:
    global _current
    _current = SessionCalendar(cfg)
    return _current


def segment_of(kind: str, fno: bool = True) -> str:
    """The cash segment of a watchlist instrument: INDEX for an index, CASH_FNO for an F&O stock, else CASH_OTHER."""
    if kind == "index":
        return INDEX
    return CASH_FNO if fno else CASH_OTHER


def segment_for(settings, symbol: str) -> str:
    """The segment of a watchlist symbol; a symbol not in the watchlist counts as an F&O stock (the stricter
    hours: its auction prices are never taken for trades)."""
    inst = next((i for i in settings.watchlist if i.symbol == symbol), None)
    if inst is None:
        return CASH_FNO
    return segment_of(inst.kind, getattr(inst, "fno", True))


def hours(segment: str, day: date) -> Hours:
    return _current.hours(segment, day)


def close_utc(segment: str, day: date) -> datetime:
    """The close of a segment on `day`, in UTC (an underlying's official close; an option's last trade)."""
    return _current.hours(segment, day).close_utc
