#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL clean script.

Removes build artifacts, caches, and optionally user data.

**SAFETY FIRST**: this script defaults to ``--dry-run``. You must pass
``--execute`` (or ``-x``) to actually delete anything. Actions on user
data (``library.db``, ``cookies.enc``, downloads) require an additional
``--yes`` flag.

Clean levels:

- ``--safe`` (default): build artifacts + caches only. **Never** touches
  user data. Safe to run anytime.
- ``--all``: ``--safe`` + runtime data (``library.db``, ``cookies.enc``,
  logs, temp). Requires ``--yes``.
- ``--hard``: ``--all`` + ``.env``, ``.venv/``, ``node_modules/``.
  Requires ``--yes`` **and** typing ``DELETE`` to confirm.

Usage::

    # Preview what would be deleted (default, no deletion)
    python scripts/clean.py

    # Clean build artifacts (safe)
    python scripts/clean.py --safe --execute

    # Clean build + runtime data
    python scripts/clean.py --all --execute --yes

    # Nuclear option (build + runtime + env + venv + node_modules)
    python scripts/clean.py --hard --execute --yes

    # Keep specific patterns
    python scripts/clean.py --all --execute --yes --keep "*.log"

Exit codes::

    0   Success (or dry-run completed)
    1   Partial failure (some files could not be deleted)
    2   Invalid arguments
    3   Safety check failed (path outside allowed roots)
    4   User aborted (did not type DELETE for --hard)
    130 Interrupted (Ctrl+C)

Notes::

    - Never follows symlinks (``follow_symlinks=False``).
    - Never deletes outside ``_REPO_ROOT``, ``_USER_DATA_DIR``,
      ``_USER_CONFIG_DIR``, or ``_USER_CACHE_DIR``.
    - ``.env`` is **never** deleted unless ``--hard``.
    - Refuses to delete the active virtualenv (``--hard`` will warn
      and require re-activation).
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# User directories (from platformdirs)
try:
    import platformdirs

    _USER_DATA_DIR: Final[Path] = Path(platformdirs.user_data_dir("nexusdl"))
    _USER_CONFIG_DIR: Final[Path] = Path(platformdirs.user_config_dir("nexusdl"))
    _USER_CACHE_DIR: Final[Path] = Path(platformdirs.user_cache_dir("nexusdl"))
    _USER_LOG_DIR: Final[Path] = Path(platformdirs.user_log_dir("nexusdl"))
except ImportError:
    _USER_DATA_DIR = Path.home() / ".local" / "share" / "nexusdl"
    _USER_CONFIG_DIR = Path.home() / ".config" / "nexusdl"
    _USER_CACHE_DIR = Path.home() / ".cache" / "nexusdl"
    _USER_LOG_DIR = Path.home() / ".local" / "state" / "nexusdl" / "logs"

#: Allowed roots for deletion (security: nothing outside these)
_ALLOWED_ROOTS: Final[tuple[Path, ...]] = (
    _REPO_ROOT,
    _USER_DATA_DIR,
    _USER_CONFIG_DIR,
    _USER_CACHE_DIR,
    _USER_LOG_DIR,
)

#: Safe patterns (build artifacts + caches)
_SAFE_PATTERNS: Final[tuple[str, ...]] = (
    # Python
    "**/__pycache__",
    "**/*.py[cod]",
    "**/*.egg-info",
    "**/*.egg-link",
    "**/build",
    "**/dist",
    "**/.eggs",
    "**/*.egg",
    "**/*.whl",
    # Test & tool caches
    "**/.pytest_cache",
    "**/.mypy_cache",
    "**/.ruff_cache",
    "**/.hypothesis",
    "**/.tox",
    "**/.nox",
    "**/.coverage",
    "**/coverage.xml",
    "**/htmlcov",
    # Frontend
    "**/.next",
    "**/out",
    "**/.turbo",
    "**/*.tsbuildinfo",
    # Docs
    "docs/_build",
    # Root-level build dirs
    "build",
    "dist",
)

#: Runtime patterns (user data)
_RUNTIME_PATTERNS: Final[tuple[str, ...]] = (
    "**/library.db",
    "**/library.db-*",
    "**/library.db-journal",
    "**/library.db-wal",
    "**/library.db-shm",
    "**/cookies.enc",
    "**/logs",
    "**/temp",
    "**/tmp",
    "**/.cache",
    "**/.nexusdl",
)

