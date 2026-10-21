# Amparo

An AI agent that helps people in Colombia draft an *acción de tutela* when their EPS denies a health service. Powered by NVIDIA Nemotron models on Nebius Token Factory and Tavily search.

> Amparo is a drafting aid, not legal advice.

Live demo: https://amparo-swart.vercel.app (rate-limited; "Watch a sample run" replays a recorded run without spending tokens). Judges: see [docs/TESTING.md](docs/TESTING.md).

## How it works

0. **Clarifying questions** (optional): if the story misses something critical (EPS, what was denied, a prescription, what the EPS did), up to 3 questions in the UI; answers are optional (Nemotron Super).
1. **Intake**: extract the facts, including the city, from the person's own narrative (Nemotron Nano). A deterministic check escalates to Nemotron Super when the EPS, the dates or the denied service are not in the narrative (names and dates match exactly, descriptions may be paraphrased); an invented city or condition is set to null without a model call.
2. **Legal analysis**: rights at stake, admissibility, urgency, search queries (Nemotron Ultra).
3. **Case-law search**: Constitutional Court rulings via Tavily (parallel queries), screened for relevance (Nano); ruling pages are fetched with their real charset to restore the accents Tavily drops.
4. **Drafting**: the tutela in Spanish, citing only the rulings found (Nemotron Super), in the usual Colombian format with [COMPLETAR] fields for personal data.
5. **Citation check**: every cited ruling is checked against the sources; unverified ones get a targeted Tavily search, or their paragraph is rewritten without them (Super), up to 2 iterations, so no unverified citation reaches the document. Plus a coherence review (Ultra).
6. **Delivery**: annex checklist and filing guidance (Super); the official online filing channel for the person's city, searched with Tavily on .gov.co sites (every link or e-mail shown comes from a source found); an optional English summary for reviewers (Nano).

The document downloads as .docx or PDF (generated on the API, no LLM). The results panel shows which model resolved each step and the cost against running every call on Ultra.

## Quickstart

```bash
cp .env.example .env          # add NEBIUS_API_KEY and TAVILY_API_KEY
cd backend && uv sync && cd ..
uv run --project backend python scripts/smoke_test.py          # check APIs and model IDs
uv run --project backend python scripts/run_case.py --all      # run the 3 synthetic demo cases

# API on :8000 (POST /api/cases/intake, POST /api/cases/run as Server-Sent Events,
# POST /api/documents/tutela.docx|pdf, GET /api/usage)
uv run --project backend --directory backend uvicorn app.main:app --reload

# Web UI on :3000
cd frontend && cp .env.example .env.local && npm install && npm run dev
```

Deployment (Cloud Run + Vercel): see [docs/DEPLOY.md](docs/DEPLOY.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
