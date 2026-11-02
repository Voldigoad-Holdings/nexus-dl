"""Canonical constants shared across the NexusDL pipeline.

This module is deliberately **small**. It exists to hold the handful of
values that (a) have a single authoritative meaning across the entire
project, (b) would otherwise be duplicated as string literals in several
modules, and (c) are not better expressed as an enum
(:mod:`nexusdl.core.models.enums`), a config field
(:mod:`nexusdl.core.config`), or a path (:mod:`nexusdl.core.paths`).

Everything else — numeric tuning, per-module defaults, regex patterns,
HTTP header names — lives where it is used. A caller reading
``worker.py`` sees the packager defaults it applies; a caller reading
``cache.py`` sees the cache's own tuning. There is no "central dumping
ground" for values that would otherwise sit next to their consumer.

Three groups
------------
* **Application identity** — the human-readable name and the technical
  slug. These are the only two identity strings the codebase ever needs.
  ``APP_NAME`` is the shipped default for the display name; a deployment
  that overrides ``NEXUSDL_APP__NAME`` changes the effective display
  name without touching ``APP_NAME``. ``APP_SLUG`` is the invariant
  technical identifier: it appears in filesystem paths, cache keys,
  plugin data directories, and the ``pyproject.toml`` distribution name.
  It cannot be overridden — changing it would silently orphan every
  existing installation's data.
* **Data package** — the Python package path from which the pipeline
  loads its bundled resources (``sites.yaml``, ``sites_schema.json``,
  ``user_agents.txt``, ``default_config.yaml``, ``comic_info_template.xml``).
  This value is resolved via :mod:`importlib.resources`, so it must
  match the actual package name in the installed wheel.
* **Event types** — the twelve canonical event names emitted on the
  :class:`~nexusdl.core.events.EventBus`. Every module that publishes
  or subscribes to an event references one of these constants. A typo
  in an event name is currently undetectable at the type level (the bus
  keys on strings); centralising the names in one file means the only
  place a typo can occur is here.

Documented duplication
----------------------
Several modules were shipped **before** this file existed, and they
contain private copies of the constants defined here:

* :mod:`nexusdl.core.config` — private ``_APP_NAME = "nexusdl"`` and
  ``_DEFAULT_YAML_PACKAGE = "nexusdl.data"``.
* :mod:`nexusdl.core.registry.loader` — public ``DATA_PACKAGE = "nexusdl.data"``.
* :mod:`nexusdl.core.registry.validator` — private ``_SCHEMA_PACKAGE = "nexusdl.data"``.
* :mod:`nexusdl.core.session.user_agents` — private ``_DATA_PACKAGE = "nexusdl.data"``.
* :mod:`nexusdl.core.downloader.worker` — private ``_EVENT_DOWNLOAD_STARTED``,
  ``_EVENT_DOWNLOAD_PROGRESS``, ``_EVENT_DOWNLOAD_COMPLETED``,
  ``_EVENT_DOWNLOAD_FAILED``.
* :mod:`nexusdl.core.downloader.progress` — same four ``_EVENT_DOWNLOAD_*``.
* :mod:`nexusdl.core.downloader.manager` — private
  ``_EVENT_DOWNLOAD_PAUSED``, ``_EVENT_DOWNLOAD_RESUMED``,
  ``_EVENT_DOWNLOAD_CANCELLED``, ``_EVENT_CHAPTER_STARTED``,
  ``_EVENT_CHAPTER_COMPLETED``, ``_EVENT_CHAPTER_FAILED``.

The duplication is **not a bug** — every copy is a plain string literal
that equals the canonical value, so runtime behaviour is identical
regardless of which module's copy is used. The problem it creates is
*maintenance*: an edit to a string in one module would silently diverge
from the others, and there would be no compile-time signal.

The refactor is **scheduled, not urgent**, and follows the same rule
used elsewhere in this codebase: each affected module is opened for the
substitution the next time it is opened **for any other reason**. The
substitution is a one-line change per module: replace the private
constant with ``from nexusdl.core.constants import EVENT_...`` (or
``APP_SLUG`` / ``DATA_PACKAGE``) and use it as before. No call site
changes; no behaviour changes.

This file will not reopen any of those modules on its own — the user's
explicit rule is that a delivered file is final, and adding a constant
here is not a reason to touch another module.

What is **not** here, by design
-------------------------------
* **No numeric defaults.** Rate limit defaults live in
  :class:`~nexusdl.core.config.RateLimitsSection`; retry defaults live in
  :mod:`nexusdl.core.downloader.retry`; cache defaults live in
  :mod:`nexusdl.core.session.cache`. Each is next to the code that uses
  it and is overridable through :class:`~nexusdl.core.config.Settings`.
* **No regex patterns.** Every pattern lives in the module that uses it
  (site ID validation in :mod:`nexusdl.core.models.site`, filename
  validation in :mod:`nexusdl.core.utils.filesystem`, robots patterns
  in :mod:`nexusdl.core.compliance`). Centralising regexes would divorce
  the pattern from the code that must keep it in sync with a schema.
* **No HTTP header names.** Header names are case-insensitive per the
  HTTP specification, and every library the pipeline uses
  (:mod:`httpx` in particular) normalises case at the transport layer.
  A ``HEADER_USER_AGENT = "User-Agent"`` constant adds a level of
  indirection without a correctness benefit — a caller typing
  ``"User-Agent"`` directly produces identical behaviour.
* **No MIME types.** Content negotiation is handled by :mod:`httpx`;
  the pipeline's only MIME-adjacent logic (the cache's content-type
  prefix check, the validator's format detection) already lives where
  it is used and depends on partial prefixes rather than full types.
* **No version string.** :mod:`nexusdl.version` is the canonical home
  for ``__version__`` and its tuple variant. Adding a third definition
  here would violate the same one-source-of-truth rule this module
  exists to enforce.
* **No ``USER_AGENTS_FILENAME`` / ``SITES_FILENAME`` / other data-file
  names.** Each filename is already public in the module that owns the
  file: :mod:`nexusdl.core.session.user_agents` exposes
  ``USER_AGENTS_FILENAME``, :mod:`nexusdl.core.registry.loader` exposes
  ``SITES_FILENAME``. Adding a second copy here would create the same
  two-source-of-truth hazard described above, for a value with a single
  natural owner.
* **No format constants** (``COMIC_INFO_ENTRY_NAME``,
  ``_FORMAT_EXTENSIONS``). ``COMIC_INFO_ENTRY_NAME`` is public in
  :mod:`nexusdl.core.packaging.comic_info` and used by every packager;
  the format-to-extension map is private to
  :mod:`nexusdl.core.downloader.worker` and :mod:`nexusdl.core.downloader.manager`
  for the reasons their module docstrings document.

Dependency graph (no cycles)::

    (no imports)

This module is a **pure leaf** — zero imports, zero third parties. It
can be imported from anywhere, including from the package's own
``__init__.py`` if that ever needs to expose ``APP_SLUG`` before the
rest of the package is importable.
"""

from __future__ import annotations

from typing import Final


__all__ = [
    # --- Application identity -------------------------------------------- #
    "APP_NAME",
    "APP_SLUG",
    "DATA_PACKAGE",
    # --- Event types ----------------------------------------------------- #
    "EVENT_CHAPTER_COMPLETED",
    "EVENT_CHAPTER_FAILED",
    "EVENT_CHAPTER_STARTED",
    "EVENT_DOWNLOAD_CANCELLED",
    "EVENT_DOWNLOAD_COMPLETED",
    "EVENT_DOWNLOAD_FAILED",
    "EVENT_DOWNLOAD_PAUSED",
    "EVENT_DOWNLOAD_PROGRESS",
    "EVENT_DOWNLOAD_RESUMED",
    "EVENT_DOWNLOAD_STARTED",
    "EVENT_LIBRARY_UPDATED",
    "EVENT_SITE_HEALTH_CHANGED",
]


# --------------------------------------------------------------------------- #
# Application identity                                                        #
# --------------------------------------------------------------------------- #

#: Human-readable application name. The shipped default for
#: :attr:`nexusdl.core.config.AppSection.name`. Rendered in window
#: titles, TUI headers, CLI ``--version`` output, and the ``User-Agent``
#: header where a stable identifier is wanted.
#:
#: A deployment that overrides ``NEXUSDL_APP__NAME`` changes the
#: effective display name (``settings.app.name``) without modifying this
#: constant — the constant is the *default*, not the effective value.
APP_NAME: Final[str] = "NexusDL"

#: Technical identifier of the application. Used as the ``platformdirs``
#: application name for every path computation
#: (``user_data_path(APP_SLUG)``, ``user_cache_path(APP_SLUG)``, …), as
#: the prefix for the ``pyproject.toml`` distribution name, and as the
#: subdirectory name inside ``<user_data_dir>`` that
#: :class:`~nexusdl.plugins.loader.PluginLoader` uses for plugin data.
#:
#: **Invariant.** Unlike :data:`APP_NAME`, this value is not user-
#: overridable. Changing it would orphan every existing installation's
#: files — the data directory, cache directory, and plugin data
#: directory would move, and existing files would no longer be found.
#: The only sanctioned way to change it is a new major release with an
#: accompanying migration that relocates the existing directories.
APP_SLUG: Final[str] = "nexusdl"

#: Python package path from which the pipeline loads its bundled
#: resources. Resolved by :func:`importlib.resources.files` — callers
#: that need a bundled file write
#: ``importlib.resources.files(DATA_PACKAGE) / <filename>`` and get a
#: :class:`~importlib.resources.abc.Traversable` that works identically
#: whether the package is installed as a wheel, imported from a source
#: tree, or running from a ``pip install -e`` checkout.
#:
#: Files bundled in this package (see the tree at
#: :mod:`nexusdl.data`): ``sites.yaml``, ``sites_schema.json``,
#: ``default_config.yaml``, ``user_agents.txt``,
#: ``comic_info_template.xml``, ``plugin_schema.json`` (if it ships in
#: a future release), and the ``translations/`` subdirectory.
DATA_PACKAGE: Final[str] = "nexusdl.data"


