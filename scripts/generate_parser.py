#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL parser generator.

Scaffolds a new parser in 3 steps:

1. Creates the parser file: ``src/nexusdl/parsers/<lang>/<site_id>.py``
2. Creates the smoke test: ``tests/unit/parsers/test_<site_id>_smoke.py``
3. Adds an entry to ``src/nexusdl/data/sites.yaml``

Supports the 6 mixins from ADR-003:

- ``madara``         → WordPress Madara (SushiScan, Scan-Manga, ...)
- ``mangathemesia``  → MangaThemesia (AsuraScans, FlameScans, ...)
- ``foolslide``      → FoolSlide (legacy FR sites)
- ``api``            → REST/GraphQL API (MangaDex, Comick, ...)
- ``cloudflare``     → Cloudflare-protected (nhentai, HentaiZone, ...)
- ``js``             → JS-rendered (React/Vue SPAs)
- ``none``           → Custom (no mixin, implement all 4 methods)

Usage::

    # List available mixins
    python scripts/generate_parser.py --list-mixins

    # Generate a Madara parser for a French site
    python scripts/generate_parser.py \\
        --site-id mysite \\
        --name "MySite" \\
        --language fr \\
        --category fr \\
        --mixin madara \\
        --domain https://mysite.example.com

    # Generate a Cloudflare parser for an adult site
    python scripts/generate_parser.py \\
        --site-id adult-site \\
        --name "Adult Site" \\
        --language en \\
        --category adult \\
        --mixin cloudflare \\
        --adult \\
        --domain https://adult.example.com

    # Dry-run (default): show what would be created
    python scripts/generate_parser.py --site-id mysite ...

    # Actually create (requires --execute)
    python scripts/generate_parser.py --site-id mysite ... --execute

    # Force overwrite existing files
    python scripts/generate_parser.py --site-id mysite ... --execute --force

Exit codes::

    0   Success (or dry-run completed)
    1   Generation failed (file exists, invalid input, ...)
    2   Invalid arguments
    3   Prerequisite missing (sites.yaml, template, ...)
    130 Interrupted (Ctrl+C)

Notes::

    - Defaults to dry-run: pass ``--execute`` to actually write files.
    - Refuses to overwrite existing files unless ``--force`` is passed.
    - Validates ``site_id`` against ``^[a-z][a-z0-9_-]*$``.
    - Checks for duplicate ``site_id`` in ``sites.yaml``.
    - After ``--execute``, runs ``check_imports`` + ``validate_sites``
      (unless ``--no-check``).
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SRC_DIR: Final[Path] = _REPO_ROOT / "src" / "nexusdl"
_PARSERS_DIR: Final[Path] = _SRC_DIR / "parsers"
_SITES_YAML: Final[Path] = _SRC_DIR / "data" / "sites.yaml"
_TESTS_DIR: Final[Path] = _REPO_ROOT / "tests" / "unit" / "parsers"

_SITE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]*$")

_VALID_CATEGORIES: Final[tuple[str, ...]] = ("fr", "en", "kr", "adult", "custom")

_VALID_MIXINS: Final[dict[str, str]] = {
    "madara": "WordPress Madara",
    "mangathemesia": "MangaThemesia",
    "foolslide": "FoolSlide",
    "api": "REST/GraphQL API",
    "cloudflare": "Cloudflare-protected",
    "js": "JS-rendered (React/Vue)",
    "none": "Custom (no mixin)",
}

_LANGUAGE_BY_CATEGORY: Final[dict[str, str]] = {
    "fr": "fr",
    "en": "en",
    "kr": "kr",
    "adult": "en",  # adult sites default to English (override with --language)
    "custom": "en",
}


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class GeneratedFile:
    """A file to generate.

    Attributes:
        path: Absolute path.
        content: File content.
        description: Human-readable description.
    """

    path: Path
    content: str
    description: str


