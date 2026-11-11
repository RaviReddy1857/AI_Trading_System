from __future__ import annotations

from typing import Callable, Protocol

from ..models import Tick

TickHandler = Callable[[Tick], None]


class Feed(Protocol):
    name: str

    def start(self, on_tick: TickHandler) -> None: ...
    def stop(self) -> None: ...
