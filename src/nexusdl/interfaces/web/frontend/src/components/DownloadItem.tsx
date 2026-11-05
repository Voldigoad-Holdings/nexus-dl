"use client";

/**
 * DownloadItem — a single row in the downloads list.
 *
 * Renders one :type:`~../types/download.DownloadTask` with:
 *
 *  * a status badge (:attr:`task.status`) with a per-status icon and
 *    colour;
 *  * the manga title and a compact metadata line
 *    (``site · progress · speed · ETA``);
 *  * a determinate or indeterminate
 *    :class:`~./ProgressBar.ProgressBar` reflecting the task's current
 *    progress;
 *  * a status-dependent action group: pause / resume / cancel for active
 *    tasks, remove for terminal tasks.
 *
 * The component is **fully controlled**: it does not call the API or the
 * store.  Every action is invoked through an optional callback that the
 * parent wires to :func:`~../hooks/useDownload.useDownload` (or to
 * :func:`~../hooks/useDownload.useDownloadTask` for a detail page).  This
 * keeps the item trivially testable and lets the same component serve both
 * the list view and any future "compact queue" panel without modification.
 *
 * Action visibility by status
 * ---------------------------
 * | Status      | Pause | Resume | Cancel | Remove |
 * |-------------|-------|--------|--------|--------|
 * | ``pending`` |   –   |   –    |   ✓    |   –    |
 * | ``running`` |   ✓   |   –    |   ✓    |   –    |
 * | ``paused``  |   –   |   ✓    |   ✓    |   –    |
 * | ``done``    |   –   |   –    |   –    |   ✓    |
 * | ``failed``  |   –   |   –    |   –    |   ✓    |
 * | ``cancelled`` | –   |   –    |   –    |   ✓    |
 *
 * A button is only rendered when its callback is provided **and** the
 * status permits it.  This means a caller that omits, say, ``onCancel``
 * gets a UI with no cancel button rather than a disabled one — the
 * cleanest signal that the action is not available in this context.
 *
 * Progress calculation
 * --------------------
 * The bar's percentage and text are derived from the task's
 * :type:`~../types/download.DownloadProgress` in this order:
 *
 * 1. **Byte progress** — when ``bytes_total > 0``.  The bar shows
 *    ``bytes_downloaded / bytes_total`` and the value text is the
 *    human-readable byte pair (``"12.4 MB / 25 MB"``).
 * 2. **Chapter progress** — when byte progress is unavailable but
 *    ``chapters_total > 0``.  The bar shows
 *    ``chapters_completed / chapters_total`` and the value text is the
 *    human-readable chapter pair (``"3 / 10 chapters"``).
 * 3. **Indeterminate** — when neither is available.  The bar pulses.
 *
 * Terminal tasks use the same calculation, so a completed download lands
 * on a full bar and a cancelled one on whatever it reached — matching what
 * the user saw while it was running.
 *
 * Error surface
 * -------------
 * For ``failed`` tasks, :attr:`task.error` (a
 * :type:`~../types/download.DownloadError`) is rendered as the metadata
 * line: ``"mangadex · Parser returned malformed data"``.  The message is
 * truncated to keep the row height stable; the full error is available via
 * the native ``title`` tooltip and, for callers that need it, by opening
 * the task detail page (a future iteration).
 *
 * Per-chapter failures inside a task that did not fail as a whole are
 * surfaced as a small ``"· 2 chapters failed"`` suffix on the metadata
 * line when ``chapters_failed > 0``.  The individual errors live in
 * :type:`~../types/download.DownloadResult` for ``done`` tasks and in
 * :attr:`~../store/downloads.DownloadsStore.chapter_errors` while
 * running — this component does not display them (it is a summary row, not
 * a detail view).
 *
 * Accessibility
 * -------------
 * * The row is not focusable itself; the interactive elements (action
 *   buttons) are.  Screen-reader users tab through the actions in order.
 * * Each action button has an explicit ``aria-label`` describing the
 *   action **and** the manga it applies to, so a screen reader announces
 *   "Pause download Berserk" rather than a bare "Pause".
 * * The status badge is a ``<span>`` with ``role="status"`` and
 *   ``aria-live="polite"`` on a wrapper, so status transitions
 *   (running → done, running → failed) are announced without the caller
 *   wiring a live region manually.
 * * The metadata line and the progress bar carry the same information
 *   visually and to assistive technology — the bar's ``aria-valuetext``
 *   duplicates the human-readable value that the metadata line shows,
 *   which is the correct pattern for a progress indicator next to a
 *   textual description.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  The item is built from
 * ``<div>``, ``<span>``, ``<button>`` primitives styled with the Tailwind
 * tokens defined in ``src/app/globals.css``, plus ``lucide-react`` icons,
 * the project's ``cn`` helper and the already-delivered
 * :class:`~./ProgressBar.ProgressBar`.  When the shadcn primitives land,
 * the visual style can be upgraded in a single self-contained edit — the
 * public API does not change.
 */
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Download as DownloadIcon,
  Pause as PauseIcon,
  Play,
  Trash2,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import {
  cn,
  formatBytes,
  formatDuration,
  formatEta,
  formatSpeed,
} from "../lib/utils";
import type { DownloadStatus, DownloadTask } from "../types/download";
import { ProgressBar, type ProgressBarVariant } from "./ProgressBar";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * Per-status presentation metadata.
 *
 * ``icon`` and ``classes`` drive the badge; the two helper functions
 * below (``progressVariantFor`` and ``buildMetaLine``) use the same status
 * to derive the bar's variant and the metadata text.  Centralising the
 * presentation here means a status-vocabulary change touches this table
 * and its two consumers, nothing else.
 */
