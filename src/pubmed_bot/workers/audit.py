"""Суточная очистка audit_logs в том же APScheduler."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.services.audit import purge_old_logs

JOB_ID = "pubmed-audit-purge"


def add_purge_job(
    scheduler: AsyncIOScheduler,
    hour_utc: int,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Тот же час UTC, минута 15 — меньше пересечения с подписками."""

    async def run_purge() -> None:
        await purge_old_logs(session_maker)

    scheduler.add_job(
        run_purge,
        CronTrigger(hour=hour_utc, minute=15, timezone=ZoneInfo("UTC")),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
