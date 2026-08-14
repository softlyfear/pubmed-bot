"""Заметки к PMID, лимит 2000 символов. Без Telegram."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import NoteRepo, SearchRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.adapters.ncbi.client import PubmedClient
from pubmed_bot.adapters.ncbi.parse import parse_pubmed_xml
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import NoteRejected, PubmedUnavailable, TranslationUnavailable
from pubmed_bot.domain.models import NoteListItem, NoteRecord
from pubmed_bot.services.search import TitleTranslator

NOTE_MAX_LEN = 2000
NOTE_PREVIEW_MAX = 120
NOTES_PAGE = 10


class NotesService:
    """Чтение, запись и список заметок. Пустое тело и сверх лимита — без INSERT/UPDATE."""

    def __init__(
        self,
        pubmed: PubmedClient,
        translation: TitleTranslator,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._pubmed = pubmed
        self._translation = translation
        self._session_maker = session_maker

    async def get(self, telegram_user_id: int, pmid: str) -> str | None:
        async with session_scope(self._session_maker) as session:
            return await NoteRepo(session).get(telegram_user_id, pmid)

    async def save(self, telegram_user_id: int, pmid: str, body: str) -> None:
        text = body.strip()
        if not text:
            raise NoteRejected("empty")
        if len(text) > NOTE_MAX_LEN:
            raise NoteRejected("too_long")
        titles = await self._titles(telegram_user_id, pmid)
        async with session_scope(self._session_maker) as session:
            await NoteRepo(session).upsert(telegram_user_id, pmid, text, titles)

    async def list_page(self, telegram_user_id: int) -> tuple[NoteListItem, ...]:
        """До 10 заметок пользователя, свежая правка сверху. NCBI не вызывается."""
        async with session_scope(self._session_maker) as session:
            rows = await NoteRepo(session).list_recent(
                telegram_user_id,
                limit=NOTES_PAGE,
            )
        return tuple(_to_list_item(row) for row in rows)

    async def delete(self, telegram_user_id: int, pmid: str) -> bool:
        """Удалить заметку пользователя. Возвращает True если была удалена."""
        async with session_scope(self._session_maker) as session:
            return await NoteRepo(session).delete(telegram_user_id, pmid)

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
        try:
            xml = await self._pubmed.efetch([pmid], db="pubmed")
        except PubmedUnavailable:
            return None
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


def _to_list_item(row: NoteRecord) -> NoteListItem:
    title = _note_title(row.title_ru, row.title_en, row.pmid)
    return NoteListItem(pmid=row.pmid, title=title, preview=_note_preview(row.body))


def _note_title(title_ru: str | None, title_en: str | None, pmid: str) -> str:
    if title_ru and title_ru.strip():
        return title_ru.strip()
    if title_en and title_en.strip():
        return title_en.strip()
    return f"PMID {pmid}"


def _note_preview(body: str) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) <= NOTE_PREVIEW_MAX:
        return collapsed
    return f"{collapsed[:NOTE_PREVIEW_MAX]}…"
