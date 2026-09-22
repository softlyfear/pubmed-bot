"""Репозитории SQLite: кэш переводов и пользователи."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pubmed_bot.adapters.db.models import (
    AuditLog,
    Favorite,
    Note,
    QueryTranslationCache,
    Search,
    SearchResult,
    Subscription,
    SubscriptionDelivery,
    TranslationCache,
    User,
)
from pubmed_bot.domain.enums import (
    IntegrityLabel,
    PublicationStatusLabel,
    PubTypeLabel,
    TranslationKind,
)
from pubmed_bot.domain.models import ArticleListItem, NoteRecord


class TranslationCacheRepo:
    """Кэш-aside: pmid + kind + source_hash."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, pmid: str, kind: TranslationKind, source_hash: str) -> str | None:
        stmt = select(TranslationCache.text_ru).where(
            TranslationCache.pmid == pmid,
            TranslationCache.kind == kind.value,
            TranslationCache.source_hash == source_hash,
        )
        return await self._session.scalar(stmt)

    async def put(
        self,
        pmid: str,
        kind: TranslationKind,
        source_hash: str,
        text_ru: str,
    ) -> None:
        stmt = (
            insert(TranslationCache)
            .values(
                pmid=pmid,
                kind=kind.value,
                source_hash=source_hash,
                text_ru=text_ru,
            )
            .on_conflict_do_nothing(index_elements=["pmid", "kind", "source_hash"])
        )
        await self._session.execute(stmt)


class QueryTranslationCacheRepo:
    """Кэш-aside финального query_en по хэшу исходника."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, source_hash: str) -> str | None:
        stmt = select(QueryTranslationCache.text_en).where(
            QueryTranslationCache.source_hash == source_hash,
        )
        return await self._session.scalar(stmt)

    async def put(self, source_hash: str, text_en: str) -> None:
        stmt = (
            insert(QueryTranslationCache)
            .values(source_hash=source_hash, text_en=text_en)
            .on_conflict_do_nothing(index_elements=["source_hash"])
        )
        await self._session.execute(stmt)


class UserRepo:
    """Upsert пользователя Telegram по telegram_user_id."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        telegram_user_id: int,
        *,
        username: str | None,
        first_name: str | None,
    ) -> None:
        now = datetime.now(UTC)
        stmt = (
            insert(User)
            .values(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                index_elements=["telegram_user_id"],
                set_={
                    "username": username,
                    "first_name": first_name,
                    "last_seen_at": now,
                },
            )
        )
        await self._session.execute(stmt)

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        stmt = select(User).where(User.telegram_user_id == telegram_user_id)
        return await self._session.scalar(stmt)


