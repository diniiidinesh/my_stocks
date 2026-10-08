from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nse_alert.config import Settings
from nse_alert.squareoff import TAG, SquareOffSchedule, parse_products, run_squareoff
from nse_alert.session import hhmm_to_time

IST = timezone(timedelta(hours=5, minutes=30))


class FakeKite:
    """Minimal Kite: cancels remove orders, market orders flatten positions."""

    def __init__(
        self,
        orders: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
        *,
        fill: bool = True,
        fail_place: set[str] | None = None,
        fail_cancel: set[str] | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self.fill = fill
        self.fail_place = fail_place or set()
        self.fail_cancel = fail_cancel or set()
        self.events: list[tuple[str, Any]] = []

    def orders(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self._orders]

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return {"net": [dict(p) for p in self._positions]}

    def cancel_order(self, *, variety: str, order_id: str) -> str:
        if order_id in self.fail_cancel:
            raise RuntimeError("cannot cancel")
        self.events.append(("cancel", order_id))
        for o in self._orders:
            if o["order_id"] == order_id:
                o["status"] = "CANCELLED"
        return order_id

    def place_order(self, **params: Any) -> str:
        if params["tradingsymbol"] in self.fail_place:
            raise RuntimeError("rejected")
        self.events.append(("place", params))
        oid = f"SQ-{len(self.events)}"
        self._orders.append(
            {
                "order_id": oid,
                "tradingsymbol": params["tradingsymbol"],
                "tag": params["tag"],
                "status": "COMPLETE" if self.fill else "OPEN",
                "product": params["product"],
                "transaction_type": params["transaction_type"],
                "quantity": params["quantity"],
                "order_type": "MARKET",
            }
        )
        if self.fill:
            for p in self._positions:
                if p["tradingsymbol"] == params["tradingsymbol"]:
                    p["quantity"] = 0
        return oid


def _order(oid: str, symbol: str, status: str = "TRIGGER PENDING", product: str = "MIS") -> dict[str, Any]:
    return {
        "order_id": oid,
        "tradingsymbol": symbol,
        "status": status,
        "product": product,
        "transaction_type": "SELL",
        "quantity": 5,
        "order_type": "SL",
        "variety": "regular",
        "tag": None,
    }


def _pos(symbol: str, qty: int, product: str = "MIS") -> dict[str, Any]:
    return {"tradingsymbol": symbol, "exchange": "NSE", "product": product, "quantity": qty}


def _run(kite: FakeKite, **kw: Any):
    return run_squareoff(kite, live=kw.pop("live", True), sleep=lambda _s: None, **kw)


def test_cancels_stop_before_selling() -> None:
    kite = FakeKite([_order("S1", "INFY")], [_pos("INFY", 5)])
    report = _run(kite)
    kinds = [e[0] for e in kite.events]
    assert kinds == ["cancel", "place"]
    assert report.ok and report.closed == ["INFY SELL 5"]
    assert kite.events[1][1]["order_type"] == "MARKET"
    assert kite.events[1][1]["tag"] == TAG


def test_short_position_is_bought_back() -> None:
    kite = FakeKite(positions=[_pos("SBIN", -7)])
    report = _run(kite)
    assert report.ok
    assert kite.events[0][1]["transaction_type"] == "BUY"
    assert kite.events[0][1]["quantity"] == 7


def test_only_configured_products_are_touched() -> None:
    kite = FakeKite(
        [_order("S1", "INFY"), _order("D1", "TCS", product="CNC")],
        [_pos("INFY", 5), _pos("TCS", 10, product="CNC"), _pos("HDFC", 3, product="NRML")],
    )
    report = _run(kite)
    assert report.ok
    assert ("cancel", "D1") not in kite.events
    placed = [e[1]["tradingsymbol"] for e in kite.events if e[0] == "place"]
    assert placed == ["INFY"]


def test_dry_run_sends_nothing() -> None:
    kite = FakeKite([_order("S1", "INFY")], [_pos("INFY", 5)])
    report = _run(kite, live=False)
    assert kite.events == []
    assert report.cancelled and report.closed == ["INFY SELL 5"]
    assert "DRY RUN" in report.format()


def test_flat_account_is_a_noop() -> None:
    kite = FakeKite()
    report = _run(kite)
    assert report.ok and kite.events == [] and not report.closed


def test_second_run_does_not_stack_exit_orders() -> None:
    # An unfinished square-off order from an earlier run is still working.
    working = _order("SQ-0", "INFY", status="OPEN")
    working["tag"] = TAG
    kite = FakeKite([working], [_pos("INFY", 5)], fill=False)
    report = _run(kite)
    assert [e for e in kite.events if e[0] == "place"] == []
    assert ("cancel", "SQ-0") not in kite.events  # our own order is left alone
    assert report.remaining_symbols == {"INFY"} and not report.ok


def test_failed_exit_is_reported_as_remaining() -> None:
    kite = FakeKite(positions=[_pos("INFY", 5), _pos("TCS", 2)], fail_place={"INFY"})
    report = _run(kite)
    assert not report.ok
    assert report.remaining_symbols == {"INFY"}
    assert any("INFY" in m for m in report.close_failed)
    assert "STILL OPEN" in report.format()


def test_uncancellable_order_is_reported() -> None:
    kite = FakeKite([_order("S1", "INFY")], fail_cancel={"S1"})
    report = _run(kite)
    assert not report.ok and any("INFY" in m for m in report.cancel_failed)


def test_parse_products() -> None:
    assert parse_products("mis, nrml") == frozenset({"MIS", "NRML"})
    assert parse_products("") == frozenset({"MIS"})


def _ist(h: int, m: int, day: int = 21) -> datetime:
    # 2026-09-21 is a Monday; 2026-09-19 a Saturday.
    return datetime(2026, 9, day, h, m, tzinfo=IST)


@pytest.fixture
def schedule(tmp_path: Path) -> SquareOffSchedule:
    return SquareOffSchedule(
        tmp_path / "squareoff.json", at=hhmm_to_time(1505), latest=hhmm_to_time(1510)
    )


def test_schedule_windows(schedule: SquareOffSchedule) -> None:
    assert schedule.check(_ist(14, 59)) == "idle"
    assert schedule.check(_ist(15, 5)) == "run"
    assert schedule.check(_ist(15, 10)) == "run"
    assert schedule.check(_ist(15, 11)) == "missed"
    assert schedule.check(_ist(15, 30)) == "idle"
    assert schedule.check(_ist(15, 5, day=19)) == "idle"  # Saturday


def test_schedule_runs_once_per_day(schedule: SquareOffSchedule) -> None:
    schedule.mark(_ist(15, 5), "done")
    assert schedule.check(_ist(15, 6)) == "idle"
    assert schedule.check(_ist(15, 5, day=22)) == "run"  # next day


def test_live_mode_tightens_entry_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQUAREOFF_MODE", "live")
    clock = Settings(_env_file=None).session_clock({"INFY"})
    assert clock.mis_cutoff("INFY") == hhmm_to_time(1505)
    assert clock.mis_cutoff("OTHER") == hhmm_to_time(1505)
    monkeypatch.setenv("SQUAREOFF_MODE", "dry_run")
    assert Settings(_env_file=None).session_clock({"INFY"}).mis_cutoff("INFY") == hhmm_to_time(1512)
