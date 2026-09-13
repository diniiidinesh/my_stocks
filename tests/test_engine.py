from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nse_alert.engine import AlertEngine
from nse_alert.notify import ConsoleNotifier, build_notifier
from nse_alert.universe import Instrument, build_universe


def test_alert_engine_fires_once(tmp_path: Path) -> None:
    state = tmp_path / "fired.json"
    engine = AlertEngine(
        prev_closes={"AAA": 100.0},
        threshold_pct=13.0,
        state_path=state,
    )
    assert engine.on_tick("AAA", 112.0) is None
    alert = engine.on_tick("AAA", 114.0)
    assert alert is not None
    assert alert.direction == "UP"
    assert alert.change_pct == pytest.approx(14.0)
    assert engine.on_tick("AAA", 120.0) is None  # deduped
    assert state.exists()
    data = state.read_text(encoding="utf-8")
    assert date.today().isoformat() in data
    assert "AAA" in data


def test_alert_engine_down_move(tmp_path: Path) -> None:
    engine = AlertEngine(
        prev_closes={"BBB": 100.0},
        threshold_pct=13.0,
        state_path=tmp_path / "fired.json",
    )
    alert = engine.on_tick("BBB", 86.0)
    assert alert is not None
    assert alert.direction == "DOWN"
    assert alert.change_pct == pytest.approx(-14.0)


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
