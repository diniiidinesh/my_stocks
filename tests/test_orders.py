from __future__ import annotations

from pathlib import Path

from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest, round_tick


def test_round_tick() -> None:
    assert round_tick(100.0 * 0.98) == 98.0
    assert round_tick(113.37 * 0.98) == 111.1


def test_dry_run_entry_with_2pct_stop(tmp_path: Path) -> None:
    book = OrderBook(tmp_path / "orders.json")
    executor = OrderExecutor(
        mode="dry_run",
        default_qty=1,
        max_orders_per_day=5,
        trade_on_thresholds=[13],
        trade_sides="up",
        stop_loss_pct=2.0,
        book=book,
    )
    assert executor.should_trade_alert(direction="UP", threshold_pct=13.0)
    assert not executor.should_trade_alert(direction="UP", threshold_pct=7.0)
    assert not executor.should_trade_alert(direction="DOWN", threshold_pct=13.0)

    req = executor.build_request_from_alert(
        symbol="RELIANCE",
        direction="UP",
        threshold_pct=13.0,
        change_pct=13.5,
    )
    entry_ltp = 113.0
    entry, sl = executor.place_entry_with_stop(req, entry_ltp=entry_ltp)
    assert entry.ok
    assert sl is not None and sl.ok
    assert sl.request.trigger_price == round_tick(entry_ltp * 0.98)
    assert "SL-M" in sl.message
    assert book.has_open_position("RELIANCE")

    # Second entry blocked while position open
    again, _ = executor.place_entry_with_stop(req, entry_ltp=entry_ltp)
    assert not again.ok
    assert "Open position" in again.message


def test_pending_stores_entry_ltp(tmp_path: Path) -> None:
    book = OrderBook(tmp_path / "orders.json")
    req = OrderRequest(
        symbol="TCS",
        side="BUY",
        quantity=1,
        product="CNC",
        order_type="MARKET",
        price=None,
        trigger_price=None,
        market_protection=2,
        tag="nsealrt",
        reason="test",
    )
    pending = book.add_pending(
        req,
        alert_symbol="TCS",
        alert_threshold=13.0,
        alert_direction="UP",
        entry_ltp=2200.0,
        ttl_minutes=30,
    )
    got = book.get_pending(pending.id)
    assert got is not None
    assert got.entry_ltp == 2200.0
