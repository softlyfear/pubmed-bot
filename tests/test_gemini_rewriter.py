"""Адаптер Gemini: prompt, timeout, пост-обработка. Без живого API."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, OpenAIError

from pubmed_bot.adapters.llm.gemini_rewriter import (
    GEMINI_BASE_URL,
    GEMINI_MAX_TOKENS,
    GEMINI_TEMPERATURE,
    GEMINI_TIMEOUT_SECONDS,
    SYSTEM_PROMPT,
    GeminiQueryRewriter,
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
        gemini_api_key="gemini-test",
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
    rewriter = GeminiQueryRewriter(_settings(), FakeOpenAIClient(completions))
    result = await rewriter.rewrite("рост ягодиц", "buttock growth")
    assert result == "gluteal hypertrophy"
    assert len(completions.calls) == 1
    call = completions.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    assert call["temperature"] == GEMINI_TEMPERATURE
    assert call["max_tokens"] == GEMINI_MAX_TOKENS
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
        httpx.Request(
            "GET", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
    )
    rewriter = GeminiQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_api_error_maps_to_unavailable() -> None:
    completions = FakeCompletions()
    completions.error = OpenAIError("down")
    rewriter = GeminiQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_empty_content_unavailable() -> None:
    completions = FakeCompletions()
    completions.content = "   "
    rewriter = GeminiQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_missing_choices_unavailable() -> None:
    completions = FakeCompletions()
    completions.choices = []
    rewriter = GeminiQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_rewrite_none_content_unavailable() -> None:
    completions = FakeCompletions()
    completions.content = None
    rewriter = GeminiQueryRewriter(_settings(), FakeOpenAIClient(completions))
    with pytest.raises(QueryRewriteUnavailable):
        await rewriter.rewrite("a", "b")


@pytest.mark.asyncio
async def test_gemini_client_timeout_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    completions = FakeCompletions()

    class FakeSDK:
        def __init__(self, *, api_key: str, base_url: str, timeout: float) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["timeout"] = timeout
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr("pubmed_bot.adapters.llm.gemini_rewriter.OpenAI", FakeSDK)
    rewriter = GeminiQueryRewriter(_settings())
    assert await rewriter.rewrite("exercise[tiab]", "exercise[tiab]") == "gluteal hypertrophy"
    assert captured["base_url"] == GEMINI_BASE_URL
    assert captured["timeout"] == GEMINI_TIMEOUT_SECONDS
    assert captured["timeout"] == 8.0
    assert captured["api_key"] == "gemini-test"


@pytest.mark.asyncio
async def test_rewrite_custom_to_thread_and_non_str() -> None:
    completions = FakeCompletions()
    rewriter = GeminiQueryRewriter(
        _settings(),
        FakeOpenAIClient(completions),
        to_thread=_immediate,
    )
    assert await rewriter.rewrite("a", "b") == "gluteal hypertrophy"

    async def not_str(_fn: Callable[[], object]) -> object:
        return 1

    broken = GeminiQueryRewriter(
        _settings(),
        FakeOpenAIClient(completions),
        to_thread=not_str,
    )
    with pytest.raises(QueryRewriteUnavailable):
        await broken.rewrite("a", "b")

    async def boom(_fn: Callable[[], object]) -> str:
        raise OpenAIError("down")

    failing = GeminiQueryRewriter(
        _settings(),
        FakeOpenAIClient(completions),
        to_thread=boom,
    )
    with pytest.raises(QueryRewriteUnavailable):
        await failing.rewrite("a", "b")


async def _immediate(fn: Callable[[], object]) -> object:
    return fn()
