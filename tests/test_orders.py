from __future__ import annotations

from pathlib import Path

from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest


def test_dry_run_order_and_daily_cap(tmp_path: Path) -> None:
    book = OrderBook(tmp_path / "orders.json")
    executor = OrderExecutor(
        mode="dry_run",
        default_qty=2,
        max_orders_per_day=1,
        trade_on_thresholds=[7],
        book=book,
    )
    assert executor.should_trade_alert(direction="UP", threshold_pct=7.0)
    assert not executor.should_trade_alert(direction="UP", threshold_pct=4.0)
    assert not executor.should_trade_alert(direction="DOWN", threshold_pct=7.0)

    req = executor.build_request_from_alert(
        symbol="RELIANCE",
        direction="UP",
        threshold_pct=7.0,
        change_pct=7.5,
    )
    assert req.side == "BUY"
    assert req.quantity == 2

    first = executor.place(req)
    assert first.ok and first.order_id and first.order_id.startswith("DRY-")
    assert book.placed_count == 1

    second = executor.place(req)
    assert not second.ok
    assert "cap" in second.message.lower()


def test_pending_confirm_flow(tmp_path: Path) -> None:
    book = OrderBook(tmp_path / "orders.json")
    req = OrderRequest(
        symbol="TCS",
        side="BUY",
        quantity=1,
        product="CNC",
        order_type="MARKET",
        price=None,
        market_protection=2,
        tag="nsealrt",
        reason="test",
    )
    pending = book.add_pending(
        req,
        alert_symbol="TCS",
        alert_threshold=7.0,
        alert_direction="UP",
        ttl_minutes=30,
    )
    assert book.get_pending(pending.id) is not None
    assert len(book.list_pending()) == 1
    book.mark_pending(pending.id, "confirmed")
    assert book.get_pending(pending.id).status == "confirmed"  # type: ignore[union-attr]
