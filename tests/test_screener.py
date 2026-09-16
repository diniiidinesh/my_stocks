from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from nse_alert.screener.delivery import DeliveryBook, DeliveryInfo, _normalize_bhav_df
from nse_alert.screener.engine import (
    ScreenConfig,
    evaluate_symbol,
    market_closed_enough,
    rows_to_frame,
    _sort_rows,
)
from nse_alert.screener.export import format_screener_summary, write_screener_excel
from nse_alert.screener.indicators import (
    enrich_ohlcv,
    find_volume_spikes,
    ema,
)


def _synth_uptrend(n: int = 260, start: float = 100.0) -> pd.DataFrame:
    """Synthetic daily bars in a mild uptrend with one volume spike."""
    rng = np.random.default_rng(42)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    close = start + np.linspace(0, 40, n) + rng.normal(0, 0.4, n).cumsum() * 0.05
    close = np.maximum(close, 5.0)
    high = close + 0.8
    low = close - 0.8
    open_ = close - 0.1
    volume = np.full(n, 1_000_000.0)
    # Spike 3 sessions ago
    volume[-4] = 3_500_000.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_volume_spikes_report_days_ago_and_multiples() -> None:
    df = _synth_uptrend()
    enriched = enrich_ohlcv(df)
    spikes = find_volume_spikes(
        enriched, lookback_days=20, ema_period=20, multiple=1.5
    )
    assert spikes
    assert any(s.days_ago == 3 for s in spikes)
    assert max(s.multiple for s in spikes) > 1.5


def test_evaluate_ranks_all_pass_concept(tmp_path: Path) -> None:
    cfg = ScreenConfig(
        require_volume=True,
        require_adx=False,
        require_rsi=False,
        require_macd=False,
        require_near_52w=False,
        near_52w_high_pct=50.0,
        min_price=1.0,
    )
    df = _synth_uptrend()
    row = evaluate_symbol("DEMO", df, cfg=cfg, market_cap_cr=6000, turnover_cr=20)
    assert row is not None
    # Uptrend synth should usually clear EMA stack + often SuperTrend
    assert row.pass_volume is True
    assert "T-3" in row.vol_spike_days or row.vol_spike_count >= 1


def test_sort_puts_all_pass_first() -> None:
    cfg = ScreenConfig(
        require_volume=True,
        require_adx=True,
        require_rsi=True,
        require_macd=True,
        require_near_52w=True,
    )
    df = _synth_uptrend()
    a = evaluate_symbol("AAA", df, cfg=cfg, market_cap_cr=8000, turnover_cr=30)
    b = evaluate_symbol("BBB", df, cfg=cfg, market_cap_cr=8000, turnover_cr=30)
    assert a and b
    # Force ranking flags
    a.all_pass = True
    a.mandatory_pass = True
    a.optional_score = 5
    a.pct_from_52w_high = 2.0
    b.all_pass = False
    b.mandatory_pass = True
    b.optional_score = 4
    b.pct_from_52w_high = 1.0
    ranked = _sort_rows([b, a])
    assert ranked[0].symbol == "AAA"
    assert ranked[1].symbol == "BBB"


def test_excel_and_summary(tmp_path: Path) -> None:
    from nse_alert.screener.engine import ScreenResult

    cfg = ScreenConfig(require_adx=False, require_rsi=False, require_macd=False, require_near_52w=False)
    df = _synth_uptrend()
    row = evaluate_symbol("TEST", df, cfg=cfg, name="Test Co", market_cap_cr=9000, turnover_cr=15)
    assert row is not None
    row.all_pass = True
    row.mandatory_pass = True
    result = ScreenResult(
        as_of=datetime.now().date(),
        rows=[row],
        scanned=1,
        skipped=0,
        config=cfg,
        frame=rows_to_frame([row]),
    )
    path = tmp_path / "out.xlsx"
    write_screener_excel(result, path)
    assert path.exists() and path.stat().st_size > 1000
    text = format_screener_summary(result, excel_name=path.name)
    assert "EOD TA Screener" in text
    assert "TEST" in text


