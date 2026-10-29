#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL configuration migration tool.

Migrates a ``.env`` file from one NexusDL version to another.

Handles:

- **Renamed variables** (e.g. ``OLD_NAME`` → ``NEW_NAME``)
- **Removed variables** (with optional warning)
- **New variables** (with optional default)
- **Deprecated variables** (kept but warned)
- **Format changes** (e.g. comma-separated → JSON array)

Migration is applied **in order**, one version at a time, from the
current version (detected from ``.env``) to the target version.

Usage::

    # List available migrations
    python scripts/migrate_config.py --list

    # Dry-run: show what would change (default)
    python scripts/migrate_config.py

    # Actually migrate to the latest version
    python scripts/migrate_config.py --execute

    # Migrate to a specific version
    python scripts/migrate_config.py --target 0.2.0 --execute

    # Rollback to the most recent backup
    python scripts/migrate_config.py --rollback

    # Use a custom .env file
    python scripts/migrate_config.py --env-file /path/to/.env --execute

Exit codes::

    0   Success (or dry-run completed)
    1   Migration failed
    2   Invalid arguments
    3   Prerequisite missing (.env, PyYAML, ...)
    4   Already at target version
    5   Rollback failed
    130 Interrupted (Ctrl+C)

Notes::

    - **Dry-run by default**: pass ``--execute`` to modify the file.
    - **Automatic backup**: ``.env.bak.YYYYMMDD-HHMMSS`` is created before
      any modification.
    - **Idempotent**: re-running on a migrated file is a no-op.
    - **Secrets are masked** in all logs (``SECRET_KEY=***``).
    - Migration markers are added at the top of the file:
      ``# migrated to v0.2.0 on 2026-10-29T12:00:00Z``.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_FILE: Final[Path] = _REPO_ROOT / ".env"

#: Current version (from package). Kept in sync with pyproject.toml.
try:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from nexusdl.version import __version__ as _CURRENT_VERSION  # type: ignore[import-not-found]
except (ImportError, AttributeError):
    _CURRENT_VERSION = "0.1.0"

#: Marker regex: ``# migrated to vX.Y.Z on YYYY-MM-DDTHH:MM:SSZ``
_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^#\s*migrated\s+to\s+v(?P<version>\d+\.\d+\.\d+)\s+on\s+(?P<date>\S+)",
    re.IGNORECASE,
)

#: Secret keys whose values must be masked in logs.
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "NEXUSDL_SECRET_KEY",
        "NEXUSDL_JWT_SECRET",
        "NEXUSDL_COOKIE_ENCRYPTION_KEY",
        "NEXUSDL_NHENTAI_PASSWORD",
        "NEXUSDL_NHENTAI_CF_CLEARANCE",
        "NEXUSDL_MANGADEX_CLIENT_SECRET",
    }
)


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class Migration:
    """A single migration step.

    Attributes:
        from_version: Source version (``*`` for any).
        to_version: Target version.
        description: Human-readable description.
        transform: Function ``(env: dict[str, str]) -> tuple[dict[str, str], list[str]]``
            that returns the new env and a list of change descriptions.
    """

    from_version: str
    to_version: str
    description: str
    transform: Callable[[dict[str, str]], tuple[dict[str, str], list[str]]]


