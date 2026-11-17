"""The stock option shortlist, every trading day, run by the app (no manual command). Two phases:

1. chains, from `stock_option_selling.after_close.chain_time` (15:20 IST) until the close, while bids and asks are
   live: the option chains of the priority stocks (`priority_stocks`) for their current and next monthly expiry, both
   sides (one Breeze call returns every strike of one expiry and side: 30 stocks x 2 expiries x 2 = 120 calls), paced
   by `options.chain.max_calls_per_minute`. A strike quoted without a valid bid/ask is stored with bid and ask empty
   and counted (`no_bid_ask`): recorded as unpriced at 15:20, never priced from the last trade. Not captured by the
   close (the app was not running): recorded as MISSED, never captured after the close;
2. after the close, from `start_time` (15:31 IST): the bar-history update of the watchlist for the session (normally
   one call per symbol and interval: 53 x 2 = 106), then the shortlist built FOR THE SESSION THE CHAINS WERE CAPTURED
   IN (today), never an earlier one. A symbol whose daily bar for today is not stored yet gets the session's bar built
   from its stored 5-minute bars, when all 75 (09:15 to 15:25) are stored (`session_bar_from_5m`; used in memory only,
   never written to the daily table). When the index (or a priority stock) still has neither, the step waits and
   retries every `bars_retry_minutes` (asking Breeze again only for those symbols) until `bars_deadline`; then the
   shortlist runs for today with what there is (a stock without today's bar: DATA_STALE), or FAILS without the index's
   bar. The summary says where the session's close came from.

Every Breeze call is counted in the day's Breeze request count (`store.add_requests`, the count the option-history
download also uses: `options.backtest.daily_request_budget`), and a step stops at what is left of the day's budget, so
the option-history download run later that day uses what is left. A step that fails, or stops at the budget, is
recorded with why; the steps after it still run on what is stored.

The day's result is kept in the journal state (`after_close:<day>`, JSON) and summarised by `summary_lines` for the
15:45 end-of-day Telegram summary: the candidates with side, strikes and score, or "no candidates" with the top reasons
- but only when every step succeeded and no stock lacks data; otherwise the summary says what failed or is missing.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from ..config import hhmm
from ..models import utcnow
from ..models import Bar
from ..quality import IST, MARKET_OPEN
from ..session import CASH_FNO, INDEX, calendar as session_calendar, segment_for

log = logging.getLogger("after_close")

STATE_KEY = "after_close:{day}"
RUNNING, DONE, WAITING = "RUNNING", "DONE", "WAITING"
APPROXIMATE = "APPROXIMATE"


def session_bar_from_5m(store, source: str, symbol: str, day: date, segment: str = CASH_FNO) -> tuple[Bar | None, str]:
    """(an APPROXIMATE daily bar built from the stored 5-minute bars of continuous trading, how) or (None, why not).
    Only from every bar of continuous trading (app/session.py): 09:15 to 15:25 IST (75 bars) before 3 Aug 2026; for an
    F&O stock or the index from 3 Aug 2026 09:15 to 15:10 (72 bars: the closing-auction bars are not trades). Open of
    the first, high and low over all, close of the last (the last continuous trade, NOT the official close: before
    3 Aug a weighted average of the last 30 minutes, from 3 Aug the closing-auction price), volume summed (unknown when
    any bar's is). Used only when Breeze's daily bar is still missing at the deadline."""
    from datetime import timedelta as _td
    h = session_calendar().hours(segment, day)
    start, end = h.open_utc, h.continuous_end_utc
    want = int((end - start).total_seconds() // 300)
    bars = store.history_bars("bars_5m", symbol, source, since=start, before=end)
    if len(bars) != want or bars[0].start != start or bars[-1].start != end - _td(minutes=5):
        last = bars[-1].start.astimezone(IST).strftime("%H:%M") if bars else "-"
        return None, f"5-minute bars incomplete ({len(bars)} of {want}, last {last})"
    vols = [b.volume for b in bars]
    return (Bar(symbol, start, bars[0].open, max(b.high for b in bars), min(b.low for b in bars), bars[-1].close,
                None if any(v is None for v in vols) else float(sum(vols))),
            f"{APPROXIMATE}: built from {want} stored 5-minute bars to {h.continuous_end:%H:%M} (the last continuous "
            f"trade, not the official close)")


class SessionBarsStore:
    """The store, with the session's daily bars built from 5-minute bars added to daily-bar reads where the stored
    daily history lacks them (in memory only; nothing is written to the daily table). Everything else is the store."""

    def __init__(self, store, extra: dict):
        self._store, self._extra = store, dict(extra)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def history_bars(self, table, symbol, source, *, since=None, before=None, limit=None):
        bars = self._store.history_bars(table, symbol, source, since=since, before=before, limit=limit)
        b = self._extra.get(symbol) if table == "bars_1d" else None
        if b is None or (since is not None and b.start < since) or (before is not None and b.start >= before):
            return bars
        if bars and bars[-1].start >= b.start:
            return bars                                    # the daily bar is stored: it wins
        out = bars + [b]
        return out[-limit:] if limit is not None else out
# reason codes that mean "data missing", not "the rules said no": with any of them "no candidates" is never said
DATA_GAP_CODES = frozenset({"NO_CHAIN", "DATA_STALE", "DATA_MISSING", "LOT_SIZE_UNKNOWN", "INSUFFICIENT_HISTORY"})


def is_data_gap(code: str) -> bool:
    return code in DATA_GAP_CODES or code.endswith("_UNKNOWN")


def priority_stocks(settings) -> list[str]:
    """The priority stocks: the list of the stock option history download (its status file), else the first
    `stock_option_selling.backtest.priority_stocks` watchlist stocks. Only watchlist stocks."""
    from .stock_backtest import read_status
    stocks = [i.symbol for i in settings.watchlist if i.kind == "stock"]
    listed = read_status(settings).get("stocks") or stocks[:settings.stock_option_selling.backtest.priority_stocks]
    return [s for s in listed if s in stocks]


def budget_left(settings, store, day: date) -> int:
    from .option_history import todays_budget
    return max(0, todays_budget(settings, store, day) - store.requests_used(day))


def expected_calls(settings) -> dict:
    """Breeze calls a normal day uses (no empty replies asked again) and the most it can use."""
    chains = len(priority_stocks(settings)) * 2 * 2
    hist = len(settings.watchlist) * 2
    return {"chains": chains, "history": hist, "normal": chains + hist,
            "most": chains + hist * (1 + settings.history.empty_retries)}


class AfterCloseJob:
    """`chains`: an app.options.chain.ChainLoader; `history`: a factory (max_requests, on_request) -> HistoryLoader, or
    None when there is no history source (sim feed)."""

    def __init__(self, settings, store, journal, calendar, chains, history, source: str):
        self.s, self.store, self.j, self.cal = settings, store, journal, calendar
        self.chains, self.history, self.source = chains, history, source
        self._running = False

    def state(self, day: date) -> dict | None:
        raw = self.j.get_state(STATE_KEY.format(day=day.isoformat()))
        return json.loads(raw) if raw else None

    def _save(self, day: date, st: dict, now: datetime) -> None:
        self.j.set_state(STATE_KEY.format(day=day.isoformat()), json.dumps(st, default=str), now)

    def chains_stop(self, day: date) -> datetime:
        """The capture stops when F&O stocks' continuous trading ends (15:15 from 3 Aug 2026; 15:30 before): bids and
        asks of the options are live, and the underlying still trades, only until then."""
        return session_calendar().hours(CASH_FNO, day).continuous_end_utc

    def due(self, now: datetime) -> str | None:
        """The phase due now: "chains" (chain_time to the end of F&O stocks' continuous trading, once a day),
        "finish" (from start_time, after every segment's close, once a day), or None. A phase left RUNNING by an app
        that stopped mid-run is due again (the budget still caps it)."""
        cfg = self.s.stock_option_selling.after_close
        day = now.astimezone(IST).date()
        if not cfg.enabled or not self.cal.is_trading_day(day) or self._running:
            return None
        t = now.astimezone(IST).time()
        st = self.state(day) or {}
        stop = self.chains_stop(day).astimezone(IST).time()
        if hhmm(cfg.chain_time) <= t < stop and st.get("chains_status") != DONE:
            return "chains"
        if t >= max(hhmm(cfg.start_time), session_calendar().latest_close(day)) and st.get("status") != DONE:
            if st.get("status") == WAITING and now < datetime.fromisoformat(st["next_retry_utc"]):
                return None
            return "finish"
        return None

    def run(self, now: datetime | None = None) -> dict | None:
        """Run the phase due now (nothing when none is)."""
        now = now or utcnow()
        phase = self.due(now)
        if phase is None:
            return None
        self._running = True
        try:
            return self.run_chains(now) if phase == "chains" else self.run_finish(now)
        finally:
            self._running = False

    def _start(self, day: date, now: datetime) -> dict:
        st = self.state(day) or {"day": day.isoformat(), "steps": {}, "requests": 0}
        st.setdefault("steps", {})
        st.setdefault("requests", 0)
        st["priority_stocks"] = priority_stocks(self.s)
        return st

    def _counter(self, day: date, st: dict):
        def count():
            self.store.add_requests(day)
            st["requests"] = st.get("requests", 0) + 1
        return count

    def run_chains(self, now: datetime) -> dict:
        """Phase 1: the priority stocks' chains, while bids and asks are live."""
        day = now.astimezone(IST).date()
        st = self._start(day, now)
        st.update(chains_status=RUNNING, chains_started_utc=now.isoformat(),
                  budget_left_at_chains=budget_left(self.s, self.store, day))
        self._save(day, st, now)
        try:
            stop = self.chains_stop(day)
            res = self.chains.load(st["priority_stocks"], now=now, max_requests=budget_left(self.s, self.store, day),
                                   on_request=self._counter(day, st), keep_unquoted=True, stop_at=stop,
                                   clock=utcnow)
            missed = sorted(k for k, v in res.items() if v.get("missed"))
            failed = {k: v["error"] for k, v in res.items() if "error" in v and not v.get("missed")}
            stamps = sorted(v["as_of_utc"] for v in res.values() if v.get("as_of_utc"))
            st["steps"]["chains"] = {"ok": not failed and not missed, "at_utc": now.isoformat(),
                                     "first_fetch_utc": stamps[0] if stamps else None,
                                     "last_fetch_utc": stamps[-1] if stamps else None,
                                     "stop_utc": stop.isoformat(), "missed": missed,
                                     "chains": len(res),
                                     "loaded": sum(1 for v in res.values() if "error" not in v), "failed": failed,
                                     "strikes": sum(v.get("saved", 0) for v in res.values()),
                                     "no_bid_ask": sum(v.get("no_bid_ask", 0) for v in res.values()),
                                     "rate_wait_seconds": round(getattr(getattr(self.chains, "rate", None), "waited", 0.0), 1)}
        except Exception as e:                       # noqa: BLE001 - a failing step is reported, never fatal
            log.exception("chain capture failed")
            st["steps"]["chains"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        st["chains_status"] = DONE
        self._save(day, st, utcnow())
        log.info("stock option chains captured: %s", json.dumps(st["steps"]["chains"], default=str)[:1000])
        return st

    def run_finish(self, now: datetime) -> dict:
        """Phase 2, after the close: the bar history, then the shortlist."""
        day = now.astimezone(IST).date()
        st = self._start(day, now)
        if st.get("chains_status") != DONE:
            was = "INTERRUPTED: the capture stopped before it finished" if st.get("chains_status") == RUNNING else \
                "MISSED: the app was not running"
            st["steps"]["chains"] = {
                "ok": False, "error": f"{was} between {self.s.stock_option_selling.after_close.chain_time} and "
                                      f"{self.chains_stop(day).astimezone(IST):%H:%M} (end of continuous trading); "
                                      f"chains are not captured after it (no live bid/ask)"}
            st["chains_status"] = DONE
        cfg = self.s.stock_option_selling.after_close
        attempt = st.get("bar_attempts", 0) + 1
        retry_only = set(st.get("bars_missing") or []) if attempt > 1 else None
        st.update(status=RUNNING, started_utc=st.get("started_utc") or now.isoformat(), step="history",
                  bar_attempts=attempt)
        self._save(day, st, now)
        count = self._counter(day, st)

        # history (what is left of the day's budget); a retry asks only for the symbols still without today's bar
        if self.history is None:
            st["steps"]["history"] = {"ok": True, "skipped": f"no history source for the {self.source} feed"}
        else:
            try:
                res = self.history(budget_left(self.s, self.store, day), count).update(now, symbols=retry_only)
                failed = {k: v["error"] for k, v in res.items() if "error" in v}
                st["steps"]["history"] = {"ok": not failed, "failed": failed, "attempt": attempt,
                                          "saved": sum(v.get("saved", 0) for v in res.values())}
            except Exception as e:                   # noqa: BLE001
                log.exception("after-close history failed")
                st["steps"]["history"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        # the session's bars: the chains' day (today), never an earlier session. Breeze's daily bar is the official
        # close (from 3 Aug 2026 the closing-auction price): waited for until the deadline; only on the last attempt
        # is a missing one replaced by an APPROXIMATE close built from 5-minute bars of continuous trading
        index = self.s.regime.index_symbol
        built, from_daily, missing = {}, [], {}
        deadline = datetime.combine(day, hhmm(cfg.bars_deadline), IST).astimezone(timezone.utc)
        nxt = now + timedelta(minutes=cfg.bars_retry_minutes)
        last_try = nxt > deadline
        for sym in [index] + [x for x in st["priority_stocks"] if x != index]:
            got = self.store.history_bars("bars_1d", sym, self.source, since=datetime.combine(
                day, MARKET_OPEN, IST).astimezone(timezone.utc), limit=1)
            if got:
                from_daily.append(sym)
                continue
            if not last_try:
                missing[sym] = f"no daily bar (official close) for {day:%d-%b} yet"
                continue
            bar, why = session_bar_from_5m(self.store, self.source, sym, day, segment_for(self.s, sym))
            if bar is not None:
                built[sym] = bar
            else:
                missing[sym] = f"no daily bar for {day:%d-%b} by {cfg.bars_deadline}; {why}"
        st["session_bars"] = {"day": day.isoformat(), "daily": len(from_daily), "from_5m": sorted(built),
                              "approximate": sorted(built), "missing": missing}
        st["bars_missing"] = sorted(missing)
        if missing and not last_try:
            st.update(status=WAITING, step=f"waiting for {day:%d-%b}'s bars", next_retry_utc=nxt.isoformat())
            self._save(day, st, utcnow())
            log.info("after-close: %d symbol(s) without %s's bar; retry at %s", len(missing), day, nxt.isoformat())
            return st
        st.update(step="shortlist")
        self._save(day, st, utcnow())

        # shortlist, for the chains' session, stored
        try:
            from .shortlist import run_shortlist
            if index in missing:
                raise _NoSessionBar(f"no bar for {index} on {day:%d-%b-%Y} after {attempt} attempt(s) until "
                                    f"{cfg.bars_deadline}: {missing[index]}; the shortlist is not run on an older session")
            out = run_shortlist(self.s, SessionBarsStore(self.store, built), self.source, now=now,
                                symbols=st["priority_stocks"], session=day)
            if out.get("error"):
                st["steps"]["shortlist"] = {"ok": False, "error": out["error"]}
            else:
                rows = out["rows"]
                st["steps"]["shortlist"] = {
                    "ok": True, "session_date": out["session_date"], "stocks": len(rows),
                    "candidates": [{"stock": r["stock"], "side": r["side"], "expiry": r["expiry"],
                                    "strikes": r.get("strikes"), "score": r["score"], "rank": r["rank"]}
                                   for r in rows if r["status"] == "SHORTLISTED"],
                    "reasons": _ranked(Counter(_code(r["reasons"][0]) for r in rows
                                               if r["status"] != "SHORTLISTED" and r["reasons"])),
                    "data_gaps": _ranked(Counter(c for r in rows if r["status"] != "SHORTLISTED"
                                                 for c in {_code(x) for x in r["reasons"]} if is_data_gap(c)))}
        except _NoSessionBar as e:
            st["steps"]["shortlist"] = {"ok": False, "error": str(e)}
        except Exception as e:                       # noqa: BLE001
            log.exception("after-close shortlist failed")
            st["steps"]["shortlist"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        st.update(status=DONE, step=None, finished_utc=utcnow().isoformat())
        st.pop("next_retry_utc", None)
        self._save(day, st, utcnow())
        log.info("after-close shortlist job: %s", json.dumps(st["steps"], default=str)[:2000])
        return st


class _NoSessionBar(RuntimeError):
    pass


def _ranked(counts: Counter) -> dict:
    """Most frequent first, ties by code: the same data always gives the same text."""
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _code(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def _side(side: str | None) -> str:
    return {"PE": "bull put", "CE": "bear call"}.get(side or "", "-")


RUNNING_GRACE_MINUTES = 30      # a step still RUNNING at the deadline: its result is waited for this long more


def shortlist_message(settings, journal, day: date, now: datetime) -> str | None:
    """The stock option shortlist as its own Telegram message (user, 2026-11-17), or None while it is not due:
    sent once the after-close job has finished (DONE: a result, or a failure it reached), or at
    `after_close.bars_deadline` (17:30) when it has not - then saying NOT RUN / NOT FINISHED, as the summary rules
    say. A step still RUNNING at the deadline is waited for `RUNNING_GRACE_MINUTES` more. None when the job is off."""
    cfg = settings.stock_option_selling.after_close
    if not cfg.enabled:
        return None
    raw = journal.get_state(STATE_KEY.format(day=day.isoformat()))
    st = json.loads(raw) if raw else {}
    deadline = datetime.combine(day, hhmm(cfg.bars_deadline), IST).astimezone(timezone.utc)
    if st.get("status") != DONE:
        if now < deadline:
            return None
        if st.get("status") == RUNNING and now < deadline + timedelta(minutes=RUNNING_GRACE_MINUTES):
            return None
    return "\n".join([f"STOCK OPTION SHORTLIST {day:%a %d-%b-%Y}"] + summary_lines(settings, journal, day, now))


def summary_lines(settings, journal, day: date, now: datetime) -> list[str]:
    """The shortlist part of the end-of-day summary. "No candidates" only when every step succeeded and no stock
    lacks data; anything else says what failed, is missing or has not finished."""
    cfg = settings.stock_option_selling.after_close
    head = "Stock option shortlist"
    if not cfg.enabled:
        return [f"{head}: OFF (stock_option_selling.after_close.enabled is false)"]
    raw = journal.get_state(STATE_KEY.format(day=day.isoformat()))
    if not raw:
        return [f"{head}: NOT RUN - the after-close job had not started by {now.astimezone(IST):%H:%M} "
                f"(starts {cfg.start_time} on a trading day while the app is running): no result today"]
    st = json.loads(raw)
    steps = st.get("steps", {})
    problems = []
    for name in ("chains", "history"):
        s = steps.get(name) or {}
        if s.get("error"):
            problems.append(f"{name} FAILED: {s['error']}")
        elif s.get("failed"):
            items = s["failed"]
            problems.append(f"{name} failed for {len(items)}: " + "; ".join(f"{k} ({v})" for k, v in list(items.items())[:4])
                            + (" ..." if len(items) > 4 else ""))
    if st.get("status") == WAITING:
        nxt = datetime.fromisoformat(st["next_retry_utc"]).astimezone(IST)
        miss = st.get("session_bars", {}).get("missing", {})
        return [f"{head}: NOT FINISHED by {now.astimezone(IST):%H:%M}: waiting for today's bars (attempt "
                f"{st.get('bar_attempts')}, next try {nxt:%H:%M}, until {cfg.bars_deadline}); no result yet",
                "  without today's bar: " + "; ".join(f"{k} ({v})" for k, v in list(miss.items())[:4])
                + (" ..." if len(miss) > 4 else "")] + [f"  {x}" for x in problems]
    if st.get("status") != DONE:
        step = st.get("step") or ("history and shortlist (after the close)" if st.get("chains_status") == DONE
                                  else "chains")
        return [f"{head}: NOT FINISHED by {now.astimezone(IST):%H:%M} (step: {step}): no result yet"]
    sl = steps.get("shortlist") or {}
    calls = f"{st.get('requests', 0)} Breeze calls"
    if not sl.get("ok"):
        return [f"{head}: FAILED - {sl.get('error', 'not built')}"] + [f"  {p}" for p in problems] + [f"  ({calls})"]
    lines = [f"{head} (session {sl['session_date']}, {sl['stocks']} priority stocks, {calls}):"]
    sb = st.get("session_bars") or {}
    if sb:
        how = (f"session close: Breeze daily bars (official close) for {sb.get('daily', 0)}"
               + (f"; APPROXIMATE close for {len(sb['from_5m'])} (no daily bar by {cfg.bars_deadline}: the last "
                  f"continuous trade from stored 5-minute bars, not the official closing-auction price): "
                  + ", ".join(sb["from_5m"][:8]) + (" ..." if len(sb["from_5m"]) > 8 else "") if sb.get("from_5m") else "")
               + (f" (after {st.get('bar_attempts')} attempts)" if (st.get("bar_attempts") or 1) > 1 else ""))
        lines.append(f"  {how}")
        if sb.get("missing"):
            problems.append(f"no bar for {sb['day']} by {cfg.bars_deadline} for {len(sb['missing'])}: "
                            + ", ".join(list(sb["missing"])[:8]))
    ch = steps.get("chains") or {}
    if ch.get("missed"):
        stop = datetime.fromisoformat(ch["stop_utc"]).astimezone(IST) if ch.get("stop_utc") else None
        problems.append(f"chains: {len(ch['missed'])} of {ch.get('chains', '?')} expiry chains MISSED (not fetched by "
                        f"{f'{stop:%H:%M}' if stop else 'the end of continuous trading'}): "
                        + ", ".join(ch["missed"][:6]) + (" ..." if len(ch["missed"]) > 6 else ""))
    if ch.get("no_bid_ask"):
        at = datetime.fromisoformat(ch["at_utc"]).astimezone(IST) if ch.get("at_utc") else None
        problems.append(f"chains{f' at {at:%H:%M}' if at else ''}: {ch['no_bid_ask']} of {ch.get('strikes', '?')} "
                        f"strikes had no valid bid/ask (stored as unpriced)")
    for c in sl.get("candidates", []):
        strikes = "/".join(f"{k:g}" for k in (c.get("strikes") or [])) or "strikes -"
        score = "incomplete" if c.get("score") is None else f"{c['score']:.1f}"
        lines.append(f"  {c['rank']}. {c['stock']} {_side(c['side'])} {strikes} exp {c['expiry']} score {score}")
    gaps = sl.get("data_gaps") or {}
    if gaps:
        problems.append("data missing: " + ", ".join(f"{k} {n}" for k, n in gaps.items()))
    reasons = ", ".join(f"{k} {n}" for k, n in list((sl.get("reasons") or {}).items())[:5])
    if not sl.get("candidates"):
        if problems:
            lines.append("  NO RESULT: a step failed or data is missing, so the list is not complete")
        else:
            lines.append(f"  No candidates. Top reasons: {reasons or '-'}")
    elif reasons:
        lines.append(f"  Not on the list: {reasons}")
    lines += [f"  {p}" for p in problems]
    return lines
