"""Сервис статьи: EFetch, OA, перевод, viewed. Без сети."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.models import Search, SearchResult, User
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory, session_scope
from pubmed_bot.bot.formatting import article_blocks, format_article_messages
from pubmed_bot.bot.texts import (
    FULLTEXT_IN_FILE,
    FULLTEXT_UNAVAILABLE,
    TEXT_UNAVAILABLE,
    TRANSLATION_MISSING,
)
from pubmed_bot.config import get_settings
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import TranslationUnavailable
from pubmed_bot.domain.models import Article
from pubmed_bot.services.article import ArticleService, OpenedArticle
from pubmed_bot.services.export import render_export_txt
from pubmed_bot.services.search import SearchService

FIXTURES = Path(__file__).parent / "fixtures" / "ncbi"


class FakePubmed:
    def __init__(self, pubmed_xml: str, pmc_xml: str = "") -> None:
        self.pubmed_xml = pubmed_xml
        self.pmc_xml = pmc_xml
        self.efetch_calls: list[tuple[tuple[str, ...], str]] = []

    async def esearch(self, term: str, **kwargs):
        raise AssertionError("esearch не нужен")

    async def esummary(self, pmids):
        raise AssertionError("esummary не нужен")

    async def efetch(self, ids, *, db: str = "pubmed") -> str:
        self.efetch_calls.append((tuple(ids), db))
        if db == "pmc":
            return self.pmc_xml
        return self.pubmed_xml


class FakeTranslation:
    def __init__(self) -> None:
        self.fail_kind: TranslationKind | None = None

    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str:
        if self.fail_kind is kind:
            raise TranslationUnavailable("перевод недоступен")
        return f"RU:{text}"

    async def translate_query(self, query_text: str) -> str:
        return query_text.strip()


def _set_env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "d")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()


@pytest.fixture
def sqlite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "pubmed.db"
    _set_env(monkeypatch, db_path)
    command.upgrade(Config("alembic.ini"), "head")
    return db_path


async def _seed_search(engine: AsyncEngine, pmid: str, title: str) -> None:
    factory = session_factory(engine)
    async with session_scope(factory) as session:
        user = User(telegram_user_id=7, last_seen_at=datetime.now(UTC))
        session.add(user)
        await session.flush()
        search = Search(user_id=user.id, query_text="knee", query_en="knee", page=1)
        session.add(search)
        await session.flush()
        session.add(SearchResult(search_id=search.id, position=1, pmid=pmid, title_en=title))


def _xml(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _article_service(
    db_path: Path,
    pubmed_xml: str,
    pmc_xml: str = "",
) -> tuple[ArticleService, FakePubmed, FakeTranslation, AsyncEngine]:
    engine = create_engine_from_path(db_path)
    pubmed = FakePubmed(pubmed_xml, pmc_xml)
    translation = FakeTranslation()
    service = ArticleService(pubmed, translation, session_factory(engine))
    return service, pubmed, translation, engine


@pytest.mark.asyncio
async def test_open_translates_abstract_and_oa(sqlite_file: Path) -> None:
    service, pubmed, _tr, engine = _article_service(
        sqlite_file,
        _xml("pubmed_retracted.xml"),
        _xml("pmc_with_fig_table.xml"),
    )
    try:
        await _seed_search(engine, "2002", "Retracted sports medicine paper.")
        opened = await service.open(7, "2002")
        assert opened is not None
        assert opened.article.pmid == "2002"
        assert opened.title_ru is not None and opened.title_ru.startswith("RU:")
        assert opened.abstract_ru is not None
        assert opened.abstract_ru[0].text.startswith("RU:")
        assert opened.has_oa is True
        assert opened.fulltext_ru is not None
        joined = "\n".join(opened.fulltext_ru)
        assert "Figure caption" not in joined
        assert "Table caption" not in joined
        assert pubmed.efetch_calls[0] == (("2002",), "pubmed")
        assert pubmed.efetch_calls[1][1] == "pmc"
        page = await SearchService(pubmed, _tr, session_factory(engine)).current_list(7)
        assert page is not None
        assert page.items[0].viewed is True
        html = "\n".join(format_article_messages(opened))
        assert "pubmed.ncbi.nlm.nih.gov/2002" in html
        assert "10.1000/retracted" in html
        assert "PMC555" in html
        assert "First paragraph" not in html
        assert "<b>Полный текст</b>" not in html
        assert FULLTEXT_IN_FILE in html
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_pmid(sqlite_file: Path) -> None:
    service, _pubmed, _tr, engine = _article_service(
        sqlite_file,
        "<PubmedArticleSet></PubmedArticleSet>",
    )
    try:
        await _seed_search(engine, "2001", "Gone")
        assert await service.open(7, "999") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_abstract_no_oa_message(sqlite_file: Path) -> None:
    service, _pubmed, _tr, engine = _article_service(
        sqlite_file,
        _xml("pubmed_rct_no_abstract.xml"),
    )
    try:
        await _seed_search(engine, "2003", "RCT of resistance training.")
        opened = await service.open(7, "2003")
        assert opened is not None
        assert opened.article.abstract == ()
        assert opened.has_oa is False
        joined = "\n".join(article_blocks(opened))
        assert TEXT_UNAVAILABLE in joined
        assert "PubMed" in joined
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_abstract_without_oa_says_fulltext_unavailable(sqlite_file: Path) -> None:
    service, _pubmed, _tr, engine = _article_service(sqlite_file, _xml("pubmed_ahead.xml"))
    try:
        await _seed_search(engine, "2001", "Ahead of print exercise trial.")
        opened = await service.open(7, "2001")
        assert opened is not None
        assert opened.has_oa is False
        joined = "\n".join(article_blocks(opened))
        assert FULLTEXT_UNAVAILABLE in joined
        assert "PMC Open Access" in joined
        assert "pubmed.ncbi.nlm.nih.gov/2001" in joined
        assert "BACKGROUND" in joined
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_old_callback_opens_pmid_not_in_current_list(sqlite_file: Path) -> None:
    service, _pubmed, _tr, engine = _article_service(sqlite_file, _xml("pubmed_ahead.xml"))
    try:
        await _seed_search(engine, "111", "Other")
        opened = await service.open(7, "2001")
        assert opened is not None
        assert opened.article.pmid == "2001"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_translation_failure_keeps_original(sqlite_file: Path) -> None:
    service, _pubmed, translation, engine = _article_service(
        sqlite_file,
        _xml("pubmed_ahead.xml"),
    )
    translation.fail_kind = TranslationKind.ABSTRACT
    try:
        await _seed_search(engine, "2001", "Ahead of print exercise trial.")
        opened = await service.open(7, "2001")
        assert opened is not None
        assert opened.translation_failed is True
        assert opened.abstract_ru is None
        chunks = format_article_messages(opened)
        joined = "\n".join(chunks)
        assert TRANSLATION_MISSING in joined
        assert "Training load" in joined
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fulltext_quota_keeps_original_oa(sqlite_file: Path) -> None:
    service, _pubmed, translation, engine = _article_service(
        sqlite_file,
        _xml("pubmed_retracted.xml"),
        _xml("pmc_with_fig_table.xml"),
    )
    translation.fail_kind = TranslationKind.FULLTEXT
    try:
        await _seed_search(engine, "2002", "Retracted sports medicine paper.")
        opened = await service.open(7, "2002")
        assert opened is not None
        assert opened.has_oa is True
        assert opened.fulltext_ru is None
        joined = "\n".join(format_article_messages(opened))
        assert FULLTEXT_IN_FILE in joined
        assert "First paragraph" not in joined
        exported = render_export_txt(opened)
        assert "First paragraph" in exported
        assert TRANSLATION_MISSING in exported
    finally:
        await engine.dispose()


def test_article_chunks_are_new_messages_not_empty() -> None:
    opened = OpenedArticle(
        article=Article(pmid="1", title_en="T"),
        title_ru="З",
        abstract_ru=(),
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    chunks = format_article_messages(opened)
    assert chunks
    assert TEXT_UNAVAILABLE in chunks[0]
    assert "pubmed.ncbi.nlm.nih.gov/1" in chunks[0]