@dataclass
class GenerationReport:
    """Result of a generation run.

    Attributes:
        files: Files generated (or to generate).
        site_id: The site ID.
        category: The category.
        mixin: The mixin.
        dry_run: Whether this was a dry-run.
        errors: Any errors.
        warnings: Any warnings.
    """

    files: list[GeneratedFile] = field(default_factory=list)
    site_id: str = ""
    category: str = ""
    mixin: str = ""
    dry_run: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _find_sites_yaml() -> Path | None:
    """Locate ``sites.yaml`` (repo layout or installed package).

    Returns:
        Path to ``sites.yaml``, or ``None`` if not found.
    """
    candidates = (
        _SITES_YAML,
        _SRC_DIR / "data" / "sites.yaml",
        _REPO_ROOT / "src" / "nexusdl" / "data" / "sites.yaml",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _validate_site_id(site_id: str) -> str | None:
    """Validate a site ID.

    Args:
        site_id: The site ID to validate.

    Returns:
        An error message, or ``None`` if valid.
    """
    if not site_id:
        return "site_id is empty"
    if len(site_id) > 50:
        return f"site_id is too long ({len(site_id)} chars, max 50)"
    if not _SITE_ID_RE.match(site_id):
        return (
            f"invalid site_id '{site_id}': must match '^[a-z][a-z0-9_-]*$' "
            "(lowercase, start with letter, only a-z 0-9 _ -)"
        )
    return None


def _validate_mixin(mixin: str) -> str | None:
    """Validate a mixin name.

    Args:
        mixin: The mixin name.

    Returns:
        An error message, or ``None`` if valid.
    """
    if mixin not in _VALID_MIXINS:
        return (
            f"invalid mixin '{mixin}': must be one of "
            f"{', '.join(_VALID_MIXINS.keys())}"
        )
    return None


def _validate_category(category: str) -> str | None:
    """Validate a category.

    Args:
        category: The category.

    Returns:
        An error message, or ``None`` if valid.
    """
    if category not in _VALID_CATEGORIES:
        return (
            f"invalid category '{category}': must be one of "
            f"{', '.join(_VALID_CATEGORIES)}"
        )
    return None


def _check_coherence(site_id: str, category: str, adult: bool) -> list[str]:
    """Check coherence between category and adult flag.

    Args:
        site_id: The site ID.
        category: The category.
        adult: The adult flag.

    Returns:
        List of warnings (not errors).
    """
    warnings: list[str] = []
    if category == "adult" and not adult:
        warnings.append(
            f"site '{site_id}' is in category 'adult' but --adult is not set. "
            "The parser will be loaded for all users."
        )
    if adult and category != "adult":
        warnings.append(
            f"site '{site_id}' is marked --adult but lives in category '{category}'. "
            "Consider moving it to 'adult/'."
        )
    return warnings


def _read_sites_yaml(path: Path) -> dict:
    """Read and parse ``sites.yaml``.

    Args:
        path: Path to ``sites.yaml``.

    Returns:
        The parsed YAML (as a dict).

    Raises:
        RuntimeError: If the file cannot be parsed.
    """
    try:
        import yaml
    except ImportError:
        raise RuntimeError("PyYAML is required: pip install pyyaml") from None

    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise RuntimeError(f"sites.yaml must be a mapping, got {type(data).__name__}")
        if "sites" not in data or not isinstance(data["sites"], list):
            raise RuntimeError("sites.yaml must have a 'sites:' list")
        return data
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Cannot parse sites.yaml: {exc}") from exc


def _write_sites_yaml(path: Path, data: dict) -> None:
    """Write ``sites.yaml`` back to disk.

    Args:
        path: Path to ``sites.yaml``.
        data: The data to write.

    Raises:
        RuntimeError: If the file cannot be written.
    """
    try:
        import yaml
    except ImportError:
        raise RuntimeError("PyYAML is required: pip install pyyaml") from None

    try:
        content = yaml.safe_dump(
            data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
        # Preserve a trailing newline
        path.write_text(content + "\n", encoding="utf-8")
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot write sites.yaml: {exc}") from exc


def _site_exists_in_yaml(data: dict, site_id: str) -> bool:
    """Return ``True`` if ``site_id`` is already in ``sites.yaml``.

    Args:
        data: The parsed YAML.
        site_id: The site ID.

    Returns:
        ``True`` if the site is already declared.
    """
    sites = data.get("sites", [])
    return any(site.get("id") == site_id for site in sites if isinstance(site, dict))


# ────────────────────────────────────────────────────────────────────
# Template rendering
# ────────────────────────────────────────────────────────────────────

_HEADER_TEMPLATE: Final[str] = '''\
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""{site_name} parser.

Site: {domain}
Language: {language}
Category: {category}
Mixin: {mixin_description}
Generated: {date}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

'''

_MIXIN_IMPORT_TEMPLATE: Final[dict[str, str]] = {
    "madara": "from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin\n",
    "mangathemesia": "from nexusdl.parsers.mixins.mangathemesia import MangaThemesiaMixin\n",
    "foolslide": "from nexusdl.parsers.mixins.foolslide import FoolSlideMixin\n",
    "api": "from nexusdl.parsers.mixins.api_based import ApiBasedMixin\n",
    "cloudflare": "from nexusdl.parsers.mixins.cloudflare import CloudflareMixin\n",
    "js": "from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin\n",
    "none": "",
}

_MIXIN_CLASS_TEMPLATE: Final[dict[str, str]] = {
    "madara": "WordPressMadaraMixin",
    "mangathemesia": "MangaThemesiaMixin",
    "foolslide": "FoolSlideMixin",
    "api": "ApiBasedMixin",
    "cloudflare": "CloudflareMixin",
    "js": "JsRenderedMixin",
    "none": "",
}

_METHODS_TEMPLATE: Final[str] = '''
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> list[SearchResult]:
        """Search mangas matching the query.

        Args:
            query: Search query.
            page: Page number (1-based).

        Returns:
            List of search results.

        Raises:
            ParserError: If the site is unreachable or the HTML is invalid.
        """
        # TODO: implement (or remove if using a mixin)
        raise NotImplementedError

    async def get_manga(self, url_or_id: str) -> Manga:
        """Fetch full manga metadata.

        Args:
            url_or_id: Manga URL or site-specific ID.

        Returns:
            Manga metadata.

        Raises:
            ParserError: If the manga is not found or the HTML is invalid.
        """
        # TODO: implement (or remove if using a mixin)
        raise NotImplementedError

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Fetch the chapter list.

        Args:
            manga: The manga.

        Returns:
            List of chapters, sorted by number (ascending).

        Raises:
            ParserError: If the chapter list cannot be fetched.
        """
        # TODO: implement (or remove if using a mixin)
        raise NotImplementedError

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Fetch the page URLs of a chapter.

        Args:
            chapter: The chapter.

        Returns:
            List of pages, sorted by index (ascending).

        Raises:
            ParserError: If the pages cannot be fetched.
        """
        # TODO: implement (or remove if using a mixin)
        raise NotImplementedError
'''


def _render_parser(
    site_id: str,
    site_name: str,
    language: str,
    category: str,
    mixin: str,
    domain: str,
    adult: bool,
) -> str:
    """Render the parser Python file.

    Args:
        site_id: Site ID (e.g. ``mysite``).
        site_name: Display name (e.g. ``MySite``).
        language: Language code (``en``, ``fr``, ...).
        category: Category folder (``fr``, ``en``, ...).
        mixin: Mixin name (``madara``, ``none``, ...).
        domain: Primary domain URL.
        adult: Adult flag.

    Returns:
        The rendered Python source.
    """
    from datetime import datetime, timezone

    date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    mixin_description = _VALID_MIXINS[mixin]

    parts: list[str] = []

    # Header
    parts.append(
        _HEADER_TEMPLATE.format(
            site_name=site_name,
            domain=domain,
            language=language,
            category=category,
            mixin_description=mixin_description,
            date=date,
        )
    )

    # Mixin import
    parts.append(_MIXIN_IMPORT_TEMPLATE[mixin])

    # Common imports
    parts.append(
        "from nexusdl.parsers.base import BaseParser\n"
        "from nexusdl.parsers.factory import register\n"
    )
    parts.append("\nif TYPE_CHECKING:\n")
    parts.append("    from nexusdl.core.models.chapter import Chapter\n")
    parts.append("    from nexusdl.core.models.manga import Manga\n")
    parts.append("    from nexusdl.core.models.page import Page\n")
    parts.append("    from nexusdl.core.models.search import SearchResult\n")
    parts.append("\n\n")

    # Class declaration
    mixin_class = _MIXIN_CLASS_TEMPLATE[mixin]
    bases = f"{mixin_class}, BaseParser" if mixin_class else "BaseParser"
    class_name = f"{_to_class_name(site_id)}Parser"
    parts.append(f"class {class_name}({bases}):\n")
    parts.append(f'    """Parser for {site_name}."""\n\n')
    parts.append(f'    site_id = "{site_id}"\n')
    parts.append(f'    language = "{language}"\n')
    parts.append(f"    adult = {adult}\n")

    # If no mixin, add method stubs
    if mixin == "none":
        parts.append(_METHODS_TEMPLATE)
    else:
        parts.append(
            f"\n    # Override methods from {mixin_class} only if needed:\n"
            f"    # async def get_pages(self, chapter: Chapter) -> list[Page]:\n"
            f"    #     ...\n"
        )

    # Registration
    parts.append(f'\n\nregister("{site_id}", {class_name})\n')

    return "".join(parts)


def _to_class_name(site_id: str) -> str:
    """Convert ``site_id`` to a ``PascalCase`` class name.

    Args:
        site_id: Site ID (e.g. ``my_site``).

    Returns:
        PascalCase (e.g. ``MySite``).
    """
    parts = re.split(r"[_-]", site_id)
    return "".join(p.capitalize() for p in parts if p)


def _render_smoke_test(
    site_id: str,
    site_name: str,
    domain: str,
) -> str:
    """Render the smoke test file.

    Args:
        site_id: Site ID.
        site_name: Display name.
        domain: Primary domain.

    Returns:
        The rendered Python source.
    """
    class_name = f"{_to_class_name(site_id)}Parser"
    return f'''\
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""{site_name} smoke tests.

These tests hit the real network and are only run with::

    pytest tests/unit/parsers/test_{site_id}_smoke.py -m smoke

They are **not** run on PRs (to avoid hammering the site).
"""

from __future__ import annotations

import pytest

from nexusdl.parsers.{_find_parser_package(site_id)}.{site_id} import {class_name}


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.integration,
    pytest.mark.asyncio,
]


@pytest.mark.skip(reason="Uncomment after implementing the parser")
async def test_search_returns_results(parser: {class_name}) -> None:
    results = await parser.search("one piece")
    assert len(results) > 0
    assert results[0].title


@pytest.mark.skip(reason="Uncomment after implementing the parser")
async def test_get_manga(parser: {class_name}) -> None:
    manga = await parser.get_manga("{domain}/manga/one-piece")
    assert manga.title
    assert manga.url


@pytest.mark.skip(reason="Uncomment after implementing the parser")
async def test_get_pages(parser: {class_name}, sample_chapter) -> None:
    pages = await parser.get_pages(sample_chapter)
    assert len(pages) > 0
    assert pages[0].url.startswith("https://")
'''


def _find_parser_package(site_id: str) -> str:
    """Return the package path (e.g. ``en``) for the given site."""
    for cat in _VALID_CATEGORIES:
        candidate = _PARSERS_DIR / cat / f"{site_id}.py"
        if candidate.exists():
            return cat
    return "en"  # fallback


def _render_sites_yaml_entry(
    site_id: str,
    site_name: str,
    language: str,
    category: str,
    mixin: str,
    domain: str,
    adult: bool,
) -> dict:
    """Render the ``sites.yaml`` entry (as a dict, ready to be inserted).

    Args:
        site_id: Site ID.
        site_name: Display name.
        language: Language code.
        category: Category folder.
        mixin: Mixin name.
        domain: Primary domain URL.
        adult: Adult flag.

    Returns:
        The YAML entry as a dict.
    """
    parser_class = f"nexusdl.parsers.{category}.{site_id}:{_to_class_name(site_id)}Parser"

    return {
        "id": site_id,
        "name": site_name,
        "domains": [domain],
        "parser_class": parser_class,
        "language": language,
        "adult": adult,
        "capabilities": {
            "supports_search": True,
            "supports_manga_info": True,
            "supports_chapters": True,
            "supports_pages": True,
            "supports_download": True,
            "requires_auth": False,
            "requires_cloudflare_bypass": mixin == "cloudflare",
            "max_concurrent_downloads": 4,
            "default_rate_limit_per_second": 2.0,
        },
        "default_headers": {},
        "cookies_required": [],
    }


# ────────────────────────────────────────────────────────────────────
# Generation
# ────────────────────────────────────────────────────────────────────


def generate(
    site_id: str,
    site_name: str,
    language: str,
    category: str,
    mixin: str,
    domain: str,
    adult: bool,
    *,
    dry_run: bool = True,
    force: bool = False,
) -> GenerationReport:
    """Generate the parser, smoke test, and sites.yaml entry.

    Args:
        site_id: Site ID.
        site_name: Display name.
        language: Language code.
        category: Category folder.
        mixin: Mixin name.
        domain: Primary domain URL.
        adult: Adult flag.
        dry_run: If ``True``, do not write files.
        force: If ``True``, overwrite existing files.

    Returns:
        The generation report.
    """
    report = GenerationReport(
        site_id=site_id,
        category=category,
        mixin=mixin,
        dry_run=dry_run,
    )

    # ── Validate ──
    if err := _validate_site_id(site_id):
        report.errors.append(err)
        return report
    if err := _validate_mixin(mixin):
        report.errors.append(err)
        return report
    if err := _validate_category(category):
        report.errors.append(err)
        return report
    if not domain.startswith(("http://", "https://")):
        report.errors.append(f"invalid domain '{domain}': must start with http:// or https://")
        return report

    # ── Coherence ──
    report.warnings.extend(_check_coherence(site_id, category, adult))

    # ── Check sites.yaml ──
    sites_yaml_path = _find_sites_yaml()
    if sites_yaml_path is None:
        report.errors.append("sites.yaml not found in the repository")
        return report

    try:
        sites_data = _read_sites_yaml(sites_yaml_path)
    except RuntimeError as exc:
        report.errors.append(str(exc))
        return report

    if _site_exists_in_yaml(sites_data, site_id):
        report.errors.append(f"site '{site_id}' already exists in sites.yaml")
        return report

    # ── Check parser file ──
    parser_path = _PARSERS_DIR / category / f"{site_id}.py"
    if parser_path.exists() and not force:
        report.errors.append(
            f"parser file already exists: {parser_path.relative_to(_REPO_ROOT)} "
            "(use --force to overwrite)"
        )
        return report

    # ── Check smoke test file ──
    test_path = _TESTS_DIR / f"test_{site_id}_smoke.py"
    if test_path.exists() and not force:
        report.errors.append(
            f"smoke test file already exists: {test_path.relative_to(_REPO_ROOT)} "
            "(use --force to overwrite)"
        )
        return report

    # ── Render parser ──
    parser_content = _render_parser(
        site_id=site_id,
        site_name=site_name,
        language=language,
        category=category,
        mixin=mixin,
        domain=domain,
        adult=adult,
    )
    try:
        ast.parse(parser_content)
    except SyntaxError as exc:
        report.errors.append(f"generated parser has a syntax error: {exc}")
        return report

    report.files.append(
        GeneratedFile(
            path=parser_path,
            content=parser_content,
            description="Parser",
        )
    )

    # ── Render smoke test ──
    smoke_content = _render_smoke_test(site_id, site_name, domain)
    try:
        ast.parse(smoke_content)
    except SyntaxError as exc:
        report.errors.append(f"generated smoke test has a syntax error: {exc}")
        return report

    report.files.append(
        GeneratedFile(
            path=test_path,
            content=smoke_content,
            description="Smoke test",
        )
    )

    # ── Render sites.yaml entry ──
    entry = _render_sites_yaml_entry(
        site_id=site_id,
        site_name=site_name,
        language=language,
        category=category,
        mixin=mixin,
        domain=domain,
        adult=adult,
    )

    # ── Write (or simulate) ──
    if dry_run:
        return report

    # Write parser
    try:
        parser_path.parent.mkdir(parents=True, exist_ok=True)
        parser_path.write_text(parser_content, encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"cannot write parser: {exc}")
        return report

    # Write smoke test
    try:
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(smoke_content, encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"cannot write smoke test: {exc}")
        # Rollback parser
        try:
            parser_path.unlink()
        except OSError:
            pass
        return report

    # Write sites.yaml
    try:
        sites_data["sites"].append(entry)
        _write_sites_yaml(sites_yaml_path, sites_data)
    except RuntimeError as exc:
        report.errors.append(str(exc))
        # Rollback
        try:
            parser_path.unlink()
            test_path.unlink()
        except OSError:
            pass
        return report

    return report


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="generate_parser",
        description="Scaffold a new NexusDL parser (parser + smoke test + sites.yaml).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --list-mixins                              # list mixins
  %(prog)s --site-id mysite --name "MySite" \\\\
              --category en --domain https://mysite.com  # dry-run
  %(prog)s --site-id mysite --name "MySite" \\\\
              --category en --domain https://mysite.com \\\\
              --mixin madara --execute                  # generate
  %(prog)s --site-id mysite ... --execute --force       # overwrite
        """,
    )

    parser.add_argument(
        "--list-mixins",
        action="store_true",
        help="List available mixins and exit",
    )
    parser.add_argument(
        "--site-id",
        default="",
        help="Site ID (lowercase, e.g. 'mysite')",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Display name (e.g. 'MySite')",
    )
    parser.add_argument(
        "--language",
        default="",
        help="Language code (en, fr, kr, ...) — default: category",
    )
    parser.add_argument(
        "--category",
        default="",
        choices=_VALID_CATEGORIES,
        help=f"Category folder: {', '.join(_VALID_CATEGORIES)}",
    )
    parser.add_argument(
        "--mixin",
        default="none",
        choices=list(_VALID_MIXINS.keys()),
        help=f"Mixin to use (default: none). Choices: {', '.join(_VALID_MIXINS.keys())}",
    )
    parser.add_argument(
        "--domain",
        default="",
        help="Primary domain (e.g. https://mysite.com)",
    )
    parser.add_argument(
        "--adult",
        action="store_true",
        help="Mark the site as adult (18+)",
    )
    parser.add_argument(
        "--execute",
        "-x",
        action="store_true",
        help="Actually write files (default: dry-run)",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing files",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="Skip post-generation checks (check_imports, validate_sites)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    return parser


def _print_report(report: GenerationReport) -> None:
    """Print the generation report."""
    print()
    print("═" * 70)
    mode = "DRY RUN" if report.dry_run else "GENERATED"
    print(f"  {mode} — site_id={report.site_id}, mixin={report.mixin}")
    print("═" * 70)

    if report.warnings:
        print()
        print("  Warnings:")
        for w in report.warnings:
            print(f"    ⚠️  {w}")

    if report.errors:
        print()
        print("  Errors:", file=sys.stderr)
        for e in report.errors:
            print(f"    ❌ {e}", file=sys.stderr)
        return

    print()
    print(f"  Files ({len(report.files)}):")
    for f in report.files:
        try:
            rel = f.path.relative_to(_REPO_ROOT)
        except ValueError:
            rel = f.path
        print(f"    • {rel}  ({f.description})")

    print()
    if report.dry_run:
        print("  Run with --execute to actually write these files.")
    else:
        print("  Next steps:")
        print(f"    1. Implement the methods in {report.files[0].path.name}")
        print(f"    2. Validate: python scripts/validate_sites.py")
        print(f"    3. Check:    python scripts/check_imports.py")
        print(f"    4. Test:     pytest {report.files[1].path.name} -m smoke")


def _run_post_checks() -> int:
    """Run check_imports and validate_sites after generation.

    Returns:
        Exit code (0 = OK, 1 = failed).
    """
    scripts = (
        (_REPO_ROOT / "scripts" / "check_imports.py", "check_imports"),
        (_REPO_ROOT / "scripts" / "validate_sites.py", "validate_sites"),
    )
    for script, name in scripts:
        if not script.exists():
            print(f"  ⚠️  {name} not found at {script}", file=sys.stderr)
            continue
        print(f"  → running {name}...")
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(script)],
            cwd=str(_REPO_ROOT),
            check=False,
        )
        if result.returncode != 0:
            print(f"  ❌ {name} failed", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args()

    # ── List mixins ──
    if args.list_mixins:
        print("Available mixins:")
        for name, description in _VALID_MIXINS.items():
            print(f"  {name:<15} {description}")
        return 0

    # ── Validate required args ──
    missing: list[str] = []
    if not args.site_id:
        missing.append("--site-id")
    if not args.name:
        missing.append("--name")
    if not args.category:
        missing.append("--category")
    if not args.domain:
        missing.append("--domain")
    if missing:
        print(f"❌ Missing required arguments: {', '.join(missing)}", file=sys.stderr)
        return 2

    # ── Default language = category (if not overridden) ──
    language = args.language or _LANGUAGE_BY_CATEGORY.get(args.category, "en")

    # ── Generate ──
    report = generate(
        site_id=args.site_id,
        site_name=args.name,
        language=language,
        category=args.category,
        mixin=args.mixin,
        domain=args.domain,
        adult=args.adult,
        dry_run=not args.execute,
        force=args.force,
    )

    _print_report(report)

    if report.errors:
        return 1

    # ── Post-checks (only if files were actually written) ──
    if args.execute and not args.no_check:
        print()
        rc = _run_post_checks()
        if rc != 0:
            return rc

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted", file=sys.stderr)
        raise SystemExit(130) from None
