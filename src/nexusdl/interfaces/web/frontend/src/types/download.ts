/**
 * Download domain types — wire format for the NexusDL REST API and
 * WebSocket events.
 *
 * Every interface here mirrors a corresponding Pydantic schema in
 * ``src/nexusdl/interfaces/web/backend/schemas/{requests,responses}.py``
 * field-for-field.  Field names are ``snake_case`` because this is the JSON
 * wire format — the frontend deliberately does **not** camelCase the API
 * payloads, so that a single glance at a network tab or a log line matches
 * the backend definition verbatim.
 *
 * Enums are modelled as **string literal unions**, not TypeScript
 * ``enum``s.  Reasons:
 *
 *  - Literal unions are structurally typed: any JSON payload that contains
 *    ``"running"`` is assignable to ``DownloadStatus`` without a cast.
 *  - They tree-shake cleanly: the values only exist at the call sites that
 *    actually compare against them.
 *  - They interop with the ``PACKAGING_FORMATS`` / ``DOWNLOAD_STATUSES``
 *    tuples below, which are the source of truth for UI dropdowns and
 *    validation.
 *
 * The numeric priority values (``LOW = 0``, ``NORMAL = 1``, ``HIGH = 2``)
 * are exposed as ``PRIORITY_VALUES`` so that sort keys and queue
 * visualisation can reproduce the backend ordering without duplicating the
 * mapping.  The wire format uses the string form (``"low"``, ``"normal"``,
 * ``"high"``) — Pydantic serialises the enum by value, and the backend
 * declares those values as lowercase strings.
 *
 * Timestamps are ISO 8601 strings.  The frontend never parses them at the
 * type level; ``date-fns`` handles formatting at render time.  Using
 * ``string`` (rather than a branded ``IsoDateTime`` type) keeps every
 * interface trivially compatible with ``JSON.parse``'s output.
 *
 * UUIDs (``DownloadTask.id``) and 16-hex identifiers
 * (``manga_id``, ``chapter_id``) are all ``string`` on the wire.  The
 * distinction is documentary only — both are opaque to the frontend.
 */

// ───────────────────────────────────────────────────────────────────────────
// Enumerations
// ───────────────────────────────────────────────────────────────────────────

/**
 * Lifecycle state of a download task.
 *
 * Mirrors ``DownloadStatus`` in ``core/models/enums.py``.  The state machine
 * is documented in the PROMPT MASTER:
 *
 * ```
 * PENDING ──► RUNNING ──► DONE
 *    │           │
 *    │           ├──► PAUSED ──► RUNNING
 *    │           │
 *    │           └──► FAILED
 *    │
 *    └──► CANCELLED
 * ```
 *
 * ``done``, ``failed`` and ``cancelled`` are terminal.  Every other
 * transition raises ``InvalidStateTransitionError`` on the backend.
 */
export type DownloadStatus =
  | "pending"
  | "running"
  | "paused"
  | "done"
  | "failed"
  | "cancelled";

/**
 * Task priority.  Mirrors ``Priority`` in ``core/models/enums.py``.
 * Numeric ordering is exposed via ``PRIORITY_VALUES``.
 */
export type DownloadPriority = "low" | "normal" | "high";

/**
 * Output packaging format.  Mirrors ``PackagingFormat`` in
 * ``core/models/enums.py``.  There is no ``cbr`` — CBR is deliberately not
 * supported (ADR-002); users who need it can convert a CBZ externally.
 */
export type PackagingFormat =
  | "folder"
  | "zip"
  | "cbz"
  | "cb7"
  | "pdf"
  | "cbt";

// ───────────────────────────────────────────────────────────────────────────
// Canonical value lists and lookup tables
// ───────────────────────────────────────────────────────────────────────────

/** Every value of :type:`DownloadStatus`, in state-machine order. */
export const DOWNLOAD_STATUSES: readonly DownloadStatus[] = [
  "pending",
  "running",
  "paused",
  "done",
  "failed",
  "cancelled",
] as const;

/** Statuses after which a task will never transition again. */
export const TERMINAL_STATUSES: readonly DownloadStatus[] = [
  "done",
  "failed",
  "cancelled",
] as const;

/** Statuses in which a task is still expected to make progress. */
export const ACTIVE_STATUSES: readonly DownloadStatus[] = [
  "pending",
  "running",
  "paused",
] as const;

/** Every value of :type:`DownloadPriority`, ordered low → high. */
export const DOWNLOAD_PRIORITIES: readonly DownloadPriority[] = [
  "low",
  "normal",
  "high",
] as const;

/** Numeric values matching the Python ``Priority`` enum. */
export const PRIORITY_VALUES: Readonly<Record<DownloadPriority, 0 | 1 | 2>> = {
  low: 0,
  normal: 1,
  high: 2,
} as const;

