# Amparo frontend

Single-page Next.js UI (App Router, TypeScript, Tailwind). It posts the user's narrative to the API and renders the pipeline's Server-Sent Events live.

```bash
cp .env.example .env.local   # NEXT_PUBLIC_API_URL, default http://localhost:8000
npm install
npm run dev                  # http://localhost:3000
```

`npm run dev` and `npm run build` first copy the synthetic demo cases from `../docs/demo_cases` into `src/data/demoCases.json`.