interface StatusMeta {
  label: string;
  icon: LucideIcon;
  classes: string;
}

const STATUS_META: Record<DownloadStatus, StatusMeta> = {
  pending: {
    label: "Queued",
    icon: Clock,
    classes: "bg-muted text-muted-foreground ring-border",
  },
  running: {
    label: "Downloading",
    icon: DownloadIcon,
    classes:
      "bg-blue-500/10 text-blue-700 ring-blue-600/20 dark:text-blue-400",
  },
  paused: {
    label: "Paused",
    icon: PauseIcon,
    classes:
      "bg-amber-500/10 text-amber-700 ring-amber-600/20 dark:text-amber-400",
  },
  done: {
    label: "Completed",
    icon: CheckCircle2,
    classes:
      "bg-emerald-500/10 text-emerald-700 ring-emerald-600/20 dark:text-emerald-400",
  },
  failed: {
    label: "Failed",
    icon: AlertCircle,
    classes: "bg-red-500/10 text-red-700 ring-red-600/20 dark:text-red-400",
  },
  cancelled: {
    label: "Cancelled",
    icon: XCircle,
    classes: "bg-muted text-muted-foreground ring-border",
  },
};

// ───────────────────────────────────────────────────────────────────────────
// Derivations
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return the progress-bar variant that matches *status*.
 *
 * A terminal status colours the bar definitively — ``done`` green,
 * ``failed`` red, ``cancelled`` amber (to signal "stopped partway").  All
 * active statuses use the default primary colour, so a download in flight
 * reads as "in progress" rather than as a verdict.
 */
function progressVariantFor(status: DownloadStatus): ProgressBarVariant {
  switch (status) {
    case "done":
      return "success";
    case "failed":
      return "danger";
    case "cancelled":
      return "warning";
    case "pending":
    case "running":
    case "paused":
      return "default";
  }
}

/**
 * Compute the bar's value (0–100 or ``null`` for indeterminate) and the
 * human-readable value text.
 *
 * Priority: bytes first (most precise when available), then chapters
 * (always available once a chapter list is fetched), then indeterminate.
 * The rationale is in the module docstring.
 */
function computeProgress(task: DownloadTask): {
  value: number | null;
  valueText: string;
} {
  const p = task.progress;

  if (p.bytes_total > 0) {
    const percent = (p.bytes_downloaded / p.bytes_total) * 100;
    return {
      value: percent,
      valueText: `${formatBytes(p.bytes_downloaded)} / ${formatBytes(
        p.bytes_total,
      )}`,
    };
  }

  if (p.chapters_total > 0) {
    const percent = (p.chapters_completed / p.chapters_total) * 100;
    return {
      value: percent,
      valueText: `${p.chapters_completed} / ${p.chapters_total} chapters`,
    };
  }

  return { value: null, valueText: "" };
}