/** Every value of :type:`PackagingFormat`, in menu order. */
export const PACKAGING_FORMATS: readonly PackagingFormat[] = [
  "folder",
  "zip",
  "cbz",
  "cb7",
  "pdf",
  "cbt",
] as const;

// ───────────────────────────────────────────────────────────────────────────
// Type guards
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return ``true`` when *value* is a valid :type:`DownloadStatus`.
 *
 * Used to validate untrusted WS payloads and API responses before they
 * reach the state store, so that a backend bug cannot corrupt the
 * frontend's status-derived UI logic.
 */
export function isDownloadStatus(value: unknown): value is DownloadStatus {
  return (
    typeof value === "string" &&
    (DOWNLOAD_STATUSES as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *value* is a valid :type:`DownloadPriority`. */
export function isDownloadPriority(
  value: unknown,
): value is DownloadPriority {
  return (
    typeof value === "string" &&
    (DOWNLOAD_PRIORITIES as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *value* is a valid :type:`PackagingFormat`. */
export function isPackagingFormat(value: unknown): value is PackagingFormat {
  return (
    typeof value === "string" &&
    (PACKAGING_FORMATS as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *status* is a terminal state. */
export function isTerminalStatus(status: DownloadStatus): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

/** Return ``true`` when *status* is an active (non-terminal) state. */
export function isActiveStatus(status: DownloadStatus): boolean {
  return (ACTIVE_STATUSES as readonly string[]).includes(status);
}

/**
 * Normalise an untrusted priority value from the wire.
 *
 * The backend serialises priorities as lowercase strings, but some legacy
 * deployments and third-party tools emit the numeric form (``0``, ``1``,
 * ``2``).  This helper accepts both and returns the canonical string, or
 * ``null`` when the value cannot be interpreted.  Callers should treat
 * ``null`` as "unknown priority" and display it without a rank badge rather
 * than silently defaulting to ``"normal"``.
 */
export function normalizePriority(
  value: unknown,
): DownloadPriority | null {
  if (isDownloadPriority(value)) {
    return value;
  }
  if (typeof value === "number") {
    if (value === 0) return "low";
    if (value === 1) return "normal";
    if (value === 2) return "high";
  }
  return null;
}

// ───────────────────────────────────────────────────────────────────────────
// Domain entities
// ───────────────────────────────────────────────────────────────────────────

/**
 * Live progress counters for a download task.
 *
 * These are cumulative over the whole task's lifetime; pausing and resuming
 * does not reset them.  Every counter is a non-negative integer, and
 * ``*_completed + *_failed`` never exceeds ``*_total`` — a backend invariant
 * that the frontend relies on for progress-bar widths.
 */
export interface DownloadProgress {
  /** Number of chapters the task is expected to process. */
  chapters_total: number;
  /** Number of chapters successfully completed so far. */
  chapters_completed: number;
  /** Number of chapters that failed (permanently) so far. */
  chapters_failed: number;

  /** Total page count across all chapters, when known upfront. */
  pages_total: number;
  /** Number of pages successfully downloaded so far. */
  pages_completed: number;
  /** Number of pages that failed (permanently) so far. */
  pages_failed: number;

  /** Total payload size in bytes, when known upfront. */
  bytes_total: number;
  /** Bytes downloaded so far. */
  bytes_downloaded: number;

  /** Current download speed in bytes per second; ``0`` when idle. */
  speed_bps: number;
  /**
   * Estimated seconds remaining, or ``null`` when the estimator cannot yet
   * produce a stable value (e.g. before the first page completes).
   */
  eta_seconds: number | null;

  /** Chapter currently being processed, or ``null`` between chapters. */
  current_chapter_id: string | null;
  /** Number of the chapter currently being processed, if known. */
  current_chapter_number: number | null;
  /** 0-based index of the page currently being downloaded, if any. */
  current_page: number | null;
}

/**
 * Structured error attached to a failed download task.
 *
 * Mirrors the ``error`` block of the API error envelope
 * (``{error: {code, message, details, request_id}}``) but scoped to a
 * download.  When a *chapter* fails inside an otherwise-running task, the
 * task's top-level ``error`` stays ``null`` and the failure is recorded in
 * ``DownloadResult.chapter_errors``; only a task-level failure — the whole
 * batch — populates this field.
 */
export interface DownloadError {
  /** Machine-readable error code, e.g. ``"DOWNLOAD_ERROR"``. */
  code: string;
  /** Human-readable error message. */
  message: string;
  /** Structured diagnostic details; empty object when the backend has none. */
  details: Record<string, unknown>;
  /** Identifier the caller can correlate with a server-side log entry. */
  request_id: string | null;
}

/**
 * Outcome of a task that has reached a terminal state.
 *
 * Present when ``status`` is ``"done"`` or ``"failed"``; ``null`` otherwise.
 * ``done`` tasks may still carry ``chapter_errors`` — a chapter that fails
 * does not fail the batch unless the batch-level policy makes it terminal,
 * which the backend encodes into ``status``.
 */
export interface DownloadResult {
  /** Archive paths produced by the task, one per successfully packaged chapter. */
  output_paths: string[];
  /** Total chapters the task attempted to process. */
  chapters_total: number;
  /** Chapters successfully packaged. */
  chapters_completed: number;
  /** Chapters that failed and were skipped. */
  chapters_failed: number;
  /** Total pages successfully downloaded across all chapters. */
  pages_completed: number;
  /** Total pages that failed across all chapters. */
  pages_failed: number;
  /** Total bytes downloaded across all chapters. */
  bytes_downloaded: number;
  /** Wall-clock duration of the task, in seconds. */
  duration_seconds: number;
  /** Per-chapter errors, keyed by ``chapter_id``.  Empty when none failed. */
  chapter_errors: Record<string, DownloadError>;
}

/**
 * A single download task.
 *
 * A task corresponds to a **manga download** — one call to
 * ``POST /api/v1/download`` produces one task.  Internally the worker
 * processes one chapter at a time, and :type:`DownloadProgress` aggregates
 * the per-chapter state.  The state machine applies to the task as a
 * whole; individual chapter failures are recorded in
 * ``result.chapter_errors`` rather than flipping the task to ``failed``
 * unless every chapter fails.
 */
export interface DownloadTask {
  /** Server-assigned task identifier (UUID4). */
  id: string;
  /** The manga being downloaded (16-hex identifier). */
  manga_id: string;
  /** Human-readable manga title, pre-resolved by the backend. */
  manga_title: string;
  /** The site the manga belongs to. */
  site_id: string;
  /** Canonical manga URL on the source site. */
  source_url: string;

  /** Current lifecycle state. */
  status: DownloadStatus;
  /** Queue priority, echoed back from the request. */
  priority: DownloadPriority;
  /** Packaging format, echoed back from the request. */
  packaging_format: PackagingFormat;

  /**
   * Chapter identifiers the task will process, in chronological order.
   * ``null`` means "every chapter of the manga as of task creation time".
   * The list is materialised on the backend when the task is created so
   * that a chapter published after task submission is not silently
   * included.
   */
  chapter_ids: string[] | null;
  /** When ``true``, chapters already present on disk are skipped. */
  skip_existing: boolean;
  /** Resolved output directory, or ``null`` when using the default. */
  output_dir: string | null;

  /** Live progress counters (see :type:`DownloadProgress`). */
  progress: DownloadProgress;

  /** ISO 8601 creation timestamp. */
  created_at: string;
  /** ISO 8601 timestamp of the first ``running`` transition, or ``null``. */
  started_at: string | null;
  /** ISO 8601 timestamp of the terminal transition, or ``null``. */
  finished_at: string | null;

  /** Present iff ``status`` is terminal; ``null`` otherwise. */
  result: DownloadResult | null;
  /** Present iff the *task* failed; per-chapter errors live in ``result``. */
  error: DownloadError | null;
}

// ───────────────────────────────────────────────────────────────────────────
// Requests
// ───────────────────────────────────────────────────────────────────────────

/**
 * Body for ``POST /api/v1/download``.
 *
 * Only ``manga_id`` is required.  Every other field defaults to the value
 * documented in ``data/default_config.yaml`` on the backend:
 *
 *  - ``chapter_ids`` omitted or ``null`` → download every chapter.
 *  - ``packaging_format`` omitted → ``"cbz"``.
 *  - ``priority`` omitted → ``"normal"``.
 *  - ``skip_existing`` omitted → ``true``.
 *  - ``output_dir`` omitted → ``null`` (use the backend's default).
 */
export interface CreateDownloadRequest {
  manga_id: string;
  chapter_ids?: string[] | null;
  packaging_format?: PackagingFormat;
  priority?: DownloadPriority;
  skip_existing?: boolean;
  output_dir?: string | null;
}

/**
 * Query parameters for ``GET /api/v1/download``.
 *
 * All fields are optional; the backend applies defaults consistent with
 * ``GET /api/v1/library``: ``page=1``, ``per_page=20``.  ``per_page`` is
 * capped at 100 server-side — the type permits any positive integer but
 * the backend clamps it.
 */
export interface ListDownloadsParams {
  /** Filter by one or more statuses.  Empty / omitted means "all". */
  status?: DownloadStatus[];
  /** Filter by manga. */
  manga_id?: string;
  /** Filter by site. */
  site_id?: string;
  /** 1-based page number. */
  page?: number;
  /** Items per page (backend caps at 100). */
  per_page?: number;
}

// ───────────────────────────────────────────────────────────────────────────
// Responses
// ───────────────────────────────────────────────────────────────────────────

/**
 * Response for ``POST /api/v1/download`` — HTTP 202 Accepted.
 *
 * The backend creates the task and returns its id immediately; callers
 * observe the task's evolution through ``GET /api/v1/download/{task_id}``
 * or through the ``/ws/v1/events`` WebSocket.
 */
export interface CreateDownloadResponse {
  task_id: string;
}

/**
 * Response for ``GET /api/v1/download``.
 *
 * Follows the project-wide pagination envelope
 * (``items/total/page/per_page/total_pages/has_next/has_prev``).
 */
export interface DownloadListResponse {
  items: DownloadTask[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

// ───────────────────────────────────────────────────────────────────────────
// WebSocket event payloads
// ───────────────────────────────────────────────────────────────────────────

/**
 * Payload of the ``download.started`` event.
 *
 * Emitted once when a task transitions from ``pending`` to ``running``.
 * The full task snapshot is included so that a client that missed the
 * create response can populate its store without a follow-up REST call.
 */
export interface DownloadStartedPayload {
  task: DownloadTask;
}

/**
 * Payload of the ``download.progress`` event.
 *
 * Emitted whenever the task's :type:`DownloadProgress` changes in a
 * user-visible way — typically once per page download, plus on
 * chapter transitions.  The event carries only the counters that changed
 * since the last ``download.progress``; the type is nevertheless a full
 * snapshot so that a client that misses an intermediate event converges
 * correctly on the next one.
 */
export interface DownloadProgressPayload {
  task_id: string;
  progress: DownloadProgress;
}

/**
 * Payload of the ``download.completed`` event.
 *
 * Emitted exactly once, when a task reaches ``done``.  The ``result`` is
 * identical to the one on the task resource, so a client that missed the
 * final REST poll can update its state from this event alone.
 */
export interface DownloadCompletedPayload {
  task_id: string;
  result: DownloadResult;
}

/**
 * Payload of the ``download.failed`` event.
 *
 * Emitted exactly once, when a task reaches ``failed``.  Per-chapter
 * failures inside a still-running task are **not** reported as task
 * failures; they surface through ``download.progress`` and through the
 * final ``download.completed`` result.
 */
export interface DownloadFailedPayload {
  task_id: string;
  error: DownloadError;
}

/** Payload of the ``download.paused`` event. */
export interface DownloadPausedPayload {
  task_id: string;
}

/** Payload of the ``download.resumed`` event. */
export interface DownloadResumedPayload {
  task_id: string;
}

/** Payload of the ``download.cancelled`` event. */
export interface DownloadCancelledPayload {
  task_id: string;
}

/**
 * Payload of the ``chapter.started`` event.
 *
 * Emitted when the worker begins downloading a chapter.  ``task_id``
 * identifies the parent task; ``chapter_id`` and ``chapter_number``
 * identify the chapter within that task.
 */
export interface ChapterStartedPayload {
  task_id: string;
  chapter_id: string;
  chapter_number: number | null;
}

/**
 * Payload of the ``chapter.completed`` event.
 *
 * Emitted after a chapter has been downloaded, validated and packaged
 * successfully.  ``output_path`` is the packaged archive path (or the
 * chapter directory, when ``packaging_format === "folder"``).
 */
export interface ChapterCompletedPayload {
  task_id: string;
  chapter_id: string;
  chapter_number: number | null;
  output_path: string;
}

/**
 * Payload of the ``chapter.failed`` event.
 *
 * Emitted after a chapter's download or packaging failed permanently
 * (after the worker's retry budget was exhausted).  The error is the same
 * structure that the task's ``result.chapter_errors[chapter_id]`` will
 * contain once the task completes.
 */
export interface ChapterFailedPayload {
  task_id: string;
  chapter_id: string;
  chapter_number: number | null;
  error: DownloadError;
}

// ───────────────────────────────────────────────────────────────────────────
// Download-related event type discriminator
// ───────────────────────────────────────────────────────────────────────────

/**
 * The subset of WS event names that carry a download payload.
 *
 * This union is what lets ``useWebSocket`` narrow an incoming event to its
 * typed payload.  Every name here corresponds to a payload interface above.
 */
export type DownloadEventType =
  | "download.started"
  | "download.progress"
  | "download.completed"
  | "download.failed"
  | "download.paused"
  | "download.resumed"
  | "download.cancelled"
  | "chapter.started"
  | "chapter.completed"
  | "chapter.failed";
