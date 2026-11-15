"""The Data status page: one row per data source, with how fresh it is, what stops working when it is
not, and exactly what to do. A STALE, MISSING or PARTIAL source is never shown as OK, and the reason a
signal is blocked is never hidden.

Statuses: OK (fresh), STALE (older than its limit), MISSING (never loaded), PARTIAL (some stocks or years
missing), INFO (informational, not counted), DATA_ERROR (rows dated after today). Colours: OK green; STALE
and PARTIAL amber; MISSING and DATA_ERROR red; the Breeze session red when it is not confirmed for today.

Rows dated after today (after now, for timestamps) are never used to judge freshness: a date in the future
cannot make a source look up to date. Every source on the page leaves them out and flags them as a data
error (status DATA_ERROR, the rows listed, and the freshness without them in the detail). Dates that may
lie ahead by nature are not flagged: meeting dates of RESULTS events, ex-dates of announced corporate
actions, covered_through of a COVERAGE row and RBI meetings entered as 'scheduled'.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from .config import Settings
from .history import bar_date
from .manual_data import coverage_fields
from .models import utcnow
from .quality import IST
from .store import Store

OK, STALE, MISSING, PARTIAL, INFO, DATA_ERROR = "OK", "STALE", "MISSING", "PARTIAL", "INFO", "DATA_ERROR"
COLOUR = {OK: "green", STALE: "amber", PARTIAL: "amber", MISSING: "red", INFO: "grey", DATA_ERROR: "red"}

NSE_FII_DII = "https://www.nseindia.com/reports/fii-dii"
NSE_BOARD = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"
NSE_ACTIONS = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
RBI_PAGE = "https://www.rbi.org.in (Monetary Policy > Monetary Policy Statements)"

HOW_TO_UPDATE = {
    "daily": [
        "Before the market opens: log in to ICICI Breeze with the login link, put the new apisession number in .env "
        "(BREEZE_SESSION_TOKEN) and restart the app. The Breeze session row turns green once a price or a download "
        "arrives.",
        f"After the market closes: open {NSE_FII_DII}, press Download (.csv) for FII/FPI & DII trading activity, put the "
        "file in data/inbox/ and press Import inbox files (or run python -m app.inbox). Or paste the table into the "
        "FII/DII box on the main page.",
        "The app fetches the daily stock history itself after you log in (python -m app.history does the same).",
    ],
    "weekly": [
        f"Board meetings (results dates): open {NSE_BOARD}, choose a date range from today through at least the next "
        "monthly expiry, with NO symbol, index or purpose filter, Download (.csv) and put it in data/inbox/. Enter the "
        "same from and through dates on this page, tick the all-companies confirmation and press Import inbox files. "
        "This adds RESULTS events and a COVERAGE row for every watchlist stock through your end date. A file with few "
        "companies, or whose meeting dates do not span the dates you entered, is rejected.",
        f"Corporate actions: open {NSE_ACTIONS}, choose Equity and the coming weeks, Download (.csv), put it in "
        "data/inbox/ and press Import inbox files.",
        "Stock option history: python -m app.options.stock_backtest --download after a fresh Breeze login (within the "
        "daily request budget; it resumes where it stopped).",
    ],
    "after each RBI meeting": [
        f"Enter the decision with the RBI form on this page (date, decision, e.g. 'repo unchanged at 5.25%'), from "
        f"{RBI_PAGE}. Enter the next meetings' dates with decision 'scheduled' so a missed entry turns amber.",
    ],
}


def fmt_ist(value) -> str | None:
    """A time in IST as "15 Nov 2026, 10:30 AM"; a date alone as "15 Nov 2026". ISO text, datetime or date;
    None stays None. A time without a zone is taken as UTC (the store keeps UTC)."""
    if value is None or value == "":
        return None
    v = value
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v) if ("T" in v or " " in v.strip()) else date.fromisoformat(v)
        except ValueError:
            return value
    if isinstance(v, datetime):
        t = (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(IST)
        return f"{t.day} {t:%b %Y}, {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"
    return f"{v.day} {v:%b %Y}"


def _age_days(when: datetime | date | None, today: date) -> int | None:
    if when is None:
        return None
    d = when.astimezone(IST).date() if isinstance(when, datetime) else when
    return (today - d).days


def _row(source, last, coverage, status, blocks, todo, detail=None) -> dict:
    raw = last.isoformat() if isinstance(last, (date, datetime)) else last
    return {"source": source, "last_updated": fmt_ist(raw), "last_updated_iso": raw, "coverage": coverage,
            "status": status, "colour": COLOUR[status],
            "blocks": blocks, "what_to_do": todo, "detail": detail or {}}


def _flag_future(row: dict, future: list[str], what: str) -> dict:
    """Mark a row DATA_ERROR when rows dated after today were found (they were already left out of its
    freshness); the freshness without them stays in the detail."""
    if not future:
        return row
    row["detail"] = {**row["detail"], "future_dated": future[:20], "future_dated_count": len(future),
                     "status_without_them": row["status"]}
    row["status"], row["colour"] = DATA_ERROR, COLOUR[DATA_ERROR]
    row["coverage"] = f"{row['coverage']}; DATA ERROR: {len(future)} {what} dated after today (ignored)"
    row["what_to_do"] = (f"Fix or delete the {what} dated after today ({', '.join(future[:5])}"
                         f"{' ...' if len(future) > 5 else ''}), then re-import. "
                         + ("" if row["what_to_do"] == "-" else row["what_to_do"]))
    return row


def _ts(text: str) -> datetime:
    """An ISO timestamp; one without a zone is UTC (the store keeps UTC)."""
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _after(ts: str | None, now: datetime) -> bool:
    """An ISO timestamp later than now."""
    return bool(ts) and _ts(ts) > now


def trading_days_after(cal, a: date, b: date) -> int:
    """Trading days after a, up to and including b."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if cal.is_trading_day(d):
            n += 1
    return n


