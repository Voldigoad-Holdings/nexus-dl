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
* ``_read_schema_text`` in :mod:`~nexusdl.core.registry.validator` and
  ``load_schema`` — the validator caches its schema internally; tools
  that need the raw schema call
  :func:`~nexusdl.core.registry.validator.load_schema` from the
  submodule directly.

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

The ``__init__.py`` imports submodules in leaf-to-root order so that a
``from nexusdl.core.registry import X`` never triggers a
partially-initialized module. The order is enforced here for
readability, not by necessity (Python resolves the graph regardless),
but it makes the file usable as a dependency diagram.
"""

from __future__ import annotations

# --- Leaf: validator (structural validation) ------------------------------ #
from nexusdl.core.registry.validator import (
    SiteSchemaIssue,
    SitesValidator,
    load_schema,
)

# --- Leaf: protocols (injection contracts) ------------------------------- #
from nexusdl.core.registry.protocols import (
    HttpSession,
    HttpSessionProtocol,
    ParserFactory,
    ParserProtocol,
)

# --- Document layer: loader (read, parse, validate, iterate) ------------- #
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

# --- Runtime layer: site_registry (the main entry point) ----------------- #
from nexusdl.core.registry.site_registry import (
    DEFAULT_MAX_CONCURRENT_SEARCHES,
    SITES_FILENAME,
    SiteRegistry,
)

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
]
