from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nse_alert.engine import parse_thresholds
from nse_alert.orders import TradeMode


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

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    custom_universe_file: str = Field(default="", alias="CUSTOM_UNIVERSE_FILE")
    state_dir: Path = Field(default=Path(".nse_alert"), alias="STATE_DIR")

    # Trading — test defaults: BUY only on +13%, SL 2% below entry
    trade_mode: str = Field(default="off", alias="TRADE_MODE")
    trade_qty: int = Field(default=1, alias="TRADE_QTY")
    trade_product: str = Field(default="CNC", alias="TRADE_PRODUCT")
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
    trade_confirm_ttl_minutes: int = Field(default=30, alias="TRADE_CONFIRM_TTL_MINUTES")

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
    def use_kite(self) -> bool:
        return self.feed_mode.strip().lower() == "kite"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)
