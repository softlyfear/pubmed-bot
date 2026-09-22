"""Хендлеры избранного, заметки и экспорта. Без сети."""

from datetime import UTC, datetime
from html import escape
from unittest.mock import AsyncMock

import pytest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    User,
)

from pubmed_bot.bot.handlers.extras import on_export, on_note_start
from pubmed_bot.bot.texts import NOTE_PROMPT_EDIT
from pubmed_bot.domain.models import Article
from pubmed_bot.services.article import OpenedArticle


def _tg_user() -> User:
    return User.model_validate({"id": 7, "is_bot": False, "first_name": "Ann"})


def _message() -> Message:
    user = _tg_user()
    return Message.model_validate(
        {
            "message_id": 10,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": user.id, "type": "private"}),
            "from": user,
            "text": "list",
        }
    )


def _callback(data: str, message: Message) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": "cb1",
            "from": _tg_user(),
            "chat_instance": "inst",
            "data": data,
            "message": message.model_dump(mode="python"),
        }
    )


@pytest.mark.asyncio
async def test_note_start_escapes_html(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    notes = AsyncMock()
    notes.get = AsyncMock(return_value="p<0.05 & B")
    state = AsyncMock()
    await on_note_start(_callback("n:2001", _message()), state, notes)
    assert sent
    assert escape("p<0.05 & B") in sent[0]
    assert "<0.05" not in sent[0]
    assert NOTE_PROMPT_EDIT in sent[0]
    state.set_state.assert_awaited()


@pytest.mark.asyncio
async def test_export_sends_utf8_document(monkeypatch: pytest.MonkeyPatch) -> None:
    documents: list[BufferedInputFile] = []

    async def fake_document(self, document, **kwargs):
        documents.append(document)

    monkeypatch.setattr(Message, "answer_document", fake_document)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    opened = OpenedArticle(
        article=Article(pmid="2001", title_en="Title"),
        title_ru="Заголовок",
        abstract_ru=(),
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    article_service = AsyncMock()
    article_service.open = AsyncMock(return_value=opened)
    await on_export(_callback("x:2001", _message()), article_service)
    assert documents
    payload = documents[0].data
    assert documents[0].filename == "pubmed-2001.txt"
    text = payload.decode("utf-8")
    assert "Заголовок" in text
    assert "secret" not in text
    article_service.open.assert_awaited_once_with(7, "2001")


@pytest.mark.asyncio
async def test_fav_add_edits_card_to_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubmed_bot.bot.handlers.extras import on_fav_add
    from pubmed_bot.bot.texts import BTN_FAV_REMOVE, FAV_ADDED

    edited: list[object] = []
    toasts: list[str] = []

    async def fake_edit(self: Message, **kwargs: object) -> None:
        edited.append(kwargs.get("reply_markup"))

    async def fake_cb_answer(self: CallbackQuery, text: str = "", **kwargs: object) -> None:
        toasts.append(text)

    monkeypatch.setattr(Message, "edit_reply_markup", fake_edit)
    monkeypatch.setattr(CallbackQuery, "answer", fake_cb_answer)
    favs = AsyncMock()
    favs.add = AsyncMock(return_value=True)
    await on_fav_add(_callback("f:2001", _message()), favs)
    assert toasts == [FAV_ADDED]
    markup = edited[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[1][0].text == BTN_FAV_REMOVE
    assert markup.inline_keyboard[1][0].callback_data == "fd:2001"
    favs.add.assert_awaited_once_with(7, "2001")


@pytest.mark.asyncio
async def test_fav_remove_from_card_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubmed_bot.bot.handlers.extras import on_fav_remove
    from pubmed_bot.bot.keyboards import favorites_list_keyboard
    from pubmed_bot.bot.texts import BTN_FAV_ADD, FAV_EMPTY, FAV_NOT_SAVED, FAV_REMOVED
    from pubmed_bot.domain.models import ArticleListItem

    edited_markup: list[object] = []
    edited_text: list[tuple[str, object]] = []
    toasts: list[str] = []

    async def fake_edit_markup(self: Message, **kwargs: object) -> None:
        edited_markup.append(kwargs.get("reply_markup"))

    async def fake_edit_text(self: Message, text: str, **kwargs: object) -> None:
        edited_text.append((text, kwargs.get("reply_markup")))

    async def fake_cb_answer(self: CallbackQuery, text: str = "", **kwargs: object) -> None:
        toasts.append(text)

    monkeypatch.setattr(Message, "edit_reply_markup", fake_edit_markup)
    monkeypatch.setattr(Message, "edit_text", fake_edit_text)
    monkeypatch.setattr(CallbackQuery, "answer", fake_cb_answer)

    favs = AsyncMock()
    favs.remove = AsyncMock(return_value=True)
    await on_fav_remove(_callback("fd:2001", _message()), favs)
    assert toasts == [FAV_REMOVED]
    card = edited_markup[0]
    assert isinstance(card, InlineKeyboardMarkup)
    assert card.inline_keyboard[1][0].text == BTN_FAV_ADD
    assert card.inline_keyboard[1][0].callback_data == "f:2001"

    favs.remove = AsyncMock(return_value=False)
    await on_fav_remove(_callback("fd:2001", _message()), favs)
    assert toasts[-1] == FAV_NOT_SAVED

    user = _tg_user()
    list_message = Message.model_validate(
        {
            "message_id": 10,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": user.id, "type": "private"}),
            "from": user,
            "text": "list",
            "reply_markup": favorites_list_keyboard(("2001", "2002")).model_dump(mode="python"),
        }
    )
    favs.remove = AsyncMock(return_value=True)
    favs.list_page = AsyncMock(return_value=(ArticleListItem(pmid="2002", title_en="Keep"),))
    await on_fav_remove(_callback("fd:2001", list_message), favs)
    assert toasts[-1] == FAV_REMOVED
    remaining = edited_text[0][1]
    assert isinstance(remaining, InlineKeyboardMarkup)
    labels = [btn.text for row in remaining.inline_keyboard for btn in row]
    data = [btn.callback_data for row in remaining.inline_keyboard for btn in row]
    assert "Удалить" in labels
    assert "o:2002" in data
    assert "fd:2002" in data
    assert "fd:2001" not in data
    favs.remove.assert_awaited_with(7, "2001")

    favs.list_page = AsyncMock(return_value=())
    await on_fav_remove(_callback("fd:2002", list_message), favs)
    assert edited_text[-1][0] == FAV_EMPTY
    empty_markup = edited_text[-1][1]
    assert isinstance(empty_markup, InlineKeyboardMarkup)
    assert empty_markup.inline_keyboard == []
    favs.add.assert_not_called()


def test_favorites_list_keyboard_has_remove_rows() -> None:
    from pubmed_bot.bot.keyboards import favorites_list_keyboard, list_keyboard

    markup = favorites_list_keyboard(("11", "12"))
    rows = markup.inline_keyboard
    assert [btn.text for btn in rows[0]] == ["1", "Удалить"]
    assert [btn.callback_data for btn in rows[0]] == ["o:11", "fd:11"]
    assert [btn.text for btn in rows[1]] == ["2", "Удалить"]
    assert [btn.callback_data for btn in rows[1]] == ["o:12", "fd:12"]
    search = list_keyboard(("11",), has_more=False)
    search_labels = [btn.text for row in search.inline_keyboard for btn in row]
    assert "убрать 1" not in search_labels
    assert "Новый запрос" not in search_labels


@pytest.mark.asyncio
async def test_notes_list_empty_and_items(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubmed_bot.bot.handlers.extras import on_notes_list
    from pubmed_bot.bot.texts import BTN_NOTES, NOTES_EMPTY
    from pubmed_bot.domain.models import NoteListItem

    sent: list[tuple[str, object]] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    notes = AsyncMock()
    notes.list_page = AsyncMock(return_value=())
    await on_notes_list(_callback("m:notes", _message()), notes)
    notes.list_page.assert_awaited_once_with(7)
    assert sent[0][0] == NOTES_EMPTY
    assert sent[0][1] is None

    notes.list_page = AsyncMock(
        return_value=(
            NoteListItem(pmid="11", title="Title <x>", preview="p" * 10),
            NoteListItem(pmid="12", title="PMID 12", preview="hello"),
        )
    )
    await on_notes_list(_callback("m:notes", _message()), notes)
    html, markup = sent[-1]
    assert "Title &lt;x&gt;" in html
    assert "<x>" not in html
    assert "hello" in html
    assert isinstance(markup, InlineKeyboardMarkup)
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert labels == ["1", "Править", "Удалить", "2", "Править", "Удалить", "Главное меню"]
    assert data == ["o:11", "n:11", "nd:11", "o:12", "n:12", "nd:12", "m:main"]
    assert "Новый запрос" not in labels
    from pubmed_bot.bot.keyboards import start_keyboard

    start_labels = [btn.text for row in start_keyboard().inline_keyboard for btn in row]
    start_data = [btn.callback_data for row in start_keyboard().inline_keyboard for btn in row]
    assert BTN_NOTES in start_labels
    assert "m:notes" in start_data
