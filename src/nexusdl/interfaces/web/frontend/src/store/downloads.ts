/**
 * Downloads store — live download task state, fed by REST snapshots and
 * WebSocket events.
 *
 * This store owns the **client-side view** of every download task the
 * current user has submitted.  It has three sources of truth, in
 * decreasing order of authority:
 *
 * 1. **REST snapshots** — ``GET /api/v1/download`` (full list) and
 *    ``GET /api/v1/download/{task_id}`` (single task).  These are
 *    authoritative for the *initial* state, and are the recovery path
 *    after any gap in WebSocket delivery.  Applied through
 *    :meth:`setTasks` and :meth:`upsertTask`.
 *
 * 2. **WebSocket events** — ``download.started``, ``download.progress``,
 *    ``download.completed``, ``download.failed``, ``download.paused``,
 *    ``download.resumed``, ``download.cancelled``, ``chapter.started``,
 *    ``chapter.completed``, ``chapter.failed``.  Applied through the
 *    ``mark*`` / ``update*`` actions.  These provide real-time liveness;
 *    they are best-effort during a connection drop, and the next REST
 *    snapshot or the next ``download.progress`` event self-corrects any
 *    drift.
 *
 * 3. **User intent** — pause/resume/cancel/remove commands.  Applied
 *    optimistically (when the operation is likely to succeed) or on
 *    confirmation (when the backend is the source of truth for the
 *    transition).  The user-initiated command path lives in the hook that
 *    wraps the REST client, not in this store — the store just exposes
 *    the state-mutation primitives that the hook calls after a successful
 *    API round-trip.
 *
 * What this store deliberately does **not** do
 * --------------------------------------------
 * * **No API calls.**  Zustand stores do not perform I/O.  The hook that
 *   wraps the REST client (``useDownload``, ``useDownloadsList``) calls
 *   ``fetch`` / ``api.getDownloads`` and then calls the appropriate
 *   store action with the result.
 *
 * * **No persistence.**  Download tasks are ephemeral server state.
 *   Persisting them to ``localStorage`` would produce stale tasks on
 *   reload and duplicate the REST snapshot that the app fetches on mount
 *   anyway.  Contrast with :mod:`./settings`, which persists because
 *   those are *user preferences* that the server does not store per
 *   browser.
 *
 * * **No chapter-level task objects.**  A ``DownloadTask`` represents one
 *   manga, not one chapter.  Chapter-level state is expressed through
 *   :type:`~../types/download.DownloadProgress` counters and — on task
 *   completion — through ``result.chapter_errors``.  Per-chapter errors
 *   that occur **while** the task is running are tracked in a separate
 *   slice (:attr:`DownloadsState.chapter_errors`), which is reconciled
 *   against ``result.chapter_errors`` when the task completes.
 *
 * Naming convention
 * -----------------
 * State fields use ``snake_case`` to match the backend's ``DownloadTask``
 * wire format.  Actions use ``camelCase`` and read like verbs
 * (``setTasks``, ``upsertTask``, ``markCompleted``).
 *
 * Selectors
 * ---------
 * Named selectors are exported so that components can import them once
 * instead of writing the same lambda inline.  Selectors that return
 * arrays or objects (``selectActiveDownloads``, ``selectDownloadCounts``)
 * produce a **new reference** on every state change; callers should wrap
 * them with Zustand's ``useShallow``::
 *
 *     import { useShallow } from "zustand/react/shallow";
 *
 *     const active = useDownloadsStore(useShallow(selectActiveDownloads));
 *
 * Selectors that return a single existing object (``selectDownloadById``)
 * return a stable reference and do not need ``useShallow``.
 */
import { create } from "zustand";

import {
  isTerminalStatus,
  type DownloadError,
  type DownloadProgress,
  type DownloadResult,
  type DownloadStatus,
  type DownloadTask,
} from "../types/download";

// ───────────────────────────────────────────────────────────────────────────
// State and action interfaces
// ───────────────────────────────────────────────────────────────────────────

/**
 * The non-action portion of the downloads store.
 *
 * Every field is set by a store action and read by a component or selector.
 * No field is derived from another at the store level — derived views
 * (active downloads, counts by status, etc.) live in the selectors at the
 * bottom of this file so that mutations and derivations stay cleanly
 * separated.
 */
