"""Карточка статьи: новые сообщения, назад к списку, просмотрено."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from pubmed_bot.bot.formatting import format_article_messages, format_list
from pubmed_bot.bot.keyboards import (
    CALLBACK_BACK,
    CALLBACK_OPEN_PREFIX,
    article_keyboard,
    list_keyboard,
    start_keyboard,
)
from pubmed_bot.bot.texts import ARTICLE_MISSING, LIST_GONE, OPEN_PROGRESS, PUBMED_DOWN
from pubmed_bot.domain.exceptions import PubmedUnavailable
from pubmed_bot.services.article import ArticleService
from pubmed_bot.services.export import export_filename, render_export_txt
from pubmed_bot.services.favorites import FavoritesService
from pubmed_bot.services.search import SearchService

logger = logging.getLogger(__name__)
router = Router(name="article")


@router.callback_query(F.data.startswith(CALLBACK_OPEN_PREFIX))
async def on_open(
    callback: CallbackQuery,
    article_service: ArticleService,
    favorites_service: FavoritesService,
) -> None:
    pmid = (callback.data or "").removeprefix(CALLBACK_OPEN_PREFIX).strip()
    if not pmid or callback.from_user is None:
        await callback.answer()
        return
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    status = await callback.message.answer(OPEN_PROGRESS)
    try:
        opened = await article_service.open(callback.from_user.id, pmid)
    except PubmedUnavailable:
        logger.warning("NCBI недоступен при открытии pmid=%s", pmid)
        await status.edit_text(PUBMED_DOWN, reply_markup=article_keyboard())
        return
    if opened is None:
        await status.edit_text(ARTICLE_MISSING, reply_markup=article_keyboard())
        return
    chunks = format_article_messages(opened)
    if not chunks:
        await status.edit_text(ARTICLE_MISSING, reply_markup=article_keyboard())
        return
    is_favorite = await favorites_service.exists(callback.from_user.id, pmid)
    markup = article_keyboard(pmid, is_favorite=is_favorite)
    first, *rest = chunks
    if not rest:
        await status.edit_text(first, reply_markup=markup)
    else:
        await status.edit_text(first)
        last_index = len(rest) - 1
        for index, chunk in enumerate(rest):
            chunk_markup = markup if index == last_index else None
            await callback.message.answer(chunk, reply_markup=chunk_markup)
    if opened.has_oa and opened.body_paragraphs():
        payload = render_export_txt(opened).encode("utf-8")
        document = BufferedInputFile(payload, filename=export_filename(pmid))
        await callback.message.answer_document(document)


@router.callback_query(F.data == CALLBACK_BACK)
async def on_back(
    callback: CallbackQuery,
    search_service: SearchService,
    state: FSMContext,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    page = await search_service.current_list(callback.from_user.id)
    await callback.answer()
    if page is None or not page.items:
        await state.clear()
        if callback.message and isinstance(callback.message, Message):
            await callback.message.answer(LIST_GONE, reply_markup=start_keyboard())
        return
    html = format_list(page.items)
    markup = list_keyboard(
        tuple(item.pmid for item in page.items),
        has_more=page.has_more,
        with_subscribe=True,
        with_new_query=True,
    )
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(html, reply_markup=markup)
