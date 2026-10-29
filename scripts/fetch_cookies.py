#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL cookie fetcher.

Extracts cookies from a browser (Chrome, Firefox, Edge, Brave) for a
specific site and writes them to ``.env`` as
``NEXUSDL_<SITE>_<COOKIE>=<value>``.

Useful for sites that require:

- **Cloudflare cookies** (``cf_clearance``, ``cf_bm``)
- **Session cookies** (authenticated sites)
- **Consent cookies** (GDPR/CCPA)

Usage::

    # Interactive (prompts for site, browser, confirmation)
    python scripts/fetch_cookies.py

    # Non-interactive (dry-run by default)
    python scripts/fetch_cookies.py \\
        --site nhentai --browser chrome --cookie cf_clearance

    # Write to .env (requires --yes)
    python scripts/fetch_cookies.py \\
        --site nhentai --browser chrome --cookie cf_clearance --yes

    # Use a specific .env file
    python scripts/fetch_cookies.py \\
        --site nhentai --env-file /path/to/.env --yes

    # Fetch all cookies for a site
    python scripts/fetch_cookies.py \\
        --site nhentai --browser firefox --all-cookies --yes

    # List available cookies (no write)
    python scripts/fetch_cookies.py \\
        --site nhentai --browser chrome --list

Exit codes::

    0   Success
    1   Cookie not found / read error
    2   Invalid arguments
    3   Missing prerequisite (browser, cryptography, ...)
    4   User aborted (declined confirmation)
    5   Write failed (.env not writable)
    130 Interrupted (Ctrl+C)

⚠️  **Security warning**: this script reads your browser cookies. Anyone
    with access to the resulting ``.env`` file can impersonate you on the
    target site. Keep ``.env`` private (``chmod 600``).

Requirements::

    pip install cryptography  # for Chrome/Edge/Brave decryption

Supported browsers::

    - Firefox   (no encryption, easiest)
    - Chrome    (AES-GCM decryption via OS keyring)
    - Edge      (same as Chrome)
    - Brave     (same as Chrome)

Known limitations::

    - **Chrome v127+ (App-Bound Encryption)** is not supported yet.
      Workaround: use Firefox, or manually copy the cookie value from
      DevTools → Application → Cookies.
    - **macOS Keychain** prompts for password on first access.
    - **Linux keyring** must be running (gnome-keyring or kwallet).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_FILE: Final[Path] = _REPO_ROOT / ".env"

# Site IDs from sites.yaml
_KNOWN_SITES: Final[tuple[str, ...]] = (
    "mangadex",
    "mangafire",
    "sushiscan",
    "nhentai",
    "hentaizone",
    "pururin",
    "scan-hentai",
    "x-manga",
    # add more as needed
)

# Browser display names
_BROWSER_NAMES: Final[dict[str, str]] = {
    "firefox": "Firefox",
    "chrome": "Chrome",
    "edge": "Edge",
    "brave": "Brave",
}

# Site → domain mapping (for cookie filtering)
_SITE_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    "nhentai": ("nhentai.net",),
    "mangadex": ("mangadex.org",),
    "mangafire": ("mangafire.to",),
    "sushiscan": ("sushiscan.net", "sushiscan.fr"),
    "hentaizone": ("hentaizone.com", "hentaizone.xxx"),
    "pururin": ("pururin.to", "pururin.io"),
    "scan-hentai": ("scan-hentai.com",),
    "x-manga": ("x-manga.net", "x-manga.org"),
}


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class Cookie:
    """A single cookie.

    Attributes:
        name: Cookie name (e.g. ``cf_clearance``).
        value: Cookie value.
        domain: Cookie domain (e.g. ``.nhentai.net``).
        path: Cookie path (default ``/``).
        expires_utc: Expiry timestamp (Unix epoch, 0 = session).
        is_secure: Whether the cookie is HTTPS-only.
        is_httponly: Whether the cookie is HTTP-only.
    """

    name: str
    value: str
    domain: str
    path: str = "/"
    expires_utc: int = 0
    is_secure: bool = False
    is_httponly: bool = False

    @property
    def is_expired(self) -> bool:
        if self.expires_utc == 0:
            return False  # session cookie, not expired
        return self.expires_utc < datetime.now(tz=timezone.utc).timestamp()

    def __str__(self) -> str:
        status = " (expired)" if self.is_expired else ""
        return f"{self.name}={self.value[:20]}... @ {self.domain}{status}"


