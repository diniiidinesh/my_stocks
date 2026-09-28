"""Intraday volume-spike and 52-week breakout signals.

Both run inside ``watch`` alongside the ±% threshold engine, on the same
tick stream. See docs/SIGNALS.md for the rules and message format.

* **Volume spike** — ticks are bucketed into N-minute candles aligned to the
  09:15 IST open (the same buckets Kite's historical candles use). When a
  candle closes, its volume is compared with the EMA of the previous
  candles' volume *before* that candle is folded in; above ``mult`` × EMA
  fires once for that candle. Candle volume comes from the day-cumulative
  ``volume_traded`` field (KiteTicker quote mode), so it needs no extra API.
* **52-week breakout** — LTP above the prior 52-week high (or below the
  prior 52-week low), both computed from daily bars *before* today. Fires
  once per symbol and direction per day, persisted across restarts.

History (prior sessions' intraday candles to warm the EMA, and daily bars
for the 52-week range / yesterday's move) is loaded in a background thread
by ``SignalSeeder`` so the watcher can start streaming immediately; each
symbol arms as soon as its own history arrives.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from nse_alert.session import CONTINUOUS_CLOSE_NON_CAS, SessionClock, to_ist
from nse_alert.universe import Instrument

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

#: Candle sizes Kite's historical API serves, keyed by minutes.
KITE_INTERVALS: dict[int, str] = {
    1: "minute",
    3: "3minute",
    5: "5minute",
    10: "10minute",
    15: "15minute",
    30: "30minute",
    60: "60minute",
}

VOLUME_SPIKE = "VOLUME_SPIKE"
HIGH_52W = "HIGH_52W"
LOW_52W = "LOW_52W"


def parse_timeframes(raw: str | int | list[int]) -> list[int]:
    """Parse ``"5"`` / ``"5,15"`` / ``[5, 15]`` into sorted candle minutes."""
    if isinstance(raw, int):
        values = [raw]
    elif isinstance(raw, list):
        values = [int(v) for v in raw]
    else:
        values = [int(part.strip()) for part in str(raw).split(",") if part.strip()]
    bad = [v for v in values if v not in KITE_INTERVALS]
    if bad:
        allowed = ",".join(str(k) for k in KITE_INTERVALS)
        raise ValueError(f"Unsupported candle minutes {bad}; allowed: {allowed}")
    return sorted(set(values))


def _session_bounds(
    ist_now: datetime, close: dtime = CONTINUOUS_CLOSE_NON_CAS
) -> tuple[datetime, datetime]:
    open_dt = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_dt = ist_now.replace(
        hour=close.hour, minute=close.minute, second=0, microsecond=0
    )
    return open_dt, close_dt


def candle_start(
    now: datetime, minutes: int, close: dtime = CONTINUOUS_CLOSE_NON_CAS
) -> datetime | None:
    """IST start of the ``minutes`` candle containing *now*.

    None outside continuous trading (09:15 → *close*; 15:15 for CAS/F&O
    stocks since Aug 2026, 15:30 for the rest — see ``session.py``).
    """
    ist = to_ist(now)
    open_dt, close_dt = _session_bounds(ist, close)
    if ist < open_dt or ist >= close_dt:
        return None
    idx = int((ist - open_dt).total_seconds() // (minutes * 60))
    return open_dt + timedelta(minutes=idx * minutes)


@dataclass(frozen=True, slots=True)
class SymbolContext:
    """Per-symbol reference levels from daily bars *before* today."""

    prev_close: float
    prev_day_change_pct: float | None
    high_52w: float | None
    low_52w: float | None


def context_from_daily(df: pd.DataFrame, *, today: date) -> SymbolContext | None:
    """Build a ``SymbolContext`` from daily OHLCV (DatetimeIndex, naive IST dates)."""
    if df is None or df.empty:
        return None
    bars = df[df.index.date < today]
    if bars.empty:
        return None
    prev_close = float(bars["close"].iloc[-1])
    prev_day_pct: float | None = None
    if len(bars) >= 2:
        before = float(bars["close"].iloc[-2])
        if before > 0:
            prev_day_pct = (prev_close / before - 1.0) * 100.0
    window = bars[bars.index >= pd.Timestamp(today - timedelta(days=365))]
    if window.empty:
        return SymbolContext(prev_close, prev_day_pct, None, None)
    return SymbolContext(
        prev_close=prev_close,
        prev_day_change_pct=prev_day_pct,
        high_52w=float(window["high"].max()),
        low_52w=float(window["low"].min()),
    )


@dataclass(frozen=True, slots=True)
class Signal:
    kind: str  # VOLUME_SPIKE | HIGH_52W | LOW_52W
    symbol: str
    ltp: float
    prev_close: float
    fired_at: datetime
    context: SymbolContext | None = None
    is_fno: bool = False
    is_asm: bool = False
    # Volume spike only
    timeframe_min: int = 0
    candle_start: datetime | None = None
    candle_volume: int = 0
    volume_ema: float = 0.0
    volume_mult: float = 0.0
    # 52w only: the prior high/low that was broken
    level: float = 0.0

    @property
    def day_change_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.ltp / self.prev_close - 1.0) * 100.0


@dataclass(slots=True)
class _CandleState:
    ema: float | None = None
    samples: int = 0
    seeded: bool = False
    bucket: datetime | None = None
    # Cumulative day volume when ``bucket`` began; None = we joined this
    # candle part-way (startup / feed gap) so its volume is unknowable.
    bucket_start_cum: int | None = None
    last_cum: int = 0
    # The day's last candle has been settled at the continuous close.
    day_done: bool = False


@dataclass(frozen=True, slots=True)
class ClosedCandle:
    symbol: str
    timeframe_min: int
    start: datetime
    volume: int
    ema: float
    mult: float


class VolumeSpikeDetector:
    """Build N-minute candles from cumulative volume; flag volume > mult × EMA."""

    def __init__(
        self,
        *,
        timeframes: list[int],
        ema_period: int = 21,
        mult: float = 2.0,
        skip_opening_candle: bool = True,
        close_for: Callable[[str], dtime] | None = None,
    ) -> None:
        self.timeframes = list(timeframes)
        # Per-symbol continuous close (CAS stocks stop at 15:15).
        self.close_for = close_for or (lambda _s: CONTINUOUS_CLOSE_NON_CAS)
        self.ema_period = max(1, int(ema_period))
        self.alpha = 2.0 / (self.ema_period + 1)
        self.mult = float(mult)
        self.skip_opening_candle = skip_opening_candle
        self._states: dict[tuple[str, int], _CandleState] = {}
        # Ticks arrive on the KiteTicker thread; seeding on the seeder thread.
        self._lock = threading.Lock()

    def _state(self, symbol: str, tf: int) -> _CandleState:
        key = (symbol, tf)
        st = self._states.get(key)
        if st is None:
            st = _CandleState()
            self._states[key] = st
        return st

    def _fold(self, st: _CandleState, volume: float) -> None:
        if st.ema is None:
            st.ema = float(volume)
        else:
            st.ema += self.alpha * (float(volume) - st.ema)
        st.samples += 1

    def is_armed(self, symbol: str, tf: int) -> bool:
        with self._lock:
            st = self._states.get((symbol, tf))
            return bool(st and st.seeded and st.samples >= self.ema_period)

    def seed(
        self,
        symbol: str,
        tf: int,
        candles: list[tuple[datetime, int]],
        *,
        now: datetime | None = None,
    ) -> None:
        """Warm the EMA from historical candles (oldest first).

        Only candles that started before the live candle being built are
        used — Kite returns the still-forming candle too, and the live path
        will account for that one itself. Candles at/after the symbol's
        continuous close are dropped too: for CAS stocks those hold the
        closing-auction print, not continuous-session volume.
        """
        close = self.close_for(symbol)
        with self._lock:
            st = self._state(symbol, tf)
            cutoff = st.bucket or candle_start(
                now or datetime.now(timezone.utc), tf, close
            )
            st.ema = None
            st.samples = 0
            for start, volume in candles:
                if cutoff is not None and start >= cutoff:
                    continue
                if to_ist(start).time() >= close:
                    continue
                self._fold(st, volume)
            st.seeded = True

    def on_tick(
        self, symbol: str, cum_volume: int, now: datetime
    ) -> list[ClosedCandle]:
        """Feed one tick's day-cumulative volume; return candles that spiked."""
        spikes: list[ClosedCandle] = []
        close = self.close_for(symbol)
        ist = to_ist(now)
        with self._lock:
            for tf in self.timeframes:
                st = self._state(symbol, tf)
                start = candle_start(now, tf, close)
                if start is None:
                    # Past the continuous close: settle the day's last candle
                    # once, from volume seen *before* the close, so closing-
                    # auction / post-close prints never land in a candle.
                    if (
                        st.bucket is not None
                        and not st.day_done
                        and st.bucket.date() == ist.date()
                        and ist.time() >= close
                    ):
                        st.day_done = True
                        closed = self._close(symbol, tf, st)
                        if closed is not None:
                            spikes.append(closed)
                    continue
                if st.bucket is None or start.date() != st.bucket.date():
                    st.bucket = start
                    st.day_done = False
                    # volume_traded resets daily, so the day's first candle
                    # always starts from 0 even if we joined late.
                    open_dt, _ = _session_bounds(start)
                    st.bucket_start_cum = 0 if start == open_dt else None
                    st.last_cum = 0
                elif start > st.bucket:
                    closed = self._close(symbol, tf, st)
                    if closed is not None:
                        spikes.append(closed)
                    contiguous = start == st.bucket + timedelta(minutes=tf)
                    st.bucket = start
                    # After a gap (feed reconnect, illiquid name) the volume
                    # between our last tick and now can't be attributed to
                    # one candle — skip the new candle rather than guess.
                    st.bucket_start_cum = st.last_cum if contiguous else None
                st.last_cum = max(st.last_cum, int(cum_volume))
        return spikes

    def _close(self, symbol: str, tf: int, st: _CandleState) -> ClosedCandle | None:
        if st.bucket is None or st.bucket_start_cum is None:
            return None
        volume = max(0, st.last_cum - st.bucket_start_cum)
        if not st.seeded:
            return None
        prior_ema = st.ema
        prior_samples = st.samples
        self._fold(st, volume)
        if prior_ema is None or prior_ema <= 0 or prior_samples < self.ema_period:
            return None
        mult = volume / prior_ema
        if mult <= self.mult:
            return None
        open_dt, _ = _session_bounds(st.bucket)
        if self.skip_opening_candle and st.bucket == open_dt:
            return None
        return ClosedCandle(symbol, tf, st.bucket, volume, prior_ema, mult)


