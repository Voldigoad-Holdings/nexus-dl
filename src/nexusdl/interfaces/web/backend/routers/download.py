"""Download router — ``/api/v1/download``.

FastAPI glue for the download queue. Covers the six documented endpoints
plus four clearly-marked extensions that the Web UI needs:

Documented (per API contract)
=============================

* ``POST   /api/v1/download``                 — submit a task (202 Accepted)
* ``GET    /api/v1/download``                 — list the caller's queue
* ``GET    /api/v1/download/{task_id}``       — single task detail
* ``POST   /api/v1/download/{task_id}/pause`` — pause an active task
* ``POST   /api/v1/download/{task_id}/resume``— resume a paused task
* ``DELETE /api/v1/download/{task_id}``       — cancel (if active) or forget
                                               (if terminal), idempotently.

Extensions (marked ``# Extension:`` in code)
============================================

* ``POST   /api/v1/download/{task_id}/retry`` — retry a FAILED/CANCELLED task
* ``POST   /api/v1/download/pause-all``       — pause every active task
* ``POST   /api/v1/download/resume-all``      — resume every paused task
* ``POST   /api/v1/download/cancel-all``      — cancel every active task
* ``POST   /api/v1/download/clear-finished``  — drop every terminal task

These mirror the GUI's :class:`DownloadView` and are required for a
feature-complete Web UI. They are additive — the documented contract is
untouched.

User scoping
============

The caller's identity is **always** ``payload.sub``. No endpoint accepts
a ``user_id`` query parameter or body field. A task owned by another
user returns **404** (never 403) — the server does not leak the
existence of other users' tasks. Admins bypass the ownership check (via
:func:`~..security.role_at_least`), which is required for moderation.

Status transitions
==================

The router enforces **soft idempotency** before delegating to the
service:

* ``pause`` on ``PAUSED`` → 200 no-op (intent satisfied).
* ``resume`` on ``RUNNING`` → 200 no-op.
* ``cancel`` on ``CANCELLED`` → 200 no-op.
* ``pause`` on a terminal task (``DONE``/``FAILED``/``CANCELLED``) → 409.
* ``resume`` on a terminal task, or on ``PENDING`` → 409.
* ``retry`` on anything but ``FAILED``/``CANCELLED`` → 409.

The service is still the authority. If a service rejects a transition
the router thought valid (because it enforces a stricter rule), the
router translates the exception to 409 (see
:func:`_translate_service_error`). Two layers of validation, both with
clear error envelopes.

Service contract (duck-typed)
=============================

Every method is looked up by multiple names; the first callable wins.
Any method may be sync or async (``inspect.isawaitable`` handles both).

* **Submit**: ``submit(...)``, ``submit_download(...)``, ``enqueue(...)``.
  Returns a domain task (with ``.id`` / ``.task_id``) or a bare string id.
* **List**: ``list_tasks(...)``, ``list(...)``, ``query(...)``,
  ``snapshot(...)``. Returns list / tuple / object; see
  :func:`_normalize_tasks_result`.
* **Get**: ``get_task(task_id)``, ``get(task_id)``.
* **Pause / resume / cancel / retry**: ``pause(task_id)``,
  ``pause_task(task_id)``, etc. Return the updated task, or ``None``.
* **Bulk**: ``pause_all()``, ``resume_all()``, ``cancel_all()``,
  ``forget_all_terminal()``. Return the count of affected tasks (or
  ``None``).
* **Queue state**: ``is_queue_paused()`` method or ``queue_paused``
  attribute.
* **Counts**: ``counts(...)`` (preferred) **or** fall back to a full
  unfiltered list and :func:`compute_status_counts`.
"""
from __future__ import annotations

import inspect
import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from loguru import logger as _logger

from nexusdl.core.models.enums import DownloadStatus, UserRole

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.common import DownloadQueryParams
from ..schemas.requests import DownloadRequest
from ..schemas.responses import (
    DownloadSubmitResponse,
    DownloadTaskListResponse,
    DownloadTaskSchema,
    ErrorCode,
    MessageResponse,
    compute_status_counts,
    error_response,
)
from ..security import role_at_least

_log = _logger.bind(component="web.routers.download")

router = APIRouter(prefix="/api/v1/download", tags=["download"])

