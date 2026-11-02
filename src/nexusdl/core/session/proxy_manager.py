"""Proxy pool, rotation, per-site binding, and health tracking.

Some sites are only reachable through a specific proxy (geo-restriction,
IP reputation, corporate egress), some deployments want to spread
requests across a rotating pool to avoid per-IP rate limits, and every
serious HTTP client needs a way to bench a proxy that has started
failing. This module provides that layer as a single
:class:`ProxyManager` that the
:class:`~nexusdl.core.session.http_session.HttpSession` consults before
every request.

Three concerns, one class
-------------------------
* **Selection** — pick a proxy for a given ``site_id`` (or with no
  affinity) from the configured pool, using a rotation strategy when a
  fresh pick is needed.
* **Stickiness** — the same ``site_id`` receives the same proxy for as
  long as that proxy stays healthy. A download session must not switch
  proxies mid-chapter: doing so breaks authentication state on sites
  that bind the session cookie to the client IP, and produces hard-to-
  diagnose "random login failures".
* **Health** — a proxy that fails ``failure_threshold`` times in a row
  is benched for ``cooldown_seconds``. While benched it is invisible to
  the selection layer. When the cooldown expires, it returns to the
  available pool automatically — the manager never requires an explicit
  "revive" call.

The three concerns compose: a fresh ``get_proxy("mangadex")`` is a
selection query, whose result is bound to the site and later updated by
``mark_failure`` / ``mark_success``. The health layer reacts to those
calls and, when a proxy enters cooldown, drops every sticky binding to
it so the next selection query for any site re-picks a healthy proxy.

No-proxy and all-benched cases
------------------------------
``get_proxy`` returns ``None`` in three cases:

1. **No proxies configured.** The session falls back to a direct
   connection. This is the default deployment.
2. **Every proxy is currently benched.** A ``WARNING`` is logged (with
   counts), then ``None`` is returned so the request still goes out.
   Falling back to direct is safer than refusing traffic — a proxy
   outage should degrade the client, not stop it.
3. **``site_id`` is bound to a proxy that has since been benched or
   removed.** The stale binding is dropped and a fresh proxy is picked
   (which may itself be ``None``).

A caller that wants strict proxy semantics — "fail if the site has no
proxy available" — checks ``stats().healthy_count`` before issuing the
request. The manager does not raise: proxy policy is a decision for the
caller, not for the transport layer.

Security: proxy URLs are secrets
--------------------------------
Proxy URLs frequently embed credentials (``http://user:pass@host:port``).
:class:`ProxyConfig` stores them as :class:`pydantic.SecretStr`, so
``str()`` / ``repr()`` of the config never leak the password, and
:attr:`ProxyConfig.redacted` gives a
``http://user:***@host:port`` form suitable for logs and diagnostics.
The plaintext URL is exposed only via :attr:`ProxyConfig.url` (which the
session unwraps with ``.get_secret_value()`` when building an
:class:`httpx.AsyncClient`) — nowhere else in this module does the
password surface.

The composition root sources proxy URLs from ``.env`` /
``Settings.proxy``; those sources use :class:`SecretStr` too. The
plaintext never touches a config file committed to version control.

Thread safety
-------------
Every public method takes an :class:`asyncio.Lock`. Critical sections
are O(n) over the proxy pool (typically 1–20 entries) — negligible.
The manager is not thread-safe across OS threads; it is designed for a
single asyncio event loop, matching the session.

Dependency graph (no cycles)::

    loguru, pydantic.SecretStr  ──►  core.session.proxy_manager
    asyncio, random, time, urllib.parse (stdlib)

No ``nexusdl.core`` import besides ``pydantic`` — this module is a leaf.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlparse

from loguru import logger
from pydantic import SecretStr


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "ProxyConfig",
    "ProxyHealth",
    "ProxyManager",
    "ProxyStats",
    "RotationStrategy",
    "load_proxy_configs",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Proxy URL schemes the manager accepts. ``socks5`` and ``socks5h`` require
#: ``httpx[socks]`` at the ``HttpSession`` layer; this module only validates
#: the string, not the installed extras.
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset(
    {"http", "https", "socks5", "socks5h"}
)

#: Consecutive failures that bench a proxy. Three is the conventional
#: circuit-breaker threshold — one failure is noise, two is suspicious,
#: three is a pattern.
DEFAULT_FAILURE_THRESHOLD: Final[int] = 3

#: Default duration, in seconds, a benched proxy is excluded from
#: selection. Five minutes is long enough for a transient outage to clear
#: and short enough that a permanently dead proxy is retried within one
#: chapter's typical runtime.
DEFAULT_COOLDOWN_SECONDS: Final[float] = 300.0

#: Strategy for picking a **fresh** proxy when a selection query needs one.
#: ``"round_robin"`` (default) spreads load deterministically, which is
#: usually right for proxies with per-IP quotas. ``"random"`` shuffles
#: picks and is useful when the pool is large and its order is meaningful.
RotationStrategy = Literal["round_robin", "random"]

_log = logger.bind(component="session.proxy")


# --------------------------------------------------------------------------- #
# ProxyConfig                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """One proxy endpoint, with its URL kept as a secret.

    Immutable. Instances are created by the composition root (typically
    from ``Settings.proxy`` via :func:`load_proxy_configs`) and consumed
    by :class:`ProxyManager`, which hands them back to the session. The
    URL — including any embedded credentials — is stored as
    :class:`~pydantic.SecretStr` so that accidental ``repr`` /
    ``str`` never leaks the password.

    Attributes:
        url: The full proxy URL, as a :class:`~pydantic.SecretStr`.
            Must be ``scheme://[user:pass@]host[:port]`` with ``scheme``
            in :data:`_ALLOWED_SCHEMES`.
        label: Optional human-readable identifier (e.g. ``"us-west-res-1"``).
            Used in logs and in error messages instead of the redacted
            URL when set. Empty string means "no label" — log lines
            fall back to :attr:`redacted`.
    """

    url: SecretStr
    label: str = ""

    def __post_init__(self) -> None:
        """Validate URL structure.

        Raises:
            ValueError: If the URL is empty, uses an unsupported scheme,
                has no hostname, has a malformed port, or contains only
                one of username / password. The error message is built
                from :attr:`redacted` — never from the plaintext URL — so
                a validation failure cannot leak a password into a log
                or traceback.
        """
        raw = self.url.get_secret_value()
        if not raw:
            raise ValueError("proxy URL must be non-empty")

        parsed = urlparse(raw)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"proxy URL scheme must be one of "
                f"{sorted(_ALLOWED_SCHEMES)}, got {parsed.scheme!r}"
            )
        if not parsed.hostname:
            raise ValueError(
                f"proxy URL is missing a hostname: {self.redacted!r}"
            )

        # ``parsed.port`` raises ``ValueError`` if the port is malformed
        # (e.g. ``"http://host:notaport"``); force evaluation to surface
        # it here rather than at request time.
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(
                f"proxy URL has an invalid port: {self.redacted!r}: {exc}"
            ) from exc

        has_user = parsed.username is not None
        has_pass = parsed.password is not None
        if has_user != has_pass:
            raise ValueError(
                "proxy URL must include both username and password, "
                "or neither"
            )

    @property
    def redacted(self) -> str:
        """Return the URL with the password replaced by ``***``.

        ``http://user:secret@host:8080`` becomes
        ``http://user:***@host:8080``. A URL with no credentials is
        returned unchanged (nothing to redact). Always safe to log.
        """
        raw = self.url.get_secret_value()
        parsed = urlparse(raw)
        if parsed.password is None:
            return raw
        masked = f"{parsed.username}:***@{parsed.hostname}"
        if parsed.port is not None:
            masked = f"{masked}:{parsed.port}"
        return parsed._replace(netloc=masked).geturl()

    def __repr__(self) -> str:
        """Return a debug representation with the password masked."""
        if self.label:
            return f"ProxyConfig(label={self.label!r}, url={self.redacted!r})"
        return f"ProxyConfig(url={self.redacted!r})"

    def __str__(self) -> str:
        """Return :attr:`label` when set, :attr:`redacted` otherwise."""
        return self.label or self.redacted


# --------------------------------------------------------------------------- #
# Diagnostics                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProxyHealth:
    """Immutable health snapshot for a single proxy.

    One instance per proxy, produced by :meth:`ProxyManager.stats`. The
    admin CLI and the diagnostic endpoint render these verbatim; tests
    assert against individual fields.

    Attributes:
        label: The proxy's ``label`` (empty string when unset).
        redacted_url: The password-masked URL (never the plaintext).
        consecutive_failures: Current run of failures since the last
            success. Zero for a healthy proxy.
        last_error: Human-readable description of the most recent
            failure, or ``None`` if the proxy has never failed.
        cooldown_remaining_seconds: Seconds until the benched proxy
            returns to the available pool. ``0.0`` for an available
            proxy.
        is_available: ``True`` iff the proxy is currently selectable.
            Equivalent to ``cooldown_remaining_seconds == 0.0`` but
            named for callers that just want the boolean.
    """

    label: str
    redacted_url: str
    consecutive_failures: int
    last_error: str | None
    cooldown_remaining_seconds: float
    is_available: bool


@dataclass(frozen=True, slots=True)
class ProxyStats:
    """Immutable aggregate snapshot of the proxy manager.

    Returned by :meth:`ProxyManager.stats`. Proxies are listed in the
    same order they were passed to the manager, so a caller diffing two
    snapshots sees a stable position for each proxy.

    Attributes:
        proxy_count: Total number of configured proxies.
        healthy_count: Proxies currently available (not benched).
        benched_count: Proxies currently benched by cooldown.
        rotation_strategy: The configured :data:`RotationStrategy`.
        sticky: Whether per-site binding is enabled.
        per_proxy: One :class:`ProxyHealth` per configured proxy, in
            declaration order.
    """

    proxy_count: int
    healthy_count: int
    benched_count: int
    rotation_strategy: str
    sticky: bool
    per_proxy: tuple[ProxyHealth, ...]


# --------------------------------------------------------------------------- #
# Internal state                                                              #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _ProxyState:
    """Mutable state for a single proxy inside :class:`ProxyManager`.

    Never escapes the manager — :meth:`ProxyManager.stats` projects it
    onto an immutable :class:`ProxyHealth`.
    """

    config: ProxyConfig
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    last_error: str | None = None
    cooldown_until: float = 0.0


# --------------------------------------------------------------------------- #
# ProxyManager                                                                #
# --------------------------------------------------------------------------- #


class ProxyManager:
    """Select, bind, and health-track proxies for the session.

    See the module docstring for the full contract. The manager is
    asyncio-native, holds no external resources, and is safe to share
    across coroutines.

    Example:
        >>> manager = ProxyManager(load_proxy_configs(settings.proxy))
        >>> config = await manager.get_proxy("mangadex")
        >>> if config is not None:
        ...     # Build a client for this proxy
        ...     client = httpx.AsyncClient(
        ...         proxy=config.url.get_secret_value()
        ...     )
        ...     try:
        ...         response = await client.get(url)
        ...         await manager.mark_success(config)
        ...     except Exception as exc:
        ...         await manager.mark_failure(config, error=str(exc))
        ...         raise

    Args:
        proxies: The proxy pool. Empty or ``None`` disables proxying —
            every :meth:`get_proxy` call returns ``None`` and the session
            uses a direct connection.
        strategy: Rotation strategy for **fresh** picks. Sticky bindings
            are unaffected by the strategy — once a site is bound, the
            same proxy is returned regardless.
        sticky: When ``True`` (the default), a site that receives a
            proxy keeps it until that proxy is benched. Set to ``False``
            to disable per-site binding — every ``get_proxy(site_id)``
            call then behaves like ``get_proxy()`` with a fresh pick.
            Recommended ``False`` only for stateless scraping where the
            target site does not bind sessions to client IP.
        failure_threshold: Consecutive failures that bench a proxy.
            Must be ``>= 1``.
        cooldown_seconds: How long a benched proxy stays excluded.
            ``0.0`` effectively disables health tracking (a proxy becomes
            available again immediately after the failure that benched
            it). Must be ``>= 0``.
        clock: Monotonic clock, injectable for tests. Defaults to
            :func:`time.monotonic`.
        rng: Random source, injectable for deterministic tests. Defaults
            to a module-private :class:`random.Random`.

    Raises:
        ValueError: If ``strategy`` is unknown, ``failure_threshold`` is
            less than 1, ``cooldown_seconds`` is negative, or two proxies
            share the same URL (duplicates would confuse the health
            tracking and the sticky bindings).
    """

    __slots__ = (
        "_clock",
        "_cooldown",
        "_failure_threshold",
        "_lock",
        "_rng",
        "_round_robin_index",
        "_site_bindings",
        "_states",
        "_strategy",
        "_sticky",
    )

    def __init__(
        self,
        proxies: Sequence[ProxyConfig] | None = None,
        *,
        strategy: RotationStrategy = "round_robin",
        sticky: bool = True,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        if strategy not in ("round_robin", "random"):
            raise ValueError(
                f"strategy must be 'round_robin' or 'random', "
                f"got {strategy!r}"
            )
        if failure_threshold < 1:
            raise ValueError(
                f"failure_threshold must be >= 1, got {failure_threshold}"
            )
        if cooldown_seconds < 0:
            raise ValueError(
                f"cooldown_seconds must be >= 0, got {cooldown_seconds}"
            )

        pool = tuple(proxies) if proxies is not None else ()
        # Reject duplicate URLs: the health tracking keys on the URL, so
        # duplicates would silently merge two logical proxies into one.
        seen_urls: set[str] = set()
        states: dict[str, _ProxyState] = {}
        for config in pool:
            url_key = config.url.get_secret_value()
            if url_key in seen_urls:
                raise ValueError(
                    f"duplicate proxy URL in pool: {config.redacted!r}"
                )
            seen_urls.add(url_key)
            states[url_key] = _ProxyState(config=config)

        self._states: dict[str, _ProxyState] = states
        #: site_id → plaintext URL of the proxy it is bound to.
        self._site_bindings: dict[str, str] = {}
        self._strategy: RotationStrategy = strategy
        self._sticky: bool = sticky
        self._failure_threshold: int = failure_threshold
        self._cooldown: float = float(cooldown_seconds)
        self._clock: Callable[[], float] = clock
        self._rng: random.Random = rng if rng is not None else random.Random()
        self._round_robin_index: int = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Selection                                                          #
    # ------------------------------------------------------------------ #

    async def get_proxy(
        self, site_id: str | None = None
    ) -> ProxyConfig | None:
        """Return a proxy for *site_id*, or ``None`` for a direct connection.

        Applies the sticky-binding, health, and rotation rules described
        in the module docstring. Never raises — a missing proxy is a
        valid configuration and an all-benched pool degrades to ``None``
        with a ``WARNING``.

        Args:
            site_id: The site the request targets, used for sticky
                binding. ``None`` requests a fresh pick with no affinity
                (used by, e.g., health-check requests that are not
                associated with a site).

        Returns:
            A :class:`ProxyConfig` (password still masked in ``repr``)
            or ``None`` when no proxy should be used.
        """
        async with self._lock:
            if not self._states:
                return None

            # Sticky path: honour an existing binding if the proxy behind
            # it is still available.
            if site_id is not None and self._sticky:
                bound_url = self._site_bindings.get(site_id)
                if bound_url is not None:
                    state = self._states.get(bound_url)
                    if state is not None and self._is_available_locked(state):
                        return state.config
                    # The binding is stale (proxy benched or removed).
                    self._site_bindings.pop(site_id, None)

            available = [
                state
                for state in self._states.values()
                if self._is_available_locked(state)
            ]
            if not available:
                _log.warning(
                    "All {} configured proxies are benched; falling back "
                    "to a direct connection for site_id={!r}",
                    len(self._states),
                    site_id,
                )
                return None

            picked = self._pick_locked(available)
            if site_id is not None and self._sticky:
                self._site_bindings[site_id] = picked.config.url.get_secret_value()
            return picked.config

    # ------------------------------------------------------------------ #
    # Health updates                                                     #
    # ------------------------------------------------------------------ #

    async def mark_success(
        self, proxy: ProxyConfig | str
    ) -> None:
        """Record a successful request through *proxy*.

        Clears the proxy's failure streak and any active cooldown. Unknown
        proxies (an object the manager does not know about) are ignored
        silently — a session that calls ``mark_success`` on a proxy it
        was not handed is not a bug worth surfacing.

        Args:
            proxy: The config that was used, or the URL string. Both
                forms are accepted so a session that stores only the URL
                (e.g. because it caches ``httpx.AsyncClient`` per URL)
                can still report success.
        """
        url_key = _extract_url(proxy)
        async with self._lock:
            state = self._states.get(url_key)
            if state is None:
                return
            state.consecutive_failures = 0
            state.last_failure_at = None
            state.last_error = None
            state.cooldown_until = 0.0

    async def mark_failure(
        self,
        proxy: ProxyConfig | str,
        *,
        error: str | None = None,
    ) -> None:
        """Record a failed request through *proxy*.

        Increments the proxy's failure streak. When the streak reaches
        :attr:`failure_threshold`, the proxy enters cooldown until
        ``clock() + cooldown_seconds``. Every sticky binding to the
        benched proxy is dropped so the next :meth:`get_proxy` call for
        any site re-picks a healthy proxy.

        Calls to ``mark_failure`` on a proxy already in cooldown are
        still recorded (they bump the streak further, which is
        information the operator sees in :meth:`stats`) but do not extend
        the cooldown — the proxy is benched until ``cooldown_until``
        regardless of how many more failures it produces.

        Args:
            proxy: The config that was used, or the URL string. See
                :meth:`mark_success` for why both forms are accepted.
            error: Optional human-readable error description. Stored in
                the proxy's health snapshot and echoed in the ``WARNING``
                log line when the proxy is benched.
        """
        url_key = _extract_url(proxy)
        now = self._clock()
        async with self._lock:
            state = self._states.get(url_key)
            if state is None:
                return
            state.consecutive_failures += 1
            state.last_failure_at = now
            state.last_error = error
            if state.cooldown_until > now:
                # Already benched — do not extend the cooldown.
                return
            if state.consecutive_failures < self._failure_threshold:
                return
            state.cooldown_until = now + self._cooldown
            # Drop every sticky binding pointing at this proxy so the
            # next get_proxy() for any of those sites re-picks.
            dropped_sites = [
                sid
                for sid, bound_url in self._site_bindings.items()
                if bound_url == url_key
            ]
            for sid in dropped_sites:
                del self._site_bindings[sid]
            _log.warning(
                "Proxy {} benched for {:.0f}s after {} consecutive "
                "failures ({} binding(s) dropped): {}",
                state.config.label or state.config.redacted,
                self._cooldown,
                state.consecutive_failures,
                len(dropped_sites),
                error or "<no error message>",
            )

    # ------------------------------------------------------------------ #
    # Maintenance                                                        #
    # ------------------------------------------------------------------ #

    async def reset(self) -> None:
        """Reset every proxy to healthy and clear all sticky bindings.

        Cumulative failure counters are set back to zero, cooldowns are
        cleared, sticky bindings are dropped, and the round-robin index
        is rewound. Intended for tests and for an explicit "everything
        looks healthy now, start fresh" operation (an operator has just
        fixed the upstream network).
        """
        async with self._lock:
            for state in self._states.values():
                state.consecutive_failures = 0
                state.last_failure_at = None
                state.last_error = None
                state.cooldown_until = 0.0
            self._site_bindings.clear()
            self._round_robin_index = 0

    async def stats(self) -> ProxyStats:
        """Return an immutable snapshot of the manager's state.

        Refreshes cooldown state against the current clock before
        projecting, so a proxy whose cooldown just expired reports as
        available even if no selection query has run since.

        Returns:
            A :class:`ProxyStats` with aggregate counts and one
            :class:`ProxyHealth` per configured proxy.
        """
        now = self._clock()
        async with self._lock:
            healths: list[ProxyHealth] = []
            healthy = 0
            benched = 0
            for state in self._states.values():
                remaining = max(0.0, state.cooldown_until - now)
                available = remaining == 0.0
                if available:
                    healthy += 1
                else:
                    benched += 1
                healths.append(
                    ProxyHealth(
                        label=state.config.label,
                        redacted_url=state.config.redacted,
                        consecutive_failures=state.consecutive_failures,
                        last_error=state.last_error,
                        cooldown_remaining_seconds=remaining,
                        is_available=available,
                    )
                )
            return ProxyStats(
                proxy_count=len(self._states),
                healthy_count=healthy,
                benched_count=benched,
                rotation_strategy=self._strategy,
                sticky=self._sticky,
                per_proxy=tuple(healths),
            )

    # ------------------------------------------------------------------ #
    # Read-only properties                                               #
    # ------------------------------------------------------------------ #

    @property
    def proxy_count(self) -> int:
        """The number of configured proxies (0 for a direct-only setup)."""
        return len(self._states)

    @property
    def has_proxies(self) -> bool:
        """``True`` iff at least one proxy is configured (of any state)."""
        return bool(self._states)

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"ProxyManager(proxies={len(self._states)}, "
            f"strategy={self._strategy!r}, "
            f"sticky={self._sticky}, "
            f"bindings={len(self._site_bindings)})"
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _is_available_locked(self, state: _ProxyState) -> bool:
        """Return ``True`` iff *state* is not currently in cooldown.

        Caller must hold :attr:`_lock`.
        """
        return state.cooldown_until <= self._clock()

    def _pick_locked(
        self, available: list[_ProxyState]
    ) -> _ProxyState:
        """Pick a proxy from *available* per the configured strategy.

        Caller must hold :attr:`_lock`. The round-robin index persists
        across calls; when the ``available`` set changes size, the index
        is taken modulo the new size, which distributes evenly without
        any special handling.
        """
        if self._strategy == "random":
            return self._rng.choice(available)
        picked = available[self._round_robin_index % len(available)]
        self._round_robin_index = (self._round_robin_index + 1) % len(available)
        return picked


# --------------------------------------------------------------------------- #
# Settings integration                                                       #
# --------------------------------------------------------------------------- #


def load_proxy_configs(config: object | None) -> tuple[ProxyConfig, ...]:
    """Extract :class:`ProxyConfig` instances from a ``Settings.proxy`` section.

    Defensive against every shape the section can take:

    * ``None`` (the section is absent) — returns ``()``.
    * ``enabled=False`` — returns ``()` regardless of what else is set.
    * ``url=<single>`` — one proxy.
    * ``urls=<list>`` — a rotation pool.
    * Both — the single URL is placed **first** in the resulting tuple,
      so ``round_robin`` picks it first on a fresh start.

    Each raw entry may be a :class:`~pydantic.SecretStr` (the shape the
    Pydantic settings parser produces) or a plain :class:`str` (a test
    fixture, or a settings model that predates the SecretStr
    convention). Both forms are normalised to
    :class:`~pydantic.SecretStr` before constructing the config.

    Args:
        config: The ``settings.proxy`` object. Any object that exposes
            ``enabled``, ``url``, and ``urls`` attributes works — the
            function uses :func:`getattr` with defaults so a partially-
            populated section is handled gracefully.

    Returns:
        A tuple of validated :class:`ProxyConfig` instances, in the
        order described above. Empty when the section is absent or
        disabled.

    Raises:
        ValueError: If any configured URL is invalid — unsupported
            scheme, missing host, malformed port, half-configured
            credentials. A broken proxy URL is a configuration error
            worth surfacing at startup rather than at the first request.
    """
    if config is None:
        return ()
    if not getattr(config, "enabled", True):
        return ()

    raw_entries: list[object] = []
    single = getattr(config, "url", None)
    if single is not None:
        raw_entries.append(single)
    multi = getattr(config, "urls", None) or ()
    raw_entries.extend(multi)

    result: list[ProxyConfig] = []
    for raw in raw_entries:
        secret = raw if isinstance(raw, SecretStr) else SecretStr(str(raw))
        result.append(ProxyConfig(url=secret))
    return tuple(result)


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _extract_url(proxy: ProxyConfig | str) -> str:
    """Return the plaintext URL key for *proxy*.

    Accepts either a :class:`ProxyConfig` (whose URL is a
    :class:`~pydantic.SecretStr`) or a raw URL string, normalising both
    to the plaintext form the manager keys on internally.

    Args:
        proxy: A config or a URL string.

    Returns:
        The plaintext proxy URL.
    """
    if isinstance(proxy, ProxyConfig):
        return proxy.url.get_secret_value()
    return proxy