def data_status(settings: Settings, store: Store, calendar, feed_name: str, live_ticks_today: bool = False,
                manual=None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    today = now.astimezone(IST).date()
    last_session = today if calendar.is_trading_day(today) and now.astimezone(IST).hour >= 16 else calendar.previous_trading_day(today)
    lim = settings.data_status
    rows = []
    stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
    status_file = settings.path("data/option_download_status.json")
    try:
        dl = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dl = {}
    priority = list(dl.get("stocks") or stocks[:settings.stock_option_selling.backtest.priority_stocks])
    imports = store.last_imports()

    # 1. Breeze session (never read from .env: confirmed only by data that arrived today)
    with store._lock:
        fetched_today = store._db.execute("SELECT MAX(fetched_utc) FROM option_fetch_log WHERE source='breeze' AND rows>0 "
                                          "AND fetched_utc>=? AND fetched_utc<=?",
                                          (datetime.combine(today, datetime.min.time(), IST).astimezone(timezone.utc)
                                           .isoformat(), now.astimezone(timezone.utc).isoformat())).fetchone()[0]
        future_fetch = [r[0] for r in store._db.execute(
            "SELECT DISTINCT fetched_utc FROM option_fetch_log WHERE fetched_utc>? ORDER BY fetched_utc",
            (now.astimezone(timezone.utc).isoformat(),)).fetchall()]
    confirmed = (feed_name == "breeze" and live_ticks_today) or fetched_today is not None
    rows.append(_row("Breeze session", fetched_today or ("live prices today" if live_ticks_today else None),
                     f"feed: {feed_name}", OK if confirmed else STALE,
                     "live prices, option chains, actionable signals, option history downloads",
                     "-" if confirmed else "Log in: open the login link, put the apisession number in .env, restart the app",
                     {"red": not confirmed,
                      "how_confirmed": "live prices received today or an option-history download that returned data today; "
                                       "the token itself is never read"}))
    if not confirmed:
        rows[-1]["colour"] = "red"
    _flag_future(rows[-1], future_fetch, "download log entries")

    # 2. Breeze option history download
    planned_years = list(range(settings.stock_option_selling.backtest.download_first_years_from, today.year + 1))
    all_years = list(range((settings.backtest.test_start.get("1day") or date(2019, 1, 22)).year, today.year + 1))
    done = {s: set(v) for s, v in (dl.get("complete") or {}).items()}
    pri_done = sum(1 for s in priority for y in planned_years if y in done.get(s, set()))
    all_done = sum(1 for s in priority for y in all_years if y in done.get(s, set()))
    from .options.option_history import todays_budget
    used = store.requests_used(today)
    budget = todays_budget(settings, store, today)
    default = settings.options.backtest.daily_request_budget
    budget_text = f"{budget:,}" + ("" if budget == default else f" (set for today only; default {default:,})")
    need_pri = len(priority) * len(planned_years)
    st = MISSING if not done else (OK if pri_done == need_pri and all_done == len(priority) * len(all_years) else PARTIAL)
    dl_future = [dl["updated"]] if _after(dl.get("updated"), now) else []
    rows.append(_row("Breeze option history (stock options)", None if dl_future else dl.get("updated"),
                     f"{pri_done} of {need_pri} stock-years {planned_years[0]}-{planned_years[-1]} complete; "
                     f"{all_done} of {len(priority) * len(all_years)} for {all_years[0]}-{all_years[-1]}; "
                     f"{used:,} of {budget_text} requests used today",
                     st, "backtests on the stocks and years not complete (they are left out of the result)",
                     "-" if st == OK else "After a fresh Breeze login run: python -m app.options.stock_backtest --download "
                                          "(stops at the daily budget and resumes the next day)",
                     {"requests_today": used, "budget": budget, "budget_default": default}))
    _flag_future(rows[-1], dl_future, "download progress updates")

    # 3. FII/DII
    fd_all = store.fii_dii_all()
    fd = [r for r in fd_all if r["date"] <= today]
    fd_future = [r["date"].isoformat() for r in fd_all if r["date"] > today]
    last_fd = fd[-1]["date"] if fd else None
    if last_fd is None:
        st, cov = MISSING, "no rows"
    else:
        behind = trading_days_after(calendar, last_fd, last_session)
        st = OK if behind <= lim.fii_dii_max_trading_days else STALE
        cov = f"{fd[0]['date'].isoformat()} to {last_fd.isoformat()} ({len(fd)} days); {behind} trading day(s) behind"
    rows.append(_row("FII/DII cash flows", last_fd.isoformat() if last_fd else None, cov, st,
                     "FII bias and the FII scenarios; the FII-streak alert",
                     "-" if st == OK else f"Open {NSE_FII_DII}, Download (.csv) (FII/FPI & DII trading activity), put the file "
                                          f"in data/inbox/ and press Import inbox files; or paste it into the FII/DII box on "
                                          f"the main page"))
    _flag_future(rows[-1], fd_future, "FII/DII rows")

    # 4. results dates and coverage
    events = store.all_events()
    cov_future = [f"{e['symbol']} {e['date'].isoformat()}" for e in events if e["type"] == "COVERAGE" and e["date"] > today]
    cov_rows = [e for e in events if e["type"] == "COVERAGE" and e["date"] <= today]      # retrieved on or before today
    newest_cov = {}
    for e in cov_rows:
        f = coverage_fields(e["note"])
        if f is None:
            continue
        prev = newest_cov.get(e["symbol"])
        if prev is None or e["date"] > prev[0]:
            newest_cov[e["symbol"]] = (e["date"], f[1], f[0])
    from .options.instruments import InstrumentBook
    book = InstrumentBook(store, feed_name, max_age_days=36500)
    missing_cov, old_cov = [], []
    for s in priority:
        nxt = next((x.expiry for x in book.specs(s, today) if x.kind == "monthly"), None)
        c = newest_cov.get(s)
        if c is None or nxt is None or c[1] < nxt:
            missing_cov.append(s + ("" if nxt else " (next expiry unknown)"))
        elif (today - c[0]).days > lim.coverage_max_age_days:
            old_cov.append(s)
    results = [e for e in events if e["type"] == "RESULTS"]
    if not cov_rows:
        st = MISSING
    elif missing_cov or old_cov:
        st = STALE
    else:
        st = OK
    rows.append(_row("Results dates and event coverage", max((e["date"] for e in cov_rows), default=None),
                     f"{len(results)} RESULTS events; coverage for {len(priority) - len(missing_cov)} of {len(priority)} "
                     f"priority stocks through their next expiry", st,
                     "actionable stock option signals (EVENT_COVERAGE_UNKNOWN) for the stocks listed",
                     "-" if st == OK else f"Open {NSE_BOARD}, choose dates from today through the next monthly expiry (no "
                                          f"filter), Download (.csv), put it in data/inbox/, enter the same dates and tick "
                                          f"the all-companies box on this page, and press Import inbox files",
                     {"stocks_without_coverage": missing_cov, "coverage_older_than_limit": old_cov}))
    _flag_future(rows[-1], cov_future, "COVERAGE rows (retrieved_at)")

    # 5. corporate actions
    ca_last = [v for k, v in imports.items() if k == "corporate_actions"]
    acts = store.corporate_actions()
    ca_future = sorted({a["imported_utc"] for a in acts if _after(a.get("imported_utc"), now)})
    stamps = [a["imported_utc"] for a in acts if a.get("imported_utc") and not _after(a["imported_utc"], now)]
    if not stamps:
        st, last = MISSING, None
    else:
        last = max(stamps, key=_ts)
        st = OK if _age_days(datetime.fromisoformat(last), today) <= lim.corporate_actions_max_age_days else STALE
    srcs = sorted({a["source"] for a in acts})
    rows.append(_row("Corporate actions", last, f"{len(acts)} actions ({', '.join(srcs) or 'none'}); latest ex-date "
                                                f"{max((a['ex_date'] for a in acts), default='-')}", st,
                     "split/bonus/demerger adjustment of daily history (trend, 52-week low, range, profile): new actions "
                     "are not applied, and their price breaks cut the history (INSUFFICIENT_HISTORY)",
                     "-" if st == OK else f"Open {NSE_ACTIONS}, choose Equity and the coming weeks, Download (.csv), put it in "
                                          f"data/inbox/ and press Import inbox files"))
    _flag_future(rows[-1], ca_future, "corporate-action imports (imported time)")

    # 6. RBI policy dates
    rbi = [e for e in events if e["type"] == "RBI_POLICY"]
    sched = [e["date"] for e in rbi if "scheduled" in (e["note"] or "").lower()]
    rbi_future = [e["date"].isoformat() for e in rbi if "scheduled" not in (e["note"] or "").lower() and e["date"] > today]
    decided = [e["date"] for e in rbi if "scheduled" not in (e["note"] or "").lower() and e["date"] <= today]
    overdue = [d for d in sched if d + timedelta(days=lim.rbi_decision_grace_days) < today
               and not any(d - timedelta(days=1) <= x <= d + timedelta(days=lim.rbi_decision_grace_days) for x in decided)]
    upcoming = sorted(d for d in sched if d >= today)
    if not rbi:
        st, why = MISSING, "no RBI dates"
    elif overdue:
        st, why = STALE, f"scheduled meeting(s) without a decision: {', '.join(d.isoformat() for d in overdue)}"
    elif not upcoming:
        st, why = STALE, "no coming meeting is entered as 'scheduled', so a missed decision cannot be noticed"
    else:
        st, why = OK, f"next scheduled {upcoming[0].isoformat()}"
    rows.append(_row("RBI policy dates", max(decided).isoformat() if decided else None,
                     f"{len(decided)} decisions; latest {max(decided).isoformat() if decided else '-'}; {why}", st,
                     "the EVENT regime on RBI days (risk is not flagged on a missing date)",
                     "-" if st == OK else f"Enter the decision (and the next meetings as 'scheduled') with the RBI form on "
                                          f"this page, from {RBI_PAGE}"))
    _flag_future(rows[-1], rbi_future, "RBI decisions (a decision cannot be dated after today)")

    # 7. external repo import (informational)
    ext_all = {k: v for k, v in imports.items() if v["source"] == "external_repo"}
    ext = {k: v for k, v in ext_all.items() if not _after(v["imported_utc"], now)}
    ext_future = sorted(f"{k} {v['imported_utc']}" for k, v in ext_all.items() if k not in ext)
    if ext:
        any_ = next(iter(ext.values()))
        rows.append(_row("External repo import (aswinv90/indian-market-historical-data)", any_["imported_utc"],
                         "; ".join(f"{k} {v['rows']}" for k, v in sorted(ext.items())), INFO,
                         "nothing (informational; personal use only, never published)",
                         "-", {"commit": any_["source_ref"]}))
    else:
        rows.append(_row("External repo import (aswinv90/indian-market-historical-data)", None, "not imported", INFO,
                         "nothing (informational)", "Optional: python -m app.external_data after cloning the repo"))
    _flag_future(rows[-1], ext_future, "import records")

    # 8. stock daily history (Breeze)
    behind, per, bars_future = [], {}, []
    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), IST).astimezone(timezone.utc)
    for s in priority:
        ahead = store.history_bars("bars_1d", s, feed_name, since=tomorrow)
        bars_future += [f"{s} {bar_date(x).isoformat()}" for x in ahead]
        b = store.history_bars("bars_1d", s, feed_name, before=tomorrow, limit=1)
        last_d = bar_date(b[-1]) if b else None
        per[s] = last_d.isoformat() if last_d else None
        if last_d is None or last_d < last_session:
            behind.append(s)
    st = MISSING if all(v is None for v in per.values()) else (PARTIAL if behind else OK)
    rows.append(_row("Stock daily history (Breeze)", max((v for v in per.values() if v), default=None),
                     f"{len(priority) - len(behind)} of {len(priority)} priority stocks up to {last_session.isoformat()}", st,
                     "the shortlist, profiles and research candidates of the stocks behind (DATA_STALE)",
                     "-" if st == OK else "After a fresh Breeze login restart the app (it updates history on start) or run "
                                          "python -m app.history",
                     {"behind": behind, "last_date_per_stock": per}))
    _flag_future(rows[-1], bars_future, "daily bars")

    if manual is not None:
        for p in manual.problems():
            rows.append(_row("Your data files", None, p, STALE, "the regime and scenarios that read the file",
                             "Fix the file named (or re-import it); the last good copy is in use"))
    counts = {c: sum(1 for r in rows if r["colour"] == c) for c in ("green", "amber", "red")}
    return {"as_of_utc": now.isoformat(), "as_of_ist": fmt_ist(now), "today": today.isoformat(),
            "last_session": last_session.isoformat(), "last_session_ist": fmt_ist(last_session),
            "counts": counts, "rows": rows, "how_to_update": HOW_TO_UPDATE,
            "note": "A STALE, MISSING or PARTIAL source is never treated as OK; what it blocks is named in its row."}
