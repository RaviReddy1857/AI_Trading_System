"""MarketHub: every tick passes the quality gate, then feeds bars and the
latest-price table. Bad ticks are logged and dropped.

It also keeps the first good tick of every 1-minute bucket of each symbol (the last hour in memory, the last day in
the database, so a restart does not lose it), so the paper trader fills a pending trade at the first quote after the
bar close (`first_quote`) however late its poll runs, never at a later tick. When that capture was lost (the app was
not receiving quotes at the planned fill: stopped, or started after it), the first quote received after is returned
as a substitute, marked `substitute`, never as the first quote.

Closing auction (from 3 Aug 2026, app/session.py): a tick of an F&O stock or the index received while its segment is
in the auction is an auction indication (stock) or an indicative value (index), not a trade. It is kept apart
(`auction`, shown as AUCTION on the dashboard) and is never checked as a trade quote (no quality event), never part
of a bar, never a first quote for a paper fill and never the last good tick; the symbol's status is AUCTION, which is
not usable. Live (non-sim) feeds only, like the market-hours rule."""
from __future__ import annotations

import threading
from collections import OrderedDict
import logging
from datetime import datetime, timedelta

from .bars import BarBuilder
from .calendar import MarketCalendar
from .config import Settings
from .models import Tick, utcnow
from .quality import IST, is_finite, check_tick, freshness
from .session import AUCTION, calendar as session_calendar, segment_of
from .store import Store

log = logging.getLogger("hub")


