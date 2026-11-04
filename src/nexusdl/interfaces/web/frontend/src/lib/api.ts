/**
 * HTTP client for the NexusDL backend REST API.
 *
 * This module is the frontend's single point of contact with the backend
 * over HTTP.  Every hook and page that needs data from the API calls a
 * method here; nothing else in the frontend issues a ``fetch`` directly.
 *
 * URL derivation
 * --------------
 * All requests use **relative** URLs (``/api/v1/...``).  In development and
 * in production, the reverse proxy — the Next.js rewrite in dev, Caddy or
 * nginx in prod — forwards ``/api/*`` to the backend.  As a result:
 *
 *   * The browser only ever sees same-origin URLs.
 *   * No CORS configuration is needed.
 *   * No ``NEXT_PUBLIC_*`` variable is exposed to the client bundle.
 *   * The frontend is identical in dev and prod, byte-for-byte.
 *
 * Tests and non-browser environments can pass ``baseUrl`` to point at an
 * absolute URL (``"http://localhost:8000"``); the default of ``""`` makes
 * every URL relative, which is what a browser expects.
 *
 * Authentication
 * --------------
 * Every request automatically includes ``Authorization: Bearer <token>``,
 * where ``<token>`` is read from the ``getToken()`` callback provided at
 * construction time.  The default callback reads
 * ``localStorage["nexusdl_access_token"]``; the auth store (delivered
 * separately) will override this with an in-memory implementation.
 *
 * On ``401 Unauthorized`` responses, the client transparently tries to
 * refresh the token using the refresh token (also read from a callback,
 * defaulting to ``localStorage["nexusdl_refresh_token"]``) and retries the
 * original request **once**.  Concurrent 401s share a single refresh
 * attempt (single-flight).  If the refresh fails, the error is propagated
 * to the caller with code ``"UNAUTHORIZED"`` so that the auth layer can
 * redirect to login.  The client never retries more than once, so there is
 * no risk of an infinite loop if the backend is misbehaving.
 *
 * Errors
 * ------
 * Every failure — transport, HTTP, parse, network — is normalised into a
 * :class:`NexusApiError` carrying:
 *
 *   * ``code``        — one of the standard API error codes, or the raw
 *     string the backend sent (for forward compatibility with future codes).
 *   * ``status``      — the HTTP status code (``0`` for transport errors).
 *   * ``details``     — the ``error.details`` object from the envelope
 *     (empty object when the backend has none).
 *   * ``request_id``  — the value of ``error.request_id`` or the
 *     ``X-Request-ID`` response header, whichever is present.
 *   * ``cause``       — the original exception, when the error originated
 *     below the HTTP layer.
 *
 * This is what makes the client ergonomic: callers can ``catch`` a single
 * error class and switch on ``error.code`` to handle specific cases, without
 * inspecting the underlying fetch/response/JSON-parsing exceptions.
 *
 * Pagination
 * ----------
 * Every paginated endpoint returns an envelope of the shape::
 *
 *     { items, total, page, per_page, total_pages, has_next, has_prev }
 *
 * Each response type (``DownloadListResponse``, ``SearchResponse``,
 * ``LibraryListResponse``) declares the envelope explicitly so that the
 * frontend hook can rely on the exact item type.  The client does not
 * attempt to unify these — the shape is already uniform, and separate
 * types are what let the compiler enforce the item shape per endpoint.
 *
 * Wire types
 * ----------
 * The ``./types/*`` modules own the wire types for downloads, manga and
 * sites.  The types for **auth**, **library**, **settings**, **health**
 * and **version** — which have no corresponding module in ``types/`` — are
 * defined here, next to the methods that produce them.  This is the same
 * pattern used by the ``parsers`` mixins on the backend: types that exist
 * only to support a single consumer live next to that consumer, not in a
 * shared folder.  When ``types/library.ts`` or ``types/auth.ts`` is
 * delivered in a future iteration, the corresponding types here will be
 * re-exported from those modules to preserve a single source of truth.
 *
 * Singleton
 * ---------
 * :func:`getApiClient` returns a process-wide :class:`NexusApiClient`.  The
 * returned instance is configured with default options, which is what the
 * app uses everywhere.  Tests and edge cases can call ``resetApiClient()``
 * followed by ``getApiClient(customOptions)`` to obtain a differently
 * configured instance.  A module-level ``api`` singleton is also exported
 * for callers that want to skip the ``getApiClient()`` call.
 */
import type {
  CreateDownloadRequest,
  CreateDownloadResponse,
  DownloadListResponse,
  DownloadStatus,
  DownloadTask,
  ListDownloadsParams,
} from "../types/download";
import type {
  ChapterPagesResponse,
  ChapterResponse,
  Manga,
  MangaChaptersResponse,
  MangaResponse,
  SearchRequest,
  SearchResponse,
  SiteHealthResponse,
  SiteListResponse,
  SiteResponse,
} from "../types/manga";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Prefix every versioned API route with this. */
const API_PREFIX = "/api/v1";

