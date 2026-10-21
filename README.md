# Amparo

An AI agent that helps people in Colombia draft an *acción de tutela* when their EPS denies a health service. Powered by NVIDIA Nemotron models on Nebius Token Factory and Tavily search.

> Amparo is a drafting aid, not legal advice.

Live demo: https://amparo-swart.vercel.app (rate-limited; "Watch a sample run" replays a recorded run without spending tokens).

## How it works

1. **Intake**: extract the facts from the person's own narrative (Nemotron Nano). A deterministic check that the service, EPS and dates appear in the narrative escalates to Nemotron Super when they do not.
2. **Legal analysis**: rights at stake, admissibility, urgency, search queries (Nemotron Ultra).
3. **Case-law search**: Constitutional Court rulings via Tavily (parallel queries), screened for relevance (Nano); ruling pages are fetched with their real charset to restore the accents Tavily drops.
4. **Drafting**: the tutela in Spanish, citing only the rulings found (Nemotron Super).
5. **Citation check**: every cited ruling is checked against the sources; unverified ones get a targeted Tavily search, or their paragraph is rewritten without them (Super), up to 2 iterations, so no unverified citation reaches the document. Plus a coherence review (Ultra).
6. **Delivery**: annex checklist and filing guidance (Nano).

## Quickstart

```bash
cp .env.example .env          # add NEBIUS_API_KEY and TAVILY_API_KEY
cd backend && uv sync && cd ..
uv run --project backend python scripts/smoke_test.py          # check APIs and model IDs
uv run --project backend python scripts/run_case.py --all      # run the 3 synthetic demo cases

# API on :8000 (GET /health, POST /api/cases/run as Server-Sent Events)
uv run --project backend --directory backend uvicorn app.main:app --reload

# Web UI on :3000
cd frontend && cp .env.example .env.local && npm install && npm run dev
```

Deployment (Cloud Run + Vercel): see [docs/DEPLOY.md](docs/DEPLOY.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
