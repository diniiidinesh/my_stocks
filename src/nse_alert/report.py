from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nse_alert.engine import Alert, alert_from_event

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
class DayReport:
    report_date: date
    events: list[Alert]
    counts_by_threshold: dict[float, int]
    counts_by_direction: dict[str, int]
    unique_symbols: int
    multi_level: list[tuple[str, str, list[Alert]]]
    gaps: list[ThresholdGap]


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


def build_day_report(events: list[Alert], *, report_date: date | None = None) -> DayReport:
    day = report_date or date.today()
    counts_by_threshold: Counter[float] = Counter()
    counts_by_direction: Counter[str] = Counter()
    by_symbol_dir: dict[tuple[str, str], list[Alert]] = defaultdict(list)

    for alert in events:
        counts_by_threshold[alert.threshold_pct] += 1
        counts_by_direction[alert.direction] += 1
        by_symbol_dir[(alert.symbol, alert.direction)].append(alert)

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

    return DayReport(
        report_date=day,
        events=events,
        counts_by_threshold=dict(sorted(counts_by_threshold.items())),
        counts_by_direction=dict(counts_by_direction),
        unique_symbols=len({a.symbol for a in events}),
        multi_level=multi_level,
        gaps=gaps,
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
    if report.counts_by_direction:
        up = report.counts_by_direction.get("UP", 0)
        down = report.counts_by_direction.get("DOWN", 0)
        lines.append(f"Direction: UP={up}  DOWN={down}")
    lines.append("")
    lines.append("Crossings by threshold:")
    if not report.counts_by_threshold:
        lines.append("  (none)")
    else:
        for thr, count in report.counts_by_threshold.items():
            lines.append(f"  ±{thr:g}%  →  {count}")

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
