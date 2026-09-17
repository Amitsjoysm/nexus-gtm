# Phase 01: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the schema, permissions and pure libraries every later phase builds on, with no behaviour change for any existing feature.

**Architecture:** One additive migration (`0057_engagement`) creates every engagement and ledger table, registered as ORM models. Four pure modules hold the rules later phases must not re-derive: ULIDs (`ids.py`), one-"Re:" subjects (`subjects.py`), recipient timezones and business days (`timekeeping.py`) and date-phrase resolution (`dates.py`). Workspace engagement settings (`settings.py`) live in `Tenant.email_settings["engagement"]`. Nothing reads the new tables yet.

**Tech Stack:** SQLAlchemy 2.0 async ORM, Alembic, `zoneinfo` + `tzdata`, pytest.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §4 (data model), §6 (date resolution, confidence bar), §8 (scheduling), D16, D23.

**Verified:** every code block in this plan was run in the CI image (`nexus-ci-deps:py311`) on 2026-09-17 against `feat/sdr-engagement@b9642f0`: migration replay, RLS guard, all 74 engagement tests and `ruff check nexus tests` pass.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Modify | `pyproject.toml` | add `tzdata` |
| Modify | `nexus/core/rbac.py` | `run_engagement` (rep+), `manage_engagement` (manager+) |
| Create | `nexus/engagement/__init__.py` | package marker |
| Create | `nexus/engagement/ids.py` | ULIDs |
| Create | `nexus/engagement/subjects.py` | base subject, reply subject |
| Create | `nexus/engagement/timekeeping.py` | recipient timezone, business days, follow-up times |
| Create | `nexus/engagement/dates.py` | date-phrase resolution |
| Create | `nexus/engagement/settings.py` | workspace confidence bar, reminder hours, OOO default |
| Create | `nexus/models/engagement.py` | nine engagement models |
| Create | `nexus/models/ledger.py` | `TrainingConsent`, `LedgerOutbox` |
| Modify | `nexus/models/__init__.py` | register the eleven models |
| Modify | `nexus/models/identity.py` | `PendingRegistration.training_consent` |
| Modify | `nexus/models/calling.py` | `CallTask.engagement_enrollment_id` |
| Create | `migrations/versions/0057_engagement.py` | the migration |
| Create | `tests/test_engagement_permissions.py` | RBAC |
| Create | `tests/test_engagement_text_and_ids.py` | subjects, ULIDs, settings |
| Create | `tests/test_engagement_timekeeping.py` | zones, business days, follow-up times |
| Create | `tests/test_engagement_dates.py` | 45 real reply phrases |
| Create | `tests/test_engagement_models.py` | uniqueness guarantees + RLS enrolment |
| Modify | `CLAUDE.md` | migration head and a short engagement section |

---

### Task 1: Add the timezone database dependency

`zoneinfo` needs a zone database. Linux images have one in `/usr/share/zoneinfo`; Windows and some slim images do not (measured: `ZoneInfo("Europe/Berlin")` raises `ZoneInfoNotFoundError` on the local Python 3.14). The `tzdata` package is the CPython-maintained fallback that `zoneinfo` loads automatically.

**Files:**
- Modify: `pyproject.toml` (the `dependencies` list)

- [ ] **Step 1: Prove the failure locally**

Run: `python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin')"`
Expected on a machine without a system zone database: `ZoneInfoNotFoundError: 'No time zone found with key Europe/Berlin'`. (On Linux it succeeds; the dependency still matters for Windows developers and minimal images.)

- [ ] **Step 2: Add the dependency**

In `pyproject.toml`, add one line to `[project].dependencies`, after `"aiosqlite>=0.19",`:

```toml
    "tzdata>=2024.1",           # IANA zones for zoneinfo where the OS has none (Windows, minimal images)
```

- [ ] **Step 3: Install and check**

Run: `pip install -e ".[dev,migrate]"` then `python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Europe/Berlin'))"`
Expected: `Europe/Berlin`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build(engagement): add tzdata so zoneinfo works on every platform"
```

---

### Task 2: Engagement permissions

SDRs are reps and must build and run their own campaigns; today's `manage_campaigns` is manager-only. Managers set the confidence range (D23) and see the team's reply desk.

**Files:**
- Modify: `nexus/core/rbac.py`
- Test: `tests/test_engagement_permissions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engagement_permissions.py`:

```python
"""Reps run their own outreach; managers set the team's confidence range and see the team desk."""
from __future__ import annotations

from nexus.core.rbac import Permission, Role, has_permission


def test_every_role_can_run_engagement():
    for role in Role:
        assert has_permission(role, Permission.run_engagement)


def test_only_managers_and_above_manage_engagement():
    assert not has_permission(Role.rep, Permission.manage_engagement)
    for role in (Role.manager, Role.admin, Role.owner):
        assert has_permission(role, Permission.manage_engagement)
```

- [ ] **Step 2: Run it to see it fail**

Run: `pytest tests/test_engagement_permissions.py -n0 -q`
Expected: FAIL with `AttributeError: run_engagement`

- [ ] **Step 3: Add the permissions**

In `nexus/core/rbac.py`, add to `class Permission` after `assign_accounts`:

```python
    # rep+: build campaigns, review drafts, work your own reply desk, connect your own mailbox.
    # SDRs are reps; the old `manage_campaigns` stopped at manager, which kept the people who send
    # the email out of the screen that sends it.
    run_engagement = "run_engagement"
    # manager+: the workspace confidence bar and its range (D23), the team's reply desk, reassigning
    # a reply, lifting a do-not-contact block.
    manage_engagement = "manage_engagement"
```

and to `_MIN_ROLE`:

```python
    Permission.run_engagement: Role.rep,
    Permission.manage_engagement: Role.manager,
```

- [ ] **Step 4: Run it to see it pass**

Run: `pytest tests/test_engagement_permissions.py -n0 -q`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/core/rbac.py tests/test_engagement_permissions.py
git commit -m "feat(engagement): run_engagement for reps, manage_engagement for managers"
```

---

### Task 3: ULIDs

Ledger event ids and the `X-Nexus-Ref` header need ids that are unique and sort by creation time, so "everything after the last shipped event" is a range scan.

**Files:**
- Create: `nexus/engagement/__init__.py`, `nexus/engagement/ids.py`
- Test: `tests/test_engagement_text_and_ids.py` (created here, extended in Tasks 4 and 7)

- [ ] **Step 1: Write the failing test**

Create `tests/test_engagement_text_and_ids.py` with only the ULID test for now:

```python
"""Subjects keep exactly one "Re:" (D16); ULIDs sort by time; workspace settings stay in range (D23)."""
from __future__ import annotations

import pytest

from nexus.engagement.ids import new_ulid, ulid_timestamp_ms


def test_ulids_sort_by_time_and_carry_their_timestamp():
    early, late = new_ulid(1_700_000_000_000), new_ulid(1_700_000_000_001)
    assert len(early) == 26 and early < late
    assert ulid_timestamp_ms(early) == 1_700_000_000_000
    assert len({new_ulid() for _ in range(5000)}) == 5000
    with pytest.raises(ValueError):
        ulid_timestamp_ms("not-a-ulid")
```

- [ ] **Step 2: Run it to see it fail**

Run: `pytest tests/test_engagement_text_and_ids.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement'`

- [ ] **Step 3: Implement**

Create `nexus/engagement/__init__.py`:

```python
"""SDR engagement engine (spec: docs/superpowers/specs/2026-09-17-sdr-engagement-design.md)."""
```

Create `nexus/engagement/ids.py`:

```python
"""Sortable unique ids (ULID) for ledger events and outbound message references.

A ULID is 48 bits of milliseconds followed by 80 random bits, written as 26 Crockford base32
characters. Sorting the strings sorts by creation time, which is what the ledger outbox and the
archive store rely on when they batch "everything after the last shipped event".
"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_ALPHABET)}
_MAX_MS = (1 << 48) - 1


def new_ulid(now_ms: int | None = None) -> str:
    """A new ULID. ``now_ms`` pins the timestamp part (used by tests and backfills)."""
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if ms < 0 or ms > _MAX_MS:
        raise ValueError(f"timestamp {ms} is outside the ULID range")
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def ulid_timestamp_ms(ulid: str) -> int:
    """The millisecond timestamp a ULID carries. Raises ``ValueError`` on anything malformed."""
    text = (ulid or "").strip().upper()
    if len(text) != 26 or any(c not in _DECODE for c in text):
        raise ValueError(f"not a ULID: {ulid!r}")
    value = 0
    for c in text[:10]:
        value = (value << 5) | _DECODE[c]
    return value
```

- [ ] **Step 4: Run it to see it pass**

Run: `pytest tests/test_engagement_text_and_ids.py -n0 -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/__init__.py nexus/engagement/ids.py tests/test_engagement_text_and_ids.py
git commit -m "feat(engagement): sortable ULIDs for ledger events and message references"
```

---

### Task 4: One base subject, exactly one "Re:" (D16)

**Files:**
- Create: `nexus/engagement/subjects.py`
- Test: `tests/test_engagement_text_and_ids.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engagement_text_and_ids.py` (import at the top, tests at the bottom):

```python
from nexus.engagement.subjects import normalize_subject, reply_subject


@pytest.mark.parametrize("raw,base", [
    ("Re: RE: AW: Quick question", "Quick question"),
    ("RE : Fwd: [EXTERNAL] Re[2]: pricing", "pricing"),
    ("SV: VS: hello", "hello"),
    ("Antw: Odp: Ynt: x", "x"),
    ("TR: RV: y", "y"),
    ("  Re:   spaced   out ", "spaced out"),
    ("Revenue ops at Acme", "Revenue ops at Acme"),
    ("R&D hiring at Acme", "R&D hiring at Acme"),
    ("", ""),
])
def test_every_reply_and_forward_marker_is_removed(raw, base):
    assert normalize_subject(raw) == base


def test_a_reply_subject_carries_exactly_one_re():
    assert reply_subject("RE: Re: RE: hello") == "Re: hello"
    assert reply_subject(reply_subject("hello")) == "Re: hello"
    assert reply_subject("") == "Re:"
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_text_and_ids.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.subjects'`

- [ ] **Step 3: Implement**

Create `nexus/engagement/subjects.py`:

```python
"""Email subjects: one base subject per conversation, and exactly one "Re:" on replies (D16).

Mail clients prepend a reply or forward marker in the reader's language, and many prepend again on
every round trip: "RE: Re: AW: Quick question". A follow-up that simply adds "Re: " to whatever
arrived produces the stack the product owner ruled out. So every outbound follow-up and response is
built from the BASE subject — the text with every leading marker and mail-system tag removed — with
one "Re: " in front.

The marker list covers the clients SDRs actually receive from: English (Re, Fw, Fwd), German (AW,
WG), Nordic (SV, VS), Dutch (Antw), French (TR), Spanish (RV), Italian (R), Polish (Odp), Turkish
(Ynt), numbered forms (Re[2], Re(3)) and a space before the colon ("RE :"). Corporate gateways add
an "[EXTERNAL]" tag to inbound mail, and the buyer's reply carries it back, so those tags are
stripped too.
"""
from __future__ import annotations

import re

_MARKER = re.compile(
    r"""^\s*(?:
        (?:re|fw|fwd|aw|wg|sv|vs|antw|tr|rv|r|odp|ynt)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]
      | \[\s*(?:external|ext|external\s+email|external\s+sender)\s*\]
      | \*\s*external\s*\*
      | external\s*:
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)
_SPACES = re.compile(r"\s+")


def normalize_subject(subject: str | None) -> str:
    """The subject with every leading reply/forward marker and gateway tag removed."""
    text = _SPACES.sub(" ", str(subject or "")).strip()
    while True:
        stripped = _MARKER.sub("", text, count=1)
        if stripped == text:
            return text
        text = stripped.strip()


def reply_subject(subject: str | None) -> str:
    """The subject for a follow-up or response: exactly one "Re: " before the base subject."""
    base = normalize_subject(subject)
    return f"Re: {base}" if base else "Re:"
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_text_and_ids.py -n0 -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/subjects.py tests/test_engagement_text_and_ids.py
git commit -m "feat(engagement): base subjects and exactly one Re: on replies"
```

