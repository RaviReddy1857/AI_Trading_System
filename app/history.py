"""Historical daily and 5-minute bars for every watchlist symbol.

Two sources share one code path:
- BreezeHistorySource: calls BreezeFeed.historical() (read-only data call).
- SimHistorySource: generates deterministic fake bars in the same raw format, so
  tests and sim mode run offline. Sim bars are stored with source='sim' and are
  never mixed with real ('breeze') bars.

Every raw row passes a bar-quality check; bad rows are written to quality_events
(reason BAD_BAR) and dropped. Incomplete bars (today's daily bar before the close,
a 5-minute bar still forming) are never stored.

Run a live download by hand:  python -m app.history
"""
from __future__ import annotations

import logging
import math
import random
import time as _time
import zlib
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

from .calendar import MarketCalendar
from .config import Instrument, Settings
from .models import Bar, utcnow
from .quality import IST, MARKET_CLOSE, MARKET_OPEN
from .store import Store

log = logging.getLogger("history")

DAILY, INTRADAY = "1day", "5minute"
TABLE = {DAILY: "bars_1d", INTRADAY: "bars_5m"}
CHUNK_DAYS = {DAILY: 900, INTRADAY: 10}     # Breeze returns at most ~1000 candles per call
BAD_BAR = "BAD_BAR"
FIVE_MIN = timedelta(minutes=5)


