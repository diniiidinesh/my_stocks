from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{ddmmyyyy}.csv"
)


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    instrument_token: int
    name: str
    last_price: float
    prev_close: float
    turnover_cr: float


def _session() -> httpx.Client:
    client = httpx.Client(headers=NSE_HEADERS, timeout=30.0, follow_redirects=True)
    # Warm cookies; NSE often requires a home hit first.
    try:
        client.get("https://www.nseindia.com")
    except httpx.HTTPError as exc:
        logger.debug("NSE cookie warmup failed: %s", exc)
    return client


def fetch_bhavcopy(as_of: date | None = None) -> pd.DataFrame:
    """Download NSE full bhavcopy for the most recent available session."""
    start = as_of or date.today()
    with _session() as client:
        for offset in range(0, 10):
            day = start - timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            url = BHAVCOPY_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("bhavcopy request failed for %s: %s", day, exc)
                continue
            if resp.status_code != 200:
                continue
            text = resp.text
            if "SYMBOL" not in text[:200].upper():
                continue
            df = pd.read_csv(io.StringIO(text))
            df.columns = [c.strip().upper() for c in df.columns]
            logger.info("Loaded NSE bhavcopy for %s (%d rows)", day.isoformat(), len(df))
            return df
    raise RuntimeError(
        "Could not download NSE bhavcopy for the last ~10 calendar days. "
        "Check network access to nsearchives.nseindia.com or use CUSTOM_UNIVERSE_FILE."
    )


