"""READ-ONLY ICICI Breeze feed.

This module is the only place the broker SDK is touched, and it uses data
functions only: login session, live quote subscription, historical bars and
option chain. Nothing in this project sends, changes or closes an order, and
tests/test_safety.py fails the build if such a call is ever added.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import os
import time
from collections.abc import Callable

from ..config import Instrument
from ..models import Tick
from .base import TickHandler

log = logging.getLogger("breeze_feed")


def _num(value) -> float | None:
    """Parse a number; unknown stays None (never silently zero). NaN/inf count as unknown."""
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def tick_from_breeze(raw: dict, symbol: str, kind: str) -> Tick | None:
    """Convert one Breeze 'exchange quotes' message into our Tick."""
    last = _num(raw.get("last"))
    if last is None:
        return None
    bid, ask = _num(raw.get("bPrice")), _num(raw.get("sPrice"))
    negative = any(q is not None and q < 0 for q in (bid, ask))
    if kind == "index":
        bid = ask = None  # indices carry no real bid/ask
    elif not negative and (not bid or not ask):
        bid = ask = None  # 0 or missing on either side = no quote
    # Negative values are passed through on purpose: the quality gate quarantines
    # and logs them (app/quality.py) instead of this parser hiding them.
    return Tick(
        symbol=symbol, last=last, bid=bid, ask=ask,
        cum_volume=_num(raw.get("ttq")) if kind == "stock" else None,
        prev_close=_num(raw.get("close")), source="breeze",
        day_high=_num(raw.get("high")), day_low=_num(raw.get("low")),
    )


def _subscribed_ok(resp) -> bool:
    """breeze-connect 1.0.69 returns {"message": "Stock ... subscribed successfully"} on success
    and a plain string ("Exception while subscribing to feeds ...") on failure."""
    return isinstance(resp, dict) and "subscribed" in str(resp.get("message", "")).lower()


class BreezeFeed:
    name = "breeze"

    def __init__(self, watchlist: list[Instrument], subscribe_pause_seconds: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep):
        self._watch = watchlist
        self._sdk = None
        self._by_token: dict[str, Instrument] = {}
        self._pause = subscribe_pause_seconds
        self._sleep = sleep
        # Filled by start(): {"subscribed": [symbols], "failed": [(symbol, code, why)], "total": n}
        self.subscription_summary: dict | None = None

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

    def connect(self) -> None:
        """Log in for data calls only (used by the history downloader)."""
        if self._sdk is None:
            self._sdk = self._login()

    def _subscribe_one(self, inst: Instrument) -> str | None:
        """Subscribe one symbol. Returns None on success, else the reason it failed."""
        token = self._sdk.get_stock_token_value(
            exchange_code="NSE", stock_code=inst.breeze_code, product_type="cash",
            get_exchange_quotes=True, get_market_depth=False,
        )
        if not isinstance(token, tuple) or not token or not token[0]:
            return f"token lookup failed; SDK returned {type(token).__name__}: {token!r}"
        try:
            resp = self._sdk.subscribe_feeds(stock_token=token[0])
        except Exception as e:  # one symbol must not stop the others
            return f"subscribe raised {type(e).__name__}: {e}"
        if not _subscribed_ok(resp):
            return f"subscribe refused: {resp!r}"
        self._by_token[token[0]] = inst
        log.info("subscribed %s (%s)", inst.symbol, token[0])
        return None

    def start(self, on_tick: TickHandler) -> None:
        self.connect()
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
        # SDK quirk (breeze-connect 1.0.69): get_stock_token_value() reads self.interval,
        # which the SDK only sets inside subscribe_feeds(). Called before any subscription
        # it raises AttributeError internally and RETURNS the exception instead of a token.
        # "" is the same value subscribe_feeds() uses by default (plain quote stream).
        self._sdk.interval = ""
        ok: list[str] = []
        failed: list[tuple[str, str, str]] = []
        for n, inst in enumerate(self._watch):
            if n and self._pause:
                self._sleep(self._pause)          # do not fire ~50 subscriptions at once
            try:
                why = self._subscribe_one(inst)
            except Exception as e:
                why = f"{type(e).__name__}: {e}"
            if why is None:
                ok.append(inst.symbol)
            else:
                failed.append((inst.symbol, inst.breeze_code, why))
                log.error("Breeze subscription failed for %s (code %s) - skipped; %s",
                          inst.symbol, inst.breeze_code, why)
        self.subscription_summary = {"subscribed": ok, "failed": failed, "total": len(self._watch)}
        log.info("Breeze live feed: subscribed %d of %d symbols", len(ok), len(self._watch))
        if failed:
            log.warning("Breeze live feed: %d failed: %s", len(failed),
                        ", ".join(f"{s} ({c})" for s, c, _ in failed))

    def stop(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.ws_disconnect()
            except Exception:
                log.exception("disconnect failed")

    # ---- read-only helpers used by later steps ----
    def historical(self, inst: Instrument, interval: str, from_iso: str, to_iso: str) -> list[dict]:
        """interval: 1second | 1minute | 5minute | 30minute | 1day"""
        self.connect()
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


# ---- instrument list (no login needed) ----
NSE_SCRIP_FIELDS = ("Token", "ShortName", "Series", "CompanyName", "ISINCode", "ExchangeCode")


def parse_scrip_master(text_lines) -> list[dict[str, str]]:
    """Rows of ICICI's NSEScripMaster.txt as dicts with clean keys. ShortName is the
    Breeze stock code; ExchangeCode is the NSE symbol."""
    reader = csv.reader(text_lines)
    header = [h.strip().strip('"') for h in next(reader, [])]
    missing = [f for f in NSE_SCRIP_FIELDS if f not in header]
    if missing:
        raise ValueError(f"NSE instrument list has no {missing} column(s)")
    idx = {f: header.index(f) for f in NSE_SCRIP_FIELDS}
    out = []
    for row in reader:
        if len(row) < len(header):
            continue
        out.append({f: row[i].strip().strip('"').strip() for f, i in idx.items()})
    return out


def nse_scrip_master() -> list[dict[str, str]]:
    """ICICI's NSE instrument list, exactly as breeze-connect downloads it when imported
    (SecurityMaster.zip, the file get_names() reads). Public file: no login, no order call."""
    from breeze_connect import breeze_connect as sdk_module
    from breeze_connect import config as sdk_config

    name = sdk_config.ISEC_NSE_CODE_MAP_FILE["nse"]
    with sdk_module.zip.open(name) as f:
        return parse_scrip_master(io.TextIOWrapper(f, encoding="utf-8"))