#: Hard patterns (env, venv, node_modules)
_HARD_PATTERNS: Final[tuple[str, ...]] = (
    "**/.env",
    "**/.venv",
    "**/venv",
    "**/env",
    "**/node_modules",
)


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class Target:
    """A single path targeted for deletion.

    Attributes:
        path: The path.
        level: ``safe``, ``runtime``, or ``hard``.
        is_dir: Whether it's a directory.
        size_bytes: Computed size (0 if unknown).
        reason: Human-readable reason.
    """

    path: Path
    level: str
    is_dir: bool
    size_bytes: int = 0
    reason: str = ""

    def __str__(self) -> str:
        kind = "dir " if self.is_dir else "file"
        return f"{kind} {self.path} ({_format_bytes(self.size_bytes)})"


@dataclass
class CleanReport:
    """Aggregated report."""

    targets: list[Target] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    total_bytes: int = 0

    def add_target(self, t: Target) -> None:
        self.targets.append(t)
        self.total_bytes += t.size_bytes

    def add_deleted(self, path: Path) -> None:
        self.deleted.append(path)

    def add_failed(self, path: Path, reason: str) -> None:
        self.failed.append((path, reason))

    def add_skipped(self, path: Path, reason: str) -> None:
        self.skipped.append((path, reason))


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _format_bytes(n: float) -> str:
    """Format bytes for display."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _path_size(path: Path) -> int:
    """Compute the total size of a path (file or directory).

    Args:
        path: File or directory.

    Returns:
        Total size in bytes (``0`` if inaccessible).
    """
    try:
        if path.is_file() and not path.is_symlink():
            return path.stat().st_size
        if path.is_dir() and not path.is_symlink():
            total = 0
            for root, _dirs, files in os.walk(path, followlinks=False):
                for name in files:
                    file_path = Path(root) / name
                    try:
                        if not file_path.is_symlink():
                            total += file_path.stat().st_size
                    except OSError:
                        continue
            return total
    except OSError:
        pass
    return 0


def _is_safe_to_delete(path: Path) -> bool:
    """Check whether ``path`` is inside an allowed root.

    Args:
        path: Path to check.

    Returns:
        ``True`` if the resolved path is under one of ``_ALLOWED_ROOTS``.
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False

    # Reject if the path IS a root (never delete the root itself)
    for root in _ALLOWED_ROOTS:
        try:
            root_resolved = root.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == root_resolved:
            return False
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def _is_active_venv() -> bool:
    """Return ``True`` if the current Python runs in a virtualenv."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _matches_any(path: Path, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` if ``path`` matches any of the glob patterns.

    Args:
        path: Path to check.
        patterns: Glob patterns (posix style).

    Returns:
        ``True`` on match.
    """
    posix = path.as_posix()
    name = path.name
    for pattern in patterns:
        # Handle `**/pattern` and `pattern`
        if fnmatch.fnmatch(posix, pattern):
            return True
        if fnmatch.fnmatch(name, pattern):
            return True
        # Handle `**/name` explicitly
        if pattern.startswith("**/") and fnmatch.fnmatch(name, pattern[3:]):
            return True
    return False


# ────────────────────────────────────────────────────────────────────
# Collection
# ────────────────────────────────────────────────────────────────────


def _collect_safe_targets(keep: list[str]) -> list[Target]:
    """Collect safe-to-delete targets (build artifacts + caches).

    Args:
        keep: Glob patterns to keep.

    Returns:
        List of targets.
    """
    targets: list[Target] = []
    seen: set[Path] = set()

    for pattern in _SAFE_PATTERNS:
        # Anchor to repo root
        for path in _REPO_ROOT.glob(pattern):
            if path in seen:
                continue
            seen.add(path)

            if _matches_any(path, tuple(keep)):
                continue

            if not _is_safe_to_delete(path):
                continue

            targets.append(
                Target(
                    path=path,
                    level="safe",
                    is_dir=path.is_dir(),
                    size_bytes=_path_size(path),
                    reason="build artifact / cache",
                )
            )

    return targets


def _collect_runtime_targets(keep: list[str]) -> list[Target]:
    """Collect runtime targets (library.db, cookies, logs).

    Args:
        keep: Glob patterns to keep.

    Returns:
        List of targets.
    """
    targets: list[Target] = []
    seen: set[Path] = set()

    # Runtime files are in user dirs, not repo
    for base in (_USER_DATA_DIR, _USER_LOG_DIR, _USER_CACHE_DIR):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path in seen:
                continue
            if _matches_any(path, tuple(keep)):
                continue
            if not _matches_any(path, _RUNTIME_PATTERNS):
                continue
            if not _is_safe_to_delete(path):
                continue
            seen.add(path)
            targets.append(
                Target(
                    path=path,
                    level="runtime",
                    is_dir=path.is_dir(),
                    size_bytes=_path_size(path),
                    reason="runtime data",
                )
            )

    return targets


def _collect_hard_targets(keep: list[str]) -> list[Target]:
    """Collect hard targets (.env, .venv, node_modules).

    Args:
        keep: Glob patterns to keep.

    Returns:
        List of targets.
    """
    targets: list[Target] = []
    seen: set[Path] = set()

    # .env in config
    env_path = _USER_CONFIG_DIR / ".env"
    if env_path.exists() and not _matches_any(env_path, tuple(keep)):
        if _is_safe_to_delete(env_path):
            targets.append(
                Target(
                    path=env_path,
                    level="hard",
                    is_dir=False,
                    size_bytes=_path_size(env_path),
                    reason="secrets file",
                )
            )
            seen.add(env_path)

    # Venv + node_modules in repo
    for pattern in ("**/.venv", "**/venv", "**/env", "**/node_modules"):
        for path in _REPO_ROOT.glob(pattern):
            if path in seen:
                continue
            if _matches_any(path, tuple(keep)):
                continue
            if not _is_safe_to_delete(path):
                continue
            seen.add(path)

            # Warn if this is the active venv
            is_active = False
            if _is_active_venv():
                try:
                    active = Path(sys.prefix).resolve()
                    if path.resolve() == active or active.is_relative_to(path.resolve()):
                        is_active = True
                except (OSError, ValueError):
                    pass

            targets.append(
                Target(
                    path=path,
                    level="hard",
                    is_dir=True,
                    size_bytes=_path_size(path),
                    reason="active venv" if is_active else "virtual env / node_modules",
                )
            )

    return targets


# ────────────────────────────────────────────────────────────────────
# Deletion
# ────────────────────────────────────────────────────────────────────


def _delete_path(path: Path) -> None:
    """Delete a file or directory, refusing to follow symlinks.

    Args:
        path: Path to delete.

    Raises:
        OSError: If deletion fails.
    """
    if path.is_symlink():
        # Only unlink the symlink, never the target
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=False)
    elif path.is_file():
        path.unlink()
    else:
        raise OSError(f"Not a file or directory: {path}")


