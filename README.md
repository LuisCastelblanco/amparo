# Amparo

An AI agent that helps people in Colombia draft an *acción de tutela* when their EPS denies a health service. Powered by NVIDIA Nemotron models on Nebius Token Factory and Tavily search.

> Amparo is a drafting aid, not legal advice.

## Quickstart

```bash
cp .env.example .env          # add NEBIUS_API_KEY and TAVILY_API_KEY
cd backend && uv sync && cd ..
uv run --project backend python scripts/smoke_test.py
uv run --project backend --directory backend uvicorn app.main:app --reload   # GET /health
```

## License

Apache 2.0. See [LICENSE](LICENSE).
