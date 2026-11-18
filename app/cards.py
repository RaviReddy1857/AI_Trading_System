"""Phase 6 signal cards: everything shown for one signal, built by code from the signal, its risk decision, the
score and the track record. No number on a card comes from an LLM.

An approved card carries: instrument, direction, entry zone, stop, target, quantity or lots, valid-until, strategy
id and version, regime and the data timestamp; 'Why now' in plain words (the rule conditions that fired, the
regime, the FII bias, the events this week); an 'Estimate if target is hit, not a forecast' section in rupees; the
strategy's past results with their sample size; the score (a ranking aid). A NO_TRADE card is collapsed and lists
every reason. A card past its valid-until is shown greyed out and cannot be acted on.
"""
from __future__ import annotations

from datetime import datetime

from .journal import TRADE_TYPE_NAMES, pool_of_trade_type, signal_trade_type
from .quality import IST

ESTIMATE_LABEL = "Estimate if target is hit, not a forecast"


def ist(t: datetime | str | None) -> str | None:
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t.astimezone(IST).strftime("%d-%b-%Y %H:%M IST")


def instrument(sig) -> str:
    if not sig.legs:
        return f"{sig.symbol} (cash)"
    parts = [f"{x.action} {x.strike:g} {x.right} {x.expiry:%d-%b-%Y}" for x in sig.legs]
    return f"{sig.symbol} options: " + "; ".join(parts)


def why_now(sig, regime: str | None, bias: str | None, events_week: list[dict] | None, coverage=None) -> list[str]:
    """The rule conditions that fired (from the strategy, with their values), then the market facts. `coverage`
    (`manual_data.WeekCoverage`): "no event" is said only when the instrument's results dates are covered for the
    week (an index: every watchlist stock's); otherwise EVENT COVERAGE MISSING, with any events that are known."""
    from .manual_data import EVENT_COVERAGE_MISSING
    out = list(sig.why_now)
    out.append(f"market regime is {regime or 'UNKNOWN'}")
    out.append(f"FII bias is {bias or 'UNKNOWN'}")
    if events_week is None:
        out.append("events this week not known (events.csv not loaded)")
        return out
    mine = [e for e in events_week if e.get("symbol") in ("", None, sig.symbol) and e.get("type") != "COVERAGE"]
    listed = "; ".join(f"{e['date']} {e['type']}" + (f" {e['symbol']}" if e.get("symbol") else "") for e in mine[:4])
    symbols = None if sig.asset_class == "INDEX_OPTION" else [sig.symbol]
    missing = (coverage.text(symbols) if coverage is not None
               else f"{EVENT_COVERAGE_MISSING} (coverage not checked)")
    if missing:
        out.append(missing + (f"; known: {listed}" if listed else ""))
    else:
        out.append("events this week: " + listed if listed else "no event for this instrument or the market this week")
    return out


def estimate(sig, decision) -> dict | None:
    """Rupee estimate of an approved signal for its approved size (decision figures; costs included)."""
    if decision.status != "APPROVED":
        return None
    d = sig.details or {}
    profit, loss, costs = decision.est_profit_inr, decision.est_loss_inr, decision.costs_inr
    out = {"label": ESTIMATE_LABEL, "profit_if_target_inr": profit, "loss_if_stop_inr": loss, "costs_inr": costs,
           "risk_reward": round(profit / loss, 2) if profit is not None and loss else None,
           "breakevens": d.get("breakevens")}
    selling = any(x.action == "SELL" for x in sig.legs or ())
    if selling and decision.lots:
        sold = sum(x.price for x in sig.legs if x.action == "SELL")
        bought = sum(x.price for x in sig.legs if x.action == "BUY")
        shares = decision.lots * sig.legs[0].lot_size
        credit = round((sold - bought) * shares, 2)
        out["premium_collected_inr"] = credit
        out["max_profit_inr"] = round(credit - (costs or 0.0), 2) if costs is not None else None
        out["max_profit_note"] = "option selling: maximum profit = premium collected less charges (held to expiry)"
        out["loss_if_stop_note"] = "spread: maximum loss"
    if not sig.legs and decision.quantity and costs is not None:
        per = costs / decision.quantity
        out["breakevens"] = [round(sig.entry + per if sig.direction == "LONG" else sig.entry - per, 2)]
        out["breakeven_note"] = "entry plus the round-trip charges per share"
    return out


