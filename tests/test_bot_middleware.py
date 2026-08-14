"""Middleware: private chat, upsert user, per-user лимит. Без сети Telegram."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select

from pubmed_bot.adapters.db.models import User as DbUser
from pubmed_bot.adapters.db.session import create_engine_from_path, session_factory
from pubmed_bot.bot.middlewares import (
    RATE_LIMIT_TEXT,
    PrivateChatMiddleware,
    UserRateLimitMiddleware,
    UserUpsertMiddleware,
)
from pubmed_bot.bot.states import NoteStates
from pubmed_bot.config import Settings, get_settings
from pubmed_bot.services.rate_limit import PerUserWindow


def _tg_user(**kwargs) -> User:
    payload = {"id": 42, "is_bot": False, "first_name": "Ann", "username": "ann"}
    payload.update(kwargs)
    return User.model_validate(payload)


def _message(
    *, chat_type: str = "private", text: str = "knee pain", user: User | None = None
) -> Message:
    user = user or _tg_user()
    return Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(UTC),
            "chat": Chat.model_validate({"id": user.id, "type": chat_type}),
            "from": user,
            "text": text,
        }
    )


def _callback(*, data: str = "o:123", chat_type: str = "private") -> CallbackQuery:
    user = _tg_user()
    message = _message(chat_type=chat_type, text="list")
    return CallbackQuery.model_validate(
        {
            "id": "cb1",
            "from": user,
            "chat_instance": "inst",
            "data": data,
            "message": message.model_dump(mode="python"),
        }
    )


@pytest.fixture
def sqlite_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "pubmed.db"
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("NCBI_EMAIL", "dev@example.com")
    monkeypatch.setenv("DEEPL_AUTH_KEY", "d")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    return db_path


@pytest.mark.asyncio
async def test_group_message_is_dropped() -> None:
    called: list[object] = []

    async def handler(event, data):
        called.append(event)
        return "ok"

    result = await PrivateChatMiddleware()(handler, _message(chat_type="group"), {})
    assert result is None
    assert called == []


@pytest.mark.asyncio
async def test_private_message_reaches_handler() -> None:
    called: list[object] = []

    async def handler(event, data):
        called.append(event)
        return "ok"

    result = await PrivateChatMiddleware()(handler, _message(), {})
    assert result == "ok"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_upsert_creates_and_updates_user(sqlite_file: Path) -> None:
    engine = create_engine_from_path(sqlite_file)
    factory = session_factory(engine)
    middleware = UserUpsertMiddleware(factory)

    async def handler(event, data):
        return "ok"

    try:
        await middleware(handler, _message(), {})
        await middleware(handler, _message(user=_tg_user(first_name="Ann2")), {})
        async with factory() as session:
            rows = (await session.scalars(select(DbUser))).all()
            assert len(rows) == 1
            assert rows[0].telegram_user_id == 42
            assert rows[0].first_name == "Ann2"
            assert rows[0].last_seen_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_rate_limit_blocks_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[str] = []

    async def fake_answer(self, text, **kwargs):
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    called: list[int] = []

    async def handler(event, data):
        called.append(1)
        return "ok"

    middleware = UserRateLimitMiddleware(PerUserWindow(1), PerUserWindow(20))
    assert await middleware(handler, _message(text="one"), {}) == "ok"
    assert await middleware(handler, _message(text="two"), {}) is None
    assert called == [1]
    assert answers == [RATE_LIMIT_TEXT]


@pytest.mark.asyncio
async def test_open_rate_limit_on_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[str] = []

    async def fake_answer(self, text, **kwargs):
        answers.append(text)

    monkeypatch.setattr(CallbackQuery, "answer", fake_answer)
    called: list[int] = []

    async def handler(event, data):
        called.append(1)
        return "ok"

    middleware = UserRateLimitMiddleware(PerUserWindow(10), PerUserWindow(1))
    assert await middleware(handler, _callback(data="o:1"), {}) == "ok"
    assert await middleware(handler, _callback(data="o:2"), {}) is None
    assert called == [1]
    assert answers == [RATE_LIMIT_TEXT]


@pytest.mark.asyncio
async def test_start_command_does_not_count_as_search() -> None:
    called: list[int] = []

    async def handler(event, data):
        called.append(1)
        return "ok"

    middleware = UserRateLimitMiddleware(PerUserWindow(1), PerUserWindow(1))
    await middleware(handler, _message(text="/start"), {})
    await middleware(handler, _message(text="/help"), {})
    assert called == [1, 1]


@pytest.mark.asyncio
async def test_note_state_text_does_not_count_as_search() -> None:
    called: list[int] = []

    async def handler(event, data):
        called.append(1)
        return "ok"

    class FakeState:
        async def get_state(self) -> str:
            state = NoteStates.waiting_body.state
            assert state is not None
            return state

    middleware = UserRateLimitMiddleware(PerUserWindow(1), PerUserWindow(1))
    data = {"state": FakeState()}
    assert await middleware(handler, _message(text="p<0.05"), data) == "ok"
    assert await middleware(handler, _message(text="second note"), data) == "ok"
    assert called == [1, 1]


@pytest.mark.asyncio
async def test_export_and_fav_add_count_as_open(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[str] = []

    async def fake_answer(self, text, **kwargs):
        answers.append(text)

    monkeypatch.setattr(CallbackQuery, "answer", fake_answer)
    called: list[int] = []

    async def handler(event, data):
        called.append(1)
        return "ok"

    middleware = UserRateLimitMiddleware(PerUserWindow(10), PerUserWindow(1))
    assert await middleware(handler, _callback(data="x:1"), {}) == "ok"
    assert await middleware(handler, _callback(data="f:2"), {}) is None
    assert called == [1]
    assert answers == [RATE_LIMIT_TEXT]


@pytest.mark.asyncio
async def test_fav_delete_does_not_count_as_open() -> None:
    called: list[int] = []

    async def handler(event: object, data: object) -> str:
        called.append(1)
        return "ok"

    middleware = UserRateLimitMiddleware(PerUserWindow(10), PerUserWindow(1))
    assert await middleware(handler, _callback(data="o:1"), {}) == "ok"
    assert await middleware(handler, _callback(data="fd:2"), {}) == "ok"
    assert called == [1, 1]


def test_empty_api_keys_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            bot_token="  ",
            ncbi_api_key="k",
            ncbi_email="a@b.c",
            deepl_auth_key="d",
            openai_api_key="sk-test",
            sqlite_path=tmp_path / "x.db",
            _env_file=None,
        )
    with pytest.raises(ValidationError):
        Settings(
            bot_token="t",
            ncbi_api_key="",
            ncbi_email="a@b.c",
            deepl_auth_key="d",
            openai_api_key="sk-test",
            sqlite_path=tmp_path / "x.db",
            _env_file=None,
        )
    with pytest.raises(ValidationError):
        Settings(
            bot_token="t",
            ncbi_api_key="k",
            ncbi_email="a@b.c",
            deepl_auth_key="d",
            openai_api_key="  ",
            sqlite_path=tmp_path / "x.db",
            _env_file=None,
        )