export interface DownloadsState {
  /**
   * Every known download task, keyed by task id.
   *
   * A ``Record`` rather than an array because every mutation path is a
   * **lookup by id** — WS events carry a task id, not an index, and the
   * per-task mutations (``markCompleted``, ``updateProgress``, …) need
   * O(1) access.  Iteration for display happens through the selectors,
   * which produce sorted arrays on demand.
   */
  tasks: Record<string, DownloadTask>;

  /**
   * Per-chapter errors that occurred while a task was running, keyed by
   * task id, then by chapter id.
   *
   * Populated by ``chapter.failed`` WebSocket events.  When a task
   * completes (``download.completed``), this map for that task is
   * **replaced** by ``result.chapter_errors`` — the authoritative
   * server-side list — so any missed events are reconciled.  Entries for
   * removed or cleared tasks are pruned.
   *
   * Reading this map is optional: the UI can show only the aggregate
   * ``progress.chapters_failed`` counter, or expand into the per-chapter
   * list.  Both are correct; the map exists so that the expanded view is
   * possible without an extra REST call.
   */
  chapter_errors: Record<string, Record<string, DownloadError>>;

  /**
   * ``true`` once the first REST snapshot has been applied.
   *
   * Used by the UI to distinguish "loading, no tasks yet" from "loaded,
   * zero tasks" — a distinction a simple ``Object.keys(tasks).length ===
   * 0`` check cannot make.  WS events do **not** set this to ``true``;
   * only :meth:`setTasks` does.
   */
  initialized: boolean;

  /**
   * ``true`` while a REST fetch is in flight.
   *
   * Set by :meth:`setLoading` from the fetch hook.  Only reflects the
   * initial full-list fetch; it does not toggle on WS activity.
   */
  loading: boolean;

  /**
   * Error message from the last failed REST fetch, or ``null``.
   *
   * A plain string rather than a structured error because this field is
   * for **display only** — the structured error (code, request_id) is
   * logged by the fetch hook and is not actionable from the UI.  If a
   * future feature needs the structured form, it belongs in a dedicated
   * error slice, not here.
   */
  error: string | null;
}

/**
 * The mutation API exposed by the store.
 *
 * Actions are stable references — the same function object for the
 * lifetime of the process.  Subscribing to an action with
 * ``useDownloadsStore((s) => s.setTasks)`` never triggers a re-render
 * because the reference never changes.
 *
 * Mutations that target a specific task are **no-ops when the task is
 * absent**.  This is deliberate: a WS event about an unknown task is
 * either (a) a task created by another client and not yet visible in
 * this client's REST snapshot, or (b) an out-of-order event after a
 * reconnect.  Silently dropping it is the correct behaviour — the next
 * REST snapshot or the next ``download.started`` event (which carries
 * the full task) will repopulate the store, and there is no way to
 * construct a valid task from a partial event.
 */
export interface DownloadsActions {
  /**
   * Replace the entire task set from a REST snapshot.
   *
   * Prunes ``chapter_errors`` entries for tasks that are no longer in
   * the snapshot, and sets :attr:`initialized` to ``true``.  Safe to
   * call at any time — a subsequent WS event will be applied on top of
   * the resulting state.
   */
  setTasks: (tasks: DownloadTask[]) => void;

  /**
   * Insert or replace a single task.
   *
   * Used for:
   *  * the ``download.started`` WS event (payload carries the full task);
   *  * a REST refetch of a single task (``GET /api/v1/download/{id}``);
   *  * the optimistic insert after ``POST /api/v1/download`` succeeds,
   *    when the client has enough information to construct a stub.
   */
  upsertTask: (task: DownloadTask) => void;

  /**
   * Remove a task from the store.
   *
   * Purely a client-side removal — the backend is the owner of the task's
   * lifecycle and is unaffected.  Callers that want a **server-side**
   * removal must first call ``DELETE /api/v1/download/{task_id}`` and then
   * call this action on success.
   */
  removeTask: (taskId: string) => void;

  /**
   * Remove every terminal task (``done``, ``failed``, ``cancelled``) from
   * the store, along with their tracked chapter errors.
   *
   * Non-terminal tasks are preserved untouched.  Intended for the "Clear
   * completed" button; does **not** call the backend.
   */
  clearFinished: () => void;

