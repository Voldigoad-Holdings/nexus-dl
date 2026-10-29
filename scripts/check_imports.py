#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL architecture & secret checker.

Enforces the **hexagonal architecture** (ADR-001) and **blocks secrets**
in the source tree.

What it checks:

1. **Dependency rules** (ADR-001):
   - ``core/``       MUST NOT import ``parsers/``, ``interfaces/``, ``plugins/``
   - ``parsers/``    MUST NOT import ``interfaces/``, ``plugins/``
   - ``plugins/``    MUST NOT import ``interfaces/``, ``parsers/``
   - ``data/``       MUST NOT import anything from ``nexusdl``

2. **Forbidden patterns** (secrets & prod URLs):
   - GitHub tokens (``ghp_``, ``gho_``, ``ghs_``, ``ghr_``)
   - AWS access keys (``AKIA...``)
   - Private key blocks (``-----BEGIN ... PRIVATE KEY-----``)
   - Hardcoded passwords (``password = "..."``)
   - NexusDL production URLs (``nexusdl.dev``, ``api.nexusdl.*``)

3. **Optional checks** (``--check-future``, ``--check-spdx``):
   - ``from __future__ import annotations`` at top of Python files
   - SPDX license header at top of Python files

Usage::

    # Check the whole repo (default)
    python scripts/check_imports.py

    # Check specific files (pre-commit passes them as args)
    python scripts/check_imports.py src/nexusdl/core/config.py

    # Verbose
    python scripts/check_imports.py --verbose

    # With suggestions
    python scripts/check_imports.py --suggest

    # Extra checks
    python scripts/check_imports.py --check-future --check-spdx

Exit codes::

    0   No violations
    1   One or more violations
    2   Invalid arguments
    3   Internal error (config, paths)

Notes::

    - Excludes ``.venv/``, ``build/``, ``dist/``, ``node_modules/``,
      ``__pycache__/``, ``tests/`` (except ``tests/unit/parsers/``),
      and the frontend directory.
    - Ignores lines starting with ``#`` (comments).
    - Ignores docstrings (via AST parsing).
    - Ignores ``if TYPE_CHECKING:`` blocks (ADR-003 pattern).
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterator

# ════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION
# ════════════════════════════════════════════════════════════════════

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SRC_DIR: Final[Path] = _REPO_ROOT / "src" / "nexusdl"

#: Modules that a given layer MUST NOT import.
#: Empty set = no restriction (e.g. ``interfaces/`` may import anything).
_FORBIDDEN_IMPORTS: Final[dict[str, frozenset[str]]] = {
    "core": frozenset({"nexusdl.parsers", "nexusdl.interfaces", "nexusdl.plugins"}),
    "parsers": frozenset({"nexusdl.interfaces", "nexusdl.plugins"}),
    "plugins": frozenset({"nexusdl.interfaces", "nexusdl.parsers"}),
    "data": frozenset({"nexusdl.core", "nexusdl.parsers", "nexusdl.interfaces", "nexusdl.plugins"}),
}

#: Forbidden patterns: (label, regex).
_FORBIDDEN_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("NexusDL prod URL", re.compile(r"https?://(?:api\.)?nexusdl\.(?:dev|com|org|io)")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    (
        "Hardcoded password",
        re.compile(
            r"""(?:password|passwd|pwd|secret|token|api[_-]?key)\s*=\s*["']([^"']{8,})["']""",
            re.IGNORECASE,
        ),
    ),
)

#: Files and directories to exclude entirely (matched on the posix path
#: relative to ``_REPO_ROOT``).
_EXCLUDED_PATHS: Final[tuple[str, ...]] = (
    "scripts/check_imports.py",  # this file
    "scripts/validate_sites.py",
)

#: Directories to skip during recursive walk.
_EXCLUDED_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "htmlcov",
        ".next",
        "out",
        ".turbo",
        ".idea",
        ".vscode",
        "frontend",
        "tests",
    }
)

#: Tests subdirectories that should still be checked (e.g. parser smoke tests).
_INCLUDED_TEST_SUBDIRS: Final[tuple[str, ...]] = ("unit/parsers",)


# ════════════════════════════════════════════════════════════════════
# 2. DATA CLASSES
# ════════════════════════════════════════════════════════════════════


@dataclass
class Violation:
    """A single architecture or secret violation.

    Attributes:
        file: File path (relative to repo root).
        line: Line number (1-based, 0 if unknown).
        rule: Rule ID (e.g. ``R1``, ``SECRET``, ``PATTERN``).
        message: Human-readable message.
        suggestion: Optional fix suggestion.
    """

    file: str
    line: int
    rule: str
    message: str
    suggestion: str | None = None

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: [{self.rule}] {self.message}"


