from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nse_alert.engine import parse_thresholds
from nse_alert.orders import TradeMode
from nse_alert.session import SessionClock, hhmm_to_time


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Comma-separated list, e.g. "4,7,11" or a single value "13"
    threshold_pct: str = Field(default="4,7,11,13", alias="THRESHOLD_PCT")
    # Thresholds that apply only to NSE F&O underlyings (cash EQ with futures)
    fo_only_thresholds: str = Field(default="4", alias="FO_ONLY_THRESHOLDS")
    # Tag ASM names in alerts using Zerodha RMS sheet (not a Kite API)
    asm_enabled: bool = Field(default=True, alias="ASM_ENABLED")
    asm_sheet_url: str = Field(default="", alias="ASM_SHEET_URL")
    min_turnover_cr: float = Field(default=25.0, alias="MIN_TURNOVER_CR")
    min_price: float = Field(default=20.0, alias="MIN_PRICE")
    feed_mode: str = Field(default="mock", alias="FEED_MODE")

    kite_api_key: str = Field(default="", alias="KITE_API_KEY")
    kite_api_secret: str = Field(default="", alias="KITE_API_SECRET")
    kite_access_token: str = Field(default="", alias="KITE_ACCESS_TOKEN")
    # AWS dual-stack VMs otherwise place orders via IPv6 (not the Elastic IPv4)
    kite_force_ipv4: bool = Field(default=True, alias="KITE_FORCE_IPV4")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    custom_universe_file: str = Field(default="", alias="CUSTOM_UNIVERSE_FILE")
    state_dir: Path = Field(default=Path(".nse_alert"), alias="STATE_DIR")

    # Trading — test defaults: BUY only on +13%, SL 2% below entry
    trade_mode: str = Field(default="off", alias="TRADE_MODE")
    trade_qty: int = Field(default=1, alias="TRADE_QTY")
    # margin = size so MIS margin ≈ TRADE_MARGIN_INR; fixed = use TRADE_QTY
    trade_sizing: str = Field(default="margin", alias="TRADE_SIZING")
    trade_margin_inr: float = Field(default=10_000.0, alias="TRADE_MARGIN_INR")
    # Used only when margins API unavailable (dry_run / offline)
    trade_fallback_leverage: float = Field(default=5.0, alias="TRADE_FALLBACK_LEVERAGE")
    trade_product: str = Field(default="MIS", alias="TRADE_PRODUCT")
    trade_order_type: str = Field(default="MARKET", alias="TRADE_ORDER_TYPE")
    trade_market_protection: int = Field(default=2, alias="TRADE_MARKET_PROTECTION")
    trade_max_orders_per_day: int = Field(default=10, alias="TRADE_MAX_ORDERS_PER_DAY")
    # Only place when this alert level is crossed (test: 13)
    trade_on_thresholds: str = Field(default="13", alias="TRADE_ON_THRESHOLDS")
    # up = BUY on UP alerts only
    trade_sides: str = Field(default="up", alias="TRADE_SIDES")
    trade_stop_loss_pct: float = Field(default=2.0, alias="TRADE_STOP_LOSS_PCT")
    # SELL SL-Limit: limit = trigger − N × ₹0.05 ticks
    trade_stop_limit_ticks: int = Field(default=2, alias="TRADE_STOP_LIMIT_TICKS")
    # Wait for entry MARKET fill before placing SL (live auto/confirm)
    trade_stop_wait_sec: float = Field(default=20.0, alias="TRADE_STOP_WAIT_SEC")
    # When LTP rises this % above entry, move SL trigger to entry (cost-to-cost)
    trade_trail_breakeven: bool = Field(default=True, alias="TRADE_TRAIL_BREAKEVEN")
    trade_trail_breakeven_pct: float = Field(default=2.0, alias="TRADE_TRAIL_BREAKEVEN_PCT")
    trade_confirm_ttl_minutes: int = Field(default=30, alias="TRADE_CONFIRM_TTL_MINUTES")
    # Exit at market the moment LTP reaches the upper-circuit band from prev
    # close — most NSE mid/small caps circuit at +20%, well above the 11/13%
    # entry thresholds, and a resting SL below entry won't fill once the
    # stock locks upper with no sellers left.
    trade_exit_on_upper_circuit: bool = Field(default=True, alias="TRADE_EXIT_ON_UPPER_CIRCUIT")
    trade_upper_circuit_pct: float = Field(default=20.0, alias="TRADE_UPPER_CIRCUIT_PCT")
    # No fresh MIS entries (auto / confirm / signal offers) after these IST
    # times — Zerodha's MIS auto square-off since the Aug 2026 closing
    # auction: 15:12 for F&O (CAS) stocks, 15:25 for the rest.
    mis_cutoff_cas_hhmm: int = Field(default=1512, alias="MIS_CUTOFF_CAS_HHMM")
    mis_cutoff_non_cas_hhmm: int = Field(default=1525, alias="MIS_CUTOFF_NON_CAS_HHMM")

    # --- Intraday signals (volume spike / 52-week breakout) — docs/SIGNALS.md ---
    volume_spike_enabled: bool = Field(default=True, alias="VOLUME_SPIKE_ENABLED")
    # Candle minutes, comma-separated (1,3,5,10,15,30,60), e.g. "5" or "5,15"
    volume_spike_timeframes: str = Field(default="5", alias="VOLUME_SPIKE_TIMEFRAMES")
    volume_spike_ema_period: int = Field(default=21, alias="VOLUME_SPIKE_EMA_PERIOD")
    # Fire when closed-candle volume > this × EMA of prior candles' volume
    volume_spike_mult: float = Field(default=2.0, alias="VOLUME_SPIKE_MULT")
    # The 09:15 candle is nearly always > 2× (pre-open volume lands in it)
    volume_spike_skip_opening_candle: bool = Field(
        default=True, alias="VOLUME_SPIKE_SKIP_OPENING_CANDLE"
    )
    breakout_52w_enabled: bool = Field(default=True, alias="BREAKOUT_52W_ENABLED")
    # Attach /confirm BUY + SELL offers to signal alerts (needs TRADE_MODE≠off)
    signal_orders_enabled: bool = Field(default=True, alias="SIGNAL_ORDERS_ENABLED")
    signal_order_sides: str = Field(default="buy,sell", alias="SIGNAL_ORDER_SIDES")

    # --- EOD TA screener (separate from intraday alerts / orders) ---
    screen_min_market_cap_cr: float = Field(default=5000.0, alias="SCREEN_MIN_MARKET_CAP_CR")
    screen_min_turnover_cr: float = Field(default=10.0, alias="SCREEN_MIN_TURNOVER_CR")
    screen_min_price: float = Field(default=20.0, alias="SCREEN_MIN_PRICE")
    screen_lookback_days: int = Field(default=20, alias="SCREEN_LOOKBACK_DAYS")
    screen_volume_ema_period: int = Field(default=20, alias="SCREEN_VOLUME_EMA_PERIOD")
    screen_volume_mult: float = Field(default=1.5, alias="SCREEN_VOLUME_MULT")
    screen_supertrend_period: int = Field(default=10, alias="SCREEN_SUPERTREND_PERIOD")
    screen_supertrend_mult: float = Field(default=3.0, alias="SCREEN_SUPERTREND_MULT")
    screen_ema_fast: int = Field(default=20, alias="SCREEN_EMA_FAST")
    screen_ema_mid: int = Field(default=50, alias="SCREEN_EMA_MID")
    screen_ema_slow: int = Field(default=200, alias="SCREEN_EMA_SLOW")
    screen_adx_period: int = Field(default=14, alias="SCREEN_ADX_PERIOD")
    screen_adx_min: float = Field(default=25.0, alias="SCREEN_ADX_MIN")
    screen_rsi_period: int = Field(default=14, alias="SCREEN_RSI_PERIOD")
    screen_rsi_min: float = Field(default=40.0, alias="SCREEN_RSI_MIN")
    screen_rsi_max: float = Field(default=60.0, alias="SCREEN_RSI_MAX")
    screen_macd_fast: int = Field(default=12, alias="SCREEN_MACD_FAST")
    screen_macd_slow: int = Field(default=26, alias="SCREEN_MACD_SLOW")
    screen_macd_signal: int = Field(default=9, alias="SCREEN_MACD_SIGNAL")
    screen_near_52w_high_pct: float = Field(default=5.0, alias="SCREEN_NEAR_52W_HIGH_PCT")
    screen_require_volume: bool = Field(default=True, alias="SCREEN_REQUIRE_VOLUME")
    screen_require_adx: bool = Field(default=True, alias="SCREEN_REQUIRE_ADX")
    screen_require_rsi: bool = Field(default=True, alias="SCREEN_REQUIRE_RSI")
    screen_require_macd: bool = Field(default=True, alias="SCREEN_REQUIRE_MACD")
    screen_require_near_52w: bool = Field(default=True, alias="SCREEN_REQUIRE_NEAR_52W")
    # Delivery % on volume-spike days (NSE bhavcopy); pass if any spike day ≥ threshold
    screen_require_delivery: bool = Field(default=True, alias="SCREEN_REQUIRE_DELIVERY")
    screen_min_delivery_pct: float = Field(default=40.0, alias="SCREEN_MIN_DELIVERY_PCT")
    screen_history_days: int = Field(default=400, alias="SCREEN_HISTORY_DAYS")
    screen_after_hhmm: int = Field(default=1540, alias="SCREEN_AFTER_HHMM")
    screen_max_symbols: int = Field(default=0, alias="SCREEN_MAX_SYMBOLS")
    screen_prefer_kite_history: bool = Field(
        default=True, alias="SCREEN_PREFER_KITE_HISTORY"
    )
    screen_market_cap_file: str = Field(default="", alias="SCREEN_MARKET_CAP_FILE")
    # Optional symbol list for the screener only (does NOT reuse CUSTOM_UNIVERSE_FILE)
    screen_custom_universe_file: str = Field(
        default="", alias="SCREEN_CUSTOM_UNIVERSE_FILE"
    )

    @property
    def thresholds(self) -> list[float]:
        return parse_thresholds(self.threshold_pct)

    @property
    def fo_only_threshold_list(self) -> list[float]:
        text = self.fo_only_thresholds.strip()
        if not text:
            return []
        return parse_thresholds(text)

    @property
    def trade_threshold_list(self) -> list[float]:
        text = self.trade_on_thresholds.strip()
        if not text:
            return []
        return parse_thresholds(text)

    @property
    def resolved_trade_mode(self) -> TradeMode:
        mode = self.trade_mode.strip().lower()
        if mode not in {"off", "dry_run", "confirm", "auto"}:
            return "off"
        return mode  # type: ignore[return-value]

    @property
    def volume_spike_timeframe_list(self) -> list[int]:
        from nse_alert.signals import parse_timeframes

        return parse_timeframes(self.volume_spike_timeframes)

    @property
    def signal_order_side_list(self) -> list[str]:
        sides = [p.strip().upper() for p in self.signal_order_sides.split(",")]
        return [s for s in ("BUY", "SELL") if s in sides]

    def session_clock(
        self, fo_symbols: set[str] | None = None, *, fo_known: bool = True
    ) -> SessionClock:
        return SessionClock(
            fo_symbols=fo_symbols,
            fo_known=fo_known,
            mis_cutoff_cas=hhmm_to_time(self.mis_cutoff_cas_hhmm),
            mis_cutoff_non_cas=hhmm_to_time(self.mis_cutoff_non_cas_hhmm),
        )

    @property
    def use_kite(self) -> bool:
        return self.feed_mode.strip().lower() == "kite"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)
