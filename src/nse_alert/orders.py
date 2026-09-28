from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

NSE_TICK = 0.05
_TERMINAL_ORDER_STATUSES = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})


class SizingRefusedError(RuntimeError):
    """Raised instead of silently falling back to approximate sizing.

    A bad/expired Kite token makes ``order_margins`` fail the same way a
    network blip does, but the two must not be treated the same: sizing off
    an approximate leverage guess while auth is broken produced a real
    mis-sized order on 2026-09-17 (see docs/RCA-2026-09-17.md). Callers
    should alert and skip the trade rather than place it anyway.
    """


def parse_order_margins_payload(detail: Any) -> dict[str, Any]:
    """Unwrap Kite ``order_margins`` into a single order row.

    The Python client normally returns a list of rows. Some responses (or
    wrappers) are a dict with ``orders`` / ``data``. A missing ``total``
    must not be treated as 0 silently — that would size every name to 1 share.
    """
    if isinstance(detail, list):
        if not detail:
            raise RuntimeError("order_margins returned an empty list")
        row = detail[0]
    elif isinstance(detail, dict):
        orders = detail.get("orders")
        if isinstance(orders, list) and orders:
            row = orders[0]
        elif "data" in detail and detail.get("data") is not detail:
            return parse_order_margins_payload(detail["data"])
        else:
            row = detail
    else:
        raise RuntimeError(f"Unexpected order_margins response: {detail!r}")
    if not isinstance(row, dict):
        raise RuntimeError(f"Unexpected order_margins row: {row!r}")
    if row.get("error_type") or str(row.get("status") or "").lower() == "error":
        raise RuntimeError(str(row.get("message") or row))
    return row

TradeMode = Literal["off", "dry_run", "confirm", "auto"]
TransactionSide = Literal["BUY", "SELL"]


