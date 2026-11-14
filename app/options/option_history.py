"""Historical option prices (phase 4, step 0).

Finding (2026-11-13, scripts/check_option_history.py): Breeze returns historical bars for single
option contracts, including expired ones (index and stock, back to at least 2017; daily, 5-minute
and 1-minute), with traded open/high/low/close, volume and open interest. There is NO bid/ask in
history: prices are trades only.

So option backtests price entries and exits from these stored bars (`OptionHistory`, price source
"option_history"). `ModelPrices` (Black-Scholes on the underlying's bars, price source "model") is
used only where no option history can exist (the sim source); its reports say "payoff simulation".
A backtest uses exactly one price source and every trade records it.

Bars are downloaded one contract at a time, on demand, with the same pacing, timeout and quality
rules as stock history, into `option_bars`. Every contract asked for is logged in
`option_fetch_log` (also when nothing came back), so it is never asked for twice. A contract that is
neither stored nor fetchable is UNKNOWN: the backtest skips that trade and counts it.

Expiry dates of expired contracts are not in the instrument list. `discover_expiries` finds them
from the data itself: a date is an expiry if an at-the-money contract with that expiry date has a
bar on that very day. Found dates are stored in `option_expiries`.

Run:  python -m app.options.option_history            # what is stored
      python -m app.options.option_history --iv-history RELIANCE TCS   # ATM history for stock IV rank
"""
from __future__ import annotations

import argparse
import bisect
import calendar as _cal
import logging
import math
import time as _time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from ..history import _num, _parse_dt, call_with_timeout, daily_start
from ..models import Bar, utcnow
from ..quality import IST, MARKET_OPEN
from ..store import Store
from .greeks import bs_price, implied_vol, years_to_expiry
from .instruments import nice_step

log = logging.getLogger("option_history")

DAILY = "1day"
BREEZE_RIGHT = {"CE": "call", "PE": "put"}
LOOKBACK_DAYS = 100           # a contract's bars are requested from this many days before its expiry
MIN_PRICE = 0.05              # NSE option tick: the lowest price an option can trade at


class OptionHistoryError(RuntimeError):
    """Breeze answered with an error (bad session, rate limit, network). Not the same as 'no data'."""


class RequestBudgetUsed(RuntimeError):
    pass


@dataclass(frozen=True)
class OptionBar:
    d: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    oi: float | None = None


def parse_option_rows(rows: list[dict]) -> tuple[list[tuple], list[tuple[str, dict]]]:
    """Raw Breeze option history rows (daily) -> storable tuples (start_utc iso, o, h, l, c, volume,
    oi), oldest first, plus rejected rows. Same rules as stock history: a timestamp, positive
    finite prices, high/low consistent with open/close, no negative volume or open interest."""
    good: dict[datetime, tuple] = {}
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
        vol, oi = _num(raw.get("volume")), _num(raw.get("open_interest"))
        if (vol is not None and vol < 0) or (oi is not None and oi < 0):
            bad.append(("negative volume or open interest", raw))
            continue
        start = daily_start(dt.astimezone(IST).date())
        good[start] = (start.isoformat(), o, h, l, c, vol, oi)
    return [good[k] for k in sorted(good)], bad


