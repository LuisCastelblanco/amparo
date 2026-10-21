"""Usage control for the public demo: kill switch, per-IP and global daily limits, spend cap, concurrency.

Counters live in Firestore (USAGE_STORE=firestore) so they survive restarts and scale-to-zero, and are
shared by every instance; USAGE_STORE=memory keeps them in-process for local development. Days and
hours are UTC. Client IPs are stored only as HMAC-SHA256 hashes, and per-IP documents expire.
"""

import asyncio
import hashlib
import hmac
import logging
import random
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol

from app.config import Settings, get_settings

logger = logging.getLogger("amparo.usage")

# Messages are shown verbatim by the UI.
MESSAGES = {
    "live_runs_disabled": "Live runs are switched off for now. You can still watch a recorded sample run.",
    "usage_unavailable": "Live runs are temporarily unavailable. Please try again in a minute, "
    "or watch a recorded sample run.",
    "busy": "Amparo is already drafting the maximum number of cases at once. Please try again in a minute.",
    "ip_hourly_limit": "You have reached the limit of {limit} cases per hour. Please try again later, "
    "or watch a recorded sample run.",
    "ip_daily_limit": "You have reached the limit of {limit} cases per day. Please come back tomorrow (UTC), "
    "or watch a recorded sample run.",
    "daily_case_limit": "The public demo has reached its limit of {limit} cases for today. "
    "It resets at 00:00 UTC; meanwhile you can watch a recorded sample run.",
    "daily_cost_limit": "The public demo has reached today's budget for live runs. "
    "It resets at 00:00 UTC; meanwhile you can watch a recorded sample run.",
}


@dataclass
class Refusal:
    code: str
    message: str
    status: int
    retry_after_s: int | None = None

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "retry_after_s": self.retry_after_s}