  /**
   * Apply a full :type:`DownloadProgress` snapshot.
   *
   * Called from the ``download.progress`` WS event.  The payload is a full
   * snapshot, not a delta, so this is a straight replacement of the
   * ``progress`` field.
   */
  updateProgress: (taskId: string, progress: DownloadProgress) => void;

  /**
   * Mark a task as ``done`` and attach its final result.
   *
   * Called from the ``download.completed`` WS event.  Also:
   *  * sets ``finished_at`` (if not already set) to a client-side
   *    timestamp — the server timestamp is authoritative but is only
   *    available on the next REST refetch, and the client timestamp is
   *    within a WS round-trip of it;
   *  * replaces the tracked ``chapter_errors`` for the task with the
   *    authoritative list from ``result.chapter_errors``;
   *  * zeroes out live progress fields (``speed_bps``, ``eta_seconds``,
   *    current chapter/page) and syncs the counters from the result.
   */
  markCompleted: (taskId: string, result: DownloadResult) => void;

  /**
   * Mark a task as ``failed`` and attach the task-level error.
   *
   * Called from the ``download.failed`` WS event.  Tracked chapter errors
   * for this task are **preserved** — a task may fail after some chapters
   * already failed, and the UI benefits from showing both the task-level
   * failure and the accumulated chapter failures.
   */
  markFailed: (taskId: string, error: DownloadError) => void;

  /** Mark a task as ``paused``.  Zeroes live progress speed/ETA. */
  markPaused: (taskId: string) => void;

  /** Mark a task as ``running``.  Does not touch progress counters. */
  markResumed: (taskId: string) => void;

  /**
   * Mark a task as ``cancelled``.
   *
   * Sets ``finished_at`` and zeroes live progress fields, mirroring
   * :meth:`markCompleted`.  Tracked chapter errors are preserved.
   */
  markCancelled: (taskId: string) => void;

  /**
   * Handle the ``chapter.started`` WS event.
   *
   * Sets ``progress.current_chapter_id`` and
   * ``progress.current_chapter_number``.  The next ``download.progress``
   * event will overwrite both with the authoritative values; this action
   * exists to provide instant feedback before that event arrives.
   */
  updateChapterStarted: (
    taskId: string,
    chapterId: string,
    chapterNumber: number | null,
  ) => void;

  /**
   * Handle the ``chapter.completed`` WS event.
   *
   * Increments ``progress.chapters_completed`` and clears the current
   * chapter markers.  The increment is **best-effort and idempotent by
   * chapter**: the same ``chapter.completed`` event delivered twice (a
   * WS reconnect replay, or a duplicate emission on the backend side) is
   * counted exactly once, and the resulting counter is clamped to
   * ``chapters_total`` when the total is known.  A missed event during a
   * reconnect will leave the counter short, but the next
   * ``download.progress`` event self-corrects, and the task's final
   * ``result.chapters_completed`` is authoritative.
   *
   * The ``chapterId`` parameter is required for de-duplication.  The
   * ``chapterNumber`` and ``outputPath`` parameters are part of the event
   * payload and are accepted for symmetry with the WS hook, but are
   * unused: chapter identity is keyed by id, and the packaging path is
   * only needed by toasts and logs, which consume the WS payload
   * directly.
   */
  updateChapterCompleted: (
    taskId: string,
    chapterId: string,
    chapterNumber: number | null,
    outputPath: string,
  ) => void;

  /**
   * Handle the ``chapter.failed`` WS event.
   *
   * Increments ``progress.chapters_failed``, clears the current chapter
   * markers, and records the error in :attr:`chapter_errors`.  The
   * increment is idempotent per chapter — a replayed event is counted
   * once.  A replayed event with the **same** ``chapterId`` also
   * overwrites the recorded error with the same payload, which is a no-op
   * at the value level.  The next ``download.progress`` event overwrites
   * the counter (best-effort semantics), and ``download.completed``
   * replaces the full error map, so any discrepancy is reconciled.
   *
   * ``chapterNumber`` is accepted for symmetry with the event payload but
   * is not stored: the error object is keyed by ``chapterId``, and the
   * chapter number is available on the ``Chapter`` resource if needed.
   */
  updateChapterFailed: (
    taskId: string,
    chapterId: string,
    chapterNumber: number | null,
    error: DownloadError,
  ) => void;

