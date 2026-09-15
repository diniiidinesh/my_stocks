from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CONFIRM_RE = re.compile(r"^\s*(?:/)?confirm\s+([A-Fa-f0-9]{4,12})\s*$", re.I)
_CANCEL_RE = re.compile(r"^\s*(?:/)?cancel\s+([A-Fa-f0-9]{4,12})\s*$", re.I)


class TelegramConfirmListener:
    """Poll Telegram for CONFIRM <id> / CANCEL <id> while watch is running."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        on_confirm: Callable[[str], None],
        on_cancel: Callable[[str], None] | None = None,
        poll_sec: float = 2.0,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.poll_sec = poll_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="telegram-confirm", daemon=True
        )
        self._thread.start()
        logger.info("Telegram confirm listener started (chat_id=%s)", self.chat_id)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        while not self._stop.is_set():
            try:
                resp = httpx.get(
                    url,
                    params={
                        "timeout": 25,
                        "offset": self._offset,
                        "allowed_updates": '["message"]',
                    },
                    timeout=35.0,
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("Telegram poll failed: %s", exc)
                time.sleep(self.poll_sec)
                continue

            for update in payload.get("result", []):
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                self._handle(update)
            time.sleep(0.2)

    def _handle(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != self.chat_id:
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        m = _CONFIRM_RE.match(text)
        if m:
            self.on_confirm(m.group(1).upper())
            return
        m = _CANCEL_RE.match(text)
        if m and self.on_cancel:
            self.on_cancel(m.group(1).upper())
