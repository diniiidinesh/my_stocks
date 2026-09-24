from __future__ import annotations

from datetime import datetime, timezone

from nse_alert.engine import Alert
from nse_alert.notify.telegram import (
    _format_telegram_alert,
    _format_telegram_alert_group,
    format_ist_clock,
)


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


def test_same_tick_thresholds_combine_into_one_message() -> None:
    fired_at = datetime(2026, 9, 24, 3, 39, 14, tzinfo=timezone.utc)
    alerts = [
        Alert(
            symbol="CHOLAFIN",
            ltp=1630.0,
            prev_close=1774.0,
            change_pct=-8.12,
            direction="DOWN",
            threshold_pct=threshold,
            fired_at=fired_at,
            is_fno=True,
        )
        for threshold in (4.0, 7.0)
    ]
    text = _format_telegram_alert_group(alerts)
    # One message naming both crossed thresholds, not two separate messages.
    assert text.count("CHOLAFIN") == 1
    assert "±4%" in text and "±7%" in text
