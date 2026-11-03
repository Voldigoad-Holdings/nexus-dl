"""Parser capabilities — canonical set, declaration, and inference.

Every site in ``data/sites.yaml`` declares a ``capabilities`` object with nine
boolean fields.  That object lives on the site's :class:`SiteConfig` and is
validated against ``data/sites_schema.json`` at load time by
:class:`~nexusdl.core.registry.SiteRegistry`.

This module provides the **parser-side** view of the same nine capabilities:

* :class:`ParserCapability` — the canonical enum of the nine capability
  names, matching ``sites.yaml`` exactly.
* :data:`CAPABILITY_NAMES` — the canonical tuple of string values, useful
  for schema validation, CLI help output, and tests.
* :class:`ParserCapabilities` — a frozen dataclass holding the nine booleans,
  with helpers for round-tripping to/from the ``sites.yaml`` shape.
* :func:`infer_capabilities` — best-effort structural inference of which
  capabilities a parser class supports, based on its MRO.  Only the five
  capabilities that are inferable from class structure are returned;
  the other four depend on the parser's implementation choices and cannot
  be inferred.
* :func:`validate_capabilities` — consistency check between a site's
  declared capabilities and what the parser class structure actually
  supports.  Returns a list of human-readable warning strings (never
  raises); the registry validator decides whether to log them or fail.

Why a separate model from ``SiteConfig.capabilities``
-----------------------------------------------------
:class:`SiteConfig` is a **Pydantic** model loaded from YAML, designed to
validate untrusted input.  It belongs to ``core/models/site.py`` and is
imported by library code, the registry, the CLI and the web backend.
Bringing that validation machinery into the parser package would mean
every parser import transitively pulls in Pydantic's YAML loader, which is
both slow and architecturally wrong (``core/`` must not depend on
``parsers/``, but the reverse should also stay minimal).

:class:`ParserCapabilities` is a **plain frozen dataclass** — no Pydantic,
no YAML, no validation framework.  It is the smallest possible representation
of the same nine booleans, cheap to construct and compare, and suitable for
the parser layer's internal use (validation, introspection, CLI display).

The two models are structurally compatible: a ``SiteConfig.capabilities``
object can be turned into a :class:`ParserCapabilities` via
:meth:`ParserCapabilities.from_dict` on ``site.capabilities.model_dump()``,
and vice versa.  The parser layer never imports the Pydantic model.

Why only five capabilities are inferable
----------------------------------------
Of the nine capabilities, five are determined by the parser class structure
alone:

* ``search``, ``chapter_list``, ``reader_images`` — always true, because
  the corresponding methods are **abstract** on :class:`BaseParser`.  Every
  parser must implement them; a parser that cannot must raise
  :class:`NotImplementedError` at runtime, which is an explicit declaration
  that the site does not support the capability, not a structural gap.

* ``cloudflare``, ``js_rendered`` — inferable from the MRO:
  ``CloudflareMixin in parser_cls.__mro__`` and
  ``JsRenderedMixin in parser_cls.__mro__``.

The other four cannot be inferred structurally:

* ``pagination`` — depends on whether the parser's ``search`` yields more
  than one page (some parsers intentionally cap results at one page).
* ``filters`` — depends on whether the parser's ``search`` accepts filter
  parameters (the signature is uniform, but support varies).
* ``metadata`` — depends on whether the parser populates the optional
  fields of :class:`Manga` (authors, description, publication year, etc.).
* ``cover_art`` — depends on whether the parser extracts a cover URL from
  the search results (some parsers only populate ``cover_url`` in
  ``get_manga``, not in ``search``).

For these four, the parser's site declaration is authoritative.  The
validator only checks the five inferable capabilities; the other four are
taken as declared without cross-check.

Usage in the registry validator
-------------------------------
:mod:`nexusdl.core.registry.validator` calls
:func:`validate_capabilities` once per site after the parser class has
been resolved::

    warnings = validate_capabilities(
        site.capabilities.model_dump(),
        parser_cls,
    )
    for warning in warnings:
        logger.warning(warning)

Non-empty return value does **not** fail the application.  It signals a
mismatch between the site declaration and the parser implementation that
the operator should investigate — but the application stays usable, which
is consistent with the project's philosophy of not blocking startup on
recoverable configuration drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Mapping

from nexusdl.core.exceptions import NexusDLError

if TYPE_CHECKING:
    from nexusdl.parsers.base import BaseParser


__all__ = [
    "ParserCapability",
    "CAPABILITY_NAMES",
    "ParserCapabilities",
    "infer_capabilities",
    "validate_capabilities",
]


# ---------------------------------------------------------------------------
# Canonical capability enum
# ---------------------------------------------------------------------------
class ParserCapability(str, Enum):
    """The canonical, exhaustive set of parser capabilities.

    The nine values mirror the nine boolean fields declared per site in
    ``data/sites.yaml`` under the ``capabilities`` key.  Adding a new
    capability is a two-step process:

    1. Add the value to this enum.
    2. Add the corresponding field to ``data/sites_schema.json`` and to
       every site entry in ``data/sites.yaml``.

    Both steps are required — :class:`ParserCapabilities.from_dict` rejects
    unknown or missing keys so that a partial rollout is caught immediately
    at configuration load time rather than at runtime.
    """

    SEARCH = "search"
    """The parser implements the four :class:`BaseParser` operations."""

    PAGINATION = "pagination"
    """The parser's ``search`` yields paginated results."""

    FILTERS = "filters"
    """The parser's ``search`` accepts filter parameters (language, adult, etc.)."""

    METADATA = "metadata"
    """The parser returns rich metadata (authors, description, publication info)."""

    COVER_ART = "cover_art"
    """The parser returns cover image URLs (at least from ``get_manga``)."""

    CHAPTER_LIST = "chapter_list"
    """The parser enumerates the complete chapter list for a manga."""

    READER_IMAGES = "reader_images"
    """The parser enumerates the complete reader image list for a chapter."""

    CLOUDFLARE = "cloudflare"
    """The parser requires Cloudflare challenge handling."""

    JS_RENDERED = "js_rendered"
    """The parser requires JavaScript rendering to obtain page content."""


