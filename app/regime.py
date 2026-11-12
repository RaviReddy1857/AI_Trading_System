"""Market-regime classifier (versioned, deterministic) and FII/DII next-day bias.

classify() and fii_bias() are pure functions: same inputs, same result. They read
no clock, no network and no database. RegimeService gathers the inputs from the
store, the live hub and the calendar, then calls them.

Rule order (regime_v1):
  1. UNKNOWN   if any required input is missing or not LIVE (never a guess)
  2. EVENT     if the session date has a market-wide event of a configured type
  3. VOLATILE  if India VIX > its N-th percentile over the lookback,
               or NIFTY's day range > mult x ATR
  4. TRENDING_UP / TRENDING_DOWN  if NIFTY is above / below both averages and breadth agrees
  5. RANGE     otherwise
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .calendar import MarketCalendar
from .config import Regime as RegimeParams
from .config import Settings
from .history import bar_date, daily_start
from .hub import MarketHub
from .indicators import after_last_break, atr, breadth, last, percentile, sma
from .manual_data import ManualData
from .models import Bar, utcnow
from .quality import IST, is_finite
from .store import Store

log = logging.getLogger("regime")

RULES_VERSION = "regime_v1"
_ONE_MIN = timedelta(minutes=1)
REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGE", "VOLATILE", "EVENT", "UNKNOWN")
BIASES = ("BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN")


def rules_version(params: RegimeParams) -> str:
    """Rule-set version plus a short hash of the thresholds that were used."""
    blob = json.dumps(params.model_dump(), sort_keys=True).encode()
    return f"{RULES_VERSION}/{hashlib.sha256(blob).hexdigest()[:8]}"


def _positive(x) -> bool:
    """A usable price-like number: finite and above zero."""
    return is_finite(x) and x > 0


def _f(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}"


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass
class RegimeInputs:
    """Everything the rules need. All daily history is for sessions strictly
    before the session being classified (no look-ahead)."""
    session_date: date
    index_symbol: str
    index_history: Sequence[DailyBar]
    index_price: float | None
    index_day_high: float | None
    index_day_low: float | None
    vix_history: Sequence[float]                 # daily closes
    vix_now: float | None
    stocks: Mapping[str, tuple[Sequence[float], float | None]]   # closes, current price
    events: Sequence[Mapping[str, str]] = ()     # rows for session_date: type, symbol, note
    events_error: str | None = None
    data_problems: Sequence[str] = ()            # e.g. history not updated; forces UNKNOWN
    notes: Sequence[str] = ()                    # context added by the caller (not rules)


@dataclass
class RegimeResult:
    regime: str
    reasons: list[str]
    rules_version: str
    values: dict = field(default_factory=dict)


def classify(inp: RegimeInputs, p: RegimeParams) -> RegimeResult:
    version = rules_version(p)
    idx = inp.index_symbol
    hist = list(inp.index_history)
    missing: list[str] = []
    values: dict = {}

    need_index = max(p.sma_slow_days, p.atr_days)
    if len(hist) < need_index:
        missing.append(f"{idx} has {len(hist)} completed daily bars; {need_index} needed")
    if not _positive(inp.index_price):
        missing.append(f"{idx} price is not LIVE")
    if not (_positive(inp.index_day_high) and _positive(inp.index_day_low)):
        missing.append(f"{idx} day high/low is unknown")
    elif inp.index_day_high < inp.index_day_low:
        missing.append(f"{idx} day high {_f(inp.index_day_high)} is below day low {_f(inp.index_day_low)}")
    if not _positive(inp.vix_now):
        missing.append("India VIX is not LIVE")
    bad_hist = [d.date for d in hist
                if not all(_positive(x) for x in (d.open, d.high, d.low, d.close))]
    if bad_hist:
        missing.append(f"{idx} daily history has invalid prices on {', '.join(map(str, bad_hist[:3]))}")
    if not all(_positive(v) for v in inp.vix_history):
        missing.append("India VIX daily history has invalid values")
    if len(inp.vix_history) < p.vix_min_history_days:
        missing.append(f"India VIX has {len(inp.vix_history)} daily closes; "
                       f"{p.vix_min_history_days} needed")
    if inp.events_error:
        missing.append(f"event calendar could not be read ({inp.events_error})")
    missing.extend(inp.data_problems)

    b = breadth(inp.stocks, p.breadth_sma_days)
    coverage = (b.counted / len(inp.stocks) * 100.0) if inp.stocks else 0.0
    values["breadth_pct"] = None if b.pct_above is None else round(b.pct_above, 1)
    values["breadth_coverage_pct"] = round(coverage, 1)
    values["breadth_missing"] = b.missing
    if coverage < p.breadth_min_coverage_pct or b.pct_above is None:
        missing.append(f"breadth covers {b.counted} of {len(inp.stocks)} stocks "
                       f"({coverage:.0f}%); {p.breadth_min_coverage_pct:.0f}% needed"
                       + (f"; missing {', '.join(b.missing)}" if b.missing else ""))

    market_events = [e for e in inp.events
                     if e.get("type") in p.event_types and not e.get("symbol")]
    event_text = [f"{e['type']}{' ' + e['symbol'] if e.get('symbol') else ''}"
                  f"{': ' + e['note'] if e.get('note') else ''}" for e in inp.events]
    values["events"] = event_text

    def unknown(problems: list[str]) -> RegimeResult:
        reasons = ["Not enough usable data, so no regime is assumed: " + m for m in problems]
        if market_events:
            reasons.append(f"Note: {inp.session_date} is also an event day")
        return RegimeResult("UNKNOWN", reasons + list(inp.notes), version, values)

    if missing:
        return unknown(missing)

    closes = [d.close for d in hist]
    sma_fast = last(sma(closes, p.sma_fast_days))
    sma_slow = last(sma(closes, p.sma_slow_days))
    atr_v = last(atr([d.high for d in hist], [d.low for d in hist], closes, p.atr_days))
    vix_window = list(inp.vix_history)[-p.vix_lookback_days:]
    vix_cut = percentile(vix_window, p.vix_percentile)
    # Every value divided by, or compared against, below must be a positive finite number.
    derived = []
    if not _positive(atr_v):
        derived.append(f"{idx} {p.atr_days}-day ATR is zero or unknown, so the day range cannot be judged")
    if not (_positive(sma_fast) and _positive(sma_slow)):
        derived.append(f"{idx} moving averages could not be computed")
    if not _positive(vix_cut):
        derived.append("India VIX percentile could not be computed")
    if derived:
        return unknown(derived)

    day_range = inp.index_day_high - inp.index_day_low
    price = inp.index_price
    values.update({
        "index_price": price, "sma_fast": round(sma_fast, 2), "sma_slow": round(sma_slow, 2),
        "atr": round(atr_v, 2), "day_range": round(day_range, 2),
        "range_atr_ratio": round(day_range / atr_v, 2),
        "vix": inp.vix_now, "vix_cutoff": round(vix_cut, 2), "vix_days": len(vix_window),
    })

    if market_events:
        reasons = [f"{inp.session_date} is an event day: {t}" for t in event_text
                   if any(t.startswith(e["type"]) for e in market_events)]
        return RegimeResult("EVENT", reasons + list(inp.notes), version, values)

    volatile = []
    if inp.vix_now > vix_cut:
        volatile.append(f"India VIX {_f(inp.vix_now)} is above its {p.vix_percentile:g}th percentile "
                        f"{_f(vix_cut)} over the last {len(vix_window)} days")
    if day_range > p.volatile_range_atr_mult * atr_v:
        volatile.append(f"{idx} day range {_f(day_range)} is {day_range / atr_v:.2f}x its "
                        f"{p.atr_days}-day ATR {_f(atr_v)} (limit {p.volatile_range_atr_mult:g}x)")
    if volatile:
        return RegimeResult("VOLATILE", volatile + list(inp.notes), version, values)

    calm = (f"India VIX {_f(inp.vix_now)} is at or below its {p.vix_percentile:g}th percentile "
            f"{_f(vix_cut)}; day range is {day_range / atr_v:.2f}x ATR")
    bp = b.pct_above
    above = price > sma_fast and price > sma_slow
    below = price < sma_fast and price < sma_slow
    trend_text = (f"{idx} {_f(price)} vs {p.sma_fast_days}-day average {_f(sma_fast)} "
                  f"and {p.sma_slow_days}-day average {_f(sma_slow)}")
    breadth_text = (f"breadth {bp:.0f}% of {b.counted} stocks above their "
                    f"{p.breadth_sma_days}-day average")
    if above and bp >= p.breadth_up_pct:
        return RegimeResult("TRENDING_UP", [
            f"{trend_text}: above both",
            f"{breadth_text} (needs {p.breadth_up_pct:g}% or more)", calm, *inp.notes], version, values)
    if below and bp <= p.breadth_down_pct:
        return RegimeResult("TRENDING_DOWN", [
            f"{trend_text}: below both",
            f"{breadth_text} (needs {p.breadth_down_pct:g}% or less)", calm, *inp.notes], version, values)

    if above:
        why = f"{trend_text}: above both, but {breadth_text}, below {p.breadth_up_pct:g}%"
    elif below:
        why = f"{trend_text}: below both, but {breadth_text}, above {p.breadth_down_pct:g}%"
    else:
        why = f"{trend_text}: between the averages; {breadth_text}"
    return RegimeResult("RANGE", [why, calm, *inp.notes], version, values)


# ---------------- FII / DII bias ----------------
def fii_bias(rows_desc: Sequence[Mapping], session_date: date, calendar: MarketCalendar,
             streak: int) -> tuple[str, list[str]]:
    """Next-day bias for session_date from FII net cash flows of the sessions before it.

    rows_desc: most recent first, all dated before session_date.
    BEARISH after `streak` straight trading days of FII net selling, BULLISH after
    `streak` of net buying, else NEUTRAL. If the most recent figures are not for
    the previous trading days, the bias is UNKNOWN."""
    expected = []
    d = session_date
    for _ in range(streak):
        d = calendar.previous_trading_day(d)
        expected.append(d)
    got = {r["date"]: r for r in rows_desc}
    absent = [x for x in expected if x not in got]
    if absent:
        return "UNKNOWN", [f"FII/DII figures missing for {', '.join(x.isoformat() for x in absent)}; "
                           f"import them to get a bias"]
    nets = [got[x]["fii_net_cr"] for x in expected]
    detail = "; ".join(f"{x.isoformat()} FII net {_f(got[x]['fii_net_cr'])} cr" for x in expected)
    if all(v < 0 for v in nets):
        return "BEARISH", [f"FII net sellers for {streak} straight sessions ({detail})"]
    if all(v > 0 for v in nets):
        return "BULLISH", [f"FII net buyers for {streak} straight sessions ({detail})"]
    return "NEUTRAL", [f"No {streak}-session FII buying or selling streak ({detail})"]


# ---------------- service ----------------
def _daily(bars: Sequence[Bar]) -> list[DailyBar]:
    return [DailyBar(bar_date(b), b.open, b.high, b.low, b.close) for b in bars]


class RegimeService:
    """Gathers inputs and runs the rules. Logs to regime_log only when the
    regime or bias changes."""

    def __init__(self, settings: Settings, store: Store, hub: MarketHub,
                 calendar: MarketCalendar, manual: ManualData):
        self.settings = settings
        self.store = store
        self.hub = hub
        self.calendar = calendar
        self.manual = manual
        self._lock = threading.Lock()
        self._last_logged: tuple | None = None
        self._latest: dict | None = None

    def _source(self) -> str:
        return self.hub.feed_name

    def gather(self, now: datetime) -> tuple[RegimeInputs, dict]:
        s, p = self.settings, self.settings.regime
        src = self._source()
        session = self.calendar.target_session(now) if self.hub.enforce_hours else now.astimezone(IST).date()
        live = (not self.hub.enforce_hours) or self.calendar.is_open(now)
        lookback = max(p.sma_slow_days, p.atr_days, p.vix_lookback_days, p.breadth_sma_days) + 5
        stocks = [i.symbol for i in s.watchlist if i.kind == "stock"]
        notes: list[str] = []
        problems: list[str] = []

        def hist(sym: str, before: datetime, n: int) -> list[Bar]:
            return self.store.history_bars("bars_1d", sym, src, before=before, limit=n)

        def day_bar(sym: str, d: date) -> Bar | None:
            got = self.store.history_bars("bars_1d", sym, src, since=daily_start(d),
                                          before=daily_start(d) + _ONE_MIN)
            return got[0] if got else None

        # current values per symbol: (price, day high, day low)
        current: dict[str, tuple[float | None, float | None, float | None]] = {}
        if live:
            basis, basis_date = "LIVE", session
            for sym in [p.index_symbol, p.vix_symbol, *stocks]:
                q = self.hub.live_quote(sym, now)
                current[sym] = (q["last"], q["day_high"], q["day_low"]) if q else (None, None, None)
                if sym == p.index_symbol and q and q["range_source"] not in (None, "feed"):
                    notes.append(f"Day range measured from {q['range_source']} only")
        else:
            # Market closed: classify on the last completed session's close.
            basis = "LAST_CLOSE"
            last_idx = hist(p.index_symbol, daily_start(session), 1)
            basis_date = bar_date(last_idx[0]) if last_idx else None
            if basis_date is not None:
                for sym in [p.index_symbol, p.vix_symbol, *stocks]:
                    b = day_bar(sym, basis_date)
                    current[sym] = (b.close, b.high, b.low) if b else (None, None, None)
                notes.append(f"Market closed: based on the {basis_date} close, for the session of {session}")
                expected = self.calendar.previous_trading_day(session)
                if basis_date != expected:
                    problems.append(f"daily history ends {basis_date} but the last session was {expected}; "
                                    f"refresh history (python -m app.history) or add the holiday to holidays.csv")

        if basis_date is None:
            idx_bars, vix_bars, stock_data = [], [], {sym: ([], None) for sym in stocks}
            index_price = hi = lo = vix_now = None
        else:
            cutoff = daily_start(basis_date)
            need_last = self.calendar.previous_trading_day(basis_date)
            idx_bars = hist(p.index_symbol, cutoff, lookback)
            vix_bars = hist(p.vix_symbol, cutoff, lookback)
            for sym, bars in ((p.index_symbol, idx_bars), (p.vix_symbol, vix_bars)):
                if bars and bar_date(bars[-1]) != need_last:
                    problems.append(f"{sym} daily history ends {bar_date(bars[-1])}, expected {need_last}")
            index_price, hi, lo = current[p.index_symbol]
            vix_now = current[p.vix_symbol][0]
            stock_data = {}
            for sym in stocks:
                # +1 bar so a break on the oldest day is seen; never average across a break
                sb = after_last_break(hist(sym, cutoff, p.breadth_sma_days + 1),
                                      s.history.max_overnight_gap_pct)[-p.breadth_sma_days:]
                fresh = bool(sb) and bar_date(sb[-1]) == need_last
                stock_data[sym] = ([b.close for b in sb], current[sym][0] if fresh else None)

        # An invalid events, holidays or FII/DII file makes the regime UNKNOWN until fixed.
        problems.extend(self.manual.problems())
        inputs = RegimeInputs(
            session_date=session, index_symbol=p.index_symbol, index_history=_daily(idx_bars),
            index_price=index_price, index_day_high=hi, index_day_low=lo,
            vix_history=[b.close for b in vix_bars], vix_now=vix_now, stocks=stock_data,
            events=self.store.events_on(session),
            data_problems=problems, notes=notes,
        )
        meta = {"basis": basis, "basis_date": basis_date.isoformat() if basis_date else None,
                "source": src}
        return inputs, meta

    def evaluate(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        inputs, meta = self.gather(now)
        result = classify(inputs, self.settings.regime)
        rows = self.store.fii_dii_before(inputs.session_date, 30)
        bias, bias_reasons = fii_bias(rows, inputs.session_date, self.calendar, self.settings.bias.streak_days)
        if self.manual.errors.get("fii_dii"):
            bias, bias_reasons = "UNKNOWN", [x for x in self.manual.problems()
                                             if x.startswith(self.manual.display_path("fii_dii"))]
        out = {
            "regime": result.regime, "reasons": result.reasons,
            "bias": bias, "bias_reasons": bias_reasons,
            "rules_version": result.rules_version, "session_date": inputs.session_date.isoformat(),
            "as_of_utc": now.isoformat(), "values": result.values, **meta,
            "holiday": self.calendar.holiday_name(now.astimezone(IST).date()),
            "data_warnings": {k: v[:5] for k, v in self.manual.errors.items() if v},
        }
        with self._lock:
            session = inputs.session_date.isoformat()
            key = (session, result.regime, bias, meta["basis"], result.rules_version)
            if self._last_logged is None or self._last_logged[0] != session:
                # After a restart (or a new session) compare with what is stored, not with memory,
                # so restarting the app does not add a duplicate entry.
                last = self.store.last_regime(inputs.session_date)
                self._last_logged = (session, last["regime"], last["bias"], last["basis"],
                                     last["rules_version"]) if last else None
            if key != self._last_logged:
                self.store.save_regime(now, inputs.session_date, result.regime, bias,
                                       {"regime": result.reasons, "bias": bias_reasons},
                                       result.rules_version, meta["basis"])
                self._last_logged = key
            self._latest = out
        return out



# ---------------- point-in-time regime (backtests) ----------------
class PointInTimeRegime:
    """The regime as it would have been known at a past moment, from stored bars only.

    at_bar(t):   during a session, at the close time t of a 5-minute bar. Uses daily bars of
                 sessions before t's session, and only 5-minute bars that ended at or before t.
    at_close(d): after session d closed (swing signals). Uses daily bars up to and including d.

    It never reads the live hub, the clock or today's regime. A price older than `max_age`
    counts as missing, so gaps give UNKNOWN exactly as live data would.
    Stored 5-minute bars starting at `regime.ignore_5m_bar_starts` (Breeze's unreliable 15:15 and 15:20
    bars) are skipped, as if they did not exist; they stay in storage.
    """

    def __init__(self, settings: Settings, store: Store, calendar: MarketCalendar, source: str,
                 max_age: timedelta = timedelta(minutes=10)):
        self.p = settings.regime
        self.store = store
        self.calendar = calendar
        self.source = source
        self.max_age = max_age
        self.stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
        self.max_gap = settings.history.max_overnight_gap_pct
        self.ignore_starts = {time.fromisoformat(x) for x in settings.regime.ignore_5m_bar_starts}
        self._daily: dict[str, list[Bar]] = {}
        self._five: dict[str, dict[date, list[Bar]]] = {}
        self._cache: dict = {}
        self._idx: dict = {}
        self._starts: dict = {}
        self._hist: dict = {}

    def _d(self, sym: str) -> list[Bar]:
        if sym not in self._daily:
            self._daily[sym] = self.store.history_bars("bars_1d", sym, self.source)
        return self._daily[sym]

    def _f(self, sym: str, session: date) -> list[Bar]:
        if sym not in self._five:
            by_day: dict[date, list[Bar]] = {}
            for b in self.store.history_bars("bars_5m", sym, self.source):
                if b.start.astimezone(IST).time() in self.ignore_starts:
                    continue
                by_day.setdefault(bar_date(b), []).append(b)
            self._five[sym] = by_day
        return self._five[sym].get(session, [])

    def _before(self, sym: str, cutoff: datetime, n: int) -> list[Bar]:
        import bisect
        bars = self._d(sym)
        if sym not in self._starts:
            self._starts[sym] = [b.start for b in bars]
        i = bisect.bisect_left(self._starts[sym], cutoff)
        return bars[max(0, i - n):i]

    def _lookback(self) -> int:
        p = self.p
        return max(p.sma_slow_days, p.atr_days, p.vix_lookback_days, p.breadth_sma_days) + 5

    def _history(self, session: date):
        """Daily history for a session: identical for every bar of that session, so cached."""
        if session not in self._hist:
            p = self.p
            cutoff = daily_start(session)
            need_last = self.calendar.previous_trading_day(session)
            lookback = self._lookback()
            idx_bars = self._before(p.index_symbol, cutoff, lookback)
            vix_bars = self._before(p.vix_symbol, cutoff, lookback)
            problems = [f"{sym} daily history ends {bar_date(bars[-1])}, expected {need_last}"
                        for sym, bars in ((p.index_symbol, idx_bars), (p.vix_symbol, vix_bars))
                        if bars and bar_date(bars[-1]) != need_last]
            stocks = {}
            for sym in self.stocks:
                sb = after_last_break(self._before(sym, cutoff, p.breadth_sma_days + 1),
                                      self.max_gap)[-p.breadth_sma_days:]
                stocks[sym] = ([b.close for b in sb], bool(sb) and bar_date(sb[-1]) == need_last)
            self._hist[session] = (_daily(idx_bars), [b.close for b in vix_bars], stocks, problems,
                                   self.store.events_on(session))
        return self._hist[session]

    def _inputs(self, session: date, current: dict, notes: list[str]) -> RegimeInputs:
        """current: sym -> (price, high, low) as known at that moment."""
        p = self.p
        idx_hist, vix_hist, stocks, problems, events = self._history(session)
        stock_data = {sym: (closes, current.get(sym, (None,))[0] if fresh else None)
                      for sym, (closes, fresh) in stocks.items()}
        price, hi, lo = current.get(p.index_symbol, (None, None, None))
        return RegimeInputs(
            session_date=session, index_symbol=p.index_symbol, index_history=idx_hist,
            index_price=price, index_day_high=hi, index_day_low=lo,
            vix_history=vix_hist, vix_now=current.get(p.vix_symbol, (None,))[0],
            stocks=stock_data, events=events, data_problems=list(problems), notes=notes)

    def _session_index(self, sym: str, session: date):
        key = (sym, session)
        if key not in self._idx:
            bars = self._f(sym, session)
            ends, hi, lo = [], [], []
            for b in bars:
                ends.append(b.start + timedelta(minutes=5))
                hi.append(max(hi[-1], b.high) if hi else b.high)
                lo.append(min(lo[-1], b.low) if lo else b.low)
            self._idx[key] = (ends, [b.close for b in bars], hi, lo)
        return self._idx[key]

    def _so_far(self, sym: str, session: date, t: datetime) -> tuple[float | None, float | None, float | None]:
        """Last close and the session high/low from 5-minute bars that ENDED at or before t."""
        import bisect
        ends, closes, hi, lo = self._session_index(sym, session)
        i = bisect.bisect_right(ends, t) - 1
        if i < 0 or t - ends[i] > self.max_age:
            return (None, None, None)
        return (closes[i], hi[i], lo[i])

    def at_bar(self, t: datetime) -> RegimeResult:
        """Regime known at time t (UTC), the close of a 5-minute bar."""
        key = ("bar", t)
        if key not in self._cache:
            session = t.astimezone(IST).date()
            syms = [self.p.index_symbol, self.p.vix_symbol, *self.stocks]
            current = {s: self._so_far(s, session, t) for s in syms}
            inp = self._inputs(session, current, [])
            self._cache[key] = classify(inp, self.p)
        return self._cache[key]

    def at_close(self, d: date) -> RegimeResult:
        """Regime known after session d closed, from daily bars up to and including d."""
        key = ("close", d)
        if key not in self._cache:
            current = {}
            for s in [self.p.index_symbol, self.p.vix_symbol, *self.stocks]:
                b = [x for x in self._before(s, daily_start(d) + _ONE_MIN, 1) if bar_date(x) == d]
                current[s] = (b[0].close, b[0].high, b[0].low) if b else (None, None, None)
            # history = sessions before d; d's own bar supplies the "current" values
            inp = self._inputs(d, current, [f"based on the {d} close"])
            self._cache[key] = classify(inp, self.p)
        return self._cache[key]
