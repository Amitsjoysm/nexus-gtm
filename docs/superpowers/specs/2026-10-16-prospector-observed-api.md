# InfoJoy Search API: what the first real calls showed (2026-10-19)

Recorded per CLAUDE.md's rule that a provider's shape is not a contract until observed, from
`scripts/infojoy_probe.py` and a one-off diagnosis run inside the local deploy's app container
against the key stored in Provider keys. Every person-identifying value is redacted; the token was
never printed.

## Observed

| Call | Result |
|---|---|
| No `X-Api-Token` header | **401** `application/json`: `{"error":"Missing or invalid API token."}` |
| A wrong token | **401**, same body |
| The stored token, documented sample request (contacts: Titles + SeniorityLevels + Countries, PageSize 10) | **500, empty body**, no content type |
| The stored token, every other search tried (contacts and companies, with and without `IncludeTotalCount`, `HasDirectPhone` true/false, 20 location filter names, PageSize 1 to 50) | **500, empty body** |
| `PageSize: 501` | **400** `{"Errors": ["PageSize cannot be greater than 500."]}`, exactly as documented |
| Latency | first call **16.3 s** (a cold start); every later call 0.2-0.3 s |

So the token authenticates (a bad one is refused with a 401) and request validation runs, but the
search itself fails server-side for every query. **This is a provider-side fault**, reported to the
owner; the search is not usable until InfoJoy fixes it.

## Consequences in the code

- **Errors are as handled.** A 401/403 records the rejection and rotates keys; `{"Errors": [...]}`
  is kept as the provider's sentence (422); a 500 (empty body) is `unavailable`, answered as 503
  "The data provider failed (HTTP 500). Try again shortly." and never cached.
- **The read timeout is 30 s** (was 20 s): the observed cold start is 16 s, so the first search after
  the provider idles would otherwise time out.
- **Still unobserved, still provisional**: the success response's field names and casing, the total
  count field's name, the value vocabularies (seniority, department, size and revenue ranges), and
  which location set the contacts endpoint's documented location filters match (D15). The parser
  stays tolerant of casing and the enum fields stay free text until the probe can run against a
  working search; `fields.py` keeps the company-level location set disabled on the Contacts view
  until it is proven.

## To finish once the provider answers

1. Re-run `scripts/infojoy_probe.py` in the app container.
2. Record the success shape here; set `records.py` aliases, `fields.py` options and the location
   API names to what was observed; re-run the suite.
3. If the second location set's names cannot be proven by the probe, ask the owner for InfoJoy's
   field names rather than guess.
