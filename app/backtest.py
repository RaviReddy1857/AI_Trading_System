"""Backtests with costs, next-bar entry and walk-forward evaluation.

Rules (CLAUDE.md, architecture.md section 14):
- A signal is produced after a bar has CLOSED. The earliest fill is the NEXT bar's open, plus
  adverse slippage. The signal bar's own prices are never used as a fill.
- Stop and target are checked conservatively: if one bar touches both, the stop is assumed hit
  first. A bar that opens beyond the stop fills at its open (worse than the stop).
- Intraday trades are closed at the close of the bar ending at `backtest.intraday_exit_time`
  (15:15 IST); no entries after `last_entry_time`.
- Every trade pays brokerage, taxes and charges from config.yaml (app/costs.py). Empty rates stop
  the run with an error naming them.
- The regime at each moment comes from PointInTimeRegime: only data available at that moment.
- Size: risk.max_loss_per_trade_pct of capital at the stop, capped at backtest.position_cap_pct of
  capital in position value. One open trade per symbol per strategy; portfolio limits are phase 5.

Walk-forward: rules are fixed (nothing is fitted). Sessions are split into a development part and a
final holdout: sessions from backtest.holdout_start[interval] on (fixed dates, phase 3b), or else the
last backtest.holdout_pct of sessions. The development part is replayed once and reported overall and
in rolling windows of window_sessions. The holdout is not simulated at all unless --holdout is given.

Run:  python -m app.backtest [--strategy orb ...] [--symbols RELIANCE TCS] [--source breeze|sim] [--holdout]
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .calendar import MarketCalendar
from .config import Settings, hhmm
from .costs import CostModel, money
from .history import bar_date
from .indicators import gap_breaks
from .models import Bar, Signal, utcnow
from .quality import IST
from .regime import PointInTimeRegime
from .store import Store
from .strategies import MarketContext, Strategy, build_strategies

log = logging.getLogger("backtest")
FIVE_MIN = timedelta(minutes=5)


@dataclass
class Trade:
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    horizon: str
    segment: str
    signal_id: str
    signal_time: str
    signal_close: float          # the signal bar's close: reference only, never a fill
    regime: str
    entry_time: str
    entry_price: float           # next bar's open after slippage
    qty: int
    stop: float
    target: float
    exit_time: str
    exit_price: float            # after slippage
    exit_reason: str             # STOP | TARGET | EOD | TIME | PERIOD_END
    gross_pnl: float
    costs: dict
    net_pnl: float
    why_now: list[str] = field(default_factory=list)


def position_size(capital: float, risk_pct: float, cap_pct: float, entry: float, stop: float) -> int:
    """Shares so that a stop-out loses at most risk_pct of capital (before costs), and the position
    value stays within cap_pct of capital. 0 means no trade."""
    per_share = abs(entry - stop)
    if not (math.isfinite(per_share) and per_share > 0 and entry > 0):
        return 0
    by_risk = math.floor(capital * risk_pct / 100 / per_share)
    by_cap = math.floor(capital * cap_pct / 100 / entry)
    return max(0, min(by_risk, by_cap))


class Engine:
    def __init__(self, settings: Settings, store: Store, source: str, calendar: MarketCalendar | None = None,
                 regime: PointInTimeRegime | None = None):
        self.s = settings
        self.store = store
        self.source = source
        self.calendar = calendar or MarketCalendar(store.holidays())
        self.costs = CostModel(settings.costs)
        self.regime = regime or PointInTimeRegime(settings, store, self.calendar, source)
        self.cfg = settings.backtest
        self._five: dict[str, dict[date, list[Bar]]] = {}
        self._five_days: dict[str, list[date]] = {}
        self._daily: dict[str, list[Bar]] = {}
        self._daily_dates: dict[str, list[date]] = {}

    # ---- data ----
    def five(self, sym: str) -> dict[date, list[Bar]]:
        if sym not in self._five:
            by: dict[date, list[Bar]] = defaultdict(list)
            for b in self.store.history_bars("bars_5m", sym, self.source):
                by[bar_date(b)].append(b)
            self._five[sym] = dict(by)
        return self._five[sym]

    def daily(self, sym: str) -> list[Bar]:
        if sym not in self._daily:
            self._daily[sym] = self.store.history_bars("bars_1d", sym, self.source)
        return self._daily[sym]

    def prior_sessions(self, sym: str, d: date, n: int) -> tuple[tuple[Bar, ...], ...]:
        """The last n stored sessions of 5-minute bars before session d (oldest first)."""
        if n <= 0:
            return ()
        data = self.five(sym)
        if sym not in self._five_days:
            self._five_days[sym] = sorted(data)
        days = self._five_days[sym]
        i = bisect.bisect_left(days, d)
        return tuple(tuple(data[x]) for x in days[max(0, i - n):i])

    def index_daily(self, d: date) -> tuple[Bar, ...]:
        """The index's daily bars up to and including session d."""
        sym = self.s.regime.index_symbol
        bars = self.daily(sym)
        if sym not in self._daily_dates:
            self._daily_dates[sym] = [bar_date(b) for b in bars]
        return tuple(bars[:bisect.bisect_right(self._daily_dates[sym], d)])

    def breaks(self, sym: str) -> list[int]:
        """Indexes of daily bars that opened beyond history.max_overnight_gap_pct from the previous
        close (unadjusted split, bonus or demerger). Stocks only; index moves are real."""
        bars = self.daily(sym)
        return gap_breaks([b.open for b in bars], [b.close for b in bars], self.s.history.max_overnight_gap_pct)

    def sessions(self, strategy: Strategy) -> list[date]:
        """Sessions the strategy can be tested on: dates with NIFTY bars of the strategy's interval."""
        idx = self.s.regime.index_symbol
        if strategy.interval == "5minute":
            return sorted(self.five(idx))
        return [bar_date(b) for b in self.daily(idx)]

    # ---- fills ----
    def _segment(self, strategy: Strategy) -> str:
        return "intraday_equity" if strategy.horizon == "INTRADAY" else "delivery_equity"

    def _open(self, sig: Signal, bar: Bar, segment: str, skipped: Counter):
        """Fill at the bar's open (+ slippage). Returns (fill, qty) or None."""
        o = bar.open
        if sig.direction == "LONG" and (o <= sig.stop or o >= sig.target):
            skipped["GAPPED_PAST_STOP_OR_TARGET"] += 1
            return None
        if sig.direction == "SHORT" and (o >= sig.stop or o <= sig.target):
            skipped["GAPPED_PAST_STOP_OR_TARGET"] += 1
            return None
        fill = self.costs.fill_price(segment, o, "BUY" if sig.direction == "LONG" else "SELL")
        qty = position_size(self.s.account.capital_inr, self.s.risk.max_loss_per_trade_pct,
                            self.cfg.position_cap_pct, float(fill), sig.stop)
        if qty < 1:
            skipped["ZERO_SIZE"] += 1
            return None
        return fill, qty

    @staticmethod
    def _hit(direction: str, bar: Bar, stop: float, target: float, is_entry_bar: bool):
        """(reason, raw price) if the bar hits stop or target. Stop first when both are touched."""
        if direction == "LONG":
            if bar.low <= stop:
                return "STOP", (bar.open if (not is_entry_bar and bar.open < stop) else stop)
            if bar.high >= target:
                return "TARGET", target
        else:
            if bar.high >= stop:
                return "STOP", (bar.open if (not is_entry_bar and bar.open > stop) else stop)
            if bar.low <= target:
                return "TARGET", target
        return None

    def _close(self, sig: Signal, segment: str, entry: Decimal, qty: int, entry_t: datetime,
               exit_raw: float, exit_t: datetime, reason: str, strategy: Strategy) -> Trade:
        side = "SELL" if sig.direction == "LONG" else "BUY"
        # A swing trade that exits on its entry day is an intraday trade for charges.
        if segment == "delivery_equity" and exit_t.astimezone(IST).date() == entry_t.astimezone(IST).date():
            segment = "intraday_equity"
        exit_fill = self.costs.fill_price(segment, exit_raw, side)
        q = Decimal(qty)
        if sig.direction == "LONG":
            buy_v, sell_v = money(entry * q), money(exit_fill * q)
        else:
            buy_v, sell_v = money(exit_fill * q), money(entry * q)
        gross = sell_v - buy_v
        c = self.costs.equity(segment, buy_v, sell_v)
        return Trade(
            strategy_id=strategy.id, strategy_version=strategy.version, symbol=sig.symbol,
            direction=sig.direction, horizon=sig.horizon, segment=segment, signal_id=sig.signal_id,
            signal_time=sig.created_at.isoformat(), signal_close=sig.entry, regime=sig.regime,
            entry_time=entry_t.isoformat(), entry_price=float(entry), qty=qty, stop=sig.stop, target=sig.target,
            exit_time=exit_t.isoformat(), exit_price=float(exit_fill), exit_reason=reason,
            gross_pnl=float(gross), costs=c.as_dict(), net_pnl=float(gross - c.total), why_now=list(sig.why_now))

    # ---- intraday ----
    def run_intraday(self, strategy: Strategy, sym: str, kind: str, sessions: list[date],
                     skipped: Counter) -> list[Trade]:
        seg = self._segment(strategy)
        exit_at, last_entry = hhmm(self.cfg.intraday_exit_time), hhmm(self.cfg.last_entry_time)
        trades: list[Trade] = []
        data = self.five(sym)
        for d in sessions:
            bars = data.get(d, [])
            prior = self.prior_sessions(sym, d, strategy.prior_sessions_needed) if bars else ()
            pos = None   # (signal, entry fill, qty, entry time, entry index)
            for i, bar in enumerate(bars):
                end = bar.start + FIVE_MIN
                end_t = end.astimezone(IST).time()
                if pos is not None:
                    sig, fill, qty, et, ei = pos
                    if bar.start.astimezone(IST).time() >= exit_at:
                        # the bar ending at the exit time is missing: leave at the first price after it
                        trades.append(self._close(sig, seg, fill, qty, et, bar.open, bar.start, "EOD", strategy))
                        pos = None
                    else:
                        hit = self._hit(sig.direction, bar, sig.stop, sig.target, i == ei)
                        if hit:
                            trades.append(self._close(sig, seg, fill, qty, et, hit[1], end, hit[0], strategy))
                            pos = None
                        elif end_t >= exit_at:
                            trades.append(self._close(sig, seg, fill, qty, et, bar.close, end, "EOD", strategy))
                            pos = None
                if pos is not None or end_t >= exit_at:
                    continue
                # bar i has closed: the strategy sees bars[0..i] and the regime known at `end`
                ctx = MarketContext(sym, kind, end, self.regime.at_bar(end).regime, intraday=tuple(bars[:i + 1]),
                                    prior_sessions=prior)
                for sig in strategy.evaluate(ctx):
                    nxt = bars[i + 1] if i + 1 < len(bars) else None
                    if nxt is None or nxt.start != end or nxt.start >= sig.valid_until:
                        skipped["NO_NEXT_BAR"] += 1
                        continue
                    if nxt.start.astimezone(IST).time() > last_entry:
                        skipped["AFTER_LAST_ENTRY_TIME"] += 1
                        continue
                    got = self._open(sig, nxt, seg, skipped)
                    if got:
                        pos = (sig, got[0], got[1], nxt.start, i + 1)
                    break
            if pos is not None:   # data ended before the exit time
                sig, fill, qty, et, ei = pos
                trades.append(self._close(sig, seg, fill, qty, et, bars[-1].close,
                                          bars[-1].start + FIVE_MIN, "EOD", strategy))
        return trades

    # ---- swing ----
    def run_swing(self, strategy: Strategy, sym: str, kind: str, sessions: list[date],
                  skipped: Counter, max_hold: int) -> list[Trade]:
        seg = self._segment(strategy)
        allowed = set(sessions)
        last_allowed = max(sessions) if sessions else None
        bars = self.daily(sym)
        brk = set(self.breaks(sym)) if kind == "stock" else set()
        trades: list[Trade] = []
        pos = None   # (signal, fill, qty, entry time, entry index)
        start = 0    # history visible to the strategy begins after the last break
        for i, bar in enumerate(bars):
            d = bar_date(bar)
            if last_allowed is None or d > last_allowed:
                break
            close_t = datetime.combine(d, time(15, 30), IST).astimezone(timezone.utc)
            if i in brk:
                start = i
                if pos is not None:
                    # prices after the break are in different units: the outcome is unknowable
                    skipped["DROPPED_DATA_BREAK_DURING_TRADE"] += 1
                    pos = None
            if pos is not None:
                sig, fill, qty, et, ei = pos
                hit = self._hit(sig.direction, bar, sig.stop, sig.target, i == ei)
                if hit:
                    trades.append(self._close(sig, seg, fill, qty, et, hit[1], close_t, hit[0], strategy))
                    pos = None
                elif i - ei + 1 >= max_hold:
                    trades.append(self._close(sig, seg, fill, qty, et, bar.close, close_t, "TIME", strategy))
                    pos = None
                elif d == last_allowed:
                    trades.append(self._close(sig, seg, fill, qty, et, bar.close, close_t, "PERIOD_END", strategy))
                    pos = None
            if pos is not None or d not in allowed or d == last_allowed:
                continue
            ctx = MarketContext(sym, kind, close_t, self.regime.at_close(d).regime, daily=tuple(bars[start:i + 1]),
                                index_daily=self.index_daily(d) if strategy.needs_index_daily else ())
            for sig in strategy.evaluate(ctx):
                nxt = bars[i + 1] if i + 1 < len(bars) else None
                if nxt is None or nxt.start >= sig.valid_until:
                    skipped["NO_NEXT_BAR"] += 1
                    continue
                if i + 1 in brk:
                    skipped["DATA_BREAK_AT_ENTRY"] += 1
                    continue
                got = self._open(sig, nxt, seg, skipped)
                if got:
                    pos = (sig, got[0], got[1], nxt.start, i + 1)
                break
        return trades

    def run(self, strategy: Strategy, sessions: list[date], symbols: list[tuple[str, str]]) -> tuple[list[Trade], Counter]:
        self.costs.require(self._segment(strategy))           # fail loudly before any work
        skipped: Counter = Counter()
        trades: list[Trade] = []
        for sym, kind in symbols:
            if strategy.interval == "5minute":
                trades += self.run_intraday(strategy, sym, kind, sessions, skipped)
            else:
                trades += self.run_swing(strategy, sym, kind, sessions, skipped, strategy.params.max_hold_days)
        trades.sort(key=lambda t: (t.exit_time, t.symbol))
        return trades, skipped


