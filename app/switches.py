"""Phase 6 trade-type alert switches.

Six switches, one per trade type (app.journal.TRADE_TYPES). Each controls alerts only (the dashboard highlight
and Telegram) for signals of that type; every strategy keeps paper-trading whatever its switch says. Stored in
the database (`alert_switches`), changed only through POST /journal/alert-switches, default OFF, every change
logged with its time and old and new value. Exit calls for positions I hold are always sent, whatever the
switch.

A signal alert needs BOTH the type's switch on AND the strategy's `strategies.status` entry allowing alerts
(docs/architecture.md section 15: the default until the user decides otherwise), so a strategy labelled
"negative in backtests" stays silent until its status is changed in config.yaml.
"""
from __future__ import annotations

from .journal import TRADE_TYPE_NAMES, TRADE_TYPES


def alert_allowed(switches: dict, trade_type: str, strategy_status) -> tuple[bool, str]:
    """(allowed, why) for a signal alert of this type and strategy."""
    on = bool(switches.get(trade_type, {}).get("on"))
    if not on:
        return False, f"switch '{TRADE_TYPE_NAMES.get(trade_type, trade_type)}' is off: paper only, no alert"
    if not strategy_status.alerts:
        return False, f"strategy status '{strategy_status.label}' does not allow alerts (strategies.status in config.yaml)"
    return True, "switch on and the strategy's status allows alerts"


def parse_switch_body(payload: dict) -> tuple[str, bool]:
    """POST /journal/alert-switches body: {"type": one of the six, "on": true/false}."""
    t, on = payload.get("type"), payload.get("on")
    if t not in TRADE_TYPES:
        raise ValueError(f"type must be one of {list(TRADE_TYPES)}")
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    return t, on
