"""Parser factory — registration and instantiation of parser classes.

Every concrete parser in :mod:`nexusdl.parsers` calls
:func:`register` at module scope::

    register("asurascans", AsuraScansParser)

Importing :mod:`nexusdl.parsers` therefore populates the module-level
registry with every shipped parser.  The composition root — CLI, TUI, GUI,
web backend, tests — creates a :class:`DefaultParserFactory` and hands it to
:class:`~nexusdl.core.registry.site_registry.SiteRegistry`, which uses it to
instantiate a parser for a given site id on demand.

Two registries, two responsibilities
------------------------------------
* :class:`~nexusdl.core.registry.site_registry.SiteRegistry` owns the
  **site catalogue** loaded from ``data/sites.yaml`` and answers questions
  about which sites exist, their domains, languages and capabilities.
* This factory owns the **implementation map**: which Python class handles
  which site id.  It knows nothing about ``sites.yaml`` and never validates
  sites — that is :class:`SiteRegistry`'s job.

A site can therefore be declared in ``sites.yaml`` without a parser (the
admin disabled it, or its parser has not shipped yet), and a parser class
can be registered without being present in ``sites.yaml`` (a development
build).  Cross-validation — ensuring every enabled site has a parser — is
performed by :mod:`nexusdl.core.registry.validator`, not by this module.

Thread safety
-------------
Registration runs at import time; the actual import of
:mod:`nexusdl.parsers` is serialized by Python's import lock.  Lookups run
during normal operation and may occur from multiple threads (e.g. test
suites using ``pytest-xdist``, or an async application with several
event-loop threads in the future).  The registry is therefore protected by
a :class:`threading.Lock`, and every public function acquires it before
touching the underlying dict.

Re-registration policy
----------------------
A parser may be re-registered with the **same** class (harmless, allows
``importlib.reload`` in tests).  Re-registration with a **different** class
raises :class:`NexusDLError`: it always indicates a programming error
(two parser modules mistakenly declaring the same ``site_id``), and
silently preferring one over the other would hide the bug.

Testing helpers
---------------
:func:`unregister` and :func:`reset_registry` exist for tests.  Note that
once a parser module has been imported, Python caches the module; calling
:func:`reset_registry` does **not** re-trigger the module-scope
``register(...)`` call on a subsequent import.  Tests that need to
repopulate the registry must use :func:`importlib.reload` on the relevant
module.
"""
from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

from nexusdl.core.exceptions import NexusDLError, ParserNotFoundError
from nexusdl.parsers.base import BaseParser

if TYPE_CHECKING:
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.flaresolverr import FlareSolverr
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.core.session.playwright_pool import PlaywrightPool


__all__ = [
    "DefaultParserFactory",
    "get_registered_parser",
    "list_registered_parsers",
    "register",
    "reset_registry",
    "unregister",
]


# ---------------------------------------------------------------------------
# Registry internals
# ---------------------------------------------------------------------------

#: Canonical site-id pattern, mirroring ``data/sites_schema.json``.
#: Enforced at registration time so that a typo in a parser module fails
#: immediately (at import), not at the first attempt to look it up.
_SITE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Backing map.  ``site_id`` → parser class.
_registry: dict[str, type[BaseParser]] = {}

#: Protects :data:`_registry`.  Registration is rare and cheap, so a plain
#: ``threading.Lock`` is sufficient; a reader-writer lock would be overkill.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public registration API
# ---------------------------------------------------------------------------
def register(site_id: str, parser_cls: type[BaseParser]) -> None:
    """Register *parser_cls* under *site_id*.

    Called at module scope by every concrete parser.  Importing
    :mod:`nexusdl.parsers` triggers every parser's module-scope call, so
    the registry is fully populated once that import completes.

    Args:
        site_id: The site's canonical id, as declared in
            ``data/sites.yaml``.  Must match
            :data:`_SITE_ID_PATTERN` (``^[a-z][a-z0-9_-]*$``).
        parser_cls: The parser class.  Must be a subclass of
            :class:`~nexusdl.parsers.base.BaseParser`.

    Raises:
        NexusDLError: If *site_id* does not match the required pattern, if
            *parser_cls* is not a :class:`BaseParser` subclass, or if
            *site_id* is already registered with a **different** class.
    """
    if not isinstance(site_id, str) or not _SITE_ID_PATTERN.fullmatch(site_id):
        raise NexusDLError(
            f"Cannot register parser {getattr(parser_cls, '__name__', parser_cls)!r} "
            f"for site {site_id!r}: site ids must match "
            f"{_SITE_ID_PATTERN.pattern!r} (lowercase letters, digits, "
            f"underscores and hyphens; starting with a letter)."
        )

    if not (isinstance(parser_cls, type) and issubclass(parser_cls, BaseParser)):
        raise NexusDLError(
            f"Cannot register parser {parser_cls!r} for site {site_id!r}: "
            f"parser classes must inherit from BaseParser."
        )

    with _lock:
        existing = _registry.get(site_id)
        if existing is not None and existing is not parser_cls:
            raise NexusDLError(
                f"Site {site_id!r} is already registered with "
                f"{existing.__module__}.{existing.__qualname__}; cannot "
                f"re-register with "
                f"{parser_cls.__module__}.{parser_cls.__qualname__}. "
                f"This usually indicates two parser modules mistakenly "
                f"declare the same site_id."
            )
        _registry[site_id] = parser_cls


