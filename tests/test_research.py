from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from nse_alert.engine import Alert
from nse_alert.research.analyze import analyze_local_events, load_events_for_days
from nse_alert.research.bars import _chunk_ranges
from nse_alert.research.strategies import STRATEGY_CATALOG, run_strategy
from nse_alert.research.synth import DayBars, synthesize_alerts_for_day
from nse_alert.research.runner import format_backtest_report, BacktestReport
from nse_alert.research.strategies import StrategyResult

IST = ZoneInfo("Asia/Kolkata")


def _intraday_frame(
    session: date,
    *,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> pd.DataFrame:
    start = datetime.combine(session, datetime.min.time()).replace(
        hour=9, minute=15, tzinfo=None
    )
    idx = [start + timedelta(minutes=5 * i) for i in range(len(closes))]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1e5] * len(closes)},
        index=pd.DatetimeIndex(idx),
    )


def test_chunk_ranges_respects_span() -> None:
    chunks = _chunk_ranges(date(2026, 1, 1), date(2026, 4, 15), max_span_days=100)
    assert chunks[0] == (date(2026, 1, 1), date(2026, 4, 9))
    assert chunks[-1][1] == date(2026, 4, 15)
    for a, b in chunks:
        assert (b - a).days + 1 <= 100


def test_synthesize_multi_level_up() -> None:
    session = date(2026, 9, 15)
    # prev=100; highs climb through 4, 7, 11
    bars = _intraday_frame(
        session,
        opens=[100, 103, 106, 110],
        highs=[101, 105, 108, 112],
        lows=[99, 102, 105, 109],
        closes=[100.5, 104, 107, 111],
    )
    day = DayBars("AAA", session, prev_close=100.0, bars=bars, is_fno=True)
    alerts = synthesize_alerts_for_day(
        day, thresholds=[4, 7, 11, 13], fo_only_thresholds={4.0}
    )
    assert [a.threshold_pct for a in alerts] == [4.0, 7.0, 11.0]
    assert all(a.direction == "UP" for a in alerts)
    assert alerts[0].ltp == pytest.approx(104.0)


def test_fo_only_skips_non_fno() -> None:
    session = date(2026, 9, 15)
    bars = _intraday_frame(
        session,
        opens=[100],
        highs=[108],
        lows=[99],
        closes=[105],
    )
    day = DayBars("BBB", session, prev_close=100.0, bars=bars, is_fno=False)
    alerts = synthesize_alerts_for_day(
        day, thresholds=[4, 7], fo_only_thresholds={4.0}
    )
    assert [a.threshold_pct for a in alerts] == [7.0]


def test_strategy_a1_stop_and_close() -> None:
    session = date(2026, 9, 15)
    # Alert at +13 → entry 113. Path dips to stop then recovers.
    bars = _intraday_frame(
        session,
        opens=[113, 112, 111, 114],
        highs=[114, 112.5, 111.5, 115],
        lows=[112, 110.5, 110.0, 113],
        closes=[113, 111, 110.5, 114],
    )
    alert = Alert(
        symbol="AAA",
        ltp=113.0,
        prev_close=100.0,
        change_pct=13.0,
        direction="UP",
        threshold_pct=13.0,
        fired_at=datetime(2026, 9, 15, 9, 15, tzinfo=IST),
    )
    result = run_strategy(
        STRATEGY_CATALOG["a1"],
        alerts=[alert],
        bars_by_symbol={"AAA": bars},
        close_by_symbol_day={( "AAA", session): 114.0},
        cost_bps_roundtrip=0.0,
    )
    assert result.n == 1
    assert result.trades[0].exit_reason == "stop"
    assert result.trades[0].pnl_pct == pytest.approx(-2.0)

    hold = run_strategy(
        STRATEGY_CATALOG["a_close_13"],
        alerts=[alert],
        bars_by_symbol={"AAA": bars},
        close_by_symbol_day={("AAA", session): 120.0},
        cost_bps_roundtrip=0.0,
    )
    assert hold.trades[0].pnl_pct == pytest.approx((120 / 113 - 1) * 100)


def test_analyze_local_fired_archive(tmp_path: Path) -> None:
    import json

    payload = {
        "date": "2026-09-15",
        "fired": ["AAA|UP|7"],
        "events": [
            {
                "symbol": "AAA",
                "ltp": 107.0,
                "prev_close": 100.0,
                "change_pct": 7.0,
                "direction": "UP",
                "threshold_pct": 7.0,
                "fired_at": "2026-09-15T05:00:00+00:00",
                "is_asm": False,
                "is_fno": True,
            }
        ],
    }
    path = tmp_path / "fired-2026-09-15.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    events = load_events_for_days(tmp_path, [date(2026, 9, 15)])
    analysis = analyze_local_events(events)
    assert analysis.unique_symbols == 1
    assert analysis.by_threshold_dir[(7.0, "UP")] == 1


def test_format_backtest_report_smoke() -> None:
    report = BacktestReport(
        start=date(2026, 8, 1),
        end=date(2026, 9, 15),
        interval="5minute",
        n_symbols=3,
        n_alerts=10,
        strategies=[StrategyResult("a1", "test")],
        cost_bps_roundtrip=10.0,
    )
    text = format_backtest_report(report)
    assert "a1" in text
    assert "Symbols with bars: 3" in text
