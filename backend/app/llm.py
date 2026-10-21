"""Single LLM client for Nebius Token Factory (OpenAI-compatible API)."""

import logging
import random
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

import openai
from openai import OpenAI

from app.config import get_settings

logger = logging.getLogger("amparo.llm")

Tier = Literal["fast", "draft", "reasoning"]

_RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)
_DEPRECATION_MARKERS = ("deprecat", "decommission", "no longer available", "not found", "does not exist")


@dataclass
class ChatResult:
    content: str
    model: str
    tier: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: float | None
    used_fallback: bool
    raw: Any


@lru_cache
def get_client() -> OpenAI:
    settings = get_settings()
    if not settings.nebius_api_key:
        raise RuntimeError("NEBIUS_API_KEY is not set")
    # Retries are handled in chat() so they can be logged and combined with fallback.
    return OpenAI(
        api_key=settings.nebius_api_key,
        base_url=settings.nebius_base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    )


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = get_settings().model_pricing.get(model)
    if prices is None:
        return None
    price_in, price_out = prices
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def _is_model_unavailable(exc: openai.APIStatusError) -> bool:
    if isinstance(exc, openai.NotFoundError):
        return True
    message = str(exc).lower()
    return exc.status_code in (400, 410, 422) and any(m in message for m in _DEPRECATION_MARKERS)


def _call_with_retries(model: str, messages: list[dict], **kwargs: Any) -> Any:
    max_retries = get_settings().llm_max_retries
    for attempt in range(max_retries + 1):
        try:
            return get_client().chat.completions.create(model=model, messages=messages, **kwargs)
        except _RETRYABLE as exc:
            if attempt == max_retries:
                raise
            delay = min(2**attempt, 20) + random.uniform(0, 0.5)
            logger.warning(
                "llm retry model=%s attempt=%d/%d error=%s sleeping=%.1fs",
                model, attempt + 1, max_retries, type(exc).__name__, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def chat(
    tier: Tier,
    messages: list[dict],
    response_format: dict | None = None,
    **kwargs: Any,
) -> ChatResult:
    """Run a chat completion using the model configured for `tier`.

    Falls back to MODEL_FALLBACK when the tier's model is missing or deprecated.
    """
    settings = get_settings()
    model = settings.model_for_tier(tier)
    if response_format is not None:
        kwargs["response_format"] = response_format

    used_fallback = False
    start = time.perf_counter()
    try:
        response = _call_with_retries(model, messages, **kwargs)
    except openai.APIStatusError as exc:
        fallback = settings.model_fallback
        if not (_is_model_unavailable(exc) and fallback and fallback != model):
            raise
        logger.warning("llm fallback tier=%s model=%s -> %s reason=%s", tier, model, fallback, exc)
        model, used_fallback = fallback, True
        start = time.perf_counter()
        response = _call_with_retries(model, messages, **kwargs)
    latency = time.perf_counter() - start

    usage = response.usage
    input_tokens = usage.prompt_tokens if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    served_model = response.model or model
    cost = estimate_cost(model, input_tokens, output_tokens)

    logger.info(
        "llm call tier=%s model=%s in_tokens=%d out_tokens=%d latency=%.2fs cost_usd=%s fallback=%s",
        tier, served_model, input_tokens, output_tokens, latency,
        f"{cost:.6f}" if cost is not None else "unknown", used_fallback,
    )

    return ChatResult(
        content=(response.choices[0].message.content or "").strip(),
        model=served_model,
        tier=tier,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_s=latency,
        cost_usd=cost,
        used_fallback=used_fallback,
        raw=response,
    )
