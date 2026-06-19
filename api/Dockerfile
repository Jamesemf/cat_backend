# Backend image for the Cats FastAPI service.
#
# Build context is the REPO ROOT (requirements.txt lives there, code in api/):
#   docker build -f api/Dockerfile -t cats-api .
#
# All Python deps ship as binary wheels (psycopg2-binary, Pillow, bcrypt), so
# the slim base needs no compiler toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Install deps first so they cache across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code. uvicorn is imported via "app.main:app", so run from /srv/api.
COPY api/ ./api
WORKDIR /srv/api

EXPOSE 8000

# Single worker by default; scale horizontally with more container instances
# (the in-memory rate-limit/reconcile state assumes per-process, see code TODOs).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
