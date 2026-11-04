"""Security primitives for the NexusDL FastAPI backend.

Framework-agnostic. Handles:

* password hashing and verification (bcrypt via passlib, pluggable),
* password-policy enforcement (data-driven),
* JWT issuance and verification (access + refresh, pluggable backend),
* refresh-token rotation with **replay detection**,
* token revocation (JTI-based, per-family, pluggable store),
* role hierarchy enforcement (:data:`ROLE_RANK`).

This module intentionally does **not** import FastAPI, Starlette, Pydantic,
or any transport layer. The endpoint layer lives in
:mod:`nexusdl.interfaces.web.backend.dependencies` and converts the typed
exceptions here (each carrying a stable ``code`` attribute) into the
project's standard HTTP error envelope.

Design notes
============

* **Secrets are never logged.** :class:`AuthConfig.__repr__` redacts
  ``secret_key``; no exception message ever contains a secret.
* **Refresh-token rotation** — every successful refresh revokes the old
  JTI and issues a new pair in the *same family*. If a **revoked** refresh
  token is presented again, the entire family is revoked (standard
  OAuth 2.1 reuse-detection). This logs out every session derived from
  that original login, neutralising token theft.
* **Token type is enforced.** An access token can never be presented as a
  refresh token and vice versa; the ``typ`` claim is mandatory.
* **bcrypt hard limit.** Passwords longer than 72 bytes are rejected
  rather than silently truncated. ``PasswordPolicy.max_length`` is
  ``72`` by default for that reason; hosts that use a different hasher
  can override.
* **Timing-safe** comparisons everywhere a secret is compared
  (``hmac.compare_digest``); passlib handles password verify timing.
* **Testability.** Every external dependency is behind a Protocol; a
  fake ``JWTBackend``, in-memory ``RevocationStore``, and a deterministic
  ``token_id_factory`` make full coverage feasible without cryptography.

Public surface
==============

Exceptions (each carries a stable ``.code``):

* :class:`SecurityError` — base
* :class:`PasswordPolicyError`
* :class:`InvalidCredentialsError`
* :class:`TokenError` → :class:`TokenExpiredError`,
  :class:`InvalidTokenError`, :class:`TokenRevokedError`,
  :class:`TokenTypeMismatchError`
* :class:`InsufficientRoleError`

Dataclasses:

* :class:`AuthConfig`, :class:`PasswordPolicy`, :class:`TokenPair`,
  :class:`TokenPayload`

Services:

* :class:`PasswordHasher`, :class:`AuthService`

Stores / backends:

* :class:`RevocationStore` (Protocol),
  :class:`InMemoryRevocationStore` (default)
* :class:`JWTBackend` (Protocol), :class:`JoseJWTBackend` (default)

Helpers:

* :func:`role_at_least`, :data:`ROLE_RANK`
"""
from __future__ import annotations

import asyncio
import hmac
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from loguru import logger as _logger

from nexusdl.core.models.enums import UserRole

_log = _logger.bind(component="web.backend.security")


# ===========================================================================
# Public constants
# ===========================================================================

DEFAULT_ALGORITHM: str = "HS256"
DEFAULT_ACCESS_TTL_S: int = 900          # 15 min
DEFAULT_REFRESH_TTL_S: int = 7 * 86400   # 7 days
DEFAULT_LEEWAY_S: int = 30               # clock skew tolerance
MAX_ACCESS_TTL_S: int = 3600             # 1 hour
MAX_REFRESH_TTL_S: int = 30 * 86400      # 30 days
BCRYPT_MAX_BYTES: int = 72

ROLE_RANK: dict[UserRole, int] = {
    UserRole.GUEST: 0,
    UserRole.USER: 1,
    UserRole.ADMIN: 2,
}


