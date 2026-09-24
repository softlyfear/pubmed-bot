"""Middleware: только private chat, upsert user, per-user лимиты."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, override

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pubmed_bot.adapters.db.repositories import UserRepo
from pubmed_bot.adapters.db.session import session_scope
from pubmed_bot.bot.keyboards import (
    CALLBACK_EXPORT_PREFIX,
    CALLBACK_FAV_ADD_PREFIX,
    CALLBACK_MORE,
    CALLBACK_OPEN_PREFIX,
)
from pubmed_bot.bot.states import NoteStates
from pubmed_bot.services.rate_limit import PerUserWindow

RATE_LIMIT_TEXT = "Слишком много запросов. Подождите минуту."
PROCESSING_TEXT = "Подождите, запрос выполняется…"


def _chat_type(event: TelegramObject) -> str | None:
    if isinstance(event, Message):
        return event.chat.type
    if isinstance(event, CallbackQuery) and event.message is not None:
        return event.message.chat.type
    return None


def _telegram_user(event: TelegramObject):
    if isinstance(event, Message | CallbackQuery):
        return event.from_user
    return None


def _rate_kind(event: TelegramObject, fsm_state: str | None = None) -> str | None:
    if isinstance(event, Message):
        text = event.text or event.caption or ""
        if text.startswith("/"):
            return None
        if fsm_state == NoteStates.waiting_body.state:
            return None
        if text:
            return "search"
        return None
    if isinstance(event, CallbackQuery):
        data = event.data or ""
        if data.startswith((CALLBACK_OPEN_PREFIX, CALLBACK_EXPORT_PREFIX, CALLBACK_FAV_ADD_PREFIX)):
            return "open"
        if data == CALLBACK_MORE:
            return "search"
        return None
    return None


class PrivateChatMiddleware(BaseMiddleware):
    """Группы и каналы игнорируются, ответа нет."""

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if _chat_type(event) != ChatType.PRIVATE:
            return None
        return await handler(event, data)


class UserUpsertMiddleware(BaseMiddleware):
    """Первый апдейт создаёт пользователя, дальше обновляет last_seen_at."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _telegram_user(event)
        if user is None or user.is_bot:
            return None
        async with session_scope(self._session_maker) as session:
            await UserRepo(session).upsert(
                user.id,
                username=user.username,
                first_name=user.first_name,
            )
        return await handler(event, data)


class UserRateLimitMiddleware(BaseMiddleware):
    """Лимиты поиска и открытия; при превышении NCBI не вызывается."""

    def __init__(self, search: PerUserWindow, open_article: PerUserWindow) -> None:
        self._search = search
        self._open = open_article

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        fsm_state: str | None = None
        context = data.get("state")
        if context is not None:
            fsm_state = await context.get_state()
        kind = _rate_kind(event, fsm_state)
        user = _telegram_user(event)
        if kind is None or user is None:
            return await handler(event, data)
        window = self._search if kind == "search" else self._open
        if not window.allow(user.id):
            await _notify_limit(event)
            return None
        return await handler(event, data)


def _guarded(event: TelegramObject) -> bool:
    """Кнопки и текст (не команды) ждут окончания текущего запроса."""
    if isinstance(event, CallbackQuery):
        return True
    if isinstance(event, Message):
        text = event.text or ""
        return bool(text) and not text.startswith("/")
    return False


class ProcessingBlockerMiddleware(BaseMiddleware):
    """Блокирует кнопки и новые запросы, пока выполняется предыдущий."""

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not _guarded(event):
            return await handler(event, data)
        context = data.get("state")
        if context is None:
            return await handler(event, data)
        state_data = await context.get_data()
        if state_data.get("processing"):
            await _notify_processing(event)
            return None
        try:
            await context.update_data(processing=True)
            return await handler(event, data)
        finally:
            await context.update_data(processing=False)


async def _notify_processing(event: TelegramObject) -> None:
    if isinstance(event, Message):
        await event.answer(PROCESSING_TEXT)
        return
    if isinstance(event, CallbackQuery):
        await event.answer(PROCESSING_TEXT, show_alert=True)


async def _notify_limit(event: TelegramObject) -> None:
    if isinstance(event, Message):
        await event.answer(RATE_LIMIT_TEXT)
        return
    if isinstance(event, CallbackQuery):
        await event.answer(RATE_LIMIT_TEXT, show_alert=True)
