from __future__ import annotations

from datetime import datetime, timezone

from nse_alert.engine import Alert
from nse_alert.notify.telegram import _format_telegram_alert, format_ist_clock


def test_format_ist_clock_converts_utc() -> None:
    # 04:00 UTC = 09:30 IST
    dt = datetime(2026, 9, 17, 4, 0, 0, tzinfo=timezone.utc)
    assert format_ist_clock(dt) == "2026-09-17 09:30:00"


def test_format_ist_clock_naive_treated_as_utc() -> None:
    dt = datetime(2026, 9, 17, 4, 0, 0)
    assert format_ist_clock(dt) == "2026-09-17 09:30:00"


def test_telegram_alert_label_is_ist() -> None:
    alert = Alert(
        symbol="INFY",
        ltp=114.0,
        prev_close=100.0,
        change_pct=14.0,
        direction="UP",
        threshold_pct=13.0,
        fired_at=datetime(2026, 9, 17, 4, 0, 0, tzinfo=timezone.utc),
    )
    text = _format_telegram_alert(alert)
    assert "Time (IST): 2026-09-17 09:30:00" in text
    assert "Time (UTC)" not in text
    assert "INFY" in text
