from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

_NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
_SMALLCAP250_URL = (
    "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv"
)


def load_index_symbols(urls: list[str] | None = None) -> set[str]:
    """Nifty 500 ∪ Smallcap 250 — covers large/mid/small names."""
    urls = urls or [_NIFTY500_URL, _SMALLCAP250_URL]
    symbols: set[str] = set()
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
    with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = client.get(url)
                resp.raise_for_status()
                from io import StringIO

                df = pd.read_csv(StringIO(resp.text))
                col = "Symbol" if "Symbol" in df.columns else df.columns[2]
                for sym in df[col].astype(str).str.strip().str.upper():
                    if sym and sym != "NAN":
                        symbols.add(sym)
                logger.info("Loaded %d symbols from %s", len(df), url.split("/")[-1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Index list fetch failed (%s): %s", url, exc)
    return symbols


def load_market_cap_file(path: Path) -> dict[str, float]:
    """CSV with columns symbol, market_cap_cr (or market_cap in INR)."""
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    sym_col = cols.get("symbol") or cols.get("tradingsymbol")
    if not sym_col:
        raise ValueError(f"Market cap file {path} needs a symbol column")
    out: dict[str, float] = {}
    if "market_cap_cr" in cols:
        for _, row in df.iterrows():
            try:
                out[str(row[sym_col]).strip().upper()] = float(row[cols["market_cap_cr"]])
            except (TypeError, ValueError):
                continue
        return out
    mcap_col = cols.get("market_cap") or cols.get("marketcap")
    if not mcap_col:
        raise ValueError(f"Market cap file {path} needs market_cap_cr or market_cap")
    for _, row in df.iterrows():
        try:
            rupees = float(row[mcap_col])
            out[str(row[sym_col]).strip().upper()] = rupees / 1e7
        except (TypeError, ValueError):
            continue
    return out


def fetch_market_caps_yfinance(
    symbols: list[str],
    *,
    cache_path: Path | None = None,
    max_age_hours: float = 24.0,
    sleep_sec: float = 0.05,
) -> dict[str, float]:
    """Return market cap in ₹ crore via Yahoo Finance fast_info.

    Uses a fresh on-disk cache when present, but still fetches any
    requested symbols that are missing from that cache (partial smoke
    caches must not shrink the universe).
    """
    import yfinance as yf

    out: dict[str, float] = {}
    if cache_path and cache_path.exists():
        age_h = (time.time() - cache_path.stat().st_mtime) / 3600.0
        if age_h <= max_age_hours:
            out = load_market_cap_file(cache_path)
            if out:
                logger.info("Loaded market-cap cache (%d names, age %.1fh)", len(out), age_h)

    wanted = [s.upper() for s in symbols]
    missing = [s for s in wanted if s not in out]
    if not missing:
        return {s: out[s] for s in wanted if s in out}

    total = len(missing)
    logger.info("Fetching market caps for %d symbols (%d already cached)", total, len(out))
    for i, sym in enumerate(missing, start=1):
        try:
            info = yf.Ticker(f"{sym}.NS").fast_info
            mcap = getattr(info, "market_cap", None)
            if mcap is None and hasattr(info, "get"):
                mcap = info.get("marketCap") or info.get("market_cap")
            if mcap is not None and float(mcap) > 0:
                out[sym] = float(mcap) / 1e7
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcap failed for %s: %s", sym, exc)
        if sleep_sec:
            time.sleep(sleep_sec)
        if i % 50 == 0 or i == total:
            logger.info("Market cap progress %d / %d (cache_ok=%d)", i, total, len(out))

    if cache_path and out:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [{"symbol": k, "market_cap_cr": v} for k, v in sorted(out.items())]
        ).to_csv(cache_path, index=False)
    return {s: out[s] for s in wanted if s in out}