# ---------------- statistics and reports ----------------
def stats(trades: list[Trade], capital: float, min_trades: int) -> dict:
    n = len(trades)
    nets = [Decimal(str(t.net_pnl)) for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    equity = peak = dd = Decimal(0)
    for t in sorted(trades, key=lambda t: t.exit_time):
        equity += Decimal(str(t.net_pnl))
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    gross_loss = -sum(losses, Decimal(0))
    out = {
        "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "avg_win_inr": float(money(sum(wins, Decimal(0)) / len(wins))) if wins else None,
        "avg_loss_inr": float(money(sum(losses, Decimal(0)) / len(losses))) if losses else None,
        "gross_pnl_inr": float(money(sum((Decimal(str(t.gross_pnl)) for t in trades), Decimal(0)))),
        "costs_inr": float(money(sum((Decimal(str(t.costs["total"])) for t in trades), Decimal(0)))),
        "net_pnl_inr": float(money(sum(nets, Decimal(0)))),
        "max_drawdown_inr": float(money(dd)),
        "max_drawdown_pct": round(float(dd) / capital * 100, 2) if capital else None,
        "profit_factor": round(float(sum(wins, Decimal(0)) / gross_loss), 2) if gross_loss > 0 else None,
        "low_sample": n < min_trades,
    }
    out["sample_note"] = (f"LOW SAMPLE: {n} trades (fewer than {min_trades}); do not rely on this result"
                          if n < min_trades else f"{n} trades")
    if out["profit_factor"] is None:
        out["profit_factor_note"] = "no losing trades" if wins else "no trades"
    return out


def exposure(trades: list[Trade], capital: float) -> dict:
    """Peak number of trades open at the same time and their combined position value. Trades are
    sized one by one (no portfolio limit until phase 5), so this can exceed the capital."""
    events = []
    for t in trades:
        v = t.entry_price * t.qty
        events.append((t.entry_time, 1, v))
        events.append((t.exit_time, -1, -v))
    n = peak_n = 0
    val = peak_v = 0.0
    for _, dn, dv in sorted(events, key=lambda e: (e[0], e[1])):     # exits before entries at the same time
        n += dn
        val += dv
        peak_n, peak_v = max(peak_n, n), max(peak_v, val)
    return {"max_open_trades": peak_n, "max_position_value_inr": round(peak_v, 2),
            "max_position_value_pct_of_capital": round(peak_v / capital * 100, 1) if capital else None}


def split_sessions(sessions: list[date], holdout_pct: float,
                   holdout_start: date | None = None) -> tuple[list[date], list[date]]:
    """(development, holdout). With holdout_start, the holdout is every session on or after it;
    otherwise the final holdout_pct of sessions, at least one."""
    if not sessions:
        return [], []
    if holdout_start is not None:
        return [d for d in sessions if d < holdout_start], [d for d in sessions if d >= holdout_start]
    h = max(1, math.ceil(len(sessions) * holdout_pct / 100))
    return sessions[:-h], sessions[-h:]


def dev_holdout(settings: Settings, sessions: list[date], interval: str) -> tuple[list[date], list[date]]:
    """(development, holdout) sessions for a bar interval: sessions before backtest.test_start are not
    tested, and the holdout follows backtest.holdout_start (or holdout_pct)."""
    cfg = settings.backtest
    start = cfg.test_start.get(interval)
    if start is not None:
        sessions = [d for d in sessions if d >= start]
    return split_sessions(sessions, cfg.holdout_pct, cfg.holdout_start.get(interval))


def windows(sessions: list[date], size: int, step: int) -> list[list[date]]:
    out, i = [], 0
    while i < len(sessions):
        out.append(sessions[i:i + size])
        if i + size >= len(sessions):
            break
        i += step
    return out


def build_report(strategy: Strategy, settings: Settings, costs: CostModel, source: str,
                 dev: list[date], hold: list[date], dev_trades: list[Trade], skipped: Counter,
                 hold_trades: list[Trade] | None, hold_skipped: Counter | None, symbols: int,
                 filtered: dict | None = None) -> dict:
    cfg = settings.backtest
    cap = settings.account.capital_inr
    by_regime: dict[str, list[Trade]] = defaultdict(list)
    for t in dev_trades:
        by_regime[t.regime].append(t)
    wf = []
    for w in windows(dev, cfg.window_sessions, cfg.step_sessions):
        ws = set(w)
        tw = [t for t in dev_trades if datetime.fromisoformat(t.entry_time).astimezone(IST).date() in ws]
        wf.append({"from": w[0].isoformat(), "to": w[-1].isoformat(), "sessions": len(w),
                   **stats(tw, cap, cfg.min_trades)})
    rep = {
        "strategy_id": strategy.id, "strategy_version": strategy.version,
        "params": strategy.params.model_dump(),
        "allowed_regimes": sorted(strategy.allowed_regimes), "horizon": strategy.horizon,
        "cost_label": costs.label, "costs": costs.info(),
        "data_source": source,
        "data_note": "SIMULATED DATA - results mean nothing about the real market" if source == "sim" else "",
        "symbols": symbols,
        "period": {"from": dev[0].isoformat() if dev else None, "to": dev[-1].isoformat() if dev else None,
                   "sessions": len(dev)},
        "capital_inr": cap, "risk_per_trade_pct": settings.risk.max_loss_per_trade_pct,
        "overall": stats(dev_trades, cap, cfg.min_trades),
        "exposure": exposure(dev_trades, cap),
        "by_regime": {r: stats(ts, cap, cfg.min_trades) for r, ts in sorted(by_regime.items())},
        "walk_forward": {
            "rules": "fixed (no parameters fitted); rolling test windows over the development period",
            "window_sessions": cfg.window_sessions, "step_sessions": cfg.step_sessions,
            "windows": wf,
            "profitable_windows": sum(1 for w in wf if w["net_pnl_inr"] > 0),
            "windows_with_trades": sum(1 for w in wf if w["trades"] > 0),
        },
        "holdout": {
            "from": hold[0].isoformat() if hold else None, "to": hold[-1].isoformat() if hold else None,
            "sessions": len(hold),
            "evaluated": hold_trades is not None,
            "result": stats(hold_trades, cap, cfg.min_trades) if hold_trades is not None else
            "untouched - run with --holdout only once you have finished changing this strategy",
            "skipped_signals": dict(hold_skipped) if hold_skipped is not None else None,
        },
        "skipped_signals": dict(skipped),
        "filtered_signals": dict(filtered or {}),
        "strategy_status": {"alerts": strategy.alerts_enabled, "mode": strategy.mode, "label": strategy.status_label},
        "round_trip_cost_pct": strategy.round_trip_cost_pct,
        "notes": [
            "Entries fill at the next bar's open plus slippage; the signal bar's close is never a fill.",
            "If a bar touches both stop and target, the stop is assumed hit first.",
            "Intraday trades close by %s IST." % cfg.intraday_exit_time,
            "Regime at each point uses only data available at that time.",
            "Each trade is sized on its own (one open trade per symbol). There are no portfolio limits until "
            "phase 5, so many trades can be open at once and use more than the capital (see exposure); "
            "rupee totals and drawdown are then larger than one account could have had.",
        ],
    }
    return rep


def run_backtest(settings: Settings, store: Store, source: str, strategy: Strategy,
                 symbols: list[str] | None = None, include_holdout: bool = False,
                 engine: Engine | None = None, save: bool = True, now: datetime | None = None) -> dict:
    eng = engine or Engine(settings, store, source)
    eng.costs.require(eng._segment(strategy))
    now = now or utcnow()
    stocks = [(i.symbol, i.kind) for i in settings.watchlist
              if i.kind == "stock" and (symbols is None or i.symbol in symbols)]
    sessions = eng.sessions(strategy)
    dev, hold = dev_holdout(settings, sessions, strategy.interval)
    strategy.rejections.clear()
    trades, skipped = eng.run(strategy, dev, stocks)
    filtered = dict(strategy.rejections)
    hold_trades = hold_skipped = None
    if include_holdout and hold:
        hold_trades, hold_skipped = eng.run(strategy, hold, stocks)
    rep = build_report(strategy, settings, eng.costs, source, dev, hold, trades, skipped,
                       hold_trades, hold_skipped, len(stocks), filtered)
    rep["data_breaks"] = {
        sym: [bar_date(eng.daily(sym)[i]).isoformat() for i in eng.breaks(sym)] for sym, _ in stocks if eng.breaks(sym)}
    rep["notes"].append(f"Stock daily history is not adjusted for splits, bonuses or demergers. An overnight gap above "
                        f"{settings.history.max_overnight_gap_pct:g}% is a data break: no average, ATR, breakout or breadth "
                        f"spans it, and a swing trade open across one is dropped (see data_breaks).")
    run_id = f"{strategy.id}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    rep["run_id"], rep["created_utc"] = run_id, now.isoformat()
    all_trades = [asdict(t) for t in trades] + ([{**asdict(t), "holdout": True} for t in hold_trades]
                                                if hold_trades else [])
    if save:
        store.save_backtest(run_id, now, strategy.id, strategy.version, rep["params"], rep, all_trades)
        out = settings.path(settings.backtest.reports_dir)
        out.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"report": rep, "trades": all_trades}, indent=2, default=str)
        (out / f"{run_id}.json").write_text(payload, encoding="utf-8")
        (out / f"{strategy.id}_latest.json").write_text(payload, encoding="utf-8")
    return {"report": rep, "trades": all_trades}


