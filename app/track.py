"""Phase 6 track records: a strategy's (and a trade type's) results from its stored backtest and from its paper
trades, each with the number of trades behind it. Under `backtest.min_trades` (30) trades a figure is marked
"low sample". With no recorded trade there is no hit rate at all: a probability of profit is never shown unless
recorded trades back it.

Backtest figures come from the newest stored run of the strategy id (its `report.overall`): trades, win rate and net
result after costs. A run on simulated data is labelled so. Paper figures come from the closed paper trades.
"""
from __future__ import annotations

from decimal import Decimal

from .journal import TRADE_TYPE_NAMES, TRADE_TYPES

# the strategies that produce each trade type (phase 6 live loop; stock option selling is the phase 9 strategy)
TYPE_STRATEGIES = {
    "INTRADAY_STOCKS": ("orb", "vwap_pullback", "range_reversion", "orb_v2", "range_reversion_v2"),
    "SWING_STOCKS": ("swing_pullback", "swing_breakout", "swing_pullback_v2", "swing_breakout_v2"),
    "INDEX_OPTION_BUYING": ("long_option",),
    "INDEX_OPTION_SELLING": ("credit_spread", "iron_condor"),
    "STOCK_OPTION_BUYING": ("long_option",),
    "STOCK_OPTION_SELLING": ("stock_option_selling",),
}


def _stats(results: list[float], min_trades: int) -> dict:
    n = len(results)
    if n == 0:
        return {"trades": 0, "win_rate_pct": None, "net_inr": None, "avg_inr": None, "low_sample": True,
                "note": "no trades recorded: no hit rate"}
    wins = sum(1 for r in results if r > 0)
    net = float(sum((Decimal(str(r)) for r in results), Decimal(0)))
    return {"trades": n, "win_rate_pct": round(wins / n * 100, 1), "net_inr": round(net, 2),
            "avg_inr": round(net / n, 2), "low_sample": n < min_trades,
            "note": f"low sample: {n} trades (fewer than {min_trades})" if n < min_trades else f"{n} trades"}


def latest_backtests(store) -> dict[str, dict]:
    """strategy_id -> the newest stored run (report.overall)."""
    out: dict[str, dict] = {}
    for r in store.backtest_runs(200):
        out.setdefault(r["strategy_id"], r)
    return out


def backtest_record(run: dict | None, min_trades: int) -> dict:
    if run is None:
        return {**_stats([], min_trades), "source": "backtest", "run_id": None}
    o = (run.get("report") or {}).get("overall") or {}
    n = int(o.get("trades") or 0)
    net = o.get("net_pnl_inr")
    sim = (run.get("report") or {}).get("data_source") == "sim"
    return {"source": "backtest", "run_id": run["run_id"], "version": run.get("version"), "trades": n,
            "win_rate_pct": o.get("win_rate_pct") if n else None, "net_inr": net if n else None,
            "avg_inr": round(net / n, 2) if n and net is not None else None, "low_sample": n < min_trades,
            "note": ("SIMULATED DATA; " if sim else "") + (f"low sample: {n} trades (fewer than {min_trades})"
                                                          if n < min_trades else f"{n} trades")}


def paper_record(journal, strategy_ids, min_trades: int, trade_type: str | None = None) -> dict:
    rows = [t for t in journal.paper_trades(status="CLOSED")
            if t["strategy_id"] in strategy_ids and (trade_type is None or t["trade_type"] == trade_type)
            and t["net_pnl_inr"] is not None]
    return {**_stats([t["net_pnl_inr"] for t in rows], min_trades), "source": "paper"}


def strategy_record(store, journal, settings, strategy_id: str, runs: dict | None = None) -> dict:
    m = settings.backtest.min_trades
    runs = runs if runs is not None else latest_backtests(store)
    status = settings.strategies.status_of(strategy_id)
    return {"strategy_id": strategy_id, "backtest": backtest_record(runs.get(strategy_id), m),
            "paper": paper_record(journal, (strategy_id,), m), "label": status.label, "alerts": status.alerts,
            "note": "past results with the number of trades behind them; not a probability of profit"}


def type_record(store, journal, settings, trade_type: str, runs: dict | None = None) -> dict:
    """The track record beside a trade-type switch: backtest and paper trades of its strategies, and its label:
    UNPROVEN when nothing is recorded, else the strategies' status labels (e.g. "negative in backtests")."""
    m = settings.backtest.min_trades
    runs = runs if runs is not None else latest_backtests(store)
    ids = TYPE_STRATEGIES[trade_type]
    bts = [backtest_record(runs.get(i), m) for i in ids]
    n = sum(b["trades"] for b in bts)
    nets = [b["net_inr"] for b in bts if b["net_inr"] is not None]
    wins = sum(round((b["win_rate_pct"] or 0) * b["trades"] / 100) for b in bts)
    bt = {"source": "backtest", "trades": n, "win_rate_pct": round(wins / n * 100, 1) if n else None,
          "net_inr": round(sum(nets), 2) if nets else None, "low_sample": n < m,
          "note": "no backtest trades" if n == 0 else (f"low sample: {n} trades (fewer than {m})" if n < m else f"{n} trades"),
          "by_strategy": {i: b for i, b in zip(ids, bts)}}
    paper = paper_record(journal, ids, m, trade_type)
    labels = sorted({settings.strategies.status_of(i).label for i in ids})
    unproven = n == 0 and paper["trades"] == 0
    return {"type": trade_type, "name": TRADE_TYPE_NAMES[trade_type], "strategies": list(ids), "backtest": bt,
            "paper": paper, "label": "UNPROVEN: no track record yet" if unproven else "; ".join(labels),
            "status_labels": labels,
            "alerts_allowed_by_status": [i for i in ids if settings.strategies.status_of(i).alerts]}


def all_type_records(store, journal, settings) -> dict[str, dict]:
    runs = latest_backtests(store)
    return {t: type_record(store, journal, settings, t, runs) for t in TRADE_TYPES}