def _num(v) -> float | None:
    """Parse a number. NaN, +inf and -inf count as missing (math.isfinite)."""
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _parse_dt(text) -> datetime | None:
    """Breeze sends 'YYYY-MM-DD HH:MM:SS' in IST. Aware strings are converted."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def daily_start(d: date) -> datetime:
    """Daily bars are keyed by the session open, 09:15 IST, stored in UTC."""
    return datetime.combine(d, MARKET_OPEN, IST).astimezone(timezone.utc)


def bar_date(bar: Bar) -> date:
    return bar.start.astimezone(IST).date()


def parse_rows(rows: list[dict], symbol: str, kind: str, interval: str) -> tuple[list[Bar], list[tuple[str, dict]]]:
    """Convert raw Breeze history rows into Bars. Returns (good bars, rejected rows)."""
    good: dict[datetime, Bar] = {}
    bad: list[tuple[str, dict]] = []
    for raw in rows or []:
        dt = _parse_dt(raw.get("datetime"))
        o, h, l, c = (_num(raw.get(k)) for k in ("open", "high", "low", "close"))
        if dt is None:
            bad.append(("no timestamp", raw))
            continue
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            bad.append(("missing or non-positive price", raw))
            continue
        if h < max(o, c, l) or l > min(o, c):
            bad.append(("high/low inconsistent with open/close", raw))
            continue
        vol = _num(raw.get("volume"))
        if kind == "index":
            vol = None                       # index volume is not meaningful
        elif vol is not None and vol < 0:
            bad.append(("negative volume", raw))
            continue
        if interval == DAILY:
            start = daily_start(dt.astimezone(IST).date())
        else:
            start = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
        good[start] = Bar(symbol, start, o, h, l, c, vol)
    return [good[k] for k in sorted(good)], bad


def is_complete(bar: Bar, interval: str, now_utc: datetime) -> bool:
    if interval == INTRADAY:
        return bar.start + FIVE_MIN <= now_utc
    now_ist = now_utc.astimezone(IST)
    d = bar_date(bar)
    return d < now_ist.date() or (d == now_ist.date() and now_ist.time() >= MARKET_CLOSE)


# ---------------- sources ----------------
class BreezeHistorySource:
    name = "breeze"
    remote = True         # real API calls: paced, and empty replies are retried

    def __init__(self, feed):
        self._feed = feed     # BreezeFeed; the SDK stays inside breeze_feed.py

    def fetch(self, inst: Instrument, interval: str, start: datetime, end: datetime) -> list[dict]:
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        return self._feed.historical(inst, interval, start.astimezone(timezone.utc).strftime(fmt),
                                     end.astimezone(timezone.utc).strftime(fmt))


class SimHistorySource:
    """Deterministic fake history. The bars for a given symbol and date are always
    the same, whatever range is requested. NOT real market data."""
    name = "sim"
    remote = False
    ANCHOR = date(2024, 1, 1)
    START = {"NIFTY": 21700.0, "BANKNIFTY": 48000.0, "INDIAVIX": 14.0}
    STEPS = 75                                   # 5-minute bars per session

    def __init__(self):
        self._cache: dict[str, dict[date, list[tuple]]] = {}

    @staticmethod
    def _seed(*parts) -> int:
        return zlib.crc32(":".join(map(str, parts)).encode())

    def _start_price(self, symbol: str) -> float:
        if symbol in self.START:
            return self.START[symbol]
        return 800 + self._seed(symbol) % 2700

    def _block_drift(self, key: str, d: date) -> float:
        block = (d - self.ANCHOR).days // 45     # trends last about six weeks
        return random.Random(self._seed("drift", key, block)).gauss(0, 0.0009)

    def _days(self, symbol: str, until: date) -> dict[date, list[tuple]]:
        """5-minute (open, high, low, close, volume) tuples per weekday from ANCHOR."""
        cached = self._cache.get(symbol)
        if cached and max(cached) >= until:
            return cached
        is_vix = symbol == "INDIAVIX"
        out: dict[date, list[tuple]] = {}
        price = self._start_price(symbol)
        d = self.ANCHOR
        while d <= until:
            if d.weekday() < 5:
                rng = random.Random(self._seed(symbol, d.isoformat()))
                bars = []
                if is_vix:
                    price = max(8.0, price + 0.08 * (14.0 - price) + rng.gauss(0, 0.6))
                    p = price
                    for _ in range(self.STEPS):
                        o = p
                        p = max(7.5, p * (1 + rng.gauss(0, 0.004)))
                        hi, lo = max(o, p) * (1 + abs(rng.gauss(0, 0.001))), min(o, p) * (1 - abs(rng.gauss(0, 0.001)))
                        bars.append((o, hi, lo, p, 0.0))
                else:
                    drift = (self._block_drift("MARKET", d) + 0.5 * self._block_drift(symbol, d)) / self.STEPS
                    p = price * (1 + rng.gauss(0, 0.002))      # overnight gap
                    vol_scale = 1.0 if symbol in self.START else 2000.0
                    for _ in range(self.STEPS):
                        o = p
                        p = p * (1 + drift + rng.gauss(0, 0.0011))
                        hi = max(o, p) * (1 + abs(rng.gauss(0, 0.0003)))
                        lo = min(o, p) * (1 - abs(rng.gauss(0, 0.0003)))
                        bars.append((o, hi, lo, p, float(int(rng.uniform(0.5, 1.5) * vol_scale))))
                    price = p
                out[d] = bars
            d += timedelta(days=1)
        self._cache[symbol] = out
        return out

    def fetch(self, inst: Instrument, interval: str, start: datetime, end: datetime) -> list[dict]:
        s_ist, e_ist = start.astimezone(IST), end.astimezone(IST)
        days = self._days(inst.symbol, e_ist.date())
        rows: list[dict] = []
        for d in sorted(days):
            if d < s_ist.date() or d > e_ist.date():
                continue
            five = days[d]
            if interval == DAILY:
                rows.append({"datetime": f"{d.isoformat()} 00:00:00",
                             "open": round(five[0][0], 2), "high": round(max(b[1] for b in five), 2),
                             "low": round(min(b[2] for b in five), 2), "close": round(five[-1][3], 2),
                             "volume": sum(b[4] for b in five), "stock_code": inst.breeze_code})
            else:
                t0 = datetime.combine(d, MARKET_OPEN, IST)
                for i, (o, h, l, c, v) in enumerate(five):
                    ts = t0 + i * FIVE_MIN
                    if start <= ts <= end:
                        rows.append({"datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
                                     "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
                                     "close": round(c, 2), "volume": v, "stock_code": inst.breeze_code})
        return rows


# ---------------- loader ----------------
class HistoryLoader:
    def __init__(self, settings: Settings, store: Store, source, calendar: MarketCalendar | None = None,
                 sleep: Callable[[float], None] = _time.sleep):
        self.settings = settings
        self.store = store
        self.source = source
        self.calendar = calendar or MarketCalendar()
        self._sleep = sleep
        self._remote = bool(getattr(source, "remote", False))
        self._requests = 0

    def _window(self, inst: Instrument, interval: str, now: datetime) -> tuple[datetime, datetime]:
        days = self.settings.history.daily_days if interval == DAILY else self.settings.history.intraday_days
        start = now - timedelta(days=days)
        last = self.store.last_history_start(TABLE[interval], inst.symbol, self.source.name)
        if last is not None and last > start:
            # re-fetch from the start of the last stored day so a partial day is completed
            start = datetime.combine(last.astimezone(IST).date(), time(0, 0), IST).astimezone(timezone.utc)
        return start, now

    def last_complete_bar_start(self, interval: str, now: datetime) -> datetime | None:
        """Start of the newest bar that should already be complete. None for 5-minute bars
        while the market is open (a newer bar is always forming then)."""
        now_ist = now.astimezone(IST)
        today = now_ist.date()
        if self.calendar.is_trading_day(today) and now_ist.time() >= MARKET_CLOSE:
            session = today
        else:
            session = self.calendar.previous_trading_day(today)
        if interval == DAILY:
            return daily_start(session)
        if self.calendar.is_open(now):
            return None
        return datetime.combine(session, MARKET_CLOSE, IST).astimezone(timezone.utc) - FIVE_MIN

    def is_up_to_date(self, inst: Instrument, interval: str, now: datetime) -> bool:
        """True when the store already holds the newest complete bar, so no request is needed."""
        need = self.last_complete_bar_start(interval, now)
        last = self.store.last_history_start(TABLE[interval], inst.symbol, self.source.name)
        return need is not None and last is not None and last >= need

    def _fetch(self, inst: Instrument, interval: str, a: datetime, b: datetime) -> list[dict]:
        """One request. For a remote source: pause between requests (rate limit) and ask
        again when a reply is empty. An empty reply after the retries is accepted (the
        range may hold no trading day) and reported."""
        h = self.settings.history
        tries = 1 + (h.empty_retries if self._remote else 0)
        rows: list[dict] = []
        for attempt in range(tries):
            if self._remote:
                if attempt:
                    self._sleep(h.retry_pause_seconds)
                elif self._requests and h.request_pause_seconds:
                    self._sleep(h.request_pause_seconds)
            self._requests += 1
            rows = self.source.fetch(inst, interval, a, b)
            if rows:
                return rows
            if attempt + 1 < tries:
                log.info("empty reply for %s %s %s..%s; retry %d of %d", inst.symbol, interval,
                         a.isoformat(), b.isoformat(), attempt + 1, tries - 1)
        return rows

    def update_symbol(self, inst: Instrument, interval: str, now: datetime) -> dict:
        if self.is_up_to_date(inst, interval, now):
            return {"saved": 0, "rejected": 0, "skipped": "already stored"}
        start, end = self._window(inst, interval, now)
        raw: list[dict] = []
        chunk = timedelta(days=CHUNK_DAYS[interval])
        a = start
        while a < end:
            b = min(a + chunk, end)
            raw += self._fetch(inst, interval, a, b)
            a = b
        bars, bad = parse_rows(raw, inst.symbol, inst.kind, interval)
        for reason, row in bad:
            self.store.save_quality_event(now, inst.symbol, BAD_BAR, _num(row.get("close")),
                                          None, None, f"{self.source.name}:{interval}:{reason}")
        complete = [b for b in bars if is_complete(b, interval, now)
                    and self.calendar.is_trading_day(bar_date(b))]
        saved = self.store.save_history_bars(TABLE[interval], complete, self.source.name)
        out = {"saved": saved, "rejected": len(bad)}
        if not raw:
            out["empty"] = True
        return out

    def update(self, now: datetime | None = None, symbols: set[str] | None = None) -> dict:
        """Download/refresh history for every watchlist symbol (or only `symbols`).
        Symbols already up to date are skipped without a request. One symbol failing
        does not stop the others; failures are reported."""
        now = now or utcnow()
        summary: dict = {}
        for inst in self.settings.watchlist:
            if symbols is not None and inst.symbol not in symbols:
                continue
            for interval in (DAILY, INTRADAY):
                key = f"{inst.symbol}:{interval}"
                try:
                    summary[key] = self.update_symbol(inst, interval, now)
                except Exception as e:  # network/SDK errors must not kill the app
                    log.exception("history download failed for %s", key)
                    summary[key] = {"error": f"{type(e).__name__}: {e}"}
        return summary


def seed_sim_history(settings: Settings, store: Store, calendar: MarketCalendar,
                     now: datetime | None = None) -> dict:
    """Sim mode: fill history up to the last completed session (never today)."""
    now = now or utcnow()
    today_open = datetime.combine(now.astimezone(IST).date(), time(0, 0), IST).astimezone(timezone.utc)
    return HistoryLoader(settings, store, SimHistorySource(), calendar).update(today_open)


def summarize(summary: dict) -> str:
    """Result of a download: counts plus every failed or empty symbol."""
    skipped = [k for k, v in summary.items() if v.get("skipped")]
    errors = {k: v["error"] for k, v in summary.items() if "error" in v}
    empty = [k for k, v in summary.items() if v.get("empty")]
    saved = sum(v.get("saved", 0) for v in summary.values())
    lines = [f"{len(summary)} downloads: {saved} bars saved, {len(skipped)} already stored (skipped), "
             f"{len(errors)} failed, {len(empty)} empty after retries"]
    lines += [f"  FAILED {k}: {e}" for k, e in errors.items()]
    lines += [f"  EMPTY  {k}" for k in empty]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """Manual live download: python -m app.history [SYMBOL ...]"""
    import sys

    from dotenv import load_dotenv

    from .config import ROOT, load_settings
    from .feeds.breeze_feed import BreezeFeed
    from .manual_data import ManualData

    args = sys.argv[1:] if argv is None else argv
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    wanted = {a.upper() for a in args} or None
    unknown = sorted((wanted or set()) - {i.symbol for i in settings.watchlist})
    if unknown:
        raise SystemExit(f"not in the watchlist: {unknown}")
    # Breeze data always goes to the live file, whatever feed.mode says.
    store = Store(settings.path(settings.storage.db_path_live), live=True)
    calendar = MarketCalendar()
    ManualData(settings, store, calendar).load_all()
    feed = BreezeFeed(settings.watchlist)
    summary = HistoryLoader(settings, store, BreezeHistorySource(feed), calendar).update(symbols=wanted)
    for key, res in summary.items():
        print(f"{key:28s} {res}")
    print(summarize(summary))


if __name__ == "__main__":
    main()
