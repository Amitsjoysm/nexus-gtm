# NEXUS GTM — production image (multi-stage).
#
# Stage 1 builds the React SPA; stage 2 is a slim Python runtime serving the API,
# the built SPA (FastAPI static + SPA fallback), and the worker (same image,
# different command — see docker-compose.prod.yml).
#
# Build from the repository root:  docker build -f deploy/Dockerfile -t nexus-gtm .

# ---- Stage 1: frontend build ------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci
COPY frontend ./frontend
# vite emits to ../nexus/web/dist relative to frontend/ (see vite.config.ts)
RUN mkdir -p nexus/web && cd frontend && npm run build

# ---- Stage 2: python runtime --------------------------------------------------
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Install the app + production extras (Postgres driver, Redis/Valkey queue, Alembic, metrics).
COPY pyproject.toml ./
COPY nexus ./nexus
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir ".[postgres,redis,migrate,metrics]"

# The compiled SPA from stage 1 (served by FastAPI with SPA fallback).
COPY --from=frontend /src/nexus/web/dist ./nexus/web/dist
COPY scripts ./scripts
COPY deploy/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Non-root runtime user.
RUN useradd --create-home --uid 10001 nexus
USER nexus

EXPOSE 8000
# The entrypoint migrates the DB to head (when NEXUS_RUN_MIGRATIONS=1, set on the app service)
# then execs the command below. The worker overrides the command in compose (and leaves
# NEXUS_RUN_MIGRATIONS unset, so it never races the app on migrations).
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "nexus.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