/** Health and version endpoints live outside the versioned prefix. */
const HEALTH_PATH = "/health";
const READINESS_PATH = "/health/ready";
const VERSION_PATH = "/api/version";

/** Default per-request timeout in milliseconds. */
const DEFAULT_TIMEOUT_MS = 30_000;

/** ``localStorage`` keys used by the default token callbacks. */
const ACCESS_TOKEN_STORAGE_KEY = "nexusdl_access_token";
const REFRESH_TOKEN_STORAGE_KEY = "nexusdl_refresh_token";

// ───────────────────────────────────────────────────────────────────────────
// Error codes
// ───────────────────────────────────────────────────────────────────────────

/**
 * The standard API error codes, matching the backend's error envelope
 * (see PROMPT MASTER §"Error envelope").
 *
 * The list is closed at the *type* level — a code that is not in this list
 * is still carried as a raw string by :class:`NexusApiError` so that a
 * newer backend does not break the frontend.  Use
 * :func:`isKnownErrorCode` to narrow a string to this union when the
 * distinction matters.
 */
export const API_ERROR_CODES = [
  "UNAUTHORIZED",
  "FORBIDDEN",
  "NOT_FOUND",
  "VALIDATION_ERROR",
  "RATE_LIMIT_EXCEEDED",
  "SITE_UNAVAILABLE",
  "SITE_BLOCKED",
  "PARSER_ERROR",
  "DOWNLOAD_ERROR",
  "PACKAGING_ERROR",
  "LIBRARY_ERROR",
  "CONFIG_ERROR",
  "INTERNAL_ERROR",
] as const;

/** Union of the standard API error codes. */
export type ApiErrorCode = (typeof API_ERROR_CODES)[number];

