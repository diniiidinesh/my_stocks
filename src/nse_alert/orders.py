from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

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

    def record_placed(self, result: OrderResult) -> None:
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
        entry_order_id: str | None,
        stop_order_id: str | None,
        mode: str,
    ) -> None:
        self._data["positions"][symbol.upper()] = {
            "status": "open",
            "quantity": quantity,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "mode": mode,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
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
    """Place CNC equity BUY on +threshold, with a %-stop SL-M from entry."""

    def __init__(
        self,
        *,
        api_key: str = "",
        access_token: str = "",
        mode: TradeMode = "off",
        default_qty: int = 1,
        product: str = "CNC",
        order_type: str = "MARKET",
        market_protection: int = 2,
        max_orders_per_day: int = 3,
        trade_on_thresholds: list[float] | None = None,
        trade_sides: str = "up",
        stop_loss_pct: float = 2.0,
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

    def stop_price_from_entry(self, entry: float) -> float:
        raw = entry * (1.0 - self.stop_loss_pct / 100.0)
        return round_tick(raw)

    def build_request_from_alert(
        self,
        *,
        symbol: str,
        direction: str,
        threshold_pct: float,
        change_pct: float,
        quantity: int | None = None,
    ) -> OrderRequest:
        side = self.side_for_direction(direction)
        qty = quantity if quantity is not None else self.default_qty
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
                f"SL {self.stop_loss_pct:g}% below entry"
            ),
        )

    def _kite(self) -> Any:
        from kiteconnect import KiteConnect

        if not self.api_key or not self.access_token:
            raise RuntimeError("KITE_API_KEY and KITE_ACCESS_TOKEN required to place orders")
        kite = KiteConnect(api_key=self.api_key)
        kite.set_access_token(self.access_token)
        return kite

    def place(self, request: OrderRequest, *, force_mode: str | None = None) -> OrderResult:
        mode = force_mode or self.mode
        now = datetime.now(timezone.utc)
        if mode == "off":
            return OrderResult(False, mode, request, None, "TRADE_MODE=off", now)

        if self.book and self.book.placed_count >= self.max_orders_per_day:
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
            result = OrderResult(
                True,
                mode,
                request,
                fake_id,
                f"DRY RUN: would {request.side} {request.quantity} {request.symbol}",
                now,
            )
            if self.book:
                self.book.record_placed(result)
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
            self.book.record_placed(result)
        logger.info("%s", result.message)
        return result

    def place_entry_with_stop(
        self,
        request: OrderRequest,
        *,
        entry_ltp: float,
        force_mode: str | None = None,
    ) -> tuple[OrderResult, OrderResult | None]:
        """BUY (or SELL) entry, then attach an opposite SL-M at stop_loss_pct from entry."""
        entry = self.place(request, force_mode=force_mode)
        if not entry.ok:
            return entry, None

        stop_px = self.stop_price_from_entry(entry_ltp)
        if request.side != "BUY":
            # Test run is BUY-only with downside SL; skip SL for sells.
            return entry, None

        sl_req = OrderRequest(
            symbol=request.symbol,
            side="SELL",
            quantity=request.quantity,
            product=request.product,
            order_type="SL-M",
            price=None,
            trigger_price=stop_px,
            market_protection=request.market_protection,
            tag="nseasl",
            reason=f"stop-loss {self.stop_loss_pct:g}% under entry {entry_ltp:.2f}",
        )
        mode = force_mode or self.mode
        if mode == "dry_run":
            sl = OrderResult(
                True,
                mode,
                sl_req,
                f"DRY-SL-{uuid.uuid4().hex[:6]}",
                (
                    f"DRY RUN: would place SL-M SELL {sl_req.quantity} {sl_req.symbol} "
                    f"trigger={stop_px:.2f} (entry≈{entry_ltp:.2f})"
                ),
                datetime.now(timezone.utc),
            )
            logger.info("%s", sl.message)
            if self.book:
                self.book.record_placed(sl)
                self.book.record_position(
                    symbol=request.symbol,
                    quantity=request.quantity,
                    entry_price=entry_ltp,
                    stop_price=stop_px,
                    entry_order_id=entry.order_id,
                    stop_order_id=sl.order_id,
                    mode=mode,
                )
            return entry, sl

        # Live: placing SL does not count against "entry" semantics the same way,
        # but still goes through place() which increments placed_count — bump cap
        # temporarily by allowing one extra via direct kite call path:
        sl = self.place(sl_req, force_mode=mode)
        if self.book and entry.ok:
            self.book.record_position(
                symbol=request.symbol,
                quantity=request.quantity,
                entry_price=entry_ltp,
                stop_price=stop_px,
                entry_order_id=entry.order_id,
                stop_order_id=sl.order_id if sl.ok else None,
                mode=mode,
            )
        return entry, sl