@dataclass
class FetchReport:
    """Result of a fetch operation.

    Attributes:
        site: The site ID.
        browser: The browser name.
        cookies_found: All cookies for the site's domain.
        cookies_written: Cookies written to ``.env``.
        env_file: The ``.env`` file path.
        errors: Any errors encountered.
    """

    site: str
    browser: str
    cookies_found: list[Cookie] = field(default_factory=list)
    cookies_written: list[str] = field(default_factory=list)
    env_file: Path = _DEFAULT_ENV_FILE
    errors: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _is_site_known(site: str) -> bool:
    """Return ``True`` if ``site`` is in ``_KNOWN_SITES``."""
    return site in _KNOWN_SITES


def _site_domains(site: str) -> tuple[str, ...]:
    """Return the domains associated with ``site``."""
    return _SITE_DOMAINS.get(site, (f"{site}.com",))


def _domain_matches(cookie_domain: str, site_domains: tuple[str, ...]) -> bool:
    """Return ``True`` if ``cookie_domain`` matches any site domain.

    Args:
        cookie_domain: The cookie's domain (with or without leading dot).
        site_domains: The site's domains.

    Returns:
        ``True`` on match.
    """
    normalized = cookie_domain.lstrip(".").lower()
    for site_domain in site_domains:
        if normalized == site_domain or normalized.endswith("." + site_domain):
            return True
    return False


def _env_var_name(site: str, cookie_name: str) -> str:
    """Compute the env var name for a cookie.

    Args:
        site: Site ID (e.g. ``nhentai``).
        cookie_name: Cookie name (e.g. ``cf_clearance``).

    Returns:
        Env var name (e.g. ``NEXUSDL_NHENTAI_CF_CLEARANCE``).
    """
    site_part = site.upper().replace("-", "_")
    cookie_part = cookie_name.upper().replace("-", "_").replace(".", "_")
    return f"NEXUSDL_{site_part}_{cookie_part}"


def _quote_env_value(value: str) -> str:
    """Quote a value for ``.env`` (escape special characters).

    Args:
        value: Raw value.

    Returns:
        Quoted value safe for ``.env``.
    """
    # If value contains only safe chars, return as-is
    if all(c.isalnum() or c in "-_.~+/=" for c in value):
        return value
    # Otherwise, use double quotes and escape
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _backup_env(env_file: Path) -> Path | None:
    """Create a backup of ``env_file`` (``.env.bak``).

    Args:
        env_file: The ``.env`` file.

    Returns:
        The backup path, or ``None`` if no backup was created.
    """
    if not env_file.exists():
        return None
    backup = env_file.with_suffix(env_file.suffix + ".bak")
    try:
        shutil.copy2(env_file, backup)
        return backup
    except OSError:
        return None


# ────────────────────────────────────────────────────────────────────
# Firefox
# ────────────────────────────────────────────────────────────────────


def _firefox_profile_dirs() -> list[Path]:
    """Locate Firefox profile directories (cross-platform).

    Returns:
        List of profile directories.
    """
    system = platform.system()
    home = Path.home()

    if system == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
    elif system == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:  # Linux
        base = home / ".mozilla" / "firefox"

    if not base.exists():
        return []

    profiles: list[Path] = []
    for entry in base.iterdir():
        if entry.is_dir():
            cookies = entry / "cookies.sqlite"
            if cookies.exists():
                profiles.append(entry)
    return profiles


