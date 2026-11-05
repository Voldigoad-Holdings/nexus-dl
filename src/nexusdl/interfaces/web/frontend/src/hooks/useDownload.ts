"use client";

/**
 * React hook for download management — the frontend's single entry point
 * for creating, listing and controlling download tasks.
 *
 * What this hook does
 * -------------------
 * 1. **Initial list fetch.** On first mount, if the store has not yet been
 *    initialised, the hook fetches **every** download task from the backend
 *    (paginated, capped at 20 pages / 2000 tasks), and installs them into
 *    :class:`~../store/downloads.DownloadsStore` via ``setTasks``.  Once
 *    ``initialized`` is true, subsequent mounts of any component that uses
 *    this hook are no-ops — the store already has the state.
 *
 * 2. **Live state.** The hook subscribes to the store through per-slice
 *    selectors wrapped in ``useShallow`` so that a component re-renders
 *    only when the fields it consumes actually change.  Live updates from
 *    the WebSocket (delivered by :mod:`./useWebSocket`) flow into the
 *    store transparently — this hook never touches the WebSocket itself.
 *
 * 3. **Actions.** :meth:`create`, :meth:`pause`, :meth:`resume`,
 *    :meth:`cancel`, :meth:`remove` and :meth:`clearFinished` are stable
 *    callbacks that mutate the backend **and** the store.  Actions are
 *    always server-first: the API call is awaited, and the store is only
 *    updated on success.  This keeps the UI consistent with the backend
 *    even when the WebSocket is disconnected — the WebSocket event, when
 *    it arrives, confirms an update that already happened.
 *
 * 4. **Create state.** :meth:`create` is a special case: it exposes
 *    ``isCreating`` and ``createError`` so a "Download" button can render
 *    a spinner and an inline error without a local ``useState``.
 *
 * Design boundaries
 * -----------------
 * * **No TanStack Query for the list.**  The list is fetched exactly once
 *   per session (via :meth:`refetch` on demand) and otherwise maintained
 *   by the WebSocket.  Layering TanStack Query on top would create a
 *   second source of truth that drifts from the store on every WS event.
 *   The store **is** the cache.
 *
 * * **No TanStack Query for actions.**  Each action is a one-shot request
 *   that either succeeds or fails.  ``useMutation`` would add a per-action
 *   loading/error surface that most call sites do not need, and would
 *   require separate mutation instances for pause/resume/cancel/remove.
 *   Plain ``async`` callbacks plus a local ``useState`` for the one action
 *   that actually needs UI feedback (``create``) is simpler and sufficient.
 *
 * * **No WebSocket connection management.**  :mod:`./useWebSocket` owns
 *   that.  This hook is safe to call from any client component regardless
 *   of whether the WebSocket is connected.
 *
 * * **No authentication handling.**  The API client normalises auth
 *   failures to :class:`~../lib/api.NexusApiError` with code
 *   ``"UNAUTHORIZED"``; callers that need to react to auth expiration
 *   should check ``error.code`` on the returned ``createError`` or on the
 *   exception thrown by an action.
 *
 * Store mutation ordering
 * -----------------------
 * When an action succeeds, the store is updated **immediately** via
 * ``markPaused`` / ``markResumed`` / ``markCancelled`` / ``removeTask``.
 * The WebSocket event that follows confirms the same transition and is
 * applied idempotently — no double-update, no flicker.  When the WebSocket
 * is disabled, the store still reflects the action, so the UI remains
 * correct (if slightly less live during background processing).
 *
 * Pagination cap
 * --------------
 * The initial fetch loops through pages of 100 tasks until ``has_next`` is
 * false or a hard cap of 20 pages (2000 tasks) is reached.  The cap exists
 * to bound the worst case if a runaway process floods the queue.  A real
 * session will almost always fetch a single page.
 */
import { useCallback, useEffect, useState } from "react";
import { useShallow } from "zustand/react/shallow";