class SearchRepo:
    """Ровно один текущий поиск на пользователя: замена строки и результатов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_current(self, telegram_user_id: int) -> Search | None:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return None
        stmt = select(Search).options(selectinload(Search.results)).where(Search.user_id == user.id)
        return await self._session.scalar(stmt)

    async def replace(
        self,
        telegram_user_id: int,
        *,
        query_text: str,
        query_en: str,
        page: int,
        items: tuple[ArticleListItem, ...],
        ncbi_retstart: int = 0,
        list_chat_id: int | None = None,
        list_message_id: int | None = None,
    ) -> Search:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            msg = "пользователь должен быть в БД до поиска"
            raise RuntimeError(msg)
        await self._session.execute(delete(Search).where(Search.user_id == user.id))
        search = Search(
            user_id=user.id,
            query_text=query_text,
            query_en=query_en,
            page=page,
            ncbi_retstart=ncbi_retstart,
            list_chat_id=list_chat_id,
            list_message_id=list_message_id,
        )
        self._session.add(search)
        await self._session.flush()
        for position, item in enumerate(items, start=1):
            self._session.add(
                SearchResult(
                    search_id=search.id,
                    position=position,
                    pmid=item.pmid,
                    title_en=item.title_en,
                    title_ru=item.title_ru,
                    date_label=item.date_label,
                    status_label=item.status_label.value if item.status_label else None,
                    integrity_label=(item.integrity_label.value if item.integrity_label else None),
                    pub_type_label=item.pub_type_label.value if item.pub_type_label else None,
                )
            )
        return search

    async def save_list_message(
        self,
        telegram_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        search = await self.get_current(telegram_user_id)
        if search is None:
            return
        search.list_chat_id = chat_id
        search.list_message_id = message_id

    async def current_items(
        self,
        telegram_user_id: int,
    ) -> tuple[tuple[ArticleListItem, ...], int] | None:
        search = await self.get_current(telegram_user_id)
        if search is None:
            return None
        rows = sorted(search.results, key=lambda row: row.position)
        return tuple(_item_from_row(row) for row in rows), search.page

    async def mark_viewed(self, telegram_user_id: int, pmid: str) -> None:
        search = await self.get_current(telegram_user_id)
        if search is None:
            return
        now = datetime.now(UTC)
        for row in search.results:
            if row.pmid == pmid:
                row.viewed_at = now


def _item_from_row(row: SearchResult) -> ArticleListItem:
    return ArticleListItem(
        pmid=row.pmid,
        title_en=row.title_en,
        title_ru=row.title_ru,
        date_label=row.date_label,
        status_label=_enum_or_none(PublicationStatusLabel, row.status_label),
        integrity_label=_enum_or_none(IntegrityLabel, row.integrity_label),
        pub_type_label=_enum_or_none(PubTypeLabel, row.pub_type_label),
        viewed=row.viewed_at is not None,
    )


class FavoriteRepo:
    """Избранное: UNIQUE (user_id, pmid), идемпотентный upsert."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        telegram_user_id: int,
        *,
        pmid: str,
        title_en: str,
        title_ru: str | None,
    ) -> None:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            msg = "пользователь должен быть в БД до избранного"
            raise RuntimeError(msg)
        stmt = (
            insert(Favorite)
            .values(
                user_id=user.id,
                pmid=pmid,
                title_en=title_en,
                title_ru=title_ru,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "pmid"],
                set_={"title_en": title_en, "title_ru": title_ru},
            )
        )
        await self._session.execute(stmt)

    async def list_recent(
        self,
        telegram_user_id: int,
        *,
        limit: int = 10,
    ) -> tuple[ArticleListItem, ...]:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return ()
        stmt = (
            select(Favorite)
            .where(Favorite.user_id == user.id)
            .order_by(Favorite.created_at.desc(), Favorite.id.desc())
            .limit(limit)
        )
        rows = (await self._session.scalars(stmt)).all()
        return tuple(
            ArticleListItem(pmid=row.pmid, title_en=row.title_en, title_ru=row.title_ru)
            for row in rows
        )

    async def exists(self, telegram_user_id: int, pmid: str) -> bool:
        """True, если PMID в избранном этого пользователя."""
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return False
        stmt = select(Favorite.id).where(Favorite.user_id == user.id, Favorite.pmid == pmid)
        return await self._session.scalar(stmt) is not None

    async def delete(self, telegram_user_id: int, pmid: str) -> None:
        """Идемпотентное удаление строки UNIQUE (user_id, pmid)."""
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return
        stmt = delete(Favorite).where(Favorite.user_id == user.id, Favorite.pmid == pmid)
        await self._session.execute(stmt)