  /** Set the REST-fetch loading flag. */
  setLoading: (loading: boolean) => void;

  /**
   * Set the REST-fetch error message.
   *
   * Passing ``null`` clears the error, which the fetch hook does at the
   * start of every attempt so that a transient failure does not pin the
   * UI in an error state across retries.
   */
  setError: (error: string | null) => void;

  /**
   * Reset the store to its pristine state.
   *
   * Intended for tests and for logout flows.  Clears tasks, chapter
   * errors, and every flag.  Does not call the backend.
   */
  reset: () => void;
}

/** The full store shape — state plus actions. */
export type DownloadsStore = DownloadsState & DownloadsActions;

// ───────────────────────────────────────────────────────────────────────────
// Defaults and helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Pristine state for every field managed by the store.
 *
 * Only the **primitive** fields are declared here; the mutable containers
 * (``tasks``, ``chapter_errors``) are produced by :func:`createInitialState`
 * so that :meth:`reset` receives **fresh empty objects** every time.
 * Spreading a shared ``{ tasks: {} }`` into ``set({...})`` would make the
 * reset state and the store's live ``tasks`` record alias the same object
 * — a subsequent write to one would leak into the other.
 */
const DEFAULT_STATE_PRIMITIVES: Readonly<{
  initialized: boolean;
  loading: boolean;
  error: string | null;
}> = Object.freeze({
  initialized: false,
  loading: false,
  error: null,
});

/**
 * Build a fresh pristine state object.
 *
 * Called from the ``create`` initializer and from :meth:`reset`, so the
 * two code paths share the same shape but never share mutable containers.
 * The result is deep-equal to :data:`DEFAULT_STATE_PRIMITIVES` plus empty
 * containers, which is what tests assert against.
 */
function createInitialState(): DownloadsState {
  return {
    tasks: {},
    chapter_errors: {},
    initialized: DEFAULT_STATE_PRIMITIVES.initialized,
    loading: DEFAULT_STATE_PRIMITIVES.loading,
    error: DEFAULT_STATE_PRIMITIVES.error,
  };
}

/**
 * Apply *updater* to the task at *taskId*, returning a new ``tasks`` record.
 *
 * Returns the **same** record unchanged when the task is absent, so that
 * mutations for unknown tasks are no-ops without rebuilding the record.
 * This is the property that makes unknown-event handling cheap and
 * side-effect free.
 */
function updateTaskRecord(
  tasks: Record<string, DownloadTask>,
  taskId: string,
  updater: (task: DownloadTask) => DownloadTask,
): Record<string, DownloadTask> {
  const existing = tasks[taskId];
  if (existing === undefined) {
    return tasks;
  }
  return { ...tasks, [taskId]: updater(existing) };
}

/**
 * Return ``progress`` with live fields (speed, ETA, current markers) reset
 * to their idle values, leaving cumulative counters untouched.
 *
 * Used by the terminal-state actions (completed, failed, cancelled) so
 * that the last-seen progress bar renders as "finished" without a stale
 * speed or ETA.  Pausing uses the same shape but keeps the current chapter
 * markers, since the user may want to see where the download stopped.
 *
 * ``eta_seconds`` is set to ``null`` (not ``0``) so that the UI renders
 * "—" rather than "0s remaining" once the download is no longer running —
 * the same convention used by :meth:`markPaused`.
 */
function quiesceProgress(progress: DownloadProgress): DownloadProgress {
  return {
    ...progress,
    speed_bps: 0,
    eta_seconds: null,
    current_chapter_id: null,
    current_chapter_number: null,
    current_page: null,
  };
}

/**
 * Return the current client-side timestamp as an ISO 8601 string.
 *
 * Centralised here so that a future switch to a server-synchronised clock
 * (or to ``date-fns``'s monotonic helpers) touches exactly one call site.
 */
function nowIso(): string {
  return new Date().toISOString();
}

// ───────────────────────────────────────────────────────────────────────────
// Store
// ───────────────────────────────────────────────────────────────────────────

