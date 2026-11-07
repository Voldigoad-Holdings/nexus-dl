"use client";

/**
 * Home page — the ``/`` route.
 *
 * This is the dashboard the user lands on when they open the app.  Its job
 * is to answer four questions at a glance, in this order:
 *
 * 1. **What is happening right now?**  A strip of four stat cards —
 *    active downloads, completed downloads, library size and sites
 *    available — each a live count derived from the same data every other
 *    page uses.
 * 2. **What just happened?**  A two-column activity split: the five most
 *    recent download tasks on the left (with their live controls), the
 *    five most recently added library entries on the right.
 * 3. **Where do I start?**  When the user has never downloaded a manga
 *    and never added anything to the library, the two activity panels are
 *    replaced by a single onboarding block that explains what NexusDL
 *    does and offers a large call-to-action into ``/search``.
 * 4. **Is everything healthy?**  A small live-updates indicator and a
 *    silent, per-query error state — the page never hides a broken backend
 *    behind an empty-looking dashboard.
 *
 * The page is deliberately a **read-only overview**: it does not own any
 * new state, does not introduce new queries beyond ``/sites`` and
 * ``/library`` (both already cached by the shell), and does not offer any
 * functionality that another page does not already expose.  Its only
 * responsibility is composition.
 *
 * Reuse over duplication
 * ----------------------
 * * Recent downloads reuse :class:`~../components/DownloadItem.DownloadItem`
 *   with the same action wiring as ``/downloads`` — pause, resume,
 *   cancel, remove are all available from the home page, because a user
 *   who notices a stuck download on the dashboard should not have to
 *   navigate to ``/downloads`` to fix it.
 * * Recently added entries reuse :class:`~../components/MangaCard.MangaCard`
 *   in a small two-column grid, so the tile looks identical to what the
 *   library page shows.
 *
 * Queries and cache
 * -----------------
 * * ``["sites"]`` — the same cache entry as the search page and the
 *   library page.  Landing on ``/`` warms it; navigating to ``/search``
 *   afterwards does not trigger a duplicate fetch.
 * * ``["library", { page: 1, per_page: 5 }]`` — a distinct cache entry
 *   from the full library listing (which uses a different page size), but
 *   one that shares the same invalidation trigger (``library.updated``
 *   WebSocket events invalidate the ``["library"]`` prefix, so both
 *   entries are refreshed).
 * * Downloads — read from the downloads store via
 *   :func:`~../hooks/useDownload.useDownload`.  Because the provider
 *   wires WebSocket events into the store and every page uses the same
 *   store, a download started on ``/manga/{id}`` is immediately visible
 *   here, without a refetch.
 *
 * Empty vs. loading vs. error
 * ---------------------------
 * Four distinct states, distinguished so the user never sees a misleading
 * page:
 *
 * * **Loading** — at least one of the two queries (or the downloads
 *   store) has not yet produced data.  A skeleton with the page's real
 *   footprint renders.
 * * **Fresh install** — every query has succeeded and returned zero
 *   items across the board.  The onboarding block renders.
 * * **Empty-per-section** — a subset of panels is empty (e.g. no
 *   downloads but a non-empty library).  Each panel renders its own
 *   small empty state, and the rest of the page renders normally.
 * * **Error** — one or more queries failed.  A scoped error banner appears
 *   above the affected panel with a retry button; the rest of the page
 *   keeps rendering with whatever data succeeded.  A partial failure must
 *   never blank the whole dashboard.
 *
 * Hydration
 * ---------
 * All queries are client-side (``useQuery``) and the page is a client
 * component.  ``useDownload`` returns ``isInitialized: false`` on the
 * server and on the first client render — the skeleton renders in both
 * cases, and the mismatch is impossible because the skeleton is derived
 * only from the boolean flag, not from the store contents.
 */
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Download as DownloadIcon,
  Globe,
  Inbox,
  Loader2,
  RefreshCw,
  Search as SearchIcon,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo } from "react";