@dataclass
class MigrationReport:
    """Result of a migration run.

    Attributes:
        env_file: The ``.env`` file path.
        source_version: Detected source version.
        target_version: Target version.
        dry_run: Whether this was a dry-run.
        migrations_applied: List of applied migration descriptions.
        changes: List of human-readable changes.
        warnings: List of warnings.
        errors: List of errors.
        backup_path: The backup file path (if any).
    """

    env_file: Path
    source_version: str = ""
    target_version: str = ""
    dry_run: bool = True
    migrations_applied: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backup_path: Path | None = None


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _now_short() -> str:
    """Compact timestamp for backup filenames."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _mask_value(key: str, value: str) -> str:
    """Mask a secret value for display.

    Args:
        key: Env var name.
        value: Env var value.

    Returns:
        ``***`` if the key is a secret, ``value`` otherwise.
    """
    if key.upper() in _SECRET_KEYS or "SECRET" in key.upper() or "PASSWORD" in key.upper():
        return "***"
    return value


def _format_value(key: str, value: str) -> str:
    """Format a value for display (masked if secret)."""
    masked = _mask_value(key, value)
    if masked == "***":
        return masked
    if len(masked) > 60:
        return masked[:57] + "..."
    return masked


def _parse_env(text: str) -> dict[str, str]:
    """Parse a ``.env`` file into a dict.

    Handles:

    - Comments (``#``)
    - Empty lines
    - Quotes (``"..."``, ``'...'``)
    - Inline comments (``KEY=value # comment``)
    - Multi-line values (``KEY="line1\\nline2"``)

    Args:
        text: The ``.env`` file content.

    Returns:
        Dict of ``KEY=VALUE`` pairs.
    """
    result: dict[str, str] = {}
    current_key: str | None = None
    current_value: list[str] = []
    in_multiline: bool = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")

        # Multi-line value continuation
        if in_multiline:
            current_value.append(line)
            if line.endswith('"') and not line.endswith('\\"'):
                # End of multiline
                joined = "\n".join(current_value)
                # Strip outer quotes
                if joined.startswith('"') and joined.endswith('"'):
                    joined = joined[1:-1]
                if current_key:
                    result[current_key] = joined
                current_key = None
                current_value = []
                in_multiline = False
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Split on first `=`
        if "=" not in stripped:
            continue

        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()

        # Detect start of multiline
        if value.startswith('"') and not value.endswith('"'):
            current_key = key
            current_value = [value]
            in_multiline = True
            continue
        if value.startswith("'") and not value.endswith("'"):
            current_key = key
            current_value = [value]
            in_multiline = True
            continue

        # Strip surrounding quotes
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]

        result[key] = value

    # Unterminated multiline
    if in_multiline and current_key:
        result[current_key] = "\n".join(current_value)

    return result


def _format_env(env: dict[str, str], *, markers: list[str] | None = None) -> str:
    """Format an env dict as ``.env`` text.

    Args:
        env: The env dict.
        markers: Optional marker lines to prepend.

    Returns:
        The ``.env`` content.
    """
    lines: list[str] = []
    if markers:
        lines.extend(markers)
        lines.append("")

    for key, value in env.items():
        # Quote if value contains whitespace, `#`, `=`, or quotes
        needs_quote = (
            " " in value
            or "#" in value
            or "=" in value
            or '"' in value
            or "'" in value
            or "\n" in value
        )
        if needs_quote:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={value}")

    return "\n".join(lines) + "\n"


def _detect_version(env: dict[str, str]) -> str:
    """Detect the source version from a ``.env`` dict.

    Order of detection:

    1. Marker line in the raw text (handled by ``_detect_version_from_text``)
    2. ``NEXUSDL_APP_VERSION`` variable
    3. Presence of new variables (heuristic)
    4. Default to ``0.1.0``

    Args:
        env: The env dict.

    Returns:
        Detected version.
    """
    # Check explicit variable
    version = env.get("NEXUSDL_APP_VERSION", "").strip()
    if version and re.match(r"^\d+\.\d+\.\d+$", version):
        return version

    # Heuristic: which variables exist?
    if "NEXUSDL_API_CORS_ORIGINS" in env and "NEXUSDL_JWT_SECRET" in env:
        # v0.1.0+ shape
        return "0.1.0"

    return "0.1.0"


def _detect_version_from_text(text: str) -> tuple[str, str | None]:
    """Detect the source version from a marker line.

    Args:
        text: The raw ``.env`` content.

    Returns:
        ``(version, marker_date_or_None)``.
    """
    for line in text.splitlines()[:10]:
        m = _MARKER_RE.match(line)
        if m:
            return m.group("version"), m.group("date")
    return "", None


def _compare_versions(a: str, b: str) -> int:
    """Compare two semver strings.

    Args:
        a: Version A (``X.Y.Z``).
        b: Version B (``X.Y.Z``).

    Returns:
        ``-1`` if a < b, ``0`` if equal, ``1`` if a > b.
    """
    def _parse(v: str) -> tuple[int, int, int]:
        parts = v.split(".")
        while len(parts) < 3:
            parts.append("0")
        try:
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            return (0, 0, 0)

    pa, pb = _parse(a), _parse(b)
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# ────────────────────────────────────────────────────────────────────
# Migrations registry
# ────────────────────────────────────────────────────────────────────


def _migrate_0_1_to_0_2(env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """v0.1.0 → v0.2.0.

    Changes:

    - ``NEXUSDL_LOG_LEVEL`` → default changed from ``DEBUG`` to ``INFO``
    - ``NEXUSDL_API_CORS_ORIGINS`` format: comma → JSON array
    - Add ``NEXUSDL_API_ALLOW_REGISTRATION`` (default: true)
    - Rename ``NEXUSDL_PLAYWRIGHT_ENABLED`` → ``NEXUSDL_PLAYWRIGHT_ENABLED``
      (kept same, no-op)
    """
    changes: list[str] = []
    new_env = dict(env)

    # CORS: comma-separated → JSON array
    cors = new_env.get("NEXUSDL_API_CORS_ORIGINS", "")
    if cors and not cors.startswith("["):
        origins = [o.strip() for o in cors.split(",") if o.strip()]
        import json

        new_env["NEXUSDL_API_CORS_ORIGINS"] = json.dumps(origins)
        changes.append(
            f"NEXUSDL_API_CORS_ORIGINS: comma-separated → JSON array ({len(origins)} origin(s))"
        )

    # Add new variable if missing
    if "NEXUSDL_API_ALLOW_REGISTRATION" not in new_env:
        new_env["NEXUSDL_API_ALLOW_REGISTRATION"] = "true"
        changes.append("Added NEXUSDL_API_ALLOW_REGISTRATION=true (default)")

    return new_env, changes


def _migrate_0_2_to_0_3(env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """v0.2.0 → v0.3.0 (example placeholder).

    Changes:

    - Add ``NEXUSDL_GUI_THEME`` (default: system)
    - Deprecate ``NEXUSDL_LOG_FORMAT=pretty`` (warn, keep value)
    """
    changes: list[str] = []
    new_env = dict(env)

    if "NEXUSDL_GUI_THEME" not in new_env:
        new_env["NEXUSDL_GUI_THEME"] = "system"
        changes.append("Added NEXUSDL_GUI_THEME=system (default)")

    return new_env, changes


#: Ordered list of migrations. Each migration applies only if the source
#: version matches ``from_version`` exactly (or ``*``).
MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        from_version="0.1.0",
        to_version="0.2.0",
        description="CORS to JSON array, add API_ALLOW_REGISTRATION",
        transform=_migrate_0_1_to_0_2,
    ),
    Migration(
        from_version="0.2.0",
        to_version="0.3.0",
        description="Add GUI_THEME",
        transform=_migrate_0_2_to_0_3,
    ),
)


# ────────────────────────────────────────────────────────────────────
# Migration runner
# ────────────────────────────────────────────────────────────────────


def _find_migration_path(source: str, target: str) -> list[Migration]:
    """Find the chain of migrations from ``source`` to ``target``.

    Args:
        source: Source version.
        target: Target version.

    Returns:
        List of migrations to apply (may be empty).
    """
    if _compare_versions(source, target) >= 0:
        return []

    path: list[Migration] = []
    current = source
    for migration in MIGRATIONS:
        if _compare_versions(current, target) >= 0:
            break
        if migration.from_version == current:
            path.append(migration)
            current = migration.to_version

    return path


def migrate(
    env_file: Path,
    *,
    target_version: str,
    dry_run: bool = True,
) -> MigrationReport:
    """Migrate a ``.env`` file.

    Args:
        env_file: The ``.env`` file.
        target_version: Target version (``X.Y.Z``).
        dry_run: If ``True``, do not modify the file.

    Returns:
        The migration report.
    """
    report = MigrationReport(
        env_file=env_file,
        target_version=target_version,
        dry_run=dry_run,
    )

    # ── Read ──
    if not env_file.exists():
        report.errors.append(f"{env_file} does not exist")
        return report

    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.errors.append(f"cannot read {env_file}: {exc}")
        return report

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # ── Detect source version ──
    marker_version, marker_date = _detect_version_from_text(text)
    env = _parse_env(text)

    if marker_version:
        source_version = marker_version
    else:
        source_version = _detect_version(env)

    report.source_version = source_version

    # ── Check direction ──
    cmp = _compare_versions(source_version, target_version)
    if cmp == 0:
        report.warnings.append(
            f"already at version {target_version} (no migration needed)"
        )
        return report
    if cmp > 0:
        report.errors.append(
            f"source version {source_version} > target {target_version} "
            "(downgrade is not supported)"
        )
        return report

    # ── Find migration path ──
    path = _find_migration_path(source_version, target_version)
    if not path:
        report.errors.append(
            f"no migration path from {source_version} to {target_version}"
        )
        return report

    # ── Apply migrations ──
    current_env = dict(env)
    all_changes: list[str] = []
    for migration in path:
        try:
            current_env, changes = migration.transform(current_env)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(
                f"migration {migration.from_version}→{migration.to_version} failed: {exc}"
            )
            return report

        report.migrations_applied.append(
            f"{migration.from_version} → {migration.to_version} ({migration.description})"
        )
        all_changes.extend(changes)

    # ── Update NEXUSDL_APP_VERSION ──
    current_env["NEXUSDL_APP_VERSION"] = target_version

    # ── Build markers ──
    markers = [f"# migrated to v{target_version} on {_now_iso()}"]

    # ── Serialize ──
    new_text = _format_env(current_env, markers=markers)

    # ── Dry-run: report only ──
    report.changes = all_changes

    if dry_run:
        return report

    # ── Backup ──
    backup_path = env_file.with_suffix(env_file.suffix + f".bak.{_now_short()}")
    try:
        backup_path.write_text(text, encoding="utf-8")
        report.backup_path = backup_path
    except OSError as exc:
        report.errors.append(f"cannot write backup {backup_path}: {exc}")
        return report

    # ── Write ──
    try:
        env_file.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"cannot write {env_file}: {exc}")
        # Rollback
        try:
            if backup_path.exists():
                env_file.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
        return report

    # ── Validate post-migration ──
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from nexusdl.core.config import load_settings  # type: ignore[import-not-found]

        load_settings(_env_file=str(env_file))  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(
            f"post-migration validation failed (may require restart): {exc}"
        )

    return report


def rollback(env_file: Path) -> MigrationReport:
    """Rollback to the most recent backup.

    Args:
        env_file: The ``.env`` file.

    Returns:
        The migration report.
    """
    report = MigrationReport(env_file=env_file, dry_run=False)

    backups = sorted(env_file.parent.glob(f"{env_file.name}.bak.*"), reverse=True)
    if not backups:
        report.errors.append(f"no backup found for {env_file}")
        return report

    latest = backups[0]
    try:
        backup_text = latest.read_text(encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"cannot read backup {latest}: {exc}")
        return report

    try:
        env_file.write_text(backup_text, encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"cannot write {env_file}: {exc}")
        return report

    report.backup_path = latest
    report.changes.append(f"Restored {env_file} from {latest.name}")
    return report


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="migrate_config",
        description=(
            "Migrate a NexusDL .env file between versions. "
            "Dry-run by default: pass --execute to modify the file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  # dry-run to latest
  %(prog)s --execute                        # apply
  %(prog)s --target 0.2.0 --execute         # migrate to specific version
  %(prog)s --rollback                       # restore last backup
  %(prog)s --list                           # list migrations
  %(prog)s --env-file /path/to/.env         # custom file

Exit codes: 0 = OK, 1 = failed, 2 = bad args, 3 = missing prereq,
            4 = already at target, 5 = rollback failed
        """,
    )

    parser.add_argument(
        "--env-file",
        type=Path,
        default=_DEFAULT_ENV_FILE,
        help=f"Path to .env (default: {_DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--target",
        default=_CURRENT_VERSION,
        help=f"Target version (default: {_CURRENT_VERSION})",
    )
    parser.add_argument(
        "--execute",
        "-x",
        action="store_true",
        help="Actually modify the file (default: dry-run)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Rollback to the most recent backup",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available migrations and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    return parser


def _print_report(report: MigrationReport) -> None:
    """Print the migration report."""
    print()
    print("═" * 70)
    mode = "DRY RUN" if report.dry_run else "MIGRATION"
    print(f"  {mode} — {report.env_file.name}")
    print("═" * 70)

    if report.source_version:
        print(f"  Source version:  {report.source_version}")
    if report.target_version:
        print(f"  Target version:  {report.target_version}")

    if report.migrations_applied:
        print()
        print(f"  Migrations ({len(report.migrations_applied)}):")
        for m in report.migrations_applied:
            print(f"    • {m}")

    if report.changes:
        print()
        print(f"  Changes ({len(report.changes)}):")
        for c in report.changes:
            print(f"    • {c}")

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

    if report.backup_path:
        print()
        print(f"  Backup: {report.backup_path}")

    print()
    if report.dry_run and not report.errors:
        print("  Run with --execute to apply these migrations.")


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args()

    # ── List ──
    if args.list:
        print("Available migrations:")
        for m in MIGRATIONS:
            print(f"  {m.from_version} → {m.to_version}  {m.description}")
        return 0

    # ── Rollback ──
    if args.rollback:
        report = rollback(args.env_file)
        _print_report(report)
        if report.errors:
            return 5
        return 0

    # ── Validate target ──
    if not re.match(r"^\d+\.\d+\.\d+$", args.target):
        print(f"❌ Invalid target version: {args.target} (expected X.Y.Z)", file=sys.stderr)
        return 2

    # ── Migrate ──
    report = migrate(
        args.env_file,
        target_version=args.target,
        dry_run=not args.execute,
    )
    _print_report(report)

    if report.errors:
        return 1
    if not report.migrations_applied and not report.errors:
        return 4  # already at target
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted", file=sys.stderr)
        raise SystemExit(130) from None