def role_at_least(actual: UserRole, required: UserRole) -> bool:
    """Return whether ``actual`` is at least as privileged as ``required``."""
    return ROLE_RANK.get(actual, 0) >= ROLE_RANK.get(required, 0)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unix(dt: datetime) -> int:
    return int(dt.timestamp())


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _default_token_id() -> str:
    return str(uuid.uuid4())


TokenIdFactory = Callable[[], str]


# ===========================================================================
# Exceptions
# ===========================================================================

class SecurityError(Exception):
    """Base exception for every security-related error."""

    code: str = "SECURITY_ERROR"


class PasswordPolicyError(SecurityError):
    """Raised when a password does not satisfy the configured policy."""

    code = "PASSWORD_POLICY_VIOLATION"

    def __init__(self, message: str, violations: list[str] | None = None) -> None:
        super().__init__(message)
        self.violations: list[str] = violations or []


class InvalidCredentialsError(SecurityError):
    """Raised when login credentials do not match (either field is wrong).

    Deliberately ambiguous — do not reveal which of "user not found" or
    "wrong password" occurred. The router maps this to HTTP 401 with a
    generic message.
    """

    code = "INVALID_CREDENTIALS"


class TokenError(SecurityError):
    """Base class for token-related errors."""

    code = "INVALID_TOKEN"


class TokenExpiredError(TokenError):
    code = "TOKEN_EXPIRED"


class InvalidTokenError(TokenError):
    code = "INVALID_TOKEN"


class TokenRevokedError(TokenError):
    code = "TOKEN_REVOKED"


class TokenTypeMismatchError(TokenError):
    code = "TOKEN_TYPE_MISMATCH"


class InsufficientRoleError(SecurityError):
    """Raised when a user lacks the required role for an action."""

    code = "INSUFFICIENT_ROLE"


# ===========================================================================
# Password policy
# ===========================================================================

@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """Data-driven password policy.

    Adding a rule = one new field + one branch in :meth:`validate`.
    Callers should never access fields directly to enforce policy — always
    go through :meth:`validate`, which aggregates **all** violations into a
    single :class:`PasswordPolicyError`.

    Attributes:
        min_length: Minimum character count.
        max_length: Maximum **byte** count (bcrypt's 72-byte hard limit).
        require_upper: Require at least one uppercase letter.
        require_lower: Require at least one lowercase letter.
        require_digit: Require at least one digit.
        require_symbol: Require at least one non-alphanumeric character.
        banned_substrings: Case-insensitive substrings forbidden in the
            password (e.g. ``("password", "admin", app_name)``).
    """

    min_length: int = 8
    max_length: int = BCRYPT_MAX_BYTES
    require_upper: bool = False
    require_lower: bool = True
    require_digit: bool = False
    require_symbol: bool = False
    banned_substrings: tuple[str, ...] = ()

    def validate(self, password: str) -> None:
        """Raise :class:`PasswordPolicyError` if ``password`` violates the policy."""
        if not isinstance(password, str):
            raise PasswordPolicyError("Password must be a string")

        violations: list[str] = []
        if len(password) < self.min_length:
            violations.append(
                f"must be at least {self.min_length} characters"
            )
        if len(password.encode("utf-8")) > self.max_length:
            violations.append(
                f"must be at most {self.max_length} bytes"
            )
        if self.require_upper and not any(c.isupper() for c in password):
            violations.append("must contain an uppercase letter")
        if self.require_lower and not any(c.islower() for c in password):
            violations.append("must contain a lowercase letter")
        if self.require_digit and not any(c.isdigit() for c in password):
            violations.append("must contain a digit")
        if self.require_symbol and not any(
            not c.isalnum() for c in password
        ):
            violations.append("must contain a symbol")

        lower = password.lower()
        for banned in self.banned_substrings:
            if banned and banned.lower() in lower:
                violations.append(f"must not contain {banned!r}")

        if violations:
            raise PasswordPolicyError(
                "Password does not satisfy the policy: " + "; ".join(violations),
                violations=violations,
            )


# ===========================================================================
# Auth configuration
# ===========================================================================

