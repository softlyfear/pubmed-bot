"""Суточный прогон подписок в том же процессе. Без хендлеров."""

from __future__ import annotations

import logging
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from pubmed_bot.adapters.ncbi.query import PAGE_SIZE
from pubmed_bot.bot.formatting import format_list
from pubmed_bot.bot.keyboards import list_keyboard
from pubmed_bot.domain.exceptions import PubmedUnavailable
from pubmed_bot.services.subscriptions import SubscriptionsService, SubscriptionView

logger = logging.getLogger(__name__)
JOB_ID = "pubmed-subscriptions"


class SubscriptionRunner(Protocol):
    """Минимальный контракт воркера для APScheduler."""

    async def run_cycle(self) -> None: ...


class SubscriptionWorker:
    """Обходит активные подписки: NCBI → новые PMID → сообщение."""

    def __init__(self, service: SubscriptionsService, bot: Bot) -> None:
        self._service = service
        self._bot = bot

    async def run_cycle(self) -> None:
        items = await self._service.list_active()
        for item in items:
            try:
                await self._process_one(item)
            except PubmedUnavailable:
                logger.warning(
                    "NCBI недоступен для подписки id=%s user=%s",
                    item.id,
                    item.telegram_user_id,
                )
                await self._service.log_ncbi_error(item)
            except Exception:
                logger.exception("сбой проверки подписки id=%s", item.id)

    async def _process_one(self, item: SubscriptionView) -> None:
        news = await self._service.collect_new(item)
        for offset in range(0, len(news), PAGE_SIZE):
            chunk = news[offset : offset + PAGE_SIZE]
            await self._bot.send_message(
                item.telegram_user_id,
                format_list(chunk),
                reply_markup=list_keyboard(
                    tuple(row.pmid for row in chunk),
                    has_more=False,
                ),
            )
            await self._service.record_deliveries(
                item.id,
                tuple(row.pmid for row in chunk),
            )
        await self._service.touch_checked(item.id)


def build_scheduler(hour_utc: int, worker: SubscriptionRunner) -> AsyncIOScheduler:
    """Cron раз в сутки в час SUBSCRIPTION_HOUR_UTC, timezone UTC."""
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
    scheduler.add_job(
        worker.run_cycle,
        CronTrigger(hour=hour_utc, minute=0, timezone=ZoneInfo("UTC")),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
