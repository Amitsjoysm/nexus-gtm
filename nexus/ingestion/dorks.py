# nexus/ingestion/dorks.py
"""Search dorks: high-precision queries that find one *kind* of buying signal.

``WebNewsSource`` fires a single broad OR-query per account —
``"<name> <industry> (funding OR hiring OR launches OR partnership OR acquisition)"`` — and takes
the first six results. That is one query competing against itself: a funding round, a job posting
and a product launch all fight for the same six slots, so a strong signal is routinely crowded out
by three weak press mentions, and the six results are ranked by *relevance*, which for a
well-covered company means the most-linked article, not the most recent one.

A dork fixes both by narrowing the question:

* **One query per signal kind**, so each kind gets its own result budget and a funding round can
  never be displaced by a launch blog post.
* **Scoped to the publishers that actually carry that signal.** Funding rounds appear on
  TechCrunch/Crunchbase/PR wires; job postings on the ATS hosts (Greenhouse/Lever/Ashby); leadership
  changes on the company newsroom. ``site:`` turns "search the web and hope" into "look where this
  is published", which is most of the precision.
* **Recency in the query itself.** The default provider is DuckDuckGo HTML, which has no date
  filter — Google's ``after:`` and ``tbs=qdr:`` do not work there. What *does* work everywhere is
  ``inurl:`` on the year (news CMSs put the date in the path: ``/2026/07/…``) and the year as a
  literal token. Providers that support a real date filter get one via
  ``SearchProvider.search_recent``; the dork stays useful when they do not.

Operators used here are deliberately limited to the intersection every engine supports —
``site:``, ``-site:``, ``intitle:``, ``inurl:``, quoted phrases, ``OR``, ``-``. No ``after:``, no
``filetype:`` chains, no engine-specific syntax: a dork that silently degrades to a keyword soup on
three of four providers is worse than a plainer one that works on all of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Publishers that actually break each signal class. Kept short on purpose: every extra site widens
# the query, and a `site:` list long enough to cover everything stops narrowing anything.
_FUNDING_SITES = (
    # news.crunchbase.com, NOT crunchbase.com: the bare domain is a company **directory**, and
    # including it returned "Vanta - Crunchbase Company Profile & Funding" — a profile page that
    # matches the name perfectly, reports no event, and is exactly what `_NOISE_SITES` exists to
    # keep out. The news subdomain is the publisher.
    "techcrunch.com", "news.crunchbase.com", "venturebeat.com", "axios.com",
    "prnewswire.com", "businesswire.com", "finsmes.com", "eu-startups.com",
)
_ATS_SITES = (
    "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "apply.workable.com", "job-boards.greenhouse.io",
)
_EXEC_SITES = ("prnewswire.com", "businesswire.com", "globenewswire.com", "linkedin.com/posts")

# Aggregators that rank well and carry nothing: a company directory page matches the account name
# perfectly and reports no event at all. Excluding them is worth more than any added keyword.
_NOISE_SITES = (
    "zoominfo.com", "rocketreach.co", "signalhire.com", "leadiq.com", "apollo.io",
    "glassdoor.com", "indeed.com", "wikipedia.org", "bloomberg.com/profile",
    "pitchbook.com/profiles", "dnb.com", "zippia.com", "owler.com", "craft.co",
)


def _exclusions(sites: tuple[str, ...]) -> str:
    return " ".join(f"-site:{s}" for s in sites)


def _sites(sites: tuple[str, ...]) -> str:
    """``(site:a OR site:b)`` — an OR-group, not repeated ``site:`` terms, which every engine reads
    as an impossible AND and answers with nothing."""
    return "(" + " OR ".join(f"site:{s}" for s in sites) + ")"


@dataclass(frozen=True, slots=True)
class Dork:
    """One search intent, expressed in both dialects, plus what a hit from it means.

    **Two renderings, because the backends are not the same kind of thing.** ``template`` is the
    Google-style operator query for keyword engines. ``phrase`` states the same intent in plain
    words for neural engines, which read operators as literal text — measured on Exa, the operator
    form of the ATS dork returns other companies' job posts that mention the account name, while the
    phrase form returns the account's own careers page. Domains then travel structurally via
    ``sites``/``exclude`` instead of inside the query.

    Writing only one of the two would silently halve the library on whichever backend is configured,
    and nothing in the output would say so — the source would just return fewer signals.

    ``kind``/``strength`` are the *prior* the dork carries.
    """

    slug: str
    kind: str
    template: str
    strength: float
    # Natural-language form for `semantic` backends. Same placeholders as `template`.
    phrase: str = ""
    # Structured domain filters for semantic backends; the keyword `template` already embeds these
    # as site:/-site: terms.
    sites: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    # Trust the dork's own `kind`/`strength` over the headline classifier.
    #
    # Set this only where the URL PATTERN settles the event class: a hit on an ATS board is a job
    # posting whatever its title says. Do NOT set it merely because the dork is scoped to relevant
    # publishers — TechCrunch carries funding, acquisitions and launches from the same paths, so
    # trusting the dork there stamps one kind on all three.
    trust_kind: bool = False
    # A hit here is about this company by construction (its own domain, its own ATS board), so the
    # name-in-text check would only reject legitimate hits — a job posting titled
    # "Senior Engineer, Platform" names the company nowhere in the snippet.
    self_evident: bool = False
    # Cheap relevance floor for open-web dorks: a hit must contain at least one of these.
    require_any: tuple[str, ...] = ()

    def render(self, *, name: str, domain: str, industry: str, now: datetime,
               dialect: str = "plain") -> str:
        """The query to send, in the dialect this backend understands.

        Only ``operator`` gets the operator template. ``plain`` and ``semantic`` both get the
        phrase, for different reasons — the first because operators return nothing there, the
        second because they return the wrong thing — and the difference between them is whether
        domains travel structurally (see :meth:`domains`).
        """
        source = self.template if (dialect == "operator" or not self.phrase) else self.phrase
        year = now.year
        return source.format(
            name=f'"{name}"' if name else "",
            bare_name=name,
            domain=domain,
            industry=industry,
            year=year,
            prev_year=year - 1,
            noise=_exclusions(_NOISE_SITES),
            funding_sites=_sites(_FUNDING_SITES),
            ats_sites=_sites(_ATS_SITES),
            exec_sites=_sites(_EXEC_SITES),
        ).strip()

    def domains(self, *, domain: str, dialect: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(include, exclude)`` for a semantic backend. ``{domain}`` resolves to the account's.

        Empty for ``operator`` (already inline in the query) and for ``plain`` (the backend has no
        structured filter and no working operator, so precision there comes from the phrase and the
        post-filters alone — that is the price of a keyless default).
        """
        if dialect != "semantic":
            return (), ()
        include = tuple(
            (domain if s == "{domain}" else s) for s in self.sites if s != "{domain}" or domain
        )
        return include, (() if include else tuple(self.exclude))


