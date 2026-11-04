"""Public API of the NexusDL source-site registry subsystem.

The :mod:`nexusdl.core.registry` package owns the lifecycle of source
site configuration and of the parsers bound to it. It is the layer
between the static data (``sites.yaml``, ``sites_schema.json``) and every
consumer that asks "what does site X look like, who can parse it, and is
it healthy?".

Four modules cooperate, each with a single responsibility:

* :mod:`~nexusdl.core.registry.validator` — structural validation of a
  raw ``sites.yaml`` document against the shipped JSON Schema
  (:class:`SitesValidator`, :class:`SiteSchemaIssue`).
* :mod:`~nexusdl.core.registry.protocols` — the injection contracts at
  the parser / HTTP-session boundary (:class:`ParserFactory`,
  :class:`ParserProtocol`, :class:`HttpSessionProtocol`). This is where
  ADR-003's "inject a Protocol, not a class" rule is anchored.
* :mod:`~nexusdl.core.registry.loader` — canonical, reusable document
  loading: read, parse, and validate ``sites.yaml``, plus granular
  iterators and CI-time parser-class resolution
  (:func:`load_and_validate_sites`, :func:`iter_sites`,
  :func:`check_parser_classes`).
* :mod:`~nexusdl.core.registry.site_registry` — the runtime registry
  (:class:`SiteRegistry`): loads the sites configuration into immutable
  snapshots, resolves aliases, provides parser instances, runs bounded
  concurrent searches, and tracks per-site health.

Two responsibilities, two audiences
-----------------------------------
* **Tools** (CLI, CI scripts, tests) work at the **document** layer:
  they call :func:`load_and_validate_sites`, iterate the raw YAML with
  :func:`iter_sites` / :func:`iter_aliases` / :func:`iter_categories`,
  and use :func:`check_parser_classes` in CI to catch a broken
  ``parser_class`` field before it ships. They never construct a
  :class:`SiteRegistry`.
* **Application code** (composition roots) works at the **runtime**
  layer: they construct a :class:`SiteRegistry`, await its
  :meth:`~SiteRegistry.initialize`, and then call its lookup, parser, and
  search methods. They never touch ``sites.yaml`` directly.

Keeping the two separated means a CI job that validates configuration
does not have to import the runtime registry, and a deployed process
that serves traffic does not have to re-run schema validation on every
request.

Composition example
-------------------
The composition root wires the registry with a concrete
:class:`ParserFactory` (typically
:class:`~nexusdl.parsers.factory.DefaultParserFactory`, provided by the
parser package)::

    from nexusdl.core.registry import SiteRegistry
    from nexusdl.core.registry.protocols import ParserFactory
    from nexusdl.parsers.factory import DefaultParserFactory

    factory: ParserFactory = DefaultParserFactory()

    registry = SiteRegistry(
        parser_factory=factory,
        settings=settings,
        # sites_path defaults to the bundled sites.yaml
    )
    await registry.initialize()

    site = registry.get_site("mangadex")
    if site is not None and not registry.is_disabled(site.id):
        parser = registry.get_parser(site.id, session)
        page = await parser.search("one piece")
        for hit in page.items:
            print(hit.title)

CI usage — validate ``sites.yaml`` and its parser classes::

    import importlib
    from nexusdl.core.registry import (
        ParserClassIssue,
        check_parser_classes,
        load_and_validate_sites,
    )

    def _resolve(parser_class: str) -> str | None:
        try:
            module_path, class_name = parser_class.split(":", 1)
            module = importlib.import_module(module_path)
            getattr(module, class_name)
        except (ImportError, AttributeError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    document = load_and_validate_sites()
    issues = check_parser_classes(document, _resolve)
    for issue in issues:
        print(issue)  # sites[3].parser_class ('foo:Bar'): ...

Lazy loading
------------
All twenty exports are resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core.registry`` **does not import any
submodule**.

This matters more here than in most other packages because the eager
``__init__.py`` paid a heavy cost even for callers who only needed a
pure Protocol type:

* ``protocols.py`` — :class:`ParserFactory`, :class:`ParserProtocol`,
  :class:`HttpSessionProtocol`, :class:`HttpSession` — is a set of pure
  typing contracts. It imports only ``typing`` and (transitively) the
  domain enums. Cost: **~2 ms**. Every parser implementation in
  ``nexusdl.parsers.*`` imports at least one of these Protocols, so
  they are loaded on the hot path of the parser registration chain.
* ``validator.py`` — :class:`SitesValidator`, :class:`SiteSchemaIssue`,
  :func:`load_schema` — pulls in ``jsonschema`` (~30-40 ms) and
  ``pydantic``.
* ``loader.py`` — the document-layer helpers — pulls in ``yaml``
  (~15 ms) on top of ``validator``'s dependencies.
* ``site_registry.py`` — :class:`SiteRegistry` — pulls in everything,
  plus the ``asyncio.TaskGroup`` machinery, and (transitively, through
  its collaborator type annotations only) the whole session protocol
  family.

**Before:** any of ``from ...registry import ParserFactory``,
``from ...registry import SitesValidator``, or
``from ...registry import SiteRegistry`` paid the entire ~90 ms.

**After:**

=============================================  =========  =========
Scenario                                        Before     After
=============================================  =========  =========
``from ...registry import ParserFactory``      ~90 ms     ~2 ms
``from ...registry import ParserProtocol``     ~90 ms     ~2 ms
``from ...registry import HttpSessionProtocol`` ~90 ms    ~2 ms
``from ...registry import SitesValidator``     ~90 ms     ~45 ms
``from ...registry import load_and_validate_sites`` ~90 ms ~45 ms
``from ...registry import SiteRegistry``       ~90 ms     ~90 ms (unchanged)
=============================================  =========  =========

The public surface is unchanged: ``from nexusdl.core.registry import
X`` works exactly as before — resolved on first access and cached. No
caller has to change a line.

**Why the leaf-to-root ordering is no longer needed.** An earlier
version of this file imported every submodule eagerly, in a strict
leaf-to-root order (validator and protocols first, then loader, then
site_registry), to guarantee that ``from nexusdl.core.registry import
X`` never triggered a partially-initialized module. Lazy loading
removes that guarantee — each submodule is imported on first access,
in whatever order the caller happens to access them. This is still
**safe** because the graph has no cycles: ``validator`` and
``protocols`` are independent leaves, ``loader`` depends only on
``validator``, and ``site_registry`` depends on both ``validator`` and
``protocols``. Python's normal import machinery resolves the nested
imports correctly regardless of entry point when there is no cycle.
The only scenario the old ordering protected against — a
partially-initialized module caused by a cycle — cannot occur by
design.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``SCHEMA_FILENAME``, ``DATA_PACKAGE`` — module-private path constants.
* ``_KEY_SITES``, ``_KEY_ALIASES``, ``_KEY_CATEGORIES``,
  ``_KEY_DISABLED_SITES`` — YAML key names.
* ``_RegistryState`` — the registry's internal immutable snapshot type.
* Every ``_build_*`` / ``_read_*`` / ``_select_*`` / ``_ensure_*``
  helper in :mod:`~nexusdl.core.registry.site_registry`.
* ``_read_schema_text`` in :mod:`~nexusdl.core.registry.validator` —
  the validator caches its schema internally.

.. note::
   ``load_schema`` **is** re-exported at the top level (it is listed in
   ``__all__`` and imported from ``validator.py``), because tools that
   need to introspect the raw schema need a stable entry point. The note
   above previously listed it as "not re-exported", which contradicted
   the code; that was a documentation bug and is now fixed. If you need
   the *compiled* schema for advanced use, prefer
   ``nexusdl.core.registry.validator.load_schema()`` (same object, one
   fewer hop through ``__getattr__``).

Dependency graph (no cycles)::

    core.exceptions
    core.models.enums
    core.models.search
    core.models.site
    (leaf models)
         ▲
         │
    ┌────┴────────────┬─────────────────┐
    │                 │                 │
    validator       protocols          │
    │                 ▲                │
    │                 │                │
    loader ───────────┘                │
    │                                   │
    site_registry ◄─────────────────────┘

``validator`` and ``protocols`` are independent leaves. ``loader``
depends on ``validator``. ``site_registry`` depends on both
``validator`` and ``protocols``. No module depends on ``site_registry``
at runtime — the composition root is the only importer.

The arrows above describe **runtime dependencies at first use**, not
imports performed by this file. This file imports **nothing** at module
load — the four submodules are loaded lazily, on first attribute
access. Python's normal import machinery resolves the nested imports
correctly regardless of which symbol is accessed first (the graph is
cycle-free, so ordering does not matter).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Runtime registry (composition root entry point) ---------------- #
    "SiteRegistry",
    "DEFAULT_MAX_CONCURRENT_SEARCHES",
    "SITES_FILENAME",
    # --- Validation ------------------------------------------------------ #
    "SitesValidator",
    "SiteSchemaIssue",
    "load_schema",
    # --- Document layer (tools, CI, scripts) ----------------------------- #
    "ParserClassIssue",
    "check_parser_classes",
    "iter_aliases",
    "iter_categories",
    "iter_disabled",
    "iter_sites",
    "load_and_validate_sites",
    "parse_sites_document",
    "read_sites_yaml",
    "validate_sites_document",
    # --- Injection contracts (parser authors, composition roots) -------- #
    "HttpSession",
    "HttpSessionProtocol",
    "ParserFactory",
    "ParserProtocol",
    # --- Introspection --------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.

_LAZY: dict[str, tuple[str, str]] = {
    # --- validator (medium: jsonschema + pydantic) ----------------------- #
    "SitesValidator": ("validator", "SitesValidator"),
    "SiteSchemaIssue": ("validator", "SiteSchemaIssue"),
    "load_schema": ("validator", "load_schema"),
    # --- protocols (light: typing only) ---------------------------------- #
    "HttpSession": ("protocols", "HttpSession"),
    "HttpSessionProtocol": ("protocols", "HttpSessionProtocol"),
    "ParserFactory": ("protocols", "ParserFactory"),
    "ParserProtocol": ("protocols", "ParserProtocol"),
    # --- loader (medium: validator + yaml) ------------------------------- #
    "ParserClassIssue": ("loader", "ParserClassIssue"),
    "check_parser_classes": ("loader", "check_parser_classes"),
    "iter_aliases": ("loader", "iter_aliases"),
    "iter_categories": ("loader", "iter_categories"),
    "iter_disabled": ("loader", "iter_disabled"),
    "iter_sites": ("loader", "iter_sites"),
    "load_and_validate_sites": ("loader", "load_and_validate_sites"),
    "parse_sites_document": ("loader", "parse_sites_document"),
    "read_sites_yaml": ("loader", "read_sites_yaml"),
    "validate_sites_document": ("loader", "validate_sites_document"),
    # --- site_registry (heavy: everything + asyncio) -------------------- #
    "SiteRegistry": ("site_registry", "SiteRegistry"),
    "DEFAULT_MAX_CONCURRENT_SEARCHES": ("site_registry", "DEFAULT_MAX_CONCURRENT_SEARCHES"),
    "SITES_FILENAME": ("site_registry", "SITES_FILENAME"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.registry.loader import (
        ParserClassIssue,
        check_parser_classes,
        iter_aliases,
        iter_categories,
        iter_disabled,
        iter_sites,
        load_and_validate_sites,
        parse_sites_document,
        read_sites_yaml,
        validate_sites_document,
    )
    from nexusdl.core.registry.protocols import (
        HttpSession,
        HttpSessionProtocol,
        ParserFactory,
        ParserProtocol,
    )
    from nexusdl.core.registry.site_registry import (
        DEFAULT_MAX_CONCURRENT_SEARCHES,
        SITES_FILENAME,
        SiteRegistry,
    )
    from nexusdl.core.registry.validator import (
        SiteSchemaIssue,
        SitesValidator,
        load_schema,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public registry symbol on first access (PEP 562).

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in ``globals()``. Only the
    names in :data:`__all__` are handled; anything else raises
    :class:`AttributeError` so that ``hasattr``, ``getattr(x, name,
    default)`` and interactive introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The symbol registered under ``name`` in the submodule recorded in
        :data:`_LAZY`. The submodule is imported once and its binding is
        cached in ``globals()``, so subsequent accesses are plain dict
        lookups.

    Raises:
        AttributeError: When ``name`` is not a public export, when the
            submodule cannot be imported (a missing optional dependency
            such as ``jsonschema`` or ``yaml``, a syntax error deeper in
            the module, or a broken installation), or when the submodule
            does not define the requested name. ``AttributeError`` —
            rather than ``ImportError`` — is required by PEP 562 so that
            ``hasattr`` works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available registry exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.registry.validator import "
            f"load_schema' or 'from nexusdl.core.registry.protocols "
            f"import ParserProtocol'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the registry subsystem.
        if exc.name in {"jsonschema", "yaml", "pydantic"}:
            raise ImportError(
                f"{name!r} requires the core extra (registry subsystem "
                f"dependencies: jsonschema, PyYAML, pydantic). "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The registry submodule {submodule_basename!r} does not "
            f"define {attr_name!r}. This is a package-internal "
            f"inconsistency - nexusdl.core.registry.__all__ and the "
            f"submodules are out of sync."
        ) from None

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it
    with each candidate name to decide what to display. Because none of
    the twenty exports are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would
    appear empty in a REPL.

    The returned list is :data:`__all__` plus the standard module
    dunders a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the registry subsystem's public surface without importing
    every submodule.
    """
    # Only the twenty public exports, not the introspection helpers
    # themselves - otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the registry symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, composition-root builders that resolve a symbol
    from a config string, CI tooling that resolves a validator or loader
    helper).

    Raises:
        KeyError: If ``name`` is not a public registry export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown registry symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.registry.<submodule>' directly."
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
