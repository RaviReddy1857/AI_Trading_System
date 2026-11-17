"""Phase 6 step 14: failure recovery. Each failure ends in a safe, visible state: never a signal on stale or missing
data, open positions still monitored, and an alert that could not be delivered sent again.

The `Watchdog` runs in its own thread (so it still reports when the signal loop is stuck) and checks, during
market hours on a trading day:
- feed: the index price not LIVE -> one heartbeat alert per outage ("feed STALE since hh:mm"); after
  `engine.feed_reconnect_after_seconds` the feed is asked to reconnect itself (`feed.reconnect()`, if it has one),
  at most once per that interval. Signals stop by themselves: the risk engine says DATA_STALE / KILL_SWITCH, and
  they resume only on fresh LIVE prices.
- engine: a bar closed `alerts.heartbeat_bar_gap_seconds` ago and was not processed -> one heartbeat alert per bar.
- sleep: the wall clock jumped more than `alerts.sleep_gap_seconds` since the watchdog last ran (the laptop slept)
  -> one alert "system was asleep from hh:mm to hh:mm". Prices are STALE until fresh ticks arrive (the hub's
  freshness rule), exit calls due in the gap are evaluated on the next bar with fresh prices, and alerts or signals
  whose valid-until passed are not sent (the alert log drops them as expired).
- Breeze login: an expired session reported by the broker (`feed.login_state`) -> red on the dashboard and the Data
  status page and one alert a day with the re-login steps. The session token is never read here or logged.
After a restart the dashboard shows "restarted at hh:mm, data from hh:mm" (the first good tick after the start).

Hours come from the session calendar (app/session.py): the engine heartbeat and the sleep check cover the market day
to the last segment's close (15:40 from 3 Aug 2026, options); the feed check covers the index's continuous trading
only (in the closing auction the index is indicative, AUCTION, which is expected and not a feed fault).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .engine import floor_bar, session_open_utc
from .journal import ist_day
from .quality import IST
from .session import INDEX

RELOGIN_STEPS = ("Breeze login expired. To resume: 1) open the ICICI Breeze login link and log in; 2) copy the new "
                 "API session value into BREEZE_SESSION_TOKEN in .env; 3) restart the app. No signal is issued until "
                 "fresh LIVE prices arrive; exit calls resume on fresh prices.")


def hm(t: datetime | None) -> str:
    return t.astimezone(IST).strftime("%H:%M") if t else "-"


class Watchdog:
    def __init__(self, settings, hub, journal, alertbook, calendar, feed, engine=None, now_fn=None):
        self.s, self.hub, self.j, self.book, self.cal, self.feed, self.engine = (settings, hub, journal, alertbook,
                                                                                 calendar, feed, engine)
        self.now_fn = now_fn
        self.started = None
        self.last_run: datetime | None = None
        self.stale_since: datetime | None = None
        self.last_reconnect: datetime | None = None
        self.asleep: list[dict] = []
        prev = journal.get_state("last_bar_utc")
        self.previous_last_bar = datetime.fromisoformat(prev) if prev else None
        last = journal.get_state("watchdog_last_utc")
        self.last_run = datetime.fromisoformat(last) if last else None

    def start(self, now: datetime) -> None:
        self.started = now

    def market_open(self, now: datetime) -> bool:
        return self.cal.is_open(now)

    def check(self, now: datetime) -> list[str]:
        """One watchdog pass. Returns the alert kinds raised."""
        raised = []
        s = self.s
        # ---- sleep (wall-clock jump) ----
        if self.last_run is not None and (now - self.last_run).total_seconds() > s.alerts.sleep_gap_seconds:
            gap_from, gap_to = self.last_run, now
            if self._overlaps_session(gap_from, gap_to):
                key = f"asleep:{gap_from.isoformat()}"
                text = (f"System was asleep from {hm(gap_from)} to {hm(gap_to)} IST ({gap_from:%d-%b}). Prices are STALE "
                        f"until fresh ticks arrive; exit calls due in the gap are re-checked on fresh prices; signals "
                        f"that expired meanwhile are not sent.")
                if self.book.enqueue(key, "ASLEEP", text, now):
                    raised.append("ASLEEP")
                self.asleep.append({"from_utc": gap_from.isoformat(), "to_utc": gap_to.isoformat()})
        self.last_run = now
        self.j.set_state("watchdog_last_utc", now.isoformat(), now)
        # ---- Breeze login ----
        ls = getattr(self.feed, "login_state", None)
        if isinstance(ls, dict) and ls.get("state") == "EXPIRED":
            if self.book.enqueue(f"login:{ist_day(now)}", "LOGIN_EXPIRED", RELOGIN_STEPS, now,
                                 now + timedelta(hours=12)):
                raised.append("LOGIN_EXPIRED")
        if not self.market_open(now):
            self.stale_since = None
            return raised
        # ---- feed (the index's continuous trading only: in the closing auction it is indicative, not stale) ----
        idx = self.s.regime.index_symbol
        q = self.hub.quote(idx, now)
        if not self.cal.is_open(now, INDEX, continuous=True):
            self.stale_since = None
        elif q["status"] != "LIVE":
            since = q["at"] or max(session_open_utc(ist_day(now)), self.started or now)
            if self.stale_since is None:
                self.stale_since = since
            if self.book.enqueue(f"heartbeat:feed:{ist_day(now)}:{self.stale_since.isoformat()}", "HEARTBEAT",
                                 f"HEARTBEAT: {idx} feed {q['status']} since {hm(self.stale_since)} IST. No new signals "
                                 f"until fresh LIVE prices; exit calls paused for stale prices.", now):
                raised.append("HEARTBEAT_FEED")
            waited = (now - self.stale_since).total_seconds()
            if (waited >= s.engine.feed_reconnect_after_seconds and hasattr(self.feed, "reconnect")
                    and (self.last_reconnect is None
                         or (now - self.last_reconnect).total_seconds() >= s.engine.feed_reconnect_after_seconds)):
                self.last_reconnect = now
                try:
                    self.feed.reconnect()
                    raised.append("RECONNECT")
                except Exception:
                    raised.append("RECONNECT_FAILED")
        else:
            self.stale_since = None
        # ---- engine ----
        b = floor_bar(now)
        close_t = self.cal.last_close_utc(ist_day(now))
        last = self.engine.last_bar if self.engine is not None else None
        if (session_open_utc(ist_day(now)) < b <= close_t and (last is None or last < b)
                and (now - b).total_seconds() >= s.alerts.heartbeat_bar_gap_seconds):
            if self.book.enqueue(f"heartbeat:engine:{b.isoformat()}", "HEARTBEAT",
                                 f"HEARTBEAT: the signal loop has not processed the {hm(b)} bar "
                                 f"({int((now - b).total_seconds())} s ago). Signals and exit calls are not being "
                                 f"updated.", now):
                raised.append("HEARTBEAT_ENGINE")
        return raised

    def _overlaps_session(self, a: datetime, b: datetime) -> bool:
        d = ist_day(a)
        while d <= ist_day(b):
            if self.cal.is_trading_day(d):
                o = session_open_utc(d)
                c = self.cal.last_close_utc(d)
                if a < c and b > o:
                    return True
            d += timedelta(days=1)
        return False

    def status(self, now: datetime) -> dict:
        """For the dashboard banner."""
        banners = []
        first = self.hub.first_tick_at() if hasattr(self.hub, "first_tick_at") else None
        restart = None
        if self.previous_last_bar is not None and self.started is not None:
            restart = (f"restarted at {hm(self.started)}, data from "
                       + (hm(first) if first else "- (no fresh tick yet)"))
        ls = getattr(self.feed, "login_state", None)
        if isinstance(ls, dict) and ls.get("state") == "EXPIRED":
            banners.append({"colour": "red", "text": "Breeze login EXPIRED: no signals until you log in again ("
                                                     + (ls.get("message") or "") + ")"})
        if self.stale_since is not None:
            banners.append({"colour": "red", "text": f"{self.s.regime.index_symbol} feed stale since "
                                                     f"{hm(self.stale_since)}: no new signals; exit calls paused for "
                                                     f"stale prices"})
        if self.asleep:
            a = self.asleep[-1]
            banners.append({"colour": "warn", "text": f"system was asleep from {hm(datetime.fromisoformat(a['from_utc']))} "
                                                      f"to {hm(datetime.fromisoformat(a['to_utc']))}"})
        nd = self.book.rows("status=?", ("NOT_DELIVERED",), 50)
        if nd:
            banners.append({"colour": "red", "text": f"{len(nd)} alert(s) not delivered (see Alerts)"})
        unc = self.book.rows("status=?", ("UNCERTAIN",), 50)
        if unc:
            banners.append({"colour": "warn", "text": f"{len(unc)} information alert(s) may not have arrived (delivery "
                                                      f"not confirmed; not sent again - see Alerts)"})
        return {"restart_text": restart, "banners": banners, "login": ls,
                "stale_since_utc": self.stale_since.isoformat() if self.stale_since else None}
