# Deploying Amparo

What is deployed, and the exact commands used (October 2026).

| Part | Where | URL |
| --- | --- | --- |
| Backend (FastAPI) | Google Cloud Run, `us-central1`, project `amparo-hackathon-791d7a` | https://amparo-api-945117299883.us-central1.run.app |
| Frontend (Next.js) | Vercel project `amparo`, root `frontend/` | https://amparo-swart.vercel.app |
| Usage counters | Firestore (native mode, `us-central1`), collection `amparo_usage` | |

Commands use bash syntax (Git Bash works on Windows) and run from the repo root.

## 1. Google Cloud project

A dedicated gcloud configuration keeps this project apart from any other default project:

```bash
gcloud config configurations create amparo --no-activate
export CLOUDSDK_ACTIVE_CONFIG_NAME=amparo      # use the amparo configuration in this shell only
gcloud config set account YOUR_ACCOUNT
gcloud config set run/region us-central1

export PROJECT_ID=amparo-hackathon-791d7a
gcloud projects create "$PROJECT_ID" --name="Amparo"
gcloud config set project "$PROJECT_ID"
gcloud billing accounts list
gcloud billing projects link "$PROJECT_ID" --billing-account=BILLING_ACCOUNT_ID

gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com billingbudgets.googleapis.com
```

Budget with alerts at 50 %, 90 % and 100 %. A budget must use the billing account's currency; ours is in
COP, so USD 5 was set as COP 20,000:

```bash
gcloud billing budgets create --billing-account=BILLING_ACCOUNT_ID \
  --display-name="Amparo ~USD 5 (COP 20000)" --budget-amount=20000COP \
  --filter-projects="projects/$PROJECT_ID" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

Firestore for the usage counters, with a TTL policy that deletes the per-IP documents (they carry `expire_at`):

```bash
gcloud firestore databases create --location=us-central1 --type=firestore-native
gcloud firestore fields ttls update expire_at --collection-group=amparo_usage --enable-ttl --async
```

## 2. Secrets and service account

API keys are read from the local `.env` and piped, never typed on the command line. The IP-hashing key is random:

```bash
grep '^NEBIUS_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n' | gcloud secrets create nebius-api-key --data-file=-
grep '^TAVILY_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n' | gcloud secrets create tavily-api-key --data-file=-
python -c "import secrets,sys;sys.stdout.write(secrets.token_hex(32))" | gcloud secrets create ip-hash-key --data-file=-
```

The service runs as a dedicated service account that can only read those secrets and use Firestore:

```bash
gcloud iam service-accounts create amparo-api --display-name="Amparo API (Cloud Run)"
SA="amparo-api@$PROJECT_ID.iam.gserviceaccount.com"
for s in nebius-api-key tavily-api-key ip-hash-key; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role=roles/datastore.user --condition=None
```

A new service account can take a few seconds to become visible to IAM; retry a binding that fails with
"does not exist". To rotate a key: `... | gcloud secrets versions add nebius-api-key --data-file=-`.

## 3. Backend on Cloud Run

```bash
gcloud run deploy amparo-api --source backend --region us-central1 --allow-unauthenticated \
  --service-account "$SA" \
  --set-secrets NEBIUS_API_KEY=nebius-api-key:latest,TAVILY_API_KEY=tavily-api-key:latest,IP_HASH_KEY=ip-hash-key:latest \
  --set-env-vars "^@^MODEL_FAST=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B@MODEL_DRAFT=nvidia/nemotron-3-super-120b-a12b@MODEL_REASONING=nvidia/Nemotron-3-Ultra-550b-a55b@USAGE_STORE=firestore@FIRESTORE_PROJECT=$PROJECT_ID@FRONTEND_ORIGIN=http://localhost:3000" \
  --timeout 300 --max-instances 2 --min-instances 0 --memory 512Mi
```

- `--source backend` builds `backend/Dockerfile` with Cloud Build (`backend/.gcloudignore` keeps `.venv` out of the upload).
- `^@^` switches the env-var separator to `@`, so `FRONTEND_ORIGIN` can hold a comma-separated list.
- `--timeout 300` covers a whole case (about 40 to 70 s); `--max-instances 2` with at most 2 running cases per instance caps concurrency at 4.
- Model IDs and every limit are configuration (see `.env.example`); change them with `--update-env-vars`, not in code.

Checks run against the public URL:

```bash
API_URL=https://amparo-api-945117299883.us-central1.run.app
curl "$API_URL/health"        # {"status":"ok"}
curl "$API_URL/api/usage"     # today's cases, estimated spend and limits (no personal data)
```

SSE streams unbuffered through Cloud Run: each event arrived when its step finished (response
`text/event-stream`, `transfer-encoding: chunked`; e.g. intake at 3.2 s, research at 17.3 s, done at 38.6 s).

## 4. Frontend on Vercel

```bash
cd frontend
npx vercel@latest link --yes --project amparo
printf '%s' "$API_URL" | npx vercel@latest env add NEXT_PUBLIC_API_URL production
npx vercel@latest --prod --yes
```

Vercel CLI 39 is rejected by the deploy endpoint (it requires 47.2.2 or later), hence `npx vercel@latest`.
`NEXT_PUBLIC_API_URL` is inlined at build time, so redeploy after changing it. The demo cases and the
recorded sample runs (`src/data/samples/`) are committed, so the build does not need `docs/`.

Then allow the Vercel domains on the API:

```bash
gcloud run services update amparo-api --region us-central1 \
  --update-env-vars "^@^FRONTEND_ORIGIN=https://amparo-swart.vercel.app,https://amparo-xamir-castelblancos-projects.vercel.app,http://localhost:3000"
```

## 5. Operating the public demo

All limits are environment variables on the Cloud Run service; counters are per UTC day in Firestore.

| Variable | Value | Effect when reached |
| --- | --- | --- |
| `RATE_LIMIT_PER_HOUR` / `RATE_LIMIT_PER_DAY` | 3 / 10 | cases per client IP (hashed with `IP_HASH_KEY`) |
| `DAILY_CASE_LIMIT` | 150 | cases per day, all clients |
| `DAILY_COST_LIMIT_USD` | 2 | estimated LLM spend per day; live runs stop until 00:00 UTC |
| `MAX_CONCURRENT_CASES` | 2 | running cases per instance; others get "try again in a minute" |
| `MAX_NARRATIVE_CHARS` / `MAX_TAVILY_SEARCHES_PER_CASE` | 4000 / 10 | input size and search budget |
| `LIVE_RUNS_ENABLED` | true | kill switch: `false` leaves only the recorded sample runs |

```bash
# Emergency stop (and back on)
gcloud run services update amparo-api --region us-central1 --update-env-vars LIVE_RUNS_ENABLED=false
gcloud run services update amparo-api --region us-central1 --update-env-vars LIVE_RUNS_ENABLED=true
```

A limit was verified end to end by setting `DAILY_CASE_LIMIT=1` after 3 cases: the API answered
429 `daily_case_limit` and the UI explained it and offered the sample run; the value was then restored to 150.

Re-record the sample runs after changing the pipeline:

```bash
uv run --project backend python scripts/run_case.py --all --quiet --record frontend/src/data/samples
```

## Local Docker check

```bash
docker build -t amparo-api:local backend
docker run --rm -p 8080:8080 --env-file .env amparo-api:local
curl localhost:8080/health
```