def read_firefox_cookies(site_domains: tuple[str, ...]) -> list[Cookie]:
    """Read cookies from Firefox.

    Args:
        site_domains: Domains to filter for.

    Returns:
        List of matching cookies.

    Raises:
        RuntimeError: If no Firefox profile is found or the DB is locked.
    """
    profiles = _firefox_profile_dirs()
    if not profiles:
        raise RuntimeError("No Firefox profile found. Open Firefox at least once.")

    cookies: list[Cookie] = []
    for profile in profiles:
        source = profile / "cookies.sqlite"
        if not source.exists():
            continue

        # Copy to temp (SQLite locks the live DB when Firefox is open)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_db = Path(tmp) / "cookies.sqlite"
            try:
                shutil.copy2(source, tmp_db)
            except OSError:
                continue

            try:
                with contextlib.closing(sqlite3.connect(str(tmp_db), timeout=5.0)) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
                        "FROM moz_cookies"
                    ).fetchall()
            except sqlite3.Error:
                continue

            for row in rows:
                if not _domain_matches(row["host"], site_domains):
                    continue
                cookies.append(
                    Cookie(
                        name=row["name"],
                        value=row["value"],
                        domain=row["host"],
                        path=row["path"] or "/",
                        expires_utc=int(row["expiry"] or 0),
                        is_secure=bool(row["isSecure"]),
                        is_httponly=bool(row["isHttpOnly"]),
                    )
                )
    return cookies


# ────────────────────────────────────────────────────────────────────
# Chrome / Edge / Brave (Chromium)
# ────────────────────────────────────────────────────────────────────


def _chromium_paths(browser: str) -> tuple[Path | None, Path | None]:
    """Locate Chromium-based browser's cookie DB and key file.

    Args:
        browser: ``chrome``, ``edge``, or ``brave``.

    Returns:
        ``(cookies_db, key_file)`` — either may be ``None``.
    """
    system = platform.system()
    home = Path.home()

    if browser == "chrome":
        base_name = "Chrome"
    elif browser == "edge":
        base_name = "Edge"
    elif browser == "brave":
        base_name = "BraveSoftware/Brave-Browser"
    else:
        return None, None

    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / base_name / "User Data"
    elif system == "Darwin":
        base = home / "Library" / "Application Support" / base_name
    else:  # Linux
        base = home / ".config" / base_name.replace("/", "/")

    cookies_db = base / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        cookies_db = base / "Default" / "Cookies"  # older layout
    if not cookies_db.exists():
        return None, None

    if system == "Windows":
        key_file = None  # key is in Local State
    elif system == "Darwin":
        key_file = None  # key is in macOS Keychain
    else:
        key_file = None  # key is in gnome-keyring/kwallet

    return cookies_db, key_file


def _get_chromium_encryption_key() -> bytes | None:
    """Retrieve the Chromium encryption key from the OS keyring.

    Returns:
        The 16-byte AES key, or ``None`` if unavailable.
    """
    system = platform.system()

    if system == "Linux":
        # Try gnome-keyring or kwallet via `keyring` package
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            return None

        for service in ("Chrome Safe Storage", "Chromium Safe Storage", "Brave Safe Storage"):
            try:
                password = keyring.get_password(service, "Chrome")
                if password:
                    return _derive_key_from_password(password)
            except Exception:  # noqa: BLE001
                continue
        return None

    if system == "Darwin":
        # macOS Keychain via `security` command
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return _derive_key_from_password(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    if system == "Windows":
        # DPAPI — requires `pywin32` or `win32crypt`
        try:
            import win32crypt  # type: ignore[import-not-found]
        except ImportError:
            return None

        # Read Local State to find the encrypted key
        local_state = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Local State"
        if not local_state.exists():
            return None

        try:
            with local_state.open(encoding="utf-8") as f:
                state = json.load(f)
            encrypted_key_b64 = state.get("os_crypt", {}).get("encrypted_key")
            if not encrypted_key_b64:
                return None
            import base64

            encrypted_key = base64.b64decode(encrypted_key_b64)
            # Strip "DPAPI" prefix
            encrypted_key = encrypted_key[5:]
            key, _ = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)
            return key
        except Exception:  # noqa: BLE001
            return None

    return None


def _derive_key_from_password(password: str) -> bytes:
    """Derive the 16-byte AES key from the Chromium "Safe Storage" password.

    Chromium uses PBKDF2-HMAC-SHA1 with 1 iteration and a fixed salt
    (``saltysalt``) to derive the key.

    Args:
        password: The Safe Storage password.

    Returns:
        16-byte AES key.
    """
    import hashlib

    return hashlib.pbkdf2_hmac(
        "sha1",
        password.encode("utf-8"),
        b"saltysalt",
        1,
        dklen=16,
    )


