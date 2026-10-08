"""End-of-day square-off: cancel open orders, then flatten open positions.

Order matters. Resting stop-loss orders are cancelled *first*; otherwise a
SL-Limit SELL that is still live when the square-off SELL fills would sell the
same shares twice and leave the account short. The state of truth is Kite
(``orders()`` / ``positions()``), not ``orders.json``, so this also flattens
positions that were opened by hand.
"""

from __future__ import annotations

import json
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Literal

from nse_alert.session import to_ist

logger = logging.getLogger(__name__)

TAG = "nsesqoff"
_TERMINAL = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})
_NOT_CANCELLABLE = _TERMINAL | {"CANCEL PENDING"}
MARKET_CLOSE = time(15, 30)


def parse_products(raw: str) -> frozenset[str]:
    products = frozenset(p.strip().upper() for p in raw.split(",") if p.strip())
    return products or frozenset({"MIS"})


@dataclass
class SquareOffReport:
    live: bool
    cancelled: list[str] = field(default_factory=list)
    cancel_failed: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    close_failed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    remaining_symbols: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not (self.cancel_failed or self.close_failed or self.remaining)

    def format(self, ist_time: str = "") -> str:
        head = "🧹 EOD square-off" + (" (LIVE)" if self.live else " (DRY RUN — nothing sent)")
        if ist_time:
            head += f" {ist_time} IST"
        verb_c, verb_p = ("Cancelled", "Squared off") if self.live else ("Would cancel", "Would square off")
        lines = [head]
        lines.append(
            f"{verb_c} {len(self.cancelled)} open order(s)"
            + (": " + "; ".join(self.cancelled) if self.cancelled else "")
        )
        lines.append(
            f"{verb_p} {len(self.closed)} position(s)"
            + (": " + "; ".join(self.closed) if self.closed else "")
        )
        if self.cancel_failed:
            lines.append("⚠️ Could not cancel: " + "; ".join(self.cancel_failed))
        if self.close_failed:
            lines.append("⚠️ Square-off order failed: " + "; ".join(self.close_failed))
        if self.remaining:
            lines.append(
                "🚨 STILL OPEN: " + "; ".join(self.remaining) + " — act in Kite now"
            )
        elif self.live and self.ok:
            lines.append("✅ Flat.")
        return "\n".join(lines)


def _is_ours(order: dict[str, Any]) -> bool:
    return order.get("tag") == TAG or TAG in (order.get("tags") or [])


def _order_label(o: dict[str, Any]) -> str:
    return (
        f"{o.get('tradingsymbol')} {o.get('transaction_type')} "
        f"{o.get('quantity')} {o.get('order_type')}"
    )


def _cancellable_orders(kite: Any, products: frozenset[str]) -> list[dict[str, Any]]:
    return [
        o
        for o in kite.orders()
        if str(o.get("product", "")).upper() in products
        and str(o.get("status", "")).upper() not in _NOT_CANCELLABLE
        and not _is_ours(o)
    ]


def _open_positions(kite: Any, products: frozenset[str]) -> list[dict[str, Any]]:
    return [
        p
        for p in kite.positions().get("net", [])
        if str(p.get("product", "")).upper() in products and int(p.get("quantity") or 0) != 0
    ]


def _symbols_with_live_squareoff_order(kite: Any) -> set[str]:
    return {
        str(o.get("tradingsymbol"))
        for o in kite.orders()
        if _is_ours(o) and str(o.get("status", "")).upper() not in _TERMINAL
    }


