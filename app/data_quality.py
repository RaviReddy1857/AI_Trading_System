"""Data-quality exclusion of stock-years from the Phase 9 backtests (pre-registered 2026-11-15, before any
2023-2024 option backtest).

A (stock, year) is EXCLUDED when, unless it is listed as resolved (config
`data_quality.resolved_stock_years`, with the reason recorded in docs/STATUS.md):
  (a) its UNEXPLAINED close mismatches between Breeze and the external repo exceed `max_unexplained_pct`
      (5%) of the year's sessions compared, or
  (b) the Breeze daily closes contain any one-day move larger than `max_one_day_jump_pct` (20%, up or down)
      on a session that is not the ex-date of a split, bonus or demerger (any source).
"Unexplained" = a stored mismatch over 0.5% with no explanation (price_crosscheck.explained_by empty); a
mismatch "possibly" explained by a later demerger or rights issue is not counted as unexplained. A jump
belongs to the year of the session it lands on. A year with no external closes to compare is judged on
rule (b) alone.

The same exclusion applies to the strategy, every variant and the baseline: no entry is taken in an
excluded stock-year, nor in the `ENTRY_MARGIN_DAYS` calendar days before one (a position opened there
would live through it).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .history import bar_date

ENTRY_MARGIN_DAYS = 45
PRICE_ACTIONS = ("SPLIT", "BONUS", "DEMERGER")


def assess(settings, store, stocks: list[str], source: str = "breeze", repo_closes=None) -> dict[tuple[str, int], dict]:
    """{(stock, year): {sessions, compared, unexplained, unexplained_pct, jumps, excluded, reasons, resolved}}.
    `repo_closes(sym) -> {date: close}` (optional): evidence for each jump, whether the repo shows the same
    one-day move (within 3 points); the rule itself does not use it."""
    cfg = settings.data_quality
    resolved = {x.strip().upper() for x in cfg.resolved_stock_years}
    mism = defaultdict(int)
    mism_days = defaultdict(set)
    for r in store.crosscheck_rows():
        if r["symbol"] in stocks:
            mism_days[r["symbol"]].add(r["date"])
            if not r["explained_by"]:
                mism[(r["symbol"], int(r["date"][:4]))] += 1
    compared_from = settings.external_data.history_since
    out = {}
    for sym in stocks:
        bars = store.history_bars("bars_1d", sym, source)
        rc = repo_closes(sym) if repo_closes is not None else None
        ex_dates = {date.fromisoformat(a["ex_date"]) for a in store.corporate_actions(sym, PRICE_ACTIONS)}
        by_year = defaultdict(lambda: {"sessions": 0, "jumps": []})
        for k, b in enumerate(bars):
            d = bar_date(b)
            y = by_year[d.year]
            y["sessions"] += 1
            if k and bars[k - 1].close > 0:
                move = (b.close / bars[k - 1].close - 1) * 100
                prev = bar_date(bars[k - 1])
                action = any(prev < x <= d for x in ex_dates)
                if abs(move) > cfg.max_one_day_jump_pct and not action:
                    # evidence only (the rule does not use it): the repo's own move on the same day
                    agrees = None
                    if rc and d in rc and prev in rc and rc[prev] > 0:
                        agrees = abs((rc[d] / rc[prev] - 1) * 100 - move) <= 3.0
                    y["jumps"].append({"date": d.isoformat(), "move_pct": round(move, 1), "repo_shows_same_move": agrees})
        for year, y in by_year.items():
            n = y["sessions"]
            u = mism.get((sym, year), 0)
            pct = round(u / n * 100, 2) if n else None
            reasons = []
            if pct is not None and pct > cfg.max_unexplained_pct:
                reasons.append(f"unexplained mismatches {u} of {n} sessions ({pct}%) > {cfg.max_unexplained_pct:g}%")
            if y["jumps"]:
                tagj = {True: " (repo: same move)", False: " (repo: differs)", None: " (repo: not compared)"}
                reasons.append("one-day jump(s) over {:g}% with no corporate action: ".format(cfg.max_one_day_jump_pct)
                               + ", ".join(f"{j['date']} {j['move_pct']:+.1f}%{tagj[j['repo_shows_same_move']]}"
                                           for j in y["jumps"]))
            key = f"{sym}:{year}"
            out[(sym, year)] = {"sessions": n, "unexplained": u, "unexplained_pct": pct, "jumps": y["jumps"],
                                "reasons": reasons, "resolved": key in resolved,
                                "excluded": bool(reasons) and key not in resolved}
    return out


def excluded_sessions(assessment: dict, sessions: list[date]) -> dict[str, set[date]]:
    """Per stock, the sessions on which no entry may be taken: those of an excluded year and the
    ENTRY_MARGIN_DAYS before it."""
    bad = defaultdict(set)
    for (sym, year), v in assessment.items():
        if v["excluded"]:
            bad[sym].add(year)
    out = {}
    for sym, years in bad.items():
        out[sym] = {d for d in sessions if d.year in years or (d + timedelta(days=ENTRY_MARGIN_DAYS)).year in years}
    return out


def excluded_list(assessment: dict) -> list[dict]:
    return [{"stock": s, "year": y, **{k: v[k] for k in ("sessions", "unexplained", "unexplained_pct", "reasons")}}
            for (s, y), v in sorted(assessment.items()) if v["excluded"]]
