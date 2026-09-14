from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nse_alert.engine import parse_thresholds


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Comma-separated list, e.g. "4,7,11" or a single value "13"
    threshold_pct: str = Field(default="13", alias="THRESHOLD_PCT")
    min_turnover_cr: float = Field(default=25.0, alias="MIN_TURNOVER_CR")
    min_price: float = Field(default=20.0, alias="MIN_PRICE")
    feed_mode: str = Field(default="mock", alias="FEED_MODE")

    kite_api_key: str = Field(default="", alias="KITE_API_KEY")
    kite_access_token: str = Field(default="", alias="KITE_ACCESS_TOKEN")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    custom_universe_file: str = Field(default="", alias="CUSTOM_UNIVERSE_FILE")
    state_dir: Path = Field(default=Path(".nse_alert"), alias="STATE_DIR")

    @property
    def thresholds(self) -> list[float]:
        return parse_thresholds(self.threshold_pct)

    @property
    def use_kite(self) -> bool:
        return self.feed_mode.strip().lower() == "kite"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)
