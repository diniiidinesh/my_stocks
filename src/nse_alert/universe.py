from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    instrument_token: int
    name: str
    last_price: float
    prev_close: float
    turnover_cr: float


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


def _kite_client(api_key: str, access_token: str) -> Any:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def load_nse_eq_instruments(kite: Any) -> pd.DataFrame:
    """All NSE cash EQ instruments from Kite's instrument dump."""
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


def _quote_batches(kite: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    quote_map: dict[str, dict[str, Any]] = {}
    batch_size = 250
    for i in range(0, len(symbols), batch_size):
        keys = [f"NSE:{s}" for s in symbols[i : i + batch_size]]
        quote_map.update(kite.quote(keys))
        # Quote limit is ~1 req/sec; pause between full-universe batches.
        if i + batch_size < len(symbols):
            time.sleep(0.35)
    return quote_map


def _build_from_kite_quotes(
    kite: Any,
    *,
    min_turnover_cr: float,
    min_price: float,
    symbols_filter: list[str] | None = None,
) -> list[Instrument]:
    """Screen and build the watchlist entirely from Kite instruments + quotes.

    Liquidity proxy: session ``volume * last_price`` (₹), compared to
    ``min_turnover_cr``. Prev close comes from quote OHLC.
    """
    nse = load_nse_eq_instruments(kite)
    if symbols_filter is not None:
        wanted = {s.upper() for s in symbols_filter}
        nse = nse[nse["tradingsymbol"].isin(wanted)].copy()
        if nse.empty:
            raise RuntimeError("No overlap between custom universe and Kite NSE EQ list")

    symbols = nse["tradingsymbol"].tolist()
    logger.info("Quoting %d NSE EQ symbols via Kite for liquidity screen", len(symbols))
    quote_map = _quote_batches(kite, symbols)

    token_by_symbol = dict(
        zip(
            nse["tradingsymbol"].tolist(),
            nse["instrument_token"].astype(int).tolist(),
            strict=True,
        )
    )
    name_by_symbol = dict(
        zip(
            nse["tradingsymbol"].tolist(),
            nse["name"].fillna("").astype(str).tolist(),
            strict=True,
        )
    )

    min_turnover = min_turnover_cr * 1e7  # crore → rupees
    # Custom lists skip the liquidity floor (user chose the names explicitly).
    apply_liquidity = symbols_filter is None
    instruments: list[Instrument] = []

    for symbol in symbols:
        q = quote_map.get(f"NSE:{symbol}", {})
        ohlc = q.get("ohlc") or {}
        prev_close = float(ohlc.get("close") or 0.0)
        last_price = float(q.get("last_price") or prev_close or 0.0)
        volume = float(q.get("volume") or 0.0)
        turnover = volume * last_price if volume and last_price else 0.0
        turnover_cr = turnover / 1e7

        if prev_close <= 0:
            continue
        if prev_close < min_price:
            continue
        if apply_liquidity and turnover < min_turnover:
            continue

        instruments.append(
            Instrument(
                symbol=symbol,
                instrument_token=int(token_by_symbol[symbol]),
                name=name_by_symbol.get(symbol) or symbol,
                last_price=last_price,
                prev_close=prev_close,
                turnover_cr=float(turnover_cr),
            )
        )

    if not instruments:
        raise RuntimeError(
            "Kite universe is empty after filters. "
            "Lower MIN_TURNOVER_CR / MIN_PRICE, or set CUSTOM_UNIVERSE_FILE."
        )
    logger.info(
        "Universe ready from Kite: %d instruments (turnover>=%.1f Cr, price>=%.1f)",
        len(instruments),
        min_turnover_cr if apply_liquidity else 0.0,
        min_price,
    )
    return instruments


def build_universe(
    *,
    min_turnover_cr: float,
    min_price: float,
    kite_api_key: str = "",
    kite_access_token: str = "",
    custom_universe_file: str = "",
    mock: bool = False,
) -> list[Instrument]:
    """Build the intraday watchlist.

    * **mock** — small synthetic set (includes DEMO13); no network.
    * **Kite credentials** — instruments + quotes from Kite only (no NSE website).
    * **custom file + Kite** — restrict to listed symbols, still quote via Kite.
    * **custom file, no Kite** — symbols only (token=0); for listing, not live watch.
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

    custom_symbols = (
        load_custom_universe(custom_universe_file) if custom_universe_file else None
    )
    has_kite = bool(kite_api_key and kite_access_token)

    if has_kite:
        kite = _kite_client(kite_api_key, kite_access_token)
        return _build_from_kite_quotes(
            kite,
            min_turnover_cr=min_turnover_cr,
            min_price=min_price,
            symbols_filter=custom_symbols,
        )

    if custom_symbols is not None:
        return [
            Instrument(
                symbol=sym,
                instrument_token=0,
                name=sym,
                last_price=0.0,
                prev_close=0.0,
                turnover_cr=0.0,
            )
            for sym in custom_symbols
        ]

    raise RuntimeError(
        "Live universe needs Kite credentials (KITE_API_KEY + KITE_ACCESS_TOKEN) "
        "or CUSTOM_UNIVERSE_FILE. Set FEED_MODE=kite after login, or use mock mode."
    )
