"""Environment-driven configuration and dependency wiring.

Values come from the environment, which a ``.env`` file at the repo root fills
in for a local run without overriding anything already exported — see
:mod:`app.env`.

Configuration is validated eagerly at startup (see :func:`validate_configuration`),
because the alternative is a container that passes its health check and only
reveals a typo when the first real request arrives. A value that is not
recognised is an error, never a silent fallback to a default — a misspelled
``LLM_MODE`` must not quietly serve mock election data.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

from . import ENV_FILE_LOADED, ENV_NAMES_LOADED
from .mailer import DisabledMailer, Mailer, SmtpMailer
from .parser import DEFAULT_PAGE_LIMIT, ElectionParser, UnavailableParser
from .search import DEFAULT_SEARCH_LIMIT
from .store import (
    DEFAULT_STALE_AFTER_SECONDS,
    ElectionStore,
    InMemoryElectionStore,
    InMemoryTrackedStore,
    TrackedStore,
)
from .usage import InMemoryUsageStore, UsageRecorder, UsageStore
from .wikipedia import DEFAULT_LANGUAGE
from .wishlist import DisabledWishlist, GithubWishlist, Wishlist

log = logging.getLogger(__name__)

#: Every recognised value, so an unknown one can name the alternatives.
STORE_BACKENDS = ("firestore", "sqlite", "memory")
LLM_MODES = ("mock", "live", "off")
SEARCH_MODES = ("auto", "google", "anthropic", "off")
WIKIPEDIA_MODES = ("on", "off")
IFES_ELECTIONGUIDE_MODES = ("on", "off")

DEFAULT_SQLITE_PATH = "./data/elections.db"


class ConfigError(RuntimeError):
    """The environment asks for something this app cannot provide."""


def _env_choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    raw = os.environ.get(name)
    value = (default if raw is None or not raw.strip() else raw).strip().lower()
    if value not in allowed:
        raise ConfigError(
            f"{name} must be one of {', '.join(allowed)}, got {raw!r}"
        )
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}")
    return value


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _store_backend() -> str:
    """Which backend the elections and the usage counters live in."""
    return _env_choice(
        "ELECTION_STORE", STORE_BACKENDS,
        "firestore" if os.environ.get("GOOGLE_CLOUD_PROJECT") else "memory",
    )


@lru_cache(maxsize=1)
def get_store() -> ElectionStore:
    """Pick a storage backend.

    ``ELECTION_STORE`` selects it: ``firestore`` (shared, the deployed default),
    ``sqlite`` (a local file that survives restarts, so repeated local runs do
    not re-fetch and re-extract the same pages), or ``memory`` (fastest, forgets
    everything on exit). Without ``GOOGLE_CLOUD_PROJECT`` Firestore is not
    available, so the default falls back to ``memory``.
    """
    stale_after = _env_float("PARSE_LEASE_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    backend = _env_choice(
        "ELECTION_STORE", STORE_BACKENDS, "firestore" if project else "memory"
    )

    if backend == "memory":
        return InMemoryElectionStore(stale_after=stale_after)

    if backend == "sqlite":
        from .sqlite_store import SqliteElectionStore

        return SqliteElectionStore(
            os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH), stale_after=stale_after
        )

    if not project:
        raise ConfigError("ELECTION_STORE=firestore requires GOOGLE_CLOUD_PROJECT")

    from google.cloud import firestore

    from .cached_store import CachedElectionStore
    from .firestore_store import FirestoreElectionStore

    client = firestore.Client(
        project=project, database=os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    # Firestore bills every document read, and viewing is open to anyone.
    return CachedElectionStore(FirestoreElectionStore(client, stale_after=stale_after))


@lru_cache(maxsize=1)
def get_outreach_store():
    """The outreach approval queue, in the same backend the elections use.

    Beside :func:`get_store` rather than inside it: the queue is not part of
    the election data at all, but ``ELECTION_STORE`` is still the one variable
    that says whether this process has a database, so a second one would only
    ask the same question twice.
    """
    from .outreach import InMemoryOutreachStore

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    backend = _env_choice(
        "ELECTION_STORE", STORE_BACKENDS, "firestore" if project else "memory"
    )

    if backend == "memory":
        return InMemoryOutreachStore()

    if backend == "sqlite":
        from .sqlite_store import SqliteOutreachStore

        return SqliteOutreachStore(os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))

    if not project:
        raise ConfigError("ELECTION_STORE=firestore requires GOOGLE_CLOUD_PROJECT")

    from google.cloud import firestore

    from .firestore_store import FirestoreOutreachStore

    client = firestore.Client(
        project=project, database=os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    return FirestoreOutreachStore(client)


@lru_cache(maxsize=1)
def get_tracked_store() -> TrackedStore:
    """The elections this deployment watches, in the same backend as the elections.

    Beside :func:`get_store` rather than inside it, for the same reason
    :func:`get_outreach_store` is: tracking is not part of the election data,
    and ``ELECTION_STORE`` is still the one variable that says whether this
    process has a database.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    backend = _env_choice(
        "ELECTION_STORE", STORE_BACKENDS, "firestore" if project else "memory"
    )

    if backend == "memory":
        return InMemoryTrackedStore()

    if backend == "sqlite":
        from .sqlite_store import SqliteTrackedStore

        return SqliteTrackedStore(os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))

    if not project:
        raise ConfigError("ELECTION_STORE=firestore requires GOOGLE_CLOUD_PROJECT")

    from google.cloud import firestore

    from .firestore_store import FirestoreTrackedStore

    client = firestore.Client(
        project=project, database=os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    return FirestoreTrackedStore(client)


#: Most tracked elections one scheduled tick may refresh. The cap is what keeps
#: a tick inside Cloud Run's request timeout: each one is a fetch and at most
#: one model call, run one after another (plan 3, A3).
DEFAULT_REFRESH_MAX_PER_TICK = 5


def refresh_max_per_tick() -> int:
    value = _env_int("REFRESH_MAX_PER_TICK", DEFAULT_REFRESH_MAX_PER_TICK)
    if value < 1:
        raise ConfigError("REFRESH_MAX_PER_TICK must be at least 1")
    return value


def refresh_model() -> str:
    """A cheaper model for scheduled refreshes, or empty for the extractor's own.

    Plan 3, A6.3: a refresh re-reads a page whose shape the strict schema and
    the gates in :mod:`app.refresh` already check, so it is the one place a
    weaker model can be tried without a person having to notice the mistake.
    """
    return _env_str("REFRESH_MODEL")


@lru_cache(maxsize=1)
def get_refresh_config():
    """The refresh schedule, read once per process from ``refresh.yaml``."""
    from .refresh_config import load_configured

    return load_configured()


def ifes_electionguide_enabled() -> bool:
    """Whether the calendar scan may read IFES ElectionGuide (plan 3, A5).

    ``off`` by default and independent of ``LLM_MODE``: this source is
    deterministic, not model-backed, so there is no "mocked pipeline" reason to
    leave it off the way there is for Wikipedia and search. The only reason it
    is off is IFES's data use policy — personal and non-commercial use only —
    and the owner not yet having IFES's written word that this project's use
    (a non-profit paying developers from sponsorship) qualifies. See
    ``doc/contribute.md`` and :mod:`app.calendar`'s module docstring, item 3.
    """
    return _env_choice("IFES_ELECTIONGUIDE", IFES_ELECTIONGUIDE_MODES, "off") == "on"


@lru_cache(maxsize=1)
def get_calendar_scanner():
    """Where the monthly calendar scan looks for elections to track (plan 3, A5).

    Wikidata needs no key and is always asked; Wikipedia's calendar articles
    are asked too when Wikipedia is on, with the same model as the extractor
    as its fallback for a shaped-differently article. IFES ElectionGuide is
    wired in only when :func:`ifes_electionguide_enabled` says so.
    """
    from .calendar import (
        AnthropicCalendarExtractor,
        CalendarScanner,
        IfesElectionGuide,
        StubCalendarExtractor,
        Wikidata,
        WikipediaArticles,
        WikipediaCalendar,
    )

    mode = _env_choice("LLM_MODE", LLM_MODES, "mock")
    contact = _env_str("WIKIPEDIA_CONTACT")
    wikidata = Wikidata(contact=contact)
    ifes = IfesElectionGuide(contact=contact) if ifes_electionguide_enabled() else None

    wikipedia = get_wikipedia()
    wikipedia_source = None
    if wikipedia is not None:
        extractor = None
        if mode == "live":
            extractor = AnthropicCalendarExtractor()
        elif mode == "mock":
            extractor = StubCalendarExtractor()
        wikipedia_source = WikipediaCalendar(WikipediaArticles(wikipedia), extractor=extractor)

    return CalendarScanner(
        wikidata=wikidata, wikipedia=wikipedia_source, ifes=ifes, store=get_store()
    )


def search_mode() -> str:
    """Which search engine an import consults besides the resolver, in effect.

    ``auto`` — the default — is Google where it is configured and ``off``
    otherwise. Off is not a degraded mode: the resolver searches as part of
    identifying the election and comes back with candidate pages, so a second
    round through the same hosted search tool would mostly pay twice for the
    same answer. ``SEARCH_MODE=anthropic`` asks for it anyway, which is worth
    it where the resolver keeps naming elections it cannot find pages for.

    Always ``off`` unless extraction is live: nothing a mocked pipeline does
    depends on a real page being found, so searching could only ever be dead
    code.
    """
    mode = _env_choice("SEARCH_MODE", SEARCH_MODES, "auto")
    if _env_choice("LLM_MODE", LLM_MODES, "mock") != "live":
        return "off"
    if mode != "auto":
        return mode
    if _env_str("GOOGLE_SEARCH_API_KEY") and _env_str("GOOGLE_SEARCH_CX"):
        return "google"
    return "off"


@lru_cache(maxsize=1)
def get_search():
    """The search seam, built from ``SEARCH_MODE``.

    Consulted only when the resolver named fewer candidate pages than an import
    may read, so a deployment whose resolutions come back complete never calls a
    search engine.

    A mode asked for by name must have its credentials, whether or not this
    process will get as far as using it — ``SEARCH_MODE=google`` without a key
    is a mistake worth hearing about at startup rather than on the one import
    that needed it. ``auto`` asks for nothing and settles for what is there.
    """
    from .search import AnthropicWebSearch, DisabledSearch, GoogleSearch

    requested = _env_choice("SEARCH_MODE", SEARCH_MODES, "auto")
    key, cx = _env_str("GOOGLE_SEARCH_API_KEY"), _env_str("GOOGLE_SEARCH_CX")
    if requested == "google" and not (key and cx):
        raise ConfigError(
            "SEARCH_MODE=google requires GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX"
        )
    if requested == "anthropic" and not _env_str("ANTHROPIC_API_KEY"):
        raise ConfigError("SEARCH_MODE=anthropic requires ANTHROPIC_API_KEY")

    mode = search_mode()
    if mode == "off":
        return DisabledSearch()
    if mode == "google":
        return GoogleSearch(key, cx)
    return AnthropicWebSearch()


def wikipedia_mode() -> str:
    """Whether an import looks the election up in Wikipedia first.

    ``on`` by default: the lookup needs no credential, costs nothing, and an
    encyclopedia article is the page most likely to *state seats* rather than
    votes — see :meth:`app.parser.LlmElectionParser._candidates` for why that
    earns it first place in the queue. ``WIKIPEDIA=off`` takes it out again, and
    an import then reads the resolver's candidates as it did before.

    Always ``off`` unless extraction is live, for the same reason searching is:
    the mock extractor ignores the page it is handed, so fetching a real article
    could only ever be a call made for nothing.
    """
    mode = _env_choice("WIKIPEDIA", WIKIPEDIA_MODES, "on")
    if _env_choice("LLM_MODE", LLM_MODES, "mock") != "live":
        return "off"
    return mode


def wikipedia_language() -> str:
    """Which Wikipedia to search. A language code, because it becomes a hostname."""
    language = _env_str("WIKIPEDIA_LANGUAGE").lower() or DEFAULT_LANGUAGE
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", language):
        raise ConfigError(
            f"WIKIPEDIA_LANGUAGE must be a language code like 'en' or 'pt-br', got {language!r}"
        )
    return language


@lru_cache(maxsize=1)
def get_wikipedia():
    """The Wikipedia seam, or ``None`` where it is switched off.

    ``None`` rather than a disabled stand-in, because this seam is two things at
    once — the candidate source the parser asks, and the fetcher that serves
    those articles — and "no Wikipedia" means an import is wired exactly as it
    was before either existed.

    The language is validated whether or not this process will use it: like a
    named ``SEARCH_MODE`` without its keys, a typo there is worth hearing about
    at startup rather than on the one import that needed it.
    """
    language = wikipedia_language()
    if wikipedia_mode() == "off":
        return None

    from .wikipedia import Wikipedia

    # ``WIKIPEDIA_CONTACT`` only replaces the address in the User-Agent; there is
    # always one, because a client that names nobody is refused outright.
    return Wikipedia(language=language, contact=_env_str("WIKIPEDIA_CONTACT"))


@lru_cache(maxsize=1)
def get_parser() -> ElectionParser:
    """Wire the import pipeline: resolve the request, read pages, extract seats.

    ``LLM_MODE`` selects the agents: ``mock`` (the default) reads the request
    back and extracts a fixed set of parties without contacting the Anthropic
    API; ``live`` runs the real ones and requires ``ANTHROPIC_API_KEY``, mounted
    from GCP Secret Manager. ``off`` disables importing entirely.

    ``IMPORT_PAGE_LIMIT`` caps how many candidate pages one import may read, and
    ``IMPORT_SEARCH_LIMIT`` how many of those candidates a search engine may
    contribute beyond the ones the resolver named (see :func:`get_search`).
    Each page read costs a fetch and a model call, so both are small numbers;
    ``IMPORT_PAGE_LIMIT=1`` reads only the first candidate — which, with
    Wikipedia on, is the article rather than the resolver's best answer.
    """
    return _build_parser(model=None)


@lru_cache(maxsize=1)
def get_refresh_parser() -> ElectionParser:
    """The scheduled refresh's own parser: same pipeline, a cheaper model.

    Plan 3, A6.3: a refresh re-reads a page whose shape the strict schema and
    :mod:`app.refresh`'s own gates already check, so it is the one place a
    weaker model can be tried without a person having to notice the mistake —
    ``REFRESH_MODEL`` names it, and an empty value means "the same one
    ``get_parser`` uses". Built the same way, so ``LLM_MODE=off`` or a mock
    deployment answers the refresh exactly as it answers an import.
    """
    return _build_parser(model=refresh_model() or None)


def _build_parser(*, model: str | None) -> ElectionParser:
    from .extractor import MODEL, AnthropicExtractor, MockExtractor
    from .fetcher import HttpPageFetcher
    from .parser import LlmElectionParser
    from .resolver import AnthropicResolver, MockResolver

    mode = _env_choice("LLM_MODE", LLM_MODES, "mock")
    if mode == "off":
        return UnavailableParser()
    limits = {
        "page_limit": _env_int("IMPORT_PAGE_LIMIT", DEFAULT_PAGE_LIMIT),
        "search_limit": _env_int("IMPORT_SEARCH_LIMIT", DEFAULT_SEARCH_LIMIT),
    }
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigError("LLM_MODE=live requires ANTHROPIC_API_KEY")
        # Wikipedia is both halves of its seam: the source the parser asks for
        # candidates, and the fetcher those candidates are served by. Wrapping
        # rather than replacing keeps every other address on the ordinary path.
        wikipedia = get_wikipedia()
        fetcher = HttpPageFetcher()
        if wikipedia is not None:
            from .wikipedia import WikipediaFetcher

            fetcher = WikipediaFetcher(wikipedia, fetcher)
        return LlmElectionParser(
            fetcher, AnthropicExtractor(model=model or MODEL), AnthropicResolver(),
            search=get_search(), wikipedia=wikipedia, **limits
        )
    # Mock: a page is still fetched and the whole pipeline runs, minus the models.
    return LlmElectionParser(
        HttpPageFetcher(), MockExtractor(), MockResolver(), search=get_search(), **limits
    )


@lru_cache(maxsize=1)
def get_usage() -> UsageStore:
    """The usage counters live wherever the elections do."""
    backend = _store_backend()
    if backend == "memory":
        return InMemoryUsageStore()

    if backend == "sqlite":
        from .sqlite_usage import SqliteUsageStore

        return SqliteUsageStore(os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))

    from google.cloud import firestore

    from .firestore_usage import FirestoreUsageStore

    client = firestore.Client(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
    )
    return FirestoreUsageStore(client)