class BreakoutDetector:
    """Fire once per symbol/direction/day when LTP breaks the prior 52w range."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._fired: set[str] = set()
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(IST).date().isoformat()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read signal state: %s", exc)
            return
        if data.get("date") == self._today():
            self._fired = set(data.get("fired_52w", []))

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"date": self._today(), "fired_52w": sorted(self._fired)}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def check(self, symbol: str, ltp: float, ctx: SymbolContext) -> list[tuple[str, float]]:
        hits: list[tuple[str, float]] = []
        if ctx.high_52w and ltp > ctx.high_52w:
            hits.append((HIGH_52W, ctx.high_52w))
        if ctx.low_52w and ltp < ctx.low_52w:
            hits.append((LOW_52W, ctx.low_52w))
        fresh: list[tuple[str, float]] = []
        for kind, level in hits:
            key = f"{symbol}|{kind}"
            if key in self._fired:
                continue
            self._fired.add(key)
            fresh.append((kind, level))
        if fresh:
            self._save()
        return fresh


@dataclass
class SignalMonitor:
    """Glue the detectors to the tick stream and shared per-symbol context."""

    prev_closes: dict[str, float]
    state_path: Path
    timeframes: list[int]
    ema_period: int = 21
    volume_mult: float = 2.0
    skip_opening_candle: bool = True
    volume_enabled: bool = True
    breakout_enabled: bool = True
    fo_symbols: set[str] = field(default_factory=set)
    asm_symbols: set[str] = field(default_factory=set)
    contexts: dict[str, SymbolContext] = field(default_factory=dict)
    clock: SessionClock | None = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = SessionClock(fo_symbols=self.fo_symbols)
        self.volume = VolumeSpikeDetector(
            timeframes=self.timeframes if self.volume_enabled else [],
            ema_period=self.ema_period,
            mult=self.volume_mult,
            skip_opening_candle=self.skip_opening_candle,
            close_for=self.clock.continuous_close,
        )
        self.breakout = BreakoutDetector(self.state_path)

    @property
    def enabled(self) -> bool:
        return self.volume_enabled or self.breakout_enabled

    def set_context(self, symbol: str, ctx: SymbolContext) -> None:
        self.contexts[symbol] = ctx

    def _signal(self, kind: str, symbol: str, ltp: float, now: datetime, **extra: Any) -> Signal:
        return Signal(
            kind=kind,
            symbol=symbol,
            ltp=ltp,
            prev_close=self.prev_closes.get(symbol, 0.0),
            fired_at=now,
            context=self.contexts.get(symbol),
            is_fno=symbol.upper() in self.fo_symbols,
            is_asm=symbol.upper() in self.asm_symbols,
            **extra,
        )

    def on_tick(
        self,
        symbol: str,
        ltp: float,
        volume: int | None = None,
        now: datetime | None = None,
    ) -> list[Signal]:
        if ltp <= 0:
            return []
        now = now or datetime.now(timezone.utc)
        out: list[Signal] = []
        if self.volume_enabled and volume is not None:
            for candle in self.volume.on_tick(symbol, volume, now):
                out.append(
                    self._signal(
                        VOLUME_SPIKE,
                        symbol,
                        ltp,
                        now,
                        timeframe_min=candle.timeframe_min,
                        candle_start=candle.start,
                        candle_volume=candle.volume,
                        volume_ema=candle.ema,
                        volume_mult=candle.mult,
                    )
                )
        # Continuous session only: during the CAS the LTP is an auction
        # print, and nothing after it can be traded intraday anyway.
        if self.breakout_enabled and self.clock.in_continuous(symbol, now):  # type: ignore[union-attr]
            ctx = self.contexts.get(symbol)
            if ctx is not None:
                for kind, level in self.breakout.check(symbol, ltp, ctx):
                    out.append(self._signal(kind, symbol, ltp, now, level=level))
        for sig in out:
            logger.info(
                "SIGNAL %s %s LTP=%.2f day=%+.2f%%%s",
                sig.kind,
                sig.symbol,
                sig.ltp,
                sig.day_change_pct,
                f" vol={sig.volume_mult:.2f}x ({sig.timeframe_min}m)"
                if sig.kind == VOLUME_SPIKE
                else f" level={sig.level:.2f}",
            )
        return out


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def _fmt_px(value: float | None) -> str:
    return "n/a" if not value else f"{value:,.2f}"


def format_signal_message(
    sig: Signal,
    *,
    offers: list[tuple[str, str]] | None = None,
    note: str | None = None,
) -> str:
    """Telegram (Markdown) text for one signal, with optional /confirm lines.

    ``offers`` is ``[(side, pending_id), ...]`` — one pending order per side.
    """
    ctx = sig.context
    high_52w = ctx.high_52w if ctx else None
    low_52w = ctx.low_52w if ctx else None
    prev_day = ctx.prev_day_change_pct if ctx else None

    if sig.kind == VOLUME_SPIKE:
        end = (
            sig.candle_start + timedelta(minutes=sig.timeframe_min)
            if sig.candle_start
            else None
        )
        window = (
            f"{sig.candle_start.astimezone(IST):%H:%M}–{end.astimezone(IST):%H:%M}"
            if sig.candle_start and end
            else "?"
        )
        head = (
            f"📊 *VOLUME SPIKE* — *{sig.symbol}* ({sig.timeframe_min}m)\n"
            f"Volume: `{sig.volume_mult:.2f}x` its {window} candle vs EMA "
            f"(`{sig.candle_volume:,}` vs `{sig.volume_ema:,.0f}`)"
        )
    elif sig.kind == HIGH_52W:
        above = (sig.ltp / sig.level - 1.0) * 100.0 if sig.level else 0.0
        head = (
            f"🚀 *52W HIGH BREAKOUT* — *{sig.symbol}*\n"
            f"Crossed prior 52w high `{_fmt_px(sig.level)}` ({above:+.2f}%)"
        )
    else:
        below = (sig.ltp / sig.level - 1.0) * 100.0 if sig.level else 0.0
        head = (
            f"🔻 *52W LOW BREAKDOWN* — *{sig.symbol}*\n"
            f"Broke prior 52w low `{_fmt_px(sig.level)}` ({below:+.2f}%)"
        )

    lines = [
        head,
        f"Price: `{_fmt_px(sig.ltp)}` | Day: `{sig.day_change_pct:+.2f}%`",
        f"52w high: `{_fmt_px(high_52w)}`"
        + (f" | 52w low: `{_fmt_px(low_52w)}`" if sig.kind != VOLUME_SPIKE else ""),
        f"Yesterday close: `{_fmt_px(sig.prev_close)}` | Yesterday: `{_fmt_pct(prev_day)}`",
    ]
    tags = [t for t, on in (("F&O", sig.is_fno), ("ASM", sig.is_asm)) if on]
    if tags:
        lines.append(f"Tags: *{' · '.join(tags)}*")
    lines.append(f"Time (IST): {sig.fired_at.astimezone(IST):%Y-%m-%d %H:%M:%S}")
    if offers:
        lines.append("")
        lines.append(
            "Order: " + " · ".join(f"{side} `/confirm {pid}`" for side, pid in offers)
        )
        lines.append("_(qty sized at confirm; SL attached)_")
    elif note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


class SignalSeeder:
    """Load history for every symbol in a background thread.

    Per symbol: daily bars (52w range, yesterday's move; shared on-disk cache
    with the EOD screener) and one intraday call per timeframe (volume EMA
    warm-up). Kite's historical API allows ~3 req/s, so ``sleep_sec`` paces
    calls; a 600-name universe with one timeframe arms in ~4-7 minutes (less
    once the daily cache is warm from the previous evening's screener).
    """

    def __init__(
        self,
        *,
        kite: Any,
        instruments: list[Instrument],
        monitor: SignalMonitor,
        daily_cache_dir: Path,
        sleep_sec: float = 0.35,
        intraday_lookback_days: int = 7,
    ) -> None:
        self.kite = kite
        self.instruments = instruments
        self.monitor = monitor
        self.daily_cache_dir = daily_cache_dir
        self.sleep_sec = sleep_sec
        self.intraday_lookback_days = intraday_lookback_days
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="signal-seeder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _pause(self) -> None:
        self._stop.wait(self.sleep_sec)

    def _run(self) -> None:
        from kiteconnect.exceptions import TokenException

        started = time.monotonic()
        ok = failed = 0
        total = len(self.instruments)
        logger.info("Signal history: loading %d symbols in background", total)
        for i, inst in enumerate(self.instruments, start=1):
            if self._stop.is_set():
                return
            try:
                self.seed_symbol(inst)
                ok += 1
            except TokenException as exc:
                logger.error("Signal history stopped — Kite token rejected: %s", exc)
                return
            except Exception as exc:  # noqa: BLE001 — one bad symbol must not stop the rest
                failed += 1
                logger.warning("Signal history failed for %s: %s", inst.symbol, exc)
            if i % 100 == 0:
                logger.info("Signal history: %d/%d symbols", i, total)
        logger.info(
            "Signal history ready: %d ok, %d failed in %.0fs",
            ok,
            failed,
            time.monotonic() - started,
        )

    def seed_symbol(self, inst: Instrument) -> None:
        today = datetime.now(IST).date()
        daily = self._daily(inst, today)
        ctx = context_from_daily(daily, today=today)
        if ctx is not None:
            self.monitor.set_context(inst.symbol, ctx)
        if not self.monitor.volume_enabled:
            return
        for tf in self.monitor.timeframes:
            if self._stop.is_set():
                return
            candles = self._intraday(inst, tf)
            self.monitor.volume.seed(inst.symbol, tf, candles)

    def _daily(self, inst: Instrument, today: date) -> pd.DataFrame:
        from nse_alert.screener.history import (
            fetch_history_kite,
            history_cache_is_fresh,
            load_cached_history,
            save_cached_history,
        )

        path = self.daily_cache_dir / f"{inst.symbol.upper()}.csv"
        cached = load_cached_history(path)
        if (
            cached is not None
            and not cached.empty
            and history_cache_is_fresh(cached.index.max().date())
        ):
            return cached
        df = fetch_history_kite(self.kite, instrument_token=inst.instrument_token, days=400)
        self._pause()
        # Kite includes today's still-forming daily bar. Never cache it: the
        # screener would treat a morning snapshot as today's final close.
        df = df[df.index.date < today]
        if not df.empty:
            save_cached_history(path, df)
        return df

    def _intraday(self, inst: Instrument, tf: int) -> list[tuple[datetime, int]]:
        now = datetime.now(IST)
        start = now - timedelta(days=self.intraday_lookback_days)
        raw = self.kite.historical_data(
            inst.instrument_token,
            start.strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%Y-%m-%d %H:%M:%S"),
            KITE_INTERVALS[tf],
        )
        self._pause()
        candles: list[tuple[datetime, int]] = []
        for row in raw or []:
            ts = row.get("date")
            if not isinstance(ts, datetime):
                ts = pd.Timestamp(ts).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            candles.append((ts, int(row.get("volume") or 0)))
        candles.sort(key=lambda c: c[0])
        return candles