/** Return ``true`` when *value* is one of the standard error codes. */
export function isKnownErrorCode(value: unknown): value is ApiErrorCode {
  return (
    typeof value === "string" &&
    (API_ERROR_CODES as readonly string[]).includes(value)
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Error class
// ───────────────────────────────────────────────────────────────────────────

/**
 * Every API failure is normalised to this single error class.
 *
 * Callers catch :class:`NexusApiError` and switch on ``error.code``.  The
 * ``status`` and ``request_id`` fields make it easy to log the failure in
 * a form that can be correlated with the backend's log, and the ``details``
 * field carries structured data specific to the failure (e.g. a field name
 * for ``VALIDATION_ERROR``, a site id for ``SITE_UNAVAILABLE``).
 */
export class NexusApiError extends Error {
  /** The standard or raw backend error code. */
  public readonly code: string;
  /** The HTTP status code, or ``0`` for transport errors. */
  public readonly status: number;
  /** The ``error.details`` object, or ``{}`` when none was provided. */
  public readonly details: Record<string, unknown>;
  /** Server-side request id, or ``null`` when not available. */
  public readonly request_id: string | null;

  constructor(
    message: string,
    options: {
      code: string;
      status: number;
      details?: Record<string, unknown>;
      request_id?: string | null;
      cause?: unknown;
    },
  ) {
    super(message, { cause: options.cause });
    this.name = "NexusApiError";
    this.code = options.code;
    this.status = options.status;
    this.details = options.details ?? {};
    this.request_id = options.request_id ?? null;
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Wire types — auth
// ───────────────────────────────────────────────────────────────────────────

/**
 * Roles exposed by the backend, matching ``UserRole`` in
 * ``core/models/enums.py``.  ``"guest"`` is used for unauthenticated
 * visitors who access a public endpoint.
 */
export type UserRole = "guest" | "user" | "admin";

/** A user account as returned by the backend. */
export interface User {
  /** UUID4 identifier, stable across sessions. */
  id: string;
  /** Primary email address. */
  email: string;
  /** Display name shown in the UI. */
  display_name: string;
  /** Account role, controlling access to admin endpoints. */
  role: UserRole;
  /** ISO 8601 creation timestamp. */
  created_at: string;
}

/** Body for ``POST /api/v1/auth/login``. */
export interface LoginRequest {
  email: string;
  password: string;
}

/** Body for ``POST /api/v1/auth/register``. */
export interface RegisterRequest {
  email: string;
  password: string;
  display_name: string;
}

/** Body for ``POST /api/v1/auth/refresh``. */
export interface RefreshRequest {
  refresh_token: string;
}

/**
 * Response from the three auth endpoints.
 *
 * The backend uses the standard OAuth 2.0 bearer token shape.  The access
 * token is a short-lived JWT (typically 1 hour) sent on every subsequent
 * request; the refresh token is a longer-lived opaque string used to
 * obtain a new access token without re-entering credentials.
 */
export interface AuthResponse {
  /** Short-lived JWT for the ``Authorization: Bearer`` header. */
  access_token: string;
  /** Long-lived token for the ``/auth/refresh`` endpoint. */
  refresh_token: string;
  /** Always ``"bearer"``. */
  token_type: "bearer";
  /** Access token lifetime in seconds. */
  expires_in: number;
  /** The authenticated user. */
  user: User;
}

// ───────────────────────────────────────────────────────────────────────────
// Wire types — library
// ───────────────────────────────────────────────────────────────────────────

/**
 * Current reading position of a manga.
 *
 * ``page`` is 0-based within the chapter, matching :type:`Page.index` on
 * the backend.  ``updated_at`` lets the UI display "read 3 days ago" and
 * lets the backend resolve conflict-free last-write-wins during sync.
 */
export interface ReadingProgress {
  chapter_id: string;
  page: number;
  updated_at: string;
}

/**
 * A manga entry in the local library, with its library-specific metadata.
 *
 * The ``manga`` field carries the standard :type:`Manga` resource; the
 * additional fields describe the **local** state that only exists because
 * the manga has been added to the library.  Keeping the two layers nested
 * (rather than flattening the manga fields into this type) matches the
 * backend's Pydantic model and makes the field's provenance obvious.
 */
export interface LibraryEntry {
  manga: Manga;
  /** ISO 8601 timestamp of when the manga was added to the library. */
  added_at: string;
  /** ISO 8601 timestamp of the last update to this library entry. */
  updated_at: string;
  /** Current reading position, or ``null`` if never opened. */
  reading_progress: ReadingProgress | null;
  /** Number of chapters present on disk. */
  downloaded_chapters: number;
  /** Total number of chapters the source site currently exposes. */
  total_chapters: number;
  /** Absolute path to the packaged output, or ``null`` before download. */
  output_path: string | null;
  /** Total size on disk in bytes. */
  file_size_bytes: number;
}

/** Paginated list of library entries. */
export interface LibraryListResponse {
  items: LibraryEntry[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

/** Query parameters for ``GET /api/v1/library``. */
export interface ListLibraryParams {
  /** 1-based page number. */
  page?: number;
  /** Items per page (backend caps at 100). */
  per_page?: number;
  /** Free-text search across title, authors and tags. */
  query?: string;
  /** Restrict to a single site. */
  site_id?: string;
}

/** Body for ``PUT /api/v1/library/{id}/progress``. */
export interface UpdateProgressRequest {
  /** Chapter the reader is currently on. */
  chapter_id: string;
  /** 0-based page within the chapter. */
  page: number;
}

// ───────────────────────────────────────────────────────────────────────────
// Wire types — settings
// ───────────────────────────────────────────────────────────────────────────

/**
 * Server-side settings, as returned by ``GET /api/v1/settings``.
 *
 * The backend's Settings model has many sections (``app``, ``http``,
 * ``rate_limits``, ``session``, ``storage``, ``logging``, ``content``,
 * ``downloader``, ``packaging``, ``image``, ``playwright``,
 * ``flaresolverr``, ``proxy``, ``library``, ``api``, ``websocket``,
 * ``tui``, ``gui``, ``plugins``, ``performance``, ``development``).
 *
 * The frontend deliberately types this as ``Record<string, unknown>``
 * rather than mirroring the full backend model, for three reasons:
 *
 *  1. The backend validates settings on write, so the frontend does not
 *     need to re-validate on read.
 *  2. The admin settings page is the only consumer, and it reads
 *     individual sections on demand — a strict type would force the
 *     entire Settings model into the frontend bundle for the sake of a
 *     single admin page.
 *  3. The backend model evolves faster than the frontend; a loose type
 *     means a new backend section does not break the admin page.
 *
 * Consumers that need a specific section should narrow it locally with a
 * type assertion at the point of use, or with a schema validator
 * (``zod``) if the value is user-editable.
 *
 * Secrets are masked by the backend before serialization — the frontend
 * never sees passwords, API keys or JWT secrets.
 */
export type SettingsSnapshot = Record<string, unknown>;

// ───────────────────────────────────────────────────────────────────────────
// Wire types — health and version
// ───────────────────────────────────────────────────────────────────────────

/** Response from ``GET /health``. */
export interface HealthResponse {
  status: "ok" | "degraded" | "down";
  version: string;
  uptime_seconds: number;
}

/**
 * Response from ``GET /health/ready``.
 *
 * ``checks`` maps each readiness component (database, session pool,
 * Playwright pool, …) to its boolean status.  A ``false`` on any component
 * makes ``ready`` false, and the orchestration layer will not route
 * traffic to the instance.
 */
export interface ReadinessResponse {
  ready: boolean;
  checks: Record<string, boolean>;
}

/** Response from ``GET /api/version``. */
export interface VersionResponse {
  version: string;
  git_commit: string | null;
  build_date: string | null;
}

// ───────────────────────────────────────────────────────────────────────────
// Options
// ───────────────────────────────────────────────────────────────────────────

/**
 * Options accepted by :class:`NexusApiClient`.
 *
 * Every field has a sensible default (documented below and on the class
 * itself), so a fully functional client can be constructed with ``{}``.
 */
export interface NexusApiClientOptions {
  /**
   * Base URL prepended to every request path.
   *
   * ``""`` (default) makes every URL **relative**, which is the correct
   * behaviour in the browser: the Next.js rewrite and the production
   * reverse proxy both forward ``/api/*`` to the backend.  Tests and
   * non-browser environments can pass an absolute URL
   * (``"http://localhost:8000"``) to bypass the proxy.
   */
  baseUrl?: string;

  /**
   * Return the current access token, or ``null`` when not authenticated.
   *
   * Called before every request.  The default reads
   * ``localStorage["nexusdl_access_token"]``, which is the fallback used
   * before the auth store is delivered.  The auth store will override
   * this with an in-memory implementation that reads from its own state.
   */
  getToken?: () => string | null;

  /**
   * Return the current refresh token, or ``null`` when not authenticated.
   *
   * Called only when a request returns ``401`` and the client needs to
   * refresh.  The default reads
   * ``localStorage["nexusdl_refresh_token"]``.
   */
  getRefreshToken?: () => string | null;

  /**
   * Invoked after the client successfully refreshes the access token.
   *
   * The auth store uses this to update its in-memory state.  When
   * omitted, the client writes the new tokens to ``localStorage`` under
   * the default keys so that a page reload preserves the session.
   */
  onTokenRefreshed?: (response: AuthResponse) => void;

  /**
   * Invoked when the client fails to refresh and the session has become
   * invalid.  Typically wired to a logout action by the auth store.
   */
  onAuthExpired?: () => void;

  /**
   * Per-request timeout in milliseconds.  ``0`` disables the timeout.
   * Defaults to ``30_000``.  Individual requests can override this via
   * :type:`RequestOptions.timeoutMs`.
   */
  defaultTimeoutMs?: number;

  /**
   * Custom ``fetch`` implementation.  Exists for tests (inject a spy, a
   * mock, or a fake); production callers should leave it at the default,
   * which is ``window.fetch`` (or ``globalThis.fetch`` in non-browser
   * environments).
   */
  fetchImpl?: typeof fetch;
}

/**
 * Per-call options accepted by every client method.
 *
 * The client merges these with its own defaults before issuing the
 * request, so a per-call timeout always wins over the client-level one.
 */
export interface RequestOptions {
  /** AbortSignal to cancel the request.  Composed with the client timeout. */
  signal?: AbortSignal;
  /** Per-request timeout in milliseconds.  Overrides the client default. */
  timeoutMs?: number;
}

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Default ``getToken`` — reads from ``localStorage``.
 *
 * Returns ``null`` outside a browser (SSR, tests) so that the client stays
 * safe to import in any environment.  A missing token is not an error: it
 * means "not logged in", and unauthenticated endpoints (health, version,
 * auth) work normally.
 */
function defaultGetToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
  } catch {
    // ``localStorage`` can throw if the user has disabled storage for the
    // site (privacy mode, blocked third-party context, etc.).  Treat that
    // as "no token" rather than crashing every request.
    return null;
  }
}

/** Default ``getRefreshToken`` — see :func:`defaultGetToken`. */
function defaultGetRefreshToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist the tokens from an :type:`AuthResponse` to ``localStorage``.
 *
 * Used as the default ``onTokenRefreshed`` handler.  The auth store will
 * override this with an implementation that also updates in-memory state.
 */
export function persistAuthTokens(response: AuthResponse): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, response.access_token);
    window.localStorage.setItem(
      REFRESH_TOKEN_STORAGE_KEY,
      response.refresh_token,
    );
  } catch {
    // Ignore — the tokens still work for the current page session; a
    // subsequent reload will simply lose the session.
  }
}

/**
 * Clear both tokens from ``localStorage``.
 *
 * Called by the auth layer on explicit logout, and by the API client when
 * a refresh fails and the session is unrecoverable.
 */
export function clearAuthTokens(): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
    window.localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY);
  } catch {
    // Ignore — see ``persistAuthTokens``.
  }
}

