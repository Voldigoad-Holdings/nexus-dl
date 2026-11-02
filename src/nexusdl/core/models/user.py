"""User account model for authentication and authorization.

This module defines the :class:`User` domain object. It is a pure, frozen
data structure — it does not hash passwords, does not validate email
format, does not talk to a database, and does not know about JWT, cookies,
or sessions. Each of those concerns belongs to a different layer:

* **Password hashing** — :mod:`nexusdl.interfaces.web.backend.security`
  (bcrypt / passlib).
* **Email format validation** — the auth endpoint, at registration time.
* **Persistence** — the ``users`` table in
  ``core/library/migrations/001_initial.sql``, mapped by whatever repository
  reads and writes it. This module provides the object shape; the mapping
  lives in the persistence layer.
* **Session state** — stateless JWT, no server-side session table.

The model exists so that every layer above auth can pass a single,
well-typed object around — ``user.role``, ``user.is_active``, ``user.email``
— without depending on the DB schema or on the hash algorithm.

Security considerations
-----------------------
:attr:`User.password_hash` is a first-class field because the ``users``
table stores it and a ``SELECT *`` from that table must produce a
:class:`User` without dropping information. However, leaking the hash into
logs, error messages, and API responses is a real risk. Two mitigations
are provided, and callers are expected to use them:

* :meth:`__repr__` **redacts** the hash — a ``User`` printed in a log line
  or a traceback shows ``password_hash='***'``.
* :meth:`to_public_dict` returns a serialisable mapping **without** the
  hash. This is the function to use whenever a ``User`` is rendered into a
  JSON API response, a UI view, or a debug dump intended to be read by a
  human.

Raw access to :attr:`password_hash` should be limited to the authentication
code path (the code that verifies a login attempt). Anywhere else,
``password_hash`` is a smell.

Model invariants
----------------
:meth:`__post_init__` enforces the invariants that the database schema
already enforces (matching ``CHECK`` constraints and ``NOT NULL`` columns)
plus two semantic invariants (``created_at <= updated_at`` and tz-aware
timestamps) that catch integration bugs at construction time rather than
at serialisation time. Every check is cheap; every check is worth the
trouble because a ``User`` with a malformed field is far more expensive
to debug later in the pipeline.

Dependency graph (no cycles)::

    core.models.enums  ──► core.models.user

No SQLAlchemy, no loguru, no I/O, no dependency on any other
:mod:`nexusdl.core` module. This file could be lifted into a standalone
library without modification.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from nexusdl.core.models.enums import UserRole


__all__ = ["User"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Redaction marker substituted for :attr:`User.password_hash` by
#: :meth:`User.__repr__`. Deliberately distinct from every plausible hash
#: prefix (``$2b$``, ``$argon2``, …) so a redacted repr cannot be mistaken
#: for a real one.
_REDACTED_HASH: Final[str] = "***"

#: Length of a canonical hyphenated UUID string. Matches the
#: ``CHECK (length(id) = 36)`` constraint on the ``users`` table; using the
#: same numeric literal here makes the correspondence obvious to a reader
#: comparing model and schema.
_UUID_STRING_LENGTH: Final[int] = 36


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    Factored out of :meth:`User.create` so the default clock is a single
    named function (visible in tracebacks, greppable) rather than an
    inline lambda.
    """
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# User                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, eq=False)
class User:
    """A NexusDL user account.

    Immutable. To produce a modified copy — e.g. after a successful login,
    or when an admin deactivates an account — use
    :func:`dataclasses.replace`::

        updated = replace(user, last_login_at=datetime.now(UTC))

    The dataclass is declared with ``eq=False`` and manual ``__eq__`` /
    ``__hash__`` implementations so that equality and hashing depend
    **only on** :attr:`id`. Two :class:`User` values with the same id but
    different mutable fields (``last_login_at``, ``is_active``, …) compare
    equal and hash identically. This matches the semantics of a database
    primary key and keeps set/dict membership stable across field updates.

    Attributes:
        id: The user's stable identifier, a canonical hyphenated UUID4
            string (36 characters). Generated by :meth:`create`; never
            changes for a given user.
        email: The user's email address. Unique across users (enforced by
            the schema). Format validation is the auth layer's
            responsibility; this field stores whatever the caller provides.
        password_hash: The full, self-describing output of the password
            hash function (e.g. a bcrypt string that includes the algorithm
            prefix and cost). **Treat as a secret** — use
            :meth:`to_public_dict` when serialising, never ``vars()`` or
            ``asdict()``, both of which expose this field.
        role: The user's role, from :class:`~nexusdl.core.models.enums.UserRole`.
            :attr:`UserRole.GUEST` is reserved for anonymous requests and
            is never expected in a persisted row; :attr:`UserRole.USER` is
            the default for newly-registered accounts.
        is_active: Whether the account can authenticate. Deactivating a
            user (``is_active=False``) preserves the row and its history;
            deleting it is a separate operation.
        created_at: Timezone-aware UTC timestamp of account creation.
        updated_at: Timezone-aware UTC timestamp of the most recent change
            to any mutable field. Must be ``>= created_at``.
        last_login_at: Timezone-aware UTC timestamp of the most recent
            successful login, or ``None`` for an account that has never
            logged in.
    """

    id: str
    email: str
    password_hash: str
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # Validation                                                         #
    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        """Validate field types and cross-field invariants.

        Two categories of check:

        * **Type checks** — reject a string where an enum is expected, an
          ``int`` where a ``bool`` is expected, a naive ``datetime`` where
          a tz-aware one is required. These catch integration bugs at the
          boundary between the persistence layer and the domain.
        * **Value checks** — reject empty required strings, an id of the
          wrong length, and ``created_at > updated_at``. These mirror the
          schema's ``CHECK`` and ``NOT NULL`` constraints so a malformed
          :class:`User` can never reach a repository that would otherwise
          raise an opaque ``IntegrityError``.

        Raises:
            TypeError: If a field has the wrong Python type (e.g.
                ``is_active=1``, ``role="USER"``, ``created_at`` naive).
            ValueError: If a field is well-typed but semantically invalid
                (empty string, id of the wrong length, inverted
                timestamps).
        """
        if not isinstance(self.id, str):
            raise TypeError(
                f"User.id must be a str, got {type(self.id).__name__}"
            )
        if len(self.id) != _UUID_STRING_LENGTH:
            raise ValueError(
                f"User.id must be a {_UUID_STRING_LENGTH}-character UUID "
                f"string, got {len(self.id)} characters"
            )
        if not isinstance(self.email, str):
            raise TypeError(
                f"User.email must be a str, got {type(self.email).__name__}"
            )
        if not self.email:
            raise ValueError("User.email must be non-empty")
        if not isinstance(self.password_hash, str):
            raise TypeError(
                f"User.password_hash must be a str, got "
                f"{type(self.password_hash).__name__}"
            )
        if not self.password_hash:
            raise ValueError("User.password_hash must be non-empty")
        if not isinstance(self.role, UserRole):
            raise TypeError(
                f"User.role must be a UserRole, got "
                f"{type(self.role).__name__}"
            )
        if not isinstance(self.is_active, bool):
            # Explicitly reject ``1`` / ``0`` — ``bool`` is a subclass of
            # ``int``, but the reverse is not true, and the persistence
            # layer sometimes reads ``is_active`` as an int. Callers must
            # coerce with ``bool()`` before constructing a User.
            raise TypeError(
                f"User.is_active must be a bool, got "
                f"{type(self.is_active).__name__}"
            )
        for field_name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if not isinstance(value, datetime):
                raise TypeError(
                    f"User.{field_name} must be a datetime, got "
                    f"{type(value).__name__}"
                )
            if value.tzinfo is None:
                raise ValueError(
                    f"User.{field_name} must be timezone-aware"
                )
        if self.created_at > self.updated_at:
            raise ValueError(
                "User.created_at must be <= User.updated_at "
                f"({self.created_at.isoformat()} > "
                f"{self.updated_at.isoformat()})"
            )
        if self.last_login_at is not None:
            if not isinstance(self.last_login_at, datetime):
                raise TypeError(
                    f"User.last_login_at must be a datetime or None, "
                    f"got {type(self.last_login_at).__name__}"
                )
            if self.last_login_at.tzinfo is None:
                raise ValueError(
                    "User.last_login_at must be timezone-aware"
                )

    # ------------------------------------------------------------------ #
    # Identity                                                           #
    # ------------------------------------------------------------------ #

    def __eq__(self, other: object) -> bool:
        """Compare by :attr:`id` only.

        Two users with the same id are the same user, regardless of their
        current ``last_login_at``, ``is_active``, or any other mutable
        field.
        """
        if not isinstance(other, User):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash by :attr:`id` only, matching :meth:`__eq__`."""
        return hash(self.id)

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a debug representation with the password hash redacted.

        The hash is never included — only the redaction marker — so that a
        :class:`User` accidentally logged or embedded in a traceback cannot
        leak credentials.
        """
        login = (
            self.last_login_at.isoformat()
            if self.last_login_at is not None
            else None
        )
        return (
            f"User(id={self.id!r}, email={self.email!r}, "
            f"password_hash={_REDACTED_HASH!r}, "
            f"role={self.role.value!r}, "
            f"is_active={self.is_active!r}, "
            f"created_at={self.created_at.isoformat()!r}, "
            f"updated_at={self.updated_at.isoformat()!r}, "
            f"last_login_at={login!r})"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping **without** the password hash.

        This is the correct method to call whenever a :class:`User` is
        rendered into an API response, a UI view, or any structured log.
        Use of :func:`dataclasses.asdict` or ``vars()`` in the same
        position is a bug: both include :attr:`password_hash`.

        Timestamps are rendered as ISO 8601 strings (with ``+00:00`` for
        UTC), matching the format expected by typical JSON consumers.

        Returns:
            A dict with ``id``, ``email``, ``role``, ``is_active``,
            ``created_at``, ``updated_at``, and ``last_login_at``.
            ``last_login_at`` is ``None`` for a user who has never
            logged in.
        """
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role.value,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_login_at": (
                self.last_login_at.isoformat()
                if self.last_login_at is not None
                else None
            ),
        }

    # ------------------------------------------------------------------ #
    # Convenience                                                        #
    # ------------------------------------------------------------------ #

    @property
    def is_admin(self) -> bool:
        """``True`` iff this user has the :attr:`UserRole.ADMIN` role.

        Provided as a property so authorisation checks read naturally
        (``if user.is_admin:``) without importing :class:`UserRole` at
        every call site.
        """
        return self.role == UserRole.ADMIN

    # ------------------------------------------------------------------ #
    # Factory                                                            #
    # ------------------------------------------------------------------ #

    @classmethod
    def create(
        cls,
        *,
        email: str,
        password_hash: str,
        role: UserRole = UserRole.USER,
        clock: Callable[[], datetime] | None = None,
    ) -> User:
        """Construct a new user with a fresh id and consistent timestamps.

        Generates a UUID4 string for :attr:`id`, sets :attr:`is_active` to
        ``True``, sets :attr:`last_login_at` to ``None``, and uses a
        single timestamp value for both :attr:`created_at` and
        :attr:`updated_at` so they are exactly equal — the schema's
        ``created_at <= updated_at`` invariant holds trivially.

        Args:
            email: The user's email address. Not validated here — the
                caller is expected to have validated it. The
                :meth:`__post_init__` check only rejects the empty string.
            password_hash: A pre-hashed password string, produced by the
                auth layer (e.g. ``bcrypt.hashpw(...).decode()``). Never
                pass a plaintext password — this constructor does not
                hash and will happily store anything it is given.
            role: The user's role. Defaults to :attr:`UserRole.USER`;
                pass :attr:`UserRole.ADMIN` only from an admin-provisioning
                path.
            clock: Callable returning a timezone-aware ``datetime``.
                Defaults to :func:`datetime.now` with UTC. Injectable for
                tests so timestamps are deterministic.

        Returns:
            A newly-constructed :class:`User`. The caller is responsible
            for persisting it.
        """
        now = (clock or _utcnow)()
        return cls(
            id=str(uuid.uuid4()),
            email=email,
            password_hash=password_hash,
            role=role,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login_at=None,
        )
