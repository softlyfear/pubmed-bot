"""Тесты настроек: чтение из окружения, без сети и без Telegram."""

from pathlib import Path

from pubmed_bot.config import Settings, get_settings
from pubmed_bot.logging import setup_logging


def test_settings_from_env(monkeypatch, tmp_path: Path) -> None:
    """Settings читает обязательные поля, включая SQLITE_PATH, из environ."""
    db_path = tmp_path / "pubmed.db"
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("NCBI_TOOL", "pubmed-bot")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "test-deepl-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()

    settings = Settings(_env_file=None)

    assert settings.bot_token == "test-bot-token"
    assert settings.ncbi_api_key == "test-ncbi-key"
    assert settings.ncbi_email == "dev@example.com"
    assert settings.ncbi_tool == "pubmed-bot"
    assert settings.deepl_auth_key == "test-deepl-key"
    assert settings.openai_api_key == "sk-test"
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.sqlite_path == db_path
    assert settings.log_level == "INFO"
    assert settings.ncbi_max_rps == 8
    assert settings.user_search_per_min == 10
    assert settings.user_open_per_min == 20
    assert settings.subscription_hour_utc == 6
    assert settings.deepl_min_chars_remaining == 20_000


def test_settings_optional_overrides(monkeypatch, tmp_path: Path) -> None:
    """Опциональные поля читаются из окружения."""
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("NCBI_EMAIL", "a@b.c")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "d")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("NCBI_MAX_RPS", "7")
    monkeypatch.setenv("USER_SEARCH_PER_MIN", "3")
    monkeypatch.setenv("USER_OPEN_PER_MIN", "4")
    monkeypatch.setenv("SUBSCRIPTION_HOUR_UTC", "9")
    monkeypatch.setenv("DEEPL_MIN_CHARS_REMAINING", "1000")

    settings = Settings(_env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.ncbi_max_rps == 7
    assert settings.user_search_per_min == 3
    assert settings.user_open_per_min == 4
    assert settings.subscription_hour_utc == 9
    assert settings.deepl_min_chars_remaining == 1000
    assert settings.openai_model == "gpt-4o"


def test_blank_openai_model_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("NCBI_EMAIL", "a@b.c")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "d")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "  ")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "db.sqlite"))
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-4o-mini"


def test_env_example_lists_openai_names() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in text
    assert "OPENAI_MODEL=" in text


def test_settings_has_no_postgres_or_whitelist_fields() -> None:
    """Публичный бот и SQLite: лишних полей доступа и Postgres нет."""
    names = set(Settings.model_fields)
    forbidden = {
        "telegram_allowed_ids",
        "database_url",
        "postgres_user",
        "postgres_password",
        "postgres_db",
    }
    assert forbidden.isdisjoint(names)


def test_setup_logging_accepts_level() -> None:
    """setup_logging не падает на валидном уровне."""
    setup_logging("INFO")
