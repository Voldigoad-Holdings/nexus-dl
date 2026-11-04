"use client";

/**
 * Search page — the ``/search`` route.
 *
 * Layout
 * ------
 * 1. **Header** — title and short description.
 * 2. **Search bar + Filters toggle** — the primary input, and a button that
 *    reveals the site picker.
 * 3. **Filters panel** (collapsible) — the :class:`~../components/SiteSelector.SiteSelector`
 *    for narrowing which sites the next search hits.  Per the
 *    :func:`~../hooks/useSearch.useSearch` contract, filter changes apply on
 *    the **next** submit — the "search form" pattern.  The panel closes
 *    itself after a successful submit.
 * 4. **Results** — a summary line ("N results for X · page A of B"), an
 *    optional warning listing sites that failed to respond, then a
 *    responsive grid of :class:`~../components/MangaCard.MangaCard` tiles
 *    and a pagination footer.
 *
 * URL integration
 * ---------------
 * ``?q=`` in the URL is the single source of truth for *which query was
 * submitted*, giving bookmarks and shared links a working target:
 *
 * * **On mount**, if ``?q=`` is present, the page auto-submits it once
 *   (after the sites catalogue loads, so that the
 *   ``include_unhealthy``-based site filter is applied correctly).
 * * **On every new submit** whose query differs from the current
 *   ``?q=``, the URL is updated with ``router.push`` — so the back button
 *   returns to the previous search, and every search is shareable.
 * * **Pagination and filters are session-scoped.**  They are intentionally
 *   not in the URL because (a) the hook's ``submitted`` snapshot is
 *   immutable and (b) carrying six site-filter fields in the query string
 *   is not what a "search this" link should look like.  Page 2 is
 *   reachable in a single click from page 1 — no bookmark required.
 *
 * Auto-submit guard
 * -----------------
 * The auto-submit effect uses a ``didAutoSubmit`` ref that becomes ``true``
 * after the first eligible render, whether or not it fired.  This prevents
 * two subtle bugs:
 *
 * * A user who submitted manually on an empty ``/search`` must not see a
 *   second auto-submit when the URL is updated to ``?q=…``.
 * * The effect must not re-run on every render caused by ``search.submit``
 *   changing identity (it depends on ``query`` and ``filters``) — otherwise
 *   it would spam the query cache with duplicate submissions.
 *
 * ``keepPreviousData``
 * --------------------
 * The hook wires TanStack Query's ``placeholderData: keepPreviousData``, so
 * navigating between pages of the same search keeps the previous page
 * visible until the next page loads.  This page renders a subtle
 * "Refreshing…" indicator during that window instead of tearing the grid
 * down.
 *
 * Hydration
 * ---------
 * ``useSearchParams()`` requires a Suspense boundary in Next.js 14, so the
 * default export wraps the body in one.  The fallback mirrors the page's
 * structure so there is no layout shift.
 */
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Filter,
  Loader2,
  RefreshCw,
  Search as SearchIcon,
} from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { MangaCard } from "../../components/MangaCard";
import { SearchBar } from "../../components/SearchBar";
import { SiteSelector } from "../../components/SiteSelector";
import {
  api,
  type NexusApiError,
  type SiteListResponse,
} from "../../lib/api";
import { cn, formatNumber } from "../../lib/utils";
import { useSearch } from "../../hooks/useSearch";
import { useSettingsStore } from "../../store/settings";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Sites-catalogue freshness window. */
const SITES_STALE_TIME_MS = 5 * 60 * 1000;

// ───────────────────────────────────────────────────────────────────────────
// Page
// ───────────────────────────────────────────────────────────────────────────

/**
 * The ``/search`` route.  See the module docstring for the full contract.
 */
