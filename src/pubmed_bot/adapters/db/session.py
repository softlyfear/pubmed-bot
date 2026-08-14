"""Async-движок SQLite: WAL и busy_timeout, без пароля и без Postgres."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_sqlite_url(path: Path) -> str:
    """Собрать URL sqlite+aiosqlite из пути к файлу."""
    resolved = path.expanduser().resolve()
    return f"sqlite+aiosqlite:///{resolved.as_posix()}"


def _enable_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """WAL, таймаут блокировки и внешние ключи на каждом connect."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine_from_path(path: Path) -> AsyncEngine:
    """Создать async-движок для файла SQLITE_PATH."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(make_sqlite_url(path), echo=False)
    event.listen(engine.sync_engine, "connect", _enable_sqlite_pragmas)
    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий для репозиториев."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Контекст сессии с commit/rollback."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
