"""Public API of the NexusDL image pipeline.

The :mod:`nexusdl.core.image` package is the complete image-processing
toolchain: validation, in-format optimization, format conversion, and
URL-based downloading. Four modules cooperate, each with a single,
non-overlapping responsibility:

* :mod:`~nexusdl.core.image.validator` - decides whether bytes or a file
  constitute a valid image (:class:`ValidationResult`,
  :func:`validate`, :func:`validate_bytes`, :func:`validate_file`,
  :class:`ValidationLimits`).
* :mod:`~nexusdl.core.image.optimizer` - re-encodes an already-validated
  image **in the same format** to fewer bytes, optionally baking EXIF
  orientation and stripping metadata (:class:`OptimizationOptions`,
  :class:`OptimizationResult`, :func:`optimize_bytes`,
  :func:`optimize_file`).
* :mod:`~nexusdl.core.image.converter` - re-encodes an image into a
  **different** format, with optional resize and mode adaptation
  (:class:`ConversionOptions`, :func:`convert`,
  :data:`SUPPORTED_TARGET_FORMATS`).
* :mod:`~nexusdl.core.image.downloader` - fetches a URL, validates the
  bytes, optionally optimizes, and atomically writes the result
  (:class:`ImageDownloader`, :class:`ImageDownloadResult`).

Pipeline order
--------------
The stages compose in a fixed order, and each stage assumes its inputs
have passed the previous stage:

    download --> validate --> [optimize] --> [convert] --> write

* :class:`ImageDownloader` folds the first three stages into one call.
* Callers who already have bytes on disk (e.g. a CLI ``optimize``
  subcommand, a test fixture) use the individual stages directly.
* The optimizer and converter are mutually exclusive for the same input
  on the same pass - the optimizer stays in the source format, the
  converter changes it. Pick one per pass.

Composition
-----------
Every public entry point is async except ``validate``, ``validate_bytes``,
and ``validate_file``, which are synchronous because they are CPU-bound
and never touch the network:

    from nexusdl.core.image import ImageDownloader, OptimizationOptions

    downloader = ImageDownloader(
        session=http_session,
        optimization=OptimizationOptions(jpeg_quality=85),
    )
    result = await downloader.download(url, dest)
    print(result.validation.format, result.bytes_written)

For the lower-level stages:

    from nexusdl.core.image import (
        ConversionOptions,
        convert,
        optimize_file,
        validate_file,
    )

    validation = validate_file(path)
    if not validation.is_valid:
        raise NexusDLError(validation.error or "invalid image")

    await optimize_file(path)                    # in place
    await convert(path, path.with_suffix(".webp"), ConversionOptions(target_format="webp"))

The structural protocol ``HttpSessionProtocol`` (defined in
``downloader.py``) is deliberately **not** re-exported. It exists only as
an injection point for :class:`ImageDownloader`; in practice, callers pass
a concrete :class:`~nexusdl.core.session.http_session.HttpSession` and never
need to name the protocol.

Lazy loading
------------
Eighteen of the twenty exports are resolved through PEP 562's
``__getattr__`` mechanism. The two remaining exports are the introspection
helpers (:func:`list_symbols`, :func:`get_symbol`), defined directly in
this module. Importing ``nexusdl.core.image`` **does not import any
submodule**.

This matters less here than for :mod:`nexusdl.core.downloader` (whose
eager imports pulled in ``httpx``, ``hishel``, ``img2pdf``, and
``py7zr``) because **PIL dominates the cost** - once PIL is loaded
(~30-50 ms), the four image submodules together add only ~5-10 ms. Still,
the principle is the same: a consumer of ``validate_bytes`` should not
pay for ``ImageDownloader``'s transitive dependencies (which include the
whole :mod:`~nexusdl.core.downloader.retry` infrastructure), and a
future submodule with a heavy extra (``avif`` via ``pyav``, ``heif`` via
``pillow-heif``, etc.) will not penalize existing importers.

Illustrative impact (numbers approximate, measured on a warm filesystem
cache; exact timings vary by platform and Pillow version):

========================================  =========  =========
Scenario                                   Before     After
========================================  =========  =========
``from ...image import validate_bytes``   ~45 ms     ~38 ms
``from ...image import SUPPORTED_TARGET_FORMATS`` ~45 ms ~38 ms
``from ...image import ImageDownloader``  ~50 ms     ~50 ms (unchanged)
========================================  =========  =========

The savings are modest today; they will grow as the package accumulates
optional format support. The main value of converting to lazy loading now
is **consistency**: every ``__init__.py`` in the project uses the same
pattern, so there is no surprise about when submodules load.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported, on purpose - they are private
implementation details, stable only within their own module:

* ``_ALLOWED_FORMATS``, ``_OPTIMIZABLE_FORMATS``,
  ``_PILLOW_FORMAT_NAMES``, ``_STRIPPABLE_INFO_KEYS`` - internal format
  tables, kept in sync by unit tests.
* ``_atomic_write``, ``_convert_sync``, ``_optimize_bytes_sync``,
  ``_optimize_file_sync``, ``_looks_like_html``, ``_invalid``,
  ``_validate_url_scheme``, ``_make_temp_path`` - private helpers.
* ``HttpSessionProtocol`` - injection point for :class:`ImageDownloader`;
  passes any object satisfying it without needing the type.

Dependency graph (no cycles within the package)::

    validator   --> core.exceptions
    optimizer   --> (PIL, loguru only)
    converter   --> core.exceptions
    downloader  --> core.downloader.retry, core.exceptions,
                    core.image.optimizer, core.image.validator

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency - rule R1 of
the PROMPT MASTER.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Validator ------------------------------------------------------- #
    "DEFAULT_LIMITS",
    "ValidationLimits",
    "ValidationResult",
    "validate",
    "validate_bytes",
    "validate_file",
    # --- Optimizer ------------------------------------------------------- #
    "DEFAULT_OPTIONS",
    "OptimizationOptions",
    "OptimizationResult",
    "optimize_bytes",
    "optimize_file",
    # --- Converter ------------------------------------------------------- #
    "SUPPORTED_TARGET_FORMATS",
    "ConversionOptions",
    "convert",
    # --- Downloader ------------------------------------------------------ #
    "ImageDownloadResult",
    "ImageDownloader",
    # --- Introspection --------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Introspection helpers
# ===========================================================================
#
# Defined before ``__getattr__`` and ``_LAZY`` so they are always bound in
# ``globals()`` — ``__getattr__`` is only invoked for names that are *not*
# already present, so these bypass the lazy registry entirely.

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the image pipeline's public surface without importing every
    submodule.

    The two introspection helpers (:func:`list_symbols`, :func:`get_symbol`)
    are part of ``__all__`` but are **not** included in the returned list:
    they are defined directly in this module and do not live in any
    submodule.
    """
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the image-pipeline symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, composition-root builders that resolve a symbol
    from a config string, image-pipeline inspectors).

    Both the submodule exports (resolved lazily through :func:`__getattr__`)
    and the two introspection helpers (defined in this module) are
    accepted.

    Raises:
        KeyError: If ``name`` is not a public image export.
    """
    if name in ("list_symbols", "get_symbol"):
        return globals()[name]
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown image symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.image.<submodule>' directly."
        )
    return __getattr__(name)


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.

_LAZY: dict[str, tuple[str, str]] = {
    # --- validator (synchronous, PIL + core.exceptions) ------------------ #
    "DEFAULT_LIMITS": ("validator", "DEFAULT_LIMITS"),
    "ValidationLimits": ("validator", "ValidationLimits"),
    "ValidationResult": ("validator", "ValidationResult"),
    "validate": ("validator", "validate"),
    "validate_bytes": ("validator", "validate_bytes"),
    "validate_file": ("validator", "validate_file"),
    # --- optimizer (asynchronous, PIL + loguru) -------------------------- #
    "DEFAULT_OPTIONS": ("optimizer", "DEFAULT_OPTIONS"),
    "OptimizationOptions": ("optimizer", "OptimizationOptions"),
    "OptimizationResult": ("optimizer", "OptimizationResult"),
    "optimize_bytes": ("optimizer", "optimize_bytes"),
    "optimize_file": ("optimizer", "optimize_file"),
    # --- converter (asynchronous, PIL + core.exceptions) ----------------- #
    "SUPPORTED_TARGET_FORMATS": ("converter", "SUPPORTED_TARGET_FORMATS"),
    "ConversionOptions": ("converter", "ConversionOptions"),
    "convert": ("converter", "convert"),
    # --- downloader (asynchronous, full pipeline) ------------------------ #
    "ImageDownloadResult": ("downloader", "ImageDownloadResult"),
    "ImageDownloader": ("downloader", "ImageDownloader"),
}


# Sanity check: ``__all__`` must be exactly the union of the lazy registry
# and the two helpers defined in this module. A mismatch means someone
# added a symbol to one place and forgot the other — silent divergence that
# only surfaces at runtime as ``AttributeError`` or ``ImportError`` in a
# specific import path.
_INTROSPECTION_HELPERS = frozenset({"list_symbols", "get_symbol"})
assert set(__all__) == set(_LAZY) | _INTROSPECTION_HELPERS, (
    f"__all__ / _LAZY mismatch: "
    f"extra in __all__={set(__all__) - set(_LAZY) - _INTROSPECTION_HELPERS}, "
    f"extra in _LAZY={set(_LAZY) - set(__all__)}"
)


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.image.converter import (
        SUPPORTED_TARGET_FORMATS,
        ConversionOptions,
        convert,
    )
    from nexusdl.core.image.downloader import (
        ImageDownloadResult,
        ImageDownloader,
    )
    from nexusdl.core.image.optimizer import (
        DEFAULT_OPTIONS,
        OptimizationOptions,
        OptimizationResult,
        optimize_bytes,
        optimize_file,
    )
    from nexusdl.core.image.validator import (
        DEFAULT_LIMITS,
        ValidationLimits,
        ValidationResult,
        validate,
        validate_bytes,
        validate_file,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

#: Root module names of the optional dependencies that a missing import
#: might refer to. Compared against ``exc.name.split(".")[0]`` so that a
#: missing submodule (``PIL._imaging``) is attributed to the correct
#: top-level package.
_OPTIONAL_DEPENDENCY_ROOTS: frozenset[str] = frozenset({"PIL", "Pillow"})


def __getattr__(name: str) -> Any:
    """Resolve a public image-pipeline symbol on first access (PEP 562).

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
            submodule cannot be imported (a missing ``Pillow`` dependency,
            a syntax error deeper in the module, or a broken installation),
            or when the submodule does not define the requested name.
            ``AttributeError`` - rather than ``ImportError`` - is required
            by PEP 562 so that ``hasattr`` works.
        ImportError: When the import failure is specifically a missing
            Pillow dependency. The original :class:`ModuleNotFoundError` is
            chained as ``__cause__``.
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
            f"Available image exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.image.validator import "
            f"ValidationResult' or 'from "
            f"nexusdl.core.image.downloader import HttpSessionProtocol'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the image pipeline. Compare on
        # the **root** module name so that a missing submodule (``PIL._imaging``)
        # is attributed to the correct top-level package.
        missing_root = exc.name.split(".")[0] if exc.name else None
        if missing_root in _OPTIONAL_DEPENDENCY_ROOTS:
            raise ImportError(
                f"{name!r} requires Pillow. "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The image submodule {submodule_basename!r} does not define "
            f"{attr_name!r}. This is a package-internal inconsistency - "
            f"nexusdl.core.image._LAZY and the submodules are out of "
            f"sync."
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
    the lazy exports are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would appear
    empty in a REPL.

    The returned list is :data:`__all__`, the module dunders, and any
    attribute already bound in ``globals()`` (whether via a resolved lazy
    import or via external assignment). This matches Python's contract
    that ``__dir__`` should report everything accessible through
    ``getattr``.
    """
    return sorted(
        set(globals().keys())
        | set(__all__)
        | {"__all__", "__doc__", "__name__", "__package__"}
    )
