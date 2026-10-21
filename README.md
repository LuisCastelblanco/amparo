# Amparo

An AI agent that helps people in Colombia draft an *acción de tutela* when their EPS denies a health service. Powered by NVIDIA Nemotron models on Nebius Token Factory and Tavily search.

> Amparo is a drafting aid, not legal advice.

## How it works

1. **Intake**: extract the facts from the person's own narrative (Nemotron Nano).
2. **Legal analysis**: rights at stake, admissibility, urgency, search queries (Nemotron Ultra).
3. **Case-law search**: Constitutional Court rulings via Tavily, screened for relevance (Nano).
4. **Drafting**: the tutela in Spanish, citing only the rulings found (Nemotron Super).
5. **Citation check**: regex verification of every cited ruling against the sources, plus a coherence review (Ultra).
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