/**
 * Build the metadata line for *task*.
 *
 * The shape depends on the status — a running task needs speed and ETA, a
 * completed one needs its duration, a failed one needs its error message.
 * Every part is optional and empty parts are omitted, so a task with no
 * byte count never renders a dangling separator.
 */
function buildMetaLine(task: DownloadTask): string {
  const parts: string[] = [task.site_id];
  const p = task.progress;

  // Failed: the error message is the most useful thing to show.
  if (task.status === "failed" && task.error !== null) {
    parts.push(task.error.message);
    return parts.join(" · ");
  }

  // Completed: chapter count, byte size, duration.
  if (task.status === "done" && task.result !== null) {
    const r = task.result;
    if (r.chapters_total > 0) {
      parts.push(`${r.chapters_completed}/${r.chapters_total} chapters`);
    }
    if (r.chapters_failed > 0) {
      parts.push(`${r.chapters_failed} failed`);
    }
    if (r.bytes_downloaded > 0) {
      parts.push(formatBytes(r.bytes_downloaded));
    }
    if (r.duration_seconds > 0) {
      parts.push(`took ${formatDuration(r.duration_seconds)}`);
    }
    return parts.join(" · ");
  }

  // Cancelled: how far it got.
  if (task.status === "cancelled") {
    if (p.chapters_total > 0) {
      parts.push(
        `stopped at ${p.chapters_completed}/${p.chapters_total} chapters`,
      );
    }
    return parts.join(" · ");
  }

  // Pending: nothing more to say beyond the site — the queue position is
  // not part of the DownloadTask wire format.
  if (task.status === "pending") {
    parts.push("in queue");
    return parts.join(" · ");
  }

  // Running or paused: chapter progress, byte progress, speed, ETA.
  if (p.chapters_total > 0) {
    parts.push(`${p.chapters_completed}/${p.chapters_total} chapters`);
  }
  if (p.chapters_failed > 0) {
    parts.push(`${p.chapters_failed} failed`);
  }
  if (p.bytes_total > 0) {
    parts.push(
      `${formatBytes(p.bytes_downloaded)} / ${formatBytes(p.bytes_total)}`,
    );
  }
  if (task.status === "running") {
    if (p.speed_bps > 0) {
      parts.push(formatSpeed(p.speed_bps));
    }
    if (p.eta_seconds !== null) {
      parts.push(`${formatEta(p.eta_seconds)} left`);
    }
  }
  return parts.join(" · ");
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * The status badge.
 *
 * Rendered as a ``<span>`` inside a ``role="status"`` wrapper so that
 * transitions are announced politely.  The wrapper is on every status, not
 * only the terminal ones, because a transition from ``paused`` back to
 * ``running`` is exactly as informative as a transition to ``done``.
 */
function StatusBadge({ status }: { status: DownloadStatus }) {
  const meta = STATUS_META[status];
  const Icon = meta.icon;

  return (
    <span
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full",
        "px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        meta.classes,
      )}
    >
      <Icon className="h-3 w-3" aria-hidden />
      <span>{meta.label}</span>
    </span>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`DownloadItem`.
 *
 * Every callback is optional.  When a callback is omitted, its button is
 * not rendered — see the "Action visibility by status" table in the module
 * docstring for which buttons appear per status when all callbacks are
 * provided.
 */
export interface DownloadItemProps {
  /** The task to display. */
  task: DownloadTask;

  /** Pause the task.  Shown for ``running`` tasks only. */
  onPause?: (taskId: string) => void;

  /** Resume the task.  Shown for ``paused`` tasks only. */
  onResume?: (taskId: string) => void;

  /**
   * Cancel the task server-side, leaving it in the list with status
   * ``cancelled``.  Shown for active tasks (``pending``, ``running``,
   * ``paused``).
   */
  onCancel?: (taskId: string) => void;

  /**
   * Remove the task from the store.  Shown for terminal tasks (``done``,
   * ``failed``, ``cancelled``).  When this is wired to
   * :meth:`~../hooks/useDownload.useDownload`'s ``remove``, calling it on a
   * still-active task also cancels it server-side — but this component
   * never invokes ``onRemove`` for active tasks, matching the table.
   */
  onRemove?: (taskId: string) => void;

  /**
   * When ``true``, every action button is disabled.  Use this while the
   * parent is mid-transition (e.g. a bulk "pause all" is running).  The
   * component does not disable the row based on its own state — that is
   * the parent's responsibility, precisely because the parent is the one
   * with the in-flight information.
   */
  disabled?: boolean;

  /**
   * Additional Tailwind classes merged onto the root element via
   * :func:`cn`.  Typical uses: ``className="border-t"`` for a list layout,
   * or ``className="bg-muted/50"`` to highlight the current row.
   */
  className?: string;
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single download row.  See the module docstring for the full contract.
 *
 * @example Wiring the component in the downloads list:
 *   const downloads = useDownload();
 *
 *   {downloads.tasks.map((task) => (
 *     <DownloadItem
 *       key={task.id}
 *       task={task}
 *       onPause={downloads.pause}
 *       onResume={downloads.resume}
 *       onCancel={downloads.cancel}
 *       onRemove={downloads.remove}
 *     />
 *   ))}
 *
 * @example Read-only display — actions are hidden because no callback is
 * passed:
 *   <DownloadItem task={task} />
 */
export function DownloadItem({
  task,
  onPause,
  onResume,
  onCancel,
  onRemove,
  disabled = false,
  className,
}: DownloadItemProps) {
  const { value: progressValue, valueText: progressValueText } =
    computeProgress(task);
  const metaLine = buildMetaLine(task);
  const variant = progressVariantFor(task.status);

  const isActive =
    task.status === "pending" ||
    task.status === "running" ||
    task.status === "paused";
  const isTerminal = !isActive;

  // Build the action list based on the visibility table and the callbacks
  // the caller actually supplied.  Building an array (rather than JSX
  // fragments) keeps the conditionals flat and the render below trivial.
  const actions: Array<{
    key: string;
    icon: LucideIcon;
    label: string;
    onClick: () => void;
    destructive?: boolean;
  }> = [];

  if (task.status === "running" && onPause !== undefined) {
    actions.push({
      key: "pause",
      icon: PauseIcon,
      label: `Pause download of ${task.manga_title}`,
      onClick: () => onPause(task.id),
    });
  }
  if (task.status === "paused" && onResume !== undefined) {
    actions.push({
      key: "resume",
      icon: Play,
      label: `Resume download of ${task.manga_title}`,
      onClick: () => onResume(task.id),
    });
  }
  if (isActive && onCancel !== undefined) {
    actions.push({
      key: "cancel",
      icon: X,
      label: `Cancel download of ${task.manga_title}`,
      onClick: () => onCancel(task.id),
      destructive: true,
    });
  }
  if (isTerminal && onRemove !== undefined) {
    actions.push({
      key: "remove",
      icon: Trash2,
      label: `Remove ${task.manga_title} from the download list`,
      onClick: () => onRemove(task.id),
      destructive: true,
    });
  }

  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-lg border bg-card p-3",
        "text-card-foreground shadow-sm",
        className,
      )}
    >
      {/* Header row: badge · title · actions ---------------------------- */}
      <div className="flex items-start gap-2">
        <StatusBadge status={task.status} />

        <div className="min-w-0 flex-1">
          <h3
            className="truncate text-sm font-medium"
            title={task.manga_title}
          >
            {task.manga_title}
          </h3>
          <p
            className="truncate text-xs text-muted-foreground"
            title={metaLine}
          >
            {metaLine}
          </p>
        </div>

        {actions.length > 0 && (
          <div className="flex shrink-0 items-center gap-0.5">
            {actions.map((action) => {
              const Icon = action.icon;
              return (
                <button
                  key={action.key}
                  type="button"
                  onClick={action.onClick}
                  disabled={disabled}
                  aria-label={action.label}
                  title={action.label}
                  className={cn(
                    "inline-flex h-7 w-7 items-center justify-center rounded-md",
                    "transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2",
                    "focus-visible:ring-ring focus-visible:ring-offset-2",
                    "focus-visible:ring-offset-background",
                    "disabled:pointer-events-none disabled:opacity-50",
                    action.destructive === true
                      ? "text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      : "text-muted-foreground hover:bg-accent hover:text-foreground",
                  )}
                >
                  <Icon className="h-3.5 w-3.5" aria-hidden />
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Progress bar ------------------------------------------------ */}
      <ProgressBar
        value={progressValue}
        valueText={progressValueText.length > 0 ? progressValueText : undefined}
        variant={variant}
        size="sm"
        showValue={progressValueText.length > 0}
      />
    </div>
  );
}
