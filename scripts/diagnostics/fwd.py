"""Engine-independent check for intraday strategies: signed forward returns (no stops, no costs)
from the entry bar's open after each real signal, compared with a baseline of every bar of every
stock in the development period and signal window whose regime (known at that bar) the strategy
trades, signed by the regime's direction (TRENDING_UP long, TRENDING_DOWN short; RANGE: weighted
by the real trades' long/short mix). The baseline uses no information from the real signals.
(Until 2026-11-12 the baseline used the real trade's own session and direction, which leaks the
future for breakout strategies.)

If a strategy is worse than random in the backtest but its raw forward returns are also worse
than the baseline, the cause is the signals themselves, not the backtest engine.

Run:  python scripts/diagnostics/fwd.py [--strategy orb range_reversion ...]   (development period only)
"""
import argparse
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import load_settings, hhmm
from app.store import Store
from app.backtest import Engine, dev_holdout, load_calendar
from app.quality import IST
from app.strategies import build_strategies


def main():
    ap = argparse.ArgumentParser(description="Raw forward returns after intraday signals vs a same-session baseline")
    ap.add_argument("--strategy", nargs="*", help="intraday strategy ids (default: all active intraday)")
    args = ap.parse_args()
    s = load_settings()
    store = Store(s.path(s.storage.db_path_live), live=True)
    eng = Engine(s, store, "breeze", calendar=load_calendar(s, store))
    stocks = [(i.symbol, i.kind) for i in s.watchlist if i.kind == "stock"]
    for strat in build_strategies(s, args.strategy):
        if strat.interval != "5minute":
            continue
        dev, _ = dev_holdout(s, eng.sessions(strat), strat.interval)
        trades, _ = eng.run(strat, dev, stocks)
        if not trades:
            print(f"{strat.id}: no trades")
            continue
        p = strat.params
        lo = hhmm(p.first_signal_time) if hasattr(p, "first_signal_time") else \
            (datetime(2000, 1, 1, 9, 15) + timedelta(minutes=p.range_minutes)).time()
        hi = hhmm(p.last_signal_time)
        long_share = sum(1 for t in trades if t.direction == "LONG") / len(trades)
        for H in (1, 3, 6, 12):
            sig_r, base_r = [], []
            for t in trades:
                d = 1 if t.direction == "LONG" else -1
                et = datetime.fromisoformat(t.entry_time)
                day = eng.five(t.symbol)[et.astimezone(IST).date()]
                i = {b.start: k for k, b in enumerate(day)}[et]
                if i + H - 1 < len(day):
                    sig_r.append(d * (day[i + H - 1].close / day[i].open - 1) * 1e4)
            for sym, _ in stocks:
                for dd in dev:
                    day = eng.five(sym).get(dd, [])
                    for j in range(1, len(day) - H + 1):
                        b = day[j]
                        if not (lo <= b.start.astimezone(IST).time() <= hi):
                            continue
                        reg = eng.regime.at_bar(b.start).regime     # known when the previous bar closed
                        if reg not in strat.allowed_regimes:
                            continue
                        r = (day[j + H - 1].close / b.open - 1) * 1e4
                        if reg == "TRENDING_UP":
                            base_r.append(r)
                        elif reg == "TRENDING_DOWN":
                            base_r.append(-r)
                        else:
                            base_r.append((2 * long_share - 1) * r)
            print(f"{strat.id:20s} next {H:2d} bars: after signal {st.mean(sig_r):+6.2f} bp (n={len(sig_r)})  "
                  f"baseline (any stock/bar, regime direction) {st.mean(base_r):+6.2f} bp (n={len(base_r)})")


if __name__ == "__main__":
    main()
