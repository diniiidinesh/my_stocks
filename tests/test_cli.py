from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nse_alert.cli import main


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
