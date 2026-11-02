from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from nse_alert import ema_trail
from nse_alert.ema_trail import IST


def _rows(start: datetime, closes: list[float]) -> list[dict]:
    return [
        {"date": start + timedelta(minutes=15 * i), "close": c}
        for i, c in enumerate(closes)
    ]


def test_closed_bucket_key_changes_once_per_candle_after_settle() -> None:
    nine_45 = int(datetime(2026, 11, 2, 9, 45, tzinfo=IST).timestamp())
    ten = int(datetime(2026, 11, 2, 10, 0, tzinfo=IST).timestamp())
    assert ema_trail.closed_bucket_key(datetime(2026, 11, 2, 10, 0, 10, tzinfo=IST)) == nine_45
    assert ema_trail.closed_bucket_key(datetime(2026, 11, 2, 10, 0, 25, tzinfo=IST)) == ten
    assert ema_trail.closed_bucket_key(datetime(2026, 11, 2, 10, 14, 59, tzinfo=IST)) == ten


def test_closed_closes_drops_forming_candle_and_sorts() -> None:
    now = datetime(2026, 11, 2, 10, 5, tzinfo=IST)
    rows = _rows(datetime(2026, 11, 2, 9, 15, tzinfo=IST), [100, 101, 102, 103, 50])
    assert ema_trail.closed_closes(list(reversed(rows)), now) == [100, 101, 102]
    paper = [{"time": int(r["date"].timestamp()), "c": r["close"]} for r in rows]
    assert ema_trail.closed_closes(paper, now) == [100, 101, 102]


def test_ema_matches_pandas_and_needs_period_candles() -> None:
    closes = [100, 102, 101, 105, 107, 106, 110]
    expected = pd.Series(closes, dtype=float).ewm(span=5, adjust=False).mean().iloc[-1]
    assert ema_trail.ema_last(closes, 5) == pytest.approx(expected)
    assert ema_trail.ema_last(closes[:4], 5) is None


def test_ema_stop_sits_buffer_below_ema_on_tick() -> None:
    closes = [200.0] * 6
    assert ema_trail.ema_stop(closes, period=5, buffer_pct=0.3) == 199.4
    assert ema_trail.ema_stop(closes, period=5, buffer_pct=0) == 200.0