import { DownloadItem } from "../components/DownloadItem";
import { MangaCard } from "../components/MangaCard";
import {
  api,
  type LibraryEntry,
  type LibraryListResponse,
  type NexusApiError,
  type SiteListResponse,
} from "../lib/api";
import { cn, formatNumber } from "../lib/utils";
import { useDownload } from "../hooks/useDownload";
import type { DownloadTask } from "../types/download";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Sites catalogue freshness — matches every other page's window. */
const SITES_STALE_TIME_MS = 5 * 60_000;

/** Library list freshness — the provider invalidates on WS events. */
const LIBRARY_STALE_TIME_MS = 30_000;

/** How many items each activity panel shows. */
const RECENT_ITEMS_LIMIT = 5;

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Parse an ISO date string (or numeric timestamp) into epoch milliseconds.
 *
 * The library endpoint is not guaranteed to return ISO-8601 with a fixed
 * timezone, and ``String.prototype.localeCompare`` would silently mis-order
 * any other format (``"15/01/2024"`` vs ``"16/01/2024"`` sorts wrong, and
 * even ISO strings without a leading zero compare wrong under some
 * locales).  Parsing through ``Date`` is the only robust option.
 *
 * Returns ``0`` for a malformed value so that the sort is stable and never
 * throws — the panel simply shows the entries in the order the backend
 * returned them.
 */
function parseAddedAt(value: string | number | null | undefined): number {
  if (value === null || value === undefined) {
    return 0;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : 0;
  }
  const ts = Date.parse(value);
  return Number.isFinite(ts) ? ts : 0;
}

/**
 * Fire an async callback and swallow any rejection, logging it at
 * developer level.
 *
 * The download actions exposed by :func:`~../hooks/useDownload.useDownload`
 * (`pause`, `resume`, `cancel`, `remove`) are ``Promise<void>`` and
 * **can reject** when the API call fails.  A bare ``void promise`` in a
 * React event handler does **not** catch the rejection, which produces an
 * unhandled promise rejection in the console — a noisy failure mode that
 * masks real errors and is penalised by every strict CI setup.
 *
 * The user-facing error path is already wired: each action's failure is
 * reflected in the store and, for ``create``, in ``createError``.  This
 * helper is purely a developer-trace sink so a failed action does not
 * disappear silently.
 */
function fireAndLog(promise: Promise<unknown>): void {
  void promise.catch((err: unknown) => {
    // eslint-disable-next-line no-console
    console.error("HomePage: download action rejected:", err);
  });
}

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/`` route.  See the module docstring for the full contract.
 */
