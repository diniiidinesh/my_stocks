from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class VolumeSpike:
    """One day where volume exceeded EMA × multiplier."""

    days_ago: int  # 0 = latest bar (today / last session)
    date: str
    volume: float
    volume_ema: float
    multiple: float


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    close: pd.Series,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_vals = atr(high, low, close, period)
    plus_di = 100.0 * pd.Series(plus_dm, index=high.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr_vals.replace(0.0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=high.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr_vals.replace(0.0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan))
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[pd.Series, pd.Series]:
    """Return (supertrend line, direction) where direction +1 = bullish (price above)."""
    hl2 = (high.astype(float) + low.astype(float)) / 2.0
    atr_vals = atr(high, low, close, period)
    basic_ub = hl2 + multiplier * atr_vals
    basic_lb = hl2 - multiplier * atr_vals

    final_ub = basic_ub.copy()
    final_lb = basic_lb.copy()
    for i in range(1, len(close)):
        if basic_ub.iloc[i] < final_ub.iloc[i - 1] or close.iloc[i - 1] > final_ub.iloc[i - 1]:
            final_ub.iloc[i] = basic_ub.iloc[i]
        else:
            final_ub.iloc[i] = final_ub.iloc[i - 1]
        if basic_lb.iloc[i] > final_lb.iloc[i - 1] or close.iloc[i - 1] < final_lb.iloc[i - 1]:
            final_lb.iloc[i] = basic_lb.iloc[i]
        else:
            final_lb.iloc[i] = final_lb.iloc[i - 1]

    st = pd.Series(index=close.index, dtype=float)
    direction = pd.Series(index=close.index, dtype=float)
    st.iloc[0] = final_ub.iloc[0]
    direction.iloc[0] = -1.0
    for i in range(1, len(close)):
        if st.iloc[i - 1] == final_ub.iloc[i - 1] and close.iloc[i] <= final_ub.iloc[i]:
            st.iloc[i] = final_ub.iloc[i]
            direction.iloc[i] = -1.0
        elif st.iloc[i - 1] == final_ub.iloc[i - 1] and close.iloc[i] > final_ub.iloc[i]:
            st.iloc[i] = final_lb.iloc[i]
            direction.iloc[i] = 1.0
        elif st.iloc[i - 1] == final_lb.iloc[i - 1] and close.iloc[i] >= final_lb.iloc[i]:
            st.iloc[i] = final_lb.iloc[i]
            direction.iloc[i] = 1.0
        else:
            st.iloc[i] = final_ub.iloc[i]
            direction.iloc[i] = -1.0
    return st, direction


def find_volume_spikes(
    df: pd.DataFrame,
    *,
    lookback_days: int,
    ema_period: int,
    multiple: float,
) -> list[VolumeSpike]:
    """Spikes anywhere in the last ``lookback_days`` sessions (incl. latest bar)."""
    if df.empty or "volume" not in df.columns:
        return []
    vol = df["volume"].astype(float)
    vol_ema = ema(vol, ema_period)
    n = len(df)
    start = max(0, n - lookback_days)
    spikes: list[VolumeSpike] = []
    for i in range(start, n):
        v = float(vol.iloc[i])
        e = float(vol_ema.iloc[i])
        if e <= 0 or not np.isfinite(e):
            continue
        mult = v / e
        if mult > multiple:
            days_ago = n - 1 - i
            dt = df.index[i]
            date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
            spikes.append(
                VolumeSpike(
                    days_ago=days_ago,
                    date=date_str,
                    volume=v,
                    volume_ema=e,
                    multiple=round(mult, 2),
                )
            )
    spikes.sort(key=lambda s: (s.days_ago, -s.multiple))
    return spikes


def enrich_ohlcv(
    df: pd.DataFrame,
    *,
    ema_fast: int = 20,
    ema_mid: int = 50,
    ema_slow: int = 200,
    st_period: int = 10,
    st_mult: float = 3.0,
    adx_period: int = 14,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    volume_ema_period: int = 20,
) -> pd.DataFrame:
    """Add indicator columns to a daily OHLCV frame (DatetimeIndex)."""
    out = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            raise ValueError(f"OHLCV missing column: {col}")
        out[col] = out[col].astype(float)

    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_mid"] = ema(out["close"], ema_mid)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["supertrend"], out["st_dir"] = supertrend(
        out["high"], out["low"], out["close"], period=st_period, multiplier=st_mult
    )
    out["adx"] = adx(out["high"], out["low"], out["close"], adx_period)
    out["rsi"] = rsi(out["close"], rsi_period)
    macd_line, macd_sig, macd_hist = macd(
        out["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal
    )
    out["macd"] = macd_line
    out["macd_signal"] = macd_sig
    out["macd_hist"] = macd_hist
    out["volume_ema"] = ema(out["volume"], volume_ema_period)
    out["high_52w"] = out["high"].rolling(window=252, min_periods=60).max()
    return out
