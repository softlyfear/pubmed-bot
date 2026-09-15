"""Клиент E-utilities: параметры, retry, без живого NCBI."""

from pathlib import Path

import httpx
import pytest
import respx

from pubmed_bot.adapters.ncbi.client import EUTILS_BASE, NcbiEutilsClient
from pubmed_bot.config import Settings
from pubmed_bot.domain.enums import SearchSort
from pubmed_bot.domain.exceptions import PubmedUnavailable
from pubmed_bot.services.rate_limit import TokenBucket

ESEARCH = f"{EUTILS_BASE}/esearch.fcgi"
ESUMMARY = f"{EUTILS_BASE}/esummary.fcgi"
EFETCH = f"{EUTILS_BASE}/efetch.fcgi"


def _settings() -> Settings:
    return Settings(
        bot_token="t",
        ncbi_api_key="ncbi-key",
        ncbi_email="dev@example.com",
        ncbi_tool="pubmed-bot",
        deepl_auth_key="d",
        gemini_api_key="sk-test",
        sqlite_path=Path("x.db"),
        _env_file=None,
    )


def _client(http: httpx.AsyncClient) -> NcbiEutilsClient:
    return NcbiEutilsClient(
        _settings(),
        TokenBucket(1000.0),
        http=http,
        retry_backoff_base=0.0,
        sleep=_no_sleep,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def _esearch_ok(ids: list[str] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "esearchresult": {
                "idlist": ids or ["111"],
                "count": "42",
                "retstart": "10",
                "retmax": "10",
            }
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_esearch_sends_best_match_and_page() -> None:
    route = respx.get(ESEARCH).mock(return_value=_esearch_ok(["111", "222"]))
    async with httpx.AsyncClient() as http:
        client = _client(http)
        result = await client.esearch("(knee) AND english[lang]", page=2)
    assert result.pmids == ("111", "222")
    assert result.count == 42
    params = route.calls.last.request.url.params
    assert params["db"] == "pubmed"
    assert params["retmax"] == "10"
    assert params["retstart"] == "10"
    assert params["sort"] == "relevance"
    assert params["api_key"] == "ncbi-key"
    assert params["email"] == "dev@example.com"
    assert params["tool"] == "pubmed-bot"
    assert "mindate" not in params


@pytest.mark.asyncio
@respx.mock
async def test_esearch_accepts_date_sort_and_mindate() -> None:
    route = respx.get(ESEARCH).mock(return_value=_esearch_ok())
    async with httpx.AsyncClient() as http:
        client = _client(http)
        await client.esearch(
            "statin",
            sort=SearchSort.PUB_DATE,
            mindate="2024/01/01",
            datetype="edat",
        )
    params = route.calls.last.request.url.params
    assert params["sort"] == "pub_date"
    assert params["mindate"] == "2024/01/01"
    assert params["datetype"] == "edat"


@pytest.mark.asyncio
@respx.mock
async def test_esearch_retstart_overrides_page() -> None:
    route = respx.get(ESEARCH).mock(return_value=_esearch_ok(["111"]))
    async with httpx.AsyncClient() as http:
        client = _client(http)
        result = await client.esearch("knee", page=9, retstart=12)
    assert result.pmids == ("111",)
    params = route.calls.last.request.url.params
    assert params["retstart"] == "12"
    assert params["retmax"] == "10"


@pytest.mark.asyncio
@respx.mock
async def test_esearch_rejects_negative_retstart() -> None:
    async with httpx.AsyncClient() as http:
        client = _client(http)
        with pytest.raises(ValueError, match="retstart"):
            await client.esearch("knee", retstart=-1)


@pytest.mark.asyncio
@respx.mock
async def test_esummary_and_efetch_params() -> None:
    summary = respx.get(ESUMMARY).mock(return_value=httpx.Response(200, json={"result": {}}))
    fetch = respx.get(EFETCH).mock(return_value=httpx.Response(200, text="<PubmedArticleSet/>"))
    async with httpx.AsyncClient() as http:
        client = _client(http)
        await client.esummary(["1", "2"])
        xml = await client.efetch(["1"], db="pmc")
    assert xml == "<PubmedArticleSet/>"
    assert summary.calls.last.request.url.params["id"] == "1,2"
    assert fetch.calls.last.request.url.params["db"] == "pmc"


@pytest.mark.asyncio
@respx.mock
async def test_429_then_unavailable() -> None:
    respx.get(ESEARCH).mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}))
    async with httpx.AsyncClient() as http:
        client = _client(http)
        with pytest.raises(PubmedUnavailable):
            await client.esearch("knee")
    assert respx.calls.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_5xx_maps_to_pubmed_unavailable() -> None:
    respx.get(ESEARCH).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        client = _client(http)
        with pytest.raises(PubmedUnavailable):
            await client.esearch("knee")


@pytest.mark.asyncio
@respx.mock
async def test_400_does_not_retry() -> None:
    respx.get(ESEARCH).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    async with httpx.AsyncClient() as http:
        client = _client(http)
        with pytest.raises(PubmedUnavailable, match="400"):
            await client.esearch("knee")
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_esearch_json_error_is_unavailable_not_empty() -> None:
    respx.get(ESEARCH).mock(
        return_value=httpx.Response(
            200,
            json={"esearchresult": {"ERROR": "Server busy", "idlist": []}},
        )
    )
    async with httpx.AsyncClient() as http:
        client = _client(http)
        with pytest.raises(PubmedUnavailable, match="временно недоступен"):
            await client.esearch("knee")
