"use client";

/**
 * React hook for aggregated search across every enabled site.
 *
 * This hook is the frontend's only entry point for search.  It manages:
 *
 * 1. **Form state** — the query text being typed and the site-filter
 *    state, both ephemeral.
 * 2. **Submission** — the moment the user hits Enter or the "Search"
 *    button, a snapshot of the current query and filters is captured and
 *    drives the actual request.  Changing the filters afterwards does
 *    **not** retrigger the search — the user must submit again, which is
 *    the standard "search form" pattern.
 * 3. **Pagination** — page navigation on the submitted snapshot.  Changing
 *    the page does not require a new submit — it reuses the same captured
 *    parameters with a different page number.
 * 4. **Caching** — results are cached by TanStack Query for 5 minutes
 *    (stale time) / 10 minutes (cache time).  Navigating away and back,
 *    or re-submitting the same search, restores cached results
 *    immediately and may trigger a background refresh.
 * 5. **Smooth pagination** — ``placeholderData: keepPreviousData`` keeps
 *    the previous page visible while the next page loads, so the result
 *    grid does not collapse during a page change.
 *
 * Wire-format mapping
 * -------------------
 * The search request body is built from the submitted snapshot:
 *
 * | Captured field    | Source                                   | Mapping                                  |
 * |-------------------|------------------------------------------|------------------------------------------|
 * | ``query``         | The trimmed text the user submitted      | → ``SearchRequest.query``                |
 * | ``sites``         | ``filters.site_ids`` or unhealthy-filter | → ``SearchRequest.sites``                |
 * | ``languages``     | ``filters.languages``                    | → ``SearchRequest.languages``            |
 * | ``include_adult`` | ``filters.include_adult``                | → ``SearchRequest.include_adult``        |
 * | (implicit)        | ``page``                                 | → ``SearchRequest.page``                 |
 * | (implicit)        | ``perPage``                              | → ``SearchRequest.per_page``             |
 *
 * The **``include_unhealthy``** filter is not part of the API.  It is
 * applied client-side, but only when the caller passes
 * ``availableSites``: the hook then excludes sites whose
 * ``health`` is ``"down"`` or ``"degraded"`` from the ``sites`` list it
 * sends.  When ``availableSites`` is not provided, the hook searches all
 * sites regardless of health — the backend's ``failed_sites`` field in
 * the response reports which sites failed mid-search, so the caller can
 * still surface that information to the user.
 *
 * Error handling
 * --------------
 * All errors from the API client are normalised to
 * :class:`~../lib/api.NexusApiError`.  Callers can switch on
 * ``error.code`` to distinguish:
 *
 *  * ``"SITE_UNAVAILABLE"`` — no site responded;
 *  * ``"RATE_LIMIT_EXCEEDED"`` — back off and retry later;
 *  * ``"VALIDATION_ERROR"`` — the query is malformed (should not happen
 *    from our own form, but defensive UI is cheap).
 *
 * The hook does **not** swallow errors: they are surfaced through
 * :attr:`UseSearchReturn.error`, and it is up to the consuming component
 * to decide what to render.  This is intentional — search errors are
 * user-facing and the correct UX depends on the context (inline message,
 * toast, "retry" button).
 *
 * Client-boundary
 * ---------------
 * The hook is marked ``"use client"`` because it depends on React state
 * and on TanStack Query, neither of which work in Server Components.  This
 * prevents a Server Component from accidentally importing the hook and
 * failing at runtime with a less helpful error.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { api, type NexusApiError } from "../lib/api";
import { useSettingsStore } from "../store/settings";
import {
  isUsableSiteHealth,
  LANGUAGES,
  type Language,
  type SearchResult,
  type Site,
} from "../types/manga";
import type { SiteFilterState } from "../types/site";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Default number of results per page.  Matches the backend's default. */
const DEFAULT_PER_PAGE = 20;

/** Result freshness window.  Within this window, re-searching uses cache. */
const STALE_TIME_MS = 5 * 60 * 1000;

/** Inactive cache retention window.  After this, results are garbage-collected. */
const CACHE_TIME_MS = 10 * 60 * 1000;

/**
 * Runtime set of valid :type:`Language` values.
 *
 * Used to filter out any non-``Language`` value that a caller may have
 * injected into ``filters.languages`` through a cast (see the note on
 * :func:`submit`).  The compiler-level typing makes such a value
 * unreachable through the documented API, but a programmatically-built
 * filter state or a corrupted store payload could still smuggle one in.
 * Filtering before sending keeps the backend's strict enum validation from
 * rejecting the entire search because of a single invalid item.
 */
const VALID_LANGUAGES: ReadonlySet<string> = new Set(LANGUAGES);

