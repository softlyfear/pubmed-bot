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
    await callback.message.answer(
        _format_subs(items),
        reply_markup=subscriptions_keyboard(tuple(item.id for item in items)),
    )


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
    await callback.message.answer(
        _format_subs(items),
        reply_markup=subscriptions_keyboard(tuple(item.id for item in items)),
    )


def _format_subs(items: tuple[SubscriptionView, ...]) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {escape(item.query_text)}")
    return "\n".join(lines)