class BreezeOptionFetcher:
    """Paced, time-limited requests for one contract's daily bars through BreezeFeed (read-only)."""

    def __init__(self, feed, codes: dict[str, str], pause_seconds: float = 0.7, timeout_seconds: float = 60.0,
                 empty_retries: int = 1, retry_pause_seconds: float = 3.0, max_requests: int = 1500,
                 sleep: Callable[[float], None] = _time.sleep):
        self._feed = feed
        self._codes = codes
        self._pause, self._timeout = pause_seconds, timeout_seconds
        self._retries, self._retry_pause = empty_retries, retry_pause_seconds
        self.max_requests = max_requests
        self._sleep = sleep
        self.requests = 0

    @property
    def budget_left(self) -> int:
        return max(0, self.max_requests - self.requests)

    def fetch(self, underlying: str, expiry: date, strike: float, right: str, expect_data: bool = True) -> list[dict]:
        code = self._codes.get(underlying)
        if code is None:
            raise OptionHistoryError(f"{underlying} has no Breeze code in the watchlist")
        a = (expiry - timedelta(days=LOOKBACK_DAYS)).isoformat() + "T00:00:00.000Z"
        b = (expiry + timedelta(days=1)).isoformat() + "T00:00:00.000Z"
        tries = 1 + (self._retries if expect_data else 0)
        rows: list[dict] = []
        for attempt in range(tries):
            if self.requests >= self.max_requests:
                raise RequestBudgetUsed(f"request limit of {self.max_requests} for this run reached")
            if attempt:
                self._sleep(self._retry_pause)
            elif self.requests and self._pause:
                self._sleep(self._pause)
            self.requests += 1
            try:
                resp = call_with_timeout(lambda: self._feed.option_historical_raw(
                    code, expiry.isoformat() + "T06:00:00.000Z", strike, BREEZE_RIGHT[right], DAILY, a, b),
                    self._timeout)
            except Exception as e:      # noqa: BLE001 - network, SDK or timeout
                raise OptionHistoryError(f"{type(e).__name__}: {e}") from e
            if not isinstance(resp, dict) or resp.get("Error") or resp.get("Status") not in (200, "200"):
                raise OptionHistoryError(f"Breeze error for {underlying} {expiry} {strike:g} {right}: "
                                         f"{(resp or {}).get('Error') if isinstance(resp, dict) else resp!r}")
            rows = resp.get("Success") or []
            if rows:
                return rows
        return rows


class OptionHistory:
    """Stored option bars, read through to Breeze when a fetcher is given. Price source 'option_history'."""
    label = "option_history"

    def __init__(self, store: Store, source: str, fetcher: BreezeOptionFetcher | None = None,
                 now: datetime | None = None):
        self.store = store
        self.source = source
        self.fetcher = fetcher
        self.now = now
        self._bars: dict[str, dict[tuple, dict[date, OptionBar]]] = {}
        self._asked: dict[str, dict[tuple, int]] = {}
        self.stats: Counter = Counter()
        self.not_stored: set[tuple] = set()
        self.error: str | None = None            # first download error; downloading stops after it

    def _load(self, underlying: str) -> None:
        if underlying in self._bars:
            return
        self._bars[underlying] = {
            key: {b[0].astimezone(IST).date(): OptionBar(b[0].astimezone(IST).date(), *b[1:]) for b in rows}
            for key, rows in self.store.option_bars(self.source, underlying, DAILY).items()}
        self._asked[underlying] = self.store.option_fetch_log(self.source, underlying, DAILY)

    def contract(self, underlying: str, expiry: date, strike: float, right: str,
                 expect_data: bool = True) -> dict[date, OptionBar] | None:
        """The contract's daily bars by date. {} = asked for, nothing exists. None = unknown (not
        stored and not downloadable now)."""
        self._load(underlying)
        key = (expiry, float(strike), right)
        if key in self._asked[underlying]:
            return self._bars[underlying].get(key, {})
        if self.fetcher is None or self.error is not None or not self.fetcher.budget_left:
            self.not_stored.add((underlying, *key))
            return None
        try:
            rows = self.fetcher.fetch(underlying, expiry, strike, right, expect_data)
        except RequestBudgetUsed:
            self.not_stored.add((underlying, *key))
            return None
        except OptionHistoryError as e:
            self.error = str(e)
            log.error("option history download stopped: %s", e)
            self.not_stored.add((underlying, *key))
            return None
        bars, bad = parse_option_rows(rows)
        now = self.now or utcnow()
        for reason, row in bad:
            self.store.save_quality_event(now, underlying, "BAD_BAR", _num(row.get("close")), None, None,
                                          f"{self.source}:option:{expiry}:{strike:g}{right}:{reason}")
        self.store.save_option_bars(self.source, underlying, expiry, strike, right, DAILY, bars, now)
        self.stats["contracts_downloaded" if bars else "contracts_empty"] += 1
        self.stats["bars_downloaded"] += len(bars)
        self.stats["bars_rejected"] += len(bad)
        self._asked[underlying][key] = len(bars)
        self._bars[underlying][key] = {
            datetime.fromisoformat(b[0]).astimezone(IST).date():
                OptionBar(datetime.fromisoformat(b[0]).astimezone(IST).date(), *b[1:]) for b in bars}
        return self._bars[underlying][key]

    def bar(self, underlying: str, expiry: date, strike: float, right: str, d: date) -> OptionBar | None:
        c = self.contract(underlying, expiry, strike, right)
        return c.get(d) if c else None

    def expiries(self, underlying: str) -> list[tuple[date, str]]:
        return self.store.option_expiries(self.source, underlying)

    def stored_contracts(self, underlying: str) -> dict[tuple, dict[date, OptionBar]]:
        self._load(underlying)
        return self._bars[underlying]

    def download_stats(self) -> dict:
        return {"requests": self.fetcher.requests if self.fetcher else 0,
                "contracts_downloaded": self.stats["contracts_downloaded"],
                "contracts_empty": self.stats["contracts_empty"],
                "bars_downloaded": self.stats["bars_downloaded"], "bars_rejected": self.stats["bars_rejected"],
                "contracts_wanted_but_not_stored": len(self.not_stored), "error": self.error}


