"""Подписки: upsert, новые PMID, идемпотентность deliveries. Без сети."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.models import (
    Search,
    SearchResult,
    Subscription,
    SubscriptionDelivery,
    User,
)
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory, session_scope
from pubmed_bot.adapters.ncbi.client import ESearchResult
from pubmed_bot.adapters.ncbi.query import build_term
from pubmed_bot.config import get_settings
from pubmed_bot.domain.enums import SearchSort, TranslationKind
from pubmed_bot.domain.exceptions import PubmedUnavailable
from pubmed_bot.domain.script import contains_cyrillic
from pubmed_bot.services.subscriptions import SubscriptionsService, _mindate
from pubmed_bot.workers.subscriptions import JOB_ID, SubscriptionWorker, build_scheduler
from tests.ncbi_xml import pubmed_articles_xml


class FakePubmed:
    def __init__(self) -> None:
        self.pmids: tuple[str, ...] = ("200", "201")
        self.pages: dict[int, tuple[str, ...]] | None = None
        self.error_for: set[str] = set()
        self.esearch_kwargs: list[dict[str, object]] = []
        self.empty_abstract: set[str] = set()

    async def esearch(self, term: str, *, page: int = 1, **kwargs) -> ESearchResult:
        self.esearch_kwargs.append({"term": term, "page": page, **kwargs})
        if term in self.error_for:
            raise PubmedUnavailable("PubMed временно недоступен")
        if self.pages is not None:
            pmids = self.pages.get(page, ())
        elif page == 1:
            pmids = self.pmids
        else:
            pmids = ()
        return ESearchResult(pmids=pmids, count=len(pmids), retstart=(page - 1) * 10, retmax=10)

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
        if db != "pubmed":
            return ""
        return pubmed_articles_xml(
            tuple(str(item) for item in ids),
            empty_abstract=self.empty_abstract,
        )


class FakeTranslation:
    def __init__(self) -> None:
        self.query_calls: list[str] = []

    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str:
        return f"RU {text}"

    async def translate_query(self, query_text: str) -> str:
        self.query_calls.append(query_text)
        return query_text.strip()


def _set_env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
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


async def _seed_user_and_search(
    engine: AsyncEngine,
    *,
    telegram_user_id: int = 7,
    query: str = "knee",
    query_en: str | None = None,
    pmids: tuple[str, ...] = ("100", "101"),
) -> None:
    factory = session_factory(engine)
    async with session_scope(factory) as session:
        user = User(telegram_user_id=telegram_user_id, last_seen_at=datetime.now(UTC))
        session.add(user)
        await session.flush()
        english = query if query_en is None else query_en
        search = Search(
            user_id=user.id,
            query_text=query,
            query_en=english,
            page=1,
        )
        session.add(search)
        await session.flush()
        for position, pmid in enumerate(pmids, start=1):
            session.add(
                SearchResult(
                    search_id=search.id,
                    position=position,
                    pmid=pmid,
                    title_en=f"Title {pmid}",
                )
            )


def _service(engine: AsyncEngine, pubmed: FakePubmed) -> SubscriptionsService:
    return SubscriptionsService(pubmed, FakeTranslation(), session_factory(engine))


@pytest.mark.asyncio
async def test_subscribe_is_idempotent_and_seeds_deliveries(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(engine)
        assert await service.subscribe_current(7) is True
        assert await service.subscribe_current(7) is True
        async with session_factory(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(Subscription))
            delivered = await session.scalar(select(func.count()).select_from(SubscriptionDelivery))
        assert count == 1
        assert delivered == 2
        listed = await service.list_for_user(7)
        assert len(listed) == 1
        assert listed[0].query_text == "knee"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subscribe_without_search(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    service = _service(engine, FakePubmed())
    try:
        async with session_scope(session_factory(engine)) as session:
            session.add(User(telegram_user_id=7, last_seen_at=datetime.now(UTC)))
        assert await service.subscribe_current(7) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_new_skips_already_delivered(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ("100", "200")
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(engine, pmids=("100", "101"))
        await service.subscribe_current(7)
        item = (await service.list_active())[0]
        news = await service.collect_new(item)
        assert tuple(row.pmid for row in news) == ("200",)
        await service.record_deliveries(item.id, ("200",))
        assert await service.collect_new(item) == ()
        kwargs = pubmed.esearch_kwargs[-1]
        assert kwargs["sort"] is SearchSort.PUB_DATE
        assert kwargs["datetype"] == "pdat"
        assert "mindate" in kwargs
        assert "knee" in str(kwargs["term"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_new_drops_empty_abstract_without_delivery(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ("200", "201")
    pubmed.empty_abstract = {"200"}
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(engine, pmids=("100",))
        await service.subscribe_current(7)
        item = (await service.list_active())[0]
        news = await service.collect_new(item)
        assert tuple(row.pmid for row in news) == ("201",)
        await service.record_deliveries(item.id, tuple(row.pmid for row in news))
        again = await service.collect_new(item)
        assert again == ()
        async with session_factory(engine)() as session:
            delivered = (await session.scalars(select(SubscriptionDelivery.pmid))).all()
        assert "200" not in delivered
        assert "201" in delivered
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_new_uses_query_en_not_cyrillic(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ("300",)
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(
            engine,
            query="Ягодицы",
            query_en="glute",
            pmids=("100",),
        )
        await service.subscribe_current(7)
        item = (await service.list_for_user(7))[0]
        assert item.query_text == "Ягодицы"
        assert item.query_en == "glute"
        await service.collect_new(item)
        term = str(pubmed.esearch_kwargs[0]["term"])
        assert not contains_cyrillic(term)
        assert "hasabstract" in term
        assert "editorial[pt]" in term
        assert "glute" in term
        assert not contains_cyrillic(term)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_new_does_not_call_translate_query(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ("300",)
    translation = FakeTranslation()
    service = SubscriptionsService(pubmed, translation, session_factory(engine))
    try:
        await _seed_user_and_search(engine, query="Ягодицы", query_en="glute", pmids=("100",))
        await service.subscribe_current(7)
        item = (await service.list_for_user(7))[0]
        await service.collect_new(item)
        assert translation.query_calls == []
        assert "glute" in str(pubmed.esearch_kwargs[0]["term"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deactivate_and_reactivate(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    service = _service(engine, FakePubmed())
    try:
        await _seed_user_and_search(engine)
        await service.subscribe_current(7)
        sub_id = (await service.list_for_user(7))[0].id
        assert await service.deactivate(7, sub_id) is True
        assert await service.list_for_user(7) == ()
        assert await service.subscribe_current(7) is True
        listed = await service.list_for_user(7)
        assert len(listed) == 1
        assert listed[0].id == sub_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_skips_failed_subscription(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ("300",)
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(engine, telegram_user_id=7, query="bad", pmids=("1",))
        await service.subscribe_current(7)
        async with session_scope(session_factory(engine)) as session:
            session.add(User(telegram_user_id=8, last_seen_at=datetime.now(UTC)))
        await _seed_search_only(engine, telegram_user_id=8, query="good", pmids=("2",))
        await service.subscribe_current(8)
        pubmed.error_for.add(build_term("bad"))
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        await SubscriptionWorker(service, bot).run_cycle()
        bot.send_message.assert_awaited()
        await_args = bot.send_message.await_args
        assert await_args is not None
        sent_user = await_args.args[0]
        assert sent_user == 8
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collect_new_reads_page_two(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pages = {
        1: tuple(str(i) for i in range(100, 110)),
        2: ("200",),
    }
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(
            engine,
            pmids=tuple(str(i) for i in range(100, 110)),
        )
        await service.subscribe_current(7)
        item = (await service.list_active())[0]
        news = await service.collect_new(item)
        assert tuple(row.pmid for row in news) == ("200",)
        pages = [call["page"] for call in pubmed.esearch_kwargs]
        assert pages == [1, 2]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_record_failure_does_not_stop_other_sub(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ("300",)
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(engine, telegram_user_id=7, query="one", pmids=("1",))
        await service.subscribe_current(7)
        async with session_scope(session_factory(engine)) as session:
            session.add(User(telegram_user_id=8, last_seen_at=datetime.now(UTC)))
        await _seed_search_only(engine, telegram_user_id=8, query="two", pmids=("2",))
        await service.subscribe_current(8)
        first_id = (await service.list_for_user(7))[0].id

        class BoomService(SubscriptionsService):
            @override
            async def record_deliveries(
                self,
                subscription_id: int,
                pmids: tuple[str, ...],
            ) -> None:
                if subscription_id == first_id:
                    raise RuntimeError("db")
                await SubscriptionsService.record_deliveries(self, subscription_id, pmids)

        boom = BoomService(pubmed, FakeTranslation(), session_factory(engine))
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        await SubscriptionWorker(boom, bot).run_cycle()
        users = [call.args[0] for call in bot.send_message.await_args_list]
        assert 8 in users
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mindate_is_anchor_minus_one_day(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    pubmed = FakePubmed()
    pubmed.pmids = ()
    service = _service(engine, pubmed)
    try:
        await _seed_user_and_search(engine)
        await service.subscribe_current(7)
        item = (await service.list_active())[0]
        await service.collect_new(item)
        assert pubmed.esearch_kwargs[0]["mindate"] == _mindate(item.created_at)
    finally:
        await engine.dispose()


async def _seed_search_only(
    engine: AsyncEngine,
    *,
    telegram_user_id: int,
    query: str,
    pmids: tuple[str, ...],
) -> None:
    factory = session_factory(engine)
    async with session_scope(factory) as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        assert user is not None
        search = Search(
            user_id=user.id,
            query_text=query,
            query_en=query,
            page=1,
        )
        session.add(search)
        await session.flush()
        for position, pmid in enumerate(pmids, start=1):
            session.add(
                SearchResult(
                    search_id=search.id,
                    position=position,
                    pmid=pmid,
                    title_en=f"Title {pmid}",
                )
            )


def test_mindate_is_utc_day_minus_one() -> None:
    anchor = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
    assert _mindate(anchor) == "2024/03/14"


def test_scheduler_uses_subscription_hour_utc() -> None:
    class Dummy:
        async def run_cycle(self) -> None:
            return None

    scheduler = build_scheduler(6, Dummy())
    job = scheduler.get_job(JOB_ID)
    assert job is not None
    assert "6" in str(job.trigger)
