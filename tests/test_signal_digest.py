from __future__ import annotations

from datetime import datetime

from nse_alert.session import IST
from nse_alert.signals import (
    HIGH_52W,
    LOW_52W,
    VOLUME_SPIKE,
    Signal,
    SignalDigest,
    SymbolContext,
    format_signal_digest,
)


def _ist(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 24, h, m, s, tzinfo=IST)


CTX = SymbolContext(prev_close=100.0, prev_day_change_pct=-0.5, high_52w=104.0, low_52w=70.0)


def _sig(symbol: str, kind: str, at: datetime, **kw) -> Signal:
    return Signal(
        kind=kind, symbol=symbol, ltp=105.0, prev_close=100.0, fired_at=at, context=CTX, **kw
    )


def test_digest_releases_once_per_clock_window() -> None:
    d = SignalDigest(15)
    d.add(_sig("AAA", HIGH_52W, _ist(10, 2), level=104.0))
    d.add(_sig("BBB", LOW_52W, _ist(10, 14, 59), level=104.0))
    assert d.pop_due(_ist(10, 14, 59)) == []  # window 10:00–10:15 still open
    due = d.pop_due(_ist(10, 15, 1))
    assert [s.symbol for s in due] == ["AAA", "BBB"]
    assert d.slot_start(due[0].fired_at) == _ist(10, 0)
    assert d.pop_due(_ist(10, 30, 1)) == []  # nothing sent twice
    assert len(d) == 0


def test_digest_message_is_collapsed_and_complete() -> None:
    entries = [
        (_sig("M&M", HIGH_52W, _ist(10, 2), level=104.0, is_fno=True), [("BUY", "A1B2C3"), ("SELL", "D4E5F6")], None),
        (_sig("CCC", LOW_52W, _ist(10, 5), level=106.0), [], "No order offer: past MIS entry cutoff"),
        (_sig("VVV", VOLUME_SPIKE, _ist(10, 6), volume_mult=3.2, timeframe_min=5), [], None),
    ]
    [msg] = format_signal_digest(entries, start=_ist(10, 0), end=_ist(10, 15))
    head, quote = msg.split("<blockquote expandable>")
    assert "Signals 10:00–10:15 IST" in head
    assert "🚀 52W highs (1): M&amp;M +5.0%" in head  # HTML-escaped
    assert "🔻 52W lows (1): CCC" in head
    assert "📊 Volume spikes (1): VVV 3.2x" in head
    assert "/confirm" not in head  # details stay collapsed
    assert "<code>/confirm A1B2C3</code>" in quote and "<code>/confirm D4E5F6</code>" in quote
    assert "Broke <code>104.00</code> at <code>105.00</code> (+0.96%)" in quote
    assert "Yday close <code>100.00</code> (<code>-0.50%</code>)" in quote
    assert "past MIS entry cutoff" in quote
    assert quote.count("</blockquote>") == 1


def test_large_digest_splits_under_telegram_limit() -> None:
    entries = [
        (_sig(f"S{i:03d}", HIGH_52W, _ist(10, 2), level=104.0), [("BUY", "A1B2C3"), ("SELL", "D4E5F6")], None)
        for i in range(60)
    ]
    msgs = format_signal_digest(entries, start=_ist(10, 0), end=_ist(10, 15))
    assert len(msgs) > 1
    assert all(len(m) < 4096 for m in msgs)
    assert all(m.count("<blockquote expandable>") == 1 for m in msgs)
    assert sum(m.count("/confirm A1B2C3") for m in msgs) == 60
    assert "(1/" in msgs[0]