@lru_cache(maxsize=1)
def get_usage_recorder() -> UsageRecorder:
    """One recorder per process, shared by every request."""
    return UsageRecorder(get_usage())


#: Everything sending the usage reports needs besides a port and a sender, both
#: of which have defaults.
SMTP_VARIABLES = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "REPORT_EMAIL_TO")

DEFAULT_SMTP_PORT = 587


@lru_cache(maxsize=1)
def get_mailer() -> Mailer:
    """Where the usage reports are emailed, when anywhere.

    Optional in the way requests are: with none of :data:`SMTP_VARIABLES` set,
    the counting still happens and the owner can read a report at
    ``/api/admin/usage``. Some of them without the rest is
    a configuration error, because a report that silently goes nowhere is what
    this switch exists to prevent.
    """
    values = {name: _env_str(name) for name in SMTP_VARIABLES}
    if not any(values.values()):
        return DisabledMailer()
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"usage report email also needs {', '.join(missing)}")
    recipients = [part.strip() for part in values["REPORT_EMAIL_TO"].split(",") if part.strip()]
    if not recipients or any("@" not in address for address in recipients):
        raise ConfigError("REPORT_EMAIL_TO must be one or more comma-separated addresses")
    return SmtpMailer(
        values["SMTP_HOST"],
        _env_int("SMTP_PORT", DEFAULT_SMTP_PORT),
        username=values["SMTP_USERNAME"],
        password=values["SMTP_PASSWORD"],
        sender=_env_str("REPORT_EMAIL_FROM") or values["SMTP_USERNAME"],
        recipients=recipients,
    )


