"""audit_logs: запись без тел статей, TTL 31 день. Без сети."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.models import AuditLog, Search, SearchResult, User
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory, session_scope
from pubmed_bot.adapters.ncbi.client import ESearchResult
from pubmed_bot.config import get_settings
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.services.article import ArticleService
from pubmed_bot.services.audit import MAX_FIELD, RETENTION_DAYS, purge_old_logs, write_audit
from pubmed_bot.services.search import SearchService
from pubmed_bot.services.subscriptions import SubscriptionsService
from pubmed_bot.workers.audit import JOB_ID as AUDIT_JOB_ID
from pubmed_bot.workers.audit import add_purge_job
from pubmed_bot.workers.subscriptions import JOB_ID as SUB_JOB_ID
from pubmed_bot.workers.subscriptions import build_scheduler

FIXTURES = Path(__file__).parent / "fixtures" / "ncbi"


class FakePubmed:
    def __init__(self, pubmed_xml: str = "") -> None:
        self.pubmed_xml = pubmed_xml
        self.pmids: tuple[str, ...] = ("100",)

    async def esearch(self, term: str, *, page: int = 1, **kwargs) -> ESearchResult:
        pmids = self.pmids if page == 1 else ()
        return ESearchResult(pmids=pmids, count=len(pmids), retstart=0, retmax=10)

    async def esummary(self, pmids: Sequence[str]) -> dict[str, object]:
        uids = [str(item) for item in pmids]
        result: dict[str, object] = {"uids": uids}
        for pmid in uids:
            result[pmid] = {
                "uid": pmid,
                "title": f"Title {pmid}",
                "pubdate": "2024 Mar",
                "pubstatus": "4",
                "pubtype": ["Journal Article"],
            }
        return {"result": result}

    async def efetch(self, ids, *, db: str = "pubmed") -> str:
        if db == "pmc":
            return ""
        return self.pubmed_xml


class FakeTranslation:
    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str:
        return f"RU {text}"

    async def translate_query(self, query_text: str) -> str:
        return query_text.strip()


def _set_env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "secret-bot-token")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "d")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()


@pytest.fixture
def sqlite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "pubmed.db"
    _set_env(monkeypatch, db_path)
    command.upgrade(Config("alembic.ini"), "head")
    return db_path


async def _seed_user(engine: AsyncEngine, telegram_user_id: int = 7) -> None:
    async with session_scope(session_factory(engine)) as session:
        session.add(User(telegram_user_id=telegram_user_id, last_seen_at=datetime.now(UTC)))


@pytest.mark.asyncio
async def test_purge_deletes_old_keeps_fresh(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    try:
        now = datetime.now(UTC)
        async with session_scope(factory) as session:
            session.add(
                AuditLog(
                    event="search",
                    telegram_user_id=1,
                    query_text="old",
                    created_at=now - timedelta(days=RETENTION_DAYS + 1),
                )
            )
            session.add(
                AuditLog(
                    event="open",
                    telegram_user_id=1,
                    pmid="100",
                    created_at=now - timedelta(days=1),
                )
            )
        deleted = await purge_old_logs(factory)
        assert deleted == 1
        async with factory() as session:
            rows = (await session.scalars(select(AuditLog))).all()
        assert len(rows) == 1
        assert rows[0].event == "open"
        assert rows[0].pmid == "100"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_writes_query_not_article_body(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    service = SearchService(pubmed, FakeTranslation(), session_factory(engine))
    try:
        await _seed_user(engine)
        await service.run(7, "knee pain", page=1)
        async with session_factory(engine)() as session:
            row = await session.scalar(select(AuditLog).where(AuditLog.event == "search"))
        assert row is not None
        assert row.telegram_user_id == 7
        assert row.query_text == "knee pain"
        assert row.pmid is None
        assert row.detail == "query_en=knee pain"
        blob = " ".join(part for part in (row.query_text, row.detail) if part)
        assert "secret-bot-token" not in blob
        assert "Title 100" not in blob
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_open_writes_pmid_without_abstract(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    xml = (FIXTURES / "pubmed_retracted.xml").read_text(encoding="utf-8")
    pubmed = FakePubmed(xml)
    service = ArticleService(pubmed, FakeTranslation(), session_factory(engine))
    try:
        async with session_scope(session_factory(engine)) as session:
            user = User(telegram_user_id=7, last_seen_at=datetime.now(UTC))
            session.add(user)
            await session.flush()
            search = Search(user_id=user.id, query_text="knee", query_en="knee", page=1)
            session.add(search)
            await session.flush()
            session.add(SearchResult(search_id=search.id, position=1, pmid="2002", title_en="T"))
        opened = await service.open(7, "2002")
        assert opened is not None
        abstract_en = opened.article.abstract[0].text if opened.article.abstract else ""
        async with session_factory(engine)() as session:
            row = await session.scalar(select(AuditLog).where(AuditLog.event == "open"))
        assert row is not None
        assert row.telegram_user_id == 7
        assert row.pmid == "2002"
        assert row.detail is None
        assert row.query_text is None
        assert abstract_en
        assert abstract_en not in (row.detail or "")
        assert abstract_en not in (row.query_text or "")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subscribe_writes_subscription_event(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    service = SubscriptionsService(FakePubmed(), FakeTranslation(), session_factory(engine))
    try:
        async with session_scope(session_factory(engine)) as session:
            user = User(telegram_user_id=7, last_seen_at=datetime.now(UTC))
            session.add(user)
            await session.flush()
            search = Search(
                user_id=user.id,
                query_text="knee",
                query_en="knee",
                page=1,
            )
            session.add(search)
            await session.flush()
            session.add(SearchResult(search_id=search.id, position=1, pmid="100", title_en="T"))
        assert await service.subscribe_current(7) is True
        async with session_factory(engine)() as session:
            row = await session.scalar(select(AuditLog).where(AuditLog.event == "subscription"))
        assert row is not None
        assert row.telegram_user_id == 7
        assert row.query_text == "knee"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_write_audit_clips_long_query(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    try:
        await write_audit(factory, event="search", telegram_user_id=7, query_text="q" * 2000)
        async with factory() as session:
            row = await session.scalar(select(AuditLog))
        assert row is not None
        assert row.query_text is not None
        assert len(row.query_text) == MAX_FIELD
    finally:
        await engine.dispose()


def test_purge_job_registered_on_same_scheduler(tmp_path: Path) -> None:
    class Dummy:
        async def run_cycle(self) -> None:
            return None

    engine = create_engine_from_path(tmp_path / "sched.db")
    scheduler = build_scheduler(6, Dummy())
    add_purge_job(scheduler, 6, session_factory(engine))
    assert scheduler.get_job(SUB_JOB_ID) is not None
    job = scheduler.get_job(AUDIT_JOB_ID)
    assert job is not None
    assert "6" in str(job.trigger)
    assert "15" in str(job.trigger)


@pytest.mark.asyncio
async def test_purge_counts_zero_when_empty(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    try:
        assert await purge_old_logs(session_factory(engine)) == 0
        async with session_factory(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(AuditLog))
        assert count == 0
    finally:
        await engine.dispose()
