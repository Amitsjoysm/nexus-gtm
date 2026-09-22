# nexus-fetch — stateless fetcher for signal collection. Runs on its own small VM, never beside the
# Reacher verifier: scraping egress must not share an IP with email verification.
#
# Build from the repository root:
#   docker build -f services/fetch/Dockerfile -t nexus-fetch .
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml constraints.txt README.md ./
COPY nexus ./nexus

# The HTTP tier (curl_cffi) needs nothing more; the browser tier needs Chromium and its system
# libraries, installed here so `mode=stealth` works without a rebuild.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -c constraints.txt ".[fetch]" \
    && scrapling install

ENV FETCH_CONCURRENCY=4
EXPOSE 8081
# One worker: the per-host pacing lives in the process, and two workers would each allow a request
# per host at once — doubling the burst the pacing exists to prevent.
CMD ["uvicorn", "nexus.fetching.service:build_app", "--factory", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