# ---------------- expiry discovery ----------------
def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, _cal.monthrange(year, month)[1])
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _back(d: date, n: int) -> date:
    """n weekdays before d."""
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def candidate_expiries(kind: str, period_start: date, preferred_weekday: int) -> list[date]:
    """Dates worth trying as the expiry of a month (kind monthly, period_start = its first day) or
    a week (kind weekly, period_start = its Monday): the usual weekday first, then one and two
    weekdays earlier (an expiry on a holiday moves earlier), then the other weekdays."""
    order = [preferred_weekday] + [w for w in (1, 3, 2, 0, 4) if w != preferred_weekday]
    out: list[date] = []

    def add(d: date) -> None:
        inside = (d.year, d.month) == (period_start.year, period_start.month) if kind == "monthly" \
            else period_start <= d < period_start + timedelta(days=7)
        if inside and d.weekday() < 5 and d not in out:
            out.append(d)

    for n, wd in enumerate(order):
        base = _last_weekday(period_start.year, period_start.month, wd) if kind == "monthly" \
            else period_start + timedelta(days=wd)
        add(base)
        if n == 0:
            add(_back(base, 1))
            add(_back(base, 2))
    return out


def periods(kind: str, start: date, end: date) -> list[date]:
    out = []
    if kind == "monthly":
        d = start.replace(day=1)
        while d <= end:
            out.append(d)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        d = start - timedelta(days=start.weekday())
        while d <= end:
            out.append(d)
            d += timedelta(days=7)
    return out


def period_key(kind: str, p: date) -> str:
    return p.strftime("%Y-%m") if kind == "monthly" else p.isoformat()


