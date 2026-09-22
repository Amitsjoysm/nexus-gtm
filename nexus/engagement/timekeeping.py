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
