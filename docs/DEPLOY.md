# Deploying Amparo

- **Backend**: FastAPI on Google Cloud Run (`us-central1`), API keys in Secret Manager.
- **Frontend**: Next.js on Vercel, root directory `frontend/`.

Commands use bash syntax (Git Bash works on Windows). Run them from the repo root.

## 1. Backend on Cloud Run

### One-time setup

```bash
gcloud auth login
export PROJECT_ID=your-project-id       # a project dedicated to Amparo
export REGION=us-central1
gcloud config set project "$PROJECT_ID"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

Store the API keys in Secret Manager (values are read from your local `.env`, never typed on the command line):

```bash
grep '^NEBIUS_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n' | \
  gcloud secrets create nebius-api-key --data-file=-
grep '^TAVILY_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n' | \
  gcloud secrets create tavily-api-key --data-file=-
```

Let the Cloud Run runtime service account read them:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
for s in nebius-api-key tavily-api-key; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

To rotate a key later: `... | gcloud secrets versions add nebius-api-key --data-file=-`.

### Deploy

```bash
gcloud run deploy amparo-api \
  --source backend \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-secrets NEBIUS_API_KEY=nebius-api-key:latest,TAVILY_API_KEY=tavily-api-key:latest \
  --set-env-vars "^@^MODEL_FAST=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B@MODEL_DRAFT=nvidia/nemotron-3-super-120b-a12b@MODEL_REASONING=nvidia/Nemotron-3-Ultra-550b-a55b@FRONTEND_ORIGIN=http://localhost:3000" \
  --timeout 600 \
  --memory 512Mi \
  --max-instances 3
```

- `--source backend` builds `backend/Dockerfile` with Cloud Build. Cloud Run sets `PORT`.
- `^@^` switches the env-var separator to `@`, so `FRONTEND_ORIGIN` can hold a comma-separated list later.
- `--timeout 600` keeps the SSE stream open for the whole pipeline (about 30 to 60 s per case).
- Model IDs are configuration: change them here, not in code.

Check it:

```bash
API_URL=$(gcloud run services describe amparo-api --region "$REGION" --format='value(status.url)')
curl "$API_URL/health"     # {"status":"ok"}
```

## 2. Frontend on Vercel

```bash
cd frontend
vercel login
vercel link                                   # create the project; root directory is frontend/
vercel env add NEXT_PUBLIC_API_URL production # paste the Cloud Run URL ($API_URL)
vercel --prod
```

`NEXT_PUBLIC_API_URL` is inlined at build time, so redeploy (`vercel --prod`) after changing it. The demo cases are copied from `docs/demo_cases/` at build time; if Vercel cannot see `docs/`, the committed `src/data/demoCases.json` is used.

## 3. Allow the Vercel origin on the API

```bash
gcloud run services update amparo-api --region "$REGION" \
  --update-env-vars "^@^FRONTEND_ORIGIN=https://your-app.vercel.app,http://localhost:3000"
```

## Local Docker check

```bash
docker build -t amparo-api:local backend
docker run --rm -p 8080:8080 --env-file .env amparo-api:local
curl localhost:8080/health
```
