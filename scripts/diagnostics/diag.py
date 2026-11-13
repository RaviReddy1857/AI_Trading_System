"""Backtest diagnosis: random-entry baseline, sample trades, exit reasons, R-multiples, MFE/MAE and
mechanical checks (stop/target sides, slippage once per side and adverse, no earlier stop/target
touch, entry on the bar after the signal). Development period only; the holdout is never simulated.

Random baseline: each real trade is paired with one random entry that has the same stop and target
distances; the engine's exit rules and costs are unchanged. Where the random entry goes uses only
information available at that moment:
- intraday: a random stock, development session (complete sessions only, as the engine requires) and
  bar in the strategy's signal window, kept only if the regime at that bar is one the strategy trades. Direction = the regime's (TRENDING_UP long,
  TRENDING_DOWN short); in RANGE, drawn from the real trades' long/short mix.
  (Until 2026-11-12 the random entry reused the real trade's own session and direction. For a breakout
  strategy that leaks the future: the session was chosen because price later broke out that way.)
- swing: the real trade's stock on a random development day whose close-of-day regime (and, for v2,
  the NIFTY filter) allows the strategy; long only, like the strategies.

Run:  python scripts/diagnostics/diag.py [--strategy orb range_reversion ...] [--seeds 20]
Output: a summary table on screen and data/diagnostics/diag_<ids>.json (full detail, 10 sample trades each).
"""
import argparse, sys, random, json, math, statistics as stx
from collections import Counter, defaultdict
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import load_settings, hhmm
from app.store import Store
from app.backtest import Engine, dev_holdout, load_calendar
from app.costs import money
from app.history import bar_date
from app.quality import IST
from app.strategies import build_strategies, Strategy
from app.strategies.base import next_weekday_open_window

FIVE = timedelta(minutes=5)
s = load_settings()
store = Store(s.path(s.storage.db_path_live), live=True)


