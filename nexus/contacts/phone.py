# nexus/contacts/phone.py
"""Phone numbers in one canonical shape: E.164.

Phone numbers arrive from four places that agree on nothing — a CRM import, an Apify actor, a
manual edit, an enrichment provider — as ``(415) 555-2671``, ``+1 415-555-2671``, ``00 1 415 555
2671`` and ``415.555.2671``. Stored raw, the same person is four contacts, dedupe never fires, and a
dialler is handed a string it cannot call.

**E.164** (``+14155552671``) is the format telephony providers actually accept, and it is the only
representation where string equality means "same number".

Two rules that matter more than the parsing:

* **A number we cannot parse is kept, not dropped.** ``normalise_phone`` returns the E.164 form and
  the original; the caller stores both. Discarding an unparseable number to keep the column clean
  would lose the only contact detail for that person on the strength of a guess about its country.
* **The region is a cascade, never a constant.** The person's own country wins, then the account's,
  then US. A default applied to a number that had a better answer available is how a UK mobile
  becomes an unroutable US number, and nothing downstream can tell.

Standard library only. ``phonenumbers`` carries Google's full metadata set and would be the right
answer for validating national number *length* per region; the table below covers dialling codes,
which is what canonical storage needs. Numbers are normalised, not validated — claiming a number is
invalid on incomplete metadata is worse than storing it as given.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ISO-3166 alpha-2 -> country calling code, for the regions this product actually sells into.
# Deliberately not exhaustive: an unknown region falls back to the default rather than guessing.
_COUNTRY_CODES: dict[str, str] = {
    "US": "1", "CA": "1", "GB": "44", "UK": "44", "IE": "353", "IN": "91", "AU": "61",
    "NZ": "64", "DE": "49", "FR": "33", "ES": "34", "IT": "39", "NL": "31", "BE": "32",
    "CH": "41", "AT": "43", "SE": "46", "NO": "47", "DK": "45", "FI": "358", "PL": "48",
    "PT": "351", "CZ": "420", "RO": "40", "GR": "30", "IL": "972", "AE": "971", "SA": "966",
    "ZA": "27", "NG": "234", "KE": "254", "EG": "20", "BR": "55", "MX": "52", "AR": "54",
    "CL": "56", "CO": "57", "JP": "81", "KR": "82", "CN": "86", "HK": "852", "SG": "65",
    "MY": "60", "ID": "62", "TH": "66", "PH": "63", "VN": "84", "TR": "90", "RU": "7",
    "UA": "380", "PK": "92", "BD": "880", "LK": "94",
}

# Country names people actually type into a country field, mapped to the ISO code. Only the ones
# that are ambiguous or common enough to matter — anything else falls through to the default.
_COUNTRY_NAMES: dict[str, str] = {
    "united states": "US", "usa": "US", "us": "US", "united states of america": "US",
    "america": "US", "canada": "CA", "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "ireland": "IE", "india": "IN",
    "australia": "AU", "new zealand": "NZ", "germany": "DE", "deutschland": "DE",
    "france": "FR", "spain": "ES", "italy": "IT", "netherlands": "NL", "holland": "NL",
    "belgium": "BE", "switzerland": "CH", "austria": "AT", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "poland": "PL", "portugal": "PT", "israel": "IL",
    "united arab emirates": "AE", "uae": "AE", "singapore": "SG", "japan": "JP",
    "south korea": "KR", "china": "CN", "hong kong": "HK", "brazil": "BR", "mexico": "MX",
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "turkey": "TR", "pakistan": "PK",
}

DEFAULT_REGION = "US"

# Longest dialling codes first, so "1" never shadows "1" inside a longer code and 44 is not read
# as 4 + 4. Rebuilt once at import.
_CODES_BY_LENGTH = sorted(set(_COUNTRY_CODES.values()), key=len, reverse=True)

# Anything that is not a digit or a leading +. Extensions are cut before this runs.
_NON_DIAL = re.compile(r"[^\d+]")
# "x123", "ext. 123", "ext 123" — an extension is not part of the E.164 number.
_EXTENSION = re.compile(r"(?:\s*(?:x|ext\.?|extension)\s*\d+)\s*$", re.I)


# Values a scraper puts in a phone field when it has no phone. Every one of these was accepted as
# a phone number before this check existed: they are non-empty strings, so "did we find something?"
# said yes, the lookup was recorded as `found`, and the junk was cached in the SHARED person record
# — suppressing re-lookup for every tenant until the TTL expired. A miss has to look like a miss.
_SENTINELS = frozenset({
    "n/a", "na", "n.a.", "none", "null", "nil", "-", "--", "—", "unknown", "not available",
    "notavailable", "not found", "notfound", "no phone", "nophone", "unavailable", "hidden",
    "private", "redacted", "premium", "premium feature", "upgrade", "locked", "restricted",
    "0", "00", "000", "false", "true", "undefined",
})

# A dialable number, once any extension is removed: an optional +, then digits and the separators
# real data actually uses. Deliberately rejects letters — a vanity number ("1-800-FLOWERS") is not
# something a dialler can place, and letters are overwhelmingly a sign the field holds prose.
_PHONE_SHAPE = re.compile(r"^\+?[\d(][\d\s().\-/+]*\d$")

# E.164 allows at most 15 digits. Fewer than 7 cannot be a routable international number, and short
# strings are where junk concentrates ("0", "123", a row id that landed in the wrong column).
_MIN_DIGITS, _MAX_DIGITS = 7, 15


def looks_like_phone(value: str | None) -> bool:
    """Whether ``value`` is plausibly a phone number at all.

    This is a *shape and sanity* gate, not validation — it runs before ``normalise_phone`` and its
    job is to keep non-numbers out of the pipeline entirely. Being wrong in the strict direction
    costs one lookup that reports `not_found` and will be retried; being wrong in the loose
    direction writes a scraper's "Premium feature" string into the shared person store as a phone
    number, where it is both wrong and cached.
    """
    raw = (value or "").strip()
    if not raw:
        return False
    if raw.lower() in _SENTINELS:
        return False
    body = _EXTENSION.sub("", raw).strip()
    if not body or not _PHONE_SHAPE.match(body):
        return False
    digits = re.sub(r"\D", "", body)
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return False
    # "0000000000", "1111111111" — placeholder rows that satisfy every check above.
    return len(set(digits)) > 1


@dataclass(slots=True)
class NormalisedPhone:
    """The canonical form plus what came in.

    ``raw`` is always populated. When ``e164`` is empty the number could not be parsed, and the
    caller stores the raw value — losing a contact's only phone number to keep a column tidy is a
    worse outcome than an unnormalised row.
    """

    e164: str = ""
    raw: str = ""
    region: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.e164)


def region_for(country: str | None) -> str:
    """An ISO-3166 alpha-2 region from whatever a country field happens to contain, or ""."""
    value = (country or "").strip()
    if not value:
        return ""
    upper = value.upper()
    if len(upper) == 2 and upper in _COUNTRY_CODES:
        return upper
    return _COUNTRY_NAMES.get(value.lower(), "")


def resolve_region(*candidates: str | None, default: str = DEFAULT_REGION) -> str:
    """First usable region among the candidates, else the default.

    Callers pass the cascade in priority order — the person's country, then their account's. The
    default only applies when nothing else answered, so a number that had better information
    available never gets a guessed country code.
    """
    for candidate in candidates:
        region = region_for(candidate)
        if region:
            return region
    return default


def normalise_phone(
    value: str | None, *, region: str = "", country: str | None = None,
    account_country: str | None = None, default: str = DEFAULT_REGION,
) -> NormalisedPhone:
    """Best-effort E.164. Never raises, never discards the input.

    ``region`` (an explicit ISO code) wins if given; otherwise the cascade runs over ``country``
    then ``account_country`` then ``default``.
    """
    raw = (value or "").strip()
    if not raw:
        return NormalisedPhone()

    chosen = region_for(region) or resolve_region(country, account_country, default=default)

    # An extension is dialled after the call connects; it is not part of the number.
    body = _EXTENSION.sub("", raw)
    # "00" is the international prefix in most of the world; "011" in NANP. Both mean "+".
    stripped = _NON_DIAL.sub("", body)
    if stripped.startswith("+"):
        digits, explicit_intl = stripped[1:], True
    elif stripped.startswith("00"):
        digits, explicit_intl = stripped[2:], True
    elif stripped.startswith("011") and chosen in ("US", "CA"):
        digits, explicit_intl = stripped[3:], True
    else:
        digits, explicit_intl = stripped, False

    digits = re.sub(r"\D", "", digits)
    if not digits:
        return NormalisedPhone(raw=raw, region=chosen)

    if explicit_intl:
        # The caller already told us it is international; trust the number's own country code.
        if 7 <= len(digits) <= 15:
            return NormalisedPhone(e164=f"+{digits}", raw=raw, region=chosen)
        return NormalisedPhone(raw=raw, region=chosen)

    code = _COUNTRY_CODES.get(chosen, _COUNTRY_CODES[DEFAULT_REGION])

    # A national number sometimes already carries its own country code with no "+" (a CRM export
    # that dropped it). Only strip it when what remains is still a plausible national number,
    # otherwise "1234567890" — a real 10-digit US number — would lose its leading 1.
    if digits.startswith(code) and _plausible_national(digits[len(code):], chosen):
        digits = digits[len(code):]

    # Trunk prefix: 0 nationally in most of the world, dropped when dialling internationally.
    if digits.startswith("0") and chosen not in ("US", "CA"):
        digits = digits.lstrip("0")

    if not _plausible_national(digits, chosen):
        return NormalisedPhone(raw=raw, region=chosen)
    return NormalisedPhone(e164=f"+{code}{digits}", raw=raw, region=chosen)


def _plausible_national(digits: str, region: str) -> bool:
    """Whether ``digits`` could be a national number in ``region``.

    A length check, not validation. NANP is exactly 10 and worth pinning because it is the default
    and the most common source of junk; everywhere else takes a wide band, because rejecting a real
    number on incomplete metadata is worse than storing one that turns out to be wrong.
    """
    if not digits:
        return False
    if region in ("US", "CA"):
        return len(digits) == 10
    return 6 <= len(digits) <= 14