def load_calendar(settings: Settings, store: Store) -> MarketCalendar:
    """The trading calendar for a backtest: holidays read from holidays.csv, so a run sees the same
    holidays as the app. Read-only: nothing is written to the database (the running app may hold its
    write lock). If the file is missing or invalid, the holidays last stored by the app are used."""
    from .manual_data import parse_holidays
    p = settings.path(settings.calendar.holidays_csv)
    try:
        parsed, errors = parse_holidays(p.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as e:
        parsed, errors = None, [f"cannot read file: {type(e).__name__}"]
    if errors or parsed is None:
        log.warning("%s not used (%s); using the holidays stored by the app", p.name, "; ".join(errors[:3]))
        return MarketCalendar(store.holidays())
    return MarketCalendar(parsed)


def main(argv: list[str] | None = None) -> int:
    from .config import load_settings
    ap = argparse.ArgumentParser(description="Cost-inclusive backtests with walk-forward windows")
    ap.add_argument("--strategy", nargs="*", help="strategy ids (default: all not in strategies.inactive)")
    ap.add_argument("--symbols", nargs="*", help="stock symbols (default: every watchlist stock)")
    ap.add_argument("--source", choices=["breeze", "sim"], default="breeze")
    ap.add_argument("--holdout", action="store_true", help="also evaluate the untouched holdout period")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    path = settings.path(settings.storage.db_path_live if args.source == "breeze" else settings.storage.db_path_sim)
    store = Store(path, live=args.source == "breeze")
    eng = Engine(settings, store, args.source, calendar=load_calendar(settings, store))
    for strat in build_strategies(settings, args.strategy):
        res = run_backtest(settings, store, args.source, strat, args.symbols, args.holdout, engine=eng)
        r = res["report"]
        o = r["overall"]
        print(f"\n{strat.id} {strat.version}  [{r['cost_label']}]  source={args.source}")
        print(f"  period {r['period']['from']} .. {r['period']['to']} ({r['period']['sessions']} sessions), "
              f"holdout {r['holdout']['from']} .. {r['holdout']['to']} "
              f"({'evaluated' if r['holdout']['evaluated'] else 'untouched'})")
        print(f"  trades {o['trades']}  win rate {o['win_rate_pct']}%  net Rs {o['net_pnl_inr']:,.2f} "
              f"(costs Rs {o['costs_inr']:,.2f})  max DD Rs {o['max_drawdown_inr']:,.2f}  PF {o['profit_factor']}"
              f"  {o['sample_note']}")
        if r["filtered_signals"]:
            print(f"  filtered signals {r['filtered_signals']}")
        print(f"  saved {r['run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
