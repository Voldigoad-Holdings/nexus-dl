"""Runtime registry of source sites and their parsers.

:class:`SiteRegistry` is the layer between *configuration* (the parsed
``sites.yaml``) and *execution* (a ready-to-use parser, a live search, a
health check). It owns four concerns:

* **Loading** — parse ``sites.yaml`` into :class:`~nexusdl.core.models.site.SiteConfig`
  instances, applying ``aliases``, ``categories``, and the
  ``disabled_sites`` section. Validation is layered: structural first
  (via :class:`~nexusdl.core.registry.validator.SitesValidator`), then
  semantic per site (via ``SiteConfig.__post_init__``).
* **Lookup** — resolve a site id (or one of its aliases) to a config, or
  a site id to a fresh parser instance, via an injected
  :class:`~nexusdl.core.registry.protocols.ParserFactory`.
* **Search fan-out** — run a query across every enabled site with a
  bounded concurrency ceiling, yielding results as they arrive, with
  per-site error isolation.
* **Health tracking** — record a :class:`~nexusdl.core.models.site.SiteHealthSnapshot`
  on every check and expose it for the ``GET /api/v1/sites/{id}/health``
  endpoint.

Design invariants
-----------------
* **Immutable state swap** — the loaded configuration is held in a
  single frozen :class:`_RegistryState`; :meth:`SiteRegistry.reload`
  builds a fresh one and assigns it in one operation. Readers
  (:meth:`get_site`, :meth:`list_sites`, :meth:`list_categories`) never
  take a lock and never observe a partially-reloaded registry.
* **Dependency inversion** — the parser factory is injected as a
  :class:`~nexusdl.core.registry.protocols.ParserFactory` Protocol. This
  module **never imports** :mod:`nexusdl.parsers` at runtime (rule R1);
  the factory is provided by the composition root.
* **Per-site error isolation** — a broken parser, a slow network, or an
  unreachable site degrades a single site in :meth:`search_all` and
  :meth:`health_check`. The rest of the pipeline is unaffected.
* **Async-native** — loading is a coroutine (it does disk I/O in a
  worker thread); searching and health checking are async. Lookup
  methods stay synchronous: they are pure dict operations on the
  already-loaded snapshot.

Lifecycle
---------
::

    registry = SiteRegistry(parser_factory=factory, settings=settings)
    await registry.initialize()          # load sites.yaml, validate, build state
    try:
        site = registry.get_site("mangadex")
        parser = registry.get_parser("mangadex", session)
        async for hit in registry.search_all("one piece", session=session):
            ...
    finally:
        # No explicit teardown required — the registry holds no
        # resources beyond immutable snapshots.
        ...

:meth:`initialize` is idempotent and must be awaited before any
lookup. Calling a lookup method before ``initialize()`` raises
:class:`~nexusdl.core.exceptions.ConfigError` with a message that names
the missing call, so a bug in a composition root surfaces immediately
rather than as an empty result set.

Dependency graph (no cycles)::

    core.exceptions
    core.models.enums
    core.models.search
    core.models.site
    core.registry.validator
    core.registry.protocols
    nexusdl.data (sites.yaml)

No dependency on ``nexusdl.parsers.*`` at runtime — see
:mod:`nexusdl.core.registry.protocols` for the injection contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml
from loguru import logger

from nexusdl.core.exceptions import ConfigError, ParserNotFoundError
from nexusdl.core.models.enums import Language, SiteHealth
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_RATE_LIMIT,
    SiteCapabilities,
    SiteConfig,
    SiteHealthSnapshot,
)
from nexusdl.core.registry.validator import SitesValidator

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.registry.protocols import (
        HttpSession,
        ParserFactory,
        ParserProtocol,
    )


__all__ = [
    "DEFAULT_MAX_CONCURRENT_SEARCHES",
    "SITES_FILENAME",
    "SiteRegistry",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the sites bundle inside :mod:`nexusdl.data`.
SITES_FILENAME: Final[str] = "sites.yaml"

#: Python package that ships the default ``sites.yaml``.
_DATA_PACKAGE: Final[str] = "nexusdl.data"

#: Default ceiling on concurrent per-site searches in :meth:`SiteRegistry.search_all`.
DEFAULT_MAX_CONCURRENT_SEARCHES: Final[int] = 4

#: Schema key for the sites array in ``sites.yaml``.
_KEY_SITES: Final[str] = "sites"

#: Schema key for the alias map.
_KEY_ALIASES: Final[str] = "aliases"

#: Schema key for the categories map.
_KEY_CATEGORIES: Final[str] = "categories"

#: Schema key for the disabled-sites list.
_KEY_DISABLED_SITES: Final[str] = "disabled_sites"


_log = logger.bind(component="registry.sites")


# --------------------------------------------------------------------------- #
# Immutable state snapshot                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _RegistryState:
    """Immutable snapshot of everything :meth:`SiteRegistry.reload` produces.

    Assigned as a single object so that a concurrent reader never observes
    a half-built registry.

    Attributes:
        sites: Canonical site configurations, keyed by
            :attr:`SiteConfig.id`.
        aliases: Mapping of alias → canonical site id.
        categories: Mapping of category name → tuple of canonical site ids.
        disabled: Union of the ``disabled_sites:`` section in
            ``sites.yaml`` and the ``Settings.sites.disabled`` override.
    """

    sites: Mapping[str, SiteConfig]
    aliases: Mapping[str, str]
    categories: Mapping[str, tuple[str, ...]]
    disabled: frozenset[str]


# --------------------------------------------------------------------------- #
# SiteRegistry                                                                #
# --------------------------------------------------------------------------- #


class SiteRegistry:
    """Runtime registry of source sites and their parsers.

    See the module docstring for the full contract.

    Args:
        parser_factory: The factory that resolves a :class:`SiteConfig` to
            a parser instance. Must satisfy
            :class:`nexusdl.core.registry.protocols.ParserFactory`.
        sites_path: Optional path to a ``sites.yaml`` file. When ``None``,
            the schema bundled inside :mod:`nexusdl.data` is used.
        settings: Optional :class:`~nexusdl.core.config.Settings`. When
            provided, ``settings.sites.disabled`` (if present) is merged
            into the ``disabled`` set on every reload.
        validator: Optional :class:`~nexusdl.core.registry.validator.SitesValidator`.
    """

    def __init__(
        self,
        *,
        parser_factory: ParserFactory,
        sites_path: Path | None = None,
        settings: Settings | None = None,
        validator: SitesValidator | None = None,
    ) -> None:
        self._parser_factory = parser_factory
        self._sites_path = sites_path
        self._settings = settings
        self._validator = (
            validator if validator is not None else SitesValidator()
        )

        self._state: _RegistryState = _RegistryState(
            sites={},
            aliases={},
            categories={},
            disabled=frozenset(),
        )

        self._health: dict[str, SiteHealthSnapshot] = {}

        self._reload_lock = asyncio.Lock()

        self._initialized = False

        self._log = logger.bind(component="registry.sites")

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """Load the sites configuration. Idempotent.

        Raises:
            ConfigError: If loading or validation fails.
        """
        if self._initialized:
            return
        async with self._reload_lock:
            if self._initialized:
                return
            await self._reload_locked()
            self._initialized = True

    async def reload(self) -> None:
        """Reload the sites configuration, replacing the current snapshot.

        Raises:
            ConfigError: If the YAML cannot be read, fails schema
                validation, or contains duplicate site ids, unknown
                category/alias targets, or a site whose ``SiteConfig``
                construction fails.
        """
        async with self._reload_lock:
            await self._reload_locked()

    async def _reload_locked(self) -> None:
        """Body of :meth:`reload`, called with the reload lock held."""
        document = await asyncio.to_thread(
            self._read_and_validate_yaml
        )
        new_state = self._build_state(document)
        old_state = self._state
        self._state = new_state

        new_health: dict[str, SiteHealthSnapshot] = {}
        for site_id, snapshot in self._health.items():
            if site_id not in new_state.sites:
                continue
            if old_state.sites.get(site_id) == new_state.sites[site_id]:
                new_health[site_id] = snapshot
        self._health = new_health

        self._log.info(
            "Loaded {} site(s) from {} ({} disabled)",
            len(new_state.sites),
            "bundle" if self._sites_path is None else str(self._sites_path),
            len(new_state.disabled),
        )

    # ------------------------------------------------------------------ #
    # Read API                                                           #
    # ------------------------------------------------------------------ #

    def get_site(self, site_id: str) -> SiteConfig | None:
        """Return the config for *site_id*, or ``None`` if unknown.

        *site_id* may be a canonical site id or one of its aliases.
        """
        self._ensure_initialized("get_site")
        canonical = self._state.aliases.get(site_id, site_id)
        return self._state.sites.get(canonical)

    def is_disabled(self, site_id: str) -> bool:
        """Return ``True`` iff *site_id* is disabled."""
        self._ensure_initialized("is_disabled")
        canonical = self._state.aliases.get(site_id, site_id)
        return canonical in self._state.disabled

    def list_sites(
        self,
        *,
        language: Language | None = None,
        include_adult: bool = False,
        include_disabled: bool = False,
    ) -> list[SiteConfig]:
        """Return the site configurations matching the filters, sorted by name."""
        self._ensure_initialized("list_sites")
        sites: list[SiteConfig] = list(self._state.sites.values())
        if language is not None:
            sites = [s for s in sites if s.language is language]
        if not include_adult:
            sites = [s for s in sites if not s.adult]
        if not include_disabled:
            sites = [
                s for s in sites if s.id not in self._state.disabled
            ]
        return sorted(sites, key=lambda s: s.name)

    def list_categories(self) -> dict[str, tuple[SiteConfig, ...]]:
        """Return every category and its (existing, non-disabled) sites."""
        self._ensure_initialized("list_categories")
        result: dict[str, tuple[SiteConfig, ...]] = {}
        for category, site_ids in self._state.categories.items():
            members: list[SiteConfig] = []
            for site_id in site_ids:
                if site_id in self._state.disabled:
                    continue
                site = self._state.sites.get(site_id)
                if site is None:
                    continue
                members.append(site)
            members.sort(key=lambda s: s.name)
            result[category] = tuple(members)
        return result

    # ------------------------------------------------------------------ #
    # Parser construction                                                #
    # ------------------------------------------------------------------ #

    def get_parser(
        self, site_id: str, session: HttpSession
    ) -> ParserProtocol:
        """Build a fresh parser for *site_id*.

        Args:
            site_id: Canonical site id or alias.
            session: The HTTP session the parser should use for network
                I/O.

        Returns:
            A ready-to-use parser bound to *site_id* and *session*.

        Raises:
            ParserNotFoundError: If *site_id* is unknown or disabled, or
                if the injected :class:`ParserFactory` cannot produce a
                parser for it.
        """
        self._ensure_initialized("get_parser")
        site = self.get_site(site_id)
        if site is None:
            raise ParserNotFoundError(
                f"unknown site {site_id!r}"
            )
        if site.id in self._state.disabled:
            raise ParserNotFoundError(
                f"site {site.id!r} is disabled"
            )
        try:
            return self._parser_factory.create(site, session)
        except ParserNotFoundError:
            raise
        except Exception as exc:
            raise ParserNotFoundError(
                f"cannot build parser for site {site.id!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Search fan-out                                                     #
    # ------------------------------------------------------------------ #

    async def search_all(
        self,
        query: str,
        *,
        session: HttpSession,
        languages: Sequence[Language] | None = None,
        include_adult: bool = False,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_SEARCHES,
    ) -> AsyncIterator[SearchResult]:
        """Search across enabled sites, yielding hits as they arrive.

        Args:
            query: The search query string.
            session: The HTTP session shared with every parser.
            languages: When provided, only sites whose
                :attr:`SiteConfig.language` is in the sequence are
                queried.
            include_adult: When ``False`` (the default), adult sites are
                skipped.
            max_concurrent: Ceiling on concurrent per-site searches.

        Yields:
            Every :class:`~nexusdl.core.models.search.SearchResult` from
            every queried site, in completion order.

        Raises:
            ValueError: If ``max_concurrent < 1``.
            asyncio.CancelledError: If the caller cancels the surrounding
                task.
        """
        self._ensure_initialized("search_all")
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {max_concurrent}"
            )

        sites = self._select_sites_for_search(
            languages=languages, include_adult=include_adult
        )
        if not sites:
            return

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _search_one(site: SiteConfig) -> list[SearchResult]:
            async with semaphore:
                try:
                    parser = self.get_parser(site.id, session)
                    # ``parser.search`` is an *async generator* — it must
                    # be iterated with ``async for``, not awaited, and it
                    # yields individual ``SearchResult`` objects (there is
                    # no wrapping "page" object with an ``.items``
                    # attribute).
                    collected: list[SearchResult] = []
                    async for hit in parser.search(query, page=1):
                        collected.append(hit)
                    return collected
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — isolation
                    self._log.warning(
                        "search_all: site {!r} failed: {}: {}",
                        site.id,
                        type(exc).__name__,
                        exc,
                    )
                    return []

        tasks = [
            asyncio.create_task(_search_one(site), name=f"search-{site.id}")
            for site in sites
        ]
        try:
            for coro in asyncio.as_completed(tasks):
                results = await coro
                for result in results:
                    yield result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Health                                                             #
    # ------------------------------------------------------------------ #

    async def health_check(
        self, site_id: str, session: HttpSession
    ) -> SiteHealthSnapshot:
        """Run a health check for *site_id* and update its snapshot.

        Args:
            site_id: Canonical site id or alias.
            session: The HTTP session the parser should use.

        Returns:
            The freshly updated :class:`~nexusdl.core.models.site.SiteHealthSnapshot`.

        Raises:
            ParserNotFoundError: If *site_id* is unknown or its parser
                cannot be constructed.
        """
        self._ensure_initialized("health_check")
        site = self.get_site(site_id)
        if site is None:
            raise ParserNotFoundError(f"unknown site {site_id!r}")

        now = datetime.now(UTC)
        previous = self._health.get(site.id)

        try:
            parser = self._parser_factory.create(site, session)
            method = getattr(parser, "health_check", None)
            if method is None:
                snapshot = SiteHealthSnapshot(
                    site_id=site.id,
                    status=SiteHealth.UNKNOWN,
                    last_check_at=now,
                    last_ok_at=(
                        previous.last_ok_at if previous is not None else None
                    ),
                    last_error="parser does not implement health_check",
                    consecutive_failures=(
                        previous.consecutive_failures
                        if previous is not None
                        else 0
                    ),
                )
                self._health[site.id] = snapshot
                return snapshot
            await method()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any failure is a health signal
            failures = (
                previous.consecutive_failures + 1
                if previous is not None
                else 1
            )
            snapshot = SiteHealthSnapshot(
                site_id=site.id,
                status=SiteHealth.DOWN,
                last_check_at=now,
                last_ok_at=(
                    previous.last_ok_at if previous is not None else None
                ),
                last_error=str(exc) or type(exc).__name__,
                consecutive_failures=failures,
            )
        else:
            snapshot = SiteHealthSnapshot(
                site_id=site.id,
                status=SiteHealth.HEALTHY,
                last_check_at=now,
                last_ok_at=now,
                last_error=None,
                consecutive_failures=0,
            )

        self._health[site.id] = snapshot
        self._log.debug(
            "health_check({}): {} (failures={})",
            site.id,
            snapshot.status.value,
            snapshot.consecutive_failures,
        )
        return snapshot

    def get_health(self, site_id: str) -> SiteHealthSnapshot | None:
        """Return the last health snapshot for *site_id*, or ``None``."""
        self._ensure_initialized("get_health")
        site = self.get_site(site_id)
        if site is None:
            return None
        return self._health.get(site.id)

    # ------------------------------------------------------------------ #
    # Internals — YAML loading                                           #
    # ------------------------------------------------------------------ #

    def _read_and_validate_yaml(self) -> Mapping[str, Any]:
        """Read and structurally validate the sites YAML. Runs in a thread.

        Raises:
            ConfigError: On any read, parse, or schema failure.
        """
        try:
            raw_text = self._read_sites_text()
        except OSError as exc:
            raise ConfigError(
                f"cannot read sites configuration: {exc}"
            ) from exc

        try:
            document = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"invalid YAML in sites configuration: {exc}"
            ) from exc

        if not isinstance(document, Mapping):
            raise ConfigError(
                "sites configuration must be a mapping at the top level, "
                f"got {type(document).__name__}"
            )

        self._validator.validate(document)
        return document

    def _read_sites_text(self) -> str:
        """Read the sites YAML from disk or from the bundle.

        Raises:
            OSError: If a user-provided file cannot be read, or the
                bundled resource cannot be located.
        """
        if self._sites_path is not None:
            return self._sites_path.read_text(encoding="utf-8")
        resource = _resource_files(_DATA_PACKAGE) / SITES_FILENAME
        return resource.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Internals — state construction                                     #
    # ------------------------------------------------------------------ #

    def _build_state(
        self, document: Mapping[str, Any]
    ) -> _RegistryState:
        """Build a fresh :class:`_RegistryState` from a validated document.

        Raises:
            ConfigError: On duplicate site ids, on any per-site
                construction failure, or on unknown alias/category
                targets.
        """
        raw_sites = document.get(_KEY_SITES, [])
        if not isinstance(raw_sites, list):
            raise ConfigError(
                f"{_KEY_SITES!r} must be a list, got "
                f"{type(raw_sites).__name__}"
            )

        sites: dict[str, SiteConfig] = {}
        for index, raw_site in enumerate(raw_sites):
            site = self._build_site_config(raw_site, index)
            if site.id in sites:
                raise ConfigError(
                    f"duplicate site id {site.id!r} at {_KEY_SITES}[{index}]"
                )
            sites[site.id] = site

        aliases = self._build_aliases(document, sites)
        categories = self._build_categories(document, sites)
        disabled = self._build_disabled(document, sites)

        return _RegistryState(
            sites=sites,
            aliases=aliases,
            categories=categories,
            disabled=disabled,
        )

    @staticmethod
    def _build_site_config(
        raw: Mapping[str, Any], index: int
    ) -> SiteConfig:
        """Translate one ``sites[i]`` entry into a :class:`SiteConfig`.

        Raises:
            ConfigError: If a required key is absent or a value has the
                wrong type. The error message names the field and the
                position in the list.
        """
        if not isinstance(raw, Mapping):
            raise ConfigError(
                f"{_KEY_SITES}[{index}] must be a mapping, got "
                f"{type(raw).__name__}"
            )
        try:
            raw_capabilities = raw.get("capabilities") or {}
            if not isinstance(raw_capabilities, Mapping):
                raise ConfigError(
                    f"{_KEY_SITES}[{index}].capabilities must be a "
                    f"mapping, got {type(raw_capabilities).__name__}"
                )
            raw_headers = raw.get("default_headers") or {}
            if not isinstance(raw_headers, Mapping):
                raise ConfigError(
                    f"{_KEY_SITES}[{index}].default_headers must be a "
                    f"mapping, got {type(raw_headers).__name__}"
                )
            raw_cookies = raw.get("cookies_required") or ()
            if not isinstance(raw_cookies, (list, tuple)):
                raise ConfigError(
                    f"{_KEY_SITES}[{index}].cookies_required must be a "
                    f"list, got {type(raw_cookies).__name__}"
                )
            raw_domains = raw["domains"]
            if not isinstance(raw_domains, (list, tuple)):
                raise ConfigError(
                    f"{_KEY_SITES}[{index}].domains must be a list, got "
                    f"{type(raw_domains).__name__}"
                )
            return SiteConfig(
                id=raw["id"],
                name=raw["name"],
                domains=tuple(str(d) for d in raw_domains),
                parser_class=raw["parser_class"],
                # ``Language`` members are declared with lowercase values
                # (``FR = "fr"``); matching is by value.  ``sites.yaml``
                # uses the same lowercase form, but we lowercase anyway to
                # tolerate a future YAML that accidentally capitalises.
                language=Language(str(raw["language"]).lower()),
                adult=bool(raw["adult"]),
                capabilities=SiteCapabilities(**dict(raw_capabilities)),
                default_headers={
                    str(k): str(v) for k, v in raw_headers.items()
                },
                cookies_required=tuple(str(c) for c in raw_cookies),
                max_concurrent_downloads=int(
                    raw.get(
                        "max_concurrent_downloads",
                        DEFAULT_MAX_CONCURRENT_DOWNLOADS,
                    )
                ),
                rate_limit=float(
                    raw.get("rate_limit", DEFAULT_RATE_LIMIT)
                ),
            )
        except ConfigError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                f"invalid site configuration at {_KEY_SITES}[{index}]: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _build_aliases(
        document: Mapping[str, Any],
        sites: Mapping[str, SiteConfig],
    ) -> Mapping[str, str]:
        """Build and validate the alias map from the ``aliases:`` section."""
        raw_aliases = document.get(_KEY_ALIASES) or {}
        if not isinstance(raw_aliases, Mapping):
            raise ConfigError(
                f"{_KEY_ALIASES!r} must be a mapping, got "
                f"{type(raw_aliases).__name__}"
            )
        aliases: dict[str, str] = {}
        for alias, target in raw_aliases.items():
            if not isinstance(alias, str) or not isinstance(target, str):
                raise ConfigError(
                    f"{_KEY_ALIASES}[{alias!r}] must map a string to a "
                    f"string, got {type(target).__name__}"
                )
            if target not in sites:
                raise ConfigError(
                    f"alias {alias!r} points to unknown site {target!r}"
                )
            aliases[alias] = target
        return aliases

    @staticmethod
    def _build_categories(
        document: Mapping[str, Any],
        sites: Mapping[str, SiteConfig],
    ) -> Mapping[str, tuple[str, ...]]:
        """Build and validate the categories map from ``categories:``."""
        raw_categories = document.get(_KEY_CATEGORIES) or {}
        if not isinstance(raw_categories, Mapping):
            raise ConfigError(
                f"{_KEY_CATEGORIES!r} must be a mapping, got "
                f"{type(raw_categories).__name__}"
            )
        categories: dict[str, tuple[str, ...]] = {}
        for category, site_ids in raw_categories.items():
            if not isinstance(category, str):
                raise ConfigError(
                    f"{_KEY_CATEGORIES} names must be strings, got "
                    f"{type(category).__name__}"
                )
            if not isinstance(site_ids, (list, tuple)):
                raise ConfigError(
                    f"{_KEY_CATEGORIES}[{category!r}] must be a list, got "
                    f"{type(site_ids).__name__}"
                )
            members: list[str] = []
            for raw_id in site_ids:
                if not isinstance(raw_id, str):
                    raise ConfigError(
                        f"{_KEY_CATEGORIES}[{category!r}] entries must be "
                        f"strings, got {type(raw_id).__name__}"
                    )
                if raw_id not in sites:
                    raise ConfigError(
                        f"category {category!r} lists unknown site "
                        f"{raw_id!r}"
                    )
                members.append(raw_id)
            categories[category] = tuple(members)
        return categories

    def _build_disabled(
        self,
        document: Mapping[str, Any],
        sites: Mapping[str, SiteConfig],
    ) -> frozenset[str]:
        """Build the disabled-site set from YAML and settings, merged.

        ``disabled_sites`` entries accept **two forms**:

        * A bare string — ``- batoto`` — the simple form.
        * A mapping — ``- {id: batoto, reason: "..."}`` — the documented
          form, where ``reason`` is preserved as a comment in the YAML
          and ignored by this loader.

        This dual form lets the YAML carry a human-readable explanation
        for why a site is disabled without forcing the loader to know
        anything about the extra keys.
        """
        raw_disabled = document.get(_KEY_DISABLED_SITES) or ()
        if not isinstance(raw_disabled, (list, tuple)):
            raise ConfigError(
                f"{_KEY_DISABLED_SITES!r} must be a list, got "
                f"{type(raw_disabled).__name__}"
            )
        yaml_disabled: set[str] = set()
        for entry in raw_disabled:
            site_id = self._extract_disabled_id(entry)
            # A disabled entry that names an unknown site is a config-
            # drift warning, not an error: the YAML may still be in sync
            # with an older site list, and refusing to load would break
            # a deployment that simply removed a site.
            if site_id not in sites:
                self._log.warning(
                    "disabled_sites references unknown site {!r}; "
                    "ignoring",
                    site_id,
                )
                continue
            yaml_disabled.add(site_id)

        return frozenset(yaml_disabled | self._settings_disabled())

    @staticmethod
    def _extract_disabled_id(entry: Any) -> str:
        """Extract the site id from a ``disabled_sites`` entry.

        Accepts either a bare string or a mapping with an ``id`` key.
        Any other shape is a configuration error.

        Raises:
            ConfigError: If the entry is neither a string nor a mapping
                with a string ``id``.
        """
        if isinstance(entry, str):
            return entry
        if isinstance(entry, Mapping):
            site_id = entry.get("id")
            if not isinstance(site_id, str):
                raise ConfigError(
                    f"{_KEY_DISABLED_SITES} entry has a non-string 'id': "
                    f"{site_id!r}"
                )
            return site_id
        raise ConfigError(
            f"{_KEY_DISABLED_SITES} entries must be strings or mappings "
            f"with an 'id' key, got {type(entry).__name__}"
        )

    def _settings_disabled(self) -> frozenset[str]:
        """Return ``Settings.sites.disabled`` as a frozen set, or empty."""
        if self._settings is None:
            return frozenset()
        sites_cfg = getattr(self._settings, "sites", None)
        if sites_cfg is None:
            return frozenset()
        disabled = getattr(sites_cfg, "disabled", None)
        if disabled is None:
            return frozenset()
        return frozenset(str(s) for s in disabled)

    # ------------------------------------------------------------------ #
    # Internals — search helpers                                         #
    # ------------------------------------------------------------------ #

    def _select_sites_for_search(
        self,
        *,
        languages: Sequence[Language] | None,
        include_adult: bool,
    ) -> list[SiteConfig]:
        """Return the sites eligible for a text search, sorted by name.

        A site is eligible iff:

        * it is not disabled,
        * it advertises :attr:`SiteCapabilities.search`,
        * its :attr:`SiteConfig.adult` flag matches ``include_adult``,
        * its :attr:`SiteConfig.language` is in *languages* (when given).
        """
        allowed_languages = (
            frozenset(languages) if languages is not None else None
        )
        selected: list[SiteConfig] = []
        for site in self._state.sites.values():
            if site.id in self._state.disabled:
                continue
            if not site.capabilities.search:
                continue
            if not include_adult and site.adult:
                continue
            if (
                allowed_languages is not None
                and site.language not in allowed_languages
            ):
                continue
            selected.append(site)
        return sorted(selected, key=lambda s: s.name)

    # ------------------------------------------------------------------ #
    # Internals — initialization guard                                   #
    # ------------------------------------------------------------------ #

    def _ensure_initialized(self, method_name: str) -> None:
        """Raise :class:`ConfigError` if :meth:`initialize` has not run.

        Args:
            method_name: The name of the public method that was called,
                used in the error message so the caller can trace the
                path that forgot to initialize.
        """
        if not self._initialized:
            raise ConfigError(
                f"SiteRegistry.{method_name} was called before "
                f"initialize(); the composition root must await "
                f"registry.initialize() during startup"
            )
