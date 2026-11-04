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
   ``dict``-ish object with those fields. May be an attribute or a
   zero-arg method returning the iterable.
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

Every probe runs inside ``asyncio.wait_for(probe, timeout=…)``:

* Timeout → :attr:`SiteHealth.DOWN` with a ``"Probe timed out"`` message.
* Exception → :attr:`SiteHealth.DOWN` with the exception class name.
* Otherwise → the value the probe returned, normalized defensively.

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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
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
) -> Response:
    probes = _discover_probes(services)

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
        return Response(
            content=body.model_dump_json(),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )

    _log.debug(
        "Readiness failed: strict={} down={} degraded={}",
        strict,
        sum(1 for c in components if c.status == SiteHealth.DOWN),
        sum(1 for c in components if c.status == SiteHealth.DEGRADED),
    )

    return Response(
        content=body.model_dump_json(),
        media_type="application/json",
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


def _discover_probes(services: ServicesDep) -> list[Probe]:
    """Return the probes declared by the host, in order.

    Discovery precedence:

    1. ``services.health_probes`` — attribute or zero-arg method returning
       an iterable of ``(name, callable)``.
    2. ``services.health_check_components`` + ``services.health_check`` —
       split form (list of names + per-name checker).
    3. Nothing → empty list, meaning "no dependencies to check".

    Every accessor is called defensively; a broken probe declaration
    never prevents the readiness endpoint from responding.
    """
    probes: list[Probe] = []

    # 1) ``health_probes`` attribute or method.
    raw = getattr(services, "health_probes", None)
    if callable(raw) and not _looks_like_probe(raw):
        try:
            raw = raw()
        except Exception:
            _log.exception("services.health_probes() raised")
            raw = None
    if raw is not None:
        try:
            for item in _coerce_probe_iterable(raw):
                name = str(item[0])
                fn = item[1]
                if callable(fn):
                    probes.append((name, fn))
                else:
                    _log.warning(
                        "Skipping probe {!r}: not callable", name
                    )
        except Exception:
            _log.exception("Failed to iterate services.health_probes")

    if probes:
        return probes

    # 2) Split form.
    names_source = getattr(services, "health_check_components", None)
    checker = getattr(services, "health_check", None)
    if callable(names_source) and callable(checker):
        try:
            names = names_source()
            if inspect.isawaitable(names):
                # Cannot await here (sync helper). The caller should
                # provide ``health_probes`` if async discovery is needed.
                _log.debug(
                    "health_check_components returned an awaitable; "
                    "use `health_probes` for async discovery."
                )
                names = None
            for name in names or ():
                probes.append(
                    (str(name), _make_probe_callable(checker, str(name)))
                )
        except Exception:
            _log.exception("Failed to build probes from split form")

    return probes


def _make_probe_callable(
    checker: Callable[..., Any], name: str
) -> Callable[[], Any]:
    """Wrap ``checker(name)`` so it can be awaited by :func:`_run_probes`."""
    def _probe() -> Any:
        return checker(name)
    return _probe


def _coerce_probe_iterable(raw: Any) -> Iterable[tuple[Any, Any]]:
    """Best-effort coercion of a probe collection to ``(name, callable)`` pairs."""
    if raw is None:
        return []
    # Mapping form ``{name: callable}``.
    if isinstance(raw, Mapping):
        return list(raw.items())
    # Sequence of tuples / objects with `.name` / `.check`.
    out: list[tuple[Any, Any]] = []
    for item in raw:
        if isinstance(item, tuple) and len(item) == 2:
            out.append(item)
            continue
        name = getattr(item, "name", None)
        check = getattr(item, "check", None) or getattr(item, "probe", None)
        if name and callable(check):
            out.append((name, check))
    return out


def _looks_like_probe(fn: Callable[..., Any]) -> bool:
    """Return whether ``fn`` looks like an individual probe (0 params)."""
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    return not any(
        p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        and p.default is inspect.Parameter.empty
        for p in sig.parameters.values()
    )


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

    out: list[ComponentHealth] = []
    for (name, _fn), result in zip(probes, results, strict=True):
        if isinstance(result, ComponentHealth):
            out.append(result)
        else:
            # ``gather`` isolated the exception; surface it as DOWN.
            _log.exception(
                "Health probe {!r} raised outside _run_single_probe", name,
                exc_info=result if isinstance(result, BaseException) else None,
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
    """Execute one probe with timeout and normalization."""
    started = _monotonic_ms()
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=timeout)
        latency = _monotonic_ms() - started
        status_value, message = _normalize_health(result)
        return ComponentHealth(
            name=name,
            status=status_value,
            message=message,
            latency_ms=latency,
        )
    except asyncio.TimeoutError:
        return ComponentHealth(
            name=name,
            status=SiteHealth.DOWN,
            message=f"Probe timed out after {timeout:.1f}s.",
            latency_ms=timeout * 1000.0,
        )
    except Exception as exc:
        latency = _monotonic_ms() - started
        _log.exception("Health probe {!r} raised", name)
        return ComponentHealth(
            name=name,
            status=SiteHealth.DOWN,
            message=f"Probe raised {type(exc).__name__}.",
            latency_ms=latency,
        )


# ===========================================================================
# Probe result normalization
# ===========================================================================

def _normalize_health(value: Any) -> tuple[SiteHealth, str | None]:
    """Normalize any accepted probe return shape to ``(status, message)``.

    Accepted shapes:

    * :class:`SiteHealth` member → itself, ``None`` message.
    * :class:`ComponentHealth` → its status and message.
    * ``str`` → matched against :data:`_HEALTH_ALIASES`.
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
        return _status_from_string(value), None

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
# Misc
# ===========================================================================

def _monotonic_ms() -> float:
    """Monotonic milliseconds — safe against NTP adjustments."""
    import time  # noqa: PLC0415 — lazy, tiny

    return time.monotonic() * 1000.0


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
# [ ] Anything that could hang is wrapped by :func:`_run_single_probe`'s
#     ``wait_for``. Never introduce a probe that bypasses it.
# [ ] New version fields are added to ``VersionResponse`` first, then
#     populated here — not the other way around.
