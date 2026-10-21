import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app import document, usage
from app.config import get_settings
from app.pipeline import StepUsage, clarifying_questions, intake, run_case, with_answers
from app.schemas import TutelaSections

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("amparo.api")

app = FastAPI(title="Amparo API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"],
)

# Keeps fire-and-forget bookkeeping tasks alive until they finish.
_background: set[asyncio.Task] = set()


class Answer(BaseModel):
    question: str = Field(max_length=300)
    answer: str = Field(default="", max_length=500)


class IntakeRequest(BaseModel):
    narrative: str = Field(min_length=20, max_length=get_settings().max_narrative_chars)


class CaseRequest(IntakeRequest):
    # Adds an English summary of the final draft for reviewers (one more fast-tier call).
    english_summary: bool = False
    # Answers to the clarifying questions from /api/cases/intake; empty answers mean "skipped".
    answers: list[Answer] = Field(default_factory=list, max_length=get_settings().max_clarifying_questions)


class DocumentRequest(TutelaSections):
    """The final draft (a TutelaDraft from a live or recorded run); texto_completo is ignored."""

    accionada: str = Field(min_length=1, max_length=200)
    ciudad: str | None = Field(default=None, max_length=120)


MAX_DOCUMENT_JSON_CHARS = 80_000
DOCUMENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


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
        "intake_checks": day.get("intake_checks", 0),
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


async def _stream(narrative: str, english_summary: bool, slot: int) -> AsyncIterator[str]:
    cost = 0.0
    completed = False
    try:
        async for event, data in run_case(narrative, english_summary=english_summary):
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
        _stream(
            with_answers(request.narrative, [(a.question, a.answer) for a in request.answers]),
            request.english_summary,
            slot,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/cases/intake", response_model=None)
async def intake_endpoint(request: IntakeRequest, http: Request) -> JSONResponse:
    """Intake plus up to MAX_CLARIFYING_QUESTIONS questions for critical missing data (draft tier).

    The UI asks them before the full run; answers are optional and go to /api/cases/run.
    """
    settings = get_settings()
    store = usage.get_store()
    now = usage.now_utc()
    if not settings.live_runs_enabled:
        return _refuse(usage.Refusal("live_runs_disabled", usage.MESSAGES["live_runs_disabled"], 503))
    try:
        day = await store.day(now)
    except Exception:  # noqa: BLE001 - fail closed, as for full runs
        logger.exception("usage store unavailable")
        return _refuse(usage.Refusal("usage_unavailable", usage.MESSAGES["usage_unavailable"], 503, 60))
    refusal = usage.check_limits(settings, now, day, ip_hour=0, ip_day=0)
    if refusal is not None:
        return _refuse(refusal)
    ip = usage.client_ip(http.headers.get("x-forwarded-for"), http.client.host if http.client else None)
    if not usage.intake_limiter.allow(usage.hash_ip(ip)):
        return _refuse(usage.Refusal("intake_limit", usage.MESSAGES["intake_limit"], 429, 600))

    step = StepUsage()
    try:
        facts, questions = await asyncio.gather(
            intake(request.narrative, step),
            clarifying_questions(request.narrative, step, settings.max_clarifying_questions),
        )
    except Exception as exc:  # noqa: BLE001 - the UI then runs the case without questions
        logger.exception("intake check failed")
        return JSONResponse({"error": "intake_failed", "message": f"{type(exc).__name__}: {exc}"}, status_code=502)
    finally:
        await store.record_intake(now, step.metrics()["cost_usd"])
    return JSONResponse({"facts": facts.model_dump(), "questions": questions, "metrics": step.metrics()})


@app.post("/api/documents/tutela.{fmt}", response_model=None)
def tutela_document(fmt: Literal["docx", "pdf"], request: DocumentRequest) -> Response:
    """The draft as a .docx or PDF file. Deterministic (no LLM), so it works for recorded runs too."""
    if len(request.model_dump_json()) > MAX_DOCUMENT_JSON_CHARS:
        return JSONResponse({"error": "too_large", "message": "The draft is too long to export."}, status_code=413)
    sections = TutelaSections(**request.model_dump(include=set(TutelaSections.model_fields)))
    blocks = document.tutela_blocks(request.accionada, request.ciudad, sections)
    content = document.render_docx(blocks) if fmt == "docx" else document.render_pdf(blocks)
    slug = re.sub(r"[^a-z0-9]+", "-", document.ascii_fold(request.accionada).lower()).strip("-") or "eps"
    return Response(
        content,
        media_type=DOCUMENT_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="tutela-{slug}.{fmt}"'},
    )