def usage_report_secret() -> str:
    """What the scheduled caller presents to have the reports sent; empty for none."""
    return _env_str("USAGE_REPORT_SECRET")


def on_cloud_run() -> bool:
    """Whether this process is a Cloud Run service or job, which set these themselves."""
    return bool(_env_str("K_SERVICE") or _env_str("CLOUD_RUN_JOB"))


#: Repository that election requests are filed against, as ``owner/name``.
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


@lru_cache(maxsize=1)
def get_wishlist() -> Wishlist:
    """Where anyone can ask for an election to be imported.

    Optional: with no ``GITHUB_ISSUES_TOKEN`` there is nowhere to file a
    request, and the page is told so by ``/api/config`` and stops offering it.
    A token with no repository to file against is a configuration error rather
    than a default, because guessing which repository to open issues on is not
    something to get wrong quietly.

    Named ``GITHUB_ISSUES_*`` rather than ``GITHUB_TOKEN``: that name is
    already taken — GitHub Actions sets it in every workflow run, and so do
    several agent runtimes — and a token that happens to be in the environment
    is not a decision to file issues with it.
    """
    token = _env_str("GITHUB_ISSUES_TOKEN")
    repo = _env_str("GITHUB_ISSUES_REPO")
    if not token:
        if repo:
            raise ConfigError("GITHUB_ISSUES_REPO is set but GITHUB_ISSUES_TOKEN is not")
        return DisabledWishlist()
    if not repo:
        raise ConfigError("GITHUB_ISSUES_TOKEN is set but GITHUB_ISSUES_REPO is not (owner/name)")
    if not _REPO_PATTERN.match(repo):
        raise ConfigError(f"GITHUB_ISSUES_REPO must be owner/name, not {repo!r}")
    owner, name = repo.split("/", 1)
    return GithubWishlist(token, owner=owner, repo=name)


