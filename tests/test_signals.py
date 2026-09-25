from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import nse_alert.cli as cli_module
from nse_alert.config import Settings
from nse_alert.feed import KiteFeed
from nse_alert.orders import OrderBook, OrderExecutor, OrderRequest
from nse_alert.signals import (
    HIGH_52W,
    IST,
    LOW_52W,
    VOLUME_SPIKE,
    BreakoutDetector,
    SignalMonitor,
    SymbolContext,
    VolumeSpikeDetector,
    candle_start,
    context_from_daily,
    format_signal_message,
    parse_timeframes,
)
from nse_alert.universe import Instrument

DAY = date(2026, 9, 24)


def _ist(h: int, m: int, s: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=IST)


def _history(n: int = 21, volume: int = 1000) -> list[tuple[datetime, int]]:
    """n prior-session 5m candles, oldest first."""
    base = datetime(2026, 9, 23, 9, 15, tzinfo=IST)
    return [(base + timedelta(minutes=5 * i), volume) for i in range(n)]


# --- candle bucketing -------------------------------------------------------


def test_candle_start_aligns_to_0915_open() -> None:
    assert candle_start(_ist(9, 15), 5) == _ist(9, 15)
    assert candle_start(_ist(10, 7, 30), 5) == _ist(10, 5)
    assert candle_start(_ist(10, 7, 30), 15) == _ist(10, 0)
    # 60m candles are 09:15, 10:15, … like Kite's
    assert candle_start(_ist(10, 20), 60) == _ist(10, 15)


def test_candle_start_none_outside_session() -> None:
    assert candle_start(_ist(9, 14, 59), 5) is None
    assert candle_start(_ist(15, 30), 5) is None


def test_parse_timeframes() -> None:
    assert parse_timeframes("15, 5") == [5, 15]
    assert parse_timeframes("5") == [5]
    with pytest.raises(ValueError):
        parse_timeframes("7")


# --- daily context ----------------------------------------------------------


def test_context_from_daily_excludes_today() -> None:
    idx = pd.to_datetime(["2025-06-01", "2026-09-22", "2026-09-23", "2026-09-24"])
    df = pd.DataFrame(
        {
            "open": [1, 1, 1, 1],
            "high": [999.0, 110.0, 105.0, 500.0],  # 2025-06 is > 52w ago; today excluded
            "low": [1.0, 90.0, 95.0, 5.0],
            "close": [100.0, 100.0, 102.0, 300.0],
            "volume": [1, 1, 1, 1],
        },
        index=idx,
    )
    ctx = context_from_daily(df, today=DAY)
    assert ctx is not None
    assert ctx.prev_close == 102.0
    assert ctx.prev_day_change_pct == pytest.approx(2.0)
    assert ctx.high_52w == 110.0
    assert ctx.low_52w == 90.0


# --- volume spikes ----------------------------------------------------------


def _detector(**kw: Any) -> VolumeSpikeDetector:
    det = VolumeSpikeDetector(timeframes=[5], ema_period=21, mult=2.0, **kw)
    det.seed("AAA", 5, _history(), now=_ist(10, 0, 10))
    return det


def test_volume_spike_fires_on_closed_candle_above_mult() -> None:
    det = _detector()
    assert det.on_tick("AAA", 50_000, _ist(10, 0, 10)) == []  # joined mid-candle
    assert det.on_tick("AAA", 51_000, _ist(10, 5, 1)) == []  # partial 10:00 skipped
    assert det.on_tick("AAA", 53_000, _ist(10, 9, 59)) == []
    spikes = det.on_tick("AAA", 53_100, _ist(10, 10, 2))
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.start == _ist(10, 5)
    assert spike.volume == 3_000  # 53,000 − 50,000 (last tick of prior candle)
    assert spike.ema == pytest.approx(1000.0)
    assert spike.mult == pytest.approx(3.0)


def test_volume_below_mult_does_not_fire() -> None:
    det = _detector()
    det.on_tick("AAA", 50_000, _ist(10, 0, 10))
    det.on_tick("AAA", 50_000, _ist(10, 5, 1))
    assert det.on_tick("AAA", 51_900, _ist(10, 10, 2)) == []  # 1.9x


