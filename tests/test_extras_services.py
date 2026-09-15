"""Избранное, заметки, экспорт. Временный SQLite, без сети."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.models import Favorite, Note, Search, SearchResult, User
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory, session_scope
from pubmed_bot.bot.keyboards import CALLBACK_OPEN_PREFIX, list_keyboard
from pubmed_bot.config import get_settings
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import NoteRejected, PubmedUnavailable
from pubmed_bot.domain.models import AbstractSection, Article
from pubmed_bot.services.article import OpenedArticle
from pubmed_bot.services.export import export_filename, render_export_txt
from pubmed_bot.services.favorites import FavoritesService
from pubmed_bot.services.notes import NOTE_MAX_LEN, NotesService

FIXTURES = Path(__file__).parent / "fixtures" / "ncbi"


class FakePubmed:
    def __init__(self, xml: str, *, fail: bool = False) -> None:
        self.xml = xml
        self.fail = fail
        self.efetch_calls = 0

    async def esearch(self, term: str, **kwargs):
        raise AssertionError("esearch не нужен")

    async def esummary(self, pmids):
        raise AssertionError("esummary не нужен")

    async def efetch(self, ids, *, db: str = "pubmed") -> str:
        self.efetch_calls += 1
        if self.fail:
            raise PubmedUnavailable("down")
        return self.xml


class FakeTranslation:
    async def translate(self, pmid: str, kind, text: str) -> str:
        return f"RU:{text}"

    async def translate_query(self, query_text: str) -> str:
        return query_text.strip()


def _set_env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "secret-bot-token")
    monkeypatch.setenv("NCBI_API_KEY", "secret-ncbi")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "secret-deepl")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()


@pytest.fixture
def sqlite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "pubmed.db"
    _set_env(monkeypatch, db_path)
    command.upgrade(Config("alembic.ini"), "head")
    return db_path


async def _seed_user(engine: AsyncEngine, telegram_user_id: int = 7) -> None:
    factory = session_factory(engine)
    async with session_scope(factory) as session:
        session.add(User(telegram_user_id=telegram_user_id, last_seen_at=datetime.now(UTC)))


def _favorites(engine: AsyncEngine, pubmed: FakePubmed) -> FavoritesService:
    return FavoritesService(pubmed, FakeTranslation(), session_factory(engine))


@pytest.mark.asyncio
async def test_favorite_add_is_idempotent(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed((FIXTURES / "pubmed_ahead.xml").read_text(encoding="utf-8"))
    service = _favorites(engine, pubmed)
    try:
        await _seed_user(engine)
        assert await service.add(7, "2001") is True
        assert await service.add(7, "2001") is True
        async with session_factory(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(Favorite))
        assert count == 1
        page = await service.list_page(7)
        assert len(page) == 1
        assert page[0].pmid == "2001"
        assert page[0].title_en.startswith("Ahead")
        buttons = list_keyboard((page[0].pmid,), has_more=False)
        assert buttons.inline_keyboard[0][0].callback_data == f"{CALLBACK_OPEN_PREFIX}2001"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_favorite_missing_pmid(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed("<PubmedArticleSet></PubmedArticleSet>")
    service = _favorites(engine, pubmed)
    try:
        await _seed_user(engine)
        assert await service.add(7, "999") is False
        assert await service.list_page(7) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_favorite_uses_search_snapshot_without_ncbi(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed("")
    service = _favorites(engine, pubmed)
    try:
        await _seed_user(engine)
        async with session_scope(session_factory(engine)) as session:
            user = await session.scalar(select(User).where(User.telegram_user_id == 7))
            assert user is not None
            search = Search(user_id=user.id, query_text="knee", query_en="knee", page=1)
            session.add(search)
            await session.flush()
            session.add(
                SearchResult(
                    search_id=search.id,
                    position=1,
                    pmid="2001",
                    title_en="From list",
                    title_ru="Со списка",
                )
            )
        assert await service.add(7, "2001") is True
        assert pubmed.efetch_calls == 0
        page = await service.list_page(7)
        assert page[0].title_en == "From list"
        assert page[0].title_ru == "Со списка"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_favorite_remove_is_idempotent_and_isolated(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed((FIXTURES / "pubmed_ahead.xml").read_text(encoding="utf-8"))
    service = _favorites(engine, pubmed)
    try:
        await _seed_user(engine, 7)
        await _seed_user(engine, 8)
        assert await service.add(7, "2001") is True
        assert await service.add(8, "2001") is True
        fetches = pubmed.efetch_calls
        assert await service.exists(7, "2001") is True
        assert await service.remove(7, "2001") is True
        assert pubmed.efetch_calls == fetches
        assert await service.list_page(7) == ()
        assert await service.remove(7, "2001") is False
        assert await service.exists(8, "2001") is True
        page = await service.list_page(8)
        assert len(page) == 1
        assert page[0].pmid == "2001"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_save_update_and_rejects(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed("<PubmedArticleSet></PubmedArticleSet>")
    notes = NotesService(pubmed, FakeTranslation(), session_factory(engine))
    try:
        await _seed_user(engine)
        await notes.save(7, "2001", "  первая  ")
        assert await notes.get(7, "2001") == "первая"
        await notes.save(7, "2001", "вторая")
        assert await notes.get(7, "2001") == "вторая"
        with pytest.raises(NoteRejected):
            await notes.save(7, "2001", "   ")
        with pytest.raises(NoteRejected):
            await notes.save(7, "2001", "x" * (NOTE_MAX_LEN + 1))
        assert await notes.get(7, "2001") == "вторая"
        await notes.save(7, "2001", "y" * NOTE_MAX_LEN)
        assert await notes.get(7, "2001") == "y" * NOTE_MAX_LEN
        async with session_factory(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(Note))
        assert count == 1
        page = await notes.list_page(7)
        assert page[0].title == "PMID 2001"
        assert page[0].preview == ("y" * 120) + "…"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_notes_list_order_preview_and_ncbi_failure(sqlite_file: Path) -> None:
    from pubmed_bot.bot.formatting import format_notes_list

    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed("", fail=True)
    notes = NotesService(pubmed, FakeTranslation(), session_factory(engine))
    try:
        await _seed_user(engine)
        await notes.save(7, "1", "alpha")
        await notes.save(7, "2", "beta <tag> & more")
        await notes.save(7, "1", "alpha2")
        assert await notes.get(7, "1") == "alpha2"
        assert pubmed.efetch_calls >= 1
        empty = NotesService(pubmed, FakeTranslation(), session_factory(engine))
        assert await empty.list_page(8) == ()
        page = await notes.list_page(7)
        assert [item.pmid for item in page] == ["1", "2"]
        assert page[0].title == "PMID 1"
        assert page[1].preview == "beta <tag> & more"
        await notes.save(7, "3", "word " * 50)
        page = await notes.list_page(7)
        preview = next(item.preview for item in page if item.pmid == "3")
        assert len(preview) == 121
        assert preview.endswith("…")
        html = format_notes_list(page)
        assert "&lt;tag&gt;" in html
        assert "&amp;" in html
        assert "<tag>" not in html
        fetches = pubmed.efetch_calls
        listed = await notes.list_page(7)
        assert listed
        assert pubmed.efetch_calls == fetches
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_save_uses_search_snapshot_without_ncbi(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed("")
    notes = NotesService(pubmed, FakeTranslation(), session_factory(engine))
    try:
        await _seed_user(engine)
        async with session_scope(session_factory(engine)) as session:
            user = await session.scalar(select(User).where(User.telegram_user_id == 7))
            assert user is not None
            search = Search(user_id=user.id, query_text="knee", query_en="knee", page=1)
            session.add(search)
            await session.flush()
            session.add(
                SearchResult(
                    search_id=search.id,
                    position=1,
                    pmid="2001",
                    title_en="From list",
                    title_ru="Со списка",
                )
            )
        await notes.save(7, "2001", "заметка")
        assert pubmed.efetch_calls == 0
        page = await notes.list_page(7)
        assert page[0].title == "Со списка"
        assert page[0].preview == "заметка"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_save_ncbi_title_and_translation_failure(sqlite_file: Path) -> None:
    from pubmed_bot.domain.exceptions import TranslationUnavailable

    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed((FIXTURES / "pubmed_ahead.xml").read_text(encoding="utf-8"))
    notes = NotesService(pubmed, FakeTranslation(), session_factory(engine))
    try:
        await _seed_user(engine)
        await notes.save(7, "2001", "body")
        page = await notes.list_page(7)
        assert page[0].title.startswith("RU:")
    finally:
        await engine.dispose()

    class FailTranslation:
        async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str:
            raise TranslationUnavailable("down")

        async def translate_query(self, query_text: str) -> str:
            return query_text.strip()

    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed((FIXTURES / "pubmed_ahead.xml").read_text(encoding="utf-8"))
    notes = NotesService(pubmed, FailTranslation(), session_factory(engine))
    try:
        await notes.save(7, "2001", "body2")
        page = await notes.list_page(7)
        assert page[0].title.startswith("RU:")
        assert await notes.get(7, "2001") == "body2"
        await notes.save(7, "9999", "only-en")
        page = await notes.list_page(7)
        fresh = next(item for item in page if item.pmid == "9999")
        assert fresh.title.startswith("Ahead")
        assert await notes.get(7, "9999") == "only-en"
    finally:
        await engine.dispose()


def test_export_utf8_txt_without_secrets() -> None:
    opened = OpenedArticle(
        article=Article(
            pmid="2001",
            title_en="Title & more",
            doi="10.1000/ahead.1",
            abstract=(AbstractSection(text="Abs", label="BACKGROUND"),),
        ),
        title_ru="Заголовок",
        abstract_ru=(AbstractSection(text="Абс", label="BACKGROUND"),),
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    text = render_export_txt(opened)
    raw = text.encode("utf-8")
    assert raw.decode("utf-8") == text
    assert export_filename("2001") == "pubmed-2001.txt"
    assert "Заголовок" in text
    assert "pubmed.ncbi.nlm.nih.gov/2001" in text
    assert "полный текст недоступен (только PMC Open Access, не PDF журнала)" in text
    assert "secret-bot-token" not in text
    assert "DEEPL" not in text
    assert "BOT_TOKEN" not in text
    assert "<b>" not in text
