"""Адаптер Gemini: rewriter английского поискового term."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from openai import OpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from pubmed_bot.config import Settings
from pubmed_bot.domain.exceptions import QueryRewriteUnavailable

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_TIMEOUT_SECONDS = 8.0
GEMINI_MAX_TOKENS = 64
GEMINI_TEMPERATURE = 0

SYSTEM_PROMPT = """You are a PubMed query rewriter for a search bot. You do not chat with the user and you do not give medical advice.

The application will send two lines:
Original query: <user text>
English draft: <DeepL English, or the original if it was already English>

Your job: emit the inner PubMed search expression (English keywords). The application wraps it as ({your output}) AND english[lang] AND hasabstract AND NOT (letter[pt] OR editorial[pt] OR news[pt] OR comment[pt] OR "newspaper article"[pt]). You must not repeat those filters.

Rules:
1. Output exactly one line. No preface, no markdown, no quotes around the whole line, no numbered lists.
2. Use concise biomedical English that PubMed Best Match can rank (anatomy, condition, intervention). Typical length: 2–8 words or a short Boolean.
3. Fix literal machine translation. Examples of the failure mode: "buttock growth" or "buttocks" for a hypertrophy/gluteal training intent → gluteal hypertrophy ; "hair growth" for a dermatology/trichology intent → androgenetic alopecia OR hair follicle (choose the reading that best matches the original).
4. You MAY add [tiab] or [mesh] on individual terms. Do NOT add hasabstract, english[lang], any [pt] publication-type filter, year/date limits, free full text, open access, ffrft, or humans[MeSH].
5. Do not mention PDF, Sci-Hub, publishers, or how to get full text.
6. If the English draft is already valid PubMed syntax (e.g. exercise[tiab], a MeSH term, a clear English phrase), return it unchanged.
7. If intent is ambiguous, pick the most common biomedical reading. Never ask a question.
8. Never output Cyrillic. Never output an empty line.

Examples:
Original query: рост ягодиц
English draft: buttock growth
gluteal hypertrophy

Original query: рост волос
English draft: hair growth
androgenetic alopecia OR hair follicle

Original query: exercise[tiab]
English draft: exercise[tiab]
exercise[tiab]"""


class QueryRewriter(Protocol):
    """Порт rewriter поискового term. Не переводит статьи."""

    async def rewrite(self, original: str, english_draft: str) -> str: ...


class _Completions(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _Chat(Protocol):
    @property
    def completions(self) -> _Completions: ...


class GeminiChatClient(Protocol):
    """Минимум sync-клиента Gemini OpenAI-compatible API для fake в тестах."""

    @property
    def chat(self) -> _Chat: ...


def user_message(original: str, english_draft: str) -> str:
    """Пользовательское сообщение rewriter: две строки original/draft."""
    return f"Original query: {original}\nEnglish draft: {english_draft}"


def postprocess_rewrite(raw: str) -> str:
    """Первая непустая строка без markdown-fence и парных кавычек вокруг."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    first: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if first is None:
        return ""
    if len(first) >= 2 and first[0] == first[-1] and first[0] in "\"'":
        return first[1:-1].strip()
    return first


class GeminiQueryRewriter:
    """Gemini OpenAI-compatible API: Chat Completions, timeout 8 с, через to_thread."""

    def __init__(
        self,
        settings: Settings,
        client: GeminiChatClient | None = None,
        *,
        to_thread: Callable[..., Awaitable[object]] | None = None,
    ) -> None:
        self._api_key = settings.gemini_api_key
        self._model = settings.gemini_model
        self._stub = client
        self._sdk: OpenAI | None = None
        self._to_thread = to_thread

    def _sdk_client(self) -> OpenAI:
        if self._sdk is None:
            self._sdk = OpenAI(
                api_key=self._api_key,
                base_url=GEMINI_BASE_URL,
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
        return self._sdk

    def _create_completion(self, messages: list[ChatCompletionMessageParam]) -> object:
        if self._stub is not None:
            return self._stub.chat.completions.create(
                model=self._model,
                temperature=GEMINI_TEMPERATURE,
                max_tokens=GEMINI_MAX_TOKENS,
                messages=messages,
            )
        return self._sdk_client().chat.completions.create(
            model=self._model,
            temperature=float(GEMINI_TEMPERATURE),
            max_tokens=GEMINI_MAX_TOKENS,
            messages=messages,
        )

    def _rewrite_sync(self, original: str, english_draft: str) -> str:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message(original, english_draft)},
        ]
        try:
            completion = self._create_completion(messages)
        except OpenAIError as exc:
            raise QueryRewriteUnavailable("rewrite недоступен") from exc
        choices = getattr(completion, "choices", None)
        if not choices:
            raise QueryRewriteUnavailable("rewrite недоступен")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            raise QueryRewriteUnavailable("rewrite недоступен")
        processed = postprocess_rewrite(content)
        if not processed:
            raise QueryRewriteUnavailable("rewrite недоступен")
        return processed

    async def _invoke(self, fn: Callable[[], object]) -> object:
        if self._to_thread is None:
            return await asyncio.to_thread(fn)
        return await self._to_thread(fn)

    async def rewrite(self, original: str, english_draft: str) -> str:
        try:
            value = await self._invoke(lambda: self._rewrite_sync(original, english_draft))
        except QueryRewriteUnavailable:
            raise
        except OpenAIError as exc:
            raise QueryRewriteUnavailable("rewrite недоступен") from exc
        if not isinstance(value, str):
            raise QueryRewriteUnavailable("rewrite недоступен")
        return value
