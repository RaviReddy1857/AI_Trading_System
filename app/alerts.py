"""Phase 6 alerts: what is sent, when, and the delivery log.

Every alert has a stable key (`signal:<signal_id>`, `exit:<position_id>:<kind>:<repeat number>`, `kill:<day>:<hh:mm>`,
`dailystop:<pool>:<day>`, `premarket:<day>`, `eod:<day>`, `heartbeat:...`, `asleep:...`, `login:<day>`) and a row
in `alert_log` (key, channel, kind, text, attempts, sent_at, error, status). The same key is never stored twice.
A send is recorded as SENDING before it starts. A failed send that certainly did not reach Telegram is retried
with back-off (`alerts.retry_first_seconds`, doubling, at most `retry_max_seconds`) until it is delivered or the
alert has expired; then it is shown as NOT_DELIVERED on the dashboard. A send whose delivery is uncertain (a
timeout or a dropped connection after the request went out, or a crash while SENDING, found at the next start):
information alerts (signals, pre-market plan, end-of-day summary) are not sent again and are shown as UNCERTAIN;
exit calls and urgent alerts (`RESEND_IF_UNCERTAIN`: kill switch, daily stop, heartbeat, sleep, expired login) are
sent again, and their text always ends with their alert key, so a second copy is recognisable as the same alert.
A Telegram failure is logged and the app carries on.

What is sent (only while `alerts.telegram_enabled` is true; otherwise every alert is still logged, status OFF, and
shown on the dashboard, and is never sent later):
- approved signals whose trade-type switch is on AND whose strategy's `strategies.status` allows alerts, the
  `alerts.top_per_bar` highest scores of a bar, at most `alerts.daily_signal_cap` a day; valid until the signal's
  valid-until (an expired signal is never sent);
- every exit call due (always, whatever the switches), including the stock option settlement alerts;
- the daily stop of a pool and the kill switch, once each time they trigger; the pre-market plan and the end-of-day
  summary; heartbeat, sleep and expired-login alerts (app/recovery.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .journal import POOLS, TRADE_TYPE_NAMES, ist_day
from .quality import IST

log = logging.getLogger("alerts")

PENDING, SENT, NOT_DELIVERED, OFF = "PENDING", "SENT", "NOT_DELIVERED", "OFF"
SENDING, UNCERTAIN = "SENDING", "UNCERTAIN"
# a duplicate of these is better than a lost one: resent after an uncertain delivery, always with their key
RESEND_IF_UNCERTAIN = frozenset({"EXIT_CALL", "KILL_SWITCH", "DAILY_STOP", "HEARTBEAT", "ASLEEP", "LOGIN_EXPIRED"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_log (
    key TEXT PRIMARY KEY, channel TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created_utc TEXT NOT NULL,
    expires_utc TEXT NOT NULL, attempts INTEGER NOT NULL, last_attempt_utc TEXT, next_attempt_utc TEXT,
    sent_utc TEXT, error TEXT, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_log_created ON alert_log (created_utc);
"""
_COLS = ["key", "channel", "kind", "text", "created_utc", "expires_utc", "attempts", "last_attempt_utc",
         "next_attempt_utc", "sent_utc", "error", "status"]


def hm(t: datetime) -> str:
    return t.astimezone(IST).strftime("%H:%M")


