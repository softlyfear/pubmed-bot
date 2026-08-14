"""Точки входа, factory, DeepL SDK-ветки. Без живых NCBI/DeepL/Telegram."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.session import (
    create_engine_from_path,
    session_factory,
    session_scope,
)
from pubmed_bot.adapters.ncbi.query import build_term
from pubmed_bot.adapters.translator.deepl import DeeplTranslator
from pubmed_bot.bot.factory import create_dispatcher
from pubmed_bot.bot.keyboards import article_callback_data
from pubmed_bot.config import Settings, get_settings
from pubmed_bot.container import _data_dir, _drop_root
from pubmed_bot.container import main as container_main
from pubmed_bot.domain.exceptions import TranslationUnavailable
from pubmed_bot.main import run_polling


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="t",
        ncbi_api_key="k",
        ncbi_email="dev@example.com",
        ncbi_tool="pubmed-bot",
        deepl_auth_key="d",
        openai_api_key="sk-test",
        sqlite_path=tmp_path / "pubmed.db",
        _env_file=None,
    )


def _set_env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "d")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()


@pytest.fixture
def sqlite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "pubmed.db"
    _set_env(monkeypatch, db_path)
    command.upgrade(Config("alembic.ini"), "head")
    return db_path


def test_get_settings_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path / "x.db")
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()


def test_create_dispatcher_registers_services(tmp_path: Path) -> None:
    dispatcher = create_dispatcher(
        _settings(tmp_path),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    assert dispatcher["search_service"] is not None
    assert dispatcher["article_service"] is not None
    assert dispatcher["subscriptions_service"] is not None


def test_data_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    assert _data_dir() == Path("/data")


def test_drop_root_skips_when_not_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    _drop_root(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_drop_root_as_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "child.txt"
    nested.write_text("x", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    chown = MagicMock()
    monkeypatch.setattr(os, "chown", chown, raising=False)
    monkeypatch.setattr(os, "setgroups", MagicMock(), raising=False)
    monkeypatch.setattr(os, "setgid", MagicMock(), raising=False)
    monkeypatch.setattr(os, "setuid", MagicMock(), raising=False)
    _drop_root(tmp_path)
    assert chown.called
    assert os.environ["HOME"] == "/app"


def test_container_main_runs_upgrade_and_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "data" / "pubmed.db"))
    monkeypatch.setattr("pubmed_bot.container.command.upgrade", MagicMock())
    monkeypatch.setattr("pubmed_bot.container.run_polling", MagicMock())
    container_main()
    assert (tmp_path / "data").is_dir()


@pytest.mark.asyncio
async def test_run_polling_starts_and_shuts_down(
    sqlite_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = SimpleNamespace(start_polling=AsyncMock())
    monkeypatch.setattr("pubmed_bot.main.create_dispatcher", lambda *a, **k: dispatcher)
    monkeypatch.setattr("pubmed_bot.main.DeeplTranslator", MagicMock())
    monkeypatch.setattr("pubmed_bot.main.OpenAIQueryRewriter", MagicMock())
    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr("pubmed_bot.main.Bot", lambda *a, **k: bot)
    pubmed = MagicMock()
    pubmed.aclose = AsyncMock()
    monkeypatch.setattr("pubmed_bot.main.NcbiEutilsClient", lambda *a, **k: pubmed)
    scheduler = MagicMock()
    monkeypatch.setattr("pubmed_bot.main.build_scheduler", lambda *a, **k: scheduler)
    monkeypatch.setattr("pubmed_bot.main.add_purge_job", lambda *a, **k: None)
    monkeypatch.setattr("pubmed_bot.main.SubscriptionWorker", MagicMock())
    await run_polling()
    dispatcher.start_polling.assert_awaited()
    scheduler.start.assert_called_once()
    scheduler.shutdown.assert_called_once()
    pubmed.aclose.assert_awaited()
    bot.session.close.assert_awaited()
    get_settings.cache_clear()


def test_main_uses_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = MagicMock()
    monkeypatch.setattr("pubmed_bot.main.asyncio.run", ran)
    from pubmed_bot.main import main

    main()
    ran.assert_called_once()


def test_empty_query_rejected() -> None:
    with pytest.raises(ValueError, match="текст запроса"):
        build_term("   ")


def test_long_pmid_callback_rejected() -> None:
    with pytest.raises(ValueError, match="64"):
        article_callback_data("1" * 80)


@pytest.mark.asyncio
async def test_session_scope_rollbacks(sqlite_file: Path) -> None:
    engine: AsyncEngine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with session_scope(factory) as session:
                raise RuntimeError("boom")
        assert session is not None
    finally:
        await engine.dispose()
        get_settings.cache_clear()


class _SdkText:
    def __init__(self, text: object) -> None:
        self.text = text


class _SdkClient:
    def __init__(self) -> None:
        self.mode = "ok"

    def translate_text(self, text: str, *, source_lang: str, target_lang: str) -> object:
        if self.mode == "list":
            return [_SdkText("ok")]
        if self.mode == "empty_list":
            return []
        if self.mode == "bad_type":
            return _SdkText(123)
        return _SdkText("ok")

    def get_usage(self) -> SimpleNamespace:
        if self.mode == "invalid_char":
            return SimpleNamespace(
                any_limit_reached=False,
                character=SimpleNamespace(valid=False, limit=1, count=0),
            )
        if self.mode == "none_limit":
            return SimpleNamespace(
                any_limit_reached=False,
                character=SimpleNamespace(valid=True, limit=None, count=1),
            )
        return SimpleNamespace(
            any_limit_reached=False,
            character=SimpleNamespace(valid=True, limit=10, count=1),
        )


@pytest.mark.asyncio
async def test_deepl_sdk_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    translator = DeeplTranslator(_settings(tmp_path))
    sdk = _SdkClient()
    monkeypatch.setattr(translator, "_sdk_client", lambda: sdk)
    assert await translator.translate("Hello") == "ok"
    sdk.mode = "list"
    assert await translator.translate("Hello") == "ok"
    sdk.mode = "empty_list"
    with pytest.raises(TranslationUnavailable):
        await translator.translate("Hello")
    sdk.mode = "bad_type"
    with pytest.raises(TranslationUnavailable):
        await translator.translate("Hello")
    sdk.mode = "ok"
    assert await translator.remaining_characters() == 9
    sdk.mode = "invalid_char"
    assert await translator.remaining_characters() is None
    sdk.mode = "none_limit"
    assert await translator.remaining_characters() is None


@pytest.mark.asyncio
async def test_deepl_non_str_invoke(tmp_path: Path) -> None:
    async def bad_thread(fn: object, *args: object, **kwargs: object) -> int:
        return 1

    fake = MagicMock()
    translator = DeeplTranslator(_settings(tmp_path), client=fake, to_thread=bad_thread)
    with pytest.raises(TranslationUnavailable):
        await translator.translate("Hello")


@pytest.mark.asyncio
async def test_deepl_remaining_non_int(tmp_path: Path) -> None:
    async def bad_thread(fn: object, *args: object, **kwargs: object) -> str:
        return "nope"

    fake = MagicMock()
    translator = DeeplTranslator(_settings(tmp_path), client=fake, to_thread=bad_thread)
    with pytest.raises(TranslationUnavailable):
        await translator.remaining_characters()


def test_sdk_client_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class FakeClient:
        def __init__(self, key: str) -> None:
            created.append(key)

    monkeypatch.setattr("pubmed_bot.adapters.translator.deepl.deepl.DeepLClient", FakeClient)
    translator = DeeplTranslator(_settings(tmp_path))
    first = translator._sdk_client()
    second = translator._sdk_client()
    assert first is second
    assert created == ["d"]