def issues_url() -> str:
    """This repository's issue tracker, as a browsable URL; empty when unset.

    Not a credential and not the API address :class:`app.wishlist.GithubWishlist`
    posts to — the page needs somewhere to send a visitor who spots a wrong
    number in an election nobody confirmed (plan 3, A1). Empty leaves the
    footnote without its link rather than guessing a repository.
    """
    repo = _env_str("GITHUB_ISSUES_REPO")
    if not repo:
        return ""
    if not _REPO_PATTERN.match(repo):
        raise ConfigError(f"GITHUB_ISSUES_REPO must be owner/name, not {repo!r}")
    return f"https://github.com/{repo}/issues"


def support_link() -> str:
    """Where the footer's donate/sponsor link points; empty hides it (plan 3, C4).

    The owner's choice of GitHub Sponsors, Ko-fi or MobilePay — one link, not a
    named provider this module would have to validate the shape of. The page
    only follows it if it is ``https``; see ``js/api.js``'s ``safeSupportLink``.
    """
    return _env_str("SUPPORT_LINK")


def support_sponsor() -> str:
    """"Supported by …" under the calculator; empty means nobody is named yet.

    Plain text, shown as text: whatever the owner sets is not a promise this
    module checks, only a name the page repeats.
    """
    return _env_str("SUPPORT_SPONSOR")


