# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL version information.

This module is the **single source of truth** for the package version.
It is imported by:

    - pyproject.toml (via ``[tool.hatch.version] path = ...``)
    - src/nexusdl/__init__.py
    - src/nexusdl/__main__.py
    - src/nexusdl/interfaces/cli/cli_entry.py (--version)
    - src/nexusdl/interfaces/web/backend/main.py (create_app)
    - docs/source/conf.py (Sphinx)
    - nexus.dl (manifest)

Contract:
    - __version__: SemVer string (e.g. "0.1.0")
    - VERSION: (major, minor, patch) tuple
    - __version_info__: alias for VERSION
    - __release__: full version string (same as __version__ by default)
    - __build__: "dev" | "ci" | "release" (from env NEXUSDL_BUILD, default "dev")
    - __commit__: short git SHA if available, else None
    - __commit_date__: ISO 8601 date of the commit, else None
    - __build_date__: ISO 8601 UTC timestamp of the **module import**
      (best approximation of build time available at runtime; the true
      build time is recorded in the wheel metadata by hatchling)
    - __author__: author name
    - __license__: SPDX identifier
    - __homepage__: project URL
    - __python_requires__: minimum Python version

The module has **no side effects** except:

    - a ``datetime.now()`` call at import time (for ``__build_date__``),
    - two ``git`` subprocess calls (``rev-parse`` and ``log``) wrapped in
      ``contextlib.suppress`` with a 1-second timeout, cached at import.
      If git is unavailable, ``__commit__`` and ``__commit_date__`` are
      ``None`` and the module continues to work.

Example:
    >>> from nexusdl.version import __version__
    >>> print(__version__)
    0.1.0
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from typing import Final

# ════════════════════════════════════════════════════════════════════
# 1. VERSION (single source of truth)
# ════════════════════════════════════════════════════════════════════
# ⚠️  This is the ONLY place where the version is hardcoded.
#     Bump it manually when releasing.
#     `pyproject.toml` reads this file (``[tool.hatch.version] path``).
#     `nexus.dl` MUST match (checked in CI).
__version__: Final[str] = "0.1.0"


# ════════════════════════════════════════════════════════════════════
# 2. VERSION PARSING
# ════════════════════════════════════════════════════════════════════
_SEMVER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def _parse_version(version: str) -> tuple[int, int, int]:
    """Parse a SemVer string into a (major, minor, patch) tuple.

    Args:
        version: A SemVer string (e.g. ``"0.1.0"``).

    Returns:
        The (major, minor, patch) tuple.

    Raises:
        ValueError: If the version is not valid SemVer.

    Example:
        >>> _parse_version("0.1.0")
        (0, 1, 0)
        >>> _parse_version("1.2.3-rc1")
        (1, 2, 3)
    """
    match = _SEMVER_RE.match(version)
    if match is None:
        raise ValueError(f"Invalid SemVer version: {version!r}")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


#: Parsed version tuple (major, minor, patch).
VERSION: Final[tuple[int, int, int]] = _parse_version(__version__)

#: Alias for VERSION.
__version_info__: Final[tuple[int, int, int]] = VERSION

#: Full release string (same as __version__ by default).
__release__: Final[str] = __version__


# ════════════════════════════════════════════════════════════════════
# 3. BUILD METADATA
# ════════════════════════════════════════════════════════════════════
#: Build type: "dev" (local), "ci" (CI), "release" (PyPI/Docker).
__build__: Final[str] = os.environ.get("NEXUSDL_BUILD", "dev")

#: Module import timestamp (UTC, ISO 8601).
#:
#: NOTE: this is the **import time**, not the build time.  The true
#: build time is recorded by hatchling in the wheel metadata
#: (``nexusdl-*.dist-info/METADATA``) and is not available at runtime
#: without reading the wheel.  For a development checkout, import time
#: and build time are equivalent.  For an installed wheel, this value
#: reflects when the module first ran in the current process.
__build_date__: Final[str] = datetime.now(tz=UTC).isoformat(timespec="seconds")


