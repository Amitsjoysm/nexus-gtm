"""Typeahead for Prospector's text filters: the provider's own values, learned, then seeds.

Asked 2026-11-19: "industries ... appear as we enter 2-3 letters, and in all other text based
filters". A text filter matches what the provider stores, so the best suggestion is a value seen in
its own answers: "Hospital & Health Care" where a rep might type "Hospitals" and match nothing
(measured on the live API: `IndustryTypes: [Hospitals]` returned no rows). So:

* **Learned** (`learn`): every page bought from the provider adds its values per field, with a
  count, to ``prospector_vocabulary`` (platform-global; the provider's data is the same for every
  workspace). Only business facts: titles, departments, industries, sub-industries, SIC codes,
  technologies, keywords, countries, states, cities, company names. Never a person's name, email
  or phone, nor a domain or LinkedIn page, which are masked and paid for.
* **Seeds** (`SEEDS`) cover a field before anything is learned: the LinkedIn industry list, the
  buying-committee title catalogue, countries, US states and Canadian provinces, departments and
  common B2B tools. Learned values rank first, most seen first; seeds fill what is left.

Total, like the page cache: learning or suggesting that fails returns nothing and never fails the
search or the screen.
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.prospector import ProspectorVocabulary

logger = logging.getLogger("nexus.prospector.vocabulary")

#: Filter key -> vocabulary field. Include and exclude forms, person and company places, share one.
FIELD_OF: dict[str, str] = {
    "titles": "title",
    "departments": "department",
    "industries": "industry", "exclude_industries": "industry",
    "sub_industries": "sub_industry", "exclude_sub_industries": "sub_industry",
    "sic_codes": "sic_code",
    "technologies": "technology",
    "keywords": "keyword", "exclude_keywords": "keyword",
    "person_countries": "country", "company_countries": "country",
    "person_states": "state", "company_states": "state",
    "person_cities": "city", "company_cities": "city",
    "company_names": "company_name",
}

#: Vocabulary field -> the provider item keys it is read from (lowercased), and whether one value
#: holds a comma-separated list ("Salesforce, HubSpot, Zendesk").
_SOURCES: dict[str, tuple[tuple[str, ...], bool]] = {
    "title": (("title",), False),
    "department": (("department",), False),
    "industry": (("industrytype", "industry"), False),
    "sub_industry": (("subindustry",), False),
    "sic_code": (("sic_code", "siccode"), False),
    "technology": (("technology", "technologies"), True),
    "keyword": (("keywords",), True),
    "country": (("country",), False),
    "state": (("state",), False),
    "city": (("city",), False),
    "company_name": (("companyname",), False),
}

_PLACEHOLDERS = frozenset({"", "null", "none", "n/a", "na", "-", "not available", "unknown"})
MAX_VALUE = 120
#: A list field can hold dozens of tools; the first few are the ones the company leads with.
MAX_LIST_ITEMS = 25
MIN_QUERY = 2
DEFAULT_LIMIT = 10


def suggestible(filter_key: str) -> bool:
    return filter_key in FIELD_OF


def _clean(raw) -> str:
    value = " ".join(str(raw or "").split()).strip(" ,;")
    if value.lower() in _PLACEHOLDERS or len(value) < 2 or len(value) > MAX_VALUE:
        return ""
    return value


def _values(item: dict, field: str) -> list[str]:
    keys, is_list = _SOURCES[field]
    lowered = {str(k).lower(): v for k, v in item.items()}
    out: list[str] = []
    for key in keys:
        raw = lowered.get(key)
        if raw in (None, ""):
            continue
        parts = str(raw).split(",")[:MAX_LIST_ITEMS] if is_list else [raw]
        out.extend(v for v in (_clean(p) for p in parts) if v)
        break
    return out


def _key(view: str, field: str, value: str) -> str:
    return hashlib.sha1(f"{view}|{field}|{value.lower()}".encode()).hexdigest()


def harvest(view: str, items: list[dict]) -> Counter:
    """``(field, value) -> times seen`` in one page, the original spelling kept."""
    seen: Counter = Counter()
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in _SOURCES:
            for value in set(_values(item, field)):
                seen[(field, value)] += 1
    return seen


async def learn(view: str, items: list[dict]) -> None:
    """Add a provider page's values to the vocabulary. Never raises."""
    try:
        counts = harvest(view, items)
        if not counts:
            return
        now = utcnow()
        rows = {_key(view, field, value): (field, value, n) for (field, value), n in counts.items()}
        async with get_platform_sessionmaker()() as session:
            ids = list(rows)
            existing: dict[str, ProspectorVocabulary] = {}
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for row in (await session.scalars(select(ProspectorVocabulary).where(
                        ProspectorVocabulary.id.in_(chunk)))).all():
                    existing[row.id] = row
            for key, (field, value, n) in rows.items():
                row = existing.get(key)
                if row is None:
                    session.add(ProspectorVocabulary(
                        id=key, view=view, field=field, value=value[:160],
                        value_lower=value.lower()[:160], seen=n, last_seen_at=now))
                else:
                    row.seen = int(row.seen or 0) + n
                    row.last_seen_at = now
            await session.commit()
    except IntegrityError:
        logger.debug("prospector vocabulary write lost a race; the next page learns it")
    except Exception:
        logger.warning("prospector vocabulary learning failed", exc_info=True)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rank(values: list[str], q: str) -> list[str]:
    """Values starting with the query first, then those containing it, each in order given."""
    starts = [v for v in values if v.lower().startswith(q)]
    contains = [v for v in values if q in v.lower() and not v.lower().startswith(q)]
    return starts + contains


