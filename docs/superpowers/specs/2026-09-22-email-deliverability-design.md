# Found emails must be correct — design

**Date:** 2026-09-22 · **Status:** approved by the product owner · **Branch:** `fix/email-deliverability`

## Why

Customers report major email deliverability failures. Investigation (systematic, before any fix) found
five independent defects in the finder, plus a gap in Similar people:

1. **An address the verifier proved invalid was still saved.** When every pattern verified invalid the
   verifying finder returned nothing, and the blind `PatternEmailProvider` after it in the waterfall
   wrote `first.last@domain` anyway. The final check labelled it invalid; it was stored regardless.
2. **Unverified guesses were saved.** A verifier that could not answer still produced `first.last` as
   `unknown`/`risky`, and the campaign gate sends `unknown` for non-sourced contacts.
3. **DNS-only "risky" says nothing about the mailbox.** Every address on a domain with MX grades
   `risky` 0.5, so every pattern ties and `first.last` wins by default.
4. **No step checked that `account.domain` is the organisation's email domain.** Website redirects,
   separate email domains and wrong mappings all produced guesses at the wrong place.
5. **Search-scraped addresses were not tied to the person.** The first address on the company domain in
   six snippets won — `press@`, or a colleague.
6. **Similar people never resolved the employer's domain.** A person at an untracked company got an
   account with whatever domain the rep typed; blank meant no email and no signals.

## Policy (decided with the product owner)

| Verdict | Saved? | Sending |
|---|---|---|
| valid | yes | normal |
| catch-all, or risky **from a mailbox check** (Reacher) | yes, labelled | the user decides (existing "Send anyway" and campaign "Send to risky addresses") |
| invalid | **never kept**; removed if proven later, remembered so it is never guessed again | — |
| unknown, or risky from **DNS only** | **not saved** when a verifier is configured | — |

**The stub verifier keeps today's behaviour.** `stub` is "no verifier configured here" (dev, tests,
offline), like `DemoSignalSource`; being strict against it would show no emails anywhere and prove
nothing. The strict rule applies wherever a real verifier (`reacher`, `dns`) is configured — which is
staging and production. Consequence stated plainly: a deployment on DNS-only verification stops
saving new pattern guesses until Reacher answers.

## Design

- **One choke point.** `WaterfallEnricher.enrich_contact` decides what is written, from the verdict's
  status and its `source` (`reacher` = mailbox checked). Providers keep finding candidates; they no
  longer decide what is saved.
- **Email-domain resolution** (`nexus/enrichment/mail_domain.py`), cached on
  `account.custom_fields["mail_domain"]` with its evidence, 30-day TTL. Evidence, strongest first:
  1. the domain of colleagues at the account whose address is verified valid;
  2. addresses published on the company's own site (homepage, `/contact`), on a domain that shares the
     company's identity (the website's or its redirect target's name);
  3. where the website redirects (`acme.io` → `acme.com`), if it accepts mail;
  4. the website domain, if it accepts mail (MX, else A as the implicit MX).
  No candidate accepting mail means no guessing at all. The site fetch is SSRF-guarded with the
  existing `sources/safety._is_blocked_host` and bounded in time and size.
- **The company's email format**, inferred from verified colleagues (name + address), moves the
  matching pattern to the front — so on a catch-all domain the one address returned is the company's
  real format, not a default `first.last`. A published address that matches the person is tried first.
- **Suspected wrong domain:** when every candidate verifies invalid on a domain served from cache, the
  domain is resolved again once and the search repeated if it changed.
- **Search-scraped addresses** must be on the email domain, must match the person's name, and must not
  be a role address.
- **Similar people** resolves the employer's domain from its name (`nexus/enrichment/company_domain.py`):
  one search, first result that is not a directory or social profile and names the company, confirmed
  to exist in DNS. The add form prefills it (editable); the server resolves it too when left blank, and
  fills a blank domain on an existing account. Unmetered: the sourcing search was already charged.
- **Cleanup script** `scripts/clean_invalid_emails.py`, dry run by default: clears invalid addresses
  and unverified finder guesses, remembering what it removed.

## Not in scope (follow-ups)

Learning from asynchronous bounces (needs NDR parsing over IMAP); automatic retry of inconclusive checks
on a backoff; a "use best guess anyway" button; charging for domain resolution.

## Tests

Offline and deterministic: injected verifiers, fetchers and DNS resolvers; no test touches the network.
Existing tests keep passing on the stub path; the new policy is pinned with a configured verifier.