def test_unseeded_symbol_never_fires() -> None:
    det = VolumeSpikeDetector(timeframes=[5])
    det.on_tick("BBB", 0, _ist(10, 0, 10))
    det.on_tick("BBB", 0, _ist(10, 5, 1))
    assert det.on_tick("BBB", 1_000_000, _ist(10, 10, 2)) == []


def test_opening_candle_skipped_by_default() -> None:
    for skip, expected in ((True, 0), (False, 1)):
        det = VolumeSpikeDetector(
            timeframes=[5], ema_period=21, mult=2.0, skip_opening_candle=skip
        )
        det.seed("AAA", 5, _history(), now=_ist(9, 0))
        det.on_tick("AAA", 10_000, _ist(9, 16))  # opening candle starts from 0
        assert len(det.on_tick("AAA", 10_100, _ist(9, 20, 1))) == expected


def test_gap_in_ticks_skips_the_candle_after_the_gap() -> None:
    det = _detector()
    det.on_tick("AAA", 50_000, _ist(10, 0, 10))
    det.on_tick("AAA", 50_000, _ist(10, 5, 1))
    # feed dropped 10:10–10:20; next tick lands in 10:20 with lots of backlog
    det.on_tick("AAA", 90_000, _ist(10, 20, 1))
    assert det.on_tick("AAA", 90_100, _ist(10, 25, 1)) == []


def test_seed_ignores_forming_candle() -> None:
    det = VolumeSpikeDetector(timeframes=[5], ema_period=2)
    det.on_tick("AAA", 0, _ist(10, 0, 10))
    det.seed(
        "AAA",
        5,
        [(_ist(9, 50), 100), (_ist(9, 55), 100), (_ist(10, 0), 999_999)],
    )
    assert det.is_armed("AAA", 5)
    st = det._states[("AAA", 5)]  # noqa: SLF001
    assert st.ema == pytest.approx(100.0)


# --- 52w breakout -----------------------------------------------------------


def test_breakout_fires_once_and_persists(tmp_path: Path) -> None:
    ctx = SymbolContext(prev_close=100.0, prev_day_change_pct=1.0, high_52w=110.0, low_52w=80.0)
    path = tmp_path / "signals.json"
    det = BreakoutDetector(path)
    assert det.check("AAA", 109.0, ctx) == []
    assert det.check("AAA", 110.5, ctx) == [(HIGH_52W, 110.0)]
    assert det.check("AAA", 112.0, ctx) == []
    # restart the same day: still deduped
    assert BreakoutDetector(path).check("AAA", 115.0, ctx) == []
    assert BreakoutDetector(path).check("AAA", 79.0, ctx) == [(LOW_52W, 80.0)]


# --- monitor + message ------------------------------------------------------


def test_monitor_emits_volume_signal_with_all_fields(tmp_path: Path) -> None:
    mon = SignalMonitor(
        prev_closes={"AAA": 100.0},
        state_path=tmp_path / "signals.json",
        timeframes=[5],
        breakout_enabled=False,
        fo_symbols={"AAA"},
    )
    mon.set_context("AAA", SymbolContext(100.0, -1.25, 130.0, 70.0))
    mon.volume.seed("AAA", 5, _history(), now=_ist(10, 0, 10))
    mon.on_tick("AAA", 101.0, 50_000, now=_ist(10, 0, 10))
    mon.on_tick("AAA", 102.0, 52_500, now=_ist(10, 9, 58))
    sigs = mon.on_tick("AAA", 103.5, 52_600, now=_ist(10, 10, 2))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.kind == VOLUME_SPIKE
    assert sig.day_change_pct == pytest.approx(3.5)

    text = format_signal_message(sig, offers=[("BUY", "A1B2C3"), ("SELL", "D4E5F6")])
    assert "*AAA*" in text
    assert "`2.50x`" in text  # volume multiplier
    assert "Price: `103.50`" in text
    assert "52w high: `130.00`" in text
    assert "Day: `+3.50%`" in text
    assert "Yesterday close: `100.00`" in text
    assert "Yesterday: `-1.25%`" in text
    assert "BUY `/confirm A1B2C3`" in text and "SELL `/confirm D4E5F6`" in text
    assert "F&O" in text


