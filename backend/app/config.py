"""Application settings loaded from environment variables (and an optional .env file).

Model IDs are configuration, never hardcoded in business logic.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# Estimated USD price per 1M tokens as (input, output). Override with the
# MODEL_PRICING env var (JSON). Used only for cost estimates in logs.
DEFAULT_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    tavily_api_key: str = ""

    model_fast: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    model_draft: str = "nvidia/nemotron-3-super-120b-a12b"
    model_reasoning: str = "nvidia/Nemotron-3-Ultra-550b-a55b"
    model_fallback: str = ""

    model_pricing: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: dict(DEFAULT_MODEL_PRICING)
    )

    # Nemotron "thinking" per tier, toggled via chat_template_kwargs.enable_thinking.
    thinking_fast: bool = False
    thinking_draft: bool = False
    thinking_reasoning: bool = True

    frontend_origin: str = "http://localhost:3000"

    # Intake grounding: share of a descriptive field's content words (denied service, special condition)
    # that must appear in the narrative, in some form, to accept it without escalating.
    grounding_min_overlap: float = 0.75
    # The special condition is usually inferred and paraphrased; a lower bar still rejects invented ones.
    grounding_min_overlap_condition: float = 0.5

    llm_max_retries: int = 3
    llm_timeout_seconds: float = 300.0

    # --- Usage control for the public demo ---
    # false: the API refuses live runs and the UI only offers the recorded sample runs.
    live_runs_enabled: bool = True
    rate_limit_per_hour: int = 3  # cases per client IP
    rate_limit_per_day: int = 10  # cases per client IP (UTC day)
    daily_case_limit: int = 150  # cases per UTC day, all clients
    daily_cost_limit_usd: float = 2.0  # estimated LLM spend per UTC day, all clients
    max_concurrent_cases: int = 2  # per instance
    max_narrative_chars: int = 4000
    # Clarifying-question checks (intake only, fast tier) per client IP per hour, per instance.
    intake_rate_limit_per_hour: int = 10
    max_clarifying_questions: int = 3
    max_tavily_searches_per_case: int = 10
    # "firestore" keeps counters across restarts and instances; "memory" is for local development.
    usage_store: str = "memory"
    firestore_project: str = ""  # empty: the project of the default credentials
    firestore_collection: str = "amparo_usage"
    # Secret key for hashing client IPs (HMAC-SHA256); IPs are never stored in clear.
    ip_hash_key: str = ""
    # Number of proxies in front of the app that append to X-Forwarded-For (Cloud Run: 1).
    trusted_proxy_hops: int = 1

    def model_for_tier(self, tier: str) -> str:
        models = {
            "fast": self.model_fast,
            "draft": self.model_draft,
            "reasoning": self.model_reasoning,
        }
        if tier not in models:
            raise ValueError(f"Unknown tier {tier!r}; expected one of {sorted(models)}")
        return models[tier]

    def thinking_for_tier(self, tier: str) -> bool:
        return {"fast": self.thinking_fast, "draft": self.thinking_draft, "reasoning": self.thinking_reasoning}[tier]

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_origin.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
