# NEXUS GTM API image. Minimal, non-root, production-oriented.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
COPY nexus ./nexus
# Install the package plus the production extras (Postgres + Redis).
RUN pip install --upgrade pip && pip install ".[postgres,redis]"

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 nexus
USER nexus

EXPOSE 8000

# Container-level liveness check hitting the app's health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "nexus.main:app", "--host", "0.0.0.0", "--port", "8000"]
