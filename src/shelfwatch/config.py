from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHELFWATCH_", env_file=".env")

    bot_token: SecretStr
    database_url: str = "sqlite+aiosqlite:///data/shelfwatch.db"
    check_interval: int = 900
    per_host_interval: float = 5.0
    max_watches_per_chat: int = 25
    user_agent: str = (
        "Mozilla/5.0 (compatible; shelfwatch/0.1; +https://github.com/qweendev/shelfwatch)"
    )
