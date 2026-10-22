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

### Second diagnosis, 2026-10-19 about 08:04 UTC

Asked by the owner after "I add keys and no data appears". Run in the local deploy's app container
with the one stored key (38 characters, no whitespace, no quotes, no `Bearer` prefix):

| Call | Result |
|---|---|
| Both of the PDF's sample requests, sent byte for byte | **500, empty body** |
| The same with `Content-Type: application/json; charset=utf-8` + `Accept: application/json`, and with curl's headers | **500, empty body** |
| The smallest searches: `Filters: {}` and `{"Countries": ["United States"]}`, PageSize 1, both endpoints | **500, empty body** |
| `GET /swagger/v1/swagger.json`, `/swagger/index.html`, `/openapi/v1.json`, `/api`, `/` | 404 (no published schema) |
| Latency | the first two searches **15.5 s and 15.3 s** before failing; every later one 0.2-0.3 s |

15 s is the default connect timeout of Microsoft's SQL client, and failing instantly afterwards is
its connection-pool blocking period after a failed connect. The likeliest cause is the API being
unable to reach its own database. Our request matches the document (path, header, the four body
fields), so nothing on our side can make a search succeed.

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

## Third look, 2026-10-22: the provider answers

Measured on the local stack through the app's own client and `scripts/infojoy_probe.py`
(~300 records). Values are redacted in the probe's output; only names, types, fill rates and the
bounded vocabularies were read.

**Success shape.** `{"PageNumber", "PageSize", "Items"}` on both endpoints. **No total field is ever
returned**, with or without `IncludeTotalCount`.

**Contact item keys**: Title, FirstName, LastName, Email, Extension, EmployeeLinkedIn, DirectPhone,
Department, EmployeeFacebook, EmployeeTwitter, CompanyName, CompanyURL (a bare domain, 50 of 50),
Address, Country, State, City, ZipCode, TimeZone, Revenue, CompanyLinkedin, EmployeeSize,
Companytwitter, CompanyFacebook, SIC_Code, SubIndustry, IndustryType, About, RegionCode, Technology,
Keywords, Fax, Phone, SeniorityLevel, EmployeeSizeRange, RevenueRange. **Company items** carry the
company half of the same set.

**Phones.** In 50 contacts `DirectPhone` was filled 6 times and `Phone` 35; the 4 holding both held
different numbers; `HasDirectPhone: true` returned 20 of 20 with `DirectPhone`, `false` 2 of 20. So
`Phone` is the office line, not the person's: `records.py` reads only `DirectPhone` as a direct phone.

**LinkedIn.** `EmployeeLinkedIn` 26 of 50, every one a `/in/` profile; `CompanyLinkedin` 29 of 50,
every one `/company/`.

**Vocabularies** (now the `fields.py` options): SeniorityLevel `Entry, Manager, Senior, VP, Director,
C Level, Owner, Internship`; EmployeeSizeRange `1-10 … 10000+` plus the stored variants `" 1-10"`,
`"1-10 employees"`, `"Not Available"` and bare headcounts; RevenueRange `$0M-$1M … $10B+`. Department
and IndustryType are not bounded (seniority words appear as departments; the two endpoints spell
industries differently), so both stay free text. Placeholders `"NULL"` and `"Not Available"` appear
as values and are read as empty.

**Location filters (contacts).** `Countries`, `States`, `Cities`, `ZipCodes` are honoured (an
impossible City or ZipCode returns no rows; an impossible Country or State makes the provider time
out at 120 s). None of the `Contact*`, `Person*`, `Company*` or `Hq*` names is honoured, so the
company-level location set stays disabled on the Contacts view.

**Latency.** Cold start, the first call after the provider idles: 14 s, 50 s, 124 s on three
occasions. Warm: 0.5-5 s. **Faults on the provider's side**, reported to the owner to raise with
InfoJoy: `IncludeTotalCount` on contacts answers 500 after 120 s even warm; `IndustryTypes` with a
country answered 500 after 120 s warm; the documented sample (Titles + SeniorityLevels + Countries)
timed out at 120 s cold; a contact's State can contradict its City and RegionCode.

### Consequences in the code

- Read timeout 60 s (was 30 s), and the request's database connection is handed back to the pool
  while the provider answers (`core.tenancy.connection_released`), so waiting costs no connection.
  A timeout says the first search after a quiet spell can take up to two minutes.
- `IncludeTotalCount` is never sent (`client.COUNT_SUPPORTED`), and "Show total" is not offered
  (`GET /prospector/filters` returns `can_count: false`).
- A Net new scan stops asking for pages after 45 s and returns a cursor ("Keep scanning").
