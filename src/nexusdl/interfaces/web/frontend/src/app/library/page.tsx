"use client";

/**
 * Library page — the ``/library`` route.
 *
 * Layout
 * ------
 * A single-column dashboard mirroring the visual grammar of the
 * ``/downloads`` page:
 *
 * 1. **Header** — title, live count, view-mode toggle (grid / list) and a
 *    manual refresh button.
 * 2. **Filters** — a text input (title / author / tag), a single-site
 *    ``<select>`` populated from ``GET /api/v1/sites``, and a clear-search
 *    affordance.
 * 3. **Content** — grid view (reuses :class:`~../components/MangaCard.MangaCard`
 *    with a thin download-completion bar overlaid along the bottom edge) or
 *    list view (compact cover thumbnail, title, site, download progress,
 *    reading position).
 * 4. **Pagination** — prev / numbered pages / next, driven by the same
 *    ``items / total / page / per_page / total_pages / has_next / has_prev``
 *    envelope as every other paginated endpoint.
 * 5. **Empty / loading / error states** — distinct messages for "loading",
 *    "library is empty" and "no match for these filters".
 *
 * State ownership
 * ---------------
 * * **Server data** — the library list and the sites catalogue are fetched
 *   via :mod:`@tanstack/react-query`.  The library query key is
 *   ``["library", { page, query, siteId, perPage }]`` so that navigation
 *   back and forward re-uses cached pages; the sites query is cached for
 *   five minutes because the catalogue changes only on backend restart.
 *
 * * **URL state** — ``?page``, ``?q`` and ``?site`` are the single source of
 *   truth for the current filter and page.  Every interaction updates the
 *   URL with ``router.replace`` (no history entry per keystroke or page
 *   click), so the browser's back button navigates between *pages the user
 *   actually visited as distinct pages* rather than replaying every filter
 *   change.
 *
 * * **View mode** — read from and written to the settings store
 *   (``library_view``, ``toggleLibraryView``).  This is a user preference,
 *   not server state, so it is persisted to ``localStorage`` and does not
 *   touch the URL.
 *
 * * **Search input** — local React state, initialised from ``?q`` and
 *   synced back on submit.  This is the standard "controlled input with
 *   URL-synced submit" pattern, which lets the user type freely without a
 *   request per keystroke.
 *
 * Refetch strategy
 * ----------------
 * The provider wires the ``library.updated`` WebSocket event to React
 * Query cache invalidation of the ``["library"]`` prefix.  This page
 * therefore does **not** subscribe to the WebSocket itself — it relies on
 * the provider, and adds two passive freshness mechanisms:
 *
 * * **Visibility refetch** — when the browser tab regains focus after
 *   being hidden, the query is refetched.  This reconciles the UI after
 *   the OS suspended the WebSocket for a background tab.
 * * **Manual refresh** — the header button triggers a direct refetch,
 *   which is the user-facing escape hatch for any case the automatic
 *   mechanisms missed.
 *
 * Hydration
 * ---------
 * ``useSearchParams()`` requires a Suspense boundary in Next.js 14, so the
 * default export wraps the body in one.  ``useSettingsStore`` uses
 * ``skipHydration`` and defaults to ``library_view: "grid"``, which is
 * what the server also renders — no hydration mismatch.
 *
 * URL sanitisation
 * ----------------
 * ``?page`` is parsed with :func:`parsePositiveInt`, which falls back to
 * ``1`` for any non-positive or non-numeric value.  An invalid page in the
 * URL should not leave the page in a broken state — the fallback makes
 * the page usable and lets the user navigate from there.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Grid3x3,
  List,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Suspense,
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";

import { MangaCard } from "../../components/MangaCard";
import {
  api,
  type LibraryEntry,
  type LibraryListResponse,
  type NexusApiError,
} from "../../lib/api";
import { clampNumber, cn, formatNumber } from "../../lib/utils";
import { useSettingsStore } from "../../store/settings";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Items per page.  A multiple of the grid column counts. */
const PER_PAGE = 24;

/** Sites catalogue freshness window. */
const SITES_STALE_TIME_MS = 5 * 60 * 1000;

