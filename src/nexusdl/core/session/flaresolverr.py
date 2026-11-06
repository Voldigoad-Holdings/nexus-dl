"""FlareSolverr client for solving Cloudflare challenges.

FlareSolverr is an external service that drives a real browser through a
Cloudflare interstitial and returns the resulting cookies, User-Agent,
and (optionally) the resolved HTML. NexusDL uses it as a **last-resort
bypass** for sites the plain HTTP path cannot reach, when the deployment
prefers a shared external service over running Playwright in-process.
 
Where it fits
-------------
::

    CloudflareMixin (parser-level)
        ├── 1. plain HTTP (HttpSession)                  fast path
        ├── 2. Playwright context (PlaywrightPool)       local browser
        └── 3. FlareSolverrClient.solve(url)             external service

The mixin tries each bypass in order and stops at the first that
produces cleared cookies. A site that is *not* behind Cloudflare never
touches this module.

Same "no retry" contract as ``http_session.py``
-----------------------------------------------
Every :meth:`FlareSolverrClient.solve` call performs exactly **one**
attempt. Retrying is the caller's responsibility; the pipeline's retry
controller (:mod:`nexusdl.core.downloader.retry`) wraps the parser call
that ultimately invokes this client. FlareSolverr's own
``maxTimeout`` is passed through — it is the *server's* internal budget
for the browser to solve the challenge, not the client's retry policy.

Two timeouts, one call
----------------------
A single :meth:`solve` call involves two timeout budgets that must be
coordinated:

* **FlareSolverr-side** — ``maxTimeout`` in the request payload bounds
  how long the *server's* browser may spend solving the challenge.
* **HTTP-side** — the httpx client needs its own timeout for the
  round-trip to the FlareSolverr server. This is
  ``maxTimeout / 1000 + grace`` where grace is
  :data:`_HTTPX_GRACE_SECONDS` — just enough to cover network latency
  and the server's own response serialisation.

Setting the HTTP timeout equal to ``maxTimeout`` would race: the server
would often finish *just* as the client gave up. The grace period makes
the client always outlast the server, so the only case where the client
times out is a genuine network or server failure.

Result parsing is defensive
---------------------------
The FlareSolverr JSON API returns a nested payload with a ``status``
field, a ``solution`` object, and (on some versions) a ``message``.
Different FlareSolverr releases have slightly different shapes — a
``solution.response`` may be a string, a base64 blob, or absent; a
``cookies`` list may be missing entirely on a solved-but-empty case. The
parser here:

* reads fields via :func:`Mapping.get` with sensible defaults,
* converts a base64-encoded ``response`` to text if the
  ``Content-Type`` in ``solution.headers`` indicates a text format
  (HTML, XML, JSON),
* returns a :class:`FlareSolverrResult` whose optional fields are
  ``None`` rather than raising,
* never inspects anything it did not opt into.

A missing ``solution`` block or a ``status`` other than ``"ok"`` is the
only condition that raises — a successful HTTP call to FlareSolverr that
*also* produced a challenge-cleared solution is the only success case.

Dependency graph (no cycles)::

    core.exceptions  ─┐
    loguru            ├──► core.session.flaresolverr
    httpx             │
    asyncio, base64, json, time

No dependency on ``core.config``, ``core.models``, or any other
``core.session`` module — this client is a leaf. It could be lifted into
a standalone library with the ``core.exceptions`` import replaced by a
local exception.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

import httpx
from loguru import logger

from nexusdl.core.exceptions import (
    SessionCloudflareError,
    SessionError,
    SessionTimeoutError,
)


__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT_MS",
    "FlareSolverrClient",
    "FlareSolverrResult",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default FlareSolverr endpoint. Matches the FlareSolverr project's own
#: default; the composition root typically overrides via
#: ``Settings.flaresolverr.endpoint``.
DEFAULT_ENDPOINT: Final[str] = "http://localhost:8191/v1"

#: Default ``maxTimeout``, in milliseconds, passed to FlareSolverr.
#: Matches the project's recommendation for a Cloudflare interstitial —
#: 60 s is enough for the challenge to complete on a slow connection.
DEFAULT_TIMEOUT_MS: Final[int] = 60_000

#: Minimum accepted ``maxTimeout``. Below this, FlareSolverr's own browser
#: launch (which alone takes 200–2000 ms) would eat most of the budget.
_MIN_TIMEOUT_MS: Final[int] = 5_000

#: Maximum accepted ``maxTimeout``. Beyond this a stuck FlareSolverr
#: would hold an HTTP connection for minutes; the operator should
#: investigate, not wait.
_MAX_TIMEOUT_MS: Final[int] = 180_000

#: Grace, in seconds, added to the FlareSolverr-side timeout to bound the
#: HTTP round-trip from the client's side. See the module docstring.
_HTTPX_GRACE_SECONDS: Final[float] = 5.0

#: FlareSolverr command for a GET-challenge solve.
_COMMAND_REQUEST_GET: Final[str] = "request.get"

#: FlareSolverr command for listing active sessions. Used by
#: :meth:`FlareSolverrClient.health` — cheap, side-effect-free.
_COMMAND_SESSIONS_LIST: Final[str] = "sessions.list"

#: Body ``status`` value that indicates success.
_STATUS_OK: Final[str] = "ok"

#: Content-Type prefixes that identify a body as text (and thus as a
#: candidate for base64 decoding in FlareSolverr's ``response`` field).
_TEXTUAL_CONTENT_TYPE_PREFIXES: Final[tuple[str, ...]] = (
    "text/",
    "application/xhtml",
    "application/xml",
    "application/json",
)

#: HTML prefixes accepted by :func:`_looks_like_html`. A genuine
#: Cloudflare-cleared page is virtually always one of these; the check
#: is a heuristic to distinguish "FlareSolverr returned HTML" from
#: "FlareSolverr returned a base64 blob or a JSON fragment".
_HTML_PREFIXES: Final[tuple[str, ...]] = (
    "<!doctype",
    "<html",
    "<!--",
    "<?xml",
)

_log = logger.bind(component="session.flaresolverr")


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FlareSolverrResult:
    """The solution returned by a successful FlareSolverr call.

    Immutable. Every field describes what the external service reported
    — the client does not synthesise or guess. Optional fields are
    ``None`` when FlareSolverr omitted the corresponding data, which is
    common (some sites solve the challenge without ever exposing the
    HTML; some responses have no ``userAgent``).

    Attributes:
        url: The URL that was solved. Typically equal to the requested
            URL; occasionally different if the challenge redirected.
        status: HTTP status of the *solved* page, as reported by
            FlareSolverr's internal browser (``200`` for a clean solve,
            ``403`` if the challenge was bypassed but the target then
            rejected the request). ``0`` when FlareSolverr did not
            report a status.
        cookies: The cookies the browser obtained while solving, as a
            ``name → value`` mapping. Contains at minimum
            ``cf_clearance`` on a successful Cloudflare bypass.
            Immutable.
        user_agent: The ``User-Agent`` string the browser used. Callers
            that make follow-up requests should reuse this exact UA —
            Cloudflare binds ``cf_clearance`` to the UA that obtained it,
            and a different UA will be rejected.
        html: The resolved page HTML, when FlareSolverr returned it.
            ``None`` for sites that solve the challenge without
            exposing content, or when the caller opted out via the
            ``return_only_cookies`` flag on :meth:`FlareSolverrClient.solve`.
        response_time_ms: Wall-clock time the solve took, in milliseconds,
            as reported by FlareSolverr (``endTimestamp - startTimestamp``).
            ``None`` when the timestamps were not returned.
    """

    url: str
    status: int
    cookies: Mapping[str, str]
    user_agent: str | None
    html: str | None
    response_time_ms: int | None

    def __post_init__(self) -> None:
        """Freeze the cookies mapping so the result is genuinely immutable.

        The caller passing a plain ``dict`` for ``cookies`` is a common
        convenience (FlareSolverr's JSON parser produces one); without
        this wrap, mutating the dict after construction would silently
        mutate the "immutable" result. Wrapping in
        :class:`types.MappingProxyType` closes that hole.

        Raises:
            TypeError: If ``cookies`` is not a mapping of ``str → str``.
        """
        if not isinstance(self.cookies, Mapping):
            raise TypeError(
                f"FlareSolverrResult.cookies must be a mapping, got "
                f"{type(self.cookies).__name__}"
            )
        for name, value in self.cookies.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError(
                    "FlareSolverrResult.cookies must map str to str; "
                    f"got {name!r} -> {type(value).__name__}"
                )
        if not isinstance(self.cookies, MappingProxyType):
            object.__setattr__(
                self, "cookies", MappingProxyType(dict(self.cookies))
            )


# --------------------------------------------------------------------------- #
# Client                                                                      #
# --------------------------------------------------------------------------- #


class FlareSolverrClient:
    """Async client for a FlareSolverr service.

    One instance per composition root, created at startup and closed at
    shutdown. Safe to share across coroutines.

    See the module docstring for the two-timeout design, the "no retry"
    contract, and the error translation.

    Example:
        >>> client = FlareSolverrClient(endpoint="http://localhost:8191/v1")
        >>> try:
        ...     result = await client.solve(
        ...         "https://example.org/protected",
        ...         site_id="example",
        ...     )
        ...     # Store ``result.cookies`` on the session, reuse
        ...     # ``result.user_agent`` for follow-up requests.
        ... finally:
        ...     await client.close()

    Args:
        endpoint: FlareSolverr's HTTP endpoint (typically
            ``http://host:8191/v1``). The trailing path segment is
            preserved verbatim — FlareSolverr serves its JSON API at
            ``/v1`` and does not accept requests on the bare host.
        timeout_ms: ``maxTimeout`` sent to FlareSolverr, in milliseconds.
            Bounds how long the server's internal browser may spend on a
            challenge. Must be in
            ``[``:data:`_MIN_TIMEOUT_MS`, ``:data:`_MAX_TIMEOUT_MS`, ``]``.
        enabled: Master switch. When ``False``, every :meth:`solve` call
            raises :class:`~nexusdl.core.exceptions.SessionError` with a
            clear message. Intended for a composition root that reads
            ``Settings.flaresolverr.enabled`` and wants a single
            construction site regardless of the setting.

    Raises:
        ValueError: If ``endpoint`` is empty, ``timeout_ms`` is out of
            range, or ``timeout_ms`` is not an ``int``.
    """

    __slots__ = (
        "_client",
        "_client_lock",
        "_closed",
        "_enabled",
        "_endpoint",
        "_timeout_ms",
    )

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        enabled: bool = True,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint must be non-empty")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TypeError(
                f"timeout_ms must be an int, got "
                f"{type(timeout_ms).__name__}"
            )
        if not (_MIN_TIMEOUT_MS <= timeout_ms <= _MAX_TIMEOUT_MS):
            raise ValueError(
                f"timeout_ms must be in [{_MIN_TIMEOUT_MS}, "
                f"{_MAX_TIMEOUT_MS}], got {timeout_ms}"
            )

        self._endpoint: str = endpoint
        self._timeout_ms: int = timeout_ms
        self._enabled: bool = enabled
        self._client: httpx.AsyncClient | None = None
        # Serialises client creation so two concurrent first-solves
        # cannot each build an ``AsyncClient``. See ``_ensure_client``
        # docstring.
        self._client_lock = asyncio.Lock()
        self._closed: bool = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> FlareSolverrClient:
        """Return ``self``. No setup is required at entry time."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the underlying HTTP client. Never suppresses exceptions."""
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client and refuse further calls.

        Idempotent. After ``close()`` returns, every :meth:`solve` call
        raises :class:`SessionError` with a message that names the
        reason. The composition root calls this exactly once per
        application lifetime.
        """
        if self._closed:
            return
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — cleanup, log and continue
                _log.warning(
                    "Error closing FlareSolverr client: {}: {}",
                    type(exc).__name__,
                    exc,
                )

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    @property
    def is_enabled(self) -> bool:
        """``True`` iff the client is configured to perform solves."""
        return self._enabled

    @property
    def endpoint(self) -> str:
        """The configured FlareSolverr endpoint."""
        return self._endpoint

    async def solve(
        self,
        url: str,
        *,
        site_id: str | None = None,
        return_only_cookies: bool = False,
    ) -> FlareSolverrResult:
        """Solve a Cloudflare challenge for *url* via FlareSolverr.

        Sends a single ``request.get`` command to the FlareSolverr
        server and translates the response into a
        :class:`FlareSolverrResult`. See the module docstring for the
        two-timeout model.

        Args:
            url: The URL to solve. Must be absolute; the client does not
                validate the scheme beyond what ``urlparse`` accepts, but
                FlareSolverr itself rejects non-HTTP(S) inputs.
            site_id: Optional identifier of the source site, used only
                in error messages and logs. Does not influence the
                FlareSolverr command.
            return_only_cookies: When ``True``, requests that
                FlareSolverr omit the resolved HTML from its response —
                faster and lighter when the caller only needs the
                cookies and UA for follow-up requests. When ``False``
                (the default), the HTML is returned when available.

        Returns:
            A :class:`FlareSolverrResult` describing what FlareSolverr
            reported.

        Raises:
            SessionError: If the client is disabled, if the underlying
                HTTP call to FlareSolverr fails at the transport level
                (connection refused, DNS failure, malformed URL), or if
                FlareSolverr returns a non-2xx HTTP status.
            SessionTimeoutError: If the HTTP call to FlareSolverr times
                out (network or FlareSolverr-side, before the server
                returns a payload).
            SessionCloudflareError: If FlareSolverr returns a payload
                whose ``status`` field is not ``"ok"`` — the challenge
                was attempted but could not be solved. This is the
                common failure mode when a site has strengthened its
                challenge beyond what FlareSolverr can bypass.
        """
        if not self._enabled:
            raise SessionError(
                "FlareSolverr is disabled in this configuration "
                "(Settings.flaresolverr.enabled is False); the caller "
                "should not be attempting a solve"
            )
        if self._closed:
            raise SessionError(
                "FlareSolverrClient is closed; create a new instance to "
                "continue solving challenges"
            )

        payload: dict[str, Any] = {
            "cmd": _COMMAND_REQUEST_GET,
            "url": url,
            "maxTimeout": self._timeout_ms,
        }
        if return_only_cookies:
            payload["returnOnlyCookies"] = True

        response = await self._post(payload, url=url, site_id=site_id)
        return self._parse_response(response, url=url, site_id=site_id)

    async def health(self) -> bool:
        """Return ``True`` iff FlareSolverr is reachable and responding.

        Issues a side-effect-free ``sessions.list`` command. Intended for
        the composition root's startup sequence and for the diagnostic
        endpoint — a caller should not call this on every solve (a
        successful :meth:`solve` is itself the best health check).

        A disabled client returns ``False`` without contacting the
        server, and **logs at DEBUG** so an operator inspecting logs
        can distinguish "disabled by configuration" from "unreachable"
        (both yield ``False``, but only the latter is a problem).
        A closed client raises :class:`SessionError` — after close,
        the caller must not be using the client at all, so a silent
        ``False`` would hide a programming error.

        Returns:
            ``True`` iff the server responded with a successful payload,
            ``False`` on any transport or payload error. Never raises
            except for the "client is closed" case.
        """
        if not self._enabled:
            _log.debug(
                "FlareSolverr health check skipped: client is disabled"
            )
            return False
        if self._closed:
            raise SessionError(
                "FlareSolverrClient is closed; health() cannot run"
            )
        try:
            response = await self._post(
                {"cmd": _COMMAND_SESSIONS_LIST},
                url=self._endpoint,
                site_id=None,
                raise_on_status=False,
            )
        except (SessionError, SessionTimeoutError):
            return False
        return response.status_code == 200

    # ------------------------------------------------------------------ #
    # Internals — HTTP                                                   #
    # ------------------------------------------------------------------ #

    async def _post(
        self,
        payload: Mapping[str, Any],
        *,
        url: str,
        site_id: str | None,
        raise_on_status: bool = True,
    ) -> httpx.Response:
        """POST *payload* to FlareSolverr and return the raw response.

        Args:
            payload: The command payload.
            url: The URL being solved (for error messages), or the
                endpoint URL itself for the health command.
            site_id: For error messages only.
            raise_on_status: When ``True``, a non-2xx status from the
                FlareSolverr server itself raises. When ``False``, the
                raw response is returned so the caller can inspect it
                (used by :meth:`health`, where a 5xx from FlareSolverr
                means "not healthy" rather than "call failed").

        Raises:
            SessionError: On a non-2xx response (when
                ``raise_on_status``) or any transport error that is not
                a timeout.
            SessionTimeoutError: On httpx timeout exceptions.
        """
        client = await self._ensure_client()
        # The client-side HTTP timeout is FlareSolverr's maxTimeout plus a
        # grace period. See the module docstring.
        http_timeout = self._timeout_ms / 1000.0 + _HTTPX_GRACE_SECONDS

        try:
            response = await client.post(
                self._endpoint, json=payload, timeout=http_timeout
            )
        except httpx.TimeoutException as exc:
            raise SessionTimeoutError(
                f"timeout after {http_timeout:.1f}s calling FlareSolverr "
                f"for {url!r}"
                + (f" (site_id={site_id!r})" if site_id else "")
            ) from exc
        except httpx.HTTPError as exc:
            raise SessionError(
                f"transport error calling FlareSolverr for {url!r}: "
                f"{type(exc).__name__}: {exc}"
                + (f" (site_id={site_id!r})" if site_id else "")
            ) from exc

        if raise_on_status and not (200 <= response.status_code < 300):
            raise SessionError(
                f"FlareSolverr returned HTTP {response.status_code} for "
                f"{url!r}"
                + (f" (site_id={site_id!r})" if site_id else "")
            )
        return response

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the underlying client, creating it lazily on first use.

        Creation is serialised by ``_client_lock`` so concurrent
        first-solves cannot each build an ``AsyncClient`` — the loser
        of such a race would leak its connection pool. After the first
        successful creation the lock is uncontended and the dict
        lookup is atomic; the fast path avoids acquiring the lock at
        all in steady state.

        Raises:
            SessionError: If the client has been closed between the
                caller's check and the acquisition of the lock (a
                ``close()`` racing a ``solve()`` — the caller should
                retry with a fresh client).
        """
        # Fast path — no lock needed once the client exists.
        client = self._client
        if client is not None:
            return client

        async with self._client_lock:
            if self._closed:
                raise SessionError(
                    "FlareSolverrClient is closed; create a new instance "
                    "to continue solving challenges"
                )
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=None,  # per-request timeout is set explicitly
                )
            return self._client

    # ------------------------------------------------------------------ #
    # Internals — payload parsing                                        #
    # ------------------------------------------------------------------ #

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        url: str,
        site_id: str | None,
    ) -> FlareSolverrResult:
        """Parse a FlareSolverr response payload into a result object.

        Raises:
            SessionError: If the response body is not valid JSON.
            SessionCloudflareError: If the payload's ``status`` field is
                not ``"ok"``, or if the payload contains no usable
                ``solution``.
        """
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            # A proxy that intercepts ``/v1`` (or a FlareSolverr that
            # serves an HTML error page instead of the JSON API) lands
            # here. Echo a short prefix of the body so the operator
            # can identify what actually responded.
            body_preview = response.text[:120].replace("\n", " ")
            raise SessionError(
                f"FlareSolverr returned invalid JSON for {url!r}: "
                f"{type(exc).__name__}: {exc} "
                f"(body starts with: {body_preview!r})"
            ) from exc

        if not isinstance(payload, Mapping):
            raise SessionError(
                f"FlareSolverr returned a non-object payload for "
                f"{url!r}: {type(payload).__name__}"
            )

        status = payload.get("status")
        if status != _STATUS_OK:
            message = payload.get("message", "<no message>")
            raise SessionCloudflareError(
                f"FlareSolverr could not solve the challenge for {url!r}"
                + (f" (site_id={site_id!r})" if site_id else "")
                + f": status={status!r}, message={message!r}"
            )

        solution = payload.get("solution")
        if not isinstance(solution, Mapping):
            raise SessionCloudflareError(
                f"FlareSolverr reported success but returned no "
                f"solution for {url!r}"
            )

        return self._build_result(
            solution,
            request_url=url,
            start_ts=_coerce_int(payload.get("startTimestamp")),
            end_ts=_coerce_int(payload.get("endTimestamp")),
        )

    def _build_result(
        self,
        solution: Mapping[str, Any],
        *,
        request_url: str,
        start_ts: int | None,
        end_ts: int | None,
    ) -> FlareSolverrResult:
        """Construct a :class:`FlareSolverrResult` from a ``solution`` payload.

        Every field is defensive: FlareSolverr versions differ on what
        they populate, and a caller is expected to handle missing fields
        rather than crash on a partial payload.
        """
        solved_url = solution.get("url")
        if not isinstance(solved_url, str) or not solved_url:
            solved_url = request_url

        status = _coerce_int(solution.get("status")) or 0

        user_agent_raw = solution.get("userAgent")
        user_agent = user_agent_raw if isinstance(user_agent_raw, str) else None

        html = _extract_html(
            solution.get("response"),
            solution.get("headers"),
        )

        cookies = _extract_cookies(solution.get("cookies"))

        response_time_ms: int | None = None
        if start_ts is not None and end_ts is not None and end_ts >= start_ts:
            response_time_ms = (end_ts - start_ts) * 1000

        return FlareSolverrResult(
            url=solved_url,
            status=status,
            cookies=cookies,
            user_agent=user_agent,
            html=html,
            response_time_ms=response_time_ms,
        )

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"FlareSolverrClient(endpoint={self._endpoint!r}, "
            f"enabled={self._enabled}, "
            f"timeout_ms={self._timeout_ms}, "
            f"closed={self._closed})"
        )


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _coerce_int(value: Any) -> int | None:
    """Return *value* as an ``int`` if possible, else ``None``.

    Defends against FlareSolverr returning ``"200"`` as a string on some
    versions while returning ``200`` as an int on others. Both are
    normalised to ``int``; non-numeric values become ``None`` rather than
    raising — a solver that succeeded but reported an odd field should
    still produce a usable result.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _extract_cookies(raw: Any) -> Mapping[str, str]:
    """Turn FlareSolverr's cookie list into a ``name → value`` mapping.

    FlareSolverr returns cookies as a list of objects:
    ``[{"name": "cf_clearance", "value": "...", "domain": "..."}, ...]``.
    We discard everything except the name and value — the caller only
    needs those to reconstruct the ``Cookie`` header for follow-up
    requests. Domain/path/expiry tracking is the ``CookieManager``'s
    job, not this client's.

    When the same cookie name appears twice (across different domains),
    the last occurrence wins. Rare in practice for the cookies
    FlareSolverr produces (``cf_clearance`` is the main one and it is
    scoped to a single domain), and there is no more correct choice
    without also carrying the domain into the caller's scope.
    """
    if not isinstance(raw, list):
        return MappingProxyType({})
    result: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name] = value
    return MappingProxyType(result)


def _is_textual_content_type(headers: Any) -> bool:
    """Return ``True`` iff *headers* describes a textual content type.

    FlareSolverr echoes the solved response's headers in
    ``solution.headers``. When the body is base64-encoded (binary
    responses), the ``Content-Type`` names the decoded format. We use
    this to decide whether a base64 blob is a candidate for decoding
    into HTML.

    Args:
        headers: The ``solution.headers`` value, expected to be either a
            mapping or a list of ``{"name": ..., "value": ...}`` dicts
            (FlareSolverr returns the latter on some versions).

    Returns:
        ``True`` iff the Content-Type header is present and starts with
        a textual prefix from :data:`_TEXTUAL_CONTENT_TYPE_PREFIXES`.
    """
    content_type = _find_header(headers, "content-type")
    if not content_type:
        return False
    lowered = content_type.lower().lstrip()
    return any(lowered.startswith(prefix) for prefix in _TEXTUAL_CONTENT_TYPE_PREFIXES)


def _find_header(headers: Any, name: str) -> str | None:
    """Return the header value for *name*, or ``None``.

    Handles both FlareSolverr header shapes: a plain mapping
    ``{"Name": "Value"}`` and the list-of-dicts form
    ``[{"name": "Name", "value": "Value"}, ...]``. Matching is
    case-insensitive.
    """
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == name.lower():
                return value if isinstance(value, str) else None
        return None
    if isinstance(headers, list):
        for entry in headers:
            if not isinstance(entry, Mapping):
                continue
            entry_name = entry.get("name")
            entry_value = entry.get("value")
            if (
                isinstance(entry_name, str)
                and entry_name.lower() == name.lower()
                and isinstance(entry_value, str)
            ):
                return entry_value
        return None
    return None


def _looks_like_html(text: str) -> bool:
    """Return ``True`` iff *text* plausibly starts with an HTML prologue.

    Used to sanity-check a body whose ``Content-Type`` was reported as
    textual: a base64 blob (or a JSON fragment) served under
    ``text/html`` should not be handed to the caller as HTML.

    Accepts a leading UTF-8 BOM and any leading whitespace before
    matching one of :data:`_HTML_PREFIXES`. Comments (``<!--``) are
    accepted because some servers prepend one before the DOCTYPE.
    """
    stripped = text.lstrip("\ufeff \t\r\n").lower()
    return stripped.startswith(_HTML_PREFIXES)


def _extract_html(raw: Any, headers: Any) -> str | None:
    """Extract the resolved HTML from a solution payload.

    FlareSolverr returns the body in ``solution.response``:

    * For **textual** responses (HTML, XML, JSON), ``response`` is
      already a decoded string.
    * For **binary** responses, ``response`` is a base64-encoded blob
      and the decoded format is named by ``Content-Type`` in
      ``solution.headers``.

    This function returns the HTML body in the textual case, decodes
    the base64 blob in the binary case when the ``Content-Type`` names
    a textual format, and returns ``None`` in every other case
    (non-HTML responses, undecodable blobs, unparseable payloads).

    Args:
        raw: The ``solution.response`` value.
        headers: The ``solution.headers`` value, used to distinguish
            base64-encoded binary from textual HTML.

    Returns:
        The HTML string, or ``None`` if the response was not HTML.
    """
    if isinstance(raw, str):
        # Textual payload. Confirm it plausibly looks like HTML (or XML)
        # before handing it to the caller — FlareSolverr occasionally
        # returns a JSON error body here when the target redirects the
        # solver to an API endpoint.
        if _looks_like_html(raw):
            return raw
        return None

    if not isinstance(raw, (bytes, bytearray)):
        # FlareSolverr may return a base64-encoded *string* for binary
        # responses (not bytes). Try that path.
        if not isinstance(raw, str):
            return None

    # Binary / base64-encoded string. Only decode when the reported
    # Content-Type is textual — decoding an image as HTML would waste
    # memory and produce garbage for the caller.
    if not _is_textual_content_type(headers):
        return None

    if isinstance(raw, (bytes, bytearray)):
        # FlareSolverr returned raw bytes (rare, some proxy setups).
        try:
            decoded = bytes(raw).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
    else:
        # Base64-encoded string (the common binary case).
        try:
            decoded_bytes = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            return None
        try:
            decoded = decoded_bytes.decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None

    if _looks_like_html(decoded):
        return decoded
    return None