---

### Task 5: Recipient timezones, business days and follow-up times (spec §8)

**Files:**
- Create: `nexus/engagement/timekeeping.py`
- Test: `tests/test_engagement_timekeeping.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_engagement_timekeeping.py`:

```python
"""Business days, recipient timezones and follow-up times (spec §8)."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from nexus.engagement import timekeeping as tk

UTC = timezone.utc


def test_the_recipient_zone_resolves_contact_then_account_then_sdr():
    explicit = tk.resolve_zone(contact_tz="Europe/Berlin", account_country="United States",
                               account_region="TX", sdr_tz="Asia/Kolkata")
    assert (explicit.name, explicit.source) == ("Europe/Berlin", "contact")

    by_region = tk.resolve_zone(contact_tz=None, account_country="United States",
                                account_region="TX", sdr_tz="Asia/Kolkata")
    assert (by_region.name, by_region.source) == ("America/Chicago", "account")

    by_country = tk.resolve_zone(contact_tz="", account_country="DE", account_region=None,
                                 sdr_tz="Asia/Kolkata")
    assert (by_country.name, by_country.source) == ("Europe/Berlin", "account")

    unknown = tk.resolve_zone(contact_tz="Mars/Base", account_country="Atlantis",
                              account_region=None, sdr_tz="Asia/Kolkata")
    assert (unknown.name, unknown.source) == ("Asia/Kolkata", "sdr")

    nothing = tk.resolve_zone(contact_tz=None, account_country=None, account_region=None,
                              sdr_tz=None)
    assert (nothing.name, nothing.source) == ("UTC", "utc")


def test_country_names_codes_and_regions_are_recognised():
    assert tk.zone_for_place("usa", "california") == "America/Los_Angeles"
    assert tk.zone_for_place("U.S.A.", "WA") == "America/Los_Angeles"
    assert tk.zone_for_place("Canada", "BC") == "America/Vancouver"
    assert tk.zone_for_place("Australia", "Western Australia") == "Australia/Perth"
    assert tk.zone_for_place("India", None) == "Asia/Kolkata"
    assert tk.zone_for_place("united kingdom", "somewhere") == "Europe/London"
    assert tk.zone_for_place("", None) == ""


def test_business_day_arithmetic_skips_weekends():
    friday, saturday = date(2026, 9, 18), date(2026, 9, 19)
    assert tk.next_business_day(friday) == date(2026, 9, 21)
    assert tk.roll_to_business_day(saturday) == date(2026, 9, 21)
    assert tk.add_business_days(saturday, 0) == date(2026, 9, 21)
    assert tk.add_business_days(friday, 2) == date(2026, 9, 22)


def test_an_auto_follow_up_goes_out_at_the_same_local_time():
    chicago = tk.zone_or_none("America/Chicago")
    sent = datetime(2026, 9, 18, 20, 30, tzinfo=UTC)  # Friday 15:30 in Chicago
    due = tk.auto_followup_at(previous_sent_at=sent, delay_business_days=2, zone=chicago)
    assert tk.to_local(due, chicago).replace(tzinfo=None) == datetime(2026, 9, 22, 15, 30)


def test_local_time_survives_a_daylight_saving_change():
    london = tk.zone_or_none("Europe/London")
    sent = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)  # Friday 09:00 BST
    due = tk.auto_followup_at(previous_sent_at=sent, delay_business_days=1, zone=london)
    assert due == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # Monday 09:00 GMT


def test_manual_timing_uses_the_chosen_time_and_allowed_weekdays():
    chicago = tk.zone_or_none("America/Chicago")
    sent = datetime(2026, 9, 18, 20, 30, tzinfo=UTC)
    due = tk.manual_followup_at(previous_sent_at=sent, delay_business_days=2,
                                send_time_local=time(9, 0), allowed_weekdays=[2, 3],
                                zone=chicago)
    assert tk.to_local(due, chicago).replace(tzinfo=None) == datetime(2026, 9, 23, 9, 0)


def test_local_day_start_is_midnight_in_the_mailbox_zone():
    la = tk.zone_or_none("America/Los_Angeles")
    start = tk.local_day_start(datetime(2026, 9, 17, 2, 0, tzinfo=UTC), la)
    assert start == datetime(2026, 9, 16, 7, 0, tzinfo=UTC)


def test_clock_strings_parse_or_refuse():
    assert tk.parse_clock("09:30") == time(9, 30)
    assert tk.parse_clock("9") is None
    assert tk.parse_clock(None) is None
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_timekeeping.py -n0 -q`
Expected: FAIL with `ImportError: cannot import name 'timekeeping' from 'nexus.engagement'`

- [ ] **Step 3: Implement**

Create `nexus/engagement/timekeeping.py`:

