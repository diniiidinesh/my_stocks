from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from click.testing import CliRunner

import nse_alert.cli as cli_module
from nse_alert.cli import main
from nse_alert.orders import OrderBook, OrderRequest


def test_cli_help_and_each_subcommand_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output

    for name in main.commands:
        sub_result = runner.invoke(main, [name, "--help"])
        assert sub_result.exit_code == 0, f"{name} --help failed: {sub_result.output}"


def test_size_command_constructs_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)

    runner = CliRunner()
    result = runner.invoke(main, ["size", "RELIANCE", "--price", "1234.5"])

    assert result.exit_code == 0, result.output
    assert "LTP used=1234.50" in result.output
    assert "qty=" in result.output


def test_watch_exits_and_alerts_on_bad_kite_token(
    tmp_path: Path, monkeypatch
) -> None:
    """watch must fail fast + alert on a bad token, not crash-loop deep inside
    universe-building (2026-09-17 incident: 13 silent restarts, no alert)."""
    from kiteconnect.exceptions import TokenException

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KITE_API_KEY", "bad-key")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "bad-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")

    sent: list[str] = []

    class _FakeKite:
        def profile(self) -> dict[str, Any]:
            raise TokenException("Incorrect `api_key` or `access_token`.")

    monkeypatch.setattr(cli_module, "_kite_client", lambda *a, **kw: _FakeKite())
    monkeypatch.setattr(
        cli_module.TelegramNotifier,
        "send_text",
        lambda self, msg, **kw: sent.append(msg),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--feed", "kite"])

    assert result.exit_code != 0
    assert len(sent) == 1
    assert "token" in sent[0].lower()
    assert "login" in sent[0].lower()


def test_watch_exits_nonzero_when_feed_dies_permanently(
    tmp_path: Path, monkeypatch
) -> None:
    """A feed that gives up reconnecting must not leave watch running with a
    dead socket that looks healthy to `docker ps` (2026-09-18 6h overnight
    silence, see docs/RCA-2026-09-17.md P5)."""
    from nse_alert.universe import Instrument

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")

    class _FakeKite:
        def profile(self) -> dict[str, Any]:
            return {"user_id": "AB1234"}

    fake_instruments = [
        Instrument(
            symbol="AAA",
            instrument_token=1,
            name="A",
            last_price=100.0,
            prev_close=100.0,
            turnover_cr=50.0,
        )
    ]

    class _DeadOnArrivalFeed:
        def __init__(self, **kwargs: Any) -> None:
            self._on_feed_dead = kwargs["on_feed_dead"]

        def start(self) -> None:
            self._on_feed_dead("🔌 feed dead in test")

        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "_kite_client", lambda *a, **kw: _FakeKite())
    monkeypatch.setattr(cli_module, "build_universe", lambda **kw: fake_instruments)
    monkeypatch.setattr(cli_module, "load_nfo_equity_underlyings", lambda kite: set())
    monkeypatch.setattr(cli_module, "load_asm_symbols", lambda **kw: set())
    monkeypatch.setattr(cli_module, "KiteFeed", _DeadOnArrivalFeed)

    sent: list[str] = []
    monkeypatch.setattr(
        cli_module.TelegramNotifier, "send_text", lambda self, msg, **kw: sent.append(msg)
    )

    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--feed", "kite"])

    assert result.exit_code == 1, result.output
    assert any("feed dead in test" in m for m in sent), sent