def unregister(site_id: str) -> None:
    """Remove *site_id* from the registry.

    Idempotent: removing an absent id is a no-op.  Primarily a testing
    helper — production code should never unregister a parser.
    """
    with _lock:
        _registry.pop(site_id, None)


def reset_registry() -> None:
    """Remove every registered parser.

    Testing helper.  Note that once a parser module has been imported,
    Python caches the module; a subsequent ``import`` does **not** re-run
    its module-scope :func:`register` call.  Tests that need to repopulate
    the registry must use :func:`importlib.reload` on the relevant module.
    """
    with _lock:
        _registry.clear()


def list_registered_parsers() -> dict[str, type[BaseParser]]:
    """Return a copy of the current registry.

    Returns:
        A new ``dict`` mapping ``site_id`` → parser class.  Mutating the
        returned dict does not affect the registry.
    """
    with _lock:
        return dict(_registry)


def get_registered_parser(site_id: str) -> type[BaseParser]:
    """Return the parser class registered for *site_id*.

    Args:
        site_id: The site's canonical id.

    Returns:
        The registered parser class.

    Raises:
        ParserNotFoundError: If no parser is registered for *site_id*.
            The exception message lists the currently registered site ids
            and reminds the caller to import :mod:`nexusdl.parsers` first
            if the registry is unexpectedly empty.
    """
    with _lock:
        try:
            return _registry[site_id]
        except KeyError:
            available = sorted(_registry.keys())
            available_repr = ", ".join(available) if available else "none"
            raise ParserNotFoundError(
                f"No parser registered for site {site_id!r}. "
                f"Registered sites: {available_repr}. "
                f"If this list is unexpectedly empty or short, make sure "
                f"`import nexusdl.parsers` has run before using the "
                f"factory — that import is what triggers the module-scope "
                f"`register(...)` calls in every parser module."
            ) from None


# ---------------------------------------------------------------------------
# Factory implementation
# ---------------------------------------------------------------------------
class DefaultParserFactory:
    """Default implementation of the ParserFactory Protocol.

    Structurally conforms to
    :class:`~nexusdl.core.registry.protocols.ParserFactory`, whose only
    method is::

        def create(self, site: SiteConfig, session: HttpSession) -> BaseParser: ...

    The factory is deliberately stateless with respect to *which* parser
    handles *which* site — that mapping lives in the module-level registry,
    shared by every factory instance.  The factory's only per-instance state
    is the pair of optional session-layer dependencies (Playwright pool and
    FlareSolverr client) that every parser may need.

    Those two dependencies are injected **into the factory**, not passed
    on every call, because the ParserFactory Protocol only knows about
    ``(site, session)``.  The composition root builds the factory once,
    with the process-wide Playwright pool and FlareSolverr client, then
    hands it to :class:`SiteRegistry`.

    Args:
        playwright_pool: Optional shared Playwright pool.  Forwarded to
            every parser instance, which may use it via
            :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` or
            :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`.
        flaresolverr: Optional shared FlareSolverr client.  Forwarded to
            every parser instance, which may use it via
            :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`.
    """

    def __init__(
        self,
        *,
        playwright_pool: "PlaywrightPool | None" = None,
        flaresolverr: "FlareSolverr | None" = None,
    ) -> None:
        self._playwright = playwright_pool
        self._flaresolverr = flaresolverr

    def create(
        self,
        site: "SiteConfig",
        session: "HttpSession",
    ) -> BaseParser:
        """Instantiate the parser registered for *site*.

        Looks up the parser class in the registry and constructs it with
        the site config, the shared session, and the process-wide
        Playwright pool and FlareSolverr client (both optional).

        Args:
            site: The site config from :class:`SiteRegistry`.
            session: The shared :class:`HttpSession`.

        Returns:
            A fresh parser instance.

        Raises:
            ParserNotFoundError: If no parser is registered for
                ``site.id``.  See :func:`get_registered_parser`.
            NexusDLError: If parser construction fails.  The parser's own
                exceptions are propagated unchanged so that the failure
                points at the real cause (e.g.
                :class:`NexusDLPlaywrightError` from a parser that
                requires a browser).
        """
        parser_cls = get_registered_parser(site.id)
        return parser_cls(
            site,
            session,
            playwright_pool=self._playwright,
            flaresolverr=self._flaresolverr,
        )