def test_monitor_emits_52w_signal(tmp_path: Path) -> None:
    mon = SignalMonitor(
        prev_closes={"AAA": 100.0},
        state_path=tmp_path / "signals.json",
        timeframes=[5],
        volume_enabled=False,
    )
    mon.set_context("AAA", SymbolContext(100.0, 0.5, 104.0, 70.0))
    sigs = mon.on_tick("AAA", 105.0, now=_ist(11, 0))
    assert [s.kind for s in sigs] == [HIGH_52W]
    text = format_signal_message(sigs[0])
    assert "52W HIGH BREAKOUT" in text and "104.00" in text and "52w low: `70.00`" in text


# --- feed -------------------------------------------------------------------


def test_kite_feed_quote_mode_passes_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ticker:
        MODE_LTP = "ltp"
        MODE_QUOTE = "quote"

        def __init__(self, *_a: Any) -> None:
            self.mode = None

        def subscribe(self, _t: list[int]) -> None:
            pass

        def set_mode(self, mode: str, _t: list[int]) -> None:
            self.mode = mode

        def connect(self, threaded: bool = True) -> None:
            pass

    monkeypatch.setattr("kiteconnect.KiteTicker", _Ticker)
    seen: list[tuple[Any, ...]] = []
    feed = KiteFeed(
        api_key="k",
        access_token="t",
        instruments=[Instrument("AAA", 1, "A", 100.0, 100.0, 50.0)],
        on_tick=lambda *a, **kw: seen.append((a, kw)),
        mode="quote",
    )
    feed.start()
    ws = feed._ticker  # noqa: SLF001
    ws.on_connect(ws, None)
    assert ws.mode == "quote"
    ws.on_ticks(ws, [{"instrument_token": 1, "last_price": 101.0, "volume_traded": 1234}])
    assert seen == [(("AAA", 101.0), {"volume": 1234})]


# --- orders -----------------------------------------------------------------


def _executor(tmp_path: Path) -> OrderExecutor:
    return OrderExecutor(mode="dry_run", book=OrderBook(tmp_path / "orders.json"))


def _req(side: str, qty: int = 5) -> OrderRequest:
    return OrderRequest(
        symbol="AAA",
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        product="MIS",
        order_type="MARKET",
        price=None,
        trigger_price=None,
        market_protection=2,
        tag="nsesig",
        reason="test",
    )


def test_protected_short_gets_buy_stop_above_entry(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    entry, sl = ex.place_entry_with_stop(_req("SELL"), entry_ltp=100.0, protect_short=True)
    assert entry.ok and sl is not None and sl.ok
    assert sl.request.side == "BUY"
    assert sl.request.trigger_price == 102.0
    assert sl.request.price == 102.10
    pos = ex.book.get_position("AAA")  # type: ignore[union-attr]
    assert pos is not None and pos["side"] == "SELL"
    # long-only managers leave shorts alone
    assert ex.manage_open_stops("AAA", 110.0) is None
    assert ex.manage_upper_circuit_exit("AAA", 130.0, 100.0) is None
    # a second entry on the same symbol is refused either way
    again, _ = ex.place_entry_with_stop(_req("SELL"), entry_ltp=99.0, protect_short=True)
    assert not again.ok and "Open position" in again.message


def test_confirm_signal_offer_sizes_at_live_ltp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    settings = Settings()
    book = OrderBook(tmp_path / "orders.json")
    ex = OrderExecutor(
        mode="dry_run", book=book, margin_budget_inr=10_000, fallback_leverage=5
    )
    pending = book.add_pending(
        _req("BUY", qty=0),
        alert_symbol="AAA",
        alert_threshold=0.0,
        alert_direction=VOLUME_SPIKE,
        entry_ltp=100.0,
        source="signal",
    )
    cli_module._confirm_pending(  # noqa: SLF001
        settings, ex, book, pending.id, None, ltp_lookup=lambda _s: 250.0
    )
    assert book.get_pending(pending.id).status == "confirmed"  # type: ignore[union-attr]
    pos = book.get_position("AAA")
    assert pos is not None
    assert pos["entry_price"] == 250.0
    assert pos["quantity"] == 200  # 10k × 5x / ₹250 (offline fallback sizing)
