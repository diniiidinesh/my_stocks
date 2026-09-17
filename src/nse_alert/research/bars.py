"""Kite historical candles with chunking for intraday intervals."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from nse_alert.screener.history import _normalize_ohlcv, load_cached_history, save_cached_history

logger = logging.getLogger(__name__)

# Max calendar-day span per Kite historical_data call (per interval).
_MAX_SPAN_DAYS: dict[str, int] = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}


def _chunk_ranges(
    start: date,
    end: date,
    *,
    max_span_days: int,
) -> list[tuple[date, date]]:
    """Inclusive calendar ranges that fit Kite's per-call span limit."""
    if end < start:
        return []
    out: list[tuple[date, date]] = []
    cursor = start
    # Keep a 1-day safety margin under the documented max.
    step = max(1, max_span_days - 1)
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=step - 1), end)
        out.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return out


def fetch_kite_bars(
    kite: Any,
    *,
    instrument_token: int,
    start: date,
    end: date,
    interval: str = "5minute",
    sleep_sec: float = 0.35,
) -> pd.DataFrame:
    """Fetch OHLCV bars for ``[start, end]``, chunking as needed."""
    span = _MAX_SPAN_DAYS.get(interval, 100)
    frames: list[pd.DataFrame] = []
    for chunk_start, chunk_end in _chunk_ranges(start, end, max_span_days=span):
        raw = kite.historical_data(
            instrument_token,
            datetime.combine(chunk_start, datetime.min.time()).replace(
                tzinfo=timezone.utc
            ),
            datetime.combine(chunk_end, datetime.max.time()).replace(
                tzinfo=timezone.utc
            ),
            interval,
            continuous=False,
            oi=False,
        )
        time.sleep(sleep_sec)
        if not raw:
            continue
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date")
        frames.append(_normalize_ohlcv(df))
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]


def get_intraday_bars(
    kite: Any,
    *,
    symbol: str,
    instrument_token: int,
    start: date,
    end: date,
    cache_dir: Path,
    interval: str = "5minute",
    sleep_sec: float = 0.35,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Load 5-minute (etc.) bars with on-disk cache under ``cache_dir``."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol.upper()}_{interval}_{start.isoformat()}_{end.isoformat()}.csv"
    if not force_refresh:
        cached = load_cached_history(path)
        if cached is not None and not cached.empty:
            return cached

    df = fetch_kite_bars(
        kite,
        instrument_token=instrument_token,
        start=start,
        end=end,
        interval=interval,
        sleep_sec=sleep_sec,
    )
    if not df.empty:
        save_cached_history(path, df)
    return df
