"""Клиент NCBI E-utilities. Порт PubmedClient, без handlers и без HTML."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from pubmed_bot.adapters.ncbi.query import PAGE_SIZE, retstart_for_page
from pubmed_bot.config import Settings
from pubmed_bot.domain.enums import SearchSort
from pubmed_bot.domain.exceptions import PubmedUnavailable
from pubmed_bot.services.rate_limit import TokenBucket

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_MAX_RETRIES = 3
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
JsonDict = dict[str, object]


@dataclass(frozen=True, slots=True)
class ESearchResult:
    """Страница PMID из ESearch."""

    pmids: tuple[str, ...]
    count: int
    retstart: int
    retmax: int


class PubmedClient(Protocol):
    """Порт доступа к PubMed. Реализация — только E-utilities."""

    async def esearch(
        self,
        term: str,
        *,
        page: int = 1,
        retstart: int | None = None,
        sort: SearchSort = SearchSort.RELEVANCE,
        mindate: str | None = None,
        datetype: str | None = None,
    ) -> ESearchResult: ...

    async def esummary(self, pmids: Sequence[str]) -> JsonDict: ...

    async def efetch(self, ids: Sequence[str], *, db: str = "pubmed") -> str: ...


class NcbiEutilsClient:
    """ESearch / ESummary / EFetch + глобальный лимит RPS."""

    def __init__(
        self,
        settings: Settings,
        limiter: TokenBucket,
        http: httpx.AsyncClient | None = None,
        *,
        retry_backoff_base: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._limiter = limiter
        self._http = http
        self._owns_http = http is None
        self._retry_backoff_base = retry_backoff_base
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    def _common_params(self) -> dict[str, str]:
        return {
            "api_key": self._settings.ncbi_api_key,
            "email": self._settings.ncbi_email,
            "tool": self._settings.ncbi_tool,
        }

    async def esearch(
        self,
        term: str,
        *,
        page: int = 1,
        retstart: int | None = None,
        sort: SearchSort = SearchSort.RELEVANCE,
        mindate: str | None = None,
        datetype: str | None = None,
    ) -> ESearchResult:
        offset = retstart_for_page(page) if retstart is None else retstart
        if offset < 0:
            msg = "retstart должен быть >= 0"
            raise ValueError(msg)
        params: dict[str, str] = {
            **self._common_params(),
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            "retmax": str(PAGE_SIZE),
            "retstart": str(offset),
            "sort": sort.value,
        }
        if mindate:
            params["mindate"] = mindate
        if datetype:
            params["datetype"] = datetype
        payload = await self._get_json(f"{EUTILS_BASE}/esearch.fcgi", params)
        raw = payload.get("esearchresult")
        if not isinstance(raw, dict):
            raw = {}
        result = {str(key): value for key, value in raw.items()}
        error = result.get("ERROR") or result.get("error") or payload.get("error")
        if error:
            raise PubmedUnavailable("PubMed временно недоступен")
        ids = result.get("idlist") or []
        return ESearchResult(
            pmids=tuple(str(item) for item in ids),
            count=int(result.get("count") or 0),
            retstart=int(result.get("retstart") or 0),
            retmax=int(result.get("retmax") or PAGE_SIZE),
        )

    async def esummary(self, pmids: Sequence[str]) -> JsonDict:
        if not pmids:
            return {}
        params = {
            **self._common_params(),
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "json",
        }
        return await self._get_json(f"{EUTILS_BASE}/esummary.fcgi", params)

    async def efetch(self, ids: Sequence[str], *, db: str = "pubmed") -> str:
        if not ids:
            return ""
        params = {
            **self._common_params(),
            "db": db,
            "id": ",".join(ids),
            "retmode": "xml",
        }
        return await self._get_text(f"{EUTILS_BASE}/efetch.fcgi", params)

    async def _get_json(self, url: str, params: dict[str, str]) -> JsonDict:
        response = await self._request(url, params)
        try:
            data = response.json()
        except ValueError as exc:
            raise PubmedUnavailable("NCBI вернул не JSON") from exc
        if not isinstance(data, dict):
            raise PubmedUnavailable("NCBI вернул неожиданный JSON")
        return {str(key): value for key, value in data.items()}

    async def _get_text(self, url: str, params: dict[str, str]) -> str:
        response = await self._request(url, params)
        return response.text

    async def _request(self, url: str, params: dict[str, str]) -> httpx.Response:
        last_error: Exception | None = None
        for _attempt in range(_MAX_RETRIES):
            await self._limiter.acquire()
            try:
                response = await self._client().get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                await self._backoff(_attempt, retry_after=None)
                continue
            if response.status_code in _RETRY_STATUSES:
                last_error = PubmedUnavailable(f"NCBI HTTP {response.status_code}")
                await self._backoff(_attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                raise PubmedUnavailable(f"NCBI HTTP {response.status_code}")
            return response
        raise PubmedUnavailable("PubMed временно недоступен") from last_error

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        wait = self._retry_backoff_base * (2**attempt)
        if retry_after is not None:
            try:
                wait = float(retry_after)
            except ValueError:
                pass
        await self._sleep(max(wait, 0.0))
