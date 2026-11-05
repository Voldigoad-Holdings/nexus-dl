/**
 * Site-domain types — wire re-exports and client-side UI state.
 *
 * Two distinct concerns live in this module:
 *
 * 1. **Wire-format site types** (:type:`Site`, :type:`SiteCapabilities`,
 *    :type:`SiteHealth`, and the site endpoints' response envelopes).
 *    These are **re-exported from** :mod:`./manga`, where they are
 *    defined.  The reason they live there rather than here is that the
 *    manga, search and library wire formats all embed site references
 *    (``site_id``, ``site_name``) — colocating the two halves of that
 *    shared shape in one file removes a circular-type dependency and
 *    keeps the wire model in one place.  This module is the ergonomic
 *    entry point for callers whose concern is *sites*, not manga.
 *
 * 2. **Client-side UI state types** — the types needed by the site
 *    selector, the site list page and the search filters panel, which are
 *    never sent to or received from the backend.  These are defined here,
 *    and only here: they are the frontend's own vocabulary for "which
 *    sites am I looking at, in what order, grouped how".
 *
 * The split follows the same principle as :mod:`./download` and
 * :mod:`./manga`:
 *
 *  - Enums are **string literal unions**, not TS ``enum``s.
 *  - The corresponding runtime value lists (``SITE_HEALTHS`` etc.) and
 *    type guards are re-exported alongside the types, because they are
 *    the source of truth for dropdowns and validation.
 *  - No field is ever optional "just because" — every optional wire field
 *    is documented as optional in :mod:`./manga` at the point of
 *    definition.
 *
 * No business logic lives here.  The two tiny helpers at the bottom —
 * :func:`siteHealthTone`, :func:`indexSites` — are pure derivations of the
 * types above; they are colocated because they are useful precisely when
 * the types are imported, and because moving them to :mod:`../lib/utils`
 * would force every caller to import from two modules to get a working
 * site view.
 */

// ───────────────────────────────────────────────────────────────────────────
// Wire-format site types — re-exported from ./manga
// ───────────────────────────────────────────────────────────────────────────
//
// These are exported **by type** and **by value** so that callers can use
//   import type { Site } from "@/types/site";
//   import { SITE_HEALTHS, isSiteHealth } from "@/types/site";
// without ever reaching across into the manga module.
//
// Duplication is avoided by construction: a re-export does not create a
// second definition — it is the same symbol, re-surfaced.

import type { Site, SiteHealth } from "./manga";

export type {
  Site,
  SiteCapabilities,
  SiteHealth,
  SiteHealthResponse,
  SiteHealthChangedPayload,
  SiteListResponse,
  SiteResponse,
} from "./manga";

export {
  SITE_HEALTHS,
  USABLE_SITE_HEALTHS,
  isSiteHealth,
  isUsableSiteHealth,
} from "./manga";

// ───────────────────────────────────────────────────────────────────────────
// Client-side UI state types
// ───────────────────────────────────────────────────────────────────────────

/**
 * Filter state for the site selector and the search filters panel.
 *
 * This is **UI state**, not a request body.  When the user submits a
 * search, the relevant fields are translated into a
 * :type:`~./manga.SearchRequest` by the search hook — the mapping is not
 * one-to-one (e.g. ``selected_site_ids`` becomes
 * ``SearchRequest.sites``, and an empty selection becomes ``null``).
 * Keeping the two representations separate means the UI can carry state
 * the wire format does not support (``query`` text being typed,
 * ``disabled_expanded``, etc.) without forcing those fields into a request
 * type.
 */
export interface SiteFilterState {
  /**
   * Current filter query text.  Matched against site id, name and every
   * alias, case-insensitively.  Empty string means "no text filter".
   */
  query: string;
  /** Restrict the visible sites to these languages.  Empty means "all". */
  languages: ReadonlyArray<string>;
  /**
   * Restrict the visible sites to these ids.  Empty means "all".
   *
   * Deliberately typed as a plain array rather than ``ReadonlySet`` so
   * that the state serialises cleanly into a URL query parameter or a
   * Zustand store — both of which are common places to persist it.  The
   * caller can build a ``Set`` when it needs O(1) membership tests.
   */
  site_ids: ReadonlyArray<string>;
  /** Include sites flagged ``adult`` in ``sites.yaml``. */
  include_adult: boolean;
  /**
   * Include sites flagged ``down`` or ``degraded``.  Defaults to ``true``
   * in the UI — the health badge conveys the state — but the search hook
   * uses it to skip dead sites when the user has explicitly disabled them.
   */
  include_unhealthy: boolean;
  /** Include sites present in ``disabled_sites`` (admin-only). */
  include_disabled: boolean;
}

