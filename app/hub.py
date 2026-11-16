"""MarketHub: every tick passes the quality gate, then feeds bars and the
latest-price table. Bad ticks are logged and dropped."""
from __future__ import annotations

import threading
from datetime import datetime

from .bars import BarBuilder
from .calendar import MarketCalendar
from .config import Settings
from .models import Tick, utcnow
from .quality import IST, is_finite, check_tick, freshness
from .store import Store


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
        self._bad_count = 0
        self._bars = BarBuilder(lambda bar: store.save_bar(bar, feed_name))

    def on_tick(self, tick: Tick) -> None:
        with self._lock:
            reasons = check_tick(tick, self._last_good.get(tick.symbol),
                                 self.settings.feed.max_jump_pct)
            if reasons:
                self._bad_count += 1
                for r in reasons:
                    self.store.save_quality_event(tick.received_at, tick.symbol, r,
                                                  tick.last, tick.bid, tick.ask, tick.source)
                return
            self._good_count += 1
            self._last_good[tick.symbol] = tick
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

    def _status(self, t: Tick | None, now: datetime) -> str:
        return freshness(t.received_at if t else None, now,
                         self.settings.feed.stale_after_seconds, self.enforce_hours,
                         self.calendar.holidays)

    def snapshot(self, now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        rows = []
        with self._lock:
            for inst in self.settings.watchlist:
                t = self._last_good.get(inst.symbol)
                status = self._status(t, now)
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
            if self._status(t, now) != "LIVE":
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
            status = self._status(t, now)
            if t is None:
                return {"status": status, "bid": None, "ask": None, "last": None, "at": None}
            return {"status": status, "bid": t.bid, "ask": t.ask, "last": t.last, "at": t.received_at}

    def last_tick_at(self) -> datetime | None:
        """Time of the newest good tick of any symbol (None before the first)."""
        with self._lock:
            return max((t.received_at for t in self._last_good.values()), default=None)

    def stats(self) -> dict:
        with self._lock:
            return {"good_ticks": self._good_count, "quarantined_ticks": self._bad_count}

    def close(self) -> None:
        with self._lock:
            self._bars.flush()
