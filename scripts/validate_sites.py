#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL site configuration validator.

Validates ``sites.yaml`` against:

1. **JSON Schema** (``sites_schema.json``) — structure & types
2. **Business rules** (ADR-002, ADR-004):
   - No CBR format
   - No secrets (``password``, ``token``, ``api_key``, ``secret``)
   - No production URLs (``nexusdl.dev``, ``api.nexusdl.*``)
3. **Cross-checks** with parser files (``--check-parsers``):
   - ``parser_class`` imports successfully
   - ``site_id``, ``language``, ``adult`` match the parser class
4. **Duplicate detection**:
   - Two sites with the same ``id``
   - Two sites with the same primary domain

Usage::

    # Validate sites.yaml (default)
    python scripts/validate_sites.py

    # Verbose (site by site)
    python scripts/validate_sites.py --verbose

    # Strict (warnings = errors)
    python scripts/validate_sites.py --strict

    # JSON output (for CI)
    python scripts/validate_sites.py --json

    # Cross-check parser files
    python scripts/validate_sites.py --check-parsers

    # Summary by language/adult
    python scripts/validate_sites.py --summary

Exit codes::

    0   Valid (or no warnings with --strict)
    1   Invalid (violations found)
    2   Invalid arguments
    3   Prerequisite missing (schema, yaml, jsonschema)
    130 Interrupted (Ctrl+C)

Notes::

    - The schema is at ``src/nexusdl/data/sites_schema.json``.
    - ``sites.yaml`` is at ``src/nexusdl/data/sites.yaml``.
    - This script is run by pre-commit and CI on every change to
      ``sites.yaml`` or the schema.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SRC_DIR: Final[Path] = _REPO_ROOT / "src" / "nexusdl"
_DATA_DIR: Final[Path] = _SRC_DIR / "data"

#: Candidate paths for sites.yaml
_SITES_YAML_CANDIDATES: Final[tuple[Path, ...]] = (
    _DATA_DIR / "sites.yaml",
    _DATA_DIR / "sites.yml",
)

#: Candidate paths for sites_schema.json
_SCHEMA_CANDIDATES: Final[tuple[Path, ...]] = (
    _DATA_DIR / "sites_schema.json",
)

#: Forbidden keys (secrets) — ADR-004
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api_secret",
        "credential",
        "credentials",
        "private_key",
        "access_key",
    }
)

#: Forbidden values (prod URLs, formats) — ADR-002, ADR-004
_FORBIDDEN_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("NexusDL prod URL", re.compile(r"https?://(?:api\.)?nexusdl\.(?:dev|com|org|io)")),
    ("CBR format", re.compile(r"^\s*CBR\s*$", re.IGNORECASE)),
    ("Hardcoded password value", re.compile(r"""["']password["']\s*:\s*["'][^"']+["']""", re.IGNORECASE)),
)


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class Violation:
    """A single validation violation.

    Attributes:
        site_id: The site ID (or ``<root>`` for global issues).
        path: The JSON path within the site (e.g. ``capabilities.supports_search``).
        message: Human-readable message.
        severity: ``error`` or ``warning``.
        suggestion: Optional fix.
    """

    site_id: str
    path: str
    message: str
    severity: str = "error"
    suggestion: str | None = None

    def __str__(self) -> str:
        loc = f"{self.site_id}:{self.path}" if self.path else self.site_id
        return f"{loc}: {self.message}"


