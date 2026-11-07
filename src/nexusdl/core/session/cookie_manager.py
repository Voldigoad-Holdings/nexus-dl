"""Encrypted, persistent cookie store with per-site metadata and env overrides.

Cookies are the authentication state of every site the pipeline talks to.
A Cloudflare clearance, a session ID, a CSRF token — losing them means
re-logging in, and leaking them means anyone with access to the store can
impersonate the user. This module is the store.

Security model
--------------
* **Encryption at rest** — the entire file is encrypted with
  :class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256, the
  standard authenticated-encryption construction from the ``cryptography``
  library). The key is a 32-byte URL-safe base64 string, passed at
  construction time as :class:`pydantic.SecretStr` so it can never leak
  through a ``repr`` or a log line.
* **File permissions** — ``chmod 600`` on every write. On POSIX, only
  the owning user can read or modify the file. On Windows, the
  corresponding permission bit is set but the semantics differ; the
  encryption is the real defense on every platform. On POSIX, a
  failed ``chmod`` raises — a cookie file world-readable is a security
  defect, not a warning.
* **Debug-safe reprs** — :class:`CookieEntry` redacts its value in
  ``repr`` and ``str``. Logging a full cookie jar by accident prints
  ``value='***'`` instead of the session token.
* **In memory** — cookie values are plain :class:`str`. The
  encryption protects them **at rest**; runtime access is limited by
  process isolation, which is the OS's job, not this module's.
* **Value validation** — cookie values are validated on construction:
  CR/LF/NUL are rejected (header injection / protocol errors), and
  semicolons are rejected (they would smuggle a second cookie when
  the value is interpolated into the ``Cookie`` header).

Encryption is not optional. A deployment that wants a plaintext file is
one bad configuration away from leaking every auth token on the host, and
the performance cost of Fernet on a few KB of cookie data is
imperceptible.

Two merge sources
-----------------
The effective cookie jar for a site is the **union** of two layers:

1. **The encrypted file** — persisted cookies, typically collected from
   ``Set-Cookie`` headers during previous sessions.
2. **Environment variables** — cookies injected by the operator via
   ``NEXUSDL_<SITE>_<COOKIE>=value``, typically used to seed a
   Cloudflare clearance or a manually-obtained session token before the
   first request. The ``NEXUSDL_`` prefix is matched case-insensitively
   (matching :class:`~nexusdl.core.config.Settings`'s
   ``case_sensitive=False``); the site id is matched case-insensitively
   against ``known_sites``; the cookie name is preserved in its original
   case.

**Environment variables win over the file.** Rationale: an operator who
sets ``NEXUSDL_MANGADEX_cf_clearance=xyz`` in ``.env`` wants that exact
value used. If the file always won, the escape hatch would be useless.

The format of the env var name is documented in
:func:`_parse_env_cookies`. Because site ids can contain underscores, the
manager needs the list of known sites to disambiguate — the composition
root passes it as ``known_sites``. Without that list, env cookies are not
parsed (documented and logged).

Persistence is explicit
-----------------------
The manager does **not** auto-save. ``update_cookies`` from a response is
a dict update in memory; persisting to disk is a separate, explicit
``save()`` call. Rationale: auto-save would perform disk I/O on every
request, which is both slow and unnecessary — the composition root saves
at strategic points (after authentication, on shutdown, periodically).

A typical lifecycle::

    manager = CookieManager(
        key=settings.cookie_encryption_key,
        path=settings.paths.cookies_file,
        env=os.environ,
        known_sites=registry.list_sites(include_disabled=True),
    )
    manager.load()           # may be a no-op on first run
    try:
        # ... requests, which call get_cookies / update_cookies ...
        pass
    finally:
        manager.save()       # explicit persistence

Thread safety
-------------
Every public method is guarded by a :class:`threading.RLock`. The critical
sections are small dict operations, so the lock is never contended in
practice — it exists to make the manager usable from a thread pool
without surprising readers.

The manager is **not** asyncio-native (all its methods are synchronous)
because :class:`~nexusdl.core.session.http_session.HttpSession` calls
:meth:`get_cookies` and :meth:`update_cookies` from synchronous helpers
(``_build_request_headers`` and ``_persist_cookies`` respectively). Making
these async would force the session's helpers to be async, which would
propagate asyncness into code that has no I/O to await.

A caller who wants to run :meth:`load` or :meth:`save` in a worker thread
uses :func:`asyncio.to_thread`. Both are idempotent and short — the
overhead of a thread hop is unnecessary but harmless.

Dependency graph (no cycles)::

    core.exceptions            ─┐
    pydantic.SecretStr          ├──► core.session.cookie_manager
    cryptography.fernet         │
    loguru, asyncio, json, os,  │
    pathlib, tempfile, threading│

No ``nexusdl.core`` import besides ``core.exceptions`` — this module is a
leaf of the session package.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from pydantic import SecretStr

from nexusdl.core.exceptions import CookieEncryptionError


__all__ = [
    "ENV_PREFIX",
    "FILE_VERSION",
    "CookieEntry",
    "CookieManager",
    "CookieStats",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Current file format version. Incremented when the JSON schema changes
#: in a way that requires a migration. Older versions are readable by
#: :meth:`CookieManager.load`; newer versions are rejected so a downgrade
#: cannot silently corrupt the file.
FILE_VERSION: Final[int] = 1

#: Prefix for environment-variable cookie overrides. See
#: :func:`_parse_env_cookies` for the exact format. Matched
#: case-insensitively against the env var name (matching
#: ``Settings.case_sensitive=False``).
ENV_PREFIX: Final[str] = "NEXUSDL_"

#: Default threshold, in seconds, for :meth:`CookieManager.needs_refresh`.
#: A cookie expiring within one hour is treated as "needs refresh" — long
#: enough that a background refresh has time to run, short enough that a
#: request does not fail mid-download.
DEFAULT_REFRESH_THRESHOLD_SECONDS: Final[float] = 3600.0

#: Number of bytes of the Fernet key. Fernet requires a 32-byte URL-safe
#: base64 string (44 characters including padding).
_FERNET_KEY_LENGTH: Final[int] = 44

#: Characters that must never appear in a cookie value: CR/LF/NUL break
#: the HTTP header protocol, and ``;`` would smuggle a second cookie
#: through the ``Cookie:`` header interpolation done by
#: :class:`~nexusdl.core.session.http_session.HttpSession`.
_FORBIDDEN_VALUE_CHARS: Final[frozenset[str]] = frozenset(
    {"\r", "\n", "\0", ";"}
)

_log = logger.bind(component="session.cookie")


# --------------------------------------------------------------------------- #
# CookieEntry                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CookieEntry:
    """One cookie, with its value and optional expiry metadata.

    Immutable. The value is a plain :class:`str` — the encryption
    boundary is the on-disk file, not the in-memory object. The
    :meth:`__repr__` redacts the value so a log line never leaks a token.

    **Value validation.** The value is checked at construction time for
    characters that would corrupt the ``Cookie`` header when the
    session interpolates it (CR, LF, NUL) and for ``;`` which would
    smuggle a second cookie into the header. This protects against
    server-side attacks (a malicious ``Set-Cookie``) and operator
    mistakes (a malformed env override). Rejected values raise
    :class:`ValueError`; the error message redacts the value.

    Attributes:
        value: The cookie's value, as it goes into the ``Cookie`` header.
            Must not contain CR, LF, NUL, or ``;``.
        expires_at: Optional Unix timestamp (seconds) at which the cookie
            becomes invalid. ``None`` means "session cookie" — valid
            until the process exits. A numeric value means the server
            told us when it expires (via ``Max-Age`` or ``Expires`` in a
            ``Set-Cookie`` header), and the manager uses it for
            :meth:`CookieManager.needs_refresh` and
            :meth:`CookieManager.get_cookies`'s expiry filter.
    """

    value: str
    expires_at: float | None = None

    def __post_init__(self) -> None:
        """Validate the cookie value.

        Raises:
            TypeError: If ``value`` is not a :class:`str`.
            ValueError: If ``value`` contains CR, LF, NUL, or ``;``.
                The error message includes the offending character
                category but never the value itself.
        """
        if not isinstance(self.value, str):
            raise TypeError(
                f"CookieEntry.value must be a str, got "
                f"{type(self.value).__name__}"
            )
        for char in _FORBIDDEN_VALUE_CHARS:
            if char in self.value:
                category = {
                    "\r": "CR",
                    "\n": "LF",
                    "\0": "NUL",
                    ";": "semicolon (would smuggle a second cookie)",
                }[char]
                raise ValueError(
                    f"cookie value contains a forbidden {category}; "
                    f"refusing to construct CookieEntry"
                )

    def is_expired(
        self,
        now: float,
        *,
        threshold_seconds: float = 0.0,
    ) -> bool:
        """Return ``True`` iff the cookie is expired or about to expire.

        A session cookie (``expires_at is None``) is never considered
        expired — the browser model treats it as valid until the process
        ends.

        Args:
            now: The reference time, as a Unix timestamp in seconds.
            threshold_seconds: Expiry window. A cookie whose
                ``expires_at`` is within ``threshold_seconds`` of *now*
                is treated as already expired — useful for proactive
                refresh.

        Returns:
            ``True`` iff ``expires_at is not None`` and
            ``expires_at <= now + threshold_seconds``.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= now + threshold_seconds

    def __repr__(self) -> str:
        """Return a debug representation with the value masked."""
        return (
            f"CookieEntry(value='***', "
            f"expires_at={self.expires_at!r})"
        )

    def __str__(self) -> str:
        """Return ``name=***`` for safe log output."""
        return "CookieEntry(value='***')"


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CookieStats:
    """Immutable aggregate snapshot of a :class:`CookieManager`'s state.

    Returned by :meth:`CookieManager.stats`. The CLI's
    ``nexusdl cookies stats`` subcommand and the diagnostic endpoint
    render these fields verbatim.

    Attributes:
        site_count: Total number of sites with any tracked cookies,
            including sites whose cookies have all expired.
        total_cookies: Total number of cookie entries across all sites.
        expired_cookies: Subset of :attr:`total_cookies` that are expired
            at the time of the snapshot. Expired cookies are still
            stored but not returned by :meth:`CookieManager.get_cookies`.
        env_override_sites: Number of sites that have at least one env-
            var override active. These sites always report the env value
            for those specific cookies, regardless of what is on disk.
    """

    site_count: int
    total_cookies: int
    expired_cookies: int
    env_override_sites: int