#: The canonical tuple of capability names, in declaration order.
#:
#: Useful for schema validation, CLI help output, and tests.  The order is
#: stable and matches the field order of :class:`ParserCapabilities` so that
#: ``to_dict()`` output is deterministic.
CAPABILITY_NAMES: tuple[str, ...] = tuple(cap.value for cap in ParserCapability)


#: Capabilities that can be inferred from a parser class's structure.
#:
#: Everything else must be declared by the site in ``sites.yaml`` and is
#: taken as authoritative without structural cross-check.  See the module
#: docstring for the rationale.
_STRUCTURALLY_INFERABLE: frozenset[ParserCapability] = frozenset(
    {
        ParserCapability.SEARCH,
        ParserCapability.CHAPTER_LIST,
        ParserCapability.READER_IMAGES,
        ParserCapability.CLOUDFLARE,
        ParserCapability.JS_RENDERED,
    }
)


# ---------------------------------------------------------------------------
# Frozen capability set
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ParserCapabilities:
    """Immutable set of declared capabilities for a site.

    This is the parser-side view of the nine ``sites.yaml`` capability
    fields.  It is deliberately a plain frozen dataclass — no Pydantic, no
    YAML, no validation framework — so that constructing one is essentially
    free and can happen on every site lookup if needed.

    Defaults
    --------
    The defaults reflect what an **ideal, fully-featured parser** supports:
    the four universal capabilities (``search``, ``chapter_list``,
    ``reader_images``, and their implicit counterpart ``download``) are
    enabled, as are the four "content-richness" capabilities
    (``pagination``, ``filters``, ``metadata``, ``cover_art``).  Only
    ``cloudflare`` and ``js_rendered`` default to ``False``, because those
    are properties of specific sites, not of well-behaved parsers in
    general.

    Concrete parsers never construct :class:`ParserCapabilities` from its
    defaults — they use :meth:`from_dict` on the site's declared
    capabilities, or :meth:`from_iterable` for ad-hoc tests.
    """

    search: bool = True
    pagination: bool = True
    filters: bool = True
    metadata: bool = True
    cover_art: bool = True
    chapter_list: bool = True
    reader_images: bool = True
    cloudflare: bool = False
    js_rendered: bool = False

    # ------------------------------------------------------------------ #
    #  Query helpers                                                     #
    # ------------------------------------------------------------------ #
    def supports(self, capability: ParserCapability | str) -> bool:
        """Return ``True`` when *capability* is enabled.

        Args:
            capability: Either a :class:`ParserCapability` member or its
                string value (``"search"``, ``"cloudflare"``, etc.).

        Returns:
            ``True`` if the capability is enabled, ``False`` otherwise —
            including when *capability* is an unrecognised string, so that
            callers can safely pass arbitrary values from untrusted input.
        """
        if isinstance(capability, str) and not isinstance(
            capability, ParserCapability
        ):
            try:
                capability = ParserCapability(capability)
            except ValueError:
                return False
        return bool(getattr(self, capability.value))

    def enabled(self) -> frozenset[ParserCapability]:
        """Return the set of enabled capabilities."""
        return frozenset(
            cap for cap in ParserCapability if getattr(self, cap.value)
        )

    def disabled(self) -> frozenset[ParserCapability]:
        """Return the set of disabled capabilities."""
        return frozenset(
            cap for cap in ParserCapability if not getattr(self, cap.value)
        )

    # ------------------------------------------------------------------ #
    #  Serialisation                                                     #
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, bool]:
        """Return the capabilities as a plain ``dict[str, bool]``.

        The returned dict has exactly the nine keys of
        :data:`CAPABILITY_NAMES`, in declaration order.  It is safe to feed
        directly back into :meth:`from_dict` (round-trip identity) and to
        compare against ``site.capabilities.model_dump()``.
        """
        return {cap.value: getattr(self, cap.value) for cap in ParserCapability}

    @classmethod
    def from_dict(cls, data: Mapping[str, bool]) -> "ParserCapabilities":
        """Build a :class:`ParserCapabilities` from a ``{name: bool}`` mapping.

        Used to convert ``SiteConfig.capabilities.model_dump()`` into the
        parser-side representation.

        Args:
            data: A mapping whose keys are exactly the nine capability
                names and whose values are booleans.  Truthy / falsy
                non-boolean values are coerced via ``bool(...)``.

        Returns:
            A populated :class:`ParserCapabilities`.

        Raises:
            NexusDLError: If *data* is missing any of the nine keys or
                contains an unknown key.  Strict mode is intentional: a
                missing or extraneous key in ``sites.yaml`` is always a
                configuration bug that should be fixed, not tolerated.
        """
        provided = set(data.keys())
        expected = set(CAPABILITY_NAMES)

        unknown = provided - expected
        if unknown:
            raise NexusDLError(
                f"Unknown capability key(s) {sorted(unknown)!r}: the "
                f"canonical set is {sorted(expected)!r}."
            )

        missing = expected - provided
        if missing:
            raise NexusDLError(
                f"Missing capability key(s) {sorted(missing)!r}: the "
                f"canonical set is {sorted(expected)!r}."
            )

        return cls(**{name: bool(data[name]) for name in CAPABILITY_NAMES})

    @classmethod
    def from_iterable(
        cls,
        capabilities: Iterable[ParserCapability | str],
    ) -> "ParserCapabilities":
        """Build a :class:`ParserCapabilities` from a set of enabled names.

        Every capability **not** present in *capabilities* is disabled.
        This is a convenience constructor for tests, CLI flag parsing and
        programmatic construction, where listing the enabled capabilities
        is more natural than listing all nine with their booleans.

        Args:
            capabilities: Iterable of :class:`ParserCapability` members
                and/or their string values.  Unknown strings raise
                :class:`ValueError` through the enum constructor, which is
                the correct behaviour — a typo in a test is a bug, not an
                input to tolerate.

        Returns:
            A populated :class:`ParserCapabilities`.
        """
        enabled_set: set[ParserCapability] = set()
        for cap in capabilities:
            enabled_set.add(
                cap if isinstance(cap, ParserCapability) else ParserCapability(cap)
            )
        return cls(
            **{name: ParserCapability(name) in enabled_set for name in CAPABILITY_NAMES}
        )

    # ------------------------------------------------------------------ #
    #  Named constants                                                   #
    # ------------------------------------------------------------------ #
    @classmethod
    def all_enabled(cls) -> "ParserCapabilities":
        """Return a :class:`ParserCapabilities` with every capability enabled.

        Useful as the "ideal parser" baseline in tests and validator
        defaults.  In practice no shipped parser enables all nine — every
        parser is either Cloudflare-protected or JS-rendered or neither,
        and no parser supports every optional feature.
        """
        return cls(**{name: True for name in CAPABILITY_NAMES})

    @classmethod
    def none_enabled(cls) -> "ParserCapabilities":
        """Return a :class:`ParserCapabilities` with every capability disabled.

        Useful for negative tests and as a documented "empty" baseline.
        """
        return cls(**{name: False for name in CAPABILITY_NAMES})


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
def infer_capabilities(
    parser_cls: type[BaseParser],
) -> frozenset[ParserCapability]:
    """Return the capabilities that can be inferred from *parser_cls*.

    Only the five structurally inferable capabilities are returned (see
    the module docstring for the rationale):

    * ``search``, ``chapter_list``, ``reader_images`` — always present,
      because the corresponding methods are abstract on :class:`BaseParser`.
    * ``cloudflare`` — present iff :class:`CloudflareMixin` is in the MRO.
    * ``js_rendered`` — present iff :class:`JsRenderedMixin` is in the MRO.

    The other four capabilities (``pagination``, ``filters``, ``metadata``,
    ``cover_art``) are **not** returned, even when the parser in fact
    supports them, because they cannot be determined from class structure
    alone.

    Args:
        parser_cls: A parser class (subclass of :class:`BaseParser`).

    Returns:
        A :class:`frozenset` of inferred capabilities.
    """
    # Lazy imports avoid triggering the ``nexusdl.parsers.mixins.__init__``
    # side effects at module import time, keeping this module's import cost
    # minimal and its dependency graph straightforward.
    from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
    from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin

    inferred: set[ParserCapability] = {
        ParserCapability.SEARCH,
        ParserCapability.CHAPTER_LIST,
        ParserCapability.READER_IMAGES,
    }

    mro = parser_cls.__mro__
    if CloudflareMixin in mro:
        inferred.add(ParserCapability.CLOUDFLARE)
    if JsRenderedMixin in mro:
        inferred.add(ParserCapability.JS_RENDERED)

    return frozenset(inferred)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_capabilities(
    declared: Mapping[str, bool],
    parser_cls: type[BaseParser],
) -> list[str]:
    """Check *declared* capabilities against *parser_cls*'s structure.

    The check is one-directional and conservative:

    * For every **structurally inferable** capability (see
      :func:`infer_capabilities`), if the site declares it ``True`` but the
      parser class structure does not support it, a warning is produced.
      This is always a real inconsistency — the site promises a capability
      the parser cannot deliver.

    * The reverse direction (parser supports the capability but site
      declares it ``False``) is **not** flagged, because defensively
      inheriting a mixin without advertising the capability is legitimate.
      A parser may, for example, inherit :class:`CloudflareMixin` today
      even if the target site is not currently behind Cloudflare, so that
      the parser keeps working if the site later enables it.

    * The four non-inferable capabilities are never cross-checked.  Their
      declared value is authoritative.

    Args:
        declared: The site's declared capabilities, as a mapping of
            capability name → boolean.  Callers typically pass
            ``site.capabilities.model_dump()``.
        parser_cls: The parser class resolved for the site.

    Returns:
        A list of human-readable warning strings, one per inconsistency.
        Empty when everything is consistent.  The function **never raises**
        — the caller (the registry validator) decides how to act on the
        warnings.
    """
    inferred = infer_capabilities(parser_cls)
    warnings: list[str] = []
    parser_name = f"{parser_cls.__module__}.{parser_cls.__qualname__}"

    for cap in _STRUCTURALLY_INFERABLE:
        if not declared.get(cap.value, False):
            continue
        if cap in inferred:
            continue
        warnings.append(
            f"Site declares {cap.value!r} capability, but parser "
            f"{parser_name} does not support it based on its class "
            f"structure (missing the corresponding mixin, or — for "
            f"universal capabilities — the abstract method was removed)."
        )

    return warnings