def test_market_closed_gate() -> None:
    ist = ZoneInfo("Asia/Kolkata")
    morning = datetime(2026, 9, 16, 10, 0, tzinfo=ist)
    evening = datetime(2026, 9, 16, 15, 45, tzinfo=ist)
    assert not market_closed_enough(after_hhmm=1540, now=morning)
    assert market_closed_enough(after_hhmm=1540, now=evening)


def test_supertrend_not_all_nan() -> None:
    df = _synth_uptrend(260)
    en = enrich_ohlcv(df)
    assert en["supertrend"].notna().sum() > 200
    assert np.isfinite(float(en["supertrend"].iloc[-1]))
    assert float(en["st_dir"].iloc[-1]) in (-1.0, 1.0)
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ema(s, 3)
    assert len(out) == 5
    assert out.iloc[-1] > out.iloc[0]


class _FakeDeliveryBook:
    def __init__(self, mapping: dict[tuple[str, date], DeliveryInfo]) -> None:
        self.mapping = mapping

    def get(self, symbol: str, session_day: date) -> DeliveryInfo | None:
        return self.mapping.get((symbol.upper(), session_day))


def test_delivery_filter_any_spike_ignores_missing() -> None:
    df = _synth_uptrend()
    spikes = find_volume_spikes(
        enrich_ohlcv(df), lookback_days=20, ema_period=20, multiple=1.5
    )
    assert spikes
    spike_day = date.fromisoformat(spikes[0].date)
    # Only one spike day has delivery; it is above 40% → pass
    book = _FakeDeliveryBook(
        {
            ("DEMO", spike_day): DeliveryInfo(
                traded_qty=1e6, delivery_qty=5e5, delivery_pct=55.0
            )
        }
    )
    cfg = ScreenConfig(
        require_volume=True,
        require_adx=False,
        require_rsi=False,
        require_macd=False,
        require_near_52w=False,
        require_delivery=True,
        min_delivery_pct=40.0,
        min_price=1.0,
    )
    row = evaluate_symbol("DEMO", df, cfg=cfg, delivery_book=book)  # type: ignore[arg-type]
    assert row is not None
    assert row.pass_delivery is True
    assert row.deliv_pct_max_on_spikes == 55.0
    assert f"T-{spikes[0].days_ago}" in row.deliv_spike_days
    assert "55.0%" in row.deliv_spike_detail or "55%" in row.deliv_spike_detail

    # Known delivery below threshold → fail (missing other days ignored)
    book_low = _FakeDeliveryBook(
        {
            ("DEMO", spike_day): DeliveryInfo(
                traded_qty=1e6, delivery_qty=2e5, delivery_pct=20.0
            )
        }
    )
    row_low = evaluate_symbol("DEMO", df, cfg=cfg, delivery_book=book_low)  # type: ignore[arg-type]
    assert row_low is not None
    assert row_low.pass_delivery is False
    assert row_low.deliv_spike_days == ""

    # All missing → fail optional delivery (no known qualifying day)
    row_miss = evaluate_symbol("DEMO", df, cfg=cfg, delivery_book=_FakeDeliveryBook({}))  # type: ignore[arg-type]
    assert row_miss is not None
    assert row_miss.pass_delivery is False
    assert "n/a" in row_miss.deliv_spike_detail


def test_normalize_bhav_df_parses_delivery_columns() -> None:
    raw = pd.DataFrame(
        {
            "SYMBOL": ["AAA", "BBB"],
            "SERIES": ["EQ", "EQ"],
            "TTL_TRD_QNTY": [1000, 2000],
            "DELIV_QTY": [500, 100],
            "DELIV_PER": ["50.00", "5"],
        }
    )
    out = _normalize_bhav_df(raw)
    assert list(out["symbol"]) == ["AAA", "BBB"]
    assert "delivery_pct" in out.columns

