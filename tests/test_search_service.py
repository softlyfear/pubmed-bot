"""Сервис поиска: 10 записей, пусто, страница 2. Без сети NCBI/DeepL."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.models import Search, SearchResult, User
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory, session_scope
from pubmed_bot.adapters.ncbi.client import ESearchResult
from pubmed_bot.adapters.ncbi.query import ENGLISH_FILTER, HASABSTRACT_FILTER
from pubmed_bot.config import get_settings
from pubmed_bot.domain.enums import SearchSort, TranslationKind
from pubmed_bot.domain.exceptions import (
    PubmedUnavailable,
    QueryRewriteUnavailable,
    TranslationUnavailable,
)
from pubmed_bot.domain.script import contains_cyrillic
from pubmed_bot.services.search import SearchService
from pubmed_bot.services.translation import TranslationService
from tests.ncbi_xml import pubmed_articles_xml


class FakePubmed:
    def __init__(self) -> None:
        self.esearch_calls: list[tuple[str, int]] = []
        self.esearch_kwargs: list[dict[str, object]] = []
        self.efetch_calls: list[tuple[str, ...]] = []
        self.offset_pmids: dict[int, tuple[str, ...]] = {
            0: tuple(str(i) for i in range(100, 110)),
        }
        self.empty_abstract: set[str] = set()
        self.error: Exception | None = None
        self.summary_payload: dict[str, object] | None = None

    async def esearch(
        self,
        term: str,
        *,
        page: int = 1,
        retstart: int | None = None,
        **kwargs: object,
    ) -> ESearchResult:
        offset = (page - 1) * 10 if retstart is None else retstart
        self.esearch_calls.append((term, offset))
        self.esearch_kwargs.append({"page": page, "retstart": retstart, **kwargs})
        if self.error is not None:
            raise self.error
        pmids = self.offset_pmids.get(offset, ())
        return ESearchResult(pmids=pmids, count=42, retstart=offset, retmax=10)

    async def esummary(self, pmids: Sequence[str]) -> dict[str, object]:
        if self.summary_payload is not None:
            return self.summary_payload
        uids = [str(item) for item in pmids]
        result: dict[str, object] = {"uids": uids}
        for pmid in uids:
            result[pmid] = {
                "uid": pmid,
                "title": f"Title {pmid}",
                "pubdate": "2024 Mar",
                "pubstatus": "4",
                "pubtype": ["Randomized Controlled Trial"],
            }
        return {"result": result}

    async def efetch(self, ids: Sequence[str], *, db: str = "pubmed") -> str:
        self.efetch_calls.append(tuple(str(item) for item in ids))
        if db != "pubmed":
            return ""
        return pubmed_articles_xml(
            tuple(str(item) for item in ids),
            empty_abstract=self.empty_abstract,
        )


class FakeTranslation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TranslationKind, str]] = []
        self.query_calls: list[str] = []
        self.query_error: Exception | None = None
        self.cyrillic_en = "glute"

    async def translate(self, pmid: str, kind: TranslationKind, text: str) -> str:
        self.calls.append((pmid, kind, text))
        return f"RU {text}"

    async def translate_query(self, query_text: str) -> str:
        self.query_calls.append(query_text)
        if self.query_error is not None:
            raise self.query_error
        if contains_cyrillic(query_text):
            return self.cyrillic_en
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


async def _seed_user(engine: AsyncEngine, telegram_user_id: int = 7) -> None:
    factory = session_factory(engine)
    async with session_scope(factory) as session:
        session.add(User(telegram_user_id=telegram_user_id, last_seen_at=datetime.now(UTC)))


def _service(db_path: Path) -> tuple[SearchService, FakePubmed, FakeTranslation, AsyncEngine]:
    engine = create_engine_from_path(db_path)
    pubmed = FakePubmed()
    translation = FakeTranslation()
    service = SearchService(pubmed, translation, session_factory(engine))
    return service, pubmed, translation, engine


@pytest.mark.asyncio
async def test_search_returns_ten_and_replaces(sqlite_file: Path) -> None:
    service, pubmed, translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        page = await service.run(7, "knee", page=1)
        assert len(page.items) == 10
        assert page.empty is False
        assert page.has_more is True
        assert all(
            item.title_ru is not None and item.title_ru.startswith("RU ") for item in page.items
        )
        assert pubmed.esearch_calls[0][1] == 0
        assert "knee" in pubmed.esearch_calls[0][0]
        assert "hasabstract" in pubmed.esearch_calls[0][0]
        assert "letter[pt]" in pubmed.esearch_calls[0][0]
        assert pubmed.esearch_kwargs[0]["sort"] is SearchSort.RELEVANCE
        assert pubmed.esearch_kwargs[0]["retstart"] == 0
        assert "humans[MeSH]" not in pubmed.esearch_calls[0][0]
        assert len(pubmed.esearch_calls) == 1
        assert len(translation.calls) == 10
        await service.run(7, "statin", page=1)
        async with session_factory(engine)() as session:
            searches = (await session.scalars(select(Search))).all()
            results = (await session.scalars(select(SearchResult))).all()
            assert len(searches) == 1
            assert searches[0].query_text == "statin"
            assert searches[0].query_en == "statin"
            assert searches[0].ncbi_retstart == 10
            assert len(results) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_first_page(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.offset_pmids[0] = ()
        page = await service.run(7, "zzzz", page=1)
        assert page.empty is True
        assert page.items == ()
        assert page.no_more is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_page_two_empty_is_no_more_without_wipe(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        await service.run(7, "knee", page=1)
        pubmed.offset_pmids[10] = ()
        second = await service.next_page(7)
        assert second is not None
        assert second.no_more is True
        async with session_factory(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(SearchResult))
            assert count == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_page_two_returns_next_pmids(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        await service.run(7, "knee", page=1)
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        second = await service.next_page(7)
        assert second is not None
        assert [item.pmid for item in second.items] == [str(i) for i in range(200, 210)]
        assert pubmed.esearch_calls[-1][1] == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unparsed_esummary_is_unavailable(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)

    pubmed.summary_payload = {
        "result": {"uids": ["1"], "1": {"error": "cannot get document summary"}}
    }
    try:
        await _seed_user(engine)
        with pytest.raises(PubmedUnavailable):
            await service.run(7, "knee")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pubmed_error_propagates(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.error = PubmedUnavailable("PubMed временно недоступен")
        with pytest.raises(PubmedUnavailable):
            await service.run(7, "knee")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cyrillic_query_goes_to_ncbi_as_english(sqlite_file: Path) -> None:
    service, pubmed, translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        page = await service.run(7, "Ягодицы")
        assert page.empty is False
        term = pubmed.esearch_calls[0][0]
        assert not contains_cyrillic(term)
        assert "glute" in term
        assert HASABSTRACT_FILTER in term
        assert ENGLISH_FILTER in term
        assert translation.query_calls == ["Ягодицы"]
        async with session_factory(engine)() as session:
            search = (await session.scalars(select(Search))).one()
            assert search.query_text == "Ягодицы"
            assert search.query_en == "glute"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_latin_query_passthrough(sqlite_file: Path) -> None:
    service, pubmed, translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        await service.run(7, "  exercise[tiab]  ")
        term = pubmed.esearch_calls[0][0]
        assert "(exercise[tiab])" in term
        assert translation.query_calls == ["exercise[tiab]"]
        async with session_factory(engine)() as session:
            search = (await session.scalars(select(Search))).one()
            assert search.query_text == "exercise[tiab]"
            assert search.query_en == "exercise[tiab]"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cyrillic_mt_failure_does_not_call_esearch(sqlite_file: Path) -> None:
    service, pubmed, translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        translation.query_error = TranslationUnavailable("перевод недоступен")
        with pytest.raises(TranslationUnavailable):
            await service.run(7, "Ягодицы")
        assert pubmed.esearch_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_next_page_uses_saved_query_en(sqlite_file: Path) -> None:
    service, pubmed, translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        await service.run(7, "Ягодицы")
        translation.query_calls.clear()
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        second = await service.next_page(7)
        assert second is not None
        assert translation.query_calls == []
        term, offset = pubmed.esearch_calls[-1]
        assert offset == 10
        assert "glute" in term
        assert not contains_cyrillic(term)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skips_empty_abstract_and_fills_from_next_window(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.offset_pmids[0] = tuple(str(i) for i in range(100, 110))
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        pubmed.empty_abstract = {"108", "109"}
        page = await service.run(7, "knee")
        assert [item.pmid for item in page.items] == [
            *[str(i) for i in range(100, 108)],
            "200",
            "201",
        ]
        assert "108" not in {item.pmid for item in page.items}
        assert "109" not in {item.pmid for item in page.items}
        assert len(page.items) == 10
        assert len(pubmed.esearch_calls) == 2
        assert pubmed.esearch_calls[0][1] == 0
        assert pubmed.esearch_calls[1][1] == 10
        async with session_factory(engine)() as session:
            search = (await session.scalars(select(Search))).one()
            assert search.ncbi_retstart == 12
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_all_live_does_not_fetch_second_window(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        page = await service.run(7, "knee")
        assert len(page.items) == 10
        assert len(pubmed.esearch_calls) == 1
        assert pubmed.esearch_calls[0][1] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_next_page_starts_after_consumed_offset_not_page_times_ten(
    sqlite_file: Path,
) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.offset_pmids[0] = tuple(str(i) for i in range(100, 110))
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        pubmed.offset_pmids[12] = tuple(str(i) for i in range(202, 212))
        pubmed.empty_abstract = {"108", "109"}
        first = await service.run(7, "knee")
        first_ids = {item.pmid for item in first.items}
        pubmed.esearch_calls.clear()
        second = await service.next_page(7)
        assert second is not None
        assert pubmed.esearch_calls[0][1] == 12
        second_ids = {item.pmid for item in second.items}
        assert first_ids.isdisjoint(second_ids)
        assert "202" in second_ids
        assert "200" not in second_ids
        assert "201" not in second_ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_window_cap_stops_after_three_windows(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.offset_pmids[0] = tuple(str(i) for i in range(100, 110))
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        pubmed.offset_pmids[20] = tuple(str(i) for i in range(300, 310))
        pubmed.offset_pmids[30] = tuple(str(i) for i in range(400, 410))
        pubmed.empty_abstract = (
            {str(i) for i in range(100, 110)}
            | {str(i) for i in range(200, 210)}
            | {str(i) for i in range(305, 310)}
        )
        page = await service.run(7, "knee")
        assert [item.pmid for item in page.items] == [str(i) for i in range(300, 305)]
        assert len(page.items) == 5
        assert len(pubmed.esearch_calls) == 3
        assert 30 not in {call[1] for call in pubmed.esearch_calls}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_page_when_next_window_empty(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.empty_abstract = {"108", "109"}
        page = await service.run(7, "knee")
        assert [item.pmid for item in page.items] == [str(i) for i in range(100, 108)]
        assert page.has_more is False
        assert len(pubmed.esearch_calls) == 2
        assert pubmed.esearch_calls[1][1] == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fills_ten_from_short_last_window_then_exhausted(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.empty_abstract = {"108", "109"}
        pubmed.offset_pmids[10] = ("200", "201")
        page = await service.run(7, "knee")
        assert [item.pmid for item in page.items][-2:] == ["200", "201"]
        assert len(page.items) == 10
        assert page.has_more is False
        async with session_factory(engine)() as session:
            search = (await session.scalars(select(Search))).one()
            assert search.ncbi_retstart == 12
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_all_empty_abstracts_is_empty_result(sqlite_file: Path) -> None:
    service, pubmed, _translation, engine = _service(sqlite_file)
    try:
        await _seed_user(engine)
        pubmed.offset_pmids[0] = tuple(str(i) for i in range(100, 110))
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        pubmed.offset_pmids[20] = tuple(str(i) for i in range(300, 310))
        pubmed.empty_abstract = (
            {str(i) for i in range(100, 110)}
            | {str(i) for i in range(200, 210)}
            | {str(i) for i in range(300, 310)}
        )
        page = await service.run(7, "knee")
        assert page.empty is True
        assert page.items == ()
        assert len(pubmed.esearch_calls) == 3
    finally:
        await engine.dispose()


class _QueryMt:
    def __init__(self) -> None:
        self.query_calls: list[str] = []
        self.result = "buttock growth"
        self.error: Exception | None = None

    async def translate(
        self,
        text: str,
        *,
        source_lang: str = "EN",
        target_lang: str = "RU",
    ) -> str:
        if source_lang == "RU":
            self.query_calls.append(text)
        if self.error is not None:
            raise self.error
        return self.result

    async def remaining_characters(self) -> int | None:
        return 100_000


class _QueryRw:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = "gluteal hypertrophy"
        self.error: Exception | None = None

    async def rewrite(self, original: str, english_draft: str) -> str:
        self.calls.append((original, english_draft))
        if self.error is not None:
            raise self.error
        return self.result


def _wired(
    db_path: Path,
) -> tuple[SearchService, FakePubmed, _QueryMt, _QueryRw, AsyncEngine]:
    engine = create_engine_from_path(db_path)
    pubmed = FakePubmed()
    mt = _QueryMt()
    rewriter = _QueryRw()
    translation = TranslationService(mt, rewriter, session_factory(engine), 20_000)
    service = SearchService(pubmed, translation, session_factory(engine))
    return service, pubmed, mt, rewriter, engine


@pytest.mark.asyncio
async def test_search_uses_rewritten_term_not_literal_deepl(sqlite_file: Path) -> None:
    service, pubmed, mt, rewriter, engine = _wired(sqlite_file)
    try:
        await _seed_user(engine)
        page = await service.run(7, "рост ягодиц")
        assert page.empty is False
        term = pubmed.esearch_calls[0][0]
        assert "gluteal hypertrophy" in term
        assert "buttock growth" not in term
        assert HASABSTRACT_FILTER in term
        assert ENGLISH_FILTER in term
        assert "letter[pt]" in term
        assert mt.query_calls == ["рост ягодиц"]
        assert rewriter.calls == [("рост ягодиц", "buttock growth")]
        async with session_factory(engine)() as session:
            search = (await session.scalars(select(Search))).one()
            assert search.query_en == "gluteal hypertrophy"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        QueryRewriteUnavailable("rewrite недоступен"),
        "",
        "glute hasabstract",
    ],
)
async def test_search_rewriter_fail_open_keeps_deepl_draft(
    sqlite_file: Path,
    broken: QueryRewriteUnavailable | str,
) -> None:
    service, pubmed, _mt, rewriter, engine = _wired(sqlite_file)
    try:
        await _seed_user(engine)
        if isinstance(broken, QueryRewriteUnavailable):
            rewriter.error = broken
        else:
            rewriter.result = broken
        page = await service.run(7, "рост ягодиц")
        assert page.empty is False
        term = pubmed.esearch_calls[0][0]
        assert "buttock growth" in term
        assert "gluteal hypertrophy" not in term
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_latin_hair_growth_rewritten(sqlite_file: Path) -> None:
    service, pubmed, mt, rewriter, engine = _wired(sqlite_file)
    try:
        await _seed_user(engine)
        rewriter.result = "androgenetic alopecia OR hair follicle"
        await service.run(7, "hair growth")
        assert mt.query_calls == []
        assert rewriter.calls == [("hair growth", "hair growth")]
        term = pubmed.esearch_calls[0][0]
        assert "androgenetic alopecia OR hair follicle" in term
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_exercise_tiab_unchanged(sqlite_file: Path) -> None:
    service, pubmed, mt, rewriter, engine = _wired(sqlite_file)
    try:
        await _seed_user(engine)
        rewriter.result = "exercise[tiab]"
        await service.run(7, "exercise[tiab]")
        assert mt.query_calls == []
        term = pubmed.esearch_calls[0][0]
        assert "(exercise[tiab])" in term
        assert rewriter.calls == [("exercise[tiab]", "exercise[tiab]")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_next_page_does_not_call_rewriter(sqlite_file: Path) -> None:
    service, pubmed, _mt, rewriter, engine = _wired(sqlite_file)
    try:
        await _seed_user(engine)
        await service.run(7, "рост ягодиц")
        assert len(rewriter.calls) == 1
        pubmed.offset_pmids[10] = tuple(str(i) for i in range(200, 210))
        second = await service.next_page(7)
        assert second is not None
        assert len(rewriter.calls) == 1
        assert "gluteal hypertrophy" in pubmed.esearch_calls[-1][0]
    finally:
        await engine.dispose()