/**
 * Encode *params* as a query string.
 *
 * Arrays become repeated parameters (``?status=running&status=pending``),
 * which is what ``httpx`` and ``FastAPI`` expect for list-typed query
 * parameters.  ``undefined`` and ``null`` values are omitted so that
 * callers can pass a partially-populated params object without filtering
 * it beforehand.
 */
function buildQueryString(params: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) {
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== undefined && item !== null) {
          parts.push(
            `${encodeURIComponent(key)}=${encodeURIComponent(String(item))}`,
          );
        }
      }
    } else if (
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    ) {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
    // Objects and functions are not serialised — endpoints that need them
    // use POST bodies, not query strings.
  }
  return parts.length > 0 ? `?${parts.join("&")}` : "";
}

/**
 * Attempt to parse an error envelope from a response body.
 *
 * The backend's error envelope is::
 *
 *     { "error": { "code": "...", "message": "...", "details": {}, "request_id": "..." } }
 *
 * This helper returns the parsed fields or ``null`` when the body is not
 * an error envelope — the caller then falls back to a generic error with
 * the response status text as the message.
 */
function parseErrorEnvelope(
  body: unknown,
): { code: string; message: string; details: Record<string, unknown>; request_id: string | null } | null {
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const envelope = body as { error?: unknown };
  const error = envelope.error;
  if (typeof error !== "object" || error === null) {
    return null;
  }
  const e = error as Record<string, unknown>;
  const code = typeof e.code === "string" ? e.code : null;
  const message = typeof e.message === "string" ? e.message : null;
  if (code === null || message === null) {
    return null;
  }
  const details =
    typeof e.details === "object" && e.details !== null
      ? (e.details as Record<string, unknown>)
      : {};
  const request_id = typeof e.request_id === "string" ? e.request_id : null;
  return { code, message, details, request_id };
}