def test_watch_alerts_on_pre_expired_pending_order(tmp_path: Path, monkeypatch) -> None:
    """watch must notice and alert on an already-expired pending order on its
    first eligible tick, not only when someone happens to run `pending`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADE_MODE", "confirm")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)

    # Seed one already-expired pending order before watch starts.
    book = OrderBook(tmp_path / ".nse_alert" / "orders.json")
    req = OrderRequest(
        symbol="DEMO13",
        side="BUY",
        quantity=5,
        product="MIS",
        order_type="MARKET",
        price=None,
        trigger_price=None,
        market_protection=2,
        tag="t",
        reason="test",
    )
    pending = book.add_pending(
        req,
        alert_symbol="DEMO13",
        alert_threshold=13.0,
        alert_direction="UP",
        entry_ltp=100.0,
        ttl_minutes=30,
    )
    created = datetime.now(timezone.utc) - timedelta(minutes=31)
    book._data["pending"][pending.id]["created_at"] = created.isoformat()  # noqa: SLF001
    book._data["pending"][pending.id]["expires_at"] = (  # noqa: SLF001
        created + timedelta(minutes=30)
    ).isoformat()
    book._save()  # noqa: SLF001

    sent: list[str] = []
    monkeypatch.setattr(
        cli_module.TelegramNotifier, "send_text", lambda self, msg, **kw: sent.append(msg)
    )
    # The confirm listener would otherwise poll the real Telegram API with a
    # fake token; it's not what this test is exercising.
    monkeypatch.setattr(cli_module.TelegramConfirmListener, "start", lambda self: None)
    monkeypatch.setattr(cli_module.TelegramConfirmListener, "stop", lambda self: None)

    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--feed", "mock", "--max-ticks", "5"])

    # mock feed exits 1 when zero DEMO alerts fired in this run — unrelated
    # to the expiry alert this test is checking for.
    assert any("EXPIRED unconfirmed" in m for m in sent), sent
    assert any("DEMO13" in m for m in sent), sent
    assert any(pending.id in m for m in sent), sent


def test_in_ist_market_hours() -> None:
    from nse_alert.cli import _in_ist_market_hours

    # Wed 2026-09-16, 10:00 IST = 04:30 UTC.
    assert _in_ist_market_hours(datetime(2026, 9, 16, 4, 30, tzinfo=timezone.utc))
    # Wed, 09:14 IST (just before open) = 03:44 UTC.
    assert not _in_ist_market_hours(datetime(2026, 9, 16, 3, 44, tzinfo=timezone.utc))
    # Wed, 15:31 IST (just after close) = 10:01 UTC.
    assert not _in_ist_market_hours(datetime(2026, 9, 16, 10, 1, tzinfo=timezone.utc))
    # Sat 2026-09-19, 10:00 IST = 04:30 UTC — weekend.
    assert not _in_ist_market_hours(datetime(2026, 9, 19, 4, 30, tzinfo=timezone.utc))


def test_build_heartbeat_reflects_real_state(tmp_path: Path, monkeypatch) -> None:
    from datetime import date as date_cls

    from nse_alert.cli import _build_heartbeat
    from nse_alert.config import Settings
    from nse_alert.engine import Alert
    from nse_alert.report import build_day_report

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADE_MODE", "confirm")
    monkeypatch.setenv("FEED_MODE", "kite")
    settings = Settings()
    day = date_cls(2026, 9, 18)

    events = [
        Alert(
            symbol="AAA", ltp=110.0, prev_close=100.0, change_pct=10.0,
            direction="UP", threshold_pct=7.0,
            fired_at=datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc),
        ),
        Alert(
            symbol="BBB", ltp=113.0, prev_close=100.0, change_pct=13.0,
            direction="UP", threshold_pct=13.0,
            fired_at=datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc),
        ),
    ]
    report = build_day_report(events, report_date=day)

    book = OrderBook(tmp_path / ".nse_alert" / "orders.json")
    req = OrderRequest(
        symbol="AAA", side="BUY", quantity=5, product="MIS", order_type="MARKET",
        price=None, trigger_price=None, market_protection=2, tag="t", reason="test",
    )
    pending = book.add_pending(
        req, alert_symbol="AAA", alert_threshold=13.0, alert_direction="UP",
        entry_ltp=100.0, ttl_minutes=30,
    )
    created = datetime.now(timezone.utc) - timedelta(minutes=31)
    book._data["pending"][pending.id]["created_at"] = created.isoformat()  # noqa: SLF001
    book._data["pending"][pending.id]["expires_at"] = (  # noqa: SLF001
        created + timedelta(minutes=30)
    ).isoformat()
    book._save()  # noqa: SLF001

    msg = _build_heartbeat(settings, report, book, day)

    assert "2026-09-18" in msg
    assert "Mode: confirm" in msg
    assert "Feed: kite" in msg
    assert "Alerts: 2 fired" in msg
    assert "7%:1" in msg
    assert "13%:1" in msg
    assert "0 placed, 1 expired unconfirmed" in msg
    assert "Screener: scheduled 20:30 IST" in msg
    assert "Warnings: 1 order(s) expired unconfirmed" in msg


def test_build_heartbeat_no_warnings_when_nothing_expired(tmp_path: Path, monkeypatch) -> None:
    from datetime import date as date_cls

    from nse_alert.cli import _build_heartbeat
    from nse_alert.config import Settings
    from nse_alert.report import build_day_report

    monkeypatch.chdir(tmp_path)
    settings = Settings()
    day = date_cls(2026, 9, 18)
    report = build_day_report([], report_date=day)
    book = OrderBook(tmp_path / ".nse_alert" / "orders.json")

    msg = _build_heartbeat(settings, report, book, day)

    assert "Alerts: 0 fired" in msg
    assert "0 placed, 0 expired unconfirmed" in msg
    assert "Warnings: none" in msg


def test_report_telegram_sends_report_then_heartbeat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")

    sent: list[str] = []
    monkeypatch.setattr(
        cli_module.TelegramNotifier, "send_text", lambda self, msg, **kw: sent.append(msg)
    )

    runner = CliRunner()
    result = runner.invoke(main, ["report", "--telegram"])

    assert result.exit_code == 0, result.output
    assert len(sent) == 2
    assert "end-of-day report" in sent[0]
    assert "Daily heartbeat" in sent[1]