```python
"""Whose clock, which days: time arithmetic for scheduling outreach (spec §8).

Three rules this module owns, so no caller re-derives them:

* **A business day is Monday to Friday in the recipient's timezone.** There is no holiday
  calendar; the spec defines business days as weekdays, and a follow-up landing on a public holiday
  is a smaller cost than a holiday table that is wrong for most of the world.
* **The recipient's timezone resolves contact → account → SDR.** A contact may carry an explicit
  IANA zone in ``custom_fields["timezone"]`` (a CSV import can map one). Otherwise the account's
  country — and, for the countries that span several zones, its region — picks the zone. Otherwise
  the SDR's mailbox timezone is used, because sending at the SDR's 9am is a better guess than UTC.
* **Everything stored is UTC; everything decided is local.** Callers pass aware UTC datetimes in and
  get aware UTC datetimes out; local dates and times exist only inside these functions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
WEEKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday..Friday, as date.weekday() numbers

# ISO 3166 alpha-2 code -> the zone most of the country's business population works in.
_COUNTRY_ZONE: dict[str, str] = {
    "ae": "Asia/Dubai", "ar": "America/Argentina/Buenos_Aires", "at": "Europe/Vienna",
    "au": "Australia/Sydney", "bd": "Asia/Dhaka", "be": "Europe/Brussels",
    "bg": "Europe/Sofia", "br": "America/Sao_Paulo", "ca": "America/Toronto",
    "ch": "Europe/Zurich", "cl": "America/Santiago", "cn": "Asia/Shanghai",
    "co": "America/Bogota", "cy": "Asia/Nicosia", "cz": "Europe/Prague", "de": "Europe/Berlin",
    "dk": "Europe/Copenhagen", "ee": "Europe/Tallinn", "eg": "Africa/Cairo",
    "es": "Europe/Madrid", "fi": "Europe/Helsinki", "fr": "Europe/Paris",
    "gb": "Europe/London", "gr": "Europe/Athens", "hk": "Asia/Hong_Kong",
    "hr": "Europe/Zagreb", "hu": "Europe/Budapest", "id": "Asia/Jakarta",
    "ie": "Europe/Dublin", "il": "Asia/Jerusalem", "in": "Asia/Kolkata", "is": "Atlantic/Reykjavik",
    "it": "Europe/Rome", "jp": "Asia/Tokyo", "ke": "Africa/Nairobi", "kr": "Asia/Seoul",
    "lk": "Asia/Colombo", "lt": "Europe/Vilnius", "lu": "Europe/Luxembourg",
    "lv": "Europe/Riga", "ma": "Africa/Casablanca", "mt": "Europe/Malta",
    "mx": "America/Mexico_City", "my": "Asia/Kuala_Lumpur", "ng": "Africa/Lagos",
    "nl": "Europe/Amsterdam", "no": "Europe/Oslo", "np": "Asia/Kathmandu",
    "nz": "Pacific/Auckland", "pe": "America/Lima", "ph": "Asia/Manila", "pk": "Asia/Karachi",
    "pl": "Europe/Warsaw", "pt": "Europe/Lisbon", "qa": "Asia/Qatar", "ro": "Europe/Bucharest",
    "rs": "Europe/Belgrade", "ru": "Europe/Moscow", "sa": "Asia/Riyadh", "se": "Europe/Stockholm",
    "sg": "Asia/Singapore", "si": "Europe/Ljubljana", "sk": "Europe/Bratislava",
    "th": "Asia/Bangkok", "tr": "Europe/Istanbul", "tw": "Asia/Taipei", "ua": "Europe/Kyiv",
    "us": "America/New_York", "uy": "America/Montevideo", "vn": "Asia/Ho_Chi_Minh",
    "za": "Africa/Johannesburg",
}

# Names and aliases seen in `accounts.country`, which is free text from enrichment and CSV imports.
_COUNTRY_ALIAS: dict[str, str] = {
    "united states": "us", "united states of america": "us", "usa": "us", "america": "us",
    "united kingdom": "gb", "uk": "gb", "great britain": "gb", "england": "gb", "scotland": "gb",
    "wales": "gb", "northern ireland": "gb", "germany": "de", "deutschland": "de",
    "france": "fr", "spain": "es", "españa": "es", "italy": "it", "netherlands": "nl",
    "the netherlands": "nl", "holland": "nl", "belgium": "be", "switzerland": "ch",
    "austria": "at", "sweden": "se", "norway": "no", "denmark": "dk", "finland": "fi",
    "iceland": "is", "ireland": "ie", "portugal": "pt", "poland": "pl", "czech republic": "cz",
    "czechia": "cz", "slovakia": "sk", "hungary": "hu", "romania": "ro", "bulgaria": "bg",
    "greece": "gr", "croatia": "hr", "slovenia": "si", "serbia": "rs", "estonia": "ee",
    "latvia": "lv", "lithuania": "lt", "luxembourg": "lu", "malta": "mt", "cyprus": "cy",
    "ukraine": "ua", "russia": "ru", "russian federation": "ru", "turkey": "tr", "türkiye": "tr",
    "israel": "il", "united arab emirates": "ae", "uae": "ae", "saudi arabia": "sa",
    "qatar": "qa", "egypt": "eg", "morocco": "ma", "nigeria": "ng", "kenya": "ke",
    "south africa": "za", "india": "in", "pakistan": "pk", "bangladesh": "bd",
    "sri lanka": "lk", "nepal": "np", "china": "cn", "hong kong": "hk", "taiwan": "tw",
    "japan": "jp", "south korea": "kr", "korea": "kr", "republic of korea": "kr",
    "singapore": "sg", "malaysia": "my", "indonesia": "id", "thailand": "th",
    "vietnam": "vn", "viet nam": "vn", "philippines": "ph", "australia": "au",
    "new zealand": "nz", "canada": "ca", "mexico": "mx", "méxico": "mx", "brazil": "br",
    "brasil": "br", "argentina": "ar", "chile": "cl", "colombia": "co", "peru": "pe",
    "uruguay": "uy",
}

# Countries whose business population spans zones: region (code or name) -> zone.
_REGION_ZONE: dict[str, dict[str, str]] = {
    "us": {
        **{s: "America/New_York" for s in (
            "ct", "de", "dc", "fl", "ga", "in", "ky", "me", "md", "ma", "mi", "nh", "nj", "ny",
            "nc", "oh", "pa", "ri", "sc", "vt", "va", "wv",
            "connecticut", "delaware", "district of columbia", "florida", "georgia", "indiana",
            "kentucky", "maine", "maryland", "massachusetts", "michigan", "new hampshire",
            "new jersey", "new york", "north carolina", "ohio", "pennsylvania", "rhode island",
            "south carolina", "vermont", "virginia", "west virginia",
        )},
        **{s: "America/Chicago" for s in (
            "al", "ar", "il", "ia", "ks", "la", "mn", "ms", "mo", "ne", "nd", "ok", "sd", "tn",
            "tx", "wi",
            "alabama", "arkansas", "illinois", "iowa", "kansas", "louisiana", "minnesota",
            "mississippi", "missouri", "nebraska", "north dakota", "oklahoma", "south dakota",
            "tennessee", "texas", "wisconsin",
        )},
        **{s: "America/Denver" for s in (
            "co", "id", "mt", "nm", "ut", "wy",
            "colorado", "idaho", "montana", "new mexico", "utah", "wyoming",
        )},
        "az": "America/Phoenix", "arizona": "America/Phoenix",
        **{s: "America/Los_Angeles" for s in (
            "ca", "nv", "or", "wa", "california", "nevada", "oregon", "washington",
        )},
        "ak": "America/Anchorage", "alaska": "America/Anchorage",
        "hi": "Pacific/Honolulu", "hawaii": "Pacific/Honolulu",
    },
    "ca": {
        "bc": "America/Vancouver", "british columbia": "America/Vancouver",
        "ab": "America/Edmonton", "alberta": "America/Edmonton",
        "sk": "America/Regina", "saskatchewan": "America/Regina",
        "mb": "America/Winnipeg", "manitoba": "America/Winnipeg",
        "on": "America/Toronto", "ontario": "America/Toronto",
        "qc": "America/Toronto", "quebec": "America/Toronto", "québec": "America/Toronto",
        "nb": "America/Halifax", "new brunswick": "America/Halifax",
        "ns": "America/Halifax", "nova scotia": "America/Halifax",
        "pe": "America/Halifax", "prince edward island": "America/Halifax",
        "nl": "America/St_Johns", "newfoundland and labrador": "America/St_Johns",
    },
    "au": {
        "nsw": "Australia/Sydney", "new south wales": "Australia/Sydney",
        "act": "Australia/Sydney", "australian capital territory": "Australia/Sydney",
        "vic": "Australia/Melbourne", "victoria": "Australia/Melbourne",
        "tas": "Australia/Hobart", "tasmania": "Australia/Hobart",
        "qld": "Australia/Brisbane", "queensland": "Australia/Brisbane",
        "sa": "Australia/Adelaide", "south australia": "Australia/Adelaide",
        "nt": "Australia/Darwin", "northern territory": "Australia/Darwin",
        "wa": "Australia/Perth", "western australia": "Australia/Perth",
    },
    "br": {
        "am": "America/Manaus", "amazonas": "America/Manaus",
        "ba": "America/Bahia", "bahia": "America/Bahia",
        "pe": "America/Recife", "pernambuco": "America/Recife",
        "ce": "America/Fortaleza", "ceará": "America/Fortaleza", "ceara": "America/Fortaleza",
    },
    "mx": {
        "bc": "America/Tijuana", "baja california": "America/Tijuana",
        "son": "America/Hermosillo", "sonora": "America/Hermosillo",
        "chih": "America/Chihuahua", "chihuahua": "America/Chihuahua",
        "q roo": "America/Cancun", "quintana roo": "America/Cancun",
    },
}


def zone_or_none(name: str | None) -> ZoneInfo | None:
    """A ZoneInfo for a valid IANA name, else None. Never raises."""
    text = (name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _key(text: str | None) -> str:
    return " ".join((text or "").strip().lower().replace(".", "").split())


def country_code(country: str | None) -> str:
    """ISO alpha-2 (lowercase) for a free-text country, or "" when unknown."""
    key = _key(country)
    if len(key) == 2 and key in _COUNTRY_ZONE:
        return key
    return _COUNTRY_ALIAS.get(key, "")


def zone_for_place(country: str | None, region: str | None = None) -> str:
    """The IANA zone name for a country (and region, where the country spans zones), or ""."""
    code = country_code(country)
    if not code:
        return ""
    regional = _REGION_ZONE.get(code, {})
    return regional.get(_key(region), "") or _COUNTRY_ZONE.get(code, "")


@dataclass(frozen=True, slots=True)
class ResolvedZone:
    zone: tzinfo
    name: str
    source: str  # contact | account | sdr | utc


def resolve_zone(
    *, contact_tz: str | None, account_country: str | None, account_region: str | None,
    sdr_tz: str | None,
) -> ResolvedZone:
    """The recipient's timezone, resolved contact -> account -> SDR -> UTC."""
    explicit = zone_or_none(contact_tz)
    if explicit is not None:
        return ResolvedZone(explicit, str(contact_tz).strip(), "contact")
    place = zone_for_place(account_country, account_region)
    placed = zone_or_none(place)
    if placed is not None:
        return ResolvedZone(placed, place, "account")
    sdr = zone_or_none(sdr_tz)
    if sdr is not None:
        return ResolvedZone(sdr, str(sdr_tz).strip(), "sdr")
    return ResolvedZone(UTC, "UTC", "utc")


def is_business_day(day: date) -> bool:
    return day.weekday() in WEEKDAYS


def roll_to_business_day(day: date) -> date:
    """``day`` itself when it is a business day, else the next one."""
    while not is_business_day(day):
        day += timedelta(days=1)
    return day


def next_business_day(day: date) -> date:
    """The first business day strictly after ``day``."""
    return roll_to_business_day(day + timedelta(days=1))


def add_business_days(day: date, count: int) -> date:
    """``count`` business days after ``day``. Zero returns ``day`` rolled to a business day."""
    if count < 0:
        raise ValueError("count must not be negative")
    current = roll_to_business_day(day) if count == 0 else day
    for _ in range(count):
        current = next_business_day(current)
    return current


def to_local(moment: datetime, zone: tzinfo) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone)


def at_local(day: date, clock: time, zone: tzinfo) -> datetime:
    """The UTC instant of ``clock`` on ``day`` in ``zone``."""
    return datetime.combine(day, clock.replace(tzinfo=None), tzinfo=zone).astimezone(UTC)


def parse_clock(text: str | None) -> time | None:
    """"HH:MM" -> time, else None. The storage format of ``engagement_steps.send_time_local``."""
    raw = (text or "").strip()
    try:
        hours, minutes = raw.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


def auto_followup_at(*, previous_sent_at: datetime, delay_business_days: int, zone: tzinfo) -> datetime:
    """Auto timing: N business days after the previous email, at the same local time of day."""
    local = to_local(previous_sent_at, zone)
    day = add_business_days(local.date(), max(0, delay_business_days))
    return at_local(day, local.time(), zone)


def manual_followup_at(
    *, previous_sent_at: datetime, delay_business_days: int, send_time_local: time,
    allowed_weekdays: tuple[int, ...] | list[int], zone: tzinfo,
) -> datetime:
    """Manual timing: N business days later, moved to an allowed weekday, at the chosen time."""
    allowed = tuple(sorted({int(d) for d in allowed_weekdays if 0 <= int(d) <= 6})) or WEEKDAYS
    local = to_local(previous_sent_at, zone)
    day = add_business_days(local.date(), max(0, delay_business_days))
    while day.weekday() not in allowed:
        day += timedelta(days=1)
    return at_local(day, send_time_local, zone)


def local_day_start(now: datetime, zone: tzinfo) -> datetime:
    """UTC instant of local midnight for the day ``now`` falls on in ``zone``."""
    local = to_local(now, zone)
    return at_local(local.date(), time(0, 0), zone)
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_timekeeping.py -n0 -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/timekeeping.py tests/test_engagement_timekeeping.py
git commit -m "feat(engagement): recipient timezones, business days, follow-up times"
```

---

### Task 6: Date phrases resolve in code, or not at all (D8, D15)

The table in the test is the contract: 33 phrases that must resolve to a specific date and rule, and 10 that must resolve to nothing because a person could mean more than one thing. The reference moment is Thursday 17 September 2026, 10:00 UTC.

**Files:**
- Create: `nexus/engagement/dates.py`
- Test: `tests/test_engagement_dates.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_engagement_dates.py`:

```python
"""Date phrases from real replies resolve to the date a person means, or to nothing (spec §6)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from nexus.engagement.dates import resolve_date

# Thursday 17 September 2026, 10:00 UTC.
RECEIVED = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
UTC = timezone.utc

RESOLVED = [
    # month names: first business day of the next occurrence
    ("try me in June", date(2027, 6, 1), "month"),
    ("next June", date(2027, 6, 1), "month"),
    ("reach out in October", date(2026, 10, 1), "month"),
    ("next September", date(2027, 9, 1), "month"),
    ("in May", date(2027, 5, 3), "month"),               # 1 May 2027 is a Saturday
    ("mid-November", date(2026, 11, 16), "month"),       # 15 Nov is a Sunday
    ("late January", date(2027, 1, 22), "month"),
    ("early December 2026", date(2026, 12, 1), "month"),
    # quarters, halves, years
    ("after Q3", date(2026, 10, 1), "quarter"),
    ("in Q1", date(2027, 1, 1), "quarter"),
    ("second quarter", date(2027, 4, 1), "quarter"),
    ("first half of 2027", date(2027, 1, 1), "half"),
    ("in 2027", date(2027, 1, 1), "year"),
    ("next quarter", date(2026, 10, 1), "next_period"),
    ("next month", date(2026, 10, 1), "next_period"),
    ("next year", date(2027, 1, 1), "next_period"),
    ("next week", date(2026, 9, 21), "next_period"),
    ("end of the month", date(2026, 9, 25), "end_of_period"),
    ("end of the year", date(2026, 12, 1), "end_of_period"),
    # offsets: from the received date, moved to a business day
    ("in two weeks", date(2026, 10, 1), "offset"),
    ("in 2 weeks' time", date(2026, 10, 1), "offset"),
    ("a couple of weeks", date(2026, 10, 1), "offset"),
    ("in 3 months", date(2026, 12, 17), "offset"),
    ("you may reach me in two weeks", date(2026, 10, 1), "offset"),
    ("tomorrow", date(2026, 9, 18), "tomorrow"),
    # specific days: returned as written
    ("back on the 24th", date(2026, 9, 24), "day_of_month"),
    ("back on the 5th", date(2026, 10, 5), "day_of_month"),
    ("returning Tuesday, 29th September", date(2026, 9, 29), "day_month"),
    ("out until October 3", date(2026, 10, 3), "day_month"),
    ("3 October", date(2026, 10, 3), "day_month"),
    ("2026-10-12", date(2026, 10, 12), "iso_date"),
    ("24/09", date(2026, 9, 24), "numeric_date"),
    ("back Monday", date(2026, 9, 21), "weekday"),
]

UNCLEAR = [
    "September",                              # the current month: this one or next year's?
    "Q3",                                     # the current quarter
    "in a few weeks",                         # no number
    "03/04",                                  # March 4th or 3rd April
    "returning Monday 29th September",        # 29 September 2026 is a Tuesday
    "between June and July",                  # two dates
    "H2 next year",                           # H2 read as the current half, then "next year"
    "end of the quarter",                     # already past the quarter's end window
    "not now",
    "",
]


@pytest.mark.parametrize("phrase,expected,rule", RESOLVED)
def test_a_phrase_resolves_to_the_date_it_names(phrase, expected, rule):
    result = resolve_date(phrase, RECEIVED, UTC)
    assert result is not None, f"{phrase!r} did not resolve"
    assert (result.date, result.rule) == (expected, rule)


@pytest.mark.parametrize("phrase", UNCLEAR)
def test_an_ambiguous_phrase_resolves_to_nothing(phrase):
    assert resolve_date(phrase, RECEIVED, UTC) is None


def test_tomorrow_is_read_in_the_contacts_timezone():
    # 23:30 on the 17th in UTC is already the 18th in Tokyo, so "tomorrow" there is the 19th.
    late = datetime(2026, 9, 17, 23, 30, tzinfo=timezone.utc)
    assert resolve_date("tomorrow", late, UTC).date == date(2026, 9, 18)
    assert resolve_date("tomorrow", late, ZoneInfo("Asia/Tokyo")).date == date(2026, 9, 19)


def test_a_date_already_past_is_not_a_reengagement_date():
    assert resolve_date("2026-09-01", RECEIVED, UTC) is None
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_dates.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.dates'`

