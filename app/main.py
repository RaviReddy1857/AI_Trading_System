"""Local research app. Live prices, data quality and market regime.
No order routes exist. Write routes are allowed only under /journal/,
/data/import/ and /capital/ (phase 2 adds /data/import/{kind})."""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager

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
from .page import PAGE_HTML
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

    def history_job() -> None:
        try:
            summary = HistoryLoader(settings, store, BreezeHistorySource(feed), calendar).update()
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

    @app.post("/data/import/{kind}")
    async def data_import(kind: str, request: Request):
        """Body: JSON {"text": "<pasted CSV>"}. Writes only the user's own data files."""
        if kind not in KINDS:
            raise HTTPException(404, f"unknown kind; use one of {list(KINDS)}")
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
        result = manual.import_text(kind, text)
        return JSONResponse(result, status_code=200 if result["ok"] else 400)

    @app.get("/", response_class=HTMLResponse)
    def home():
        return PAGE_HTML

    return app


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port)


if __name__ == "__main__":
    main()
