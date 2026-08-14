"""Alembic env: URL только из SQLITE_PATH, без Postgres."""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from pubmed_bot.adapters.db.models import Base
from pubmed_bot.adapters.db.session import create_engine_from_path, make_sqlite_url
from pubmed_bot.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _engine() -> AsyncEngine:
    settings = Settings()
    return create_engine_from_path(settings.sqlite_path)


def run_migrations_offline() -> None:
    """Офлайн-режим: URL из SQLITE_PATH."""
    settings = Settings()
    url = make_sqlite_url(settings.sqlite_path)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = _engine()
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
