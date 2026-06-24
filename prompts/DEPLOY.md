# Deployment runbook

How the Cats app ships. Two independent, path-filtered pipelines:

| Side | Path trigger | Pipeline | Target |
| --- | --- | --- | --- |
| Backend (`api/`) | `api/**`, `requirements.txt` | `.github/workflows/api.yml` | ECR → **AWS App Runner** |
| Frontend (`app/`) | `app/**` | `.github/workflows/app.yml` | **EAS Build** → TestFlight |

```
                       push to main
                            │
          ┌─────────────────┴──────────────────┐
       api/** changed                      app/** changed
          │                                     │
   pytest → docker build               eas build --profile preview
   → push ECR → App Runner             (production = manual, auto-submit
   deploy                               to TestFlight)
          │                                     │
   api.catapp.uk            internal testers / App Store
```

A normal release is just `git push` to `main`. Only the side you touched rebuilds.

---

## Domains

| Host | Serves |
| --- | --- |
| `api.catapp.uk` | Backend API (App Runner custom domain) |
| `media.catapp.uk` | Media CDN (CloudFront → S3), optional |

---

## GitHub repo secrets

Settings → Secrets and variables → Actions:

| Secret | Used by | What |
| --- | --- | --- |
| `EXPO_TOKEN` | `app.yml` | expo.dev access token (Account → Access tokens) |
| `AWS_ACCESS_KEY_ID` | `api.yml` | IAM user with ECR push + App Runner deploy |
| `AWS_SECRET_ACCESS_KEY` | `api.yml` | — |
| `APPRUNNER_SERVICE_ARN` | `api.yml` | ARN of the App Runner service |

> Hardening: swap the static AWS keys for GitHub OIDC role auth later. The
> deploy job already requests `id-token: write`.

---

## One-time AWS setup

The pipelines assume this infrastructure already exists.

1. **ECR repository**
   ```bash
   aws ecr create-repository --repository-name cats-api --region us-east-1
   ```

2. **Postgres** — default is a managed provider ([Neon](https://neon.tech) free
   tier: $0, no VPC setup, standard Postgres so no lock-in). Create a project,
   copy its `postgresql://...` string into `DATABASE_URL`. To use RDS instead,
   run the bootstrap with `CREATE_RDS=true` (provisions RDS + a VPC connector,
   ~$15/mo). The app's `create_all` builds the schema on first boot; Alembic is
   the planned follow-up for versioned migrations.

3. **S3 bucket** for media
   ```bash
   aws s3 mb s3://cats-media-prod --region us-east-1
   ```
   (Optional) Put **CloudFront** in front and point `media.catapp.uk`
   at it; set `MEDIA_BASE_URL` to that domain. Without a CDN the backend serves
   presigned S3 URLs instead.

4. **App Runner service**
   - Source: ECR `cats-api:latest`, with an ECR access role.
   - Port `8000`, health check path `/health`.
   - Custom domain `api.catapp.uk`.
   - Set the environment variables below (secrets via Secrets Manager).
   - Copy its ARN into the `APPRUNNER_SERVICE_ARN` GitHub secret.

5. **Route 53** — `api.` → App Runner custom domain; `media.` → CloudFront.

---

## Backend environment variables

Set on the App Runner service. Names map to `api/app/config.py` (pydantic
reads them case-insensitively).

| Var | Prod value | Notes |
| --- | --- | --- |
| `DATABASE_URL` | Neon `postgresql://…?sslmode=require` (or RDS) | empty/default → local SQLite |
| `SECRET_KEY` | random 32-byte hex | `python -c "import secrets;print(secrets.token_hex(32))"` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` | 7 days |
| `S3_BUCKET` | `cats-media-prod` | **empty → local disk storage** |
| `S3_REGION` | `us-east-1` | |
| `MEDIA_BASE_URL` | `https://media.catapp.uk` | empty → presigned S3 URLs |
| `STORAGE_RECONCILE_ENABLED` | `true` | background DB↔bucket sweep |
| `STORAGE_RECONCILE_INTERVAL_HOURS` | `6` | |
| `STORAGE_ORPHAN_GRACE_HOURS` | `24` | don't sweep uploads younger than this |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Claude vision |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | |

---

## EAS / TestFlight

`API_BASE` is injected per build profile (`app/eas.json` → `app.config.js` →
`constants/api.ts`):

| Profile | `API_BASE` | Use |
| --- | --- | --- |
| `development` | LAN fallback / `app/.env` | dev client + Metro |
| `preview` | `api.catapp.uk` | internal testers |
| `production` | `api.catapp.uk` | App Store / TestFlight |

- `production` builds need Apple App Store Connect credentials configured in EAS
  (`eas.json` → `submit.production`) for the workflow's `--auto-submit` to work.
- Backend changes need **no** app rebuild — the app targets a stable URL.

---

## Migrating existing dev photos to S3

The 28 local files under `api/uploads/` are not in the bucket. One-time copy:

```bash
aws s3 cp api/uploads/ s3://cats-media-prod/uploads/ --recursive
```

DB rows already store backend-independent keys (`uploads/<uuid>.jpg`), so no row
changes are needed — only the bytes move.

---

## Local development

Nothing here is required locally. Defaults give SQLite + local-disk media:

```bash
# backend (from api/)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# point at Postgres instead (PowerShell)
$env:DATABASE_URL = "postgresql://postgres:cats@localhost:5432/cats"
```

Run the backend tests:

```bash
cd api && python -m pytest -q
```
