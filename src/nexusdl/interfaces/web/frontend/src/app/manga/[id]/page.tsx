"use client";

/**
 * Manga detail page — the ``/manga/[id]`` route.
 *
 * This is the page a user lands on after clicking a search result or a
 * library card.  Its single job is to help them **start a download**: it
 * presents the metadata (cover, synopsis, tags, status), enumerates every
 * chapter, and lets the user select the chapters they want and submit them
 * as a single :type:`~../../../types/download.CreateDownloadRequest`.
 *
 * Data flow
 * ---------
 * Two independent server queries, both cached by TanStack Query:
 *
 * * ``api.getManga(id)`` — the :type:`~../../../types/manga.Manga` resource.
 * * ``api.getMangaChapters(id)`` — every :type:`~../../../types/manga.Chapter`
 *   for the series, oldest first.
 *
 * The two queries are treated separately: if the manga loads but the
 * chapters fail, the user still sees the metadata and a scoped chapter
 * error with a retry button.  The reverse — chapters loaded but manga failed
 * — is impossible (chapters are only fetched when a manga id is present).
 *
 * Live download status
 * --------------------
 * The "Downloads for this series" section and the per-chapter status
 * indicators both read from :class:`~../../../store/downloads.DownloadsStore`.
 * Two **distinct subscriptions** keep them fast:
 *
 * * The section subscribes to the full task objects for this manga
 *   (progress updates require it).
 * * The chapter statuses are derived from a **signature** that only changes
 *   when a chapter transitions state (idle → downloading → downloaded or
 *   failed).  Progress byte updates do not touch the signature, so a
 *   1000-chapter list does not re-render on every progress tick.
 *
 * Selection and submission
 * ------------------------
 * Selection is local state, cleared on successful submission.  Submission
 * calls :meth:`~../../../hooks/useDownload.useDownload`'s ``create`` action
 * with the user's default packaging preferences (read from the settings
 * store) — the user's choice is remembered on ``/settings``, not repeated
 * on every download.
 *
 * Layout
 * ------
 * Hero (cover + metadata + description + tags) above a chapter section.
 * Fully responsive: single-column on mobile, cover and info side-by-side
 * from the ``md`` breakpoint up.
 *
 * Hydration
 * ---------
 * Both queries are client-side (``useQuery``) and the page is a client
 * component, so there is no server-render phase to mismatch with.  The
 * settings store uses ``skipHydration`` and returns the defaults on both
 * server and first client render.
 */
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  BookOpen,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useParams } from "next/navigation";
import { memo, useCallback, useMemo, useState } from "react";

import {
  ChapterList,
  type ChapterStatus,
} from "../../../components/ChapterList";
import { DownloadItem } from "../../../components/DownloadItem";
import {
  api,
  type MangaChaptersResponse,
  type MangaResponse,
  type NexusApiError,
} from "../../../lib/api";
import { cn } from "../../../lib/utils";
import { useDownload } from "../../../hooks/useDownload";
import { useDownloadsStore } from "../../../store/downloads";
import { useSettingsStore } from "../../../store/settings";
import type { DownloadTask } from "../../../types/download";
import type { Manga } from "../../../types/manga";

// ───────────────────────────────────────────────────────────────────────────
// Chapter status derivation
// ───────────────────────────────────────────────────────────────────────────

/**
 * Derive a per-chapter download status map from the tasks that belong to
 * *tasks*.
 *
 * Only tasks with an explicit ``chapter_ids`` list can contribute a
 * "downloaded" mark — a task created with ``chapter_ids: null`` ("all
 * chapters") is not precise enough to know which chapters were actually
 * present when the task ran, and marking them all downloaded would be
 * misleading if new chapters were added afterwards.  Failed chapters from
 * those tasks **are** marked, because a failure is always precise.
 *
 * Priority, from lowest to highest:
 *
 * 1. ``"failed"`` — a `failed` task attempted the chapter and it did not
 *    succeed.
 * 2. ``"downloaded"`` — a `done` task completed the chapter.  Overwrites
 *    a prior failure (the user retried and it worked).
 * 3. ``"downloading"`` — an active task is working on the chapter right
 *    now.  Overwrites everything else (the user is re-downloading it).
 */
