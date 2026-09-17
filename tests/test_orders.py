from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nse_alert.confirm_bot import _CANCEL_RE, _CONFIRM_RE
from nse_alert.engine import Alert, AlertEngine
from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest, parse_order_margins_payload, round_tick


# ---------------------------------------------------------------------------
# TC matrix helpers
# ---------------------------------------------------------------------------


def _executor(tmp_path: Path, **kwargs: Any) -> OrderExecutor:
    defaults: dict[str, Any] = {
        "mode": "dry_run",
        "default_qty": 1,
        "sizing_mode": "fixed",
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
        product="MIS",
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
    assert sl.request.order_type == "SL"
    trigger = round_tick(113.0 * 0.98)
    assert sl.request.trigger_price == trigger
    assert sl.request.price == round_tick(trigger - 0.10)  # 2 ticks
    assert ex.book is not None
    assert ex.book.has_open_position("RELIANCE")
    # Entry counts toward cap; stop does not
    assert ex.book.placed_count == 1
    assert len(ex.book._data["history"]) == 2  # noqa: SLF001


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
    ex = _executor(tmp_path, max_orders_per_day=1)
    # Cap=1 still allows entry + stop (stop excluded from cap)
    entry, sl = ex.place_entry_with_stop(_buy_req("AAA"), entry_ltp=100.0)
    assert entry.ok and sl is not None and sl.ok
    assert ex.book is not None and ex.book.placed_count == 1
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

    def __init__(self, *, fill_price: float = 1500.0, fill_qty: int = 1) -> None:
        self.calls: list[dict[str, Any]] = []
        self.modifies: list[dict[str, Any]] = []
        self._n = 0
        self.fill_price = fill_price
        self.fill_qty = fill_qty

    def place_order(self, **params: Any) -> str:
        self._n += 1
        self.calls.append(params)
        return f"OID-{self._n}"

    def order_history(self, order_id: str) -> list[dict[str, Any]]:
        return [
            {
                "status": "COMPLETE",
                "average_price": self.fill_price,
                "filled_quantity": self.fill_qty,
                "quantity": self.fill_qty,
            }
        ]

    def modify_order(self, **params: Any) -> str:
        self.modifies.append(params)
        return "MOD-1"


def test_tc_live_mock_auto_places_market_then_sl_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill_price = 1498.75
    fake = _FakeKite(fill_price=fill_price, fill_qty=1)
    ex = _executor(
        tmp_path,
        mode="auto",
        api_key="k",
        access_token="t",
        max_orders_per_day=1,  # stop must still place when cap is 1
        stop_wait_sec=1.0,
    )
    monkeypatch.setattr(ex, "_kite", lambda: fake)

    req = _buy_req("INFY")
    entry, sl = ex.place_entry_with_stop(req, entry_ltp=1500.0)
    assert entry.ok and entry.order_id == "OID-1"
    assert sl is not None and sl.ok and sl.order_id == "OID-2"
    assert len(fake.calls) == 2
    assert fake.calls[0]["transaction_type"] == "BUY"
    assert fake.calls[0]["order_type"] == "MARKET"
    assert fake.calls[0]["product"] == "MIS"
    assert fake.calls[0]["tradingsymbol"] == "INFY"
    assert fake.calls[0]["market_protection"] == 2
    assert fake.calls[1]["transaction_type"] == "SELL"
    assert fake.calls[1]["order_type"] == "SL"
    assert fake.calls[1]["product"] == "MIS"
    trigger = round_tick(fill_price * 0.98)
    assert fake.calls[1]["trigger_price"] == trigger
    assert fake.calls[1]["price"] == round_tick(trigger - 0.10)
    assert "market_protection" not in fake.calls[1]
    assert ex.book is not None
    assert ex.book.has_open_position("INFY")
    pos = ex.book.get_position("INFY")
    assert pos is not None and pos["entry_price"] == fill_price
    assert ex.book.placed_count == 1


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
        ("/confirm@MyBot dead01", "confirm", "DEAD01"),
        ("/CONFIRM@nse_alert_bot ABC123", "confirm", "ABC123"),
        ("CANCEL ab12", "cancel", "AB12"),
        ("cancel FF00AA", "cancel", "FF00AA"),
        ("/cancel@MyBot ab12", "cancel", "AB12"),
        ("hello", None, None),
        ("CONFIRM", None, None),
        ("/confirm@MyBot", None, None),
    ],
)
def test_tc_confirm_telegram_regex(text: str, kind: str | None, pid: str | None) -> None:
    from nse_alert.confirm_bot import parse_trade_command

    parsed = parse_trade_command(text)
    cm = _CONFIRM_RE.match(text)
    xm = _CANCEL_RE.match(text)
    if kind == "confirm":
        assert parsed == ("confirm", pid)
        assert cm and cm.group(1).upper() == pid
        assert not xm
    elif kind == "cancel":
        assert parsed == ("cancel", pid)
        assert xm and xm.group(1).upper() == pid
        assert not cm
    else:
        assert parsed is None
        assert not cm and not xm