/**
 * Sort order for a site list.
 *
 *  - ``"name-asc"``  — alphabetical, A → Z (default).
 *  - ``"name-desc"`` — alphabetical, Z → A.
 *  - ``"language"``  — grouped by language, then alphabetical within each
 *    language.  Equivalent to grouping by language and sorting inside each
 *    group.
 *  - ``"health"``    — healthiest first (``healthy`` → ``degraded`` →
 *    ``down`` → ``unknown``), then alphabetical within each health tier.
 *
 * Ties are always broken by ``name-asc``, so the ordering is total and
 * deterministic.  This matters for the site selector: a stable order means
 * the checkbox the user just clicked does not jump to a different row on
 * the next render.
 */
export type SiteSortKey =
  | "name-asc"
  | "name-desc"
  | "language"
  | "health";

/**
 * Grouping strategy for a site list.
 *
 *  - ``"none"``     — flat list, sorted by :type:`SiteSortKey` alone.
 *  - ``"language"`` — one section per language; sections ordered by
 *    language display order, sites alphabetically within each.
 *  - ``"health"``   — one section per health state; sections ordered from
 *    healthy to down, sites alphabetically within each.
 *
 * Grouping and sorting are orthogonal: the group key determines the outer
 * ordering, the sort key determines the inner ordering.  When ``group_by``
 * is ``"language"`` and ``sort_key`` is ``"language"``, the inner sort
 * collapses to ``"name-asc"`` (sorting by the group key inside its own
 * group is a no-op).
 */
export type SiteGroupBy = "none" | "language" | "health";

/**
 * Visual tone for a health badge.
 *
 * Purely presentational: components map ``tone`` to a Tailwind colour class
 * (green / yellow / red / grey) via :func:`siteHealthTone`.  The mapping
 * is not encoded in CSS directly so that a theme change never requires
 * touching the badge component.
 */
export type SiteHealthTone = "ok" | "warn" | "bad" | "neutral";

/**
 * Index of sites by id, for O(1) lookups.
 *
 * The site list is fetched once per session and cached; downstream
 * components often need "what is the display name for this site id",
 * "what is this site's language", "what is its health badge".  Handing
 * them a ``Map`` avoids an O(n) ``find`` per row in the search results,
 * which for a 60-site catalogue × 100-result page is measurable.
 *
 * Modelled as ``ReadonlyMap`` rather than an object so that callers
 * cannot accidentally mutate the cache.
 */
export type SiteIndex = ReadonlyMap<string, Site>;

// ───────────────────────────────────────────────────────────────────────────
// Derived state helpers
// ───────────────────────────────────────────────────────────────────────────
//
// These are pure, synchronous, side-effect-free derivations of the types
// above.  They are colocated with the types for ergonomic reasons: every
// caller that needs one of them is already importing from this module.

/**
 * Return the visual tone for a health badge.
 *
 *  - ``healthy``  → ``"ok"``       (green)
 *  - ``degraded`` → ``"warn"``     (yellow)
 *  - ``down``     → ``"bad"``      (red)
 *  - ``unknown``  → ``"neutral"``  (grey)
 *
 * This is the single place the mapping is written.  A theme redesign
 * changes one function, not every badge in the app.
 */
export function siteHealthTone(health: SiteHealth): SiteHealthTone {
  switch (health) {
    case "healthy":
      return "ok";
    case "degraded":
      return "warn";
    case "down":
      return "bad";
    case "unknown":
      return "neutral";
  }
}

/**
 * Build a :type:`SiteIndex` from a list of sites.
 *
 * The last occurrence of a duplicated id wins.  The backend guarantees
 * unique ids, so a duplicate here would indicate a bug — but handling it
 * deterministically beats letting ``Map`` semantics surprise the caller.
 *
 * The input array is not mutated, and the resulting map is a fresh
 * :class:`Map`.  Callers can safely memoise the result with
 * ``useMemo(() => indexSites(sites), [sites])``.
 */
export function indexSites(sites: ReadonlyArray<Site>): SiteIndex {
  const index = new Map<string, Site>();
  for (const site of sites) {
    index.set(site.id, site);
  }
  return index;
}