export default function HomePage() {
  // -------------------------------------------------------------------
  // Queries
  // -------------------------------------------------------------------
  const sitesQuery = useQuery<SiteListResponse, NexusApiError>({
    queryKey: ["sites"],
    queryFn: () => api.getSites(),
    staleTime: SITES_STALE_TIME_MS,
    refetchOnWindowFocus: false,
  });

  const libraryQuery = useQuery<LibraryListResponse, NexusApiError>({
    queryKey: ["library", { page: 1, per_page: RECENT_ITEMS_LIMIT }],
    queryFn: () =>
      api.getLibrary({ page: 1, per_page: RECENT_ITEMS_LIMIT }),
    staleTime: LIBRARY_STALE_TIME_MS,
  });

  // -------------------------------------------------------------------
  // Downloads — same store and hooks as the downloads page.
  // -------------------------------------------------------------------
  const downloads = useDownload();

  // -------------------------------------------------------------------
  // Derived state
  // -------------------------------------------------------------------
  const sitesTotal = sitesQuery.data?.items.length ?? 0;
  const libraryTotal = libraryQuery.data?.total ?? 0;

  // Most recent downloads — ``useDownload`` already sorts by ``created_at``
  // descending, so taking a prefix is enough.  Keep the *full* length
  // available to the panel so it can decide whether to show the "View all"
  // link without having to re-read the store.
  const recentDownloads = useMemo<DownloadTask[]>(
    () => downloads.tasks.slice(0, RECENT_ITEMS_LIMIT),
    [downloads.tasks],
  );
  const hasMoreDownloads = downloads.tasks.length > RECENT_ITEMS_LIMIT;

  // Most recently added library entries.  Sorted client-side so the
  // dashboard does not depend on the backend's ordering guarantee for a
  // page size the library endpoint was not designed around.
  const recentLibrary = useMemo<LibraryEntry[]>(() => {
    const items = libraryQuery.data?.items ?? [];
    return [...items]
      .sort((a, b) => parseAddedAt(b.added_at) - parseAddedAt(a.added_at))
      .slice(0, RECENT_ITEMS_LIMIT);
  }, [libraryQuery.data]);

  // -------------------------------------------------------------------
  // Overall page state
  // -------------------------------------------------------------------
  // A page is "loading" until every query that drives a stat or a panel
  // has produced data at least once.  Query errors do not block the page —
  // each panel renders its own error state below.
  const isInitialLoading =
    !downloads.isInitialized ||
    libraryQuery.isLoading ||
    sitesQuery.isLoading;

  // "Fresh install" is only claimable when **every** source succeeded and
  // every source is empty.  If a query errored, we do not know whether the
  // user has content — showing onboarding would be misleading.
  const queriesSucceeded =
    downloads.isInitialized &&
    libraryQuery.isSuccess &&
    sitesQuery.isSuccess;

  const isFreshInstall =
    queriesSucceeded &&
    downloads.tasks.length === 0 &&
    libraryTotal === 0;

  const hasAnyContent =
    !queriesSucceeded ||
    downloads.tasks.length > 0 ||
    libraryTotal > 0;

  // -------------------------------------------------------------------
  // Action handlers — wrapped in ``useCallback`` so their identity is
  // stable across re-renders, and passed through ``fireAndLog`` so a
  // rejected action never becomes an unhandled promise rejection.
  // -------------------------------------------------------------------
  const handlePause = useCallback(
    (id: string) => fireAndLog(downloads.pause(id)),
    [downloads.pause],
  );
  const handleResume = useCallback(
    (id: string) => fireAndLog(downloads.resume(id)),
    [downloads.resume],
  );
  const handleCancel = useCallback(
    (id: string) => fireAndLog(downloads.cancel(id)),
    [downloads.cancel],
  );
  const handleRemove = useCallback(
    (id: string) => fireAndLog(downloads.remove(id)),
    [downloads.remove],
  );

  // -------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      {/* Header ---------------------------------------------- */}
      <header>
        <h1 className="neon-text text-2xl font-bold tracking-tight sm:text-3xl">
          Home
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Your NexusDL at a glance.
        </p>
      </header>

      {/* Stats ----------------------------------------------- */}
      <section
        aria-label="Overview"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        <StatCard
          label="Active"
          value={downloads.counts.active}
          icon={Loader2}
          tone="primary"
          href="/downloads?status=active"
          isLoading={!downloads.isInitialized}
          error={downloads.error}
        />
        <StatCard
          label="Completed"
          value={downloads.counts.done}
          icon={CheckCircle2}
          tone="success"
          href="/downloads?status=completed"
          isLoading={!downloads.isInitialized}
          error={downloads.error}
        />
        <StatCard
          label="Library"
          value={libraryTotal}
          icon={BookOpen}
          tone="neutral"
          href="/library"
          isLoading={libraryQuery.isLoading}
          error={libraryQuery.error}
        />
        <StatCard
          label="Sites"
          value={sitesTotal}
          icon={Globe}
          tone="neutral"
          href="/search"
          isLoading={sitesQuery.isLoading}
          error={sitesQuery.error}
        />
      </section>

      {/* Content --------------------------------------------- */}
      {isInitialLoading && <LoadingSkeleton />}

      {!isInitialLoading && isFreshInstall && <OnboardingState />}

      {!isInitialLoading && hasAnyContent && !isFreshInstall && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <RecentDownloadsPanel
            tasks={recentDownloads}
            hasMore={hasMoreDownloads}
            error={downloads.error}
            onRetry={downloads.refetch}
            onPause={handlePause}
            onResume={handleResume}
            onCancel={handleCancel}
            onRemove={handleRemove}
          />

          <RecentlyAddedPanel
            entries={recentLibrary}
            total={libraryTotal}
            error={libraryQuery.error}
            isLoading={libraryQuery.isLoading}
            onRetry={() => void libraryQuery.refetch()}
          />
        </div>
      )}
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Stats
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single stat card in the overview strip.
 *
 * Rendered as a ``<Link>`` (not a button) because every stat is a link to
 * the page it summarises — the user's mental model is "click the stat to
 * see the details", and a link matches that.  ``aria-label`` narrates both
 * the value and the destination, so the card is self-describing to
 * assistive technology.
 *
 * When ``isLoading`` is true, the number renders as a pulsing skeleton.
 * When ``error`` is not ``null``, an inline warning icon replaces the
 * number and the card's ``aria-label`` notes the failure — the value is
 * unknown, not zero.
 */