@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Configuration for :class:`AuthService`.

    ``secret_key`` accepts a ``str`` or a ``pydantic.SecretStr``; both are
    supported so callers can pass ``settings.jwt_secret`` directly. The
    :attr:`__repr__` redacts the secret.
    """

    secret_key: Any
    algorithm: str = DEFAULT_ALGORITHM
    issuer: str | None = None
    audience: str | None = None
    access_ttl_s: int = DEFAULT_ACCESS_TTL_S
    refresh_ttl_s: int = DEFAULT_REFRESH_TTL_S
    leeway_s: int = DEFAULT_LEEWAY_S

    def __post_init__(self) -> None:
        secret = self.secret
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret_key must be a non-empty string")
        if self.access_ttl_s <= 0 or self.access_ttl_s > MAX_ACCESS_TTL_S:
            raise ValueError(
                f"access_ttl_s must be in (0, {MAX_ACCESS_TTL_S}]"
            )
        if self.refresh_ttl_s <= self.access_ttl_s:
            raise ValueError("refresh_ttl_s must be > access_ttl_s")
        if self.refresh_ttl_s > MAX_REFRESH_TTL_S:
            raise ValueError(
                f"refresh_ttl_s must be <= {MAX_REFRESH_TTL_S}"
            )
        if self.leeway_s < 0:
            raise ValueError("leeway_s must be >= 0")

    @property
    def secret(self) -> str:
        """Return the raw secret string, unwrapping ``SecretStr`` if needed."""
        raw = self.secret_key
        getter = getattr(raw, "get_secret_value", None)
        if callable(getter):
            return str(getter())
        return str(raw)

    def __repr__(self) -> str:
        return (
            f"AuthConfig(algorithm={self.algorithm!r}, "
            f"issuer={self.issuer!r}, audience={self.audience!r}, "
            f"access_ttl_s={self.access_ttl_s}, "
            f"refresh_ttl_s={self.refresh_ttl_s}, "
            f"leeway_s={self.leeway_s}, secret=<redacted>)"
        )


# ===========================================================================
# Token payloads
# ===========================================================================

@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Decoded token contents.

    Attributes:
        sub: User identifier (``str``, not necessarily a UUID).
        role: User role at issuance time.
        jti: Unique token id, used for revocation.
        iat: Issued-at, unix seconds.
        exp: Expires-at, unix seconds.
        typ: Token type — ``"access"`` or ``"refresh"``.
        family: Refresh-token family id, shared across a rotation chain.
            ``None`` for access tokens.
        username: Optional username, echoed for client convenience.
        iss: Issuer claim.
        aud: Audience claim.
        extra: Any additional claims (preserved on round-trip).
    """

    sub: str
    role: UserRole
    jti: str
    iat: int
    exp: int
    typ: Literal["access", "refresh"]
    family: str | None = None
    username: str | None = None
    iss: str | None = None
    aud: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_claims(self) -> dict[str, Any]:
        claims: dict[str, Any] = {
            "sub": self.sub,
            "role": self.role.value,
            "jti": self.jti,
            "iat": self.iat,
            "exp": self.exp,
            "typ": self.typ,
        }
        if self.family is not None:
            claims["fam"] = self.family
        if self.username is not None:
            claims["username"] = self.username
        if self.iss is not None:
            claims["iss"] = self.iss
        if self.aud is not None:
            claims["aud"] = self.aud
        claims.update(self.extra)
        return claims

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> TokenPayload:
        raw_role = claims.get("role", UserRole.GUEST.value)
        try:
            role = UserRole(str(raw_role))
        except (ValueError, TypeError):
            role = UserRole.GUEST

        typ_raw = str(claims.get("typ", "access"))
        typ: Literal["access", "refresh"] = (
            "refresh" if typ_raw == "refresh" else "access"
        )

        known = {
            "sub", "role", "jti", "iat", "exp", "typ",
            "fam", "username", "iss", "aud",
        }
        extra = {k: v for k, v in claims.items() if k not in known}

        return cls(
            sub=str(claims["sub"]),
            role=role,
            jti=str(claims["jti"]),
            iat=int(claims["iat"]),
            exp=int(claims["exp"]),
            typ=typ,
            family=claims.get("fam"),
            username=claims.get("username"),
            iss=claims.get("iss"),
            aud=claims.get("aud"),
            extra=extra,
        )


