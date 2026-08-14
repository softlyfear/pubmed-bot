"""Кэш-aside перевода: хит без API, промах, порог квоты fulltext, rewriter запросов."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory
from pubmed_bot.config import get_settings
from pubmed_bot.domain.enums import TranslationKind
from pubmed_bot.domain.exceptions import QueryRewriteUnavailable, TranslationUnavailable
from pubmed_bot.services.translation import TranslationService, source_hash


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pairs: list[tuple[str, str]] = []
        self.remaining: int | None = 100_000
        self.result = "перевод"
        self.error: Exception | None = None

    async def translate(
        self,
        text: str,
        *,
        source_lang: str = "EN",
        target_lang: str = "RU",
    ) -> str:
        self.calls.append(text)
        self.pairs.append((source_lang, target_lang))
        if self.error is not None:
            raise self.error
        return self.result

    async def remaining_characters(self) -> int | None:
        return self.remaining


class FakeRewriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result: str | None = None
        self.error: Exception | None = None

    async def rewrite(self, original: str, english_draft: str) -> str:
        self.calls.append((original, english_draft))
        if self.error is not None:
            raise self.error
        if self.result is None:
            return english_draft
        return self.result


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


def _service(
    db_path: Path,
) -> tuple[TranslationService, FakeTranslator, FakeRewriter, AsyncEngine]:
    engine = create_engine_from_path(db_path)
    fake = FakeTranslator()
    rewriter = FakeRewriter()
    svc = TranslationService(fake, rewriter, session_factory(engine), min_chars_remaining=20_000)
    return svc, fake, rewriter, engine


@pytest.mark.asyncio
async def test_cache_miss_then_hit_skips_api(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        first = await svc.translate("123", TranslationKind.TITLE, "Hello")
        second = await svc.translate("123", TranslationKind.TITLE, "Hello")
        assert first == "перевод"
        assert second == "перевод"
        assert fake.calls == ["Hello"]
        assert rewriter.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cache_miss_on_different_source_hash(sqlite_file: Path) -> None:
    svc, fake, _rewriter, engine = _service(sqlite_file)
    try:
        await svc.translate("123", TranslationKind.TITLE, "Hello")
        fake.result = "другой"
        changed = await svc.translate("123", TranslationKind.TITLE, "Hello!")
        assert changed == "другой"
        assert fake.calls == ["Hello", "Hello!"]
        assert source_hash("Hello") != source_hash("Hello!")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fulltext_quota_below_threshold_skips_api(sqlite_file: Path) -> None:
    svc, fake, _rewriter, engine = _service(sqlite_file)
    try:
        fake.remaining = 19_999
        with pytest.raises(TranslationUnavailable, match="перевод недоступен"):
            await svc.translate("123", TranslationKind.FULLTEXT, "Long OA text")
        assert fake.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_title_ignores_fulltext_threshold(sqlite_file: Path) -> None:
    svc, fake, _rewriter, engine = _service(sqlite_file)
    try:
        fake.remaining = 0
        text = await svc.translate("123", TranslationKind.TITLE, "Hello")
        assert text == "перевод"
        assert fake.calls == ["Hello"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_error_not_swallowed(sqlite_file: Path) -> None:
    svc, fake, _rewriter, engine = _service(sqlite_file)
    try:
        fake.error = TranslationUnavailable("перевод недоступен")
        with pytest.raises(TranslationUnavailable):
            await svc.translate("123", TranslationKind.ABSTRACT, "Abstract")
        assert fake.calls == ["Abstract"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_latin_skips_deepl(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        assert await svc.translate_query("  gluteus  ") == "gluteus"
        assert await svc.translate_query("exercise[tiab]") == "exercise[tiab]"
        assert fake.calls == []
        assert rewriter.calls == [
            ("gluteus", "gluteus"),
            ("exercise[tiab]", "exercise[tiab]"),
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_cyrillic_cache_hit(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.result = "glute"
        first = await svc.translate_query("Ягодицы")
        second = await svc.translate_query("Ягодицы")
        assert first == "glute"
        assert second == "glute"
        assert fake.calls == ["Ягодицы"]
        assert fake.pairs == [("RU", "EN-US")]
        assert rewriter.calls == [("Ягодицы", "glute")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_mixed_cyrillic_goes_to_deepl(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.result = "knee pain"
        assert await svc.translate_query("knee боль") == "knee pain"
        assert fake.calls == ["knee боль"]
        assert fake.pairs == [("RU", "EN-US")]
        assert rewriter.calls == [("knee боль", "knee pain")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_ignores_fulltext_quota(sqlite_file: Path) -> None:
    svc, fake, _rewriter, engine = _service(sqlite_file)
    try:
        fake.remaining = 0
        fake.result = "glute"
        assert await svc.translate_query("Ягодицы") == "glute"
        assert fake.calls == ["Ягодицы"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_empty_mt_is_unavailable(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.result = "   "
        with pytest.raises(TranslationUnavailable):
            await svc.translate_query("Ягодицы")
        assert rewriter.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_empty_rejected(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        with pytest.raises(ValueError, match="пустым"):
            await svc.translate_query("   ")
        assert fake.calls == []
        assert rewriter.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_rewriter_overrides_literal_deepl(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.result = "buttock growth"
        rewriter.result = "gluteal hypertrophy"
        assert await svc.translate_query("рост ягодиц") == "gluteal hypertrophy"
        assert rewriter.calls == [("рост ягодиц", "buttock growth")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_rewriter_fail_open_uses_draft(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.result = "buttock growth"
        rewriter.error = QueryRewriteUnavailable("rewrite недоступен")
        assert await svc.translate_query("рост ягодиц") == "buttock growth"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_rewriter_empty_and_filters_fail_open(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.result = "buttock growth"
        rewriter.result = "   "
        assert await svc.translate_query("рост ягодиц") == "buttock growth"
        rewriter.result = "glute hasabstract"
        assert await svc.translate_query("другой") == "buttock growth"
        rewriter.result = "x english[lang]"
        assert await svc.translate_query("третий") == "buttock growth"
        rewriter.result = "letter[pt]"
        assert await svc.translate_query("четвёртый") == "buttock growth"
        rewriter.result = "a" * 301
        assert await svc.translate_query("пятый") == "buttock growth"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_deepl_failure_skips_rewriter(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        fake.error = TranslationUnavailable("перевод недоступен")
        with pytest.raises(TranslationUnavailable):
            await svc.translate_query("рост ягодиц")
        assert rewriter.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_latin_rewriter_and_cache(sqlite_file: Path) -> None:
    svc, fake, rewriter, engine = _service(sqlite_file)
    try:
        rewriter.result = "androgenetic alopecia OR hair follicle"
        first = await svc.translate_query("hair growth")
        second = await svc.translate_query("hair growth")
        assert first == "androgenetic alopecia OR hair follicle"
        assert second == first
        assert fake.calls == []
        assert rewriter.calls == [("hair growth", "hair growth")]
    finally:
        await engine.dispose()
