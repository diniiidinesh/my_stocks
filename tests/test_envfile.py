from __future__ import annotations

from pathlib import Path

from nse_alert.envfile import read_env_value, upsert_env


def test_upsert_env_creates_and_updates(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    upsert_env({"KITE_API_KEY": "abc", "FEED_MODE": "mock"}, path=path)
    text = path.read_text(encoding="utf-8")
    assert "KITE_API_KEY=abc" in text
    assert "FEED_MODE=mock" in text

    upsert_env({"FEED_MODE": "kite", "KITE_ACCESS_TOKEN": "tok123"}, path=path)
    text = path.read_text(encoding="utf-8")
    assert "FEED_MODE=kite" in text
    assert "KITE_ACCESS_TOKEN=tok123" in text
    assert text.count("FEED_MODE=") == 1
    assert read_env_value("KITE_API_KEY", path=path) == "abc"
    assert read_env_value("KITE_ACCESS_TOKEN", path=path) == "tok123"
