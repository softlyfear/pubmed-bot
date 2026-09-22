"""Хендлер: без «Найти» NCBI не вызывается."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters.logic import _InvertFilter
from aiogram.filters.state import StateFilter
from aiogram.types import InlineKeyboardMarkup

from pubmed_bot.bot.factory import create_dispatcher
from pubmed_bot.bot.handlers.search import on_find, on_query, on_text_without_find, router
from pubmed_bot.bot.keyboards import CALLBACK_FIND, FIND_CALLBACKS, list_keyboard, start_keyboard
from pubmed_bot.bot.states import NoteStates, SearchStates
from pubmed_bot.bot.texts import (
    ASK_QUERY,
    BTN_NEW_QUERY,
    DISCLAIMER,
    EMPTY_RESULT,
    NEED_FIND,
    QUERY_TRANSLATE_FAILED,
    SEARCH_PROGRESS,
    START_TEXT,
)


def test_search_module_imports() -> None:
    import pubmed_bot.bot.handlers.search as search_mod

    assert search_mod.router is not None
    assert create_dispatcher is not None


def test_start_has_disclaimer_and_find_hint() -> None:
    assert "не медицинская рекомендация" in START_TEXT.lower() or DISCLAIMER in START_TEXT
    assert "Найти" in START_TEXT
    markup = start_keyboard()
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert texts == ["Найти", "Избранное", "Заметки", "Подписки"]
    assert data == [CALLBACK_FIND, "m:fav", "m:notes", "m:sub"]
    assert CALLBACK_FIND in FIND_CALLBACKS
    assert "d:sport" in FIND_CALLBACKS


@pytest.mark.asyncio
async def test_text_without_find_does_not_search() -> None:
    message = AsyncMock()
    message.text = "knee pain"
    await on_text_without_find(message)
    message.answer.assert_awaited_once_with(NEED_FIND)


@pytest.mark.asyncio
async def test_cyrillic_mt_failure_tells_user() -> None:
    from pubmed_bot.domain.exceptions import TranslationUnavailable

    message = AsyncMock()
    message.text = "Ягодицы"
    message.from_user.id = 1
    status = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    state = AsyncMock()
    search = AsyncMock()
    search.run = AsyncMock(side_effect=TranslationUnavailable("перевод недоступен"))
    await on_query(message, state, search)
    message.answer.assert_awaited_once_with(SEARCH_PROGRESS)
    status.edit_text.assert_awaited_once_with(QUERY_TRANSLATE_FAILED)
    search.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_in_waiting_query_does_not_search() -> None:
    message = AsyncMock()
    message.text = "/help"
    message.from_user.id = 1
    state = AsyncMock()
    search = AsyncMock()
    await on_query(message, state, search)
    search.run.assert_not_awaited()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_query_in_waiting_query_calls_run() -> None:
    from pubmed_bot.services.search import SearchPage

    message = AsyncMock()
    message.text = "knee"
    message.from_user.id = 1
    status = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    state = AsyncMock()
    search = AsyncMock()
    search.run = AsyncMock(
        return_value=SearchPage(
            items=(),
            page=1,
            has_more=False,
            empty=True,
            no_more=True,
        )
    )
    await on_query(message, state, search)
    search.run.assert_awaited_once_with(1, "knee", page=1)
    message.answer.assert_awaited_once_with(SEARCH_PROGRESS)
    status.edit_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["m:find", "d:sport", "d:medicine", "d:other"])
async def test_find_non_message_still_asks_query(data: str) -> None:
    callback = MagicMock()
    callback.data = data
    callback.message = MagicMock()
    callback.from_user.id = 99
    callback.answer = AsyncMock()
    callback.bot.send_message = AsyncMock()
    state = AsyncMock()
    await on_find(callback, state)
    state.set_state.assert_awaited_once_with(SearchStates.waiting_query)
    callback.bot.send_message.assert_awaited_once_with(99, ASK_QUERY)


@pytest.mark.asyncio
async def test_find_non_message_without_bot_skips_ask() -> None:
    callback = MagicMock()
    callback.data = "m:find"
    callback.message = MagicMock()
    callback.from_user.id = 99
    callback.answer = AsyncMock()
    callback.bot = None
    state = AsyncMock()
    await on_find(callback, state)
    state.set_state.assert_awaited_once_with(SearchStates.waiting_query)


def test_without_find_filter_excludes_waiting_query() -> None:
    catchall = next(h for h in router.message.handlers if h.callback is on_text_without_find)
    catchall_filters = catchall.filters
    assert catchall_filters is not None
    inverted: list[object] = []
    for filter_obj in catchall_filters:
        callback = filter_obj.callback
        if isinstance(callback, _InvertFilter):
            inner = callback.target.callback
            if isinstance(inner, StateFilter):
                inverted.extend(inner.states)
    assert SearchStates.waiting_query in inverted
    assert NoteStates.waiting_body in inverted
    query_handler = next(h for h in router.message.handlers if h.callback is on_query)
    query_filters = query_handler.filters
    assert query_filters is not None
    assert any(item.callback is SearchStates.waiting_query for item in query_filters)


@pytest.mark.asyncio
async def test_query_sends_progress_then_edits_list() -> None:
    from pubmed_bot.domain.models import ArticleListItem
    from pubmed_bot.services.search import SearchPage

    order: list[str] = []
    status = AsyncMock()
    status.chat.id = 7
    status.message_id = 42
    edited_html: list[str] = []

    async def answer(text: str, **kwargs: object) -> AsyncMock:
        order.append(f"answer:{text}")
        return status

    async def edit_text(text: str, **kwargs: object) -> None:
        order.append("edit")
        edited_html.append(text)
        markup = kwargs.get("reply_markup")
        assert isinstance(markup, InlineKeyboardMarkup)
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert BTN_NEW_QUERY in labels
        assert CALLBACK_FIND in data
        assert "Подписка на этот запрос" in labels

    async def run(*args: object, **kwargs: object) -> SearchPage:
        order.append("run")
        return SearchPage(
            items=(ArticleListItem(pmid="1", title_en="Hamstring"),),
            page=1,
            has_more=False,
            empty=False,
            no_more=True,
        )

    message = AsyncMock()
    message.text = "рост ягодиц"
    message.from_user.id = 7
    message.answer = answer
    status.edit_text = edit_text
    state = AsyncMock()
    search = AsyncMock()
    search.run = run
    search.save_list_message = AsyncMock()
    await on_query(message, state, search)
    assert order == [f"answer:{SEARCH_PROGRESS}", "run", "edit"]
    assert edited_html
    assert SEARCH_PROGRESS not in edited_html[0]
    search.save_list_message.assert_awaited_once_with(7, 7, 42)


def test_search_list_keyboard_has_new_query_on_own_row() -> None:
    markup = list_keyboard(
        ("100", "200"),
        has_more=True,
        with_subscribe=True,
        with_new_query=True,
    )
    rows = markup.inline_keyboard
    assert [btn.text for btn in rows[0]] == ["1", "2"]
    assert [btn.callback_data for btn in rows[0]] == ["o:100", "o:200"]
    assert [btn.text for btn in rows[1]] == ["Ещё", "Подписка на этот запрос"]
    assert [btn.text for btn in rows[2]] == [BTN_NEW_QUERY, "Главное меню"]
    assert [btn.callback_data for btn in rows[2]] == [CALLBACK_FIND, "m:main"]


def test_fav_and_worker_list_keyboard_omit_new_query() -> None:
    markup = list_keyboard(("1",), has_more=False)
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert BTN_NEW_QUERY not in labels
    assert CALLBACK_FIND not in data
    subscribed = list_keyboard(("1",), has_more=True, with_subscribe=True)
    labels = [btn.text for row in subscribed.inline_keyboard for btn in row]
    assert BTN_NEW_QUERY not in labels


@pytest.mark.asyncio
async def test_empty_run_attaches_new_query_button() -> None:
    from pubmed_bot.services.search import SearchPage

    status = AsyncMock()
    captured: dict[str, object] = {}

    async def answer(text: str, **kwargs: object) -> AsyncMock:
        return status

    async def edit_text(text: str, **kwargs: object) -> None:
        captured["text"] = text
        captured["markup"] = kwargs.get("reply_markup")

    message = AsyncMock()
    message.text = "zzzz"
    message.from_user.id = 1
    message.answer = answer
    status.edit_text = edit_text
    search = AsyncMock()
    search.run = AsyncMock(
        return_value=SearchPage(
            items=(),
            page=1,
            has_more=False,
            empty=True,
            no_more=False,
        )
    )
    await on_query(message, AsyncMock(), search)
    assert captured["text"] == EMPTY_RESULT
    markup = captured["markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [[btn.text for btn in row] for row in markup.inline_keyboard] == [
        [BTN_NEW_QUERY, "Главное меню"]
    ]
    assert markup.inline_keyboard[0][0].callback_data == CALLBACK_FIND


def test_handlers_do_not_import_openai_or_deepl() -> None:
    from pathlib import Path

    root = Path("src/pubmed_bot/bot/handlers")
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import openai" not in source
        assert "from openai" not in source
        assert "import deepl" not in source
        assert "from deepl" not in source
