from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Alert:
    symbol: str
    ltp: float
    prev_close: float
    change_pct: float
    direction: str
    fired_at: datetime


class AlertEngine:
    """Compute day % move and fire at most once per symbol per calendar day."""

    def __init__(
        self,
        *,
        prev_closes: dict[str, float],
        threshold_pct: float,
        state_path: Path,
    ) -> None:
        self.prev_closes = prev_closes
        self.threshold_pct = abs(threshold_pct)
        self.state_path = state_path
        self._fired: set[str] = set()
        self._load_state()

    def _today_key(self) -> str:
        return date.today().isoformat()

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

    def on_tick(self, symbol: str, ltp: float) -> Alert | None:
        prev = self.prev_closes.get(symbol)
        if prev is None or prev <= 0 or ltp <= 0:
            return None
        change_pct = (ltp / prev - 1.0) * 100.0
        if abs(change_pct) < self.threshold_pct:
            return None
        if symbol in self._fired:
            return None
        self._fired.add(symbol)
        self._save_state()
        direction = "UP" if change_pct > 0 else "DOWN"
        alert = Alert(
            symbol=symbol,
            ltp=ltp,
            prev_close=prev,
            change_pct=change_pct,
            direction=direction,
            fired_at=datetime.now(timezone.utc),
        )
        logger.info(
            "ALERT %s %s %.2f%% LTP=%.2f prev=%.2f",
            alert.direction,
            alert.symbol,
            alert.change_pct,
            alert.ltp,
            alert.prev_close,
        )
        return alert