function StatCard({
  label,
  value,
  icon: Icon,
  tone,
  href,
  isLoading,
  error,
}: {
  label: string;
  value: number;
  icon: LucideIcon;
  tone: "primary" | "success" | "neutral";
  href: string;
  isLoading: boolean;
  error: NexusApiError | null;
}) {
  const toneClasses: Record<typeof tone, string> = {
    primary: "text-primary",
    success: "text-emerald-500",
    neutral: "text-muted-foreground",
  };

  const ariaLabel =
    error !== null
      ? `${label}: unavailable`
      : isLoading
        ? `${label}: loading`
        : `${label}: ${formatNumber(value)}`;

  return (
    <Link
      href={href}
      aria-label={ariaLabel}
      className={cn(
        "group flex flex-col items-start gap-1 rounded-lg border p-3",
        "text-left transition-all duration-150",
        "border-border bg-card hover:border-primary/40 hover:bg-accent/30",
        "focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background",
      )}
    >
      <div className="flex w-full items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        <Icon className={cn("h-4 w-4", toneClasses[tone])} aria-hidden />
      </div>

      {error !== null ? (
        <span
          className="inline-flex items-center gap-1 font-mono text-sm text-destructive"
          title={error.message}
        >
          <AlertCircle className="h-4 w-4" aria-hidden />
          <span>—</span>
        </span>
      ) : isLoading ? (
        <span className="h-7 w-12 animate-pulse rounded bg-muted" />
      ) : (
        <span className="font-mono text-2xl font-bold tabular-nums text-foreground">
          {formatNumber(value)}
        </span>
      )}
    </Link>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Activity panels
// ───────────────────────────────────────────────────────────────────────────

/**
 * The "Recent downloads" panel.
 *
 * Wraps :class:`~../components/DownloadItem.DownloadItem` — every
 * action the downloads page exposes is available here, because the user
 * should not have to navigate away from the dashboard to unblock a stuck
 * task.  The header carries a "View all" link into ``/downloads``.
 *
 * Renders three distinct states:
 *
 * * **Error with no data** — a scoped error banner with a retry button.
 * * **Empty** — a small empty block with a link to ``/search``.
 * * **Populated** — the recent tasks, followed by a "View all" link when
 *   ``hasMore`` is true (i.e. the store holds more than the panel shows).
 */
function RecentDownloadsPanel({
  tasks,
  hasMore,
  error,
  onRetry,
  onPause,
  onResume,
  onCancel,
  onRemove,
}: {
  tasks: readonly DownloadTask[];
  hasMore: boolean;
  error: NexusApiError | null;
  onRetry: () => Promise<void>;
  onPause: (taskId: string) => void;
  onResume: (taskId: string) => void;
  onCancel: (taskId: string) => void;
  onRemove: (taskId: string) => void;
}) {
  return (
    <section
      aria-labelledby="recent-downloads-heading"
      className="flex flex-col gap-3"
    >
      <PanelHeader
        id="recent-downloads-heading"
        title="Recent downloads"
        viewAllHref="/downloads"
        viewAllLabel="View all"
      />

      {error !== null && (
        <ErrorBanner
          title="Could not load downloads"
          message={error.message}
          onRetry={() => void onRetry()}
        />
      )}

      {error === null && tasks.length === 0 && (
        <PanelEmpty
          icon={Inbox}
          title="No downloads yet"
          description="Start a download from a manga page to see it here."
          actionLabel="Search for a manga"
          actionHref="/search"
        />
      )}

      {tasks.length > 0 && (
        <div className="flex flex-col gap-2">
          {tasks.map((task) => (
            <DownloadItem
              key={task.id}
              task={task}
              onPause={onPause}
              onResume={onResume}
              onCancel={onCancel}
              onRemove={onRemove}
            />
          ))}
          {hasMore && (
            <Link
              href="/downloads"
              className={cn(
                "mt-1 inline-flex items-center gap-1 self-start rounded-md",
                "px-2 py-1 text-xs font-medium text-muted-foreground",
                "hover:bg-accent hover:text-foreground",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring",
              )}
            >
              View all downloads
              <ArrowRight className="h-3 w-3" aria-hidden />
            </Link>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * The "Recently added" panel.
 *
 * Renders the most recently added library entries as :class:`MangaCard`
 * tiles in a small grid.  The header carries the total library count and a
 * "View all" link into ``/library``.
 */
function RecentlyAddedPanel({
  entries,
  total,
  error,
  isLoading,
  onRetry,
}: {
  entries: readonly LibraryEntry[];
  total: number;
  error: NexusApiError | null;
  isLoading: boolean;
  onRetry: () => void;
}) {
  return (
    <section
      aria-labelledby="recently-added-heading"
      className="flex flex-col gap-3"
    >
      <PanelHeader
        id="recently-added-heading"
        title="Recently added"
        subtitle={total > 0 ? formatNumber(total) : undefined}
        viewAllHref="/library"
        viewAllLabel="View all"
      />

      {error !== null && (
        <ErrorBanner
          title="Could not load the library"
          message={error.message}
          onRetry={onRetry}
        />
      )}

      {error === null && !isLoading && entries.length === 0 && (
        <PanelEmpty
          icon={BookOpen}
          title="Your library is empty"
          description="Add a manga to your library to see it here."
          actionLabel="Search for a manga"
          actionHref="/search"
        />
      )}

      {error === null && entries.length > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {entries.map((entry) => (
            <MangaCard key={entry.manga.id} data={entry.manga} />
          ))}
        </div>
      )}
    </section>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Shared panel sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * The header of an activity panel: title, optional subtitle, and a
 * "View all" link that aligns with the panel's right edge.
 */
function PanelHeader({
  id,
  title,
  subtitle,
  viewAllHref,
  viewAllLabel,
}: {
  id: string;
  title: string;
  subtitle?: string;
  viewAllHref: string;
  viewAllLabel: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <h2
        id={id}
        className="text-lg font-semibold tracking-tight"
      >
        {title}
        {subtitle !== undefined && (
          <span className="ml-2 text-sm font-normal text-muted-foreground">
            ({subtitle})
          </span>
        )}
      </h2>
      <Link
        href={viewAllHref}
        className={cn(
          "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5",
          "text-xs font-medium text-muted-foreground",
          "hover:bg-accent hover:text-foreground",
          "focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-ring",
        )}
      >
        {viewAllLabel}
        <ArrowRight className="h-3 w-3" aria-hidden />
      </Link>
    </div>
  );
}

/**
 * A scoped error banner — one per panel, so a failure in one panel does not
 * blank the other.
 */
function ErrorBanner({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-md border border-destructive/30",
        "bg-destructive/10 p-3 text-sm text-destructive",
      )}
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1">
        <p className="font-medium">{title}</p>
        <p className="mt-0.5 text-xs opacity-80">{message}</p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className={cn(
          "inline-flex items-center gap-1 rounded px-2 py-1",
          "text-xs font-medium",
          "hover:bg-destructive/20",
          "focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-destructive/50",
        )}
      >
        <RefreshCw className="h-3 w-3" aria-hidden />
        Retry
      </button>
    </div>
  );
}

/**
 * A small panel-scoped empty state with a call-to-action link.
 */
function PanelEmpty({
  icon: Icon,
  title,
  description,
  actionLabel,
  actionHref,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  actionLabel: string;
  actionHref: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-2 rounded-lg border",
        "border-dashed px-4 py-8 text-center",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "grid h-10 w-10 place-items-center rounded-full",
          "bg-muted text-muted-foreground",
        )}
      >
        <Icon className="h-5 w-5" />
      </span>
      <div>
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {description}
        </p>
      </div>
      <Link
        href={actionHref}
        className={cn(
          "mt-1 inline-flex items-center gap-1 rounded-md px-2.5 py-1",
          "text-xs font-medium",
          "border bg-background hover:bg-accent",
          "focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-ring focus-visible:ring-offset-2",
          "focus-visible:ring-offset-background",
        )}
      >
        {actionLabel}
        <ArrowRight className="h-3 w-3" aria-hidden />
      </Link>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Onboarding
// ───────────────────────────────────────────────────────────────────────────

/**
 * The full-width onboarding block shown on a **fresh install**.
 */
function OnboardingState() {
  return (
    <section
      aria-labelledby="onboarding-heading"
      className={cn(
        "neon-card",
        "flex flex-col items-center gap-6 p-8 text-center",
        "sm:p-12",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "grid h-16 w-16 place-items-center rounded-full",
          "bg-primary/10 text-primary",
          "shadow-[0_0_32px_hsl(var(--primary)/0.35)]",
          "animate-neon-pulse",
        )}
      >
        <Sparkles className="h-8 w-8" />
      </span>

      <div className="max-w-lg">
        <h2
          id="onboarding-heading"
          className="text-xl font-semibold tracking-tight sm:text-2xl"
        >
          Welcome to NexusDL
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Download manga, webtoons, comics and doujinshi from every
          supported site into a single local library — packaged as CBZ,
          CB7, PDF or plain folders, all from this interface.
        </p>
      </div>

      <ol className="grid w-full max-w-xl grid-cols-1 gap-3 text-left sm:grid-cols-3">
        <OnboardingStep
          number={1}
          icon={SearchIcon}
          title="Search"
          description="Look for a series across every enabled site at once."
        />
        <OnboardingStep
          number={2}
          icon={BookOpen}
          title="Add to library"
          description="Track a series locally so you can pick up where you left off."
        />
        <OnboardingStep
          number={3}
          icon={DownloadIcon}
          title="Download"
          description="Pick the chapters you want and NexusDL packages them for you."
        />
      </ol>

      <Link
        href="/search"
        className={cn(
          "inline-flex h-11 items-center gap-2 rounded-md px-5",
          "bg-primary text-sm font-semibold text-primary-foreground",
          "transition-[background-color,box-shadow] duration-150",
          "hover:bg-primary/90 hover:shadow-[0_0_24px_hsl(var(--primary)/0.5)]",
          "focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-ring focus-visible:ring-offset-2",
          "focus-visible:ring-offset-background",
        )}
      >
        <SearchIcon className="h-4 w-4" aria-hidden />
        Find your first manga
        <ArrowRight className="h-4 w-4" aria-hidden />
      </Link>
    </section>
  );
}

/**
 * A single step in the onboarding list.
 */
function OnboardingStep({
  number,
  icon: Icon,
  title,
  description,
}: {
  number: number;
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <li className="flex items-start gap-3 rounded-lg border bg-background/50 p-3">
      <div className="flex flex-col items-center gap-1">
        <span
          aria-hidden
          className={cn(
            "grid h-8 w-8 place-items-center rounded-full",
            "bg-primary/10 text-primary",
          )}
        >
          <Icon className="h-4 w-4" />
        </span>
        <span
          aria-hidden
          className="text-[10px] font-mono uppercase text-muted-foreground"
        >
          {number.toString().padStart(2, "0")}
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
          {description}
        </p>
      </div>
    </li>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Loading
// ───────────────────────────────────────────────────────────────────────────

/**
 * Skeleton shown while the initial queries are in flight.
 */
function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true">
      <span className="sr-only">Loading dashboard…</span>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {[0, 1].map((panelIndex) => (
          <div
            key={panelIndex}
            className="flex flex-col gap-3"
          >
            <div className="h-6 w-40 animate-pulse rounded bg-muted" />

            {panelIndex === 0 ? (
              <>
                {[0, 1, 2].map((rowIndex) => (
                  <div
                    key={rowIndex}
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
              </>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                {[0, 1, 2, 3].map((cardIndex) => (
                  <div key={cardIndex} className="flex flex-col gap-2">
                    <div className="aspect-[2/3] animate-pulse rounded-lg bg-muted" />
                    <div className="h-4 animate-pulse rounded bg-muted" />
                    <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
