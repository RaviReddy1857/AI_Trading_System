"""Local research app. Step 1: live prices + data quality. No order routes exist."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import ROOT, Settings, load_settings
from .hub import MarketHub
from .models import utcnow
from .page import PAGE_HTML
from .store import Store


def build_feed(settings: Settings):
    if settings.feed.mode == "breeze":
        from .feeds.breeze_feed import BreezeFeed
        return BreezeFeed(settings.watchlist)
    from .feeds.sim import SimFeed
    return SimFeed(settings.watchlist)


def create_app(settings: Settings | None = None, feed=None, db_path: str | None = None) -> FastAPI:
    settings = settings or load_settings()
    feed = feed or build_feed(settings)
    store = Store(db_path or (ROOT / settings.storage.db_path))
    hub = MarketHub(settings, store, feed.name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        feed.start(hub.on_tick)
        yield
        feed.stop()
        hub.close()

    app = FastAPI(title="Trading Research System (research only)", lifespan=lifespan)
    app.state.hub = hub

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
