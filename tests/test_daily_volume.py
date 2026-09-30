from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from nse_alert.session import IST
from nse_alert.signals import (
    DAILY_TF,
    SignalMonitor,
    SymbolContext,
    context_from_daily,
    format_signal_message,
    parse_min_ema,
    parse_tf_values,
    parse_timeframes,
)


def _ist(h: int, m: int, day: int = 24) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=IST)  # 24 Sep = Thursday


CTX = SymbolContext(100.0, 1.0, 130.0, 70.0, avg_daily_value=5e9, daily_volume_ema=1_000_000)


def _mon(tmp_path: Path, **kw) -> SignalMonitor:
    mon = SignalMonitor(
        prev_closes={"AAA": 100.0, "FNO": 100.0},
        state_path=tmp_path / "signals.json",
        timeframes=[DAILY_TF],
        breakout_enabled=False,
        fo_symbols={"FNO"},
        **kw,
    )
    mon.set_context("AAA", CTX)
    mon.set_context("FNO", CTX)
    return mon


def test_parse_daily_timeframe_and_per_tf_values() -> None:
    assert parse_timeframes("D") == [DAILY_TF]
    assert parse_timeframes("15, d, 5") == [5, 15, DAILY_TF]
    assert parse_tf_values("2.5") == (2.5, {})
    assert parse_tf_values("5:2.5,15:3,D:2") == (None, {5: 2.5, 15: 3.0, DAILY_TF: 2.0})
    assert parse_tf_values("2.5,D:2") == (2.5, {DAILY_TF: 2.0})
    assert parse_min_ema("D:1_000_000") == {DAILY_TF: 1_000_000.0}
    with pytest.raises(ValueError):
        parse_timeframes("W")
    with pytest.raises(ValueError):
        parse_min_ema("1000")


def test_daily_fires_once_when_running_volume_crosses(tmp_path: Path) -> None:
    mon = _mon(tmp_path, volume_mult=2.5)
    assert mon.on_tick("AAA", 101.0, 2_400_000, now=_ist(11, 0)) == []  # 2.4x
    [sig] = mon.on_tick("AAA", 103.0, 2_600_000, now=_ist(11, 5))  # 2.6x
    assert sig.timeframe_min == DAILY_TF
    assert sig.volume_mult == pytest.approx(2.6)
    assert sig.candle_volume == 2_600_000 and sig.volume_ema == 1_000_000
    assert mon.on_tick("AAA", 104.0, 5_000_000, now=_ist(13, 0)) == []  # once a day
    # restart the same day: still once
    assert _mon(tmp_path, volume_mult=2.5).on_tick("AAA", 104.0, 6_000_000, now=_ist(14, 0)) == []


def test_daily_uses_per_timeframe_multiplier(tmp_path: Path) -> None:
    mon = _mon(tmp_path, volume_mult=2.5, volume_mults={DAILY_TF: 2.0})
    assert len(mon.on_tick("AAA", 101.0, 2_100_000, now=_ist(11, 0))) == 1


def test_daily_ignores_auction_and_preopen_and_respects_floors(tmp_path: Path) -> None:
    mon = _mon(tmp_path, volume_mult=2.5)
    # F&O stock: after 15:15 the closing-auction print must not count
    assert mon.on_tick("FNO", 101.0, 9_000_000, now=_ist(15, 20)) == []
    # before 09:15 (pre-open) nothing either
    assert mon.on_tick("AAA", 101.0, 9_000_000, now=_ist(9, 10)) == []
    # EMA floor for D
    floored = _mon(tmp_path / "f", volume_mult=2.5, min_ema={DAILY_TF: 2_000_000})
    assert floored.on_tick("AAA", 101.0, 9_000_000, now=_ist(11, 0)) == []
    # market-cap floor
    capped = _mon(tmp_path / "c", volume_mult=2.5, min_market_cap_cr=1_000)
    capped.market_caps = {"AAA": 500.0}
    assert capped.on_tick("AAA", 101.0, 9_000_000, now=_ist(11, 0)) == []


def test_daily_needs_history(tmp_path: Path) -> None:
    mon = _mon(tmp_path, volume_mult=2.5)
    mon.set_context("AAA", SymbolContext(100.0, 1.0, 130.0, 70.0, 5e9, None))
    assert mon.on_tick("AAA", 101.0, 9_000_000, now=_ist(11, 0)) == []


def test_context_daily_volume_ema_excludes_today() -> None:
    idx = pd.bdate_range(end="2026-09-24", periods=40)  # last bar = "today"
    vols = [1_000_000.0] * 39 + [50_000_000.0]
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5, "volume": vols}, index=idx)
    ctx = context_from_daily(df, today=datetime(2026, 9, 24).date(), ema_period=21)
    assert ctx is not None and ctx.daily_volume_ema == pytest.approx(1_000_000.0)
    short = context_from_daily(df.tail(10), today=datetime(2026, 9, 24).date(), ema_period=21)
    assert short is not None and short.daily_volume_ema is None


def test_daily_message_text(tmp_path: Path) -> None:
    [sig] = _mon(tmp_path, volume_mult=2.5).on_tick("AAA", 103.0, 2_600_000, now=_ist(11, 5))
    text = format_signal_message(sig, offers=[("BUY", "A1B2C3")])
    assert "DAILY VOLUME SPIKE" in text
    assert "`2.60x` its daily EMA (`2,600,000` vs `1,000,000`)" in text
    assert "Yesterday close: `100.00`" in text and "52w high: `130.00`" in text


def test_daily_fires_again_next_day_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nse_alert.signals import BreakoutDetector

    day = {"v": "2026-09-24"}
    monkeypatch.setattr(BreakoutDetector, "_today", staticmethod(lambda: day["v"]))
    mon = _mon(tmp_path, volume_mult=2.5)
    assert len(mon.on_tick("AAA", 101.0, 3_000_000, now=_ist(11, 0))) == 1
    assert mon.on_tick("AAA", 101.0, 4_000_000, now=_ist(12, 0)) == []
    day["v"] = "2026-09-25"  # same process, past midnight
    assert len(mon.on_tick("AAA", 99.0, 3_000_000, now=_ist(10, 0, day=25))) == 1