@lru_cache(maxsize=1)
def get_reddit_poster():
    """Where an approved reply is actually posted.

    All five ``REDDIT_*`` variables or none: :func:`app.reddit.poster_from_env`
    returns a poster that answers ``503`` for every one missing, so the rest of
    the approval gate — the queue, the email, viewing a draft — works before any
    Reddit credential exists.
    """
    from . import reddit

    return reddit.poster_from_env()


#: The subreddits this deployment may post to. The scanner keeps no list of
#: its own — it reads threads saved by hand — so this is the only allowlist,
#: and a draft for a subreddit outside it is refused here.
def outreach_allowed_subreddits() -> frozenset[str]:
    raw = _env_str("OUTREACH_ALLOWED_SUBREDDITS")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


#: At most this many posted replies per subreddit per rolling seven days.
def outreach_subreddit_weekly_cap() -> int:
    return _env_int("OUTREACH_SUBREDDIT_WEEKLY_CAP", 2)


#: At most this many posted replies in total per day.
def outreach_daily_cap() -> int:
    return _env_int("OUTREACH_DAILY_CAP", 3)


def public_base_url() -> str:
    """Where this site is reachable, as an absolute URL. No trailing slash.

    A plain variable, not a secret. Nothing in the app needs it today; it is
    kept for what does need an absolute link to the site, such as the approval
    email in doc/plans/04-reddit-outreach.md.
    """
    return _env_str("PUBLIC_BASE_URL").rstrip("/")


