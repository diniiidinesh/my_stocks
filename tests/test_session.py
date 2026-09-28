from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

from nse_alert.session import IST, SessionClock
from nse_alert.signals import (
    SignalMonitor,
    SymbolContext,
    VolumeSpikeDetector,
    candle_start,
)


def _ist(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 24, h, m, s, tzinfo=IST)  # Thursday


CLOCK = SessionClock(fo_symbols={"FNO"})


def test_cas_stocks_close_at_1515_others_at_1530() -> None:
    assert CLOCK.continuous_close("FNO") == time(15, 15)
    assert CLOCK.continuous_close("CASH") == time(15, 30)
    assert CLOCK.in_continuous("FNO", _ist(15, 14, 59))
    assert not CLOCK.in_continuous("FNO", _ist(15, 15))
    assert CLOCK.in_continuous("CASH", _ist(15, 29))


def test_mis_entry_cutoffs() -> None:
    assert CLOCK.entries_open("FNO", _ist(15, 11))
    assert not CLOCK.entries_open("FNO", _ist(15, 12))
    assert CLOCK.entries_open("CASH", _ist(15, 24))
    assert not CLOCK.entries_open("CASH", _ist(15, 25))
    # CNC isn't squared off — allowed until the continuous close
    assert CLOCK.entries_open("FNO", _ist(15, 13), product="CNC")
    assert not CLOCK.entries_open("FNO", _ist(15, 15), product="CNC")
    # weekend
    assert not CLOCK.entries_open("CASH", _ist(11, 0) + timedelta(days=2))


def test_unknown_fo_list_uses_earlier_cas_times_for_everyone() -> None:
    clock = SessionClock(fo_known=False)
    assert clock.mis_cutoff("ANY") == time(15, 12)
    assert clock.continuous_close("ANY") == time(15, 15)


def test_candle_start_respects_per_symbol_close() -> None:
    assert candle_start(_ist(15, 16), 5, time(15, 15)) is None
    assert candle_start(_ist(15, 16), 5) == _ist(15, 15)


def _history() -> list[tuple[datetime, int]]:
    base = datetime(2026, 9, 23, 9, 15, tzinfo=IST)
    return [(base + timedelta(minutes=5 * i), 1000) for i in range(30)]


def test_last_candle_settles_at_close_without_auction_volume() -> None:
    det = VolumeSpikeDetector(timeframes=[5], close_for=CLOCK.continuous_close)
    det.seed("FNO", 5, _history(), now=_ist(15, 0))
    det.on_tick("FNO", 100_000, _ist(15, 5, 1))
    det.on_tick("FNO", 103_000, _ist(15, 10, 1))  # 15:10 candle opens at 100,000
    det.on_tick("FNO", 103_500, _ist(15, 14, 50))
    # First tick after 15:15 carries the closing-auction print (+500k):
    spikes = det.on_tick("FNO", 603_500, _ist(15, 31))
    assert len(spikes) == 1
    assert spikes[0].start == _ist(15, 10)
    assert spikes[0].volume == 3_500  # 103,500 − 100,000; auction's 500k excluded
    assert det.on_tick("FNO", 700_000, _ist(15, 40)) == []  # settled once


def test_seed_drops_auction_candles_for_cas_stock() -> None:
    det = VolumeSpikeDetector(timeframes=[5], ema_period=2, close_for=CLOCK.continuous_close)
    base = datetime(2026, 9, 23, 15, 5, tzinfo=IST)
    det.seed(
        "FNO",
        5,
        [(base, 100), (base + timedelta(minutes=5), 100), (base + timedelta(minutes=25), 10**7)],
        now=_ist(9, 0),
    )
    assert det._states[("FNO", 5)].ema == 100  # noqa: SLF001


def test_new_day_resets_cumulative_volume() -> None:
    det = VolumeSpikeDetector(timeframes=[5], close_for=CLOCK.continuous_close)
    det.seed("CASH", 5, _history(), now=_ist(9, 0))
    det.on_tick("CASH", 900_000, _ist(15, 20))
    next_day = _ist(9, 16) + timedelta(days=1)
    det.on_tick("CASH", 1_000, next_day)
    st = det._states[("CASH", 5)]  # noqa: SLF001
    assert st.last_cum == 1_000 and st.bucket_start_cum == 0


def test_52w_breakout_not_evaluated_during_closing_auction(tmp_path: Path) -> None:
    mon = SignalMonitor(
        prev_closes={"FNO": 100.0},
        state_path=tmp_path / "signals.json",
        timeframes=[5],
        volume_enabled=False,
        fo_symbols={"FNO"},
    )
    mon.set_context("FNO", SymbolContext(100.0, 0.0, 104.0, 70.0))
    assert mon.on_tick("FNO", 105.0, now=_ist(15, 20)) == []
    assert len(mon.on_tick("FNO", 105.0, now=_ist(15, 10))) == 1