import { api, NexusApiError } from "../lib/api";
import {
  selectActiveDownloads,
  selectAllDownloads,
  selectChapterErrors,
  selectCompletedDownloads,
  selectDownloadById,
  selectDownloadCounts,
  selectFailedDownloads,
  useDownloadsStore,
  type DownloadCounts,
} from "../store/downloads";
import type {
  CreateDownloadRequest,
  DownloadError,
  DownloadTask,
} from "../types/download";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Items per page for the initial list fetch.  Backend caps at 100. */
const LIST_PAGE_SIZE = 100;

/**
 * Hard safety cap on the number of pages the initial fetch will follow.
 *
 * ``20 × 100 = 2000`` tasks — far more than any realistic session and low
 * enough that a runaway queue cannot lock the browser fetching pages
 * forever.
 */
const LIST_MAX_PAGES = 20;

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Coerce any thrown value into a :class:`~../lib/api.NexusApiError`.
 *
 * The API client throws :class:`NexusApiError` for every documented failure
 * path, but defensive code must also handle programming errors (typos,
 * ``TypeError`` from a bad option, etc.) without crashing the calling
 * component.  This helper is the single place that normalisation happens.
 */
function toApiError(error: unknown): NexusApiError {
  if (error instanceof NexusApiError) {
    return error;
  }
  return new NexusApiError(
    error instanceof Error ? error.message : String(error),
    { code: "INTERNAL_ERROR", status: 0, cause: error },
  );
}

/**
 * Fetch every download task, paginating until ``has_next`` is false or the
 * safety cap is reached.
 *
 * Errors propagate — the caller wraps this in the try/catch that installs
 * the store's ``error`` field.  Partial progress is discarded on failure
 * (the caller's catch sets the store to its error state), so a mid-loop
 * failure never leaves a half-populated store.
 */
async function fetchAllDownloadTasks(): Promise<DownloadTask[]> {
  const all: DownloadTask[] = [];
  let page = 1;

  while (page <= LIST_MAX_PAGES) {
    const response = await api.listDownloads({
      page,
      per_page: LIST_PAGE_SIZE,
    });
    all.push(...response.items);
    if (!response.has_next) {
      break;
    }
    page += 1;
  }

  return all;
}

// ───────────────────────────────────────────────────────────────────────────
// useDownload
// ───────────────────────────────────────────────────────────────────────────

/**
 * The value returned by :func:`useDownload`.
 *
 * Every field is a stable reference or a primitive — destructuring the
 * result does not cause re-render churn on the parent component.
 */
export interface UseDownloadReturn {
  // -------------------------------------------------------------------
  // List state — reactive, fed by the store
  // -------------------------------------------------------------------

  /** Every known task, newest first. */
  tasks: DownloadTask[];
  /** Tasks whose status is ``pending``, ``running`` or ``paused``. */
  activeTasks: DownloadTask[];
  /** Tasks whose status is ``done``, most recently finished first. */
  completedTasks: DownloadTask[];
  /** Tasks whose status is ``failed`` or ``cancelled``. */
  failedTasks: DownloadTask[];
  /** Counts per status, plus totals. */
  counts: DownloadCounts;

  /** ``true`` when a list fetch is in flight. */
  isLoading: boolean;
  /** ``true`` once the initial list fetch has completed. */
  isInitialized: boolean;

  /**
   * The most recent fetch error, or ``null``.
   *
   * Always a :class:`~../lib/api.NexusApiError`, so callers can inspect
   * ``error.code``.  This field reflects **list-fetch** failures only —
   * per-action failures are surfaced through :attr:`createError` (for
   * :meth:`create`) or through the exception thrown by other actions.
   */
  error: NexusApiError | null;

  /**
   * Re-fetch the full task list.
   *
   * Safe to call at any time: deduped against concurrent calls via the
   * store's ``loading`` flag, and idempotent once the list is up to date.
   * Resolves when the fetch completes (success or failure); the resulting
   * error, if any, is set on :attr:`error`.
   */
  refetch: () => Promise<void>;

  // -------------------------------------------------------------------
  // Create
  // -------------------------------------------------------------------

  /**
   * Submit a new download task.
   *
   * Returns the newly-created task id.  The task is also installed into
   * the store immediately via ``upsertTask`` (best-effort ``GET`` right
   * after the ``POST``) so the UI shows it without waiting for the
   * WebSocket ``download.started`` event.
   *
   * Errors are set on :attr:`createError` **and** re-thrown, so callers
   * can either react to the state or ``try``/``catch`` locally — both
   * patterns work without interfering.
   */
  create: (request: CreateDownloadRequest) => Promise<string>;