function deriveChapterStatuses(
  tasks: readonly DownloadTask[],
  visibleChapterIds: ReadonlySet<string>,
): Map<string, ChapterStatus> {
  const statuses = new Map<string, ChapterStatus>();

  // 1. Done tasks — mark success and failure per explicit chapter_ids.
  for (const task of tasks) {
    if (task.status !== "done" || task.result === null) {
      continue;
    }
    const failedIds = new Set(Object.keys(task.result.chapter_errors));

    if (task.chapter_ids !== null) {
      for (const cid of task.chapter_ids) {
        if (!visibleChapterIds.has(cid)) continue;
        statuses.set(cid, failedIds.has(cid) ? "failed" : "downloaded");
      }
    } else {
      // No explicit list — only the failed chapters are identifiable.
      for (const cid of failedIds) {
        if (visibleChapterIds.has(cid)) {
          statuses.set(cid, "failed");
        }
      }
    }
  }

  // 2. Failed tasks — mark attempted chapters as failed, unless a later
  //    done task already succeeded on the same chapter.
  for (const task of tasks) {
    if (task.status !== "failed" || task.chapter_ids === null) {
      continue;
    }
    for (const cid of task.chapter_ids) {
      if (!visibleChapterIds.has(cid)) continue;
      if (statuses.get(cid) !== "downloaded") {
        statuses.set(cid, "failed");
      }
    }
  }

  // 3. Active tasks — the current chapter is "downloading".
  for (const task of tasks) {
    if (task.status !== "running" && task.status !== "paused") {
      continue;
    }
    const cid = task.progress.current_chapter_id;
    if (cid !== null && visibleChapterIds.has(cid)) {
      statuses.set(cid, "downloading");
    }
  }

  return statuses;
}

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return ``true`` when *error* is a "not found" response from the backend.
 *
 * Handled separately from other errors because "this manga does not exist"
 * is a terminal state — the retry button would never succeed — while any
 * other error is worth retrying.
 */
function isNotFoundError(error: NexusApiError | null): boolean {
  if (error === null) {
    return false;
  }
  return error.code === "NOT_FOUND" || error.status === 404;
}

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/manga/[id]`` route.  See the module docstring for the full
 * contract.
 */
