"""Local research app. Live prices, data quality and market regime.
No order routes exist. Write routes are allowed only under /journal/,
/data/import/ and /capital/ (phase 2 adds /data/import/{kind})."""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .calendar import MarketCalendar
from .config import ROOT, Settings, load_settings
from .history import BreezeHistorySource, HistoryLoader, seed_sim_history
from .hub import MarketHub
from .manual_data import KINDS, MAX_IMPORT_BYTES, ManualData
from .models import utcnow
from .options.analytics import OptionDesk
from .options.chain import BreezeChainSource, ChainLoader, SimChainSource
from .options.instruments import InstrumentBook, refresh_from_broker, sim_specs
from .options.margin import import_margins, load_margin_file
from .options.settlement import rules_view
from .options.shortlist import correlation_groups, reversal_watch, stock_view
from .page import BACKTEST_HTML, DATA_STATUS_HTML, OPTIONS_HTML, PAGE_HTML, SHORTLIST_HTML
from .quality import IST
from .regime import RegimeService
from .store import Store

log = logging.getLogger("main")


def build_feed(settings: Settings):
    if settings.feed.mode == "breeze":
        from .feeds.breeze_feed import BreezeFeed
        return BreezeFeed(settings.watchlist, settings.feed.subscribe_pause_seconds)
    from .feeds.sim import SimFeed
    return SimFeed(settings.watchlist)


SAFE_METHODS = {"GET", "HEAD"}
_CHAIN_KEEP = timedelta(days=2)      # option-chain snapshots older than this are deleted


def loop_threads(engine, notifier, watchdog, stop: threading.Event, poll_seconds: float,
                 watchdog_seconds: float = 15.0, now_fn=utcnow) -> list[threading.Thread]:
    """The three background loops of the live system, each on its own thread so none waits for another: the signal
    loop (`engine.step`), alert dispatch (`notifier.tick`: scheduled messages and Telegram sends) and the watchdog
    (`watchdog.check`: feed and engine heartbeats). A stuck or slow engine step therefore still gets its heartbeat
    alert raised and sent. Each loop logs a failure and carries on. Returned not started."""
    def run(name: str, fn, every: float):
        def loop() -> None:
            while not stop.is_set():
                try:
                    fn(now_fn())
                except Exception:
                    log.exception("%s loop failed (it carries on)", name)
                stop.wait(every)
        return threading.Thread(target=loop, name=name, daemon=True)

    return [run("engine", engine.step, poll_seconds), run("alerts", notifier.tick, poll_seconds),
            run("watchdog", watchdog.check, watchdog_seconds)]


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def write_request_problem(method: str, headers, port: int) -> tuple[int, str] | None:
    """Checks for every non-GET request. Returns (status, message) to reject, or None.
    - Host must be 127.0.0.1:<port> or localhost:<port> (blocks DNS-rebinding pages)
    - Origin, if sent, must be http(s)://127.0.0.1:<port> or http(s)://localhost:<port>
      (blocks other websites posting through your browser)
    - Content-Type must be application/json (a plain HTML form cannot send it)"""
    if method.upper() in SAFE_METHODS:
        return None
    ok = allowed_hosts(port)
    host = (headers.get("host") or "").strip().lower()
    if host not in ok:
        return 403, "write requests must be sent to 127.0.0.1 or localhost on this app's port"
    origin = headers.get("origin")
    if origin is not None:
        o = origin.strip().lower()
        scheme, _, hostport = o.partition("://")
        if scheme not in ("http", "https") or hostport.rstrip("/") not in ok:
            return 403, "cross-site write request refused"
    ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 415, "write requests must use Content-Type: application/json"
    return None


