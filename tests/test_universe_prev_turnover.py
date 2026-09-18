"""Prior-session liquidity screen.

Regression cover for 2026-09-18: the watcher crash-looped before the open
because the liquidity screen used *today's* Kite quote volume, which is 0 until
the session starts, so every symbol was filtered out and `build_universe`
raised "Kite universe is empty after filters".
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from nse_alert import universe as u


class _FakeKite:
    """Minimal Kite stand-in: two symbols, pre-open (volume=0) quotes."""

    def instruments(self, exchange: str) -> list[dict]:
        assert exchange == "NSE"
        return [
            {
                "tradingsymbol": s,
                "instrument_token": t,
                "name": s,
                "segment": "NSE",
                "instrument_type": "EQ",
                "exchange": "NSE",
            }
            for s, t in (("RELIANCE", 111), ("TINYCO", 222))
        ]

    def quote(self, keys: list[str]) -> dict:
        # Pre-open: last_price carries yesterday's close, volume is 0.
        return {
            k: {"last_price": 1000.0, "volume": 0, "ohlc": {"close": 1000.0}}
            for k in keys
        }


def _build(monkeypatch, prev: dict[str, float] | None, **kw):
    monkeypatch.setattr(u, "_kite_client", lambda *a, **k: _FakeKite())
    monkeypatch.setattr(
        u, "load_prev_session_turnover_cr", lambda **_: dict(prev or {})
    )
    return u.build_universe(
        min_turnover_cr=25.0,
        min_price=20.0,
        kite_api_key="k",
        kite_access_token="t",
        **kw,
    )


def test_prev_session_turnover_survives_zero_live_volume(monkeypatch):
    """The bug: pre-open volume=0 must not empty the universe."""
    got = _build(monkeypatch, {"RELIANCE": 900.0, "TINYCO": 1.0})
    assert [i.symbol for i in got] == ["RELIANCE"]  # TINYCO below 25 cr
    assert got[0].turnover_cr == pytest.approx(900.0)


def test_live_volume_screen_empties_universe_before_open(monkeypatch):
    """Without prior-session data we fall back to live volume — and pre-open
    that still fails. Pins the old behaviour so the fallback stays honest."""
    with pytest.raises(RuntimeError, match="empty after filters"):
        _build(monkeypatch, {})


def test_error_message_points_at_the_liquidity_source(monkeypatch):
    with pytest.raises(RuntimeError, match="live volume, which is 0 before the open"):
        _build(monkeypatch, {})


def test_custom_universe_skips_liquidity_screen(monkeypatch, tmp_path):
    """A custom list must not be filtered, and must not fetch a bhavcopy."""
    path = tmp_path / "syms.txt"
    path.write_text("TINYCO\n")

    def _boom(**_):  # noqa: ANN003
        raise AssertionError("bhavcopy must not be fetched for a custom universe")

    monkeypatch.setattr(u, "load_prev_session_turnover_cr", _boom)
    monkeypatch.setattr(u, "_kite_client", lambda *a, **k: _FakeKite())
    got = u.build_universe(
        min_turnover_cr=25.0,
        min_price=20.0,
        kite_api_key="k",
        kite_access_token="t",
        custom_universe_file=str(path),
    )
    assert [i.symbol for i in got] == ["TINYCO"]


def test_loader_parses_lacs_to_crore_and_caches(monkeypatch, tmp_path):
    df = pd.DataFrame(
        {
            "symbol": ["RELIANCE", "TINYCO", "BADROW"],
            "traded_qty": [1, 1, 1],
            "delivery_qty": [1, 1, 1],
            "delivery_pct": [50.0, 50.0, 50.0],
            "turnover_lacs": ["90,000.5", "100", "n/a"],
        }
    )
    calls = []

    def _fake_fetch(day, **_):
        calls.append(day)
        return df

    monkeypatch.setattr(
        "nse_alert.screener.delivery.fetch_bhavcopy_day", _fake_fetch
    )
    out = u.load_prev_session_turnover_cr(
        state_dir=tmp_path, session_day=date(2026, 9, 17)
    )
    assert out == {"RELIANCE": pytest.approx(900.005), "TINYCO": pytest.approx(1.0)}
    assert "BADROW" not in out  # unparseable turnover dropped, not fatal

    cached = tmp_path / "universe" / "turnover-2026-09-17.json"
    assert json.loads(cached.read_text())["RELIANCE"] == pytest.approx(900.005)

    # second call is served from cache — no second fetch
    u.load_prev_session_turnover_cr(state_dir=tmp_path, session_day=date(2026, 9, 17))
    assert len(calls) == 1


def test_loader_returns_empty_when_bhavcopy_unavailable(monkeypatch, tmp_path):
    """A holiday / 404 / network error must degrade, never raise."""

    def _boom(day, **_):
        raise RuntimeError("404")

    monkeypatch.setattr("nse_alert.screener.delivery.fetch_bhavcopy_day", _boom)
    assert (
        u.load_prev_session_turnover_cr(
            state_dir=tmp_path, session_day=date(2026, 9, 17)
        )
        == {}
    )


def test_loader_handles_missing_turnover_column(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nse_alert.screener.delivery.fetch_bhavcopy_day",
        lambda day, **_: pd.DataFrame({"symbol": ["X"], "traded_qty": [1]}),
    )
    assert (
        u.load_prev_session_turnover_cr(
            state_dir=tmp_path, session_day=date(2026, 9, 17)
        )
        == {}
    )
