"use client";

/**
 * Downloads page — the ``/downloads`` route.
 *
 * Layout
 * ------
 * A single-column dashboard:
 *
 * 1. **Header** — title, live status pill (WebSocket connection), and a
 *    "Clear finished" action.
 * 2. **Stat strip** — four cards summarising active / completed / failed /
 *    total tasks.  Clicking a card filters the list below by that status.
 * 3. **Filter tabs** — "All", "Active", "Completed", "Failed".  The active
 *    tab is reflected in the URL query string (``?status=active``), so a
 *    filtered view is bookmarkable and shareable.
 * 4. **Task list** — one :class:`~../components/DownloadItem.DownloadItem`
 *    per task, with the neon-card styling.  Terminal tasks are shown only
 *    under the "All", "Completed" and "Failed" tabs; the "Active" tab
 *    hides them.
 * 5. **Empty states** — three distinct messages: "loading", "no downloads
 *    yet" and "no downloads match this filter".
 *
 * State ownership
 * ---------------
 * * Task list, counts, pause/resume/cancel/remove — all from
 *   :func:`~../hooks/useDownload.useDownload`.  The page never touches the
 *   API or the store directly.
 * * WebSocket connection status — from
 *   :func:`~../hooks/useWebSocket.useWebSocket`.  The provider already
 *   wires the store; this hook is invoked **only** to observe the
 *   connection state for the status pill.  It is idempotent — calling it
 *   in a page is safe, because the underlying client is a process-wide
 *   singleton.
 * * Filter selection — local state, mirrored into the URL.  When the URL
 *   changes (back/forward navigation), the tab updates; when the tab is
 *   clicked, the URL updates via ``router.replace`` so the back button
 *   does not create a history entry for every tab click.
 *
 * URL synchronisation
 * -------------------
 * ``useSearchParams()`` requires a Suspense boundary in Next.js 14, so the
 * default export wraps the body in one.  The fallback is a skeleton with
 * the same footprint as the real list, so there is no layout shift.
 *
 * Hydration
 * ---------
 * ``useDownload`` defers the initial fetch to a client effect and reads
 * the store via TanStack-Query-independent selectors, so the page renders
 * deterministically on the server (empty list, loading=true) and hydrates
 * cleanly.  ``useWebSocket`` similarly returns ``"idle"`` on the server.
 * Neither hook produces a hydration mismatch.
 */
import {
  AlertCircle,
  CheckCircle2,
  Inbox,
  Loader2,
  RefreshCw,
  Trash2,
  Wifi,
  WifiOff,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef } from "react";

import { DownloadItem } from "../../components/DownloadItem";
import { useDownload } from "../../hooks/useDownload";
import { useWebSocket } from "../../hooks/useWebSocket";
import { cn } from "../../lib/utils";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * The filter tabs, in display order.
 *
 * ``id`` is used both as the URL value (``?status=active``) and as the
 * discriminator in the filtering logic below.  Keeping the two coupled
 * through this single table means adding a new tab touches exactly one
 * place.
 */
interface FilterTab {
  id: "all" | "active" | "completed" | "failed";
  label: string;
  description: string;
}

const FILTER_TABS: readonly FilterTab[] = [
  { id: "all", label: "All", description: "Every download task" },
  { id: "active", label: "Active", description: "Queued, running or paused" },
  { id: "completed", label: "Completed", description: "Successfully finished" },
  { id: "failed", label: "Failed", description: "Failed or cancelled" },
] as const;

/** Default tab when no valid ``?status=`` is present. */
const DEFAULT_TAB: FilterTab["id"] = "all";

/**
 * Return ``true`` when *value* is a valid tab id.
 *
 * Used to sanitise the ``?status=`` query parameter — an arbitrary string
 * in the URL should not leave the page in a state where no tab is active.
 */
function isFilterTabId(value: string | null): value is FilterTab["id"] {
  if (value === null) {
    return false;
  }
  return FILTER_TABS.some((tab) => tab.id === value);
}

/**
 * Exhaustiveness helper for ``switch`` over the tab union.
 *
 * Never called at runtime; its presence makes the compiler fail if a new
 * ``FilterTab["id"]`` is added and a ``switch`` is not updated.
 */