async def suggest(view: str, filter_key: str, query: str, *,
                  limit: int = DEFAULT_LIMIT) -> list[str]:
    """Up to ``limit`` values for a filter that start with, then contain, ``query``."""
    field = FIELD_OF.get(filter_key)
    q = " ".join((query or "").split()).lower()
    if field is None or len(q) < MIN_QUERY:
        return []
    limit = max(1, min(int(limit), 25))
    out: list[str] = []
    try:
        pattern = f"%{_escape(q)}%"
        async with get_platform_sessionmaker()() as session:
            learned = (await session.scalars(
                select(ProspectorVocabulary.value)
                .where(ProspectorVocabulary.view == view, ProspectorVocabulary.field == field,
                       ProspectorVocabulary.value_lower.like(pattern, escape="\\"))
                .order_by(ProspectorVocabulary.seen.desc(), ProspectorVocabulary.value)
                .limit(limit * 4))).all()
        out = _rank(list(learned), q)
    except Exception:
        logger.warning("prospector suggestions could not be read", exc_info=True)
    taken = {v.lower() for v in out}
    for value in _rank(list(seeds(field)), q):
        if value.lower() not in taken:
            out.append(value)
            taken.add(value.lower())
    return out[:limit]


# ---- seeds ----------------------------------------------------------------------------------

_COUNTRIES = (
    "United States", "United Kingdom", "Canada", "Australia", "India", "Germany", "France",
    "Netherlands", "Ireland", "Spain", "Italy", "Sweden", "Norway", "Denmark", "Finland",
    "Switzerland", "Austria", "Belgium", "Portugal", "Poland", "Czech Republic", "Romania",
    "Hungary", "Greece", "Israel", "United Arab Emirates", "Saudi Arabia", "Qatar", "Turkey",
    "South Africa", "Nigeria", "Kenya", "Egypt", "Singapore", "Malaysia", "Indonesia",
    "Philippines", "Thailand", "Vietnam", "Japan", "South Korea", "China", "Hong Kong", "Taiwan",
    "New Zealand", "Brazil", "Mexico", "Argentina", "Chile", "Colombia", "Peru", "Pakistan",
    "Bangladesh", "Sri Lanka", "Luxembourg", "Estonia", "Latvia", "Lithuania", "Ukraine",
    "Bulgaria", "Croatia", "Serbia", "Slovakia", "Slovenia", "Iceland", "Cyprus", "Malta",
    "Morocco",
)
_STATES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
    "District of Columbia", "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Nova Scotia", "Ontario", "Quebec", "Saskatchewan",
)
_DEPARTMENTS = (
    "Sales", "Marketing", "Engineering", "Information Technology", "Finance", "Human Resources",
    "Operations", "Customer Success", "Customer Support", "Product", "Legal", "Procurement",
    "Executive", "Business Development", "Research and Development", "Supply Chain",
    "Manufacturing", "Quality", "Security", "Data", "Design", "Administration",
)
_TECHNOLOGIES = (
    "Salesforce", "HubSpot", "Marketo", "Pardot", "Microsoft Dynamics", "Zoho", "Pipedrive",
    "Outreach", "Salesloft", "Gong", "ZoomInfo", "Apollo", "Clearbit", "LinkedIn Sales Navigator",
    "Zendesk", "Intercom", "Freshdesk", "ServiceNow", "Jira", "Confluence", "Slack",
    "Microsoft Teams", "Zoom", "Google Workspace", "Microsoft Office 365", "Outlook", "Gmail",
    "Amazon AWS", "Microsoft Azure", "Google Cloud", "Snowflake", "Databricks", "Tableau",
    "Power BI", "Looker", "Segment", "Mixpanel", "Amplitude", "Google Analytics", "Hotjar",
    "Shopify", "Magento", "WordPress", "Webflow", "Stripe", "PayPal", "NetSuite", "SAP",
    "Oracle", "Workday", "QuickBooks", "Xero", "Mailchimp", "SendGrid", "Twilio", "Okta",
    "Cloudflare", "GitHub", "GitLab", "Atlassian", "Asana", "Monday.com", "Notion", "DocuSign",
)


@lru_cache(maxsize=None)
def seeds(field: str) -> tuple[str, ...]:
    if field == "industry" or field == "sub_industry":
        from nexus.prospecting.industries import all_industries

        return tuple(sorted({i.label for i in all_industries().values()}))
    if field == "title":
        from nexus.relevance.titles import ROLES

        titles: list[str] = []
        for role in ROLES:
            for title in (role.title, *role.alternatives):
                if title not in titles:
                    titles.append(title)
        return tuple(titles)
    return {"country": _COUNTRIES, "state": _STATES, "department": _DEPARTMENTS,
            "technology": _TECHNOLOGIES}.get(field, ())
