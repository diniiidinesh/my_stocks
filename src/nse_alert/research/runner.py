"""Orchestrate Kite-backed alert reconstruction + strategy backtests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from nse_alert.research.bars import get_intraday_bars
from nse_alert.research.strategies import (
    STRATEGY_CATALOG,
    StrategyResult,
    run_strategy,
)
from nse_alert.research.synth import DayBars, synthesize_alerts
from nse_alert.universe import Instrument

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BacktestReport:
    start: date
    end: date
    interval: str
    n_symbols: int
    n_alerts: int
    strategies: list[StrategyResult]
    cost_bps_roundtrip: float


def _session_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _prev_close_from_daily(daily: pd.DataFrame, session: date) -> float | None:
    """Previous session close from a daily OHLCV frame indexed by date."""
    if daily is None or daily.empty:
        return None
    idx_dates = [ts.date() if hasattr(ts, "date") else ts for ts in daily.index]
    prior = [(i, d) for i, d in enumerate(idx_dates) if d < session]
    if not prior:
        return None
    _, last_d = prior[-1]
    # Match row by date
    for ts, row in daily.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        if d == last_d:
            return float(row["close"])
    return None


def _day_close_from_bars(bars: pd.DataFrame, session: date) -> float | None:
    if bars.empty:
        return None
    day = bars[bars.index.map(lambda ts: ts.date() == session)]
    if day.empty:
        return None
    return float(day.iloc[-1]["close"])


def build_day_bars_for_symbol(
    *,
    symbol: str,
    instrument_token: int,
    sessions: list[date],
    kite: Any,
    cache_dir: Path,
    interval: str,
    daily_history: pd.DataFrame | None,
    fo_symbols: set[str],
    asm_symbols: set[str],
    sleep_sec: float,
) -> list[DayBars]:
    if not sessions:
        return []
    start, end = sessions[0], sessions[-1]
    bars = get_intraday_bars(
        kite,
        symbol=symbol,
        instrument_token=instrument_token,
        start=start,
        end=end,
        cache_dir=cache_dir,
        interval=interval,
        sleep_sec=sleep_sec,
    )
    if bars.empty:
        logger.warning("No %s bars for %s (%s→%s)", interval, symbol, start, end)
        return []

    out: list[DayBars] = []
    for session in sessions:
        prev: float | None = None
        if daily_history is not None and not daily_history.empty:
            prev = _prev_close_from_daily(daily_history, session)
        if prev is None:
            # Fallback: last close before this session from intraday bars.
            prior = bars[bars.index.map(lambda ts: ts.date() < session)]
            if prior.empty:
                continue
            prev = float(prior.iloc[-1]["close"])
        out.append(
            DayBars(
                symbol=symbol.upper(),
                session=session,
                prev_close=prev,
                bars=bars,
                is_fno=symbol.upper() in fo_symbols,
                is_asm=symbol.upper() in asm_symbols,
            )
        )
    return out


def run_backtest(
    *,
    instruments: list[Instrument],
    kite: Any,
    start: date,
    end: date,
    thresholds: list[float],
    fo_only_thresholds: list[float] | set[float],
    fo_symbols: set[str],
    asm_symbols: set[str],
    strategy_ids: list[str],
    cache_dir: Path,
    interval: str = "5minute",
    cost_bps_roundtrip: float = 10.0,
    sleep_sec: float = 0.35,
    daily_by_symbol: dict[str, pd.DataFrame] | None = None,
) -> BacktestReport:
    sessions = _session_days(start, end)
    if not sessions:
        raise ValueError("No weekday sessions in the requested range")

    day_bars: list[DayBars] = []
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    for inst in instruments:
        sym = inst.symbol.upper()
        daily = (daily_by_symbol or {}).get(sym)
        built = build_day_bars_for_symbol(
            symbol=sym,
            instrument_token=inst.instrument_token,
            sessions=sessions,
            kite=kite,
            cache_dir=cache_dir / "intraday",
            interval=interval,
            daily_history=daily,
            fo_symbols=fo_symbols,
            asm_symbols=asm_symbols,
            sleep_sec=sleep_sec,
        )
        if not built:
            continue
        day_bars.extend(built)
        # All DayBars for a symbol share the same bars frame.
        bars_by_symbol[sym] = built[0].bars

    alerts = synthesize_alerts(
        day_bars,
        thresholds=thresholds,
        fo_only_thresholds={float(t) for t in fo_only_thresholds},
    )

    close_by_symbol_day: dict[tuple[str, date], float] = {}
    for db in day_bars:
        close = _day_close_from_bars(db.bars, db.session)
        if close is not None:
            close_by_symbol_day[(db.symbol, db.session)] = close

    results: list[StrategyResult] = []
    for sid in strategy_ids:
        spec = STRATEGY_CATALOG.get(sid)
        if spec is None:
            raise ValueError(
                f"Unknown strategy {sid!r}; choose from {sorted(STRATEGY_CATALOG)}"
            )
        results.append(
            run_strategy(
                spec,
                alerts=alerts,
                bars_by_symbol=bars_by_symbol,
                close_by_symbol_day=close_by_symbol_day,
                cost_bps_roundtrip=cost_bps_roundtrip,
            )
        )

    return BacktestReport(
        start=start,
        end=end,
        interval=interval,
        n_symbols=len(bars_by_symbol),
        n_alerts=len(alerts),
        strategies=results,
        cost_bps_roundtrip=cost_bps_roundtrip,
    )


def format_backtest_report(report: BacktestReport) -> str:
    lines = [
        "=== Alert strategy backtest ===",
        f"Range: {report.start} → {report.end} ({report.interval})",
        f"Symbols with bars: {report.n_symbols}",
        f"Synthetic alerts: {report.n_alerts}",
        f"Cost assumption: {report.cost_bps_roundtrip:g} bps round-trip",
        "",
        f"{'ID':<10} {'N':>5} {'Win%':>7} {'Avg%':>8} {'Total%':>9} {'PF':>6}  Description",
        "-" * 88,
    ]
    for s in report.strategies:
        pf = s.profit_factor
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"{s.strategy_id:<10} {s.n:>5} {100 * s.win_rate:>6.1f}% "
            f"{s.avg_pnl_pct:>7.2f}% {s.total_pnl_pct:>8.2f}% {pf_s:>6}  "
            f"{s.description}"
        )
    lines.append("")
    lines.append("Exit reasons (per strategy):")
    for s in report.strategies:
        if not s.trades:
            lines.append(f"  {s.strategy_id}: (no trades)")
            continue
        from collections import Counter

        reasons = Counter(t.exit_reason for t in s.trades)
        detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        lines.append(f"  {s.strategy_id}: {detail}")
    lines.append("")
    lines.append(
        "Interpretation: prefer strategies that beat a1 and hold-to-close "
        "baselines on enough trades; two live days alone are not enough."
    )
    return "\n".join(lines)


def write_trades_csv(results: list[StrategyResult], path: Path) -> None:
    rows: list[dict[str, object]] = []
    for s in results:
        for t in s.trades:
            rows.append(
                {
                    "strategy_id": t.strategy_id,
                    "symbol": t.symbol,
                    "session": t.session.isoformat(),
                    "entry_threshold": t.entry_threshold,
                    "entry_time": t.entry_time.isoformat(),
                    "entry_price": t.entry_price,
                    "exit_time": t.exit_time.isoformat(),
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "pnl_pct": t.pnl_pct,
                    "is_fno": t.is_fno,
                    "is_asm": t.is_asm,
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