#: Shortest ``ORIGIN_SECRET`` accepted, so a placeholder is a boot failure.
MIN_ORIGIN_SECRET_CHARS = 32


def origin_secret() -> str:
    """The secret a fronting proxy must present, or empty when there is none.

    Set it when the app sits behind Cloudflare (or any proxy) so that the
    platform's own address — ``*.run.app`` — stops answering on its own.
    """
    return _env_str("ORIGIN_SECRET")


def admin_secret() -> str:
    """The secret the owner presents to import, or empty on a local run.

    Importing is the one thing on this site that spends money, and it is the
    owner's alone: the page sends this as ``x-admin-secret`` once it has been
    pasted in. With none configured every caller is the owner, which is right
    on a laptop and nowhere else — see
    :func:`validate_configuration`, which refuses to boot on Cloud Run without it.
    """
    return _env_str("ADMIN_SECRET")


def imports_enabled() -> bool:
    """Whether this deployment imports at all. ``LLM_MODE=off`` switches it off."""
    return _env_choice("LLM_MODE", LLM_MODES, "mock") != "off"


def max_wait_seconds() -> float:
    return _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)


def describe_configuration() -> dict[str, str]:
    """The choices actually in effect, for the startup log."""
    return {
        "store": _store_backend(),
        "llm_mode": _env_choice("LLM_MODE", LLM_MODES, "mock"),
        "admin": "secret" if admin_secret() else "open",
        "requests": "github" if _env_str("GITHUB_ISSUES_TOKEN") else "off",
        "reports": "email" if _env_str("SMTP_HOST") else "off",
        "search": search_mode(),
        "wikipedia": (
            f"{wikipedia_language()}.wikipedia.org" if wikipedia_mode() == "on" else "off"
        ),
        # Worth its own line in the startup log: this one reads real people's
        # written confirmation before it may be "on" at all (module docstring).
        "ifes_electionguide": "on" if ifes_electionguide_enabled() else "off",
        "reddit": "posting" if not _missing_reddit_credentials() else "off",
    }


