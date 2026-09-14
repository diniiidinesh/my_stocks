from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_thresholds(raw: str | float | int | list[float] | tuple[float, ...]) -> list[float]:
    """Parse one or more positive thresholds from CLI/env input.

    Accepts ``13``, ``"13"``, ``"4,7,11"``, or ``[4, 7, 11]``.
    """
    if isinstance(raw, (int, float)):
        values = [float(raw)]
    elif isinstance(raw, (list, tuple)):
        values = [float(x) for x in raw]
    else:
        text = str(raw).strip()
        if not text:
            raise ValueError("At least one threshold is required")
        values = [float(part.strip()) for part in text.split(",") if part.strip()]
    cleaned = sorted({abs(v) for v in values if abs(v) > 0})
    if not cleaned:
        raise ValueError("At least one positive threshold is required")
    return cleaned


@dataclass(frozen=True, slots=True)
class Alert:
    symbol: str
    ltp: float
    prev_close: float
    change_pct: float
    direction: str
    threshold_pct: float
    fired_at: datetime


class AlertEngine:
    """Compute day % move and fire once per symbol/direction/threshold per day."""

    def __init__(
        self,
        *,
        prev_closes: dict[str, float],
        thresholds: list[float] | float,
        state_path: Path,
    ) -> None:
        self.prev_closes = prev_closes
        if isinstance(thresholds, (int, float)):
            self.thresholds = parse_thresholds(thresholds)
        else:
            self.thresholds = parse_thresholds(list(thresholds))
        self.state_path = state_path
        self._fired: set[str] = set()
        self._load_state()

    def _today_key(self) -> str:
        return date.today().isoformat()

    @staticmethod
    def _fired_key(symbol: str, direction: str, threshold: float) -> str:
        # Normalize so 4 and 4.0 collide in state.
        return f"{symbol}|{direction}|{threshold:g}"

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read alert state: %s", exc)
            return
        if data.get("date") != self._today_key():
            return
        self._fired = set(data.get("fired", []))

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"date": self._today_key(), "fired": sorted(self._fired)}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def on_tick(self, symbol: str, ltp: float) -> list[Alert]:
        """Return newly crossed threshold alerts for this tick (may be multiple)."""
        prev = self.prev_closes.get(symbol)
        if prev is None or prev <= 0 or ltp <= 0:
            return []
        change_pct = (ltp / prev - 1.0) * 100.0
        abs_move = abs(change_pct)
        if abs_move < self.thresholds[0]:
            return []

        direction = "UP" if change_pct > 0 else "DOWN"
        now = datetime.now(timezone.utc)
        alerts: list[Alert] = []
        for threshold in self.thresholds:
            if abs_move < threshold:
                break
            key = self._fired_key(symbol, direction, threshold)
            if key in self._fired:
                continue
            self._fired.add(key)
            alert = Alert(
                symbol=symbol,
                ltp=ltp,
                prev_close=prev,
                change_pct=change_pct,
                direction=direction,
                threshold_pct=threshold,
                fired_at=now,
            )
            alerts.append(alert)
            logger.info(
                "ALERT %s %s crossed ±%.4g%% (now %.2f%%) LTP=%.2f prev=%.2f",
                alert.direction,
                alert.symbol,
                alert.threshold_pct,
                alert.change_pct,
                alert.ltp,
                alert.prev_close,
            )

        if alerts:
            self._save_state()
        return alerts