@dataclass
class CheckReport:
    """Aggregated report of a check run."""

    files_checked: int = 0
    files_skipped: int = 0
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    by_rule: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, v: Violation) -> None:
        self.violations.append(v)
        self.by_rule[v.rule] += 1

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


# ════════════════════════════════════════════════════════════════════
# 3. HELPERS
# ════════════════════════════════════════════════════════════════════


def _rel(path: Path) -> str:
    """Return the path relative to the repo root, as a posix string.

    Args:
        path: Absolute or relative path.

    Returns:
        Posix-style relative path (or the original path if outside the repo).
    """
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _layer_of(path: Path) -> str | None:
    """Determine the architectural layer of a file.

    Args:
        path: Path to a Python file.

    Returns:
        One of ``core``, ``parsers``, ``plugins``, ``data``, ``interfaces``,
        or ``None`` if the file is not in a known layer.
    """
    try:
        rel = path.resolve().relative_to(_SRC_DIR)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    return parts[0] if parts[0] in _FORBIDDEN_IMPORTS or parts[0] == "interfaces" else None


def _is_test_file(path: Path) -> bool:
    """Return True if ``path`` is a test file we should check (parser smoke tests)."""
    rel = _rel(path)
    if not rel.startswith("tests/"):
        return False
    return any(rel.startswith(f"tests/{sub}/") for sub in _INCLUDED_TEST_SUBDIRS)


def _should_skip(path: Path) -> bool:
    """Return True if ``path`` should be excluded from checks."""
    rel = _rel(path)
    if rel in _EXCLUDED_PATHS:
        return True
    # Skip test files that are not parser smoke tests
    if rel.startswith("tests/") and not _is_test_file(path):
        return True
    return False


def _iter_python_files(root: Path) -> Iterator[Path]:
    """Yield all ``.py`` files under ``root``, skipping excluded dirs.

    Args:
        root: Root directory to walk.

    Yields:
        Paths to Python files.
    """
    for path in root.rglob("*.py"):
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        yield path


def _read_source(path: Path) -> str | None:
    """Read a Python file, returning ``None`` on decode error.

    Args:
        path: File to read.

    Returns:
        File content, or ``None`` if it could not be read as UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _ast_parse(source: str, path: Path) -> ast.AST | None:
    """Parse Python source into an AST, returning ``None`` on syntax error.

    Args:
        source: Python source.
        path: Path used for the error message.

    Returns:
        The root AST node, or ``None`` if parsing failed.
    """
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


def _collect_docstring_lines(tree: ast.AST) -> set[int]:
    """Collect line numbers occupied by docstrings.

    Args:
        tree: Root AST node.

    Returns:
        Set of line numbers (1-based) that are part of a docstring.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            start = node.lineno
            end = node.end_lineno or start
            docstrings.update(range(start, end + 1))
    return docstrings