// ───────────────────────────────────────────────────────────────────────────
// Types
// ───────────────────────────────────────────────────────────────────────────

/**
 * Options accepted by :func:`useSearch`.
 */
export interface UseSearchOptions {
  /**
   * Initial text for the search input.
   *
   * Used to pre-fill the form when navigating to the search page from a
   * link that already contains a query (e.g. "search for this series"
   * from a manga detail page).  Defaults to ``""``.
   */
  initialQuery?: string;

  /**
   * Initial state for the site-filter panel.  Any field not provided falls
   * back to its default:
   *
   *  * ``query``             — ``""``
   *  * ``languages``         — ``[]``
   *  * ``site_ids``          — ``[]``
   *  * ``include_adult``     — the current ``allow_adult`` setting
   *  * ``include_unhealthy`` — the current ``default_include_unhealthy``
   *    setting
   *  * ``include_disabled``  — ``false``
   */
  initialFilters?: Partial<SiteFilterState>;

  /**
   * Number of results per page.  The backend caps this at 100.
   * Defaults to :data:`DEFAULT_PER_PAGE`.
   */
  perPage?: number;

  /**
   * Optional list of all available sites.
   *
   * When provided **and** ``filters.include_unhealthy`` is ``false``
   * **and** no explicit ``site_ids`` are selected, the hook restricts the
   * search to sites whose health is usable (``healthy`` or ``degraded``)
   * rather than searching all sites.  When omitted, the hook always
   * searches all sites in that case — the backend's ``failed_sites``
   * field reports which sites ultimately failed, so no signal is lost.
   */
  availableSites?: ReadonlyArray<Site>;
}

/**
 * The exact parameters captured at submit time.
 *
 * Deliberately a **plain, serialisable** object so that TanStack Query can
 * include it in the cache key structurally — a fresh object with the same
 * field values always produces the same cache entry.
 */
interface SubmittedSearch {
  query: string;
  sites: string[] | null;
  languages: Language[] | null;
  include_adult: boolean;
}

/**
 * The value returned by :func:`useSearch`.
 *
 * All fields are stable references — the caller can destructure freely
 * without re-render churn.
 */
export interface UseSearchReturn {
  // -------------------------------------------------------------------
  // Form state
  // -------------------------------------------------------------------

  /** Current text in the search input. */
  query: string;
  /** Update the search input text. */
  setQuery: (query: string) => void;

  /** Current site-filter state. */
  filters: SiteFilterState;
  /**
   * Replace the site-filter state.
   *
   * The caller must supply a full :type:`SiteFilterState` — the hook does
   * not merge partial updates.  Use
   * ``setFilters({ ...filters, site_ids: ["mangadex"] })`` to change one
   * field.
   */
  setFilters: (filters: SiteFilterState) => void;

  // -------------------------------------------------------------------
  // Submission
  // -------------------------------------------------------------------

  /**
   * The captured parameters of the most recent search, or ``null`` when
   * no search has been submitted yet.
   *
   * Exposed for advanced callers that need to display "Searching for X"
   * or "No results for Y" messages — the current :attr:`query` input may
   * have already diverged from the submitted text.
   */
  submitted: SubmittedSearch | null;

  /**
   * Submit the current query and filters as a new search.
   *
   * No-op when the trimmed query is empty.  Resets the page to 1 and
   * captures a snapshot of the current filters so that subsequent filter
   * edits do not retrigger the search until the user submits again.
   */
  submit: () => void;

  /**
   * Clear the search input, discard the submitted parameters and reset
   * the page.  Cached results remain in the TanStack Query cache until
   * their ``gcTime`` expires.
   */
  reset: () => void;

  // -------------------------------------------------------------------
  // Pagination
  // -------------------------------------------------------------------

  /** Current 1-based page number. */
  page: number;
  /**
   * Change the page.  Values below 1 are ignored.  Does **not** re-apply
   * the current filters — the submitted snapshot drives the request.
   */
  setPage: (page: number) => void;

  /** Number of results per page.  Echoed from the options. */
  perPage: number;

  // -------------------------------------------------------------------
  // Results
  // -------------------------------------------------------------------

  /** The current page's results, or ``[]`` before the first successful fetch. */
  results: SearchResult[];

  /** Total number of matches across all queried sites. */
  total: number;

  /** Total number of pages, based on ``total`` and ``perPage``. */
  totalPages: number;

  /**
   * Site ids that failed to respond during the most recent search.
   *
   * Empty when every site responded.  Callers should surface this to the
   * user as a warning ("3 sites failed to respond") rather than silently
   * showing fewer results than expected.
   */
  failedSites: string[];

