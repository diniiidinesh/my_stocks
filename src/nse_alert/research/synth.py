"""Reconstruct day-move alert events from OHLC bars (same rules as AlertEngine)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

import pandas as pd

from nse_alert.engine import Alert


@dataclass(frozen=True, slots=True)
class DayBars:
    symbol: str
    session: date
    prev_close: float
    bars: pd.DataFrame  # indexed datetime, columns open/high/low/close/volume
    is_fno: bool = False
    is_asm: bool = False


def _session_slice(bars: pd.DataFrame, session: date) -> pd.DataFrame:
    if bars.empty:
        return bars
    idx = bars.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
        bars = bars.copy()
        bars.index = idx
    mask = bars.index.date == session
    return bars.loc[mask]


def synthesize_alerts_for_day(
    day: DayBars,
    *,
    thresholds: list[float],
    fo_only_thresholds: set[float] | None = None,
) -> list[Alert]:
    """Walk intraday bars and fire once per symbol×direction×threshold.

    Uses each bar's **high** for UP crossings and **low** for DOWN so a
    spike inside the candle still counts (conservative for backtests that
    enter at the alert level, not at the extreme).
    """
    fo_only = {float(t) for t in (fo_only_thresholds or set())}
    session_bars = _session_slice(day.bars, day.session)
    if session_bars.empty or day.prev_close <= 0:
        return []

    fired: set[str] = set()
    out: list[Alert] = []

    for ts, row in session_bars.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        up_pct = (high / day.prev_close - 1.0) * 100.0
        down_pct = (low / day.prev_close - 1.0) * 100.0
        fired_at = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if isinstance(fired_at, datetime) and fired_at.tzinfo is None:
            fired_at = fired_at.replace(tzinfo=timezone.utc)

        for thr in sorted(thresholds):
            if thr in fo_only and not day.is_fno:
                continue
            key_up = f"UP|{thr}"
            if key_up not in fired and up_pct >= thr:
                fired.add(key_up)
                # Entry reference: alert level price (not the wick extreme).
                ltp = day.prev_close * (1.0 + thr / 100.0)
                out.append(
                    Alert(
                        symbol=day.symbol,
                        ltp=ltp,
                        prev_close=day.prev_close,
                        change_pct=(ltp / day.prev_close - 1.0) * 100.0,
                        direction="UP",
                        threshold_pct=float(thr),
                        fired_at=fired_at,
                        is_asm=day.is_asm,
                        is_fno=day.is_fno,
                    )
                )
            key_down = f"DOWN|{thr}"
            if key_down not in fired and down_pct <= -thr:
                fired.add(key_down)
                ltp = day.prev_close * (1.0 - thr / 100.0)
                out.append(
                    Alert(
                        symbol=day.symbol,
                        ltp=ltp,
                        prev_close=day.prev_close,
                        change_pct=(ltp / day.prev_close - 1.0) * 100.0,
                        direction="DOWN",
                        threshold_pct=float(thr),
                        fired_at=fired_at,
                        is_asm=day.is_asm,
                        is_fno=day.is_fno,
                    )
                )

    return out


def synthesize_alerts(
    days: Iterable[DayBars],
    *,
    thresholds: list[float],
    fo_only_thresholds: set[float] | None = None,
) -> list[Alert]:
    events: list[Alert] = []
    for day in days:
        events.extend(
            synthesize_alerts_for_day(
                day,
                thresholds=thresholds,
                fo_only_thresholds=fo_only_thresholds,
            )
        )
    return events
