"""Validate raw ``sites.yaml`` documents against ``data/sites_schema.json``.

This module runs **before** the loader constructs
:class:`~nexusdl.core.models.site.SiteConfig` instances. It performs the
*structural* validation — the JSON Schema contract — and produces error
messages that point at a YAML path (``sites[3].domains[1]``), so a bad
edit to ``sites.yaml`` fails with a message an editor can act on.

Two layers of validation, two different jobs
--------------------------------------------
NexusDL validates site configuration at **two** distinct points, and they
are complementary rather than redundant:

* **Structural** — this module, driven by ``data/sites_schema.json``, a
  Draft 2020-12 schema. It catches *shape* problems: a missing required
  key, a wrong type, a value outside an enum, an extra key where
  ``additionalProperties: false`` forbids it. It reports the precise YAML
  path, so ``sites[3].domains[1]`` in a 60-site file is a one-line fix.
* **Semantic** — :meth:`SiteConfig.__post_init__` in
  :mod:`nexusdl.core.models.site`. It re-checks the same constraints and
  is responsible for **invariants the schema cannot express**: a domain
  URL that is structurally ``https://...`` but not a valid host, an
  ``id`` that matches the pattern but duplicates another site, an
  ``excluded_formats`` frozenset populated with non-lowercase values. It
  also runs on *programmatically constructed* sites — a plugin that
  registers a site at runtime never touches this module, but still passes
  through ``__post_init__``.

That split is deliberate: this module's job is to reject a bad YAML
document early and cheaply, before any code touches the loader. It is
**not** a substitute for the semantic checks in :class:`SiteConfig`.

Library choice
--------------
The JSON Schema validator is :mod:`jsonschema` (Draft 2020-12), the
reference implementation for the standard. The library is a runtime
dependency of this module. If your ``pyproject.toml`` has
``jsonschema[format]`` only in the dev dependencies (as the PROMPT
MASTER's stack list suggests), move it to the runtime section — the
loader imports this module on every startup.

Schema source
-------------
The schema is loaded from ``nexusdl.data/sites_schema.json`` via
:mod:`importlib.resources`, so the validator works whether ``nexusdl``
is installed as a wheel, imported from a source tree, or run from a
``pip install -e`` checkout. A caller can override the source by
passing an explicit ``schema`` to :class:`SitesValidator` (used by tests
and by a future "custom schema for a private site registry" feature).

Schema caching
--------------
Parsing the JSON schema and compiling a validator is a few milliseconds
— cheap, but not free, and unnecessary to repeat on every ``sites.yaml``
load. :func:`load_schema` caches the parsed document at module scope,
guarded by a lock so that concurrent first calls (a hypothetical
multithreaded composition root) do not race. A caller who passes a
custom ``schema_path`` bypasses the cache — a custom schema is a
one-off, and caching it by file path would be a memory leak in a
long-running process that swaps schemas.

Cancellation
------------
The module is entirely synchronous: JSON parsing, schema validation, and
YAML reading are bounded operations that never wait on I/O in a way that
would benefit from ``asyncio.to_thread``. A validator called from a
coroutine completes in microseconds-to-low-milliseconds, well below the
threshold where threading would help.

Dependency graph (no cycles)::

    core.exceptions    ─┐
    jsonschema          ├──► core.registry.validator
    PyYAML              │
    loguru              │
    nexusdl.data        │
                        │
    No dependency on core.registry.* (site_registry, loader, protocols
    all import *this* module, not the reverse).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any, Final

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as _JsSchemaError
from jsonschema.exceptions import ValidationError as _JsValidationError
from loguru import logger

from nexusdl.core.exceptions import ConfigError


__all__ = [
    "SCHEMA_FILENAME",
    "SiteSchemaIssue",
    "SitesValidator",
    "load_schema",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the schema, relative to :data:`_SCHEMA_PACKAGE`.
SCHEMA_FILENAME: Final[str] = "sites_schema.json"

#: Python package that ships the schema as package data. Resolved through
#: :mod:`importlib.resources` so the same code works from a wheel, a source
#: tree, and an editable install.
_SCHEMA_PACKAGE: Final[str] = "nexusdl.data"

#: Marker used when a validation error's path refers to the root document
#: (e.g. "the top level is not a mapping").
_ROOT_PATH: Final[str] = "<root>"


_log = logger.bind(component="registry.validator")

#: Module-level cache for the parsed schema. Populated by
#: :func:`load_schema` on the first call and never invalidated — a schema
#: change requires a code change (rebuild a wheel) and therefore a process
#: restart, so cache invalidation is out of scope by design.
_schema_cache_lock: Final[threading.Lock] = threading.Lock()
_schema_cache: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# SiteSchemaIssue                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SiteSchemaIssue:
    """One violation of ``sites_schema.json`` in a ``sites.yaml`` document.

    Immutable and safe to cache or serialise. The CLI
    (``nexusdl sites validate``) renders the list of issues as a
    numbered report; a future IDE integration would map
    :attr:`yaml_path` back to a source line.

    Attributes:
        yaml_path: Location of the offending value in the parsed
            document, formatted as ``sites[3].domains[1]``. Equal to
            ``"<root>"`` when the error refers to the whole document
            (e.g. its type is not a mapping).
        message: The validation message from :mod:`jsonschema`, verbatim.
            Already human-readable: ``"64 is greater than the maximum
            of 32"``, ``"'ftp://x' does not match '^https://'"``.
        schema_path: Location in the schema of the failing constraint,
            formatted the same way as :attr:`yaml_path`. Useful for
            debugging the schema itself; usually ignored by end users.
        validator: The name of the failing JSON Schema keyword
            (``"required"``, ``"type"``, ``"pattern"``, ``"anyOf"``, …).
            Lets a caller branch on the kind of violation without
            parsing :attr:`message`.
    """

    yaml_path: str
    message: str
    schema_path: str
    validator: str

    def __str__(self) -> str:
        """Return the human-readable line ``<yaml_path>: <message>``.

        The default ``__repr__`` includes the schema path and validator
        name, which is noisy in a CLI. This ``__str__`` produces exactly
        the shape a report wants.
        """
        return f"{self.yaml_path}: {self.message}"


# --------------------------------------------------------------------------- #
# Schema loading                                                              #
# --------------------------------------------------------------------------- #


def load_schema(schema_path: Path | None = None) -> dict[str, Any]:
    """Return the parsed sites JSON Schema, cached when loaded from the bundle.

    The first call with ``schema_path=None`` loads the bundled schema,
    validates that it *is* a well-formed Draft 2020-12 schema, and caches
    the parsed dict at module scope. Subsequent calls return the cached
    object without touching disk.

    A call with an explicit ``schema_path`` reads and validates that file
    but does **not** populate or read the cache — the returned object is
    fresh on every call.

    Args:
        schema_path: Optional path to a JSON schema file. ``None`` loads
            the schema bundled inside :mod:`nexusdl.data`.

    Returns:
        The parsed schema as a plain ``dict``.

    Raises:
        ConfigError: If the schema cannot be read (missing file, permission
            error, missing package data), is not valid JSON, or is not a
            valid Draft 2020-12 JSON Schema.
    """
    global _schema_cache

    if schema_path is None:
        with _schema_cache_lock:
            if _schema_cache is not None:
                return _schema_cache

    raw_text = _read_schema_text(schema_path)

    try:
        schema = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in sites schema: {exc}"
        ) from exc

    # Validate the schema itself. A malformed schema would silently accept
    # or reject documents in ways nobody expects; failing loudly here turns
    # a subtle downstream bug into a startup error.
    try:
        Draft202012Validator.check_schema(schema)
    except _JsSchemaError as exc:
        raise ConfigError(
            f"invalid sites JSON Schema: {exc.message}"
        ) from exc

    if schema_path is None:
        with _schema_cache_lock:
            _schema_cache = schema

    _log.debug(
        "Loaded sites schema from {}",
        "bundle" if schema_path is None else str(schema_path),
    )
    return schema


def _read_schema_text(schema_path: Path | None) -> str:
    """Read the schema text from disk or from the bundled package data.

    Args:
        schema_path: Explicit path, or ``None`` for the bundled schema.

    Returns:
        The schema file's contents as a UTF-8 string.

    Raises:
        ConfigError: If the file cannot be read, or if the bundled
            package data cannot be located.
    """
    if schema_path is not None:
        try:
            return schema_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"cannot read sites schema {schema_path}: {exc}"
            ) from exc

    try:
        resource = _resource_files(_SCHEMA_PACKAGE) / SCHEMA_FILENAME
        return resource.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError) as exc:
        raise ConfigError(
            f"bundled sites schema {SCHEMA_FILENAME!r} not found in "
            f"package {_SCHEMA_PACKAGE!r}; is the package data missing "
            f"from the installation? ({type(exc).__name__}: {exc})"
        ) from exc


# --------------------------------------------------------------------------- #
# SitesValidator                                                              #
# --------------------------------------------------------------------------- #


class SitesValidator:
    """Validate raw ``sites.yaml`` documents against the sites schema.

    The class is stateless after construction: instances are safe to
    share across coroutines and threads, and repeated
    :meth:`validate` / :meth:`iter_issues` calls do not accumulate state.

    Example:
        >>> validator = SitesValidator()
        >>> raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        >>> validator.validate(raw)   # raises ConfigError on failure
        >>> # Or collect issues without raising:
        >>> for issue in validator.iter_issues(raw):
        ...     print(issue)

    Args:
        schema: Optional schema to use instead of
            :func:`load_schema`'s default. Useful for tests (a minimal
            inline schema) and for a future feature that loads a
            user-provided schema. When ``None``, the default bundled
            schema is used and shared with every other ``SitesValidator``
            instance in the process.
    """

    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        """Compile a validator against the schema.

        Args:
            schema: Optional schema; see the class docstring. When
                ``None``, the bundled schema is loaded (and cached)
                via :func:`load_schema`.
        """
        self._schema: dict[str, Any] = (
            schema if schema is not None else load_schema()
        )
        self._validator = Draft202012Validator(
            self._schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def validate(self, document: Mapping[str, Any]) -> None:
        """Validate *document*, raising :class:`ConfigError` on the first batch.

        All issues are collected before raising, so the resulting message
        lists every problem in one pass — an editor fixing a large
        ``sites.yaml`` does not play whack-a-mole with the validator.
        Callers that want to inspect the issues programmatically should
        use :meth:`iter_issues` instead.

        Args:
            document: The parsed ``sites.yaml`` document, as returned by
                :func:`yaml.safe_load`. Typically a ``dict``.

        Raises:
            ConfigError: If the document fails schema validation. The
                message enumerates every issue, one per line, prefixed
                by the YAML path.
        """
        issues = list(self.iter_issues(document))
        if not issues:
            return

        header = (
            f"sites document failed schema validation: "
            f"{len(issues)} issue(s)"
        )
        body = "\n".join(f"  - {issue}" for issue in issues)
        raise ConfigError(f"{header}\n{body}")

    def iter_issues(
        self, document: Mapping[str, Any]
    ) -> Iterator[SiteSchemaIssue]:
        """Yield every issue without raising.

        Issues are ordered by their YAML path so the output is
        deterministic — two runs against the same document produce the
        same sequence, which matters for CI snapshots and for
        diagnosing a regression against a known-good baseline.

        Args:
            document: The parsed ``sites.yaml`` document.

        Yields:
            A :class:`SiteSchemaIssue` for each violation. Never raises
            for a document that fails validation; a document that is
            not a mapping yields exactly one issue and returns.
        """
        if not isinstance(document, Mapping):
            yield SiteSchemaIssue(
                yaml_path=_ROOT_PATH,
                message=(
                    f"document must be a mapping, got "
                    f"{type(document).__name__}"
                ),
                schema_path=_ROOT_PATH,
                validator="type",
            )
            return

        # ``iter_errors`` yields ``ValidationError`` objects; sorting them
        # by their absolute YAML path gives the caller a stable sequence
        # even when ``jsonschema`` changes its internal traversal order.
        # ``anyOf`` and ``oneOf`` errors carry ``context`` sub-errors — we
        # surface the parent only, because the sub-errors duplicate the
        # same information with deeper ``schema_path``s and produce a lot
        # of noise without adding value at the CLI level. A power user can
        # inspect ``ValidationError.context`` directly by calling
        # ``self._validator.iter_errors`` — that is deliberately not
        # exposed here to keep the public surface small.
        errors = sorted(
            self._validator.iter_errors(document),
            key=_error_sort_key,
        )
        for error in errors:
            yield self._to_issue(error)

    def validate_file(self, path: Path) -> Mapping[str, Any]:
        """Load a YAML file and validate it, returning the parsed document.

        Convenience method for CLI and for tests. The parsed document is
        returned so a caller that wants to inspect it after validation
        does not need to load it twice.

        Args:
            path: Path to a ``sites.yaml`` file. Must exist and be a
                regular file.

        Returns:
            The parsed document, as produced by :func:`yaml.safe_load`.

        Raises:
            ConfigError: If *path* does not exist, cannot be read, is not
                valid YAML, does not parse to a mapping, or fails schema
                validation. Schema-violation errors are prefixed with
                the source path so the caller can see which file failed.
        """
        if not path.is_file():
            raise ConfigError(f"sites file not found: {path}")

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"cannot read sites file {path}: {exc}"
            ) from exc

        try:
            document = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"invalid YAML in {path}: {exc}"
            ) from exc

        if document is None:
            raise ConfigError(
                f"sites file is empty: {path}"
            )
        if not isinstance(document, Mapping):
            raise ConfigError(
                f"sites file must contain a mapping, got "
                f"{type(document).__name__}: {path}"
            )

        try:
            self.validate(document)
        except ConfigError as exc:
            raise ConfigError(
                f"invalid sites file {path}:\n{exc}"
            ) from exc

        return document

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_issue(error: _JsValidationError) -> SiteSchemaIssue:
        """Translate a jsonschema ``ValidationError`` to a :class:`SiteSchemaIssue`.

        Args:
            error: The exception produced by ``iter_errors``.

        Returns:
            The corresponding :class:`SiteSchemaIssue`.
        """
        return SiteSchemaIssue(
            yaml_path=_format_path(error.absolute_path),
            message=error.message,
            schema_path=_format_path(error.absolute_schema_path),
            validator=str(error.validator) if error.validator else "unknown",
        )


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _error_sort_key(
    error: _JsValidationError,
) -> tuple[tuple[tuple[int, Any], ...], str]:
    """Return a sort key that orders errors by YAML path, then message.

    A JSON path is a sequence of ``str`` (dict keys) and ``int`` (list
    indices). Mixed-type comparison is not defined in Python, so we map
    each segment to ``(kind_rank, value)`` — ``0`` for ints, ``1`` for
    strings — giving a total order across mixed paths.

    Args:
        error: The validation error to key.

    Returns:
        A tuple suitable as a ``sorted(..., key=...)`` key.
    """
    segments: list[tuple[int, Any]] = []
    for segment in error.absolute_path:
        if isinstance(segment, int):
            segments.append((0, segment))
        else:
            segments.append((1, str(segment)))
    return (tuple(segments), error.message)


def _format_path(path: Any) -> str:
    """Format a jsonschema path (a deque of keys) as a human-readable string.

    Examples::

        [sites, 3, domains, 1]  →  "sites[3].domains[1]"
        [sites, 0]              →  "sites[0]"
        []                      →  "<root>"

    Args:
        path: The path to format. Any iterable of ``str`` / ``int``
            segments — the deques exposed by jsonschema, or a manually
            built list.

    Returns:
        The formatted path, or :data:`_ROOT_PATH` when *path* is empty.
    """
    parts: list[str] = []
    first = True
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            if not first:
                parts.append(".")
            parts.append(str(segment))
        first = False
    return "".join(parts) if parts else _ROOT_PATH
