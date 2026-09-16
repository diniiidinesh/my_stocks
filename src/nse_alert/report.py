from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nse_alert.engine import Alert, alert_from_event

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class ThresholdGap:
    symbol: str
    direction: str
    from_threshold: float
    to_threshold: float
    from_time: datetime
    to_time: datetime
    gap: timedelta


@dataclass(frozen=True, slots=True)
class SymbolCloseOutcome:
    """One alerted symbol's day close vs the alert(s) it fired."""

    symbol: str
    direction: str
    prev_close: float
    close_price: float
    close_change_pct: float
    thresholds_fired: tuple[float, ...]
    alert_change_pct: float  # change% on the last (highest) alert for this dir


@dataclass(frozen=True, slots=True)
class DayReport:
    report_date: date
    events: list[Alert]
    counts_by_threshold: dict[float, int]
    counts_by_threshold_direction: dict[float, dict[str, int]]
    counts_by_direction: dict[str, int]
    unique_symbols: int
    unique_up_symbols: int
    unique_down_symbols: int
    asm_alert_count: int
    multi_level: list[tuple[str, str, list[Alert]]]
    gaps: list[ThresholdGap]
    symbol_closes: list[SymbolCloseOutcome]
    closed_above_by_threshold: dict[float, dict[str, int]]
    close_prices_source: str  # "kite" | "events_ltp" | "none"


def load_events(state_path: Path, *, as_of: date | None = None) -> list[Alert]:
    """Load persisted alert events for a calendar day from fired.json."""
    if not state_path.exists():
        return []
    data = json.loads(state_path.read_text(encoding="utf-8"))
    day = (as_of or date.today()).isoformat()
    if data.get("date") != day:
        return []
    events = data.get("events", [])
    if not isinstance(events, list):
        return []
    out: list[Alert] = []
    for item in events:
        if isinstance(item, dict):
            out.append(alert_from_event(item))
    return out


def fetch_eod_closes_kite(
    kite: Any,
    symbols: list[str],
) -> dict[str, float]:
    """Return last traded / day price per symbol via Kite quote batches."""
    from nse_alert.universe import _quote_batches

    if not symbols:
        return {}
    quote_map = _quote_batches(kite, sorted({s.upper() for s in symbols}))
    out: dict[str, float] = {}
    for sym in symbols:
        q = quote_map.get(f"NSE:{sym.upper()}", {}) or {}
        ohlc = q.get("ohlc") or {}
        # Prefer day's close when present; else LTP (fine after hours / last tick)
        close = float(ohlc.get("close") or 0.0)
        last = float(q.get("last_price") or 0.0)
        px = last if last > 0 else close
        if px > 0:
            out[sym.upper()] = px
    return out


def _closes_from_events(events: list[Alert]) -> dict[str, float]:
    """Fallback: use the highest LTP seen on any alert for that symbol today."""
    best: dict[str, float] = {}
    for a in events:
        sym = a.symbol.upper()
        prev = best.get(sym)
        if prev is None or a.ltp > prev:
            best[sym] = a.ltp
    return best