  /** ``true`` when there is a page after the current one. */
  hasNext: boolean;

  /** ``true`` when there is a page before the current one. */
  hasPrev: boolean;

  // -------------------------------------------------------------------
  // Loading and error state
  // -------------------------------------------------------------------

  /**
   * ``true`` when a search has been submitted and its initial fetch is
   * still in flight.  ``false`` before the first submit — this is what
   * distinguishes "not searched yet" from "loading".
   */
  isLoading: boolean;

  /**
   * ``true`` whenever a request is in flight, including background
   * refetches and page changes on an already-populated result set.
   */
  isFetching: boolean;

  /** ``true`` when the most recent request failed. */
  isError: boolean;

  /**
   * The most recent error, or ``null``.
   *
   * Always a :class:`~../lib/api.NexusApiError` (the API client normalises
   * every failure to this class), so callers can safely access
   * ``error.code`` without an ``instanceof`` check.
   */
  error: NexusApiError | null;

  /**
   * Manually refetch the current query.  No-op when no search has been
   * submitted.
   */
  refetch: () => void;
}

// ───────────────────────────────────────────────────────────────────────────
// Hook
// ───────────────────────────────────────────────────────────────────────────

/**
 * Manage aggregated search state and execution.
 *
 * See the module docstring for the full contract.  The hook is intentionally
 * composable: it does not fetch the sites list itself, does not render any
 * UI, and does not subscribe to any global event stream.  Its only job is to
 * own the search request and its lifecycle.
 *
 * @example Standard usage in a search page component:
 *   const search = useSearch({ availableSites: sitesQuery.data?.items });
 *
 *   <SearchBar
 *     value={search.query}
 *     onChange={search.setQuery}
 *     onSubmit={search.submit}
 *   />
 *   {search.isLoading && <Skeleton />}
 *   {search.isError && <ErrorBanner error={search.error} />}
 *   {search.results.map((r) => <MangaCard key={r.manga_id} result={r} />)}
 *   <Pagination
 *     page={search.page}
 *     totalPages={search.totalPages}
 *     onChange={search.setPage}
 *   />
 */
