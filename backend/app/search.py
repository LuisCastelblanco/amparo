"""Tavily client for Colombian Constitutional Court case law, with a result cache and credit accounting.

Every search goes through a `Searcher` (one per case): it answers from the cache when it can (0 credits),
otherwise reserves the search's credits against the daily cap and searches live. Results are cached by
normalized query plus parameters for SEARCH_CACHE_TTL_DAYS, in Firestore (deployed) or in files under
.cache/tavily (local development).

Credits (Tavily pricing): a basic search costs 1, an advanced search 2. Extract costs 1 credit per 5 URLs
(basic) or 2 (advanced); Amparo does not use it, since it downloads ruling pages itself (app/sources.py).
"""

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from tavily import AsyncTavilyClient, TavilyClient
from tavily.errors import ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError, UsageLimitExceededError

from app.config import REPO_ROOT, get_settings

logger = logging.getLogger("amparo.search")

JURISPRUDENCE_DOMAINS = ["corteconstitucional.gov.co"]
# Official sources for filing channels: the judiciary and Colombian government sites.
OFFICIAL_DOMAINS = ["ramajudicial.gov.co", "gov.co"]
# Total wall-clock budget per search; the SDK timeout alone does not bound slow responses.
SEARCH_TIMEOUT_S = 30.0

Depth = Literal["basic", "advanced"]
CREDITS_PER_SEARCH: dict[str, int] = {"basic": 1, "advanced": 2}
# Tavily refuses these for the account as a whole (429 rate limit, 432/433 plan or pay-as-you-go limit,
# 401/403 key); retrying or trying another query will not help.
_REFUSALS = (UsageLimitExceededError, ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError)


@dataclass
class SearchResult:
    title: str
    url: str
    content: str


@dataclass
class Searched:
    results: list[SearchResult]
    credits: int  # 0 for a cache hit
    cached: bool


class SearchUnavailable(RuntimeError):
    """Case-law search cannot run now: Tavily refused (credits, limits, key), our own daily credit cap is
    reached, or it kept failing. `reason` is "tavily_refused", "daily_credit_limit" or "error"."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class CreditGate(Protocol):
    """The daily Tavily credit cap, shared by every case (Firestore in production)."""

    async def reserve(self, credits: int) -> bool:
        """Count `credits` against today's cap; False (and nothing counted) if they do not fit."""

    async def release(self, credits: int) -> None:
        """Give back credits reserved for a search that failed or cost less."""


def _api_key() -> str:
    key = get_settings().tavily_api_key
    if not key:
        raise MissingAPIKeyError()
    return key


@lru_cache
def get_tavily() -> TavilyClient:
    return TavilyClient(api_key=_api_key())


def _params(query: str, max_results: int, domains: list[str], depth: Depth) -> dict[str, Any]:
    return {"query": query, "include_domains": domains, "max_results": max_results, "search_depth": depth}


def _parse(response: dict) -> list[SearchResult]:
    return [
        SearchResult(title=r.get("title", ""), url=r.get("url", ""), content=r.get("content", ""))
        for r in response.get("results", [])
    ]


def search_jurisprudence(query: str, max_results: int = 5) -> list[SearchResult]:
    """One live search, without cache or accounting (smoke test only)."""
    params = _params(query, max_results, JURISPRUDENCE_DOMAINS, "basic")
    return _parse(get_tavily().search(**params, timeout=SEARCH_TIMEOUT_S))


