from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nse_alert.engine import AlertEngine, parse_thresholds
from nse_alert.notify import ConsoleNotifier, build_notifier
from nse_alert.universe import Instrument, build_universe


def test_parse_thresholds_csv() -> None:
    assert parse_thresholds("4,7,11") == [4.0, 7.0, 11.0]
    assert parse_thresholds("13") == [13.0]
    assert parse_thresholds(4) == [4.0]
    assert parse_thresholds([11, 4, 7, 4]) == [4.0, 7.0, 11.0]


def test_alert_engine_fires_once(tmp_path: Path) -> None:
    state = tmp_path / "fired.json"
    engine = AlertEngine(
        prev_closes={"AAA": 100.0},
        thresholds=13.0,
        state_path=state,
    )
    assert engine.on_tick("AAA", 112.0) == []
    alerts = engine.on_tick("AAA", 114.0)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.direction == "UP"
    assert alert.change_pct == pytest.approx(14.0)
    assert alert.threshold_pct == 13.0
    assert engine.on_tick("AAA", 120.0) == []  # deduped
    assert state.exists()
    data = state.read_text(encoding="utf-8")
    assert date.today().isoformat() in data
    assert "AAA|UP|13" in data


def test_alert_engine_multiple_thresholds(tmp_path: Path) -> None:
    engine = AlertEngine(
        prev_closes={"AAA": 100.0},
        thresholds=[4, 7, 11],
        state_path=tmp_path / "fired.json",
    )
    assert engine.on_tick("AAA", 103.0) == []
    first = engine.on_tick("AAA", 105.0)
    assert len(first) == 1
    assert first[0].threshold_pct == 4.0

    jumped = engine.on_tick("AAA", 112.0)
    assert [a.threshold_pct for a in jumped] == [7.0, 11.0]
    assert all(a.direction == "UP" for a in jumped)
    assert engine.on_tick("AAA", 120.0) == []


def test_alert_engine_down_move(tmp_path: Path) -> None:
    engine = AlertEngine(
        prev_closes={"BBB": 100.0},
        thresholds=13.0,
        state_path=tmp_path / "fired.json",
    )
    alerts = engine.on_tick("BBB", 86.0)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.direction == "DOWN"
    assert alert.change_pct == pytest.approx(-14.0)


def test_up_and_down_thresholds_are_independent(tmp_path: Path) -> None:
    engine = AlertEngine(
        prev_closes={"CCC": 100.0},
        thresholds=[4],
        state_path=tmp_path / "fired.json",
    )
    up = engine.on_tick("CCC", 105.0)
    assert len(up) == 1 and up[0].direction == "UP"
    down = engine.on_tick("CCC", 95.0)
    assert len(down) == 1 and down[0].direction == "DOWN"


def test_mock_universe_contains_demo() -> None:
    instruments = build_universe(
        min_turnover_cr=25.0,
        min_price=20.0,
        mock=True,
    )
    symbols = {i.symbol for i in instruments}
    assert "DEMO13" in symbols
    assert all(isinstance(i, Instrument) for i in instruments)


def test_build_notifier_console_only() -> None:
    notifier = build_notifier(always_console=True)
    assert isinstance(notifier, ConsoleNotifier)
