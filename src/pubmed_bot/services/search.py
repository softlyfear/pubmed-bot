"""Разовый поиск: NCBI → перевод заголовков → замена текущего поиска."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import SearchRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.adapters.ncbi.client import PubmedClient
from pubmed_bot.adapters.ncbi.parse import has_nonempty_abstract, parse_esummary, parse_pubmed_xml
from pubmed_bot.adapters.ncbi.query import MAX_SEARCH_WINDOWS, PAGE_SIZE, build_term
from pubmed_bot.domain.enums import SearchSort, TranslationKind
from pubmed_bot.domain.exceptions import PubmedUnavailable, TranslationUnavailable
from pubmed_bot.domain.models import ArticleListItem
from pubmed_bot.services.audit import write_audit


class TitleTranslator(Protocol):
    """Перевод заголовка: pmid + kind + текст. Реализация — TranslationService."""

    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str: ...

    async def translate_query(self, query_text: str) -> str: ...


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Страница выдачи после оркестрации NCBI и перевода."""

    items: tuple[ArticleListItem, ...]
    page: int
    has_more: bool
    empty: bool
    no_more: bool


@dataclass(frozen=True, slots=True)
class _LiveFill:
    """Живые PMID одной отображаемой страницы и курсор ESearch."""

    pmids: tuple[str, ...]
    next_retstart: int
    ncbi_exhausted: bool


class SearchService:
    """Один текущий поиск на пользователя. Без Telegram."""

    def __init__(
        self,
        pubmed: PubmedClient,
        translation: TitleTranslator,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._pubmed = pubmed
        self._translation = translation
        self._session_maker = session_maker

    async def run(
        self,
        telegram_user_id: int,
        query_text: str,
        page: int = 1,
        *,
        query_en: str | None = None,
        retstart: int | None = None,
    ) -> SearchPage:
        query_text = query_text.strip()
        try:
            resolved_en = (
                query_en.strip()
                if query_en is not None
                else await self._translation.translate_query(query_text)
            )
        except TranslationUnavailable:
            await self._audit_error(telegram_user_id, query_text, detail="query_translate_failed")
            raise
        term = build_term(resolved_en)
        start = 0 if retstart is None else retstart
        try:
            fill = await self._fill_live(term, start)
        except PubmedUnavailable:
            await self._audit_error(telegram_user_id, query_text)
            raise
        if not fill.pmids:
            if page > 1:
                await self._audit_search(telegram_user_id, query_text, resolved_en)
                return SearchPage(items=(), page=page, has_more=False, empty=False, no_more=True)
            await self._replace(
                telegram_user_id,
                query_text,
                resolved_en,
                page,
                (),
                fill.next_retstart,
            )
            await self._audit_search(telegram_user_id, query_text, resolved_en)
            return SearchPage(items=(), page=page, has_more=False, empty=True, no_more=False)
        try:
            summary = await self._pubmed.esummary(fill.pmids)
        except PubmedUnavailable:
            await self._audit_error(telegram_user_id, query_text)
            raise
        items = await self._with_titles(parse_esummary(summary))
        if not items:
            await self._audit_error(telegram_user_id, query_text)
            raise PubmedUnavailable("PubMed временно недоступен")
        await self._replace(
            telegram_user_id,
            query_text,
            resolved_en,
            page,
            items,
            fill.next_retstart,
        )
        await self._audit_search(telegram_user_id, query_text, resolved_en)
        return SearchPage(
            items=items,
            page=page,
            has_more=not fill.ncbi_exhausted,
            empty=False,
            no_more=False,
        )

    async def next_page(self, telegram_user_id: int) -> SearchPage | None:
        async with session_scope(self._session_maker) as session:
            current = await SearchRepo(session).get_current(telegram_user_id)
            if current is None:
                return None
            query_text = current.query_text
            query_en = current.query_en
            page = current.page + 1
            retstart = current.ncbi_retstart
        return await self.run(
            telegram_user_id,
            query_text,
            page=page,
            query_en=query_en,
            retstart=retstart,
        )

    async def save_list_message(
        self,
        telegram_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        async with session_scope(self._session_maker) as session:
            await SearchRepo(session).save_list_message(telegram_user_id, chat_id, message_id)

    async def current_list(self, telegram_user_id: int) -> SearchPage | None:
        async with session_scope(self._session_maker) as session:
            loaded = await SearchRepo(session).current_items(telegram_user_id)
        if loaded is None:
            return None
        items, page = loaded
        return SearchPage(
            items=items,
            page=page,
            has_more=len(items) == PAGE_SIZE,
            empty=not items,
            no_more=False,
        )

    async def mark_viewed(self, telegram_user_id: int, pmid: str) -> None:
        async with session_scope(self._session_maker) as session:
            await SearchRepo(session).mark_viewed(telegram_user_id, pmid)

    async def _fill_live(self, term: str, start_retstart: int) -> _LiveFill:
        live: list[str] = []
        cursor = start_retstart
        exhausted = False
        for _window in range(MAX_SEARCH_WINDOWS):
            result = await self._pubmed.esearch(
                term,
                retstart=cursor,
                sort=SearchSort.RELEVANCE,
            )
            if not result.pmids:
                exhausted = True
                break
            xml = await self._pubmed.efetch(result.pmids, db="pubmed")
            by_pmid = {article.pmid: article for article in parse_pubmed_xml(xml)}
            window_full = len(result.pmids) == PAGE_SIZE
            for index, pmid in enumerate(result.pmids):
                cursor += 1
                article = by_pmid.get(pmid)
                if article is not None and has_nonempty_abstract(article):
                    live.append(pmid)
                if len(live) == PAGE_SIZE:
                    last_in_window = index == len(result.pmids) - 1
                    exhausted = last_in_window and not window_full
                    return _LiveFill(tuple(live), cursor, exhausted)
            if not window_full:
                exhausted = True
                break
        return _LiveFill(tuple(live), cursor, exhausted)

    async def _replace(
        self,
        telegram_user_id: int,
        query_text: str,
        query_en: str,
        page: int,
        items: tuple[ArticleListItem, ...],
        ncbi_retstart: int,
    ) -> None:
        async with session_scope(self._session_maker) as session:
            await SearchRepo(session).replace(
                telegram_user_id,
                query_text=query_text,
                query_en=query_en,
                page=page,
                items=items,
                ncbi_retstart=ncbi_retstart,
            )

    async def _audit_search(
        self,
        telegram_user_id: int,
        query_text: str,
        query_en: str,
    ) -> None:
        await write_audit(
            self._session_maker,
            event="search",
            telegram_user_id=telegram_user_id,
            query_text=query_text,
            detail=f"query_en={query_en}",
        )

    async def _audit_error(
        self,
        telegram_user_id: int,
        query_text: str,
        detail: str = "pubmed_unavailable",
    ) -> None:
        await write_audit(
            self._session_maker,
            event="error",
            telegram_user_id=telegram_user_id,
            query_text=query_text,
            detail=detail,
        )

    async def _with_titles(
        self,
        items: tuple[ArticleListItem, ...],
    ) -> tuple[ArticleListItem, ...]:
        filled: list[ArticleListItem] = []
        for item in items:
            try:
                title_ru = await self._translation.translate(
                    item.pmid,
                    TranslationKind.TITLE,
                    item.title_en,
                )
            except TranslationUnavailable:
                title_ru = None
            filled.append(replace(item, title_ru=title_ru))
        return tuple(filled)
