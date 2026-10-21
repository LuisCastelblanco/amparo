"""Tavily client for Colombian Constitutional Court case law."""

from dataclasses import dataclass
from functools import lru_cache

from tavily import TavilyClient

from app.config import get_settings

JURISPRUDENCE_DOMAINS = ["corteconstitucional.gov.co"]


@dataclass
class SearchResult:
    title: str
    url: str
    content: str


@lru_cache
def get_tavily() -> TavilyClient:
    settings = get_settings()
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    return TavilyClient(api_key=settings.tavily_api_key)


def search_jurisprudence(query: str, max_results: int = 5) -> list[SearchResult]:
    response = get_tavily().search(
        query=query,
        include_domains=JURISPRUDENCE_DOMAINS,
        max_results=max_results,
        search_depth="advanced",
    )
    return [
        SearchResult(title=r.get("title", ""), url=r.get("url", ""), content=r.get("content", ""))
        for r in response.get("results", [])
    ]
