"""Добор веток хендлеров для покрытия. Без сети."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from pubmed_bot.bot.handlers.article import on_back, on_open
from pubmed_bot.bot.handlers.extras import (
    on_export,
    on_fav_add,
    on_fav_list,
    on_fav_remove,
    on_note_body,
)
from pubmed_bot.bot.handlers.search import on_find, on_more, on_query
from pubmed_bot.bot.handlers.start import cmd_start
from pubmed_bot.bot.handlers.subscriptions import on_sub_list, on_sub_off, on_subscribe
from pubmed_bot.bot.keyboards import list_keyboard, start_keyboard, subscriptions_keyboard
from pubmed_bot.bot.states import SearchStates
from pubmed_bot.bot.texts import (
    ASK_QUERY,
    EMPTY_RESULT,
    FAV_EMPTY,
    NOTE_EMPTY,
    NOTE_SAVED,
    NOTE_TOO_LONG,
    OPEN_PROGRESS,
    PUBMED_DOWN,
    SEARCH_PROGRESS,
    START_TEXT,
    SUB_EMPTY,
)
from pubmed_bot.domain.exceptions import NoteRejected, PubmedUnavailable
from pubmed_bot.domain.models import ArticleListItem
from pubmed_bot.services.search import SearchPage
from pubmed_bot.services.subscriptions import SubscriptionView


def _tg_user() -> User:
    return User.model_validate({"id": 7, "is_bot": False, "first_name": "Ann"})


def _message(*, text: str = "list") -> Message:
    user = _tg_user()
    return Message.model_validate(
        {
            "message_id": 10,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": user.id, "type": "private"}),
            "from": user,
            "text": text,
        }
    )


def _callback(data: str, message: Message | None = None) -> CallbackQuery:
    payload: dict[str, object] = {
        "id": "cb1",
        "from": _tg_user(),
        "chat_instance": "inst",
        "data": data,
    }
    if message is not None:
        payload["message"] = message.model_dump(mode="python")
    return CallbackQuery.model_validate(payload)


@pytest.mark.asyncio
async def test_start_sends_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, object]] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(Message, "answer", fake_answer)
    state = AsyncMock()
    await cmd_start(_message(), state)
    assert sent[0][0] == START_TEXT
    assert sent[0][1] is not None
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_find_and_query_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    edited: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> Message:
        sent.append(text)
        return self

    async def fake_edit(self: Message, text: str, **kwargs: object) -> None:
        edited.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    state = AsyncMock()
    await on_find(_callback("m:find", _message()), state)
    state.set_state.assert_awaited()
    assert ASK_QUERY in sent
    search = AsyncMock()
    search.run = AsyncMock(
        return_value=SearchPage(
            items=(ArticleListItem(pmid="1", title_en="T", title_ru="З"),),
            page=1,
            has_more=True,
            empty=False,
            no_more=False,
        )
    )
    search.save_list_message = AsyncMock()
    await on_query(_message(text="glute"), state, search)
    search.run.assert_awaited()
    search.save_list_message.assert_awaited()
    assert SEARCH_PROGRESS in sent
    assert edited


@pytest.mark.asyncio
async def test_legacy_domain_is_find_and_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    state = AsyncMock()
    sent: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> Message:
        sent.append(text)
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_find(_callback("d:sport", _message()), state)
    state.set_state.assert_awaited_once_with(SearchStates.waiting_query)
    assert ASK_QUERY in sent
    ready = AsyncMock()
    await on_query(_message(text="   "), ready, AsyncMock())
    assert ASK_QUERY in sent


@pytest.mark.asyncio
async def test_query_pubmed_down(monkeypatch: pytest.MonkeyPatch) -> None:
    answered: list[str] = []
    edited: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> Message:
        answered.append(text)
        return self

    async def fake_edit(self: Message, text: str, **kwargs: object) -> None:
        edited.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    search = AsyncMock()
    search.run = AsyncMock(side_effect=PubmedUnavailable("down"))
    await on_query(_message(text="knee"), AsyncMock(), search)
    assert SEARCH_PROGRESS in answered
    assert PUBMED_DOWN in edited
    assert PUBMED_DOWN not in answered


@pytest.mark.asyncio
async def test_query_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    answered: list[str] = []
    edited: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> Message:
        answered.append(text)
        return self

    async def fake_edit(self: Message, text: str, **kwargs: object) -> None:
        edited.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"domain": "other"})
    search = AsyncMock()
    search.run = AsyncMock(
        return_value=SearchPage(items=(), page=1, has_more=False, empty=True, no_more=False)
    )
    await on_query(_message(text="zzzz"), state, search)
    assert SEARCH_PROGRESS in answered
    assert EMPTY_RESULT in edited
    assert EMPTY_RESULT not in answered


@pytest.mark.asyncio
async def test_more_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    edited: list[str] = []

    async def fake_edit(self: Message, text: str, **kwargs: object) -> None:
        edited.append(text)

    monkeypatch.setattr(Message, "edit_text", fake_edit)
    answered: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        answered.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    search = AsyncMock()
    search.next_page = AsyncMock(return_value=None)
    await on_more(_callback("p:next", _message()), search)
    search.next_page = AsyncMock(side_effect=PubmedUnavailable("down"))
    await on_more(_callback("p:next", _message()), search)
    search.next_page = AsyncMock(
        return_value=SearchPage(items=(), page=2, has_more=False, empty=False, no_more=True)
    )
    await on_more(_callback("p:next", _message()), search)
    search.next_page = AsyncMock(
        return_value=SearchPage(items=(), page=2, has_more=False, empty=True, no_more=False)
    )
    await on_more(_callback("p:next", _message()), search)
    search.next_page = AsyncMock(
        return_value=SearchPage(
            items=(ArticleListItem(pmid="2", title_en="T"),),
            page=2,
            has_more=False,
            empty=False,
            no_more=False,
        )
    )
    search.save_list_message = AsyncMock()
    await on_more(_callback("p:next", _message()), search)
    assert edited
    assert SEARCH_PROGRESS not in answered
    assert SEARCH_PROGRESS not in edited


@pytest.mark.asyncio
async def test_open_pubmed_down_and_back_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    answered: list[str] = []
    edited: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> Message:
        answered.append(text)
        return self

    async def fake_edit(self: Message, text: str, **kwargs: object) -> None:
        edited.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    article = AsyncMock()
    article.open = AsyncMock(side_effect=PubmedUnavailable("down"))
    await on_open(_callback("o:1", _message()), article, AsyncMock())
    assert OPEN_PROGRESS in answered
    assert PUBMED_DOWN in edited
    search = AsyncMock()
    search.current_list = AsyncMock(return_value=None)
    await on_back(_callback("b:list", _message()), search, AsyncMock())


@pytest.mark.asyncio
async def test_open_empty_pmid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    article = AsyncMock()
    await on_open(_callback("o:", _message()), article, AsyncMock())
    article.open.assert_not_called()


@pytest.mark.asyncio
async def test_fav_list_and_add(monkeypatch: pytest.MonkeyPatch) -> None:
    answered: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        answered.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_reply_markup", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    favs = AsyncMock()
    favs.list_page = AsyncMock(return_value=())
    await on_fav_list(_callback("m:fav", _message()), favs)
    assert FAV_EMPTY in answered
    favs.list_page = AsyncMock(
        return_value=(ArticleListItem(pmid="1", title_en="T", title_ru="З"),)
    )
    await on_fav_list(_callback("m:fav", _message()), favs)
    favs.add = AsyncMock(return_value=True)
    await on_fav_add(_callback("f:1", _message()), favs)
    favs.add = AsyncMock(return_value=False)
    await on_fav_add(_callback("f:1", _message()), favs)
    favs.add = AsyncMock(side_effect=PubmedUnavailable("down"))
    await on_fav_add(_callback("f:1", _message()), favs)
    await on_fav_add(_callback("f:", _message()), favs)
    await on_fav_remove(_callback("fd:", _message()), favs)


@pytest.mark.asyncio
async def test_note_body_and_export_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    answered: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        answered.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    notes = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"note_pmid": "1"})
    notes.save = AsyncMock(side_effect=NoteRejected("too_long"))
    await on_note_body(_message(text="x"), state, notes)
    assert NOTE_TOO_LONG in answered
    notes.save = AsyncMock(side_effect=NoteRejected("empty"))
    await on_note_body(_message(text="ok"), state, notes)
    assert NOTE_EMPTY in answered
    notes.save = AsyncMock()
    await on_note_body(_message(text="ok"), state, notes)
    assert NOTE_SAVED in answered
    state.get_data = AsyncMock(return_value={})
    await on_note_body(_message(text="x"), state, notes)
    article = AsyncMock()
    article.open = AsyncMock(side_effect=PubmedUnavailable("down"))
    await on_export(_callback("x:1", _message()), article)
    article.open = AsyncMock(return_value=None)
    await on_export(_callback("x:1", _message()), article)
    await on_export(_callback("x:", _message()), article)


@pytest.mark.asyncio
async def test_subscriptions_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    answered: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        answered.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    subs = AsyncMock()
    subs.subscribe_current = AsyncMock(return_value=True)
    await on_subscribe(_callback("s:add", _message()), subs)
    subs.subscribe_current = AsyncMock(return_value=False)
    await on_subscribe(_callback("s:add", _message()), subs)
    subs.list_for_user = AsyncMock(return_value=())
    await on_sub_list(_callback("m:sub", _message()), subs)
    assert SUB_EMPTY in answered
    item = SubscriptionView(
        id=3,
        telegram_user_id=7,
        query_text="knee",
        query_en="knee",
        last_checked_at=None,
        created_at=None,
    )
    subs.list_for_user = AsyncMock(return_value=(item,))
    await on_sub_list(_callback("m:sub", _message()), subs)
    subs.deactivate = AsyncMock(return_value=False)
    await on_sub_off(_callback("s:x:3", _message()), subs)
    subs.deactivate = AsyncMock(return_value=True)
    subs.list_for_user = AsyncMock(return_value=())
    await on_sub_off(_callback("s:x:3", _message()), subs)
    subs.list_for_user = AsyncMock(return_value=(item,))
    await on_sub_off(_callback("s:x:3", _message()), subs)
    await on_sub_off(_callback("s:x:nope", _message()), subs)


def test_keyboards_extra_rows() -> None:
    start = start_keyboard()
    labels = [btn.text for row in start.inline_keyboard for btn in row]
    assert labels == ["Найти", "Избранное", "Заметки", "Подписки"]
    markup = list_keyboard(("1", "2"), has_more=True, with_subscribe=True)
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "Ещё" in labels
    assert "Подписка на этот запрос" in labels
    assert "Новый запрос" not in labels
    search_list = list_keyboard(
        ("1", "2"),
        has_more=True,
        with_subscribe=True,
        with_new_query=True,
    )
    search_labels = [btn.text for row in search_list.inline_keyboard for btn in row]
    assert "Новый запрос" in search_labels
    off = subscriptions_keyboard((5,))
    assert off.inline_keyboard[0][0].text == "Отключить 1"