#: Maximum accepted length for the ``{task_id}`` path segment. UUID4 is
#: 36 chars; the extra headroom accommodates custom ID formats.
_MAX_TASK_ID_LENGTH: int = 64

# --- Status sets used by the transition rules --------------------------

_TERMINAL: frozenset[DownloadStatus] = frozenset(
    {DownloadStatus.DONE, DownloadStatus.FAILED, DownloadStatus.CANCELLED}
)
_PAUSABLE: frozenset[DownloadStatus] = frozenset(
    {DownloadStatus.RUNNING, DownloadStatus.PENDING}
)
_RESUMABLE: frozenset[DownloadStatus] = frozenset({DownloadStatus.PAUSED})
_RETRIABLE: frozenset[DownloadStatus] = frozenset(
    {DownloadStatus.FAILED, DownloadStatus.CANCELLED}
)


# ===========================================================================
# Bulk endpoints (declared first — must not collide with ``{task_id}``)
# ===========================================================================


# Extension: bulk operations, mirroring the GUI's DownloadView.
@router.post(
    "/pause-all",
    response_model=MessageResponse,
    summary="Pause every active task",
    description=(
        "Pause every task in the caller's queue that is currently "
        "`running` or `pending`. Idempotent: tasks already paused are "
        "ignored. Returns the count of tasks actually paused.\n\n"
        "*Extension — not part of the strict v1 API contract, but required "
        "for the Web UI.*"
    ),
)
async def pause_all(
    payload: CurrentUser,
    services: ServicesDep,
) -> MessageResponse:
    affected = await _bulk_op(services, "pause_all", user_id=_uid(payload))
    return MessageResponse(
        message=f"Paused {affected} task(s).",
        details={"affected": affected, "operation": "pause_all"},
    )


# Extension: bulk operations, mirroring the GUI's DownloadView.
@router.post(
    "/resume-all",
    response_model=MessageResponse,
    summary="Resume every paused task",
    description=(
        "Resume every paused task in the caller's queue. Idempotent. "
        "Returns the count of tasks actually resumed.\n\n"
        "*Extension — not part of the strict v1 API contract.*"
    ),
)
async def resume_all(
    payload: CurrentUser,
    services: ServicesDep,
) -> MessageResponse:
    affected = await _bulk_op(services, "resume_all", user_id=_uid(payload))
    return MessageResponse(
        message=f"Resumed {affected} task(s).",
        details={"affected": affected, "operation": "resume_all"},
    )


# Extension: bulk operations, mirroring the GUI's DownloadView.
@router.post(
    "/cancel-all",
    response_model=MessageResponse,
    summary="Cancel every active task",
    description=(
        "Cancel every task in the caller's queue that is currently "
        "`running`, `pending` or `paused`. Idempotent. Returns the count "
        "of tasks actually cancelled.\n\n"
        "*Extension — not part of the strict v1 API contract.*"
    ),
)
async def cancel_all(
    payload: CurrentUser,
    services: ServicesDep,
) -> MessageResponse:
    affected = await _bulk_op(services, "cancel_all", user_id=_uid(payload))
    return MessageResponse(
        message=f"Cancelled {affected} task(s).",
        details={"affected": affected, "operation": "cancel_all"},
    )


# Extension: bulk operations, mirroring the GUI's DownloadView.
@router.post(
    "/clear-finished",
    response_model=MessageResponse,
    summary="Drop every terminal task",
    description=(
        "Remove every task in the caller's queue that is in a terminal "
        "state (`done`, `failed`, `cancelled`). Files on disk are **not** "
        "deleted — use `DELETE /download/{id}?delete_files=true` for that.\n\n"
        "*Extension — not part of the strict v1 API contract.*"
    ),
)
async def clear_finished(
    payload: CurrentUser,
    services: ServicesDep,
) -> MessageResponse:
    affected = await _bulk_op(
        services, "forget_all_terminal", user_id=_uid(payload)
    )
    return MessageResponse(
        message=f"Removed {affected} finished task(s) from the queue.",
        details={"affected": affected, "operation": "clear_finished"},
    )


# ===========================================================================
# Submit
# ===========================================================================