# The library. Ordered by signal value, because the per-account query budget cuts from the end.
DORKS: tuple[Dork, ...] = (
    # ---- funding -----------------------------------------------------------------------------
    # The highest-value signal, so it gets two attempts from opposite directions: the trade press
    # (fast, good headlines) and the wires (slower, but they carry rounds the press ignores).
    Dork(
        slug="funding_press",
        kind="funding",
        # `inurl:{year}` is the portable recency lever: news CMSs put the date in the path, so this
        # excludes the 2019 round that otherwise outranks last week's on link authority alone.
        template='{funding_sites} {name} (raises OR raised OR "series a" OR "series b" OR '
                 '"series c" OR "seed round") (inurl:{year} OR {year})',
        strength=0.9,
        # NOT trust_kind, even though this dork is scoped to funding publishers. TechCrunch and the
        # wires cover acquisitions and launches from the same URLs, so trusting the dork here would
        # stamp `funding` 0.9 on every one of them. Provenance settles the event class only when the
        # URL pattern itself does (an ATS board, a company's own engineering blog) — a trade-press
        # hit still needs the headline read.
        require_any=("raise", "series", "seed", "funding", "round", "million", "$"),
        phrase="{bare_name} raises a new funding round",
        sites=_FUNDING_SITES,
    ),
    Dork(
        slug="funding_wire",
        kind="funding",
        template='{name} ("announces" OR "closes" OR "secures") '
                 '("series" OR "funding round" OR "financing") {year} {noise}',
        strength=0.85,
        require_any=("series", "funding", "financing", "million", "$"),
        phrase="{bare_name} announces funding round financing {year}",
        exclude=_NOISE_SITES,
    ),
    # ---- hiring ------------------------------------------------------------------------------
    # An open ATS req is the strongest hiring signal there is: it is the company's own words, it is
    # current by definition (closed reqs come down), and it names the team that has budget.
    Dork(
        slug="hiring_ats",
        kind="job_posting",
        template='{ats_sites} {bare_name}',
        strength=0.7,
        trust_kind=True,
        self_evident=True,
        # No `sites=_ATS_SITES` here. Restricting a semantic search to the ATS hosts surfaces
        # whoever mentions the account in a job description — measured: "Ramp" returned an
        # unrelated firm hiring for QuickBooks/Ramp/Gusto experience. Asking for the account's
        # openings in plain words returns its own board.
        phrase="{bare_name} careers job openings hiring",
    ),
    Dork(
        slug="hiring_careers",
        kind="job_posting",
        template='site:{domain} (careers OR jobs OR "open roles" OR "we are hiring")',
        strength=0.55,
        trust_kind=True,
        self_evident=True,
        phrase="{bare_name} open roles we are hiring",
        sites=("{domain}",),
    ),
    # ---- leadership --------------------------------------------------------------------------
    # A new VP/CxO rewrites the buying committee, which is why it outranks generic news: the person
    # who said no last quarter may no longer be in the room.
    Dork(
        slug="exec_change",
        kind="hiring",
        template='{exec_sites} {name} ("appoints" OR "names" OR "joins as" OR "new chief" OR '
                 '"promoted to") ({year} OR {prev_year})',
        strength=0.7,
        require_any=("appoint", "names", "joins", "chief", "vp ", "president", "officer"),
        phrase="{bare_name} appoints new chief executive officer or vice president",
        sites=_EXEC_SITES,
    ),
    # ---- tech stack --------------------------------------------------------------------------
    # An engineering blog post naming a technology is a real adoption signal and, unlike a
    # scanner-based install list, it says *why* — which is what a rep can actually open with.
    Dork(
        slug="tech_adoption",
        kind="tech_install",
        template='site:{domain} (engineering OR blog OR tech) '
                 '("migrated to" OR "we built" OR "now using" OR "switched to" OR "powered by")',
        strength=0.6,
        trust_kind=True,
        self_evident=True,
        phrase="{bare_name} engineering blog migrated to new technology stack",
        sites=("{domain}",),
    ),
    # ---- corporate events --------------------------------------------------------------------
    Dork(
        slug="expansion",
        kind="news",
        template='{name} ("acquires" OR "acquisition of" OR "merges with" OR "opens office" OR '
                 '"expands into" OR "partnership with") {year} {noise}',
        strength=0.6,
        require_any=("acquir", "merge", "office", "expand", "partner"),
        phrase="{bare_name} acquisition merger partnership or office expansion {year}",
        exclude=_NOISE_SITES,
    ),
    # The company's own newsroom: lower ceiling than the press, but it is first-party and it exists
    # for companies too small for anyone else to cover.
    Dork(
        slug="newsroom",
        kind="news",
        template='site:{domain} (news OR press OR newsroom OR announcement) {year}',
        strength=0.5,
        self_evident=True,
        phrase="{bare_name} newsroom press announcement {year}",
        sites=("{domain}",),
    ),
)

DORKS_BY_SLUG = {d.slug: d for d in DORKS}


def select_dorks(*, has_domain: bool, limit: int) -> list[Dork]:
    """The dorks worth running for this account, best first.

    Domain-scoped dorks are dropped when the account has no domain — rendering
    ``site:`` with an empty value produces a query that matches the entire web, which is how a
    precision tool turns into a noise generator.
    """
    out = [d for d in DORKS if has_domain or "{domain}" not in d.template]
    return out[: max(0, limit)]
