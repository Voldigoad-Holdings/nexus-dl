"""Discovery primitives for the library's forward-only SQL migrations.

This package is a **container**, not a runner. It exposes the small set of
helpers :class:`nexusdl.core.library.database.LibraryDatabase` needs to
locate and read the ``.sql`` files in this directory; it does not apply
migrations, does not talk to SQLite, and does not import SQLAlchemy.

Separation of concerns
----------------------
* **This module** - answers "which migration files exist, in what order,
  and are their names well-formed?" Pure filesystem discovery, no I/O
  beyond listing the package directory.
* **``LibraryDatabase.init()``** - decides which of the discovered files
  still need to be applied (by comparing against its own
  ``schema_version`` record), wraps each in a transaction, and handles
  rollback. The runner's contract is documented at the top of
  ``001_initial.sql``.

Keeping the two apart means the migration files can be loaded into memory,
copied, or validated by a build script (``scripts/migrate_config.py``,
future) without spinning up a database.

Filename contract
-----------------
Every ``.sql`` file in this directory must match::

    <version>_<name>.sql

where:

* ``<version>`` is one or more **ASCII digits** (``0-9``). Leading zeros
  are allowed and ignored (``001`` and ``1`` are the same version).
  Non-ASCII digits (Arabic-Indic, Devanagari, etc.) are rejected even
  though Python's ``int()`` would accept them, because a migration file
  with a non-ASCII version is always a copy-paste accident and would
  silently produce the same version number as its ASCII equivalent.
* ``<name>`` is one or more of ``a-z``, ``0-9``, or ``_``. Lowercase only.
* The extension is exactly ``.sql``.

Files that do not match (including ``__init__.py``, editor backups, and
stray ``.sql~`` files) are **ignored**, not rejected - a build environment
can leave artefacts in the directory without breaking the runner. Only a
``.sql`` file that *looks like* a migration but has a malformed version
(e.g. ``00a_foo.sql``, ``_foo.sql``, ``foo.sql``, ``٠٠١_foo.sql``) is a
hard error, because that almost always means a typo in a real migration.

Two migrations with the same ``<version>`` raise
:class:`~nexusdl.core.exceptions.LibraryError` at discovery time, before
any SQL is executed.

Usage
-----
:class:`~nexusdl.core.library.database.LibraryDatabase` calls:

    from nexusdl.core.library.migrations import list_migrations

    for migration in list_migrations():
        if migration.version > current_version:
            sql = migration.path.read_text(encoding="utf-8")
            await conn.executescript(sql)
            current_version = migration.version

Ordering is guaranteed by :func:`list_migrations`: it returns migrations
sorted by integer version, not by filename. This means a future
``010_something.sql`` sorts after ``009_...`` and ``002_...`` regardless
of how the versions are zero-padded.

Adding a migration
------------------
1. Create ``NNN_short_description.sql`` in this directory.
2. Add it to the test fixture list in
   ``tests/unit/core/library/test_migrations.py`` (future) so the schema
   is rebuilt with the new file.
3. Commit both files in the same change.

No registration, no import, no ``__init__.py`` edit required - discovery is
dynamic by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from nexusdl.core.exceptions import LibraryError


__all__ = [
    "MIGRATIONS_DIR",
    "MigrationFile",
    "list_migrations",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Absolute path to the directory containing this ``__init__.py``, i.e. the
#: directory the migration ``.sql`` files live in. Resolved so that symlinks
#: and relative working directories cannot affect discovery.
MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent

#: Filename pattern for a migration. The two named groups are the only
#: fields the parser needs; ``name`` is captured for error messages even
#: though :class:`MigrationFile` does not store it.
#:
#: ``[0-9]`` is used instead of ``\d`` deliberately: ``\d`` is Unicode-aware
#: in Python 3 and would accept Arabic-Indic, Devanagari, and other
#: non-ASCII digit sets. A migration file named with non-ASCII digits is
#: always a copy-paste accident (the file was probably intended to be
#: ASCII), and would silently produce the same version number as its ASCII
#: equivalent - a real footgun. ``[0-9]`` enforces the "ASCII digits"
#: contract stated in the module docstring.
_MIGRATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<version>[0-9]+)_(?P<name>[a-z0-9_]+)\.sql$"
)


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """A single on-disk migration, discovered and version-parsed.

    Immutable and safe to cache, pass across coroutines, or compare in
    tests. The class deliberately stores only what the runner needs - the
    version number and the path. The descriptive ``<name>`` from the
    filename is not retained; it is used only for error messages during
    discovery.

    Attributes:
        version: Integer schema version. ``001_initial.sql`` yields ``1``;
            ``002_add_reading_progress.sql`` yields ``2``.
        path: Absolute path to the ``.sql`` file. The file exists at the
            time :func:`list_migrations` returned this object; it may be
            removed before the caller reads it, in which case
            :meth:`Path.read_text` raises :class:`FileNotFoundError` as
            usual - this class is not a caching layer.
    """

    version: int
    path: Path


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def list_migrations() -> tuple[MigrationFile, ...]:
    """Return every migration in this directory, sorted by version.

    Scans :data:`MIGRATIONS_DIR` for ``.sql`` files matching the filename
    contract (see the module docstring). Non-``.sql`` files and files whose
    names do not resemble a migration are silently ignored; a ``.sql`` file
    whose name *does* look like a migration but whose version segment is
    malformed is a hard error.

    The result is sorted by integer version, not lexicographically, so that
    ``010_...`` correctly follows ``009_...`` regardless of zero-padding.
    Empty migrations directories are valid and yield an empty tuple - a
    freshly-installed package with no shipped migrations should not be a
    hard error at discovery time.

    Returns:
        A tuple of :class:`MigrationFile`, sorted by ascending ``version``,
        with no duplicates. The tuple is safe to hold across ``await``
        boundaries: it captures the state at call time and subsequent
        directory changes are not reflected until the next call.

    Raises:
        LibraryError: If two migrations share the same integer version, or
            if a file whose name resembles a migration cannot be parsed
            (e.g. ``001-foo.sql`` - hyphen instead of underscore - or
            ``٠٠١_foo.sql`` - non-ASCII digits - both of which almost
            certainly mean a typo in a real migration file). The error
            message names the offending file(s) and, for the duplicate
            case, both versions.
    """
    found: list[MigrationFile] = []
    versions: dict[int, Path] = {}

    for entry in MIGRATIONS_DIR.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".sql":
            continue

        match = _MIGRATION_PATTERN.match(entry.name)
        if match is None:
            raise LibraryError(
                f"malformed migration filename: {entry.name!r}. "
                f"Expected '<version>_<name>.sql' where <version> is "
                f"ASCII digits [0-9] and <name> is [a-z0-9_]+."
            )

        version = int(match.group("version"))
        previous = versions.get(version)
        if previous is not None:
            raise LibraryError(
                f"duplicate migration version {version}: "
                f"{previous.name!r} and {entry.name!r}"
            )
        versions[version] = entry
        found.append(MigrationFile(version=version, path=entry))

    found.sort(key=lambda m: m.version)
    return tuple(found)
