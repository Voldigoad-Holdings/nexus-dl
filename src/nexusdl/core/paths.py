"""Filesystem path computation and setup helpers.

:class:`~nexusdl.core.config.PathsSection` is the **configuration
object**: the fields an operator overrides via ``NEXUSDL_PATHS__*`` or a
user config file. This module is the **computation layer** that sits
behind it, and the helpers every other part of the pipeline calls when
it needs a path.

Two distinct problems this module solves that ``PathsSection`` alone
cannot:

* **Bootstrap.** Loading :class:`~nexusdl.core.config.Settings`
  requires the ``.env`` file, which requires knowing where the working
  directory is, which may require walking up from a subdirectory.
  Something has to compute paths **before** ``Settings`` is available.
  The pure default accessors here (:func:`user_data_dir`,
  :func:`database_file`, …) are that something — they call
  :mod:`platformdirs` directly and return a :class:`pathlib.Path`, no
  config object required.
* **Consistent derivation.** Pydantic's ``default_factory`` does not
  chain between fields — overriding ``PathsSection.user_data_dir``
  does **not** relocate ``cookies_file``, ``database_file``, or
  ``plugins_dir`` (documented in :mod:`nexusdl.core.config`).
  :func:`resolve_paths` computes the entire set from a single base
  directory, so an operator who wants everything under
  ``/mnt/nexusdl`` calls one function and gets eleven paths that agree
  with each other.

Public surface
--------------
* **Pure default accessors** — one function per path, each returning
  the OS-default location. No imports of :mod:`nexusdl.core.config`;
  safe to call from bootstrap code.
* :func:`resolve_paths` — the entire set from a single base directory.
* **Download pipeline derivation** — :func:`manga_dir`,
  :func:`chapter_output_path`, :func:`format_extension`. These match
  :meth:`nexusdl.core.downloader.worker.DownloadWorker._compute_output_path`
  byte-for-byte, so a caller checking whether a chapter is already
  downloaded gets the same path the worker wrote to.
* :func:`ensure_all_directories` — creates every directory the pipeline
  needs, given a :class:`~nexusdl.core.config.Settings`.
* :func:`describe` — returns every path as a display string, for a
  ``nexusdl paths`` diagnostic command.
* :func:`find_env_file` — walks up from a directory looking for the
  ``.env`` file that :class:`~nexusdl.core.config.Settings` will read.

Documented duplication
----------------------
Two constants are duplicated from other modules, for the same reason
the duplication elsewhere is documented rather than eliminated:

* :data:`_FORMAT_EXTENSIONS` — identical to the private
  ``_FORMAT_EXTENSIONS`` in :mod:`nexusdl.core.downloader.worker` and
  :mod:`nexusdl.core.downloader.manager`. Those two modules are already
  delivered and use their own copies; this module provides the
  **canonical** version that a future refactor should route them
  through. Until then, the three copies are guaranteed equal by a
  cross-checking test rather than by a shared import — a change to any
  of them without the others would fail that test.
* The **default paths** themselves — the pure accessors compute
  ``platformdirs.user_data_path("nexusdl") / "downloads"`` etc., which
  is what :class:`~nexusdl.core.config.PathsSection`'s
  ``default_factory`` produces. The two must agree; again, a
  cross-checking test enforces the invariant without forcing
  ``config.py`` to import this module.

Neither duplication can be eliminated without reopening a delivered
file, and neither creates a runtime inconsistency — both are
"cross-checked by test" duplications, not "silently diverging"
duplications.

Excluded by design
------------------
* **No filesystem I/O on import.** Every function is pure; only
  :func:`ensure_all_directories` performs ``mkdir`` (the name says so).
* **No :class:`~nexusdl.core.config.Settings` import at runtime.** The
  :func:`ensure_all_directories` and :func:`describe` signatures
  reference :class:`Settings` under ``TYPE_CHECKING``; the module
  never imports it at runtime, so importing ``paths.py`` costs nothing
  beyond :mod:`platformdirs`.
* **No slug policy invention.** :func:`manga_dir` and
  :func:`chapter_output_path` use raw
  :func:`slugify.slugify` — exactly as the worker does — so the two
  produce identical output for every input. The latent empty-slug bug
  in both (a manga titled ``"!!!"`` produces no slug) is documented
  but **not** fixed here: fixing it requires changing
  :meth:`~nexusdl.core.downloader.worker.DownloadWorker._compute_output_path`
  at the same time, and that module is already delivered.

Dependency graph (no cycles)::

    core.constants         ─┐
    core.models.enums       ├──► core.paths
    core.utils.filesystem   │
    platformdirs, slugify  ─┘
    (core.config only under TYPE_CHECKING)

``core.constants`` is a leaf. ``core.models.enums`` is a leaf.
``core.utils.filesystem`` is a leaf. ``core.config`` is only imported
for type annotations under ``TYPE_CHECKING``, so this module is
importable from the bootstrap phase before ``Settings`` exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import platformdirs
from slugify import slugify

from nexusdl.core.constants import APP_SLUG
from nexusdl.core.models.enums import PackagingFormat
from nexusdl.core.utils.filesystem import ensure_directory

if TYPE_CHECKING:
    from nexusdl.core.config import Settings


__all__ = [
    "cache_dir",
    "chapter_output_path",
    "cookies_file",
    "database_file",
    "describe",
    "downloads_dir",
    "ensure_all_directories",
    "find_env_file",
    "format_extension",
    "logs_dir",
    "manga_dir",
    "plugins_dir",
    "resolve_paths",
    "user_cache_dir",
    "user_config_dir",
    "user_data_dir",
    "user_log_dir",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Subdirectory name for downloaded content under the data directory.
#: Matches :attr:`nexusdl.core.config.StorageSection.downloads_dir`'s
#: default ``platformdirs.user_data_path(APP_SLUG) / "downloads"``.
_DOWNLOADS_SUBDIR: Final[str] = "downloads"

#: Plugin data subdirectory name under the data directory. Matches
#: :meth:`nexusdl.plugins.loader.PluginLoader`'s fallback
#: ``platformdirs.user_data_path("nexusdl") / "plugins"``.
_PLUGINS_SUBDIR: Final[str] = "plugins"

#: Filename of the encrypted cookie store. Matches the
#: :class:`~nexusdl.core.config.PathsSection` default.
_COOKIES_FILENAME: Final[str] = "cookies.enc"

#: Filename of the SQLite library database. Matches the
#: :class:`~nexusdl.core.config.PathsSection` default.
_DATABASE_FILENAME: Final[str] = "library.db"

#: Filename of the ``.env`` file that :class:`~nexusdl.core.config.Settings`
#: reads. Resolved relative to the current working directory by
#: :class:`Settings`; exposed here so a bootstrap diagnostic
#: (:func:`find_env_file`) can look for it.
_ENV_FILENAME: Final[str] = ".env"

#: Extension to append per packaging format. **Duplicated** from the
#: private ``_FORMAT_EXTENSIONS`` in
#: :mod:`nexusdl.core.downloader.worker` and
#: :mod:`nexusdl.core.downloader.manager`; see the module docstring's
#: "Documented duplication" section. A change here without a matching
#: change in those modules (or vice versa) would fail the cross-check
#: test that guards the three copies.
_FORMAT_EXTENSIONS: Final[dict[PackagingFormat, str]] = {
    PackagingFormat.FOLDER: "",
    PackagingFormat.ZIP: ".zip",
    PackagingFormat.CBZ: ".cbz",
    PackagingFormat.CB7: ".cb7",
    PackagingFormat.PDF: ".pdf",
    PackagingFormat.CBT: ".tar",
}


# --------------------------------------------------------------------------- #
# Pure default accessors (no Settings required)                              #
# --------------------------------------------------------------------------- #
# Each function calls ``platformdirs`` directly, which computes the
# per-OS default. They are individually named rather than consolidated
# into a single accessor because every caller wants exactly one of
# them — a caller that needs the database path does not want to build
# the entire dict just to read one field.
# --------------------------------------------------------------------------- #


def user_data_dir() -> Path:
    """Return the default persistent data directory for this user.

    Returns:
        ``platformdirs.user_data_path(APP_SLUG)`` — the OS-specific
        standard location for application data.
    """
    return platformdirs.user_data_path(APP_SLUG)


def user_config_dir() -> Path:
    """Return the default configuration directory for this user.

    Returns:
        ``platformdirs.user_config_path(APP_SLUG)``.
    """
    return platformdirs.user_config_path(APP_SLUG)


def user_cache_dir() -> Path:
    """Return the default cache directory for this user.

    Returns:
        ``platformdirs.user_cache_path(APP_SLUG)``.
    """
    return platformdirs.user_cache_path(APP_SLUG)


def user_log_dir() -> Path:
    """Return the default log directory for this user.

    Returns:
        ``platformdirs.user_log_path(APP_SLUG)``.
    """
    return platformdirs.user_log_path(APP_SLUG)


def downloads_dir() -> Path:
    """Return the default root directory for downloaded chapters.

    Returns:
        ``user_data_dir() / "downloads"``.
    """
    return user_data_dir() / _DOWNLOADS_SUBDIR


def cookies_file() -> Path:
    """Return the default path of the encrypted cookie store.

    Returns:
        ``user_data_dir() / "cookies.enc"``.
    """
    return user_data_dir() / _COOKIES_FILENAME


def database_file() -> Path:
    """Return the default path of the SQLite library database.

    Returns:
        ``user_data_dir() / "library.db"``.
    """
    return user_data_dir() / _DATABASE_FILENAME


def cache_dir() -> Path:
    """Return the default root directory for the HTTP cache.

    Returns:
        ``user_cache_dir()`` — the ``HttpCache`` instance is constructed
        against a subdirectory (``cache_dir() / "http"``) by the
        composition root.
    """
    return user_cache_dir()


def logs_dir() -> Path:
    """Return the default root directory for log files.

    Returns:
        ``user_log_dir()``.
    """
    return user_log_dir()


def plugins_dir() -> Path:
    """Return the default root directory for user-installed plugins.

    Returns:
        ``user_data_dir() / "plugins"``.
    """
    return user_data_dir() / _PLUGINS_SUBDIR


# --------------------------------------------------------------------------- #
# Consistent resolution (relocate everything from one base)                  #
# --------------------------------------------------------------------------- #


def resolve_paths(base_dir: Path | None = None) -> dict[str, Path]:
    """Compute a consistent set of paths, optionally relocated under *base_dir*.

    The entire set is derived from a single base directory, so overriding
    one path relocates the others consistently — the behaviour
    :class:`~nexusdl.core.config.PathsSection`'s ``default_factory``
    cannot provide (see the module docstring's "Consistent derivation"
    note).

    When *base_dir* is ``None``, the paths match
    :class:`~nexusdl.core.config.PathsSection`'s defaults exactly. When
    it is provided, every path is derived from it::

        resolve_paths(Path("/mnt/nexusdl")) == {
            "user_data_dir":   Path("/mnt/nexusdl/data"),
            "user_config_dir": Path("/mnt/nexusdl/config"),
            "user_cache_dir":  Path("/mnt/nexusdl/cache"),
            "user_log_dir":    Path("/mnt/nexusdl/log"),
            "downloads_dir":   Path("/mnt/nexusdl/data/downloads"),
            "cookies_file":    Path("/mnt/nexusdl/data/cookies.enc"),
            "database_file":   Path("/mnt/nexusdl/data/library.db"),
            "cache_dir":       Path("/mnt/nexusdl/cache"),
            "logs_dir":        Path("/mnt/nexusdl/log"),
            "plugins_dir":     Path("/mnt/nexusdl/data/plugins"),
        }

    The dict has the same keys as :class:`~nexusdl.core.config.PathsSection`'s
    fields, plus ``downloads_dir`` (which lives on
    :class:`~nexusdl.core.config.StorageSection` in the config object).
    This makes the result directly usable with :func:`ensure_all_directories`
    and :func:`describe`.

    Args:
        base_dir: Optional root directory under which to relocate every
            path. ``None`` uses the platformdirs-computed defaults,
            which are the same as a fresh :class:`Settings().paths`.

    Returns:
        A new mapping from path name to :class:`pathlib.Path`. The
        caller owns the dict; mutating it does not affect any internal
        state.
    """
    if base_dir is None:
        # Defaults — match PathsSection's default_factory exactly.
        data = user_data_dir()
        config = user_config_dir()
        cache = user_cache_dir()
        log = user_log_dir()
    else:
        # Relocated — every path derives from the single base.
        data = base_dir / "data"
        config = base_dir / "config"
        cache = base_dir / "cache"
        log = base_dir / "log"

    return {
        "user_data_dir": data,
        "user_config_dir": config,
        "user_cache_dir": cache,
        "user_log_dir": log,
        "downloads_dir": data / _DOWNLOADS_SUBDIR,
        "cookies_file": data / _COOKIES_FILENAME,
        "database_file": data / _DATABASE_FILENAME,
        "cache_dir": cache,
        "logs_dir": log,
        "plugins_dir": data / _PLUGINS_SUBDIR,
    }


# --------------------------------------------------------------------------- #
# Download pipeline derivation                                                #
# --------------------------------------------------------------------------- #


def format_extension(format_: PackagingFormat) -> str:
    """Return the file extension the worker appends for *format_*.

    Matches :meth:`nexusdl.core.downloader.worker.DownloadWorker._compute_output_path`
    exactly — see the module docstring's "Documented duplication"
    section. ``FOLDER`` produces an empty extension (the target is a
    directory, not a file); ``CBT`` produces ``".tar"`` even though no
    ``CbtPackager`` ships in this release, so a caller that computes a
    path for a future CBT packager already produces the correct
    extension.

    Args:
        format_: The packaging format.

    Returns:
        The extension including the leading dot, or ``""`` for
        ``FOLDER``.

    Raises:
        ValueError: If *format_* is not a known
            :class:`~nexusdl.core.models.enums.PackagingFormat`. All
            enum members are covered today; the raise exists for the
            future case where a new member is added to the enum without
            a matching extension.
    """
    if format_ not in _FORMAT_EXTENSIONS:
        raise ValueError(
            f"unknown PackagingFormat {format_!r}; "
            f"known values: {sorted(_FORMAT_EXTENSIONS)}"
        )
    return _FORMAT_EXTENSIONS[format_]


def manga_dir(downloads_root: Path, manga_title: str) -> Path:
    """Return the directory the worker writes *manga_title*'s chapters to.

    Byte-for-byte identical to the path
    :meth:`~nexusdl.core.downloader.worker.DownloadWorker._compute_output_path`
    constructs from the same ``(downloads_dir, manga.title)`` pair, so a
    caller using this helper to test whether a manga is already
    downloaded gets the path the worker actually created.

    Uses :func:`slugify.slugify` with no ``max_length``, matching the
    worker's behaviour exactly. Two latent bugs in the worker's version
    — an empty slug for an all-punctuation title, and no filename
    truncation for very long titles — are **inherited** here, because
    fixing them requires changing the worker's method at the same time
    (see the module docstring's "Excluded by design" section). The
    eventual migration to :func:`nexusdl.core.utils.text.slugify_safe`
    fixes both, and should be applied to the worker and to this helper
    in one commit.

    Args:
        downloads_root: The root directory under which mangas are
            stored. Typically :attr:`Settings.storage.downloads_dir`.
        manga_title: The manga's title, slugified internally.

    Returns:
        ``downloads_root / slugify(manga_title)``.
    """
    return downloads_root / slugify(manga_title)


def chapter_output_path(
    manga_directory: Path,
    chapter_title: str,
    format_: PackagingFormat,
) -> Path:
    """Return the final path a chapter will be written to.

    Constructs ``manga_directory / f"{slugify(chapter_title)}{ext}"``
    where *ext* is :func:`format_extension`'s value — identical to the
    last two lines of the worker's ``_compute_output_path``. A caller
    that wants to skip a download because the target already exists
    calls ``chapter_output_path(...).exists()`` and gets the same
    answer the manager's ``_is_already_downloaded`` would.

    Args:
        manga_directory: The manga's directory, as returned by
            :func:`manga_dir` or read from a path helper.
        chapter_title: The chapter's title, slugified internally.
        format_: The packaging format, driving the extension.

    Returns:
        The destination path the worker will produce.
    """
    extension = format_extension(format_)
    return manga_directory / f"{slugify(chapter_title)}{extension}"


# --------------------------------------------------------------------------- #
# Setup helpers                                                               #
# --------------------------------------------------------------------------- #


def ensure_all_directories(settings: Settings) -> None:
    """Create every directory the pipeline uses, if it does not exist.

    Iterates the directories referenced by *settings* and calls
    :func:`nexusdl.core.utils.filesystem.ensure_directory` on each. For
    the two file paths (``cookies_file``, ``database_file``), the
    **parent** directory is created — creating a directory whose name
    ends in ``.enc`` or ``.db`` would be a bug, since the file is
    written by the corresponding service, not by this helper.

    Idempotent — a second call is a no-op. Intended for the composition
    root's startup sequence, right after :class:`Settings` is loaded::

        settings = Settings()
        configure_logging_from_settings(settings)
        ensure_all_directories(settings)

    Args:
        settings: The application settings. Only ``settings.paths`` and
            ``settings.storage.downloads_dir`` are read.

    Raises:
        OSError: If a directory cannot be created — a read-only
            filesystem, a permission error, or a path component that
            exists as a regular file.
    """
    # Directory paths — created in full.
    for directory in (
        settings.paths.user_data_dir,
        settings.paths.user_config_dir,
        settings.paths.user_cache_dir,
        settings.paths.user_log_dir,
        settings.paths.downloads_dir,
        settings.paths.cache_dir,
        settings.paths.logs_dir,
        settings.paths.plugins_dir,
        settings.storage.downloads_dir,
    ):
        ensure_directory(directory)

    # File paths — create the parent only.
    for file_path in (
        settings.paths.cookies_file,
        settings.paths.database_file,
    ):
        ensure_directory(file_path.parent)


def describe(settings: Settings) -> dict[str, str]:
    """Return every path the pipeline uses, as display strings.

    Intended for a ``nexusdl paths`` diagnostic command. Values are
    rendered with :func:`str` so the result is directly printable; a
    caller that needs the ``Path`` objects uses
    :func:`resolve_paths` or reads the fields directly from
    :class:`~nexusdl.core.config.PathsSection`.

    The keys are the field names a user would override via
    ``NEXUSDL_PATHS__<FIELD>`` — matching
    :class:`~nexusdl.core.config.PathsSection`'s fields plus
    ``downloads_dir`` (which lives on ``settings.storage`` in the
    config object).

    Args:
        settings: The application settings.

    Returns:
        An ordered mapping from path name to its string form. The order
        matches :func:`resolve_paths`'s keys, so two calls produce
        diffable output.
    """
    return {
        "user_data_dir": str(settings.paths.user_data_dir),
        "user_config_dir": str(settings.paths.user_config_dir),
        "user_cache_dir": str(settings.paths.user_cache_dir),
        "user_log_dir": str(settings.paths.user_log_dir),
        "downloads_dir": str(settings.storage.downloads_dir),
        "cookies_file": str(settings.paths.cookies_file),
        "database_file": str(settings.paths.database_file),
        "cache_dir": str(settings.paths.cache_dir),
        "logs_dir": str(settings.paths.logs_dir),
        "plugins_dir": str(settings.paths.plugins_dir),
    }


# --------------------------------------------------------------------------- #
# Bootstrap helpers                                                           #
# --------------------------------------------------------------------------- #


def find_env_file(start_dir: Path | None = None) -> Path | None:
    """Walk up from *start_dir* looking for a ``.env`` file.

    :class:`~nexusdl.core.config.Settings` resolves ``.env`` relative to
    the **current working directory**. A user who runs ``nexusdl`` from
    a project subdirectory (``~/projects/my-manga-list/scripts/``) does
    not get their parent ``.env``, because it is not on the path
    pydantic-settings checks. This helper reproduces the ``.env`` lookup
    a caller would otherwise have to write by hand — the same pattern
    ``git`` uses for its ``.git`` directory — so a bootstrap command
    (``nexusdl admin init``) can find the file if it exists.

    **The caller is responsible for actually using the result.**
    ``Settings`` does not accept an explicit ``.env`` path after
    construction — a caller that finds a ``.env`` in a parent directory
    either changes the working directory or sets the three secrets via
    environment variables directly. This helper exists so that a
    bootstrap command can print ``"using .env from /home/user/.env"``
    and a diagnostic command can show what would be found.

    Args:
        start_dir: The directory to start walking up from. Defaults to
            :func:`pathlib.Path.cwd`.

    Returns:
        The path to the closest ``.env`` file at or above *start_dir*,
        or ``None`` when no ``.env`` exists along the parent chain.
    """
    current = (start_dir or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        env_path = candidate / _ENV_FILENAME
        if env_path.is_file():
            return env_path
    return None