def build_day_report(
    events: list[Alert],
    *,
    report_date: date | None = None,
    eod_closes: dict[str, float] | None = None,
    close_prices_source: str = "none",
) -> DayReport:
    day = report_date or date.today()
    counts_by_threshold: Counter[float] = Counter()
    counts_by_direction: Counter[str] = Counter()
    thr_dir: dict[float, Counter[str]] = defaultdict(Counter)
    by_symbol_dir: dict[tuple[str, str], list[Alert]] = defaultdict(list)
    up_symbols: set[str] = set()
    down_symbols: set[str] = set()
    asm_alert_count = 0

    for alert in events:
        counts_by_threshold[alert.threshold_pct] += 1
        counts_by_direction[alert.direction] += 1
        thr_dir[alert.threshold_pct][alert.direction] += 1
        by_symbol_dir[(alert.symbol, alert.direction)].append(alert)
        if alert.direction == "UP":
            up_symbols.add(alert.symbol)
        elif alert.direction == "DOWN":
            down_symbols.add(alert.symbol)
        if alert.is_asm:
            asm_alert_count += 1

    multi_level: list[tuple[str, str, list[Alert]]] = []
    gaps: list[ThresholdGap] = []
    for (symbol, direction), group in sorted(by_symbol_dir.items()):
        group_sorted = sorted(group, key=lambda a: (a.threshold_pct, a.fired_at))
        if len(group_sorted) < 2:
            continue
        multi_level.append((symbol, direction, group_sorted))
        for left, right in zip(group_sorted, group_sorted[1:], strict=False):
            gaps.append(
                ThresholdGap(
                    symbol=symbol,
                    direction=direction,
                    from_threshold=left.threshold_pct,
                    to_threshold=right.threshold_pct,
                    from_time=left.fired_at,
                    to_time=right.fired_at,
                    gap=right.fired_at - left.fired_at,
                )
            )

    counts_by_threshold_direction = {
        thr: dict(dirs) for thr, dirs in sorted(thr_dir.items())
    }

    # Resolve close prices
    source = close_prices_source
    closes = {k.upper(): float(v) for k, v in (eod_closes or {}).items() if v and float(v) > 0}
    if not closes and events:
        closes = _closes_from_events(events)
        source = "events_ltp" if closes else "none"
    elif closes and source == "none":
        source = "provided"

    symbol_closes: list[SymbolCloseOutcome] = []
    closed_above: dict[float, Counter[str]] = defaultdict(Counter)

    if closes:
        for (symbol, direction), group in sorted(by_symbol_dir.items()):
            group_sorted = sorted(group, key=lambda a: a.threshold_pct)
            close_px = closes.get(symbol.upper())
            if close_px is None or close_px <= 0:
                continue
            prev = float(group_sorted[0].prev_close)
            if prev <= 0:
                continue
            close_chg = (close_px / prev - 1.0) * 100.0
            close_chg = round(close_chg, 4)
            thrs = tuple(a.threshold_pct for a in group_sorted)
            last_alert = group_sorted[-1]
            symbol_closes.append(
                SymbolCloseOutcome(
                    symbol=symbol,
                    direction=direction,
                    prev_close=prev,
                    close_price=close_px,
                    close_change_pct=close_chg,
                    thresholds_fired=thrs,
                    alert_change_pct=float(last_alert.change_pct),
                )
            )
            for thr in thrs:
                if direction == "UP" and close_chg + 1e-9 >= thr:
                    closed_above[thr]["UP"] += 1
                elif direction == "DOWN" and close_chg - 1e-9 <= -thr:
                    closed_above[thr]["DOWN"] += 1

    # Sort closes: UP first by close %, then DOWN
    symbol_closes.sort(
        key=lambda r: (
            0 if r.direction == "UP" else 1,
            -abs(r.close_change_pct),
            r.symbol,
        )
    )

    return DayReport(
        report_date=day,
        events=events,
        counts_by_threshold=dict(sorted(counts_by_threshold.items())),
        counts_by_threshold_direction=counts_by_threshold_direction,
        counts_by_direction=dict(counts_by_direction),
        unique_symbols=len({a.symbol for a in events}),
        unique_up_symbols=len(up_symbols),
        unique_down_symbols=len(down_symbols),
        asm_alert_count=asm_alert_count,
        multi_level=multi_level,
        gaps=gaps,
        symbol_closes=symbol_closes,
        closed_above_by_threshold={
            thr: dict(dirs) for thr, dirs in sorted(closed_above.items())
        },
        close_prices_source=source,
    )