# --------------------------------------------------------------------------- #
# Event types                                                                 #
# --------------------------------------------------------------------------- #
# Every event published on the
# :class:`~nexusdl.core.events.EventBus` is named here. A subscriber that
# wants to react to a specific event imports the constant rather than
# retyping the string, so a rename is a single-place edit and a typo is
# a compile error rather than a silent no-match at runtime.
#
# The four "download state" events (started / progress / completed /
# failed) come from the worker and are re-emitted by the manager's
# ``download_manga`` iterator; the three "download lifecycle" events
# (paused / resumed / cancelled) come from the manager. The three
# "chapter" events come from the manager's ``download_manga`` loop. The
# library and site events are emitted by the library database (on
# mutation) and the site registry (on health transition) respectively.
#
# Event names are lowercase, dot-separated, and follow a
# ``<namespace>.<verb>`` convention. A plugin that emits custom events
# must namespace them under ``plugin.<plugin_name>.*`` — the four
# plugin example events in
# :mod:`nexusdl.plugins._example.plugin` are namespaced exactly that way.
# --------------------------------------------------------------------------- #

# --- Download state (worker → bus) ----------------------------------- #

#: Emitted by the worker when a task's pages are fetched and the
#: packaging step begins. Payload includes ``task_id``, ``chapter_id``,
#: ``manga_id``, ``page_count``, and ``format``.
EVENT_DOWNLOAD_STARTED: Final[str] = "download.started"

#: Emitted by the worker once per page, throttled to at most once per
#: progress-reporting interval. Payload includes ``task_id``,
#: ``chapter_id``, ``progress`` (fraction in ``[0.0, 1.0]``), and
#: ``phase`` (``"pages"`` or ``"packaging"``).
EVENT_DOWNLOAD_PROGRESS: Final[str] = "download.progress"

#: Emitted by the worker on success. Payload includes ``task_id``,
#: ``chapter_id``, ``output_path``, ``pages_downloaded``,
#: ``bytes_downloaded``, and ``duration_seconds``.
EVENT_DOWNLOAD_COMPLETED: Final[str] = "download.completed"

#: Emitted by the worker on failure. Payload includes ``task_id``,
#: ``chapter_id``, ``error_type``, ``error_message``, and
#: ``duration_seconds``.
EVENT_DOWNLOAD_FAILED: Final[str] = "download.failed"

# --- Download lifecycle (manager → bus) ------------------------------ #

#: Emitted by the manager when a running task is paused. Payload
#: includes ``task_id`` and ``chapter_id``.
EVENT_DOWNLOAD_PAUSED: Final[str] = "download.paused"

#: Emitted by the manager when a paused task is resumed. Payload
#: includes ``task_id`` and ``chapter_id``.
EVENT_DOWNLOAD_RESUMED: Final[str] = "download.resumed"

#: Emitted by the manager when a pending task is cancelled. Payload
#: includes ``task_id`` and ``chapter_id``.
EVENT_DOWNLOAD_CANCELLED: Final[str] = "download.cancelled"

# --- Chapter-level (manager → bus) ---------------------------------- #

#: Emitted by the manager's ``download_manga`` iterator before a
#: chapter's task is submitted. Payload includes ``manga_id``,
#: ``chapter_id``, and ``task_id``.
EVENT_CHAPTER_STARTED: Final[str] = "chapter.started"

#: Emitted by the manager's ``download_manga`` iterator after a
#: chapter's task completes successfully. Payload includes ``manga_id``,
#: ``chapter_id``, ``task_id``, and ``output_path``.
EVENT_CHAPTER_COMPLETED: Final[str] = "chapter.completed"

#: Emitted by the manager's ``download_manga`` iterator when a
#: chapter's task fails, is cancelled, or cannot be submitted. Payload
#: includes ``manga_id``, ``chapter_id``, and ``error_type`` /
#: ``error_message`` (both ``None`` when the failure is a
#: generator-level cancellation).
EVENT_CHAPTER_FAILED: Final[str] = "chapter.failed"

# --- Library and site ------------------------------------------------ #

#: Emitted by the library database after any write that changes the
#: stored content — a manga or chapter upsert, a reading-progress
#: update, a manga deletion. A subscriber that re-renders the library
#: view on this event does not need to know which write triggered it.
EVENT_LIBRARY_UPDATED: Final[str] = "library.updated"

#: Emitted by the site registry when a health check changes a site's
#: :class:`~nexusdl.core.models.enums.SiteHealth` value from one state
#: to another. Emitted only on the transition, not on every check —
#: a subscriber that updates a UI indicator sees one event per state
#: change, not one per poll. Payload includes ``site_id``,
#: ``previous_status``, and ``new_status``.
EVENT_SITE_HEALTH_CHANGED: Final[str] = "site.health.changed"
