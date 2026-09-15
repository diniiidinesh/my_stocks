from __future__ import annotations

from pathlib import Path

from nse_alert.surveillance import _fetch_asm_symbols, load_asm_symbols, load_nfo_equity_underlyings


def test_fetch_asm_symbols_parses_lt_and_st_columns(monkeypatch) -> None:
    csv = (
        "Long term ASM,,,,,,Short term ASM,,,,,\n"
        "SR No.,SYMBOL,COMPANY,ISIN,ASM Stage,With effect from,"
        "SR No.,SYMBOL,COMPANY,ISIN,ASM Stage,With effect from\n"
        "1,AAA,AAA Ltd,INE1,I,01-Jan-26,1,BBB,BBB Ltd,INE2,I,01-Jan-26\n"
        "2,CCC,CCC Ltd,INE3,II,01-Jan-26,,,,,,\n"
    )

    class _Resp:
        text = csv

        def raise_for_status(self) -> None:
            return None

    def _fake_get(*_a, **_k):
        return _Resp()

    monkeypatch.setattr("nse_alert.surveillance.httpx.get", _fake_get)
    symbols = _fetch_asm_symbols("https://example.invalid/asm.csv")
    assert symbols == {"AAA", "BBB", "CCC"}


def test_load_asm_symbols_uses_cache(tmp_path: Path) -> None:
    cache = tmp_path / "asm.txt"
    cache.write_text("FOO\nBAR\n", encoding="utf-8")
    symbols = load_asm_symbols(cache_path=cache, enabled=True)
    assert symbols == {"FOO", "BAR"}


def test_load_asm_disabled() -> None:
    assert load_asm_symbols(enabled=False) == set()


def test_load_nfo_equity_underlyings() -> None:
    class _Kite:
        def instruments(self, exchange: str):
            assert exchange == "NFO"
            return [
                {"instrument_type": "FUT", "segment": "NFO-FUT", "exchange": "NFO", "name": "RELIANCE"},
                {"instrument_type": "FUT", "segment": "NFO-FUT", "exchange": "NFO", "name": "TCS"},
                {"instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO", "name": "RELIANCE"},
                {"instrument_type": "FUT", "segment": "NFO-FUT", "exchange": "NFO", "name": ""},
            ]

    assert load_nfo_equity_underlyings(_Kite()) == {"RELIANCE", "TCS"}
