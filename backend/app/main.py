import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import usage
from app.config import get_settings
from app.pipeline import run_case

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("amparo.api")

app = FastAPI(title="Amparo API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# Keeps fire-and-forget bookkeeping tasks alive until they finish.
_background: set[asyncio.Task] = set()


class CaseRequest(BaseModel):
    narrative: str = Field(min_length=20, max_length=get_settings().max_narrative_chars)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/usage")
async def usage_today() -> dict[str, Any]:
    """Today's (UTC) aggregate usage and limits. No personal data."""
    settings = get_settings()
    now = usage.now_utc()
    day = await usage.get_store().day(now)
    return {
        "date_utc": now.strftime("%Y-%m-%d"),
        "live_runs_enabled": settings.live_runs_enabled,
        "cases_started": day.get("cases", 0),
        "cases_completed": day.get("completed", 0),
        "cases_failed": day.get("failed", 0),
        "estimated_cost_usd": round(day.get("cost_usd", 0.0), 6),
        "refused": day.get("refused", {}),
        "running_on_this_instance": usage.concurrency.running,
        "limits": {
            "daily_case_limit": settings.daily_case_limit,
            "daily_cost_limit_usd": settings.daily_cost_limit_usd,
            "rate_limit_per_hour": settings.rate_limit_per_hour,
            "rate_limit_per_day": settings.rate_limit_per_day,
            "max_concurrent_cases_per_instance": settings.max_concurrent_cases,
            "max_narrative_chars": settings.max_narrative_chars,
            "max_tavily_searches_per_case": settings.max_tavily_searches_per_case,
        },
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _record_result(cost_usd: float, completed: bool) -> None:
    task = asyncio.create_task(usage.get_store().record_result(usage.now_utc(), cost_usd, completed))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _stream(narrative: str, slot: int) -> AsyncIterator[str]:
    cost = 0.0
    completed = False
    try:
        async for event, data in run_case(narrative):
            if event in ("step_completed", "step_failed"):
                cost += data["metrics"]["cost_usd"]
            completed = completed or event == "case_completed"
            yield _sse(event, data)
    except Exception as exc:  # noqa: BLE001 - never break the stream without telling the client
        logger.exception("pipeline crashed outside a step")
        yield _sse("case_failed", {"failed_step": None, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        # Runs on completion, failure and client disconnect; spend counts against the daily cap.
        usage.concurrency.release(slot)
        _record_result(cost, completed)
        logger.info("case finished completed=%s cost_usd=%.6f", completed, cost)


def _refuse(refusal: usage.Refusal) -> JSONResponse:
    headers = {"Retry-After": str(refusal.retry_after_s)} if refusal.retry_after_s else None
    return JSONResponse(refusal.body(), status_code=refusal.status, headers=headers)


@app.post("/api/cases/run", response_model=None)
async def run_case_endpoint(request: CaseRequest, http: Request) -> StreamingResponse | JSONResponse:
    settings = get_settings()
    store = usage.get_store()
    now = usage.now_utc()
    if not settings.live_runs_enabled:
        refusal = usage.Refusal("live_runs_disabled", usage.MESSAGES["live_runs_disabled"], 503)
        await store.record_refusal(now, refusal.code)
        return _refuse(refusal)
    slot = usage.concurrency.try_acquire()
    if slot is None:
        refusal = usage.Refusal("busy", usage.MESSAGES["busy"], 429, 60)
        await store.record_refusal(now, refusal.code)
        return _refuse(refusal)
    try:
        ip = usage.client_ip(http.headers.get("x-forwarded-for"), http.client.host if http.client else None)
        refusal = await store.reserve(usage.hash_ip(ip), now)
    except Exception:  # noqa: BLE001 - fail closed: without counters, limits cannot be enforced
        logger.exception("usage store unavailable")
        usage.concurrency.release(slot)
        return _refuse(usage.Refusal("usage_unavailable", usage.MESSAGES["usage_unavailable"], 503, 60))
    if refusal is not None:
        usage.concurrency.release(slot)
        await store.record_refusal(now, refusal.code)
        logger.info("case refused code=%s", refusal.code)
        return _refuse(refusal)
    return StreamingResponse(
        _stream(request.narrative, slot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