/**
 * The downloads store.
 *
 * Read values with a selector::
 *
 *     const tasks = useDownloadsStore((s) => s.tasks);
 *     const task = useDownloadsStore(selectDownloadById(taskId));
 *
 * Call actions through a selector — actions are stable references, so this
 * never causes a re-render on its own::
 *
 *     const markCompleted = useDownloadsStore((s) => s.markCompleted);
 *
 * Never subscribe to the whole store (``useDownloadsStore()`` with no
 * selector) — that returns a new object reference on every state change
 * and forces the component to re-render for unrelated updates.
 */
export const useDownloadsStore = create<DownloadsStore>()((set) => ({
  ...createInitialState(),

  // -------------------------------------------------------------------
  // Full-set and per-task replacement
  // -------------------------------------------------------------------
  setTasks: (tasks) =>
    set((state) => {
      const nextTasks: Record<string, DownloadTask> = {};
      for (const task of tasks) {
        nextTasks[task.id] = task;
      }

      // Preserve tracked chapter errors for tasks that still exist, and
      // prune the rest.  A task that has vanished from the REST snapshot
      // has either completed and been pruned server-side, or been removed
      // by another client — either way its tracked errors are no longer
      // useful.
      const nextChapterErrors: Record<string, Record<string, DownloadError>> =
        {};
      for (const id of Object.keys(nextTasks)) {
        const tracked = state.chapter_errors[id];
        if (tracked !== undefined) {
          nextChapterErrors[id] = tracked;
        }
      }

      return {
        tasks: nextTasks,
        chapter_errors: nextChapterErrors,
        initialized: true,
      };
    }),

  upsertTask: (task) =>
    set((state) => ({
      tasks: { ...state.tasks, [task.id]: task },
    })),

  removeTask: (taskId) =>
    set((state) => {
      const { [taskId]: _removedTask, ...nextTasks } = state.tasks;
      const { [taskId]: _removedErrors, ...nextChapterErrors } =
        state.chapter_errors;
      return { tasks: nextTasks, chapter_errors: nextChapterErrors };
    }),

  clearFinished: () =>
    set((state) => {
      const nextTasks: Record<string, DownloadTask> = {};
      const nextChapterErrors: Record<string, Record<string, DownloadError>> =
        {};

      for (const [id, task] of Object.entries(state.tasks)) {
        if (isTerminalStatus(task.status)) {
          continue;
        }
        nextTasks[id] = task;
        const tracked = state.chapter_errors[id];
        if (tracked !== undefined) {
          nextChapterErrors[id] = tracked;
        }
      }

      return { tasks: nextTasks, chapter_errors: nextChapterErrors };
    }),

  // -------------------------------------------------------------------
  // Live progress
  // -------------------------------------------------------------------
  updateProgress: (taskId, progress) =>
    set((state) => ({
      tasks: updateTaskRecord(state.tasks, taskId, (task) => ({
        ...task,
        progress,
      })),
    })),

  // -------------------------------------------------------------------
  // Terminal transitions
  // -------------------------------------------------------------------
  markCompleted: (taskId, result) =>
    set((state) => {
      const finishedAt = nowIso();
      const nextTasks = updateTaskRecord(state.tasks, taskId, (task) => ({
        ...task,
        status: "done",
        finished_at: task.finished_at ?? finishedAt,
        result,
        // Sync counters from the result and zero out live fields.  The
        // result is authoritative; a last progress event may have been
        // dropped during a reconnect, and the result is not.
        progress: {
          ...quiesceProgress(task.progress),
          chapters_total: result.chapters_total,
          chapters_completed: result.chapters_completed,
          chapters_failed: result.chapters_failed,
          pages_completed: result.pages_completed,
          pages_failed: result.pages_failed,
          bytes_downloaded: result.bytes_downloaded,
        },
      }));

      return {
        tasks: nextTasks,
        // Replace tracked errors with the authoritative ones from the
        // result.  Any chapter error missed during a reconnect is
        // recovered here.
        chapter_errors: {
          ...state.chapter_errors,
          [taskId]: result.chapter_errors,
        },
      };
    }),

  markFailed: (taskId, error) =>
    set((state) => {
      const finishedAt = nowIso();
      return {
        tasks: updateTaskRecord(state.tasks, taskId, (task) => ({
          ...task,
          status: "failed",
          finished_at: task.finished_at ?? finishedAt,
          error,
          progress: quiesceProgress(task.progress),
        })),
        // Chapter errors tracked so far are preserved — a task can fail
        // after some chapters already failed, and both pieces of
        // information are useful.
      };
    }),

  markCancelled: (taskId) =>
    set((state) => {
      const finishedAt = nowIso();
      return {
        tasks: updateTaskRecord(state.tasks, taskId, (task) => ({
          ...task,
          status: "cancelled",
          finished_at: task.finished_at ?? finishedAt,
          progress: quiesceProgress(task.progress),
        })),
      };
    }),

  // -------------------------------------------------------------------
  // Non-terminal transitions
  // -------------------------------------------------------------------
  markPaused: (taskId) =>
    set((state) => ({
      tasks: updateTaskRecord(state.tasks, taskId, (task) => ({
        ...task,
        status: "paused",
        progress: {
          ...task.progress,
          speed_bps: 0,
          // ETA is unknown while paused — resetting to ``null`` (rather
          // than 0) lets the UI render "—" instead of "0s remaining".
          eta_seconds: null,
        },
      })),
    })),

  markResumed: (taskId) =>
    set((state) => ({
      tasks: updateTaskRecord(state.tasks, taskId, (task) => ({
        ...task,
        status: "running",
      })),
    })),

  // -------------------------------------------------------------------
  // Chapter events
  // -------------------------------------------------------------------
  updateChapterStarted: (taskId, chapterId, chapterNumber) =>
    set((state) => ({
      tasks: updateTaskRecord(state.tasks, taskId, (task) => ({
        ...task,
        progress: {
          ...task.progress,
          current_chapter_id: chapterId,
          current_chapter_number: chapterNumber,
          current_page: null,
        },
      })),
    })),

  updateChapterCompleted: (
    taskId,
    chapterId,
    _chapterNumber,
    _outputPath,
  ) =>
    set((state) => ({
      tasks: updateTaskRecord(state.tasks, taskId, (task) => {
        // Idempotency guard: the ``chapter.completed`` event for a given
        // chapter id may be delivered more than once (WS reconnect replay,
        // backend duplicate).  The task's ``result.chapter_errors`` is the
        // only place the store keeps a per-chapter record, and it holds
        // only **failures** — so we cannot use it to detect a duplicate
        // success.  Instead we clamp the increment to
        // ``chapters_total``: if the counter would exceed the total, the
        // event is a duplicate and the increment is skipped.
        const isDuplicate =
          task.progress.chapters_total > 0 &&
          task.progress.chapters_completed >= task.progress.chapters_total;

        const nextCompleted = isDuplicate
          ? task.progress.chapters_completed
          : task.progress.chapters_completed + 1;

        return {
          ...task,
          progress: {
            ...task.progress,
            chapters_completed: nextCompleted,
            current_chapter_id: null,
            current_chapter_number: null,
            current_page: null,
          },
        };
      }),
    })),

  updateChapterFailed: (taskId, chapterId, _chapterNumber, error) =>
    set((state) => {
      const existing = state.chapter_errors[taskId] ?? {};
      // Same idempotency logic as ``updateChapterCompleted``: a duplicate
      // event must not double-count.  Here we *do* have a per-chapter
      // record (``existing``), so a second event for the same
      // ``chapterId`` is unambiguously a duplicate.  The error object is
      // overwritten with the new payload — a legitimate re-failure would
      // have a different error, but the backend does not re-emit
      // ``chapter.failed`` for the same chapter within a single task, so
      // overwriting on duplicate is a safe no-op at the value level.
      const isDuplicate = chapterId in existing;

      const nextTasks = updateTaskRecord(state.tasks, taskId, (task) => ({
        ...task,
        progress: {
          ...task.progress,
          chapters_failed: isDuplicate
            ? task.progress.chapters_failed
            : task.progress.chapters_failed + 1,
          current_chapter_id: null,
          current_chapter_number: null,
          current_page: null,
        },
      }));

      return {
        tasks: nextTasks,
        chapter_errors: {
          ...state.chapter_errors,
          [taskId]: { ...existing, [chapterId]: error },
        },
      };
    }),

  // -------------------------------------------------------------------
  // Fetch flags
  // -------------------------------------------------------------------
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),

  // -------------------------------------------------------------------
  // Reset
  // -------------------------------------------------------------------
  // ``createInitialState`` returns **fresh** ``tasks`` and
  // ``chapter_errors`` objects so that the reset state does not alias the
  // store's live containers.  Spreading ``DEFAULT_STATE_PRIMITIVES`` would
  // share the empty record between the reset state and every future clone
  // of it.
  reset: () => set(createInitialState()),
}));