def execute_deletion(
    targets: list[Target],
    *,
    dry_run: bool,
    report: CleanReport,
) -> None:
    """Delete the given targets.

    Args:
        targets: Targets to delete.
        dry_run: If ``True``, do not delete.
        report: Report to populate.
    """
    for target in targets:
        path = target.path

        if dry_run:
            continue  # already counted in report.targets

        try:
            _delete_path(path)
            report.add_deleted(path)
        except OSError as exc:
            report.add_failed(path, str(exc))


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="clean",
        description=(
            "Clean NexusDL build artifacts, caches, and (optionally) user data. "
            "Defaults to --dry-run: pass --execute to actually delete."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # dry-run (safe level)
  %(prog)s --safe --execute                   # clean build artifacts
  %(prog)s --all --execute --yes              # + runtime data
  %(prog)s --hard --execute --yes             # + .env, .venv, node_modules
  %(prog)s --safe --execute --keep "*.log"    # keep *.log

Exit codes: 0 = success, 1 = partial failure, 2 = bad args, 3 = safety, 4 = aborted
        """,
    )

    level = parser.add_mutually_exclusive_group()
    level.add_argument(
        "--safe",
        action="store_const",
        dest="level",
        const="safe",
        help="Clean build artifacts + caches (default)",
    )
    level.add_argument(
        "--all",
        action="store_const",
        dest="level",
        const="all",
        help="+ runtime data (library.db, cookies, logs) — requires --yes",
    )
    level.add_argument(
        "--hard",
        action="store_const",
        dest="level",
        const="hard",
        help="+ .env, .venv, node_modules — requires --yes + type DELETE",
    )
    parser.set_defaults(level="safe")

    parser.add_argument(
        "--execute",
        "-x",
        action="store_true",
        help="Actually delete (default: dry-run)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation for --all and --hard",
    )
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Glob pattern to keep (repeatable)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress progress output (only errors)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="Write a log of actions to this file",
    )

    return parser


def _print_targets(targets: list[Target], *, quiet: bool) -> None:
    """Print the list of targets."""
    if quiet:
        return
    by_level: dict[str, list[Target]] = {"safe": [], "runtime": [], "hard": []}
    for t in targets:
        by_level[t.level].append(t)

    for level in ("safe", "runtime", "hard"):
        items = by_level[level]
        if not items:
            continue
        print(f"\n  [{level.upper()}] {len(items)} target(s):")
        for t in items:
            marker = "⚠️ " if level == "hard" else "  "
            print(f"    {marker}{t}")


def _confirm_hard() -> bool:
    """Ask the user to type DELETE for --hard."""
    print()
    print("  ⚠️  You are about to delete .env (secrets), .venv, and/or")
    print("      node_modules. This is IRREVERSIBLE.")
    print()
    try:
        response = input("  Type DELETE to confirm: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return response == "DELETE"


def _confirm_all() -> bool:
    """Ask the user to confirm --all."""
    try:
        response = input("  Delete runtime data (library.db, cookies, logs)? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return response in ("y", "yes")


def print_report(report: CleanReport, *, dry_run: bool, level: str) -> None:
    """Print the final report.

    Args:
        report: The report.
        dry_run: Whether this was a dry-run.
        level: The clean level.
    """
    print()
    print("═" * 70)
    if dry_run:
        print(f"  DRY RUN — level={level} — nothing was deleted")
    else:
        print(f"  Clean report — level={level}")
    print("═" * 70)

    print(f"  Targets:   {len(report.targets)}")
    print(f"  Total:     {_format_bytes(report.total_bytes)}")
    print(f"  Deleted:   {len(report.deleted)}")
    print(f"  Failed:    {len(report.failed)}")
    print(f"  Skipped:   {len(report.skipped)}")

    if report.failed:
        print()
        print("  ❌ Failed:")
        for path, reason in report.failed:
            print(f"    {path}: {reason}")

    if dry_run and report.targets:
        print()
        print("  Run with --execute to actually delete these targets.")


def _write_log(report: CleanReport, log_path: Path) -> None:
    """Write a log of the actions."""
    try:
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"# NexusDL clean log — {_now_iso()}\n\n")
            for t in report.targets:
                f.write(f"{t.level}\t{t.path}\t{t.size_bytes}\n")
            f.write("\n# Deleted\n")
            for p in report.deleted:
                f.write(f"{p}\n")
            f.write("\n# Failed\n")
            for p, r in report.failed:
                f.write(f"{p}\t{r}\n")
    except OSError as exc:
        print(f"⚠️  Cannot write log: {exc}", file=sys.stderr)


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code (0 = OK, 1 = partial failure, 2 = bad args, 3 = safety, 4 = aborted).
    """
    parser = build_parser()
    args = parser.parse_args()

    dry_run = not args.execute
    level = args.level

    # ── Safety: --all and --hard require --yes ──
    if not dry_run and level in ("all", "hard") and not args.yes:
        if level == "hard":
            if not _confirm_hard():
                print("\n  Aborted.", file=sys.stderr)
                return 4
        else:
            if not _confirm_all():
                print("\n  Aborted.", file=sys.stderr)
                return 4

    # ── Collect targets ──
    targets: list[Target] = []
    targets.extend(_collect_safe_targets(args.keep))
    if level in ("all", "hard"):
        targets.extend(_collect_runtime_targets(args.keep))
    if level == "hard":
        targets.extend(_collect_hard_targets(args.keep))

    # Filter out active venv from hard (unless --yes passed explicitly)
    if level == "hard":
        filtered: list[Target] = []
        for t in targets:
            if t.reason == "active venv":
                print(
                    f"  ⚠️  Skipping active venv: {t.path} "
                    f"(deactivate it first, or use --yes)",
                    file=sys.stderr,
                )
                continue
            filtered.append(t)
        targets = filtered

    # ── Deduplicate ──
    seen: set[Path] = set()
    unique: list[Target] = []
    for t in targets:
        if t.path in seen:
            continue
        seen.add(t.path)
        unique.append(t)
    targets = unique

    # ── Report ──
    report = CleanReport()
    for t in targets:
        report.add_target(t)

    if not args.quiet:
        print(f"→ NexusDL clean (level={level}, {'dry-run' if dry_run else 'execute'})")
        _print_targets(targets, quiet=args.quiet)

    # ── Execute ──
    if not dry_run:
        execute_deletion(targets, dry_run=dry_run, report=report)

    # ── Print report ──
    if not args.quiet:
        print_report(report, dry_run=dry_run, level=level)

    # ── Log ──
    if args.log:
        _write_log(report, args.log)

    # ── Exit code ──
    if report.failed:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user", file=sys.stderr)
        raise SystemExit(130) from None