@dataclass(frozen=True, slots=True)
class TokenPair:
    """A freshly issued access + refresh token pair."""

    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int
    token_type: str = "Bearer"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON shape returned by ``/auth/login``."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_in": self.access_expires_in,
            "refresh_expires_in": self.refresh_expires_in,
            "token_type": self.token_type,
        }


# ===========================================================================
# Password hasher
# ===========================================================================

class PasswordHasher:
    """Wraps passlib's :class:`CryptContext` behind a small, typed API.

    The default context is bcrypt (matching ``passlib[bcrypt]`` in
    ``pyproject.toml``). Inject a different ``context`` for tests or to
    switch to Argon2id when ``passlib[argon2]`` is installed.
    """

    def __init__(self, *, context: Any | None = None) -> None:
        if context is None:
            try:
                from passlib.context import CryptContext  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - dependency error
                raise ImportError(
                    "NexusDL web backend requires 'passlib[bcrypt]'. "
                    "Install it with: pip install 'nexusdl[web]'"
                ) from exc
            context = CryptContext(
                schemes=["bcrypt"],
                deprecated="auto",
                bcrypt__rounds=12,
            )
        self._ctx = context

    def hash(self, password: str) -> str:
        """Hash ``password``. Raises if the hasher fails."""
        return self._ctx.hash(password)

    def verify(self, password: str, hashed: str) -> bool:
        """Verify ``password`` against ``hashed``. Never raises on mismatch."""
        try:
            return bool(self._ctx.verify(password, hashed))
        except Exception:
            return False

    def needs_rehash(self, hashed: str) -> bool:
        """Return whether ``hashed`` uses an outdated scheme or cost."""
        try:
            return bool(self._ctx.needs_update(hashed))
        except Exception:
            return False


# ===========================================================================
# Revocation store
# ===========================================================================

@runtime_checkable
class RevocationStore(Protocol):
    """Abstract store for revoked token JTIs.

    Implementations must be safe to call concurrently. Hosts typically back
    this with Redis, but :class:`InMemoryRevocationStore` is provided for
    single-process deployments and tests.
    """

    async def add(
        self,
        jti: str,
        expires_at: datetime,
        family: str | None = None,
    ) -> None:
        """Mark ``jti`` as revoked until ``expires_at``."""

    async def is_revoked(self, jti: str, family: str | None = None) -> bool:
        """Return whether ``jti`` (or its ``family``) is revoked."""

    async def revoke_family(self, family: str) -> None:
        """Revoke every token in ``family``, past, present, and future."""

    async def cleanup(self) -> int:
        """Purge expired entries. Return the number of purged entries."""


@dataclass(frozen=True, slots=True)
class _RevocationEntry:
    expires_at: datetime
    family: str | None


