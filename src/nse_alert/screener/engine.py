from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from nse_alert.screener.history import get_daily_history
from nse_alert.screener.indicators import enrich_ohlcv, find_volume_spikes
from nse_alert.screener.delivery import DeliveryBook, parse_iso_date
from nse_alert.screener.market_cap import (
    fetch_market_caps_yfinance,
    load_index_symbols,
    load_market_cap_file,
)
from nse_alert.universe import load_nse_eq_instruments

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ScreenConfig:
    min_market_cap_cr: float = 5000.0
    min_turnover_cr: float = 10.0
    min_price: float = 20.0
    lookback_days: int = 20
    volume_ema_period: int = 20
    volume_mult: float = 1.5
    supertrend_period: int = 10
    supertrend_mult: float = 3.0
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    adx_period: int = 14
    adx_min: float = 25.0
    rsi_period: int = 14
    rsi_min: float = 40.0
    rsi_max: float = 60.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    near_52w_high_pct: float = 5.0
    require_volume: bool = True
    require_adx: bool = True
    require_rsi: bool = True
    require_macd: bool = True
    require_near_52w: bool = True
    require_delivery: bool = True
    min_delivery_pct: float = 40.0
    history_days: int = 400
    after_hhmm: int = 1540
    max_symbols: int = 0
    prefer_kite_history: bool = True
    universe_mode: str = "index_mcap"  # index_mcap | custom
    market_cap_file: str = ""


@dataclass
class ScreenRow:
    symbol: str
    name: str
    close: float
    market_cap_cr: float
    turnover_cr: float
    ema_fast: float
    ema_mid: float
    ema_slow: float
    supertrend: float
    adx: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    high_52w: float
    pct_from_52w_high: float
    pass_ema_stack: bool
    pass_supertrend: bool
    pass_volume: bool
    pass_adx: bool
    pass_rsi: bool
    pass_macd: bool
    pass_near_52w: bool
    pass_delivery: bool
    mandatory_pass: bool
    optional_score: int
    optional_total: int
    all_pass: bool
    vol_spike_count: int
    vol_spike_best_mult: float
    vol_spike_days: str
    vol_spike_detail: str
    deliv_pct_max_on_spikes: float
    deliv_spike_days: str
    deliv_spike_detail: str
    as_of: str


@dataclass
class ScreenResult:
    as_of: date
    rows: list[ScreenRow]
    scanned: int
    skipped: int
    config: ScreenConfig
    frame: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)

    @property
    def mandatory_count(self) -> int:
        return sum(1 for r in self.rows if r.mandatory_pass)

    @property
    def all_pass_count(self) -> int:
        return sum(1 for r in self.rows if r.all_pass)