def test_tc_confirm_group_message_accepted_even_if_chat_id_differs() -> None:
    """Group confirms used to be dropped when TELEGRAM_CHAT_ID was the DM id."""
    from nse_alert.confirm_bot import TelegramConfirmListener

    seen: list[tuple[str, str]] = []
    listener = TelegramConfirmListener(
        bot_token="x",
        chat_id="11111",  # private chat
        on_confirm=lambda pid: seen.append(("confirm", pid)),
        on_cancel=lambda pid: seen.append(("cancel", pid)),
    )
    listener._handle(  # noqa: SLF001
        {
            "message": {
                "chat": {"id": -1001234567890, "type": "supergroup", "title": "trades"},
                "text": "/confirm@MyBot abc123",
            }
        }
    )
    assert seen == [("confirm", "ABC123")]
    listener._handle(  # noqa: SLF001
        {
            "edited_message": {
                "chat": {"id": -1001234567890, "type": "supergroup", "title": "trades"},
                "text": "/cancel DEF456",
            }
        }
    )
    assert seen == [("confirm", "ABC123"), ("cancel", "DEF456")]


def test_tc_confirm_chat_id_normalizes_quotes_and_spaces() -> None:
    from nse_alert.confirm_bot import TelegramConfirmListener

    seen: list[str] = []
    listener = TelegramConfirmListener(
        bot_token="x",
        chat_id=' "-10099" ',
        on_confirm=lambda pid: seen.append(pid),
    )
    listener._handle(  # noqa: SLF001
        {"message": {"chat": {"id": -10099, "type": "group"}, "text": "confirm ab12"}}
    )
    assert seen == ["AB12"]


def test_pending_not_found_message_includes_path_and_known_ids(tmp_path: Path) -> None:
    from nse_alert.cli import _pending_not_found_msg
    from nse_alert.config import Settings

    book = OrderBook(tmp_path / "orders.json")
    book.add_pending(
        _buy_req("INFY"),
        alert_symbol="INFY",
        alert_threshold=13.0,
        alert_direction="UP",
        entry_ltp=1500.0,
    )
    settings = Settings(STATE_DIR=tmp_path)
    msg = _pending_not_found_msg(settings, book, "156A78")
    assert "156A78" in msg
    assert str(tmp_path / "orders.json") in msg
    assert "ids=" in msg
    assert "Docker" in msg


def test_summarize_chats_dedupes_group_and_dm() -> None:
    from nse_alert.confirm_bot import summarize_chats

    chats = summarize_chats(
        [
            {
                "message": {
                    "chat": {"id": 42, "type": "private", "first_name": "Dinesh"},
                    "text": "hi",
                }
            },
            {
                "message": {
                    "chat": {
                        "id": -1001,
                        "type": "supergroup",
                        "title": "alerts",
                    },
                    "text": "/confirm abc",
                }
            },
            {
                "edited_message": {
                    "chat": {"id": 42, "type": "private", "first_name": "Dinesh"},
                    "text": "again",
                }
            },
        ]
    )
    ids = {c["id"] for c in chats}
    assert ids == {"42", "-1001"}
    group = next(c for c in chats if c["id"] == "-1001")
    assert group["type"] == "supergroup"
    assert group["title"] == "alerts"