// ───────────────────────────────────────────────────────────────────────────
// Client
// ───────────────────────────────────────────────────────────────────────────

/**
 * The NexusDL API client.
 *
 * See the module docstring for the URL derivation, authentication, error
 * handling and singleton policies.
 */
export class NexusApiClient {
  private readonly _baseUrl: string;
  private readonly _getToken: () => string | null;
  private readonly _getRefreshToken: () => string | null;
  private readonly _onTokenRefreshed: (response: AuthResponse) => void;
  private readonly _onAuthExpired: (() => void) | null;
  private readonly _defaultTimeoutMs: number;
  private readonly _fetch: typeof fetch;

  /**
   * Promise of an in-flight refresh attempt, or ``null``.
   *
   * Used to coalesce concurrent 401s so that only one ``/auth/refresh``
   * request is sent even when several requests fail simultaneously.
   */
  private _refreshPromise: Promise<boolean> | null = null;

  constructor(options: NexusApiClientOptions = {}) {
    this._baseUrl = (options.baseUrl ?? "").replace(/\/+$/, "");
    this._getToken = options.getToken ?? defaultGetToken;
    this._getRefreshToken = options.getRefreshToken ?? defaultGetRefreshToken;
    this._onTokenRefreshed = options.onTokenRefreshed ?? persistAuthTokens;
    this._onAuthExpired = options.onAuthExpired ?? null;
    this._defaultTimeoutMs = options.defaultTimeoutMs ?? DEFAULT_TIMEOUT_MS;
    this._fetch =
      options.fetchImpl ??
      ((input, init) => globalThis.fetch(input, init));
  }

  // ====================================================================
  //  Auth
  // ====================================================================

  /** ``POST /api/v1/auth/login``. */
  async login(
    request: LoginRequest,
    options?: RequestOptions,
  ): Promise<AuthResponse> {
    return this._request<AuthResponse>(
      "POST",
      `${API_PREFIX}/auth/login`,
      request,
      options,
      { skipAuth: true, skipRefresh: true },
    );
  }

  /** ``POST /api/v1/auth/register``. */
  async register(
    request: RegisterRequest,
    options?: RequestOptions,
  ): Promise<AuthResponse> {
    return this._request<AuthResponse>(
      "POST",
      `${API_PREFIX}/auth/register`,
      request,
      options,
      { skipAuth: true, skipRefresh: true },
    );
  }

  /** ``POST /api/v1/auth/refresh``. */
  async refresh(
    request: RefreshRequest,
    options?: RequestOptions,
  ): Promise<AuthResponse> {
    return this._request<AuthResponse>(
      "POST",
      `${API_PREFIX}/auth/refresh`,
      request,
      options,
      { skipAuth: true, skipRefresh: true },
    );
  }

  // ====================================================================
  //  Health and version
  // ====================================================================

  /** ``GET /health``. */
  async getHealth(options?: RequestOptions): Promise<HealthResponse> {
    return this._request<HealthResponse>("GET", HEALTH_PATH, undefined, options, {
      skipAuth: true,
      skipRefresh: true,
    });
  }

  /** ``GET /health/ready``. */
  async getReadiness(options?: RequestOptions): Promise<ReadinessResponse> {
    return this._request<ReadinessResponse>(
      "GET",
      READINESS_PATH,
      undefined,
      options,
      { skipAuth: true, skipRefresh: true },
    );
  }

  /** ``GET /api/version``. */
  async getVersion(options?: RequestOptions): Promise<VersionResponse> {
    return this._request<VersionResponse>(
      "GET",
      VERSION_PATH,
      undefined,
      options,
      { skipAuth: true, skipRefresh: true },
    );
  }

  // ====================================================================
  //  Sites
  // ====================================================================

