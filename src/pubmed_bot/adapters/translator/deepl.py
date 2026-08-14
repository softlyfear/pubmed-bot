"""Адаптер DeepL API Free. Sync-клиент вызывается через to_thread."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import deepl

from pubmed_bot.config import Settings
from pubmed_bot.domain.exceptions import TranslationUnavailable

_SOURCE_LANG = "EN"
_TARGET_LANG = "RU"
# DeepL отклоняет голый EN как target; для запросов используем американский английский.
_ENGLISH_TARGET_LANG = "EN-US"


def _deepl_target_lang(target_lang: str) -> str:
    """Код target, который принимает DeepL (EN → EN-US)."""
    if target_lang == "EN":
        return _ENGLISH_TARGET_LANG
    return target_lang


class Translator(Protocol):
    """Порт машинного перевода: EN→RU для статей, RU→EN для запросов. Без LLM."""

    async def translate(
        self,
        text: str,
        *,
        source_lang: str = "EN",
        target_lang: str = "RU",
    ) -> str: ...

    async def remaining_characters(self) -> int | None: ...


class _TextResult(Protocol):
    @property
    def text(self) -> str: ...


class _CharacterUsage(Protocol):
    @property
    def valid(self) -> bool: ...

    @property
    def limit(self) -> int | None: ...

    @property
    def count(self) -> int | None: ...


class _Usage(Protocol):
    @property
    def any_limit_reached(self) -> bool: ...

    @property
    def character(self) -> _CharacterUsage: ...


class DeepLApi(Protocol):
    """Минимум sync-клиента DeepL, чтобы тесты подставляли fake."""

    def translate_text(self, text: str, *, source_lang: str, target_lang: str) -> _TextResult: ...

    def get_usage(self) -> _Usage: ...


class DeeplTranslator:
    """Официальный `deepl.DeepLClient`: EN→RU для статей, RU→EN для запросов."""

    def __init__(
        self,
        settings: Settings,
        client: DeepLApi | None = None,
        *,
        to_thread: Callable[..., Awaitable[object]] | None = None,
    ) -> None:
        self._auth_key = settings.deepl_auth_key
        self._stub = client
        self._sdk: deepl.DeepLClient | None = None
        self._to_thread = to_thread

    def _translate_sync(self, text: str, source_lang: str, target_lang: str) -> str:
        resolved_target = _deepl_target_lang(target_lang)
        if self._stub is not None:
            return self._stub.translate_text(
                text,
                source_lang=source_lang,
                target_lang=resolved_target,
            ).text
        result = self._sdk_client().translate_text(
            text,
            source_lang=source_lang,
            target_lang=resolved_target,
        )
        if isinstance(result, list):
            if not result:
                raise TranslationUnavailable("перевод недоступен")
            result = result[0]
        text_out = result.text
        if not isinstance(text_out, str):
            raise TranslationUnavailable("перевод недоступен")
        return text_out

    def _remaining_sync(self) -> int | None:
        if self._stub is not None:
            usage: _Usage = self._stub.get_usage()
        else:
            usage = self._sdk_client().get_usage()
        if usage.any_limit_reached:
            return 0
        character = usage.character
        if not character.valid:
            return None
        limit = character.limit
        count = character.count
        if limit is None or count is None:
            return None
        return max(limit - count, 0)

    def _sdk_client(self) -> deepl.DeepLClient:
        if self._sdk is None:
            self._sdk = deepl.DeepLClient(self._auth_key)
        return self._sdk

    async def _invoke(self, fn: Callable[[], object]) -> object:
        if self._to_thread is None:
            return await asyncio.to_thread(fn)
        return await self._to_thread(fn)

    async def translate(
        self,
        text: str,
        *,
        source_lang: str = _SOURCE_LANG,
        target_lang: str = _TARGET_LANG,
    ) -> str:
        if not text:
            return ""
        try:
            value = await self._invoke(lambda: self._translate_sync(text, source_lang, target_lang))
        except deepl.DeepLException as exc:
            raise TranslationUnavailable("перевод недоступен") from exc
        if not isinstance(value, str):
            raise TranslationUnavailable("перевод недоступен")
        return value

    async def remaining_characters(self) -> int | None:
        try:
            value = await self._invoke(self._remaining_sync)
        except deepl.DeepLException as exc:
            raise TranslationUnavailable("перевод недоступен") from exc
        if value is not None and not isinstance(value, int):
            raise TranslationUnavailable("перевод недоступен")
        return value