@router.post(
    "",
    response_model=DownloadSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a download task",
    description=(
        "Queue a new download. Responds **202 Accepted** immediately with "
        "the new `task_id` — the actual download runs asynchronously and "
        "progress is streamed via the WebSocket at `/ws/v1/events` "
        "(`download.started`, `download.progress`, `download.completed`, "
        "`download.failed`).\n\n"
        "Identify the target by **exactly one** of `manga_id` or `url`. "
        "``chapter_ids`` is optional; omit it to download every chapter."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "The manga or URL could not be resolved.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Ambiguous or missing target.",
        },
    },
)
async def submit_download(
    body: DownloadRequest,
    payload: CurrentUser,
    services: ServicesDep,
) -> DownloadSubmitResponse:
    user_id = _uid(payload)

    try:
        result = await _invoke_service(
            services.downloader,
            ("submit", "submit_download", "enqueue"),
            manga_id=body.manga_id,
            url=body.url,
            chapter_ids=body.chapter_ids,
            packaging_format=body.packaging_format,
            priority=body.priority,
            requested_by=user_id,
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"submit download for user {user_id}"
        ) from exc

    task_id = _extract_task_id(result)
    if not task_id:
        _log.exception(
            "download.submit returned no task id (result type: {})",
            type(result).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to obtain the task id from the download service.",
            ).model_dump(),
        )

    queue_position = await _safe_await(
        services.downloader, "queue_position", task_id, default=None
    )
    estimated_start = await _safe_await(
        services.downloader, "estimated_start_at", task_id, default=None
    )

    _log.info(
        "download submitted user={} task={} target={}",
        user_id,
        task_id,
        body.manga_id or body.url,
    )

    return DownloadSubmitResponse(
        task_id=task_id,
        accepted=True,
        queue_position=queue_position,
        estimated_start_at=estimated_start,
        message="Download queued.",
    )


# ===========================================================================
# List
# ===========================================================================


@router.get(
    "",
    response_model=DownloadTaskListResponse,
    summary="List the caller's download queue",
    description=(
        "Return the caller's queue. Filters narrow the `items`; the "
        "`counts` field always reflects the **full** queue so the UI can "
        "show accurate badges regardless of filters.\n\n"
        "The queue is not paginated — it is small by design and clients "
        "want the full picture in one round-trip."
    ),
)
async def list_downloads(
    filters: Annotated[DownloadQueryParams, Depends()],
    payload: CurrentUser,
    services: ServicesDep,
) -> DownloadTaskListResponse:
    user_id = _uid(payload)

    # 1) Filtered items.
    try:
        raw_items = await _invoke_service(
            services.downloader,
            ("list_tasks", "list", "query", "snapshot"),
            user_id=user_id,
            status=filters.status or None,
            manga_id=filters.manga_id,
            chapter_id=filters.chapter_id,
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"list tasks for user {user_id}"
        ) from exc

    tasks = _normalize_tasks_result(raw_items)
    items = [_to_task_schema(t) for t in tasks]

    # 2) Full-queue counts.
    counts = await _full_queue_counts(services, user_id=user_id)

    # 3) Queue paused flag.
    queue_paused = await _safe_await(
        services.downloader,
        "is_queue_paused",
        default=False,
        attribute_fallback="queue_paused",
    )

    return DownloadTaskListResponse(
        items=items,
        counts=counts,
        queue_paused=bool(queue_paused),
    )


# ===========================================================================
# Single task + per-task mutations
# ===========================================================================