  /** ``true`` while :meth:`create` has an in-flight request. */
  isCreating: boolean;

  /** The most recent :meth:`create` error, or ``null``. */
  createError: NexusApiError | null;

  /** Clear :attr:`createError`.  Useful before a retry. */
  clearCreateError: () => void;

  // -------------------------------------------------------------------
  // Per-task actions
  // -------------------------------------------------------------------

  /** Pause a ``running`` task.  Throws on failure. */
  pause: (taskId: string) => Promise<void>;

  /** Resume a ``paused`` task.  Throws on failure. */
  resume: (taskId: string) => Promise<void>;

  /** Cancel a task server-side.  Throws on failure. */
  cancel: (taskId: string) => Promise<void>;

  /**
   * Cancel (if still active) and remove from the store.
   *
   * When the task is already terminal, the cancel call is skipped and only
   * the store removal happens — matching the semantics of the "Remove
   * from list" button.  When the task is still active, the backend cancel
   * is awaited first, so the operation is not purely client-side.
   */
  remove: (taskId: string) => Promise<void>;

  /**
   * Remove every terminal task from the store.
   *
   * **Client-side only** — no backend call.  The backend retains its
   * history for audit; clearing the visible list is purely a UI
   * convenience.
   */
  clearFinished: () => void;
}

/**
 * Manage download tasks — see the module docstring for the full contract.
 *
 * @example Standard usage in the downloads page:
 *   const downloads = useDownload();
 *
 *   if (downloads.isLoading && !downloads.isInitialized) return <Skeleton />;
 *   if (downloads.error) return <ErrorBanner error={downloads.error} onRetry={downloads.refetch} />;
 *
 *   return (
 *     <>
 *       <TaskList
 *         tasks={downloads.activeTasks}
 *         onPause={downloads.pause}
 *         onResume={downloads.resume}
 *         onCancel={downloads.cancel}
 *       />
 *       <CompletedList
 *         tasks={downloads.completedTasks}
 *         onRemove={downloads.remove}
 *       />
 *       <button onClick={downloads.clearFinished}>Clear finished</button>
 *     </>
 *   );
 *
 * @example Download button in a manga detail page:
 *   const { create, isCreating, createError } = useDownload();
 *   const handleDownload = async () => {
 *     try {
 *       const taskId = await create({
 *         manga_id: manga.id,
 *         packaging_format: "cbz",
 *       });
 *       router.push(`/downloads#${taskId}`);
 *     } catch {
 *       // Error is already on `createError` and shown by the component
 *     }
 *   };
 */
