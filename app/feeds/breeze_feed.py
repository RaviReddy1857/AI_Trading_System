"""READ-ONLY ICICI Breeze feed.

This module is the only place the broker SDK is touched, and it uses data
functions only: login session, live quote subscription, historical bars and
option chain. Nothing in this project sends, changes or closes an order, and
tests/test_safety.py fails the build if such a call is ever added.
"""
from __future__ import annotations

import logging
import os

from ..config import Instrument
from ..models import Tick
from .base import TickHandler

log = logging.getLogger("breeze_feed")


def _num(value) -> float | None:
    """Parse a number; unknown stays None (never silently zero)."""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def tick_from_breeze(raw: dict, symbol: str, kind: str) -> Tick | None:
    """Convert one Breeze 'exchange quotes' message into our Tick."""
    last = _num(raw.get("last"))
    if last is None:
        return None
    bid, ask = _num(raw.get("bPrice")), _num(raw.get("sPrice"))
    if kind == "index" or not bid or not ask:
        bid = ask = None  # indices carry no real bid/ask
    return Tick(
        symbol=symbol, last=last, bid=bid, ask=ask,
        cum_volume=_num(raw.get("ttq")) if kind == "stock" else None,
        prev_close=_num(raw.get("close")), source="breeze",
    )


class BreezeFeed:
    name = "breeze"

    def __init__(self, watchlist: list[Instrument]):
        self._watch = watchlist
        self._sdk = None
        self._by_token: dict[str, Instrument] = {}

    def _login(self):
        key = os.environ.get("BREEZE_API_KEY", "")
        secret = os.environ.get("BREEZE_API_SECRET", "")
        token = os.environ.get("BREEZE_SESSION_TOKEN", "")
        if not (key and secret and token):
            raise RuntimeError(
                "Breeze credentials missing. Copy .env.example to .env and fill in "
                "BREEZE_API_KEY, BREEZE_API_SECRET and today's BREEZE_SESSION_TOKEN."
            )
        # Imported here because the SDK downloads ICICI's instrument list on import.
        from breeze_connect import BreezeConnect

        sdk = BreezeConnect(api_key=key)
        sdk.generate_session(api_secret=secret, session_token=token)
        return sdk

    def start(self, on_tick: TickHandler) -> None:
        self._sdk = self._login()
        self._sdk.ws_connect()

        def handle(raw: dict) -> None:
            try:
                inst = self._by_token.get(raw.get("symbol", ""))
                if inst is None or raw.get("quotes") != "Quotes Data":
                    return
                tick = tick_from_breeze(raw, inst.symbol, inst.kind)
                if tick is not None:
                    on_tick(tick)
            except Exception:  # a bad message must never kill the feed
                log.exception("failed to handle tick")

        self._sdk.on_ticks = handle
        for inst in self._watch:
            token = self._sdk.get_stock_token_value(
                exchange_code="NSE", stock_code=inst.breeze_code, product_type="cash",
                get_exchange_quotes=True, get_market_depth=False,
            )
            if not isinstance(token, tuple) or not token[0]:
                log.error("Unknown Breeze code %s for %s - skipped", inst.breeze_code, inst.symbol)
                continue
            self._by_token[token[0]] = inst
            self._sdk.subscribe_feeds(stock_token=token[0])
            log.info("subscribed %s (%s)", inst.symbol, token[0])

    def stop(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.ws_disconnect()
            except Exception:
                log.exception("disconnect failed")

    # ---- read-only helpers used by later steps ----
    def historical(self, inst: Instrument, interval: str, from_iso: str, to_iso: str) -> list[dict]:
        """interval: 1second | 1minute | 5minute | 30minute | 1day"""
        resp = self._sdk.get_historical_data_v2(
            interval=interval, from_date=from_iso, to_date=to_iso,
            stock_code=inst.breeze_code, exchange_code="NSE", product_type="cash",
        )
        return resp.get("Success") or []

    def option_chain(self, breeze_code: str, expiry_iso: str, right: str) -> list[dict]:
        """right: call | put. expiry_iso like 2026-11-17T06:00:00.000Z"""
        resp = self._sdk.get_option_chain_quotes(
            stock_code=breeze_code, exchange_code="NFO", product_type="options",
            expiry_date=expiry_iso, right=right,
        )
        return resp.get("Success") or []