export default function MangaDetailPage() {
  const params = useParams<{ id: string }>();
  const mangaId = params.id;

  // -------------------------------------------------------------------
  // Queries
  // -------------------------------------------------------------------
  const mangaQuery = useQuery<MangaResponse, NexusApiError>({
    queryKey: ["manga", mangaId],
    queryFn: () => api.getManga(mangaId),
  });

  const chaptersQuery = useQuery<MangaChaptersResponse, NexusApiError>({
    queryKey: ["manga-chapters", mangaId],
    queryFn: () => api.getMangaChapters(mangaId),
  });

  // -------------------------------------------------------------------
  // User preferences (defaults for the download request)
  // -------------------------------------------------------------------
  const defaultFormat = useSettingsStore((s) => s.default_packaging_format);
  const defaultPriority = useSettingsStore((s) => s.default_priority);
  const defaultSkipExisting = useSettingsStore((s) => s.default_skip_existing);

  // -------------------------------------------------------------------
  // Download action
  // -------------------------------------------------------------------
  const {
    create: createDownload,
    isCreating,
    createError,
    clearCreateError,
  } = useDownload();

  const [selectedChapterIds, setSelectedChapterIds] = useState<string[]>([]);

  const handleDownload = useCallback(
    async (chapterIds: string[]): Promise<void> => {
      try {
        await createDownload({
          manga_id: mangaId,
          chapter_ids: chapterIds,
          packaging_format: defaultFormat,
          priority: defaultPriority,
          skip_existing: defaultSkipExisting,
        });
        setSelectedChapterIds([]);
      } catch {
        // The error is already exposed through ``createError`` — the page
        // renders it inline.  Swallowing the rejection here prevents an
        // unhandled promise rejection in the console.
      }
    },
    [
      createDownload,
      mangaId,
      defaultFormat,
      defaultPriority,
      defaultSkipExisting,
    ],
  );

  // -------------------------------------------------------------------
  // Chapter statuses — signature-based memoisation
  // -------------------------------------------------------------------
  //
  // The signature is a list of strings, one per task that belongs to this
  // manga.  Each string captures exactly the fields that would affect a
  // chapter's status.  Progress byte updates do not change it, so the map
  // below is not recomputed on every download tick — a critical
  // optimisation for chapter lists that can reach 1000+ entries.
  const statusSignature = useDownloadsStore((s) => {
    const parts: string[] = [];
    for (const task of Object.values(s.tasks)) {
      if (task.manga_id !== mangaId) continue;
      parts.push(
        [
          task.id,
          task.status,
          task.progress.current_chapter_id ?? "",
          (task.chapter_ids ?? []).join(","),
          task.result
            ? Object.keys(task.result.chapter_errors).sort().join(",")
            : "",
        ].join("|"),
      );
    }
    return parts.join("\n");
  });

  const visibleChapterIds = useMemo(
    () => new Set<string>(chaptersQuery.data?.items.map((c) => c.id) ?? []),
    [chaptersQuery.data],
  );

  const chapterStatuses = useMemo<Map<string, ChapterStatus>>(() => {
    // Read the full task state inside the memo.  We only get here when the
    // signature has changed, so the derived statuses are guaranteed to
    // reflect the tasks that produced the new signature.
    const allTasks = Object.values(useDownloadsStore.getState().tasks);
    const relevant = allTasks.filter((t) => t.manga_id === mangaId);
    return deriveChapterStatuses(relevant, visibleChapterIds);
  }, [statusSignature, mangaId, visibleChapterIds]);

  // -------------------------------------------------------------------
  // Render states
  // -------------------------------------------------------------------
  if (mangaQuery.isLoading) {
    return <PageSkeleton />;
  }

  if (mangaQuery.error !== null) {
    return (
      <FullErrorState
        error={mangaQuery.error}
        isNotFound={isNotFoundError(mangaQuery.error)}
        onRetry={() => void mangaQuery.refetch()}
      />
    );
  }

  const manga = mangaQuery.data?.manga;
  if (manga === undefined) {
    return (
      <FullErrorState
        error={null}
        isNotFound
        onRetry={() => void mangaQuery.refetch()}
      />
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      {/* Breadcrumb --------------------------------------------- */}
      <Link
        href="/search"
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-md",
          "text-sm text-muted-foreground hover:text-foreground",
          "focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-ring focus-visible:ring-offset-2",
          "focus-visible:ring-offset-background",
        )}
      >
        <ArrowLeft className="h-4 w-4" aria-hidden />
        <span>Back to search</span>
      </Link>

      <MangaHero manga={manga} />

      {/* Active downloads ------------------------------------- */}
      <MangaActiveDownloads mangaId={mangaId} />

      {/* Chapters --------------------------------------------- */}
      <section
        aria-labelledby="chapters-heading"
        className="flex flex-col gap-3"
      >
        <div className="flex items-center justify-between gap-2">
          <h2
            id="chapters-heading"
            className="text-lg font-semibold tracking-tight"
          >
            Chapters
            {chaptersQuery.data !== undefined && (
              <span className="ml-2 text-sm font-normal text-muted-foreground">
                ({chaptersQuery.data.items.length})
              </span>
            )}
          </h2>
          {chaptersQuery.isFetching && !chaptersQuery.isLoading && (
            <span
              className="inline-flex items-center gap-1 text-xs text-muted-foreground"
              aria-live="polite"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              Refreshing…
            </span>
          )}
        </div>

        {chaptersQuery.isLoading && <ChaptersSkeleton />}

        {chaptersQuery.error !== null && chaptersQuery.isError && (
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
              <p className="font-medium">Could not load chapters</p>
              <p className="mt-0.5 text-xs opacity-80">
                {chaptersQuery.error.message}
              </p>
            </div>
            <button
              type="button"
              onClick={() => void chaptersQuery.refetch()}
              className={cn(
                "inline-flex items-center gap-1 rounded px-2 py-1",
                "text-xs font-medium",
                "text-destructive hover:bg-destructive/20",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-destructive/50",
              )}
            >
              <RefreshCw className="h-3 w-3" aria-hidden />
              Retry
            </button>
          </div>
        )}

        {chaptersQuery.data !== undefined && (
          <>
            <MemoizedChapterList
              chapters={chaptersQuery.data.items}
              selectedIds={selectedChapterIds}
              onSelectionChange={setSelectedChapterIds}
              chapterStatuses={chapterStatuses}
              onDownloadSelected={handleDownload}
              isDownloading={isCreating}
            />

            {/* Create error ------------------------------- */}
            {createError !== null && (
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
                  <p className="font-medium">Could not start download</p>
                  <p className="mt-0.5 text-xs opacity-80">
                    {createError.message}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={clearCreateError}
                  aria-label="Dismiss error"
                  title="Dismiss error"
                  className={cn(
                    "rounded p-1",
                    "text-destructive hover:bg-destructive/20",
                    "focus-visible:outline-none focus-visible:ring-2",
                    "focus-visible:ring-destructive/50",
                  )}
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </div>
            )}

            {/* Defaults hint ------------------------------- */}
            <p className="text-xs text-muted-foreground">
              New downloads will be packaged as{" "}
              <span className="font-mono font-medium text-foreground">
                {defaultFormat.toUpperCase()}
              </span>
              , queued at{" "}
              <span className="font-mono font-medium text-foreground">
                {defaultPriority}
              </span>{" "}
              priority
              {defaultSkipExisting && ", skipping chapters already on disk"}.
              Change these in{" "}
              <Link
                href="/settings"
                className="underline underline-offset-2 hover:text-foreground"
              >
                Settings
              </Link>
              .
            </p>
          </>
        )}
      </section>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * ``ChapterList`` wrapped in :func:`React.memo`.
 *
 * The list is potentially very long (1000+ rows for long-running series).
 * Every prop we pass is a stable reference across re-renders of the parent
 * — ``chapters`` comes from a query, ``selectedIds`` from local state,
 * ``chapterStatuses`` from a memoised map, and both callbacks from
 * ``useCallback`` / ``useState``.  ``memo`` therefore skips re-rendering
 * the list entirely when the parent re-renders for an unrelated reason
 * (e.g. a download progress update that only affects the section above).
 */
const MemoizedChapterList = memo(ChapterList);

/**
 * The hero block — cover, metadata, description and tags.
 *
 * Rendered as a single :func:`Manga` prop with no internal state, so it is
 * re-rendered only when the manga resource itself changes (never after the
 * first successful fetch).
 */
function MangaHero({ manga }: { manga: Manga }) {
  const metaParts: string[] = [];
  if (manga.year !== null) metaParts.push(String(manga.year));
  if (manga.language) metaParts.push(manga.language.toUpperCase());
  metaParts.push(manga.site_name);

  return (
    <section className="flex flex-col gap-6 md:flex-row md:items-start">
      {/* Cover -------------------------------------------------- */}
      <div className="mx-auto w-full max-w-[180px] shrink-0 md:mx-0 md:w-[200px]">
        <div className="relative aspect-[2/3] w-full overflow-hidden rounded-lg border bg-muted shadow-lg">
          {manga.cover_url !== null ? (
            <Image
              src={manga.cover_url}
              alt=""
              fill
              sizes="(min-width: 768px) 200px, 180px"
              priority
              className="object-cover"
            />
          ) : (
            <div
              className="flex h-full w-full items-center justify-center text-muted-foreground"
              aria-hidden
            >
              <BookOpen className="h-12 w-12" />
            </div>
          )}
        </div>
      </div>

      {/* Info --------------------------------------------------- */}
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <div>
          <h1 className="neon-text text-2xl font-bold leading-tight tracking-tight sm:text-3xl">
            {manga.title}
          </h1>
          {manga.alt_titles.length > 0 && (
            <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
              {manga.alt_titles.join(" · ")}
            </p>
          )}
        </div>

        {/* Badges and meta ------------------------------------- */}
        <div className="flex flex-wrap items-center gap-1.5">
          <StatusChip status={manga.status} />
          {manga.adult && <AdultChip />}
          {metaParts.map((part) => (
            <span
              key={part}
              className={cn(
                "inline-flex items-center rounded-full px-2 py-0.5",
                "bg-muted text-xs font-medium text-muted-foreground",
              )}
            >
              {part}
            </span>
          ))}
        </div>

        {/* Authors and artists ---------------------------------- */}
        {(manga.authors.length > 0 || manga.artists.length > 0) && (
          <dl className="space-y-0.5 text-sm">
            {manga.authors.length > 0 && (
              <div className="flex gap-1.5">
                <dt className="shrink-0 text-muted-foreground">Author:</dt>
                <dd className="truncate">{manga.authors.join(", ")}</dd>
              </div>
            )}
            {manga.artists.length > 0 &&
              manga.artists.join(",") !== manga.authors.join(",") && (
                <div className="flex gap-1.5">
                  <dt className="shrink-0 text-muted-foreground">
                    Artist:
                  </dt>
                  <dd className="truncate">{manga.artists.join(", ")}</dd>
                </div>
              )}
          </dl>
        )}

        {/* Description ----------------------------------------- */}
        {manga.description !== null && (
          <p className="whitespace-pre-line text-sm leading-relaxed text-muted-foreground">
            {manga.description}
          </p>
        )}

        {/* Tags ------------------------------------------------ */}
        {manga.tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {manga.tags.map((tag) => (
              <span
                key={tag}
                className={cn(
                  "inline-flex items-center rounded-full border px-2 py-0.5",
                  "text-xs text-muted-foreground",
                )}
              >
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * The active downloads section for this manga.
 *
 * Extracted from the page body so that it has its own store subscription.
 * The page above does not subscribe to full task objects, so this
 * component is the only one that re-renders on every progress update.  It
 * uses :func:`useDownload` for the action methods — those are stable
 * references.
 */
function MangaActiveDownloads({ mangaId }: { mangaId: string }) {
  const { pause, resume, cancel, remove } = useDownload();

  const mangaTasks = useDownloadsStore(
    // A plain selector — this section intentionally re-renders on every
    // task update, because it displays live progress.
    (s) => {
      const result: DownloadTask[] = [];
      for (const task of Object.values(s.tasks)) {
        if (task.manga_id === mangaId) {
          result.push(task);
        }
      }
      result.sort((a, b) => {
        if (a.created_at === b.created_at) {
          return a.id.localeCompare(b.id);
        }
        return a.created_at < b.created_at ? 1 : -1;
      });
      return result;
    },
    // Default equality is ``Object.is`` — the array is always new, so
    // this would always re-render.  ``useShallow`` is intentionally not
    // used here: the section genuinely needs to re-render on every task
    // change (that is what makes progress animate).
  );

  if (mangaTasks.length === 0) {
    return null;
  }

  return (
    <section
      aria-labelledby="downloads-heading"
      className="flex flex-col gap-2"
    >
      <h2
        id="downloads-heading"
        className="text-lg font-semibold tracking-tight"
      >
        Downloads for this series
      </h2>
      {mangaTasks.map((task) => (
        <DownloadItem
          key={task.id}
          task={task}
          onPause={(id) => {
            void pause(id);
          }}
          onResume={(id) => {
            void resume(id);
          }}
          onCancel={(id) => {
            void cancel(id);
          }}
          onRemove={(id) => {
            void remove(id);
          }}
        />
      ))}
    </section>
  );
}

/**
 * Small status chip shown in the hero.
 *
 * Neutral labels — "Ongoing", "Completed", etc. — styled to match the
 * palette used elsewhere (downloads page stat cards, chapter list).  An
 * unknown status renders as a muted chip with no icon.
 */
function StatusChip({ status }: { status: Manga["status"] }) {
  const label = {
    ongoing: "Ongoing",
    completed: "Completed",
    hiatus: "Hiatus",
    cancelled: "Cancelled",
    unknown: "Unknown",
  }[status];

  const classes = {
    ongoing:
      "bg-blue-500/10 text-blue-700 ring-blue-600/20 dark:text-blue-400",
    completed:
      "bg-emerald-500/10 text-emerald-700 ring-emerald-600/20 dark:text-emerald-400",
    hiatus:
      "bg-amber-500/10 text-amber-700 ring-amber-600/20 dark:text-amber-400",
    cancelled:
      "bg-red-500/10 text-red-700 ring-red-600/20 dark:text-red-400",
    unknown: "bg-muted text-muted-foreground ring-border",
  }[status];

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5",
        "text-xs font-medium ring-1 ring-inset",
        classes,
      )}
    >
      {label}
    </span>
  );
}

/** Adult-content chip.  Mirrors the badge on :class:`MangaCard`. */
function AdultChip() {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5",
        "bg-red-600 text-xs font-bold uppercase text-white",
      )}
      title="Adult content"
    >
      18+
    </span>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Skeleton and error states
// ───────────────────────────────────────────────────────────────────────────

/**
 * Full-page skeleton shown while the manga resource is loading.
 *
 * The footprint matches the hero + chapter list layout so the real content
 * replaces it without a layout shift.
 */
function PageSkeleton() {
  return (
    <div
      className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Loading manga…</span>
      <div className="h-5 w-32 animate-pulse rounded bg-muted" />
      <div className="flex flex-col gap-6 md:flex-row">
        <div className="mx-auto aspect-[2/3] w-full max-w-[180px] animate-pulse rounded-lg bg-muted md:mx-0 md:w-[200px]" />
        <div className="flex flex-1 flex-col gap-3">
          <div className="h-9 w-2/3 animate-pulse rounded bg-muted" />
          <div className="h-5 w-40 animate-pulse rounded bg-muted" />
          <div className="h-4 w-full animate-pulse rounded bg-muted" />
          <div className="h-4 w-5/6 animate-pulse rounded bg-muted" />
          <div className="h-4 w-4/6 animate-pulse rounded bg-muted" />
        </div>
      </div>
      <ChaptersSkeleton />
    </div>
  );
}

/** Skeleton for the chapter list section. */
function ChaptersSkeleton() {
  return (
    <div
      className="flex flex-col gap-2 rounded-lg border bg-card p-3"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Loading chapters…</span>
      <div className="h-9 animate-pulse rounded bg-muted" />
      {[0, 1, 2, 3, 4].map((index) => (
        <div
          key={index}
          className="flex animate-pulse items-start gap-3 rounded-md px-2 py-2"
        >
          <div className="h-4 w-4 rounded bg-muted" />
          <div className="flex-1 space-y-1.5">
            <div className="h-4 w-1/3 rounded bg-muted" />
            <div className="h-3 w-1/2 rounded bg-muted" />
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Full-page error state.
 *
 * ``isNotFound`` switches the message and hides the retry button — a
 * "this manga does not exist" error cannot be fixed by retrying, and
 * showing a disabled retry button would suggest otherwise.
 */
function FullErrorState({
  error,
  isNotFound,
  onRetry,
}: {
  error: NexusApiError | null;
  isNotFound: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col items-center gap-4 p-8 text-center">
      <span
        aria-hidden
        className={cn(
          "grid h-14 w-14 place-items-center rounded-full",
          "bg-destructive/10 text-destructive",
        )}
      >
        <AlertCircle className="h-7 w-7" />
      </span>
      <div>
        <h1 className="text-lg font-semibold">
          {isNotFound ? "Manga not found" : "Could not load manga"}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {isNotFound
            ? "The manga you are looking for does not exist, or has been removed."
            : (error?.message ?? "An unexpected error occurred.")}
        </p>
      </div>
      <div className="flex items-center gap-2">
        <Link
          href="/search"
          className={cn(
            "inline-flex h-9 items-center gap-1.5 rounded-md",
            "border bg-background px-3 text-sm font-medium",
            "hover:bg-accent",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
        >
          Back to search
        </Link>
        {!isNotFound && (
          <button
            type="button"
            onClick={onRetry}
            className={cn(
              "inline-flex h-9 items-center gap-1.5 rounded-md px-3",
              "text-sm font-medium",
              "bg-primary text-primary-foreground hover:bg-primary/90",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
            )}
          >
            <RefreshCw className="h-4 w-4" aria-hidden />
            Retry
          </button>
        )}
      </div>
    </div>
  );
}