function assertNeverTab(value: never): never {
  throw new Error(`Unhandled filter tab: ${String(value)}`);
}

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/downloads`` route.  See the module docstring for the full
 * contract.
 */
export default function DownloadsPage() {
  return (
    <Suspense fallback={<DownloadsPageFallback />}>
      <DownloadsBody />
    </Suspense>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Body
// ───────────────────────────────────────────────────────────────────────────

function DownloadsBody() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const downloads = useDownload();
  const ws = useWebSocket();

  // Active tab, derived from the URL.  A bad ``?status=`` value collapses
  // to the default, so the page never renders with no active tab.
  const rawStatus = searchParams.get("status");
  const activeTab: FilterTab["id"] = isFilterTabId(rawStatus)
    ? rawStatus
    : DEFAULT_TAB;

  // When the user switches tabs, update the URL **without** adding a
  // history entry — otherwise the back button becomes a stack of tab
  // toggles, which is surprising.
  //
  // The path is always explicit (``pathname`` + optional query): passing
  // only ``"?status=active"`` to ``router.replace`` is fragile in the App
  // Router and can lose the current route on some Next versions.
  const setActiveTab = useCallback(
    (tab: FilterTab["id"]) => {
      const params = new URLSearchParams(searchParams.toString());
      if (tab === DEFAULT_TAB) {
        params.delete("status");
      } else {
        params.set("status", tab);
      }
      const query = params.toString();
      const target = query.length > 0 ? `${pathname}?${query}` : pathname;
      router.replace(target, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  // -------------------------------------------------------------------
  // Filtered task list
  // -------------------------------------------------------------------
  const visibleTasks = useMemo(() => {
    switch (activeTab) {
      case "all":
        return downloads.tasks;
      case "active":
        return downloads.activeTasks;
      case "completed":
        return downloads.completedTasks;
      case "failed":
        return downloads.failedTasks;
      default:
        return assertNeverTab(activeTab);
    }
  }, [
    activeTab,
    downloads.tasks,
    downloads.activeTasks,
    downloads.completedTasks,
    downloads.failedTasks,
  ]);

  // -------------------------------------------------------------------
  // Derived display state
  // -------------------------------------------------------------------
  const showInitialLoading =
    downloads.isLoading && !downloads.isInitialized;
  const isEmptyOverall =
    downloads.isInitialized && downloads.tasks.length === 0;
  const isEmptyFiltered =
    downloads.isInitialized &&
    downloads.tasks.length > 0 &&
    visibleTasks.length === 0;

  const hasFinished = useMemo(
    () =>
      downloads.completedTasks.length + downloads.failedTasks.length > 0,
    [downloads.completedTasks.length, downloads.failedTasks.length],
  );

  // -------------------------------------------------------------------
  // Re-fetch on visibility change
  // -------------------------------------------------------------------
  // A user who leaves the tab open in the background may miss WS events
  // (e.g. the OS suspends the socket).  A refresh when the tab becomes
  // visible again reconciles the store with the backend without polling.
  //
  // ``refetch`` is stashed in a ref so the listener is attached **once**
  // for the component lifetime, regardless of whether ``useDownload``
  // returns a stable callback.  Depending on it directly would re-attach
  // the listener on every render (a real bug if the hook uses an inline
  // arrow).
  const refetchRef = useRef(downloads.refetch);
  refetchRef.current = downloads.refetch;

  useEffect(() => {
    if (typeof document === "undefined") {
      return;
    }
    const handleVisibility = (): void => {
      if (document.visibilityState === "visible") {
        void refetchRef.current();
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  // Confirmation for the destructive "Clear finished" action.  Removing
  // every terminal task in one click is easy to trigger by accident, and
  // there is no undo.
  const handleClearFinished = useCallback(() => {
    if (typeof window === "undefined") {
      return;
    }
    const confirmed = window.confirm(
      "Remove every completed, failed and cancelled task from this list?",
    );
    if (confirmed) {
      downloads.clearFinished();
    }
  }, [downloads.clearFinished]);

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      {/* Header ------------------------------------------------- */}
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="neon-text text-2xl font-bold tracking-tight sm:text-3xl">
            Downloads
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Manage every download task on this account.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <ConnectionPill state={ws.state} isDisabled={ws.isDisabled} />
          <button
            type="button"
            onClick={() => void downloads.refetch()}
            disabled={downloads.isLoading}
            aria-label="Refresh download list"
            title="Refresh download list"
            className={cn(
              "inline-flex h-9 items-center gap-1.5 rounded-md",
              "border bg-background px-3 text-sm font-medium",
              "text-muted-foreground hover:bg-accent hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            {downloads.isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden />
            )}
            <span className="hidden sm:inline">Refresh</span>
          </button>
        </div>
      </header>

      {/* Initial fetch error -------------------------------- */}
      {downloads.error !== null && (
        <div
          role="alert"
          className={cn(
            "flex items-start gap-2 rounded-md border border-destructive/30",
            "bg-destructive/10 p-3 text-sm text-destructive",
          )}
        >
          <AlertCircle
            className="mt-0.5 h-4 w-4 shrink-0"
            aria-hidden
          />
          <div className="flex-1">
            <p className="font-medium">Could not load downloads</p>
            <p className="mt-0.5 text-xs opacity-80">
              {downloads.error.message}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void downloads.refetch()}
            className={cn(
              "rounded px-2 py-1 text-xs font-medium",
              "text-destructive hover:bg-destructive/20",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-destructive/50",
            )}
          >
            Retry
          </button>
        </div>
      )}

      {/* Stat cards ------------------------------------------ */}
      <section
        aria-label="Download statistics"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        <StatCard
          label="Active"
          value={downloads.counts.active}
          icon={Loader2}
          tone="primary"
          active={activeTab === "active"}
          onClick={() => setActiveTab("active")}
          disabled={showInitialLoading}
        />
        <StatCard
          label="Completed"
          value={downloads.counts.done}
          icon={CheckCircle2}
          tone="success"
          active={activeTab === "completed"}
          onClick={() => setActiveTab("completed")}
          disabled={showInitialLoading}
        />
        <StatCard
          label="Failed"
          value={downloads.counts.failed + downloads.counts.cancelled}
          icon={XCircle}
          tone="danger"
          active={activeTab === "failed"}
          onClick={() => setActiveTab("failed")}
          disabled={showInitialLoading}
        />
        <StatCard
          label="Total"
          value={downloads.counts.total}
          icon={Inbox}
          tone="neutral"
          active={activeTab === "all"}
          onClick={() => setActiveTab("all")}
          disabled={showInitialLoading}
        />
      </section>

      {/* Filter tabs ---------------------------------------- */}
      <nav
        aria-label="Filter downloads"
        className="flex flex-wrap items-center gap-1 border-b"
      >
        {FILTER_TABS.map((tab) => {
          const isActive = activeTab === tab.id;
          const count =
            tab.id === "all"
              ? downloads.counts.total
              : tab.id === "active"
                ? downloads.counts.active
                : tab.id === "completed"
                  ? downloads.counts.done
                  : downloads.counts.failed + downloads.counts.cancelled;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              aria-current={isActive ? "page" : undefined}
              title={tab.description}
              className={cn(
                "relative inline-flex items-center gap-1.5 px-3 py-2",
                "text-sm font-medium transition-colors",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring focus-visible:ring-offset-2",
                "focus-visible:ring-offset-background",
                isActive
                  ? "text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <span>{tab.label}</span>
              <span
                className={cn(
                  "rounded-full px-1.5 py-0.5 text-xs font-mono",
                  isActive
                    ? "bg-primary/20 text-primary"
                    : "bg-muted text-muted-foreground",
                )}
                aria-hidden
              >
                {count}
              </span>
              {isActive && (
                <span
                  aria-hidden
                  className={cn(
                    "absolute inset-x-2 -bottom-px h-0.5 rounded-full",
                    "bg-primary shadow-[0_0_8px_hsl(var(--primary)/0.8)]",
                  )}
                />
              )}
            </button>
          );
        })}

        {/* Clear finished ------------------------------- */}
        {hasFinished && (
          <button
            type="button"
            onClick={handleClearFinished}
            aria-label="Clear finished downloads"
            title="Remove every completed, failed and cancelled task from the list"
            className={cn(
              "ml-auto inline-flex items-center gap-1.5 rounded px-2 py-1",
              "text-xs font-medium text-muted-foreground",
              "hover:bg-accent hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring",
            )}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
            <span>Clear finished</span>
          </button>
        )}
      </nav>

      {/* Task list ------------------------------------------ */}
      <section aria-label="Download tasks" className="flex flex-col gap-2">
        {showInitialLoading && <LoadingState />}

        {!showInitialLoading && isEmptyOverall && (
          <EmptyState
            icon={Inbox}
            title="No downloads yet"
            description="Search for a manga and start a download to see it here."
          />
        )}

        {!showInitialLoading && isEmptyFiltered && (
          <EmptyState
            icon={Inbox}
            title="No downloads match this filter"
            description={
              FILTER_TABS.find((t) => t.id === activeTab)?.description ??
              "Try a different filter."
            }
          />
        )}

        {!showInitialLoading &&
          visibleTasks.map((task) => (
            <DownloadItem
              key={task.id}
              task={task}
              onPause={(id) => {
                void downloads.pause(id);
              }}
              onResume={(id) => {
                void downloads.resume(id);
              }}
              onCancel={(id) => {
                void downloads.cancel(id);
              }}
              onRemove={(id) => {
                void downloads.remove(id);
              }}
            />
          ))}
      </section>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * The WebSocket connection pill shown in the header.
 *
 * Five visual states, derived from :type:`~../../lib/ws.ConnectionState`
 * and the ``isDisabled`` flag from the hook:
 *
 * * ``idle`` / ``closed``         — "Live updates off" (grey)
 * * ``connecting`` / ``reconnecting`` — "Connecting…" (amber, pulsing)
 * * ``connected``                 — "Live" (green, glowing)
 * * ``failed``                    — "Disconnected" (red)
 *
 * The pill is purely informational.  Reconnect handling lives in the
 * hook and in the app's provider — the user has no reason to click
 * anything here; if the connection is truly dead, the "Refresh" button
 * in the header is the correct escape hatch.
 */
function ConnectionPill({
  state,
  isDisabled,
}: {
  state: ReturnType<typeof useWebSocket>["state"];
  isDisabled: boolean;
}) {
  let label: string;
  let icon: LucideIcon;
  let classes: string;
  let animate = false;

  if (isDisabled || state === "idle" || state === "closed") {
    label = "Live updates off";
    icon = WifiOff;
    classes = "bg-muted text-muted-foreground ring-border";
  } else if (state === "connecting" || state === "reconnecting") {
    label = "Connecting…";
    icon = Loader2;
    classes =
      "bg-amber-500/10 text-amber-700 ring-amber-600/20 dark:text-amber-400";
    animate = true;
  } else if (state === "connected") {
    label = "Live";
    icon = Wifi;
    classes =
      "bg-emerald-500/10 text-emerald-700 ring-emerald-600/20 dark:text-emerald-400";
  } else if (state === "failed") {
    label = "Disconnected";
    icon = WifiOff;
    classes = "bg-red-500/10 text-red-700 ring-red-600/20 dark:text-red-400";
  } else {
    // Defensive fallback: a new state value must be handled explicitly
    // in this component rather than silently masquerading as "failed".
    label = "Unknown";
    icon = WifiOff;
    classes = "bg-muted text-muted-foreground ring-border";
  }

  const Icon = icon;

  return (
    <span
      role="status"
      aria-live="polite"
      aria-label={`Live updates: ${label}`}
      className={cn(
        "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-md",
        "px-2.5 text-xs font-medium ring-1 ring-inset",
        classes,
      )}
    >
      <Icon
        className={cn("h-3.5 w-3.5", animate && "animate-spin")}
        aria-hidden
      />
      <span className="hidden sm:inline">{label}</span>
    </span>
  );
}

/**
 * A single stat card in the top strip.
 *
 * Rendered as a ``<button>`` because every card filters the list when
 * clicked — the icons and count are inside the button, which is the
 * correct accessible structure for a single-action tile.
 */
function StatCard({
  label,
  value,
  icon: Icon,
  tone,
  active,
  onClick,
  disabled,
}: {
  label: string;
  value: number;
  icon: LucideIcon;
  tone: "primary" | "success" | "danger" | "neutral";
  active: boolean;
  onClick: () => void;
  disabled: boolean;
}) {
  const toneClasses: Record<typeof tone, string> = {
    primary: "text-primary",
    success: "text-emerald-500",
    danger: "text-red-500",
    neutral: "text-muted-foreground",
  };

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
      aria-label={`${label}: ${value}. ${active ? "Currently active filter." : "Click to filter."}`}
      className={cn(
        "group flex flex-col items-start gap-1 rounded-lg border p-3",
        "text-left transition-all duration-150",
        "focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background",
        "disabled:cursor-not-allowed disabled:opacity-50",
        active
          ? "border-primary/60 bg-primary/5 shadow-[0_0_16px_hsl(var(--primary)/0.15)]"
          : "border-border bg-card hover:border-primary/40 hover:bg-accent/30",
      )}
    >
      <div className="flex w-full items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        <Icon
          className={cn("h-4 w-4", toneClasses[tone])}
          aria-hidden
        />
      </div>
      <span
        className={cn(
          "font-mono text-2xl font-bold tabular-nums",
          active ? "text-foreground" : "text-foreground/90",
        )}
      >
        {value}
      </span>
    </button>
  );
}

/**
 * Skeleton shown while the initial fetch is in flight.
 *
 * Uses the same vertical rhythm as a real task row (badge + two text
 * lines + progress bar), so the layout does not shift when the real rows
 * replace the skeletons.
 *
 * ``aria-live`` is intentionally **not** used here: the ``sr-only`` text
 * is static, so an ``aria-live`` region would never announce anything
 * (screen readers only announce *changes*).  The section's ``aria-busy``
 * is the correct signal for "content is loading".
 */
function LoadingState() {
  return (
    <div className="flex flex-col gap-2" aria-busy="true">
      <span className="sr-only">Loading downloads…</span>
      {[0, 1, 2].map((index) => (
        <div
          key={index}
          className="flex animate-pulse flex-col gap-2 rounded-lg border bg-card p-3"
        >
          <div className="flex items-start gap-2">
            <div className="h-5 w-20 rounded-full bg-muted" />
            <div className="flex-1 space-y-1.5">
              <div className="h-4 w-1/3 rounded bg-muted" />
              <div className="h-3 w-2/3 rounded bg-muted" />
            </div>
          </div>
          <div className="h-2 w-full rounded-full bg-muted" />
        </div>
      ))}
    </div>
  );
}

/**
 * The empty-state block shared by "no downloads yet" and "no match".
 *
 * The two cases are distinguished by the icon and copy only — the
 * structure is identical, so a future "start a download" CTA can be added
 * in one place.
 */
function EmptyState({
  icon: Icon,
  title,
  description,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-lg border border-dashed",
        "px-6 py-12 text-center",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "grid h-12 w-12 place-items-center rounded-full",
          "bg-muted text-muted-foreground",
        )}
      >
        <Icon className="h-6 w-6" />
      </span>
      <div>
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Fallback (Suspense)
// ───────────────────────────────────────────────────────────────────────────

/**
 * Skeleton rendered while the Suspense boundary resolves
 * ``useSearchParams()``.
 *
 * Matches the real page's structure — header, four stat cards, tabs and
 * three loading rows — so there is no layout shift when the real content
 * replaces the fallback.
 */
function DownloadsPageFallback() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      <div className="space-y-2">
        <div className="h-9 w-40 animate-pulse rounded bg-muted" />
        <div className="h-4 w-64 animate-pulse rounded bg-muted" />
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <div
            key={index}
            className="h-[76px] animate-pulse rounded-lg border bg-card"
          />
        ))}
      </div>
      <div className="flex gap-1 border-b pb-2">
        {[0, 1, 2, 3].map((index) => (
          <div
            key={index}
            className="h-8 w-24 animate-pulse rounded bg-muted"
          />
        ))}
      </div>
      <LoadingState />
    </div>
  );
}
