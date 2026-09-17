"""Analyze persisted live alert events (hypothesis generation from VM history)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nse_alert.engine import Alert, alert_from_event
from nse_alert.report import load_events


@dataclass(frozen=True, slots=True)
class LocalAnalysis:
    days: tuple[date, ...]
    events: list[Alert]
    by_threshold_dir: dict[tuple[float, str], int]
    unique_symbols: int
    multi_level_symbols: int
    notes: tuple[str, ...]


def _load_fired_file(path: Path) -> list[Alert]:
    import json

    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", [])
    out: list[Alert] = []
    for item in events:
        if isinstance(item, dict):
            out.append(alert_from_event(item))
    return out


def load_events_for_days(
    state_dir: Path,
    days: list[date] | None = None,
) -> list[Alert]:
    """Load alerts from ``fired.json`` and optional ``fired-YYYY-MM-DD.json`` archives."""
    collected: list[Alert] = []
    seen_keys: set[str] = set()

    candidates = [state_dir / "fired.json"]
    candidates.extend(sorted(state_dir.glob("fired-*.json")))
    candidates.extend(sorted(state_dir.glob("**/fired*.json")))

    for path in candidates:
        if not path.is_file():
            continue
        # Prefer dated archive loader; fall back to full file parse.
        day_hint: date | None = None
        name = path.name
        if name.startswith("fired-") and name.endswith(".json"):
            try:
                day_hint = date.fromisoformat(name[len("fired-") : -len(".json")])
            except ValueError:
                day_hint = None

        if day_hint is not None and days is not None and day_hint not in days:
            continue

        if day_hint is not None:
            batch = load_events(path, as_of=day_hint)
            if not batch:
                batch = _load_fired_file(path)
        else:
            batch = _load_fired_file(path)
            if days is not None:
                batch = [e for e in batch if e.fired_at.date() in days]

        for ev in batch:
            key = (
                f"{ev.symbol}|{ev.direction}|{ev.threshold_pct}|"
                f"{ev.fired_at.isoformat()}"
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append(ev)

    collected.sort(key=lambda e: e.fired_at)
    return collected


def analyze_local_events(events: list[Alert]) -> LocalAnalysis:
    if not events:
        return LocalAnalysis(
            days=(),
            events=[],
            by_threshold_dir={},
            unique_symbols=0,
            multi_level_symbols=0,
            notes=("No events found.",),
        )

    days = tuple(sorted({e.fired_at.date() for e in events}))
    by_td: dict[tuple[float, str], int] = Counter(
        (float(e.threshold_pct), e.direction) for e in events
    )
    symbols = {e.symbol for e in events}

    levels_by_sym: dict[tuple[str, str, date], set[float]] = defaultdict(set)
    for e in events:
        levels_by_sym[(e.symbol, e.direction, e.fired_at.date())].add(
            float(e.threshold_pct)
        )
    multi = sum(1 for levels in levels_by_sym.values() if len(levels) >= 2)

    notes = [
        f"Sessions covered: {len(days)} ({days[0]} → {days[-1]})",
        f"Total alert events: {len(events)} across {len(symbols)} symbols",
        f"Multi-level climbs (same symbol/dir/day, ≥2 thresholds): {multi}",
        "This slice is for hypotheses only — not a strategy proof.",
    ]
    return LocalAnalysis(
        days=days,
        events=events,
        by_threshold_dir=dict(sorted(by_td.items())),
        unique_symbols=len(symbols),
        multi_level_symbols=multi,
        notes=tuple(notes),
    )


def format_analysis(analysis: LocalAnalysis) -> str:
    lines = ["=== Local alert analysis ===", *analysis.notes, "", "Counts by level × direction:"]
    if not analysis.by_threshold_dir:
        lines.append("  (none)")
    else:
        for (thr, direction), n in analysis.by_threshold_dir.items():
            lines.append(f"  ±{thr:g}% {direction:<4} → {n}")
    lines.append("")
    lines.append("Tip: archive each day on the VM as `.nse_alert/fired-YYYY-MM-DD.json`")
    lines.append("before the next session overwrites `fired.json`.")
    return "\n".join(lines)
