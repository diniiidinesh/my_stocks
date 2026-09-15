from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nse_alert.confirm_bot import _CANCEL_RE, _CONFIRM_RE
from nse_alert.engine import Alert, AlertEngine
from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest, round_tick


# ---------------------------------------------------------------------------
# TC matrix helpers
# ---------------------------------------------------------------------------


def _executor(tmp_path: Path, **kwargs: Any) -> OrderExecutor:
    defaults: dict[str, Any] = {
        "mode": "dry_run",
        "default_qty": 1,
        "max_orders_per_day": 10,
        "trade_on_thresholds": [13],
        "trade_sides": "up",
        "stop_loss_pct": 2.0,
        "book": OrderBook(tmp_path / "orders.json"),
    }
    defaults.update(kwargs)
    return OrderExecutor(**defaults)


def _buy_req(symbol: str = "RELIANCE") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
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


# ---------------------------------------------------------------------------
# Filter / gate tests (TC-FILTER-*)
# ---------------------------------------------------------------------------


def test_tc_filter_off_mode_never_trades(tmp_path: Path) -> None:
    ex = _executor(tmp_path, mode="off")
    assert not ex.enabled()
    assert not ex.should_trade_alert(direction="UP", threshold_pct=13.0)


@pytest.mark.parametrize(
    ("direction", "threshold", "sides", "levels", "expected"),
    [
        ("UP", 13.0, "up", [13], True),
        ("DOWN", 13.0, "up", [13], False),
        ("UP", 7.0, "up", [13], False),
        ("UP", 13.0, "down", [13], False),
        ("DOWN", 13.0, "down", [13], True),
        ("UP", 13.0, "both", [13], True),
        ("DOWN", 13.0, "both", [13], True),
        ("UP", 11.0, "up", [11, 13], True),
    ],
)
def test_tc_filter_should_trade_matrix(
    tmp_path: Path,
    direction: str,
    threshold: float,
    sides: str,
    levels: list[float],
    expected: bool,
) -> None:
    ex = _executor(tmp_path, trade_sides=sides, trade_on_thresholds=levels)
    assert ex.should_trade_alert(direction=direction, threshold_pct=threshold) is expected


def test_tc_filter_alert_engine_to_trade_gate(tmp_path: Path) -> None:
    """+13% UP from engine should pass trade gate; +4% should not."""
    engine = AlertEngine(
        prev_closes={"DEMO13": 100.0},
        thresholds=[4, 7, 11, 13],
        state_path=tmp_path / "fired.json",
        fo_symbols={"DEMO13"},
    )
    ex = _executor(tmp_path)
    mid = engine.on_tick("DEMO13", 105.0)
    assert mid and mid[0].threshold_pct == 4.0
    assert not ex.should_trade_alert(
        direction=mid[0].direction, threshold_pct=mid[0].threshold_pct
    )
    big = engine.on_tick("DEMO13", 114.0)
    levels = [a.threshold_pct for a in big]
    assert 13.0 in levels
    hit = next(a for a in big if a.threshold_pct == 13.0)
    assert ex.should_trade_alert(direction=hit.direction, threshold_pct=hit.threshold_pct)


# ---------------------------------------------------------------------------
# Dry-run auto path (TC-DRY-*)
# ---------------------------------------------------------------------------