- [ ] **Step 3: Implement**

Create `nexus/engagement/dates.py`:

```python
"""Turn a reply's date phrase into a calendar date — in code, never in the model (D15, spec §6).

The classifier returns only the words the person wrote ("next June", "after Q3", "back on the
24th", "in two weeks"). This module interprets them against the moment the reply was RECEIVED, in
the CONTACT's timezone, because "tomorrow" in a reply read three days later still means the day
after it was written.

**Ambiguity is an answer.** Anything that resolves more than one way, or not at all, returns
``None``, and the caller files the reply as ``unclear`` so a person decides (D8). Guessing wrong
here means emailing someone before they asked, which is exactly the outcome "later" replies exist to
prevent. Concretely ambiguous:

* ``03/04`` — March 4th or 3rd April, depending on who wrote it.
* ``June`` written in June — this June's remaining weeks, or next year's.
* ``a few weeks`` — no number to add.
* two different dates in one phrase — "between June and July".

Period phrases land on the FIRST BUSINESS DAY of the period (spec §6). A specific day ("the 24th",
"September 30") is returned as written; the scheduler decides what to do when it falls on a weekend.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from nexus.engagement.timekeeping import roll_to_business_day, to_local

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5,
    "sat": 5, "sunday": 6, "sun": 6,
}
_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "couple": 2, "a couple": 2, "a couple of": 2,
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}
_ORDINAL_QUARTERS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
                     "fourth": 4, "4th": 4, "last": 4}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_NUMBER_ALT = r"\d{1,3}|" + "|".join(sorted(_NUMBERS, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class Resolution:
    date: date
    rule: str


class _Ambiguous(Exception):
    """A rule recognised the phrase and found more than one reading."""


def _clean(phrase: str) -> str:
    text = (phrase or "").lower()
    text = text.replace("’", "'").replace("'s", "").replace("'", "")
    text = re.sub(r"[,;!?()\"]", " ", text)
    text = re.sub(r"\.(?!\d)", " ", text)
    return " ".join(text.split())


def _first_business_day(year: int, month: int) -> date:
    return roll_to_business_day(date(year, month, 1))


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _number(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _NUMBERS.get(token)


def _year_for_month(month: int, today: date, *, year: int | None, explicit_next: bool) -> int:
    if year is not None:
        return year
    if month > today.month:
        return today.year
    if month < today.month or explicit_next:
        return today.year + 1
    raise _Ambiguous("the named month is the current month")


def _check_weekday(day: date, weekday_word: str | None) -> None:
    if weekday_word and _WEEKDAYS[weekday_word] != day.weekday():
        raise _Ambiguous("the weekday named does not match the date")


# Each rule: (name, compiled pattern, handler(match, today) -> date). Ordered most specific first;
# a rule's match is removed from the text before the next rule runs, so "Monday 29th September"
# is read once as a date rather than again as "next Monday".
def _iso(m: dict, today: date) -> date:
    return date(int(m["y"]), int(m["m"]), int(m["d"]))


def _numeric(m: dict, today: date) -> date:
    a, b = int(m["a"]), int(m["b"])
    if a <= 12 and b <= 12 and a != b:
        raise _Ambiguous("day and month order cannot be told apart")
    day, month = (a, b) if a > 12 else (b, a)
    year = int(m["y"]) if m["y"] else None
    if year is not None and year < 100:
        year += 2000
    target_year = year if year is not None else today.year
    result = date(target_year, month, day)
    if year is None and result < today:
        result = date(today.year + 1, month, day)
    return result


def _day_month(m: dict, today: date) -> date:
    month = _MONTHS[m["month"]]
    day = int(m["day"])
    year = int(m["year"]) if m["year"] else None
    result = date(year or today.year, month, day)
    if year is None and result < today:
        result = date(today.year + 1, month, day)
    _check_weekday(result, m["wd"])
    return result


def _tomorrow(m: dict, today: date) -> date:
    return today + timedelta(days=2 if m["after"] else 1)


def _offset(m: dict, today: date) -> date:
    count = _number(m["n"])
    if count is None or count <= 0:
        raise _Ambiguous("no usable number")
    unit = m["unit"]
    if unit.startswith("day"):
        result = today + timedelta(days=count)
    elif unit.startswith("week"):
        result = today + timedelta(weeks=count)
    else:
        result = _add_months(today, count)
    return roll_to_business_day(result)


def _vague_offset(m: dict, today: date) -> date:
    raise _Ambiguous("'a few' / 'several' has no number")


def _next_period(m: dict, today: date) -> date:
    period = m["period"]
    if period == "week":
        return today + timedelta(days=7 - today.weekday())
    if period == "month":
        nxt = _add_months(today.replace(day=1), 1)
        return _first_business_day(nxt.year, nxt.month)
    if period == "quarter":
        quarter_start = (today.month - 1) // 3 * 3 + 1
        nxt = _add_months(date(today.year, quarter_start, 1), 3)
        return _first_business_day(nxt.year, nxt.month)
    return _first_business_day(today.year + 1, 1)


def _end_of_period(m: dict, today: date) -> date:
    period = m["period"]
    if period == "month":
        result = roll_to_business_day(date(today.year, today.month, 25))
    elif period == "quarter":
        last_month = ((today.month - 1) // 3 + 1) * 3
        result = roll_to_business_day(date(today.year, last_month, 15))
    else:
        result = roll_to_business_day(date(today.year, 12, 1))
    if result < today:
        raise _Ambiguous("that period's end has already passed")
    return result


def _quarter_number(m: dict) -> int:
    word = m["q"] or m["qword"]
    return int(word) if word and word.isdigit() else _ORDINAL_QUARTERS[word]


def _quarter(m: dict, today: date) -> date:
    q = _quarter_number(m)
    after = bool(m["after"])
    year = int(m["year"]) if m["year"] else None
    if after:
        q += 1
        if q == 5:
            q, year = 1, (year + 1 if year else None)
            if year is None:
                return _first_business_day(today.year + 1, 1)
    current_q = (today.month - 1) // 3 + 1
    if year is None:
        if q > current_q:
            year = today.year
        elif q < current_q or m["next"]:
            year = today.year + 1
        elif after:
            year = today.year
        else:
            raise _Ambiguous("the named quarter is the current quarter")
    return _first_business_day(year, (q - 1) * 3 + 1)


def _half(m: dict, today: date) -> date:
    half = 1 if (m["h"] == "1" or m["hword"] == "first") else 2
    current = 1 if today.month <= 6 else 2
    if m["year"]:
        year = int(m["year"])
    elif half > current:
        year = today.year
    elif half < current or m["next"]:
        year = today.year + 1
    else:
        raise _Ambiguous("the named half is the current half")
    return _first_business_day(year, 1 if half == 1 else 7)


def _month(m: dict, today: date) -> date:
    word = m["month"]
    if word == "may" and not (m["qual"] or m["next"] or m["year"] or m["lead"]):
        raise _NotADate()
    month = _MONTHS[word]
    year = _year_for_month(
        month, today, year=int(m["year"]) if m["year"] else None, explicit_next=bool(m["next"])
    )
    qual = (m["qual"] or "").strip()
    day = 1
    if qual in ("mid", "middle of", "mid-"):
        day = 15
    elif qual in ("late", "end of", "the end of"):
        day = 22
    return roll_to_business_day(date(year, month, day))


def _year(m: dict, today: date) -> date:
    year = int(m["year"])
    if year <= today.year:
        raise _Ambiguous("a year that has already started")
    return _first_business_day(year, 1)


def _weekday(m: dict, today: date) -> date:
    target = _WEEKDAYS[m["wd"]]
    delta = (target - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def _day_of_month(m: dict, today: date) -> date:
    day = int(m["day"] or m["day2"])
    weekday_word = m["wd"] or m["wd2"]
    if not 1 <= day <= 31:
        raise _Ambiguous("not a day of the month")
    year, month = today.year, today.month
    for _ in range(13):
        if day <= calendar.monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if candidate >= today:
                _check_weekday(candidate, weekday_word)
                return candidate
        month += 1
        if month == 13:
            year, month = year + 1, 1
    raise _Ambiguous("no such day")


class _NotADate(Exception):
    """The words matched a pattern but are not a date here ("you may")."""


_RULES: tuple[tuple[str, re.Pattern, object], ...] = (
    ("iso_date", re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"), _iso),
    ("numeric_date",
     re.compile(r"\b(?P<a>\d{1,2})[/.](?P<b>\d{1,2})(?:[/.](?P<y>\d{2,4}))?\b"), _numeric),
    ("day_month", re.compile(
        rf"\b(?:(?P<wd>{_WEEKDAY_ALT}) )?(?:the )?(?P<day>\d{{1,2}})(?:st|nd|rd|th)? (?:of )?"
        rf"(?P<month>{_MONTH_ALT})(?: (?P<year>\d{{4}}))?\b"), _day_month),
    ("day_month", re.compile(
        rf"\b(?:(?P<wd>{_WEEKDAY_ALT}) )?(?P<month>{_MONTH_ALT}) (?:the )?(?P<day>\d{{1,2}})"
        rf"(?:st|nd|rd|th)?(?: (?P<year>\d{{4}}))?\b"), _day_month),
    ("tomorrow", re.compile(r"\b(?P<after>(?:the )?day after )?tomorrow\b"), _tomorrow),
    ("vague_offset", re.compile(r"\b(?:a few|few|several|some) (?:days?|weeks?|months?)\b"),
     _vague_offset),
    ("offset", re.compile(
        rf"\b(?:in |after |within )?(?P<n>{_NUMBER_ALT}) (?:of )?(?P<unit>days?|weeks?|months?)"
        rf"(?: time| from now)?\b"), _offset),
    ("end_of_period", re.compile(
        r"\b(?:the )?end of (?:the |this )?(?P<period>month|quarter|year)\b"), _end_of_period),
    ("next_period", re.compile(
        r"\b(?:next|following|(?:start|beginning) of next|early next) (?P<period>week|month|quarter|year)\b"),
     _next_period),
    ("quarter", re.compile(
        r"\b(?P<after>after |post |once )?(?:the )?(?P<next>next )?(?:(?:q(?P<q>[1-4]))|"
        r"(?P<qword>first|second|third|fourth|1st|2nd|3rd|4th|last) quarter)"
        r"(?: (?:of )?(?P<year>\d{4}))?\b"), _quarter),
    ("half", re.compile(
        r"\b(?P<next>next )?(?:h(?P<h>[12])|(?P<hword>first|second) half(?: of the year)?)"
        r"(?: (?P<year>\d{4}))?\b"), _half),
    ("month", re.compile(
        rf"\b(?P<lead>in |until |after |by |around |from )?(?P<qual>early |beginning of |start of |"
        rf"mid-|mid |middle of |late |end of |the end of )?(?P<next>next )?"
        rf"(?P<month>{_MONTH_ALT})(?: (?P<year>\d{{4}}))?\b"), _month),
    ("year", re.compile(r"\b(?:in |during |until )(?P<year>20\d{2})\b"), _year),
    ("day_of_month", re.compile(
        rf"\b(?:(?P<wd>{_WEEKDAY_ALT}) )?(?:on )?the (?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b|"
        rf"\b(?:(?P<wd2>{_WEEKDAY_ALT}) )?(?P<day2>\d{{1,2}})(?:st|nd|rd|th)\b"), _day_of_month),
    ("weekday", re.compile(
        rf"\b(?:next |this |on |by |until )?(?P<wd>{_WEEKDAY_ALT})\b"), _weekday),
)


def resolve_date(phrase: str | None, received_at: datetime, zone: tzinfo) -> Resolution | None:
    """The date ``phrase`` names, read at ``received_at`` in ``zone``; ``None`` when unclear."""
    text = _clean(phrase or "")
    if not text:
        return None
    today = to_local(received_at, zone).date()
    found: list[Resolution] = []
    for name, pattern, handler in _RULES:
        while True:
            match = pattern.search(text)
            if match is None:
                break
            text = f"{text[:match.start()]} | {text[match.end():]}"
            try:
                found.append(Resolution(handler(match.groupdict(), today), name))
            except _NotADate:
                continue
            except (_Ambiguous, ValueError):
                return None
    dates = {r.date for r in found}
    if len(dates) != 1:
        return None
    result = found[0]
    if result.date < today:
        return None
    return result
```