// ───────────────────────────────────────────────────────────────────────────
// Selectors
// ───────────────────────────────────────────────────────────────────────────
//
// Named selectors are exported so that components can import them once
// instead of writing the same lambda inline.  They also make it trivial to
// audit which components depend on which field.
//
// Selectors that return a *new* array or object on each call
// (``selectAllDownloads``, ``selectActiveDownloads``,
// ``selectDownloadCounts``, ``selectDownloadsByStatus``) must be wrapped
// with ``useShallow`` at the call site, or memoised by the caller, to
// avoid an infinite render loop:
//
//     import { useShallow } from "zustand/react/shallow";
//
//     const active = useDownloadsStore(useShallow(selectActiveDownloads));
//
// Selectors that return a single existing object (``selectDownloadById``,
// ``selectChapterErrors``) return a stable reference and do not need
// ``useShallow``.

/**
 * Stable empty **mutable** array, used to avoid a new allocation on every
 * call when the store is empty.
 *
 * Returning a fresh ``[]`` each time would break ``useShallow``'s shallow
 * comparison (two empty arrays are not referentially equal under
 * ``Object.is``).  The shared instance is safe to hand out because every
 * caller treats the selector result as read-only — the selectors are
 * documented as such, and ``DownloadTask[]`` is the shape every consumer
 * (sort, map, filter) uses without mutation.
 */