ORIGIN_TEXT = {"PENDING": "approved signal awaiting your decision", "MANUAL": "your position (entered by hand)",
               "SIGNAL": "your position (from a signal you took)"}


def commitment(decision) -> dict | None:
    """What the capital-pool check counted (decision facts["pool"]): my real book only - my open positions and the
    approved signals awaiting my decision. Paper trades run in their own simulated book and are never in it.
    A snapshot taken when the signal was issued: informational only. Every new approval is checked on the live
    book at that moment (`journal.pool_account`), never on this."""
    pf = (decision.facts or {}).get("pool") if hasattr(decision, "facts") else None
    if not pf:
        return None
    return {"book": "REAL: your open positions and approved signals awaiting your decision (paper trades are a "
                    "separate simulated book and are not counted)",
            "as_of": "when this signal was issued (informational; a later signal is checked on the live book)",
            "committed_inr": pf.get("committed_inr"), "new_inr": pf.get("new_inr"), "after_inr": pf.get("after_inr"),
            "usable_inr": pf.get("usable_inr"), "capital_inr": pf.get("capital_inr"), "fit": pf.get("capital_fit"),
            "items": [{"what": ORIGIN_TEXT.get(x.get("origin"), str(x.get("origin")).lower()), "symbol": x.get("symbol"),
                       "id": x.get("id"), "committed_inr": x.get("committed_inr")} for x in pf.get("items") or []]}


def build_card(sig, decision, *, score: dict | None, regime: str | None, bias: str | None,
               events_week: list[dict] | None, track: dict | None, alert: dict | None, now: datetime,
               coverage=None) -> dict:
    tt = signal_trade_type(sig)
    approved = decision.status == "APPROVED"
    zone = list(sig.entry_zone) if sig.entry_zone else [sig.entry, sig.entry]
    card = {
        "signal_id": sig.signal_id, "status": decision.status, "collapsed": not approved,
        "instrument": instrument(sig), "symbol": sig.symbol, "asset_class": sig.asset_class,
        "trade_type": tt, "trade_type_name": TRADE_TYPE_NAMES[tt], "pool": pool_of_trade_type(tt),
        "direction": sig.direction, "horizon": sig.horizon,
        "entry_zone": zone, "entry_reference": sig.entry, "stop": sig.stop, "target": sig.target,
        "price_basis": ("option premium per share (spreads: net credit; stop and target = cost to buy the spread back)"
                        if sig.legs else "share price"),
        "legs": [x.to_dict() for x in sig.legs] if sig.legs else None,
        "quantity": decision.quantity if approved else None, "lots": decision.lots if approved else None,
        "risk_inr": decision.risk_inr, "risk_pct": decision.risk_pct, "margin_inr": decision.margin_inr,
        "capital_fit": decision.capital_fit, "size_factor": decision.size_factor,
        "valid_until_utc": sig.valid_until.isoformat(), "valid_until": ist(sig.valid_until),
        "strategy_id": sig.strategy_id, "strategy_version": sig.strategy_version, "regime": sig.regime,
        "regime_now": regime, "data_as_of_utc": sig.data_as_of.isoformat(), "data_as_of": ist(sig.data_as_of),
        "created": ist(sig.created_at), "built": ist(now),
        "why_now": why_now(sig, regime, bias, events_week, coverage) if approved else list(sig.why_now),
        "estimate": estimate(sig, decision),
        "track_record": track, "score": score,
        "reasons": list(decision.reasons), "reason_codes": list(decision.reason_codes),
        "commitment": commitment(decision),
        "warnings": list(decision.warnings) + list((sig.details or {}).get("warnings") or []),
        "alert": alert,
        "note": "Research only: you place every order yourself in ICICI Direct.",
    }
    return card


def card_view(card: dict, now: datetime, decision_row: dict | None = None) -> dict:
    """The stored card with its state at `now`: expired (greyed out, cannot be acted on) and my decision."""
    expired = now >= datetime.fromisoformat(card["valid_until_utc"])
    return {**card, "expired": expired, "actionable": card["status"] == "APPROVED" and not expired and decision_row is None,
            "my_decision": decision_row}
