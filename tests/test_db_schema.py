"""Схема SQLite через Alembic: UNIQUE, CASCADE, WAL. Без Postgres."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from pubmed_bot.adapters.db.models import (
    AuditLog,
    QueryTranslationCache,
    Search,
    SearchResult,
    TranslationCache,
    User,
)
from pubmed_bot.adapters.db.session import (
    create_engine_from_path,
    make_sqlite_url,
    session_factory,
    session_scope,
)
from pubmed_bot.config import get_settings

REQUIRED_TABLES = {
    "users",
    "searches",
    "search_results",
    "favorites",
    "notes",
    "subscriptions",
    "subscription_deliveries",
    "translation_cache",
    "audit_logs",
    "query_translation_cache",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("NCBI_API_KEY", "test-key")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "test-deepl")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()


def _upgrade(db_path: Path) -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    assert db_path.exists()


@pytest.fixture
def sqlite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "pubmed.db"
    _set_required_env(monkeypatch, db_path)
    _upgrade(db_path)
    return db_path


@pytest.mark.asyncio
async def test_alembic_creates_required_tables(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"),
        )
        names = {row[0] for row in rows}
    await engine.dispose()
    assert REQUIRED_TABLES.issubset(names)


def test_domain_migration_deletes_subscriptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    _set_required_env(monkeypatch, db_path)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "8c2a1b4e7d90")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_user_id, last_seen_at) VALUES (1, CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO subscriptions "
            "(user_id, domain, query_text, query_en, is_active) "
            "SELECT id, 'sport', 'knee', 'knee', 1 FROM users"
        )
        conn.execute(
            "INSERT INTO subscription_deliveries (subscription_id, pmid) "
            "SELECT id, '1' FROM subscriptions"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        sub_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()
        del_count = conn.execute("SELECT COUNT(*) FROM subscription_deliveries").fetchone()
        search_cols = {row[1] for row in conn.execute("PRAGMA table_info(searches)")}
        sub_cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
    finally:
        conn.close()
    assert sub_count == (0,)
    assert del_count == (0,)
    assert "domain" not in search_cols
    assert "domain" not in sub_cols


def test_ncbi_retstart_migration_from_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    _set_required_env(monkeypatch, db_path)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "c3d9f0a1b2e4")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_user_id, last_seen_at) VALUES (1, CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO searches (user_id, query_text, query_en, page) "
            "SELECT id, 'knee', 'knee', 2 FROM users"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(searches)")}
        value = conn.execute("SELECT ncbi_retstart FROM searches").fetchone()
    finally:
        conn.close()
    assert "ncbi_retstart" in cols
    assert cols["ncbi_retstart"][3] == 1
    assert value == (20,)


def test_query_cache_migration_deletes_old_deepl_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    _set_required_env(monkeypatch, db_path)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "d4e5f6a7b8c9")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_user_id, last_seen_at) VALUES (1, CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO searches (user_id, query_text, query_en, page, ncbi_retstart) "
            "SELECT id, 'рост ягодиц', 'buttock growth', 1, 10 FROM users"
        )
        conn.execute(
            "INSERT INTO query_translation_cache (source_hash, text_en) "
            "VALUES ('abc', 'buttock growth')"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        cache_count = conn.execute("SELECT COUNT(*) FROM query_translation_cache").fetchone()
        search_row = conn.execute("SELECT query_text, query_en FROM searches").fetchone()
    finally:
        conn.close()
    assert cache_count == (0,)
    assert search_row == ("рост ягодиц", "buttock growth")


def test_notes_title_columns_nullable_after_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    _set_required_env(monkeypatch, db_path)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "e6a7b8c9d0e1")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (telegram_user_id, last_seen_at) VALUES (1, CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO notes (user_id, pmid, body) SELECT id, '2001', 'старое тело' FROM users"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(notes)")}
        row = conn.execute("SELECT body, title_en, title_ru FROM notes").fetchone()
    finally:
        conn.close()
    assert cols["title_en"][3] == 0
    assert cols["title_ru"][3] == 0
    assert row == ("старое тело", None, None)


@pytest.mark.asyncio
async def test_wal_and_busy_timeout(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    async with engine.connect() as conn:
        journal = await conn.scalar(text("PRAGMA journal_mode"))
        timeout = await conn.scalar(text("PRAGMA busy_timeout"))
        fks = await conn.scalar(text("PRAGMA foreign_keys"))
    await engine.dispose()
    assert str(journal).lower() == "wal"
    assert int(timeout) == 5000
    assert int(fks) == 1


def test_url_is_aiosqlite_not_postgres(tmp_path: Path) -> None:
    url = make_sqlite_url(tmp_path / "x.db")
    assert url.startswith("sqlite+aiosqlite:///")
    assert "postgres" not in url
    assert "asyncpg" not in url


def test_pyproject_has_no_asyncpg() -> None:
    text_toml = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "asyncpg" not in text_toml
    assert "psycopg" not in text_toml


@pytest.mark.asyncio
async def test_one_search_per_user_unique(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as session:
        user = User(telegram_user_id=1, last_seen_at=now)
        session.add(user)
        await session.flush()
        session.add(
            Search(user_id=user.id, query_text="knee", query_en="knee", page=1),
        )
        await session.commit()
        session.add(
            Search(
                user_id=user.id,
                query_text="heart",
                query_en="heart",
                page=1,
            ),
        )
        with pytest.raises(IntegrityError):
            await session.flush()
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_delete_cascades_search(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as session:
        user = User(telegram_user_id=2, last_seen_at=now)
        session.add(user)
        await session.flush()
        session.add(Search(user_id=user.id, query_text="x", query_en="x", page=1))
        await session.commit()
        user_id = user.id
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
        left = await session.scalar(select(Search).where(Search.user_id == user_id))
        assert left is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_translation_cache_unique(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    async with factory() as session:
        session.add(
            TranslationCache(
                pmid="1",
                kind="title",
                source_hash="abc",
                text_ru="заголовок",
            ),
        )
        await session.commit()
        session.add(
            TranslationCache(
                pmid="1",
                kind="title",
                source_hash="abc",
                text_ru="повтор",
            ),
        )
        with pytest.raises(IntegrityError):
            await session.flush()
    await engine.dispose()


@pytest.mark.asyncio
async def test_query_translation_cache_unique(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    async with factory() as session:
        session.add(QueryTranslationCache(source_hash="abc", text_en="glute"))
        await session.commit()
        session.add(QueryTranslationCache(source_hash="abc", text_en="buttocks"))
        with pytest.raises(IntegrityError):
            await session.flush()
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_result_position_check(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as session:
        user = User(telegram_user_id=3, last_seen_at=now)
        session.add(user)
        await session.flush()
        search = Search(user_id=user.id, query_text="knee", query_en="knee", page=1)
        session.add(search)
        await session.flush()
        session.add(
            SearchResult(
                search_id=search.id,
                position=0,
                pmid="1",
                title_en="t",
            ),
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
        user = User(telegram_user_id=4, last_seen_at=now)
        session.add(user)
        await session.flush()
        search = Search(user_id=user.id, query_text="knee", query_en="knee", page=1)
        session.add(search)
        await session.flush()
        session.add(
            SearchResult(
                search_id=search.id,
                position=1,
                pmid="1",
                title_en="t",
            ),
        )
        await session.flush()
    await engine.dispose()


@pytest.mark.asyncio
async def test_audit_log_event_check(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    async with factory() as session:
        session.add(AuditLog(event="unknown"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
        session.add(AuditLog(event="search"))
        await session.flush()
    await engine.dispose()


@pytest.mark.asyncio
async def test_session_scope_commits(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    now = datetime.now(UTC)
    async with session_scope(factory) as session:
        session.add(User(telegram_user_id=99, last_seen_at=now))
    async with factory() as session:
        found = await session.scalar(select(User).where(User.telegram_user_id == 99))
        assert found is not None
    await engine.dispose()