  /** ``GET /api/v1/sites``. */
  async getSites(options?: RequestOptions): Promise<SiteListResponse> {
    return this._request<SiteListResponse>(
      "GET",
      `${API_PREFIX}/sites`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/sites/{id}``. */
  async getSite(id: string, options?: RequestOptions): Promise<SiteResponse> {
    return this._request<SiteResponse>(
      "GET",
      `${API_PREFIX}/sites/${encodeURIComponent(id)}`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/sites/{id}/health``. */
  async getSiteHealth(
    id: string,
    options?: RequestOptions,
  ): Promise<SiteHealthResponse> {
    return this._request<SiteHealthResponse>(
      "GET",
      `${API_PREFIX}/sites/${encodeURIComponent(id)}/health`,
      undefined,
      options,
    );
  }

  // ====================================================================
  //  Search
  // ====================================================================

  /** ``POST /api/v1/search``. */
  async search(
    request: SearchRequest,
    options?: RequestOptions,
  ): Promise<SearchResponse> {
    return this._request<SearchResponse>(
      "POST",
      `${API_PREFIX}/search`,
      request,
      options,
    );
  }

  // ====================================================================
  //  Manga and chapters
  // ====================================================================

  /** ``GET /api/v1/manga/{id}``. */
  async getManga(id: string, options?: RequestOptions): Promise<MangaResponse> {
    return this._request<MangaResponse>(
      "GET",
      `${API_PREFIX}/manga/${encodeURIComponent(id)}`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/manga/{id}/chapters``. */
  async getMangaChapters(
    id: string,
    options?: RequestOptions,
  ): Promise<MangaChaptersResponse> {
    return this._request<MangaChaptersResponse>(
      "GET",
      `${API_PREFIX}/manga/${encodeURIComponent(id)}/chapters`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/chapters/{id}``. */
  async getChapter(
    id: string,
    options?: RequestOptions,
  ): Promise<ChapterResponse> {
    return this._request<ChapterResponse>(
      "GET",
      `${API_PREFIX}/chapters/${encodeURIComponent(id)}`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/chapters/{id}/pages``. */
  async getChapterPages(
    id: string,
    options?: RequestOptions,
  ): Promise<ChapterPagesResponse> {
    return this._request<ChapterPagesResponse>(
      "GET",
      `${API_PREFIX}/chapters/${encodeURIComponent(id)}/pages`,
      undefined,
      options,
    );
  }

  // ====================================================================
  //  Downloads
  // ====================================================================

  /** ``POST /api/v1/download`` (HTTP 202). */
  async createDownload(
    request: CreateDownloadRequest,
    options?: RequestOptions,
  ): Promise<CreateDownloadResponse> {
    return this._request<CreateDownloadResponse>(
      "POST",
      `${API_PREFIX}/download`,
      request,
      options,
    );
  }

  /** ``GET /api/v1/download/{task_id}``. */
  async getDownload(
    taskId: string,
    options?: RequestOptions,
  ): Promise<DownloadTask> {
    return this._request<DownloadTask>(
      "GET",
      `${API_PREFIX}/download/${encodeURIComponent(taskId)}`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/download``. */
  async listDownloads(
    params: ListDownloadsParams = {},
    options?: RequestOptions,
  ): Promise<DownloadListResponse> {
    const query = buildQueryString({
      status: params.status,
      manga_id: params.manga_id,
      site_id: params.site_id,
      page: params.page,
      per_page: params.per_page,
    });
    return this._request<DownloadListResponse>(
      "GET",
      `${API_PREFIX}/download${query}`,
      undefined,
      options,
    );
  }

  /** ``POST /api/v1/download/{task_id}/pause``. */
  async pauseDownload(
    taskId: string,
    options?: RequestOptions,
  ): Promise<void> {
    await this._request<void>(
      "POST",
      `${API_PREFIX}/download/${encodeURIComponent(taskId)}/pause`,
      undefined,
      options,
    );
  }

  /** ``POST /api/v1/download/{task_id}/resume``. */
  async resumeDownload(
    taskId: string,
    options?: RequestOptions,
  ): Promise<void> {
    await this._request<void>(
      "POST",
      `${API_PREFIX}/download/${encodeURIComponent(taskId)}/resume`,
      undefined,
      options,
    );
  }

  /** ``DELETE /api/v1/download/{task_id}`` — cancel the task. */
  async cancelDownload(
    taskId: string,
    options?: RequestOptions,
  ): Promise<void> {
    await this._request<void>(
      "DELETE",
      `${API_PREFIX}/download/${encodeURIComponent(taskId)}`,
      undefined,
      options,
    );
  }

  // ====================================================================
  //  Library
  // ====================================================================

  /** ``GET /api/v1/library``. */
  async getLibrary(
    params: ListLibraryParams = {},
    options?: RequestOptions,
  ): Promise<LibraryListResponse> {
    const query = buildQueryString({
      page: params.page,
      per_page: params.per_page,
      query: params.query,
      site_id: params.site_id,
    });
    return this._request<LibraryListResponse>(
      "GET",
      `${API_PREFIX}/library${query}`,
      undefined,
      options,
    );
  }

  /** ``GET /api/v1/library/{id}``. */
  async getLibraryEntry(
    mangaId: string,
    options?: RequestOptions,
  ): Promise<LibraryEntry> {
    return this._request<LibraryEntry>(
      "GET",
      `${API_PREFIX}/library/${encodeURIComponent(mangaId)}`,
      undefined,
      options,
    );
  }

  /** ``PUT /api/v1/library/{id}/progress``. */
  async updateReadingProgress(
    mangaId: string,
    request: UpdateProgressRequest,
    options?: RequestOptions,
  ): Promise<LibraryEntry> {
    return this._request<LibraryEntry>(
      "PUT",
      `${API_PREFIX}/library/${encodeURIComponent(mangaId)}/progress`,
      request,
      options,
    );
  }

  /** ``DELETE /api/v1/library/{id}``. */
  async deleteLibraryEntry(
    mangaId: string,
    options?: RequestOptions,
  ): Promise<void> {
    await this._request<void>(
      "DELETE",
      `${API_PREFIX}/library/${encodeURIComponent(mangaId)}`,
      undefined,
      options,
    );
  }

  // ====================================================================
  //  Settings (admin)
  // ====================================================================

  /** ``GET /api/v1/settings`` — secrets masked by the backend. */
  async getSettings(options?: RequestOptions): Promise<SettingsSnapshot> {
    return this._request<SettingsSnapshot>(
      "GET",
      `${API_PREFIX}/settings`,
      undefined,
      options,
    );
  }

  /** ``PATCH /api/v1/settings`` — partial update, ADMIN only. */
  async updateSettings(
    patch: SettingsSnapshot,
    options?: RequestOptions,
  ): Promise<SettingsSnapshot> {
    return this._request<SettingsSnapshot>(
      "PATCH",
      `${API_PREFIX}/settings`,
      patch,
      options,
    );
  }

  // ====================================================================
  //  Internal — request plumbing
  // ====================================================================

  /**
   * Core request method.  Every public method delegates here.
   *
   * Responsibilities, in order:
   *
   *  1. Build the URL (base + path) and the headers (auth, content-type,
   *     accept).
   *  2. Compose a timeout signal with the caller's ``signal``, if any.
   *  3. Issue the ``fetch`` call, catching transport errors.
   *  4. On ``401`` (and when ``skipRefresh`` is false), attempt a
   *     single-flight refresh and retry **once**.
   *  5. On any other non-2xx status, parse the error envelope and throw
   *     :class:`NexusApiError`.
   *  6. On success, parse the JSON body (or return ``undefined`` for
   *     ``204 No Content``).
   */
  private async _request<T>(
    method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
    path: string,
    body: unknown,
    options: RequestOptions | undefined,
    flags: { skipAuth?: boolean; skipRefresh?: boolean } = {},
  ): Promise<T> {
    const url = `${this._baseUrl}${path}`;

    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (!flags.skipAuth) {
      const token = this._getToken();
      if (token !== null && token.length > 0) {
        headers.Authorization = `Bearer ${token}`;
      }
    }

    const response = await this._fetchWithTimeout(
      url,
      {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      },
      options,
    );

    // 401 handling — attempt a single refresh and retry once.
    if (
      response.status === 401 &&
      !flags.skipRefresh &&
      !flags.skipAuth
    ) {
      const refreshed = await this._tryRefresh();
      if (refreshed) {
        // Retry the original request with the new token.  Mark the retry
        // so that a second 401 does not trigger another refresh.
        return this._request<T>(method, path, body, options, {
          skipAuth: flags.skipAuth,
          skipRefresh: true,
        });
      }
      // Refresh failed — clear tokens and notify the auth layer.
      clearAuthTokens();
      this._onAuthExpired?.();
      throw new NexusApiError(
        "Session expired.  Please log in again.",
        {
          code: "UNAUTHORIZED",
          status: 401,
          request_id: response.headers.get("X-Request-ID"),
        },
      );
    }

    if (!response.ok) {
      const errorBody = await this._safeJson(response);
      const envelope = parseErrorEnvelope(errorBody);
      if (envelope !== null) {
        throw new NexusApiError(envelope.message, {
          code: envelope.code,
          status: response.status,
          details: envelope.details,
          request_id:
            envelope.request_id ?? response.headers.get("X-Request-ID"),
        });
      }
      // No parseable envelope — fall back to the status text.
      throw new NexusApiError(
        `HTTP ${response.status} ${response.statusText || "error"}`,
        {
          code: "INTERNAL_ERROR",
          status: response.status,
          request_id: response.headers.get("X-Request-ID"),
        },
      );
    }

    // 204 No Content — no body to parse.
    if (response.status === 204) {
      return undefined as T;
    }

    // 202 Accepted without a JSON body is a protocol violation; treat
    // an empty body the same as 204 rather than failing on JSON parse.
    const text = await response.text();
    if (text.length === 0) {
      return undefined as T;
    }

    try {
      return JSON.parse(text) as T;
    } catch (err) {
      throw new NexusApiError(
        `Invalid JSON response from ${method} ${path}: ${
          err instanceof Error ? err.message : String(err)
        }`,
        {
          code: "INTERNAL_ERROR",
          status: response.status,
          request_id: response.headers.get("X-Request-ID"),
          cause: err,
        },
      );
    }
  }

  /**
   * Issue a ``fetch`` call, catching transport errors and applying the
   * timeout.
   *
   * Combines the caller's ``signal`` and the client-level timeout with
   * an internal ``AbortController``.  This keeps the code portable across
   * environments where ``AbortSignal.any`` may not be available.
   */
  private async _fetchWithTimeout(
    url: string,
    init: RequestInit,
    options: RequestOptions | undefined,
  ): Promise<Response> {
    const timeoutMs =
      options?.timeoutMs ?? this._defaultTimeoutMs;
    const controller = new AbortController();

    // Propagate the caller's abort signal, if any, into the controller.
    if (options?.signal !== undefined) {
      if (options.signal.aborted) {
        controller.abort(options.signal.reason);
      } else {
        options.signal.addEventListener(
          "abort",
          () => controller.abort(options.signal?.reason),
          { once: true },
        );
      }
    }

    let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
    if (timeoutMs > 0) {
      timeoutHandle = setTimeout(() => {
        controller.abort(
          new Error(`Request timed out after ${timeoutMs} ms`),
        );
      }, timeoutMs);
    }

    try {
      return await this._fetch(url, {
        ...init,
        signal: controller.signal,
      });
    } catch (err) {
      // The fetch API throws a DOMException with name ``AbortError`` when
      // the signal aborts; a genuine network failure throws a TypeError.
      // Both are surfaced as ``NexusApiError`` so that callers can catch a
      // single class.
      const isAbort =
        err instanceof DOMException && err.name === "AbortError";
      throw new NexusApiError(
        isAbort
          ? `Request to ${url} was aborted`
          : `Network error while requesting ${url}: ${
              err instanceof Error ? err.message : String(err)
            }`,
        {
          code: "INTERNAL_ERROR",
          status: 0,
          cause: err,
        },
      );
    } finally {
      if (timeoutHandle !== null) {
        clearTimeout(timeoutHandle);
      }
    }
  }

  /**
   * Attempt to refresh the access token, coalescing concurrent attempts.
   *
   * Returns ``true`` on success (the new tokens have already been
   * persisted via ``onTokenRefreshed``), ``false`` on failure.  Never
   * throws — the caller decides what to do with the failure (typically:
   * propagate a 401).
   */
  private _tryRefresh(): Promise<boolean> {
    if (this._refreshPromise !== null) {
      return this._refreshPromise;
    }
    this._refreshPromise = this._doRefresh();
    return this._refreshPromise.finally(() => {
      this._refreshPromise = null;
    });
  }

  private async _doRefresh(): Promise<boolean> {
    const refreshToken = this._getRefreshToken();
    if (refreshToken === null || refreshToken.length === 0) {
      return false;
    }
    try {
      const response = await this.refresh({ refresh_token: refreshToken });
      this._onTokenRefreshed(response);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Attempt to parse *response* as JSON, returning ``null`` on failure.
   *
   * Used on error responses so that the client can distinguish "the
   * server sent a parseable envelope" from "the server sent HTML or
   * nothing".  Never throws.
   */
  private async _safeJson(response: Response): Promise<unknown> {
    try {
      const text = await response.text();
      if (text.length === 0) {
        return null;
      }
      return JSON.parse(text);
    } catch {
      return null;
    }
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Singleton
// ───────────────────────────────────────────────────────────────────────────

let _singleton: NexusApiClient | null = null;

/**
 * Return the process-wide :class:`NexusApiClient`.
 *
 * On first call, constructs the singleton with *options* (or the defaults).
 * On subsequent calls, returns the existing instance **and ignores
 * *options*** — a running client cannot be reconfigured without a
 * ``resetApiClient()`` call first.  This is deliberate: silently applying
 * new options would surprise the caller and could leave requests in flight
 * with a mixed configuration.
 *
 * The singleton is module-scoped, so it survives React Strict Mode's
 * double-mount and HMR re-renders within a single page session.  Tests
 * that need a fresh instance call :func:`resetApiClient` first.
 */
export function getApiClient(
  options?: NexusApiClientOptions,
): NexusApiClient {
  if (_singleton === null) {
    _singleton = new NexusApiClient(options ?? {});
  }
  return _singleton;
}

/**
 * Discard the current singleton.
 *
 * Safe to call when no singleton exists.  Idempotent.  Use this in tests
 * (before constructing a client with custom options) and in logout flows
 * (before the auth store re-initialises).
 */
export function resetApiClient(): void {
  _singleton = null;
}

/**
 * Convenience default instance — equivalent to calling ``getApiClient()``
 * with no options.
 *
 * Exported so that callers can write ``api.getManga(id)`` instead of
 * ``getApiClient().getManga(id)``.  The instance is constructed lazily
 * (the constructor performs no I/O), so importing this module in an SSR
 * context is safe.
 */
export const api: NexusApiClient = getApiClient();