export default function SearchPage() {
  return (
    <Suspense fallback={<SearchFallback />}>
      <SearchBody />
    </Suspense>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Body
// ───────────────────────────────────────────────────────────────────────────

function SearchBody() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get("q") ?? "";

  const allowAdult = useSettingsStore((s) => s.allow_adult);

  // -------------------------------------------------------------------
  // Sites catalogue — needed both by the picker and by the hook's
  // unhealthy-site exclusion.
  // -------------------------------------------------------------------
  const sitesQuery = useQuery<SiteListResponse>({
    queryKey: ["sites"],
    queryFn: () => api.getSites(),
    staleTime: SITES_STALE_TIME_MS,
    refetchOnWindowFocus: false,
  });

  // -------------------------------------------------------------------
  // Search hook
  // -------------------------------------------------------------------
  const search = useSearch({
    initialQuery,
    availableSites: sitesQuery.data?.items,
  });

  // -------------------------------------------------------------------
  // Auto-submit on first load when the URL carries a query
  // -------------------------------------------------------------------
  const didAutoSubmit = useRef(false);
  useEffect(() => {
    if (didAutoSubmit.current) {
      return;
    }
    if (search.submitted !== null) {
      // The user (or a previous instance) already submitted — mark the
      // ref so we never auto-submit later, then bail.
      didAutoSubmit.current = true;
      return;
    }
    if (initialQuery.trim().length === 0) {
      return;
    }
    if (sitesQuery.isLoading) {
      // Wait for the sites catalogue so that
      // ``filters.include_unhealthy === false`` resolves the correct
      // site list before we submit.
      return;
    }
    didAutoSubmit.current = true;
    search.submit();
  }, [
    initialQuery,
    search.submit,
    search.submitted,
    sitesQuery.isLoading,
  ]);

  // -------------------------------------------------------------------
  // Keep URL in sync with the submitted query
  // -------------------------------------------------------------------
  // Only runs when ``submitted`` changes (or the URL does).  Uses
  // ``router.push`` (not ``replace``) so that the back button returns to
  // the previous search — the standard behaviour for a search form.
  useEffect(() => {
    if (search.submitted === null) {
      return;
    }
    const currentQ = searchParams.get("q");
    if (search.submitted.query === currentQ) {
      return;
    }
    router.push(
      `/search?q=${encodeURIComponent(search.submitted.query)}`,
    );
  }, [search.submitted, searchParams, router]);

  // -------------------------------------------------------------------
  // Local UI state
  // -------------------------------------------------------------------
  const [showFilters, setShowFilters] = useState(false);

  const activeFilterCount =
    search.filters.site_ids.length + search.filters.languages.length;

  // -------------------------------------------------------------------
  // Derived display state
  // -------------------------------------------------------------------
  const startResult =
    search.total === 0 ? 0 : (search.page - 1) * search.perPage + 1;
  const endResult = Math.min(search.page * search.perPage, search.total);

  // -------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      {/* Header ---------------------------------------------- */}
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="neon-text text-2xl font-bold tracking-tight sm:text-3xl">
            Search
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Search every enabled site at once.
          </p>
        </div>

        {search.isFetching && !search.isLoading && (
          <span
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
            aria-live="polite"
          >
            <Loader2
              className="h-3.5 w-3.5 animate-spin"
              aria-hidden
            />
            Refreshing…
          </span>
        )}
      </header>

      {/* Search bar + filters toggle ------------------------- */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="flex-1">
          <SearchBar
            value={search.query}
            onChange={search.setQuery}
            onSubmit={search.submit}
            isLoading={search.isFetching && search.submitted !== null}
            disabled={search.isFetching && search.submitted !== null}
          />
        </div>

        <button
          type="button"
          onClick={() => setShowFilters((visible) => !visible)}
          aria-expanded={showFilters}
          aria-controls="filters-panel"
          className={cn(
            "inline-flex h-10 items-center gap-1.5 rounded-md px-3",
            "border bg-background text-sm font-medium",
            "text-muted-foreground transition-colors",
            "hover:bg-accent hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
            showFilters && "bg-accent text-foreground",
          )}
        >
          <Filter className="h-4 w-4" aria-hidden />
          <span>Filters</span>
          {activeFilterCount > 0 && (
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-xs font-mono",
                "bg-primary/20 text-primary",
              )}
            >
              {activeFilterCount}
            </span>
          )}
        </button>
      </div>

      {/* Filters panel --------------------------------------- */}
      {showFilters && (
        <section
          id="filters-panel"
          aria-label="Search filters"
          className={cn(
            "flex flex-col gap-3 rounded-lg border bg-card p-4",
            "text-card-foreground shadow-sm",
          )}
        >
          <div>
            <h2 className="text-sm font-semibold">Sites</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Restrict the next search to the sites you select.  Leave
              every site unchecked to search all of them.
            </p>
          </div>

          <SiteSelector
            sites={sitesQuery.data?.items ?? []}
            value={search.filters.site_ids}
            onChange={(ids) =>
              search.setFilters({ ...search.filters, site_ids: ids })
            }
            includeAdult={allowAdult}
            disabled={sitesQuery.isLoading}
            className="max-h-96 overflow-y-auto"
          />

          <p className="text-xs text-muted-foreground">
            Filter changes apply on the next search.
          </p>
        </section>
      )}

      {/* Sites load error (non-blocking) -------------------- */}
      {sitesQuery.error !== null && (
        <div
          role="status"
          className={cn(
            "flex items-start gap-2 rounded-md border border-amber-500/30",
            "bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-400",
          )}
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div className="flex-1">
            <p className="font-medium">Site list unavailable</p>
            <p className="mt-0.5 text-xs opacity-80">
              You can still search, but per-site filters are disabled.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void sitesQuery.refetch()}
            className={cn(
              "rounded px-2 py-1 text-xs font-medium",
              "hover:bg-amber-500/20",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-amber-500/50",
            )}
          >
            Retry
          </button>
        </div>
      )}

      {/* Results ---------------------------------------------- */}
      {search.submitted === null && <InitialState />}

      {search.isLoading && <ResultsSkeleton />}

      {search.isError && search.error !== null && (
        <ErrorBanner error={search.error} onRetry={search.refetch} />
      )}

      {!search.isLoading && search.submitted !== null && (
        <>
          {search.results.length > 0 && (
            <>
              <ResultSummary
                query={search.submitted.query}
                total={search.total}
                page={search.page}
                totalPages={search.totalPages}
                start={startResult}
                end={endResult}
              />

              {search.failedSites.length > 0 && (
                <FailedSitesWarning sites={search.failedSites} />
              )}

              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
                {search.results.map((result) => (
                  <MangaCard key={result.manga_id} data={result} />
                ))}
              </div>

              {search.totalPages > 1 && (
                <Pagination
                  page={search.page}
                  totalPages={search.totalPages}
                  hasPrev={search.hasPrev}
                  hasNext={search.hasNext}
                  onChange={search.setPage}
                />
              )}
            </>
          )}

          {search.results.length === 0 &&
            !search.isError &&
            search.submitted !== null && (
              <NoResults query={search.submitted.query} />
            )}
        </>
      )}
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * Empty state shown before the first submit — the landing experience.
 *
 * Rendered as a centred block so the page does not look broken on first
 * visit; the SearchBar above is the call to action.
 */
