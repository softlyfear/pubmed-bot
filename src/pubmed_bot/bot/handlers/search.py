"""Поиск: «Найти» → запрос → список из 10, пагинация."""

import logging
import time
from contextlib import AbstractAsyncContextManager, nullcontext, suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from pubmed_bot.bot.formatting import format_list
from pubmed_bot.bot.keyboards import (
    CALLBACK_MAIN,
    CALLBACK_MORE,
    FIND_CALLBACKS,
    list_keyboard,
    start_keyboard,
)
from pubmed_bot.bot.middlewares import INTERRUPTED_KEY
from pubmed_bot.bot.states import NoteStates, SearchStates
from pubmed_bot.bot.texts import (
    ASK_QUERY,
    EMPTY_RESULT,
    NEED_FIND,
    NO_MORE,
    PUBMED_DOWN,
    QUERY_TRANSLATE_FAILED,
    SEARCH_PROGRESS,
    START_TEXT,
)
from pubmed_bot.domain.exceptions import PubmedUnavailable, TranslationUnavailable
from pubmed_bot.services.search import SearchPage, SearchService

logger = logging.getLogger(__name__)
router = Router(name="search")


@router.callback_query(F.data.in_(FIND_CALLBACKS))
async def on_find(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_query)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(ASK_QUERY)
        return
    bot = callback.bot
    if bot is None or callback.from_user is None:
        return
    await bot.send_message(callback.from_user.id, ASK_QUERY)


@router.message(SearchStates.waiting_query, F.text, ~F.text.startswith("/"))
async def on_query(
    message: Message,
    state: FSMContext,
    search_service: SearchService,
) -> None:
    query = (message.text or "").strip()
    if not query:
        await message.answer(ASK_QUERY)
        return
    if query.startswith("/"):
        return
    if message.from_user is None:
        return
    status = await message.answer(SEARCH_PROGRESS)
    started = time.monotonic()
    try:
        async with _typing(message.bot, message.chat.id):
            page = await search_service.run(message.from_user.id, query, page=1)
    except TranslationUnavailable:
        logger.warning(
            "перевод запроса недоступен user_id=%s",
            message.from_user.id,
        )
        status = await _status_at_bottom(message, state, status)
        await status.edit_text(QUERY_TRANSLATE_FAILED)
        return
    except PubmedUnavailable:
        logger.warning(
            "NCBI недоступен для user_id=%s",
            message.from_user.id if message.from_user else None,
        )
        status = await _status_at_bottom(message, state, status)
        await status.edit_text(PUBMED_DOWN)
        return
    logger.info(
        "поиск готов user_id=%s за %.1f с, статей=%s",
        message.from_user.id,
        time.monotonic() - started,
        len(page.items),
    )
    status = await _status_at_bottom(message, state, status)
    await _send_page(message, search_service, page, status=status)


@router.message(
    F.text,
    ~F.text.startswith("/"),
    ~StateFilter(NoteStates.waiting_body, SearchStates.waiting_query),
)
async def on_text_without_find(message: Message) -> None:
    await message.answer(NEED_FIND)


@router.callback_query(F.data == CALLBACK_MORE)
async def on_more(callback: CallbackQuery, search_service: SearchService) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    try:
        async with _typing(callback.bot, callback.from_user.id):
            page = await search_service.next_page(callback.from_user.id)
    except PubmedUnavailable:
        logger.warning(
            "NCBI недоступен на «ещё» user_id=%s",
            callback.from_user.id if callback.from_user else None,
        )
        await callback.answer(PUBMED_DOWN, show_alert=True)
        return
    if page is None:
        await callback.answer()
        return
    if page.no_more:
        await callback.answer(NO_MORE, show_alert=True)
        return
    await callback.answer()
    if page.empty:
        if callback.message and isinstance(callback.message, Message):
            await callback.message.answer(EMPTY_RESULT)
        return
    if callback.message and isinstance(callback.message, Message):
        await callback.message.edit_text(
            format_list(page.items),
            reply_markup=list_keyboard(
                tuple(item.pmid for item in page.items),
                has_more=page.has_more,
                with_subscribe=True,
                with_new_query=True,
            ),
        )
        await search_service.save_list_message(
            callback.from_user.id,
            callback.message.chat.id,
            callback.message.message_id,
        )


@router.callback_query(F.data == CALLBACK_MAIN)
async def on_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.clear()
    await callback.message.answer(
        START_TEXT,
        reply_markup=start_keyboard(),
    )


def _typing(bot: Bot | None, chat_id: int) -> AbstractAsyncContextManager[object]:
    """«печатает…» в шапке чата, пока идёт поиск."""
    if bot is None:
        return nullcontext()
    return ChatActionSender.typing(chat_id=chat_id, bot=bot)


async def _status_at_bottom(message: Message, state: FSMContext, status: Message) -> Message:
    """Пользователь писал во время поиска: статус ушёл вверх, ответ — новым сообщением."""
    data = await state.get_data()
    if data.get(INTERRUPTED_KEY) is not True:
        return status
    with suppress(TelegramBadRequest):
        await status.delete()
    return await message.answer(SEARCH_PROGRESS)


async def _send_page(
    message: Message,
    search_service: SearchService,
    page: SearchPage,
    *,
    status: Message,
) -> None:
    if page.empty or not page.items:
        await status.edit_text(
            EMPTY_RESULT,
            reply_markup=list_keyboard((), has_more=False, with_new_query=True),
        )
        return
    await status.edit_text(
        format_list(page.items),
        reply_markup=list_keyboard(
            tuple(item.pmid for item in page.items),
            has_more=page.has_more,
            with_subscribe=True,
            with_new_query=True,
        ),
    )
    if message.from_user is None:
        return
    await search_service.save_list_message(
        message.from_user.id,
        status.chat.id,
        status.message_id,
    )