def market_closed_enough(*, after_hhmm: int = 1540, now: datetime | None = None) -> bool:
    """True if local IST time is on/after the configured HHMM (default 15:40)."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    hhmm = now.hour * 100 + now.minute
    return hhmm >= after_hhmm


def _turnover_from_quotes(quote_map: dict[str, dict[str, Any]], symbol: str) -> tuple[float, float]:
    q = quote_map.get(f"NSE:{symbol}", {}) or {}
    ohlc = q.get("ohlc") or {}
    prev_close = float(ohlc.get("close") or 0.0)
    last_price = float(q.get("last_price") or prev_close or 0.0)
    volume = float(q.get("volume") or 0.0)
    turnover_cr = (volume * last_price / 1e7) if volume and last_price else 0.0
    return last_price, turnover_cr


def _build_candidate_symbols(
    cfg: ScreenConfig,
    *,
    kite: Any | None,
    custom_symbols: list[str] | None,
) -> tuple[list[str], dict[str, int], dict[str, str]]:
    token_by: dict[str, int] = {}
    name_by: dict[str, str] = {}

    if custom_symbols:
        symbols = [s.upper() for s in custom_symbols]
    else:
        index_syms = load_index_symbols()
        if not index_syms:
            raise RuntimeError(
                "Could not load Nifty 500 / Smallcap 250 lists. "
                "Set SCREEN_MARKET_CAP_FILE or CUSTOM_UNIVERSE_FILE."
            )
        symbols = sorted(index_syms)

    if kite is not None:
        try:
            nse = load_nse_eq_instruments(kite)
            token_by = dict(
                zip(
                    nse["tradingsymbol"].tolist(),
                    nse["instrument_token"].astype(int).tolist(),
                    strict=True,
                )
            )
            name_by = dict(
                zip(
                    nse["tradingsymbol"].tolist(),
                    nse["name"].fillna("").astype(str).tolist(),
                    strict=True,
                )
            )
            if not custom_symbols:
                symbols = [s for s in symbols if s in token_by]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load Kite instruments: %s", exc)

    if cfg.max_symbols and cfg.max_symbols > 0:
        symbols = symbols[: cfg.max_symbols]
    return symbols, token_by, name_by


def evaluate_symbol(
    symbol: str,
    df: pd.DataFrame,
    *,
    cfg: ScreenConfig,
    name: str = "",
    market_cap_cr: float = 0.0,
    turnover_cr: float = 0.0,
    delivery_book: DeliveryBook | None = None,
) -> ScreenRow | None:
    if df is None or len(df) < max(cfg.ema_slow, 60) + 5:
        return None
    enriched = enrich_ohlcv(
        df,
        ema_fast=cfg.ema_fast,
        ema_mid=cfg.ema_mid,
        ema_slow=cfg.ema_slow,
        st_period=cfg.supertrend_period,
        st_mult=cfg.supertrend_mult,
        adx_period=cfg.adx_period,
        rsi_period=cfg.rsi_period,
        macd_fast=cfg.macd_fast,
        macd_slow=cfg.macd_slow,
        macd_signal=cfg.macd_signal,
        volume_ema_period=cfg.volume_ema_period,
    )
    last = enriched.iloc[-1]
    close = float(last["close"])
    if close < cfg.min_price:
        return None

    spikes = find_volume_spikes(
        enriched,
        lookback_days=cfg.lookback_days,
        ema_period=cfg.volume_ema_period,
        multiple=cfg.volume_mult,
    )
    pass_volume = len(spikes) > 0
    spike_days = ", ".join(f"T-{s.days_ago}" for s in spikes) if spikes else ""
    spike_detail = (
        "; ".join(
            f"T-{s.days_ago} ({s.date}) {s.multiple:.2f}x "
            f"(vol={s.volume:.0f} vs EMA={s.volume_ema:.0f})"
            for s in spikes
        )
        if spikes
        else ""
    )
    best_mult = max((s.multiple for s in spikes), default=0.0)

    # Delivery % on spike days (missing days ignored)
    deliv_known: list[tuple[int, str, float]] = []
    deliv_parts: list[str] = []
    for s in spikes:
        day = parse_iso_date(s.date)
        info = delivery_book.get(symbol, day) if delivery_book and day else None
        if info is None:
            deliv_parts.append(f"T-{s.days_ago}:n/a")
            continue
        deliv_known.append((s.days_ago, s.date, info.delivery_pct))
        deliv_parts.append(f"T-{s.days_ago}:{info.delivery_pct:.1f}%")
    deliv_pct_max = max((p for _, _, p in deliv_known), default=float("nan"))
    qualifying = [
        (dago, dt, pct)
        for dago, dt, pct in deliv_known
        if pct >= cfg.min_delivery_pct
    ]
    # Pass if ANY spike day with known delivery meets the threshold.
    # Days with missing data are ignored (neither pass nor fail on their own).
    pass_delivery = len(qualifying) > 0
    deliv_spike_days = (
        ", ".join(f"T-{dago}" for dago, _, _ in qualifying) if qualifying else ""
    )
    deliv_spike_detail = "; ".join(deliv_parts) if deliv_parts else ""

    ema_f = float(last["ema_fast"])
    ema_m = float(last["ema_mid"])
    ema_s = float(last["ema_slow"])
    pass_ema = ema_f > ema_m > ema_s
    st = float(last["supertrend"])
    pass_st = (
        pd.notna(st)
        and float(last["st_dir"]) > 0
        and close > st
    )
    adx_v = float(last["adx"]) if pd.notna(last["adx"]) else float("nan")
    pass_adx = bool(pd.notna(adx_v) and adx_v > cfg.adx_min)
    rsi_v = float(last["rsi"]) if pd.notna(last["rsi"]) else float("nan")
    pass_rsi = bool(pd.notna(rsi_v) and cfg.rsi_min <= rsi_v <= cfg.rsi_max)
    macd_v = float(last["macd"]) if pd.notna(last["macd"]) else float("nan")
    macd_sig = float(last["macd_signal"]) if pd.notna(last["macd_signal"]) else float("nan")
    macd_h = float(last["macd_hist"]) if pd.notna(last["macd_hist"]) else float("nan")
    pass_macd = bool(pd.notna(macd_v) and macd_v > 0)
    high_52w = float(last["high_52w"]) if pd.notna(last["high_52w"]) else float("nan")
    if high_52w and high_52w > 0 and pd.notna(high_52w):
        pct_from_high = (high_52w - close) / high_52w * 100.0
    else:
        pct_from_high = float("nan")
    pass_near = bool(
        pd.notna(pct_from_high) and 0 <= pct_from_high <= cfg.near_52w_high_pct
    )

    mandatory = pass_ema and pass_st
    optional_flags: list[tuple[bool, bool]] = [
        (cfg.require_volume, pass_volume),
        (cfg.require_adx, pass_adx),
        (cfg.require_rsi, pass_rsi),
        (cfg.require_macd, pass_macd),
        (cfg.require_near_52w, pass_near),
        (cfg.require_delivery, pass_delivery),
    ]
    enabled = [(need, ok) for need, ok in optional_flags if need]
    optional_total = len(enabled)
    optional_score = sum(1 for _, ok in enabled if ok)
    all_pass = mandatory and optional_score == optional_total and optional_total >= 0
    # If no optional filters enabled, all_pass == mandatory
    if optional_total == 0:
        all_pass = mandatory

    as_of = enriched.index[-1]
    as_of_str = as_of.strftime("%Y-%m-%d") if hasattr(as_of, "strftime") else str(as_of)[:10]

    return ScreenRow(
        symbol=symbol.upper(),
        name=name or symbol.upper(),
        close=round(close, 2),
        market_cap_cr=round(market_cap_cr, 2),
        turnover_cr=round(turnover_cr, 2),
        ema_fast=round(ema_f, 2),
        ema_mid=round(ema_m, 2),
        ema_slow=round(ema_s, 2),
        supertrend=round(st, 2) if pd.notna(st) else float("nan"),
        adx=round(adx_v, 2) if pd.notna(adx_v) else float("nan"),
        rsi=round(rsi_v, 2) if pd.notna(rsi_v) else float("nan"),
        macd=round(macd_v, 4) if pd.notna(macd_v) else float("nan"),
        macd_signal=round(macd_sig, 4) if pd.notna(macd_sig) else float("nan"),
        macd_hist=round(macd_h, 4) if pd.notna(macd_h) else float("nan"),
        high_52w=round(high_52w, 2) if pd.notna(high_52w) else float("nan"),
        pct_from_52w_high=round(pct_from_high, 2) if pd.notna(pct_from_high) else float("nan"),
        pass_ema_stack=pass_ema,
        pass_supertrend=pass_st,
        pass_volume=pass_volume,
        pass_adx=pass_adx,
        pass_rsi=pass_rsi,
        pass_macd=pass_macd,
        pass_near_52w=pass_near,
        pass_delivery=pass_delivery,
        mandatory_pass=mandatory,
        optional_score=optional_score,
        optional_total=optional_total,
        all_pass=all_pass,
        vol_spike_count=len(spikes),
        vol_spike_best_mult=best_mult,
        vol_spike_days=spike_days,
        vol_spike_detail=spike_detail,
        deliv_pct_max_on_spikes=(
            round(deliv_pct_max, 2) if pd.notna(deliv_pct_max) else float("nan")
        ),
        deliv_spike_days=deliv_spike_days,
        deliv_spike_detail=deliv_spike_detail,
        as_of=as_of_str,
    )


def _sort_rows(rows: list[ScreenRow]) -> list[ScreenRow]:
    """Full-criteria matches first, then higher optional score, then closer to 52w high."""

    def key(r: ScreenRow) -> tuple:
        pct = r.pct_from_52w_high if pd.notna(r.pct_from_52w_high) else 999.0
        return (
            0 if r.all_pass else 1,
            0 if r.mandatory_pass else 1,
            -r.optional_score,
            pct,
            -r.vol_spike_best_mult,
            r.symbol,
        )

    return sorted(rows, key=key)


def rows_to_frame(rows: list[ScreenRow]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([asdict(r) for r in rows])


def run_screener(
    cfg: ScreenConfig,
    *,
    state_dir: Path,
    kite: Any | None = None,
    custom_symbols: list[str] | None = None,
    quote_map: dict[str, dict[str, Any]] | None = None,
) -> ScreenResult:
    """Scan universe, score criteria, return rows sorted with all-pass on top."""
    symbols, token_by, name_by = _build_candidate_symbols(
        cfg, kite=kite, custom_symbols=custom_symbols
    )
    logger.info("Screener candidates: %d symbols", len(symbols))

    # Market cap
    mcaps: dict[str, float] = {}
    if cfg.market_cap_file:
        mcaps = load_market_cap_file(Path(cfg.market_cap_file))
    cache_path = state_dir / "screener" / "market_caps.csv"
    missing = [s for s in symbols if s not in mcaps]
    if missing:
        fetched = fetch_market_caps_yfinance(missing, cache_path=cache_path)
        mcaps.update(fetched)

    if cfg.min_market_cap_cr > 0:
        before = len(symbols)
        symbols = [s for s in symbols if mcaps.get(s, 0.0) >= cfg.min_market_cap_cr]
        logger.info(
            "Market cap >= %.0f Cr: %d → %d",
            cfg.min_market_cap_cr,
            before,
            len(symbols),
        )

    # Turnover via Kite quotes when available
    turnovers: dict[str, float] = {}
    if quote_map is None and kite is not None and symbols:
        from nse_alert.universe import _quote_batches

        try:
            quote_map = _quote_batches(kite, symbols)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Quote batch failed (%s); skipping live turnover filter", exc)
            quote_map = {}
    quote_map = quote_map or {}
    if quote_map and cfg.min_turnover_cr > 0:
        kept: list[str] = []
        for sym in symbols:
            _, tovr = _turnover_from_quotes(quote_map, sym)
            turnovers[sym] = tovr
            if tovr >= cfg.min_turnover_cr:
                kept.append(sym)
        logger.info(
            "Turnover >= %.1f Cr: %d → %d",
            cfg.min_turnover_cr,
            len(symbols),
            len(kept),
        )
        symbols = kept
    elif quote_map:
        for sym in symbols:
            _, tovr = _turnover_from_quotes(quote_map, sym)
            turnovers[sym] = tovr

    delivery_book = DeliveryBook(state_dir / "screener" / "bhavcopy")
    try:
        delivery_book.prefetch_lookback(
            as_of=date.today(), lookback_days=cfg.lookback_days
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Delivery bhavcopy prefetch failed: %s", exc)

    hist_dir = state_dir / "screener" / "history"
    rows: list[ScreenRow] = []
    skipped = 0
    for i, sym in enumerate(symbols, start=1):
        df = get_daily_history(
            sym,
            cache_dir=hist_dir,
            days=cfg.history_days,
            kite=kite,
            instrument_token=token_by.get(sym),
            prefer_kite=cfg.prefer_kite_history,
        )
        row = evaluate_symbol(
            sym,
            df,
            cfg=cfg,
            name=name_by.get(sym, sym),
            market_cap_cr=mcaps.get(sym, 0.0),
            turnover_cr=turnovers.get(sym, 0.0),
            delivery_book=delivery_book,
        )
        if row is None:
            skipped += 1
        else:
            rows.append(row)
        if i % 25 == 0 or i == len(symbols):
            logger.info("History/screen progress %d / %d (rows=%d)", i, len(symbols), len(rows))

    # Keep mandatory-pass names in the main ranking list; still include others below.
    ranked = _sort_rows(rows)
    frame = rows_to_frame(ranked)
    as_of = date.today()
    if ranked:
        try:
            as_of = date.fromisoformat(ranked[0].as_of)
        except ValueError:
            pass
    return ScreenResult(
        as_of=as_of,
        rows=ranked,
        scanned=len(symbols),
        skipped=skipped,
        config=cfg,
        frame=frame,
    )
