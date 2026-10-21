"""Smoke test for Nebius Token Factory and Tavily connectivity.

Run from the repo root:
    uv run --project backend python scripts/smoke_test.py
"""

import difflib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.llm import chat, get_client  # noqa: E402
from app.search import search_jurisprudence  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

TIERS = ("fast", "draft", "reasoning")


def header(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def closest(model_id: str, candidates: list[str]) -> str | None:
    lowered = {c.lower(): c for c in candidates}
    if model_id.lower() in lowered:
        return lowered[model_id.lower()]
    match = difflib.get_close_matches(model_id.lower(), list(lowered), n=1, cutoff=0.0)
    return lowered[match[0]] if match else None


def check_models() -> bool:
    header("a) Available NVIDIA models (GET /v1/models)")
    settings = get_settings()
    start = time.perf_counter()
    ids = sorted(m.id for m in get_client().models.list())
    print(f"{len(ids)} models total ({time.perf_counter() - start:.2f}s)")
    nvidia = [i for i in ids if "nvidia" in i.lower() or "nemotron" in i.lower()]
    for model_id in nvidia:
        print(f"  - {model_id}")

    ok = True
    configured = {
        "MODEL_FAST": settings.model_fast,
        "MODEL_DRAFT": settings.model_draft,
        "MODEL_REASONING": settings.model_reasoning,
    }
    if settings.model_fallback:
        configured["MODEL_FALLBACK"] = settings.model_fallback
    print("\nConfigured models:")
    for name, model_id in configured.items():
        if model_id in ids:
            print(f"  [OK]      {name} = {model_id}")
            continue
        ok = False
        suggestion = closest(model_id, nvidia or ids)
        if suggestion and suggestion.lower() == model_id.lower():
            print(f"  [CASE]    {name} = {model_id} -> exact ID differs in case: {suggestion}")
        else:
            print(f"  [MISSING] {name} = {model_id} -> closest available: {suggestion}")
    return ok


def check_tiers() -> None:
    header("b) One short call per tier")
    messages = [{"role": "user", "content": "Reply with one short sentence: what is an acción de tutela?"}]
    for tier in TIERS:
        try:
            r = chat(tier, messages, max_tokens=1024)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"[{tier}] FAILED: {type(exc).__name__}: {exc}")
            continue
        cost = f"${r.cost_usd:.6f}" if r.cost_usd is not None else "unknown"
        print(
            f"[{tier}] model={r.model} latency={r.latency_s:.2f}s "
            f"tokens in/out={r.input_tokens}/{r.output_tokens} cost={cost} fallback={r.used_fallback}"
        )
        print(f"    -> {r.content.strip()[:300]!r}")


def check_structured() -> None:
    header("c) Structured output (JSON schema) with tier fast")
    schema = {
        "type": "object",
        "properties": {
            "service_denied": {"type": "string"},
            "eps_name": {"type": "string"},
            "is_urgent": {"type": "boolean"},
        },
        "required": ["service_denied", "eps_name", "is_urgent"],
        "additionalProperties": False,
    }
    messages = [
        {"role": "system", "content": "Extract the fields from the user's message as JSON."},
        {
            "role": "user",
            "content": "Mi EPS Salud Ejemplo me negó la insulina glargina que me formuló el médico "
            "y se me acaba en dos días.",
        },
    ]
    try:
        r = chat(
            "fast",
            messages,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "intake", "schema": schema, "strict": True},
            },
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return
    print(f"model={r.model} latency={r.latency_s:.2f}s tokens in/out={r.input_tokens}/{r.output_tokens}")
    print(f"raw: {r.content!r}")
    try:
        parsed = json.loads(r.content)
        missing = [k for k in schema["required"] if k not in parsed]
        print(f"parsed OK: {parsed}" + (f" (missing keys: {missing})" if missing else ""))
    except json.JSONDecodeError as exc:
        print(f"NOT valid JSON: {exc}")


def check_tavily() -> None:
    header("d) Tavily search on corteconstitucional.gov.co")
    query = "tutela suministro de medicamentos EPS derecho a la salud"
    start = time.perf_counter()
    try:
        results = search_jurisprudence(query)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return
    print(f"query={query!r} results={len(results)} latency={time.perf_counter() - start:.2f}s")
    for i, r in enumerate(results[:3], 1):
        print(f"\n{i}. {r.title}\n   {r.url}\n   {r.content[:300]!r}")


def main() -> None:
    settings = get_settings()
    print(f"Base URL: {settings.nebius_base_url}")
    check_models()
    check_tiers()
    check_structured()
    check_tavily()


if __name__ == "__main__":
    main()
