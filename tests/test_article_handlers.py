"""Хендлер карточки: новые сообщения, без protect_content."""

from datetime import UTC, datetime
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

from pubmed_bot.bot.handlers.article import on_back, on_open
from pubmed_bot.bot.keyboards import article_keyboard, start_keyboard
from pubmed_bot.bot.texts import (
    ARTICLE_MISSING,
    BTN_FAV_ADD,
    BTN_FAV_REMOVE,
    FULLTEXT_IN_FILE,
    FULLTEXT_UNAVAILABLE,
    LIST_GONE,
    OPEN_PROGRESS,
)
from pubmed_bot.domain.models import AbstractSection, Article, ArticleListItem
from pubmed_bot.services.article import OpenedArticle
from pubmed_bot.services.export import render_export_txt
from pubmed_bot.services.search import SearchPage


def _favorites(*, is_favorite: bool = False) -> AsyncMock:
    service = AsyncMock()
    service.exists = AsyncMock(return_value=is_favorite)
    return service


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
async def test_open_sends_new_messages_without_protect(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    edited: list[tuple[str, dict[str, object]]] = []

    async def fake_answer(self, text, **kwargs):
        sent.append((text, kwargs))
        return self

    async def fake_edit(self, text, **kwargs):
        edited.append((text, kwargs))
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    opened = OpenedArticle(
        article=Article(
            pmid="2001",
            title_en="Title",
            abstract=(AbstractSection(text="Abs", label="BACKGROUND"),),
        ),
        title_ru="Заголовок",
        abstract_ru=(AbstractSection(text="Абс", label="BACKGROUND"),),
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    article_service = AsyncMock()
    article_service.open = AsyncMock(return_value=opened)
    callback = _callback("o:2001", _message())
    await on_open(callback, article_service, _favorites())
    assert sent[0][0] == OPEN_PROGRESS
    article_service.open.assert_awaited_once_with(7, "2001")
    assert edited
    for _text, kwargs in edited:
        assert kwargs.get("protect_content") is not True
    markup = edited[-1][1].get("reply_markup")
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[1][0].text == BTN_FAV_ADD
    assert FULLTEXT_UNAVAILABLE in edited[0][0] or FULLTEXT_UNAVAILABLE in "".join(
        text for text, _kwargs in sent[1:]
    )


@pytest.mark.asyncio
async def test_missing_pmid_answers_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    edited: list[tuple[str, dict[str, object]]] = []

    async def fake_answer(self, text, **kwargs):
        sent.append((text, kwargs))
        return self

    async def fake_edit(self, text, **kwargs):
        edited.append((text, kwargs))
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    article_service = AsyncMock()
    article_service.open = AsyncMock(return_value=None)
    await on_open(_callback("o:999", _message()), article_service, _favorites())
    assert sent[0][0] == OPEN_PROGRESS
    assert edited[0][0] == ARTICLE_MISSING
    assert edited[0][1].get("protect_content") is not True


@pytest.mark.asyncio
async def test_open_progress_before_service_and_extra_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    sent: list[str] = []
    edited: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)
        order.append(f"answer:{text}")
        return self

    async def fake_edit(self, text, **kwargs):
        edited.append(text)
        order.append("edit")
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    cb_answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", cb_answer)
    opened = OpenedArticle(
        article=Article(pmid="2001", title_en="Title"),
        title_ru="Заголовок",
        abstract_ru=None,
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    article_service = AsyncMock()

    async def open_article(*args: object, **kwargs: object) -> OpenedArticle:
        order.append("open")
        return opened

    article_service.open = open_article
    monkeypatch.setattr(
        "pubmed_bot.bot.handlers.article.format_article_messages",
        lambda _opened: ("chunk-1", "chunk-2"),
    )
    await on_open(_callback("o:2001", _message()), article_service, _favorites())
    assert cb_answer.await_args is not None
    assert cb_answer.await_args.args == ()
    assert cb_answer.await_args.kwargs.get("text") is None
    assert order[0] == f"answer:{OPEN_PROGRESS}"
    assert "open" in order
    assert order.index("open") > order.index(f"answer:{OPEN_PROGRESS}")
    assert edited[0] == "chunk-1"
    assert sent[1] == "chunk-2"
    assert OPEN_PROGRESS not in edited


@pytest.mark.asyncio
async def test_back_shows_viewed_list(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    async def fake_answer(self, text, **kwargs):
        sent.append((text, kwargs))

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    search_service = AsyncMock()
    search_service.current_list = AsyncMock(
        return_value=SearchPage(
            items=(
                ArticleListItem(
                    pmid="2001",
                    title_en="Title",
                    title_ru="Заголовок",
                    viewed=True,
                ),
            ),
            page=1,
            has_more=False,
            empty=False,
            no_more=False,
        )
    )
    state = AsyncMock()
    await on_back(_callback("b:list", _message()), search_service, state)
    assert sent
    assert "просмотрено" in sent[0][0]
    assert sent[0][0] != LIST_GONE
    assert sent[0][1].get("protect_content") is not True
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_gone_shows_start_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    async def fake_answer(self, text, **kwargs):
        sent.append((text, kwargs))

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    search_service = AsyncMock()
    search_service.current_list = AsyncMock(return_value=None)
    state = AsyncMock()
    await on_back(_callback("b:list", _message()), search_service, state)
    assert sent[0][0] == LIST_GONE
    expected = {
        btn.callback_data
        for row in start_keyboard().inline_keyboard
        for btn in row
        if btn.callback_data
    }
    markup = sent[0][1]["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    got = {btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data}
    assert got == expected
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_back_empty_items_shows_start_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    async def fake_answer(self, text, **kwargs):
        sent.append((text, kwargs))

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    search_service = AsyncMock()
    search_service.current_list = AsyncMock(
        return_value=SearchPage(
            items=(),
            page=1,
            has_more=False,
            empty=True,
            no_more=True,
        )
    )
    state = AsyncMock()
    await on_back(_callback("b:list", _message()), search_service, state)
    assert sent[0][0] == LIST_GONE
    assert sent[0][1]["reply_markup"] is not None
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_oa_sends_one_export_document(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, object]]] = []
    edited: list[tuple[str, dict[str, object]]] = []
    documents: list[tuple[BufferedInputFile, dict[str, object]]] = []

    async def fake_answer(self, text, **kwargs):
        sent.append((text, kwargs))
        return self

    async def fake_edit(self, text, **kwargs):
        edited.append((text, kwargs))
        return self

    async def fake_document(self, document, **kwargs):
        documents.append((document, kwargs))

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    monkeypatch.setattr(Message, "answer_document", fake_document)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    opened = OpenedArticle(
        article=Article(
            pmid="2002",
            title_en="Title",
            abstract=(AbstractSection(text="Abs", label="BACKGROUND"),),
            fulltext_paragraphs=("OA body",),
        ),
        title_ru="Заголовок",
        abstract_ru=(AbstractSection(text="Абс", label="BACKGROUND"),),
        fulltext_ru=("Полный OA",),
        has_oa=True,
        translation_failed=False,
    )
    article_service = AsyncMock()
    article_service.open = AsyncMock(return_value=opened)
    await on_open(_callback("o:2002", _message()), article_service, _favorites())
    assert sent[0][0] == OPEN_PROGRESS
    assert FULLTEXT_IN_FILE in edited[0][0]
    assert "Полный OA" not in edited[0][0]
    assert "<b>Полный текст</b>" not in edited[0][0]
    assert len(documents) == 1
    document, kwargs = documents[0]
    assert kwargs.get("caption") is None
    assert kwargs.get("reply_markup") is None
    assert document.filename == "pubmed-2002.txt"
    text = document.data.decode("utf-8")
    assert text == render_export_txt(opened)
    assert "Полный OA" in text
    assert edited[-1][1].get("reply_markup") is not None


@pytest.mark.asyncio
async def test_open_without_oa_does_not_send_document(monkeypatch: pytest.MonkeyPatch) -> None:
    documents: list[object] = []

    async def fake_answer(self, text, **kwargs):
        return self

    async def fake_edit(self, text, **kwargs):
        return self

    async def fake_document(self, document, **kwargs):
        documents.append(document)

    monkeypatch.setattr(Message, "answer", fake_answer)
    monkeypatch.setattr(Message, "edit_text", fake_edit)
    monkeypatch.setattr(Message, "answer_document", fake_document)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    opened = OpenedArticle(
        article=Article(
            pmid="2001",
            title_en="Title",
            abstract=(AbstractSection(text="Abs"),),
        ),
        title_ru="Заголовок",
        abstract_ru=(AbstractSection(text="Абс"),),
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    article_service = AsyncMock()
    article_service.open = AsyncMock(return_value=opened)
    await on_open(_callback("o:2001", _message()), article_service, _favorites())
    assert documents == []


def test_article_keyboard_has_back() -> None:
    markup = article_keyboard()
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "Назад к списку" in texts


def test_article_keyboard_toggles_favorite_button() -> None:
    added = article_keyboard("2001", is_favorite=False)
    removed = article_keyboard("2001", is_favorite=True)
    add_btn = added.inline_keyboard[1][0]
    del_btn = removed.inline_keyboard[1][0]
    assert add_btn.text == BTN_FAV_ADD
    assert add_btn.callback_data == "f:2001"
    assert del_btn.text == BTN_FAV_REMOVE
    assert del_btn.callback_data == "fd:2001"
    assert [btn.text for btn in added.inline_keyboard[1][1:]] == ["Заметка", "Экспорт"]
    assert [btn.text for btn in removed.inline_keyboard[1][1:]] == ["Заметка", "Экспорт"]
