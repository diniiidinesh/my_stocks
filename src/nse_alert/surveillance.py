from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

# Zerodha Utilities → "Consolidated list of scrips blocked/allowed by RMS"
# (not available via Kite Connect APIs).
_DEFAULT_ASM_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1XwWNCASDmrXfx5LtFNna0Kmkt5vHtqkjICvVcUZaQhw/export?format=csv&gid=1228122613"
)


def load_asm_symbols(
    *,
    url: str = "",
    cache_path: Path | None = None,
    enabled: bool = True,
) -> set[str]:
    """Return NSE cash symbols currently under long/short-term ASM.

    Soft-fails to an empty set if the sheet cannot be fetched (alerts still run).
    """
    if not enabled:
        return set()

    if cache_path is not None and cache_path.exists():
        try:
            cached = {
                line.strip().upper()
                for line in cache_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
            if cached:
                logger.info("Loaded %d ASM symbols from cache %s", len(cached), cache_path)
                return cached
        except OSError as exc:
            logger.warning("Could not read ASM cache: %s", exc)

    sheet_url = (url or _DEFAULT_ASM_CSV_URL).strip()
    try:
        symbols = _fetch_asm_symbols(sheet_url)
    except Exception as exc:  # noqa: BLE001 — soft-fail for optional enrichment
        logger.warning("ASM list unavailable (%s); alerts will omit ASM tags", exc)
        return set()

    if cache_path is not None and symbols:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                "# Auto-refreshed ASM symbols (Zerodha RMS sheet)\n"
                + "\n".join(sorted(symbols))
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write ASM cache: %s", exc)

    logger.info("Loaded %d ASM symbols from Zerodha RMS sheet", len(symbols))
    return symbols


def _fetch_asm_symbols(url: str) -> set[str]:
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    text = resp.text
    if "<html" in text[:200].lower():
        raise RuntimeError("ASM sheet returned HTML instead of CSV")

    # Header row 0 is section titles; row 1 has SYMBOL columns for LT + ST ASM.
    df = pd.read_csv(StringIO(text), header=1)
    symbols: set[str] = set()
    for col in df.columns:
        # Duplicate headers become SYMBOL / SYMBOL.1 / Unnamed… — keep SYMBOL*
        col_name = str(col).strip().upper()
        if not col_name.startswith("SYMBOL"):
            continue
        for value in df[col].dropna().astype(str):
            sym = value.strip().upper()
            if sym and not sym.startswith("SYMBOL") and sym[0].isalpha():
                symbols.add(sym)
    if not symbols:
        raise RuntimeError("ASM CSV parsed but no SYMBOL rows found")
    return symbols


def load_nfo_equity_underlyings(kite: Any) -> set[str]:
    """Cash EQ symbols that have NSE F&O futures (from Kite NFO instrument dump)."""
    instruments = kite.instruments("NFO")
    if not instruments:
        raise RuntimeError("Kite returned no NFO instruments")
    underlyings: set[str] = set()
    for row in instruments:
        if str(row.get("instrument_type", "")).upper() != "FUT":
            continue
        if str(row.get("segment", "")).upper() not in {"NFO-FUT", "NFO"}:
            # Still accept if exchange is NFO — segment naming varies slightly.
            if str(row.get("exchange", "")).upper() != "NFO":
                continue
        name = str(row.get("name") or "").strip().upper()
        if name and name[0].isalpha() and "-" not in name:
            underlyings.add(name)
    logger.info("NFO equity underlyings (FUT): %d symbols", len(underlyings))
    if not underlyings:
        raise RuntimeError("No NFO FUT underlyings found in Kite instruments")
    return underlyings