export function useDownload(): UseDownloadReturn {
  // -------------------------------------------------------------------
  // Local state for list-fetch and create errors
  // -------------------------------------------------------------------
  //
  // The store's own ``error`` field is a plain string intended for global
  // consumers (notifications, etc.).  This hook keeps a full
  // ``NexusApiError`` locally so callers get ``error.code``.
  const [fetchError, setFetchError] = useState<NexusApiError | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [createError, setCreateError] = useState<NexusApiError | null>(null);

  // -------------------------------------------------------------------
  // Store subscriptions — one per slice, each wrapped in useShallow
  // so that a change to one slice does not re-render subscribers of
  // another.  The array selectors are compared shallowly by reference
  // (task objects are stable between updates), so a component re-renders
  // only when the set of tasks it consumes actually changes.
  // -------------------------------------------------------------------
  const tasks = useDownloadsStore(useShallow(selectAllDownloads));
  const activeTasks = useDownloadsStore(useShallow(selectActiveDownloads));
  const completedTasks = useDownloadsStore(
    useShallow(selectCompletedDownloads),
  );
  const failedTasks = useDownloadsStore(useShallow(selectFailedDownloads));
  const counts = useDownloadsStore(useShallow(selectDownloadCounts));
  const isLoading = useDownloadsStore((s) => s.loading);
  const isInitialized = useDownloadsStore((s) => s.initialized);

  // -------------------------------------------------------------------
  // Initial fetch + refetch
  // -------------------------------------------------------------------
  //
  // ``fetchAll`` is the single fetcher.  It dedupes against concurrent
  // calls through the store's ``loading`` flag: if another component (or
  // another instance of this hook) is already fetching, the second call
  // returns immediately.  This is what makes ``useDownload`` safe to call
  // from many components simultaneously.
  const fetchAll = useCallback(async (): Promise<void> => {
    const store = useDownloadsStore.getState();
    if (store.loading) {
      return;
    }
    store.setLoading(true);
    store.setError(null);
    setFetchError(null);

    try {
      const fetched = await fetchAllDownloadTasks();
      useDownloadsStore.getState().setTasks(fetched);
    } catch (err) {
      const apiError = toApiError(err);
      useDownloadsStore.getState().setError(apiError.message);
      setFetchError(apiError);
      throw apiError;
    } finally {
      useDownloadsStore.getState().setLoading(false);
    }
  }, []);

  // Auto-fetch once, on first mount, if the store is not yet initialised.
  // ``fetchAll`` is stable, so the effect runs once per component
  // lifetime; the inner guard skips the fetch entirely when another
  // component already populated the store.
  useEffect(() => {
    if (!useDownloadsStore.getState().initialized) {
      // Swallow the rejection — the error is already stored in
      // ``fetchError`` and (via ``setError``) in the store.  Letting it
      // propagate here would crash React's effect runner.
      void fetchAll().catch(() => undefined);
    }
  }, [fetchAll]);

  const refetch = useCallback(async (): Promise<void> => {
    // A refetch always runs, even when the store is initialised — that is
    // the whole point of the manual refresh.  But it still respects the
    // in-flight dedup so a double-click does not issue two requests.
    await fetchAll().catch(() => undefined);
  }, [fetchAll]);

  // -------------------------------------------------------------------
  // Create
  // -------------------------------------------------------------------
  const create = useCallback(
    async (request: CreateDownloadRequest): Promise<string> => {
      setIsCreating(true);
      setCreateError(null);
      try {
        const response = await api.createDownload(request);

        // Best-effort: install the newly-created task into the store
        // immediately, so the UI shows it without waiting for the
        // WebSocket ``download.started`` event.  If this GET fails (rare —
        // it is issued right after a successful POST against the same
        // backend), the WebSocket will populate the store when it
        // arrives; we do **not** fail the create because of it.
        try {
          const task = await api.getDownload(response.task_id);
          useDownloadsStore.getState().upsertTask(task);
        } catch {
          // Intentionally ignored — see comment above.
        }

        return response.task_id;
      } catch (err) {
        const apiError = toApiError(err);
        setCreateError(apiError);
        throw apiError;
      } finally {
        setIsCreating(false);
      }
    },
    [],
  );

  const clearCreateError = useCallback(() => {
    setCreateError(null);
  }, []);

  // -------------------------------------------------------------------
  // Per-task actions
  // -------------------------------------------------------------------
  //
  // Each action is server-first: the API call is awaited, then the store
  // is updated on success.  The WebSocket event that follows is applied
  // idempotently (markX is a no-op if the state is already X).
  //
  // Actions throw on failure so callers can use ``try``/``catch`` — the
  // store is not updated on failure, so the UI remains consistent with
  // the backend's actual state.
  const pause = useCallback(async (taskId: string): Promise<void> => {
    await api.pauseDownload(taskId);
    useDownloadsStore.getState().markPaused(taskId);
  }, []);

  const resume = useCallback(async (taskId: string): Promise<void> => {
    await api.resumeDownload(taskId);
    useDownloadsStore.getState().markResumed(taskId);
  }, []);

  const cancel = useCallback(async (taskId: string): Promise<void> => {
    await api.cancelDownload(taskId);
    useDownloadsStore.getState().markCancelled(taskId);
  }, []);

  const remove = useCallback(async (taskId: string): Promise<void> => {
    const task = useDownloadsStore.getState().tasks[taskId];

    // For an active task, cancel on the backend first so we do not leak
    // a running task.  For a terminal task, there is nothing to cancel.
    if (
      task !== undefined &&
      task.status !== "done" &&
      task.status !== "failed" &&
      task.status !== "cancelled"
    ) {
      await api.cancelDownload(taskId);
    }

    useDownloadsStore.getState().removeTask(taskId);
  }, []);

  const clearFinished = useCallback((): void => {
    useDownloadsStore.getState().clearFinished();
  }, []);

  // -------------------------------------------------------------------
  // Return
  // -------------------------------------------------------------------
  return {
    tasks,
    activeTasks,
    completedTasks,
    failedTasks,
    counts,
    isLoading,
    isInitialized,
    error: fetchError,
    refetch,
    create,
    isCreating,
    createError,
    clearCreateError,
    pause,
    resume,
    cancel,
    remove,
    clearFinished,
  };
}