def _decrypt_chromium_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt a Chromium cookie value (AES-GCM or AES-CBC).

    Args:
        encrypted: The encrypted value bytes.
        key: The 16-byte AES key.

    Returns:
        The decrypted string (or ``""`` on failure).
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return ""

    # v10 prefix = AES-CBC (older), v20 = AES-GCM (newer)
    if encrypted.startswith(b"v10"):
        # AES-CBC: 3-byte prefix + IV (16 bytes) + ciphertext
        iv = b" " * 16
        ciphertext = encrypted[3:]
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(ciphertext) + decryptor.finalize()
            # Remove PKCS7 padding
            padding_len = decrypted[-1]
            return decrypted[:-padding_len].decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    if encrypted.startswith(b"v20"):
        # AES-GCM: 3-byte prefix + nonce (12 bytes) + ciphertext + tag
        nonce = encrypted[3:15]
        ciphertext = encrypted[15:]
        try:
            aesgcm = AESGCM(key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, None)
            return decrypted.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    # No prefix — plaintext (rare, older Chromium)
    return encrypted.decode("utf-8", errors="replace")


def read_chromium_cookies(browser: str, site_domains: tuple[str, ...]) -> list[Cookie]:
    """Read cookies from Chrome/Edge/Brave.

    Args:
        browser: ``chrome``, ``edge``, or ``brave``.
        site_domains: Domains to filter for.

    Returns:
        List of matching cookies.

    Raises:
        RuntimeError: If the browser is not found or the DB is locked.
    """
    cookies_db, _ = _chromium_paths(browser)
    if cookies_db is None:
        raise RuntimeError(f"{_BROWSER_NAMES[browser]} cookies not found.")

    # Copy to temp (Chrome locks the DB when open)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "Cookies"
        try:
            shutil.copy2(cookies_db, tmp_db)
        except OSError as exc:
            raise RuntimeError(f"Cannot copy cookie DB: {exc}") from exc

        cookies: list[Cookie] = []
        try:
            with contextlib.closing(sqlite3.connect(str(tmp_db), timeout=5.0)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT name, encrypted_value, host_key, path, expires_utc, is_secure, is_httponly "
                    "FROM cookies"
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Cannot read cookie DB: {exc}") from exc

        key = _get_chromium_encryption_key()
        if key is None and any(row["encrypted_value"] for row in rows):
            raise RuntimeError(
                f"{_BROWSER_NAMES[browser]}: cannot decrypt cookies "
                "(OS keyring unavailable). Try Firefox instead, or copy manually."
            )

        for row in rows:
            if not _domain_matches(row["host_key"], site_domains):
                continue

            encrypted = row["encrypted_value"]
            if encrypted and key:
                value = _decrypt_chromium_value(encrypted, key)
            else:
                value = row["encrypted_value"].decode("utf-8", errors="replace") if encrypted else ""

            if not value:
                continue

            # expires_utc is in microseconds since 1601-01-01 (Chrome epoch)
            expires_chrome = row["expires_utc"] or 0
            if expires_chrome > 0:
                # Convert to Unix epoch (seconds)
                expires_unix = int((expires_chrome / 1_000_000) - 11644473600)
            else:
                expires_unix = 0

            cookies.append(
                Cookie(
                    name=row["name"],
                    value=value,
                    domain=row["host_key"],
                    path=row["path"] or "/",
                    expires_utc=expires_unix,
                    is_secure=bool(row["is_secure"]),
                    is_httponly=bool(row["is_httponly"]),
                )
            )

        return cookies


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────


def fetch_cookies(
    site: str,
    browser: str,
) -> list[Cookie]:
    """Fetch cookies from a browser for a site.

    Args:
        site: Site ID.
        browser: Browser name (``firefox``, ``chrome``, ``edge``, ``brave``).

    Returns:
        List of cookies (may be empty).

    Raises:
        RuntimeError: If the browser is not supported or the DB is inaccessible.
    """
    site_domains = _site_domains(site)

    if browser == "firefox":
        return read_firefox_cookies(site_domains)
    if browser in ("chrome", "edge", "brave"):
        return read_chromium_cookies(browser, site_domains)

    raise RuntimeError(f"Unsupported browser: {browser}")


# ────────────────────────────────────────────────────────────────────
# .env writing
# ────────────────────────────────────────────────────────────────────


def _read_env(env_file: Path) -> dict[str, str]:
    """Read a ``.env`` file into a dict.

    Args:
        env_file: The ``.env`` file.

    Returns:
        Dict of ``KEY=VALUE`` (empty if file doesn't exist).
    """
    if not env_file.exists():
        return {}

    result: dict[str, str] = {}
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _write_env(env_file: Path, updates: dict[str, str]) -> None:
    """Update a ``.env`` file with new key-value pairs (merge).

    Args:
        env_file: The ``.env`` file.
        updates: The key-value pairs to add/update.

    Raises:
        OSError: If writing fails.
    """
    existing = _read_env(env_file)
    existing.update(updates)

    # Preserve original order, then append new keys
    lines: list[str] = []
    seen: set[str] = set()

    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                else:
                    lines.append(line)
            else:
                lines.append(line)

    # Append new keys
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_cookies_to_env(
    cookies: list[Cookie],
    site: str,
    env_file: Path,
    *,
    cookie_names: list[str] | None = None,
    dry_run: bool = True,
) -> list[str]:
    """Write cookies to ``.env``.

    Args:
        cookies: The cookies to write.
        site: The site ID.
        env_file: The ``.env`` file.
        cookie_names: If set, only write these cookies.
        dry_run: If ``True``, do not modify the file.

    Returns:
        List of written env var names.

    Raises:
        OSError: If writing fails.
    """
    written: list[str] = []

    updates: dict[str, str] = {}
    for cookie in cookies:
        if cookie_names and cookie.name not in cookie_names:
            continue
        if cookie.is_expired:
            continue
        env_name = _env_var_name(site, cookie.name)
        updates[env_name] = _quote_env_value(cookie.value)

    if dry_run or not updates:
        return list(updates.keys())

    _write_env(env_file, updates)
    written = list(updates.keys())
    return written


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="fetch_cookies",
        description=(
            "Fetch cookies from a browser and write them to .env. "
            "Defaults to --dry-run: pass --yes to actually write."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                            # interactive
  %(prog)s --site nhentai --browser chrome --list     # list cookies
  %(prog)s --site nhentai --browser chrome --cookie cf_clearance
  %(prog)s --site nhentai --browser chrome --cookie cf_clearance --yes
  %(prog)s --site nhentai --all-cookies --yes         # all cookies

Supported browsers: firefox, chrome, edge, brave
Known sites: """ + ", ".join(_KNOWN_SITES) + """

Exit codes: 0 = OK, 1 = not found, 2 = bad args, 3 = missing prereq,
            4 = aborted, 5 = write failed
        """,
    )

    parser.add_argument(
        "--site",
        default="",
        help="Site ID (e.g. nhentai). Required unless --interactive.",
    )
    parser.add_argument(
        "--browser",
        choices=["firefox", "chrome", "edge", "brave"],
        default="firefox",
        help="Browser to read cookies from (default: firefox)",
    )
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        help="Cookie name(s) to write (e.g. cf_clearance). Repeatable.",
    )
    parser.add_argument(
        "--all-cookies",
        action="store_true",
        help="Write all cookies for the site (requires --yes to write).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=_DEFAULT_ENV_FILE,
        help=f".env file path (default: {_DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Write to .env (default: dry-run)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List cookies without writing",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Interactive prompts",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of text",
    )

    return parser


def _interactive_prompt() -> tuple[str, str, list[str]]:
    """Prompt the user for site, browser, and cookie names.

    Returns:
        ``(site, browser, cookie_names)``.
    """
    print(f"{_C_BOLD if '_C_BOLD' in dir() else ''}NexusDL — cookie fetcher (interactive){_C_RESET if '_C_RESET' in dir() else ''}")
    print()

    print("Known sites:")
    for i, site in enumerate(_KNOWN_SITES, 1):
        print(f"  {i}. {site}")
    site_input = input("Site (name or number): ").strip()
    if site_input.isdigit():
        idx = int(site_input) - 1
        site = _KNOWN_SITES[idx] if 0 <= idx < len(_KNOWN_SITES) else ""
    else:
        site = site_input

    print()
    print("Browsers: firefox, chrome, edge, brave")
    browser = input("Browser [firefox]: ").strip() or "firefox"

    print()
    print("Cookie names (comma-separated, empty = all):")
    cookies_input = input("Cookies: ").strip()
    cookie_names = [c.strip() for c in cookies_input.split(",") if c.strip()] if cookies_input else []

    return site, browser, cookie_names


def _check_cryptography_for_chromium(browser: str) -> None:
    """Exit if ``cryptography`` is missing but required for ``browser``.

    Args:
        browser: Browser name.

    Raises:
        SystemExit: If ``cryptography`` is missing and browser is Chromium-based.
    """
    if browser not in ("chrome", "edge", "brave"):
        return
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print(
            "❌ 'cryptography' package required for Chromium-based browsers.",
            file=sys.stderr,
        )
        print("   Install with: pip install cryptography", file=sys.stderr)
        sys.exit(3)


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args()

    # Interactive mode
    site = args.site
    browser = args.browser
    cookie_names = args.cookie
    if args.interactive or not site:
        site, browser, cookie_names = _interactive_prompt()

    if not site:
        print("❌ No site specified.", file=sys.stderr)
        return 2

    if not _is_site_known(site):
        print(
            f"⚠️  Unknown site '{site}'. Continuing anyway (domains may not match).",
            file=sys.stderr,
        )

    _check_cryptography_for_chromium(browser)

    # Fetch
    try:
        cookies = fetch_cookies(site, browser)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted", file=sys.stderr)
        return 130

    # Filter expired
    valid_cookies = [c for c in cookies if not c.is_expired]
    expired_count = len(cookies) - len(valid_cookies)

    if not valid_cookies:
        print(
            f"❌ No valid cookies found for {site} in {_BROWSER_NAMES[browser]}.",
            file=sys.stderr,
        )
        print(
            f"   Did you visit {_site_domains(site)[0]} in {_BROWSER_NAMES[browser]}?",
            file=sys.stderr,
        )
        return 1

    # Filter by name if requested
    if cookie_names and not args.all_cookies:
        valid_cookies = [c for c in valid_cookies if c.name in cookie_names]
        if not valid_cookies:
            print(
                f"❌ None of the requested cookies ({', '.join(cookie_names)}) were found.",
                file=sys.stderr,
            )
            return 1

    # List mode
    if args.list or not args.yes:
        print()
        print(f"Found {len(valid_cookies)} valid cookie(s) for {site} in {_BROWSER_NAMES[browser]}:")
        if expired_count:
            print(f"  ({expired_count} expired cookie(s) ignored)")
        print()
        for c in valid_cookies:
            print(f"  {c.name}")
            print(f"    value:  {c.value[:40]}{'...' if len(c.value) > 40 else ''}")
            print(f"    domain: {c.domain}")
            print(f"    path:   {c.path}")
            print(f"    secure: {c.is_secure}")
            print(f"    httponly: {c.is_httponly}")
            print()

        if not args.yes:
            print(f"Run with --yes to write these to {args.env_file}.")
            return 0

    # Write mode
    dry_run = not args.yes
    try:
        written = write_cookies_to_env(
            valid_cookies,
            site,
            args.env_file,
            cookie_names=cookie_names if cookie_names else None,
            dry_run=dry_run,
        )
    except OSError as exc:
        print(f"❌ Cannot write to {args.env_file}: {exc}", file=sys.stderr)
        return 5

    if dry_run:
        print(f"Would write {len(written)} cookie(s) to {args.env_file} (dry-run).")
        return 0

    print(f"✓ Wrote {len(written)} cookie(s) to {args.env_file}:")
    for name in written:
        print(f"  {name}=***")

    if args.verbose:
        backup = _backup_env(args.env_file)
        if backup:
            print(f"  backup: {backup}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted", file=sys.stderr)
        raise SystemExit(130) from None