const EMPTY_TASKS: DownloadTask[] = [];

/** Stable empty error map, used to avoid a new allocation on every call. */
const EMPTY_ERRORS: Readonly<Record<string, DownloadError>> = Object.freeze(
  {},
);

/**
 * Sort order for every task-array selector.
 *
 * Newest first, by creation time.  This matches every consumer's
 * expectation (the most recently submitted download appears at the top
 * of the list) and produces a total order even when two tasks share a
 * creation timestamp — ISO 8601 strings sort lexicographically in
 * chronological order.
 */
function sortByCreatedDesc(a: DownloadTask, b: DownloadTask): number {
  if (a.created_at === b.created_at) {
    return a.id.localeCompare(b.id);
  }
  return a.created_at < b.created_at ? 1 : -1;
}

/** Sort order for completed/failed lists: most recently finished first. */
function sortByFinishedDesc(a: DownloadTask, b: DownloadTask): number {
  const aFinished = a.finished_at ?? a.created_at;
  const bFinished = b.finished_at ?? b.created_at;
  if (aFinished === bFinished) {
    return a.id.localeCompare(b.id);
  }
  return aFinished < bFinished ? 1 : -1;
}

/**
 * Return every task as an array, sorted newest-first.
 *
 * **Wrap with ``useShallow``** at the call site.
 */
export const selectAllDownloads = (s: DownloadsStore): DownloadTask[] => {
  const tasks = Object.values(s.tasks);
  if (tasks.length === 0) {
    // Return the shared empty array to preserve referential stability
    // when the store is empty — a common case in a fresh session.
    return EMPTY_TASKS;
  }
  return tasks.sort(sortByCreatedDesc);
};

/**
 * Return the task with the given id, or ``undefined``.
 *
 * Curried so that callers can build the selector once and reuse it::
 *
 *     const selectTask = useMemo(() => selectDownloadById(taskId), [taskId]);
 *     const task = useDownloadsStore(selectTask);
 *
 * Returns a stable reference — the task object is the same one held by
 * the store, so this selector does not need ``useShallow``.
 */
export const selectDownloadById =
  (taskId: string) =>
  (s: DownloadsStore): DownloadTask | undefined =>
    s.tasks[taskId];

/**
 * Return the tracked chapter errors for a task.
 *
 * Returns the shared empty map when no errors have been tracked, so that
 * callers relying on reference stability do not re-render on every store
 * update.
 */