# --------------------------------------------------------------------------- #
# CookieManager                                                               #
# --------------------------------------------------------------------------- #


class CookieManager:
    """Encrypted, persistent per-site cookie store.

    One instance per composition root, used for the process lifetime.
    See the module docstring for the security model, the merge sources,
    and the thread-safety contract.

    Example:
        >>> manager = CookieManager(
        ...     key=settings.cookie_encryption_key,
        ...     path=settings.paths.cookies_file,
        ...     env=os.environ,
        ...     known_sites=("mangadex", "comick"),
        ... )
        >>> manager.load()
        >>> manager.get_cookies("mangadex")
        {'cf_clearance': 'abc123'}
        >>> manager.update_cookies("mangadex", {"session": "xyz789"})
        >>> manager.save()

    Args:
        key: A Fernet key — 32 bytes, URL-safe base64 encoded, 44
            characters. Typically sourced from
            ``NEXUSDL_COOKIE_ENCRYPTION_KEY`` at application startup. The
            composition root is responsible for generating one (via
            :meth:`cryptography.fernet.Fernet.generate_key`) if the
            environment does not provide one.
        path: Filesystem path to the encrypted cookie file. The parent
            directory is created on :meth:`save` if missing.
        env: Optional environment mapping (typically ``os.environ``) from
            which to read ``NEXUSDL_<SITE>_<COOKIE>`` overrides. ``None``
            disables env merging — the manager will only reflect the
            encrypted file.
        known_sites: The list of site ids that env vars may reference.
            Required for disambiguation because site ids may contain
            underscores (``my_site`` vs ``my``) and the env var format
            uses underscores as separators. When empty (the default),
            env cookies are ignored — this is the safe default for
            tests and for callers that do not use env overrides.
        refresh_threshold_seconds: Default expiry window used by
            :meth:`needs_refresh`. See
            :data:`DEFAULT_REFRESH_THRESHOLD_SECONDS`.
        clock: Callable returning the current Unix timestamp in seconds.
            Defaults to :func:`time.time`. Injectable for deterministic
            tests of expiry behaviour.

    Raises:
        ValueError: If ``key`` is not a valid Fernet key (wrong length,
            wrong encoding, or non-base64). The check is eager so a
            misconfigured deployment fails at startup, not at first use.
        TypeError: If ``key`` is not a :class:`~pydantic.SecretStr`.
    """

    __slots__ = (
        "_clock",
        "_env_cookies",
        "_fernet",
        "_lock",
        "_path",
        "_refresh_threshold",
        "_site_cookies",
        "_sites_with_env_overrides",
    )

    def __init__(
        self,
        *,
        key: SecretStr,
        path: Path,
        env: Mapping[str, str] | None = None,
        known_sites: Sequence[str] = (),
        refresh_threshold_seconds: float = DEFAULT_REFRESH_THRESHOLD_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(key, SecretStr):
            raise TypeError(
                f"key must be a SecretStr, got {type(key).__name__}"
            )
        raw_key = key.get_secret_value()
        if not raw_key or len(raw_key) != _FERNET_KEY_LENGTH:
            raise ValueError(
                f"key must be a {_FERNET_KEY_LENGTH}-character Fernet "
                f"key; got {len(raw_key)} characters. Generate one with "
                f"cryptography.fernet.Fernet.generate_key().decode()"
            )
        try:
            fernet = Fernet(raw_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError(
                f"key is not a valid Fernet key: "
                f"{type(exc).__name__}: {exc}. Generate one with "
                f"cryptography.fernet.Fernet.generate_key().decode()"
            ) from exc

        self._fernet: Fernet = fernet
        self._path: Path = path
        self._clock: Callable[[], float] = clock
        self._refresh_threshold: float = float(refresh_threshold_seconds)
        self._lock = threading.RLock()

        #: In-memory state, keyed by site id. Populated by :meth:`load`
        #: and mutated by the ``set_*`` / ``update_*`` / ``clear_*`` /
        #: ``delete`` methods.
        self._site_cookies: dict[str, dict[str, CookieEntry]] = {}

        #: Env-var overrides, keyed by site id. Populated once at
        #: construction; never mutated afterwards. Values in this map
        #: take precedence over anything in ``_site_cookies``.
        self._env_cookies: dict[str, dict[str, str]] = (
            _parse_env_cookies(env, known_sites)
            if env is not None and known_sites
            else {}
        )

        #: Cached set of site ids that have at least one env override,
        #: computed once so :meth:`stats` does not re-walk the map.
        self._sites_with_env_overrides: frozenset[str] = frozenset(
            self._env_cookies
        )

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Load and decrypt the cookie file into memory.

        Idempotent. On the first run — no file, or an empty file — this
        is a no-op that leaves the manager with an empty jar (plus any
        env-var overrides parsed at construction time). Env-var
        overrides are **re-merged on top** of the file's contents, so
        the "env wins" invariant holds across load calls.

        Raises:
            CookieEncryptionError: If the file exists but cannot be
                decrypted (wrong key, or the file has been corrupted),
                or if the decrypted payload is not a valid cookie
                document (malformed JSON, unknown schema version).
        """
        with self._lock:
            if not self._path.is_file():
                _log.debug(
                    "Cookie file does not exist yet at {}; starting "
                    "with an empty jar",
                    self._path,
                )
                return

            try:
                ciphertext = self._path.read_bytes()
            except OSError as exc:
                raise CookieEncryptionError(
                    f"cannot read cookie file {self._path}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            if not ciphertext:
                _log.debug(
                    "Cookie file at {} is empty; starting with an "
                    "empty jar",
                    self._path,
                )
                return

            try:
                plaintext = self._fernet.decrypt(ciphertext)
            except InvalidToken as exc:
                raise CookieEncryptionError(
                    f"cannot decrypt cookie file {self._path}: wrong "
                    f"encryption key or corrupted file. Delete the file "
                    f"to start fresh if you cannot recover the key."
                ) from exc

            try:
                document = json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CookieEncryptionError(
                    f"cookie file {self._path} decrypted to invalid JSON: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            if not isinstance(document, dict):
                raise CookieEncryptionError(
                    f"cookie file {self._path} decrypted to a non-object "
                    f"payload ({type(document).__name__})"
                )

            version = document.get("version")
            if not isinstance(version, int):
                raise CookieEncryptionError(
                    f"cookie file {self._path} has an invalid version "
                    f"field: {version!r}"
                )
            if version > FILE_VERSION:
                raise CookieEncryptionError(
                    f"cookie file {self._path} uses format version "
                    f"{version}, but this build only supports up to "
                    f"{FILE_VERSION}. Upgrade NexusDL or delete the file "
                    f"to start fresh."
                )
            if version < 1:
                raise CookieEncryptionError(
                    f"cookie file {self._path} has version {version}, "
                    f"which is not a valid format version"
                )

            raw_sites = document.get("sites")
            if not isinstance(raw_sites, dict):
                raise CookieEncryptionError(
                    f"cookie file {self._path} is missing the 'sites' "
                    f"object"
                )

            new_state: dict[str, dict[str, CookieEntry]] = {}
            for site_id, raw_entries in raw_sites.items():
                if not isinstance(site_id, str):
                    raise CookieEncryptionError(
                        f"cookie file {self._path} has a non-string "
                        f"site id: {site_id!r}"
                    )
                if not isinstance(raw_entries, dict):
                    raise CookieEncryptionError(
                        f"cookie file {self._path}: site {site_id!r} "
                        f"is not an object"
                    )
                parsed: dict[str, CookieEntry] = {}
                for cookie_name, raw_entry in raw_entries.items():
                    if not isinstance(cookie_name, str):
                        raise CookieEncryptionError(
                            f"cookie file {self._path}: site "
                            f"{site_id!r} has a non-string cookie name: "
                            f"{cookie_name!r}"
                        )
                    parsed[cookie_name] = _parse_entry(
                        site_id, cookie_name, raw_entry, self._path
                    )
                new_state[site_id] = parsed

            self._site_cookies = new_state
            _log.info(
                "Loaded {} site(s) of cookies from {}",
                len(new_state),
                self._path,
            )

    def save(self) -> None:
        """Encrypt the in-memory jar and write it to disk atomically.

        Creates the parent directory if it does not exist. The file is
        written to a sibling temp file, encrypted, ``chmod 600``, then
        :meth:`Path.replace`'d onto the final path. A crash or I/O
        failure at any point leaves the previous file intact.

        On POSIX, a failed ``chmod`` **raises** :class:`CookieEncryptionError`
        — a cookie file readable by other users on the host is a
        security defect, not a warning. On Windows, the failure is
        logged at DEBUG (the underlying ``os.chmod`` sets the read-only
        bit, which is cosmetic; the encryption layer provides the real
        protection).

        Raises:
            CookieEncryptionError: If serialization or encryption fails,
                or if ``chmod`` fails on POSIX.
            OSError: If the file cannot be written. The previous file
                (if any) is preserved.
        """
        with self._lock:
            payload = self._serialize_locked()
            try:
                ciphertext = self._fernet.encrypt(payload)
            except Exception as exc:  # noqa: BLE001 — fernet raises its own type
                raise CookieEncryptionError(
                    f"cannot encrypt cookie data: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_temp = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.tmp-",
                suffix=".tmp",
            )
            temp_path = Path(raw_temp)
            try:
                # ``os.fdopen`` can raise ``MemoryError`` or similar
                # before the ``with`` block establishes the buffered
                # stream. If that happens, the raw ``fd`` from
                # ``mkstemp`` would leak until the GC finalises it.
                # ``try/except BaseException`` with an explicit
                # ``os.close`` in the failure branch closes the fd
                # unconditionally; the normal path lets the ``with``
                # block close it.
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(ciphertext)
                        fh.flush()
                        os.fsync(fh.fileno())
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                # chmod BEFORE replace: the final path must never be
                # visible with default umask. On POSIX a failure here
                # is fatal (see docstring); on Windows it is a
                # best-effort no-op.
                _chmod_owner_only(temp_path)
                temp_path.replace(self._path)
            except (OSError, CookieEncryptionError):
                _safe_unlink(temp_path)
                raise
            except BaseException:
                _safe_unlink(temp_path)
                raise

            _log.debug(
                "Saved {} site(s) of cookies to {}",
                len(self._site_cookies),
                self._path,
            )

    # ------------------------------------------------------------------ #
    # Read API                                                           #
    # ------------------------------------------------------------------ #

    def get_cookies(self, site_id: str) -> Mapping[str, str]:
        """Return the effective cookies for *site_id*, as ``name → value``.

        Merges the encrypted file and env-var overrides (env wins),
        filters out expired cookies, and returns a read-only mapping.
        This is the method :class:`HttpSession` calls before every
        request.

        Args:
            site_id: Canonical site id.

        Returns:
            A mapping of cookie name to value. Empty if the site has no
            valid cookies (unknown site, no cookies yet, or all expired).
            The mapping is a fresh :class:`types.MappingProxyType` — the
            caller may hold it, mutate it via ``dict(...)``, or compare
            it without affecting the manager's state.
        """
        entries = self.get_cookie_entries(site_id)
        return MappingProxyType(
            {name: entry.value for name, entry in entries.items()}
        )

    def get_cookie_entries(
        self, site_id: str
    ) -> Mapping[str, CookieEntry]:
        """Return the effective cookies for *site_id*, with full metadata.

        Like :meth:`get_cookies`, but preserves the :class:`CookieEntry`
        objects so callers can inspect expiry. Filters out expired
        cookies. Env-var overrides are projected onto entries with
        ``expires_at=None`` (env vars cannot carry expiry information).

        Args:
            site_id: Canonical site id.

        Returns:
            A read-only mapping of cookie name to :class:`CookieEntry`.
        """
        with self._lock:
            now = self._clock()
            merged: dict[str, CookieEntry] = {}

            stored = self._site_cookies.get(site_id, {})
            for name, entry in stored.items():
                if not entry.is_expired(now):
                    merged[name] = entry

            env_overrides = self._env_cookies.get(site_id, {})
            for name, value in env_overrides.items():
                merged[name] = CookieEntry(value=value, expires_at=None)

            return MappingProxyType(merged)

    def list_sites(self) -> list[str]:
        """Return every site id the manager knows about, sorted.

        Includes sites whose cookies are all expired, and sites known
        only via env overrides. Does not include sites that never had
        any cookies at all.
        """
        with self._lock:
            return sorted(
                set(self._site_cookies) | set(self._env_cookies)
            )

    def get_expired(self, site_id: str) -> tuple[str, ...]:
        """Return the names of *site_id*'s cookies that are currently expired.

        Env overrides are never reported as expired — they carry no
        expiry metadata. Sorted alphabetically.

        Args:
            site_id: Canonical site id.

        Returns:
            A tuple of cookie names, sorted. Empty when no stored
            cookies are expired.
        """
        with self._lock:
            now = self._clock()
            stored = self._site_cookies.get(site_id, {})
            return tuple(
                sorted(
                    name
                    for name, entry in stored.items()
                    if entry.is_expired(now)
                )
            )

    def needs_refresh(
        self,
        site_id: str,
        *,
        required: Sequence[str] = (),
    ) -> bool:
        """Return ``True`` if the site's cookies need to be refreshed.

        Two operating modes:

        * **Without ``required``** — returns ``True`` iff the site has no
          cookies at all (never authenticated), or any stored cookie is
          expired or expiring within
          :attr:`_refresh_threshold`. This is the "something looks wrong,
          re-run the auth flow" signal.
        * **With ``required``** — returns ``True`` iff any of the named
          cookies is missing, expired, or expiring within the threshold.
          Used by parsers that know exactly which cookie authenticates
          them (e.g. ``cf_clearance`` for a Cloudflare-protected site).
          Does not trigger the "no cookies at all" condition when
          specific names are given, because a site may legitimately need
          only one of several cookies.

        Args:
            site_id: Canonical site id.
            required: Optional sequence of cookie names that must be
                present and unexpired for the site to be considered
                healthy. Empty (the default) triggers the "any expired
                cookie or no cookies at all" check.

        Returns:
            ``True`` when a refresh is advisable, ``False`` otherwise.
        """
        with self._lock:
            if required:
                available = self.get_cookie_entries(site_id)
                return any(
                    name not in available for name in required
                )
            stored = self._site_cookies.get(site_id, {})
            env_overrides = self._env_cookies.get(site_id, {})
            # A site whose only cookies come from env overrides counts
            # as "has cookies" — the operator explicitly configured them.
            if not stored and not env_overrides:
                return True
            now = self._clock()
            for entry in stored.values():
                if entry.is_expired(
                    now, threshold_seconds=self._refresh_threshold
                ):
                    return True
            return False

    def stats(self) -> CookieStats:
        """Return an immutable aggregate snapshot of the manager's state."""
        with self._lock:
            now = self._clock()
            total = 0
            expired = 0
            for entries in self._site_cookies.values():
                for entry in entries.values():
                    total += 1
                    if entry.is_expired(now):
                        expired += 1
            site_count = len(
                set(self._site_cookies) | set(self._env_cookies)
            )
            return CookieStats(
                site_count=site_count,
                total_cookies=total,
                expired_cookies=expired,
                env_override_sites=len(self._sites_with_env_overrides),
            )

    # ------------------------------------------------------------------ #
    # Write API                                                          #
    # ------------------------------------------------------------------ #

    def set_cookies(
        self,
        site_id: str,
        cookies: Mapping[str, str | CookieEntry],
    ) -> None:
        """Replace every cookie for *site_id* with *cookies*.

        Each entry may be a plain :class:`str` (treated as a session
        cookie, ``expires_at=None``) or a :class:`CookieEntry` (with
        expiry metadata). Existing cookies for the site are discarded
        entirely — this is the "we just logged in fresh, here is the new
        session" path, and the difference from :meth:`update_cookies` is
        that any previously stored cookie not present in *cookies* is
        removed.

        Env overrides are **not** cleared by this method — an operator
        who set an env override expects it to survive a programmatic
        update. To clear env overrides, restart the process without the
        env var set.

        Args:
            site_id: Canonical site id.
            cookies: The new cookie jar for the site.
        """
        with self._lock:
            entries: dict[str, CookieEntry] = {}
            for name, value in cookies.items():
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"cookie names must be non-empty strings, got "
                        f"{name!r}"
                    )
                entries[name] = _coerce_entry(value, site_id, name)
            self._site_cookies[site_id] = entries

    def update_cookies(
        self,
        site_id: str,
        cookies: Mapping[str, str | CookieEntry],
    ) -> None:
        """Merge *cookies* into the site's existing jar.

        Each entry may be a plain :class:`str` (treated as a session
        cookie) or a :class:`CookieEntry` (with expiry metadata). Unlike
        :meth:`set_cookies`, cookies not present in *cookies* are
        preserved. This is the method :class:`HttpSession` calls after
        every response to record the site's ``Set-Cookie`` headers.

        Args:
            site_id: Canonical site id.
            cookies: The cookie values to merge in. A value of ``str`` is
                treated as a session cookie; a ``CookieEntry`` carries
                its own expiry.

        Raises:
            ValueError: If a cookie name is not a non-empty string, or
                if a value (after coercion to :class:`CookieEntry`)
                contains a forbidden character (CR, LF, NUL, ``;``).
        """
        with self._lock:
            existing = self._site_cookies.setdefault(site_id, {})
            for name, value in cookies.items():
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"cookie names must be non-empty strings, got "
                        f"{name!r}"
                    )
                existing[name] = _coerce_entry(value, site_id, name)

    def clear_cookies(self, site_id: str) -> None:
        """Remove all stored cookies for *site_id*, keeping the site entry.

        Functionally equivalent to :meth:`delete` for :meth:`get_cookies`
        (both leave the site with no cookies), but preserves the site's
        presence in :meth:`list_sites` — useful for a UI that shows "no
        cookies yet" versus "never logged in". Env overrides are
        unaffected.

        Args:
            site_id: Canonical site id. No-op if the site is unknown.
        """
        with self._lock:
            if site_id in self._site_cookies:
                self._site_cookies[site_id] = {}

    def delete(self, site_id: str) -> bool:
        """Remove *site_id* from the manager entirely.

        Removes the site's stored cookies and drops the site key. Env
        overrides are **not** removed — they are immutable after
        construction.

        Args:
            site_id: Canonical site id.

        Returns:
            ``True`` iff a site entry existed and was removed,
            ``False`` otherwise.
        """
        with self._lock:
            return self._site_cookies.pop(site_id, None) is not None

    # ------------------------------------------------------------------ #
    # Serialization                                                      #
    # ------------------------------------------------------------------ #

    def _serialize_locked(self) -> bytes:
        """Serialise the in-memory state to JSON bytes for encryption.

        Caller must hold the lock. The output is the plaintext that
        :meth:`save` encrypts before writing — never the on-disk format.

        Env-var overrides are **not** serialised: they are re-derived
        from the process environment on every restart, and persisting
        them would duplicate the source of truth across the file and the
        environment.

        Returns:
            The JSON document as UTF-8 bytes.
        """
        sites_doc: dict[str, dict[str, dict[str, Any]]] = {}
        for site_id, entries in self._site_cookies.items():
            entry_doc: dict[str, dict[str, Any]] = {}
            for name, entry in entries.items():
                entry_doc[name] = {
                    "value": entry.value,
                    "expires_at": entry.expires_at,
                }
            sites_doc[site_id] = entry_doc

        document = {
            "version": FILE_VERSION,
            "saved_at": datetime.now(UTC).isoformat(),
            "sites": sites_doc,
        }
        return json.dumps(
            document, indent=2, ensure_ascii=False
        ).encode("utf-8")

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation.

        Never includes cookie values, the file path, or the encryption
        key — only counts and the file path's basename.
        """
        with self._lock:
            site_count = len(self._site_cookies)
            total = sum(len(e) for e in self._site_cookies.values())
        return (
            f"CookieManager(path=<{self._path.name}>, "
            f"sites={site_count}, cookies={total}, "
            f"env_overrides={len(self._sites_with_env_overrides)})"
        )


# --------------------------------------------------------------------------- #
# Internal helpers — entry coercion                                          #
# --------------------------------------------------------------------------- #


def _coerce_entry(
    value: str | CookieEntry, site_id: str, cookie_name: str
) -> CookieEntry:
    """Coerce a caller-provided cookie value to a :class:`CookieEntry`.

    Args:
        value: Either a raw string (treated as session cookie) or an
            already-constructed :class:`CookieEntry`.
        site_id: For error messages.
        cookie_name: For error messages.

    Returns:
        The coerced entry.

    Raises:
        TypeError: If *value* is neither a string nor a
            :class:`CookieEntry`.
        ValueError: If the resulting :class:`CookieEntry` fails value
            validation (CR/LF/NUL/semicolon).
    """
    if isinstance(value, CookieEntry):
        # A caller-constructed entry has already passed validation in
        # its own __post_init__; re-validating is unnecessary.
        return value
    if isinstance(value, str):
        # Construction runs __post_init__, which rejects forbidden
        # characters. The error message references the site/cookie
        # names so the operator can identify the offending Set-Cookie.
        try:
            return CookieEntry(value=value, expires_at=None)
        except ValueError as exc:
            raise ValueError(
                f"cookie {cookie_name!r} for site {site_id!r} has an "
                f"invalid value: {exc}"
            ) from exc
    raise TypeError(
        f"cookie {cookie_name!r} for site {site_id!r} must be a str "
        f"or CookieEntry, got {type(value).__name__}"
    )


def _parse_entry(
    site_id: str,
    cookie_name: str,
    raw: Any,
    path: Path,
) -> CookieEntry:
    """Parse one cookie entry from a loaded JSON document.

    Raises:
        CookieEncryptionError: If the entry has the wrong shape, or if
            the value contains a forbidden character (a defensive check
            against a corrupted or malicious file).
    """
    if not isinstance(raw, dict):
        raise CookieEncryptionError(
            f"cookie file {path}: site {site_id!r} cookie "
            f"{cookie_name!r} must be an object, got "
            f"{type(raw).__name__}"
        )
    value = raw.get("value")
    if not isinstance(value, str):
        raise CookieEncryptionError(
            f"cookie file {path}: site {site_id!r} cookie "
            f"{cookie_name!r} has a non-string value"
        )
    expires_at = raw.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, (int, float)):
        raise CookieEncryptionError(
            f"cookie file {path}: site {site_id!r} cookie "
            f"{cookie_name!r} has an invalid expires_at "
            f"({type(expires_at).__name__})"
        )
    try:
        return CookieEntry(
            value=value,
            expires_at=float(expires_at) if expires_at is not None else None,
        )
    except ValueError as exc:
        # A corrupted file, or a file written by an older version that
        # did not enforce value validation. Treat as a load failure so
        # the operator is forced to investigate, rather than silently
        # accepting a value that will break the Cookie header.
        raise CookieEncryptionError(
            f"cookie file {path}: site {site_id!r} cookie "
            f"{cookie_name!r} has an invalid value: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Internal helpers — encryption file I/O                                    #
# --------------------------------------------------------------------------- #


def _chmod_owner_only(path: Path) -> None:
    """Set *path*'s permissions to owner-read-write-only (``0600``).

    On POSIX, a failure raises :class:`CookieEncryptionError` — a
    cookie file readable by other users on the host is a security
    defect, not a warning, and the caller must know. On Windows, the
    underlying :func:`os.chmod` sets the read-only bit which is
    cosmetic (the OS-level ACL model provides the real protection);
    a failure there is logged at ``DEBUG`` and swallowed.

    Args:
        path: The file whose permissions to set.

    Raises:
        CookieEncryptionError: On POSIX, if :func:`os.chmod` raises
            :class:`OSError`.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        if os.name == "posix":
            raise CookieEncryptionError(
                f"could not set 0600 permissions on {path}: "
                f"{type(exc).__name__}: {exc}. On POSIX this leaves "
                f"the cookie file readable by other users on the host. "
                f"Check the filesystem's permission model (a read-only "
                f"mount, an unusual umask, or a FUSE mount that does "
                f"not support chmod)."
            ) from exc
        # Windows: chmod sets the read-only bit; the encryption layer
        # is the real protection. Not worth a warning.
        _log.debug(
            "Could not set 0600 permissions on {}: {}: {}",
            path,
            type(exc).__name__,
            exc,
        )