def _day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _hour(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H")


def _seconds_to_next_day(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())


def _seconds_to_next_hour(now: datetime) -> int:
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return int((nxt - now).total_seconds())


def check_limits(
    settings: Settings, now: datetime, day: dict[str, Any], ip_hour: int, ip_day: int
) -> Refusal | None:
    """Pure decision from the current counters; None means the case may run."""
    if day.get("cost_usd", 0.0) >= settings.daily_cost_limit_usd:
        return Refusal("daily_cost_limit", MESSAGES["daily_cost_limit"], 429, _seconds_to_next_day(now))
    if day.get("cases", 0) >= settings.daily_case_limit:
        msg = MESSAGES["daily_case_limit"].format(limit=settings.daily_case_limit)
        return Refusal("daily_case_limit", msg, 429, _seconds_to_next_day(now))
    if ip_day >= settings.rate_limit_per_day:
        msg = MESSAGES["ip_daily_limit"].format(limit=settings.rate_limit_per_day)
        return Refusal("ip_daily_limit", msg, 429, _seconds_to_next_day(now))
    if ip_hour >= settings.rate_limit_per_hour:
        msg = MESSAGES["ip_hourly_limit"].format(limit=settings.rate_limit_per_hour)
        return Refusal("ip_hourly_limit", msg, 429, _seconds_to_next_hour(now))
    return None


class UsageStore(Protocol):
    async def reserve(self, ip_hash: str, now: datetime) -> Refusal | None:
        """Atomically check the limits and, if allowed, count one case for the day and the IP."""

    async def record_result(self, now: datetime, cost_usd: float, completed: bool) -> None: ...

    async def record_refusal(self, now: datetime, code: str) -> None: ...

    async def day(self, now: datetime) -> dict[str, Any]: ...


class MemoryStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.days: dict[str, dict[str, Any]] = {}
        self.ips: dict[str, int] = {}

    def _day_doc(self, now: datetime) -> dict[str, Any]:
        return self.days.setdefault(_day(now), {"cases": 0, "completed": 0, "failed": 0, "cost_usd": 0.0, "refused": {}})

    async def reserve(self, ip_hash: str, now: datetime) -> Refusal | None:
        day = self._day_doc(now)
        hour_key, day_key = f"{ip_hash}-h-{_hour(now)}", f"{ip_hash}-d-{_day(now)}"
        refusal = check_limits(self.settings, now, day, self.ips.get(hour_key, 0), self.ips.get(day_key, 0))
        if refusal is None:
            day["cases"] += 1
            self.ips[hour_key] = self.ips.get(hour_key, 0) + 1
            self.ips[day_key] = self.ips.get(day_key, 0) + 1
        return refusal

    async def record_result(self, now: datetime, cost_usd: float, completed: bool) -> None:
        day = self._day_doc(now)
        day["cost_usd"] += cost_usd
        day["completed" if completed else "failed"] += 1

    async def record_refusal(self, now: datetime, code: str) -> None:
        refused = self._day_doc(now)["refused"]
        refused[code] = refused.get(code, 0) + 1

    async def day(self, now: datetime) -> dict[str, Any]:
        return dict(self._day_doc(now))


class FirestoreStore:
    """Documents in one collection: day-<date> (totals) and ip-<hash>-<window> (per-IP counts, with TTL)."""

    def __init__(self, settings: Settings) -> None:
        from google.cloud import firestore

        self.settings = settings
        self.fs = firestore
        self.client = firestore.AsyncClient(project=settings.firestore_project or None)
        self.col = self.client.collection(settings.firestore_collection)

    async def reserve(self, ip_hash: str, now: datetime) -> Refusal | None:
        fs = self.fs
        day_ref = self.col.document(f"day-{_day(now)}")
        hour_ref = self.col.document(f"ip-{ip_hash}-h-{_hour(now)}")
        ipday_ref = self.col.document(f"ip-{ip_hash}-d-{_day(now)}")
        expire_at = now + timedelta(days=2)  # Firestore TTL policy on `expire_at` deletes per-IP docs

        @fs.async_transactional
        async def txn(transaction: Any) -> Refusal | None:
            day_snap = await day_ref.get(transaction=transaction)
            hour_snap = await hour_ref.get(transaction=transaction)
            ipday_snap = await ipday_ref.get(transaction=transaction)
            day = day_snap.to_dict() or {}
            ip_hour = (hour_snap.to_dict() or {}).get("count", 0)
            ip_day = (ipday_snap.to_dict() or {}).get("count", 0)
            refusal = check_limits(self.settings, now, day, ip_hour, ip_day)
            if refusal is None:
                transaction.set(day_ref, {"cases": fs.Increment(1), "date": _day(now)}, merge=True)
                transaction.set(hour_ref, {"count": fs.Increment(1), "expire_at": expire_at}, merge=True)
                transaction.set(ipday_ref, {"count": fs.Increment(1), "expire_at": expire_at}, merge=True)
            return refusal

        from google.api_core.exceptions import Aborted

        # Concurrent reservations contend on the day document; the SDK does not retry an abort
        # raised while reading, so retry the whole transaction with jitter.
        for attempt in range(6):
            try:
                return await txn(self.client.transaction())
            except Aborted:
                if attempt == 5:
                    raise
                await asyncio.sleep(0.05 * 2**attempt + random.uniform(0, 0.1))
        raise AssertionError("unreachable")

    async def record_result(self, now: datetime, cost_usd: float, completed: bool) -> None:
        fs = self.fs
        await self.col.document(f"day-{_day(now)}").set(
            {"cost_usd": fs.Increment(cost_usd), ("completed" if completed else "failed"): fs.Increment(1)},
            merge=True,
        )

    async def record_refusal(self, now: datetime, code: str) -> None:
        await self.col.document(f"day-{_day(now)}").set({"refused": {code: self.fs.Increment(1)}}, merge=True)

    async def day(self, now: datetime) -> dict[str, Any]:
        snap = await self.col.document(f"day-{_day(now)}").get()
        return snap.to_dict() or {}


@lru_cache
def get_store() -> UsageStore:
    settings = get_settings()
    if settings.usage_store == "firestore":
        return FirestoreStore(settings)
    if settings.usage_store != "memory":
        raise ValueError(f"USAGE_STORE must be 'firestore' or 'memory', not {settings.usage_store!r}")
    return MemoryStore(settings)


@lru_cache
def _ip_key() -> bytes:
    key = get_settings().ip_hash_key
    if key:
        return key.encode()
    if get_settings().usage_store == "firestore":
        raise RuntimeError("IP_HASH_KEY must be set when USAGE_STORE=firestore")
    return secrets.token_bytes(32)  # local development: per-process key


def client_ip(forwarded_for: str | None, peer: str | None) -> str:
    """The client IP: the entry appended by the trusted proxy (Cloud Run's front end) to X-Forwarded-For.

    Entries to its left are supplied by the client and can be spoofed, so they are ignored.
    """
    hops = get_settings().trusted_proxy_hops
    if forwarded_for and hops > 0:
        entries = [e.strip() for e in forwarded_for.split(",") if e.strip()]
        if entries:
            return entries[-min(hops, len(entries))]
    return peer or "unknown"


def hash_ip(ip: str) -> str:
    return hmac.new(_ip_key(), ip.encode(), hashlib.sha256).hexdigest()[:32]


class Concurrency:
    """Non-blocking cap on cases running at once in this instance.

    Slots expire after SLOT_TTL_S (longer than any request can last), so a stream that never
    started, and therefore never released its slot, cannot block the instance forever.
    """

    SLOT_TTL_S = 360.0

    def __init__(self) -> None:
        self._slots: dict[int, float] = {}

    @property
    def running(self) -> int:
        self._expire()
        return len(self._slots)

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.SLOT_TTL_S
        for token in [t for t, started in self._slots.items() if started < cutoff]:
            del self._slots[token]

    def try_acquire(self) -> int | None:
        """A slot token, or None when the instance is full."""
        if self.running >= get_settings().max_concurrent_cases:
            return None
        token = secrets.randbits(63)
        self._slots[token] = time.monotonic()
        return token

    def release(self, token: int) -> None:
        self._slots.pop(token, None)


concurrency = Concurrency()


def now_utc() -> datetime:
    return datetime.now(UTC)