export function useSearch(options: UseSearchOptions = {}): UseSearchReturn {
  const perPage = options.perPage ?? DEFAULT_PER_PAGE;

  // Pull the two settings that affect search.  Subscribing with a selector
  // rather than reading the whole store means the component using this
  // hook only re-renders when these two fields change — a sidebar toggle
  // or library-view change does not retrigger it.
  const allowAdult = useSettingsStore((s) => s.allow_adult);
  const defaultIncludeUnhealthy = useSettingsStore(
    (s) => s.default_include_unhealthy,
  );

  // ``availableSites`` is typically rebuilt on every render by the caller
  // (``sitesQuery.data?.items`` changes identity when the query resolves
  // again, and the parent may wrap it in a fresh ``?? []`` fallback).
  // Capturing it in a ref keeps ``submit`` stable across re-renders that
  // do not change the actual contents, so a parent effect depending on
  // ``submit`` does not loop.  The ref is always current at the moment
  // ``submit`` runs — the effect that updates it runs synchronously before
  // any user interaction can fire.
  const availableSitesRef = useRef<ReadonlyArray<Site> | undefined>(
    options.availableSites,
  );
  availableSitesRef.current = options.availableSites;

  // -------------------------------------------------------------------
  // Form state
  // -------------------------------------------------------------------
  const [query, setQueryState] = useState(options.initialQuery ?? "");
  const [filters, setFiltersState] = useState<SiteFilterState>(() => ({
    query: "",
    languages: [],
    site_ids: [],
    include_adult: options.initialFilters?.include_adult ?? allowAdult,
    include_unhealthy:
      options.initialFilters?.include_unhealthy ?? defaultIncludeUnhealthy,
    include_disabled: options.initialFilters?.include_disabled ?? false,
    ...options.initialFilters,
  }));

  // -------------------------------------------------------------------
  // Submitted snapshot and pagination
  // -------------------------------------------------------------------
  const [submitted, setSubmitted] = useState<SubmittedSearch | null>(null);
  const [page, setPageState] = useState(1);

  // -------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------
  const setQuery = useCallback((next: string) => {
    setQueryState(next);
  }, []);

  const setFilters = useCallback((next: SiteFilterState) => {
    setFiltersState(next);
  }, []);

  const submit = useCallback(() => {
    const trimmed = query.trim();
    if (trimmed.length === 0) {
      return;
    }

    // Resolve the sites list.  Three cases:
    //   1. Explicit selection exists — send exactly those site ids.
    //   2. No selection + exclude-unhealthy + caller provided the site
    //      list — restrict to sites whose health is usable.
    //   3. Otherwise — send ``null`` to search every enabled site.
    let sites: string[] | null = null;
    if (filters.site_ids.length > 0) {
      sites = [...filters.site_ids];
    } else if (!filters.include_unhealthy) {
      const available = availableSitesRef.current;
      if (available !== undefined) {
        sites = available
          .filter((site) => isUsableSiteHealth(site.health))
          .map((site) => site.id);
      }
    }

    // Resolve the languages list.  Empty means "all languages"; the
    // backend treats a missing field and an empty array identically.
    //
    // ``filters.languages`` is typed ``ReadonlyArray<Language>`` at the
    // compiler level — no cast is needed for that reason.  The runtime
    // filter is defensive: a programmatically-built filter state or a
    // corrupted localStorage payload could still smuggle a non-Language
    // string past the type system, and the backend's strict enum
    // validation would reject the *whole* search if even one item were
    // invalid.  Filtering here isolates the failure to the one bad item.
    const validLanguages = filters.languages.filter((lang) =>
      VALID_LANGUAGES.has(lang as string),
    );
    const languages: Language[] | null =
      validLanguages.length > 0 ? [...validLanguages] : null;

    // Resolve the adult flag.  The UI gates ``include_adult`` behind the
    // user's ``allow_adult`` setting, so ``filters.include_adult`` is
    // already trustworthy — but recompute defensively in case a caller
    // builds the filter state programmatically.
    const include_adult = allowAdult && filters.include_adult;

    setSubmitted({ query: trimmed, sites, languages, include_adult });
    setPageState(1);
  }, [query, filters, allowAdult]);

  const reset = useCallback(() => {
    setQueryState("");
    setSubmitted(null);
    setPageState(1);
  }, []);

  const setPage = useCallback((next: number) => {
    if (!Number.isFinite(next) || next < 1) {
      return;
    }
    setPageState(Math.floor(next));
  }, []);

  // -------------------------------------------------------------------
  // Query
  // -------------------------------------------------------------------
  //
  // The query key includes the submitted snapshot (a plain object with
  // primitive and array fields) and the current page.  TanStack Query
  // hashes the key structurally, so the same submitted parameters on
  // different page numbers produce distinct cache entries, and re-
  // submitting the same search within ``STALE_TIME_MS`` uses the cache.
  //
  // The ``enabled`` flag prevents the query from firing before the user
  // has submitted anything.  When disabled, ``isPending`` is still true
  // in TanStack Query v5 — but we don't expose that directly to callers;
  // see :attr:`UseSearchReturn.isLoading` below.
  const queryResult = useQuery({
    queryKey: ["search", submitted, page, perPage],
    queryFn: async () => {
      if (submitted === null) {
        // Unreachable because the query is disabled when submitted is
        // null.  Guarded here so the type checker is satisfied and so a
        // future refactor cannot introduce a subtle bug.
        throw new Error("Search query executed without a submitted payload");
      }
      return api.search({
        query: submitted.query,
        sites: submitted.sites,
        languages: submitted.languages,
        include_adult: submitted.include_adult,
        page,
        per_page: perPage,
      });
    },
    enabled: submitted !== null,
    staleTime: STALE_TIME_MS,
    gcTime: CACHE_TIME_MS,
    placeholderData: keepPreviousData,
  });

  // -------------------------------------------------------------------
  // Derived values
  // -------------------------------------------------------------------
  const data = queryResult.data;
  const results = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = data?.total_pages ?? 0;
  const failedSites = data?.failed_sites ?? [];
  const hasNext = data?.has_next ?? false;
  const hasPrev = data?.has_prev ?? false;

  // The API client normalises every error to ``NexusApiError``, so this
  // cast is safe.  The cast is written with ``as`` rather than a type
  // predicate because TanStack Query's ``error`` type is ``Error | null``
  // and we know the actual runtime value is a ``NexusApiError``.
  const error = (queryResult.error as NexusApiError | null) ?? null;

  const isLoading = submitted !== null && queryResult.isPending;

  // -------------------------------------------------------------------
  // Return
  // -------------------------------------------------------------------
  return {
    query,
    setQuery,
    filters,
    setFilters,
    submitted,
    submit,
    reset,
    page,
    setPage,
    perPage,
    results,
    total,
    totalPages,
    failedSites,
    hasNext,
    hasPrev,
    isLoading,
    isFetching: queryResult.isFetching,
    isError: queryResult.isError,
    error,
    refetch: useCallback(() => {
      if (submitted !== null) {
        void queryResult.refetch();
      }
    }, [submitted, queryResult.refetch]),
  };
}
