"""The stock option shortlist, every trading day, run by the app (no manual command). Two phases:

1. chains, from `stock_option_selling.after_close.chain_time` (15:20 IST) until the close, while bids and asks are
   live: the option chains of the priority stocks (`priority_stocks`) for their current and next monthly expiry, both
   sides (one Breeze call returns every strike of one expiry and side: 30 stocks x 2 expiries x 2 = 120 calls), paced
   by `options.chain.max_calls_per_minute`. A strike quoted without a valid bid/ask is stored with bid and ask empty
   and counted (`no_bid_ask`): recorded as unpriced at 15:20, never priced from the last trade. Not captured by the
   close (the app was not running): recorded as MISSED, never captured after the close;
2. after the close, from `start_time` (15:31 IST): the bar-history update of the watchlist for the session (normally
   one call per symbol and interval: 53 x 2 = 106), then the shortlist built from the stored data and stored.

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
from datetime import date, datetime

from ..config import hhmm
from ..models import utcnow
from ..quality import IST, MARKET_CLOSE

log = logging.getLogger("after_close")

STATE_KEY = "after_close:{day}"
RUNNING, DONE = "RUNNING", "DONE"
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

    def due(self, now: datetime) -> str | None:
        """The phase due now: "chains" (chain_time to the close, once a day), "finish" (from start_time, once a day),
        or None. A phase left RUNNING by an app that stopped mid-run is due again (the budget still caps it)."""
        cfg = self.s.stock_option_selling.after_close
        day = now.astimezone(IST).date()
        if not cfg.enabled or not self.cal.is_trading_day(day) or self._running:
            return None
        t = now.astimezone(IST).time()
        st = self.state(day) or {}
        if hhmm(cfg.chain_time) <= t < MARKET_CLOSE and st.get("chains_status") != DONE:
            return "chains"
        if t >= hhmm(cfg.start_time) and st.get("status") != DONE:
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
            res = self.chains.load(st["priority_stocks"], now=now, max_requests=budget_left(self.s, self.store, day),
                                   on_request=self._counter(day, st), keep_unquoted=True)
            failed = {k: v["error"] for k, v in res.items() if "error" in v}
            st["steps"]["chains"] = {"ok": not failed, "at_utc": now.isoformat(),
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
                "ok": False, "error": f"{was} between {self.s.stock_option_selling.after_close.chain_time} and the "
                                      f"close; chains are not captured after the close (no live bid/ask)"}
            st["chains_status"] = DONE
        st.update(status=RUNNING, started_utc=now.isoformat(), step="history")
        self._save(day, st, now)
        count = self._counter(day, st)

        # history (what is left of the day's budget)
        if self.history is None:
            st["steps"]["history"] = {"ok": True, "skipped": f"no history source for the {self.source} feed"}
        else:
            try:
                res = self.history(budget_left(self.s, self.store, day), count).update(now)
                failed = {k: v["error"] for k, v in res.items() if "error" in v}
                st["steps"]["history"] = {"ok": not failed, "failed": failed,
                                          "saved": sum(v.get("saved", 0) for v in res.values())}
            except Exception as e:                   # noqa: BLE001
                log.exception("after-close history failed")
                st["steps"]["history"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        st["step"] = "shortlist"
        self._save(day, st, utcnow())

        # shortlist, stored for the session
        try:
            from .shortlist import run_shortlist
            out = run_shortlist(self.s, self.store, self.source, now=utcnow(), symbols=st["priority_stocks"])
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
        except Exception as e:                       # noqa: BLE001
            log.exception("after-close shortlist failed")
            st["steps"]["shortlist"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        st.update(status=DONE, step=None, finished_utc=utcnow().isoformat())
        self._save(day, st, utcnow())
        log.info("after-close shortlist job: %s", json.dumps(st["steps"], default=str)[:2000])
        return st


def _ranked(counts: Counter) -> dict:
    """Most frequent first, ties by code: the same data always gives the same text."""
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _code(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def _side(side: str | None) -> str:
    return {"PE": "bull put", "CE": "bear call"}.get(side or "", "-")


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
    if st.get("status") != DONE:
        step = st.get("step") or ("history and shortlist (after the close)" if st.get("chains_status") == DONE
                                  else "chains")
        return [f"{head}: NOT FINISHED by {now.astimezone(IST):%H:%M} (step: {step}): no result yet"]
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
    sl = steps.get("shortlist") or {}
    calls = f"{st.get('requests', 0)} Breeze calls"
    if not sl.get("ok"):
        return [f"{head}: FAILED - {sl.get('error', 'not built')}"] + [f"  {p}" for p in problems] + [f"  ({calls})"]
    lines = [f"{head} (session {sl['session_date']}, {sl['stocks']} priority stocks, {calls}):"]
    ch = steps.get("chains") or {}
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
