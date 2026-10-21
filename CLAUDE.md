# Amparo

Amparo is an AI agent that helps people in Colombia draft an *acción de tutela* when their EPS (health insurer) denies a health service. Built for the Nebius x NVIDIA Global AI Hackathon (track: Best Apps and Agents). Runtime LLM calls go to Nebius Token Factory (OpenAI-compatible API, NVIDIA Nemotron models); case-law search goes to the Tavily API.

## Flow

1. **Intake**: collect the user's facts (service denied, EPS, diagnosis, urgency, prior requests).
2. **Legal analysis**: identify the fundamental rights at stake and the applicable legal grounds.
3. **Case-law search**: query Constitutional Court jurisprudence with Tavily.
4. **Drafting**: write the tutela document.
5. **Citation verification**: check every cited ruling against the search results.
6. **Delivery**: hand the final document to the user.

## Layout

- `backend/`: Python 3.12, FastAPI, dependencies managed with `uv` (`uv sync`, `uv run ...`).
  - `app/config.py`: settings via pydantic-settings.
  - `app/llm.py`: single Token Factory client; `chat(tier, messages, response_format=None)`.
  - `app/search.py`: Tavily client; `search_jurisprudence(query)`.
  - `app/main.py`: FastAPI app.
- `frontend/`: web UI (not built yet).
- `scripts/`: tooling, e.g. `uv run --project backend python scripts/smoke_test.py`.
- `docs/`: documentation.

## Rules

- Code, comments, README and UI are in English; the legal document the user receives is in Spanish.
- Never commit secrets. Keys live in `.env` (gitignored); `.env.example` lists the variables.
- Model IDs come only from configuration (`MODEL_FAST`, `MODEL_DRAFT`, `MODEL_REASONING`, `MODEL_FALLBACK`). Never hardcode them in logic; select by tier.
- The product is a drafting aid, not legal advice. Say so in the UI and in the delivered document.
- Synthetic data only. Never use real patient data.
