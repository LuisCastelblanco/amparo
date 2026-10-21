"""Tavily client for Colombian Constitutional Court case law."""

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from tavily import AsyncTavilyClient, TavilyClient

from app.config import get_settings

JURISPRUDENCE_DOMAINS = ["corteconstitucional.gov.co"]
# Total wall-clock budget per search; the SDK timeout alone does not bound slow responses.
SEARCH_TIMEOUT_S = 30.0


@dataclass
class SearchResult:
    title: str
    url: str
    content: str


def _api_key() -> str:
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    return key


@lru_cache
def get_tavily() -> TavilyClient:
    return TavilyClient(api_key=_api_key())


def _params(query: str, max_results: int) -> dict[str, Any]:
    return {
        "query": query,
        "include_domains": JURISPRUDENCE_DOMAINS,
        "max_results": max_results,
        "search_depth": "advanced",
        "timeout": SEARCH_TIMEOUT_S,
    }


def _parse(response: dict) -> list[SearchResult]:
    return [
        SearchResult(title=r.get("title", ""), url=r.get("url", ""), content=r.get("content", ""))
        for r in response.get("results", [])
    ]


def search_jurisprudence(query: str, max_results: int = 5) -> list[SearchResult]:
    return _parse(get_tavily().search(**_params(query, max_results)))


async def asearch_jurisprudence(query: str, max_results: int = 5) -> list[SearchResult]:
    client = AsyncTavilyClient(api_key=_api_key())
    response = await asyncio.wait_for(client.search(**_params(query, max_results)), SEARCH_TIMEOUT_S)
    return _parse(response)
