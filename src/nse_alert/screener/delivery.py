from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

_BHAV_URL = (
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)


@dataclass(frozen=True, slots=True)
class DeliveryInfo:
    traded_qty: float
    delivery_qty: float
    delivery_pct: float  # 0–100


def _parse_pct(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in {"-", "NA", "NaN", "nan"}:
        return None
    text = text.replace("%", "")
    try:
        val = float(text)
    except ValueError:
        return None
    if not (0.0 <= val <= 100.0):
        # Some feeds use fraction 0–1
        if 0.0 <= val <= 1.0:
            return val * 100.0
        return None
    return val


def _parse_qty(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "NA", "NaN", "nan"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_bhav_df(df: pd.DataFrame) -> pd.DataFrame:
    cols = {c.strip().upper(): c for c in df.columns}
    # Map common headers
    rename: dict[str, str] = {}
    for want, aliases in {
        "symbol": ("SYMBOL",),
        "series": ("SERIES",),
        "traded_qty": ("TTL_TRD_QNTY", "TOTAL_TRADES", "TOTTRDQTY"),
        "delivery_qty": ("DELIV_QTY", "DELIVERY_QTY", "DELIVQTY"),
        "delivery_pct": ("DELIV_PER", "DELIVERY_PER", "DELIVPER"),
        # Optional — used by the intraday universe's prior-session liquidity
        # screen. Not in `need` below, so an older bhavcopy layout still parses.
        "turnover_lacs": ("TURNOVER_LACS", "TURNOVER", "TOTTRDVAL"),
    }.items():
        for alias in aliases:
            if alias in cols:
                rename[cols[alias]] = want
                break
    out = df.rename(columns=rename)
    need = {"symbol", "traded_qty", "delivery_qty", "delivery_pct"}
    if not need.issubset(set(out.columns)):
        raise ValueError(f"Bhavcopy missing columns; have {list(out.columns)}")
    out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()
    if "series" in out.columns:
        out["series"] = out["series"].astype(str).str.strip().str.upper()
        out = out[out["series"].isin({"EQ", "BE", "BZ"})].copy()
    return out


def fetch_bhavcopy_day(session_day: date, *, client: httpx.Client | None = None) -> pd.DataFrame:
    """Download NSE security-wise full bhavcopy for one calendar day."""
    url = _BHAV_URL.format(ddmmyyyy=session_day.strftime("%d%m%Y"))
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
    own = client is None
    client = client or httpx.Client(headers=headers, timeout=60.0, follow_redirects=True)
    try:
        resp = client.get(url, headers=headers)
        if resp.status_code == 404:
            return pd.DataFrame()
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        return _normalize_bhav_df(df)
    finally:
        if own:
            client.close()


class DeliveryBook:
    """Cache of (date → symbol → DeliveryInfo) from NSE full bhavcopy."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._by_date: dict[date, dict[str, DeliveryInfo]] = {}

    def _cache_path(self, session_day: date) -> Path:
        return self.cache_dir / f"bhav_full_{session_day.isoformat()}.csv"

    def load_day(self, session_day: date, *, client: httpx.Client | None = None) -> None:
        if session_day in self._by_date:
            return
        path = self._cache_path(session_day)
        df: pd.DataFrame | None = None
        if path.exists():
            try:
                df = _normalize_bhav_df(pd.read_csv(path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bad bhav cache %s: %s", path, exc)
        if df is None or df.empty:
            try:
                df = fetch_bhavcopy_day(session_day, client=client)
                if not df.empty:
                    df.to_csv(path, index=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bhavcopy fetch failed for %s: %s", session_day, exc)
                self._by_date[session_day] = {}
                return
        mapping: dict[str, DeliveryInfo] = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                pct = _parse_pct(row.get("delivery_pct"))
                dqty = _parse_qty(row.get("delivery_qty"))
                tqty = _parse_qty(row.get("traded_qty"))
                if pct is None and dqty is not None and tqty and tqty > 0:
                    pct = 100.0 * dqty / tqty
                if pct is None or tqty is None:
                    continue
                mapping[str(row["symbol"]).upper()] = DeliveryInfo(
                    traded_qty=float(tqty),
                    delivery_qty=float(dqty) if dqty is not None else float("nan"),
                    delivery_pct=float(pct),
                )
        self._by_date[session_day] = mapping
        logger.info(
            "Delivery book %s: %d EQ symbols",
            session_day.isoformat(),
            len(mapping),
        )

    def ensure_dates(self, days: list[date]) -> None:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
        with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
            for d in sorted(set(days)):
                self.load_day(d, client=client)

    def get(self, symbol: str, session_day: date) -> DeliveryInfo | None:
        if session_day not in self._by_date:
            self.load_day(session_day)
        return self._by_date.get(session_day, {}).get(symbol.upper())

    def prefetch_lookback(self, *, as_of: date, lookback_days: int) -> None:
        """Load ~lookback calendar window (extra days cover weekends/holidays)."""
        days = [
            as_of - timedelta(days=i)
            for i in range(0, lookback_days + 14)
        ]
        self.ensure_dates(days)


def parse_iso_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:11] if fmt != "%Y-%m-%d" else text[:10], fmt).date()
        except ValueError:
            continue
    # Last resort: ISO-ish
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None
