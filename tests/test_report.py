from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nse_alert.engine import Alert, AlertEngine
from nse_alert.report import build_day_report, format_day_report, load_events


def test_engine_persists_events_with_timestamps(tmp_path: Path) -> None:
    state = tmp_path / "fired.json"
    engine = AlertEngine(
        prev_closes={"AAA": 100.0},
        thresholds=[4, 7, 11],
        state_path=state,
    )
    engine.on_tick("AAA", 105.0)
    engine.on_tick("AAA", 112.0)
    events = load_events(state)
    assert [e.threshold_pct for e in events] == [4.0, 7.0, 11.0]
    assert all(e.symbol == "AAA" and e.direction == "UP" for e in events)


def test_day_report_counts_and_gaps() -> None:
    t0 = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
    events = [
        Alert("AAA", 105, 100, 5.0, "UP", 4.0, t0),
        Alert("AAA", 108, 100, 8.0, "UP", 7.0, t0 + timedelta(minutes=12)),
        Alert("AAA", 112, 100, 12.0, "UP", 11.0, t0 + timedelta(minutes=40)),
        Alert("BBB", 86, 100, -14.0, "DOWN", 11.0, t0 + timedelta(hours=1)),
    ]
    report = build_day_report(events, report_date=date(2026, 9, 15))
    assert report.counts_by_threshold == {4.0: 1, 7.0: 1, 11.0: 2}
    assert report.unique_symbols == 2
    assert len(report.multi_level) == 1
    assert len(report.gaps) == 2
    assert report.gaps[0].gap == timedelta(minutes=12)
    assert report.gaps[1].gap == timedelta(minutes=28)

    text = format_day_report(report)
    assert "±4%  →  1" in text
    assert "±7%  →  1" in text
    assert "±11%  →  2" in text
    assert "AAA UP" in text
    assert "12m" in text
