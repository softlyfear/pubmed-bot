"""Открытие статьи: EFetch, перевод abstract/OA, без Telegram."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import SearchRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.adapters.ncbi.client import PubmedClient
from pubmed_bot.adapters.ncbi.parse import parse_pmc_xml, parse_pubmed_xml
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import PubmedUnavailable, TranslationUnavailable
from pubmed_bot.domain.models import AbstractSection, Article
from pubmed_bot.services.audit import write_audit


class TextTranslator(Protocol):
    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OpenedArticle:
    """Статья после выбора: переводы и флаги OA/сбоя MT."""

    article: Article
    title_ru: str | None
    abstract_ru: tuple[AbstractSection, ...] | None
    fulltext_ru: tuple[str, ...] | None
    has_oa: bool
    translation_failed: bool

    def body_paragraphs(self) -> tuple[str, ...]:
        """Абзацы OA: перевод, иначе оригинал из PMC."""
        if self.fulltext_ru is not None:
            return self.fulltext_ru
        return self.article.fulltext_paragraphs


class ArticleService:
    """EFetch pubmed (+ pmc), кэш перевода, viewed_at."""

    def __init__(
        self,
        pubmed: PubmedClient,
        translation: TextTranslator,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._pubmed = pubmed
        self._translation = translation
        self._session_maker = session_maker

    async def open(self, telegram_user_id: int, pmid: str) -> OpenedArticle | None:
        try:
            xml = await self._pubmed.efetch([pmid], db="pubmed")
        except PubmedUnavailable:
            await write_audit(
                self._session_maker,
                event="error",
                telegram_user_id=telegram_user_id,
                pmid=pmid,
                detail="pubmed_unavailable",
            )
            raise
        parsed = parse_pubmed_xml(xml)
        if not parsed:
            await write_audit(
                self._session_maker,
                event="error",
                telegram_user_id=telegram_user_id,
                pmid=pmid,
                detail="not_found",
            )
            return None
        article = parsed[0]
        paragraphs = await self._load_pmc(article)
        has_oa = bool(paragraphs)
        article = replace(article, fulltext_paragraphs=paragraphs)
        async with session_scope(self._session_maker) as session:
            await SearchRepo(session).mark_viewed(telegram_user_id, pmid)
        title_ru, failed_title = await self._try_translate(
            pmid,
            TranslationKind.TITLE,
            article.title_en,
        )
        abstract_ru, failed_abs = await self._translate_abstract(pmid, article.abstract)
        fulltext_ru: tuple[str, ...] | None = None
        failed_ft = False
        if has_oa:
            joined = "\n\n".join(paragraphs)
            ru, failed_ft = await self._try_translate(pmid, TranslationKind.FULLTEXT, joined)
            if ru is not None:
                fulltext_ru = tuple(part for part in ru.split("\n\n") if part.strip())
        await write_audit(
            self._session_maker,
            event="open",
            telegram_user_id=telegram_user_id,
            pmid=pmid,
        )
        return OpenedArticle(
            article=article,
            title_ru=title_ru,
            abstract_ru=abstract_ru,
            fulltext_ru=fulltext_ru,
            has_oa=has_oa,
            translation_failed=failed_title or failed_abs or failed_ft,
        )

    async def _load_pmc(self, article: Article) -> tuple[str, ...]:
        if not article.pmcid:
            return ()
        try:
            xml = await self._pubmed.efetch([article.pmcid], db="pmc")
        except PubmedUnavailable:
            return ()
        return parse_pmc_xml(xml)

    async def _translate_abstract(
        self,
        pmid: str,
        sections: tuple[AbstractSection, ...],
    ) -> tuple[tuple[AbstractSection, ...] | None, bool]:
        if not sections:
            return (), False
        out: list[AbstractSection] = []
        failed = False
        for section in sections:
            ru, err = await self._try_translate(pmid, TranslationKind.ABSTRACT, section.text)
            if err or ru is None:
                failed = True
                out.append(section)
            else:
                out.append(AbstractSection(text=ru, label=section.label))
        if failed:
            return None, True
        return tuple(out), False

    async def _try_translate(
        self,
        pmid: str,
        kind: TranslationKind,
        text: str,
    ) -> tuple[str | None, bool]:
        if not text:
            return "", False
        try:
            return await self._translation.translate(pmid, kind, text), False
        except TranslationUnavailable:
            return None, True