Rules are tried most-specific first and each match is cut out of the text before the next rule runs, so "Tuesday, 29th September" is read once as a date instead of again as "next Tuesday". A weekday that contradicts the date it is attached to raises `_Ambiguous`, because either the day or the date is a typo and we cannot tell which.

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_dates.py -n0 -q`
Expected: `45 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/dates.py tests/test_engagement_dates.py
git commit -m "feat(engagement): resolve reply date phrases in code; ambiguity resolves to nothing"
```

---

### Task 7: Workspace engagement settings (D23)

**Files:**
- Create: `nexus/engagement/settings.py`
- Test: `tests/test_engagement_text_and_ids.py`

- [ ] **Step 1: Write the failing tests**

Replace the whole of `tests/test_engagement_text_and_ids.py` with the final version (it keeps the ULID and subject tests from Tasks 3–4 and adds the settings tests):

```python
"""Subjects keep exactly one "Re:" (D16); ULIDs sort by time; workspace settings stay in range (D23)."""
from __future__ import annotations

import pytest

from nexus.engagement.ids import new_ulid, ulid_timestamp_ms
from nexus.engagement.settings import (
    DEFAULTS,
    EngagementSettings,
    effective_confidence,
    read_settings,
    validate_mailbox_confidence,
    validate_update,
    with_settings,
)
from nexus.engagement.subjects import normalize_subject, reply_subject


@pytest.mark.parametrize("raw,base", [
    ("Re: RE: AW: Quick question", "Quick question"),
    ("RE : Fwd: [EXTERNAL] Re[2]: pricing", "pricing"),
    ("SV: VS: hello", "hello"),
    ("Antw: Odp: Ynt: x", "x"),
    ("TR: RV: y", "y"),
    ("  Re:   spaced   out ", "spaced out"),
    ("Revenue ops at Acme", "Revenue ops at Acme"),
    ("R&D hiring at Acme", "R&D hiring at Acme"),
    ("", ""),
])
def test_every_reply_and_forward_marker_is_removed(raw, base):
    assert normalize_subject(raw) == base


def test_a_reply_subject_carries_exactly_one_re():
    assert reply_subject("RE: Re: RE: hello") == "Re: hello"
    assert reply_subject(reply_subject("hello")) == "Re: hello"
    assert reply_subject("") == "Re:"


def test_ulids_sort_by_time_and_carry_their_timestamp():
    early, late = new_ulid(1_700_000_000_000), new_ulid(1_700_000_000_001)
    assert len(early) == 26 and early < late
    assert ulid_timestamp_ms(early) == 1_700_000_000_000
    assert len({new_ulid() for _ in range(5000)}) == 5000
    with pytest.raises(ValueError):
        ulid_timestamp_ms("not-a-ulid")


def test_settings_default_when_nothing_is_stored_and_tolerate_garbage():
    assert read_settings({}) == DEFAULTS
    # Each bad field falls back on its own; the valid ones are kept.
    stored = {"engagement": {"reply_confidence_default": "abc", "reply_confidence_min": 0.6,
                             "reply_confidence_max": 0.2, "ooo_default_days": 400}}
    settings = read_settings(stored)
    assert settings.reply_confidence_default == DEFAULTS.reply_confidence_default
    assert settings.reply_confidence_min == 0.6
    assert settings.reply_confidence_max == DEFAULTS.reply_confidence_max
    assert settings.ooo_default_days == DEFAULTS.ooo_default_days
    # An inverted range cannot be repaired field by field, so the whole range falls back.
    inverted = read_settings({"engagement": {"reply_confidence_min": 0.9,
                                             "reply_confidence_max": 0.7}})
    assert (inverted.reply_confidence_min, inverted.reply_confidence_max) == (
        DEFAULTS.reply_confidence_min, DEFAULTS.reply_confidence_max)


def test_a_manager_sets_a_range_and_the_default_must_sit_inside_it():
    updated = validate_update(DEFAULTS, {"reply_confidence_min": 0.7,
                                         "reply_confidence_max": 0.95,
                                         "reply_confidence_default": 0.85})
    assert (updated.reply_confidence_min, updated.reply_confidence_default,
            updated.reply_confidence_max) == (0.7, 0.85, 0.95)
    assert read_settings(with_settings({"accounts": []}, updated)) == updated
    with pytest.raises(ValueError, match="inside the range"):
        validate_update(DEFAULTS, {"reply_confidence_min": 0.9, "reply_confidence_default": 0.8})
    with pytest.raises(ValueError, match="between 0.5 and 0.99"):
        validate_update(DEFAULTS, {"reply_confidence_min": 0.3})
    with pytest.raises(ValueError, match="unknown"):
        validate_update(DEFAULTS, {"threshold": 0.9})


def test_an_sdrs_own_bar_must_sit_inside_the_workspace_range():
    settings = EngagementSettings(reply_confidence_default=0.8, reply_confidence_min=0.7,
                                  reply_confidence_max=0.9)
    assert validate_mailbox_confidence(settings, 0.75) == 0.75
    assert validate_mailbox_confidence(settings, None) is None
    with pytest.raises(ValueError, match="between 0.70 and 0.90"):
        validate_mailbox_confidence(settings, 0.95)
    assert effective_confidence(settings, None) == 0.8
    assert effective_confidence(settings, 0.95) == 0.9  # a range narrowed later still binds
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_text_and_ids.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.settings'`

- [ ] **Step 3: Implement**

Create `nexus/engagement/settings.py`:

```python
"""Workspace engagement settings: the reply confidence bar and its range (D23), and two timings.

Stored in ``Tenant.email_settings["engagement"]`` beside the mailboxes, signatures and style —
workspace preference rather than schema, so no migration and nothing for ``apply_rls.py`` to enrol.

**The confidence bar decides what happens without a human.** It governs ``later``, ``declined``
and ``unsubscribe``: a reading at or above it acts (snooze, block), a reading below it becomes
``unclear`` and waits for the SDR. Owners, admins and managers set the workspace default and the
range an SDR may choose inside; an SDR sets their own value for their own mailbox; a reply is judged
by its mailbox owner's value (spec §6).

The hard limits are 0.50 and 0.99. Below 0.50 a clear "no" would be acted on when the model thinks
it is more likely NOT a clear no; 1.00 would mean no reply is ever acted on, which is a switch
dressed as a threshold.

Reading never raises. A malformed stored value falls back to the default for that field, because a
typo in a JSON blob must not stop replies being processed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

HARD_MIN = 0.50
HARD_MAX = 0.99


@dataclass(frozen=True, slots=True)
class EngagementSettings:
    reply_confidence_default: float = 0.80
    reply_confidence_min: float = 0.50
    reply_confidence_max: float = 0.99
    # Remind the SDR when an interested/question reply is unanswered this long (§19).
    reply_reminder_business_hours: int = 4
    # Out-of-office with no return date: resume after this many days (spec §6, flagged default).
    ooo_default_days: int = 7


DEFAULTS = EngagementSettings()


def _float(raw, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return value if HARD_MIN <= value <= HARD_MAX else fallback


def _int(raw, fallback: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if low <= value <= high else fallback


def read_settings(email_settings: dict | None) -> EngagementSettings:
    """The effective settings for a workspace. Never raises."""
    stored = (email_settings or {}).get("engagement")
    if not isinstance(stored, dict):
        return DEFAULTS
    low = _float(stored.get("reply_confidence_min"), DEFAULTS.reply_confidence_min)
    high = _float(stored.get("reply_confidence_max"), DEFAULTS.reply_confidence_max)
    if low > high:
        low, high = DEFAULTS.reply_confidence_min, DEFAULTS.reply_confidence_max
    default = _float(stored.get("reply_confidence_default"), DEFAULTS.reply_confidence_default)
    default = min(max(default, low), high)
    return EngagementSettings(
        reply_confidence_default=default,
        reply_confidence_min=low,
        reply_confidence_max=high,
        reply_reminder_business_hours=_int(
            stored.get("reply_reminder_business_hours"),
            DEFAULTS.reply_reminder_business_hours, 1, 72,
        ),
        ooo_default_days=_int(stored.get("ooo_default_days"), DEFAULTS.ooo_default_days, 1, 60),
    )


def validate_update(current: EngagementSettings, patch: dict) -> EngagementSettings:
    """Apply ``patch`` to ``current``. Raises ``ValueError`` naming the problem; writes nothing."""
    unknown = sorted(set(patch) - set(asdict(current)))
    if unknown:
        raise ValueError(f"unknown engagement settings: {', '.join(unknown)}")
    merged = {**asdict(current), **patch}
    for key in ("reply_confidence_default", "reply_confidence_min", "reply_confidence_max"):
        try:
            merged[key] = round(float(merged[key]), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number between {HARD_MIN} and {HARD_MAX}") from exc
        if not HARD_MIN <= merged[key] <= HARD_MAX:
            raise ValueError(f"{key} must be between {HARD_MIN} and {HARD_MAX}")
    if merged["reply_confidence_min"] > merged["reply_confidence_max"]:
        raise ValueError("the lowest value SDRs may choose cannot be above the highest")
    if not merged["reply_confidence_min"] <= merged["reply_confidence_default"] <= merged[
        "reply_confidence_max"
    ]:
        raise ValueError("the workspace default must sit inside the range SDRs may choose from")
    for key, low, high in (("reply_reminder_business_hours", 1, 72), ("ooo_default_days", 1, 60)):
        try:
            merged[key] = int(merged[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a whole number from {low} to {high}") from exc
        if not low <= merged[key] <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}")
    return EngagementSettings(**merged)


def with_settings(email_settings: dict | None, settings: EngagementSettings) -> dict:
    """A NEW email_settings dict carrying ``settings`` (reassign it so SQLAlchemy sees the change)."""
    updated = dict(email_settings or {})
    updated["engagement"] = asdict(settings)
    return updated


def validate_mailbox_confidence(settings: EngagementSettings, value: float | None) -> float | None:
    """An SDR's own bar for their mailbox; ``None`` clears it. Raises outside the workspace range."""
    if value is None:
        return None
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("the confidence bar must be a number") from exc
    if not settings.reply_confidence_min <= number <= settings.reply_confidence_max:
        raise ValueError(
            f"choose a value between {settings.reply_confidence_min:.2f} and "
            f"{settings.reply_confidence_max:.2f}, the range your workspace allows"
        )
    return number


def effective_confidence(settings: EngagementSettings, mailbox_value: float | None) -> float:
    """The bar a reply to this mailbox is judged by: the SDR's own value, clamped to the range."""
    if mailbox_value is None:
        return settings.reply_confidence_default
    return min(max(float(mailbox_value), settings.reply_confidence_min),
               settings.reply_confidence_max)
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_text_and_ids.py -n0 -q`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/settings.py tests/test_engagement_text_and_ids.py
git commit -m "feat(engagement): workspace confidence bar and range, reminder hours, OOO default"
```

---

### Task 8: Engagement and ledger models

**Files:**
- Create: `nexus/models/engagement.py`, `nexus/models/ledger.py`
- Modify: `nexus/models/__init__.py`, `nexus/models/identity.py`, `nexus/models/calling.py`
- Test: `tests/test_engagement_models.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_engagement_models.py`:

```python
"""Engagement tables: the constraints that make a double send or a double block impossible."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy.exc import IntegrityError

from nexus.core.db import get_sessionmaker, utcnow
from nexus.models.account import Account, Contact
from nexus.models.engagement import (
    DoNotContact,
    EngagementCampaign,
    EngagementEnrollment,
    EngagementMessage,
    MailboxConnection,
)
from nexus.models.identity import User
from tests.conftest import make_tenant, tenant_session


async def _user(email: str = "sdr@acme.com") -> str:
    async with get_sessionmaker()() as s:
        user = User(email=email, full_name="Sam Rep", password_hash="x")
        s.add(user)
        await s.commit()
        return user.id


async def _world(ts, user_id: str) -> tuple[MailboxConnection, EngagementCampaign, Contact]:
    account = Account(name="Acme", domain="acme.io")
    ts.add(account)
    await ts.flush()
    contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
    mailbox = MailboxConnection(owner_user_id=user_id, provider="google", email="sdr@acme.com")
    ts.add_all([contact, mailbox])
    await ts.flush()
    campaign = EngagementCampaign(
        name="Q4 outbound", owner_user_id=user_id, mailbox_connection_id=mailbox.id
    )
    ts.add(campaign)
    await ts.flush()
    return mailbox, campaign, contact


async def test_a_contact_is_enrolled_in_a_campaign_once():
    tid = await make_tenant()
    uid = await _user()
    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            mailbox, campaign, contact = await _world(ts, uid)
            for _ in range(2):
                ts.add(EngagementEnrollment(
                    campaign_id=campaign.id, contact_id=contact.id,
                    account_id=contact.account_id, mailbox_connection_id=mailbox.id,
                ))
            await ts.flush()


async def test_one_outbound_message_per_step_while_inbound_is_unlimited():
    tid = await make_tenant()
    uid = await _user()
    async with tenant_session(tid) as ts:
        mailbox, campaign, contact = await _world(ts, uid)
        enrollment = EngagementEnrollment(
            campaign_id=campaign.id, contact_id=contact.id, account_id=contact.account_id,
            mailbox_connection_id=mailbox.id,
        )
        ts.add(enrollment)
        await ts.flush()
        for n in range(2):
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id, direction="in",
                status="received", step_index=0, provider_message_id=f"in-{n}",
            ))
        ts.add(EngagementMessage(
            mailbox_connection_id=mailbox.id, enrollment_id=enrollment.id, direction="out",
            status="queued", step_index=0,
        ))
        await ts.flush()
        enrollment_id, mailbox_id = enrollment.id, mailbox.id

    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox_id, enrollment_id=enrollment_id, direction="out",
                status="queued", step_index=0,
            ))
            await ts.flush()


async def test_an_idempotency_key_is_used_once_per_workspace():
    tid = await make_tenant()
    uid = await _user()
    async with tenant_session(tid) as ts:
        mailbox, _campaign, _contact = await _world(ts, uid)
        mailbox_id = mailbox.id
        ts.add(EngagementMessage(
            mailbox_connection_id=mailbox_id, direction="out", kind="reengage", status="queued",
            idempotency_key="reengage:enr1:2027-06-01",
        ))
    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            ts.add(EngagementMessage(
                mailbox_connection_id=mailbox_id, direction="out", kind="reengage",
                status="queued", idempotency_key="reengage:enr1:2027-06-01",
            ))
            await ts.flush()


async def test_an_address_has_one_active_block_and_can_be_blocked_again_after_a_lift():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        first = DoNotContact(email="jane@acme.io", reason="declined")
        ts.add(first)
        await ts.flush()
        first.lifted_at = utcnow()
        await ts.flush()
        ts.add(DoNotContact(email="jane@acme.io", reason="unsubscribed"))
        await ts.flush()
    with pytest.raises(IntegrityError):
        async with tenant_session(tid) as ts:
            ts.add(DoNotContact(email="jane@acme.io", reason="manual"))
            await ts.flush()


def test_every_engagement_table_is_enrolled_for_row_level_security():
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location("apply_rls", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    enrolled = set(module._tenant_tables())
    for table in (
        "mailbox_connections", "sequence_templates", "engagement_campaigns", "engagement_steps",
        "engagement_enrollments", "engagement_threads", "engagement_messages",
        "reply_classifications", "do_not_contact", "training_consents", "ledger_outbox",
    ):
        assert table in enrolled, f"{table} would get no row-level security policy"
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_models.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.models.engagement'`

- [ ] **Step 3: Create the engagement models**

Create `nexus/models/engagement.py`:

```python
"""SDR engagement engine: mailboxes, sequences, conversations, replies, do-not-contact (spec §4).

Every table is tenant-scoped, so ``scripts/apply_rls.py`` enrols it with no manual policy work.

Three shapes worth knowing before reading the columns:

* **An outbound message row exists BEFORE the provider call.** It is written ``queued`` and becomes
  ``sent`` with the provider's ids, so a send that times out can be reconciled against the Sent
  folder instead of retried blindly. Two unique indexes make a double send structurally impossible:
  ``(enrollment_id, step_index)`` for outbound step messages (partial, direction = 'out') and
  ``(tenant_id, idempotency_key)`` for everything else (re-engagements, responses).
* **Threads are the provider's threads.** ``engagement_threads`` maps one provider thread in one
  mailbox to the conversation; an enrollment's ``current_thread_id`` moves when the person replies
  from a different thread (D16). It is a plain column rather than a foreign key because threads
  also point at enrollments, and a two-way foreign key would make table creation order circular.
* **Do-not-contact is keyed by the normalised address, not the contact.** The same person can be a
  contact in several accounts or be re-imported; a block that lived on one contact row would miss
  the others (D7).

Column names follow the spec, except ``references`` → ``references_header``: REFERENCES is a
reserved word in Postgres.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

# ---- vocabularies ---------------------------------------------------------------------------
MAILBOX_PROVIDERS = ("google", "microsoft")
MAILBOX_STATUSES = ("connected", "needs_reauth", "revoked", "error")

CAMPAIGN_STATUSES = ("draft", "reviewing", "active", "paused", "completed")
FIRST_SEND_MODES = ("on_approval", "scheduled")
TIMEZONE_MODES = ("contact", "sdr")
STEP_CHANNELS = ("email", "call")
TIMING_MODES = ("auto", "manual")

ENROLLMENT_STATUSES = ("awaiting_review", "active", "paused", "snoozed", "stopped", "completed")
PAUSE_REASONS = (
    "colleague_replied", "out_of_office", "needs_decision", "mailbox_disconnected",
    "out_of_credits", "manual",
)
STOP_REASONS = ("replied", "declined", "unsubscribed", "bounced", "manual")

MESSAGE_DIRECTIONS = ("out", "in")
MESSAGE_STATUSES = ("draft", "approved", "queued", "sent", "failed", "bounced", "received")
MESSAGE_KINDS = ("step", "reengage", "response", "inbound")
INBOUND_KINDS = ("human", "auto_reply", "bounce", "other_auto")

REPLY_CATEGORIES = (
    "interested", "question", "referral", "later", "out_of_office", "declined", "unsubscribe",
    "other_auto", "unclear",
)
REPLY_DECISIONS = ("reengage", "block", "close", "meeting")
LABEL_SOURCES = ("ai", "deterministic", "sdr_confirmed", "sdr_corrected")

DNC_REASONS = ("unsubscribed", "declined", "bounced", "manual")


class MailboxConnection(IdMixin, TimestampMixin, TenantScoped, Base):
    """One SDR's Gmail or Microsoft 365 mailbox, connected by OAuth (D1, D2)."""

    __tablename__ = "mailbox_connections"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_mailbox_connection_email"),)

    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # {"enc": "<fernet>"} over {"access_token", "refresh_token", "expires_at", "token_type"}.
    tokens: Mapped[dict] = mapped_column(JSON, default=dict)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="connected", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gmail historyId or the Graph deltaLink URL, which is long.
    sync_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    notifications_expire_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    # Graph subscription id; Gmail watches have no id.
    notification_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    paused_until: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    signature: Mapped[str] = mapped_column(Text, default="")
    # The SDR's own confidence bar, accepted only inside the workspace range (D23). NULL = default.
    reply_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # IANA zone of the SDR; captured from the browser at connect, editable.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")


class SequenceTemplate(IdMixin, TimestampMixin, TenantScoped, Base):
    """A reusable list of step specs a campaign is built from (replaces Cadences)."""

    __tablename__ = "sequence_templates"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # [{"channel", "angle", "timing_mode", "delay_business_days", "send_time_local",
    #   "allowed_weekdays"}], in step order.
    steps: Mapped[list] = mapped_column(JSON, default=list)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    legacy_cadence_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class EngagementCampaign(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_campaigns"
    __table_args__ = (Index("ix_engagement_campaign_tenant_status", "tenant_id", "status"),)

    name: Mapped[str] = mapped_column(String(200))
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mailbox_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_connections.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="draft")
    pause_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_every_touch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_send_mode: Mapped[str] = mapped_column(String(16), default="on_approval")
    first_send_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    timezone_mode: Mapped[str] = mapped_column(String(10), default="contact")
    source_list_id: Mapped[str | None] = mapped_column(
        ForeignKey("prospect_lists.id"), nullable=True
    )
    template_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # {"worst_case": credits, "likely": credits, "by_capability": {...}, "computed_at": iso}
    credit_estimate: Mapped[dict] = mapped_column(JSON, default=dict)
    launched_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    legacy_campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class EngagementStep(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_steps"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step_index", name="uq_engagement_step_index"),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("engagement_campaigns.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(10), default="email")
    angle: Mapped[str] = mapped_column(Text, default="")
    timing_mode: Mapped[str] = mapped_column(String(10), default="auto")
    delay_business_days: Mapped[int] = mapped_column(Integer, default=0)
    # "HH:MM" in the recipient's (or SDR's, per timezone_mode) local time. Manual timing only.
    send_time_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    allowed_weekdays: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])


class EngagementEnrollment(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_enrollments"
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_engagement_enrollment_contact"),
        # The due-claim index: WHERE status = 'active' AND next_action_at <= now.
        Index("ix_engagement_enrollment_due", "status", "next_action_at"),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("engagement_campaigns.id"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    mailbox_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_connections.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="awaiting_review")
    status_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0)
    next_action_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    # True when a person moved the next step by hand; automatic rescheduling leaves it alone.
    next_action_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    snoozed_until: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    current_thread_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # The recipient's resolved IANA zone at enrollment (timekeeping.resolve_zone).
    contact_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    legacy_enrollment_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )


class EngagementThread(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_threads"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_connection_id", "provider_thread_id", name="uq_engagement_thread_provider"
        ),
    )

    mailbox_connection_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id"), index=True
    )
    provider_thread_id: Mapped[str] = mapped_column(String(255))
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True, index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True, index=True
    )
    base_subject: Mapped[str] = mapped_column(Text, default="")
    last_message_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class EngagementMessage(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "engagement_messages"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_connection_id", "provider_message_id", name="uq_engagement_message_provider"
        ),
        # One outbound message per step per enrollment, whatever the retries do.
        Index(
            "uq_engagement_message_out_step", "enrollment_id", "step_index", unique=True,
            postgresql_where=text("direction = 'out'"), sqlite_where=text("direction = 'out'"),
        ),
        Index(
            "uq_engagement_message_idempotency", "tenant_id", "idempotency_key", unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_engagement_message_thread_time", "thread_id", "created_at"),
    )

    mailbox_connection_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id"), index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_threads.id"), nullable=True
    )
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True, index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        ForeignKey("contacts.id"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(10), default="step")
    status: Mapped[str] = mapped_column(String(10))
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    references_header: Mapped[str] = mapped_column(Text, default="")
    # Our X-Nexus-Ref value (a ULID) — the reconciliation handle.
    ref_header: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    from_addr: Mapped[str] = mapped_column(String(320), default="")
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    # The AI's text as first drafted, kept so an SDR's edits can be measured (draft.edited, §18).
    ai_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_problems: Mapped[list] = mapped_column(JSON, default=list)
    inbound_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReplyClassification(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "reply_classifications"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_reply_classification_message"),
        Index("ix_reply_classification_desk", "tenant_id", "status", "category"),
    )

    message_id: Mapped[str] = mapped_column(ForeignKey("engagement_messages.id"))
    mailbox_connection_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_connections.id"), index=True
    )
    enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True, index=True
    )
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    date_phrase: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    label_source: Mapped[str] = mapped_column(String(16), default="ai")
    corrected_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    action_taken: Mapped[str] = mapped_column(String(40), default="")
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    suggested_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open")


