from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from nse_alert.universe import Instrument

logger = logging.getLogger(__name__)

TickHandler = Callable[[str, float], None]


class PriceFeed(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class MockFeed:
    """Synthetic LTP stream that eventually crosses the threshold for DEMO13."""

    def __init__(
        self,
        instruments: list[Instrument],
        on_tick: TickHandler,
        *,
        interval_sec: float = 0.4,
        auto_stop_after_alert: bool = False,
        max_ticks: int | None = 80,
    ) -> None:
        self.instruments = instruments
        self.on_tick = on_tick
        self.interval_sec = interval_sec
        self.auto_stop_after_alert = auto_stop_after_alert
        self.max_ticks = max_ticks
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prices = {i.symbol: i.prev_close for i in instruments}
        self._token_to_symbol = {i.instrument_token: i.symbol for i in instruments}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mock-feed", daemon=True)
        self._thread.start()
        logger.info("Mock feed started (%d symbols)", len(self.instruments))

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Mock feed stopped")

    def _run(self) -> None:
        ticks = 0
        demo = next((i for i in self.instruments if i.symbol == "DEMO13"), None)
        while not self._stop.is_set():
            for inst in self.instruments:
                if self._stop.is_set():
                    break
                px = self._prices[inst.symbol]
                if inst.symbol == "DEMO13":
                    # Ramp toward +13% then beyond.
                    px = px * (1.0 + 0.02)
                else:
                    px = px * (1.0 + random.uniform(-0.002, 0.002))
                self._prices[inst.symbol] = px
                self.on_tick(inst.symbol, px)
                ticks += 1
                if self.max_ticks is not None and ticks >= self.max_ticks:
                    self._stop.set()
                    break
            if demo and self._prices[demo.symbol] / demo.prev_close - 1 >= 0.14:
                # Keep running briefly so the engine can fire, then stop if requested.
                if self.auto_stop_after_alert:
                    time.sleep(self.interval_sec)
                    self._stop.set()
                    break
            time.sleep(self.interval_sec)


class KiteFeed:
    """Live LTP via KiteTicker WebSocket (requires paid Kite Connect)."""

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        instruments: list[Instrument],
        on_tick: TickHandler,
    ) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.instruments = instruments
        self.on_tick = on_tick
        self._token_to_symbol = {i.instrument_token: i.symbol for i in instruments}
        self._ticker: Any = None

    def start(self) -> None:
        from kiteconnect import KiteTicker

        tokens = [i.instrument_token for i in self.instruments if i.instrument_token]
        if not tokens:
            raise RuntimeError("No instrument tokens to subscribe")

        ticker = KiteTicker(self.api_key, self.access_token)
        self._ticker = ticker

        def on_ticks(_ws: Any, ticks: list[dict[str, Any]]) -> None:
            for tick in ticks:
                token = tick.get("instrument_token")
                ltp = tick.get("last_price")
                if token is None or ltp is None:
                    continue
                symbol = self._token_to_symbol.get(int(token))
                if symbol:
                    self.on_tick(symbol, float(ltp))

        def on_connect(ws: Any, _response: Any) -> None:
            logger.info("KiteTicker connected; subscribing %d tokens (LTP)", len(tokens))
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_LTP, tokens)

        def on_close(_ws: Any, code: Any, reason: Any) -> None:
            logger.warning("KiteTicker closed: %s %s", code, reason)

        def on_error(_ws: Any, code: Any, reason: Any) -> None:
            logger.error("KiteTicker error: %s %s", code, reason)

        ticker.on_ticks = on_ticks
        ticker.on_connect = on_connect
        ticker.on_close = on_close
        ticker.on_error = on_error
        # threaded=True keeps the main thread free for KeyboardInterrupt.
        ticker.connect(threaded=True)
        logger.info("Kite feed started")

    def stop(self) -> None:
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown
                logger.debug("KiteTicker close: %s", exc)
            self._ticker = None
        logger.info("Kite feed stopped")
