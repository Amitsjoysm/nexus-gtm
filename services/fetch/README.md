# nexus-fetch

A stateless fetcher for signal collection: it fetches one URL, or scrapes one search-results page,
and returns what it found. It stores nothing, so the VM can be rebuilt at any time — the shared
cache lives in the app's own database (`web_cache`). Code: `nexus/fetching/service.py`.

## Deploy

1. **Provision a small VM** (2 vCPU, 2–4 GB). Not the Reacher box: scraping egress must never share
   an IP with email verification, which billing depends on.
2. **Generate the secret** and keep it for step 5:

       export FETCH_TOKEN=$(openssl rand -hex 32)

3. **Start it** from a checkout of this repository:

       docker compose -f services/fetch/docker-compose.yml up -d --build

4. **Firewall:** allow TCP 8081 only from the app's egress addresses. Never expose it publicly — if
   anyone else can reach it, it is an open proxy.
5. **Point the app at it** (API and worker): `NEXUS_FETCH_SERVICE_URL=http://<vm-ip>:8081` and
   `NEXUS_FETCH_SERVICE_TOKEN=<the token>`. Add both to `deploy/.env` too, or a rebuild wipes them.

## Check it

    curl -s localhost:8081/health -H "X-Fetch-Token: $FETCH_TOKEN"
    curl -s -X POST localhost:8081/search -H "X-Fetch-Token: $FETCH_TOKEN" \
         -H 'content-type: application/json' -d '{"query":"vanta series c funding","limit":3}'
    curl -s -X POST localhost:8081/fetch -H "X-Fetch-Token: $FETCH_TOKEN" \
         -H 'content-type: application/json' -d '{"url":"https://www.vanta.com/pricing"}'

`/search` should return three hits with real URLs. **Run these after every rebuild**: the default
fetchers call Scrapling, which is not installed in the app's test suite, so this is the only place
its API is exercised end to end.

## What the answers mean

| Response | Meaning | What happens in the app |
|---|---|---|
| `200` with hits | Answered | Result cached and used |
| `200` with `[]` | The engine found nothing | Cached as a real "nothing" |
| `503 blocked` | The engine served an anti-bot page | Falls back to the paid provider; not cached |
| `502` | The fetch or scrape failed | Falls back to the paid provider; not cached |
| `401` | Wrong or missing token (every endpoint, `/health` included) | Check `NEXUS_FETCH_SERVICE_TOKEN` |
| `400 redirected somewhere not fetchable` | The page redirected to a private or metadata address | Refused by design; nothing from there is returned |
| `400 not fetchable` | Private, loopback, metadata or non-http target | Refused by design |

Page content is capped at `FETCH_MAX_CHARS` (default 2,000,000) and the response says
`"truncated": true` when it was cut — targets come from account domains that tenants control,
so one huge page must not fill a VM every tenant shares.

## Shadow comparisons

`NEXUS_SIGNAL_FETCH_SHADOW=true` makes the app ask this service and the paid engine for every
signal search, record both, and still show reps the paid answer. Shadow runs use their own cache
keys, so comparisons start immediately. Read them with
`python scripts/fetch_shadow_report.py --days 7`; the line that matters is ONLY PAID FOUND.
Shadow doubles search volume, so turn it off once the comparison has enough rows.

Occasional `503`s are normal. **Sustained** blocks mean the pacing is too aggressive for the volume,
or the VM's IP has been flagged — lower `FETCH_CONCURRENCY`, and check the cache hit rate in the app
before adding capacity.
