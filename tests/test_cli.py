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