// ───────────────────────────────────────────────────────────────────────────
// useDownloadTask
// ───────────────────────────────────────────────────────────────────────────

/**
 * The value returned by :func:`useDownloadTask`.
 *
 * Focuses on a **single** task — subscribing to the full list would
 * re-render the component on every change to any other task, which is
 * wasteful for a detail page that only shows one.
 */
export interface UseDownloadTaskReturn {
  /** The task, or ``undefined`` while it is not yet in the store. */
  task: DownloadTask | undefined;
  /** Per-chapter errors tracked for this task, keyed by chapter id. */
  chapterErrors: Readonly<Record<string, DownloadError>>;
  /** ``true`` when :attr:`task` is currently in a terminal status. */
  isFinished: boolean;
  /** Pause the task.  Throws on failure. */
  pause: () => Promise<void>;
  /** Resume the task.  Throws on failure. */
  resume: () => Promise<void>;
  /** Cancel the task.  Throws on failure. */
  cancel: () => Promise<void>;
  /** Remove the task from the store.  See :meth:`UseDownloadReturn.remove`. */
  remove: () => Promise<void>;
}

/**
 * Subscribe to a single download task by id.
 *
 * The returned object's methods are already bound to *taskId* — callers do
 * not need to pass it again.  The task object itself is a stable reference
 * from the store, so this hook re-renders only when **this** task changes,
 * not when other tasks do.
 *
 * @example
 *   const { task, chapterErrors, pause, resume } = useDownloadTask(taskId);
 *   if (!task) return <NotFound />;
 *   return (
 *     <TaskDetail
 *       task={task}
 *       chapterErrors={chapterErrors}
 *       onPause={pause}
 *       onResume={resume}
 *     />
 *   );
 */
export function useDownloadTask(taskId: string): UseDownloadTaskReturn {
  const task = useDownloadsStore(selectDownloadById(taskId));
  const chapterErrors = useDownloadsStore(
    useShallow(selectChapterErrors(taskId)),
  );

  const pause = useCallback(async (): Promise<void> => {
    await api.pauseDownload(taskId);
    useDownloadsStore.getState().markPaused(taskId);
  }, [taskId]);

  const resume = useCallback(async (): Promise<void> => {
    await api.resumeDownload(taskId);
    useDownloadsStore.getState().markResumed(taskId);
  }, [taskId]);

  const cancel = useCallback(async (): Promise<void> => {
    await api.cancelDownload(taskId);
    useDownloadsStore.getState().markCancelled(taskId);
  }, [taskId]);

  const remove = useCallback(async (): Promise<void> => {
    const current = useDownloadsStore.getState().tasks[taskId];
    if (
      current !== undefined &&
      current.status !== "done" &&
      current.status !== "failed" &&
      current.status !== "cancelled"
    ) {
      await api.cancelDownload(taskId);
    }
    useDownloadsStore.getState().removeTask(taskId);
  }, [taskId]);

  // ``isFinished`` is derived from the current task.  ``undefined`` means
  // "not yet loaded", which is not the same as "finished" — we return
  // ``false`` in that case so that callers do not short-circuit rendering
  // before the task has a chance to appear.
  const isFinished =
    task !== undefined &&
    (task.status === "done" ||
      task.status === "failed" ||
      task.status === "cancelled");

  return {
    task,
    chapterErrors,
    isFinished,
    pause,
    resume,
    cancel,
    remove,
  };
}
