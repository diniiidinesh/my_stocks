from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from nse_alert.screener.engine import ScreenConfig, ScreenResult


def write_screener_excel(result: ScreenResult, path: Path) -> Path:
    """Write ranked Excel: all-pass on top within Hits; full table + config sheets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df = result.frame.copy()
    if df.empty:
        df = pd.DataFrame(columns=["symbol", "all_pass", "mandatory_pass", "optional_score"])

    # Values first; pass/fail flags on the right for scanning
    preferred = [
        "symbol",
        "name",
        "close",
        "market_cap_cr",
        "turnover_cr",
        "pct_from_52w_high",
        "optional_score",
        "optional_total",
        "ema_fast",
        "ema_mid",
        "ema_slow",
        "supertrend",
        "adx",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "high_52w",
        "vol_spike_count",
        "vol_spike_best_mult",
        "vol_spike_days",
        "vol_spike_detail",
        "as_of",
        "all_pass",
        "mandatory_pass",
        "pass_ema_stack",
        "pass_supertrend",
        "pass_volume",
        "pass_adx",
        "pass_rsi",
        "pass_macd",
        "pass_near_52w",
    ]
    cols = [c for c in preferred if c in df.columns] + [
        c for c in df.columns if c not in preferred
    ]
    df = df[cols]

    hits = df[df["mandatory_pass"] == True] if "mandatory_pass" in df.columns else df  # noqa: E712
    all_pass = df[df["all_pass"] == True] if "all_pass" in df.columns else df.iloc[0:0]  # noqa: E712

    cfg_rows = [{"parameter": k, "value": v} for k, v in asdict(result.config).items()]
    summary = pd.DataFrame(
        [
            {"metric": "as_of", "value": str(result.as_of)},
            {"metric": "scanned", "value": result.scanned},
            {"metric": "evaluated_rows", "value": len(result.rows)},
            {"metric": "skipped_insufficient_history", "value": result.skipped},
            {"metric": "mandatory_pass", "value": result.mandatory_count},
            {"metric": "all_criteria_pass", "value": result.all_pass_count},
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        all_pass.to_excel(writer, sheet_name="AllPass", index=False)
        hits.to_excel(writer, sheet_name="MandatoryHits", index=False)
        df.to_excel(writer, sheet_name="AllRanked", index=False)
        pd.DataFrame(cfg_rows).to_excel(writer, sheet_name="Config", index=False)
    return path


def format_screener_summary(result: ScreenResult, *, excel_name: str = "") -> str:
    lines = [
        f"EOD TA Screener — {result.as_of.isoformat()}",
        f"Scanned: {result.scanned} | Evaluated: {len(result.rows)} | "
        f"Skipped: {result.skipped}",
        f"Mandatory (EMA stack + SuperTrend): {result.mandatory_count}",
        f"All criteria: {result.all_pass_count}",
        "",
    ]
    tops = [r for r in result.rows if r.all_pass][:15]
    if tops:
        lines.append("Top all-pass (already ranked):")
        for r in tops:
            spike = f" spikes={r.vol_spike_days}" if r.vol_spike_days else ""
            lines.append(
                f"• {r.symbol} close={r.close:.2f} "
                f"ADX={r.adx:.1f} RSI={r.rsi:.1f} "
                f"from52w={r.pct_from_52w_high:.1f}%{spike}"
            )
    else:
        lines.append("No names passed every enabled filter today.")
        # show best mandatory hits
        mand = [r for r in result.rows if r.mandatory_pass][:10]
        if mand:
            lines.append("Best mandatory hits:")
            for r in mand:
                lines.append(
                    f"• {r.symbol} score={r.optional_score}/{r.optional_total} "
                    f"close={r.close:.2f}"
                )
    if excel_name:
        lines.append("")
        lines.append(f"Excel: {excel_name}")
    return "\n".join(lines)


def config_from_settings(settings: object) -> ScreenConfig:
    """Map Settings fields (screen_*) onto ScreenConfig."""
    g = settings.__getattribute__
    return ScreenConfig(
        min_market_cap_cr=float(g("screen_min_market_cap_cr")),
        min_turnover_cr=float(g("screen_min_turnover_cr")),
        min_price=float(g("screen_min_price")),
        lookback_days=int(g("screen_lookback_days")),
        volume_ema_period=int(g("screen_volume_ema_period")),
        volume_mult=float(g("screen_volume_mult")),
        supertrend_period=int(g("screen_supertrend_period")),
        supertrend_mult=float(g("screen_supertrend_mult")),
        ema_fast=int(g("screen_ema_fast")),
        ema_mid=int(g("screen_ema_mid")),
        ema_slow=int(g("screen_ema_slow")),
        adx_period=int(g("screen_adx_period")),
        adx_min=float(g("screen_adx_min")),
        rsi_period=int(g("screen_rsi_period")),
        rsi_min=float(g("screen_rsi_min")),
        rsi_max=float(g("screen_rsi_max")),
        macd_fast=int(g("screen_macd_fast")),
        macd_slow=int(g("screen_macd_slow")),
        macd_signal=int(g("screen_macd_signal")),
        near_52w_high_pct=float(g("screen_near_52w_high_pct")),
        require_volume=bool(g("screen_require_volume")),
        require_adx=bool(g("screen_require_adx")),
        require_rsi=bool(g("screen_require_rsi")),
        require_macd=bool(g("screen_require_macd")),
        require_near_52w=bool(g("screen_require_near_52w")),
        history_days=int(g("screen_history_days")),
        after_hhmm=int(g("screen_after_hhmm")),
        max_symbols=int(g("screen_max_symbols")),
        prefer_kite_history=bool(g("screen_prefer_kite_history")),
        market_cap_file=str(g("screen_market_cap_file") or ""),
    )
