"""Public API of the NexusDL utility primitives.

The :mod:`nexusdl.core.utils` package is the pipeline's **standard
library**: seven small, pure, dependency-light modules that every other
package in NexusDL depends on. Nothing in this package imports from
``parsers/``, ``plugins/``, or ``interfaces/`` — the dependency runs
strictly in one direction. Six of the seven submodules are pure
stdlib-only leaves; the seventh (``retry.py``) has a single dependency on
``core.exceptions`` for the one exception class it raises.

Seven modules, seven concerns
-----------------------------
* :mod:`~nexusdl.core.utils.url` — parse, normalize, join, and validate
  URLs. Canonical accessors (``get_scheme``, ``get_host``, …),
  predicates (``is_https``, ``is_fetchable``, ``is_same_origin``), and
  mutators (``normalize``, ``join``, ``ensure_https``). The whole module
  is stdlib (``urllib.parse``) with no third-party or in-project
  imports.
* :mod:`~nexusdl.core.utils.time` — canonical UTC time and duration
  primitives: ``utcnow``, ``format_iso``, ``parse_iso`` (lenient and
  strict variants), Unix epoch conversions, human-readable durations
  (``format_duration``, ``format_relative``), and a monotonic
  :class:`Stopwatch`. The whole module is stdlib (``datetime``,
  ``time``).
* :mod:`~nexusdl.core.utils.text` — text manipulation: Unicode-aware
  case folding (``casefold_key``), tolerant booleans (``parse_bool``),
  number extraction (``extract_number``), HTML stripping
  (``strip_html``), CSV splitting (``split_csv``), slug generation
  (``slugify_safe``), and truncation (``truncate``,
  ``truncate_words``). Depends only on ``python-slugify`` (an existing
  runtime dependency).
* :mod:`~nexusdl.core.utils.hash` — deterministic ID generation
  (``make_manga_id``, ``make_chapter_id``), general SHA-256 primitives
  (``sha256_hex``, ``sha256_truncated``), and content hashing for dedup
  (``file_sha256``, ``file_sha256_sync``). The whole module is stdlib
  (``hashlib``).
* :mod:`~nexusdl.core.utils.retry` — **generic** retry primitives with
  a wall-clock budget: :class:`RetryPolicy`,
  :class:`BackoffStrategy`, four backoff factories, and the
  ``retry_async`` / ``retry_sync`` functions. The
  downloader-specific classification and tenacity controller live in
  :mod:`nexusdl.core.downloader.retry` — see that module's docstring
  for the split.
* :mod:`~nexusdl.core.utils.filesystem` — atomic writes, safe deletion,
  size reporting, filename validation, and the ``fsync``-based helpers
  every packager and cache currently duplicates inline. The whole module
  is stdlib (``os``, ``pathlib``, ``shutil``, ``tempfile``).
* :mod:`~nexusdl.core.utils.async_helpers` — cancellation
  (:func:`cancel_safely`, :func:`cancel_all`), background scheduling
  (:func:`fire_and_forget`), and lazy initialisation
  (:class:`AsyncLazy`). Depends only on ``loguru``.

Composition example
-------------------
A CLI helper that touches several utilities at once::

    from pathlib import Path

    from nexusdl.core.utils.filesystem import (
        atomic_write_text,
        human_readable_size,
    )
    from nexusdl.core.utils.hash import make_manga_id
    from nexusdl.core.utils.text import slugify_safe
    from nexusdl.core.utils.time import Stopwatch, utcnow_iso
    from nexusdl.core.utils.url import ensure_https

    with Stopwatch() as sw:
        manga_id = make_manga_id("mangadex", "a1b2c3d4")
        safe_title = slugify_safe("One Piece — Chapitre 5.5 (VF)")
        url = ensure_https("http://mangadex.org/title/xyz")
        atomic_write_text(
            Path(f"{manga_id}-{safe_title}.json"),
            f'{{"url": "{url}", "saved_at": "{utcnow_iso()}"}}',
        )
    print(f"Done in {sw.elapsed_human()}")

Two import styles, two audiences
--------------------------------
Application code should **import from the specific submodule**::

    from nexusdl.core.utils.time import utcnow

    timestamp = utcnow()

This makes the dependency explicit, keeps the import list minimal, and
avoids pulling symbols the caller does not need. Static analysis (an
import-graph checker, a future ``scripts/check_imports.py``) can see
exactly which utility module each file depends on.

`__init__.py`'s ``__all__`` exists for three narrower purposes:

* **Discovery** — ``dir(nexusdl.core.utils)`` lists every public
  symbol in one place, useful for a docs generator or a test that
  checks the public surface.
* **Convenience in tests and one-off scripts** — a REPL session or a
  throwaway test that touches half a dozen utilities is cleaner as a
  single ``from nexusdl.core.utils import ...``.
* **Type-hint import path stability** — an external package that
  depends on NexusDL can type-annotate with
  ``nexusdl.core.utils.RetryPolicy`` without committing to a specific
  submodule path. If the class ever moves, ``__init__.py``'s re-export
  keeps the annotation valid.

The recommendation is: submodule imports in production code, unified
imports in tests, scripts, and external annotations.

Retry — one module, two layers
------------------------------
:mod:`~nexusdl.core.utils.retry` and
:mod:`~nexusdl.core.downloader.retry` both exist and both deal with
retries, and that is intentional:

* **This package** — the generic primitives. No exception
  classification, no downloader-specific defaults, a wall-clock budget
  that raises :class:`~nexusdl.core.exceptions.RetryTimeoutError`.
  Used by Playwright launches, FlareSolverr calls, future database
  retries, CLI helpers running before the event loop starts, and
  third-party plugins.
* **``core.downloader.retry``** — the downloader-specific layer.
  Hardcoded ``_TRANSIENT_ERRORS`` / ``_PERMANENT_ERRORS``,
  tenacity-backed controller, no wall-clock budget. Used by the worker.

Duplicating the primitives into the downloader package would either
fragment the API (callers would need to know which retry module to
import depending on context) or force the generic module to absorb the
downloader's exception classification (which the generic layer has no
business knowing). Both modules document the split in their docstrings;
neither is a "future consolidation target" — they are the same pattern
applied at two different layers of abstraction.

Filesystem — the de-duplication target
--------------------------------------
:mod:`~nexusdl.core.utils.filesystem` exists specifically to **stop**
new duplications. Before it, the atomic-write pattern was implemented
nine times across the packagers, the cache, the image pipeline, and the
cookie manager, each with slightly different signatures and slightly
different cleanup behaviour. Every helper in the filesystem module is
documented as a refactor target for at least one existing private
helper, and each substitution is a one-line change, behaviour-preserving.

The migration is **scheduled, not urgent** — the duplicated helpers
currently work correctly. When each affected file is next opened for any
reason, the substitution is applied. Until then, the duplication is
acknowledged and documented rather than hidden.

Lazy loading
------------
All ~84 exports are resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core.utils`` **does not import any
submodule**.

The gain here is smaller than in other packages in the project (the
utility modules are predominantly stdlib-only), but it is real and it
completes the lazy-loading convention across every ``__init__.py`` in
the project:

* ``url.py``, ``time.py``, ``hash.py``, ``filesystem.py``, and
  ``retry.py`` are stdlib-only — ~2-3 ms each.
* ``text.py`` pulls in ``python-slugify`` (~10-15 ms, with
  ``text-unidecode`` as a transitive dep).
* ``async_helpers.py`` pulls in ``loguru`` (~10-15 ms).

**Total eager cost: ~30-50 ms.** Modest in absolute terms — but every
parser in ``nexusdl.parsers.*`` imports at least one utility helper
(``make_manga_id``, ``make_chapter_id``, ``utcnow``, ``utcnow_iso``,
``normalize``, …) at module load, and none of them needs
``python-slugify`` or ``loguru`` at that point.

**Before:** any of ``from ...utils import make_manga_id``,
``from ...utils import utcnow``, or
``from ...utils import atomic_write_bytes`` paid the entire ~40 ms.

**After:**

=============================================  =========  =========
Scenario                                        Before     After
=============================================  =========  =========
``from ...utils import make_manga_id``         ~40 ms     ~3 ms
``from ...utils import utcnow``                ~40 ms     ~2 ms
``from ...utils import atomic_write_bytes``    ~40 ms     ~3 ms
``from ...utils import RetryPolicy``           ~40 ms     ~3 ms
``from ...utils import slugify_safe``          ~40 ms     ~15 ms
``from ...utils import fire_and_forget``       ~40 ms     ~15 ms
=============================================  =========  =========

The public surface is unchanged: ``from nexusdl.core.utils import X``
works exactly as before — resolved on first access and cached. No
caller has to change a line.

.. note::
   The ``DEFAULT_TRUNCATION_SUFFIX``, ``DEFAULT_SLUG_FALLBACK``, and
   ``DEFAULT_SLUG_MAX_LENGTH`` module constants in ``text.py`` remain
   **not** re-exported (they are defaults, not contract), which is why
   they are absent from ``_LAZY``. A caller that needs a specific
   truncation suffix or slug fallback passes it as an explicit argument
   to ``truncate`` / ``slugify_safe``.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The symbols fall into four stability tiers, all re-exported:

* **Tier 1 — heavily consumed** (``utcnow``, ``format_iso``,
  ``make_manga_id``, ``make_chapter_id``, ``slugify_safe``,
  ``atomic_write_bytes``, ``RetryPolicy``, ``Stopwatch``). Renaming
  any of these is a project-wide breaking change.
* **Tier 2 — used by a specific subsystem** (``split_csv``,
  ``strip_html``, ``file_sha256``, ``fire_and_forget``, ``cancel_all``,
  ``AsyncLazy``, ``normalize``, ``join``). Renaming is a
  subsystem-scoped change; also breaking.
* **Tier 3 — convenience accessors** (the URL ``get_*`` and ``is_*``
  functions, ``parse_bool``, ``extract_number``, ``truncate``,
  ``human_readable_size``). Stable but lower impact to break.
* **Tier 4 — constants** (``SECONDS_PER_*``, ``UNIX_EPOCH``,
  ``MANGA_ID_LENGTH``, ``SHA256_HEX_LENGTH``, ``HTTP_SCHEMES``, …).
  Their values are the contract; the names are stable.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* Every ``_*`` helper in every submodule.
* ``_DEFAULT_*`` constants in ``retry.py`` (``_DEFAULT_BACKOFF``,
  ``_DEFAULT_INITIAL_DELAY``, ``_DEFAULT_MAX_ATTEMPTS``,
  ``_DEFAULT_MAX_DELAY``, ``_DEFAULT_JITTER_FACTOR``) — the downloader
  package's public constants use the same names, and re-exporting the
  private ones would create a two-source-of-truth hazard. A caller who
  wants the specific value writes it explicitly.
* ``_ID_SEPARATOR``, ``_ID_ENCODING`` in ``hash.py`` — the id formula's
  internal composition, not part of the API.
* ``_SIZE_UNITS``, ``_SIZE_STEP``, ``_INVALID_FILENAME_CHARS``,
  ``_TEMP_FILE_PREFIX_FMT``, ``_BACKUP_DIR_SUFFIX`` in
  ``filesystem.py`` — internal implementation details.
* ``_TRUE_STRINGS``, ``_FALSE_STRINGS``, ``_BLOCK_CLOSING_TAGS``,
  ``_BR_TAGS``, ``_HTML_TAG``, ``_WHITESPACE_RUN``, ``_NUMBER``,
  ``_TEMPLATE``, ``DEFAULT_TRUNCATION_SUFFIX``,
  ``DEFAULT_SLUG_FALLBACK``, ``DEFAULT_SLUG_MAX_LENGTH`` in
  ``text.py`` — implementation details and defaults; a caller who
  wants a specific truncation suffix passes it as an argument.
* ``_background_tasks``, ``_log_background_exception`` in
  ``async_helpers.py`` — module-private task registry and callback.
* ``_parse``, ``_sort_query`` in ``url.py``; ``_check_time_budget``,
  ``_should_retry`` in ``retry.py`` — internal helpers.
* ``_TEMP_PREFIX_FMT`` (the packager-owned variant) — modules that
  share the convention reference their own copy; this module exports
  the canonical helper, not the constant.

Dependency graph (no cycles)::

    core.exceptions
         ^
         | (retry.py only — RetryTimeoutError)
         |
    +----+--------------------------------------+
    |  url.py   time.py   text.py   hash.py      |
    |  filesystem.py         async_helpers.py    |
    |                    ^                        |
    |                    | (loguru only)          |
    |                    |                        |
    |              retry.py                       |
    +--------------------------------------------+
                        ^
                        |
    core.models.* / core.downloader.* / core.image.* /
    core.library.* / core.packaging.* / core.registry.* /
    core.session.* / interfaces.*  — every other package depends on
    this one, and this one depends on none of them.

Six of the seven submodules are pure leaves with **zero** nexusdl
imports; ``retry.py`` imports ``core.exceptions`` for the single
exception class it raises. The package is at the bottom of the
dependency graph, and this ``__init__.py`` preserves that property —
the seven submodules are loaded lazily, on first attribute access, in
whatever order the caller happens to access them. Python's normal
import machinery resolves the nested imports correctly regardless of
entry point (the graph is cycle-free, so ordering does not matter).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- URL manipulation ------------------------------------------------ #
    "DEFAULT_PORTS",
    "HTTP_SCHEMES",
    "SUPPORTED_SCHEMES",
    "ensure_https",
    "get_extension",
    "get_fragment",
    "get_filename",
    "get_host",
    "get_path",
    "get_port",
    "get_query",
    "get_scheme",
    "get_stem",
    "is_absolute",
    "is_fetchable",
    "is_file",
    "is_http",
    "is_https",
    "is_relative",
    "is_same_origin",
    "is_supported_scheme",
    "join",
    "normalize",
    "strip_fragment",
    "strip_query",
    "strip_userinfo",
    # --- Time and duration ---------------------------------------------- #
    "SECONDS_PER_DAY",
    "SECONDS_PER_HOUR",
    "SECONDS_PER_MINUTE",
    "SECONDS_PER_WEEK",
    "UNIX_EPOCH",
    "Stopwatch",
    "format_duration",
    "format_iso",
    "format_relative",
    "from_unix",
    "parse_iso",
    "parse_iso_or_none",
    "to_unix",
    "utcnow",
    "utcnow_iso",
    # --- Text and strings ----------------------------------------------- #
    "casefold_key",
    "coerce_str",
    "extract_number",
    "normalize_whitespace",
    "parse_bool",
    "slugify_safe",
    "split_csv",
    "strip_html",
    "truncate",
    "truncate_words",
    # --- Hashing and IDs ------------------------------------------------ #
    "CHAPTER_ID_LENGTH",
    "DEFAULT_FILE_CHUNK_SIZE",
    "MANGA_ID_LENGTH",
    "SHA256_HEX_LENGTH",
    "file_sha256",
    "file_sha256_sync",
    "make_chapter_id",
    "make_manga_id",
    "sha256_hex",
    "sha256_truncated",
    # --- Generic retry -------------------------------------------------- #
    "BackoffStrategy",
    "RetryPolicy",
    "constant_backoff",
    "exponential_backoff",
    "exponential_jitter_backoff",
    "linear_backoff",
    "retry_async",
    "retry_sync",
    # --- Filesystem primitives ----------------------------------------- #
    "atomic_replace_directory",
    "atomic_write_bytes",
    "atomic_write_text",
    "chmod_owner_only",
    "delete_file_quietly",
    "ensure_directory",
    "fsync_directory",
    "get_file_size",
    "human_readable_size",
    "is_safe_filename",
    "validate_filename",
    # --- Async concurrency --------------------------------------------- #
    "AsyncLazy",
    "cancel_all",
    "cancel_safely",
    "fire_and_forget",
    # --- Introspection -------------------------------------------------- #
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
    # --- url (stdlib only) ----------------------------------------------- #
    "DEFAULT_PORTS": ("url", "DEFAULT_PORTS"),
    "HTTP_SCHEMES": ("url", "HTTP_SCHEMES"),
    "SUPPORTED_SCHEMES": ("url", "SUPPORTED_SCHEMES"),
    "ensure_https": ("url", "ensure_https"),
    "get_extension": ("url", "get_extension"),
    "get_fragment": ("url", "get_fragment"),
    "get_filename": ("url", "get_filename"),
    "get_host": ("url", "get_host"),
    "get_path": ("url", "get_path"),
    "get_port": ("url", "get_port"),
    "get_query": ("url", "get_query"),
    "get_scheme": ("url", "get_scheme"),
    "get_stem": ("url", "get_stem"),
    "is_absolute": ("url", "is_absolute"),
    "is_fetchable": ("url", "is_fetchable"),
    "is_file": ("url", "is_file"),
    "is_http": ("url", "is_http"),
    "is_https": ("url", "is_https"),
    "is_relative": ("url", "is_relative"),
    "is_same_origin": ("url", "is_same_origin"),
    "is_supported_scheme": ("url", "is_supported_scheme"),
    "join": ("url", "join"),
    "normalize": ("url", "normalize"),
    "strip_fragment": ("url", "strip_fragment"),
    "strip_query": ("url", "strip_query"),
    "strip_userinfo": ("url", "strip_userinfo"),
    # --- time (stdlib only) ---------------------------------------------- #
    "SECONDS_PER_DAY": ("time", "SECONDS_PER_DAY"),
    "SECONDS_PER_HOUR": ("time", "SECONDS_PER_HOUR"),
    "SECONDS_PER_MINUTE": ("time", "SECONDS_PER_MINUTE"),
    "SECONDS_PER_WEEK": ("time", "SECONDS_PER_WEEK"),
    "UNIX_EPOCH": ("time", "UNIX_EPOCH"),
    "Stopwatch": ("time", "Stopwatch"),
    "format_duration": ("time", "format_duration"),
    "format_iso": ("time", "format_iso"),
    "format_relative": ("time", "format_relative"),
    "from_unix": ("time", "from_unix"),
    "parse_iso": ("time", "parse_iso"),
    "parse_iso_or_none": ("time", "parse_iso_or_none"),
    "to_unix": ("time", "to_unix"),
    "utcnow": ("time", "utcnow"),
    "utcnow_iso": ("time", "utcnow_iso"),
    # --- text (python-slugify) ------------------------------------------- #
    "casefold_key": ("text", "casefold_key"),
    "coerce_str": ("text", "coerce_str"),
    "extract_number": ("text", "extract_number"),
    "normalize_whitespace": ("text", "normalize_whitespace"),
    "parse_bool": ("text", "parse_bool"),
    "slugify_safe": ("text", "slugify_safe"),
    "split_csv": ("text", "split_csv"),
    "strip_html": ("text", "strip_html"),
    "truncate": ("text", "truncate"),
    "truncate_words": ("text", "truncate_words"),
    # --- hash (stdlib only) ---------------------------------------------- #
    "CHAPTER_ID_LENGTH": ("hash", "CHAPTER_ID_LENGTH"),
    "DEFAULT_FILE_CHUNK_SIZE": ("hash", "DEFAULT_FILE_CHUNK_SIZE"),
    "MANGA_ID_LENGTH": ("hash", "MANGA_ID_LENGTH"),
    "SHA256_HEX_LENGTH": ("hash", "SHA256_HEX_LENGTH"),
    "file_sha256": ("hash", "file_sha256"),
    "file_sha256_sync": ("hash", "file_sha256_sync"),
    "make_chapter_id": ("hash", "make_chapter_id"),
    "make_manga_id": ("hash", "make_manga_id"),
    "sha256_hex": ("hash", "sha256_hex"),
    "sha256_truncated": ("hash", "sha256_truncated"),
    # --- retry (stdlib + core.exceptions) -------------------------------- #
    "BackoffStrategy": ("retry", "BackoffStrategy"),
    "RetryPolicy": ("retry", "RetryPolicy"),
    "constant_backoff": ("retry", "constant_backoff"),
    "exponential_backoff": ("retry", "exponential_backoff"),
    "exponential_jitter_backoff": ("retry", "exponential_jitter_backoff"),
    "linear_backoff": ("retry", "linear_backoff"),
    "retry_async": ("retry", "retry_async"),
    "retry_sync": ("retry", "retry_sync"),
    # --- filesystem (stdlib only) ---------------------------------------- #
    "atomic_replace_directory": ("filesystem", "atomic_replace_directory"),
    "atomic_write_bytes": ("filesystem", "atomic_write_bytes"),
    "atomic_write_text": ("filesystem", "atomic_write_text"),
    "chmod_owner_only": ("filesystem", "chmod_owner_only"),
    "delete_file_quietly": ("filesystem", "delete_file_quietly"),
    "ensure_directory": ("filesystem", "ensure_directory"),
    "fsync_directory": ("filesystem", "fsync_directory"),
    "get_file_size": ("filesystem", "get_file_size"),
    "human_readable_size": ("filesystem", "human_readable_size"),
    "is_safe_filename": ("filesystem", "is_safe_filename"),
    "validate_filename": ("filesystem", "validate_filename"),
    # --- async_helpers (loguru) ------------------------------------------ #
    "AsyncLazy": ("async_helpers", "AsyncLazy"),
    "cancel_all": ("async_helpers", "cancel_all"),
    "cancel_safely": ("async_helpers", "cancel_safely"),
    "fire_and_forget": ("async_helpers", "fire_and_forget"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.utils.async_helpers import (
        AsyncLazy,
        cancel_all,
        cancel_safely,
        fire_and_forget,
    )
    from nexusdl.core.utils.filesystem import (
        atomic_replace_directory,
        atomic_write_bytes,
        atomic_write_text,
        chmod_owner_only,
        delete_file_quietly,
        ensure_directory,
        fsync_directory,
        get_file_size,
        human_readable_size,
        is_safe_filename,
        validate_filename,
    )
    from nexusdl.core.utils.hash import (
        CHAPTER_ID_LENGTH,
        DEFAULT_FILE_CHUNK_SIZE,
        MANGA_ID_LENGTH,
        SHA256_HEX_LENGTH,
        file_sha256,
        file_sha256_sync,
        make_chapter_id,
        make_manga_id,
        sha256_hex,
        sha256_truncated,
    )
    from nexusdl.core.utils.retry import (
        BackoffStrategy,
        RetryPolicy,
        constant_backoff,
        exponential_backoff,
        exponential_jitter_backoff,
        linear_backoff,
        retry_async,
        retry_sync,
    )
    from nexusdl.core.utils.text import (
        casefold_key,
        coerce_str,
        extract_number,
        normalize_whitespace,
        parse_bool,
        slugify_safe,
        split_csv,
        strip_html,
        truncate,
        truncate_words,
    )
    from nexusdl.core.utils.time import (
        SECONDS_PER_DAY,
        SECONDS_PER_HOUR,
        SECONDS_PER_MINUTE,
        SECONDS_PER_WEEK,
        UNIX_EPOCH,
        Stopwatch,
        format_duration,
        format_iso,
        format_relative,
        from_unix,
        parse_iso,
        parse_iso_or_none,
        to_unix,
        utcnow,
        utcnow_iso,
    )
    from nexusdl.core.utils.url import (
        DEFAULT_PORTS,
        HTTP_SCHEMES,
        SUPPORTED_SCHEMES,
        ensure_https,
        get_extension,
        get_fragment,
        get_filename,
        get_host,
        get_path,
        get_port,
        get_query,
        get_scheme,
        get_stem,
        is_absolute,
        is_fetchable,
        is_file,
        is_http,
        is_https,
        is_relative,
        is_same_origin,
        is_supported_scheme,
        join,
        normalize,
        strip_fragment,
        strip_query,
        strip_userinfo,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public utility symbol on first access (PEP 562).

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
            such as ``python-slugify`` or ``loguru``, a syntax error
            deeper in the module, or a broken installation), or when the
            submodule does not define the requested name.
            ``AttributeError`` — rather than ``ImportError`` — is
            required by PEP 562 so that ``hasattr`` works.
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
            f"Available utility exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.utils.text import "
            f"DEFAULT_TRUNCATION_SUFFIX' or 'from "
            f"nexusdl.core.utils.retry import _DEFAULT_MAX_ATTEMPTS'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the utilities package.
        if exc.name in {"slugify", "loguru"}:
            raise ImportError(
                f"{name!r} requires the core extra "
                f"({'python-slugify' if exc.name == 'slugify' else 'loguru'}). "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The utility submodule {submodule_basename!r} does not "
            f"define {attr_name!r}. This is a package-internal "
            f"inconsistency - nexusdl.core.utils.__all__ and the "
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
    the ~84 exports are present in ``__dict__`` at import time, an
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
    list_symbols())``), documentation generators, and tooling that needs
    to enumerate the utilities' public surface without importing every
    submodule. This is the largest ``__all__`` in the project (~84
    symbols); a docs generator that wants to render the whole surface
    without paying the import cost will find this function useful.
    """
    # Only the public exports, not the introspection helpers themselves -
    # otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the utility symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, documentation generators that need to introspect
    the surface, external packages that resolve a helper by name).

    Raises:
        KeyError: If ``name`` is not a public utility export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown utility symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.utils.<submodule>' directly."
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
