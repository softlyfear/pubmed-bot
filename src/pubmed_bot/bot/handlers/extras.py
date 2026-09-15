"""Избранное, заметки, экспорт. Без новых slash-команд."""

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from pubmed_bot.bot.formatting import format_list, format_notes_list
from pubmed_bot.bot.keyboards import (
    CALLBACK_EXPORT_PREFIX,
    CALLBACK_FAV,
    CALLBACK_FAV_ADD_PREFIX,
    CALLBACK_FAV_DEL_PREFIX,
    CALLBACK_NOTE_DEL_PREFIX,
    CALLBACK_NOTE_PREFIX,
    CALLBACK_NOTES,
    CALLBACK_OPEN_PREFIX,
    article_keyboard,
    favorites_list_keyboard,
    notes_list_keyboard,
)
from pubmed_bot.bot.states import NoteStates
from pubmed_bot.bot.texts import (
    ARTICLE_MISSING,
    FAV_ADDED,
    FAV_EMPTY,
    FAV_MISSING,
    FAV_NOT_SAVED,
    FAV_REMOVED,
    NOTE_DELETED,
    NOTE_EMPTY,
    NOTE_PROMPT,
    NOTE_PROMPT_EDIT,
    NOTE_SAVED,
    NOTE_TOO_LONG,
    NOTES_EMPTY,
    PUBMED_DOWN,
)
from pubmed_bot.domain.exceptions import NoteRejected, PubmedUnavailable
from pubmed_bot.services.article import ArticleService
from pubmed_bot.services.export import export_filename, render_export_txt
from pubmed_bot.services.favorites import FavoritesService
from pubmed_bot.services.notes import NotesService

logger = logging.getLogger(__name__)
router = Router(name="extras")


@router.callback_query(F.data == CALLBACK_FAV)
async def on_fav_list(callback: CallbackQuery, favorites_service: FavoritesService) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    items = await favorites_service.list_page(callback.from_user.id)
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    if not items:
        await callback.message.answer(FAV_EMPTY)
        return
    await callback.message.answer(
        format_list(items),
        reply_markup=favorites_list_keyboard(tuple(item.pmid for item in items)),
    )


@router.callback_query(F.data == CALLBACK_NOTES)
async def on_notes_list(callback: CallbackQuery, notes_service: NotesService) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    items = await notes_service.list_page(callback.from_user.id)
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    if not items:
        await callback.message.answer(NOTES_EMPTY)
        return
    await callback.message.answer(
        format_notes_list(items),
        reply_markup=notes_list_keyboard(tuple(item.pmid for item in items)),
    )


@router.callback_query(F.data.startswith(CALLBACK_FAV_ADD_PREFIX))
async def on_fav_add(callback: CallbackQuery, favorites_service: FavoritesService) -> None:
    pmid = (callback.data or "").removeprefix(CALLBACK_FAV_ADD_PREFIX).strip()
    if not pmid or callback.from_user is None:
        await callback.answer()
        return
    try:
        saved = await favorites_service.add(callback.from_user.id, pmid)
    except PubmedUnavailable:
        logger.warning("NCBI недоступен при избранном pmid=%s", pmid)
        await callback.answer(PUBMED_DOWN, show_alert=True)
        return
    if saved:
        await callback.answer(FAV_ADDED, show_alert=True)
        if callback.message is not None and isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(
                reply_markup=article_keyboard(pmid, is_favorite=True),
            )
        return
    await callback.answer(FAV_MISSING, show_alert=True)


@router.callback_query(F.data.startswith(CALLBACK_FAV_DEL_PREFIX))
async def on_fav_remove(callback: CallbackQuery, favorites_service: FavoritesService) -> None:
    pmid = (callback.data or "").removeprefix(CALLBACK_FAV_DEL_PREFIX).strip()
    if not pmid or callback.from_user is None:
        await callback.answer()
        return
    removed = await favorites_service.remove(callback.from_user.id, pmid)
    message = callback.message if isinstance(callback.message, Message) else None
    if message is not None and _is_favorites_list_markup(message.reply_markup):
        await callback.answer(FAV_REMOVED)
        items = await favorites_service.list_page(callback.from_user.id)
        if not items:
            await message.edit_text(
                FAV_EMPTY,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            )
            return
        await message.edit_text(
            format_list(items),
            reply_markup=favorites_list_keyboard(tuple(item.pmid for item in items)),
        )
        return
    toast = FAV_REMOVED if removed else FAV_NOT_SAVED
    await callback.answer(toast, show_alert=True)
    if message is not None:
        await message.edit_reply_markup(
            reply_markup=article_keyboard(pmid, is_favorite=False),
        )


