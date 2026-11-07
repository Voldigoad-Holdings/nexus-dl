"""Health, readiness, and version router.

Serves the three infrastructure-probe endpoints and the version endpoint:

* ``GET /health``      — **liveness**. Always 200 if the process can respond.
* ``GET /health/live`` — alias for ``GET /health`` (Kubernetes convention).
* ``GET /health/ready`` — **readiness**. 200 when dependencies are OK,
  503 when they are not. Pulls the pod out of rotation, does **not** kill it.
* ``GET /api/version`` — version and build metadata.

None of these endpoints are authenticated. Kubernetes / Docker / systemd
probes cannot supply a JWT; requiring auth here would restart the pod on
every liveness cycle.

Liveness vs readiness
=====================

The distinction matters:

* **Liveness** answers *"is this process alive?"*. Its failure causes the
  orchestrator to **restart** the pod. It must **never** depend on
  external services — a Redis outage that fails liveness would produce a
  restart loop, not a recovery.
* **Readiness** answers *"can this process serve traffic?"*. Its failure
  causes the orchestrator to **remove the pod from the load balancer**.
  This is where dependency checks belong.

This router follows that split: ``/health`` always returns
``status="healthy"`` (the response proves the process is alive), while
``/health/ready`` runs dependency probes and returns 503 when they fail.

Probes (data-driven)
====================

Readiness discovers probes on the injected ``ServicesDep`` in this order:

1. ``services.health_probes`` — an iterable of ``(name, callable)`` where
   ``callable()`` returns a :class:`SiteHealth`, a
   :class:`ComponentHealth`, a ``(SiteHealth, message)`` tuple, or a
   ``dict``-ish object with those fields. May be:
     * an attribute holding such an iterable, **or**
     * a **zero-arg method returning such an iterable**.
   The router tries the method form first if the callable is a bound
   method without positional-only required parameters — the return
   value is inspected; if it is a valid probe collection, it is used;
   otherwise the callable itself is treated as a single probe.
2. ``services.health_check_components()`` (async, returns list of names)
   **and** ``services.health_check(name)`` (async, returns a status) —
   the split form.
3. Neither present → readiness runs with zero probes and always
   reports ``ready=true`` (a valid configuration for library-only
   deployments with no external dependencies).

Adding a probe is a single entry in the composition root. This router
never touches that logic.

Failure isolation
=================

Every probe runs inside its own isolated awaitable:

* **Sync probes** execute in a thread via ``asyncio.to_thread`` so that
  a blocking dependency (synchronous socket, disk I/O, DNS) cannot
  stall the event loop — the timeout is enforced by the outer
  ``wait_for`` and the probe coroutine is cancellable.
* **Async probes** are wrapped in ``asyncio.wait_for``. A probe that
  ignores cancellation is still bounded by the outer ``wait_for``.
* **Timeout** → :attr:`SiteHealth.DOWN` with a ``"Probe timed out"``
  message and the elapsed latency.
* **Exception** → :attr:`SiteHealth.DOWN` with the exception class name.

Probes run **in parallel** via ``asyncio.gather(return_exceptions=True)``,
so the readiness endpoint cost is ``O(max_timeout)`` and not
``O(N × timeout)``. A single hanging probe cannot stall the rest.

Overall status computation
==========================

:func:`_compute_overall_status` maps the individual probe statuses to a
single :class:`HealthResponse.status` value:

* Any ``DOWN`` → ``"down"``.
* Otherwise any ``DEGRADED`` (or ``UNKNOWN``) → ``"degraded"``.
* Otherwise → ``"healthy"``.

Readiness reports ``ready=true`` when every probe is ``HEALTHY`` (or
when ``?strict=false`` and no probe is ``DOWN``). ``?strict=true``
tightens this: any ``DEGRADED`` also makes the pod not-ready.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from loguru import logger as _logger

from nexusdl.core.models.enums import SiteHealth

from ..dependencies import ServicesDep
from ..schemas.responses import (
    API_VERSION,
    ComponentHealth,
    ErrorCode,
    HealthResponse,
    ReadinessResponse,
    VersionResponse,
    error_response,
)

_log = _logger.bind(component="web.routers.health")

router = APIRouter(tags=["health"])

#: Capture the process start time at module import. This is the closest
#: proxy to "when did this worker start serving" that does not require
#: cooperation from the host. Hosts that expose a more accurate
#: ``services.started_at`` get it preferred by :func:`_uptime_seconds`.
_PROCESS_STARTED_AT: datetime = datetime.now(timezone.utc)

#: Default per-probe timeout in seconds. Overridable per-request.
_DEFAULT_PROBE_TIMEOUT_S: float = 2.0

#: Bounds for ``?timeout=`` — protects against a client accidentally
#: setting an 8-hour timeout that would hang the k8s probe until its own
#: kill deadline.
_MIN_PROBE_TIMEOUT_S: float = 0.1
_MAX_PROBE_TIMEOUT_S: float = 30.0

#: Short names recognised by ``_normalize_health`` (case-insensitive).
_HEALTH_ALIASES: dict[str, SiteHealth] = {
    "healthy": SiteHealth.HEALTHY,
    "ok": SiteHealth.HEALTHY,
    "up": SiteHealth.HEALTHY,
    "degraded": SiteHealth.DEGRADED,
    "warn": SiteHealth.DEGRADED,
    "warning": SiteHealth.DEGRADED,
    "down": SiteHealth.DOWN,
    "fail": SiteHealth.DOWN,
    "error": SiteHealth.DOWN,
    "unknown": SiteHealth.UNKNOWN,
}


# ===========================================================================
# Liveness endpoints
# ===========================================================================

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Return 200 as long as the process can respond. **Never** "
        "checks external dependencies — an outage of Redis, the DB, or "
        "any parser must not cause a pod restart. Use `/health/ready` "
        "for dependency checks.\n\n"
        "This endpoint is **unauthenticated** so that infrastructure "
        "probes (Kubernetes, Docker, systemd, load balancers) can call "
        "it without a JWT."
    ),
    include_in_schema=True,
)
@router.get(
    "/health/live",
    response_model=HealthResponse,
    summary="Liveness probe (Kubernetes alias)",
    include_in_schema=False,  # `/health` is the canonical doc entry
)
async def liveness(services: ServicesDep) -> HealthResponse:
    """Return liveness status. Always succeeds if the process is alive."""
    return HealthResponse(
        status="healthy",
        version=_app_version(services),
        api_version=API_VERSION,
        uptime_seconds=_uptime_seconds(services),
        timestamp=datetime.now(timezone.utc),
    )


# ===========================================================================
# Readiness endpoint
# ===========================================================================

@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description=(
        "Return 200 when all dependencies are operational, 503 when at "
        "least one is not. The body always carries per-component detail "
        "so the caller can pinpoint the failure.\n\n"
        "* `?strict=true`: also fail on `degraded` (default: only `down`).\n"
        "* `?component=name`: check a single component (debugging aid).\n"
        "* `?timeout=N`: per-probe timeout in seconds (0.1-30.0, default 2.0).\n\n"
        "This endpoint is **unauthenticated** so that infrastructure "
        "probes (Kubernetes, Docker, systemd) can reach it."
    ),
    responses={
        status.HTTP_200_OK: {"description": "All dependencies are healthy."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "At least one dependency is down (or degraded "
                           "in strict mode).",
        },
    },
)
async def readiness(
    services: ServicesDep,
    strict: Annotated[
        bool,
        Query(
            description="When `true`, a `degraded` component makes the "
                        "pod not-ready. Default: only `down` does."
        ),
    ] = False,
    component: Annotated[
        str | None,
        Query(
            description="Restrict the check to a single probe name. Useful "
                        "for targeted debugging and per-component dashboards."
        ),
    ] = None,
    timeout: Annotated[
        float,
        Query(
            ge=_MIN_PROBE_TIMEOUT_S,
            le=_MAX_PROBE_TIMEOUT_S,
            description="Per-probe timeout in seconds.",
        ),
    ] = _DEFAULT_PROBE_TIMEOUT_S,
) -> JSONResponse:
    probes = await _discover_probes(services)

    if component is not None:
        probes = [p for p in probes if p[0] == component]
        if not probes:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response(
                    ErrorCode.NOT_FOUND,
                    f"No health probe named {component!r}.",
                    details={
                        "hint": "Call `GET /health/ready` without "
                                "`?component=` to list all probes."
                    },
                ).model_dump(),
            )

    components = await _run_probes(probes, timeout=timeout)
    ready = _compute_ready(components, strict=strict)

    body = ReadinessResponse(
        ready=ready,
        components=components,
        timestamp=datetime.now(timezone.utc),
    )

    if ready:
        return JSONResponse(
            content=body.model_dump(mode="json"),
            status_code=status.HTTP_200_OK,
        )

    _log.debug(
        "Readiness failed: strict={} down={} degraded={}",
        strict,
        sum(1 for c in components if c.status == SiteHealth.DOWN),
        sum(1 for c in components if c.status == SiteHealth.DEGRADED),
    )

    return JSONResponse(
        content=body.model_dump(mode="json"),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


# ===========================================================================
# Version endpoint
# ===========================================================================

@router.get(
    "/api/version",
    response_model=VersionResponse,
    summary="Version and build metadata",
    description=(
        "Return the running application's version, the HTTP API contract "
        "version, the Python version, and (when available) the git commit "
        "and build date.\n\n"
        "This endpoint is **unauthenticated** — useful for 'what's running "
        "here?' diagnostics without a token."
    ),
)
async def version(services: ServicesDep) -> VersionResponse:
    return VersionResponse(
        version=_app_version(services),
        api_version=API_VERSION,
        python_version=sys.version.split()[0],
        git_commit=_git_commit(services),
        build_date=_build_date(services),
    )


# ===========================================================================
# Probe discovery
# ===========================================================================

#: Shape of one probe: ``(name, callable)`` where ``callable()`` may be
#: sync or async and returns any shape :func:`_normalize_health` accepts.
Probe = tuple[str, Callable[[], Any]]


async def _discover_probes(services: ServicesDep) -> list[Probe]:
    """Return the probes declared by the host, in order.

    Discovery precedence:

    1. ``services.health_probes`` — attribute, iterable, **or** method
       returning an iterable of ``(name, callable)``. A method is
       invoked (awaited if async); its return value is inspected to
       decide whether it is a probe collection (iterable of
       ``(name, callable)``) or a single probe result. If it is the
       former, the collection is used; if the latter, the callable
       itself is treated as a single probe.
    2. ``services.health_check_components`` + ``services.health_check`` —
       split form (list of names + per-name checker). Both are awaited
       when async.
    3. Nothing → empty list, meaning "no dependencies to check".

    Every accessor is called defensively; a broken probe declaration
    never prevents the readiness endpoint from responding.
    """
    probes: list[Probe] = []

    # 1) ``health_probes`` attribute or method.
    raw = getattr(services, "health_probes", None)
    if callable(raw):
        # Try the method form. If it takes required positional args,
        # it is not a "list my probes" method — treat it as a probe.
        if _callable_takes_no_required_args(raw):
            try:
                result = raw()
                if inspect.isawaitable(result):
                    result = await result
                # If the return value looks like a probe collection,
                # use it. Otherwise treat ``raw`` as a single probe.
                coerced = _try_coerce_probes(result)
                if coerced is not None:
                    probes.extend(coerced)
                    if probes:
                        return probes
                # Fall through: ``raw`` is a single probe.
                probes.append(("health_probes", raw))
            except Exception:
                _log.exception("services.health_probes() raised")
        else:
            probes.append(("health_probes", raw))

    if probes:
        return probes

    # Also handle the attribute form (not callable).
    if raw is not None and not callable(raw):
        coerced = _try_coerce_probes(raw)
        if coerced is not None:
            probes.extend(coerced)
            if probes:
                return probes

    # 2) Split form.
    names_source = getattr(services, "health_check_components", None)
    checker = getattr(services, "health_check", None)
    if callable(names_source) and callable(checker):
        try:
            names = names_source()
            if inspect.isawaitable(names):
                names = await names
            for name in names or ():
                probes.append(
                    (str(name), _make_probe_callable(checker, str(name)))
                )
        except Exception:
            _log.exception("Failed to build probes from split form")

    return probes


def _callable_takes_no_required_args(fn: Callable[..., Any]) -> bool:
    """Return whether ``fn`` can be called with zero positional args."""
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        # Cannot introspect — assume it can be called.
        return True
    for p in sig.parameters.values():
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.KEYWORD_ONLY,
        ) and p.default is inspect.Parameter.empty:
            return False
    return True


def _try_coerce_probes(raw: Any) -> list[Probe] | None:
    """Try to interpret ``raw`` as an iterable of ``(name, callable)`` pairs.

    Returns the list of probes on success, ``None`` if the shape does not
    look like a probe collection. Never raises.
    """
    if raw is None:
        return None
    # Mapping form ``{name: callable}``.
    if isinstance(raw, Mapping):
        items: Iterable[tuple[Any, Any]] = list(raw.items())
    else:
        try:
            items = list(raw)
        except TypeError:
            return None

    # Empty iterable — assume it is a valid (empty) probe list.
    if not items:
        return []

    out: list[Probe] = []
    for item in items:
        if isinstance(item, tuple) and len(item) == 2:
            name, fn = item
            if callable(fn):
                out.append((str(name), fn))
                continue
            return None
        # Object form with `.name` / `.check` or `.probe`.
        name = getattr(item, "name", None)
        check = getattr(item, "check", None) or getattr(item, "probe", None)
        if name and callable(check):
            out.append((str(name), check))
            continue
        # Anything else — not a probe shape.
        return None
    return out


def _make_probe_callable(
    checker: Callable[..., Any], name: str
) -> Callable[[], Any]:
    """Wrap ``checker(name)`` so it can be awaited by :func:`_run_probes`."""
    def _probe() -> Any:
        return checker(name)
    return _probe


# ===========================================================================
# Probe execution
# ===========================================================================

async def _run_probes(
    probes: list[Probe], *, timeout: float
) -> list[ComponentHealth]:
    """Run all probes in parallel with a per-probe timeout.

    Never raises: every failure mode (timeout, exception, bad shape) is
    reflected in the returned :class:`ComponentHealth` for that probe.
    """
    if not probes:
        return []

    tasks = [
        _run_single_probe(name, fn, timeout=timeout)
        for name, fn in probes
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Pair results with probes by length (do not rely on ``zip(strict=)``
    # which is 3.10+ only; the project declares 3.12 as its main target
    # but the code should work down to 3.9 if ``requires-python`` allows).
    out: list[ComponentHealth] = []
    for idx, (name, _fn) in enumerate(probes):
        result = results[idx] if idx < len(results) else None
        if isinstance(result, ComponentHealth):
            out.append(result)
        else:
            # ``gather`` isolated the exception; surface it as DOWN.
            # Loguru does not take ``exc_info`` as a kwarg — use
            # ``_log.opt(exception=...)`` instead.
            if isinstance(result, BaseException):
                _log.opt(exception=result).error(
                    "Health probe {!r} raised outside _run_single_probe",
                    name,
                )
            else:
                _log.error(
                    "Health probe {!r} returned unexpected result: {!r}",
                    name,
                    result,
                )
            out.append(
                ComponentHealth(
                    name=name,
                    status=SiteHealth.DOWN,
                    message="Probe execution failed.",
                )
            )
    return out


async def _run_single_probe(
    name: str, fn: Callable[[], Any], *, timeout: float
) -> ComponentHealth:
    """Execute one probe with timeout and normalization.

    Sync probes are dispatched via ``asyncio.to_thread`` so a blocking
    dependency (socket connect, disk I/O, DNS) cannot stall the event
    loop. Async probes are awaited in place. Both are wrapped in an
    outer ``wait_for`` so the timeout is enforced end-to-end.
    """
    started = time.monotonic()

    async def _invoke() -> Any:
        result = fn()
        if inspect.isawaitable(result):
            # Async probe — return the coroutine so ``wait_for`` can
            # cancel it if needed.
            return await result
        # Sync probe — offload to a thread so we do not block the loop
        # and so ``wait_for`` can actually interrupt a blocked call.
        # ``asyncio.to_thread`` is available on 3.9+.
        return await asyncio.to_thread(_call_probe_safely, fn, result)

    try:
        value = await asyncio.wait_for(_invoke(), timeout=timeout)
    except asyncio.TimeoutError:
        latency = (time.monotonic() - started) * 1000.0
        return ComponentHealth(
            name=name,
            status=SiteHealth.DOWN,
            message=f"Probe timed out after {timeout:.1f}s.",
            latency_ms=latency,
        )
    except asyncio.CancelledError:
        # Propagate cancellation — server shutdown path.
        raise
    except Exception as exc:
        latency = (time.monotonic() - started) * 1000.0
        _log.opt(exception=exc).error("Health probe {!r} raised", name)
        return ComponentHealth(
            name=name,
            status=SiteHealth.DOWN,
            message=f"Probe raised {type(exc).__name__}.",
            latency_ms=latency,
        )

    latency = (time.monotonic() - started) * 1000.0
    status_value, message = _normalize_health(value)
    return ComponentHealth(
        name=name,
        status=status_value,
        message=message,
        latency_ms=latency,
    )


def _call_probe_safely(fn: Callable[[], Any], _unused: Any = None) -> Any:
    """Call ``fn()`` in a worker thread.

    The ``_unused`` parameter exists so ``asyncio.to_thread`` does not
    accidentally re-invoke ``fn`` with the result of the original call
    (the original ``fn()`` in ``_invoke`` is executed only for the
    sync-vs-async branch decision, not for its return value — but
    calling it twice would be a bug for probes with side effects).
    """
    return fn()


# ===========================================================================
# Probe result normalization
# ===========================================================================

def _normalize_health(value: Any) -> tuple[SiteHealth, str | None]:
    """Normalize any accepted probe return shape to ``(status, message)``.

    Accepted shapes:

    * :class:`SiteHealth` member → itself, ``None`` message.
    * :class:`ComponentHealth` → its status and message.
    * ``str`` → matched against :data:`_HEALTH_ALIASES`; unrecognized
      strings are surfaced in the message for debugging.
    * ``tuple(status, message)`` → both used.
    * ``dict`` with ``status`` / ``message`` keys.
    * ``None`` → :attr:`SiteHealth.UNKNOWN` with an explanatory message.
    * Anything else → :attr:`SiteHealth.UNKNOWN` with a shape warning.
    """
    if isinstance(value, ComponentHealth):
        return value.status, value.message

    if isinstance(value, SiteHealth):
        return value, None

    if isinstance(value, str):
        status_value = _status_from_string(value)
        if status_value is SiteHealth.UNKNOWN and value.strip():
            # Do not lose the raw string — an operator debugging a
            # readiness failure needs to know what the probe actually
            # returned to understand why it was not recognized.
            return status_value, f"Unrecognized status string: {value!r}"
        return status_value, None

    if isinstance(value, tuple) and len(value) >= 1:
        status_value = _status_from_any(value[0])
        message = str(value[1]) if len(value) > 1 and value[1] else None
        return status_value, message

    if isinstance(value, Mapping):
        status_value = _status_from_any(value.get("status"))
        message_raw = value.get("message")
        message = str(message_raw) if message_raw else None
        return status_value, message

    if value is None:
        return SiteHealth.UNKNOWN, "Probe returned no status."

    _log.debug("Probe returned unexpected shape: {}", type(value).__name__)
    return SiteHealth.UNKNOWN, (
        f"Probe returned unexpected value of type {type(value).__name__}."
    )


def _status_from_any(value: Any) -> SiteHealth:
    """Coerce ``value`` to a :class:`SiteHealth` or ``UNKNOWN``."""
    if isinstance(value, SiteHealth):
        return value
    if isinstance(value, str):
        return _status_from_string(value)
    return SiteHealth.UNKNOWN


def _status_from_string(value: str) -> SiteHealth:
    return _HEALTH_ALIASES.get(value.strip().lower(), SiteHealth.UNKNOWN)


# ===========================================================================
# Status computation
# ===========================================================================

def _compute_ready(
    components: list[ComponentHealth], *, strict: bool
) -> bool:
    """Return whether the pod is ready to serve traffic.

    * Non-strict: not ready iff any component is ``DOWN``.
    * Strict: not ready iff any component is ``DOWN`` **or** ``DEGRADED``.
      ``UNKNOWN`` alone never fails readiness (a component that has
      never been probed is not necessarily unhealthy).
    * Empty component list: ready (nothing to check).
    """
    if not components:
        return True
    for component in components:
        if component.status == SiteHealth.DOWN:
            return False
        if strict and component.status == SiteHealth.DEGRADED:
            return False
    return True


# ===========================================================================
# Version / uptime helpers
# ===========================================================================

def _app_version(services: ServicesDep) -> str:
    """Return the running application version.

    Priority: ``services.version`` (attribute or zero-arg method) →
    ``importlib.metadata.version("nexusdl")`` → package fallback → safe
    default.
    """
    try:
        provided = getattr(services, "version", None)
        if callable(provided):
            try:
                provided = provided()
            except Exception:
                provided = None
        if isinstance(provided, str) and provided:
            return provided
    except Exception:
        _log.debug("services.version accessor raised")

    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415
        return _pkg_version("nexusdl")
    except Exception:
        pass

    try:
        from nexusdl.version import __version__ as _v  # noqa: PLC0415
        return str(_v)
    except Exception:
        return "0.0.0+unknown"


def _uptime_seconds(services: ServicesDep) -> float:
    """Return the process uptime in seconds.

    Priority: ``services.started_at`` (datetime) → module-level
    :data:`_PROCESS_STARTED_AT`. Never raises.
    """
    try:
        started_at = getattr(services, "started_at", None)
        if isinstance(started_at, datetime):
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - started_at
            return max(0.0, delta.total_seconds())
    except Exception:
        _log.debug("services.started_at accessor raised")

    delta = datetime.now(timezone.utc) - _PROCESS_STARTED_AT
    return max(0.0, delta.total_seconds())


def _git_commit(services: ServicesDep) -> str | None:
    """Return the git commit SHA if exposed, else ``None``.

    Priority: ``services.git_commit`` → env ``NEXUSDL_GIT_COMMIT`` →
    ``GIT_COMMIT`` → ``None``. Values longer than 40 chars are truncated
    (full SHAs are hex; anything longer is garbage).
    """
    try:
        attr = getattr(services, "git_commit", None)
        if callable(attr):
            try:
                attr = attr()
            except Exception:
                attr = None
        if isinstance(attr, str) and attr.strip():
            return attr.strip()[:40]
    except Exception:
        pass
    for env_name in ("NEXUSDL_GIT_COMMIT", "GIT_COMMIT"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()[:40]
    return None


def _build_date(services: ServicesDep) -> datetime | None:
    """Return the build date if exposed, else ``None``.

    Priority: ``services.build_date`` (datetime or ISO string) → env
    ``NEXUSDL_BUILD_DATE`` (ISO string). Invalid values become ``None``.
    """
    candidates: list[Any] = []
    try:
        candidates.append(getattr(services, "build_date", None))
    except Exception:
        pass
    candidates.append(os.environ.get("NEXUSDL_BUILD_DATE"))

    for value in candidates:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] New endpoints do **not** require authentication. Every probe path
#     must remain public, otherwise infrastructure probes fail.
# [ ] Liveness (`/health`) does not call any probe. If you need a
#     dependency-aware liveness, add a separate endpoint with a clear
#     name (`/health/deep`) — never change `/health`'s semantics.
# [ ] New probe shapes are handled in :func:`_normalize_health` and
#     documented in the module docstring.
# [ ] New discovery mechanisms go through :func:`_discover_probes` and
#     never raise from a broken host declaration.
# [ ] Sync probes **must** be dispatched via ``asyncio.to_thread`` in
#     :func:`_run_single_probe` — do not call them directly on the
#     event loop, or a blocking probe will hang the whole pod.
# [ ] Anything that could hang is wrapped by an outer ``wait_for``.
#     Never introduce a probe that bypasses it.
# [ ] New version fields are added to ``VersionResponse`` first, then
#     populated here — not the other way around.
