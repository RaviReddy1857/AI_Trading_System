"""Phase 6 step 9: the pre-market plan (`alerts.premarket_time`, 09:00 IST) and the end-of-day summary
(`alerts.end_of_day_time`, 15:45 IST), built by code from stored data. Each is logged once a trading day
(key `premarket:<day>` / `eod:<day>`); the plan is not sent once the market has opened, the summary any time
that evening. Both are also served at GET /api/plan."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .config import hhmm
from .journal import POOLS, TOOK, ist_day
from .quality import IST, MARKET_OPEN


def fmt(x) -> str:
    return "-" if x is None else f"{x:,.2f}"


def week_coverage_of(settings, store, day, end):
    """WeekCoverage of the watchlist stocks for the trading week ending `end` (its Friday at the latest)."""
    from .manual_data import week_coverage
    monday = day - timedelta(days=day.weekday())
    through = min(end, monday + timedelta(days=4))
    stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
    return week_coverage(store.all_events(), stocks, day, through, settings.data_status.coverage_max_age_days)


def events_week_text(settings, store, day, end) -> str:
    """The events listed from `day` to `end`. "None" is said only when every watchlist stock's results dates are
    covered for the week; otherwise EVENT COVERAGE MISSING, with any events that are known after it."""
    ev = [e for e in store.events_between(day, end) if e["type"] != "COVERAGE"]
    listed = "; ".join(f"{e['date']:%d-%b} {e['type']}" + (f" {e['symbol']}" if e["symbol"] else "") for e in ev[:6])
    missing = week_coverage_of(settings, store, day, end).text()
    if missing:
        return missing + (f"; known: {listed}" if listed else "")
    return listed or "none (results dates covered for every watchlist stock)"


def premarket_plan(settings, store, journal, regime, now: datetime, source: str = "sim") -> str:
    day = ist_day(now)
    r = regime.evaluate(now) if regime is not None else {}
    lines = [f"PRE-MARKET PLAN {day:%a %d-%b-%Y}",
             f"Regime: {r.get('regime', 'UNKNOWN')}; FII bias: {r.get('bias', 'UNKNOWN')}"]
    for why in (r.get("reasons") or [])[:2]:
        lines.append(f"  - {why}")
    monday = day - timedelta(days=day.weekday())
    try:
        lines.append("Events this week: " + events_week_text(settings, store, day, monday + timedelta(days=6)))
    except Exception:
        lines.append("Events this week: not known")
    open_pos = journal.positions("OPEN")
    symbols = [settings.regime.index_symbol, "BANKNIFTY"] + sorted({p.symbol for p in open_pos})
    lines.append("Levels (previous session high / low / close):")
    for sym in dict.fromkeys(symbols):
        bars = store.history_bars("bars_1d", sym, source, limit=1)
        if bars:
            b = bars[-1]
            lines.append(f"  {sym}: {fmt(b.high)} / {fmt(b.low)} / {fmt(b.close)}")
        else:
            lines.append(f"  {sym}: no stored daily bar")
    lines.append(f"Open positions: {len(open_pos)}" + "".join(
        f"\n  {p.symbol} {p.trade_type_name if hasattr(p, 'trade_type_name') else p.trade_type} stop {fmt(p.stop)}"
        + (" UNHEDGED" if not p.hedged else "") for p in open_pos))
    lines.append("Research only: you place every order yourself.")
    return "\n".join(lines)


def eod_summary(settings, journal, now: datetime, risk=None) -> str:
    day = ist_day(now)
    start = datetime.combine(day, time(0, 0), IST).astimezone(timezone.utc)
    sigs = [s for s in journal.signals_since(start) if ist_day(datetime.fromisoformat(s["created_utc"])) == day]
    decisions = journal.decisions()
    approved = [s for s in sigs if s["status"] == "APPROVED"]
    took = sum(1 for s in approved if (decisions.get(s["signal_id"]) or {}).get("decision") == TOOK)
    skipped = sum(1 for s in approved if (decisions.get(s["signal_id"]) or {}).get("decision") == "SKIPPED")
    paper = [t for t in journal.paper_trades(status="CLOSED") if t["exit_utc"] and ist_day(datetime.fromisoformat(t["exit_utc"])) == day]
    paper_net = sum(t["net_pnl_inr"] or 0 for t in paper)
    lines = [f"END OF DAY {day:%a %d-%b-%Y}",
             f"Signals: {len(sigs)} ({len(approved)} approved, {len(sigs) - len(approved)} NO TRADE); taken {took}, "
             f"skipped {skipped}",
             f"Paper: {len(paper)} closed today, net Rs {fmt(paper_net)} after costs"]
    for pool in POOLS:
        r = journal.realised(pool, since=day, until=day)
        lines.append(f"Realised today, {pool.replace('_', ' ').lower()} pool: Rs {fmt(None if r is None else float(r))}")
    open_pos = journal.positions("OPEN")
    lines.append(f"Open positions: {len(open_pos)}")
    for p in open_pos:
        stop = p.plan.get("plan_stop", p.stop)
        lines.append(f"  {p.symbol} {p.trade_type} {p.quantity} {'lots' if p.is_option else 'shares'}, entry "
                     f"{fmt(p.entry_price)}, stop {fmt(stop)}" + (" UNHEDGED" if not p.hedged else ""))
    open_paper = journal.paper_trades(status="OPEN")
    lines.append(f"Open paper trades: {len(open_paper)}")
    from .options.after_close import summary_lines
    try:
        lines += summary_lines(settings, journal, day, now)
    except Exception as e:                       # noqa: BLE001 - the summary is sent whatever happens here
        lines.append(f"Stock option shortlist: NOT KNOWN - its result could not be read ({type(e).__name__})")
    return "\n".join(lines)


def scheduled(notifier, now: datetime) -> None:
    s = notifier.s
    day = ist_day(now)
    if not notifier.cal.is_trading_day(day):
        return
    t = now.astimezone(IST).time()
    if hhmm(s.alerts.premarket_time) <= t < MARKET_OPEN and notifier.book.get(f"premarket:{day}") is None:
        text = premarket_plan(s, notifier.store, notifier.j, notifier.regime, now, getattr(notifier, "source", "sim"))
        notifier.book.enqueue(f"premarket:{day}", "PREMARKET", text, now,
                              datetime.combine(day, MARKET_OPEN, IST).astimezone(timezone.utc))
    if t >= hhmm(s.alerts.end_of_day_time) and notifier.book.get(f"eod:{day}") is None:
        notifier.book.enqueue(f"eod:{day}", "EOD", eod_summary(s, notifier.j, now), now,
                              datetime.combine(day, time(23, 59), IST).astimezone(timezone.utc))
