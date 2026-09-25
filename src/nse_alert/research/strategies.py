"""Intraday strategy simulations on synthetic (or live) alert events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from nse_alert.engine import Alert

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class Trade:
    strategy_id: str
    symbol: str
    session: date
    direction: str  # LONG only for now
    entry_threshold: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    pnl_pct: float
    is_fno: bool = False
    is_asm: bool = False


@dataclass
class StrategyResult:
    strategy_id: str
    description: str
    trades: list[Trade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl_pct > 0) / len(self.trades)

    @property
    def avg_pnl_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.pnl_pct for t in self.trades) / len(self.trades)

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        losses = sum(-t.pnl_pct for t in self.trades if t.pnl_pct < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses


def _to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _flat_deadline(session: date) -> datetime:
    return datetime.combine(session, time(15, 15), tzinfo=IST)


def _bars_after(bars: pd.DataFrame, when: datetime) -> pd.DataFrame:
    if bars.empty:
        return bars
    when_naive = _to_ist(when).replace(tzinfo=None)
    idx = bars.index
    if getattr(idx, "tz", None) is not None:
        bars = bars.copy()
        bars.index = idx.tz_convert(None)
    return bars.loc[bars.index >= when_naive]


def _simulate_long(
    *,
    strategy_id: str,
    alert: Alert,
    bars: pd.DataFrame,
    stop_loss_pct: float,
    trail_breakeven_pct: float | None,
    cost_bps_roundtrip: float,
) -> Trade | None:
    """MIS-style long: enter at alert LTP, SL, optional BE trail, flat by 15:15."""
    session = _to_ist(alert.fired_at).date()
    entry = float(alert.ltp)
    if entry <= 0:
        return None
    path = _bars_after(bars, alert.fired_at)
    if path.empty:
        return None

    stop = entry * (1.0 - stop_loss_pct / 100.0)
    be_armed = False
    be_level = entry * (1.0 + (trail_breakeven_pct or 0) / 100.0)
    deadline = _flat_deadline(session)

    exit_price = float(path.iloc[-1]["close"])
    exit_time = path.index[-1].to_pydatetime()
    if exit_time.tzinfo is None:
        exit_time = exit_time.replace(tzinfo=IST)
    exit_reason = "eod"

    for ts, row in path.iterrows():
        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        ts_ist = _to_ist(ts_dt if isinstance(ts_dt, datetime) else datetime.combine(session, time(9, 15)))
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        if ts_ist >= deadline:
            exit_price = close
            exit_time = ts_ist
            exit_reason = "time_flat"
            break

        if trail_breakeven_pct is not None and not be_armed and high >= be_level:
            be_armed = True
            stop = entry

        if low <= stop:
            exit_price = stop
            exit_time = ts_ist
            exit_reason = "stop_be" if be_armed and abs(stop - entry) < 1e-9 else "stop"
            break

    gross = (exit_price / entry - 1.0) * 100.0
    net = gross - cost_bps_roundtrip / 100.0
    return Trade(
        strategy_id=strategy_id,
        symbol=alert.symbol,
        session=session,
        direction="LONG",
        entry_threshold=float(alert.threshold_pct),
        entry_time=_to_ist(alert.fired_at),
        entry_price=entry,
        exit_time=_to_ist(exit_time),
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_pct=net,
        is_fno=alert.is_fno,
        is_asm=alert.is_asm,
    )


def _simulate_hold_to_close(
    *,
    strategy_id: str,
    alert: Alert,
    day_close: float,
    cost_bps_roundtrip: float,
) -> Trade | None:
    entry = float(alert.ltp)
    if entry <= 0 or day_close <= 0:
        return None
    session = _to_ist(alert.fired_at).date()
    gross = (day_close / entry - 1.0) * 100.0
    net = gross - cost_bps_roundtrip / 100.0
    return Trade(
        strategy_id=strategy_id,
        symbol=alert.symbol,
        session=session,
        direction="LONG",
        entry_threshold=float(alert.threshold_pct),
        entry_time=_to_ist(alert.fired_at),
        entry_price=entry,
        exit_time=_flat_deadline(session),
        exit_price=day_close,
        exit_reason="hold_close",
        pnl_pct=net,
        is_fno=alert.is_fno,
        is_asm=alert.is_asm,
    )


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy_id: str
    description: str
    entry_thresholds: tuple[float, ...]
    sides: tuple[str, ...] = ("UP",)
    stop_loss_pct: float = 2.0
    trail_breakeven_pct: float | None = 2.0
    hold_to_close: bool = False


STRATEGY_CATALOG: dict[str, StrategySpec] = {
    "a1": StrategySpec(
        strategy_id="a1",
        description="Current bot: BUY +13 UP, 2% SL, trail BE @ +2%, flat 15:15",
        entry_thresholds=(13.0,),
        stop_loss_pct=2.0,
        trail_breakeven_pct=2.0,
    ),
    "a2_7": StrategySpec(
        strategy_id="a2_7",
        description="BUY +7 UP, 2% SL, trail BE @ +2%, flat 15:15",
        entry_thresholds=(7.0,),
        stop_loss_pct=2.0,
        trail_breakeven_pct=2.0,
    ),
    "a2_11": StrategySpec(
        strategy_id="a2_11",
        description="BUY +11 UP, 2% SL, trail BE @ +2%, flat 15:15",
        entry_thresholds=(11.0,),
        stop_loss_pct=2.0,
        trail_breakeven_pct=2.0,
    ),
    "a_close_13": StrategySpec(
        strategy_id="a_close_13",
        description="BUY +13 UP, hold to close (no SL) — baseline",
        entry_thresholds=(13.0,),
        hold_to_close=True,
        trail_breakeven_pct=None,
    ),
    "a_close_7": StrategySpec(
        strategy_id="a_close_7",
        description="BUY +7 UP, hold to close (no SL) — baseline",
        entry_thresholds=(7.0,),
        hold_to_close=True,
        trail_breakeven_pct=None,
    ),
}


def run_strategy(
    spec: StrategySpec,
    *,
    alerts: list[Alert],
    bars_by_symbol: dict[str, pd.DataFrame],
    close_by_symbol_day: dict[tuple[str, date], float],
    cost_bps_roundtrip: float = 10.0,
    one_trade_per_symbol_day: bool = True,
) -> StrategyResult:
    result = StrategyResult(strategy_id=spec.strategy_id, description=spec.description)
    seen: set[tuple[str, date]] = set()

    eligible = [
        a
        for a in alerts
        if a.direction in spec.sides and float(a.threshold_pct) in spec.entry_thresholds
    ]
    eligible.sort(key=lambda a: a.fired_at)

    for alert in eligible:
        session = _to_ist(alert.fired_at).date()
        key = (alert.symbol, session)
        if one_trade_per_symbol_day and key in seen:
            continue
        bars = bars_by_symbol.get(alert.symbol.upper())
        if bars is None or bars.empty:
            continue

        if spec.hold_to_close:
            day_close = close_by_symbol_day.get(key)
            if day_close is None:
                day_bars = bars[bars.index.date == session] if len(bars) else bars
                if day_bars.empty:
                    continue
                day_close = float(day_bars.iloc[-1]["close"])
            trade = _simulate_hold_to_close(
                strategy_id=spec.strategy_id,
                alert=alert,
                day_close=day_close,
                cost_bps_roundtrip=cost_bps_roundtrip,
            )
        else:
            trade = _simulate_long(
                strategy_id=spec.strategy_id,
                alert=alert,
                bars=bars,
                stop_loss_pct=spec.stop_loss_pct,
                trail_breakeven_pct=spec.trail_breakeven_pct,
                cost_bps_roundtrip=cost_bps_roundtrip,
            )
        if trade is None:
            continue
        seen.add(key)
        result.trades.append(trade)
    return result