class NoteRepo:
    """Заметка к PMID: UNIQUE (user_id, pmid)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_user_id: int, pmid: str) -> str | None:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return None
        stmt = select(Note.body).where(Note.user_id == user.id, Note.pmid == pmid)
        return await self._session.scalar(stmt)

    async def upsert(
        self,
        telegram_user_id: int,
        pmid: str,
        body: str,
        titles: tuple[str, str | None] | None = None,
    ) -> None:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            msg = "пользователь должен быть в БД до заметки"
            raise RuntimeError(msg)
        now = datetime.now(UTC)
        if titles is None:
            stmt = (
                insert(Note)
                .values(user_id=user.id, pmid=pmid, body=body, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["user_id", "pmid"],
                    set_={"body": body, "updated_at": now},
                )
            )
        else:
            title_en, title_ru = titles
            conflict: dict[str, object] = {
                "body": body,
                "updated_at": now,
                "title_en": title_en,
            }
            if title_ru is not None:
                conflict["title_ru"] = title_ru
            stmt = (
                insert(Note)
                .values(
                    user_id=user.id,
                    pmid=pmid,
                    body=body,
                    updated_at=now,
                    title_en=title_en,
                    title_ru=title_ru,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "pmid"],
                    set_=conflict,
                )
            )
        await self._session.execute(stmt)

    async def list_recent(
        self,
        telegram_user_id: int,
        *,
        limit: int = 10,
    ) -> tuple[NoteRecord, ...]:
        """До `limit` заметок пользователя, свежие `updated_at` сверху."""
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return ()
        stmt = (
            select(Note)
            .where(Note.user_id == user.id)
            .order_by(Note.updated_at.desc(), Note.id.desc())
            .limit(limit)
        )
        rows = (await self._session.scalars(stmt)).all()
        return tuple(
            NoteRecord(
                pmid=row.pmid,
                body=row.body,
                title_en=row.title_en,
                title_ru=row.title_ru,
            )
            for row in rows
        )

    async def delete(self, telegram_user_id: int, pmid: str) -> bool:
        """Удалить заметку. Возвращает True если была удалена."""
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return False
        stmt = delete(Note).where(Note.user_id == user.id, Note.pmid == pmid)
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0


class SubscriptionRepo:
    """Подписки UNIQUE (user, query) и идемпотентные deliveries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        telegram_user_id: int,
        *,
        query_text: str,
        query_en: str,
    ) -> int:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            msg = "пользователь должен быть в БД до подписки"
            raise RuntimeError(msg)
        stmt = (
            insert(Subscription)
            .values(
                user_id=user.id,
                query_text=query_text,
                query_en=query_en,
                is_active=True,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "query_text"],
                set_={"is_active": True, "query_en": query_en},
            )
            .returning(Subscription.id)
        )
        sub_id = await self._session.scalar(stmt)
        if sub_id is None:
            msg = "не удалось сохранить подписку"
            raise RuntimeError(msg)
        return int(sub_id)

    async def list_for_user(self, telegram_user_id: int) -> tuple[Subscription, ...]:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return ()
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user.id, Subscription.is_active.is_(True))
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
        return tuple((await self._session.scalars(stmt)).all())

    async def list_active(self) -> tuple[tuple[Subscription, int], ...]:
        stmt = (
            select(Subscription, User.telegram_user_id)
            .join(User, User.id == Subscription.user_id)
            .where(Subscription.is_active.is_(True))
            .order_by(Subscription.id)
        )
        rows = (await self._session.execute(stmt)).all()
        items: list[tuple[Subscription, int]] = []
        for row in rows:
            subscription = row[0]
            if not isinstance(subscription, Subscription):
                continue
            items.append((subscription, int(row[1])))
        return tuple(items)

    async def deactivate(self, telegram_user_id: int, subscription_id: int) -> bool:
        user = await UserRepo(self._session).get_by_telegram_id(telegram_user_id)
        if user is None:
            return False
        stmt = select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.user_id == user.id,
        )
        row = await self._session.scalar(stmt)
        if row is None:
            return False
        row.is_active = False
        return True

    async def delivered_among(
        self,
        subscription_id: int,
        pmids: tuple[str, ...],
    ) -> frozenset[str]:
        if not pmids:
            return frozenset[str]()
        stmt = select(SubscriptionDelivery.pmid).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.pmid.in_(pmids),
        )
        rows = (await self._session.scalars(stmt)).all()
        return frozenset(str(item) for item in rows)

    async def record_deliveries(self, subscription_id: int, pmids: tuple[str, ...]) -> None:
        now = datetime.now(UTC)
        for pmid in pmids:
            stmt = (
                insert(SubscriptionDelivery)
                .values(subscription_id=subscription_id, pmid=pmid, delivered_at=now)
                .on_conflict_do_nothing(index_elements=["subscription_id", "pmid"])
            )
            await self._session.execute(stmt)

    async def touch_checked(self, subscription_id: int) -> None:
        stmt = select(Subscription).where(Subscription.id == subscription_id)
        row = await self._session.scalar(stmt)
        if row is None:
            return
        row.last_checked_at = datetime.now(UTC)


class AuditLogRepo:
    """Служебные события без тел статей; TTL чистит по created_at."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        event: str,
        telegram_user_id: int | None = None,
        query_text: str | None = None,
        pmid: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._session.add(
            AuditLog(
                event=event,
                telegram_user_id=telegram_user_id,
                query_text=query_text,
                pmid=pmid,
                detail=detail,
            )
        )

    async def delete_older_than(self, cutoff: datetime) -> int:
        stmt = delete(AuditLog).where(AuditLog.created_at < cutoff).returning(AuditLog.id)
        deleted = (await self._session.scalars(stmt)).all()
        return len(deleted)


def _enum_or_none[T: StrEnum](enum_cls: type[T], value: str | None) -> T | None:
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None
