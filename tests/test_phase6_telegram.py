"""Phase 6 M3: the Telegram sender and the alert log. Send-only, to my own chat id only; incoming messages are never
read; the token never appears in a log line, an alert_log row or an error; alerts are OFF until switched on; a failed
send is retried with back-off until delivered or expired ("not delivered"); the same key is never sent twice."""
import logging
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.alerts import NOT_DELIVERED, OFF, PENDING, SENT, AlertBook
from app.store import Store
from app.telegram import SendError, TelegramSender
from tests.phase6_fixtures import settings

TOKEN = "123456:FAKE-token-for-tests-only"
CHAT = "424242"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
T = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


class Recorder:
    def __init__(self, fail=0, exc=None):
        self.calls, self.fail, self.exc = [], fail, exc

    def __call__(self, url, body, timeout):
        self.calls.append((url, body))
        if self.fail > 0:
            self.fail -= 1
            raise self.exc or urllib.error.URLError(f"cannot reach {url}")
        return 200, b'{"ok": true}'


def test_send_message_to_my_chat_only():
    import json
    rec = Recorder()
    TelegramSender(post=rec, env=ENV).send("hello")
    url, body = rec.calls[0]
    assert url.endswith("/sendMessage") and url.startswith("https://api.telegram.org/bot")
    assert json.loads(body) == {"chat_id": CHAT, "text": "hello", "disable_web_page_preview": True}


def test_errors_never_contain_the_token_or_chat(caplog):
    rec = Recorder(fail=1)                                    # the URL (with the token) is in the exception text
    with pytest.raises(SendError) as e:
        TelegramSender(post=rec, env=ENV).send("x")
    assert TOKEN not in str(e.value) and CHAT not in str(e.value) and "<redacted>" in str(e.value)
    # through the alert log and the logger
    s = settings(); raw = s.model_dump(); raw["alerts"]["telegram_enabled"] = True
    s = type(s).model_validate(raw)
    book = AlertBook(Store(":memory:"), s)
    book.enqueue("k1", "SIGNAL", "text", T)
    with caplog.at_level(logging.DEBUG):
        book.dispatch(T, TelegramSender(post=Recorder(fail=1), env=ENV))
    row = book.get("k1")
    assert TOKEN not in (row["error"] or "") and TOKEN not in caplog.text and CHAT not in caplog.text
    assert row["status"] == PENDING and row["attempts"] == 1


def test_missing_settings_named_never_valued():
    with pytest.raises(SendError) as e:
        TelegramSender(post=Recorder(), env={"TELEGRAM_CHAT_ID": CHAT}).send("x")
    assert str(e.value) == "TELEGRAM_BOT_TOKEN not set in .env"


def test_no_code_reads_incoming_messages():
    hits = [f.name for f in (ROOT / "app").rglob("*.py")
            if any(w in f.read_text(encoding="utf-8") for w in ("getUpdates", "setWebhook", "getWebhookInfo"))]
    assert hits == []


def test_alerts_are_off_until_switched_on():
    s = settings()
    assert s.alerts.telegram_enabled is False                   # the shipped config.yaml
    book = AlertBook(Store(":memory:"), s)
    assert book.enqueue("k", "SIGNAL", "t", T)
    rec = Recorder()
    assert book.get("k")["status"] == OFF
    book.dispatch(T + timedelta(minutes=1), TelegramSender(post=rec, env=ENV))
    assert rec.calls == []


def _on():
    s = settings(); raw = s.model_dump(); raw["alerts"]["telegram_enabled"] = True
    return type(s).model_validate(raw)


def test_retry_with_back_off_until_delivered_and_never_twice():
    book = AlertBook(Store(":memory:"), _on())
    book.enqueue("exit:p1:STOP:1", "EXIT_CALL", "stop", T, T + timedelta(hours=1))
    rec = Recorder(fail=2)
    snd = TelegramSender(post=rec, env=ENV)
    book.dispatch(T, snd)
    assert book.get("exit:p1:STOP:1")["next_attempt_utc"] == (T + timedelta(seconds=30)).isoformat()
    book.dispatch(T + timedelta(seconds=10), snd)               # not due yet
    assert len(rec.calls) == 1
    book.dispatch(T + timedelta(seconds=30), snd)
    assert book.get("exit:p1:STOP:1")["next_attempt_utc"] == (T + timedelta(seconds=90)).isoformat()   # 60 s more
    book.dispatch(T + timedelta(seconds=90), snd)
    row = book.get("exit:p1:STOP:1")
    assert (row["status"], row["attempts"], len(rec.calls)) == (SENT, 3, 3)
    book.dispatch(T + timedelta(minutes=5), snd)
    assert len(rec.calls) == 3                                  # never sent twice
    assert book.enqueue("exit:p1:STOP:1", "EXIT_CALL", "stop again", T) is False


def test_undelivered_until_expiry_is_shown_not_delivered():
    book = AlertBook(Store(":memory:"), _on())
    book.enqueue("signal:s1", "SIGNAL", "x", T, T + timedelta(minutes=2))
    snd = TelegramSender(post=Recorder(fail=99), env=ENV)
    for k in range(0, 300, 30):
        book.dispatch(T + timedelta(seconds=k), snd)
    row = book.get("signal:s1")
    assert row["status"] == NOT_DELIVERED and "expired before delivery" in row["error"]
    assert book.view()["not_delivered"][0]["key"] == "signal:s1"
