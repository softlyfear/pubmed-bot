"""Адаптер OpenAI Chat Completions: prompt, timeout, пост-обработка. Без живого API."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, OpenAIError

from pubmed_bot.adapters.llm.openai_rewriter import (
    OPENAI_MAX_TOKENS,
    OPENAI_TEMPERATURE,
    OPENAI_TIMEOUT_SECONDS,
    SYSTEM_PROMPT,
    OpenAIQueryRewriter,
    postprocess_rewrite,
    user_message,
)
from pubmed_bot.config import Settings
from pubmed_bot.domain.exceptions import QueryRewriteUnavailable


def _settings() -> Settings:
    return Settings(
        bot_token="t",
        ncbi_api_key="k",
        ncbi_email="dev@example.com",
        ncbi_tool="pubmed-bot",
        deepl_auth_key="d",
        openai_api_key="sk-test",
        sqlite_path=Path("x.db"),
        _env_file=None,
    )


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.content: str | None = "gluteal hypertrophy"
        self.error: Exception | None = None
        self.choices: list[object] | None = None

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.choices is not None:
            return SimpleNamespace(choices=self.choices)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeOpenAIClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def test_system_prompt_is_verbatim_ac() -> None:
    assert SYSTEM_PROMPT.startswith(
        "You are a PubMed query rewriter for a search bot. You do not chat with the user"
    )
    assert "gluteal hypertrophy" in SYSTEM_PROMPT
    assert "androgenetic alopecia OR hair follicle" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.endswith("exercise[tiab]")


def test_user_message_two_lines() -> None:
    assert user_message("рост ягодиц", "buttock growth") == (
        "Original query: рост ягодиц\nEnglish draft: buttock growth"
    )


def test_postprocess_fence_quotes_first_line() -> None:
    assert postprocess_rewrite("```\ngluteal hypertrophy\n```") == "gluteal hypertrophy"
    assert postprocess_rewrite('```text\n"hair follicle"\n```') == "hair follicle"
    assert postprocess_rewrite("'exercise[tiab]'") == "exercise[tiab]"
    assert postprocess_rewrite("one\ntwo") == "one"
    assert postprocess_rewrite("   ") == ""


@pytest.mark.asyncio
async def test_rewrite_sends_prompt_temperature_and_tokens() -> None:
    completions = FakeCompletions()
    rewriter = OpenAIQueryRewriter(_settings(), FakeOpenAIClient(completions))
    result = await rewriter.rewrite("рост ягодиц", "buttock growth")
    assert result == "gluteal hypertrophy"
    assert len(completions.calls) == 1
    call = completions.calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["temperature"] == OPENAI_TEMPERATURE
    assert call["max_tokens"] == OPENAI_MAX_TOKENS
    messages = call["messages"]
    assert isinstance(messages, list)
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1] == {
        "role": "user",
        "content": user_message("рост ягодиц", "buttock growth"),
    }


@pytest.mark.asyncio
async def test_rewrite_timeout_maps_to_unavailable() -> None:
    completions = FakeCompletions()
    completions.error = APITimeoutError(
        httpx.Request("GET", "https://api.openai.com/v1/chat/completions")
    )
    rewriter = OpenAIQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_api_error_maps_to_unavailable() -> None:
    completions = FakeCompletions()
    completions.error = OpenAIError("down")
    rewriter = OpenAIQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_empty_content_unavailable() -> None:
    completions = FakeCompletions()
    completions.content = "   "
    rewriter = OpenAIQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_missing_choices_unavailable() -> None:
    completions = FakeCompletions()
    completions.choices = []
    rewriter = OpenAIQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_none_content_unavailable() -> None:
    completions = FakeCompletions()
    completions.content = None
    rewriter = OpenAIQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_openai_client_timeout_is_eight_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    completions = FakeCompletions()

    class FakeSDK:
        def __init__(self, *, api_key: str, timeout: float) -> None:
            captured["api_key"] = api_key
            captured["timeout"] = timeout
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr("pubmed_bot.adapters.llm.openai_rewriter.OpenAI", FakeSDK)
    rewriter = OpenAIQueryRewriter(_settings())
    assert await rewriter.rewrite("exercise[tiab]", "exercise[tiab]") == "gluteal hypertrophy"
    assert captured["timeout"] == OPENAI_TIMEOUT_SECONDS
    assert captured["timeout"] == 8.0
    assert captured["api_key"] == "sk-test"


@pytest.mark.asyncio
async def test_rewrite_custom_to_thread_and_non_str() -> None:
    completions = FakeCompletions()
    rewriter = OpenAIQueryRewriter(
        _settings(),
        FakeOpenAIClient(completions),
        to_thread=_immediate,
    )
    assert await rewriter.rewrite("a", "b") == "gluteal hypertrophy"

    async def not_str(_fn: Callable[[], object]) -> object:
        return 1

    broken = OpenAIQueryRewriter(
        _settings(),
        FakeOpenAIClient(completions),
        to_thread=not_str,
    )
    with pytest.raises(QueryRewriteUnavailable):
        await broken.rewrite("a", "b")

    async def boom(_fn: Callable[[], object]) -> str:
        raise OpenAIError("down")

    failing = OpenAIQueryRewriter(
        _settings(),
        FakeOpenAIClient(completions),
        to_thread=boom,
    )
    with pytest.raises(QueryRewriteUnavailable):
        await failing.rewrite("a", "b")


async def _immediate(fn: Callable[[], object]) -> object:
    return fn()