class DoNotContact(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "do_not_contact"
    __table_args__ = (
        # One ACTIVE block per address; a lifted row stays as history and a new block can follow.
        Index(
            "uq_do_not_contact_active", "tenant_id", "email", unique=True,
            postgresql_where=text("lifted_at IS NULL"), sqlite_where=text("lifted_at IS NULL"),
        ),
    )

    email: Mapped[str] = mapped_column(String(320), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(16))
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    lifted_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lift_note: Mapped[str] = mapped_column(Text, default="")
```

Create `nexus/models/ledger.py`:

```python
"""Training & insights ledger: consent decisions and the transactional outbox (spec §18).

``training_consents`` is append-only: one row per decision, and the newest row is the one in
force. Keeping every decision rather than a flag on the tenant answers "when did this workspace
agree, to which terms, and who switched it off" without reading the audit log.

``ledger_outbox`` is written in the SAME transaction as the action it records, so an event exists
exactly when its action committed. The shipper reads it across tenants through the worker's owner
connection and marks ``shipped_archive_at``; rows are deleted seven days after that.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

CONSENT_STATUSES = ("on", "off", "pending")
CONSENT_SOURCES = ("signup", "prompt", "settings")


class TrainingConsent(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "training_consents"
    __table_args__ = (Index("ix_training_consent_tenant_decided", "tenant_id", "decided_at"),)

    status: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(10))
    terms_version: Mapped[str] = mapped_column(String(20))
    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(TZDateTime())


class LedgerOutbox(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "ledger_outbox"
    __table_args__ = (
        Index("uq_ledger_outbox_event", "event_id", unique=True),
        Index("ix_ledger_outbox_shipping", "shipped_archive_at", "occurred_at"),
    )

    event_id: Mapped[str] = mapped_column(String(26))
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime())
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    shipped_archive_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Register the models**

In `nexus/models/__init__.py`, after `from nexus.models.calling import CallActivity, CallTask`, add:

```python
from nexus.models.engagement import (
    DoNotContact,
    EngagementCampaign,
    EngagementEnrollment,
    EngagementMessage,
    EngagementStep,
    EngagementThread,
    MailboxConnection,
    ReplyClassification,
    SequenceTemplate,
)
from nexus.models.ledger import LedgerOutbox, TrainingConsent
```

and in `__all__`, after `"CallActivity",`, add:

```python
    "MailboxConnection",
    "SequenceTemplate",
    "EngagementCampaign",
    "EngagementStep",
    "EngagementEnrollment",
    "EngagementThread",
    "EngagementMessage",
    "ReplyClassification",
    "DoNotContact",
    "TrainingConsent",
    "LedgerOutbox",
```

- [ ] **Step 5: Add the two columns on existing models**

In `nexus/models/identity.py`, class `PendingRegistration`, after the `last_sent_at` line:

```python
    # The "Help improve the AI" choice made on the sign-up form (D24). It has to survive the OTP
    # step: the tenant, and so its consent row, is only created once the code is verified.
    training_consent: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="1"
    )
```

In `nexus/models/calling.py`, class `CallTask`, after the `cadence_step_index` line:

```python
    # Engagement engine linkage (spec §13). The cadence columns above stay as history.
    engagement_enrollment_id: Mapped[str | None] = mapped_column(
        ForeignKey("engagement_enrollments.id"), nullable=True
    )
```

- [ ] **Step 6: Run to see them pass**

Run: `pytest tests/test_engagement_models.py -n0 -q`
Expected: `5 passed` (the suite builds tables with `create_all`, so the migration is not needed yet)

- [ ] **Step 7: Commit**

```bash
git add nexus/models/engagement.py nexus/models/ledger.py nexus/models/__init__.py nexus/models/identity.py nexus/models/calling.py tests/test_engagement_models.py
git commit -m "feat(engagement): engagement and ledger models with double-send and double-block guards"
```

---

### Task 9: Migration `0057_engagement`

`tests/test_migrations_replay.py` builds a database from nothing but `alembic upgrade head` and compares it with the models, so it fails now (models without a migration) and passes once the migration matches.

**Files:**
- Create: `migrations/versions/0057_engagement.py`

- [ ] **Step 1: See the replay test fail**

Run: `pytest tests/test_migrations_replay.py -n0 -q`
Expected: FAIL — `tables in the models but not produced by the chain — a model was added without a migration: ['do_not_contact', 'engagement_campaigns', ...]`

- [ ] **Step 2: Write the migration**

Create `migrations/versions/0057_engagement.py`:

```python
"""engagement engine + training ledger: mailboxes, sequences, conversations, replies, consent

Additive only. Creates every table the SDR engagement engine and the training & insights ledger
need (spec §4), plus three columns on existing tables:

* ``pending_registrations.training_consent`` — the sign-up choice has to survive the OTP step,
  because the tenant (and so the consent row) is only created after the code is verified. Server
  default true, matching the pre-selected box (D24).
* ``call_tasks.engagement_enrollment_id`` — call steps link to the new enrollments; the old
  ``cadence_enrollment_id`` column stays for history.

Nothing reads these tables until the engagement code ships; every one carries ``tenant_id`` so
``scripts/apply_rls.py`` enrols it on deploy. The old campaign and cadence tables are untouched —
they remain as read-only history (D13).

Revision ID: 0057_engagement
Revises: 0056_alert_routing
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_engagement"
down_revision = "0056_alert_routing"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.String(length=32), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", sa.String(length=32), nullable=False)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    ]


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "mailbox_connections",
        _id(), _tenant(),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id"),
                  nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("tokens", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="connected"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sync_cursor", sa.Text(), nullable=True),
        _ts("last_synced_at"),
        _ts("notifications_expire_at"),
        sa.Column("notification_subscription_id", sa.String(length=255), nullable=True),
        _ts("paused_until"),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply_confidence", sa.Float(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        *_stamps(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_mailbox_connection_email"),
    )
    op.create_index("ix_mailbox_connections_tenant_id", "mailbox_connections", ["tenant_id"])
    op.create_index("ix_mailbox_connections_owner_user_id", "mailbox_connections",
                    ["owner_user_id"])
    op.create_index("ix_mailbox_connections_email", "mailbox_connections", ["email"])
    op.create_index("ix_mailbox_connections_status", "mailbox_connections", ["status"])
    op.create_index("ix_mailbox_connections_notification_subscription_id",
                    "mailbox_connections", ["notification_subscription_id"])

    op.create_table(
        "sequence_templates",
        _id(), _tenant(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        _ts("archived_at"),
        sa.Column("legacy_cadence_id", sa.String(length=32), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_sequence_templates_tenant_id", "sequence_templates", ["tenant_id"])
    op.create_index("ix_sequence_templates_legacy_cadence_id", "sequence_templates",
                    ["legacy_cadence_id"])

    op.create_table(
        "engagement_campaigns",
        _id(), _tenant(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), sa.ForeignKey("users.id"),
                  nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("pause_reason", sa.String(length=40), nullable=True),
        sa.Column("review_every_touch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_send_mode", sa.String(length=16), nullable=False,
                  server_default="on_approval"),
        _ts("first_send_at"),
        sa.Column("timezone_mode", sa.String(length=10), nullable=False,
                  server_default="contact"),
        sa.Column("source_list_id", sa.String(length=32), sa.ForeignKey("prospect_lists.id"),
                  nullable=True),
        sa.Column("template_id", sa.String(length=32), nullable=True),
        sa.Column("credit_estimate", sa.JSON(), nullable=False),
        _ts("launched_at"),
        _ts("completed_at"),
        sa.Column("legacy_campaign_id", sa.String(length=32), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_engagement_campaigns_tenant_id", "engagement_campaigns", ["tenant_id"])
    op.create_index("ix_engagement_campaign_tenant_status", "engagement_campaigns",
                    ["tenant_id", "status"])
    op.create_index("ix_engagement_campaigns_owner_user_id", "engagement_campaigns",
                    ["owner_user_id"])
    op.create_index("ix_engagement_campaigns_mailbox_connection_id", "engagement_campaigns",
                    ["mailbox_connection_id"])
    op.create_index("ix_engagement_campaigns_legacy_campaign_id", "engagement_campaigns",
                    ["legacy_campaign_id"])

    op.create_table(
        "engagement_steps",
        _id(), _tenant(),
        sa.Column("campaign_id", sa.String(length=32), sa.ForeignKey("engagement_campaigns.id"),
                  nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False, server_default="email"),
        sa.Column("angle", sa.Text(), nullable=False, server_default=""),
        sa.Column("timing_mode", sa.String(length=10), nullable=False, server_default="auto"),
        sa.Column("delay_business_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_time_local", sa.String(length=5), nullable=True),
        sa.Column("allowed_weekdays", sa.JSON(), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("campaign_id", "step_index", name="uq_engagement_step_index"),
    )
    op.create_index("ix_engagement_steps_tenant_id", "engagement_steps", ["tenant_id"])
    op.create_index("ix_engagement_steps_campaign_id", "engagement_steps", ["campaign_id"])

    op.create_table(
        "engagement_enrollments",
        _id(), _tenant(),
        sa.Column("campaign_id", sa.String(length=32), sa.ForeignKey("engagement_campaigns.id"),
                  nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=False),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="awaiting_review"),
        sa.Column("status_reason", sa.String(length=40), nullable=True),
        sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_action_at"),
        sa.Column("next_action_override", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        _ts("snoozed_until"),
        sa.Column("current_thread_id", sa.String(length=32), nullable=True),
        sa.Column("contact_timezone", sa.String(length=64), nullable=False,
                  server_default="UTC"),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("legacy_enrollment_id", sa.String(length=32), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_engagement_enrollment_contact"),
    )
    op.create_index("ix_engagement_enrollments_tenant_id", "engagement_enrollments",
                    ["tenant_id"])
    op.create_index("ix_engagement_enrollment_due", "engagement_enrollments",
                    ["status", "next_action_at"])
    for col in ("campaign_id", "contact_id", "account_id", "mailbox_connection_id",
                "current_thread_id", "legacy_enrollment_id"):
        op.create_index(f"ix_engagement_enrollments_{col}", "engagement_enrollments", [col])

    op.create_table(
        "engagement_threads",
        _id(), _tenant(),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=255), nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=True),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("engagement_enrollments.id"), nullable=True),
        sa.Column("base_subject", sa.Text(), nullable=False, server_default=""),
        _ts("last_message_at"),
        *_stamps(),
        sa.UniqueConstraint("mailbox_connection_id", "provider_thread_id",
                            name="uq_engagement_thread_provider"),
    )
    op.create_index("ix_engagement_threads_tenant_id", "engagement_threads", ["tenant_id"])
    for col in ("mailbox_connection_id", "contact_id", "account_id", "enrollment_id"):
        op.create_index(f"ix_engagement_threads_{col}", "engagement_threads", [col])

    op.create_table(
        "engagement_messages",
        _id(), _tenant(),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=False),
        sa.Column("thread_id", sa.String(length=32), sa.ForeignKey("engagement_threads.id"),
                  nullable=True),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("engagement_enrollments.id"), nullable=True),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="step"),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("rfc_message_id", sa.String(length=255), nullable=True),
        sa.Column("in_reply_to", sa.String(length=255), nullable=True),
        sa.Column("references_header", sa.Text(), nullable=False, server_default=""),
        sa.Column("ref_header", sa.String(length=40), nullable=True),
        sa.Column("from_addr", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("to_addrs", sa.JSON(), nullable=False),
        sa.Column("cc_addrs", sa.JSON(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_subject", sa.Text(), nullable=True),
        sa.Column("ai_body", sa.Text(), nullable=True),
        sa.Column("quality_problems", sa.JSON(), nullable=False),
        sa.Column("inbound_kind", sa.String(length=12), nullable=True),
        sa.Column("approved_by_user_id", sa.String(length=32), nullable=True),
        _ts("approved_at"),
        _ts("sent_at"),
        _ts("received_at"),
        sa.Column("error", sa.Text(), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("mailbox_connection_id", "provider_message_id",
                            name="uq_engagement_message_provider"),
    )
    op.create_index("ix_engagement_messages_tenant_id", "engagement_messages", ["tenant_id"])
    for col in ("mailbox_connection_id", "enrollment_id", "contact_id", "rfc_message_id",
                "ref_header"):
        op.create_index(f"ix_engagement_messages_{col}", "engagement_messages", [col])
    op.create_index("ix_engagement_message_thread_time", "engagement_messages",
                    ["thread_id", "created_at"])
    op.create_index(
        "uq_engagement_message_out_step", "engagement_messages", ["enrollment_id", "step_index"],
        unique=True, postgresql_where=sa.text("direction = 'out'"),
        sqlite_where=sa.text("direction = 'out'"),
    )
    op.create_index(
        "uq_engagement_message_idempotency", "engagement_messages",
        ["tenant_id", "idempotency_key"], unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "reply_classifications",
        _id(), _tenant(),
        sa.Column("message_id", sa.String(length=32), sa.ForeignKey("engagement_messages.id"),
                  nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=32),
                  sa.ForeignKey("mailbox_connections.id"), nullable=False),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("engagement_enrollments.id"), nullable=True),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("account_id", sa.String(length=32), sa.ForeignKey("accounts.id"),
                  nullable=True),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("date_phrase", sa.String(length=200), nullable=True),
        sa.Column("resolved_date", sa.Date(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("label_source", sa.String(length=16), nullable=False, server_default="ai"),
        sa.Column("corrected_category", sa.String(length=20), nullable=True),
        sa.Column("action_taken", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_by_user_id", sa.String(length=32), nullable=True),
        _ts("decided_at"),
        sa.Column("suggested_response", sa.Text(), nullable=True),
        sa.Column("assigned_user_id", sa.String(length=32), nullable=True),
        _ts("responded_at"),
        _ts("reminded_at"),
        sa.Column("status", sa.String(length=8), nullable=False, server_default="open"),
        *_stamps(),
        sa.UniqueConstraint("message_id", name="uq_reply_classification_message"),
    )
    op.create_index("ix_reply_classifications_tenant_id", "reply_classifications", ["tenant_id"])
    op.create_index("ix_reply_classification_desk", "reply_classifications",
                    ["tenant_id", "status", "category"])
    for col in ("mailbox_connection_id", "enrollment_id", "assigned_user_id"):
        op.create_index(f"ix_reply_classifications_{col}", "reply_classifications", [col])

    op.create_table(
        "do_not_contact",
        _id(), _tenant(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("contact_id", sa.String(length=32), sa.ForeignKey("contacts.id"),
                  nullable=True),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("source_message_id", sa.String(length=32), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=True),
        _ts("lifted_at"),
        sa.Column("lifted_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("lift_note", sa.Text(), nullable=False, server_default=""),
        *_stamps(),
    )
    op.create_index("ix_do_not_contact_tenant_id", "do_not_contact", ["tenant_id"])
    op.create_index("ix_do_not_contact_email", "do_not_contact", ["email"])
    op.create_index(
        "uq_do_not_contact_active", "do_not_contact", ["tenant_id", "email"], unique=True,
        postgresql_where=sa.text("lifted_at IS NULL"), sqlite_where=sa.text("lifted_at IS NULL"),
    )

    op.create_table(
        "training_consents",
        _id(), _tenant(),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("terms_version", sa.String(length=20), nullable=False),
        sa.Column("decided_by_user_id", sa.String(length=32), nullable=True),
        _ts("decided_at", nullable=False),
        *_stamps(),
    )
    op.create_index("ix_training_consents_tenant_id", "training_consents", ["tenant_id"])
    op.create_index("ix_training_consent_tenant_decided", "training_consents",
                    ["tenant_id", "decided_at"])

    op.create_table(
        "ledger_outbox",
        _id(), _tenant(),
        sa.Column("event_id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        _ts("occurred_at", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        _ts("shipped_archive_at"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_ledger_outbox_tenant_id", "ledger_outbox", ["tenant_id"])
    op.create_index("ix_ledger_outbox_event_type", "ledger_outbox", ["event_type"])
    op.create_index("uq_ledger_outbox_event", "ledger_outbox", ["event_id"], unique=True)
    op.create_index("ix_ledger_outbox_shipping", "ledger_outbox",
                    ["shipped_archive_at", "occurred_at"])

    op.add_column(
        "pending_registrations",
        sa.Column("training_consent", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # batch mode: SQLite cannot add a foreign key in place (same as 0056).
    with op.batch_alter_table("call_tasks") as batch:
        batch.add_column(
            sa.Column("engagement_enrollment_id", sa.String(length=32), nullable=True)
        )
        batch.create_foreign_key(
            "fk_call_tasks_engagement_enrollment_id", "engagement_enrollments",
            ["engagement_enrollment_id"], ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("call_tasks") as batch:
        batch.drop_constraint("fk_call_tasks_engagement_enrollment_id", type_="foreignkey")
        batch.drop_column("engagement_enrollment_id")
    with op.batch_alter_table("pending_registrations") as batch:
        batch.drop_column("training_consent")
    for table in (
        "ledger_outbox", "training_consents", "do_not_contact", "reply_classifications",
        "engagement_messages", "engagement_threads", "engagement_enrollments",
        "engagement_steps", "engagement_campaigns", "sequence_templates", "mailbox_connections",
    ):
        op.drop_table(table)
```

- [ ] **Step 3: Run the replay and RLS guards**

Run: `pytest tests/test_migrations_replay.py tests/test_rls_binding_guard.py tests/test_engagement_models.py -n0 -q`
Expected: `8 passed`

- [ ] **Step 4: Check there is exactly one head**

Run: `alembic heads`
Expected: `0057_engagement (head)`

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0057_engagement.py
git commit -m "feat(engagement): migration 0057 - engagement and ledger tables"
```

---

### Task 10: Document and verify the phase

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the migration head**

In `CLAUDE.md`, section "Migrations", change `Head: \`0056_alert_routing\`` to `Head: \`0057_engagement\`` and append to the chain sentence: `-> \`0057\` (engagement engine + training ledger tables, \`pending_registrations.training_consent\`, \`call_tasks.engagement_enrollment_id\`)`.

- [ ] **Step 2: Add a short section after "Alert routing and account ownership"**

```markdown
## SDR engagement engine (`nexus/engagement/`) — in progress

Replaces Campaigns and Cadences. Spec: `docs/superpowers/specs/2026-09-17-sdr-engagement-design.md`;
plans: `docs/superpowers/plans/2026-09-17-sdr-engagement/`. Ships dark behind
`engagement_campaigns_enabled` until the cutover (phase 15).

- **Rules live in pure modules, and nothing re-derives them.** `subjects.reply_subject` is the only
  way to build a follow-up subject (exactly one "Re:", D16). `dates.resolve_date` is the only way a
  reply's "try me in June" becomes a date, and an ambiguous phrase resolves to `None` so a person
  decides (D8). `timekeeping` owns business days (Mon–Fri, no holiday table) and the recipient's
  timezone (contact `custom_fields["timezone"]` → account country/region → SDR mailbox).
- **Double sends are prevented by the schema, not by care.** An outbound message row is written
  `queued` before the provider call; a partial unique index on `(enrollment_id, step_index)` where
  `direction = 'out'` and one on `(tenant_id, idempotency_key)` make a second send of the same step,
  re-engagement or response an `IntegrityError`.
- **No mocks (D21).** New tests use real inputs, real SQLite rows, or the live suites in
  `tests_live/engagement/`. `tzdata` is a dependency because `zoneinfo` has no zone database on
  Windows.
```

- [ ] **Step 3: Full verification**

Run: `ruff check nexus tests`
Expected: `All checks passed!`

Run: `pytest -n auto -p no:cacheprovider --timeout=120 -q`
Expected: the whole suite passes (the count rises by 74).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(engagement): migration head 0057 and the engagement engine's ground rules"
```

---

## Spec coverage for this phase

| Spec item | Task |
|---|---|
| §4 every table and column (plus `references` → `references_header`, `timezone`, `idempotency_key`, `ai_subject`/`ai_body`, `kind`, `inbound_kind`, review and reply-desk columns) | 8, 9 |
| §4 partial unique `(enrollment_id, step_index)` where `direction = 'out'` | 8, 9 |
| §4 every table carries `tenant_id` (RLS) | 8 (test), 9 |
| §4 `CallTask.engagement_enrollment_id` | 8, 9 |
| §6 date resolution rules; ambiguity → unclear | 6 |
| §6 / D23 confidence default 0.8, range 0.5–0.99, SDR value inside range, mailbox-owner value wins | 7 |
| §6 OOO default +7 days (setting) | 7 |
| §8 business days, contact timezone resolution, auto and manual timing | 5 |
| §18.1 ULID event ids | 3 |
| D16 exactly one "Re:" | 4 |
| D24 sign-up choice survives OTP | 8, 9 |
| Roadmap §3 permissions | 2 |
