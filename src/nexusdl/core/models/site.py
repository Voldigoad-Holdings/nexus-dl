"""Site configuration and runtime health models.

This module defines the domain objects for a **source site**: the static
configuration loaded from ``sites.yaml`` (:class:`SiteConfig` +
:class:`SiteCapabilities`) and the runtime health snapshot tracked by the
:class:`~nexusdl.core.registry.site_registry.SiteRegistry`
(:class:`SiteHealthSnapshot`).

Every object in this module is a **pure, frozen data structure**. There is
no I/O, no network, no filesystem, no logging, and no dependency beyond
:mod:`nexusdl.core.models.enums`. The YAML parsing and JSON-Schema
validation live in ``core/registry/loader.py``; the runtime health checks
live in ``core/registry/site_registry.py``. Both of those modules construct
the objects defined here and never the reverse.

Three classes, three concerns
-----------------------------
* :class:`SiteCapabilities` — the nine booleans describing *what the site
  can do* (search, latest, filters, …). A pure value object; its only job
  is to be readable and to validate its own field types.
* :class:`SiteConfig` — the immutable configuration of a site, matching the
  per-site schema validated against ``sites_schema.json``. Every field the
  YAML can carry is represented here, with the same constraints the schema
  enforces.
* :class:`SiteHealthSnapshot` — the mutable-in-the-registry but
  immutable-in-this-object record of a site's last observed health state.
  Not part of the persisted configuration; regenerated on every health
  check.

Design invariants
-----------------
* **Immutability** — every class is a ``frozen``, ``slots`` dataclass.
  Any "update" produces a new object via :func:`dataclasses.replace`. This
  matters because a :class:`SiteConfig` is passed to every parser instance
  and read concurrently from multiple coroutines.
* **Validation at construction** — every constraint that
  ``sites_schema.json`` enforces at the YAML level is re-enforced here in
  :meth:`__post_init__`. A malformed :class:`SiteConfig` therefore cannot
  exist, regardless of whether it came from validated YAML, a hand-built
  test fixture, or a future plugin that registers a site
  programmatically.
* **No ``health`` on ``SiteConfig``** — health is runtime state, not
  configuration. Keeping it on :class:`SiteHealthSnapshot` prevents a
  cached config object from silently carrying a stale status.

Dependency graph (no cycles)::

    core.models.enums  ──► core.models.site

No SQLAlchemy, no loguru, no I/O. This file could be lifted into a
standalone library without modification.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from nexusdl.core.models.enums import Language, SiteHealth


__all__ = [
    "DEFAULT_MAX_CONCURRENT_DOWNLOADS",
    "DEFAULT_RATE_LIMIT",
    "SiteCapabilities",
    "SiteConfig",
    "SiteHealthSnapshot",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Site identifier pattern, exactly as validated by
#: ``data/sites_schema.json`` and enforced by the loader. Kept as a single
#: compiled regex so model, schema, and loader cannot drift.
_SITE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_-]*$"
)

#: A domain must be an absolute HTTPS URL. HTTP is rejected — every source
#: site NexusDL supports serves HTTPS, and accepting HTTP would open the
#: door to a downgrade attack surfaced through ``sites.yaml``.
_HTTPS_PREFIX: Final[str] = "https://"

#: A parser class reference is ``module:ClassName``. Exactly one colon,
#: neither side empty, both sides composed of identifier characters and
#: dots.
_PARSER_CLASS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
)

#: Default per-site cap on concurrent chapter downloads. Applied when the
#: YAML omits ``max_concurrent_downloads``. Matches the pipeline-wide
#: default in :mod:`nexusdl.core.downloader.queue`.
DEFAULT_MAX_CONCURRENT_DOWNLOADS: Final[int] = 4

#: Default per-site request rate, in requests per second. Applied when the
#: YAML omits ``rate_limit``. Conservative — the manager will still respect
#: the global limiter, so this is a site-specific upper bound.
DEFAULT_RATE_LIMIT: Final[float] = 1.0


# --------------------------------------------------------------------------- #
# SiteCapabilities                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SiteCapabilities:
    """The nine capability flags describing what a site can do.

    Every flag defaults to ``False`` — a site that omits the ``capabilities``
    block in ``sites.yaml`` advertises no optional features. The loader is
    expected to relax this default for sites that *must* have a capability
    (e.g. a parser that cannot work without JS rendering should declare
    ``requires_js_rendering: true`` and reject loading if the runtime
    cannot provide it — but that check happens at the loader, not here).

    The nine flags are stable across the framework. Renaming a flag is a
    breaking change requiring a schema migration; adding one requires
    bumping :data:`~nexusdl.core.registry.loader.CONFIG_VERSION`.

    Attributes:
        search: Text search is supported. When ``False``, the site's search
            entry point is disabled in every UI and
            :meth:`SiteRegistry.search_all` skips it.
        search_by_id: The site supports an "exact match by source id"
            endpoint, used for fast path lookups (e.g. resolving a
            bookmarked ``manga_id`` without a text search).
        latest: The site exposes a "recent updates" listing — required for
            any UI surface that shows "what's new".
        filters: The site supports advanced search filters (tags, status,
            year, …). Metadata-only; the UI may show a filter panel.
        pagination: Search results are paginated. When ``False``, the
            parser is expected to return the entire result set in one
            response, and UIs should not show page navigation.
        requires_auth: The site requires a logged-in session for at least
            some content. The parser is expected to check
            ``CookieManager`` before every request and raise
            :class:`~nexusdl.core.exceptions.SessionError` if cookies are
            missing.
        requires_cloudflare_bypass: The site is behind a Cloudflare
            interstitial. The parser should use the Cloudflare mixin.
        requires_js_rendering: The site requires JavaScript execution to
            fetch chapter pages (or sometimes even the manga detail page).
            The parser should use the JS-rendering mixin and depend on
            :class:`~nexusdl.core.session.playwright_pool.PlaywrightPool`.
        bulk_download: The site offers a per-chapter bulk download (e.g. a
            generated ZIP). The downloader can request the site's
            pre-packaged archive instead of fetching pages individually,
            which is dramatically cheaper for the source site and often
            for the user.
    """

    search: bool = False
    search_by_id: bool = False
    latest: bool = False
    filters: bool = False
    pagination: bool = False
    requires_auth: bool = False
    requires_cloudflare_bypass: bool = False
    requires_js_rendering: bool = False
    bulk_download: bool = False

    def __post_init__(self) -> None:
        """Validate that every flag is a genuine ``bool``.

        The YAML loader produces ``bool`` values for ``true`` / ``false``,
        but a hand-written test fixture or a plugin might pass ``1`` / ``0``
        from a JSON source. Rejecting the non-bool form catches the error
        at the boundary instead of letting a truthy ``1`` mask a
        capability check.

        Raises:
            TypeError: If any field is not exactly ``bool``. ``int`` is
                rejected even though ``bool`` is a subclass of ``int`` — the
                reverse is what matters here.
        """
        for field_name in self.__slots__:
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"SiteCapabilities.{field_name} must be a bool, "
                    f"got {type(value).__name__}"
                )

    def enabled(self) -> tuple[str, ...]:
        """Return the names of the capabilities that are ``True``.

        Convenience for logging and diagnostics — e.g. rendering a site
        listing in the CLI where only the enabled capabilities are worth
        showing.

        Returns:
            A tuple of field names, in declaration order.
        """
        return tuple(
            name for name in self.__slots__ if getattr(self, name)
        )


# --------------------------------------------------------------------------- #
# SiteConfig                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """Immutable configuration of a single source site.

    A :class:`SiteConfig` instance is produced by
    :meth:`~nexusdl.core.registry.loader.SiteLoader.load` from a validated
    ``sites.yaml`` entry, and consumed by:
    :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.get_parser`
    to construct a parser, :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.list_sites`
    to filter the site list, and every parser to decide its own behaviour
    (base URL, headers, cookies, rate limits).

    All fields match the per-site schema validated against
    ``data/sites_schema.json``. Every constraint enforced by the schema is
    re-enforced here in :meth:`__post_init__` so a programmatically-built
    :class:`SiteConfig` (a plugin registering a site, a test fixture) is
    never less valid than a YAML-loaded one.

    Attributes:
        id: Lowercase identifier, matching ``^[a-z][a-z0-9_-]*$``. Used as
            the primary key throughout the pipeline (``mangas.site_id``,
            ``parser.site_id``, cache keys, event payloads).
        name: Human-readable display name (e.g. ``"MangaDex"``). Rendered
            verbatim in every UI.
        domains: One or more absolute HTTPS URLs that serve the site.
            Multiple domains are allowed (mirrors, language-specific
            subdomains); the parser chooses the first that responds. At
            least one is required; every entry must start with
            ``https://``.
        parser_class: A ``module:ClassName`` reference resolving to the
            parser class. The loader imports this when constructing a
            parser; the format is validated here so an invalid reference
            fails fast.
        language: Primary content language. A site may serve multiple
            languages, but the ``sites.yaml`` entry declares the dominant
            one; per-manga language is stored on the manga row.
        adult: Whether the site serves adult content. ``True`` sites are
            hidden unless ``settings.content.allow_adult`` is ``True``.
        capabilities: The nine capability flags — see
            :class:`SiteCapabilities`.
        default_headers: Extra HTTP headers applied to every request the
            parser makes (typically ``Accept-Language``, ``Referer``,
            ``User-Agent`` overrides). Empty by default. Stored as an
            immutable mapping.
        cookies_required: Names of cookies the site requires for
            authentication (e.g. ``("cf_clearance",)``). The values are
            never stored in ``sites.yaml`` — they live in ``.env`` (as
            ``NEXUSDL_<SITE>_<COOKIE>=value``) or in the encrypted
            :class:`~nexusdl.core.session.cookie_manager.CookieManager`
            store. Only the *names* are listed here so the parser can
            validate their presence before every authenticated request.
        max_concurrent_downloads: Maximum number of chapters the
            :class:`~nexusdl.core.downloader.queue.DownloadQueue` may run
            concurrently for this site. In ``[1, 32]``. Defaults to
            :data:`DEFAULT_MAX_CONCURRENT_DOWNLOADS`.
        rate_limit: Maximum requests per second the
            :class:`~nexusdl.core.session.rate_limiter.RateLimiter` should
            allow for this site. Must be ``> 0``. Defaults to
            :data:`DEFAULT_RATE_LIMIT`.
    """

    id: str
    name: str
    domains: tuple[str, ...]
    parser_class: str
    language: Language
    adult: bool
    capabilities: SiteCapabilities
    default_headers: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    cookies_required: tuple[str, ...] = ()
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    rate_limit: float = DEFAULT_RATE_LIMIT

    def __post_init__(self) -> None:
        """Validate every field against the ``sites_schema.json`` rules.

        Raises:
            TypeError: On wrong Python types (non-``str`` id, non-``Language``
                language, non-``SiteCapabilities`` capabilities, non-``bool``
                adult, non-mapping ``default_headers``, …).
            ValueError: On well-typed but semantically invalid values
                (empty id, malformed id, empty name, no domains, non-HTTPS
                domain, malformed parser_class, out-of-range concurrency or
                rate limit).
        """
        # --- id ------------------------------------------------------- #
        if not isinstance(self.id, str):
            raise TypeError(
                f"SiteConfig.id must be a str, got {type(self.id).__name__}"
            )
        if not _SITE_ID_PATTERN.match(self.id):
            raise ValueError(
                f"SiteConfig.id must match "
                f"{_SITE_ID_PATTERN.pattern!r}, got {self.id!r}"
            )

        # --- name ----------------------------------------------------- #
        if not isinstance(self.name, str):
            raise TypeError(
                f"SiteConfig.name must be a str, got {type(self.name).__name__}"
            )
        if not self.name.strip():
            raise ValueError("SiteConfig.name must be non-empty")

        # --- domains -------------------------------------------------- #
        if not isinstance(self.domains, tuple):
            raise TypeError(
                f"SiteConfig.domains must be a tuple of str, got "
                f"{type(self.domains).__name__}"
            )
        if not self.domains:
            raise ValueError("SiteConfig.domains must contain at least one URL")
        for domain in self.domains:
            if not isinstance(domain, str):
                raise TypeError(
                    f"SiteConfig.domains entries must be str, got "
                    f"{type(domain).__name__}"
                )
            if not domain.startswith(_HTTPS_PREFIX):
                raise ValueError(
                    f"SiteConfig.domains entries must start with "
                    f"{_HTTPS_PREFIX!r}, got {domain!r}"
                )

        # --- parser_class --------------------------------------------- #
        if not isinstance(self.parser_class, str):
            raise TypeError(
                f"SiteConfig.parser_class must be a str, got "
                f"{type(self.parser_class).__name__}"
            )
        if not _PARSER_CLASS_PATTERN.match(self.parser_class):
            raise ValueError(
                f"SiteConfig.parser_class must be 'module:ClassName', got "
                f"{self.parser_class!r}"
            )

        # --- language ------------------------------------------------- #
        if not isinstance(self.language, Language):
            raise TypeError(
                f"SiteConfig.language must be a Language, got "
                f"{type(self.language).__name__}"
            )

        # --- adult ---------------------------------------------------- #
        if not isinstance(self.adult, bool):
            raise TypeError(
                f"SiteConfig.adult must be a bool, got "
                f"{type(self.adult).__name__}"
            )

        # --- capabilities --------------------------------------------- #
        if not isinstance(self.capabilities, SiteCapabilities):
            raise TypeError(
                f"SiteConfig.capabilities must be a SiteCapabilities, got "
                f"{type(self.capabilities).__name__}"
            )

        # --- default_headers ------------------------------------------ #
        if not isinstance(self.default_headers, Mapping):
            raise TypeError(
                f"SiteConfig.default_headers must be a Mapping, got "
                f"{type(self.default_headers).__name__}"
            )
        for key, value in self.default_headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(
                    "SiteConfig.default_headers must map str to str; "
                    f"got {key!r} -> {value!r}"
                )
        # Wrap a caller-supplied dict in a read-only view so a later
        # mutation of the source dict cannot silently corrupt the config.
        # ``frozen=True`` only prevents attribute reassignment; the nested
        # mapping is still mutable unless wrapped. ``object.__setattr__``
        # is the only way to perform this inside a frozen dataclass.
        if not isinstance(self.default_headers, MappingProxyType):
            object.__setattr__(
                self,
                "default_headers",
                MappingProxyType(dict(self.default_headers)),
            )

        # --- cookies_required ----------------------------------------- #
        if not isinstance(self.cookies_required, tuple):
            raise TypeError(
                f"SiteConfig.cookies_required must be a tuple of str, got "
                f"{type(self.cookies_required).__name__}"
            )
        for cookie_name in self.cookies_required:
            if not isinstance(cookie_name, str):
                raise TypeError(
                    f"SiteConfig.cookies_required entries must be str, got "
                    f"{type(cookie_name).__name__}"
                )
            if not cookie_name:
                raise ValueError(
                    "SiteConfig.cookies_required must not contain empty names"
                )

        # --- max_concurrent_downloads --------------------------------- #
        if not isinstance(self.max_concurrent_downloads, int):
            raise TypeError(
                f"SiteConfig.max_concurrent_downloads must be an int, got "
                f"{type(self.max_concurrent_downloads).__name__}"
            )
        if not (1 <= self.max_concurrent_downloads <= 32):
            raise ValueError(
                f"SiteConfig.max_concurrent_downloads must be in [1, 32], "
                f"got {self.max_concurrent_downloads}"
            )

        # --- rate_limit ----------------------------------------------- #
        if not isinstance(self.rate_limit, (int, float)) or isinstance(
            self.rate_limit, bool
        ):
            raise TypeError(
                f"SiteConfig.rate_limit must be a number, got "
                f"{type(self.rate_limit).__name__}"
            )
        if self.rate_limit <= 0:
            raise ValueError(
                f"SiteConfig.rate_limit must be > 0, got {self.rate_limit}"
            )

    # ------------------------------------------------------------------ #
    # Convenience                                                        #
    # ------------------------------------------------------------------ #

    @property
    def base_url(self) -> str:
        """Return the primary (first) domain, with trailing slash stripped.

        Parsers should build URLs relative to this rather than assuming
        which domain responds. The trailing slash is stripped so that
        ``site.base_url + "/api/v1/manga"`` produces ``"https://x/api/v1/manga"``,
        not ``"https://x//api/v1/manga"``.
        """
        return self.domains[0].rstrip("/")

    def supports(self, capability: str) -> bool:
        """Return ``True`` iff the named capability flag is enabled.

        Args:
            capability: The flag name (e.g. ``"search"``). Unknown names
                raise :class:`AttributeError` — this is a programmer error,
                not a runtime lookup.

        Raises:
            AttributeError: If *capability* is not a capability flag.
        """
        # ``getattr`` on a frozen slots dataclass with an unknown name
        # raises AttributeError, which is the desired behaviour — no silent
        # ``False`` for a typo.
        value = getattr(self.capabilities, capability)
        if not isinstance(value, bool):  # pragma: no cover — defensive
            raise AttributeError(
                f"{capability!r} is not a SiteCapabilities flag"
            )
        return value


# --------------------------------------------------------------------------- #
# SiteHealthSnapshot                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SiteHealthSnapshot:
    """Immutable record of a site's last observed health state.

    Produced by :class:`~nexusdl.core.registry.site_registry.SiteRegistry`
    on every health check and returned by the ``GET /api/v1/sites/{id}/health``
    endpoint. Every field is a point-in-time observation — nothing here is
    updated in place; a newer snapshot replaces an older one.

    Attributes:
        site_id: The :attr:`SiteConfig.id` this snapshot describes.
        status: The current :class:`SiteHealth` state.
        last_check_at: UTC timestamp of the health check that produced
            this snapshot.
        last_ok_at: UTC timestamp of the most recent successful check, or
            ``None`` if the site has never been observed healthy.
        last_error: Human-readable description of the last failure, or
            ``None`` when the site is healthy.
        consecutive_failures: How many health checks in a row have failed.
            ``0`` for a healthy site. Used by the registry to decide when
            to emit ``site.health.changed`` and by UIs to render a
            degraded indicator.
    """

    site_id: str
    status: SiteHealth
    last_check_at: datetime
    last_ok_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def __post_init__(self) -> None:
        """Validate field types and cross-field invariants.

        Raises:
            TypeError: On wrong Python types.
            ValueError: On negative ``consecutive_failures``, a tz-naive
                timestamp, or a ``HEALTHY`` status combined with a
                positive ``consecutive_failures`` count (an internally
                inconsistent snapshot should never be constructible).
        """
        if not isinstance(self.site_id, str) or not self.site_id:
            raise TypeError("SiteHealthSnapshot.site_id must be a non-empty str")
        if not isinstance(self.status, SiteHealth):
            raise TypeError(
                f"SiteHealthSnapshot.status must be a SiteHealth, got "
                f"{type(self.status).__name__}"
            )
        for field_name, value in (
            ("last_check_at", self.last_check_at),
            ("last_ok_at", self.last_ok_at),
        ):
            if value is None and field_name == "last_ok_at":
                continue
            if not isinstance(value, datetime):
                raise TypeError(
                    f"SiteHealthSnapshot.{field_name} must be a datetime, "
                    f"got {type(value).__name__}"
                )
            if value.tzinfo is None:
                raise ValueError(
                    f"SiteHealthSnapshot.{field_name} must be timezone-aware"
                )
        if not isinstance(self.consecutive_failures, int):
            raise TypeError(
                "SiteHealthSnapshot.consecutive_failures must be an int, "
                f"got {type(self.consecutive_failures).__name__}"
            )
        if self.consecutive_failures < 0:
            raise ValueError(
                "SiteHealthSnapshot.consecutive_failures must be >= 0, "
                f"got {self.consecutive_failures}"
            )
        if (
            self.status is SiteHealth.HEALTHY
            and self.consecutive_failures > 0
        ):
            raise ValueError(
                "SiteHealthSnapshot cannot be HEALTHY with "
                f"consecutive_failures={self.consecutive_failures}"
            )
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise TypeError(
                "SiteHealthSnapshot.last_error must be a str or None, got "
                f"{type(self.last_error).__name__}"
            )

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the snapshot.

        Timestamps are rendered as ISO 8601 strings for compatibility with
        the REST API's other endpoints. The result is safe to return
        verbatim from a FastAPI handler.

        Returns:
            A dict with ``site_id``, ``status``, ``last_check_at``,
            ``last_ok_at``, ``last_error``, and ``consecutive_failures``.
        """
        return {
            "site_id": self.site_id,
            "status": self.status.value,
            "last_check_at": self.last_check_at.isoformat(),
            "last_ok_at": (
                self.last_ok_at.isoformat()
                if self.last_ok_at is not None
                else None
            ),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }
