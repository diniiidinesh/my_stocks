from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Groups often rewrite /confirm as /confirm@BotName. Privacy mode (default)
# only delivers slash-commands, @mentions, and replies to the bot.
_CONFIRM_RE = re.compile(
    r"^\s*(?:/)?confirm(?:@[A-Za-z0-9_]+)?\s+([A-Fa-f0-9]{4,12})\s*$",
    re.I,
)
_CANCEL_RE = re.compile(
    r"^\s*(?:/)?cancel(?:@[A-Za-z0-9_]+)?\s+([A-Fa-f0-9]{4,12})\s*$",
    re.I,
)


def normalize_chat_id(raw: object) -> str:
    return str(raw or "").strip().strip('"').strip("'")


def configured_chat_ids(raw: str) -> set[str]:
    return {normalize_chat_id(part) for part in (raw or "").split(",") if part.strip()}


def extract_message(update: dict[str, Any]) -> dict[str, Any]:
    return (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )


def parse_trade_command(text: str) -> tuple[str, str] | None:
    """Return ('confirm'|'cancel', ID) if *text* is a confirm/cancel command."""
    raw = str(text or "").strip()
    m = _CONFIRM_RE.match(raw)
    if m:
        return "confirm", m.group(1).upper()
    m = _CANCEL_RE.match(raw)
    if m:
        return "cancel", m.group(1).upper()
    return None


def summarize_chats(updates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Unique chats seen in a getUpdates payload (for TELEGRAM_CHAT_ID)."""
    seen: dict[str, dict[str, str]] = {}
    for update in updates:
        message = extract_message(update)
        chat = message.get("chat") or {}
        chat_id = normalize_chat_id(chat.get("id", ""))
        if not chat_id:
            continue
        title = str(
            chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        )
        seen[chat_id] = {
            "id": chat_id,
            "type": str(chat.get("type") or ""),
            "title": title,
            "text": str(message.get("text") or "")[:80],
        }
    return list(seen.values())


class TelegramConfirmListener:
    """Poll Telegram for CONFIRM <id> / CANCEL <id> while watch is running.

    The pending order id is the authorization, so a matching command from
    **any** chat the bot can see is accepted (typical: private chat vs group).
    A mismatch with TELEGRAM_CHAT_ID is logged so you can point alerts at the
    group. Slash commands work in groups even with BotFather privacy on.
    """

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
        self.chat_id = normalize_chat_id(chat_id)
        self.allowed_chat_ids = configured_chat_ids(chat_id)
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.poll_sec = poll_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._webhook_cleared = False

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

    def _clear_webhook_once(self) -> None:
        if self._webhook_cleared:
            return
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{self.bot_token}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=10.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Could not clear Telegram webhook: %s", exc)
        else:
            self._webhook_cleared = True

    def _run(self) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        while not self._stop.is_set():
            self._clear_webhook_once()
            try:
                resp = httpx.get(
                    url,
                    params={
                        "timeout": 25,
                        "offset": self._offset,
                    },
                    timeout=35.0,
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("Telegram poll failed: %s", exc)
                time.sleep(self.poll_sec)
                continue

            if not payload.get("ok", True):
                desc = payload.get("description") or payload
                logger.warning("Telegram getUpdates error: %s", desc)
                time.sleep(self.poll_sec)
                continue

            for update in payload.get("result", []):
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                self._handle(update)
            time.sleep(0.2)

    def _handle(self, update: dict[str, Any]) -> None:
        message = extract_message(update)
        chat = message.get("chat") or {}
        incoming_id = normalize_chat_id(chat.get("id", ""))
        text = str(message.get("text") or "").strip()
        if not text:
            return
        parsed = parse_trade_command(text)
        if parsed is None:
            return
        verb, pending_id = parsed
        if incoming_id and incoming_id not in self.allowed_chat_ids:
            logger.info(
                "Confirm command from chat_id=%s type=%s title=%r "
                "(TELEGRAM_CHAT_ID=%s). Acting on it; set TELEGRAM_CHAT_ID to "
                "this id if you want alerts in this chat too.",
                incoming_id,
                chat.get("type"),
                chat.get("title") or chat.get("username") or chat.get("first_name"),
                self.chat_id,
            )
        logger.info("Telegram %s %s from chat_id=%s", verb, pending_id, incoming_id)
        if verb == "confirm":
            self.on_confirm(pending_id)
            return
        if verb == "cancel" and self.on_cancel:
            self.on_cancel(pending_id)