def _collect_type_checking_lines(tree: ast.AST) -> set[int]:
    """Collect line numbers of ``if TYPE_CHECKING:`` blocks.

    Args:
        tree: Root AST node.

    Returns:
        Set of line numbers covered by ``if TYPE_CHECKING:`` blocks.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            is_type_checking = (
                isinstance(test, ast.Name)
                and test.id == "TYPE_CHECKING"
            ) or (
                isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"
            ) or (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "TYPE_CHECKING"
            )
            if is_type_checking:
                start = node.lineno
                end = node.end_lineno or start
                lines.update(range(start, end + 1))
    return lines


def _extract_imports(tree: ast.AST) -> list[tuple[str, int]]:
    """Extract all imported modules with their line numbers.

    Args:
        tree: Root AST node.

    Returns:
        List of ``(module_name, lineno)`` tuples.
    """
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.append((node.module, node.lineno))
    return imports


# ════════════════════════════════════════════════════════════════════
# 4. CHECKS
# ════════════════════════════════════════════════════════════════════


def check_imports(path: Path, source: str, tree: ast.AST) -> list[Violation]:
    """Check that ``path`` respects the hexagonal dependency rules.

    Args:
        path: Python file.
        source: File content (unused, kept for symmetry).
        tree: Parsed AST.

    Returns:
        List of violations.
    """
    layer = _layer_of(path)
    if layer is None or layer not in _FORBIDDEN_IMPORTS:
        return []

    forbidden = _FORBIDDEN_IMPORTS[layer]
    if not forbidden:
        return []

    type_checking_lines = _collect_type_checking_lines(tree)
    violations: list[Violation] = []
    rel = _rel(path)

    for module, lineno in _extract_imports(tree):
        # Skip if inside `if TYPE_CHECKING:` block (ADR-003 pattern)
        if lineno in type_checking_lines:
            continue

        for forbidden_prefix in forbidden:
            if module == forbidden_prefix or module.startswith(forbidden_prefix + "."):
                rule = f"R{list(_FORBIDDEN_IMPORTS).index(layer) + 1}"
                violations.append(
                    Violation(
                        file=rel,
                        line=lineno,
                        rule=rule,
                        message=(
                            f"layer '{layer}' must not import '{module}' "
                            f"(forbidden: {forbidden_prefix})"
                        ),
                        suggestion=_suggest_import_fix(layer, module),
                    )
                )
                break  # one violation per import

    return violations


def _suggest_import_fix(layer: str, module: str) -> str:
    """Suggest a fix for a forbidden import."""
    if layer == "core" and module.startswith("nexusdl.parsers"):
        return (
            "Define a Protocol in core/registry/protocols.py and inject "
            "the implementation at the composition root (see ADR-003)."
        )
    if layer == "core" and module.startswith("nexusdl.interfaces"):
        return (
            "core/ must remain UI-agnostic. Move the logic to core/ "
            "and expose it via a public API."
        )
    if layer in ("parsers", "plugins") and module.startswith("nexusdl.interfaces"):
        return (
            "Adapters must not depend on interfaces. Use a core Protocol."
        )
    return "Refactor to break the dependency (see ADR-001)."


def check_patterns(path: Path, source: str, tree: ast.AST) -> list[Violation]:
    """Check that ``path`` does not contain secrets or prod URLs.

    Args:
        path: File to check.
        source: File content.
        tree: Parsed AST (used to skip docstrings).

    Returns:
        List of violations.
    """
    docstring_lines = _collect_docstring_lines(tree)
    violations: list[Violation] = []
    rel = _rel(path)

    for lineno, line in enumerate(source.splitlines(), start=1):
        # Skip comments
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        # Skip docstrings
        if lineno in docstring_lines:
            continue

        # Skip lines with `# noqa: S...` (intentional)
        if "# noqa" in line.lower() and "s" in line.lower():
            continue

        for label, pattern in _FORBIDDEN_PATTERNS:
            match = pattern.search(line)
            if match:
                violations.append(
                    Violation(
                        file=rel,
                        line=lineno,
                        rule="SECRET",
                        message=f"{label} detected: {match.group(0)[:40]}...",
                        suggestion="Remove from source, use .env or SecretStr.",
                    )
                )

    return violations


def check_future_import(path: Path, source: str, tree: ast.AST) -> list[Violation]:
    """Check that the file has ``from __future__ import annotations``.

    Args:
        path: Python file.
        source: File content.
        tree: Parsed AST.

    Returns:
        List of violations (at most one).
    """
    # Skip __init__.py and empty files
    if path.name == "__init__.py" or not source.strip():
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    return []
    rel = _rel(path)
    return [
        Violation(
            file=rel,
            line=1,
            rule="FUTURE",
            message="missing 'from __future__ import annotations'",
            suggestion="Add at the top of the file (after the module docstring).",
        )
    ]


def check_spdx(path: Path, source: str, tree: ast.AST) -> list[Violation]:
    """Check that the file has an SPDX license header.

    Args:
        path: Python file.
        source: File content.
        tree: Parsed AST.

    Returns:
        List of violations (at most one).
    """
    # Skip __init__.py, version.py, and empty files
    if path.name in {"__init__.py", "version.py"} or not source.strip():
        return []

    first_lines = source.splitlines()[:10]
    for line in first_lines:
        if "SPDX-License-Identifier:" in line:
            return []

    rel = _rel(path)
    return [
        Violation(
            file=rel,
            line=1,
            rule="SPDX",
            message="missing SPDX license header",
            suggestion=(
                "Add at the top of the file: "
                "'# SPDX-License-Identifier: GPL-3.0-or-later'"
            ),
        )
    ]


# ════════════════════════════════════════════════════════════════════
# 5. RUNNER
# ════════════════════════════════════════════════════════════════════


def run_checks(
    paths: list[Path],
    *,
    verbose: bool = False,
    check_future: bool = False,
    check_spdx: bool = False,
) -> CheckReport:
    """Run all checks on the given files.

    Args:
        paths: Files to check.
        verbose: Print per-file progress.
        check_future: Enable the ``from __future__`` check.
        check_spdx: Enable the SPDX header check.

    Returns:
        The aggregated report.
    """
    report = CheckReport()

    for path in sorted(paths):
        if _should_skip(path):
            report.files_skipped += 1
            continue

        source = _read_source(path)
        if source is None:
            report.warnings.append(f"{_rel(path)}: skipped (not UTF-8)")
            report.files_skipped += 1
            continue

        tree = _ast_parse(source, path)
        if tree is None:
            report.warnings.append(f"{_rel(path)}: skipped (syntax error)")
            report.files_skipped += 1
            continue

        report.files_checked += 1

        # Architecture rules
        for v in check_imports(path, source, tree):
            report.add(v)

        # Secret patterns
        for v in check_patterns(path, source, tree):
            report.add(v)

        # Optional checks
        if check_future:
            for v in check_future_import(path, source, tree):
                report.add(v)
        if check_spdx:
            for v in check_spdx(path, source, tree):
                report.add(v)

        if verbose:
            status = "✓" if not any(v.file == _rel(path) for v in report.violations) else "✗"
            print(f"  {status} {_rel(path)}")

    return report


# ════════════════════════════════════════════════════════════════════
# 6. CLI
# ════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="check_imports",
        description="Enforce NexusDL hexagonal architecture and block secrets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                        # check the whole repo
  %(prog)s src/nexusdl/core/config.py             # check one file
  %(prog)s src/nexusdl/                           # check a directory
  %(prog)s --verbose                              # per-file progress
  %(prog)s --check-future --check-spdx            # stricter mode
        """,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Files or directories to check (default: whole repo)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--check-future",
        action="store_true",
        help="Require 'from __future__ import annotations' in every file",
    )
    parser.add_argument(
        "--check-spdx",
        action="store_true",
        help="Require SPDX license header in every file",
    )
    parser.add_argument(
        "--suggest",
        action="store_true",
        help="Print suggestions for each violation",
    )
    return parser


