"""Запись audit_logs и TTL 31 день. Без тел статей и секретов."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import AuditLogRepo
from pubmed_bot.adapters.db.session import session_scope

logger = logging.getLogger(__name__)
RETENTION_DAYS = 31
MAX_FIELD = 500
_EVENTS = frozenset({"search", "open", "error", "ncbi", "translate", "subscription"})


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_FIELD:
        return text[:MAX_FIELD]
    return text


async def write_audit(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    event: str,
    telegram_user_id: int | None = None,
    query_text: str | None = None,
    pmid: str | None = None,
    detail: str | None = None,
) -> None:
    """Пишет событие; сбой аудита не роняет основной сценарий."""
    if event not in _EVENTS:
        logger.warning("неизвестный audit event=%s", event)
        return
    try:
        async with session_scope(session_maker) as session:
            await AuditLogRepo(session).add(
                event=event,
                telegram_user_id=telegram_user_id,
                query_text=_clip(query_text),
                pmid=_clip(pmid),
                detail=_clip(detail),
            )
    except Exception:
        logger.exception("не удалось записать audit_log")


async def purge_old_logs(session_maker: async_sessionmaker[AsyncSession]) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    async with session_scope(session_maker) as session:
        deleted = await AuditLogRepo(session).delete_older_than(cutoff)
    logger.info("audit_logs удалено=%s старше %s дней", deleted, RETENTION_DAYS)
    return deleted
