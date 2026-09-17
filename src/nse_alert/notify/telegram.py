from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from nse_alert.engine import Alert

logger = logging.getLogger(__name__)

# Same zone as EOD report gaps / screener clock.
IST = ZoneInfo("Asia/Kolkata")


def format_ist_clock(dt: datetime) -> str:
    """Format an alert time in IST (internal timestamps stay UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def _alert_tags(alert: Alert) -> str:
    tags: list[str] = []
    if alert.is_fno:
        tags.append("F&O")
    if alert.is_asm:
        tags.append("ASM")
    return " · ".join(tags)


def _format_telegram_alert(alert: Alert) -> str:
    arrow = "▲" if alert.direction == "UP" else "▼"
    tags = _alert_tags(alert)
    tag_line = f"\nTags: *{tags}*" if tags else ""
    return (
        f"{arrow} *{alert.symbol}* {alert.change_pct:+.2f}% "
        f"(crossed ±{alert.threshold_pct:g}%)\n"
        f"LTP: `{alert.ltp:.2f}` | Prev close: `{alert.prev_close:.2f}`\n"
        f"Direction: {alert.direction}"
        f"{tag_line}\n"
        f"Time (IST): {format_ist_clock(alert.fired_at)}"
    )


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class ConsoleNotifier:
    def send(self, alert: Alert) -> None:
        tags = _alert_tags(alert)
        tag_note = f" {tags}" if tags else ""
        print(
            f"[ALERT] {alert.direction} {alert.symbol} "
            f"{alert.change_pct:+.2f}% (crossed ±{alert.threshold_pct:g}%) "
            f"LTP={alert.ltp:.2f} prev={alert.prev_close:.2f}{tag_note} "
            f"@ {alert.fired_at.isoformat()}",
            flush=True,
        )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id).strip().strip('"').strip("'")
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, alert: Alert) -> None:
        self.send_text(_format_telegram_alert(alert), parse_mode="Markdown")

    def send_text(self, text: str, *, parse_mode: str | None = None) -> None:
        import httpx

        # Telegram hard limit is 4096 characters.
        chunks = _chunk_text(text, 3500)
        for chunk in chunks:
            payload: dict[str, object] = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                resp = httpx.post(self._url, json=payload, timeout=10.0)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.error("Telegram send failed: %s", exc)
                print(chunk, flush=True)

    def send_document(
        self,
        path: str | Path,
        *,
        caption: str = "",
    ) -> None:
        """Upload a file (e.g. Excel screener report) to Telegram."""
        import httpx

        file_path = Path(path)
        if not file_path.exists():
            logger.error("Telegram document missing: %s", file_path)
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        data: dict[str, str] = {"chat_id": self.chat_id}
        if caption:
            # Caption limit is 1024 chars.
            data["caption"] = caption[:1000]
        try:
            with file_path.open("rb") as fh:
                resp = httpx.post(
                    url,
                    data=data,
                    files={"document": (file_path.name, fh)},
                    timeout=120.0,
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Telegram document send failed: %s", exc)


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and current:
            chunks.append("".join(current))
            current = [line]
            size = len(line)
        else:
            current.append(line)
            size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            notifier.send(alert)


def build_notifier(
    *,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
    always_console: bool = True,
) -> Notifier:
    notifiers: list[Notifier] = []
    if always_console:
        notifiers.append(ConsoleNotifier())
    if telegram_bot_token and telegram_chat_id:
        notifiers.append(TelegramNotifier(telegram_bot_token, telegram_chat_id))
    elif not always_console:
        notifiers.append(ConsoleNotifier())
    return MultiNotifier(notifiers) if len(notifiers) > 1 else notifiers[0]
