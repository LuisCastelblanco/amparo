import json
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.pipeline import run_case

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("amparo.api")

app = FastAPI(title="Amparo API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class CaseRequest(BaseModel):
    narrative: str = Field(min_length=20, max_length=20_000)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(narrative: str) -> AsyncIterator[str]:
    try:
        async for event, data in run_case(narrative):
            yield _sse(event, data)
    except Exception as exc:  # noqa: BLE001 - never break the stream without telling the client
        logger.exception("pipeline crashed outside a step")
        yield _sse("case_failed", {"failed_step": None, "error": f"{type(exc).__name__}: {exc}"})


@app.post("/api/cases/run")
async def run_case_endpoint(request: CaseRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream(request.narrative),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