def run_squareoff(
    kite: Any,
    *,
    products: frozenset[str] = frozenset({"MIS"}),
    live: bool,
    market_protection: int = 2,
    retries: int = 2,
    settle_sec: float = 2.0,
    sleep: Callable[[float], None] = _time.sleep,
) -> SquareOffReport:
    """Cancel every open order, then market-exit every open position.

    Only *products* are touched (default MIS); delivery holdings and GTTs are
    never cancelled or sold. Safe to run twice: it acts on whatever Kite says
    is open right now, and never stacks a second exit on a symbol that
    already has an unfinished square-off order.
    """
    report = SquareOffReport(live=live)
    retries = max(0, int(retries))

    cancel_errors: dict[str, str] = {}
    for _ in range(retries + 1):
        pending = _cancellable_orders(kite, products)
        if not pending:
            break
        if not live:
            report.cancelled = [_order_label(o) for o in pending]
            break
        for o in pending:
            try:
                kite.cancel_order(variety=o.get("variety") or "regular", order_id=o["order_id"])
                label = _order_label(o)
                if label not in report.cancelled:
                    report.cancelled.append(label)
                cancel_errors.pop(o["order_id"], None)
            except Exception as exc:  # noqa: BLE001
                cancel_errors[o["order_id"]] = f"{_order_label(o)} ({exc})"
                logger.warning("Cancel failed for %s: %s", o.get("order_id"), exc)
        sleep(settle_sec)
    if live:
        report.cancel_failed = [
            cancel_errors.get(o["order_id"], _order_label(o))
            for o in _cancellable_orders(kite, products)
        ]

    place_errors: dict[str, str] = {}
    for _ in range(retries + 1):
        positions = _open_positions(kite, products)
        if not positions:
            break
        if not live:
            report.closed = [
                f"{p['tradingsymbol']} {'SELL' if int(p['quantity']) > 0 else 'BUY'} "
                f"{abs(int(p['quantity']))}"
                for p in positions
            ]
            break
        busy = _symbols_with_live_squareoff_order(kite)
        for p in positions:
            symbol = str(p["tradingsymbol"])
            if symbol in busy:
                continue
            qty = int(p["quantity"])
            side = "SELL" if qty > 0 else "BUY"
            try:
                kite.place_order(
                    variety="regular",
                    exchange=p.get("exchange") or "NSE",
                    tradingsymbol=symbol,
                    transaction_type=side,
                    quantity=abs(qty),
                    product=str(p["product"]).upper(),
                    order_type="MARKET",
                    market_protection=market_protection,
                    tag=TAG,
                )
                place_errors.pop(symbol, None)
                report.closed.append(f"{symbol} {side} {abs(qty)}")
            except Exception as exc:  # noqa: BLE001
                place_errors[symbol] = f"{symbol} {side} {abs(qty)} ({exc})"
                logger.error("Square-off order failed for %s: %s", symbol, exc)
        sleep(settle_sec)

    if live:
        left = _open_positions(kite, products)
        report.remaining = [f"{p['tradingsymbol']} {int(p['quantity']):+d}" for p in left]
        report.remaining_symbols = {str(p["tradingsymbol"]) for p in left}
        report.close_failed = [
            msg for sym, msg in place_errors.items() if sym in report.remaining_symbols
        ]
    return report


Action = Literal["idle", "run", "missed"]


class SquareOffSchedule:
    """Run the square-off once per IST day inside ``[at, latest]``.

    Progress lives in ``squareoff.json`` so a watcher restart inside the
    window neither skips the run nor repeats a finished one. A start after
    ``latest`` (but before the close) reports ``missed`` once instead of
    firing late — the broker's own auto square-off is about to take over.
    """

    def __init__(self, state_path: Path, *, at: time, latest: time) -> None:
        self.state_path = state_path
        self.at = at
        self.latest = latest

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def check(self, now: datetime) -> Action:
        ist = to_ist(now)
        if ist.weekday() >= 5:
            return "idle"
        if self._load().get("date") == ist.date().isoformat():
            return "idle"
        t = ist.time()
        if t < self.at or t >= MARKET_CLOSE:
            return "idle"
        return "run" if t <= self.latest else "missed"

    def mark(self, now: datetime, status: str, detail: str = "") -> None:
        ist = to_ist(now)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {"date": ist.date().isoformat(), "status": status, "detail": detail,
                 "at": ist.isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )
