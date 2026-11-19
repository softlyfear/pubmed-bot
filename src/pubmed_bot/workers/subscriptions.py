"""Суточный прогон подписок в том же процессе. Без хендлеров."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from pubmed_bot.adapters.ncbi.query import PAGE_SIZE
from pubmed_bot.bot.formatting import format_list
from pubmed_bot.bot.keyboards import list_keyboard
from pubmed_bot.domain.exceptions import PubmedUnavailable
from pubmed_bot.services.subscriptions import SubscriptionsService, SubscriptionView

logger = logging.getLogger(__name__)
JOB_ID = "pubmed-subscriptions"
# The scheduler is in-memory. A busy loop at :00 must not skip the daily run.
MISFIRE_GRACE_SECONDS = 6 * 60 * 60


class SubscriptionRunner(Protocol):
    """Минимальный контракт воркера для APScheduler."""

    async def run_cycle(self) -> None: ...


class SubscriptionWorker:
    """Обходит активные подписки: NCBI → новые PMID → сообщение."""

    def __init__(self, service: SubscriptionsService, bot: Bot) -> None:
        self._service = service
        self._bot = bot
        self._cycle_lock = asyncio.Lock()

    async def run_if_due(self, hour_utc: int, *, now: datetime | None = None) -> None:
        """Catch up a daily slot missed while the process was down."""
        items = await self._service.list_active()
        slot = scheduled_slot(hour_utc, now)
        if any(subscription_is_due(item, slot) for item in items):
            await self.run_cycle()

    async def run_cycle(self) -> None:
        async with self._cycle_lock:
            await self._run_locked()

    async def _run_locked(self) -> None:
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
            except Exception as exc:
                if _delivery_blocked(exc):
                    logger.warning(
                        "подписка id=%s отключена: доставка невозможна user=%s",
                        item.id,
                        item.telegram_user_id,
                    )
                    await self._service.deactivate(item.telegram_user_id, item.id)
                    continue
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
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    return scheduler


def scheduled_slot(hour_utc: int, now: datetime | None = None) -> datetime:
    """Most recent UTC cron slot at minute 0, including the current hour if it has passed."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    slot = current.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if current < slot:
        slot -= timedelta(days=1)
    return slot


def subscription_is_due(item: SubscriptionView, slot: datetime) -> bool:
    """True when this subscription existed before `slot` and was not checked after it."""
    created = _as_utc(item.created_at)
    checked = _as_utc(item.last_checked_at)
    if created is not None and created >= slot and checked is None:
        return False
    anchor = checked or created
    if anchor is None:
        return True
    return anchor < slot


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _delivery_blocked(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    if isinstance(exc, TelegramBadRequest):
        text = str(exc).casefold()
        return "chat not found" in text or "user is deactivated" in text
    return False