def discover_expiries(hist: OptionHistory, underlying: str, kind: str, start: date, end: date,
                      spot_before: Callable[[date], float | None], strike_step: float) -> dict:
    """Find the expiry of every month (or week) from `start` to `end` and store it. A candidate date
    is the expiry if the at-the-money call with that expiry date traded on that date. Periods
    already resolved (found, or searched without a result) are not searched again."""
    if hist.fetcher is None:
        raise ValueError("discover_expiries needs a fetcher")
    known = hist.expiries(underlying)
    done = {period_key(kind, e.replace(day=1) if kind == "monthly" else e - timedelta(days=e.weekday()))
            for e, k in known if kind == "weekly" or k == "monthly"}
    done |= hist.store.expiry_misses(hist.source, underlying, kind)
    preferred = next((e.weekday() for e, k in reversed(known) if k == kind), 1)
    found, missed, stopped = [], [], None
    for p in reversed(periods(kind, start, end)):          # newest first, so the usual weekday is learnt from recent data
        key = period_key(kind, p)
        if key in done:
            same = [e for e, k in known if period_key(kind, e.replace(day=1) if kind == "monthly"
                                                      else e - timedelta(days=e.weekday())) == key]
            if same:
                preferred = same[-1].weekday()
            continue
        hit = None
        cands = [c for c in candidate_expiries(kind, p, preferred) if start <= c <= end]
        for n, cand in enumerate(cands):
            spot = spot_before(cand)
            if spot is None:
                continue
            strike = round(spot / strike_step) * strike_step
            bars = hist.contract(underlying, cand, strike, "CE", expect_data=False)
            if bars is None:
                stopped = hist.error or "request limit reached"
                break
            if bars and max(bars) == cand:
                hit = cand
                if n == 0 or n > 2:
                    preferred = cand.weekday()
                break
        if stopped:
            break
        now = hist.now or utcnow()
        if hit:
            hist.store.save_option_expiry(hist.source, underlying, hit, kind, now)
            found.append(hit)
        elif cands:
            hist.store.save_expiry_miss(hist.source, underlying, kind, key, now)
            missed.append(key)
    return {"underlying": underlying, "kind": kind, "found": len(found), "not_found": missed, "stopped": stopped}


def simulated_expiries(first: date, last: date, is_trading_day: Callable[[date], bool],
                       weekly: bool) -> list[tuple[date, str]]:
    """FAKE expiry calendar for the sim source: the last trading Tuesday of each month (monthly) and,
    if `weekly`, every other Tuesday too, moved earlier when it is not a trading day."""
    out: dict[date, str] = {}

    def trading(d: date) -> date:
        while not is_trading_day(d):
            d -= timedelta(days=1)
        return d

    for p in periods("monthly", first, last):
        out[trading(_last_weekday(p.year, p.month, 1))] = "monthly"
    if weekly:
        for p in periods("weekly", first, last):
            out.setdefault(trading(p + timedelta(days=1)), "weekly")
    return sorted((e, k) for e, k in out.items() if first <= e <= last)


# ---------------- model prices (payoff simulation) ----------------
class ModelPrices:
    """Black-Scholes prices from the underlying's daily bars and a reference volatility series
    (India VIX). NOT market prices: reports built on this say "payoff simulation"."""
    label = "model"

    def __init__(self, daily: Callable[[str], list[Bar]], iv_pct_by_date: dict[date, float], rate: float,
                 expiries: Callable[[str], list[tuple[date, str]]], dividend: Callable[[str], float] = lambda s: 0.0):
        self._daily = daily
        self._iv = iv_pct_by_date
        self._iv_dates = sorted(iv_pct_by_date)
        self._rate = rate
        self._expiries = expiries
        self._div = dividend
        self._by_date: dict[str, dict[date, Bar]] = {}
        self.stats: Counter = Counter()
        self.not_stored: set[tuple] = set()
        self.error = None

    def _bar(self, underlying: str, d: date) -> Bar | None:
        if underlying not in self._by_date:
            self._by_date[underlying] = {b.start.astimezone(IST).date(): b for b in self._daily(underlying)}
        return self._by_date[underlying].get(d)

    def _iv_before(self, d: date) -> float | None:
        import bisect
        i = bisect.bisect_left(self._iv_dates, d) - 1
        return self._iv[self._iv_dates[i]] if i >= 0 else None

    def _price(self, s: float, k: float, at: datetime, expiry: date, iv_pct: float, right: str, q: float) -> float:
        t = years_to_expiry(at, expiry)
        intrinsic = max(0.0, s - k) if right == "CE" else max(0.0, k - s)
        p = bs_price(s, k, t, self._rate, q, iv_pct / 100.0, right) if t else intrinsic
        return round(max(MIN_PRICE, intrinsic, p if p is not None and math.isfinite(p) else 0.0), 2)

    def bar(self, underlying: str, expiry: date, strike: float, right: str, d: date) -> OptionBar | None:
        u = self._bar(underlying, d)
        iv_close, iv_open = self._iv.get(d), self._iv_before(d)
        if u is None or d > expiry or iv_close is None:
            return None
        iv_open = iv_open if iv_open is not None else iv_close
        q = self._div(underlying)
        t_open = datetime.combine(d, MARKET_OPEN, IST)
        t_close = datetime.combine(d, time(15, 30), IST)
        o = self._price(u.open, strike, t_open, expiry, iv_open, right, q)
        c = self._price(u.close, strike, t_close, expiry, iv_close, right, q)
        a = self._price(u.high, strike, t_close, expiry, iv_close, right, q)
        b = self._price(u.low, strike, t_close, expiry, iv_close, right, q)
        return OptionBar(d, o, max(o, c, a, b), min(o, c, a, b), c, None, None)

    def contract(self, underlying: str, expiry: date, strike: float, right: str, expect_data: bool = True):
        return None

    def expiries(self, underlying: str) -> list[tuple[date, str]]:
        return self._expiries(underlying)

    def stored_contracts(self, underlying: str) -> dict:
        return {}

    def download_stats(self) -> dict:
        return {"requests": 0, "note": "model prices: nothing is downloaded"}


