from __future__ import annotations

from typing import Any

from nse_alert.feed import KiteFeed
from nse_alert.universe import Instrument


class _FakeTicker:
    MODE_LTP = "ltp"

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None
        self.on_reconnect = None
        self.on_noreconnect = None
        self.subscribed: list[int] = []
        self.closed = False

    def subscribe(self, tokens: list[int]) -> None:
        self.subscribed = tokens

    def set_mode(self, _mode: str, _tokens: list[int]) -> None:
        pass

    def connect(self, threaded: bool = True) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _instruments() -> list[Instrument]:
    return [
        Instrument(
            symbol="AAA",
            instrument_token=1,
            name="A",
            last_price=100.0,
            prev_close=100.0,
            turnover_cr=50.0,
        )
    ]


def _feed(monkeypatch: Any, **kwargs: Any) -> KiteFeed:
    monkeypatch.setattr("kiteconnect.KiteTicker", _FakeTicker)
    feed = KiteFeed(
        api_key="k", access_token="t", instruments=_instruments(), on_tick=lambda s, p: None,
        **kwargs,
    )
    feed.start()
    return feed


def test_no_alert_before_reconnect_threshold(monkeypatch: Any) -> None:
    alerts: list[str] = []
    feed = _feed(monkeypatch, on_health_alert=alerts.append)
    feed._ticker.on_reconnect(feed._ticker, 1)  # noqa: SLF001
    feed._ticker.on_reconnect(feed._ticker, 2)  # noqa: SLF001
    assert alerts == []


def test_alert_fires_once_at_reconnect_threshold(monkeypatch: Any) -> None:
    alerts: list[str] = []
    feed = _feed(monkeypatch, on_health_alert=alerts.append, reconnect_alert_threshold=3)
    feed._ticker.on_close(feed._ticker, 403, "Invalid token")  # noqa: SLF001
    feed._ticker.on_reconnect(feed._ticker, 3)  # noqa: SLF001
    feed._ticker.on_reconnect(feed._ticker, 4)  # noqa: SLF001
    feed._ticker.on_reconnect(feed._ticker, 5)  # noqa: SLF001
    assert len(alerts) == 1
    assert "403" in alerts[0]
    assert "token" in alerts[0].lower()


def test_alert_resets_on_successful_reconnect(monkeypatch: Any) -> None:
    alerts: list[str] = []
    feed = _feed(monkeypatch, on_health_alert=alerts.append, reconnect_alert_threshold=3)
    feed._ticker.on_reconnect(feed._ticker, 3)  # noqa: SLF001
    assert len(alerts) == 1

    feed._ticker.on_connect(feed._ticker, {})  # noqa: SLF001 — connection recovered
    feed._ticker.on_reconnect(feed._ticker, 3)  # noqa: SLF001 — a fresh outage
    assert len(alerts) == 2


def test_feed_dead_callback_fires_on_noreconnect(monkeypatch: Any) -> None:
    dead_msgs: list[str] = []
    feed = _feed(monkeypatch, on_feed_dead=dead_msgs.append)
    feed._ticker.on_close(feed._ticker, 403, "Invalid token")  # noqa: SLF001
    feed._ticker.on_noreconnect(feed._ticker)  # noqa: SLF001
    assert len(dead_msgs) == 1
    assert "403" in dead_msgs[0]


def test_callback_exceptions_never_propagate(monkeypatch: Any) -> None:
    def _boom(msg: str) -> None:
        raise RuntimeError("telegram down too")

    feed = _feed(monkeypatch, on_health_alert=_boom, on_feed_dead=_boom, reconnect_alert_threshold=1)
    feed._ticker.on_reconnect(feed._ticker, 1)  # noqa: SLF001
    feed._ticker.on_noreconnect(feed._ticker)  # noqa: SLF001
    # Must not raise.


def test_no_callbacks_configured_is_fine(monkeypatch: Any) -> None:
    feed = _feed(monkeypatch)
    feed._ticker.on_reconnect(feed._ticker, 5)  # noqa: SLF001
    feed._ticker.on_noreconnect(feed._ticker)  # noqa: SLF001
    # Must not raise even with no callbacks set.