def _is_favorites_list_markup(markup: object) -> bool:
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    has_open = False
    has_delete = False
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith(CALLBACK_OPEN_PREFIX):
                has_open = True
            if data.startswith(CALLBACK_FAV_DEL_PREFIX):
                has_delete = True
    return has_open and has_delete


@router.callback_query(F.data.startswith(CALLBACK_NOTE_PREFIX))
async def on_note_start(
    callback: CallbackQuery,
    state: FSMContext,
    notes_service: NotesService,
) -> None:
    pmid = (callback.data or "").removeprefix(CALLBACK_NOTE_PREFIX).strip()
    if not pmid or callback.from_user is None:
        await callback.answer()
        return
    existing = await notes_service.get(callback.from_user.id, pmid)
    await state.set_state(NoteStates.waiting_body)
    await state.update_data(note_pmid=pmid)
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    if existing:
        await callback.message.answer(f"{escape(existing)}\n\n{NOTE_PROMPT_EDIT}")
        return
    await callback.message.answer(NOTE_PROMPT)


@router.message(NoteStates.waiting_body, F.text)
async def on_note_body(
    message: Message,
    state: FSMContext,
    notes_service: NotesService,
) -> None:
    data = await state.get_data()
    pmid = data.get("note_pmid")
    if not pmid or message.from_user is None:
        await state.clear()
        return
    try:
        await notes_service.save(message.from_user.id, pmid, message.text or "")
    except NoteRejected as exc:
        if str(exc) == "too_long":
            await message.answer(NOTE_TOO_LONG)
            return
        await message.answer(NOTE_EMPTY)
        return
    await state.clear()
    await message.answer(NOTE_SAVED)


@router.callback_query(F.data.startswith(CALLBACK_NOTE_DEL_PREFIX))
async def on_note_remove(callback: CallbackQuery, notes_service: NotesService) -> None:
    pmid = (callback.data or "").removeprefix(CALLBACK_NOTE_DEL_PREFIX).strip()
    if not pmid or callback.from_user is None:
        await callback.answer()
        return
    await notes_service.delete(callback.from_user.id, pmid)
    message = callback.message if isinstance(callback.message, Message) else None
    await callback.answer(NOTE_DELETED)
    if message is not None:
        items = await notes_service.list_page(callback.from_user.id)
        if not items:
            await message.edit_text(
                NOTES_EMPTY,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            )
            return
        await message.edit_text(
            format_notes_list(items),
            reply_markup=notes_list_keyboard(tuple(item.pmid for item in items)),
        )


@router.callback_query(F.data.startswith(CALLBACK_EXPORT_PREFIX))
async def on_export(
    callback: CallbackQuery,
    article_service: ArticleService,
) -> None:
    pmid = (callback.data or "").removeprefix(CALLBACK_EXPORT_PREFIX).strip()
    if not pmid or callback.from_user is None:
        await callback.answer()
        return
    await callback.answer()
    if callback.message is None or not isinstance(callback.message, Message):
        return
    try:
        opened = await article_service.open(callback.from_user.id, pmid)
    except PubmedUnavailable:
        logger.warning("NCBI недоступен при экспорте pmid=%s", pmid)
        await callback.message.answer(PUBMED_DOWN)
        return
    if opened is None:
        await callback.message.answer(ARTICLE_MISSING)
        return
    payload = render_export_txt(opened).encode("utf-8")
    document = BufferedInputFile(payload, filename=export_filename(pmid))
    await callback.message.answer_document(document)
