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

# Apply the base image's outstanding security updates, and refresh the build tooling it ships.
#
# The tag floats but the layer does not: python:3.11-slim is rebuilt on its own cadence, so an
# image built today still carries whatever Debian security fixes landed after that rebuild. The
# first Trivy scan this repo ever actually ran found exactly that — util-linux and shadow at
# 2.41-5 with four fixable CVEs (CVE-2026-53612/53613/53614/53615), waiting in deb13u updates.
#
# pip/setuptools/wheel are the base image's own preinstalled build tooling, and they are what
# dragged in the `wheel` and `jaraco.context` findings. They are not used once the app is
# installed, but they stay in the image, so they are part of its attack surface either way.
#
# SECURITY_REFRESH exists because this layer is otherwise only as fresh as the build cache.
# CI restores layers with cache-from, so an unrelated edit further down the file leaves this
# one cached and the "upgrade" silently re-applies whatever was current when it last ran. That
# is not hypothetical: the day after this layer was added, Debian published an openssl fix
# (CVE-2026-14456, 3.5.6 -> 3.5.7) and the scan caught the image still on the old package,
# because a change to the pip layer below had not invalidated this one.
#
# CI passes the current date, so the cache turns over daily — matching Trivy's own database
# cadence — while repeat builds within a day still reuse it.
ARG SECURITY_REFRESH=0
RUN echo "security refresh: ${SECURITY_REFRESH}" \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

# Install the app + production extras (Postgres driver, Redis/Valkey queue, Alembic, metrics).
# constraints.txt pins exact versions (C-3) so a fresh build can't silently pull a breaking
# transitive (e.g. an incompatible starlette). Constraints only cap versions — they never change
# which packages the extras resolve.
COPY pyproject.toml constraints.txt ./
COPY nexus ./nexus
COPY migrations ./migrations
COPY alembic.ini ./
# pip is removed in the SAME layer that uses it, so its files never land in the image at all.
#
# It is build tooling: the entrypoint runs python, alembic and uvicorn, and nothing at runtime
# imports pip, pkg_resources or setuptools. Keeping it was costing two HIGH findings that no
# amount of pinning could reach — pip 26.2.1 (the current release) vendors msgpack 1.1.2
# (GHSA-6v7p-g79w-8964) and setuptools 70.3.0 (CVE-2025-47273) inside pip/_vendor, declared in
# its own vendor.txt. Those are pip's dependencies, not ours; the only real fix is not shipping
# a package manager in a production runtime.
#
# Deleting in a later RUN would not do: the files would still exist in the earlier layer and
# remain extractable from the image.
RUN pip install --no-cache-dir -c constraints.txt ".[postgres,redis,migrate,metrics]" \
    && python -m pip uninstall -y pip

# The compiled SPA from stage 1 (served by FastAPI with SPA fallback).
COPY --from=frontend /src/nexus/web/dist ./nexus/web/dist
COPY scripts ./scripts
COPY deploy/entrypoint.sh ./entrypoint.sh
# Strip any CRLF (Windows checkouts) so the shebang works in the Linux container, then make exec.
RUN sed -i 's/\r$//' ./entrypoint.sh && chmod +x ./entrypoint.sh

# Non-root runtime user.
RUN useradd --create-home --uid 10001 nexus
USER nexus

EXPOSE 8000
# The entrypoint migrates the DB to head (when NEXUS_RUN_MIGRATIONS=1, set on the app service)
# then execs the command below. The worker overrides the command in compose (and leaves
# NEXUS_RUN_MIGRATIONS unset, so it never races the app on migrations).
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "nexus.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