def test_cli_order_without_qty_uses_margin_not_trade_qty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commenting out TRADE_QTY=1 is not required; omit --qty + margin sizing."""
    from nse_alert.cli import _cli_size_quantity
    from nse_alert.config import Settings

    class _MarginKite(_FakeKite):
        def quote(self, keys: list[str]) -> dict[str, Any]:
            return {"NSE:INFY": {"last_price": 1500.0}}

        def order_margins(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"total": 2000.0, "leverage": 5.0}]

    settings = Settings(
        TRADE_SIZING="margin",
        TRADE_MARGIN_INR=10_000,
        TRADE_QTY=1,
        KITE_API_KEY="k",
        KITE_ACCESS_TOKEN="t",
    )
    ex = _executor(
        tmp_path,
        mode="dry_run",
        api_key="k",
        access_token="t",
        sizing_mode="margin",
        margin_budget_inr=10_000,
        default_qty=1,
    )
    fake = _MarginKite()
    monkeypatch.setattr("nse_alert.cli._kite_client", lambda *a, **k: fake)
    monkeypatch.setattr(ex, "_kite", lambda: fake)
    qty, note = _cli_size_quantity(settings, ex, "INFY", "BUY")
    assert qty == 5
    assert "5 shares" in note


def test_cli_order_without_qty_requires_ltp_in_margin_mode(tmp_path: Path) -> None:
    import click

    from nse_alert.cli import _cli_size_quantity
    from nse_alert.config import Settings

    settings = Settings(TRADE_SIZING="margin", TRADE_QTY=1)
    ex = _executor(tmp_path, sizing_mode="margin", default_qty=1)
    with pytest.raises(click.ClickException, match="TRADE_QTY=1"):
        _cli_size_quantity(settings, ex, "INFY", "BUY")


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
    ex = OrderExecutor(stop_loss_pct=2.0, stop_limit_ticks=2)
    assert ex.stop_price_from_entry(100.0) == 98.0
    assert ex.stop_price_from_entry(113.37) == round_tick(113.37 * 0.98)
    assert ex.stop_limit_price_from_trigger(98.0) == 97.9


def test_tc_sl_trigger_clamped_below_ltp() -> None:
    ex = OrderExecutor(stop_loss_pct=2.0, stop_limit_ticks=2)
    req = ex.build_stop_request(
        symbol="AAA",
        quantity=1,
        product="MIS",
        entry_price=100.0,
        reference_ltp=98.5,
    )
    assert req.trigger_price is not None
    assert req.trigger_price < 98.5


def test_tc_trail_breakeven_modifies_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKite(fill_price=100.0)
    ex = _executor(
        tmp_path,
        mode="auto",
        api_key="k",
        access_token="t",
        trail_breakeven=True,
        trail_breakeven_pct=2.0,
        stop_wait_sec=1.0,
    )
    monkeypatch.setattr(ex, "_kite", lambda: fake)
    req = _buy_req("TATASTEEL")
    entry, sl = ex.place_entry_with_stop(req, entry_ltp=100.0)
    assert entry.ok and sl is not None and sl.ok

    assert ex.manage_open_stops("TATASTEEL", 101.0) is None
    msg = ex.manage_open_stops("TATASTEEL", 102.5)
    assert msg is not None and "Trailed SL" in msg
    assert len(fake.modifies) == 1
    assert fake.modifies[0]["trigger_price"] == 100.0
    pos = ex.book.get_position("TATASTEEL") if ex.book else None
    assert pos is not None and pos["breakeven_armed"] is True
    assert ex.manage_open_stops("TATASTEEL", 105.0) is None


def test_tc_trail_breakeven_dry_run(tmp_path: Path) -> None:
    ex = _executor(tmp_path, trail_breakeven=True, trail_breakeven_pct=2.0)
    req = _buy_req("SBIN")
    entry, sl = ex.place_entry_with_stop(req, entry_ltp=500.0)
    assert entry.ok and sl is not None and sl.ok
    msg = ex.manage_open_stops("SBIN", 510.0)
    assert msg is not None and "cost" in msg.lower()
    pos = ex.book.get_position("SBIN") if ex.book else None
    assert pos is not None and pos["breakeven_armed"] is True


def test_tc_margin_sizing_uses_order_margins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _MarginKite(_FakeKite):
        def order_margins(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # ₹2,000 margin per share → ₹10k budget ⇒ 5 shares
            return [{"total": 2000.0, "leverage": 5.0}]

    ex = _executor(
        tmp_path,
        mode="auto",
        api_key="k",
        access_token="t",
        sizing_mode="margin",
        margin_budget_inr=10_000,
    )
    monkeypatch.setattr(ex, "_kite", lambda: _MarginKite())
    req = ex.build_request_from_alert(
        symbol="INFY",
        direction="UP",
        threshold_pct=13.0,
        change_pct=13.5,
        entry_ltp=1500.0,
    )
    assert req.quantity == 5
    assert "margin≈₹10000" in req.reason
    assert "5 shares" in req.reason


def test_parse_order_margins_payload_shapes() -> None:
    row = {"total": 400.0, "leverage": 5.0, "var": 400.0}
    assert parse_order_margins_payload([row])["total"] == 400.0
    assert parse_order_margins_payload({"orders": [row]})["total"] == 400.0
    assert parse_order_margins_payload({"data": [row]})["total"] == 400.0
    with pytest.raises(RuntimeError, match="empty"):
        parse_order_margins_payload([])
    with pytest.raises(RuntimeError, match="InputException"):
        parse_order_margins_payload(
            [{"error_type": "InputException", "message": "InputException: bad"}]
        )


def test_tc_margin_sizing_qty_one_when_margin_per_share_high(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TRADE_SIZING=margin still yields 1 share if 1 share already needs ~₹10k."""

    class _Pricey(_FakeKite):
        def order_margins(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"total": 8500.0, "leverage": 1.0, "var": 8500.0}]

    ex = _executor(
        tmp_path,
        mode="auto",
        api_key="k",
        access_token="t",
        sizing_mode="margin",
        margin_budget_inr=10_000,
        default_qty=1,
    )
    monkeypatch.setattr(ex, "_kite", lambda: _Pricey())
    qty, note = ex.size_quantity(symbol="PAGEIND", price=8500.0, side="BUY")
    assert qty == 1
    assert "qty=1 because Kite margin/share" in note
    assert "TRADE_QTY" in note


def test_tc_margin_sizing_fallback_leverage(tmp_path: Path) -> None:
    ex = _executor(
        tmp_path,
        sizing_mode="margin",
        margin_budget_inr=10_000,
        fallback_leverage=5.0,
        # no api credentials → margins call fails → fallback
    )
    qty, note = ex.size_quantity(symbol="AAA", price=500.0, side="BUY")
    # 10000 * 5 / 500 = 100
    assert qty == 100
    assert "fallback" in note.lower()


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