def _collect_paths(args: list[str]) -> list[Path]:
    """Collect Python files from the CLI arguments.

    Args:
        args: CLI arguments (files or directories).

    Returns:
        List of ``.py`` files to check.
    """
    if not args:
        return list(_iter_python_files(_REPO_ROOT))

    paths: list[Path] = []
    for arg in args:
        path = Path(arg).resolve()
        if path.is_file() and path.suffix == ".py":
            paths.append(path)
        elif path.is_dir():
            paths.extend(_iter_python_files(path))
    return paths


def print_report(
    report: CheckReport,
    *,
    suggest: bool = False,
    verbose: bool = False,
) -> None:
    """Print the final report.

    Args:
        report: The report.
        suggest: Print suggestions.
        verbose: Include extra info.
    """
    if verbose:
        print()
        print(f"Files checked: {report.files_checked}")
        print(f"Files skipped: {report.files_skipped}")
        if report.warnings:
            print(f"Warnings: {len(report.warnings)}")
            for w in report.warnings:
                print(f"  ⚠️  {w}")
        print()

    if not report.has_violations:
        print(f"✅ Architecture OK — {report.files_checked} file(s) checked, 0 violation.")
        return

    print(f"❌ {len(report.violations)} violation(s) detected:", file=sys.stderr)
    print(file=sys.stderr)

    for v in report.violations:
        print(f"  • {v}", file=sys.stderr)
        if suggest and v.suggestion:
            print(f"    → {v.suggestion}", file=sys.stderr)

    print(file=sys.stderr)
    print("By rule:", file=sys.stderr)
    for rule, count in sorted(report.by_rule.items()):
        print(f"  {rule}: {count}", file=sys.stderr)

    print(file=sys.stderr)
    print(
        "See docs/source/development/decisions/ for the rules.",
        file=sys.stderr,
    )


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code (0 = OK, 1 = violations, 2 = bad args, 3 = internal error).
    """
    parser = build_parser()
    args = parser.parse_args()

    try:
        paths = _collect_paths(args.files)
    except OSError as exc:
        print(f"❌ Cannot collect files: {exc}", file=sys.stderr)
        return 3

    if not paths:
        print("⚠️  No Python files found.", file=sys.stderr)
        return 2 if args.files else 0

    try:
        report = run_checks(
            paths,
            verbose=args.verbose,
            check_future=args.check_future,
            check_spdx=args.check_spdx,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Internal error: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 3

    print_report(report, suggest=args.suggest, verbose=args.verbose)
    return 1 if report.has_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
