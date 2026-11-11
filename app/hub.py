"""MarketHub: every tick passes the quality gate, then feeds bars and the
latest-price table. Bad ticks are logged and dropped."""
from __future__ import annotations

import threading
from datetime import datetime

from .bars import BarBuilder
from .config import Settings
from .models import Tick, utcnow
from .quality import check_tick, freshness
from .store import Store


class MarketHub:
    def __init__(self, settings: Settings, store: Store, feed_name: str):
        self.settings = settings
        self.store = store
        self.feed_name = feed_name
        self._lock = threading.Lock()
        self._last_good: dict[str, Tick] = {}
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
            self._bars.add(tick)

    def snapshot(self, now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        enforce_hours = self.feed_name != "sim"
        rows = []
        with self._lock:
            for inst in self.settings.watchlist:
                t = self._last_good.get(inst.symbol)
                status = freshness(t.received_at if t else None, now,
                                   self.settings.feed.stale_after_seconds, enforce_hours)
                change = None
                if t and t.prev_close:
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

    def stats(self) -> dict:
        with self._lock:
            return {"good_ticks": self._good_count, "quarantined_ticks": self._bad_count}

    def close(self) -> None:
        with self._lock:
            self._bars.flush()