class DiagEngine(Engine):
    """Records the raw (pre-slippage) entry and exit prices of each trade."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.raw_entry = {}

    def _open(self, sig, bar, seg, skipped):
        r = super()._open(sig, bar, seg, skipped)
        if r:
            self.raw_entry[sig.signal_id] = bar
        return r

    def _close(self, sig, segment, entry, qty, entry_t, exit_raw, exit_t, reason, strategy):
        t = super()._close(sig, segment, entry, qty, entry_t, exit_raw, exit_t, reason, strategy)
        t.raw_entry = self.raw_entry[sig.signal_id].open
        t.entry_bar = self.raw_entry[sig.signal_id]
        t.raw_exit = float(exit_raw)
        return t


class RandomEntry(Strategy):
    """Fires exactly at scheduled (symbol, asof) moments with a given direction and stop/target distance."""
    def __init__(self, base, schedule):
        self.base, self.sched = base, schedule
        self.id, self.horizon, self.interval = "random_" + base.id, base.horizon, base.interval
        self.params, self.version = base.params, "random"
        self.allowed_regimes = frozenset()

    def _evaluate(self, ctx):
        return []

    def evaluate(self, ctx):
        k = (ctx.symbol, ctx.asof)
        if k not in self.sched or ctx.kind != "stock":
            return []
        direction, sp, tp = self.sched[k]
        bars = ctx.intraday if self.interval == "5minute" else ctx.daily
        e = bars[-1].close
        if direction == "LONG":
            stop, tgt = e * (1 - sp), e * (1 + tp)
        else:
            stop, tgt = e * (1 + sp), e * (1 - tp)
        valid = ctx.asof + FIVE if self.interval == "5minute" else next_weekday_open_window(bar_date(bars[-1]))
        return [self.signal(ctx, direction, e, stop, tgt, valid, ["random"])]


def pf(xs):
    w = sum(x for x in xs if x > 0)
    l = -sum(x for x in xs if x <= 0)
    return round(w / l, 3) if l > 0 else None


def variants(eng, trades):
    """P&L per trade: no costs, slippage only, charges only, both."""
    out = {"none": [], "slip_only": [], "charges_only": [], "both": []}
    for t in trades:
        d = 1 if t.direction == "LONG" else -1
        raw = d * (t.raw_exit - t.raw_entry) * t.qty
        q = Decimal(t.qty)
        bv, sv = (Decimal(str(t.raw_entry)) * q, Decimal(str(t.raw_exit)) * q)
        if d < 0:
            bv, sv = sv, bv
        ch_raw = float(eng.costs.equity(t.segment, money(bv), money(sv)).total)
        out["none"].append(raw)
        out["slip_only"].append(t.gross_pnl)
        out["charges_only"].append(raw - ch_raw)
        out["both"].append(t.net_pnl)
    return out


def risk_ps(t):
    return abs(t.raw_entry - t.stop)


def path_bars(eng, t, interval):
    et, xt = datetime.fromisoformat(t.entry_time), datetime.fromisoformat(t.exit_time)
    if interval == "5minute":
        day = eng.five(t.symbol).get(et.astimezone(IST).date(), [])
        return [b for b in day if et <= b.start < xt]
    ed, xd = et.astimezone(IST).date(), xt.astimezone(IST).date()
    return [b for b in eng.daily(t.symbol) if ed <= bar_date(b) <= xd]


def mfe_mae(eng, t, interval):
    bars = path_bars(eng, t, interval)
    r = risk_ps(t)
    if not bars or r <= 0:
        return None
    if t.direction == "LONG":
        return (max(b.high for b in bars) - t.raw_entry) / r, (t.raw_entry - min(b.low for b in bars)) / r
    return (t.raw_entry - min(b.low for b in bars)) / r, (max(b.high for b in bars) - t.raw_entry) / r


def checks(eng, trades, interval):
    """Mechanical checks on every trade. Returns Counter of problems."""
    seg_slip = lambda seg: Decimal(str(getattr(s.costs.slippage_pct, seg))) / 100
    bad = Counter()
    for t in trades:
        L = t.direction == "LONG"
        if L and not (t.stop < t.raw_entry < t.target): bad["entry not between stop and target (long)"] += 1
        if not L and not (t.target < t.raw_entry < t.stop): bad["entry not between stop and target (short)"] += 1
        if L and not (t.stop < t.signal_close < t.target): bad["signal levels wrong side (long)"] += 1
        if not L and not (t.target < t.signal_close < t.stop): bad["signal levels wrong side (short)"] += 1
        # slippage: entry slipped once, adversely, with the entry segment's rate
        entry_seg = "intraday_equity" if t.horizon == "INTRADAY" else "delivery_equity"
        sl = seg_slip(entry_seg)
        exp_e = money(Decimal(str(t.raw_entry)) * (1 + sl if L else 1 - sl))
        if Decimal(str(t.entry_price)) != exp_e: bad["entry slippage mismatch"] += 1
        if (L and t.entry_price < t.raw_entry) or (not L and t.entry_price > t.raw_entry): bad["entry slippage favourable"] += 1
        sx = seg_slip(t.segment)
        exp_x = money(Decimal(str(t.raw_exit)) * (1 - sx if L else 1 + sx))
        if Decimal(str(t.exit_price)) != exp_x: bad["exit slippage mismatch"] += 1
        if (L and t.exit_price > t.raw_exit) or (not L and t.exit_price < t.raw_exit): bad["exit slippage favourable"] += 1
        # exits
        if t.exit_reason == "STOP" and ((L and t.raw_exit > t.stop) or (not L and t.raw_exit < t.stop)): bad["stop exit better than stop"] += 1
        if t.exit_reason == "TARGET" and t.raw_exit != t.target: bad["target exit not at target"] += 1
        # no bar before the exit bar touched stop or target
        bars = path_bars(eng, t, interval)
        for b in (bars[:-1] if t.exit_reason in ("STOP", "TARGET") else bars):
            if (L and (b.low <= t.stop or b.high >= t.target)) or (not L and (b.high >= t.stop or b.low <= t.target)):
                bad["an earlier bar touched stop/target"] += 1
                break
        # entry bar is the bar right after the signal
        sig_t = datetime.fromisoformat(t.signal_time)
        et = datetime.fromisoformat(t.entry_time)
        if interval == "5minute" and et != sig_t: bad["entry bar is not the next bar"] += 1
        if interval != "5minute" and not (et > sig_t): bad["entry not after signal"] += 1
    return bad


def summarize(eng, trades, interval, label):
    n = len(trades)
    v = variants(eng, trades)
    reasons = Counter(t.exit_reason for t in trades)
    def same_bar(t):
        et, xt = datetime.fromisoformat(t.entry_time), datetime.fromisoformat(t.exit_time)
        return (xt - et == FIVE) if interval == "5minute" else (et.astimezone(IST).date() == xt.astimezone(IST).date())
    sb = [t for t in trades if same_bar(t)]
    rs = [ (1 if t.direction == "LONG" else -1) * (t.raw_exit - t.raw_entry) / risk_ps(t) for t in trades if risk_ps(t) > 0]
    rnet = [t.net_pnl / (t.qty * risk_ps(t)) for t in trades if risk_ps(t) > 0]
    me = [m for m in (mfe_mae(eng, t, interval) for t in trades) if m]
    stop_pct = [risk_ps(t) / t.raw_entry * 100 for t in trades]
    cost_r = [(((1 if t.direction == 'LONG' else -1) * (t.raw_exit - t.raw_entry) * t.qty) - t.net_pnl) / (t.qty * risk_ps(t)) for t in trades if risk_ps(t) > 0]
    return {
        "label": label, "trades": n,
        "pf": {k: pf(x) for k, x in v.items()},
        "sum": {k: round(sum(x)) for k, x in v.items()},
        "win_rate_none": round(sum(1 for x in v["none"] if x > 0) / n * 100, 1) if n else None,
        "exit_reasons_pct": {k: round(c / n * 100, 1) for k, c in reasons.most_common()} if n else {},
        "entry_bar_exits": len(sb), "entry_bar_exit_reasons": dict(Counter(t.exit_reason for t in sb)),
        "R_gross_mean": round(stx.mean(rs), 3) if rs else None,
        "R_net_mean": round(stx.mean(rnet), 3) if rnet else None,
        "R_hist": hist(rs),
        "MFE_R_mean": round(stx.mean(m[0] for m in me), 2) if me else None,
        "MAE_R_mean": round(stx.mean(m[1] for m in me), 2) if me else None,
        "stop_pct_median": round(stx.median(stop_pct), 3) if stop_pct else None,
        "cost_per_trade_R_median": round(stx.median(cost_r), 3) if cost_r else None,
        "directions": dict(Counter(t.direction for t in trades)),
    }


def hist(rs):
    edges = [-math.inf, -2, -1.5, -1.0001, -0.5, 0, 0.5, 1, 1.5, 2, 3, math.inf]
    names = ["<-2", "-2..-1.5", "-1.5..-1", "-1..-0.5", "-0.5..0", "0..0.5", "0.5..1", "1..1.5", "1.5..2", "2..3", ">3"]
    c = Counter()
    for r in rs:
        for i in range(len(names)):
            if edges[i] < r <= edges[i + 1]:
                c[names[i]] += 1
                break
    return {k: c[k] for k in names if c[k]}


def schedule_for(eng, strat, trades, dev, rng, stocks):
    sched = {}
    if strat.interval == "5minute":
        p = strat.params
        if hasattr(p, "first_signal_time"):
            lo = hhmm(p.first_signal_time)
        else:   # opening-range strategies: the first bar after the range
            lo = (datetime(2000, 1, 1, 9, 15) + timedelta(minutes=p.range_minutes + 5)).time()
        hi = hhmm(p.last_signal_time)
        long_share = sum(1 for t in trades if t.direction == "LONG") / len(trades) if trades else 0.5
        for t in trades:
            sp = abs(t.signal_close - t.stop) / t.signal_close
            tp = abs(t.target - t.signal_close) / t.signal_close
            for _ in range(500):
                sym = rng.choice(stocks)
                bars = eng.five(sym).get(rng.choice(dev), [])
                if not eng.session_usable(bars):     # the engine would not trade an incomplete session
                    continue
                cands = [b.start + FIVE for b in bars if lo <= (b.start + FIVE).astimezone(IST).time() <= hi]
                if not cands:
                    continue
                asof = rng.choice(cands)
                reg = eng.regime.at_bar(asof).regime
                if reg not in strat.allowed_regimes or (sym, asof) in sched:
                    continue
                if reg == "TRENDING_UP":
                    direction = "LONG"
                elif reg == "TRENDING_DOWN":
                    direction = "SHORT"
                else:
                    direction = "LONG" if rng.random() < long_share else "SHORT"
                sched[(sym, asof)] = (direction, sp, tp)
                break
    else:
        ok_days = [d for d in dev[:-1] if eng.regime.at_close(d).regime in strat.allowed_regimes]
        if hasattr(strat, "index_ok"):     # v2 market filter: random days must pass it too
            ok_days = [d for d in ok_days if strat.index_ok(eng.index_daily(d))]
        for t in trades:
            days = {bar_date(b) for b in eng.daily(t.symbol)}
            cands = [d for d in ok_days if d in days]
            d = rng.choice(cands)
            asof = datetime.combine(d, time(15, 30), IST).astimezone(timezone.utc)
            sp = abs(t.signal_close - t.stop) / t.signal_close
            tp = abs(t.target - t.signal_close) / t.signal_close
            sched[(t.symbol, asof)] = (t.direction, sp, tp)
    return sched


def fmt_bar(b):
    return f"{b.start.astimezone(IST).strftime('%Y-%m-%d %H:%M')} O{b.open} H{b.high} L{b.low} C{b.close}"


def samples(eng, strat, trades, k=10):
    out = []
    step = max(1, len(trades) // k)
    for t in trades[::step][:k]:
        sig_t = datetime.fromisoformat(t.signal_time)
        if strat.interval == "5minute":
            day = eng.five(t.symbol)[sig_t.astimezone(IST).date()]
            sbar = next(b for b in day if b.start + FIVE == sig_t)
            xt = datetime.fromisoformat(t.exit_time)
            xbar = next((b for b in day if b.start + FIVE == xt or b.start == xt), None)
        else:
            sd = sig_t.astimezone(IST).date()
            sbar = next(b for b in eng.daily(t.symbol) if bar_date(b) == sd)
            xd = datetime.fromisoformat(t.exit_time).astimezone(IST).date()
            xbar = next(b for b in eng.daily(t.symbol) if bar_date(b) == xd)
        L = t.direction == "LONG"
        sides_ok = (t.stop < t.raw_entry < t.target) if L else (t.target < t.raw_entry < t.stop)
        out.append("\n".join([
            f"  {t.symbol} {t.direction} regime={t.regime}",
            f"    signal bar {fmt_bar(sbar)}   (signal close {t.signal_close})",
            f"    entry  bar {fmt_bar(t.entry_bar)}",
            f"    entry raw {t.raw_entry} fill {t.entry_price} qty {t.qty} | stop {t.stop} target {t.target} "
            f"| risk/share {risk_ps(t):.2f} ({risk_ps(t) / t.raw_entry * 100:.2f}%)",
            f"    exit   bar {fmt_bar(xbar) if xbar else '?'}  at {t.exit_time}",
            f"    exit raw {t.raw_exit} fill {t.exit_price} reason {t.exit_reason} | gross {t.gross_pnl:.2f} "
            f"costs {t.costs['total']:.2f} net {t.net_pnl:.2f} | sides ok: {sides_ok}, "
            f"open beyond stop: {(t.raw_entry <= t.stop) if L else (t.raw_entry >= t.stop)}",
        ]))
    return out


def print_summary(result):
    for sid, d in result.items():
        r, R = d["real"], d["random"]
        print(f"\n===== {sid}  dev {d['dev'][0]}..{d['dev'][1]}  trades {r['trades']}  directions {r['directions']}  "
              f"median stop {r['stop_pct_median']}%  median cost per trade {r['cost_per_trade_R_median']}R")
        if not r["trades"]:
            continue
        for k in ("none", "slip_only", "charges_only", "both"):
            xs = sorted(x["pf"][k] for x in R if x["pf"][k] is not None)
            if xs:
                print(f"  PF {k:13s} real {r['pf'][k]}  random mean {stx.mean(xs):.3f} (min {xs[0]}, max {xs[-1]})"
                      f"{'  REAL BELOW EVERY SEED' if r['pf'][k] is not None and r['pf'][k] < xs[0] else ''}")
        if r.get("filtered"):
            print(f"  filtered signals {r['filtered']}")
        print(f"  mean R (no costs) {r['R_gross_mean']}  net {r['R_net_mean']}  | win% (no costs) {r['win_rate_none']}")
        print(f"  exits {r['exit_reasons_pct']}  | entry-bar exits {r['entry_bar_exits']} {r['entry_bar_exit_reasons']}")
        print(f"  MFE {r['MFE_R_mean']}R  MAE {r['MAE_R_mean']}R  | R histogram {r['R_hist']}")
        bad = [x["checks"] for x in R if x["checks"]]
        print(f"  mechanical problems: real {r['checks'] or 'none'}, random {bad or 'none'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strategy", nargs="*", help="strategy ids (default: all active)")
    ap.add_argument("--seeds", type=int, default=20)
    args = ap.parse_args()
    SEEDS = args.seeds
    eng = DiagEngine(s, store, "breeze", calendar=load_calendar(s, store))
    stocks = [(i.symbol, i.kind) for i in s.watchlist if i.kind == "stock"]
    result = {}
    for strat in build_strategies(s, args.strategy):
        dev, hold = dev_holdout(s, eng.sessions(strat), strat.interval)
        strat.rejections.clear()
        trades, skipped = eng.run(strat, dev, stocks)
        real = summarize(eng, trades, strat.interval, "real")
        real["checks"] = dict(checks(eng, trades, strat.interval))
        real["skipped"] = dict(skipped)
        real["filtered"] = dict(getattr(strat, "rejections", {}))
        rand = []
        for seed in range(SEEDS if trades else 0):
            rng = random.Random(seed)
            rs = RandomEntry(strat, schedule_for(eng, strat, trades, dev, rng, [x for x, _ in stocks]))
            rt, rsk = eng.run(rs, dev, stocks)
            r = summarize(eng, rt, strat.interval, f"seed{seed}")
            r["checks"] = dict(checks(eng, rt, strat.interval))
            rand.append(r)
        result[strat.id] = {"real": real, "random": rand, "samples": samples(eng, strat, trades),
                            "dev": [dev[0].isoformat(), dev[-1].isoformat()], "holdout_from": hold[0].isoformat()}
        print(strat.id, "done", len(trades), flush=True)
    out_dir = ROOT / "data" / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("diag_" + ("_".join(args.strategy) if args.strategy else "all") + ".json")
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print_summary(result)
    print("\nsaved", out)


if __name__ == "__main__":
    main()
