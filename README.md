# Cats — Backend

FastAPI service for the Cats app: cat sighting catalog, Re-ID matching, ownership
claims, Explorer feed, and Claude vision recognition. The Expo/React Native
client lives in a separate repository.

## Layout

```
api/app/          FastAPI application
  routers/        HTTP endpoints
  models/         SQLAlchemy ORM models
  schemas/        Pydantic request/response models
  services/       storage (local/S3), vision, reconcile, auth, push
  utils/          matching, rarity, territory
api/tests/        pytest suite
api/Dockerfile    container image (build from repo root)
requirements.txt  Python dependencies
scripts/          AWS bootstrap
prompts/DEPLOY.md deployment runbook
```

## Run locally

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
cp api/.env.example api/.env                        # fill in ANTHROPIC_API_KEY etc.
cd api && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Defaults give SQLite + local-disk media. Set `DATABASE_URL` to a Postgres URL
and `S3_BUCKET` to enable Postgres + S3.

## Test

```bash
cd api && python -m pytest -q
```

## Deploy

Pushing to `main` runs `.github/workflows/api.yml`: pytest → build image → push
to ECR → trigger an AWS App Runner deployment. See [prompts/DEPLOY.md](prompts/DEPLOY.md)
for the GitHub secrets, one-time AWS setup, and `scripts/aws-bootstrap.sh`.

## Configuration

All settings come from environment variables (see `api/app/config.py` and
`api/.env.example`). Key switches:

| Var | Effect |
| --- | --- |
| `DATABASE_URL` | empty → SQLite; `postgresql://…` → Postgres |
| `S3_BUCKET` | empty → local disk; set → S3 media storage |
| `MEDIA_BASE_URL` | CDN domain for media URLs (else presigned S3) |
