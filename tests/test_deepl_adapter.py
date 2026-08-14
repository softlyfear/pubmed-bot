"""Адаптер DeepL: EN→RU, to_thread, маппинг ошибок. Без живого API."""

from pathlib import Path
from types import SimpleNamespace

import deepl
import pytest

from pubmed_bot.adapters.translator.deepl import DeeplTranslator
from pubmed_bot.config import Settings
from pubmed_bot.domain.exceptions import TranslationUnavailable


def _settings() -> Settings:
    return Settings(
        bot_token="t",
        ncbi_api_key="k",
        ncbi_email="dev@example.com",
        ncbi_tool="pubmed-bot",
        deepl_auth_key="deepl-key",
        openai_api_key="sk-test",
        sqlite_path=Path("x.db"),
        _env_file=None,
    )


class FakeDeepL:
    def __init__(self) -> None:
        self.translate_calls: list[tuple[str, str, str]] = []
        self.usage_calls = 0
        self.result_text = "Привет"
        self.error: Exception | None = None
        self.character_count = 100
        self.character_limit = 500_000
        self.character_valid = True
        self.any_limit_reached = False

    def translate_text(self, text: str, *, source_lang: str, target_lang: str) -> SimpleNamespace:
        self.translate_calls.append((text, source_lang, target_lang))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.result_text)

    def get_usage(self) -> SimpleNamespace:
        self.usage_calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            any_limit_reached=self.any_limit_reached,
            character=SimpleNamespace(
                valid=self.character_valid,
                count=self.character_count,
                limit=self.character_limit,
            ),
        )


@pytest.mark.asyncio
async def test_translate_uses_en_ru_and_to_thread() -> None:
    fake = FakeDeepL()
    translator = DeeplTranslator(_settings(), client=fake)
    text = await translator.translate("Hello")
    assert text == "Привет"
    assert fake.translate_calls == [("Hello", "EN", "RU")]


@pytest.mark.asyncio
async def test_empty_text_skips_api() -> None:
    fake = FakeDeepL()
    translator = DeeplTranslator(_settings(), client=fake)
    assert await translator.translate("") == ""
    assert fake.translate_calls == []


@pytest.mark.asyncio
async def test_quota_exceeded_becomes_unavailable() -> None:
    fake = FakeDeepL()
    fake.error = deepl.QuotaExceededException("quota", http_status_code=456)
    translator = DeeplTranslator(_settings(), client=fake)
    with pytest.raises(TranslationUnavailable, match="перевод недоступен"):
        await translator.translate("Hello")


@pytest.mark.asyncio
async def test_remaining_characters_from_usage() -> None:
    fake = FakeDeepL()
    fake.character_count = 480_000
    fake.character_limit = 500_000
    translator = DeeplTranslator(_settings(), client=fake)
    assert await translator.remaining_characters() == 20_000
    assert fake.usage_calls == 1


@pytest.mark.asyncio
async def test_remaining_zero_when_limit_reached() -> None:
    fake = FakeDeepL()
    fake.any_limit_reached = True
    translator = DeeplTranslator(_settings(), client=fake)
    assert await translator.remaining_characters() == 0


@pytest.mark.asyncio
async def test_sync_client_runs_in_to_thread() -> None:
    fake = FakeDeepL()
    seen: list[str] = []

    async def spy(func, *args, **kwargs):
        seen.append("thread")
        return func(*args, **kwargs)

    translator = DeeplTranslator(_settings(), client=fake, to_thread=spy)
    await translator.translate("Hello")
    await translator.remaining_characters()
    assert seen == ["thread", "thread"]


@pytest.mark.asyncio
async def test_translate_ru_en() -> None:
    fake = FakeDeepL()
    fake.result_text = "glute"
    translator = DeeplTranslator(_settings(), client=fake)
    text = await translator.translate("Ягодицы", source_lang="RU", target_lang="EN")
    assert text == "glute"
    assert fake.translate_calls == [("Ягодицы", "RU", "EN-US")]
    assert all(target != "EN" for _, _, target in fake.translate_calls)


@pytest.mark.asyncio
async def test_query_target_en_is_not_sent_to_deepl() -> None:
    fake = FakeDeepL()
    fake.result_text = "glute growth"
    translator = DeeplTranslator(_settings(), client=fake)
    await translator.translate("рост ягодиц", source_lang="RU", target_lang="EN")
    await translator.translate("рост ягодиц", source_lang="RU", target_lang="EN-US")
    assert fake.translate_calls == [
        ("рост ягодиц", "RU", "EN-US"),
        ("рост ягодиц", "RU", "EN-US"),
    ]
    assert all(target != "EN" for _, _, target in fake.translate_calls)


@pytest.mark.asyncio
async def test_remaining_quota_error() -> None:
    fake = FakeDeepL()
    fake.error = deepl.QuotaExceededException("quota", http_status_code=456)
    translator = DeeplTranslator(_settings(), client=fake)
    with pytest.raises(TranslationUnavailable):
        await translator.remaining_characters()
