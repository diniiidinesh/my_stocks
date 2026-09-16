from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    rename = {
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "adj close": "close",
        "adj_close": "close",
    }
    out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
    need = ["open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in out.columns]
    if missing:
        raise ValueError(f"OHLCV missing columns: {missing}")
    if not isinstance(out.index, pd.DatetimeIndex):
        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"])
            out = out.set_index("date")
        else:
            out.index = pd.to_datetime(out.index)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out[need].astype(float)


def load_cached_history(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        return _normalize_ohlcv(df)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bad history cache %s: %s", path, exc)
        return None


def save_cached_history(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _normalize_ohlcv(df).reset_index()
    date_col = out.columns[0]
    out = out.rename(columns={date_col: "date"})
    out.to_csv(path, index=False)


def fetch_history_kite(
    kite: Any,
    *,
    instrument_token: int,
    days: int = 400,
) -> pd.DataFrame:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(days * 1.6) + 30)
    raw = kite.historical_data(
        instrument_token,
        start.isoformat(),
        end.isoformat(),
        "day",
        continuous=False,
        oi=False,
    )
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(raw)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.set_index("date")
    return _normalize_ohlcv(df)


def fetch_history_yfinance(symbol: str, *, days: int = 400) -> pd.DataFrame:
    import yfinance as yf

    ticker = f"{symbol.upper()}.NS"
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(days * 1.7) + 40)
    df = yf.download(
        ticker,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return _normalize_ohlcv(df)


def get_daily_history(
    symbol: str,
    *,
    cache_dir: Path,
    days: int = 400,
    kite: Any | None = None,
    instrument_token: int | None = None,
    prefer_kite: bool = True,
    sleep_sec: float = 0.35,
) -> pd.DataFrame:
    """Load daily OHLCV with on-disk cache; refresh from Kite or Yahoo."""
    path = cache_dir / f"{symbol.upper()}.csv"
    cached = load_cached_history(path)
    today = date.today()
    if cached is not None and not cached.empty:
        last = cached.index.max().date()
        # Cache is fresh enough after the latest session (allow weekend gap).
        if (today - last).days <= 3 and len(cached) >= min(220, days // 2):
            return cached.tail(days)

    df = pd.DataFrame()
    if prefer_kite and kite is not None and instrument_token:
        try:
            df = fetch_history_kite(kite, instrument_token=instrument_token, days=days)
            time.sleep(sleep_sec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kite history failed for %s: %s — trying Yahoo", symbol, exc)

    if df.empty:
        try:
            df = fetch_history_yfinance(symbol, days=days)
            time.sleep(min(sleep_sec, 0.2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Yahoo history failed for %s: %s", symbol, exc)
            return cached if cached is not None else df

    if not df.empty:
        save_cached_history(path, df)
    return df.tail(days) if not df.empty else df