class MarketHub:
    def __init__(self, settings: Settings, store: Store, feed_name: str,
                 calendar: MarketCalendar | None = None):
        self.settings = settings
        self.store = store
        self.feed_name = feed_name
        self.calendar = calendar or MarketCalendar()
        self._lock = threading.Lock()
        self._last_good: dict[str, Tick] = {}
        # day range seen from good ticks: symbol -> (IST date, high, low, first tick time)
        self._seen_range: dict[str, tuple] = {}
        self._good_count = 0
        self._first_tick_at = None
        self._bad_count = 0
        self._bars = BarBuilder(lambda bar: store.save_bar(bar, feed_name))
        # symbol -> {minute start: first good tick at or after it, within that minute}
        self._first: dict[str, OrderedDict] = {}
        self._segment = {i.symbol: segment_of(i.kind, getattr(i, "fno", True)) for i in settings.watchlist}
        self._auction: dict[str, Tick] = {}       # the latest closing-auction tick per symbol (never a trade)
        # symbol -> (first rejection since the last valid tick, latest rejection, its reasons): ticks the quality gate
        # rejected (crossed, non-finite, quarantined). Never a price, a bar or a fill; cleared by a newer valid tick
        self._rejected: dict[str, tuple[datetime, datetime, str]] = {}
        self._auction_count = 0

    def segment(self, symbol: str) -> str:
        from .session import CASH_FNO
        return self._segment.get(symbol, CASH_FNO)

    def in_auction(self, symbol: str, at: datetime) -> bool:
        """True when the symbol's segment is in its closing auction at `at` (live feeds only)."""
        return self.enforce_hours and session_calendar().phase(self.segment(symbol), at,
                                                               self.calendar.holidays) == AUCTION

    _FIRST_KEEP = 60                  # minutes of first-of-minute ticks kept per symbol

    def on_tick(self, tick: Tick) -> None:
        with self._lock:
            if self.in_auction(tick.symbol, tick.received_at):
                self._auction[tick.symbol] = tick          # an auction indication: not a trade, not a quality event
                self._auction_count += 1
                return
            reasons = check_tick(tick, self._last_good.get(tick.symbol),
                                 self.settings.feed.max_jump_pct)
            if reasons:
                self._bad_count += 1
                prev = self._rejected.get(tick.symbol)
                self._rejected[tick.symbol] = (prev[0] if prev else tick.received_at, tick.received_at,
                                               ", ".join(reasons))
                for r in reasons:
                    self.store.save_quality_event(tick.received_at, tick.symbol, r,
                                                  tick.last, tick.bid, tick.ask, tick.source)
                return
            self._good_count += 1
            self._rejected.pop(tick.symbol, None)          # a newer valid tick ends the rejection
            if self._first_tick_at is None:
                self._first_tick_at = tick.received_at
            self._last_good[tick.symbol] = tick
            minute = tick.received_at.replace(second=0, microsecond=0)
            firsts = self._first.setdefault(tick.symbol, OrderedDict())
            if minute not in firsts:
                firsts[minute] = tick
                while len(firsts) > self._FIRST_KEEP:
                    firsts.popitem(last=False)
                try:                                   # kept across a restart; a write failure never drops the tick
                    self.store.save_first_quote(tick.symbol, minute, tick.received_at, tick.bid, tick.ask, tick.last,
                                                self.feed_name, keep_after=minute - timedelta(days=1))
                except Exception:
                    log.exception("first quote of %s at %s not saved", tick.symbol, minute)
            day = tick.received_at.astimezone(IST).date()
            seen = self._seen_range.get(tick.symbol)
            if seen is None or seen[0] != day:
                self._seen_range[tick.symbol] = (day, tick.last, tick.last, tick.received_at)
            else:
                self._seen_range[tick.symbol] = (day, max(seen[1], tick.last),
                                                 min(seen[2], tick.last), seen[3])
            self._bars.add(tick)

    @property
    def enforce_hours(self) -> bool:
        return self.feed_name != "sim"

    def _status(self, t: Tick | None, now: datetime, symbol: str | None = None) -> str:
        return freshness(t.received_at if t else None, now,
                         self.settings.feed.stale_after_seconds, self.enforce_hours,
                         self.calendar.holidays, self.segment(symbol) if symbol else None)

    def snapshot(self, now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        rows = []
        with self._lock:
            for inst in self.settings.watchlist:
                t = self._last_good.get(inst.symbol)
                status = self._status(t, now, inst.symbol)
                if status == "AUCTION":
                    a = self._auction.get(inst.symbol)
                    if a is not None and a.received_at.astimezone(IST).date() == now.astimezone(IST).date():
                        t = a                              # shown: the latest auction indication (not usable)
                change = None
                if t and is_finite(t.prev_close) and t.prev_close > 0:
                    change = round((t.last - t.prev_close) / t.prev_close * 100, 2)
                rows.append({
                    "symbol": inst.symbol, "kind": inst.kind,
                    "last": t.last if t else None,
                    "bid": t.bid if t else None, "ask": t.ask if t else None,
                    "change_pct": change,
                    "age_seconds": round((now - t.received_at).total_seconds(), 1) if t else None,
                    "status": status,
                    # A price is usable by strategies only when it is LIVE.
                    "usable": status == "LIVE",
                })
        return rows

    def live_quote(self, symbol: str, now: datetime | None = None) -> dict | None:
        """The latest good tick for a symbol, only if its status is LIVE; else None.

        day_high/day_low come from the feed's own session high/low when it reports
        a consistent pair, otherwise from good ticks seen since the first tick today
        (range_source says which)."""
        now = now or utcnow()
        with self._lock:
            t = self._last_good.get(symbol)
            if self._status(t, now, symbol) != "LIVE":
                return None
            hi, lo, src = None, None, None
            if (is_finite(t.day_high) and is_finite(t.day_low)
                    and 0 < t.day_low <= t.last <= t.day_high):
                hi, lo, src = t.day_high, t.day_low, "feed"
            else:
                seen = self._seen_range.get(symbol)
                if seen and seen[0] == t.received_at.astimezone(IST).date():
                    hi, lo = seen[1], seen[2]
                    src = "ticks since " + seen[3].astimezone(IST).strftime("%H:%M IST")
            return {"last": t.last, "at": t.received_at, "day_high": hi, "day_low": lo,
                    "range_source": src}

    def quote(self, symbol: str, now: datetime | None = None) -> dict:
        """The last good tick of a symbol WITH its status (LIVE, STALE, NO_DATA, CLOSED), for execution
        records: a stale or missing quote is reported as such, never as zero and never filled in."""
        now = now or utcnow()
        with self._lock:
            t = self._last_good.get(symbol)
            status = self._status(t, now, symbol)
            rej = self._rejected.get(symbol)
            # the latest rejected tick since the last valid one (never its price): for the settlement alert
            rejected = {"rejected_since": rej[0], "rejected_at": rej[1], "rejected_why": rej[2]} if rej else \
                {"rejected_since": None, "rejected_at": None, "rejected_why": None}
            if t is None:
                return {"status": status, "bid": None, "ask": None, "last": None, "at": None, **rejected}
            return {"status": status, "bid": t.bid, "ask": t.ask, "last": t.last, "at": t.received_at, **rejected}

    def first_quote(self, symbol: str, after: datetime) -> dict | None:
        """The first good tick received at or after `after` (a bar close: a minute boundary), with its status as it
        was at its own time (LIVE inside market hours); None when none was received yet. Looked up in memory (the
        last hour), then in the database (the last day: captures made before a restart). For an `after` inside a
        minute, a tick in that minute before `after` is skipped and the first tick of a later minute is used.

        `substitute` is True when the capture was lost: this run's first tick came after `after` and nothing was
        kept from before, so the quote returned is the first one received after the gap, not the first quote."""
        floor = after.replace(second=0, microsecond=0)
        with self._lock:
            firsts = self._first.get(symbol) or {}
            t = next((x for m, x in firsts.items() if m >= floor and x.received_at >= after), None)
            started = self._first_tick_at
        found = {"at": t.received_at, "bid": t.bid, "ask": t.ask, "last": t.last} if t is not None else None
        try:
            kept = self.store.first_quote_after(symbol, after)
        except Exception:
            log.exception("kept first quotes of %s not read", symbol)
            kept = None
        if kept is not None and (found is None or kept["at"] < found["at"]):
            found = kept
        if found is None:
            return None
        substitute = started is not None and started > after and found["at"] >= started
        return {"status": self._status_at(found["at"], symbol), "bid": found["bid"], "ask": found["ask"],
                "last": found["last"], "at": found["at"], "substitute": substitute}

    def _status_at(self, at: datetime, symbol: str | None = None) -> str:
        return freshness(at, at, self.settings.feed.stale_after_seconds, self.enforce_hours, self.calendar.holidays,
                         self.segment(symbol) if symbol else None)

    def first_tick_at(self) -> datetime | None:
        """Time of the first good tick since the app started (the "data from" of a restart)."""
        with self._lock:
            return self._first_tick_at

    def last_tick_at(self) -> datetime | None:
        """Time of the newest good tick of any symbol (None before the first)."""
        with self._lock:
            return max((t.received_at for t in self._last_good.values()), default=None)

    def stats(self) -> dict:
        with self._lock:
            return {"good_ticks": self._good_count, "quarantined_ticks": self._bad_count,
                    "auction_ticks": self._auction_count}

    def close(self) -> None:
        with self._lock:
            self._bars.flush()
