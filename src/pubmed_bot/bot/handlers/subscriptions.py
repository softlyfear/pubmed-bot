"""Подписки: с текущего поиска, список, отключение."""

import logging
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from pubmed_bot.bot.keyboards import (
    CALLBACK_SUB,
    CALLBACK_SUB_ADD,
    CALLBACK_SUB_OFF_PREFIX,
    subscriptions_keyboard,
)
from pubmed_bot.bot.texts import (
    SUB_DISABLED,
    SUB_EMPTY,
    SUB_NO_SEARCH,
    SUB_SAVED,
)
from pubmed_bot.services.subscriptions import SubscriptionsService, SubscriptionView

logger = logging.getLogger(__name__)
router = Router(name="subscriptions")
_SUB_TEXT_LIMIT = 4000
_QUERY_CLIP = 180


@router.callback_query(F.data == CALLBACK_SUB_ADD)
async def on_subscribe(
    callback: CallbackQuery,
    subscriptions_service: SubscriptionsService,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    saved = await subscriptions_service.subscribe_current(callback.from_user.id)
    if saved:
        await callback.answer(SUB_SAVED, show_alert=True)
        return
    await callback.answer(SUB_NO_SEARCH, show_alert=True)


@router.callback_query(F.data == CALLBACK_SUB)
async def on_sub_list(
    callback: CallbackQuery,
    subscriptions_service: SubscriptionsService,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    items = await subscriptions_service.list_for_user(callback.from_user.id)
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    if not items:
        await callback.message.answer(SUB_EMPTY)
        return
    await _send_sub_pages(callback.message, items)


@router.callback_query(F.data.startswith(CALLBACK_SUB_OFF_PREFIX))
async def on_sub_off(
    callback: CallbackQuery,
    subscriptions_service: SubscriptionsService,
) -> None:
    raw = (callback.data or "").removeprefix(CALLBACK_SUB_OFF_PREFIX).strip()
    if not raw.isdigit() or callback.from_user is None:
        await callback.answer()
        return
    disabled = await subscriptions_service.deactivate(callback.from_user.id, int(raw))
    if not disabled:
        await callback.answer()
        return
    await callback.answer(SUB_DISABLED, show_alert=True)
    items = await subscriptions_service.list_for_user(callback.from_user.id)
    if callback.message is None or not isinstance(callback.message, Message):
        return
    if not items:
        await callback.message.answer(SUB_EMPTY)
        return
    await _send_sub_pages(callback.message, items)


def subscription_pages(
    items: tuple[SubscriptionView, ...],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Split the list so each Telegram message stays under the HTML limit."""
    pages: list[tuple[str, tuple[int, ...]]] = []
    lines: list[str] = []
    ids: list[int] = []
    length = 0
    local = 0
    for item in items:
        query = " ".join(item.query_text.split())
        if len(query) > _QUERY_CLIP:
            query = f"{query[: _QUERY_CLIP - 1]}…"
        line = f"{local + 1}. {escape(query)}"
        extra = len(line) if not lines else len(line) + 1
        if lines and length + extra > _SUB_TEXT_LIMIT:
            pages.append(("\n".join(lines), tuple(ids)))
            lines = []
            ids = []
            length = 0
            local = 0
            line = f"1. {escape(query)}"
            extra = len(line)
        lines.append(line)
        ids.append(item.id)
        length += extra
        local += 1
    if lines:
        pages.append(("\n".join(lines), tuple(ids)))
    return tuple(pages)


async def _send_sub_pages(message: Message, items: tuple[SubscriptionView, ...]) -> None:
    for text, ids in subscription_pages(items):
        await message.answer(text, reply_markup=subscriptions_keyboard(ids))
