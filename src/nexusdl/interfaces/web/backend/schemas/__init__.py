"""Request / response / WebSocket schemas for the NexusDL web backend.

This package groups the four Pydantic v2 schema modules used by the HTTP
and WebSocket layers of the backend:

* :mod:`.common`    — shared query-parameter groups (pagination, sort,
  time-range) and small parsing helpers.
* :mod:`.requests`  — HTTP request bodies.
* :mod:`.responses` — HTTP response bodies and the standard error envelope.
* :mod:`.ws`        — WebSocket message envelopes (client ↔ server).

Loading strategy
================

All exports are **lazy** via :pep:`562` (``__getattr__``). Importing this
package never imports Pydantic, FastAPI or the WebSocket engine. Symbols
are resolved on first attribute access and then cached in ``globals()``
— same behaviour as a top-level import, one-time cost.

Why lazy
--------

* ``schemas`` may be imported transitively by many code paths (routers,
  middleware, tests, and — via ``app.py`` — the GUI). Not every path
  needs every schema.
* :mod:`.ws` pulls in :mod:`..websocket` (an entire engine module) for
  anyone who merely wants ``ErrorResponse``. Lazy loading means an
  HTTP-only caller never pays that cost.

Error handling
--------------

If a schema module cannot be imported because an optional dependency is
missing, :func:`__getattr__` raises an :class:`ImportError` pointing at
the ``[web]`` extra instead of the raw ``ModuleNotFoundError``.

Chain of ``__getattr__`` calls
------------------------------

``schemas.X`` → ``schemas.__getattr__`` → ``import_module(".ws", __name__)``
→ ``ws.X`` (which, for ``WSCloseCode`` / ``WSEventType``, itself falls
through ``ws.__getattr__``). Everything composes transparently.

Introspection
-------------

:func:`list_schemas`, :func:`get_schema`, and :func:`list_submodules`
provide a stable public surface for discovery — parametrized tests,
OpenAPI tooling, and future schema registries.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# --- Package version (soft read from nexusdl.version) -----------------------

try:
    from nexusdl.version import __version__ as __version__
except Exception:  # pragma: no cover - defensive fallback
    __version__ = "3.0.0"


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # ----- common.py: shared query params + helpers -----
    "DEFAULT_PAGE",
    "DEFAULT_PER_PAGE",
    "MAX_PER_PAGE",
    "SortOrder",
    "QueryParams",
    "PaginationParams",
    "SortParams",
    "TimeRangeParams",
    "LibraryQueryParams",
    "DownloadQueryParams",
    "ChapterQueryParams",
    "parse_bool_query",
    "parse_csv_query",
    "parse_enum_csv",
    "paginate",
    "clamp_page",
    # ----- requests.py: HTTP request bodies -----
    "USERNAME_MIN_LENGTH",
    "USERNAME_MAX_LENGTH",
    "PASSWORD_MAX_BYTES",
    "SEARCH_QUERY_MAX_LENGTH",
    "MAX_CHAPTER_IDS_PER_REQUEST",
    "RequestBody",
    "LoginRequest",
    "RegisterRequest",
    "RefreshTokenRequest",
    "LogoutRequest",
    "SearchRequest",
    "DownloadRequest",
    "UpdateProgressRequest",
    "SettingsPatchRequest",
    # ----- responses.py: HTTP response bodies -----
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "API_VERSION",
    "ErrorCode",
    "ErrorBody",
    "ErrorResponse",
    "MessageResponse",
    "ResourceIdResponse",
    "PaginatedResponse",
    "error_response",
    "ComponentHealth",
    "HealthResponse",
    "ReadinessResponse",
    "VersionResponse",
    "UserInfo",
    "TokenResponse",
    "LoginResponse",
    "RefreshResponse",
    "SiteCapabilitiesSchema",
    "SiteSchema",
    "SiteListResponse",
    "SiteHealthSchema",
    "SiteHealthResponse",
    "MangaSummarySchema",
    "MangaDetailSchema",
    "ChapterSummarySchema",
    "PageSchema",
    "ChapterListResponse",
    "PageListResponse",
    "SearchResponse",
    "DownloadTaskSchema",
    "DownloadTaskListResponse",
    "DownloadSubmitResponse",
    "ReadingProgressSchema",
    "LibraryEntrySchema",
    "LibraryListResponse",
    "SettingFieldType",
    "SettingFieldSchema",
    "SettingsSectionSchema",
    "SettingsResponse",
    "SettingsUpdateResponse",
    "compute_status_counts",
    # ----- ws.py: WebSocket messages (WSCloseCode / WSEventType are re-exports) -----
    "MAX_MESSAGE_SIZE",
    "CLIENT_PROTOCOL_TYPES",
    "KNOWN_CLIENT_TYPES",
    "KNOWN_SERVER_TYPES",
    "WSCloseCode",
    "WSEventType",
    "AuthInfo",
    "ConnectionInfo",
    "ServerMessage",
    "ServerErrorMessage",
    "ServerErrorBody",
    "ServerHelloMessage",
    "ServerPingMessage",
    "ServerPongMessage",
    "DownloadStartedEvent",
    "DownloadProgressEvent",
    "DownloadCompletedEvent",
    "DownloadFailedEvent",
    "DownloadPausedEvent",
    "DownloadResumedEvent",
    "DownloadCancelledEvent",
    "ChapterStartedEvent",
    "ChapterCompletedEvent",
    "ChapterFailedEvent",
    "LibraryUpdatedEvent",
    "SiteHealthChangedEvent",
    "ClientMessage",
    "ClientSubscribeMessage",
    "ClientUnsubscribeMessage",
    "ClientPingMessage",
    "ClientPongMessage",
    "ClientMessageTyped",
    "parse_client_message",
    "ServerMessageAdapter",
    "ClientMessageAdapter",
    "ws_to_server_message",
    "server_message_to_ws",
    "ws_to_server_error",
    "server_error_to_ws",
    # ----- Introspection -----
    "list_schemas",
    "get_schema",
    "list_submodules",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps symbol name -> submodule basename. In every schemas submodule, the
# symbol's name equals its attribute name, so a plain dict[str, str] is
# sufficient and less error-prone than a (module, attr) tuple.
#
# To add a new symbol exported at the top level:
#   1. add it to the corresponding submodule's ``__all__``,
#   2. add an entry here,
#   3. add it to the ``__all__`` above and to the TYPE_CHECKING block.
#
# Adding a whole new submodule:
#   1. create ``schemas/<name>.py`` with its own ``__all__``,
#   2. add ``"<name>": tuple[str, ...]`` to :data:`_SUBMODULES` below,
#   3. add all its public symbols to ``_SCHEMAS`` with module key ``"<name>"``.

_SCHEMAS: dict[str, str] = {
    # ----- common -----
    "DEFAULT_PAGE": "common",
    "DEFAULT_PER_PAGE": "common",
    "MAX_PER_PAGE": "common",
    "SortOrder": "common",
    "QueryParams": "common",
    "PaginationParams": "common",
    "SortParams": "common",
    "TimeRangeParams": "common",
    "LibraryQueryParams": "common",
    "DownloadQueryParams": "common",
    "ChapterQueryParams": "common",
    "parse_bool_query": "common",
    "parse_csv_query": "common",
    "parse_enum_csv": "common",
    "paginate": "common",
    "clamp_page": "common",
    # ----- requests -----
    "USERNAME_MIN_LENGTH": "requests",
    "USERNAME_MAX_LENGTH": "requests",
    "PASSWORD_MAX_BYTES": "requests",
    "SEARCH_QUERY_MAX_LENGTH": "requests",
    "MAX_CHAPTER_IDS_PER_REQUEST": "requests",
    "RequestBody": "requests",
    "LoginRequest": "requests",
    "RegisterRequest": "requests",
    "RefreshTokenRequest": "requests",
    "LogoutRequest": "requests",
    "SearchRequest": "requests",
    "DownloadRequest": "requests",
    "UpdateProgressRequest": "requests",
    "SettingsPatchRequest": "requests",
    # ----- responses -----
    "DEFAULT_PAGE_SIZE": "responses",
    "MAX_PAGE_SIZE": "responses",
    "API_VERSION": "responses",
    "ErrorCode": "responses",
    "ErrorBody": "responses",
    "ErrorResponse": "responses",
    "MessageResponse": "responses",
    "ResourceIdResponse": "responses",
    "PaginatedResponse": "responses",
    "error_response": "responses",
    "ComponentHealth": "responses",
    "HealthResponse": "responses",
    "ReadinessResponse": "responses",
    "VersionResponse": "responses",
    "UserInfo": "responses",
    "TokenResponse": "responses",
    "LoginResponse": "responses",
    "RefreshResponse": "responses",
    "SiteCapabilitiesSchema": "responses",
    "SiteSchema": "responses",
    "SiteListResponse": "responses",
    "SiteHealthSchema": "responses",
    "SiteHealthResponse": "responses",
    "MangaSummarySchema": "responses",
    "MangaDetailSchema": "responses",
    "ChapterSummarySchema": "responses",
    "PageSchema": "responses",
    "ChapterListResponse": "responses",
    "PageListResponse": "responses",
    "SearchResponse": "responses",
    "DownloadTaskSchema": "responses",
    "DownloadTaskListResponse": "responses",
    "DownloadSubmitResponse": "responses",
    "ReadingProgressSchema": "responses",
    "LibraryEntrySchema": "responses",
    "LibraryListResponse": "responses",
    "SettingFieldType": "responses",
    "SettingFieldSchema": "responses",
    "SettingsSectionSchema": "responses",
    "SettingsResponse": "responses",
    "SettingsUpdateResponse": "responses",
    "compute_status_counts": "responses",
    # ----- ws -----
    # NOTE: WSCloseCode and WSEventType are not defined in ws.py directly;
    # they are resolved through ws.py's own __getattr__, which forwards to
    # ..websocket. The chain works transparently.
    "MAX_MESSAGE_SIZE": "ws",
    "CLIENT_PROTOCOL_TYPES": "ws",
    "KNOWN_CLIENT_TYPES": "ws",
    "KNOWN_SERVER_TYPES": "ws",
    "WSCloseCode": "ws",
    "WSEventType": "ws",
    "AuthInfo": "ws",
    "ConnectionInfo": "ws",
    "ServerMessage": "ws",
    "ServerErrorMessage": "ws",
    "ServerErrorBody": "ws",
    "ServerHelloMessage": "ws",
    "ServerPingMessage": "ws",
    "ServerPongMessage": "ws",
    "DownloadStartedEvent": "ws",
    "DownloadProgressEvent": "ws",
    "DownloadCompletedEvent": "ws",
    "DownloadFailedEvent": "ws",
    "DownloadPausedEvent": "ws",
    "DownloadResumedEvent": "ws",
    "DownloadCancelledEvent": "ws",
    "ChapterStartedEvent": "ws",
    "ChapterCompletedEvent": "ws",
    "ChapterFailedEvent": "ws",
    "LibraryUpdatedEvent": "ws",
    "SiteHealthChangedEvent": "ws",
    "ClientMessage": "ws",
    "ClientSubscribeMessage": "ws",
    "ClientUnsubscribeMessage": "ws",
    "ClientPingMessage": "ws",
    "ClientPongMessage": "ws",
    "ClientMessageTyped": "ws",
    "parse_client_message": "ws",
    "ServerMessageAdapter": "ws",
    "ClientMessageAdapter": "ws",
    "ws_to_server_message": "ws",
    "server_message_to_ws": "ws",
    "ws_to_server_error": "ws",
    "server_error_to_ws": "ws",
}


#: The four schema submodules — name → tuple of its public symbols.
#: Derived from :data:`_SCHEMAS` on first use; kept separate so
#: :func:`list_submodules` does not need to scan the registry each call.
_SUBMODULES: dict[str, tuple[str, ...]] = {
    "common": tuple(s for s, m in _SCHEMAS.items() if m == "common"),
    "requests": tuple(s for s, m in _SCHEMAS.items() if m == "requests"),
    "responses": tuple(s for s, m in _SCHEMAS.items() if m == "responses"),
    "ws": tuple(s for s, m in _SCHEMAS.items() if m == "ws"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from .common import (
        ChapterQueryParams,
        DEFAULT_PAGE,
        DEFAULT_PER_PAGE,
        DownloadQueryParams,
        LibraryQueryParams,
        MAX_PER_PAGE,
        PaginationParams,
        QueryParams,
        SortOrder,
        SortParams,
        TimeRangeParams,
        clamp_page,
        paginate,
        parse_bool_query,
        parse_csv_query,
        parse_enum_csv,
    )
    from .requests import (
        DownloadRequest,
        LoginRequest,
        LogoutRequest,
        MAX_CHAPTER_IDS_PER_REQUEST,
        PASSWORD_MAX_BYTES,
        RefreshTokenRequest,
        RegisterRequest,
        RequestBody,
        SEARCH_QUERY_MAX_LENGTH,
        SearchRequest,
        SettingsPatchRequest,
        UpdateProgressRequest,
        USERNAME_MAX_LENGTH,
        USERNAME_MIN_LENGTH,
    )
    from .responses import (
        API_VERSION,
        ChapterListResponse,
        ChapterSummarySchema,
        ComponentHealth,
        DEFAULT_PAGE_SIZE,
        DownloadSubmitResponse,
        DownloadTaskListResponse,
        DownloadTaskSchema,
        ErrorBody,
        ErrorCode,
        ErrorResponse,
        HealthResponse,
        LibraryEntrySchema,
        LibraryListResponse,
        LoginResponse,
        MangaDetailSchema,
        MangaSummarySchema,
        MAX_PAGE_SIZE,
        MessageResponse,
        PageListResponse,
        PageSchema,
        PaginatedResponse,
        ReadinessResponse,
        ReadingProgressSchema,
        RefreshResponse,
        ResourceIdResponse,
        SearchResponse,
        SettingFieldSchema,
        SettingFieldType,
        SettingsResponse,
        SettingsSectionSchema,
        SettingsUpdateResponse,
        SiteCapabilitiesSchema,
        SiteHealthResponse,
        SiteHealthSchema,
        SiteListResponse,
        SiteSchema,
        TokenResponse,
        UserInfo,
        VersionResponse,
        compute_status_counts,
        error_response,
    )
    from .ws import (
        AuthInfo,
        ChapterCompletedEvent,
        ChapterFailedEvent,
        ChapterStartedEvent,
        ClientMessage,
        ClientMessageAdapter,
        ClientMessageTyped,
        ClientPingMessage,
        ClientPongMessage,
        ClientProtocol_TYPES,
        ClientSubscribeMessage,
        ClientUnsubscribeMessage,
        CLIENT_PROTOCOL_TYPES,
        ConnectionInfo,
        DownloadCancelledEvent,
        DownloadCompletedEvent,
        DownloadFailedEvent,
        DownloadPausedEvent,
        DownloadProgressEvent,
        DownloadResumedEvent,
        DownloadStartedEvent,
        KNOWN_CLIENT_TYPES,
        KNOWN_SERVER_TYPES,
        LibraryUpdatedEvent,
        MAX_MESSAGE_SIZE,
        ServerErrorBody,
        ServerErrorMessage,
        ServerHelloMessage,
        ServerMessage,
        ServerMessageAdapter,
        ServerPingMessage,
        ServerPongMessage,
        SiteHealthChangedEvent,
        WSCloseCode,
        WSEventType,
        parse_client_message,
        server_error_to_ws,
        server_message_to_ws,
        ws_to_server_error,
        ws_to_server_message,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public schema on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known public schema symbol.
        ImportError: If the symbol's module cannot be imported because an
            optional dependency is missing. The message points at the
            ``[web]`` extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    submodule = _SCHEMAS.get(name)
    if submodule is None:
        available = ", ".join(sorted(_SCHEMAS))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available schemas: {available}"
        )

    try:
        module = importlib.import_module(f".{submodule}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name in {"pydantic", "python_jose", "jose"}:
            raise ImportError(
                f"{name!r} requires the Web extra. "
                f"Install it with: pip install 'nexusdl[web]'"
            ) from exc
        raise

    # ``getattr(module, name)`` transparently resolves through the
    # submodule's own lazy loader if it has one (as ws.py does for
    # WSCloseCode / WSEventType).
    value = getattr(module, name)

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behaviour as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the full public API to ``dir()``, IDEs and tab-completion."""
    return sorted(list(globals().keys()) + list(__all__))


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_schemas() -> list[str]:
    """Return the sorted list of top-level schema symbols.

    Covers all four submodules (``common``, ``requests``, ``responses``,
    ``ws``). Useful for parametrized tests, OpenAPI tooling, and future
    schema registries.
    """
    return sorted(_SCHEMAS.keys())


def get_schema(name: str) -> Any:
    """Return the schema symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (schema registries, plugin code, parametrized tests).

    Raises:
        KeyError: If ``name`` is not a registered schema symbol.
    """
    if name not in _SCHEMAS:
        available = ", ".join(sorted(_SCHEMAS))
        raise KeyError(
            f"Unknown schema {name!r}. Available schemas: {available}"
        )
    return getattr(__import__(__name__, fromlist=[name]), name)


def list_submodules() -> dict[str, tuple[str, ...]]:
    """Return a mapping of submodule name → its public symbols.

    The returned mapping is a shallow copy — safe to mutate by the caller
    without affecting the package. Useful for building a schema browser
    or for generating per-module documentation.
    """
    return {name: tuple(symbols) for name, symbols in _SUBMODULES.items()}