def _safe_unlink(path: Path) -> None:
    """Best-effort removal of *path*, never raising.

    Used in the exception-handling paths of :meth:`CookieManager.save` so
    that a failure to clean up a temp file (permissions, FS read-only)
    does not replace the exception the caller actually needs to see.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Internal helpers — environment variable parsing                           #
# --------------------------------------------------------------------------- #


def _parse_env_cookies(
    env: Mapping[str, str],
    known_sites: Sequence[str],
) -> dict[str, dict[str, str]]:
    """Parse ``NEXUSDL_<SITE>_<COOKIE>`` env vars into per-site cookies.

    The format is::

        NEXUSDL_<SITE_ID>_<COOKIE_NAME>=<value>

    where ``<SITE_ID>`` matches one of *known_sites* (case-insensitive)
    and ``<COOKIE_NAME>`` is the cookie name **in its original case**
    (mixed case is common: ``cf_clearance`` vs ``PHPSESSID``).

    The ``NEXUSDL_`` prefix is matched **case-insensitively**, matching
    ``Settings.case_sensitive=False``. A ``.env`` written entirely in
    lowercase (a common 12-factor convention) therefore still works.

    Because site ids may contain underscores, the parser disambiguates
    by trying the longest matching site id first. For example, with
    known sites ``("my", "my_site")``, the var
    ``NEXUSDL_MY_SITE_cf_clearance=abc`` matches site ``my_site``,
    cookie ``cf_clearance`` — not site ``my``, cookie
    ``site_cf_clearance``.

    Vars that do not match any known site are ignored with a
    ``DEBUG``-level log line, so a stray ``NEXUSDL_*`` variable that the
    operator expected to have an effect is discoverable without spamming
    logs in the common case.

    Args:
        env: The process environment mapping (typically ``os.environ``).
        known_sites: The list of site ids that env vars may reference.
            Empty disables parsing — no env vars are read.

    Returns:
        A nested mapping ``site_id → {cookie_name → value}``. Empty when
        no env vars match.
    """
    if not known_sites:
        return {}

    # Sort known sites longest-first so the longest match wins on
    # ambiguous prefixes.
    sorted_sites = sorted(known_sites, key=len, reverse=True)
    site_lookup = {site.lower(): site for site in sorted_sites}

    result: dict[str, dict[str, str]] = {}
    prefix_upper = ENV_PREFIX.upper()
    prefix_len = len(ENV_PREFIX)

    for var_name, var_value in env.items():
        # Case-insensitive prefix match (Settings is case-insensitive).
        if var_name[:prefix_len].upper() != prefix_upper:
            continue
        # Extract the remainder preserving the cookie name's original
        # case — ``cf_clearance`` must remain lowercase.
        remainder = var_name[prefix_len:]
        if not remainder:
            continue

        remainder_lower = remainder.lower()
        matched_site: str | None = None
        matched_cookie: str | None = None
        for candidate in sorted_sites:
            candidate_lower = candidate.lower()
            if not remainder_lower.startswith(candidate_lower + "_"):
                continue
            cookie = remainder[len(candidate) + 1 :]
            if not cookie:
                continue
            matched_site = site_lookup[candidate_lower]
            matched_cookie = cookie
            break

        if matched_site is None or matched_cookie is None:
            _log.debug(
                "Ignoring env cookie {!r}: does not match any known "
                "site",
                var_name,
            )
            continue

        # A cookie name that still contains an underscore may indicate
        # a `known_sites` misconfiguration (a site id with an
        # underscore that was not registered). Log at DEBUG so the
        # operator can see the interpretation the parser chose.
        if "_" in matched_cookie:
            _log.debug(
                "Env cookie {!r} parsed as site={!r}, cookie={!r}; "
                "the cookie name contains '_', which may indicate a "
                "missing site in known_sites",
                var_name,
                matched_site,
                matched_cookie,
            )

        result.setdefault(matched_site, {})[matched_cookie] = var_value

    return result
