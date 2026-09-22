"""Настройки приложения из окружения и файла .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация pubmed-bot. Секреты только из окружения, не из кода."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: str
    ncbi_api_key: str
    ncbi_email: str
    ncbi_tool: str = "pubmed-bot"
    deepl_auth_key: str
    gemini_api_key: str
    gemini_model: str = "gemini-3.5-flash-lite"
    sqlite_path: Path

    log_level: str = "INFO"
    ncbi_max_rps: int = 8
    user_search_per_min: int = 10
    user_open_per_min: int = 20
    subscription_hour_utc: int = Field(default=6, ge=0, le=23)
    deepl_min_chars_remaining: int = 20_000

    @field_validator(
        "bot_token",
        "ncbi_api_key",
        "ncbi_email",
        "ncbi_tool",
        "deepl_auth_key",
        "gemini_api_key",
        mode="after",
    )
    @classmethod
    def _reject_blank_secrets(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("пустое значение недопустимо")
        return value

    @field_validator("gemini_model", mode="after")
    @classmethod
    def _default_blank_gemini_model(cls, value: str) -> str:
        stripped = value.strip()
        return stripped if stripped else "gemini-3.5-flash-lite"


@lru_cache
def get_settings() -> Settings:
    """Вернуть кэшированный экземпляр настроек."""
    return Settings()  # type: ignore[call-arg]
