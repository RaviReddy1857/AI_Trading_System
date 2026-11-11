"""Simulated feed: random-walk prices so the whole app runs without a broker
login. It also injects an occasional bad tick so you can see the quality gate
working. NOT real market data."""
from __future__ import annotations

import random
import threading
import time

from ..config import Instrument
from ..models import Tick
from .base import TickHandler

START_PRICES = {"NIFTY": 23500.0, "BANKNIFTY": 51000.0}


class SimFeed:
    name = "sim"

    def __init__(self, watchlist: list[Instrument], interval_s: float = 0.5,
                 bad_tick_every: int = 300, seed: int | None = None):
        self._watch = watchlist
        self._interval = interval_s
        self._bad_every = bad_tick_every
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_tick: TickHandler) -> None:
        self._thread = threading.Thread(target=self._run, args=(on_tick,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self, on_tick: TickHandler) -> None:
        price = {i.symbol: START_PRICES.get(i.symbol, self._rng.uniform(800, 3500))
                 for i in self._watch}
        prev_close = dict(price)
        vol = {i.symbol: 0.0 for i in self._watch}
        n = 0
        while not self._stop.is_set():
            for inst in self._watch:
                n += 1
                s = inst.symbol
                price[s] *= 1 + self._rng.gauss(0, 0.0004)
                last = round(price[s], 2)
                if inst.kind == "stock":
                    vol[s] += self._rng.randint(10, 500)
                    spread = max(0.05, round(last * 0.0002, 2))
                    bid, ask, cum = round(last - spread, 2), round(last + spread, 2), vol[s]
                else:
                    bid = ask = cum = None
                if self._bad_every and n % self._bad_every == 0:
                    last = round(last * 1.2, 2)  # deliberate bad tick (20% jump)
                on_tick(Tick(s, last, bid, ask, cum, round(prev_close[s], 2), source="sim"))
            self._stop.wait(self._interval)
