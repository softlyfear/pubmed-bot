"""Добор покрытия: репозитории, NCBI, parse, export, audit, лимиты."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree.ElementTree import Element

import httpx
import pytest
import respx
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, User
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.models import SearchResult
from pubmed_bot.adapters.db.models import User as DbUser
from pubmed_bot.adapters.db.repositories import (
    FavoriteRepo,
    NoteRepo,
    SearchRepo,
    SubscriptionRepo,
    UserRepo,
)
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory, session_scope
from pubmed_bot.adapters.ncbi import parse as parse_mod
from pubmed_bot.adapters.ncbi.client import EUTILS_BASE, NcbiEutilsClient
from pubmed_bot.adapters.ncbi.parse import parse_esummary, parse_pmc_xml, parse_pubmed_xml
from pubmed_bot.bot.formatting import (
    _open_tags,
    _safe_cut,
    article_blocks,
    format_list,
    split_html_messages,
)
from pubmed_bot.bot.handlers.article import on_back, on_open
from pubmed_bot.bot.handlers.extras import (
    on_export,
    on_fav_list,
    on_fav_remove,
    on_note_start,
    on_notes_list,
)
from pubmed_bot.bot.handlers.search import _send_page, on_more, on_query
from pubmed_bot.bot.handlers.subscriptions import on_sub_list, on_sub_off, on_subscribe
from pubmed_bot.bot.middlewares import (
    PrivateChatMiddleware,
    UserRateLimitMiddleware,
    UserUpsertMiddleware,
    _chat_type,
    _rate_kind,
    _telegram_user,
)
from pubmed_bot.bot.texts import NOTE_PROMPT
from pubmed_bot.config import Settings, get_settings
from pubmed_bot.domain.enums import (
    IntegrityLabel,
    PublicationStatusLabel,
    PubTypeLabel,
    TranslationKind,
)
from pubmed_bot.domain.exceptions import PubmedUnavailable, TranslationUnavailable
from pubmed_bot.domain.models import AbstractSection, Article, ArticleListItem
from pubmed_bot.services.article import ArticleService, OpenedArticle
from pubmed_bot.services.audit import write_audit
from pubmed_bot.services.export import render_export_txt
from pubmed_bot.services.favorites import FavoritesService
from pubmed_bot.services.rate_limit import PerUserWindow, TokenBucket
from pubmed_bot.services.search import SearchPage, SearchService
from pubmed_bot.services.subscriptions import SubscriptionsService
from pubmed_bot.workers.audit import add_purge_job
from tests.ncbi_xml import pubmed_articles_xml


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


def _settings() -> Settings:
    return Settings(
        bot_token="t",
        ncbi_api_key="k",
        ncbi_email="dev@example.com",
        ncbi_tool="pubmed-bot",
        deepl_auth_key="d",
        gemini_api_key="sk-test",
        sqlite_path=Path("x.db"),
        _env_file=None,
    )


class _FailTitles:
    async def translate(self, pmid: str, kind: object, text: str) -> str:
        raise TranslationUnavailable("перевод недоступен")

    async def translate_query(self, query_text: str) -> str:
        return query_text.strip()


@pytest.mark.asyncio
async def test_repos_without_user(sqlite_file: Path) -> None:
    engine: AsyncEngine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    try:
        async with session_scope(factory) as session:
            repo = SearchRepo(session)
            assert await repo.get_current(1) is None
            with pytest.raises(RuntimeError, match="поиска"):
                await repo.replace(
                    1,
                    query_text="q",
                    query_en="q",
                    page=1,
                    items=(),
                )
            await repo.save_list_message(1, 1, 1)
            assert await repo.current_items(1) is None
            await repo.mark_viewed(1, "1")
            with pytest.raises(RuntimeError, match="избранного"):
                await FavoriteRepo(session).upsert(1, pmid="1", title_en="t", title_ru=None)
            assert await FavoriteRepo(session).list_recent(1) == ()
            assert await FavoriteRepo(session).exists(1, "1") is False
            await FavoriteRepo(session).delete(1, "1")
            with pytest.raises(RuntimeError, match="заметки"):
                await NoteRepo(session).upsert(1, "1", "x")
            assert await NoteRepo(session).get(1, "1") is None
            assert await NoteRepo(session).list_recent(1) == ()
            with pytest.raises(RuntimeError, match="подписки"):
                await SubscriptionRepo(session).upsert(1, query_text="q", query_en="q")
            assert await SubscriptionRepo(session).list_for_user(1) == ()
            assert await SubscriptionRepo(session).deactivate(1, 1) is False
            assert await SubscriptionRepo(session).delivered_among(1, ()) == frozenset()
            await SubscriptionRepo(session).touch_checked(999)
        service = SearchService(MagicMock(), _FailTitles(), factory)
        assert await service.next_page(1) is None
        await service.save_list_message(1, 1, 1)
        assert await service.current_list(1) is None
        await service.mark_viewed(1, "1")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enum_or_none_invalid_label(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    try:
        async with session_scope(factory) as session:
            user = DbUser(telegram_user_id=7, last_seen_at=datetime.now(UTC))
            session.add(user)
            await session.flush()
            await SearchRepo(session).replace(
                7,
                query_text="q",
                query_en="q",
                page=1,
                items=(),
            )
            search = await SearchRepo(session).get_current(7)
            assert search is not None
            session.add(
                SearchResult(
                    search_id=search.id,
                    position=1,
                    pmid="1",
                    title_en="t",
                    status_label="nope",
                    integrity_label="nope",
                    pub_type_label="nope",
                )
            )
        async with session_scope(factory) as session:
            loaded = await SearchRepo(session).current_items(7)
        assert loaded is not None
        item = loaded[0][0]
        assert item.status_label is None
        assert item.integrity_label is None
        assert item.pub_type_label is None
        async with session_scope(factory) as session:
            await SearchRepo(session).save_list_message(7, 42, 99)
            search = await SearchRepo(session).get_current(7)
            assert search is not None
            assert search.list_chat_id == 42
            assert search.list_message_id == 99
            assert await SubscriptionRepo(session).deactivate(7, 999_999) is False
            user = await session.get(DbUser, search.user_id)
            assert user is not None
            with patch.object(UserRepo, "get_by_telegram_id", AsyncMock(return_value=user)):
                with patch.object(session, "scalar", AsyncMock(return_value=None)):
                    with pytest.raises(RuntimeError, match="подписку"):
                        await SubscriptionRepo(session).upsert(7, query_text="q", query_en="q")
            fake_rows = MagicMock()
            fake_rows.all.return_value = [("not-a-sub", 7)]
            with patch.object(session, "execute", AsyncMock(return_value=fake_rows)):
                assert await SubscriptionRepo(session).list_active() == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_esummary_fail_and_title_fail(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = MagicMock()
    pubmed.esearch = AsyncMock(
        return_value=type("R", (), {"pmids": ("1",), "count": 1, "retstart": 0, "retmax": 10})()
    )
    pubmed.efetch = AsyncMock(return_value=pubmed_articles_xml(["1"]))
    pubmed.esummary = AsyncMock(side_effect=PubmedUnavailable("down"))
    service = SearchService(pubmed, _FailTitles(), session_factory(engine))
    try:
        async with session_scope(session_factory(engine)) as session:
            session.add(DbUser(telegram_user_id=7, last_seen_at=datetime.now(UTC)))
        with pytest.raises(PubmedUnavailable):
            await service.run(7, "knee")
        pubmed.esummary = AsyncMock(return_value={"result": {"uids": []}})
        with pytest.raises(PubmedUnavailable):
            await service.run(7, "knee")
        pubmed.esummary = AsyncMock(
            return_value={
                "result": {
                    "uids": ["1"],
                    "1": {
                        "uid": "1",
                        "title": "Hello",
                        "pubdate": "2024 Mar",
                        "pubstatus": "4",
                        "pubtype": ["Journal Article"],
                    },
                }
            }
        )
        page = await service.run(7, "knee")
        assert page.items[0].title_ru is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_article_pubmed_and_pmc_errors(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = MagicMock()
    pubmed.efetch = AsyncMock(side_effect=PubmedUnavailable("down"))
    service = ArticleService(pubmed, _FailTitles(), session_factory(engine))
    try:
        with pytest.raises(PubmedUnavailable):
            await service.open(7, "1")
        xml = """<PubmedArticleSet><PubmedArticle>
            <MedlineCitation><PMID>1</PMID>
            <Article><ArticleTitle>T</ArticleTitle></Article></MedlineCitation>
            <PubmedData><ArticleIdList>
            <ArticleId IdType="pmc">PMC1</ArticleId></ArticleIdList></PubmedData>
            </PubmedArticle></PubmedArticleSet>"""
        pubmed.efetch = AsyncMock(side_effect=[xml, PubmedUnavailable("down")])
        opened = await service.open(7, "1")
        assert opened is not None
        assert opened.has_oa is False
        assert await service._try_translate("1", TranslationKind.TITLE, "") == ("", False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_favorites_translation_fail(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = MagicMock()
    pubmed.efetch = AsyncMock(
        return_value="""<PubmedArticleSet><PubmedArticle>
        <MedlineCitation><PMID>9</PMID>
        <Article><ArticleTitle>Hello</ArticleTitle></Article></MedlineCitation>
        </PubmedArticle></PubmedArticleSet>"""
    )
    service = FavoritesService(pubmed, _FailTitles(), session_factory(engine))
    try:
        async with session_scope(session_factory(engine)) as session:
            session.add(DbUser(telegram_user_id=7, last_seen_at=datetime.now(UTC)))
        pair = await service._titles(7, "9")
        assert pair is not None
        assert pair[1] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subscriptions_title_fail_and_naive_mindate(sqlite_file: Path) -> None:
    from pubmed_bot.services.subscriptions import _mindate

    naive = datetime(2024, 1, 2, 12, 0)
    assert _mindate(naive) == "2024/01/01"
    engine = create_engine_from_path(sqlite_file)
    pubmed = MagicMock()
    pubmed.esearch = AsyncMock(
        return_value=type("R", (), {"pmids": ("1",), "count": 1, "retstart": 0, "retmax": 10})()
    )
    pubmed.efetch = AsyncMock(return_value=pubmed_articles_xml(["1"]))
    pubmed.esummary = AsyncMock(
        return_value={
            "result": {
                "uids": ["1"],
                "1": {"uid": "1", "title": "T", "pubdate": "2024 Mar", "pubstatus": "4"},
            }
        }
    )
    service = SubscriptionsService(pubmed, _FailTitles(), session_factory(engine))
    try:
        async with session_scope(session_factory(engine)) as session:
            user = DbUser(telegram_user_id=7, last_seen_at=datetime.now(UTC))
            session.add(user)
            await session.flush()
            await SearchRepo(session).replace(
                7,
                query_text="q",
                query_en="q",
                page=1,
                items=(),
            )
        await service.subscribe_current(7)
        item = (await service.list_active())[0]
        news = await service.collect_new(item)
        assert news[0].title_ru is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_write_audit_unknown_and_empty_and_error(sqlite_file: Path) -> None:
    factory = session_factory(create_engine_from_path(sqlite_file))
    await write_audit(factory, event="nope")
    await write_audit(factory, event="search", query_text="   ", pmid="  ")

    def boom_factory() -> object:
        raise RuntimeError("db")

    await write_audit(
        cast("async_sessionmaker[AsyncSession]", boom_factory),
        event="search",
        query_text="q",
    )


def test_export_and_article_blocks_branches() -> None:
    article = Article(
        pmid="1",
        title_en="EN",
        authors=("A",),
        journal="J",
        doi="10.1/x",
        pmcid="PMC1",
        date_label="2024",
        status_label=PublicationStatusLabel.PUBLISHED,
        integrity_label=IntegrityLabel.RETRACTED,
        pub_type_label=PubTypeLabel.RCT,
        abstract=(AbstractSection(text="abs", label="L"),),
        fulltext_paragraphs=("p1",),
    )
    opened = OpenedArticle(
        article=article,
        title_ru=None,
        abstract_ru=None,
        fulltext_ru=None,
        has_oa=True,
        translation_failed=True,
    )
    text = render_export_txt(opened)
    assert "перевод недоступен" in text
    assert "Полный текст" in text
    blocks = article_blocks(opened)
    assert blocks
    empty = OpenedArticle(
        article=Article(pmid="2", title_en="T"),
        title_ru=None,
        abstract_ru=None,
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    assert "текст недоступен" in render_export_txt(empty)
    typed = OpenedArticle(
        article=Article(pmid="3", title_en="T", pub_type_label=PubTypeLabel.RCT),
        title_ru=None,
        abstract_ru=None,
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    assert "RCT" in render_export_txt(typed)
    assert format_list(()) == ""
    assert split_html_messages([]) == ()
    huge = "<b>" + ("word " * 2000) + "</b>"
    chunks = split_html_messages([huge], limit=200)
    assert chunks
    cut = split_html_messages(["<i>hello &amp; <b>x"], limit=80)
    assert cut
    entity_block = "<b>" + ("&notentity " * 80) + "</b>"
    assert split_html_messages([entity_block], limit=80)
    assert _open_tags("<b>hi</b>") == []
    assert _open_tags("</i>") == []
    assert _safe_cut("hello &amp extra", 12) < 12
    assert _safe_cut("hello <b more text here", 14) < 14
    padded = ("word " * 12) + "tail"
    assert _safe_cut(padded, 50) >= 40


def test_format_list_shrinks_and_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    import pubmed_bot.bot.formatting as fmt

    monkeypatch.setattr(fmt, "TELEGRAM_MESSAGE_LIMIT", 70)
    items = tuple(
        ArticleListItem(
            pmid=str(index),
            title_en=("Very long English title " * 20) + str(index),
            title_ru=("Очень длинный заголовок " * 20) + str(index),
            date_label="Mar 2024",
            status_label=PublicationStatusLabel.PUBLISHED,
            pub_type_label=PubTypeLabel.RCT,
        )
        for index in range(1, 4)
    )
    html = fmt.format_list(items)
    assert html
    cuts = {"n": 0}

    def _cut_inside_carry(text: str, budget: int) -> int:
        cuts["n"] += 1
        if cuts["n"] == 1:
            return _safe_cut(text, budget)
        return 1

    monkeypatch.setattr(fmt, "_safe_cut", _cut_inside_carry)
    assert fmt._split_oversized("<b>" + ("word " * 80) + "</b>", 80)


def test_parse_internals() -> None:
    assert parse_esummary({"result": {"uids": ["1"], "1": {"uid": "1"}}}) == ()
    assert parse_pubmed_xml("<PubmedArticleSet><PubmedArticle/></PubmedArticleSet>") == ()
    assert parse_pubmed_xml("not-xml") == ()
    assert parse_pmc_xml("") == ()
    year_only = parse_mod._format_date_parts("2024", "99")
    assert year_only == "2024"
    assert parse_mod._format_date_parts(None, None) is None
    assert parse_mod._format_date_parts("2024", None, medline="2024 Jan")
    assert parse_mod._format_date_parts("xx", None, medline="Mar 2020") == "Mar 2020"
    assert (
        parse_mod._integrity_from_pubtypes(("Retracted Publication",)) is IntegrityLabel.RETRACTED
    )
    assert parse_mod._as_str_tuple(object()) == ()
    assert parse_mod._normalize_pubstatus(None) is None
    assert parse_mod._normalize_pubstatus("  ") is None
    assert parse_mod._status_label(None) is None
    assert parse_mod._normalize_pmcid(None) is None
    assert parse_mod._normalize_pmcid("   ") is None
    assert parse_mod._normalize_pmcid("not-digits") == "not-digits"
    xml = """<PubmedArticleSet><PubmedArticle>
      <MedlineCitation><PMID>1</PMID><Article>
      <ArticleTitle>T</ArticleTitle>
      <Abstract><CopyrightInformation>c</CopyrightInformation>
      <AbstractText></AbstractText></Abstract>
      <AuthorList>
        <Author><LastName>Doe</LastName><ForeName>John</ForeName></Author>
        <Author><LastName>Roe</LastName></Author>
      </AuthorList>
      </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    parsed = parse_pubmed_xml(xml)
    assert parsed
    dated = """<PubmedArticleSet><PubmedArticle>
      <MedlineCitation><PMID>2</PMID><Article>
      <ArticleTitle>Dated</ArticleTitle>
      <ArticleDate DateType="Electronic"><Year>2023</Year><Month>5</Month></ArticleDate>
      </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    dated_parsed = parse_pubmed_xml(dated)
    assert dated_parsed
    assert dated_parsed[0].date_label == "May 2023"
    assert parse_mod._as_str_tuple("x") == ("x",)
    assert parse_mod._as_str_tuple(None) == ()
    assert parse_mod._first_text(Element("r"), "Nope") is None
    assert parse_mod._first_text_under(Element("r"), "Nope", under="X") is None
    assert parse_mod._first_child_under(Element("r"), "Nope", under="X") is None
    skip = Element("body")
    fig = Element("fig")
    p = Element("p")
    p.text = "hi"
    fig.append(p)
    skip.append(fig)
    assert parse_mod._paragraphs_from_body(skip) == []


@pytest.mark.asyncio
@respx.mock
async def test_ncbi_client_error_branches() -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    client = NcbiEutilsClient(
        _settings(), TokenBucket(1000.0), retry_backoff_base=0.0, sleep=_no_sleep
    )
    try:
        assert await client.esummary(()) == {}
        assert await client.efetch(()) == ""
        respx.get(f"{EUTILS_BASE}/esearch.fcgi").mock(return_value=httpx.Response(200, json=[1]))
        with pytest.raises(PubmedUnavailable):
            await client.esearch("x")
        respx.get(f"{EUTILS_BASE}/esearch.fcgi").mock(
            return_value=httpx.Response(200, json={"not": "esearch"})
        )
        result = await client.esearch("x")
        assert result.pmids == ()
        respx.get(f"{EUTILS_BASE}/esearch.fcgi").mock(
            return_value=httpx.Response(200, text="not-json")
        )
        with pytest.raises(PubmedUnavailable, match="JSON"):
            await client.esearch("x")
        respx.get(f"{EUTILS_BASE}/esearch.fcgi").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(PubmedUnavailable):
            await client.esearch("x")
        respx.get(f"{EUTILS_BASE}/esearch.fcgi").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "nope"})
        )
        with pytest.raises(PubmedUnavailable):
            await client.esearch("x")
    finally:
        await client.aclose()


def test_rate_limit_guards_and_expiry() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        PerUserWindow(0)
    clock = {"now": 0.0}

    def mono() -> float:
        return clock["now"]

    window = PerUserWindow(1, window_sec=10.0, monotonic=mono)
    assert window.allow(1) is True
    clock["now"] = 11.0
    assert window.allow(1) is True


@pytest.mark.asyncio
async def test_middleware_helpers_and_purge(sqlite_file: Path) -> None:
    assert _chat_type(TelegramObject()) is None
    assert _telegram_user(TelegramObject()) is None
    assert _rate_kind(TelegramObject()) is None
    user = User.model_validate({"id": 1, "is_bot": False, "first_name": "A"})
    message = Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": 1, "type": "private"}),
            "from": user,
            "caption": "knee",
        }
    )
    assert _rate_kind(message) == "search"
    cb = CallbackQuery.model_validate(
        {
            "id": "1",
            "from": user,
            "chat_instance": "x",
            "data": "p:next",
            "message": message.model_dump(mode="python"),
        }
    )
    assert _chat_type(cb) == "private"
    assert _rate_kind(cb) == "search"
    mw = PrivateChatMiddleware()
    called = AsyncMock(return_value="ok")
    assert await mw(called, TelegramObject(), {}) is None
    upsert = UserUpsertMiddleware(MagicMock())
    assert await upsert(called, TelegramObject(), {}) is None
    limiter = UserRateLimitMiddleware(PerUserWindow(10), PerUserWindow(10))
    assert await limiter(called, TelegramObject(), {}) == "ok"
    empty_msg = Message.model_validate(
        {
            "message_id": 2,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": 1, "type": "private"}),
            "from": user,
        }
    )
    assert _rate_kind(empty_msg) is None
    other_cb = CallbackQuery.model_validate(
        {
            "id": "2",
            "from": user,
            "chat_instance": "x",
            "data": "m:fav",
        }
    )
    assert _rate_kind(other_cb) is None
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    try:
        scheduler = MagicMock()
        add_purge_job(scheduler, 6, factory)
        job = scheduler.add_job.call_args.args[0]
        await job()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_handler_missing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    monkeypatch.setattr(Message, "answer", AsyncMock())
    cb = MagicMock()
    cb.from_user = None
    cb.data = "p:next"
    cb.answer = AsyncMock()
    cb.message = None
    await on_more(cb, AsyncMock())
    await on_subscribe(cb, AsyncMock())
    await on_sub_list(cb, AsyncMock())
    await on_fav_list(cb, AsyncMock())
    await on_fav_remove(cb, AsyncMock())
    await on_notes_list(cb, AsyncMock())
    await on_open(cb, AsyncMock(), AsyncMock())
    await on_back(cb, AsyncMock(), AsyncMock())
    await on_export(cb, AsyncMock())
    await on_note_start(cb, AsyncMock(), AsyncMock())
    msg = MagicMock()
    msg.from_user = None
    msg.text = "knee"
    msg.answer = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"domain": "sport"})
    await on_query(msg, state, AsyncMock())
    cb2 = MagicMock()
    cb2.from_user = User.model_validate({"id": 1, "is_bot": False, "first_name": "A"})
    cb2.data = "s:x:1"
    cb2.answer = AsyncMock()
    cb2.message = None
    subs = AsyncMock()
    subs.deactivate = AsyncMock(return_value=True)
    subs.list_for_user = AsyncMock(return_value=())
    await on_sub_off(cb2, subs)


@pytest.mark.asyncio
async def test_handler_message_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    monkeypatch.setattr(Message, "answer", AsyncMock())
    user = User.model_validate({"id": 1, "is_bot": False, "first_name": "A"})
    real_msg = Message.model_validate(
        {
            "message_id": 10,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": 1, "type": "private"}),
            "from": user,
            "text": "x",
        }
    )
    cb = MagicMock()
    cb.from_user = user
    cb.message = None
    cb.answer = AsyncMock()
    cb.data = "m:fav"
    fav = AsyncMock()
    fav.list_page = AsyncMock(return_value=())
    await on_fav_list(cb, fav)
    notes = AsyncMock()
    notes.get = AsyncMock(return_value=None)
    await on_note_start(cb, AsyncMock(), notes)
    await on_export(cb, AsyncMock())
    subs = AsyncMock()
    subs.list_for_user = AsyncMock(return_value=())
    await on_sub_list(cb, subs)
    cb.data = "o:1"
    cb.message = None
    await on_open(cb, AsyncMock(), AsyncMock())
    cb.data = "fd:1"
    cb.message = None
    fav.remove = AsyncMock(return_value=False)
    await on_fav_remove(cb, fav)
    cb.data = "m:notes"
    cb.message = None
    notes.list_page = AsyncMock(return_value=())
    await on_notes_list(cb, notes)
    cb.message = real_msg
    notes.get = AsyncMock(return_value=None)
    await on_note_start(cb, AsyncMock(), notes)
    notes.get = AsyncMock(return_value="уже есть")
    await on_note_start(cb, AsyncMock(), notes)
    monkeypatch.setattr(
        "pubmed_bot.bot.handlers.article.format_article_messages",
        lambda _opened: (),
    )
    opened = OpenedArticle(
        article=Article(pmid="1", title_en="T"),
        title_ru=None,
        abstract_ru=None,
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    articles = AsyncMock()
    articles.open = AsyncMock(return_value=opened)
    cb.data = "o:1"
    await on_open(cb, articles, AsyncMock())
    page = SearchPage(
        items=(ArticleListItem(pmid="1", title_en="T"),),
        page=1,
        has_more=False,
        empty=False,
        no_more=True,
    )
    orphan = MagicMock()
    orphan.from_user = None
    orphan.answer = AsyncMock()
    status = AsyncMock()
    await _send_page(orphan, AsyncMock(), page, status=status)
    status.edit_text.assert_awaited()
    assert NOTE_PROMPT
