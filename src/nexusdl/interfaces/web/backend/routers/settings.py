"""Settings router — ``/api/v1/settings``.

Thin FastAPI glue between HTTP and the host's settings service.
Responsibilities:

* Filter sections by the caller's role (``admin_only`` marker or id
  convention). Non-admin callers see the general sections; admins see
  everything.
* Project the host's :class:`SettingsSchema` (duck-typed) into
  :mod:`..schemas.responses` models, with **secrets masked**.
* Enforce optimistic concurrency via an ``etag``. On mismatch, respond
  **409 Conflict** with the current etag so the client can re-fetch.
* Expand ``reset_to_default=true`` into concrete values before calling
  :meth:`apply_settings`, since the host contract only accepts a plain
  ``changes`` mapping.
* Reject unknown setting keys **early** with a per-key error, instead of
  silently round-tripping them to the service.
* Surface per-field validation failures from :class:`ApplyResult`
  without throwing an HTTP error — the response body carries
  ``success: bool``, matching the GUI's :class:`ApplyResult` contract.

Architecture
============

This router **must not** import from
:mod:`nexusdl.interfaces.gui.views.settings_view`. The two interfaces are
peers. Instead, the router duck-types the shape of ``SettingsSchema`` /
``SettingSection`` / ``SettingField`` — see the ``_SchemaLike`` Protocols
below. This keeps the dependency graph ``web → core`` only.

Sync/async agnosticism
======================

``AppServices.apply_settings`` / ``snapshot_settings`` /
``build_settings_schema`` are documented as **sync** in the GUI's
composition root. This router does not assume that: it calls each, then
``inspect.isawaitable()`` to await coroutines if the host returned one.
Hosts that persist asynchronously can therefore return coroutines
without a shim.

Concurrency model
=================

The ``etag`` check is **optimistic but not transactional**: the router
fetches the snapshot, compares etags, then calls ``apply_settings``. In
between, another request may have applied its own change (TOCTOU). This
is acceptable for a settings UI — settings changes are rare and
low-stakes, and the fallback behaviour (last-writer-wins) is the same as
no etag check at all. Hosts that need true compare-and-swap must accept
the expected etag in ``apply_settings`` and enforce the check inside a
transaction — the router will forward the etag if the host's signature
supports it (see :func:`_call_apply_settings`), otherwise it stays
optimistic.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, HTTPException, status
from loguru import logger as _logger

from nexusdl.core.models.enums import UserRole

from ..dependencies import AdminUser, CurrentUser, ServicesDep
from ..schemas.requests import SettingsPatchRequest
from ..schemas.responses import (
    ErrorCode,
    SettingFieldSchema,
    SettingFieldType,
    SettingsResponse,
    SettingsSectionSchema,
    SettingsUpdateResponse,
    error_response,
)
from ..security import role_at_least

_log = _logger.bind(component="web.routers.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# ===========================================================================
# Duck-typed schema Protocols (documentation + mypy help; not enforced)
# ===========================================================================
#
# These Protocols mirror the *structural* shape of the objects that
# :meth:`ServicesDep.build_settings_schema` / :meth:`snapshot_settings`
# are expected to return. They are declared here, not imported from
# ``gui.views.settings_view``, to respect the layer boundary.

class _SettingFieldLike(Protocol):
    key: str
    label: str
    kind: Any            # has ``.value: str`` — "bool", "int", "secret", …
    default: Any
    description: str
    choices: Any         # tuple[str, ...] or empty
    min_value: Any
    max_value: Any
    readonly: bool
    requires_restart: bool


class _SettingGroupLike(Protocol):
    field_keys: Sequence[str]


class _SettingSectionLike(Protocol):
    id: str
    label: str
    description: str
    icon: str
    field_keys: Sequence[str]
    groups: Sequence[_SettingGroupLike]


class _SettingsSchemaLike(Protocol):
    sections: Sequence[_SettingSectionLike]
    fields: Mapping[str, _SettingFieldLike]


# ===========================================================================
# Constants
# ===========================================================================

#: Prefix carried by every computed etag. Bumped on format changes.
_ETAG_PREFIX: str = "v1:"

#: Default value returned when the host does not provide a schema version.
_DEFAULT_SCHEMA_VERSION: str = "1"

#: Mapping from the string values used by ``FieldKind`` (in
#: ``gui.views.settings_view``) to :class:`SettingFieldType`. The router
#: reads ``field.kind.value`` as a ``str`` — no import from ``gui``.
_FIELD_KIND_MAP: dict[str, SettingFieldType] = {
    "bool": SettingFieldType.BOOLEAN,
    "int": SettingFieldType.INTEGER,
    "float": SettingFieldType.FLOAT,
    "str": SettingFieldType.STRING,
    "secret": SettingFieldType.SECRET,
    "enum": SettingFieldType.ENUM,
    "path_dir": SettingFieldType.PATH,
    "path_file": SettingFieldType.PATH,
    "readonly": SettingFieldType.READONLY,
}


# ===========================================================================
# GET — read the current settings
# ===========================================================================

@router.get(
    "",
    response_model=SettingsResponse,
    summary="Read all settings",
    description=(
        "Return every settings section the caller is allowed to see. "
        "Secrets are never returned — for ``secret`` fields only "
        "``is_set`` is exposed. Non-admin callers do not see sections "
        "marked as admin-only."
    ),
)
async def get_settings(
    payload: CurrentUser,
    services: ServicesDep,
) -> SettingsResponse:
    schema = await _maybe_await(services.build_settings_schema())
    snapshot = await _maybe_await(services.snapshot_settings())

    is_admin = role_at_least(payload.role, UserRole.ADMIN)
    sections = _project_schema(schema, snapshot, is_admin=is_admin)

    return SettingsResponse(
        sections=sections,
        version=_schema_version(services),
        etag=await _compute_etag(services, snapshot),
    )


# ===========================================================================
# PATCH — apply changes (admin only)
# ===========================================================================

@router.patch(
    "",
    response_model=SettingsUpdateResponse,
    summary="Update settings",
    description=(
        "Apply a set of changes. Admin only.\n\n"
        "* ``changes`` maps dotted keys to new values. Unknown keys are "
        "rejected with a per-key error.\n"
        "* ``etag`` (optional) enables optimistic concurrency: on "
        "mismatch the request is rejected with 409 Conflict and the "
        "current etag. **The check is optimistic, not transactional** — "
        "two concurrent PATCHes with the same valid etag will both "
        "succeed, last-writer-wins. Hosts that need strict "
        "compare-and-swap should accept the expected etag in "
        "``apply_settings`` and enforce it under a lock.\n"
        "* ``reset_to_default=true`` resets every listed key to its "
        "schema default (the per-key values are ignored).\n\n"
        "The response always carries ``success: bool`` — per-field "
        "validation failures populate ``errors`` rather than producing "
        "an HTTP error."
    ),
)
async def patch_settings(
    body: SettingsPatchRequest,
    payload: AdminUser,  # 403 for non-admins, handled by dependencies
    services: ServicesDep,
) -> SettingsUpdateResponse:
    # --- 1. Load schema (once) -------------------------------------
    try:
        schema = await _maybe_await(services.build_settings_schema())
    except Exception:
        _log.exception("build_settings_schema failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to load settings schema.",
            ).model_dump(),
        )

    # --- 2. Optimistic concurrency check ---------------------------
    if body.etag is not None:
        try:
            current_snapshot = await _maybe_await(services.snapshot_settings())
        except Exception:
            _log.exception("snapshot_settings failed during etag check")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to read current settings.",
                ).model_dump(),
            )
        current_etag = await _compute_etag(services, current_snapshot)
        if current_etag is None or body.etag != current_etag:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_response(
                    ErrorCode.CONFLICT,
                    "Settings were modified by another client.",
                    details={"current_etag": current_etag},
                ).model_dump(),
            )

    # --- 3. Expand resets into concrete values ---------------------
    changes = _expand_resets(
        body.changes, schema, reset_to_default=body.reset_to_default
    )

    # --- 4. Reject unknown keys early -----------------------------
    schema_fields = _schema_fields(schema)
    unknown = [k for k in changes if k not in schema_fields]
    if unknown:
        return SettingsUpdateResponse(
            success=False,
            errors={k: "Unknown setting key." for k in unknown},
            message=(
                f"{len(unknown)} unknown key(s). Nothing was applied."
            ),
        )

    # --- 5. Delegate to the service --------------------------------
    try:
        result = await _call_apply_settings(
            services,
            changes=changes,
            expected_etag=body.etag,
        )
    except Exception:
        _log.exception("apply_settings raised")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to apply settings.",
            ).model_dump(),
        )

    # --- 6. Compute new etag for the response ----------------------
    new_etag: str | None = None
    if getattr(result, "success", False):
        try:
            post_snapshot = await _maybe_await(services.snapshot_settings())
            new_etag = await _compute_etag(services, post_snapshot)
        except Exception:
            _log.exception("post-apply snapshot_settings failed")
            # Non-fatal: the client will re-GET if it needs the etag.

    # --- 7. Build the response ------------------------------------
    success = bool(getattr(result, "success", False))
    applied_keys = list(changes.keys()) if success else []
    errors = dict(getattr(result, "errors", {}) or {})
    message = str(getattr(result, "message", "") or "")
    requires_restart = bool(getattr(result, "requires_restart", False))

    if success:
        _log.info(
            "settings applied by user={} keys={} restart_required={}",
            payload.sub,
            applied_keys,
            requires_restart,
        )

    return SettingsUpdateResponse(
        success=success,
        applied_keys=applied_keys,
        requires_restart=requires_restart,
        errors=errors,
        message=message,
        new_etag=new_etag,
    )


# ===========================================================================
# Projection helpers
# ===========================================================================

def _project_schema(
    schema: _SettingsSchemaLike,
    snapshot: Mapping[str, Any],
    *,
    is_admin: bool,
) -> list[SettingsSectionSchema]:
    """Convert the host's ``SettingsSchema`` to API response sections.

    Non-admin callers do not see admin-only sections. Field ordering
    inside a section follows :attr:`SettingSection.field_keys` (and any
    group's keys, if groups are declared).

    Defensive: a broken host may return ``sections=None`` or
    ``fields=None``; both are coerced to empty (``[]`` / ``{}``) rather
    than crashing with a 500 on GET /settings.
    """
    schema_sections = getattr(schema, "sections", None) or ()
    schema_fields = _schema_fields(schema)

    out: list[SettingsSectionSchema] = []
    for section in schema_sections:
        if _is_admin_section(section) and not is_admin:
            continue
        field_keys = _section_field_keys(section)
        fields = [
            _project_field(schema_fields[key], snapshot)
            for key in field_keys
            if key in schema_fields
        ]
        out.append(
            SettingsSectionSchema(
                id=str(section.id),
                label=str(section.label),
                description=str(getattr(section, "description", "") or ""),
                icon=str(getattr(section, "icon", "") or ""),
                fields=fields,
            )
        )
    return out


def _project_field(
    field: _SettingFieldLike, snapshot: Mapping[str, Any]
) -> SettingFieldSchema:
    """Convert one field to its API representation, masking secrets.

    For ``secret`` fields, ``current_value`` is ``None`` and ``is_set``
    is derived from the snapshot (which the host contract declares as
    ``True``/``False``). Defensive: even if the host accidentally returns
    a raw secret string, it is **never** placed in ``current_value``.
    """
    kind_str = _field_kind_str(field)
    api_type = _FIELD_KIND_MAP.get(kind_str, SettingFieldType.STRING)
    is_secret = api_type == SettingFieldType.SECRET

    raw = snapshot.get(str(field.key))
    if is_secret:
        # Never leak: ``raw`` may be ``True``/``False`` (correct) or, by
        # contract violation, an actual value (``str``, …). ``bool()``
        # reduces both to "is a value set?".
        current_value: Any = None
        is_set: bool | None = bool(raw)
    else:
        current_value = raw
        is_set = None

    choices = getattr(field, "choices", ()) or ()
    default = getattr(field, "default", None)
    description = str(getattr(field, "description", "") or "")
    readonly = bool(getattr(field, "readonly", False))
    requires_restart = bool(getattr(field, "requires_restart", False))
    min_value = getattr(field, "min_value", None)
    max_value = getattr(field, "max_value", None)
    advanced = bool(getattr(field, "advanced", False))

    return SettingFieldSchema(
        key=str(field.key),
        label=str(field.label),
        type=api_type,
        description=description,
        default=default,
        current_value=current_value,
        is_set=is_set,
        choices=[str(c) for c in choices] if choices else None,
        min_value=min_value,
        max_value=max_value,
        readonly=readonly,
        requires_restart=requires_restart,
        advanced=advanced,
    )


# ===========================================================================
# Reset expansion
# ===========================================================================

def _expand_resets(
    changes: Mapping[str, Any],
    schema: _SettingsSchemaLike,
    *,
    reset_to_default: bool,
) -> dict[str, Any]:
    """Replace every value with its schema default when ``reset_to_default``.

    Unknown keys are preserved with their original value so the caller can
    produce a precise "unknown key" error rather than silently dropping
    them. This is important *even when* ``reset_to_default=True``: an
    unknown key has no schema default, so we must not lose the original
    value — otherwise a bug in the caller's payload would look like a
    successful reset of a non-existent key.

    The known fields (present in ``schema.fields``) are reset to their
    schema default when ``reset_to_default=True``. Unknown fields keep
    their incoming value; the caller's unknown-key check runs *after*
    this helper and rejects the whole request, so the preserved value
    never reaches the host.
    """
    schema_fields = _schema_fields(schema)
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if not reset_to_default:
            out[key] = value
            continue
        field = schema_fields.get(key)
        if field is None:
            # Unknown key: preserve the incoming value so the caller can
            # produce a precise "unknown key" error. Never replace with
            # ``None`` — that would hide the actual payload from the
            # diagnostic.
            out[key] = value
        else:
            out[key] = getattr(field, "default", None)
    return out


# ===========================================================================
# Etag / version helpers
# ===========================================================================

async def _compute_etag(
    services: ServicesDep, snapshot: Mapping[str, Any]
) -> str | None:
    """Compute an etag for the current settings.

    Prefers ``services.settings_etag()`` if the host implements it (gives
    the host full control over the format). The host may return either a
    ``str`` or an awaitable resolving to a ``str`` — this helper is
    ``async`` to support both without a shim.

    Falls back to a stable hash of the snapshot if the host does not
    provide an etag or returns a non-string value. Returns ``None`` only
    when the fallback itself fails — the caller treats ``None`` as
    "no optimistic concurrency possible".
    """
    # Host-provided etag wins.
    provider = getattr(services, "settings_etag", None)
    if callable(provider):
        try:
            value = provider()
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, str) and value:
                return (
                    value
                    if value.startswith(_ETAG_PREFIX)
                    else f"{_ETAG_PREFIX}{value}"
                )
        except Exception:
            _log.exception("services.settings_etag() raised")

    # Fallback: stable hash of the snapshot.
    try:
        canonical = json.dumps(
            {str(k): _jsonable(v) for k, v in snapshot.items()},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        _log.exception("Failed to serialize snapshot for etag")
        return None
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{_ETAG_PREFIX}{digest}"


def _jsonable(value: Any) -> Any:
    """Best-effort JSON projection for etag stability.

    The etag ends up in a response header on 409, so any value that
    could not be hashed consistently would either produce a spurious
    conflict or — worse — become an oracle that lets an attacker brute
    force a secret by observing etag changes. This helper is therefore
    conservative: scalar types are preserved, everything else is
    converted to a stable string representation.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    # Secrets are only ever stored as bools, never raw. If this is
    # reached with something exotic, str() keeps the hash stable.
    return str(value)


def _schema_version(services: ServicesDep) -> str:
    """Return the schema version reported by the host, if any."""
    try:
        attr = getattr(services, "settings_schema_version", None)
        if callable(attr):
            value = attr()
            if isinstance(value, str) and value:
                return value
        elif isinstance(attr, str) and attr:
            return attr
    except Exception:
        _log.debug("services.settings_schema_version() raised; using default")
    return _DEFAULT_SCHEMA_VERSION


# ===========================================================================
# Visibility helpers
# ===========================================================================

def _is_admin_section(section: _SettingSectionLike) -> bool:
    """Return whether ``section`` is admin-only.

    Precedence:

    1. Explicit ``section.admin_only`` attribute (host-declared).
    2. Id convention: ``admin``, ``admin.*``, ``admin_*`` (case-insensitive
       to accommodate hosts that use ``Admin`` / ``ADMIN``).

    The lowercase comparison is defensive: a case mismatch between a
    host declaration and this check would otherwise expose an
    admin-only section to non-admin callers — a real privacy regression.
    """
    explicit = getattr(section, "admin_only", None)
    if explicit is True:
        return True
    if explicit is False:
        return False
    sid = str(getattr(section, "id", "")).lower()
    return sid == "admin" or sid.startswith(("admin.", "admin_"))


def _section_field_keys(section: _SettingSectionLike) -> list[str]:
    """Return every field key declared by ``section``, in order.

    A section may declare fields via ``field_keys`` (flat) and/or via
    ``groups`` (each with its own ``field_keys``). Both are unioned here
    so a partially-declared section still renders everything. Order is
    preserved: flat keys first, then group keys in group order.
    """
    out: list[str] = []
    seen: set[str] = set()

    flat = getattr(section, "field_keys", ()) or ()
    for key in flat:
        k = str(key)
        if k not in seen:
            seen.add(k)
            out.append(k)

    groups = getattr(section, "groups", ()) or ()
    for group in groups:
        for key in getattr(group, "field_keys", ()) or ():
            k = str(key)
            if k not in seen:
                seen.add(k)
                out.append(k)

    return out


# ===========================================================================
# Schema accessors (defensive)
# ===========================================================================

def _schema_fields(schema: _SettingsSchemaLike) -> Mapping[str, _SettingFieldLike]:
    """Return ``schema.fields`` normalized to a non-None mapping.

    A broken host may return ``fields=None`` (or a non-mapping). Both are
    coerced to an empty dict so downstream logic (unknown-key check,
    projection, reset expansion) sees a stable shape rather than
    crashing with ``TypeError``. A missing fields map is a symptom of a
    configuration error, not a client error, so returning an empty map
    here surfaces the issue at the "unknown key" check rather than as a
    500.
    """
    fields = getattr(schema, "fields", None)
    if not isinstance(fields, Mapping):
        return {}
    return fields


# ===========================================================================
# Async-tolerant delegation
# ===========================================================================

async def _call_apply_settings(
    services: ServicesDep,
    *,
    changes: Mapping[str, Any],
    expected_etag: str | None,
) -> Any:
    """Call ``services.apply_settings`` with the correct signature.

    The GUI contract declares ``apply_settings(changes) -> ApplyResult``.
    Hosts that want strict compare-and-swap may declare
    ``apply_settings(changes, *, expected_etag=None)`` — in that case
    the router forwards the etag so the host can enforce the check
    atomically. Both sync and async return values are supported.

    Signature probing is done via ``inspect.signature``; if introspection
    fails (C-extension, ``functools.partial`` without ``__wrapped__``),
    the router falls back to the one-argument form.
    """
    apply_fn = getattr(services, "apply_settings", None)
    if not callable(apply_fn):
        raise AttributeError("services.apply_settings is not callable")

    supports_etag = False
    try:
        sig = inspect.signature(apply_fn)
        supports_etag = "expected_etag" in sig.parameters
    except (TypeError, ValueError):
        # Introspection not available — assume the simple contract.
        supports_etag = False

    if supports_etag:
        result = apply_fn(changes, expected_etag=expected_etag)
    else:
        result = apply_fn(changes)
    if inspect.isawaitable(result):
        result = await result
    return result


# ===========================================================================
# Misc helpers
# ===========================================================================

def _field_kind_str(field: _SettingFieldLike) -> str:
    """Return the string value of a field's kind, robust to shape.

    ``SettingField.kind`` is an enum whose ``.value`` is a stable string
    (``"bool"``, ``"secret"``, …). Hosts that pass a plain string instead
    of the enum are also accepted.
    """
    kind = getattr(field, "kind", None)
    if kind is None:
        return "str"
    value = getattr(kind, "value", None)
    if isinstance(value, str):
        return value
    if isinstance(kind, str):
        return kind
    return str(kind)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, otherwise return it unchanged.

    Allows the host to return coroutines from what the GUI contract
    declares as sync methods, without forcing this router to branch on
    each call site.
    """
    if inspect.isawaitable(value):
        return await value
    return value


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] A new setting section that should be admin-only is declared with
#     ``admin_only = True`` on the section, or its id uses the
#     ``admin*`` prefix convention (case-insensitive).
# [ ] No new code path can leak a secret value into ``current_value``.
#     The mask happens in :func:`_project_field` only — do not bypass it.
# [ ] A new field type is added to :data:`_FIELD_KIND_MAP` with the
#     matching :class:`SettingFieldType` member.
# [ ] If the host's ``apply_settings`` is extended with an
#     ``expected_etag`` keyword, :func:`_call_apply_settings` picks it up
#     automatically — no router change required.
# [ ] Any new error code you return is documented in
#     :class:`~..schemas.responses.ErrorCode`.
