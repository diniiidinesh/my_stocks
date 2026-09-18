from __future__ import annotations

from nse_alert.confirm_bot import TelegramConfirmListener


def _listener(alerts: list[str]) -> TelegramConfirmListener:
    return TelegramConfirmListener(
        bot_token="x",
        chat_id="123",
        on_confirm=lambda pid: None,
        on_health_alert=alerts.append,
        poll_sec=1.0,
    )


def test_no_alert_before_threshold() -> None:
    alerts: list[str] = []
    listener = _listener(alerts)
    listener._record_failure("409 Conflict")  # noqa: SLF001
    listener._record_failure("409 Conflict")  # noqa: SLF001
    assert alerts == []


def test_alert_fires_exactly_once_on_third_consecutive_failure() -> None:
    alerts: list[str] = []
    listener = _listener(alerts)
    for _ in range(6):
        listener._record_failure("409 Conflict")  # noqa: SLF001
    assert len(alerts) == 1
    assert "Confirm listener is down" in alerts[0]
    assert "409 Conflict" in alerts[0]


def test_alert_names_the_cause() -> None:
    alerts: list[str] = []
    listener = _listener(alerts)
    for _ in range(3):
        listener._record_failure(  # noqa: SLF001
            "409 Conflict: terminated by other getUpdates request"
        )
    assert "getUpdates request" in alerts[0]


def test_success_resets_state_and_alert_can_fire_again() -> None:
    alerts: list[str] = []
    listener = _listener(alerts)
    for _ in range(3):
        listener._record_failure("boom")  # noqa: SLF001
    assert len(alerts) == 1

    listener._record_success()  # noqa: SLF001
    assert listener._consecutive_failures == 0  # noqa: SLF001
    assert listener._alerted is False  # noqa: SLF001

    for _ in range(3):
        listener._record_failure("boom again")  # noqa: SLF001
    assert len(alerts) == 2


def test_backoff_grows_and_is_capped(monkeypatch) -> None:
    alerts: list[str] = []
    listener = _listener(alerts)
    listener.poll_sec = 2.0
    delays = [listener._record_failure("boom") for _ in range(20)]  # noqa: SLF001
    # First two failures: normal poll_sec, no backoff yet.
    assert delays[0] == 2.0
    assert delays[1] == 2.0
    # From the 3rd failure onward, backoff grows monotonically...
    assert delays[3] > delays[2]
    # ...but never exceeds the cap, which is reached well before 20 failures.
    assert all(d <= TelegramConfirmListener.MAX_BACKOFF_SEC for d in delays)
    assert delays[-1] == TelegramConfirmListener.MAX_BACKOFF_SEC


def test_alert_callback_exception_does_not_propagate() -> None:
    def _boom(msg: str) -> None:
        raise RuntimeError("telegram is also down")

    listener = TelegramConfirmListener(
        bot_token="x", chat_id="123", on_confirm=lambda pid: None, on_health_alert=_boom
    )
    for _ in range(3):
        listener._record_failure("boom")  # noqa: SLF001
    # Must not raise — alerting failures are never allowed to crash the poller.


def test_no_health_alert_callback_is_fine() -> None:
    listener = TelegramConfirmListener(
        bot_token="x", chat_id="123", on_confirm=lambda pid: None
    )
    for _ in range(5):
        listener._record_failure("boom")  # noqa: SLF001
    # Must not raise even with on_health_alert=None.