# ---------------- implied-volatility history from stored bars ----------------
IV_STRIKE_DAYS_BEFORE_EXPIRY = (35, 15)   # an at-the-money call is struck this many days before each monthly expiry


def point_in_time_atm_contracts(contract: Callable, underlying: str, expiries: list[date],
                                closes: dict[date, float], step: float | None) -> tuple[dict, dict]:
    """The at-the-money calls that make up a stock's implied-volatility history, chosen with
    information available at the time: for each monthly expiry, the call struck at the underlying's
    close about 35 and about 15 days before expiry. Returns (contracts, known_from):
    contracts[(expiry, strike, 'CE')] = its stored bars, known_from[key] = the date whose close chose
    that strike. A contract may be used only from that date on (atm_iv_series enforces it), so the
    value for a day never depends on a later close. `contract(underlying, expiry, strike, right)`
    returns the stored bars, {} if none exist, None if unknown."""
    dates = sorted(closes)
    contracts: dict[tuple, dict] = {}
    known_from: dict[tuple, date] = {}
    for e in expiries:
        for back in IV_STRIKE_DAYS_BEFORE_EXPIRY:
            want = e - timedelta(days=back)
            i = bisect.bisect_right(dates, want) - 1
            if i < 0 or (want - dates[i]).days > 7:
                continue
            struck_on = dates[i]
            for k in atm_candidates(closes[struck_on], step):
                c = contract(underlying, e, k, "CE")
                if c is None:
                    break
                if c:
                    key = (e, float(k), "CE")
                    contracts[key] = c
                    known_from[key] = min(struck_on, known_from.get(key, struck_on))
                    break
    return contracts, known_from


def atm_iv_series(contracts: dict[tuple, dict[date, OptionBar]], closes: dict[date, float], rate: float,
                  dividend: float = 0.0, max_moneyness_pct: float = 5.0, min_dte: int = 5,
                  max_dte: int = 70, known_from: dict[tuple, date] | None = None) -> dict[date, float]:
    """Implied volatility (%) per day from stored option closes: on each day, the contract whose
    strike is nearest the underlying's close (within max_moneyness_pct, with min_dte..max_dte
    calendar days left) is inverted with Black-Scholes. Days with no such contract are absent.
    With `known_from`, a contract is used only on or after its date there (the day its strike was
    chosen) and contracts without an entry are not used at all: the point-in-time rule."""
    best: dict[date, tuple[float, float]] = {}
    for key, bars in contracts.items():
        expiry, strike, right = key
        first = None
        if known_from is not None:
            first = known_from.get(key)
            if first is None:
                continue
        for d, b in bars.items():
            s = closes.get(d)
            dte = (expiry - d).days
            if s is None or not (min_dte <= dte <= max_dte) or (first is not None and d < first):
                continue
            dist = abs(strike / s - 1) * 100
            if dist > max_moneyness_pct or (d in best and best[d][0] <= dist):
                continue
            t = years_to_expiry(datetime.combine(d, time(15, 30), IST), expiry)
            iv = implied_vol(b.close, s, strike, t, rate, dividend, right) if t else None
            if iv is not None:
                best[d] = (dist, iv * 100.0)
    return {d: v[1] for d, v in sorted(best.items())}


