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

The package has **no side effects at import** (no I/O, no logging).
Files are read lazily on demand and cached with :func:`functools.lru_cache`.

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
from pathlib import Path
from typing import Any

import yaml

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


def _resource_traversable(filename: str) -> Any:
    """Return a Traversable for a data file (wheel-aware).

    Args:
        filename: Name of the file inside ``nexusdl/data/`` (e.g.
            ``"sites.yaml"``).

    Returns:
        A ``Traversable`` object that can be opened with ``.open()``.

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
        FileNotFoundError: If the file does not exist.

    Note:
        This function returns a real filesystem path. For reading the
        file content, prefer :func:`read_text`, :func:`read_json`, or
        :func:`read_yaml`, which work even in zipped environments.
    """
    return Path(str(_resource_traversable(filename))).resolve()


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
    traversable = _resource_traversable(filename)
    if isinstance(traversable, Path):
        return traversable.read_text(encoding=_ENCODING)
    # Traversable from importlib.resources (wheel, zip)
    return traversable.read_text(encoding=_ENCODING)


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
        yaml.YAMLError: If the file is not valid YAML.

    Example:
        >>> sites = read_yaml("sites.yaml")
        >>> assert "sites" in sites
    """
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
    traversable = _resource_traversable(filename)
    if isinstance(traversable, Path):
        return traversable.read_bytes()
    return traversable.read_bytes()


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
