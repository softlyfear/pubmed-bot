"""Подписки на запрос: upsert, новые PMID, без Telegram."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import SearchRepo, SubscriptionRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.adapters.ncbi.client import PubmedClient
from pubmed_bot.adapters.ncbi.parse import has_nonempty_abstract, parse_esummary, parse_pubmed_xml
from pubmed_bot.adapters.ncbi.query import PAGE_SIZE, build_term
from pubmed_bot.domain.enums import SearchSort, TranslationKind
from pubmed_bot.domain.exceptions import TranslationUnavailable
from pubmed_bot.domain.models import ArticleListItem
from pubmed_bot.services.audit import write_audit
from pubmed_bot.services.search import TitleTranslator

DATETYPE_PDAT = "pdat"
_MINDATE_LAG = timedelta(days=1)
MAX_SUB_PAGES = 20


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    """Активная подписка для списка и воркера."""

    id: int
    telegram_user_id: int
    query_text: str
    query_en: str
    last_checked_at: datetime | None
    created_at: datetime | None


class SubscriptionsService:
    """Создание подписки с текущего поиска и выборка новых PMID."""

    def __init__(
        self,
        pubmed: PubmedClient,
        translation: TitleTranslator,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._pubmed = pubmed
        self._translation = translation
        self._session_maker = session_maker

    async def subscribe_current(self, telegram_user_id: int) -> bool:
        async with session_scope(self._session_maker) as session:
            search = await SearchRepo(session).get_current(telegram_user_id)
            if search is None or not search.query_text:
                return False
            query_text = search.query_text
            query_en = search.query_en
            seed = tuple(row.pmid for row in search.results)
            sub_id = await SubscriptionRepo(session).upsert(
                telegram_user_id,
                query_text=query_text,
                query_en=query_en,
            )
            if seed:
                await SubscriptionRepo(session).record_deliveries(sub_id, seed)
        await write_audit(
            self._session_maker,
            event="subscription",
            telegram_user_id=telegram_user_id,
            query_text=query_text,
        )
        return True

    async def list_for_user(self, telegram_user_id: int) -> tuple[SubscriptionView, ...]:
        async with session_scope(self._session_maker) as session:
            rows = await SubscriptionRepo(session).list_for_user(telegram_user_id)
        return tuple(
            SubscriptionView(
                id=row.id,
                telegram_user_id=telegram_user_id,
                query_text=row.query_text,
                query_en=row.query_en,
                last_checked_at=row.last_checked_at,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_active(self) -> tuple[SubscriptionView, ...]:
        async with session_scope(self._session_maker) as session:
            rows = await SubscriptionRepo(session).list_active()
        return tuple(
            SubscriptionView(
                id=sub.id,
                telegram_user_id=telegram_id,
                query_text=sub.query_text,
                query_en=sub.query_en,
                last_checked_at=sub.last_checked_at,
                created_at=sub.created_at,
            )
            for sub, telegram_id in rows
        )

    async def deactivate(self, telegram_user_id: int, subscription_id: int) -> bool:
        async with session_scope(self._session_maker) as session:
            return await SubscriptionRepo(session).deactivate(telegram_user_id, subscription_id)

    async def log_ncbi_error(self, item: SubscriptionView) -> None:
        await write_audit(
            self._session_maker,
            event="error",
            telegram_user_id=item.telegram_user_id,
            query_text=item.query_text,
            detail="pubmed_unavailable",
        )

    async def collect_new(self, item: SubscriptionView) -> tuple[ArticleListItem, ...]:
        term = build_term(item.query_en)
        mindate = _mindate(item.last_checked_at or item.created_at)
        found: list[str] = []
        for page in range(1, MAX_SUB_PAGES + 1):
            result = await self._pubmed.esearch(
                term,
                page=page,
                sort=SearchSort.PUB_DATE,
                mindate=mindate,
                datetype=DATETYPE_PDAT,
            )
            if not result.pmids:
                break
            found.extend(result.pmids)
            if len(result.pmids) < PAGE_SIZE:
                break
        if not found:
            return ()
        candidates = tuple(found)
        async with session_scope(self._session_maker) as session:
            already = await SubscriptionRepo(session).delivered_among(item.id, candidates)
        new_ids = tuple(pmid for pmid in candidates if pmid not in already)
        if not new_ids:
            return ()
        xml = await self._pubmed.efetch(new_ids, db="pubmed")
        by_pmid = {article.pmid: article for article in parse_pubmed_xml(xml)}
        kept = tuple(
            pmid
            for pmid in new_ids
            if (article := by_pmid.get(pmid)) is not None and has_nonempty_abstract(article)
        )
        if not kept:
            return ()
        summary = await self._pubmed.esummary(kept)
        return await self._with_titles(parse_esummary(summary))

    async def record_deliveries(self, subscription_id: int, pmids: tuple[str, ...]) -> None:
        async with session_scope(self._session_maker) as session:
            await SubscriptionRepo(session).record_deliveries(subscription_id, pmids)

    async def touch_checked(self, subscription_id: int) -> None:
        async with session_scope(self._session_maker) as session:
            await SubscriptionRepo(session).touch_checked(subscription_id)

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


def _mindate(anchor: datetime | None) -> str:
    when = anchor if anchor is not None else datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (when.astimezone(UTC) - _MINDATE_LAG).strftime("%Y/%m/%d")