# ════════════════════════════════════════════════════════════════════
# 4. GIT METADATA (optional, no crash if git not available)
# ════════════════════════════════════════════════════════════════════
def _read_git_commit() -> str | None:
    """Return the short git SHA if available, else ``None``.

    Runs ``git rev-parse --short HEAD`` with a 1-second timeout.
    Any failure (git not installed, not a git repository, timeout) is
    silently swallowed and ``None`` is returned.

    Returns:
        The short git SHA (7–8 chars), or ``None``.
    """
    with suppress(subprocess.TimeoutExpired, OSError):
        result = subprocess.run(  # noqa: S603, S607 — trusted input, PATH lookup intentional
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    return None


def _read_git_commit_date() -> str | None:
    """Return the ISO 8601 commit date if available, else ``None``.

    Runs ``git log -1 --format=%cI`` with a 1-second timeout.
    Any failure is silently swallowed and ``None`` is returned.

    Returns:
        The commit date (ISO 8601, UTC), or ``None``.
    """
    with suppress(subprocess.TimeoutExpired, OSError):
        result = subprocess.run(  # noqa: S603, S607 — trusted input, PATH lookup intentional
            ["git", "log", "-1", "--format=%cI"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    return None


#: Short git SHA (or None).
__commit__: Final[str | None] = _read_git_commit()

#: ISO 8601 commit date (or None).
__commit_date__: Final[str | None] = _read_git_commit_date()


# ════════════════════════════════════════════════════════════════════
# 5. PROJECT METADATA
# ════════════════════════════════════════════════════════════════════
__author__: Final[str] = "Voldigoad-Holdings"
__author_email__: Final[str] = "drxenon487@gmail.com"
__license__: Final[str] = "GPL-3.0-or-later"
__homepage__: Final[str] = "https://github.com/Voldigoad-Holdings/nexus-dl"
__repository__: Final[str] = "https://github.com/Voldigoad-Holdings/nexus-dl"
__documentation__: Final[str] = "https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs"
__changelog__: Final[str] = "https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CHANGELOG.md"
__python_requires__: Final[str] = ">=3.12"
__title__: Final[str] = "NexusDL"
__description__: Final[str] = (
    "Multi-site manga, webtoon, comic and doujinshi downloader "
    "with CLI, TUI, GUI and Web interfaces."
)


# ════════════════════════════════════════════════════════════════════
# 6. HELPERS
# ════════════════════════════════════════════════════════════════════


def version_string() -> str:
    """Return a human-readable version string.

    Returns:
        A string like ``"0.1.0 (dev, abc1234)"`` or ``"0.1.0 (dev)"``
        if no git commit is available.
    """
    parts = [__version__, f"({__build__}"]
    if __commit__:
        parts.append(f", {__commit__}")
    parts.append(")")
    return " ".join(parts)


def version_dict() -> dict[str, str | int | list[int] | None]:
    """Return the version info as a dict (for JSON output).

    Returns:
        A dict whose values are JSON-serialisable.  ``version_info`` is
        a three-element list of integers (JSON has no tuple type);
        ``commit`` and ``commit_date`` may be ``None`` when git is
        unavailable.
    """
    return {
        "version": __version__,
        "release": __release__,
        "version_info": list(VERSION),
        "build": __build__,
        "build_date": __build_date__,
        "commit": __commit__,
        "commit_date": __commit_date__,
        "author": __author__,
        "license": __license__,
        "homepage": __homepage__,
        "python_requires": __python_requires__,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
    }


# ════════════════════════════════════════════════════════════════════
# 7. PUBLIC API
# ════════════════════════════════════════════════════════════════════
__all__ = [
    # Version
    "__version__",
    "__version_info__",
    "__release__",
    "VERSION",
    # Build
    "__build__",
    "__build_date__",
    "__commit__",
    "__commit_date__",
    # Project
    "__author__",
    "__author_email__",
    "__license__",
    "__homepage__",
    "__repository__",
    "__documentation__",
    "__changelog__",
    "__python_requires__",
    "__title__",
    "__description__",
    # Helpers
    "version_string",
    "version_dict",
]