/** Library list freshness window — the provider invalidates on WS events. */
const LIBRARY_STALE_TIME_MS = 30 * 1000;

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Parse *raw* as a strictly positive integer, or return *fallback*.
 *
 * Used to sanitise ``?page``.  ``Number.parseInt("12abc")`` yields ``12``,
 * which is what we want (forgiving input), but ``Number.parseInt("")``
 * yields ``NaN``, which is not — hence the explicit range check.
 */
function parsePositiveInt(raw: string | null, fallback: number): number {
  if (raw === null) {
    return fallback;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1) {
    return fallback;
  }
  return parsed;
}

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/library`` route.  See the module docstring for the full
 * contract.
 */
export default function LibraryPage() {
  return (
    <Suspense fallback={<LibraryFallback />}>
      <LibraryBody />
    </Suspense>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Body
// ───────────────────────────────────────────────────────────────────────────

function LibraryBody() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const libraryView = useSettingsStore((s) => s.library_view);
  const toggleLibraryView = useSettingsStore((s) => s.toggleLibraryView);

  // -------------------------------------------------------------------
  // URL-derived state
  // -------------------------------------------------------------------
  const page = parsePositiveInt(searchParams.get("page"), 1);
  const queryParam = (searchParams.get("q") ?? "").trim();
  const siteIdParam = searchParams.get("site");
  const hasActiveFilters = queryParam.length > 0 || siteIdParam !== null;

  // Search input is local, seeded from the URL and re-synced when the URL
  // changes (so browser back/forward navigations update the field).
  const [searchInput, setSearchInput] = useState(queryParam);
  useEffect(() => {
    setSearchInput(queryParam);
  }, [queryParam]);

  // -------------------------------------------------------------------
  // URL mutation helpers
  // -------------------------------------------------------------------
  const setUrlParams = useCallback(
    (updates: Record<string, string | null>) => {
      const params = new URLSearchParams(searchParams.toString());
      for (const [key, value] of Object.entries(updates)) {
        if (value === null || value === "") {
          params.delete(key);
        } else {
          params.set(key, value);
        }
      }
      const qs = params.toString();
      router.replace(qs.length > 0 ? `${pathname}?${qs}` : pathname, {
        scroll: false,
      });
    },
    [router, pathname, searchParams],
  );

  const handleSearchSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setUrlParams({ q: searchInput.trim() || null, page: null });
  };

  const handleClearSearch = (): void => {
    setSearchInput("");
    setUrlParams({ q: null, page: null });
  };

  const handleSiteChange = (value: string): void => {
    setUrlParams({ site: value || null, page: null });
  };

  const goToPage = useCallback(
    (next: number) => {
      setUrlParams({ page: next === 1 ? null : String(next) });
    },
    [setUrlParams],
  );

  const handleClearFilters = (): void => {
    setSearchInput("");
    setUrlParams({ q: null, site: null, page: null });
  };

  // -------------------------------------------------------------------
  // Data
  // -------------------------------------------------------------------
  const sitesQuery = useQuery({
    queryKey: ["sites"],
    queryFn: () => api.getSites(),
    staleTime: SITES_STALE_TIME_MS,
    refetchOnWindowFocus: false,
  });

  const libraryQuery = useQuery<LibraryListResponse, NexusApiError>({
    queryKey: [
      "library",
      { page, query: queryParam, siteId: siteIdParam, perPage: PER_PAGE },
    ],
    queryFn: () =>
      api.getLibrary({
        page,
        per_page: PER_PAGE,
        query: queryParam || undefined,
        site_id: siteIdParam ?? undefined,
      }),
    placeholderData: keepPreviousData,
    staleTime: LIBRARY_STALE_TIME_MS,
  });

  // Reconcile after the OS suspends the socket in a background tab.
  useEffect(() => {
    if (typeof document === "undefined") {
      return;
    }
    const handleVisibility = (): void => {
      if (document.visibilityState === "visible") {
        void libraryQuery.refetch();
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [libraryQuery.refetch]);

  // -------------------------------------------------------------------
  // Derived display state
  // -------------------------------------------------------------------
  const entries = libraryQuery.data?.items ?? [];
  const total = libraryQuery.data?.total ?? 0;
  const totalPages = libraryQuery.data?.total_pages ?? 0;
  const hasNext = libraryQuery.data?.has_next ?? false;
  const hasPrev = libraryQuery.data?.has_prev ?? false;

  const isInitialLoading =
    libraryQuery.isLoading && libraryQuery.data === undefined;
  const isRefreshing = libraryQuery.isFetching && !isInitialLoading;
  const isEmpty = !isInitialLoading && entries.length === 0;

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      {/* Header -------------------------------------------------- */}
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="neon-text text-2xl font-bold tracking-tight sm:text-3xl">
            Library
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {total > 0
              ? `${formatNumber(total)} ${
                  total === 1 ? "series" : "series"
                } in your library.`
              : "Your personal manga collection."}
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* View mode toggle --------------------------------- */}
          <div
            className="flex items-center rounded-md border bg-background p-0.5"
            role="group"
            aria-label="Library view mode"
          >
            <button
              type="button"
              onClick={() => {
                if (libraryView !== "grid") toggleLibraryView();
              }}
              aria-pressed={libraryView === "grid"}
              aria-label="Grid view"
              title="Grid view"
              className={cn(
                "inline-flex h-8 w-8 items-center justify-center rounded",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring",
                libraryView === "grid"
                  ? "bg-primary text-primary-foreground shadow-[0_0_8px_hsl(var(--primary)/0.5)]"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <Grid3x3 className="h-4 w-4" aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => {
                if (libraryView !== "list") toggleLibraryView();
              }}
              aria-pressed={libraryView === "list"}
              aria-label="List view"
              title="List view"
              className={cn(
                "inline-flex h-8 w-8 items-center justify-center rounded",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring",
                libraryView === "list"
                  ? "bg-primary text-primary-foreground shadow-[0_0_8px_hsl(var(--primary)/0.5)]"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <List className="h-4 w-4" aria-hidden />
            </button>
          </div>

          {/* Refresh --------------------------------------- */}
          <button
            type="button"
            onClick={() => void libraryQuery.refetch()}
            disabled={libraryQuery.isFetching}
            aria-label="Refresh library"
            title="Refresh library"
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
            {isRefreshing ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden />
            )}
            <span className="hidden sm:inline">Refresh</span>
          </button>
        </div>
      </header>

      {/* Filters ------------------------------------------------- */}
      <section
        aria-label="Library filters"
        className="flex flex-col gap-2 sm:flex-row sm:items-center"
      >
        <form onSubmit={handleSearchSubmit} className="relative flex-1">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <input
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Filter by title, author or tag…"
            aria-label="Filter library"
            className={cn(
              "h-9 w-full rounded-md border bg-background pl-8 pr-8",
              "text-sm placeholder:text-muted-foreground",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "[&::-webkit-search-cancel-button]:appearance-none",
            )}
          />
          {searchInput.length > 0 && (
            <button
              type="button"
              onClick={handleClearSearch}
              aria-label="Clear search filter"
              title="Clear search filter"
              className={cn(
                "absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5",
                "text-muted-foreground hover:bg-accent hover:text-foreground",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring",
              )}
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          )}
        </form>

        <select
          value={siteIdParam ?? ""}
          onChange={(event) => handleSiteChange(event.target.value)}
          aria-label="Filter by site"
          className={cn(
            "h-9 rounded-md border bg-background px-3 text-sm",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          <option value="">All sites</option>
          {sitesQuery.data?.items.map((site) => (
            <option key={site.id} value={site.id}>
              {site.name}
            </option>
          ))}
        </select>
      </section>

      {/* Error --------------------------------------------------- */}
      {libraryQuery.error !== null && (
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
            <p className="font-medium">Could not load the library</p>
            <p className="mt-0.5 text-xs opacity-80">
              {libraryQuery.error.message}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void libraryQuery.refetch()}
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

      {/* Content ------------------------------------------------- */}
      {isInitialLoading && <LibrarySkeleton view={libraryView} />}

      {!isInitialLoading && isEmpty && (
        <EmptyState
          title={
            hasActiveFilters
              ? "No matches in your library"
              : "Your library is empty"
          }
          description={
            hasActiveFilters
              ? "Try adjusting your filters, or clear them to see the full collection."
              : "Search for a manga and add it to your library to see it here."
          }
          action={
            hasActiveFilters ? (
              <button
                type="button"
                onClick={handleClearFilters}
                className={cn(
                  "rounded px-3 py-1.5 text-xs font-medium",
                  "border bg-background hover:bg-accent",
                  "focus-visible:outline-none focus-visible:ring-2",
                  "focus-visible:ring-ring",
                )}
              >
                Clear filters
              </button>
            ) : null
          }
        />
      )}

      {!isInitialLoading && !isEmpty && (
        <>
          {libraryView === "grid" ? (
            <LibraryGrid entries={entries} />
          ) : (
            <LibraryList entries={entries} />
          )}

          {totalPages > 1 && (
            <Pagination
              page={page}
              totalPages={totalPages}
              hasPrev={hasPrev}
              hasNext={hasNext}
              onPageChange={goToPage}
            />
          )}
        </>
      )}
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Views
// ───────────────────────────────────────────────────────────────────────────

/** Responsive grid of library cards. */
function LibraryGrid({ entries }: { entries: readonly LibraryEntry[] }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
      {entries.map((entry) => (
        <LibraryGridCard key={entry.manga.id} entry={entry} />
      ))}
    </div>
  );
}

/**
 * A single grid card.
 *
 * Reuses :class:`~../components/MangaCard.MangaCard` (the whole card stays
 * one clickable link) and overlays a thin download-completion bar along
 * the bottom edge.  The overlay has ``pointer-events-none`` so it never
 * blocks the card's navigation, and it is positioned on the wrapper (not
 * inside ``MangaCard``) so it is not clipped by the card's own
 * ``overflow-hidden``.
 */
function LibraryGridCard({ entry }: { entry: LibraryEntry }) {
  const completion =
    entry.total_chapters > 0
      ? clampNumber(
          (entry.downloaded_chapters / entry.total_chapters) * 100,
          0,
          100,
        )
      : 0;

  return (
    <div className="relative">
      <MangaCard data={entry.manga} />
      {completion > 0 && (
        <div
          aria-hidden
          className={cn(
            "pointer-events-none absolute inset-x-0 bottom-0 h-1",
            "overflow-hidden rounded-b-lg bg-muted/60",
          )}
        >
          <div
            className="h-full bg-primary shadow-[0_0_8px_hsl(var(--primary)/0.8)]"
            style={{ width: `${completion}%` }}
          />
        </div>
      )}
    </div>
  );
}

/** Vertical list of library rows. */
function LibraryList({ entries }: { entries: readonly LibraryEntry[] }) {
  return (
    <ul className="flex flex-col gap-2">
      {entries.map((entry) => (
        <li key={entry.manga.id}>
          <LibraryListRow entry={entry} />
        </li>
      ))}
    </ul>
  );
}

/**
 * A single list row.
 *
 * Compact cover thumbnail on the left, all textual metadata on the right.
 * The whole row is a ``<Link>`` to the manga detail page, matching the
 * grid card's click target.  Reading position and download progress are
 * surfaced in the meta line so the row answers both "how much do I have"
 * and "where am I in it".
 */
function LibraryListRow({ entry }: { entry: LibraryEntry }) {
  const completion =
    entry.total_chapters > 0
      ? clampNumber(
          (entry.downloaded_chapters / entry.total_chapters) * 100,
          0,
          100,
        )
      : 0;

  const parts: string[] = [entry.manga.site_name];

  if (entry.total_chapters > 0) {
    parts.push(
      `${entry.downloaded_chapters}/${entry.total_chapters} downloaded`,
    );
  }

  if (entry.reading_progress !== null) {
    parts.push(`Reading · page ${entry.reading_progress.page + 1}`);
  }

  if (entry.file_size_bytes > 0) {
    // Rendered inline rather than through ``formatBytes`` to keep the
    // meta line compact — library entries are usually small and the
    // compact "12 MB" form reads better here than "12.4 MB".
    parts.push(formatBytesShort(entry.file_size_bytes));
  }

  const metaLine = parts.join(" · ");

  return (
    <Link
      href={`/manga/${entry.manga.id}`}
      className={cn(
        "flex gap-3 rounded-lg border bg-card p-3",
        "text-card-foreground shadow-sm transition-colors",
        "hover:border-primary/40 hover:bg-accent/30",
        "focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background",
      )}
    >
      <div className="relative h-24 w-16 shrink-0 overflow-hidden rounded-md bg-muted">
        {entry.manga.cover_url !== null ? (
          <Image
            src={entry.manga.cover_url}
            alt=""
            fill
            sizes="64px"
            className="object-cover"
          />
        ) : (
          <div
            className="flex h-full w-full items-center justify-center text-muted-foreground"
            aria-hidden
          >
            <BookOpen className="h-6 w-6" />
          </div>
        )}
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h3
          className="truncate text-sm font-medium"
          title={entry.manga.title}
        >
          {entry.manga.title}
        </h3>
        <p
          className="truncate text-xs text-muted-foreground"
          title={metaLine}
        >
          {metaLine}
        </p>

        {completion > 0 && (
          <div className="mt-auto h-1 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full bg-primary shadow-[0_0_6px_hsl(var(--primary)/0.7)]"
              style={{ width: `${completion}%` }}
            />
          </div>
        )}
      </div>
    </Link>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Pagination
// ───────────────────────────────────────────────────────────────────────────

/**
 * Compact pagination control.
 *
 * Shows a sliding window of at most five page numbers around the current
 * page, with first/last jump links ("1 …", "… N") and prev/next arrows.
 * A page number is a ``<button>``, not an ``<a>`` — the navigation is
 * client-side and updates the URL with ``router.replace`` (no history
 * entry), so an anchor element would mislead the user's browser (middle-
 * click, ``href`` preview in the status bar).
 */
function Pagination({
  page,
  totalPages,
  hasPrev,
  hasNext,
  onPageChange,
}: {
  page: number;
  totalPages: number;
  hasPrev: boolean;
  hasNext: boolean;
  onPageChange: (next: number) => void;
}) {
  const windowStart = Math.max(1, page - 2);
  const windowEnd = Math.min(totalPages, page + 2);
  const pages: number[] = [];
  for (let i = windowStart; i <= windowEnd; i += 1) {
    pages.push(i);
  }

  return (
    <nav
      aria-label="Pagination"
      className="flex items-center justify-center gap-1"
    >
      <button
        type="button"
        onClick={() => onPageChange(page - 1)}
        disabled={!hasPrev}
        aria-label="Previous page"
        className={pageButtonClasses(false)}
      >
        <ChevronLeft className="h-4 w-4" aria-hidden />
      </button>

      {windowStart > 1 && (
        <>
          <PageButton
            number={1}
            active={page === 1}
            onClick={() => onPageChange(1)}
          />
          {windowStart > 2 && (
            <span
              aria-hidden
              className="px-1 text-xs text-muted-foreground"
            >
              …
            </span>
          )}
        </>
      )}

      {pages.map((number) => (
        <PageButton
          key={number}
          number={number}
          active={number === page}
          onClick={() => onPageChange(number)}
        />
      ))}

      {windowEnd < totalPages && (
        <>
          {windowEnd < totalPages - 1 && (
            <span
              aria-hidden
              className="px-1 text-xs text-muted-foreground"
            >
              …
            </span>
          )}
          <PageButton
            number={totalPages}
            active={page === totalPages}
            onClick={() => onPageChange(totalPages)}
          />
        </>
      )}

      <button
        type="button"
        onClick={() => onPageChange(page + 1)}
        disabled={!hasNext}
        aria-label="Next page"
        className={pageButtonClasses(false)}
      >
        <ChevronRight className="h-4 w-4" aria-hidden />
      </button>
    </nav>
  );
}

/** Shared class composition for the pagination prev/next arrows. */
function pageButtonClasses(active: boolean): string {
  return cn(
    "inline-flex h-8 min-w-8 items-center justify-center rounded-md",
    "px-2 text-sm font-medium transition-colors",
    "focus-visible:outline-none focus-visible:ring-2",
    "focus-visible:ring-ring focus-visible:ring-offset-2",
    "focus-visible:ring-offset-background",
    "disabled:cursor-not-allowed disabled:opacity-50",
    active
      ? "bg-primary text-primary-foreground shadow-[0_0_8px_hsl(var(--primary)/0.5)]"
      : "text-muted-foreground hover:bg-accent hover:text-foreground",
  );
}

/** A single numbered page button. */
function PageButton({
  number,
  active,
  onClick,
}: {
  number: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      aria-label={`Page ${number}`}
      className={pageButtonClasses(active)}
    >
      {number}
    </button>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// States
// ───────────────────────────────────────────────────────────────────────────

/**
 * Skeleton shown while the initial fetch is in flight.
 *
 * Adapts to the caller's current view mode so that a switch to list view
 * while the page is loading does not flash a grid skeleton.
 */
function LibrarySkeleton({ view }: { view: "grid" | "list" }) {
  if (view === "list") {
    return (
      <div
        className="flex flex-col gap-2"
        aria-busy="true"
        aria-live="polite"
      >
        <span className="sr-only">Loading library…</span>
        {[0, 1, 2, 3].map((index) => (
          <div
            key={index}
            className="flex animate-pulse gap-3 rounded-lg border bg-card p-3"
          >
            <div className="h-24 w-16 shrink-0 rounded-md bg-muted" />
            <div className="flex flex-1 flex-col gap-1.5">
              <div className="h-4 w-1/3 rounded bg-muted" />
              <div className="h-3 w-2/3 rounded bg-muted" />
              <div className="mt-auto h-1 w-full rounded-full bg-muted" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div
      className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Loading library…</span>
      {Array.from({ length: 12 }, (_, index) => (
        <div key={index} className="flex flex-col gap-2">
          <div className="aspect-[2/3] animate-pulse rounded-lg bg-muted" />
          <div className="h-4 animate-pulse rounded bg-muted" />
          <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}

/**
 * Empty-state block shared by the "library empty" and "no match" cases.
 *
 * The two cases are distinguished by ``title`` and ``description`` alone —
 * the structure is identical so that adding an action is a one-line change
 * here.
 */
function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
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
        <BookOpen className="h-6 w-6" />
      </span>
      <div>
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {description}
        </p>
      </div>
      {action}
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Small formatter
// ───────────────────────────────────────────────────────────────────────────

/**
 * Format a byte count as a compact string for the list meta line.
 *
 * Same algorithm as :func:`~../../lib/utils.formatBytes`, but without the
 * thousands separator — inside a sentence like ``"mangadex · 12 MB"`` a
 * separator would read as a stray comma.  Keeping the compact variant
 * local to this page is deliberate: no other component needs it yet, and
 * adding an unused helper to the shared utils module would violate the
 * project's "no dead code" rule.
 */
function formatBytesShort(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  if (index === 0) {
    return `${Math.round(value)} ${units[index]}`;
  }
  return `${value.toFixed(1).replace(/\.0$/, "")} ${units[index]}`;
}

// ───────────────────────────────────────────────────────────────────────────
// Fallback (Suspense)
// ───────────────────────────────────────────────────────────────────────────

/**
 * Skeleton rendered while the Suspense boundary resolves
 * ``useSearchParams()``.
 */
function LibraryFallback() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      <div className="space-y-2">
        <div className="h-9 w-32 animate-pulse rounded bg-muted" />
        <div className="h-4 w-56 animate-pulse rounded bg-muted" />
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="h-9 flex-1 animate-pulse rounded bg-muted" />
        <div className="h-9 w-32 animate-pulse rounded bg-muted" />
      </div>
      <LibrarySkeleton view="grid" />
    </div>
  );
}