def create_app(settings: Settings | None = None, feed=None, db_path: str | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or load_settings()
    feed = feed or build_feed(settings)
    expected_feed = "breeze" if settings.is_live_feed else "sim"
    if feed.name != expected_feed:
        raise ValueError(f"feed '{feed.name}' does not match feed.mode '{settings.feed.mode}' in config.yaml")
    store = Store(db_path or settings.db_path(), live=settings.is_live_feed)
    calendar = MarketCalendar()
    manual = ManualData(settings, store, calendar)
    manual.load_all()
    hub = MarketHub(settings, store, feed.name, calendar)
    regime = RegimeService(settings, store, hub, calendar, manual)
    stop = threading.Event()

    # ---- phase 4: options (read-only) ----
    margin_errors = load_margin_file(settings, store)

    def live_price(symbol: str):
        q = hub.live_quote(symbol)
        return q["last"] if q else None

    def regime_name() -> str:
        try:
            return regime.evaluate()["regime"]
        except Exception:
            log.exception("regime evaluation failed")
            return "UNKNOWN"

    from .risk.engine import RiskService
    risk_service = RiskService(settings, store, hub, calendar)     # phase 5: also the Options page's option risk limit

    # ---- phase 6: journal (signals, decisions, positions, paper trades) per capital pool ----
    from .costs import CostModel, MissingCostRates
    from .journal import CASH_INDEX, POOLS, STOCK_OPTIONS, Journal, JournalError, pool_capital
    from .pricing import Marker
    journal = Journal(store, settings)
    marker = Marker(settings, store, hub, feed.name)
    try:
        cost_model = CostModel(settings.costs)
        cost_model.require("intraday_equity", "delivery_equity", "options")
    except MissingCostRates:
        cost_model = None                     # every figure that needs charges is then unknown (NO_TRADE)
    risk_service.attach(journal, marker, cost_model, utcnow)
    for _pool in POOLS:
        journal.ensure_equity_start(_pool, utcnow(), pool_capital(settings, _pool))
    desk = OptionDesk(settings, store, feed.name, live_price, regime_name, option_budget_fn=risk_service.option_budget)
    book = InstrumentBook(store, feed.name)
    if feed.name == "breeze":
        chain_source = BreezeChainSource(feed)
    else:
        chain_source = SimChainSource(live_price, lambda: live_price(settings.options.iv_reference_symbol),
                                      settings.options.risk_free_rate_pct / 100)
    chains = ChainLoader(settings, store, chain_source, book)

    def load_instruments() -> int:
        """Lot sizes, expiries and strikes: ICICI's instrument list (breeze) or fake sim contracts."""
        if feed.name == "breeze":
            n = refresh_from_broker(store, settings.watchlist)
        else:
            prices = {r["symbol"]: r["last"] for r in hub.snapshot() if r["last"]}
            for inst in settings.watchlist:
                if inst.symbol not in prices:
                    bars = store.history_bars("bars_1d", inst.symbol, "sim", limit=1)
                    if bars:
                        prices[inst.symbol] = bars[0].close
            weekly = {s for s, c in settings.options.underlyings.items() if "weekly" in c.expiries}
            n = book.save(sim_specs({k: v for k, v in prices.items() if settings.option_underlying(k)},
                                    utcnow().astimezone(IST).date(), calendar, utcnow(), weekly))
        desk.book = InstrumentBook(store, feed.name)
        chains.book = desk.book
        log.info("instrument list loaded: %d contract expiries", n)
        return n

    def load_chains() -> dict:
        symbols = None if feed.name == "breeze" else desk.underlyings()
        res = chains.load(symbols)
        store.prune_option_chain(feed.name, utcnow() - _CHAIN_KEEP)
        return res

    def options_loop() -> None:
        loaded_on = None
        while not stop.is_set():
            try:
                today = utcnow().astimezone(IST).date()
                if loaded_on != today:
                    load_instruments()
                    loaded_on = today
                if feed.name == "sim" or calendar.is_open(utcnow()):
                    bad = [k for k, v in load_chains().items() if "error" in v]
                    if bad:
                        log.warning("option chains failed: %s", bad)
            except Exception:
                log.exception("option chain refresh failed")
            stop.wait(settings.options.chain.refresh_seconds if loaded_on else 15)

    def history_job() -> None:
        try:
            summary = HistoryLoader(settings, store, BreezeHistorySource(feed, settings.history.request_timeout_seconds), calendar).update()
            failed = [k for k, v in summary.items() if "error" in v]
            log.info("history updated; %d failed: %s", len(failed), failed)
        except Exception:
            log.exception("history update failed")

    def regime_loop() -> None:
        while not stop.is_set():
            try:
                regime.evaluate()
            except Exception:
                log.exception("regime evaluation failed")
            stop.wait(settings.regime.recompute_seconds)

    # ---- phase 6: the live signal loop (paper mode): execution records, paper trader, exit calls ----
    from .engine import SignalEngine
    from .execution import ExecutionBook
    from .paper import PaperTrader
    from .positions import ExitCallBook
    execbook = ExecutionBook(store, settings)
    paper = PaperTrader(settings, journal, marker, store, execbook, calendar, cost_model)
    exitbook = ExitCallBook(store, settings)
    engine = SignalEngine(settings=settings, store=store, hub=hub, journal=journal, marker=marker, risk=risk_service,
                          regime=regime, calendar=calendar, desk=desk, source=feed.name, costs=cost_model,
                          execbook=execbook, paper=paper, exits=exitbook, now_fn=utcnow)
    engine_info = {"started_utc": utcnow().isoformat(), "restored": engine.restore(utcnow())}

    # ---- phase 6: alerts (Telegram send-only; OFF until alerts.telegram_enabled is true) ----
    from .alerts import AlertBook, Notifier
    from .telegram import TelegramSender
    alertbook = AlertBook(store, settings)
    notifier = Notifier(settings, alertbook, journal, risk_service, TelegramSender(settings.alerts.send_timeout_seconds),
                        calendar, store=store, regime=regime)
    notifier.source = feed.name
    engine.notifier = notifier

    # ---- phase 6: failure recovery (heartbeat, feed reconnect, sleep, expired login, restart banner) ----
    from .recovery import Watchdog
    watchdog = Watchdog(settings, hub, journal, alertbook, calendar, feed, engine)
    watchdog.start(utcnow())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if feed.name == "sim" and settings.history.sim_seed_on_start:
            seed_sim_history(settings, store, calendar)
            if hasattr(feed, "set_start_prices"):
                last = {}
                for inst in settings.watchlist:
                    bars = store.history_bars("bars_1d", inst.symbol, "sim", limit=1)
                    if bars:
                        last[inst.symbol] = bars[0].close
                feed.set_start_prices(last)
        feed.start(hub.on_tick)
        if background:
            if feed.name == "breeze" and hasattr(feed, "historical"):
                threading.Thread(target=history_job, name="history", daemon=True).start()
            threading.Thread(target=regime_loop, name="regime", daemon=True).start()
            threading.Thread(target=options_loop, name="options", daemon=True).start()
            if settings.engine.enabled:
                for t in loop_threads(engine, notifier, watchdog, stop, settings.engine.poll_seconds):
                    t.start()
        yield
        stop.set()
        feed.stop()
        hub.close()

    app = FastAPI(title="Trading Research System (research only)", lifespan=lifespan)

    @app.middleware("http")
    async def local_writes_only(request: Request, call_next):
        problem = write_request_problem(request.method, request.headers, settings.server.port)
        if problem:
            return JSONResponse({"ok": False, "errors": [problem[1]]}, status_code=problem[0])
        return await call_next(request)

    app.state.hub = hub
    app.state.regime = regime
    app.state.store = store
    app.state.manual = manual
    app.state.options = desk
    app.state.load_instruments = load_instruments
    app.state.load_chains = load_chains

    @app.get("/health")
    def health():
        return {"ok": True, "feed": feed.name, "account_mode": settings.account.mode,
                "order_placement": "not implemented by design",
                "time_utc": utcnow().isoformat(), **hub.stats()}

    @app.get("/api/prices")
    def prices():
        return {"feed": feed.name, "as_of_utc": utcnow().isoformat(), "rows": hub.snapshot()}

    @app.get("/api/quality")
    def quality():
        return {**hub.stats(), "recent_events": store.recent_quality_events(30)}

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, limit: int = 60):
        return {"symbol": symbol.upper(), "bars": store.recent_bars(symbol.upper(), min(limit, 500))}

    @app.get("/api/config")
    def config():
        return {"capital_inr": settings.account.capital_inr, "account_mode": settings.account.mode,
                "risk_pct": settings.risk.model_dump(), "limits_inr": settings.limits_in_rupees()}

    @app.get("/api/regime")
    def regime_now():
        return {**regime.evaluate(), "history": store.regime_history(20),
                "history_bars": store.history_counts(feed.name)}

    # ---- data status and the inbox importers (write routes under /data/import/, same local-only checks) ----
    async def _json_body(request: Request) -> dict:
        body = await request.body()
        if len(body) > 10_000:
            raise HTTPException(413, "request body is too large")
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(400, "body must be JSON")
        if not isinstance(payload, dict):
            raise HTTPException(400, "body must be a JSON object")
        return payload

    def _status() -> dict:
        from .data_status import data_status
        live = any(r.get("status") == "LIVE" for r in hub.snapshot())
        return data_status(settings, store, calendar, feed.name, live_ticks_today=live, manual=manual,
                           login_state=getattr(feed, "login_state", None))

    @app.get("/api/data-status")
    def api_data_status():
        return _status()

    @app.get("/data-status", response_class=HTMLResponse)
    def data_status_page():
        return DATA_STATUS_HTML

    @app.post("/data/import/inbox")
    async def data_import_inbox(request: Request):
        """Import every file in data/inbox/ (NSE board meetings, corporate actions, FII/DII, RBI decisions)."""
        from .inbox import declaration, run_inbox
        payload = await _json_body(request)
        txt = lambda k: payload.get(k) if isinstance(payload.get(k), str) else ""
        try:     # board meetings: the export's range and your confirmation that it is the all-companies list
            decl = declaration(txt("board_from"), txt("board_through"), payload.get("board_all_companies") is True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        results = run_inbox(settings, store, manual, board_meetings=decl)
        for r in results:
            if not r.get("ok", True):                    # rejected: the last good data stays in use; shown on Data status
                store.log_import_failure(f"inbox {r.get('file', '')}", utcnow(), "; ".join(r.get("errors", []))[:500])
        return {"ok": all(r.get("ok", True) for r in results), "files": results,
                "note": "no files in the inbox" if not results else ""}

    @app.post("/data/import/rbi-decision")
    async def data_import_rbi(request: Request):
        """One RBI decision (or a scheduled meeting date) into data/events.csv: {"date": "...", "decision": "..."}."""
        from .inbox import rbi_event
        from .manual_data import merge_into_file, parse_date
        payload = await _json_body(request)
        try:
            ev = rbi_event(parse_date(str(payload.get("date", ""))), str(payload.get("decision", ""))[:200], "form", utcnow())
        except ValueError as e:
            return JSONResponse({"ok": False, "errors": [str(e)]}, status_code=400)
        res = merge_into_file(manual, "events", [ev])
        return JSONResponse(res, status_code=200 if res["ok"] else 400)

    @app.post("/data/import/{kind}")
    async def data_import(kind: str, request: Request):
        """Body: JSON {"text": "<pasted CSV>"}. Writes only the user's own data files."""
        if kind not in KINDS and kind != "margins":
            raise HTTPException(404, f"unknown kind; use one of {list(KINDS) + ['margins']}")
        body = await request.body()
        if len(body) > MAX_IMPORT_BYTES + 1000:
            raise HTTPException(413, "pasted text is too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(400, 'body must be JSON like {"text": "..."}')
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise HTTPException(400, 'body must be JSON like {"text": "..."}')
        text = payload["text"].lstrip("﻿")
        result = import_margins(settings, store, text) if kind == "margins" else manual.import_text(kind, text)
        if not result["ok"]:                             # rejected: the last good data stays in use; shown on Data status
            store.log_import_failure(kind, utcnow(), "; ".join(str(e) for e in result.get("errors", []))[:500])
        return JSONResponse(result, status_code=200 if result["ok"] else 400)

    # ---- phase 5: risk engine (read-only status) and the capital log (deposits never count as profit) ----
    @app.get("/api/risk")
    def risk_status():
        return risk_service.status(utcnow())

    @app.post("/capital/log")
    async def capital_log_entry(request: Request):
        """Body: {"date": "YYYY-MM-DD", "type": "DEPOSIT" | "WITHDRAWAL", "amount": rupees, "note": "..."}.
        Logged separately from trading results: never counted as profit or loss."""
        import math
        from datetime import date as _date
        payload = await _json_body(request)
        errors = []
        try:
            day = _date.fromisoformat(str(payload.get("date", "")))
            if day > utcnow().astimezone(IST).date():
                errors.append("date is in the future")
        except ValueError:
            errors.append("date must be YYYY-MM-DD")
        kind = payload.get("type")
        if kind not in ("DEPOSIT", "WITHDRAWAL"):
            errors.append("type must be DEPOSIT or WITHDRAWAL")
        amount = payload.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            errors.append("amount must be a positive number of rupees")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 200:
            errors.append("note must be text of at most 200 characters")
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)
        entry_id = store.add_capital_entry(day, kind, float(amount), note, utcnow())
        return {"ok": True, "id": entry_id, "entries": len(store.capital_log())}

    # ---- phase 6: journal writes (journal only: nothing is sent to the broker) ----
    def _journal_result(fn):
        try:
            return fn()
        except JournalError as e:
            return JSONResponse({"ok": False, "errors": [str(e)]}, status_code=e.status)

    @app.post("/journal/alert-switches")
    async def journal_alert_switch(request: Request):
        """Body: {"type": one of the six trade types, "on": true | false}. Alerts only; paper trading goes on."""
        from .switches import parse_switch_body
        payload = await _json_body(request)
        try:
            t, on = parse_switch_body(payload)
        except ValueError as e:
            return JSONResponse({"ok": False, "errors": [str(e)]}, status_code=400)
        return {"ok": True, **journal.set_switch(t, on, utcnow())}

    @app.post("/journal/positions")
    async def journal_add_position(request: Request):
        """A position I opened outside a signal (cash, or option legs). It gets the same exit calls."""
        payload = await _json_body(request)
        return _journal_result(lambda: {"ok": True, "position": journal.add_manual_position(payload, utcnow()).to_view()})

    @app.post("/journal/positions/{position_id}/close")
    async def journal_close_position(position_id: str, request: Request):
        """I closed all or part of a position: {"exit_price": x} or {"exit_prices": [per leg]}, optional
        "quantity"/"lots" (partial), "exit_time", "note". Exit calls stop once it is fully closed."""
        payload = await _json_body(request)

        def go():
            res = journal.close_position(position_id, payload, utcnow(), cost_model)
            if res.get("status") == "CLOSED":
                exitbook.resolve_closed(position_id, utcnow())          # exit calls stop once it is closed
            return res
        return _journal_result(go)

    @app.post("/journal/positions/{position_id}/stop")
    async def journal_position_stop(position_id: str, request: Request):
        """I moved my stop (after a breakeven or trailing call): {"stop": x}."""
        payload = await _json_body(request)
        return _journal_result(lambda: {"ok": True, "position": journal.set_stop(position_id, payload, utcnow()).to_view()})

    @app.post("/journal/{signal_id}/decision")
    async def journal_decision(signal_id: str, request: Request):
        """'I took this trade' / 'I skipped it': {"decision": "TOOK"|"SKIPPED", "note", "fill_price" or
        "fills" (one per leg), "fill_time", "quantity"/"lots"}. Refused after the signal's valid-until."""
        payload = await _json_body(request)

        def go():
            res = journal.record_decision(signal_id, payload, utcnow())
            d = journal.decision(signal_id)
            if d and d["decision"] == "TOOK" and d.get("fills"):        # my actual fill per leg (execution records)
                from .execution import ACTUAL_FILL, legs_of
                from .engine import signal_from_dict
                sig = signal_from_dict(journal.signal(signal_id)["signal"])
                pos = journal.position(d["position_id"])
                execbook.record_fill(legs_of(sig), signal_id, ACTUAL_FILL, d["fills"],
                                     datetime.fromisoformat(d["fill_time_utc"]),
                                     pos.quantity if not pos.is_option else pos.quantity * pos.legs[0].lot_size,
                                     "my fill (journal)")
            return res
        return _journal_result(go)

    @app.post("/journal/{signal_id}/exit")
    async def journal_signal_exit(signal_id: str, request: Request):
        """Close the position I took from this signal (same body as /journal/positions/{id}/close)."""
        payload = await _json_body(request)
        d = journal.decision(signal_id)
        if not d or not d.get("position_id"):
            return JSONResponse({"ok": False, "errors": ["no position was taken from this signal"]}, status_code=404)
        return _journal_result(lambda: journal.close_position(d["position_id"], payload, utcnow(), cost_model))

    @app.get("/api/positions")
    def api_positions():
        now = utcnow()
        def view(p):
            prices, why = marker.marks(p, now)
            from .journal import open_pnl
            v = open_pnl(p, prices, cost_model)
            return {**p.to_view(), "open_pnl_inr": None if v is None else float(v),
                    "price_note": why or None, "closes": journal.closes(p.position_id)}
        return {"as_of_utc": now.isoformat(),
                "open": [{**view(p), "exit_calls": exitbook.calls(p.position_id, active_only=True)}
                         for p in journal.positions("OPEN")],
                "closed": [{**p.to_view(), "closes": journal.closes(p.position_id)}
                           for p in journal.positions("CLOSED")][-50:],
                "pools": {pool: risk_service.pool_state(pool, now)[1] for pool in POOLS}}

    @app.get("/api/alert-switches")
    def api_alert_switches():
        from .journal import TRADE_TYPE_NAMES
        from .track import all_type_records
        sw = journal.switches()
        tracks = all_type_records(store, journal, settings)
        return {"switches": [{"type": t, "name": TRADE_TYPE_NAMES[t], **sw[t], "track": tracks[t]} for t in sw],
                "telegram_enabled": settings.alerts.telegram_enabled,
                "log": journal.switch_log(50),
                "note": "Switches control alerts only; every strategy keeps paper-trading. Exit calls for positions "
                        "you hold are always sent."}

    app.state.journal = journal
    app.state.engine = engine
    app.state.alerts = alertbook
    app.state.notifier = notifier
    app.state.watchdog = watchdog

    # ---- phase 6: read-only views of signals, positions, paper trades and execution ----
    @app.get("/api/signals")
    def api_signals(hours: int = 24):
        from .cards import card_view
        now = utcnow()
        rows = journal.signals_since(now - timedelta(hours=min(max(hours, 1), 24 * 14)))
        decisions = journal.decisions()
        cards = [card_view(r["card"], now, decisions.get(r["signal_id"])) for r in rows if r["card"]]
        cards.sort(key=lambda c: (c["status"] != "APPROVED", c["expired"], -((c.get("score") or {}).get("score") or -1)))
        return {"as_of_utc": now.isoformat(), "cards": cards, "engine": engine.last_summary,
                "problems": engine.problems,
                "note": "Research only. APPROVED cards first (highest score first), NO_TRADE cards collapsed. An expired "
                        "card is greyed out and cannot be acted on."}

    @app.get("/api/signals/{signal_id}")
    def api_signal(signal_id: str):
        from .cards import card_view
        r = journal.signal(signal_id)
        if r is None:
            raise HTTPException(404, "unknown signal_id")
        return {**r, "card": card_view(r["card"], utcnow(), journal.decision(signal_id)) if r["card"] else None,
                "paper": journal.paper_trade(signal_id), "execution": execbook.rows(signal_id)}

    @app.get("/api/paper")
    def api_paper():
        from .journal import POOLS, paper_book
        rows = journal.paper_trades()
        now = utcnow()
        return {"open": [r for r in rows if r["status"] in ("PENDING", "OPEN")],
                "closed": [r for r in rows if r["status"] in ("CLOSED", "NOT_FILLED", "NOT_OPENED")][-200:],
                "book": {p: paper_book(journal, settings, p, now) for p in POOLS},
                "note": "Paper trades run in their own simulated book (config paper:), never against your real capital."}

    @app.get("/api/exit-calls")
    def api_exit_calls():
        return {"active": exitbook.calls(active_only=True), "log": exitbook.log()[-200:]}

    @app.get("/api/slippage")
    def api_slippage():
        return execbook.report()

    @app.get("/api/slippage.csv")
    def api_slippage_csv():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(execbook.csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=slippage.csv"})

    @app.get("/api/engine")
    def api_engine():
        return {**engine_info, "status": watchdog.status(utcnow()),
                "last_bar_utc": engine.last_bar.isoformat() if engine.last_bar else None,
                "last": engine.last_summary, "problems": engine.problems,
                "strategies": {"intraday": [s.id for s in engine.cash], "swing": [s.id for s in engine.swing],
                               "options": [s.id for s in desk.strategies]}}

    @app.get("/api/alerts")
    def api_alerts():
        from .telegram import TelegramSender
        ok, why = TelegramSender().configured()
        return {**alertbook.view(), "telegram_configured": ok, "telegram_problem": why or None,
                "note": "Send-only to your own chat (TELEGRAM_CHAT_ID); incoming messages are never read. The token "
                        "is never shown."}

    @app.get("/api/plan")
    def api_plan():
        from .schedule import eod_summary, premarket_plan
        now = utcnow()
        return {"premarket": premarket_plan(settings, store, journal, regime, now, feed.name),
                "end_of_day": eod_summary(settings, journal, now)}

    @app.get("/signals", response_class=HTMLResponse)
    def signals_page():
        from .page import SIGNALS_HTML
        return SIGNALS_HTML

    @app.get("/", response_class=HTMLResponse)
    def home():
        return PAGE_HTML

    @app.get("/api/backtests")
    def backtests(limit: int = 50):
        return {"runs": store.backtest_runs(min(max(limit, 1), 200))}

    @app.get("/api/backtests/{run_id}")
    def backtest_detail(run_id: str):
        run = store.backtest_run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run_id")
        return run

    @app.get("/api/options")
    def options_index():
        return {"underlyings": desk.underlyings(), "feed": feed.name, "margin_file_errors": margin_errors,
                "instruments_as_of_utc": desk.book.as_of().isoformat() if desk.book.as_of() else None}

    @app.get("/api/options/{underlying}")
    def options_view(underlying: str):
        """Option chain analytics and the option strategies' current ideas for one underlying. Read-only."""
        sym = underlying.upper()
        if sym not in desk.underlyings():
            raise HTTPException(404, "unknown underlying; see /api/options")
        return desk.view(sym)

    @app.get("/options", response_class=HTMLResponse)
    def options_page():
        return OPTIONS_HTML

    @app.get("/backtests", response_class=HTMLResponse)
    def backtest_page():
        return BACKTEST_HTML

    # ---- phase 9: stock option selling shortlist (read-only; built by python -m app.options.shortlist) ----
    @app.get("/api/stock-option-selling/shortlist")
    def stock_option_selling_shortlist():
        out = store.shortlist()
        sos = settings.stock_option_selling
        status = settings.strategies.status_of("stock_option_selling")
        out.update({"feed": feed.name, "shortlist_size": sos.shortlist_size, "weights": sos.score.weights.model_dump(),
                    "score_note": "0-100, six equal-weight factors (app/options/score.py); a ranking aid, never a probability",
                    "stocks": [i.symbol for i in settings.watchlist if i.kind == "stock"],
                    "label": status.label, "alerts": status.alerts,
                    "note": "Research only. Every row is a research candidate from the last session's snapshot, not a "
                            "signal. An actionable signal also needs a live price, a fresh chain with bid and ask, a current "
                            "margin and the open positions known (PORTFOLIO_UNKNOWN until positions are recorded after phase 6).",
                    "correlation_groups": correlation_groups(settings, store, feed.name)})
        return out

    @app.get("/api/stock-option-selling/reversals")
    def stock_option_selling_reversals():
        """Reversal watch list: washed-out stocks, bases forming and confirmed reversals with their two
        candidate trades. Watch only, never a signal. Read-only."""
        return reversal_watch(settings, store, feed.name)

    @app.get("/api/stock-option-selling/campaigns")
    def stock_option_selling_campaigns():
        """Campaigns (an original spread and its rolls) with realised P&L, hedge cost, charges and the best
        possible remaining outcome. Read-only; positions are recorded from phase 6 on."""
        return {"campaigns": store.campaigns(),
                "note": "A campaign is an original trade and every roll of it. Positions are recorded once the "
                        "journal and manual position form exist (after phase 6)."}

    @app.get("/api/stock-option-selling/settlement")
    def stock_option_selling_settlement():
        """Settlement readiness for sold stock option legs (phase 9 M3): ICICI's rules as entered, each with
        its confirmed flag, and the alerts of the open positions. Alerts and estimates only. Read-only."""
        return {**rules_view(settings.stock_option_selling, calendar, utcnow().astimezone(IST).date()), "alerts": [],
                "note": "Stock options settle physically. Alerts and estimates only: the system never closes or "
                        "changes a position."}

    @app.get("/api/stock-option-selling/profile/{stock}")
    def stock_option_selling_profile(stock: str):
        """Range and speed profile and scenario history of one stock. History, not a forecast. Read-only."""
        sym = stock.upper()
        if sym not in {i.symbol for i in settings.watchlist if i.kind == "stock"}:
            raise HTTPException(404, "unknown stock")
        return stock_view(settings, store, feed.name, sym)

    @app.get("/stock-option-selling", response_class=HTMLResponse)
    def stock_option_selling_page():
        return SHORTLIST_HTML

    return app


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port)


if __name__ == "__main__":
    main()