@dataclass
class ValidationReport:
    """Aggregated validation report."""

    site_count: int = 0
    errors: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    by_language: Counter[str] = field(default_factory=Counter)
    by_adult: Counter[str] = field(default_factory=Counter)
    schemas_checked: int = 0
    parsers_checked: int = 0

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def add(self, v: Violation) -> None:
        if v.severity == "error":
            self.errors.append(v)
        else:
            self.warnings.append(v)


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _find_first(candidates: tuple[Path, ...], name: str) -> Path | None:
    """Return the first existing path, or ``None``."""
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file (empty file → ``{}``).

    Args:
        path: Path to the YAML file.

    Returns:
        The parsed content (``{}`` if empty).

    Raises:
        RuntimeError: On parse error.
    """
    try:
        import yaml
    except ImportError:
        raise RuntimeError("PyYAML is required: pip install pyyaml") from None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc

    if not text.strip():
        return {}

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"cannot parse {path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a mapping, got {type(data).__name__}")
    return data


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        The parsed content.

    Raises:
        RuntimeError: On parse error.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cannot parse {path}: {exc}") from exc


def _check_jsonschema() -> None:
    """Exit with code 3 if ``jsonschema`` is not installed.

    Raises:
        SystemExit: If missing.
    """
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        print(
            "❌ 'jsonschema' is required: pip install jsonschema",
            file=sys.stderr,
        )
        sys.exit(3)


def _format_path(path: list[Any]) -> str:
    """Format a JSON path (``["sites", 0, "id"]`` → ``sites[0].id``)."""
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            if result:
                result += "."
            result += str(part)
    return result


# ────────────────────────────────────────────────────────────────────
# Validation: schema
# ────────────────────────────────────────────────────────────────────


def _validate_schema(
    data: dict[str, Any],
    schema: dict[str, Any],
    report: ValidationReport,
) -> None:
    """Validate ``data`` against ``schema`` with readable errors.

    Args:
        data: The parsed ``sites.yaml``.
        schema: The parsed ``sites_schema.json``.
        report: The report to populate.
    """
    import jsonschema

    report.schemas_checked = 1

    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        path = _format_path(list(error.absolute_path))

        # Extract site_id from path if available
        site_id = "<root>"
        for i, part in enumerate(error.absolute_path):
            if part == "sites" and i + 1 < len(error.absolute_path):
                idx = error.absolute_path[i + 1]
                if isinstance(idx, int):
                    sites = data.get("sites", [])
                    if 0 <= idx < len(sites) and isinstance(sites[idx], dict):
                        site_id = sites[idx].get("id", f"<site[{idx}]>")
                break

        report.add(
            Violation(
                site_id=site_id,
                path=path,
                message=error.message,
                severity="error",
            )
        )


# ────────────────────────────────────────────────────────────────────
# Validation: business rules
# ────────────────────────────────────────────────────────────────────


def _collect_sites(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of sites (valid dicts only)."""
    sites = data.get("sites", [])
    if not isinstance(sites, list):
        return []
    return [s for s in sites if isinstance(s, dict)]


def _validate_business_rules(
    data: dict[str, Any],
    report: ValidationReport,
) -> None:
    """Validate business rules (ADR-002, ADR-004).

    Args:
        data: The parsed ``sites.yaml``.
        report: The report to populate.
    """
    sites = _collect_sites(data)

    # ── Duplicate IDs ──
    seen_ids: dict[str, int] = {}
    for i, site in enumerate(sites):
        site_id = site.get("id", "")
        if not isinstance(site_id, str):
            continue
        if site_id in seen_ids:
            report.add(
                Violation(
                    site_id=site_id,
                    path=f"sites[{i}].id",
                    message=f"duplicate site ID (first at index {seen_ids[site_id]})",
                    severity="error",
                    suggestion="Rename one of the duplicate IDs.",
                )
            )
        else:
            seen_ids[site_id] = i

    # ── Duplicate primary domains ──
    seen_domains: dict[str, str] = {}
    for site in sites:
        site_id = site.get("id", "<unknown>")
        domains = site.get("domains", [])
        if not isinstance(domains, list) or not domains:
            continue
        primary = domains[0]
        if not isinstance(primary, str):
            continue
        normalized = primary.rstrip("/").lower()
        if normalized in seen_domains and seen_domains[normalized] != site_id:
            report.add(
                Violation(
                    site_id=str(site_id),
                    path="domains[0]",
                    message=(
                        f"duplicate primary domain '{primary}' "
                        f"(also used by '{seen_domains[normalized]}')"
                    ),
                    severity="error",
                    suggestion="Use distinct primary domains per site.",
                )
            )
        else:
            seen_domains[normalized] = str(site_id)

    # ── Per-site checks ──
    for site in sites:
        site_id = str(site.get("id", "<unknown>"))

        # Forbidden keys (secrets)
        for key in _walk_keys(site):
            if key.lower() in _FORBIDDEN_KEYS:
                report.add(
                    Violation(
                        site_id=site_id,
                        path=key,
                        message=f"forbidden key '{key}' (secrets must be in .env, see ADR-004)",
                        severity="error",
                        suggestion="Remove the key and use NEXUSDL_<SITE>_<NAME> in .env.",
                    )
                )

        # Forbidden values (prod URLs, CBR, hardcoded passwords)
        for key, value in _walk_items(site):
            if not isinstance(value, str):
                continue
            for label, pattern in _FORBIDDEN_PATTERNS:
                if pattern.search(value):
                    report.add(
                        Violation(
                            site_id=site_id,
                            path=key,
                            message=f"{label} detected: '{value[:50]}'",
                            severity="error",
                            suggestion=(
                                "CBR is not supported (see ADR-002)."
                                if label == "CBR format"
                                else "Use .env for secrets/URLs (see ADR-004)."
                            ),
                        )
                    )

        # Empty domains
        domains = site.get("domains", [])
        if isinstance(domains, list) and not domains:
            report.add(
                Violation(
                    site_id=site_id,
                    path="domains",
                    message="'domains' is empty",
                    severity="error",
                    suggestion="Add at least one domain URL.",
                )
            )

        # Adult sites in non-adult language (warning)
        language = site.get("language", "")
        adult = site.get("adult", False)
        if adult and language not in ("adult",):
            report.add(
                Violation(
                    site_id=site_id,
                    path="adult",
                    message=f"adult=true but language='{language}'",
                    severity="warning",
                    suggestion="Consider moving the site to 'adult/'.",
                )
            )

        # Track language/adult stats
        if isinstance(language, str) and language:
            report.by_language[language] += 1
        report.by_adult["adult" if adult else "general"] += 1

    report.site_count = len(sites)


