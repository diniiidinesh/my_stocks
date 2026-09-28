"""NSE cash-market session times, per symbol.

Since 3 Aug 2026 (SEBI Closing Auction Session, CAS), the close differs by
stock:

* **F&O stocks** (CAS stocks): continuous trading 09:15–15:15, then the
  closing auction 15:15–15:35 (order entry 15:20–15:30, random close
  15:28–15:30, matching 15:30–15:35, ±3% band around a reference price).
  The closing price comes from the auction, not the 15:00–15:30 VWAP.
* **Non-F&O stocks**: continuous trading 09:15–15:30 as before (VWAP close).

Zerodha's MIS auto square-off follows suit: **15:12** for CAS stocks,
**15:25** for the rest. After that no fresh MIS orders are accepted, so
the watcher stops offering / placing intraday entries at those times.
See docs/ALERTS.md → "Market close (CAS)".
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
CONTINUOUS_CLOSE_CAS = time(15, 15)
CONTINUOUS_CLOSE_NON_CAS = time(15, 30)


def hhmm_to_time(value: int) -> time:
    """``1512`` → ``time(15, 12)``."""
    hours, minutes = divmod(int(value), 100)
    return time(hours, minutes)


def to_ist(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(IST)


class SessionClock:
    """Per-symbol continuous-session close and MIS entry cutoff.

    ``fo_symbols`` are the CAS stocks. When the F&O list could not be loaded
    (``fo_known=False``) every symbol gets the earlier CAS times: a missed
    late entry is cheaper than an order the broker will reject, or a signal
    measured on auction prints.
    """

    def __init__(
        self,
        *,
        fo_symbols: set[str] | None = None,
        fo_known: bool = True,
        mis_cutoff_cas: time = time(15, 12),
        mis_cutoff_non_cas: time = time(15, 25),
    ) -> None:
        self.fo_symbols = {s.upper() for s in (fo_symbols or set())}
        self.fo_known = fo_known
        self.mis_cutoff_cas = mis_cutoff_cas
        self.mis_cutoff_non_cas = mis_cutoff_non_cas

    def is_cas(self, symbol: str) -> bool:
        return not self.fo_known or symbol.upper() in self.fo_symbols

    def continuous_close(self, symbol: str) -> time:
        return CONTINUOUS_CLOSE_CAS if self.is_cas(symbol) else CONTINUOUS_CLOSE_NON_CAS

    def mis_cutoff(self, symbol: str) -> time:
        return self.mis_cutoff_cas if self.is_cas(symbol) else self.mis_cutoff_non_cas

    def in_continuous(self, symbol: str, now: datetime) -> bool:
        ist = to_ist(now)
        if ist.weekday() >= 5:
            return False
        return SESSION_OPEN <= ist.time() < self.continuous_close(symbol)

    def entries_open(self, symbol: str, now: datetime, *, product: str = "MIS") -> bool:
        """False once *symbol*'s entry cutoff has passed for the day.

        Only the close side is gated; pre-open behaviour is unchanged.
        """
        ist = to_ist(now)
        if ist.weekday() >= 5:
            return False
        end = self.mis_cutoff(symbol) if product.upper() == "MIS" else self.continuous_close(symbol)
        return ist.time() < end