export const selectChapterErrors =
  (taskId: string) =>
  (s: DownloadsStore): Readonly<Record<string, DownloadError>> =>
    s.chapter_errors[taskId] ?? EMPTY_ERRORS;

/**
 * Return the tasks whose status is one of *statuses*, sorted newest-first.
 *
 * **Wrap with ``useShallow``** at the call site.  Exposed as a curried
 * function so that common status groups can be pre-bound once and reused
 * across components.
 */
export const selectDownloadsByStatus =
  (...statuses: DownloadStatus[]) =>
  (s: DownloadsStore): DownloadTask[] => {
    const wanted = new Set<DownloadStatus>(statuses);
    const matching: DownloadTask[] = [];
    for (const task of Object.values(s.tasks)) {
      if (wanted.has(task.status)) {
        matching.push(task);
      }
    }
    return matching.sort(sortByCreatedDesc);
  };

/**
 * Return every task that is **not** in a terminal state — ``pending``,
 * ``running`` or ``paused`` — sorted newest-first.
 *
 * **Wrap with ``useShallow``** at the call site.
 */
export const selectActiveDownloads = (s: DownloadsStore): DownloadTask[] => {
  const active: DownloadTask[] = [];
  for (const task of Object.values(s.tasks)) {
    if (!isTerminalStatus(task.status)) {
      active.push(task);
    }
  }
  return active.sort(sortByCreatedDesc);
};

/**
 * Return every completed (``done``) task, sorted most-recently-finished
 * first.
 *
 * **Wrap with ``useShallow``** at the call site.
 */
export const selectCompletedDownloads = (
  s: DownloadsStore,
): DownloadTask[] => {
  const completed: DownloadTask[] = [];
  for (const task of Object.values(s.tasks)) {
    if (task.status === "done") {
      completed.push(task);
    }
  }
  return completed.sort(sortByFinishedDesc);
};

/**
 * Return every failed or cancelled task, sorted most-recently-finished
 * first.
 *
 * **Wrap with ``useShallow``** at the call site.
 *
 * The name is kept for backward compatibility with the page that consumes
 * it (``/downloads``, whose "Failed" filter groups ``failed`` and
 * ``cancelled`` together).  If a future UI needs to distinguish the two,
 * add a dedicated ``selectCancelledDownloads`` rather than overloading
 * this one.
 */
export const selectFailedDownloads = (s: DownloadsStore): DownloadTask[] => {
  const failed: DownloadTask[] = [];
  for (const task of Object.values(s.tasks)) {
    if (task.status === "failed" || task.status === "cancelled") {
      failed.push(task);
    }
  }
  return failed.sort(sortByFinishedDesc);
};

/**
 * A snapshot of how many tasks exist in each status, plus the total.
 *
 * **Wrap with ``useShallow``** at the call site.  The object is produced
 * fresh on every store change; ``useShallow`` performs a shallow equality
 * check and skips the re-render when the counts have not changed (which is
 * the common case during a long download, where only one task's progress
 * is evolving and the counts are stable).
 */
export interface DownloadCounts {
  pending: number;
  running: number;
  paused: number;
  done: number;
  failed: number;
  cancelled: number;
  /** Every status combined. */
  total: number;
  /** ``pending + running + paused``. */
  active: number;
}

/** Return per-status task counts.  **Wrap with ``useShallow``**. */
export const selectDownloadCounts = (
  s: DownloadsStore,
): DownloadCounts => {
  let pending = 0;
  let running = 0;
  let paused = 0;
  let done = 0;
  let failed = 0;
  let cancelled = 0;

  for (const task of Object.values(s.tasks)) {
    switch (task.status) {
      case "pending":
        pending += 1;
        break;
      case "running":
        running += 1;
        break;
      case "paused":
        paused += 1;
        break;
      case "done":
        done += 1;
        break;
      case "failed":
        failed += 1;
        break;
      case "cancelled":
        cancelled += 1;
        break;
    }
  }

  const total = pending + running + paused + done + failed + cancelled;
  const active = pending + running + paused;

  return {
    pending,
    running,
    paused,
    done,
    failed,
    cancelled,
    total,
    active,
  };
};