def _normalize_bhav(df: pd.DataFrame) -> pd.DataFrame:
    symbol_col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
    series_col = next((c for c in df.columns if "SERIES" in c), None)
    close_col = next(
        (c for c in ("CLOSE_PRICE", "CLOSE", "CLS_PRIC") if c in df.columns), None
    )
    turnover_col = next(
        (
            c
            for c in ("TTL_TRD_VALN", "TOTTRDVAL", "TURNOVER", "TTL_TRD_VAL")
            if c in df.columns
        ),
        None,
    )
    if close_col is None or turnover_col is None:
        raise RuntimeError(
            f"Unexpected bhavcopy columns: {list(df.columns)}. "
            "Need close and turnover fields."
        )

    out = pd.DataFrame(
        {
            "symbol": df[symbol_col].astype(str).str.strip(),
            "series": (
                df[series_col].astype(str).str.strip().str.upper()
                if series_col
                else "EQ"
            ),
            "close": pd.to_numeric(df[close_col], errors="coerce"),
            "turnover": pd.to_numeric(df[turnover_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["symbol", "close", "turnover"])
    # Prefer EQ series for intraday cash.
    out = out[out["series"].isin(["EQ", "BE", "SM"])]
    out = out.sort_values(["symbol", "series"]).drop_duplicates("symbol", keep="first")
    out = out[out["series"] == "EQ"]
    return out


def load_custom_universe(path: str) -> list[str]:
    symbols: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            symbols.append(line.split()[0].upper())
    if not symbols:
        raise ValueError(f"No symbols found in {path}")
    return symbols


def screen_liquid_symbols(
    min_turnover_cr: float,
    min_price: float,
    bhav: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return liquid EQ symbols from prior-session bhavcopy."""
    raw = bhav if bhav is not None else fetch_bhavcopy()
    df = _normalize_bhav(raw)
    min_turnover = min_turnover_cr * 1e7  # crore → rupees
    screened = df[(df["turnover"] >= min_turnover) & (df["close"] >= min_price)].copy()
    screened["turnover_cr"] = screened["turnover"] / 1e7
    logger.info(
        "Liquidity screen: %d symbols (turnover>=%.1f Cr, price>=%.1f)",
        len(screened),
        min_turnover_cr,
        min_price,
    )
    return screened.reset_index(drop=True)


def _kite_client(api_key: str, access_token: str) -> Any:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def load_nse_eq_instruments(kite: Any) -> pd.DataFrame:
    instruments = kite.instruments("NSE")
    df = pd.DataFrame(instruments)
    if df.empty:
        raise RuntimeError("Kite returned no NSE instruments")
    df = df[
        (df["instrument_type"] == "EQ")
        & (df["segment"] == "NSE")
        & (df["exchange"] == "NSE")
    ].copy()
    df["tradingsymbol"] = df["tradingsymbol"].astype(str).str.strip()
    return df


def build_universe(
    *,
    min_turnover_cr: float,
    min_price: float,
    kite_api_key: str = "",
    kite_access_token: str = "",
    custom_universe_file: str = "",
    mock: bool = False,
) -> list[Instrument]:
    """Build the intraday-liquid watchlist.

    In mock mode, returns a small synthetic set without network/Kite.
    With Kite credentials, maps screened symbols to instrument tokens and
    previous closes from quote OHLC.
    Without Kite (but not mock), returns screened symbols with token=0 and
    prev_close from bhavcopy — useful for listing the universe only.
    """
    if mock and not custom_universe_file:
        demo = [
            ("RELIANCE", 1000.0, 28.5),
            ("TCS", 3500.0, 40.0),
            ("INFY", 1500.0, 35.0),
            ("SBIN", 750.0, 30.0),
            ("DEMO13", 100.0, 50.0),
        ]
        return [
            Instrument(
                symbol=sym,
                instrument_token=i + 1,
                name=sym,
                last_price=px,
                prev_close=px,
                turnover_cr=tovr,
            )
            for i, (sym, px, tovr) in enumerate(demo)
        ]

    if custom_universe_file:
        symbols = load_custom_universe(custom_universe_file)
        screened = pd.DataFrame(
            {
                "symbol": symbols,
                "close": [0.0] * len(symbols),
                "turnover_cr": [0.0] * len(symbols),
            }
        )
    else:
        screened = screen_liquid_symbols(min_turnover_cr, min_price)

    if not kite_api_key or not kite_access_token:
        return [
            Instrument(
                symbol=row.symbol,
                instrument_token=0,
                name=row.symbol,
                last_price=float(row.close) if "close" in screened.columns else 0.0,
                prev_close=float(row.close) if "close" in screened.columns else 0.0,
                turnover_cr=float(getattr(row, "turnover_cr", 0.0) or 0.0),
            )
            for row in screened.itertuples(index=False)
        ]

    kite = _kite_client(kite_api_key, kite_access_token)
    nse = load_nse_eq_instruments(kite)
    merged = screened.merge(
        nse,
        left_on="symbol",
        right_on="tradingsymbol",
        how="inner",
    )
    if merged.empty:
        raise RuntimeError("No overlap between liquidity screen and Kite NSE EQ list")

    # Quote in batches of 250 (Kite limit).
    instruments: list[Instrument] = []
    tokens = merged["instrument_token"].astype(int).tolist()
    symbols = merged["tradingsymbol"].astype(str).tolist()
    names = merged["name"].fillna("").astype(str).tolist()
    turnover_crs = (
        merged["turnover_cr"].astype(float).tolist()
        if "turnover_cr" in merged.columns
        else [0.0] * len(merged)
    )
    bhav_closes = (
        merged["close"].astype(float).tolist()
        if "close" in merged.columns
        else [0.0] * len(merged)
    )

    quote_map: dict[str, dict[str, Any]] = {}
    batch_size = 250
    for i in range(0, len(symbols), batch_size):
        keys = [f"NSE:{s}" for s in symbols[i : i + batch_size]]
        quote_map.update(kite.quote(keys))

    for token, symbol, name, tovr, bhav_close in zip(
        tokens, symbols, names, turnover_crs, bhav_closes, strict=True
    ):
        q = quote_map.get(f"NSE:{symbol}", {})
        ohlc = q.get("ohlc") or {}
        prev_close = float(ohlc.get("close") or bhav_close or 0.0)
        last_price = float(q.get("last_price") or prev_close or 0.0)
        if prev_close <= 0:
            continue
        instruments.append(
            Instrument(
                symbol=symbol,
                instrument_token=int(token),
                name=name or symbol,
                last_price=last_price,
                prev_close=prev_close,
                turnover_cr=float(tovr or 0.0),
            )
        )

    logger.info("Universe ready: %d instruments with prev_close", len(instruments))
    return instruments
