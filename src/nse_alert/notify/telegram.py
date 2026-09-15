from __future__ import annotations

import logging
from typing import Protocol

from nse_alert.engine import Alert

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class ConsoleNotifier:
    def send(self, alert: Alert) -> None:
        print(
            f"[ALERT] {alert.direction} {alert.symbol} "
            f"{alert.change_pct:+.2f}% (crossed ±{alert.threshold_pct:g}%) "
            f"LTP={alert.ltp:.2f} prev={alert.prev_close:.2f} "
            f"@ {alert.fired_at.isoformat()}",
            flush=True,
        )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, alert: Alert) -> None:
        arrow = "▲" if alert.direction == "UP" else "▼"
        text = (
            f"{arrow} *{alert.symbol}* {alert.change_pct:+.2f}% "
            f"(crossed ±{alert.threshold_pct:g}%)\n"
            f"LTP: `{alert.ltp:.2f}` | Prev close: `{alert.prev_close:.2f}`\n"
            f"Direction: {alert.direction}\n"
            f"Time (UTC): {alert.fired_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.send_text(text, parse_mode="Markdown")

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