def _fmt_ist(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(IST)
    return local.strftime("%H:%M:%S.") + f"{int(local.microsecond / 1000):03d}"


def _fmt_gap(delta: timedelta) -> str:
    total = delta.total_seconds()
    if total < 0:
        total = abs(total)
    if total < 1:
        ms = int(round(total * 1000))
        return f"{ms}ms (same second / fast jump)" if ms else "0s (same tick / instant jump)"
    whole = int(total)
    hours, rem = divmod(whole, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_day_report(report: DayReport, *, max_gap_rows: int = 40) -> str:
    lines: list[str] = []
    lines.append(f"NSE Alert — end-of-day report ({report.report_date.isoformat()})")
    lines.append("")
    lines.append(f"Total alerts: {len(report.events)}")
    lines.append(f"Unique symbols: {report.unique_symbols}")
    up_alerts = report.counts_by_direction.get("UP", 0)
    down_alerts = report.counts_by_direction.get("DOWN", 0)
    lines.append(
        f"Positive movers (UP):   {report.unique_up_symbols} symbols, {up_alerts} alerts"
    )
    lines.append(
        f"Negative movers (DOWN): {report.unique_down_symbols} symbols, {down_alerts} alerts"
    )
    if report.asm_alert_count:
        lines.append(f"Alerts tagged ASM: {report.asm_alert_count}")
    lines.append("")
    lines.append("Crossings by threshold (UP / DOWN):")
    if not report.counts_by_threshold:
        lines.append("  (none)")
    else:
        for thr, total in report.counts_by_threshold.items():
            dirs = report.counts_by_threshold_direction.get(thr, {})
            up = dirs.get("UP", 0)
            down = dirs.get("DOWN", 0)
            lines.append(f"  ±{thr:g}%  →  UP={up}  DOWN={down}  (total {total})")

    lines.append("")
    lines.append("Closed still at/above alert level (unique scrips):")
    if not report.closed_above_by_threshold and not report.symbol_closes:
        lines.append("  (no close prices available)")
    elif not report.closed_above_by_threshold:
        lines.append("  (none held the level into the close)")
    else:
        # Show every threshold that had alerts, even if close-hold count is 0
        for thr in report.counts_by_threshold:
            held = report.closed_above_by_threshold.get(thr, {})
            up = held.get("UP", 0)
            down = held.get("DOWN", 0)
            fired_up = report.counts_by_threshold_direction.get(thr, {}).get("UP", 0)
            fired_down = report.counts_by_threshold_direction.get(thr, {}).get("DOWN", 0)
            lines.append(
                f"  ±{thr:g}%  →  UP closed≥level: {up}/{fired_up}  "
                f"DOWN closed≤-level: {down}/{fired_down}"
            )
        src = report.close_prices_source
        if src == "events_ltp":
            lines.append(
                "  (close ≈ last alert LTP — run report with Kite token after close for true EOD)"
            )
        elif src == "kite":
            lines.append("  (close from Kite quote LTP/OHLC)")

    lines.append("")
    lines.append("Per-scrip close after alert:")
    if not report.symbol_closes:
        lines.append("  (none)")
    else:
        for row in report.symbol_closes:
            thr_label = ",".join(f"{t:g}" for t in row.thresholds_fired)
            lines.append(
                f"  {row.symbol} {row.direction}  "
                f"alerted ±{thr_label}% (at {row.alert_change_pct:+.2f}%)  →  "
                f"close {row.close_change_pct:+.2f}% "
                f"(prev={row.prev_close:.2f} close={row.close_price:.2f})"
            )

    lines.append("")
    lines.append(
        f"Symbols that crossed multiple thresholds: {len(report.multi_level)}"
    )
    if report.gaps:
        lines.append("Time gaps between consecutive levels (IST):")
        for gap in report.gaps[:max_gap_rows]:
            lines.append(
                f"  {gap.symbol} {gap.direction}  "
                f"±{gap.from_threshold:g}% @ {_fmt_ist(gap.from_time)}  →  "
                f"±{gap.to_threshold:g}% @ {_fmt_ist(gap.to_time)}  "
                f"| gap {_fmt_gap(gap.gap)}"
            )
        if len(report.gaps) > max_gap_rows:
            lines.append(f"  … and {len(report.gaps) - max_gap_rows} more")
    else:
        lines.append("  (no multi-level gaps today)")

    return "\n".join(lines)


def write_day_report(report: DayReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_day_report(report) + "\n", encoding="utf-8")
    return path
