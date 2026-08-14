"""Кэш-aside перевода статей и пайплайн query_en: DeepL + rewriter."""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import QueryTranslationCacheRepo, TranslationCacheRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.adapters.llm.openai_rewriter import QueryRewriter
from pubmed_bot.adapters.translator.deepl import Translator
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import QueryRewriteUnavailable, TranslationUnavailable
from pubmed_bot.domain.script import contains_cyrillic

_REWRITE_MAX_LEN = 300


def source_hash(text: str) -> str:
    """Хэш исходника для ключа кэша (UTF-8 SHA-256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rewrite_rejected(text: str) -> bool:
    if len(text) > _REWRITE_MAX_LEN:
        return True
    return "hasabstract" in text or "english[lang]" in text or "[pt]" in text


class TranslationService:
    """Статьи EN→RU с кэшем; запросы: DeepL при кириллице, затем GPT-rewriter."""

    def __init__(
        self,
        translator: Translator,
        rewriter: QueryRewriter,
        session_maker: async_sessionmaker[AsyncSession],
        min_chars_remaining: int,
    ) -> None:
        self._translator = translator
        self._rewriter = rewriter
        self._session_maker = session_maker
        self._min_chars_remaining = min_chars_remaining

    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str:
        digest = source_hash(text)
        async with session_scope(self._session_maker) as session:
            cached = await TranslationCacheRepo(session).get(pmid, kind, digest)
            if cached is not None:
                return cached
        if kind is TranslationKind.FULLTEXT:
            remaining = await self._translator.remaining_characters()
            if remaining is None or remaining < self._min_chars_remaining:
                raise TranslationUnavailable("перевод недоступен")
        translated = await self._translator.translate(text, source_lang="EN", target_lang="RU")
        async with session_scope(self._session_maker) as session:
            await TranslationCacheRepo(session).put(pmid, kind, digest, translated)
        return translated

    async def translate_query(self, query_text: str) -> str:
        """Финальный английский term: кэш, DeepL при кириллице, rewriter, fail-open GPT."""
        query = query_text.strip()
        if not query:
            msg = "текст запроса не должен быть пустым"
            raise ValueError(msg)
        digest = source_hash(query)
        async with session_scope(self._session_maker) as session:
            cached = await QueryTranslationCacheRepo(session).get(digest)
            if cached is not None:
                return cached
        if contains_cyrillic(query):
            translated = await self._translator.translate(
                query, source_lang="RU", target_lang="EN-US"
            )
            draft = translated.strip()
            if not draft:
                raise TranslationUnavailable("перевод недоступен")
        else:
            draft = query
        query_en = draft
        try:
            rewritten = (await self._rewriter.rewrite(query, draft)).strip()
        except QueryRewriteUnavailable:
            rewritten = ""
        if rewritten and not _rewrite_rejected(rewritten):
            query_en = rewritten
        async with session_scope(self._session_maker) as session:
            await QueryTranslationCacheRepo(session).put(digest, query_en)
        return query_en
