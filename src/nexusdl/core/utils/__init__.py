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
         ▲
         │ (retry.py only — RetryTimeoutError)
         │
    ┌────┴──────────────────────────────────────┐
    │  url.py   time.py   text.py   hash.py      │
    │  filesystem.py         async_helpers.py    │
    │                    ▲                        │
    │                    │ (loguru only)          │
    │                    │                        │
    │              retry.py                       │
    └────────────────────────────────────────────┘
                        ▲
                        │
    core.models.* / core.downloader.* / core.image.* /
    core.library.* / core.packaging.* / core.registry.* /
    core.session.* / interfaces.*  — every other package depends on
    this one, and this one depends on none of them.

Six of the seven submodules are pure leaves with **zero** nexusdl
imports; ``retry.py`` imports ``core.exceptions`` for the single
exception class it raises. The package is at the bottom of the
dependency graph, and this ``__init__.py`` preserves that property —
importing it triggers the seven submodule imports in leaf-first order,
none of which can trigger a cycle.
"""

from __future__ import annotations

# --- URL manipulation ---------------------------------------------------- #
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

# --- Time and duration --------------------------------------------------- #
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

# --- Text and strings ---------------------------------------------------- #
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

# --- Hashing and IDs ----------------------------------------------------- #
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

# --- Generic retry ------------------------------------------------------- #
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

# --- Filesystem primitives ---------------------------------------------- #
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

# --- Async concurrency --------------------------------------------------- #
from nexusdl.core.utils.async_helpers import (
    AsyncLazy,
    cancel_all,
    cancel_safely,
    fire_and_forget,
)

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
]