def _missing_reddit_credentials() -> list[str]:
    from . import reddit

    return reddit.missing_credentials()


def validate_configuration() -> dict[str, str]:
    """Build every configured dependency now, so a bad value stops the boot.

    Called from the app's startup. Without it the process comes up healthy and
    a typo in ``ELECTION_STORE`` only surfaces on the first import — after the
    revision has already been rolled out and started taking traffic.
    """
    chosen = describe_configuration()
    if ENV_FILE_LOADED is not None:
        # Names only, never values: this file is where the API keys are. Worth a
        # line even so — "why are live imports on?" is answered by seeing which
        # file was picked up, especially one found by walking up from the cwd.
        log.info(
            "loaded %s from %s", ", ".join(ENV_NAMES_LOADED) or "nothing", ENV_FILE_LOADED
        )
    _env_float("PARSE_LEASE_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)
    get_store()
    get_parser()
    # A typo in the schedule does not fail loudly on its own: it just refreshes
    # the wrong elections at the wrong times until somebody notices.
    get_refresh_config()
    refresh_max_per_tick()
    get_tracked_store()
    get_refresh_parser()
    get_calendar_scanner()
    # Not reached by ``get_parser`` when importing is off, and a misspelled
    # SEARCH_MODE or WIKIPEDIA_LANGUAGE must still stop the boot.
    get_search()
    get_wikipedia()
    get_wishlist()
    get_usage()
    get_mailer()
    get_outreach_store()
    get_reddit_poster()
    for name, secret in (
        ("ORIGIN_SECRET", origin_secret()),
        ("USAGE_REPORT_SECRET", usage_report_secret()),
        ("ADMIN_SECRET", admin_secret()),
    ):
        if secret and len(secret) < MIN_ORIGIN_SECRET_CHARS:
            raise ConfigError(f"{name} must be at least {MIN_ORIGIN_SECRET_CHARS} characters")
    if on_cloud_run() and not admin_secret():
        # Without a secret every caller is the owner, which is what a local run
        # wants. On Cloud Run it would be a deployment where anyone can spend
        # the owner's money on imports — the failure a lost environment
        # variable must turn into, rather than a deployment that is wide open.
        raise ConfigError(
            "ADMIN_SECRET is required on Cloud Run: without it every caller may import"
        )
    log.info(
        "configuration ok store=%s llm_mode=%s admin=%s requests=%s "
        "reports=%s search=%s wikipedia=%s",
        chosen["store"], chosen["llm_mode"], chosen["admin"],
        chosen["requests"], chosen["reports"], chosen["search"], chosen["wikipedia"],
    )
    return chosen
