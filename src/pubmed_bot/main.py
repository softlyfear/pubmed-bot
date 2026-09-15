"""Точка входа: long polling aiogram, без webhook."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory
from pubmed_bot.adapters.llm.gemini_rewriter import GeminiQueryRewriter
from pubmed_bot.adapters.ncbi.client import NcbiEutilsClient
from pubmed_bot.adapters.translator.deepl import DeeplTranslator
from pubmed_bot.bot.factory import create_dispatcher
from pubmed_bot.config import get_settings
from pubmed_bot.logging import setup_logging
from pubmed_bot.services.article import ArticleService
from pubmed_bot.services.favorites import FavoritesService
from pubmed_bot.services.notes import NotesService
from pubmed_bot.services.rate_limit import TokenBucket
from pubmed_bot.services.search import SearchService
from pubmed_bot.services.subscriptions import SubscriptionsService
from pubmed_bot.services.translation import TranslationService
from pubmed_bot.workers.audit import add_purge_job
from pubmed_bot.workers.subscriptions import SubscriptionWorker, build_scheduler


async def run_polling() -> None:
    """Поднять polling. Пустые ключи Settings уже отвергает."""
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine_from_path(settings.sqlite_path)
    session_maker = session_factory(engine)
    pubmed = NcbiEutilsClient(settings, TokenBucket(settings.ncbi_max_rps))
    translation = TranslationService(
        DeeplTranslator(settings),
        GeminiQueryRewriter(settings),
        session_maker,
        settings.deepl_min_chars_remaining,
    )
    search = SearchService(pubmed, translation, session_maker)
    article = ArticleService(pubmed, translation, session_maker)
    favorites = FavoritesService(pubmed, translation, session_maker)
    notes = NotesService(pubmed, translation, session_maker)
    subscriptions = SubscriptionsService(pubmed, translation, session_maker)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = create_dispatcher(
        settings,
        session_maker,
        search,
        article,
        favorites,
        notes,
        subscriptions,
    )
    worker = SubscriptionWorker(subscriptions, bot)
    scheduler = build_scheduler(settings.subscription_hour_utc, worker)
    add_purge_job(scheduler, settings.subscription_hour_utc, session_maker)
    scheduler.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await pubmed.aclose()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run_polling())


if __name__ == "__main__":  # pragma: no cover
    main()
