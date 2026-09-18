from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from nse_alert.orders import NSE_TICK, round_tick

logger = logging.getLogger(__name__)

_CANCEL_RE = re.compile(r"^\s*/trail_cancel(?:@[A-Za-z0-9_]+)?(?:\s+(\S+))?\s*$", re.I)
_STATUS_RE = re.compile(r"^\s*/trail_status(?:@[A-Za-z0-9_]+)?(?:\s+(\S+))?\s*$", re.I)

_TERMINAL_STOP_STATUSES = frozenset({"CANCELLED", "REJECTED"})


@dataclass
class TrailingStopState:
    high_water: float
    trigger: float
    limit_px: float
    order_id: str


class TrailingStopRunner:
    """Ratchet a SELL SL-Limit order upward as an intraday position's LTP makes new highs.

    For a position you already hold (bought yourself on Kite, or via this
    app) that you want an automated trailing stop on — Kite has no native
    trailing-SL for intraday products, so this polls the LTP and calls
    ``modify_order`` on the stop whenever the trail distance would tighten.
    One process manages one symbol; run multiple ``nse-alert trail``
    invocations for multiple symbols.
    """

    def __init__(
        self,
        *,
        kite_api_key: str,
        kite_access_token: str,
        symbol: str,
        quantity: int,
        trail_pct: float,
        stop_limit_ticks: int = 2,
        product: str = "MIS",
        poll_sec: float = 5.0,
        mode: str = "dry_run",  # dry_run | live
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        on_message: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = kite_api_key
        self.access_token = kite_access_token
        self.symbol = symbol.upper()
        self.quantity = max(1, int(quantity))
        self.trail_pct = abs(float(trail_pct))
        self.stop_limit_ticks = max(1, int(stop_limit_ticks))
        self.product = product.upper()
        self.poll_sec = max(1.0, float(poll_sec))
        self.mode = mode
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self._notify = on_message or (lambda _text: None)
        self._stop = threading.Event()
        self._offset = 0
        self._webhook_cleared = False
        self.state: TrailingStopState | None = None

    def _kite(self) -> Any:
        from kiteconnect import KiteConnect

        if not self.api_key or not self.access_token:
            raise RuntimeError("KITE_API_KEY and KITE_ACCESS_TOKEN required for live trailing")
        kite = KiteConnect(api_key=self.api_key)
        kite.set_access_token(self.access_token)
        return kite

    def _ltp(self, kite: Any) -> float:
        payload = kite.quote([f"NSE:{self.symbol}"])
        row = payload.get(f"NSE:{self.symbol}") or {}
        return float(row.get("last_price") or 0.0)

    def _clamp_trigger(self, trigger: float, ltp: float) -> float:
        # Kite rejects a SELL SL whose trigger sits at/above the current LTP.
        if ltp > 0 and trigger >= ltp:
            trigger = round_tick(ltp - NSE_TICK)
        return max(NSE_TICK, trigger)

    def _trigger_and_limit_for(self, ltp: float) -> tuple[float, float]:
        trigger = self._clamp_trigger(round_tick(ltp * (1 - self.trail_pct / 100.0)), ltp)
        limit_px = max(NSE_TICK, round_tick(trigger - self.stop_limit_ticks * NSE_TICK))
        return trigger, limit_px

    def _place_initial_stop(self, kite: Any | None, ltp: float) -> TrailingStopState:
        trigger, limit_px = self._trigger_and_limit_for(ltp)
        if self.mode == "dry_run" or kite is None:
            order_id = f"DRY-TRAIL-{self.symbol}"
            logger.info(
                "DRY RUN: would place SL-Limit SELL %d %s trigger=%.2f limit=%.2f",
                self.quantity,
                self.symbol,
                trigger,
                limit_px,
            )
        else:
            order_id = str(
                kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NSE,
                    tradingsymbol=self.symbol,
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=self.quantity,
                    product=kite.PRODUCT_MIS if self.product != "CNC" else kite.PRODUCT_CNC,
                    order_type=kite.ORDER_TYPE_SL,
                    price=limit_px,
                    trigger_price=trigger,
                    tag="nsetrail",
                )
            )
        return TrailingStopState(high_water=ltp, trigger=trigger, limit_px=limit_px, order_id=order_id)

    def _modify_stop(self, kite: Any | None, trigger: float, limit_px: float) -> bool:
        assert self.state is not None
        if self.mode == "dry_run" or kite is None:
            logger.info(
                "DRY RUN: would trail %s stop -> trigger=%.2f limit=%.2f",
                self.symbol,
                trigger,
                limit_px,
            )
            return True
        try:
            kite.modify_order(
                variety=kite.VARIETY_REGULAR,
                order_id=self.state.order_id,
                order_type=kite.ORDER_TYPE_SL,
                quantity=self.quantity,
                price=limit_px,
                trigger_price=trigger,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Trail modify failed for %s: %s", self.symbol, exc)
            self._notify(f"Trail modify failed for {self.symbol}: {exc}")
            return False

    def _order_status(self, kite: Any) -> str:
        assert self.state is not None
        try:
            history = kite.order_history(self.state.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("order_history failed for %s: %s", self.symbol, exc)
            return ""
        if not history:
            return ""
        return str(history[-1].get("status") or "")

    def start_telegram_listener(self) -> None:
        if not (self.telegram_bot_token and self.telegram_chat_id):
            return
        threading.Thread(target=self._poll_telegram, daemon=True, name="trail-telegram").start()

    def _clear_webhook_once(self) -> None:
        if self._webhook_cleared:
            return
        try:
            httpx.post(
                f"https://api.telegram.org/bot{self.telegram_bot_token}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=10.0,
            ).raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Could not clear Telegram webhook: %s", exc)
        else:
            self._webhook_cleared = True

    def _poll_telegram(self) -> None:
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/getUpdates"
        while not self._stop.is_set():
            self._clear_webhook_once()
            try:
                resp = httpx.get(url, params={"timeout": 20, "offset": self._offset}, timeout=30.0)
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("Telegram poll failed: %s", exc)
                time.sleep(2.0)
                continue
            for update in payload.get("result", []):
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or update.get("channel_post") or {}
                text = str(message.get("text") or "").strip()
                if not text:
                    continue
                m = _CANCEL_RE.match(text)
                if m and (not m.group(1) or m.group(1).upper() == self.symbol):
                    logger.info("Telegram cancel received for %s", self.symbol)
                    self.cancel(reason="cancelled via Telegram")
                    continue
                m = _STATUS_RE.match(text)
                if m and (not m.group(1) or m.group(1).upper() == self.symbol) and self.state:
                    self._notify(
                        f"{self.symbol}: high={self.state.high_water:.2f} "
                        f"stop trigger={self.state.trigger:.2f} limit={self.state.limit_px:.2f} "
                        f"qty={self.quantity} mode={self.mode}"
                    )

    def cancel(self, *, reason: str = "cancelled") -> None:
        already_stopped = self._stop.is_set()
        self._stop.set()
        if already_stopped:
            return
        if self.state and self.mode != "dry_run":
            try:
                kite = self._kite()
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=self.state.order_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Could not cancel stop order %s: %s", self.state.order_id, exc)
        self._notify(f"Trailing stop on {self.symbol} {reason}")

    def run(self) -> None:
        kite = None if self.mode == "dry_run" else self._kite()
        ltp = self._ltp(kite) if kite else 0.0
        if ltp <= 0:
            raise RuntimeError(f"Could not fetch LTP for {self.symbol}")
        self.state = self._place_initial_stop(kite, ltp)
        self._notify(
            f"Trailing SL armed: SELL {self.quantity} {self.symbol} "
            f"trail={self.trail_pct:g}% initial trigger={self.state.trigger:.2f} "
            f"limit={self.state.limit_px:.2f} (order_id={self.state.order_id}, mode={self.mode})\n"
            f"Reply /trail_cancel {self.symbol} to cancel, /trail_status {self.symbol} to check."
        )
        self.start_telegram_listener()
        try:
            while not self._stop.is_set():
                time.sleep(self.poll_sec)
                if self._stop.is_set():
                    break
                try:
                    ltp = self._ltp(kite) if kite else self.state.high_water
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Quote failed for %s: %s", self.symbol, exc)
                    continue
                if kite:
                    status = self._order_status(kite)
                    if status == "COMPLETE":
                        self._notify(
                            f"{self.symbol}: trailing stop hit, SELL filled near {self.state.trigger:.2f}"
                        )
                        self._stop.set()
                        break
                    if status in _TERMINAL_STOP_STATUSES:
                        self._notify(f"{self.symbol}: stop order ended as {status} — trailing stopped")
                        self._stop.set()
                        break
                if ltp > self.state.high_water:
                    self.state.high_water = ltp
                    new_trigger, new_limit = self._trigger_and_limit_for(ltp)
                    if new_trigger > self.state.trigger and self._modify_stop(kite, new_trigger, new_limit):
                        self.state.trigger = new_trigger
                        self.state.limit_px = new_limit
                        self._notify(
                            f"{self.symbol} new high {ltp:.2f} -> SL trailed to "
                            f"trigger={new_trigger:.2f} limit={new_limit:.2f}"
                        )
        finally:
            self._stop.set()
