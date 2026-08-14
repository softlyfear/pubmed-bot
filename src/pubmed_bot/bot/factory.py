"""Сборка Dispatcher: Memory FSM, middleware, поиск, карточка, extras."""

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.bot.handlers.article import router as article_router
from pubmed_bot.bot.handlers.extras import router as extras_router
from pubmed_bot.bot.handlers.search import router as search_router
from pubmed_bot.bot.handlers.start import router as start_router
from pubmed_bot.bot.handlers.subscriptions import router as subscriptions_router
from pubmed_bot.bot.middlewares import (
    PrivateChatMiddleware,
    ProcessingBlockerMiddleware,
    UserRateLimitMiddleware,
    UserUpsertMiddleware,
)
from pubmed_bot.config import Settings
from pubmed_bot.services.article import ArticleService
from pubmed_bot.services.favorites import FavoritesService
from pubmed_bot.services.notes import NotesService
from pubmed_bot.services.rate_limit import PerUserWindow
from pubmed_bot.services.search import SearchService
from pubmed_bot.services.subscriptions import SubscriptionsService


def create_dispatcher(
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
    search_service: SearchService,
    article_service: ArticleService,
    favorites_service: FavoritesService,
    notes_service: NotesService,
    subscriptions_service: SubscriptionsService,
) -> Dispatcher:
    """Dispatcher с private-only, upsert, лимитами, поиском и карточкой."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["search_service"] = search_service
    dispatcher["article_service"] = article_service
    dispatcher["favorites_service"] = favorites_service
    dispatcher["notes_service"] = notes_service
    dispatcher["subscriptions_service"] = subscriptions_service
    dispatcher.message.outer_middleware(PrivateChatMiddleware())
    dispatcher.callback_query.outer_middleware(PrivateChatMiddleware())
    upsert = UserUpsertMiddleware(session_maker)
    dispatcher.message.outer_middleware(upsert)
    dispatcher.callback_query.outer_middleware(upsert)
    dispatcher.callback_query.outer_middleware(ProcessingBlockerMiddleware())
    rate = UserRateLimitMiddleware(
        PerUserWindow(settings.user_search_per_min),
        PerUserWindow(settings.user_open_per_min),
    )
    dispatcher.message.middleware(rate)
    dispatcher.callback_query.middleware(rate)
    dispatcher.include_router(start_router)
    dispatcher.include_router(article_router)
    dispatcher.include_router(extras_router)
    dispatcher.include_router(subscriptions_router)
    dispatcher.include_router(search_router)
    return dispatcher