def round_tick(price: float, tick: float = 0.05) -> float:
    """Round to NSE-style tick (default ₹0.05)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: TransactionSide
    quantity: int
    product: str  # CNC / MIS
    order_type: str  # MARKET / LIMIT / SL-M
    price: float | None
    trigger_price: float | None
    market_protection: int
    tag: str
    reason: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    ok: bool
    mode: str
    request: OrderRequest
    order_id: str | None
    message: str
    placed_at: datetime


@dataclass
class PendingOrder:
    id: str
    created_at: str
    expires_at: str
    request: dict[str, Any]
    status: str  # pending | confirmed | expired | cancelled
    alert_symbol: str
    alert_threshold: float
    alert_direction: str
    entry_ltp: float = 0.0
    # "threshold" (±% alert) or "signal" (volume spike / 52w offer). Signal
    # offers are optional by design, so their expiry is not alerted on.
    source: str = "threshold"


def format_expiry_message(item: PendingOrder) -> str:
    """One-line Telegram/log message for a pending order that just expired."""
    created = datetime.fromisoformat(item.created_at)
    expires = datetime.fromisoformat(item.expires_at)
    ttl_min = round((expires - created).total_seconds() / 60)
    side = item.request.get("side", "?")
    qty = item.request.get("quantity", "?")
    return (
        f"⏱ Order EXPIRED unconfirmed: {item.alert_symbol} {side} {qty} "
        f"@ ~₹{item.entry_ltp:.2f} (id {item.id}, {ttl_min} min TTL)"
    )


class OrderBook:
    """Persist pending confirmations, daily order count, and open SL tracks."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _today(self) -> str:
        return date.today().isoformat()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "date": self._today(),
                "placed_count": 0,
                "pending": {},
                "history": [],
                "positions": {},
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "date": self._today(),
                "placed_count": 0,
                "pending": {},
                "history": [],
                "positions": {},
            }
        if data.get("date") != self._today():
            return {
                "date": self._today(),
                "placed_count": 0,
                "pending": {},
                "history": [],
                "positions": {},
            }
        data.setdefault("pending", {})
        data.setdefault("history", [])
        data.setdefault("placed_count", 0)
        data.setdefault("positions", {})
        return data

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    @property
    def placed_count(self) -> int:
        return int(self._data.get("placed_count", 0))

    def has_open_position(self, symbol: str) -> bool:
        pos = self._data.get("positions", {}).get(symbol.upper())
        return bool(pos and pos.get("status") == "open")

    def record_placed(self, result: OrderResult, *, toward_daily_cap: bool = True) -> None:
        if toward_daily_cap:
            self._data["placed_count"] = self.placed_count + 1
        self._data["history"].append(
            {
                "order_id": result.order_id,
                "mode": result.mode,
                "symbol": result.request.symbol,
                "side": result.request.side,
                "quantity": result.request.quantity,
                "message": result.message,
                "placed_at": result.placed_at.isoformat(),
                "reason": result.request.reason,
                "toward_daily_cap": toward_daily_cap,
            }
        )
        self._save()

    def record_position(
        self,
        *,
        symbol: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        stop_limit_price: float,
        entry_order_id: str | None,
        stop_order_id: str | None,
        mode: str,
        product: str = "MIS",
        circuit_limit: float | None = None,
        side: str = "BUY",
    ) -> None:
        self._data["positions"][symbol.upper()] = {
            "status": "open",
            "side": side.upper(),
            "quantity": quantity,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "stop_limit_price": stop_limit_price,
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "mode": mode,
            "product": product.upper(),
            "breakeven_armed": False,
            # Actual per-symbol upper-circuit price from Kite quote() at entry,
            # when available; 0.0 means unknown (percent fallback applies).
            "circuit_limit": float(circuit_limit) if circuit_limit else 0.0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def close_position(self, symbol: str, *, reason: str = "closed") -> None:
        pos = self._data.get("positions", {}).get(symbol.upper())
        if not pos:
            return
        pos["status"] = "closed"
        pos["closed_reason"] = reason
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        raw = self._data.get("positions", {}).get(symbol.upper())
        if not raw or raw.get("status") != "open":
            return None
        return raw

    def list_open_positions(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for sym, raw in self._data.get("positions", {}).items():
            if raw.get("status") == "open":
                out.append((sym, raw))
        return out

    def update_stop(
        self,
        symbol: str,
        *,
        stop_price: float,
        stop_limit_price: float,
        breakeven_armed: bool | None = None,
    ) -> None:
        key = symbol.upper()
        pos = self._data.get("positions", {}).get(key)
        if not pos:
            return
        pos["stop_price"] = stop_price
        pos["stop_limit_price"] = stop_limit_price
        if breakeven_armed is not None:
            pos["breakeven_armed"] = breakeven_armed
        self._save()

    def add_pending(
        self,
        request: OrderRequest,
        *,
        alert_symbol: str,
        alert_threshold: float,
        alert_direction: str,
        entry_ltp: float,
        ttl_minutes: int = 30,
        source: str = "threshold",
    ) -> PendingOrder:
        now = datetime.now(timezone.utc)
        pending_id = secrets.token_hex(3).upper()
        expires = now.timestamp() + ttl_minutes * 60
        item = PendingOrder(
            id=pending_id,
            created_at=now.isoformat(),
            expires_at=datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
            request=asdict(request),
            status="pending",
            alert_symbol=alert_symbol,
            alert_threshold=alert_threshold,
            alert_direction=alert_direction,
            entry_ltp=float(entry_ltp),
            source=source,
        )
        self._data["pending"][pending_id] = asdict(item)
        self._save()
        return item

    def get_pending(self, pending_id: str) -> PendingOrder | None:
        raw = self._data.get("pending", {}).get(pending_id.upper())
        if not raw:
            return None
        return PendingOrder(**raw)

    def list_pending(self) -> list[PendingOrder]:
        out: list[PendingOrder] = []
        now = datetime.now(timezone.utc)
        for raw in list(self._data.get("pending", {}).values()):
            item = PendingOrder(**raw)
            exp = datetime.fromisoformat(item.expires_at)
            if item.status == "pending" and exp < now:
                item.status = "expired"
                self._data["pending"][item.id]["status"] = "expired"
            if item.status == "pending":
                out.append(item)
        self._save()
        return out

    def sweep_expired(self) -> list[PendingOrder]:
        """Expire timed-out pending orders; return only those that just did.

        `list_pending` also lazily expires as a side effect of listing, but a
        caller polling it repeatedly would re-see the same already-expired
        items every time — not useful for "notify once per expiry". This is
        meant to be polled periodically (e.g. from the watch loop) so an
        operator can be alerted exactly once when TRADE_CONFIRM_TTL_MINUTES
        elapses on an unconfirmed order, instead of finding out at day's end
        (see docs/RCA-2026-09-17.md, Incident A).
        """
        now = datetime.now(timezone.utc)
        newly: list[PendingOrder] = []
        for raw in list(self._data.get("pending", {}).values()):
            item = PendingOrder(**raw)
            if item.status != "pending":
                continue
            if datetime.fromisoformat(item.expires_at) < now:
                item.status = "expired"
                self._data["pending"][item.id]["status"] = "expired"
                newly.append(item)
        if newly:
            self._save()
        return newly

    def counts_by_status(self) -> dict[str, int]:
        """Today's pending-order counts by status, after sweeping expiries.

        The book resets daily (see `_load`), so these are always "today"'s
        counts without any date filtering needed here.
        """
        self.sweep_expired()
        counts: dict[str, int] = {}
        for raw in self._data.get("pending", {}).values():
            status = raw.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def mark_pending(self, pending_id: str, status: str) -> PendingOrder | None:
        key = pending_id.upper()
        raw = self._data.get("pending", {}).get(key)
        if not raw:
            return None
        raw["status"] = status
        self._data["pending"][key] = raw
        self._save()
        return PendingOrder(**raw)


class OrderExecutor:
    """Place MIS intraday BUY on +threshold, with a %-stop SL-Limit from fill price."""

    def __init__(
        self,
        *,
        api_key: str = "",
        access_token: str = "",
        mode: TradeMode = "off",
        default_qty: int = 1,
        product: str = "MIS",
        order_type: str = "MARKET",
        market_protection: int = 2,
        max_orders_per_day: int = 10,
        trade_on_thresholds: list[float] | None = None,
        trade_sides: str = "up",
        stop_loss_pct: float = 2.0,
        stop_limit_ticks: int = 2,
        stop_wait_sec: float = 20.0,
        trail_breakeven: bool = True,
        trail_breakeven_pct: float = 2.0,
        exit_on_upper_circuit: bool = True,
        upper_circuit_pct: float = 20.0,
        sizing_mode: str = "margin",  # margin | fixed
        margin_budget_inr: float = 10_000.0,
        fallback_leverage: float = 5.0,
        book: OrderBook | None = None,
    ) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.mode: TradeMode = mode  # type: ignore[assignment]
        self.default_qty = max(1, int(default_qty))
        self.product = product.upper()
        self.order_type = order_type.upper()
        self.market_protection = int(market_protection)
        self.max_orders_per_day = max(0, int(max_orders_per_day))
        # Test default: only +13%
        self.trade_on_thresholds = sorted(trade_on_thresholds or [13.0])
        self.trade_sides = trade_sides.strip().lower()
        self.stop_loss_pct = abs(float(stop_loss_pct))
        # SELL SL-Limit: limit price = trigger − N ticks (NSE tick ₹0.05)
        self.stop_limit_ticks = max(1, int(stop_limit_ticks))
        self.stop_wait_sec = max(1.0, float(stop_wait_sec))
        self.trail_breakeven = trail_breakeven
        self.trail_breakeven_pct = abs(float(trail_breakeven_pct))
        self.exit_on_upper_circuit = exit_on_upper_circuit
        self.upper_circuit_pct = abs(float(upper_circuit_pct))
        mode_raw = (sizing_mode or "margin").strip().lower()
        self.sizing_mode = mode_raw if mode_raw in {"margin", "fixed"} else "margin"
        self.margin_budget_inr = max(0.0, float(margin_budget_inr))
        self.fallback_leverage = max(1.0, float(fallback_leverage))
        self.book = book

    def enabled(self) -> bool:
        return self.mode in {"dry_run", "confirm", "auto"}

    def should_trade_alert(self, *, direction: str, threshold_pct: float) -> bool:
        if not self.enabled():
            return False
        if self.trade_sides == "up" and direction != "UP":
            return False
        if self.trade_sides == "down" and direction != "DOWN":
            return False
        if self.trade_on_thresholds:
            wanted = {f"{t:g}" for t in self.trade_on_thresholds}
            if f"{threshold_pct:g}" not in wanted:
                return False
        return True

    def side_for_direction(self, direction: str) -> TransactionSide:
        return "BUY" if direction == "UP" else "SELL"

    def _margin_for_quantity(
        self,
        *,
        symbol: str,
        side: TransactionSide,
        quantity: int,
        price: float | None = None,
    ) -> tuple[float, float, dict[str, Any]]:
        """Return (required_margin_inr, leverage, raw_row) for one MIS order via Kite."""
        kite = self._kite()
        params: dict[str, Any] = {
            "exchange": kite.EXCHANGE_NSE,
            "tradingsymbol": symbol.upper(),
            "transaction_type": (
                kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL
            ),
            "variety": kite.VARIETY_REGULAR,
            "product": kite.PRODUCT_MIS if self.product != "CNC" else kite.PRODUCT_CNC,
            "order_type": kite.ORDER_TYPE_MARKET,
            "quantity": max(1, int(quantity)),
        }
        if price is not None and price > 0:
            # Helps margin calc for MARKET outside hours in some cases
            params["price"] = float(price)
        detail = kite.order_margins([params])
        row = parse_order_margins_payload(detail)
        total = float(row.get("total") or 0.0)
        leverage = float(row.get("leverage") or 0.0)
        return total, leverage, row

    def size_quantity(
        self,
        *,
        symbol: str,
        price: float,
        side: TransactionSide = "BUY",
        quantity: int | None = None,
    ) -> tuple[int, str]:
        """Pick share count from fixed qty or ~TRADE_MARGIN_INR of real margin.

        ``margin`` mode: query Kite MIS margin for 1 share, then
        ``qty = floor(budget / margin_per_share)`` so deployed capital ≈ budget
        at that stock's leverage. Falls back to ``price / fallback_leverage``
        when margins API are unavailable (e.g. dry_run without token).

        ``TRADE_QTY`` (default 1) is **ignored** in margin mode — commenting it
        out does not change alert/auto size. Pass ``quantity=`` only for an
        explicit override (CLI ``--qty``).
        """
        if quantity is not None:
            qty = max(1, int(quantity))
            return qty, f"explicit qty={qty}"

        if self.sizing_mode == "fixed" or self.margin_budget_inr <= 0:
            return self.default_qty, f"fixed TRADE_QTY={self.default_qty}"

        budget = self.margin_budget_inr
        px = max(float(price), NSE_TICK)

        try:
            margin_1, leverage, raw = self._margin_for_quantity(
                symbol=symbol, side=side, quantity=1, price=px
            )
            if margin_1 <= 0:
                raise RuntimeError("order_margins returned non-positive total")
            qty = max(1, int(budget // margin_1))
            notional = qty * px
            eff_lev = (notional / (qty * margin_1)) if margin_1 > 0 else leverage
            if leverage <= 0:
                leverage = eff_lev
            one_share_note = ""
            if qty == 1 and margin_1 > budget / 2:
                one_share_note = (
                    f"; qty=1 because Kite margin/share ₹{margin_1:.0f} "
                    f"uses most of budget ₹{budget:.0f} (not TRADE_QTY)"
                )
            note = (
                f"margin≈₹{budget:.0f} → {qty} shares @≈{px:.2f} "
                f"(~₹{margin_1:.0f}/sh, lev≈{leverage:.2f}x, notional≈₹{notional:.0f})"
                f"{one_share_note}"
            )
            logger.info(
                "order_margins %s total=%s leverage=%s var=%s extra=%s",
                symbol,
                raw.get("total"),
                raw.get("leverage"),
                raw.get("var"),
                {k: raw.get(k) for k in ("span", "exposure", "additional") if k in raw},
            )
            return qty, note
        except Exception as exc:  # noqa: BLE001
            from kiteconnect.exceptions import TokenException

            if isinstance(exc, TokenException):
                raise SizingRefusedError(
                    f"Kite token invalid/expired ({exc}) — refusing to size off an "
                    "approximate fallback while auth is broken. Run the daily "
                    "login, then retry."
                ) from exc
            # Approximate: budget * leverage / price
            qty = max(1, int((budget * self.fallback_leverage) // px))
            notional = qty * px
            note = (
                f"margin-size fallback ({exc!s}): "
                f"lev={self.fallback_leverage:g}x → {qty} shares "
                f"(notional≈₹{notional:.0f} for budget ₹{budget:.0f})"
            )
            logger.warning("%s", note)
            return qty, note

    def stop_price_from_entry(self, entry: float) -> float:
        """Trigger price for the downside stop (entry × (1 − stop_loss_pct/100))."""
        raw = entry * (1.0 - self.stop_loss_pct / 100.0)
        return round_tick(raw)

    def stop_limit_price_from_trigger(self, trigger: float) -> float:
        """SELL SL-Limit price: a few ticks below trigger (must stay positive)."""
        raw = trigger - (self.stop_limit_ticks * NSE_TICK)
        return max(NSE_TICK, round_tick(raw))

    def _clamp_sell_sl_trigger(self, trigger: float, reference_ltp: float) -> float:
        """Kite rejects sell SL when trigger is at/above the reference LTP."""
        if reference_ltp <= 0:
            return trigger
        if trigger >= reference_ltp:
            trigger = round_tick(reference_ltp - NSE_TICK)
        return max(NSE_TICK, trigger)

    def build_stop_request(
        self,
        *,
        symbol: str,
        quantity: int,
        product: str,
        entry_price: float,
        reference_ltp: float | None = None,
        market_protection: int = 2,
        reason_prefix: str = "",
    ) -> OrderRequest:
        """Build a SELL SL (stop-loss limit) under the entry fill price."""
        ref = reference_ltp if reference_ltp is not None else entry_price
        trigger = self._clamp_sell_sl_trigger(
            self.stop_price_from_entry(entry_price), ref
        )
        limit_px = self.stop_limit_price_from_trigger(trigger)
        prefix = f"{reason_prefix} " if reason_prefix else ""
        return OrderRequest(
            symbol=symbol,
            side="SELL",
            quantity=max(1, int(quantity)),
            product=product,
            order_type="SL",
            price=limit_px,
            trigger_price=trigger,
            market_protection=market_protection,
            tag="nseasl",
            reason=(
                f"{prefix}SL-Limit {self.stop_loss_pct:g}% under entry {entry_price:.2f} "
                f"(trigger={trigger:.2f}, limit={limit_px:.2f})"
            ).strip(),
        )

    def build_short_stop_request(
        self,
        *,
        symbol: str,
        quantity: int,
        product: str,
        entry_price: float,
        reference_ltp: float | None = None,
        market_protection: int = 2,
    ) -> OrderRequest:
        """BUY SL-Limit above a SELL (short) entry — mirror of ``build_stop_request``."""
        ref = reference_ltp if reference_ltp is not None else entry_price
        trigger = round_tick(entry_price * (1.0 + self.stop_loss_pct / 100.0))
        # Kite rejects a buy SL whose trigger is at/below the reference LTP.
        if ref > 0 and trigger <= ref:
            trigger = round_tick(ref + NSE_TICK)
        limit_px = round_tick(trigger + self.stop_limit_ticks * NSE_TICK)
        return OrderRequest(
            symbol=symbol,
            side="BUY",
            quantity=max(1, int(quantity)),
            product=product,
            order_type="SL",
            price=limit_px,
            trigger_price=trigger,
            market_protection=market_protection,
            tag="nseasl",
            reason=(
                f"SL-Limit {self.stop_loss_pct:g}% above short entry {entry_price:.2f} "
                f"(trigger={trigger:.2f}, limit={limit_px:.2f})"
            ),
        )

    def _wait_for_entry_fill(
        self,
        order_id: str,
        *,
        fallback_price: float,
        fallback_qty: int,
    ) -> tuple[float, int]:
        """Poll Kite until the entry MARKET order completes (or timeout)."""
        try:
            kite = self._kite()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not poll entry fill (%s); using alert LTP", exc)
            return fallback_price, fallback_qty

        deadline = time.monotonic() + self.stop_wait_sec
        while time.monotonic() < deadline:
            try:
                history = kite.order_history(order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("order_history failed for %s: %s", order_id, exc)
                time.sleep(0.5)
                continue
            if history:
                latest = history[-1]
                status = str(latest.get("status") or "")
                if status == "COMPLETE":
                    avg = float(latest.get("average_price") or fallback_price)
                    qty = int(latest.get("filled_quantity") or latest.get("quantity") or fallback_qty)
                    logger.info(
                        "Entry %s COMPLETE avg=%.2f qty=%d",
                        order_id,
                        avg,
                        qty,
                    )
                    return avg, max(1, qty)
                if status in _TERMINAL_ORDER_STATUSES - {"COMPLETE"}:
                    logger.error("Entry order %s ended as %s", order_id, status)
                    return fallback_price, fallback_qty
            time.sleep(0.4)

        logger.warning(
            "Entry order %s not COMPLETE within %.0fs; SL uses fallback price %.2f",
            order_id,
            self.stop_wait_sec,
            fallback_price,
        )
        return fallback_price, fallback_qty

    def _modify_stop_order(
        self,
        *,
        stop_order_id: str,
        quantity: int,
        trigger: float,
        limit_px: float,
        force_mode: str | None = None,
    ) -> bool:
        mode = force_mode or self.mode
        if mode == "dry_run" or str(stop_order_id).startswith("DRY-"):
            logger.info(
                "DRY RUN: would modify stop %s → trigger=%.2f limit=%.2f",
                stop_order_id,
                trigger,
                limit_px,
            )
            return True
        try:
            kite = self._kite()
            kite.modify_order(
                variety=kite.VARIETY_REGULAR,
                order_id=stop_order_id,
                quantity=quantity,
                order_type=kite.ORDER_TYPE_SL,
                price=limit_px,
                trigger_price=trigger,
            )
            logger.info(
                "Modified stop %s → trigger=%.2f limit=%.2f",
                stop_order_id,
                trigger,
                limit_px,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Stop modify failed for %s: %s", stop_order_id, exc)
            return False

    def manage_open_stops(self, symbol: str, ltp: float) -> str | None:
        """Trail stop to entry (cost-to-cost) when LTP rises enough after entry."""
        if not self.book or not self.trail_breakeven:
            return None
        pos = self.book.get_position(symbol)
        if not pos or pos.get("breakeven_armed") or pos.get("side") == "SELL":
            return None
        stop_id = pos.get("stop_order_id")
        if not stop_id:
            return None
        entry = float(pos.get("entry_price") or 0.0)
        if entry <= 0 or ltp <= 0:
            return None
        if ltp < entry * (1.0 + self.trail_breakeven_pct / 100.0):
            return None

        trigger = self._clamp_sell_sl_trigger(round_tick(entry), ltp)
        limit_px = self.stop_limit_price_from_trigger(trigger)
        qty = int(pos.get("quantity") or 1)
        if self._modify_stop_order(
            stop_order_id=str(stop_id),
            quantity=qty,
            trigger=trigger,
            limit_px=limit_px,
        ):
            self.book.update_stop(
                symbol,
                stop_price=trigger,
                stop_limit_price=limit_px,
                breakeven_armed=True,
            )
            msg = (
                f"Trailed SL on {symbol} to cost: trigger={trigger:.2f} "
                f"limit={limit_px:.2f} (entry={entry:.2f}, LTP={ltp:.2f})"
            )
            logger.info(msg)
            return msg
        return None

    def fetch_upper_circuit_limit(self, symbol: str) -> float | None:
        """Look up today's actual upper-circuit price for a symbol via Kite.

        Circuit bands vary per stock (2/5/10/20%, tighter for some ASM/GSM
        names) and aren't derivable from the WebSocket LTP ticks — only the
        REST ``quote()`` endpoint returns ``upper_circuit_limit``. Called
        once per position (at entry), not per tick: the band is fixed for
        the day in the vast majority of cases, and polling it per tick would
        burn the quote rate limit for no benefit.

        Returns ``None`` (rather than raising) when credentials aren't
        configured, the call fails, or Kite doesn't return the field —
        callers fall back to the flat ``upper_circuit_pct`` band.
        """
        if not self.api_key or not self.access_token:
            return None
        try:
            kite = self._kite()
            payload = kite.quote([f"NSE:{symbol.upper()}"])
            row = payload.get(f"NSE:{symbol.upper()}") or {}
            limit = float(row.get("upper_circuit_limit") or 0.0)
            return limit if limit > 0 else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not fetch upper circuit limit for %s (falling back to "
                "%.4g%% band): %s",
                symbol,
                self.upper_circuit_pct,
                exc,
            )
            return None

    def manage_upper_circuit_exit(
        self, symbol: str, ltp: float, prev_close: float
    ) -> str | None:
        """Exit an open position at market once LTP hits the upper-circuit band.

        Uses the position's actual per-symbol circuit price (fetched from
        Kite at entry, see ``fetch_upper_circuit_limit``) when known. Falls
        back to ``upper_circuit_pct`` of prev close otherwise — e.g. no Kite
        credentials, or the quote lookup failed at entry time.

        Most NSE mid/small caps circuit at +20% of prev close — well above
        the 11/13% entry thresholds this bot buys on — but plenty circuit
        tighter (5/10%), where the flat 20% fallback would never fire. Once a
        stock locks upper there are no sellers left, so a resting SL far
        below entry will not fill; this fires a market SELL as the band is
        crossed instead, while there is still buying interest to exit into.
        """
        if not self.book or not self.exit_on_upper_circuit:
            return None
        pos = self.book.get_position(symbol)
        if not pos or pos.get("side") == "SELL":
            return None
        if ltp <= 0:
            return None

        # Float tolerance: e.g. 120/100 - 1 lands at 19.999999999999996, not 20.0.
        circuit_limit = float(pos.get("circuit_limit") or 0.0)
        if circuit_limit > 0:
            if ltp < circuit_limit - 1e-6:
                return None
            band_note = f"real circuit ₹{circuit_limit:.2f}"
            change_pct = (ltp / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
        else:
            if prev_close <= 0:
                return None
            change_pct = (ltp / prev_close - 1.0) * 100.0
            if change_pct < self.upper_circuit_pct - 1e-6:
                return None
            band_note = f"{self.upper_circuit_pct:g}% fallback band"

        qty = max(1, int(pos.get("quantity") or 1))
        stop_id = pos.get("stop_order_id")
        if stop_id and self.mode != "dry_run" and not str(stop_id).startswith("DRY-"):
            try:
                kite = self._kite()
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=str(stop_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not cancel stop %s before circuit exit on %s: %s",
                    stop_id,
                    symbol,
                    exc,
                )

        exit_req = OrderRequest(
            symbol=symbol,
            side="SELL",
            quantity=qty,
            product=str(pos.get("product") or self.product),
            order_type="MARKET",
            price=None,
            trigger_price=None,
            market_protection=self.market_protection,
            tag="nsecirc",
            reason=(
                f"upper circuit exit: LTP {change_pct:+.2f}% vs prev close ({band_note})"
            ),
        )
        result = self.place(exit_req, toward_daily_cap=False)
        self.book.close_position(symbol, reason="upper_circuit_exit")
        msg = (
            f"🔺 {symbol} hit upper circuit ({change_pct:+.2f}%, {band_note}) — "
            f"exiting {qty} at market ({result.message})"
        )
        logger.info(msg)
        return msg

    def build_request_from_alert(
        self,
        *,
        symbol: str,
        direction: str,
        threshold_pct: float,
        change_pct: float,
        entry_ltp: float = 0.0,
        quantity: int | None = None,
    ) -> OrderRequest:
        side = self.side_for_direction(direction)
        px = float(entry_ltp) if entry_ltp and entry_ltp > 0 else 0.0
        qty, size_note = self.size_quantity(
            symbol=symbol, price=px or 1.0, side=side, quantity=quantity
        )
        logger.info("Sized %s %s → %s", side, symbol, size_note)
        return OrderRequest(
            symbol=symbol,
            side=side,
            quantity=max(1, int(qty)),
            product=self.product,
            order_type=self.order_type,
            price=None,
            trigger_price=None,
            market_protection=self.market_protection,
            tag="nsealrt",
            reason=(
                f"alert {direction} ±{threshold_pct:g}% (now {change_pct:+.2f}%); "
                f"SL {self.stop_loss_pct:g}% below entry; {size_note}"
            ),
        )

    def _kite(self) -> Any:
        from kiteconnect import KiteConnect

        if not self.api_key or not self.access_token:
            raise RuntimeError("KITE_API_KEY and KITE_ACCESS_TOKEN required to place orders")
        kite = KiteConnect(api_key=self.api_key)
        kite.set_access_token(self.access_token)
        return kite

    def place(
        self,
        request: OrderRequest,
        *,
        force_mode: str | None = None,
        toward_daily_cap: bool = True,
    ) -> OrderResult:
        mode = force_mode or self.mode
        now = datetime.now(timezone.utc)
        if mode == "off":
            return OrderResult(False, mode, request, None, "TRADE_MODE=off", now)

        if (
            toward_daily_cap
            and self.book
            and self.book.placed_count >= self.max_orders_per_day
        ):
            return OrderResult(
                False,
                mode,
                request,
                None,
                f"Daily order cap reached ({self.max_orders_per_day})",
                now,
            )

        if self.book and request.side == "BUY" and self.book.has_open_position(request.symbol):
            return OrderResult(
                False,
                mode,
                request,
                None,
                f"Open position already exists for {request.symbol}",
                now,
            )

        if mode == "dry_run":
            fake_id = f"DRY-{uuid.uuid4().hex[:8]}"
            detail = f"DRY RUN: would {request.side} {request.quantity} {request.symbol}"
            if request.order_type.upper() in {"SL", "SL-M", "SLM"}:
                detail += (
                    f" {request.order_type.upper()}"
                    f" trigger={request.trigger_price}"
                    f" limit={request.price}"
                )
            result = OrderResult(True, mode, request, fake_id, detail, now)
            if self.book:
                self.book.record_placed(result, toward_daily_cap=toward_daily_cap)
            logger.info("%s", result.message)
            return result

        try:
            kite = self._kite()
        except Exception as exc:  # noqa: BLE001
            return OrderResult(False, mode, request, None, f"Order failed: {exc}", now)

        product = kite.PRODUCT_CNC if request.product == "CNC" else kite.PRODUCT_MIS
        txn = (
            kite.TRANSACTION_TYPE_BUY if request.side == "BUY" else kite.TRANSACTION_TYPE_SELL
        )
        order_type = request.order_type.upper()
        if order_type in {"SL-M", "SLM"}:
            kite_order_type = kite.ORDER_TYPE_SLM
        elif order_type == "SL":
            kite_order_type = kite.ORDER_TYPE_SL
        elif order_type == "LIMIT":
            kite_order_type = kite.ORDER_TYPE_LIMIT
        else:
            kite_order_type = kite.ORDER_TYPE_MARKET

        params: dict[str, Any] = {
            "variety": kite.VARIETY_REGULAR,
            "exchange": kite.EXCHANGE_NSE,
            "tradingsymbol": request.symbol,
            "transaction_type": txn,
            "quantity": request.quantity,
            "product": product,
            "order_type": kite_order_type,
            "tag": request.tag[:20],
        }
        if kite_order_type == kite.ORDER_TYPE_MARKET:
            params["market_protection"] = request.market_protection
        if request.price is not None:
            params["price"] = request.price
        if request.trigger_price is not None:
            params["trigger_price"] = request.trigger_price

        if kite_order_type == kite.ORDER_TYPE_SL and (
            request.price is None or request.trigger_price is None
        ):
            return OrderResult(
                False,
                mode,
                request,
                None,
                "SL (stop-loss limit) requires both trigger_price and price",
                now,
            )

        try:
            order_id = str(kite.place_order(**params))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Order failed")
            return OrderResult(False, mode, request, None, f"Order failed: {exc}", now)

        result = OrderResult(
            True,
            mode,
            request,
            order_id,
            f"Placed {request.side} {request.quantity} {request.symbol} → order_id={order_id}",
            now,
        )
        if self.book:
            self.book.record_placed(result, toward_daily_cap=toward_daily_cap)
        logger.info("%s", result.message)
        return result

    def place_entry_with_stop(
        self,
        request: OrderRequest,
        *,
        entry_ltp: float,
        force_mode: str | None = None,
        protect_short: bool = False,
    ) -> tuple[OrderResult, OrderResult | None]:
        """BUY entry, wait for fill, then attach a SELL SL-Limit (MIS).

        Uses **fill price** (not alert LTP) for the stop. Stops do not
        consume the daily **entry** cap. Prefer ``TRADE_PRODUCT=MIS`` —
        CNC sell-SL right after a buy often fails (no same-day holdings).

        SELL entries get no stop unless ``protect_short`` is set (signal
        orders), in which case a BUY SL-Limit is placed above the fill.
        """
        mode = force_mode or self.mode
        if (
            protect_short
            and request.side == "SELL"
            and self.book
            and self.book.has_open_position(request.symbol)
        ):
            return (
                OrderResult(
                    False,
                    mode,
                    request,
                    None,
                    f"Open position already exists for {request.symbol}",
                    datetime.now(timezone.utc),
                ),
                None,
            )
        entry = self.place(request, force_mode=mode, toward_daily_cap=True)
        if not entry.ok:
            return entry, None

        if request.side != "BUY" and not protect_short:
            return entry, None

        if mode == "dry_run":
            fill_price = entry_ltp
            fill_qty = request.quantity
        elif entry.order_id:
            fill_price, fill_qty = self._wait_for_entry_fill(
                entry.order_id,
                fallback_price=entry_ltp,
                fallback_qty=request.quantity,
            )
        else:
            fill_price, fill_qty = entry_ltp, request.quantity

        stop_builder = (
            self.build_stop_request if request.side == "BUY" else self.build_short_stop_request
        )
        sl_req = stop_builder(
            symbol=request.symbol,
            quantity=fill_qty,
            product=request.product,
            entry_price=fill_price,
            reference_ltp=entry_ltp,
            market_protection=request.market_protection,
        )
        sl = self.place(sl_req, force_mode=mode, toward_daily_cap=False)
        if not sl.ok:
            logger.error(
                "Stop-loss failed after entry for %s (%s %s): %s",
                request.symbol,
                request.product,
                fill_price,
                sl.message,
            )
        if self.book and entry.ok:
            trigger = float(sl_req.trigger_price or self.stop_price_from_entry(fill_price))
            limit_px = float(sl_req.price or self.stop_limit_price_from_trigger(trigger))
            circuit_limit = (
                self.fetch_upper_circuit_limit(request.symbol)
                if self.exit_on_upper_circuit and request.side == "BUY"
                else None
            )
            self.book.record_position(
                symbol=request.symbol,
                quantity=fill_qty,
                entry_price=fill_price,
                stop_price=trigger,
                stop_limit_price=limit_px,
                entry_order_id=entry.order_id,
                stop_order_id=sl.order_id if sl.ok else None,
                mode=mode,
                product=request.product,
                circuit_limit=circuit_limit,
                side=request.side,
            )
        return entry, sl
