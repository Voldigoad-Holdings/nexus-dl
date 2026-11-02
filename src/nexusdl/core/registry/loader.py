"""Canonical, reusable loader for the ``sites.yaml`` configuration.

This module owns the **document-level** half of the sites pipeline: locate
the file, read it, parse it, structurally validate it against
``sites_schema.json``, and expose the raw validated document to callers.
It does **not** build :class:`~nexusdl.core.models.site.SiteConfig`
instances — that is the registry's job, and
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` already has its
own internal implementation.

Two audiences
-------------
* **Tools** — ``scripts/validate_sites.py``, the CLI's
  ``nexusdl sites validate`` / ``nexusdl sites info`` subcommands, CI
  checks, and any test that wants to inspect ``sites.yaml`` without
  standing up a full registry. These use :func:`load_and_validate_sites`
  and the granular ``iter_*`` helpers, plus :func:`check_parser_classes`
  for CI validation.
* **A future refactor of the registry** — when
  :class:`~nexusdl.core.registry.site_registry.SiteRegistry` is next
  opened, its private ``_read_and_validate_yaml`` becomes a one-line call
  to :func:`load_and_validate_sites`, and its ``_build_aliases`` /
  ``_build_categories`` / ``_build_disabled`` become 5-line wrappers
  around the corresponding ``iter_*`` helpers. This module is written so
  that refactor is a straight substitution.

Relationship to ``site_registry.py``
------------------------------------
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` shipped first
with its own internal read/parse/validate pipeline. That is deliberate,
documented duplication — the same pattern used elsewhere in this codebase
(``_fsync_and_replace`` in the packagers). It exists because:

* The registry needed the pipeline before this module did.
* Reopening an already-delivered file to refactor it into a shared helper
  is worse than the duplication itself: the risk of a regression in a
  working file outweighs the readability gain, and the two
  implementations are cross-checked by unit tests.

The refactor is scheduled for the **next time ``site_registry.py`` is
opened for any reason**. It will reduce ``_read_and_validate_yaml`` to::

    def _read_and_validate_yaml(self):
        return load_and_validate_sites(self._sites_path, self._validator)

and each ``_build_*`` helper to a thin wrapper around ``iter_*``. Until
then, the two implementations must be kept in lock-step — see the
``tests/unit/core/registry/test_loader_registry_parity.py`` contract
(future).

What this module adds beyond read+parse
---------------------------------------
Two things the registry does not provide:

* **Granular iterators** — :func:`iter_sites`, :func:`iter_aliases`,
  :func:`iter_categories`, :func:`iter_disabled` yield the raw YAML
  entries without building :class:`SiteConfig` instances. A CLI that
  dumps or edits the raw document does not need model construction.
* **Parser-class resolution checking** — :func:`check_parser_classes`
  verifies that every ``parser_class: "module:ClassName"`` field in the
  document actually resolves to a class. This is CI gold: a broken
  ``sites.yaml`` produced by a bad PR is caught before it reaches users.
  The resolver is a callback so this module never imports the parser
  package (rule R1).

Pure vs I/O
-----------
The public API is split by whether it touches the filesystem:

* **Pure** — :func:`parse_sites_document`, :func:`validate_sites_document`,
  every ``iter_*``, :func:`check_parser_classes`. Every input is passed
  in; deterministic; trivially testable.
* **I/O** — :func:`read_sites_yaml`, :func:`load_and_validate_sites`.
  These touch disk (or the bundled package data) exactly once per call.

Callers that already have the parsed document use the pure functions;
callers that want the whole pipeline use the I/O functions.

Dependency graph (no cycles)::

    core.exceptions          ─┐
    core.registry.validator   ├──► core.registry.loader
    PyYAML                    │
    loguru                    │
                              │
    No dependency on core.registry.site_registry — the coupling runs
    the other way once the refactor lands. No runtime dependency on
    nexusdl.parsers — parser class resolution is done via callback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any, Final

import yaml
from loguru import logger

from nexusdl.core.exceptions import ConfigError
from nexusdl.core.registry.validator import SitesValidator


__all__ = [
    "DATA_PACKAGE",
    "SITES_FILENAME",
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
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the sites bundle inside :data:`DATA_PACKAGE`.
SITES_FILENAME: Final[str] = "sites.yaml"

#: Python package that ships the default ``sites.yaml``.
DATA_PACKAGE: Final[str] = "nexusdl.data"

#: Top-level YAML keys this module reads. Kept as ``Final[str]`` constants
#: so the key strings appear once and a future schema rename is a
#: single-place edit.
_KEY_SITES: Final[str] = "sites"
_KEY_ALIASES: Final[str] = "aliases"
_KEY_CATEGORIES: Final[str] = "categories"
_KEY_DISABLED_SITES: Final[str] = "disabled_sites"


_log = logger.bind(component="registry.loader")


# --------------------------------------------------------------------------- #
# ParserClassIssue                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ParserClassIssue:
    """One ``parser_class`` entry that does not resolve to a class.

    Produced by :func:`check_parser_classes`. Immutable and safe to
    serialise — a CI script or CLI can render the list verbatim.

    Attributes:
        index: Position of the site in the ``sites`` list (0-based).
            Matches what an editor would see when counting from the top
            of the YAML array.
        parser_class: The raw ``"module:ClassName"`` string from the
            document, verbatim. Quoted so a typo is obvious in the
            report.
        reason: Human-readable reason the class could not be resolved,
            as returned by the caller's resolver callback. Never empty —
            a failed resolution always has a cause.
    """

    index: int
    parser_class: str
    reason: str

    def __str__(self) -> str:
        """Return ``sites[<index>].parser_class (<class>) <reason>``."""
        return (
            f"sites[{self.index}].parser_class "
            f"({self.parser_class!r}): {self.reason}"
        )


# --------------------------------------------------------------------------- #
# I/O layer                                                                   #
# --------------------------------------------------------------------------- #


def read_sites_yaml(path: Path | None = None) -> str:
    """Read the raw text of ``sites.yaml``.

    Args:
        path: Explicit path to the file, or ``None`` for the bundled
            ``sites.yaml`` inside :data:`DATA_PACKAGE`.

    Returns:
        The file's UTF-8 contents.

    Raises:
        ConfigError: If the file cannot be located (explicit path does
            not exist, or the bundle is missing from the installation)
            or cannot be read.
    """
    if path is None:
        try:
            resource = _resource_files(DATA_PACKAGE) / SITES_FILENAME
            return resource.read_text(encoding="utf-8")
        except (ModuleNotFoundError, OSError) as exc:
            raise ConfigError(
                f"bundled sites configuration {SITES_FILENAME!r} is "
                f"unreadable from package {DATA_PACKAGE!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read sites configuration {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def load_and_validate_sites(
    path: Path | None = None,
    validator: SitesValidator | None = None,
) -> Mapping[str, Any]:
    """Read, parse, and structurally validate ``sites.yaml``.

    The canonical entry point for tools that want the **raw** validated
    document — a CLI ``nexusdl sites validate``,
    ``scripts/validate_sites.py``, or a test inspecting the shipped
    configuration.

    Semantic per-site validation (the :class:`SiteConfig.__post_init__`
    checks) is **not** performed here — this function only enforces the
    ``sites_schema.json`` contract. A caller that wants fully-built
    configs uses
    :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.initialize`
    (or the registry's internal builder) instead.

    Args:
        path: Explicit path to a ``sites.yaml`` file, or ``None`` for
            the bundled file.
        validator: Optional :class:`~nexusdl.core.registry.validator.SitesValidator`.
            A fresh default one is constructed when ``None``. Injecting
            a validator lets tests exercise the pipeline against an
            inline minimal schema without depending on the full
            production ``sites_schema.json``.

    Returns:
        The parsed and structurally validated document.

    Raises:
        ConfigError: If the file cannot be read, the YAML is malformed,
            or schema validation fails. Schema-violation messages
            enumerate every issue, one per line, prefixed by its YAML
            path.
    """
    text = read_sites_yaml(path)
    document = parse_sites_document(text)
    validate_sites_document(document, validator)
    return document


# --------------------------------------------------------------------------- #
# Pure layer — parsing and validation                                         #
# --------------------------------------------------------------------------- #


def parse_sites_document(text: str) -> Mapping[str, Any]:
    """Parse YAML text into a mapping, without schema validation.

    Exposed as a public function so a caller who already has the raw
    text in memory — a test, a future ``--file -`` CLI mode that reads
    stdin, an editor integration — can skip the filesystem round-trip
    that :func:`read_sites_yaml` performs.

    Args:
        text: Raw YAML text.

    Returns:
        The parsed document. Guaranteed to be a mapping at the top
        level.

    Raises:
        ConfigError: If the YAML is syntactically invalid, or its
            top-level value is not a mapping (a list, a string, or
            ``None`` — none of which a sites document may be).
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"invalid YAML in sites configuration: {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise ConfigError(
            "sites configuration must be a mapping at the top level, "
            f"got {type(document).__name__}"
        )
    return document


def validate_sites_document(
    document: Mapping[str, Any],
    validator: SitesValidator | None = None,
) -> None:
    """Structurally validate a parsed sites document.

    A thin delegation to :meth:`SitesValidator.validate` so callers who
    already have a parsed document do not need to reach into
    :mod:`nexusdl.core.registry.validator` directly.

    Args:
        document: The parsed document to validate.
        validator: Optional validator instance. A fresh default one is
            constructed when ``None``.

    Raises:
        ConfigError: If the document fails schema validation.
    """
    (validator if validator is not None else SitesValidator()).validate(
        document
    )


# --------------------------------------------------------------------------- #
# Pure layer — granular iterators                                             #
# --------------------------------------------------------------------------- #


def iter_sites(
    document: Mapping[str, Any],
) -> Iterator[tuple[int, Mapping[str, Any]]]:
    """Yield ``(index, raw_site)`` for every entry in the sites list.

    Does **not** build :class:`~nexusdl.core.models.site.SiteConfig`
    instances — the caller inspects the raw YAML dict. This is what
    editor-facing tools (``nexusdl sites info``, a JSON dump of
    ``sites.yaml``) and :func:`check_parser_classes` need.

    A ``sites`` key that is absent from *document* yields nothing; an
    empty sites list yields nothing. Neither is an error — a sites file
    with no sites is a valid (if useless) document.

    Args:
        document: A structurally validated sites document.

    Yields:
        A ``(index, raw_site_mapping)`` tuple per site, in document
        order.

    Raises:
        ConfigError: If the ``sites`` key is present but not a list, or
            if any entry in the list is not a mapping. Defensive — both
            conditions are already rejected by
            :func:`validate_sites_document`, but making the iterator
            self-contained lets a caller use it without prior
            validation.
    """
    raw_sites = document.get(_KEY_SITES, [])
    if not isinstance(raw_sites, list):
        raise ConfigError(
            f"{_KEY_SITES!r} must be a list, got "
            f"{type(raw_sites).__name__}"
        )
    for index, raw_site in enumerate(raw_sites):
        if not isinstance(raw_site, Mapping):
            raise ConfigError(
                f"{_KEY_SITES}[{index}] must be a mapping, got "
                f"{type(raw_site).__name__}"
            )
        yield index, raw_site


def iter_aliases(
    document: Mapping[str, Any],
) -> Iterator[tuple[str, str]]:
    """Yield ``(alias, target)`` for every entry in the aliases map.

    An absent or empty ``aliases`` section yields nothing.

    Args:
        document: A structurally validated sites document.

    Yields:
        Each ``(alias, canonical_site_id)`` pair, in document order.

    Raises:
        ConfigError: If the section is not a mapping, or an entry is not
            a string-to-string pair.
    """
    raw = document.get(_KEY_ALIASES) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"{_KEY_ALIASES!r} must be a mapping, got "
            f"{type(raw).__name__}"
        )
    for alias, target in raw.items():
        if not isinstance(alias, str) or not isinstance(target, str):
            raise ConfigError(
                f"{_KEY_ALIASES} entries must map string to string, "
                f"got {alias!r} -> {type(target).__name__}"
            )
        yield alias, target


def iter_categories(
    document: Mapping[str, Any],
) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(category, site_ids)`` for every entry in the categories map.

    The ``site_ids`` list is a **new** list — a caller can sort or filter
    it without mutating the parsed document.

    Args:
        document: A structurally validated sites document.

    Yields:
        Each ``(category_name, site_ids)`` pair, in document order.

    Raises:
        ConfigError: If the section is not a mapping, a category name is
            not a string, or a category value is not a list of strings.
    """
    raw = document.get(_KEY_CATEGORIES) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"{_KEY_CATEGORIES!r} must be a mapping, got "
            f"{type(raw).__name__}"
        )
    for category, site_ids in raw.items():
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
            members.append(raw_id)
        yield category, members


def iter_disabled(
    document: Mapping[str, Any],
) -> Iterator[str]:
    """Yield every entry in the ``disabled_sites`` section, in order.

    Does **not** filter entries that name an unknown site — a tool that
    wants to warn about stale entries does so by comparing against
    :func:`iter_sites`. This separation lets the caller choose between
    "tolerant" (ignore unknown ids) and "strict" (warn), matching the
    policy difference between the registry (tolerant) and a CI check
    (strict).

    An absent or empty ``disabled_sites`` section yields nothing.

    Args:
        document: A structurally validated sites document.

    Yields:
        Each disabled site id, in document order.

    Raises:
        ConfigError: If the section is not a list, or contains a
            non-string entry.
    """
    raw = document.get(_KEY_DISABLED_SITES) or ()
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            f"{_KEY_DISABLED_SITES!r} must be a list, got "
            f"{type(raw).__name__}"
        )
    for raw_id in raw:
        if not isinstance(raw_id, str):
            raise ConfigError(
                f"{_KEY_DISABLED_SITES} entries must be strings, got "
                f"{type(raw_id).__name__}"
            )
        yield raw_id


# --------------------------------------------------------------------------- #
# Parser class resolution check                                               #
# --------------------------------------------------------------------------- #


def check_parser_classes(
    document: Mapping[str, Any],
    resolver: Callable[[str], str | None],
) -> list[ParserClassIssue]:
    """Verify that every ``parser_class`` resolves to an importable class.

    Iterates the ``sites`` list and calls *resolver* on each
    ``parser_class`` string. A resolver that returns ``None`` means the
    class resolved successfully; any other return value is treated as a
    human-readable failure reason.

    The resolver is a **callback**, not a direct import, so this module
    never imports the parser package (rule R1). Callers choose the
    resolution mechanism:

    * For CI validation — a simple ``importlib.import_module`` wrapper::

          def _resolve(parser_class: str) -> str | None:
              try:
                  module_path, class_name = parser_class.split(":", 1)
                  module = importlib.import_module(module_path)
                  getattr(module, class_name)
              except (ImportError, AttributeError, ValueError) as exc:
                  return f"{type(exc).__name__}: {exc}"
              return None

    * For runtime validation against a registered parser factory — a
      closure over :meth:`ParserFactory.get_registered_parser` that
      catches its own exception and returns the message.

    * For tests — a resolver that returns ``None`` for known-good sites
      and a canned message for injected failures.

    A site whose ``parser_class`` key is absent, non-string, or malformed
    is reported as an issue too — those are structural errors a well-
    formed ``sites_schema.json`` would have caught first, but this
    function is safe to call on any mapping.

    Args:
        document: A parsed sites document. Structural validation is
            **not** required — this function tolerates any mapping shape
            and reports issues rather than raising.
        resolver: Callable that takes a ``"module:ClassName"`` string
            and returns ``None`` on success, or a reason string on
            failure.

    Returns:
        A list of :class:`ParserClassIssue`, in document order. Empty
        when every parser class resolves.
    """
    issues: list[ParserClassIssue] = []
    for index, raw_site in iter_sites(document):
        raw_class = raw_site.get("parser_class")
        if not isinstance(raw_class, str):
            issues.append(
                ParserClassIssue(
                    index=index,
                    parser_class=str(raw_class),
                    reason=(
                        f"parser_class must be a string, got "
                        f"{type(raw_class).__name__}"
                    ),
                )
            )
            continue
        try:
            reason = resolver(raw_class)
        except Exception as exc:  # noqa: BLE001 — resolver contract is "never raise"
            # A resolver is contractually expected to catch its own
            # exceptions and return a reason. If it does not, we still
            # produce a usable issue rather than propagating a resolver
            # bug up the stack.
            reason = (
                f"resolver raised {type(exc).__name__}: {exc}"
            )
        if reason is not None:
            issues.append(
                ParserClassIssue(
                    index=index,
                    parser_class=raw_class,
                    reason=reason,
                )
            )
    return issues
