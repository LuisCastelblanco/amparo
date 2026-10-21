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
  - `app/schemas.py`: pydantic models for each step's output.
  - `app/pipeline.py`: the six steps plus the `run_case` event generator.
  - `app/citations.py`: deterministic detection and normalization of ruling IDs (T-760/08).
  - `app/main.py`: FastAPI app; `POST /api/cases/run` streams Server-Sent Events.
- `frontend/`: Next.js (App Router, TypeScript, Tailwind) single-page UI. `NEXT_PUBLIC_API_URL` points to the API.
  This Next.js version differs from older ones; read `frontend/AGENTS.md` before changing it.
- `scripts/`: `smoke_test.py` (API check) and `run_case.py` (run demo cases), both via `uv run --project backend python scripts/...`.
- `docs/`: `demo_cases/` (synthetic cases, synced into the frontend at build time) and `DEPLOY.md`.

## Rules

- Code, comments, README and UI are in English; the legal document the user receives is in Spanish.
- Never commit secrets. Keys live in `.env` (gitignored); `.env.example` lists the variables.
- Model IDs come only from configuration (`MODEL_FAST`, `MODEL_DRAFT`, `MODEL_REASONING`, `MODEL_FALLBACK`). Never hardcode them in logic; select by tier.
- The product is a drafting aid, not legal advice. Say so in the UI and in the delivered document.
- Synthetic data only. Never use real patient data.