def _walk_keys(obj: Any, prefix: str = "") -> list[str]:
    """Recursively collect all keys in a nested dict."""
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            keys.append(path)
            keys.extend(_walk_keys(v, path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            keys.extend(_walk_keys(item, f"{prefix}[{i}]"))
    return keys


def _walk_items(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Recursively collect all (path, value) pairs in a nested dict."""
    items: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            items.append((path, v))
            items.extend(_walk_items(v, path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            items.extend(_walk_items(item, f"{prefix}[{i}]"))
    return items


# ────────────────────────────────────────────────────────────────────
# Validation: parser cross-checks
# ────────────────────────────────────────────────────────────────────


def _validate_parsers(
    data: dict[str, Any],
    report: ValidationReport,
) -> None:
    """Check that each ``parser_class`` exists and matches ``sites.yaml``.

    Args:
        data: The parsed ``sites.yaml``.
        report: The report to populate.
    """
    import importlib
    import importlib.util

    sites = _collect_sites(data)

    for site in sites:
        site_id = str(site.get("id", "<unknown>"))
        parser_class = site.get("parser_class", "")

        if not isinstance(parser_class, str) or ":" not in parser_class:
            report.add(
                Violation(
                    site_id=site_id,
                    path="parser_class",
                    message=f"invalid format: '{parser_class}' (expected 'module:Class')",
                    severity="error",
                    suggestion="Use 'nexusdl.parsers.<lang>.<site>:<Class>Parser'.",
                )
            )
            continue

        module_path, _, class_name = parser_class.partition(":")

        # Check module exists
        try:
            spec = importlib.util.find_spec(module_path)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None

        if spec is None:
            report.add(
                Violation(
                    site_id=site_id,
                    path="parser_class",
                    message=f"module '{module_path}' not found",
                    severity="error",
                    suggestion=f"Create 'src/{module_path.replace('.', '/')}.py'.",
                )
            )
            continue

        # Import and check class
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001
            report.add(
                Violation(
                    site_id=site_id,
                    path="parser_class",
                    message=f"cannot import '{module_path}': {exc}",
                    severity="error",
                )
            )
            continue

        parser_cls = getattr(module, class_name, None)
        if parser_cls is None:
            report.add(
                Violation(
                    site_id=site_id,
                    path="parser_class",
                    message=f"class '{class_name}' not found in '{module_path}'",
                    severity="error",
                )
            )
            continue

        # Cross-check attributes
        if getattr(parser_cls, "site_id", None) != site_id:
            report.add(
                Violation(
                    site_id=site_id,
                    path="id",
                    message=(
                        f"site_id mismatch: sites.yaml='{site_id}', "
                        f"parser='{getattr(parser_cls, 'site_id', None)}'"
                    ),
                    severity="error",
                )
            )

        yaml_lang = site.get("language")
        parser_lang = getattr(parser_cls, "language", None)
        if yaml_lang and parser_lang and str(yaml_lang) != str(parser_lang):
            report.add(
                Violation(
                    site_id=site_id,
                    path="language",
                    message=f"language mismatch: yaml='{yaml_lang}', parser='{parser_lang}'",
                    severity="warning",
                )
            )

        yaml_adult = site.get("adult", False)
        parser_adult = getattr(parser_cls, "adult", False)
        if bool(yaml_adult) != bool(parser_adult):
            report.add(
                Violation(
                    site_id=site_id,
                    path="adult",
                    message=f"adult mismatch: yaml={yaml_adult}, parser={parser_adult}",
                    severity="error",
                )
            )

        report.parsers_checked += 1


# ────────────────────────────────────────────────────────────────────
# Report
# ────────────────────────────────────────────────────────────────────


def _print_report(
    report: ValidationReport,
    *,
    verbose: bool,
    summary: bool,
    strict: bool,
    as_json: bool,
) -> None:
    """Print the validation report."""
    if as_json:
        data = {
            "valid": report.is_valid and (not strict or not report.warnings),
            "site_count": report.site_count,
            "errors": [
                {
                    "site_id": v.site_id,
                    "path": v.path,
                    "message": v.message,
                    "suggestion": v.suggestion,
                }
                for v in report.errors
            ],
            "warnings": [
                {
                    "site_id": v.site_id,
                    "path": v.path,
                    "message": v.message,
                    "suggestion": v.suggestion,
                }
                for v in report.warnings
            ],
            "by_language": dict(report.by_language),
            "by_adult": dict(report.by_adult),
            "schemas_checked": report.schemas_checked,
            "parsers_checked": report.parsers_checked,
        }
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    # ── Errors ──
    if report.errors:
        print(f"\n❌ {len(report.errors)} error(s):", file=sys.stderr)
        for v in report.errors:
            print(f"  • {v}", file=sys.stderr)
            if verbose and v.suggestion:
                print(f"    → {v.suggestion}", file=sys.stderr)

    # ── Warnings ──
    if report.warnings and (verbose or strict):
        label = "❌" if strict else "⚠️ "
        print(f"\n{label} {len(report.warnings)} warning(s):", file=sys.stderr)
        for v in report.warnings:
            print(f"  • {v}", file=sys.stderr)
            if verbose and v.suggestion:
                print(f"    → {v.suggestion}", file=sys.stderr)

    # ── Success ──
    if not report.errors and (not strict or not report.warnings):
        extra = ""
        if report.parsers_checked:
            extra += f", {report.parsers_checked} parser(s) checked"
        if report.schemas_checked:
            extra += f", {report.schemas_checked} schema(s) checked"
        print(f"✅ sites.yaml valid — {report.site_count} site(s) declared{extra}.")

    # ── Summary ──
    if summary:
        print()
        print("By language:")
        for lang, count in sorted(report.by_language.items()):
            print(f"  {lang:<10} {count}")
        print()
        print("By audience:")
        for aud, count in sorted(report.by_adult.items()):
            print(f"  {aud:<10} {count}")


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="validate_sites",
        description="Validate sites.yaml against sites_schema.json and business rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # validate (default)
  %(prog)s --verbose                    # site-by-site
  %(prog)s --strict                     # warnings = errors
  %(prog)s --json                       # JSON output (for CI)
  %(prog)s --check-parsers              # cross-check parser files
  %(prog)s --summary                    # language/audience summary

Exit codes: 0 = valid, 1 = invalid, 2 = bad args, 3 = missing prereq
        """,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of text",
    )
    parser.add_argument(
        "--check-parsers",
        action="store_true",
        help="Cross-check parser files (import + attributes)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print language/audience summary",
    )
    return parser


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code (0 = valid, 1 = invalid, 2 = bad args, 3 = missing prereq).
    """
    parser = build_parser()
    args = parser.parse_args()

    # ── Prerequisites ──
    _check_jsonschema()

    sites_yaml = _find_first(_SITES_YAML_CANDIDATES, "sites.yaml")
    if sites_yaml is None:
        print(
            f"❌ sites.yaml not found in {_DATA_DIR}",
            file=sys.stderr,
        )
        return 3

    schema_path = _find_first(_SCHEMA_CANDIDATES, "sites_schema.json")
    if schema_path is None:
        print(
            f"❌ sites_schema.json not found in {_DATA_DIR}",
            file=sys.stderr,
        )
        return 3

    # ── Read ──
    try:
        sites_data = _read_yaml(sites_yaml)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3

    try:
        schema = _read_json(schema_path)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3

    if args.verbose:
        print(f"Validating {sites_yaml.relative_to(_REPO_ROOT)}...")
        print(f"Schema:    {schema_path.relative_to(_REPO_ROOT)}")

    # ── Validate ──
    report = ValidationReport()
    _validate_schema(sites_data, schema, report)
    _validate_business_rules(sites_data, report)

    if args.check_parsers:
        sys.path.insert(0, str(_SRC_DIR))
        _validate_parsers(sites_data, report)

    # ── Print ──
    _print_report(
        report,
        verbose=args.verbose,
        summary=args.summary,
        strict=args.strict,
        as_json=args.json,
    )

    # ── Exit code ──
    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted", file=sys.stderr)
        raise SystemExit(130) from None