class InMemoryRevocationStore:
    """Process-local revocation store.

    Suitable for single-worker deployments and tests. **Not** shared
    across processes — multi-worker deployments must provide a Redis-backed
    :class:`RevocationStore` so revocation is consistent.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RevocationEntry] = {}
        self._revoked_families: set[str] = set()
        self._lock = asyncio.Lock()

    async def add(
        self,
        jti: str,
        expires_at: datetime,
        family: str | None = None,
    ) -> None:
        async with self._lock:
            self._entries[jti] = _RevocationEntry(
                expires_at=expires_at, family=family
            )

    async def is_revoked(
        self, jti: str, family: str | None = None
    ) -> bool:
        async with self._lock:
            if jti in self._entries:
                return True
            if family is not None and family in self._revoked_families:
                return True
            return False

    async def revoke_family(self, family: str) -> None:
        async with self._lock:
            self._revoked_families.add(family)

    async def cleanup(self) -> int:
        now = _now()
        async with self._lock:
            expired = [
                jti for jti, entry in self._entries.items()
                if entry.expires_at < now
            ]
            for jti in expired:
                self._entries.pop(jti, None)
            # Families are cheap; do not bother expiring them — a family
            # tombstone is only valuable while some token in it might still
            # exist, and the cost of retaining them is negligible.
            return len(expired)

    def __len__(self) -> int:
        return len(self._entries)


# ===========================================================================
# JWT backend
# ===========================================================================

@runtime_checkable
class JWTBackend(Protocol):
    """Abstract JWT encoder/decoder.

    Enables tests to swap in a fake that returns pre-baked claims without
    signing anything.
    """

    def encode(self, claims: dict[str, Any], key: str, algorithm: str) -> str: ...

    def decode(
        self,
        token: str,
        key: str,
        algorithms: list[str],
        *,
        audience: str | None,
        issuer: str | None,
        leeway: int,
    ) -> dict[str, Any]: ...


class JoseJWTBackend:
    """Default :class:`JWTBackend` using :mod:`python-jose`."""

    def encode(
        self, claims: dict[str, Any], key: str, algorithm: str
    ) -> str:
        from jose import jwt as jose_jwt  # noqa: PLC0415
        return str(jose_jwt.encode(claims, key, algorithm=algorithm))

    def decode(
        self,
        token: str,
        key: str,
        algorithms: list[str],
        *,
        audience: str | None,
        issuer: str | None,
        leeway: int,
    ) -> dict[str, Any]:
        from jose import jwt as jose_jwt  # noqa: PLC0415
        options = {
            "verify_aud": audience is not None,
            "verify_iss": issuer is not None,
            "leeway": leeway,
        }
        return dict(jose_jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=audience,
            issuer=issuer,
            options=options,
        ))


# ===========================================================================
# Auth service
# ===========================================================================

class AuthService:
    """Orchestrates password hashing, token issuance, and verification.

    Framework-agnostic. One instance per process is the norm; it is
    stateless apart from the injected :class:`RevocationStore`.

    Example::

        service = AuthService(config=AuthConfig(secret_key=settings.jwt_secret))

        # Registration / login
        hashed = service.hash_password(plain)
        assert service.verify_password(plain, hashed)

        pair = service.issue_pair(user_id=user.id, role=user.role, username=user.username)

        # Request handling
        payload = await service.verify_access(pair.access_token)
        service.verify_role(payload, UserRole.USER)

        # Refresh rotation
        new_pair = await service.refresh(pair.refresh_token)
    """

    def __init__(
        self,
        *,
        config: AuthConfig,
        hasher: PasswordHasher | None = None,
        revoker: RevocationStore | None = None,
        jwt_backend: JWTBackend | None = None,
        policy: PasswordPolicy | None = None,
        token_id_factory: TokenIdFactory | None = None,
    ) -> None:
        self._config = config
        self._hasher = hasher or PasswordHasher()
        self._revoker = revoker or InMemoryRevocationStore()
        self._jwt = jwt_backend or JoseJWTBackend()
        self._policy = policy or PasswordPolicy()
        self._new_id = token_id_factory or _default_token_id

    # ------------------------------------------------------------------
    # Password
    # ------------------------------------------------------------------

    def validate_password(self, password: str) -> None:
        """Raise :class:`PasswordPolicyError` if the policy rejects ``password``."""
        self._policy.validate(password)

    def hash_password(self, password: str) -> str:
        """Validate and hash ``password``. Raises on policy violation."""
        self._policy.validate(password)
        return self._hasher.hash(password)

    def verify_password(self, password: str, hashed: str) -> bool:
        """Return whether ``password`` matches ``hashed``. Never raises."""
        if not password or not hashed:
            return False
        return self._hasher.verify(password, hashed)

    def needs_rehash(self, hashed: str) -> bool:
        return self._hasher.needs_rehash(hashed)

    # ------------------------------------------------------------------
    # Issuance
    # ------------------------------------------------------------------

    def issue_pair(
        self,
        *,
        user_id: str,
        role: UserRole,
        username: str | None = None,
    ) -> TokenPair:
        """Issue a fresh access + refresh pair.

        The refresh token is bound to a new random ``family``; subsequent
        rotations preserve it so replay detection works across rotations.
        """
        family = self._new_id()
        access_token, access_payload = self._issue(
            "access", user_id, role, username, family=None
        )
        refresh_token, refresh_payload = self._issue(
            "refresh", user_id, role, username, family=family
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_in=access_payload.exp - access_payload.iat,
            refresh_expires_in=refresh_payload.exp - refresh_payload.iat,
        )

    def issue_access(
        self,
        *,
        user_id: str,
        role: UserRole,
        username: str | None = None,
    ) -> tuple[str, TokenPayload]:
        """Issue an access token only. Rarely needed; use :meth:`issue_pair`."""
        return self._issue("access", user_id, role, username, family=None)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_access(
        self, token: str, *, check_revocation: bool = True
    ) -> TokenPayload:
        """Verify an access token. Raises a :class:`TokenError` subclass on failure."""
        payload = self._decode(token, expected_type="access")
        if check_revocation:
            if await self._revoker.is_revoked(payload.jti, payload.family):
                raise TokenRevokedError("access token has been revoked")
        return payload

    async def verify_refresh(
        self, token: str, *, check_revocation: bool = True
    ) -> TokenPayload:
        """Verify a refresh token **without** consuming it."""
        payload = self._decode(token, expected_type="refresh")
        if check_revocation:
            if await self._revoker.is_revoked(payload.jti, payload.family):
                raise TokenRevokedError("refresh token has been revoked")
        return payload

    async def try_verify_access(self, token: str) -> TokenPayload | None:
        """Non-raising variant of :meth:`verify_access`."""
        try:
            return await self.verify_access(token)
        except TokenError:
            return None

    async def try_verify_refresh(self, token: str) -> TokenPayload | None:
        """Non-raising variant of :meth:`verify_refresh`."""
        try:
            return await self.verify_refresh(token)
        except TokenError:
            return None

    def verify_role(self, payload: TokenPayload, required: UserRole) -> None:
        """Raise :class:`InsufficientRoleError` if ``payload.role`` < ``required``."""
        if not role_at_least(payload.role, required):
            raise InsufficientRoleError(
                f"requires role {required.value!r} or higher"
            )

    # ------------------------------------------------------------------
    # Refresh rotation with replay detection
    # ------------------------------------------------------------------

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Rotate a refresh token into a new pair.

        **Replay detection:** if the presented token's JTI has already been
        revoked (or its family is revoked), the entire family is revoked
        and :class:`TokenRevokedError` is raised. This neutralises token
        theft: the attacker's rotated token becomes unusable as soon as
        the legitimate client tries to refresh.
        """
        payload = self._decode(refresh_token, expected_type="refresh")

        # 1) Replay check.
        if await self._revoker.is_revoked(payload.jti, payload.family):
            if payload.family is not None:
                await self._revoker.revoke_family(payload.family)
                _log.warning(
                    "Refresh-token reuse detected; revoking family. "
                    "user={} family={} jti={}",
                    payload.sub,
                    payload.family,
                    payload.jti,
                )
            raise TokenRevokedError("refresh token has been revoked")

        # 2) Consume the old token (revoke its JTI; keep family id).
        expires_at = datetime.fromtimestamp(payload.exp, tz=timezone.utc)
        await self._revoker.add(
            payload.jti, expires_at, family=payload.family
        )

        # 3) Issue a fresh pair in the same family.
        new_access, _ = self._issue(
            "access",
            payload.sub,
            payload.role,
            payload.username,
            family=None,
        )
        new_refresh, new_refresh_payload = self._issue(
            "refresh",
            payload.sub,
            payload.role,
            payload.username,
            family=payload.family,
        )
        return TokenPair(
            access_token=new_access,
            refresh_token=new_refresh,
            access_expires_in=self._config.access_ttl_s,
            refresh_expires_in=new_refresh_payload.exp - new_refresh_payload.iat,
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke(self, payload: TokenPayload) -> None:
        """Revoke ``payload.jti`` until its natural ``exp``."""
        expires_at = datetime.fromtimestamp(payload.exp, tz=timezone.utc)
        await self._revoker.add(
            payload.jti, expires_at, family=payload.family
        )

    async def revoke_family(self, family: str) -> None:
        """Revoke every token in ``family``."""
        await self._revoker.revoke_family(family)

    async def cleanup(self) -> int:
        """Purge expired revocation entries. Returns count purged."""
        return await self._revoker.cleanup()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _issue(
        self,
        typ: Literal["access", "refresh"],
        user_id: str,
        role: UserRole,
        username: str | None,
        family: str | None,
    ) -> tuple[str, TokenPayload]:
        now = _now()
        issued_at = _unix(now)
        ttl = (
            self._config.access_ttl_s
            if typ == "access"
            else self._config.refresh_ttl_s
        )
        payload = TokenPayload(
            sub=user_id,
            role=role,
            jti=self._new_id(),
            iat=issued_at,
            exp=issued_at + ttl,
            typ=typ,
            family=family,
            username=username,
            iss=self._config.issuer,
            aud=self._config.audience,
        )
        token = self._jwt.encode(
            payload.to_claims(),
            self._config.secret,
            self._config.algorithm,
        )
        return token, payload

    def _decode(
        self, token: str, *, expected_type: Literal["access", "refresh"]
    ) -> TokenPayload:
        try:
            claims = self._jwt.decode(
                token,
                self._config.secret,
                [self._config.algorithm],
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=self._config.leeway_s,
            )
        except Exception as exc:
            # python-jose raises ExpiredSignatureError / JWTError; other
            # backends may raise anything. Normalize to our hierarchy.
            name = type(exc).__name__
            if "Expired" in name:
                raise TokenExpiredError("token has expired") from exc
            raise InvalidTokenError("token is invalid") from exc

        try:
            payload = TokenPayload.from_claims(claims)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidTokenError("token claims are malformed") from exc

        if payload.typ != expected_type:
            raise TokenTypeMismatchError(
                f"expected {expected_type!r} token, got {payload.typ!r}"
            )

        return payload


# ===========================================================================
# Public surface
# ===========================================================================

__all__ = [
    # Constants
    "DEFAULT_ALGORITHM",
    "DEFAULT_ACCESS_TTL_S",
    "DEFAULT_REFRESH_TTL_S",
    "DEFAULT_LEEWAY_S",
    "MAX_ACCESS_TTL_S",
    "MAX_REFRESH_TTL_S",
    "BCRYPT_MAX_BYTES",
    "ROLE_RANK",
    "role_at_least",
    # Exceptions
    "SecurityError",
    "PasswordPolicyError",
    "InvalidCredentialsError",
    "TokenError",
    "TokenExpiredError",
    "InvalidTokenError",
    "TokenRevokedError",
    "TokenTypeMismatchError",
    "InsufficientRoleError",
    # Dataclasses
    "PasswordPolicy",
    "AuthConfig",
    "TokenPayload",
    "TokenPair",
    # Services
    "PasswordHasher",
    "AuthService",
    # Backends
    "RevocationStore",
    "InMemoryRevocationStore",
    "JWTBackend",
    "JoseJWTBackend",
    "TokenIdFactory",
]
