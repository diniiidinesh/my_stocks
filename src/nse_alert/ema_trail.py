"""Trail a long stop just under the EMA of *completed* 15-minute candles.

Shared by the live executor (Kite SL-Limit modify) and the paper desk. The
stop is recomputed once per closed 15-minute candle — never from the forming
candle — and only ever ratchets up; callers keep the higher of the existing
stop and the EMA stop.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nse_alert.orders import round_tick

IST = ZoneInfo("Asia/Kolkata")
INTERVAL = "15minute"
BUCKET_SEC = 15 * 60
# Wait this long after a candle closes so Kite's historical bar is final.
SETTLE_SEC = 20
# Calendar days of 15m history to fetch: enough candles to warm a short EMA
# across weekends and exchange holidays.
LOOKBACK_DAYS = 7


def _anchor(now: datetime) -> datetime:
    # Intraday 15m buckets start at 09:15 IST, matching exchange candles.
    return now.astimezone(IST).replace(hour=9, minute=15, second=0, microsecond=0)


def closed_bucket_key(now: datetime) -> int:
    """Epoch second at which the most recently *settled* 15m candle ended.

    Changes once per candle (SETTLE_SEC after its close); use it to run the
    trail exactly once per candle.
    """
    at = now - timedelta(seconds=SETTLE_SEC)
    anchor = int(_anchor(at).timestamp())
    return anchor + ((int(at.timestamp()) - anchor) // BUCKET_SEC) * BUCKET_SEC


def _ts(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.timestamp()
    return float(value)


def closed_closes(rows: Iterable[Mapping[str, Any]], now: datetime) -> list[float]:
    """Closes of candles whose 15-minute window has ended, oldest first.

    Accepts Kite ``historical_data`` rows (``date``/``close``) or paper-desk
    candles (``time``/``c``). The still-forming candle is dropped.
    """
    cutoff = now.timestamp()
    out: list[tuple[float, float]] = []
    for row in rows:
        start = _ts(row["date"] if "date" in row else row["time"])
        close = float(row["close"] if "close" in row else row["c"])
        if start + BUCKET_SEC <= cutoff and close > 0:
            out.append((start, close))
    out.sort()
    return [c for _, c in out]


def ema_last(closes: list[float], period: int) -> float | None:
    """Latest EMA value (seeded with the first close, like the chart/screener)."""
    if period < 1 or len(closes) < period:
        return None
    alpha = 2.0 / (period + 1)
    value = closes[0]
    for c in closes[1:]:
        value = alpha * c + (1 - alpha) * value
    return value


def ema_stop(closes: list[float], *, period: int, buffer_pct: float) -> float | None:
    """Stop trigger ``buffer_pct`` % below the EMA, rounded to the NSE tick."""
    value = ema_last(closes, period)
    if value is None:
        return None
    return round_tick(value * (1.0 - abs(buffer_pct) / 100.0))
