"""Избранное по PMID. Без Telegram."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import FavoriteRepo, SearchRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.adapters.ncbi.client import PubmedClient
from pubmed_bot.adapters.ncbi.parse import parse_pubmed_xml
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import TranslationUnavailable
from pubmed_bot.domain.models import ArticleListItem
from pubmed_bot.services.search import TitleTranslator

FAVORITES_PAGE = 10


class FavoritesService:
    """Добавление, проверка, удаление и список избранного."""

    def __init__(
        self,
        pubmed: PubmedClient,
        translation: TitleTranslator,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._pubmed = pubmed
        self._translation = translation
        self._session_maker = session_maker

    async def add(self, telegram_user_id: int, pmid: str) -> bool:
        titles = await self._titles(telegram_user_id, pmid)
        if titles is None:
            return False
        title_en, title_ru = titles
        async with session_scope(self._session_maker) as session:
            await FavoriteRepo(session).upsert(
                telegram_user_id,
                pmid=pmid,
                title_en=title_en,
                title_ru=title_ru,
            )
        return True

    async def exists(self, telegram_user_id: int, pmid: str) -> bool:
        """Есть ли PMID в избранном пользователя. NCBI не вызывается."""
        async with session_scope(self._session_maker) as session:
            return await FavoriteRepo(session).exists(telegram_user_id, pmid)

    async def remove(self, telegram_user_id: int, pmid: str) -> bool:
        """Удаляет строку пользователя. Нет строки — False, без ошибки домена."""
        async with session_scope(self._session_maker) as session:
            repo = FavoriteRepo(session)
            if not await repo.exists(telegram_user_id, pmid):
                return False
            await repo.delete(telegram_user_id, pmid)
            return True

    async def list_page(self, telegram_user_id: int) -> tuple[ArticleListItem, ...]:
        async with session_scope(self._session_maker) as session:
            return await FavoriteRepo(session).list_recent(
                telegram_user_id,
                limit=FAVORITES_PAGE,
            )

    async def _titles(
        self,
        telegram_user_id: int,
        pmid: str,
    ) -> tuple[str, str | None] | None:
        async with session_scope(self._session_maker) as session:
            loaded = await SearchRepo(session).current_items(telegram_user_id)
        if loaded is not None:
            for item in loaded[0]:
                if item.pmid == pmid:
                    return item.title_en, item.title_ru
        xml = await self._pubmed.efetch([pmid], db="pubmed")
        parsed = parse_pubmed_xml(xml)
        if not parsed:
            return None
        title_en = parsed[0].title_en
        try:
            title_ru = await self._translation.translate(
                pmid,
                TranslationKind.TITLE,
                title_en,
            )
        except TranslationUnavailable:
            title_ru = None
        return title_en, title_ru