def is_official(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == "gov.co" or host.endswith(".gov.co")


# --- Cache -------------------------------------------------------------------


def normalize_query(query: str) -> str:
    """Case, Unicode form and whitespace do not change a search; accents do, so they are kept."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", query)).strip().casefold()


def cache_key(params: dict[str, Any]) -> str:
    canonical = {
        "query": normalize_query(params["query"]),
        "include_domains": sorted(params["include_domains"]),
        "max_results": params["max_results"],
        "search_depth": params["search_depth"],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class SearchCache(Protocol):
    async def get(self, key: str) -> list[dict[str, str]] | None:
        """The cached results, or None if absent or older than the TTL."""

    async def put(self, key: str, params: dict[str, Any], results: list[dict[str, str]]) -> None: ...


def _ttl() -> timedelta:
    return timedelta(days=get_settings().search_cache_ttl_days)


class FileSearchCache:
    """One JSON file per search under `folder` (local development; .cache/ is gitignored)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    async def get(self, key: str) -> list[dict[str, str]] | None:
        path = self.folder / f"{key}.json"
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        if datetime.fromisoformat(entry["created_at"]) < datetime.now(UTC) - _ttl():
            return None
        return entry["results"]

    async def put(self, key: str, params: dict[str, Any], results: list[dict[str, str]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        entry = {"created_at": datetime.now(UTC).isoformat(), "params": params, "results": results}
        (self.folder / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


class FirestoreSearchCache:
    """Documents <key> in SEARCH_CACHE_COLLECTION; a Firestore TTL policy on `expire_at` deletes old ones."""

    def __init__(self, project: str, collection: str) -> None:
        from google.cloud import firestore

        self.col = firestore.AsyncClient(project=project or None).collection(collection)

    async def get(self, key: str) -> list[dict[str, str]] | None:
        entry = (await self.col.document(key).get()).to_dict()
        # TTL deletion can lag by a day or more; enforce the TTL here too.
        if not entry or entry["created_at"] < datetime.now(UTC) - _ttl():
            return None
        return entry["results"]

    async def put(self, key: str, params: dict[str, Any], results: list[dict[str, str]]) -> None:
        now = datetime.now(UTC)
        await self.col.document(key).set({
            "query": params["query"], "include_domains": params["include_domains"],
            "search_depth": params["search_depth"], "max_results": params["max_results"],
            "results": results, "created_at": now, "expire_at": now + _ttl(),
        })


@lru_cache
def get_search_cache() -> SearchCache:
    settings = get_settings()
    store = settings.search_cache_store or ("firestore" if settings.usage_store == "firestore" else "file")
    if store == "firestore":
        return FirestoreSearchCache(settings.firestore_project, settings.search_cache_collection)
    if store != "file":
        raise ValueError(f"SEARCH_CACHE_STORE must be 'firestore' or 'file', not {store!r}")
    return FileSearchCache(REPO_ROOT / ".cache" / "tavily")


# --- Searcher ----------------------------------------------------------------


async def _live(params: dict[str, Any]) -> dict:
    client = AsyncTavilyClient(api_key=_api_key())
    return await asyncio.wait_for(
        client.search(**params, timeout=SEARCH_TIMEOUT_S, include_usage=True), SEARCH_TIMEOUT_S
    )


class Searcher:
    """One case's access to Tavily: cache first, then a live search within the daily credit cap.

    use_cache=False forces live searches (their results still refresh the cache). After Tavily refuses
    the account once, later live searches in the same case fail at once instead of calling it again.
    """

    def __init__(self, use_cache: bool = True, gate: CreditGate | None = None) -> None:
        self.use_cache = use_cache
        self.gate = gate
        self.refused: SearchUnavailable | None = None

    async def search(
        self, query: str, *, domains: list[str] = JURISPRUDENCE_DOMAINS, depth: Depth = "advanced",
        max_results: int = 5,
    ) -> Searched:
        params = _params(query, max_results, domains, depth)
        key = cache_key(params)
        if self.use_cache:
            try:
                cached = await get_search_cache().get(key)
            except Exception:  # noqa: BLE001 - a cache outage only costs credits
                logger.exception("search cache read failed")
                cached = None
            if cached is not None:
                return Searched(_parse({"results": cached}), credits=0, cached=True)
        if self.refused is not None:
            raise self.refused

        credits = CREDITS_PER_SEARCH[depth]
        if self.gate is not None and not await self.gate.reserve(credits):
            raise SearchUnavailable("daily_credit_limit", "today's Tavily credit limit is reached")
        try:
            response = await self._with_retry(params)
        except BaseException:
            if self.gate is not None:
                await self.gate.release(credits)
            raise
        charged = int((response.get("usage") or {}).get("credits", credits))
        if self.gate is not None and charged < credits:
            await self.gate.release(credits - charged)
        results = _parse(response)
        try:
            await get_search_cache().put(key, params, [r.__dict__ for r in results])
        except Exception:  # noqa: BLE001
            logger.exception("search cache write failed")
        return Searched(results, credits=charged, cached=False)

    async def _with_retry(self, params: dict[str, Any]) -> dict:
        """One retry for timeouts and server errors; account-wide refusals are not retried."""
        for attempt in range(2):
            try:
                return await _live(params)
            except _REFUSALS as exc:
                logger.warning("tavily refused: %s: %s", type(exc).__name__, exc)
                self.refused = SearchUnavailable("tavily_refused", f"{type(exc).__name__}: {exc}")
                raise self.refused from exc
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    raise SearchUnavailable("error", f"{type(exc).__name__}: {exc}") from exc
                logger.warning("tavily search failed, retrying: %r", exc)
                await asyncio.sleep(1.0)
        raise AssertionError("unreachable")