class AlertBook:
    def __init__(self, store, settings):
        self.s = settings
        self._db, self._lock = store._db, store._lock
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()
        self._recover_sending()

    def _recover_sending(self) -> None:
        """At start: an alert left SENDING was being sent when the app stopped; its delivery is uncertain."""
        for a in self.rows("status=?", (SENDING,), 500):
            self._uncertain(a, "the app stopped while sending: delivery not confirmed", a["last_attempt_utc"])

    def _uncertain(self, a: dict, why: str, at: str | None) -> None:
        err = (a["error"] + "; " if a["error"] else "") + why
        if a["kind"] in RESEND_IF_UNCERTAIN:
            self._set(a["key"], status=PENDING, error=err + " (sent again with its alert key)",
                      next_attempt_utc=at or a["created_utc"])
        else:
            self._set(a["key"], status=UNCERTAIN, error=err + " (not sent again: an information alert)")

    @staticmethod
    def text_of(a: dict) -> str:
        """The text sent: exit calls and urgent alerts end with their alert key (a duplicate is recognisable)."""
        return a["text"] + (f"\nAlert key: {a['key']}" if a["kind"] in RESEND_IF_UNCERTAIN else "")

    def enqueue(self, key: str, kind: str, text: str, now: datetime, expires: datetime | None = None,
                channel: str = "telegram") -> bool:
        """Log an alert once. True when it is new. While Telegram is off it is logged as OFF and never sent."""
        exp = expires or now + timedelta(minutes=self.s.alerts.max_alert_age_minutes)
        status = PENDING if self.s.alerts.telegram_enabled else OFF
        with self._lock:
            cur = self._db.execute(f"INSERT OR IGNORE INTO alert_log ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
                                   (key, channel, kind, text, now.isoformat(), exp.isoformat(), 0, None, now.isoformat(),
                                    None, None if status == PENDING else "Telegram is off (alerts.telegram_enabled)", status))
            self._db.commit()
            return cur.rowcount == 1

    def rows(self, where: str = "1=1", args=(), limit: int = 200) -> list[dict]:
        with self._lock:
            rs = self._db.execute(f"SELECT {','.join(_COLS)} FROM alert_log WHERE {where} ORDER BY created_utc DESC, key "
                                  f"LIMIT ?", (*args, limit)).fetchall()
        return [dict(zip(_COLS, r)) for r in rs]

    def get(self, key: str) -> dict | None:
        r = self.rows("key=?", (key,), 1)
        return r[0] if r else None

    def count_today(self, kind: str, now: datetime) -> int:
        start = datetime.combine(ist_day(now), datetime.min.time(), IST).astimezone(timezone.utc)
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM alert_log WHERE kind=? AND created_utc>=?",
                                    (kind, start.isoformat())).fetchone()[0]

    def dispatch(self, now: datetime, sender, clock=None) -> dict:
        """Send every pending alert that is due. Never raises: a failure is recorded and retried later.
        Each alert's expiry is checked against the clock immediately before its own send attempt (`clock()`; by
        default `now` plus the real time elapsed since the batch began), so a slow earlier send never lets an expired
        alert go out."""
        import time as _time
        start = _time.monotonic()
        if clock is None:
            def clock():
                return now + timedelta(seconds=_time.monotonic() - start)
        out = {"sent": 0, "failed": 0, "expired": 0}
        due = self.rows("status=? AND next_attempt_utc<=?", (PENDING, now.isoformat()), 500)
        for a in sorted(due, key=lambda r: r["created_utc"]):
            if clock() >= datetime.fromisoformat(a["expires_utc"]):     # the clock at this attempt, not the batch start
                self._set(a["key"], status=NOT_DELIVERED,
                          error=(a["error"] + "; " if a["error"] else "") + "expired before delivery")
                out["expired"] += 1
                continue
            attempts = a["attempts"] + 1
            self._set(a["key"], status=SENDING, attempts=attempts, last_attempt_utc=now.isoformat())
            try:
                sender.send(self.text_of(a))
            except Exception as e:                      # SendError is already redacted; anything else: type only
                from .telegram import SendError
                err = str(e) if isinstance(e, SendError) else f"{type(e).__name__}"
                wait = min(self.s.alerts.retry_first_seconds * 2 ** (attempts - 1), self.s.alerts.retry_max_seconds)
                if getattr(e, "uncertain", False):
                    self._uncertain({**a, "error": None}, f"delivery not confirmed ({err})",
                                    (now + timedelta(seconds=wait)).isoformat())
                    log.warning("alert %s: delivery not confirmed (attempt %d): %s", a["key"], attempts, err)
                    out["uncertain"] = out.get("uncertain", 0) + 1
                    continue
                self._set(a["key"], status=PENDING, error=err,
                          next_attempt_utc=(now + timedelta(seconds=wait)).isoformat())
                log.warning("alert %s not sent (attempt %d): %s", a["key"], attempts, err)
                out["failed"] += 1
                continue
            self._set(a["key"], attempts=attempts, last_attempt_utc=now.isoformat(), sent_utc=now.isoformat(),
                      status=SENT, error=None)
            out["sent"] += 1
        return out

    def _set(self, key: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE alert_log SET {cols} WHERE key=?", (*fields.values(), key))
            self._db.commit()

    def view(self) -> dict:
        return {"telegram_enabled": self.s.alerts.telegram_enabled, "recent": self.rows(limit=100),
                "pending": self.rows("status=?", (PENDING,), 100),
                "not_delivered": self.rows("status=?", (NOT_DELIVERED,), 100),
                "uncertain": self.rows("status=?", (UNCERTAIN,), 100)}


# ---------------- message texts (built by code) ----------------
def fmt(x) -> str:
    return "-" if x is None else f"{x:,.2f}"


def signal_text(card: dict) -> str:
    est = card.get("estimate") or {}
    size = f"{card['lots']} lot(s)" if card.get("lots") else f"{card.get('quantity')} shares"
    sc = (card.get("score") or {}).get("score")
    lines = [f"SIGNAL - {card['trade_type_name']}: {card['instrument']} {card['direction']}",
             f"Entry {fmt(card['entry_zone'][0])}-{fmt(card['entry_zone'][1])}  Stop {fmt(card['stop'])}  "
             f"Target {fmt(card['target'])}  Size {size}",
             f"Risk Rs {fmt(card.get('risk_inr'))}; {est.get('label', '')}: profit Rs {fmt(est.get('profit_if_target_inr'))}, "
             f"loss Rs {fmt(est.get('loss_if_stop_inr'))}",
             f"Valid until {card['valid_until']}. Score {sc if sc is not None else 'not scored'} (ranking aid). "
             f"{card['strategy_id']} v{card['strategy_version']}",
             "Why: " + "; ".join(card.get("why_now", [])[:4]),
             "Research only: you place the order yourself."]
    return "\n".join(lines)


def exit_text(c: dict) -> str:
    flag = " UNHEDGED" if not c.get("hedged", True) else ""
    lvl = f" {c['level']}" if c.get("level") else ""
    rep = f" (repeat {c['repeat_no']})" if c.get("repeat_no", 1) > 1 else ""
    return (f"EXIT CALL [{c['kind']}{lvl}] {c['symbol']} - {TRADE_TYPE_NAMES.get(c['trade_type'], c['trade_type'])}"
            f"{flag}{rep}\n{c['action']}\n{c['reason']}\nAlert only: mark the position closed in the journal when done.")


class Notifier:
    """Called by the engine on every bar (signals and exit calls) and by the loop on every step (schedule, dispatch)."""

    def __init__(self, settings, book: AlertBook, journal, risk, sender, calendar, store=None, regime=None):
        self.s, self.book, self.j, self.risk, self.sender, self.cal = settings, book, journal, risk, sender, calendar
        self.store, self.regime = store, regime
        self._kill_on = False

    def on_bar(self, approved: list[dict], due_calls: list[dict], now: datetime, kill_reasons=()) -> dict:
        a = self.s.alerts
        sent_signals = 0
        room = a.daily_signal_cap - self.book.count_today("SIGNAL", now)
        for row in [r for r in approved if r.get("alert_allowed")][:a.top_per_bar]:
            if room <= 0:
                break
            if row["valid_until"] <= now:
                continue                                   # an expired signal is never sent
            if self.book.enqueue(f"signal:{row['signal_id']}", "SIGNAL", signal_text(row["card"]), now, row["valid_until"]):
                sent_signals += 1
                room -= 1
        for c in due_calls:
            minutes = (self.s.alerts.settlement_repeat_minutes.URGENT if c["kind"] == "SETTLEMENT"
                       else a.exit_call_repeat_minutes)
            self.book.enqueue(c["key"], "EXIT_CALL", exit_text(c), now, now + timedelta(minutes=minutes))
        self.system(now, kill_reasons)
        return {"signal_alerts": sent_signals, "exit_alerts": len(due_calls)}

    def price_notices(self, notices: list[dict], now: datetime) -> dict:
        """Record held-price notices: once per durable key `price:<symbol>:<date>` (an existing key is done, never
        recorded again), expiring at the end of the symbol's continuous session. Returns {"done": symbols recorded or
        already recorded, "new": symbols recorded now}; a failure leaves the symbol out of both (retried next bar)."""
        done, new = [], []
        for n in notices:
            if n["expires"] <= now:
                continue                                 # the continuous session is over: never sent
            try:
                if self.book.get(n["key"]) is None and self.book.enqueue(n["key"], "PRICE_UNAVAILABLE", n["text"], now,
                                                                           n["expires"]):
                    new.append(n["symbol"])
                done.append(n["symbol"])
            except Exception:
                log.exception("held-price notice %s not recorded", n["key"])
        return {"done": done, "new": new}

    def system(self, now: datetime, kill_reasons=()) -> None:
        """Kill switch (once per episode) and each pool's daily stop (once a day)."""
        if kill_reasons and not self._kill_on:
            self.book.enqueue(f"kill:{ist_day(now)}:{hm(now)}", "KILL_SWITCH",
                              "KILL SWITCH ON - no new signals: " + "; ".join(kill_reasons) +
                              "\nExit calls for open positions continue on live prices.", now)
        self._kill_on = bool(kill_reasons)
        from .risk.limits import loss_stops
        for pool in POOLS:
            try:
                acct = self.risk.account_state(pool, now)
            except Exception:
                continue
            if acct.positions is None:
                continue
            stops, f = loss_stops(acct.capital_inr, acct.realised_today_inr, acct.realised_month_inr, acct.positions,
                                  self.s.risk.daily_loss_stop_pct, self.s.risk.monthly_loss_pause_pct)
            for code, text in stops:
                if code == "DAILY_STOP":
                    self.book.enqueue(f"dailystop:{pool}:{ist_day(now)}", "DAILY_STOP",
                                      f"DAILY STOP - {pool.replace('_', ' ').lower()} pool: {text}. "
                                      "No new signals in this pool today.", now)

    def tick(self, now: datetime) -> dict:
        """Every loop step: the scheduled messages, then the dispatch of what is due."""
        from .schedule import scheduled
        try:
            scheduled(self, now)
        except Exception:
            log.exception("scheduled message failed")
        if not self.s.alerts.telegram_enabled:
            return {"sent": 0}
        return self.book.dispatch(now, self.sender)