function InitialState() {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-lg border border-dashed",
        "px-6 py-16 text-center",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "grid h-14 w-14 place-items-center rounded-full",
          "bg-primary/10 text-primary shadow-[0_0_24px_hsl(var(--primary)/0.3)]",
        )}
      >
        <SearchIcon className="h-7 w-7" />
      </span>
      <div className="max-w-md">
        <p className="text-base font-medium">Start typing to search</p>
        <p className="mt-1 text-sm text-muted-foreground">
          NexusDL queries every enabled site at once.  Use the{" "}
          <span className="font-medium text-foreground">Filters</span>{" "}
          panel to narrow the search to specific sites.
        </p>
      </div>
    </div>
  );
}

/** Skeleton shown during the first fetch after a submit. */
function ResultsSkeleton() {
  return (
    <div
      className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Searching…</span>
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
 * "N results for X · page A of B" summary line.
 *
 * The query is quoted with typographic quotes (``"…"`` not ``"…"``) to
 * match the surrounding prose style; using ASCII quotes inside a UI
 * message is the kind of detail that reads as an oversight rather than a
 * choice.
 */
function ResultSummary({
  query,
  total,
  page,
  totalPages,
  start,
  end,
}: {
  query: string;
  total: number;
  page: number;
  totalPages: number;
  start: number;
  end: number;
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <p className="text-sm">
        <span className="font-medium text-foreground">
          {formatNumber(total)}
        </span>{" "}
        {total === 1 ? "result" : "results"} for{" "}
        <span className="font-medium text-foreground">
          &ldquo;{query}&rdquo;
        </span>
        {totalPages > 1 && (
          <span className="ml-1 text-muted-foreground">
            · page {page} of {totalPages}
          </span>
        )}
      </p>
      {total > 0 && end > start - 1 && (
        <p className="text-xs text-muted-foreground">
          Showing {formatNumber(start)}–{formatNumber(end)} of{" "}
          {formatNumber(total)}
        </p>
      )}
    </div>
  );
}

/**
 * Warning banner listing sites that did not respond.
 *
 * Shown **above** the results — the user should know that fewer sites
 * answered than they asked for.  When there are more than three failing
 * sites, the list is truncated to "and N more" to keep the banner on one
 * line on desktop; the full list is available via the ``title`` tooltip.
 */
function FailedSitesWarning({ sites }: { sites: readonly string[] }) {
  const count = sites.length;
  const preview = sites.slice(0, 3).join(", ");
  const hasMore = count > 3;

  return (
    <div
      role="status"
      aria-live="polite"
      title={sites.join(", ")}
      className={cn(
        "flex items-start gap-2 rounded-md border border-amber-500/30",
        "bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-400",
      )}
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1">
        <p>
          {count === 1
            ? "1 site did not respond"
            : `${count} sites did not respond`}
          <span className="ml-1 opacity-80">
            ({preview}
            {hasMore ? `, +${count - 3} more` : ""})
          </span>
        </p>
        <p className="mt-0.5 text-xs opacity-80">
          Their results are missing from this page.  Retry the search to
          try again.
        </p>
      </div>
    </div>
  );
}

/** "No results" block shown when a successful search returns nothing. */
function NoResults({ query }: { query: string }) {
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
        <SearchIcon className="h-6 w-6" />
      </span>
      <div>
        <p className="text-sm font-medium">
          No results for &ldquo;{query}&rdquo;
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Try a different spelling, a shorter query, or clear the site
          filters to search every site.
        </p>
      </div>
    </div>
  );
}

/**
 * Error banner for a failed search fetch.
 *
 * Shown above the (potentially stale) results so the user knows the
 * current page may not reflect the latest state.  The retry button is the
 * primary affordance — a search error is virtually always transient
 * (timeout, rate limit, one site down).
 */
function ErrorBanner({
  error,
  onRetry,
}: {
  error: NexusApiError;
  onRetry: () => void;
}) {
  // Map the most common error codes to a helpful message.  Anything else
  // falls through to the backend's own message, which is already
  // user-facing because the backend uses the same envelope format.
  const title =
    error.code === "SITE_UNAVAILABLE"
      ? "No site responded"
      : error.code === "RATE_LIMIT_EXCEEDED"
        ? "Too many requests"
        : error.code === "SITE_BLOCKED"
          ? "Some sites are blocked"
          : "Search failed";

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
        <p className="mt-0.5 text-xs opacity-80">{error.message}</p>
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

// ───────────────────────────────────────────────────────────────────────────
// Pagination
// ───────────────────────────────────────────────────────────────────────────

/**
 * Compact pagination control.
 *
 * Mirrors the pattern used on ``/library`` — a sliding window of at most
 * five page numbers around the current page, with first/last jump links
 * when applicable.  Page numbers are buttons (not links) because the
 * navigation is client-side and does not add a history entry.
 */
function Pagination({
  page,
  totalPages,
  hasPrev,
  hasNext,
  onChange,
}: {
  page: number;
  totalPages: number;
  hasPrev: boolean;
  hasNext: boolean;
  onChange: (next: number) => void;
}) {
  const windowStart = Math.max(1, page - 2);
  const windowEnd = Math.min(totalPages, page + 2);
  const pages: number[] = [];
  for (let i = windowStart; i <= windowEnd; i += 1) {
    pages.push(i);
  }

  return (
    <nav
      aria-label="Search results pagination"
      className="flex items-center justify-center gap-1 pt-2"
    >
      <button
        type="button"
        onClick={() => onChange(page - 1)}
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
            onClick={() => onChange(1)}
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
          onClick={() => onChange(number)}
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
            onClick={() => onChange(totalPages)}
          />
        </>
      )}

      <button
        type="button"
        onClick={() => onChange(page + 1)}
        disabled={!hasNext}
        aria-label="Next page"
        className={pageButtonClasses(false)}
      >
        <ChevronRight className="h-4 w-4" aria-hidden />
      </button>
    </nav>
  );
}

/** Shared class composition for the prev/next arrows. */
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
// Fallback (Suspense)
// ───────────────────────────────────────────────────────────────────────────

/**
 * Skeleton rendered while the Suspense boundary resolves
 * ``useSearchParams()``.  Matches the page's structure (header, search
 * bar, results grid) so there is no layout shift on resolution.
 */
function SearchFallback() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
      <div className="space-y-2">
        <div className="h-9 w-32 animate-pulse rounded bg-muted" />
        <div className="h-4 w-56 animate-pulse rounded bg-muted" />
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="h-10 flex-1 animate-pulse rounded bg-muted" />
        <div className="h-10 w-24 animate-pulse rounded bg-muted" />
      </div>
      <ResultsSkeleton />
    </div>
  );
}