def test_tc_dry_entry_and_sl_and_position(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    req = ex.build_request_from_alert(
        symbol="RELIANCE", direction="UP", threshold_pct=13.0, change_pct=13.5
    )
    assert req.side == "BUY"
    assert "SL 2%" in req.reason
    entry, sl = ex.place_entry_with_stop(req, entry_ltp=113.0)
    assert entry.ok and entry.order_id and entry.order_id.startswith("DRY-")
    assert sl is not None and sl.ok
    assert sl.request.side == "SELL"
    assert sl.request.order_type == "SL-M"
    assert sl.request.trigger_price == round_tick(113.0 * 0.98)
    assert ex.book is not None
    assert ex.book.has_open_position("RELIANCE")
    assert ex.book.placed_count == 2  # entry + SL both recorded in dry_run


def test_tc_dry_blocks_second_entry_while_open(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    req = _buy_req()
    entry, _ = ex.place_entry_with_stop(req, entry_ltp=100.0)
    assert entry.ok
    again, sl2 = ex.place_entry_with_stop(req, entry_ltp=101.0)
    assert not again.ok
    assert sl2 is None
    assert "Open position" in again.message


def test_tc_dry_daily_cap_blocks_further_entries(tmp_path: Path) -> None:
    ex = _executor(tmp_path, max_orders_per_day=2)
    # First entry+SL consumes 2 placed_count in dry_run
    entry, sl = ex.place_entry_with_stop(_buy_req("AAA"), entry_ltp=100.0)
    assert entry.ok and sl is not None
    assert ex.book is not None and ex.book.placed_count == 2
    blocked, _ = ex.place_entry_with_stop(_buy_req("BBB"), entry_ltp=50.0)
    assert not blocked.ok
    assert "Daily order cap" in blocked.message


def test_tc_dry_sell_entry_skips_sl(tmp_path: Path) -> None:
    ex = _executor(tmp_path, trade_sides="down")
    req = OrderRequest(
        symbol="SBIN",
        side="SELL",
        quantity=1,
        product="CNC",
        order_type="MARKET",
        price=None,
        trigger_price=None,
        market_protection=2,
        tag="nsealrt",
        reason="down test",
    )
    entry, sl = ex.place_entry_with_stop(req, entry_ltp=80.0)
    assert entry.ok
    assert sl is None


def test_tc_mode_off_place_returns_false(tmp_path: Path) -> None:
    ex = _executor(tmp_path, mode="off")
    result = ex.place(_buy_req())
    assert not result.ok
    assert "off" in result.message


# ---------------------------------------------------------------------------
# Live path with mocked Kite (TC-LIVE-MOCK-*)
# ---------------------------------------------------------------------------


class _FakeKite:
    PRODUCT_CNC = "CNC"
    PRODUCT_MIS = "MIS"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_SLM = "SL-M"
    ORDER_TYPE_SL = "SL"
    ORDER_TYPE_LIMIT = "LIMIT"
    VARIETY_REGULAR = "regular"
    EXCHANGE_NSE = "NSE"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    def place_order(self, **params: Any) -> str:
        self._n += 1
        self.calls.append(params)
        return f"OID-{self._n}"


def test_tc_live_mock_auto_places_market_then_slm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKite()
    ex = _executor(
        tmp_path,
        mode="auto",
        api_key="k",
        access_token="t",
        max_orders_per_day=10,
    )
    monkeypatch.setattr(ex, "_kite", lambda: fake)

    req = _buy_req("INFY")
    entry, sl = ex.place_entry_with_stop(req, entry_ltp=1500.0)
    assert entry.ok and entry.order_id == "OID-1"
    assert sl is not None and sl.ok and sl.order_id == "OID-2"
    assert len(fake.calls) == 2
    assert fake.calls[0]["transaction_type"] == "BUY"
    assert fake.calls[0]["order_type"] == "MARKET"
    assert fake.calls[0]["tradingsymbol"] == "INFY"
    assert fake.calls[0]["market_protection"] == 2
    assert fake.calls[1]["transaction_type"] == "SELL"
    assert fake.calls[1]["order_type"] == "SL-M"
    assert fake.calls[1]["trigger_price"] == round_tick(1500.0 * 0.98)
    assert ex.book is not None
    assert ex.book.has_open_position("INFY")


def test_tc_live_mock_kite_error_returns_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Boom(_FakeKite):
        def place_order(self, **params: Any) -> str:
            raise RuntimeError("Insufficient funds")

    ex = _executor(tmp_path, mode="auto", api_key="k", access_token="t")
    monkeypatch.setattr(ex, "_kite", lambda: _Boom())
    result = ex.place(_buy_req())
    assert not result.ok
    assert "Insufficient funds" in result.message


def test_tc_live_requires_credentials(tmp_path: Path) -> None:
    ex = _executor(tmp_path, mode="auto", api_key="", access_token="")
    result = ex.place(_buy_req())
    assert not result.ok
    assert "KITE_API_KEY" in result.message or "required" in result.message.lower()


# ---------------------------------------------------------------------------
# Confirm / pending book (TC-CONFIRM-*)
# ---------------------------------------------------------------------------


def test_tc_confirm_pending_lifecycle(tmp_path: Path) -> None:
    book = OrderBook(tmp_path / "orders.json")
    pending = book.add_pending(
        _buy_req("TCS"),
        alert_symbol="TCS",
        alert_threshold=13.0,
        alert_direction="UP",
        entry_ltp=2200.0,
        ttl_minutes=30,
    )
    assert pending.id in {p.id for p in book.list_pending()}
    got = book.get_pending(pending.id)
    assert got is not None and got.entry_ltp == 2200.0
    book.mark_pending(pending.id, "cancelled")
    assert book.get_pending(pending.id) is not None
    assert book.get_pending(pending.id).status == "cancelled"  # type: ignore[union-attr]
    assert pending.id not in {p.id for p in book.list_pending()}


def test_tc_confirm_pending_expires(tmp_path: Path) -> None:
    book = OrderBook(tmp_path / "orders.json")
    pending = book.add_pending(
        _buy_req("AAA"),
        alert_symbol="AAA",
        alert_threshold=13.0,
        alert_direction="UP",
        entry_ltp=10.0,
        ttl_minutes=30,
    )
    # Force expiry in stored JSON
    book._data["pending"][pending.id]["expires_at"] = (  # noqa: SLF001
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    book._save()  # noqa: SLF001
    assert book.list_pending() == []
    assert book.get_pending(pending.id) is not None
    assert book._data["pending"][pending.id]["status"] == "expired"  # noqa: SLF001


@pytest.mark.parametrize(
    ("text", "kind", "pid"),
    [
        ("CONFIRM abc123", "confirm", "ABC123"),
        ("confirm ABCDEF", "confirm", "ABCDEF"),
        ("/confirm dead01", "confirm", "DEAD01"),
        ("CANCEL ab12", "cancel", "AB12"),
        ("cancel FF00AA", "cancel", "FF00AA"),
        ("hello", None, None),
        ("CONFIRM", None, None),
    ],
)
def test_tc_confirm_telegram_regex(text: str, kind: str | None, pid: str | None) -> None:
    cm = _CONFIRM_RE.match(text)
    xm = _CANCEL_RE.match(text)
    if kind == "confirm":
        assert cm and cm.group(1).upper() == pid
        assert not xm
    elif kind == "cancel":
        assert xm and xm.group(1).upper() == pid
        assert not cm
    else:
        assert not cm and not xm


# ---------------------------------------------------------------------------
# OrderBook day reset (TC-BOOK-*)
# ---------------------------------------------------------------------------


def test_tc_book_resets_on_new_calendar_day(tmp_path: Path) -> None:
    path = tmp_path / "orders.json"
    book = OrderBook(path)
    book._data["placed_count"] = 5  # noqa: SLF001
    book._data["date"] = "2000-01-01"  # noqa: SLF001
    book._save()  # noqa: SLF001
    fresh = OrderBook(path)
    assert fresh.placed_count == 0
    assert fresh._data["date"] == date.today().isoformat()  # noqa: SLF001


def test_tc_stop_price_rounding_cases() -> None:
    ex = OrderExecutor(stop_loss_pct=2.0)
    assert ex.stop_price_from_entry(100.0) == 98.0
    assert ex.stop_price_from_entry(113.37) == round_tick(113.37 * 0.98)


def test_tc_alert_dataclass_still_compatible_with_trade_fields() -> None:
    """Sanity: Alert used by watch → trade path carries needed fields."""
    alert = Alert(
        symbol="DEMO13",
        ltp=114.0,
        prev_close=100.0,
        change_pct=14.0,
        direction="UP",
        threshold_pct=13.0,
        fired_at=datetime.now(timezone.utc),
    )
    assert alert.ltp == 114.0
    assert alert.direction == "UP"
