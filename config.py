"""Vulture bot configuration via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BOT_TOKEN: str = ""
    DATABASE_URL: str = "sqlite+aiosqlite:///./vulture.db"
    PROXY_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    DEVELOPER_ID: int = 0

    @field_validator("DEVELOPER_ID", mode="before")
    @classmethod
    def _parse_developer_id(cls, value: object) -> int:
        if not value:
            return 0
        try:
            return int(value)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return 0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached *Settings* instance (lazy singleton)."""
    return Settings()
