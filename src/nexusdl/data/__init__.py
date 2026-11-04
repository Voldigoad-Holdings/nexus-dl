# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL data package.

This package contains all static data files used by NexusDL:

    - ``sites.yaml``           — supported sites (source of truth)
    - ``sites_schema.json``    — JSON Schema for ``sites.yaml``
    - ``default_config.yaml``  — default runtime configuration
    - ``comic_info_template.xml`` — ComicInfo.xml template
    - ``user_agents.txt``      — User-Agent pool
    - ``translations/*.json``  — i18n strings

It also exposes helpers to read these files **in a wheel-aware way**,
using :mod:`importlib.resources` so the paths work whether the package
is:

    - installed as a wheel
    - installed in editable mode (``pip install -e .``)
    - run from a source checkout
    - zipped as an egg

The package has **no side effects at import** (no I/O, no logging, no
optional-dependency imports). Files are read lazily on demand and cached
with :func:`functools.lru_cache`. The one optional dependency,
PyYAML, is imported **inside** :func:`read_yaml` — so a caller that only
needs :func:`read_text`, :func:`read_json`, or :func:`read_bytes` does
not require PyYAML to be installed.

Example:
    >>> from nexusdl import data
    >>> sites = data.read_yaml("sites.yaml")
    >>> schema = data.read_json("sites_schema.json")
    >>> template = data.read_text("comic_info_template.xml")
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════

#: Package name (used by importlib.resources.files).
_PACKAGE: str = "nexusdl.data"

#: UTF-8 encoding (explicit, cross-platform).
_ENCODING: str = "utf-8"


# ════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ════════════════════════════════════════════════════════════════════


def _resource_traversable(filename: str) -> Traversable:
    """Return a Traversable for a data file (wheel-aware).

    Args:
        filename: Name of the file inside ``nexusdl/data/`` (e.g.
            ``"sites.yaml"``).

    Returns:
        A :class:`Traversable` object that can be opened with
        ``.read_text()``, ``.read_bytes()``, or ``.open()``.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    traversable = files(_PACKAGE) / filename
    if not traversable.is_file():
        # Fallback for editable installs where importlib.resources
        # sometimes fails to resolve the package directory correctly.
        fallback = Path(__file__).parent / filename
        if fallback.is_file():
            return fallback
        raise FileNotFoundError(
            f"Data file not found: {filename!r} "
            f"(looked in package {_PACKAGE!r} and {fallback})"
        )
    return traversable


# ════════════════════════════════════════════════════════════════════
# PUBLIC HELPERS
# ════════════════════════════════════════════════════════════════════


def resource_path(filename: str) -> Path:
    """Return the absolute path to a data file (debug / logging only).

    Args:
        filename: Name of the file inside ``nexusdl/data/``.

    Returns:
        The absolute path to the file on disk.

    Raises:
        FileNotFoundError: If the file does not exist, **or** if the
            package was loaded from a non-filesystem source (a zipped
            wheel) so no real filesystem path exists.

    Note:
        This function returns a real filesystem path. For reading the
        file content, prefer :func:`read_text`, :func:`read_json`, or
        :func:`read_yaml`, which work even in zipped environments.
    """
    traversable = _resource_traversable(filename)

    # The common case: importlib.resources returned a real Path (wheel
    # installed to disk, source checkout, editable install). Just
    # resolve it.
    if isinstance(traversable, Path):
        return traversable.resolve()

    # Rare case: importlib.resources returned a Traversable from a
    # non-filesystem source (zipped wheel). Try to materialise a path;
    # if that fails, raise a clear error rather than returning a bogus
    # path that resolves relative to the caller's CWD.
    candidate = Path(str(traversable))
    if candidate.is_file():
        return candidate.resolve()

    raise FileNotFoundError(
        f"{filename!r} is loaded from a non-filesystem source "
        f"(zipped wheel) — no real filesystem path is available. "
        f"Use read_text()/read_bytes()/read_json()/read_yaml() instead."
    )


@lru_cache(maxsize=32)
def read_text(filename: str) -> str:
    """Read a data file as UTF-8 text.

    Args:
        filename: Name of the file inside ``nexusdl/data/``.

    Returns:
        The file content as a string.

    Raises:
        FileNotFoundError: If the file does not exist.
        UnicodeDecodeError: If the file is not UTF-8.

    Example:
        >>> template = read_text("comic_info_template.xml")
        >>> assert template.startswith("<?xml")
    """
    return _resource_traversable(filename).read_text(encoding=_ENCODING)


@lru_cache(maxsize=32)
def read_json(filename: str) -> Any:
    """Read a data file as parsed JSON.

    Args:
        filename: Name of the file inside ``nexusdl/data/``.

    Returns:
        The parsed JSON object (dict, list, etc.).

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.

    Note:
        The returned object is **shared**: subsequent calls with the
        same ``filename`` return the *same* Python object (thanks to
        :func:`functools.lru_cache`). Do not mutate it — callers that
        need a private copy should ``copy.deepcopy`` the result, or
        call :func:`clear_cache` first.

    Example:
        >>> schema = read_json("sites_schema.json")
        >>> assert schema["$schema"].startswith("https://json-schema.org/")
    """
    return json.loads(read_text(filename))


@lru_cache(maxsize=32)
def read_yaml(filename: str) -> Any:
    """Read a data file as parsed YAML.

    Args:
        filename: Name of the file inside ``nexusdl/data/``.

    Returns:
        The parsed YAML object (dict, list, etc.).

    Raises:
        FileNotFoundError: If the file does not exist.
        ImportError: If PyYAML is not installed. PyYAML is imported
            lazily so that callers who only use :func:`read_text`,
            :func:`read_json`, or :func:`read_bytes` do not need it.
        yaml.YAMLError: If the file is not valid YAML.

    Note:
        The returned object is **shared**: subsequent calls with the
        same ``filename`` return the *same* Python object (thanks to
        :func:`functools.lru_cache`). Do not mutate it — callers that
        need a private copy should ``copy.deepcopy`` the result, or
        call :func:`clear_cache` first.

    Example:
        >>> sites = read_yaml("sites.yaml")
        >>> assert "sites" in sites
    """
    # Lazy import: nexusdl.data must remain importable (and usable for
    # read_text/read_json/read_bytes) in environments where PyYAML is
    # not installed — e.g. a CLI deployment that only renders the
    # ComicInfo template and never touches sites.yaml.
    import yaml  # noqa: PLC0415

    return yaml.safe_load(read_text(filename))


@lru_cache(maxsize=32)
def read_bytes(filename: str) -> bytes:
    """Read a data file as raw bytes.

    Args:
        filename: Name of the file inside ``nexusdl/data/``.

    Returns:
        The raw bytes of the file.

    Raises:
        FileNotFoundError: If the file does not exist.

    Example:
        >>> raw = read_bytes("comic_info_template.xml")
        >>> assert raw.startswith(b"<?xml")
    """
    return _resource_traversable(filename).read_bytes()


def clear_cache() -> None:
    """Clear the internal cache.

    Useful in tests or when data files are modified at runtime.

    Example:
        >>> clear_cache()
    """
    read_text.cache_clear()
    read_json.cache_clear()
    read_yaml.cache_clear()
    read_bytes.cache_clear()


# ════════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════════

__all__ = [
    "resource_path",
    "read_text",
    "read_json",
    "read_yaml",
    "read_bytes",
    "clear_cache",
]
