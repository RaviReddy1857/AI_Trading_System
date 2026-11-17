"""Phase 6: Telegram sender. Send-only.

- Calls exactly one Bot API method, `sendMessage`, and only to the chat id in .env (TELEGRAM_CHAT_ID). It never
  asks Telegram for incoming updates and runs no webhook, so messages from any chat - including any other chat -
  are never read or acted on.
- The bot token (TELEGRAM_BOT_TOKEN in .env) is read from the environment at send time and used only to build the
  request URL. It is never printed, logged, stored or returned: every error text is redacted before it leaves this
  module, and a missing setting is reported by its NAME only.
- Whether anything is sent at all is decided by the caller (`alerts.telegram_enabled`, OFF by default).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.telegram.org"
TOKEN_ENV, CHAT_ENV = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
MAX_TEXT = 4000                       # Telegram's limit is 4096 characters


class SendError(RuntimeError):
    """A failed send. The message never contains the token. `uncertain`: the request reached Telegram but no
    reply was read (a timeout or a dropped connection after the send, or an unreadable reply), so the message
    may have been delivered; otherwise it was certainly not delivered."""

    def __init__(self, message: str, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def _after_send(e: BaseException) -> bool:
    """True when the failure happened after the request was sent (urllib wraps errors while sending the request
    in URLError; a timeout or a dropped connection while waiting for the reply is raised as is)."""
    import http.client
    import socket
    if isinstance(e, urllib.error.URLError):
        return False
    return isinstance(e, (TimeoutError, socket.timeout, http.client.RemoteDisconnected, http.client.IncompleteRead,
                          ConnectionResetError))


def redact(text: str, *secrets: str) -> str:
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "<redacted>")
    return out


def _post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:          # noqa: S310 (fixed https host)
        return r.status, r.read()


class TelegramSender:
    channel = "telegram"

    def __init__(self, timeout: float = 10.0, post=_post, env=os.environ):
        self.timeout, self._post, self._env = timeout, post, env

    def configured(self) -> tuple[bool, str]:
        missing = [n for n in (TOKEN_ENV, CHAT_ENV) if not self._env.get(n)]
        return (not missing, "" if not missing else f"{', '.join(missing)} not set in .env")

    def send(self, text: str) -> None:
        """Send one message to my chat. Raises SendError (redacted) on any failure."""
        token, chat = self._env.get(TOKEN_ENV, ""), self._env.get(CHAT_ENV, "")
        ok, why = self.configured()
        if not ok:
            raise SendError(why)
        body = json.dumps({"chat_id": chat, "text": text[:MAX_TEXT], "disable_web_page_preview": True}).encode()
        try:
            status, raw = self._post(f"{API}/bot{token}/sendMessage", body, self.timeout)
        except urllib.error.HTTPError as e:
            raise SendError(redact(f"HTTP {e.code}: {e.reason}", token, chat)) from None
        except Exception as e:                                       # network, timeout, DNS: never echo the URL
            raise SendError(redact(f"{type(e).__name__}: {e}", token, chat), uncertain=_after_send(e)) from None
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            reply = None
        if status == 200 and not (isinstance(reply, dict) and isinstance(reply.get("ok"), bool)):
            # the request reached Telegram and was answered, but the answer is not a Bot API reply: never a plain
            # failure (a resend could duplicate a delivered message)
            raise SendError("Telegram's reply could not be read or had an unexpected structure (HTTP 200): "
                            "delivery not confirmed", uncertain=True)
        if status == 200 and reply["ok"] is True:
            return
        desc = reply.get("description", "") if isinstance(reply, dict) else ""
        raise SendError(redact(f"Telegram refused the message (HTTP {status}): {desc}", token, chat))