def atm_candidates(spot: float, step: float | None, n: int = 3) -> list[float]:
    """Strikes to try for 'at the money'. With a known spacing: the nearest multiples. Without one
    (expired stock contracts): the nearest multiple of a round step of about 1% of the price, then
    of the next finer steps."""
    if step:
        base = round(spot / step) * step
        out = [base] + [base + s * i * step for i in range(1, n) for s in (1, -1)]
        return [round(k, 2) for k in sorted(set(out), key=lambda k: abs(k - spot)) if k > 0][:n]
    steps = (1000, 500, 250, 100, 50, 20, 10, 5, 2.5, 1, 0.5)
    first = steps.index(nice_step(spot))
    out: list[float] = []
    for s in steps[first:first + n]:
        k = round(round(spot / s) * s, 2)
        if k > 0 and k not in out:
            out.append(k)
    return out


def download_iv_history(hist: OptionHistory, underlying: str, expiries: list[date],
                        closes: dict[date, float], step: float | None) -> int:
    """Store the at-the-money calls of point_in_time_atm_contracts for the given monthly expiries
    (the same selection the IV history reads). Returns contracts with data."""
    contracts, _ = point_in_time_atm_contracts(hist.contract, underlying, expiries, closes, step)
    return len(contracts)


def stock_iv_series(hist, underlying: str, expiries: list[date], closes: dict[date, float], step: float | None,
                    rate: float, dividend: float = 0.0) -> dict[date, float]:
    """A stock's at-the-money implied volatility (%) by date, point in time: the value for a day uses
    only that day's prices and contracts whose strike was chosen on or before that day."""
    contracts, known_from = point_in_time_atm_contracts(hist.contract, underlying, expiries, closes, step)
    return atm_iv_series(contracts, closes, rate, dividend, known_from=known_from)


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from ..config import ROOT, load_settings
    ap = argparse.ArgumentParser(description="Stored option history (Breeze)")
    ap.add_argument("--iv-history", nargs="*", metavar="SYMBOL",
                    help="download at-the-money calls of the last 14 monthly expiries for these stocks (IV rank)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    store = Store(settings.path(settings.storage.db_path_live), live=True)
    if args.iv_history:
        from ..feeds.breeze_feed import BreezeFeed
        load_dotenv(ROOT / ".env")
        h = settings.history
        fetcher = BreezeOptionFetcher(BreezeFeed(settings.watchlist), {i.symbol: i.breeze_code for i in settings.watchlist},
                                      h.request_pause_seconds, h.request_timeout_seconds, 1, h.retry_pause_seconds,
                                      settings.options.backtest.max_requests_per_run)
        hist = OptionHistory(store, "breeze", fetcher)
        idx = settings.regime.index_symbol
        monthly = [e for e, k in hist.expiries(idx) if k == "monthly"][-14:]
        if not monthly:
            print(f"No monthly expiries stored for {idx} yet: run python -m app.options.backtest --download first")
            return 1
        for sym in (s.upper() for s in args.iv_history):
            bars = store.history_bars("bars_1d", sym, "breeze")
            closes = {b.start.astimezone(IST).date(): b.close for b in bars}
            cfg = settings.option_underlying(sym)
            n = download_iv_history(hist, sym, monthly, closes, cfg.backtest_strike_step if cfg else None)
            print(f"{sym}: {n} at-the-money contracts with data")
        print(hist.download_stats())
    print("Stored option history (source breeze):")
    rows = store.option_history_summary("breeze")
    for r in rows:
        print(f"  {r['underlying']:12s} {r['interval']:6s} contracts {r['contracts']:5d}  bars {r['bars']:7d}  "
              f"bars {r['first_bar'][:10]} .. {r['last_bar'][:10]}  expiries {r['first_expiry']} .. {r['last_expiry']}")
    if not rows:
        print("  none yet: run python -m app.options.backtest --download")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