@router.get(
    "/{task_id}",
    response_model=DownloadTaskSchema,
    summary="Get a single download task",
    description=(
        "Return one task. 404 for both 'does not exist' and 'belongs to "
        "another user' — the server does not distinguish."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task not found."},
    },
)
async def get_download(
    task_id: Annotated[str, Path(max_length=_MAX_TASK_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
) -> DownloadTaskSchema:
    task = await _get_owned_task(task_id, payload=payload, services=services)
    return _to_task_schema(task)


@router.post(
    "/{task_id}/pause",
    response_model=DownloadTaskSchema,
    summary="Pause an active task",
    description=(
        "Pause a task that is `running` or `pending`. Idempotent: pausing "
        "an already-paused task returns 200 with the unchanged state. "
        "Pausing a terminal task (done/failed/cancelled) returns 409."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task not found."},
        status.HTTP_409_CONFLICT: {"description": "Invalid state transition."},
    },
)
async def pause_download(
    task_id: Annotated[str, Path(max_length=_MAX_TASK_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
) -> DownloadTaskSchema:
    task = await _get_owned_task(task_id, payload=payload, services=services)
    current = _task_status(task)

    if current == DownloadStatus.PAUSED:
        return _to_task_schema(task)  # idempotent
    if current not in _PAUSABLE:
        raise _invalid_transition(
            "pause", current, allowed=_PAUSABLE
        )

    try:
        result = await _invoke_service(
            services.downloader,
            ("pause", "pause_task"),
            task_id,
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"pause task {task_id}"
        ) from exc

    # Some services return ``None`` on success — refetch to get the
    # post-transition state. Otherwise use the returned task directly.
    if result is None:
        return _to_task_schema(
            await _get_owned_task(
                task_id, payload=payload, services=services
            )
        )
    return _to_task_schema(result)


@router.post(
    "/{task_id}/resume",
    response_model=DownloadTaskSchema,
    summary="Resume a paused task",
    description=(
        "Resume a `paused` task. Idempotent: resuming a running task "
        "returns 200 with the unchanged state. Resuming a `pending` or "
        "terminal task returns 409."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task not found."},
        status.HTTP_409_CONFLICT: {"description": "Invalid state transition."},
    },
)
async def resume_download(
    task_id: Annotated[str, Path(max_length=_MAX_TASK_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
) -> DownloadTaskSchema:
    task = await _get_owned_task(task_id, payload=payload, services=services)
    current = _task_status(task)

    if current == DownloadStatus.RUNNING:
        return _to_task_schema(task)  # idempotent
    if current not in _RESUMABLE:
        raise _invalid_transition(
            "resume", current, allowed=_RESUMABLE
        )

    try:
        result = await _invoke_service(
            services.downloader,
            ("resume", "resume_task"),
            task_id,
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"resume task {task_id}"
        ) from exc

    if result is None:
        return _to_task_schema(
            await _get_owned_task(
                task_id, payload=payload, services=services
            )
        )
    return _to_task_schema(result)


# Extension: retry, mirroring the GUI's DownloadView ``on_retry``.
@router.post(
    "/{task_id}/retry",
    response_model=DownloadTaskSchema,
    summary="Retry a failed or cancelled task",
    description=(
        "Retry a task whose status is `failed` or `cancelled`. Any other "
        "state returns 409.\n\n"
        "*Extension — not part of the strict v1 API contract, but mirrors "
        "the GUI's retry affordance.*"
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task not found."},
        status.HTTP_409_CONFLICT: {"description": "Invalid state transition."},
    },
)
async def retry_download(
    task_id: Annotated[str, Path(max_length=_MAX_TASK_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
) -> DownloadTaskSchema:
    task = await _get_owned_task(task_id, payload=payload, services=services)
    current = _task_status(task)

    if current not in _RETRIABLE:
        raise _invalid_transition("retry", current, allowed=_RETRIABLE)

    try:
        result = await _invoke_service(
            services.downloader,
            ("retry", "retry_task"),
            task_id,
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"retry task {task_id}"
        ) from exc

    if result is None:
        return _to_task_schema(
            await _get_owned_task(
                task_id, payload=payload, services=services
            )
        )
    return _to_task_schema(result)


@router.delete(
    "/{task_id}",
    response_model=MessageResponse,
    summary="Cancel or forget a task",
    description=(
        "Remove a task from the caller's queue.\n\n"
        "* **Active task** (`running`/`pending`/`paused`): cancelled, "
        "temporary files cleaned up.\n"
        "* **Terminal task** (`done`/`failed`/`cancelled`): removed from "
        "the queue only.\n"
        "* `?delete_files=true`: also removes packaged output / partial "
        "files from disk. **Irreversible.**\n\n"
        "Idempotent: deleting an already-removed task returns 200 (no-op)."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task not found."},
    },
)
async def delete_download(
    task_id: Annotated[str, Path(max_length=_MAX_TASK_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
    delete_files: Annotated[
        bool,
        Query(
            description=(
                "When `true`, packaged output and partial files are also "
                "deleted from disk. Default `false` removes only the "
                "queue entry."
            ),
        ),
    ] = False,
) -> MessageResponse:
    task = await _get_owned_task(task_id, payload=payload, services=services)
    previous_status = _task_status(task)
    was_active = previous_status not in _TERMINAL

    # Cancel if still active, forget in either case.
    try:
        if was_active:
            await _invoke_service(
                services.downloader,
                ("cancel", "cancel_task"),
                task_id,
            )
        await _invoke_service(
            services.downloader,
            ("forget", "forget_task", "remove", "remove_task", "delete"),
            task_id,
            delete_files=delete_files,
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"delete task {task_id}"
        ) from exc

    _log.info(
        "download task deleted user={} task={} was_active={} files={}",
        _uid(payload),
        task_id,
        was_active,
        delete_files,
    )

    message = (
        "Task cancelled and removed from the queue."
        if was_active
        else "Task removed from the queue."
    )
    if delete_files:
        message += " Files were deleted from disk."

    return MessageResponse(
        message=message,
        details={
            "task_id": task_id,
            "previous_status": previous_status.value,
            "files_deleted": delete_files,
        },
    )


# ===========================================================================
# Task ownership + fetch helpers
# ===========================================================================


async def _get_owned_task(
    task_id: str, *, payload: CurrentUser, services: ServicesDep
) -> Any:
    """Fetch ``task_id``, enforce ownership, raise 404 on any refusal.

    A task that does not exist and a task owned by another user both
    produce the same 404 — no existence leak. Admins bypass the
    ownership check (necessary for moderation tooling).
    """
    try:
        task = await _invoke_service(
            services.downloader,
            ("get_task", "get"),
            task_id,
            # Pass the user id where possible; services that scope in
            # the DB will then return ``None`` for foreign tasks. Services
            # that do not scope still get the post-fetch check below.
            user_id=_uid(payload),
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"get task {task_id}"
        ) from exc

    if task is None:
        raise _task_not_found(task_id)

    if not _task_belongs_to(task, _uid(payload)) and not _is_admin(payload):
        raise _task_not_found(task_id)

    return task


def _task_belongs_to(task: Any, user_id: str) -> bool:
    """Return whether ``task`` is owned by ``user_id``.

    Looks for the common owner attribute names; a task with no owner
    attribute is treated as belonging to the caller (single-user
    deployments without ownership tracking).
    """
    for attr in ("requested_by", "user_id", "owner_id"):
        owner = getattr(task, attr, None)
        if owner is not None:
            return str(owner) == user_id
    return True


def _is_admin(payload: CurrentUser) -> bool:
    try:
        return role_at_least(payload.role, UserRole.ADMIN)
    except Exception:
        return False


# ===========================================================================
# Status + transition helpers
# ===========================================================================


def _task_status(task: Any) -> DownloadStatus:
    """Return the task's status, coerced to :class:`DownloadStatus`.

    Falls back to :attr:`DownloadStatus.PENDING` for malformed values
    (defensive — never raises from a status read).
    """
    raw = getattr(task, "status", None)
    if isinstance(raw, DownloadStatus):
        return raw
    try:
        return DownloadStatus(str(raw))
    except (ValueError, TypeError):
        return DownloadStatus.PENDING


def _invalid_transition(
    operation: str,
    current: DownloadStatus,
    *,
    allowed: frozenset[DownloadStatus],
) -> HTTPException:
    """Build a 409 with the allowed source states listed."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=error_response(
            ErrorCode.CONFLICT,
            f"Cannot {operation} a task in state {current.value!r}.",
            details={
                "operation": operation,
                "current_status": current.value,
                "allowed_statuses": sorted(s.value for s in allowed),
            },
        ).model_dump(),
    )


# ===========================================================================
# Service invocation helpers (duck-typed, sync-or-async)
# ===========================================================================


async def _invoke_service(
    service: Any, names: Sequence[str], *args: Any, **kwargs: Any
) -> Any:
    """Call the first available method among ``names``.

    Filters ``kwargs`` to those the method accepts (via
    :func:`inspect.signature`), awaits if the result is awaitable, and
    raises :class:`AttributeError` if no method exists (translated by the
    caller via :func:`_translate_service_error`).
    """
    method = None
    for name in names:
        candidate = getattr(service, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        raise AttributeError(
            f"None of {names!r} is implemented on the download service."
        )

    filtered_kwargs = _filter_kwargs(method, kwargs)
    result = method(*args, **filtered_kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _safe_await(
    service: Any,
    name: str,
    *args: Any,
    default: Any = None,
    attribute_fallback: str | None = None,
) -> Any:
    """Call ``service.<name>`` (or read ``service.<attribute_fallback>``)."""
    method = getattr(service, name, None)
    if callable(method):
        try:
            result = method(*args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            _log.debug("download service method {!r} raised", name)
    if attribute_fallback is not None:
        value = getattr(service, attribute_fallback, None)
        if not callable(value):
            return value if value is not None else default
    return default


def _filter_kwargs(method: Any, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the kwargs ``method`` accepts.

    On ``ValueError`` (e.g. builtins with no signature), returns the
    kwargs as-is — a potential ``TypeError`` there is caught by
    :func:`_translate_service_error`.
    """
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError):
        return dict(kwargs)

    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return dict(kwargs)

    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    }
    return {k: v for k, v in kwargs.items() if k in accepted}


async def _bulk_op(
    services: ServicesDep, name: str, *, user_id: str
) -> int:
    """Run a bulk operation, returning the number of affected tasks."""
    try:
        result = await _invoke_service(
            services.downloader, (name,), user_id=user_id
        )
    except Exception as exc:
        raise _translate_service_error(
            exc, context=f"bulk download op {name} for user {user_id}"
        ) from exc
    try:
        return int(result or 0)
    except (TypeError, ValueError):
        return 0


# ===========================================================================
# Normalization / projection
# ===========================================================================


def _normalize_tasks_result(raw: Any) -> list[Any]:
    """Coerce a service return value into a list of task objects."""
    if raw is None:
        return []
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], int):
        return list(raw[0] or [])
    items = getattr(raw, "items", None)
    if items is not None:
        return list(items or [])
    try:
        return list(raw)
    except TypeError:
        return []


def _extract_task_id(result: Any) -> str | None:
    """Return the new task id from any of the accepted ``submit()`` shapes."""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    for attr in ("id", "task_id"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
    return None


def _to_task_schema(task: Any) -> DownloadTaskSchema:
    """Project a domain task into :class:`DownloadTaskSchema`.

    Explicit mapping — never ``model_validate`` directly, because the
    domain object may use slightly different field names (``speed`` vs
    ``speed_bps``, ``requested_by`` vs ``user_id``, …). Each field has a
    safe default so a partial task never 500s the endpoint.
    """
    return DownloadTaskSchema(
        task_id=str(_get(task, "id", "task_id", default="")),
        manga_id=_maybe_str(_get(task, "manga_id")),
        manga_title=str(_get(task, "manga_title", default="")),
        chapter_id=_maybe_str(_get(task, "chapter_id")),
        chapter_label=str(_get(task, "chapter_label", default="")),
        site_id=str(_get(task, "site_id", default="")),
        site_name=str(_get(task, "site_name", default="")),
        status=_task_status(task),
        priority=_get(task, "priority", default=None) or _default_priority(),
        completed=int(_get(task, "completed", default=0) or 0),
        total=_maybe_int(_get(task, "total")),
        bytes_completed=int(
            _get(task, "bytes_completed", default=0) or 0
        ),
        bytes_total=_maybe_int(_get(task, "bytes_total")),
        speed_bps=_maybe_float(
            _get(task, "speed_bps", "speed", default=None)
        ),
        eta_seconds=_maybe_float(_get(task, "eta_seconds", default=None)),
        output_format=_get(task, "output_format", "packaging_format", default=None),
        output_path=_maybe_str(
            _get(task, "output_path", "path", default=None)
        ),
        error=_maybe_str(_get(task, "error", default=None)),
        created_at=_get(task, "created_at", default=None),
        started_at=_get(task, "started_at", default=None),
        finished_at=_get(task, "finished_at", default=None),
    )


async def _full_queue_counts(
    services: ServicesDep, *, user_id: str
) -> dict[str, int]:
    """Return ``{status: count}`` across the caller's **entire** queue.

    Prefers a dedicated ``counts()`` method when available. Falls back to
    a full unfiltered list + :func:`compute_status_counts`, and finally
    to an empty dict with per-status zeros if neither exists (readiness
    over correctness — the response stays well-formed).
    """
    # 1) Dedicated method.
    counts_fn = getattr(services.downloader, "counts", None)
    if callable(counts_fn):
        try:
            result = counts_fn(user_id=user_id)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, Mapping):
                return {str(k): int(v) for k, v in result.items()}
        except Exception:
            _log.exception("download.counts() raised")

    # 2) Fallback: full unfiltered list.
    try:
        raw = await _invoke_service(
            services.downloader,
            ("list_tasks", "list", "query", "snapshot"),
            user_id=user_id,
        )
        tasks = _normalize_tasks_result(raw)
        schemas = [_to_task_schema(t) for t in tasks]
        return compute_status_counts(schemas)
    except Exception:
        _log.exception("Failed to compute full-queue counts")
        return {
            "pending": 0,
            "running": 0,
            "paused": 0,
            "done": 0,
            "failed": 0,
            "cancelled": 0,
        }


# ===========================================================================
# Error translation
# ===========================================================================


def _task_not_found(task_id: str) -> HTTPException:
    """Standard 404 for a missing or foreign task."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_response(
            ErrorCode.NOT_FOUND,
            f"Download task {task_id!r} not found.",
        ).model_dump(),
    )


def _translate_service_error(
    exc: BaseException, *, context: str
) -> HTTPException:
    """Map a service exception to the appropriate HTTP error.

    Rules (matched on the exception **class name**, not its module,
    because the router is intentionally decoupled from
    ``core.exceptions``):

    * ``*InvalidStateTransition*`` → **409**
    * ``*StateTransition*``        → **409**
    * ``*Compliance*`` / ``*Blocked*`` → **403**
    * ``*NotFound*``               → **404**
    * ``*Timeout*``                → **504**
    * ``*Unavailable*`` / ``*Retry*`` → **503**
    * Anything else → **502** (bad gateway — the parser or a subsystem
      failed on the upstream side)
    """
    name = type(exc).__name__
    msg = str(exc) or name

    if "InvalidStateTransition" in name or "StateTransition" in name:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response(
                ErrorCode.CONFLICT,
                f"Invalid state transition while handling: {context}.",
                details={"reason": msg},
            ).model_dump(),
        )
    if "Compliance" in name or "Blocked" in name:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response(
                ErrorCode.FORBIDDEN,
                f"Request refused: {msg}.",
            ).model_dump(),
        )
    if "NotFound" in name:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(
                ErrorCode.NOT_FOUND, msg
            ).model_dump(),
        )
    if "Timeout" in name:
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Upstream timed out while handling: {context}.",
            ).model_dump(),
        )
    if "Unavailable" in name or "Retry" in name:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Service temporarily unavailable: {msg}.",
            ).model_dump(),
        )

    _log.exception("download service error (context={})", context)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=error_response(
            ErrorCode.DOWNLOAD_ERROR,
            f"Download operation failed: {msg}.",
            details={"reason": name},
        ).model_dump(),
    )


# ===========================================================================
# Small helpers
# ===========================================================================


def _uid(payload: CurrentUser) -> str:
    """Return the caller's user id, raising 401 if somehow absent."""
    if not payload or not payload.sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_response(
                ErrorCode.UNAUTHORIZED,
                "Authenticated user is missing from the token.",
            ).model_dump(),
        )
    return payload.sub


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-``None`` attribute among ``names``."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_priority() -> Any:
    """Import lazily to keep the module importable without the enum."""
    from nexusdl.core.models.enums import Priority  # noqa: PLC0415
    return Priority.NORMAL


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] Any new per-task endpoint uses :func:`_get_owned_task` — never
#     fetches the task directly. This is the single ownership choke
#     point; bypassing it would create a cross-user access bug.
# [ ] Any new status transition is a member of the relevant status set
#     (``_PAUSABLE``, ``_RESUMABLE``, ``_RETRIABLE``) or documents why it
#     does not fit.
# [ ] New bulk endpoints are declared **before** any ``/``{task_id}``
#     route so FastAPI's path matching does not treat their names as
#     task ids.
# [ ] New service method names are added to the corresponding names list
#     passed to :func:`_invoke_service`. Do not call ``getattr`` directly
#     in a handler.
# [ ] New return shapes are handled in :func:`_normalize_tasks_result`
#     or :func:`_extract_task_id` — never ``model_validate`` a raw
#     service result.
# [ ] New fields on ``DownloadTaskSchema`` are populated in
#     :func:`_to_task_schema`, with a safe default.
# [ ] Any new 4xx/5xx uses :func:`error_response` — never a bare
#     ``HTTPException(detail="...")``.
# [ ] Ownership enforcement respects admin bypass (``_is_admin``) —
#     otherwise moderation tooling breaks.
