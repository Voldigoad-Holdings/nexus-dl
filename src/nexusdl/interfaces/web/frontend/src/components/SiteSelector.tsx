"use client";

/**
 * SiteSelector — a multi-select site picker for search, library and
 * download-filter panels.
 *
 * The component renders the list of sites the parent passes in, with:
 *
 * * a free-text filter (matches site id, name and — case-insensitively —
 *   any language name);
 * * an optional language filter, shown only when the caller has more than
 *   one language in the input;
 * * a bulk "Select all visible" / "Clear" pair;
 * * one row per site, with a checkbox, the site name, its health badge
 *   and — when the list is not already grouped by language — its language
 *   code;
 * * grouping by ``language`` (default) or by ``health``, or no grouping;
 * * an empty state when the filter matches nothing.
 *
 * The component is **fully controlled**: it never mutates ``value``
 * directly.  Every toggle invokes ``onChange`` with a fresh array.  This
 * is the right pattern for filter panels where the parent owns the state
 * and often mirrors it in a URL or in a Zustand store — a controlled
 * selector is exactly what those callers need.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  This component is
 * therefore built exclusively with:
 *
 *   * native HTML form controls (``<input type="checkbox">``,
 *     ``<input type="search">``, ``<button>``);
 *   * Tailwind utility classes for layout, typography and colour;
 *   * the project's ``cn`` helper;
 *   * ``lucide-react`` icons;
 *   * the ``siteHealthTone`` mapping and ``Site``/``Language``/``SiteHealth``
 *     types from ``types/site.ts`` and ``types/manga.ts``.
 *
 * This keeps the component fully functional **today**, without forcing the
 * caller to wait for ``components/ui/checkbox.tsx`` or any other shadcn
 * primitive.  When those primitives land, the visual style can be upgraded
 * in a single self-contained edit — the public API (``sites``, ``value``,
 * ``onChange``, ``includeAdult``, ``groupBy``, ``className``, ``disabled``)
 * does not change.
 *
 * Orphan ids
 * ----------
 * ``value`` may legitimately contain site ids that are not present in
 * ``sites`` (e.g. a site was disabled in ``sites.yaml`` after the user
 * last selected it).  Those ids are **preserved**: toggling a visible site
 * does not silently drop them from the selection.  The parent decides
 * whether to prune them when it acts on the selection.
 *
 * Adult filtering
 * ---------------
 * Adult filtering is **caller-controlled**, via :attr:`includeAdult`.
 * When the caller passes ``false`` or omits the prop, adult-flagged sites
 * are excluded from the visible list — but ids already in ``value``
 * pointing at those sites are preserved (see "orphan ids" above).  This is
 * what lets the caller toggle the adult filter without losing the user's
 * selection.
 *
 * Accessibility
 * -------------
 * * Each row is a ``<label>`` wrapping its ``<input type="checkbox">``, so
 *   clicking anywhere on the row toggles the checkbox and the checkbox
 *   carries the label text as its accessible name.
 * * Groups are ``role="group"`` with an ``<h3>`` heading linked via
 *   ``aria-labelledby``.
 * * The "N of M selected" counter is ``aria-live="polite"`` so that a
 *   screen-reader user is notified when a bulk action changes the count.
 * * The search input has an explicit ``aria-label`` (no visible
 *   placeholder-only label) because placeholder text is not announced
 *   reliably.
 */
import { Check, Filter, Search, X } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { cn } from "../lib/utils";
import {
  LANGUAGES,
  SITE_HEALTHS,
  type Language,
  type Site,
  type SiteHealth,
} from "../types/manga";
import { siteHealthTone, type SiteHealthTone } from "../types/site";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * Human-readable labels for each health state, shown in the badge.
 *
 * Kept as a static table rather than derived from ``SITE_HEALTHS`` because
 * the strings are the UI-visible English defaults — the caller's locale
 * applies at the React tree level via the i18n layer, not at the badge
 * level.
 */
const HEALTH_LABELS: Record<SiteHealth, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  down: "Down",
  unknown: "Unknown",
};

/**
 * Tailwind colour classes for each tone of the health badge.
 *
 * The mapping is deliberately local — the ``siteHealthTone`` helper in
 * ``types/site.ts`` decides *which* tone applies; this component decides
 * *how* each tone is rendered.  A theme redesign that changes colours
 * touches this table only, not the tone helper.
 */
const HEALTH_BADGE_CLASSES: Record<SiteHealthTone, string> = {
  ok: "bg-emerald-500/10 text-emerald-700 ring-emerald-600/20 dark:text-emerald-400",
  warn: "bg-amber-500/10 text-amber-700 ring-amber-600/20 dark:text-amber-400",
  bad: "bg-red-500/10 text-red-700 ring-red-600/20 dark:text-red-400",
  neutral: "bg-muted text-muted-foreground ring-border",
};

/**
 * Human-readable labels for each language, used when the list is not
 * grouped by language.  Falls back to the ISO code when the language has
 * no entry here — the caller can add a locale-aware override via the
 * i18n layer in a future iteration without touching this component.
 */
const LANGUAGE_LABELS: Record<Language, string> = {
  fr: "French",
  en: "English",
  ja: "Japanese",
  ko: "Korean",
  zh: "Chinese",
  es: "Spanish",
  pt: "Portuguese",
  it: "Italian",
  de: "German",
  ru: "Russian",
  ar: "Arabic",
  unknown: "Unknown",
};

/** Ordering helper for language groups, using the canonical enum order. */
const LANGUAGE_ORDER: readonly Language[] = LANGUAGES;

/** Ordering helper for health groups, from best to worst. */
const HEALTH_ORDER: readonly SiteHealth[] = SITE_HEALTHS;

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`SiteSelector`.
 */
export interface SiteSelectorProps {
  /** The sites to display. */
  sites: ReadonlyArray<Site>;

  /** Currently-selected site ids.  Orphan ids are preserved. */
  value: ReadonlyArray<string>;

  /**
   * Called with the new selection whenever a row is toggled or a bulk
   * action runs.  The argument is always a **fresh** array — the component
   * never mutates ``value``.
   */
  onChange: (ids: string[]) => void;

  /**
   * Whether to show adult-flagged sites.  Defaults to ``false``, which is
   * the safe default for a component that may render in front of a user
   * who has not opted in.  The caller is expected to pass ``true`` only
   * when ``settings.allow_adult`` is enabled.
   */
  includeAdult?: boolean;

  /**
   * How to group the visible sites.  ``"language"`` (default) groups by
   * the site's declared language, in the canonical language order;
   * ``"health"`` groups by current health, from healthy to down;
   * ``"none"`` renders a flat list.
   *
   * In every grouping, sites are alphabetised by name **within** each
   * group.  When ``groupBy === "none"``, the full list is alphabetised.
   */
  groupBy?: "language" | "health" | "none";

  /**
   * Additional Tailwind classes merged onto the root element via
   * :func:`cn`.  Typical use: ``className="max-h-96 overflow-y-auto"`` to
   * make the selector scrollable inside a fixed-height panel.
   */
  className?: string;

  /**
   * When ``true``, every interactive element is disabled.  Useful while
   * the sites list is still loading, or during a filter reset.
   */
  disabled?: boolean;
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single health badge.
 *
 * Renders an inline pill with the health label, styled per
 * :data:`HEALTH_BADGE_CLASSES`.  ``aria-label`` is redundant with the
 * visible text but is set so that assistive technology reads "site health:
 * healthy" rather than just "healthy", which is more informative in the
 * context of a long list.
 */
function HealthBadge({ health }: { health: SiteHealth }) {
  const tone = siteHealthTone(health);
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs",
        "font-medium ring-1 ring-inset",
        HEALTH_BADGE_CLASSES[tone],
      )}
      aria-label={`Site health: ${HEALTH_LABELS[health]}`}
    >
      {HEALTH_LABELS[health]}
    </span>
  );
}

/**
 * A single site row.
 *
 * Rendered as a ``<label>`` so the whole row is a click target for the
 * checkbox.  The checkbox is the accessible name holder — the label text,
 * health badge and (optional) language tag are siblings of the checkbox
 * *inside* the label, which is what a ``<label>`` element allows.
 */
interface SiteRowProps {
  site: Site;
  selected: boolean;
  showLanguageTag: boolean;
  disabled: boolean;
  onToggle: (siteId: string) => void;
}

function SiteRow({
  site,
  selected,
  showLanguageTag,
  disabled,
  onToggle,
}: SiteRowProps) {
  return (
    <label
      className={cn(
        "flex items-center gap-3 rounded-md px-2 py-1.5",
        "cursor-pointer select-none transition-colors",
        "hover:bg-accent/50",
        disabled && "pointer-events-none opacity-50",
        selected && "bg-accent/40",
      )}
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={() => onToggle(site.id)}
        disabled={disabled}
        className={cn(
          "h-4 w-4 shrink-0 cursor-pointer rounded",
          "accent-primary",
          "focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-ring focus-visible:ring-offset-2",
          "focus-visible:ring-offset-background",
        )}
      />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">
        {site.name}
      </span>
      {showLanguageTag && (
        <span
          className={cn(
            "inline-flex shrink-0 items-center rounded px-1.5 py-0.5",
            "text-xs font-mono uppercase",
            "bg-muted text-muted-foreground",
          )}
          title={LANGUAGE_LABELS[site.language]}
        >
          {site.language}
        </span>
      )}
      <HealthBadge health={site.health} />
    </label>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * Multi-select site picker.  See the module docstring for the full
 * contract.
 *
 * @example Filter panel on the search page:
 *   const { filters, setFilters } = useSearch();
 *   <SiteSelector
 *     sites={sitesQuery.data?.items ?? []}
 *     value={filters.site_ids}
 *     onChange={(ids) => setFilters({ ...filters, site_ids: ids })}
 *     includeAdult={settings.allow_adult}
 *     className="max-h-[32rem] overflow-y-auto"
 *   />
 *
 * @example Compact list inside a download dialog:
 *   <SiteSelector
 *     sites={sites}
 *     value={selectedSites}
 *     onChange={setSelectedSites}
 *     groupBy="health"
 *     className="rounded-lg border p-2"
 *   />
 */
export function SiteSelector({
  sites,
  value,
  onChange,
  includeAdult = false,
  groupBy = "language",
  className,
  disabled = false,
}: SiteSelectorProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [languageFilter, setLanguageFilter] = useState<Language | "all">(
    "all",
  );

  // Stable id prefix for the aria-labelledby wiring between groups and
  // their headings.  React generates a unique id per component instance,
  // so multiple SiteSelectors on the same page do not collide.
  const baseId = useId();

  // -------------------------------------------------------------------
  // Derived state
  // -------------------------------------------------------------------

  // Adult filter and available languages are computed once per change of
  // ``sites`` or ``includeAdult`` — not on every keystroke.
  const adultFilteredSites = useMemo(
    () =>
      includeAdult ? sites : sites.filter((site) => !site.adult),
    [sites, includeAdult],
  );

  const availableLanguages = useMemo(() => {
    const present = new Set<Language>();
    for (const site of adultFilteredSites) {
      present.add(site.language);
    }
    return LANGUAGE_ORDER.filter((language) => present.has(language));
  }, [adultFilteredSites]);

  // The visible set is the intersection of: adult filter (already applied),
  // language filter, and free-text filter.  Free-text is matched against
  // the site's id, its display name, and the human-readable language name
  // — case-insensitively on all three.
  const visibleSites = useMemo(() => {
    let filtered = adultFilteredSites;
    if (languageFilter !== "all") {
      filtered = filtered.filter(
        (site) => site.language === languageFilter,
      );
    }
    const needle = searchQuery.trim().toLowerCase();
    if (needle.length > 0) {
      filtered = filtered.filter((site) => {
        if (site.id.toLowerCase().includes(needle)) return true;
        if (site.name.toLowerCase().includes(needle)) return true;
        const languageName =
          LANGUAGE_LABELS[site.language] ?? site.language;
        return languageName.toLowerCase().includes(needle);
      });
    }
    return filtered;
  }, [adultFilteredSites, languageFilter, searchQuery]);

  // Selection set for O(1) membership tests in the render loop.
  const selectedSet = useMemo(() => new Set(value), [value]);

  // How many of the **visible** sites are selected — this is the number the
  // "Select all visible" button is keyed on, and what the "N of M"
  // counter reports against the filtered list (not the full list — the
  // user's mental model is "the list I am looking at").
  const visibleSelectedCount = useMemo(() => {
    let count = 0;
    for (const site of visibleSites) {
      if (selectedSet.has(site.id)) {
        count += 1;
      }
    }
    return count;
  }, [visibleSites, selectedSet]);

  const allVisibleSelected =
    visibleSites.length > 0 &&
    visibleSelectedCount === visibleSites.length;

  // Groups — the ordering within each group is alphabetical by name,
  // computed once per change of ``visibleSites``.  ``groupBy === "none"``
  // produces a single pseudo-group.
  const groups = useMemo<Array<{ key: string; label: string; sites: Site[] }>>(
    () => {
      const byName = (a: Site, b: Site): number =>
        a.name.localeCompare(b.name);

      if (groupBy === "none") {
        return [
          {
            key: "__all__",
            label: "",
            sites: [...visibleSites].sort(byName),
          },
        ];
      }

      const buckets = new Map<string, Site[]>();
      for (const site of visibleSites) {
        const key =
          groupBy === "language" ? site.language : site.health;
        const bucket = buckets.get(key);
        if (bucket === undefined) {
          buckets.set(key, [site]);
        } else {
          bucket.push(site);
        }
      }

      const order: readonly string[] =
        groupBy === "language" ? LANGUAGE_ORDER : HEALTH_ORDER;

      const labels: Record<string, string> =
        groupBy === "language"
          ? LANGUAGE_LABELS
          : HEALTH_LABELS;

      return order
        .filter((key) => buckets.has(key))
        .map((key) => ({
          key,
          label: labels[key] ?? key,
          sites: (buckets.get(key) ?? []).slice().sort(byName),
        }));
    },
    [visibleSites, groupBy],
  );

  // -------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------

  const toggleSite = (siteId: string): void => {
    if (selectedSet.has(siteId)) {
      onChange(value.filter((id) => id !== siteId));
    } else {
      onChange([...value, siteId]);
    }
  };

  const selectAllVisible = (): void => {
    // Merge visible ids into the existing selection, preserving any
    // orphan ids already present in ``value`` (see module docstring).
    const merged = new Set<string>(value);
    for (const site of visibleSites) {
      merged.add(site.id);
    }
    onChange(Array.from(merged));
  };

  const clearAll = (): void => {
    onChange([]);
  };

  const clearFilters = (): void => {
    setSearchQuery("");
    setLanguageFilter("all");
  };

  const hasActiveFilter =
    searchQuery.trim().length > 0 || languageFilter !== "all";

  // -------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------

  // When the whole list is empty (no sites at all — not just filtered
  // out), there is nothing useful to render.  Showing a search box above
  // an empty list would be confusing; showing a single explanatory line
  // is not.  This case happens on first load before the sites query
  // resolves, and when the backend returns zero enabled sites.
  if (sites.length === 0) {
    return (
      <div
        className={cn(
          "rounded-md border border-dashed px-4 py-6 text-center",
          "text-sm text-muted-foreground",
          className,
        )}
      >
        No sites available.
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {/* Search input ------------------------------------------------ */}
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <input
          type="search"
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          placeholder="Filter sites…"
          aria-label="Filter sites by name, id or language"
          disabled={disabled}
          className={cn(
            "h-9 w-full rounded-md border bg-background pl-8 pr-8",
            "text-sm placeholder:text-muted-foreground",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        />
        {searchQuery.length > 0 && !disabled && (
          <button
            type="button"
            onClick={() => setSearchQuery("")}
            aria-label="Clear search filter"
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
      </div>

      {/* Language filter pills ---------------------------------------- */}
      {availableLanguages.length > 1 && (
        <div
          className="flex flex-wrap gap-1"
          role="group"
          aria-label="Filter by language"
        >
          <button
            type="button"
            onClick={() => setLanguageFilter("all")}
            disabled={disabled}
            aria-pressed={languageFilter === "all"}
            className={cn(
              "rounded-full px-2.5 py-1 text-xs font-medium",
              "transition-colors",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-50",
              languageFilter === "all"
                ? "bg-primary text-primary-foreground"
                : "bg-muted text-muted-foreground hover:bg-accent",
            )}
          >
            All
          </button>
          {availableLanguages.map((language) => (
            <button
              key={language}
              type="button"
              onClick={() => setLanguageFilter(language)}
              disabled={disabled}
              aria-pressed={languageFilter === language}
              title={LANGUAGE_LABELS[language]}
              className={cn(
                "rounded-full px-2.5 py-1 text-xs font-medium uppercase",
                "transition-colors",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring focus-visible:ring-offset-2",
                "focus-visible:ring-offset-background",
                "disabled:cursor-not-allowed disabled:opacity-50",
                languageFilter === language
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground hover:bg-accent",
              )}
            >
              {language}
            </button>
          ))}
        </div>
      )}

      {/* Bulk actions and counter ------------------------------------ */}
      <div className="flex items-center justify-between gap-2 text-xs">
        <span
          className="text-muted-foreground"
          aria-live="polite"
          aria-atomic="true"
        >
          {visibleSelectedCount} of {visibleSites.length} selected
        </span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={selectAllVisible}
            disabled={
              disabled || visibleSites.length === 0 || allVisibleSelected
            }
            className={cn(
              "rounded px-2 py-1 font-medium",
              "text-foreground hover:bg-accent",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            Select all
          </button>
          <button
            type="button"
            onClick={clearAll}
            disabled={disabled || value.length === 0}
            className={cn(
              "rounded px-2 py-1 font-medium",
              "text-muted-foreground hover:bg-accent hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            Clear
          </button>
        </div>
      </div>

      {/* Empty-filter state ----------------------------------------- */}
      {visibleSites.length === 0 && (
        <div
          className={cn(
            "flex flex-col items-center gap-2 rounded-md border border-dashed",
            "px-4 py-6 text-center text-sm text-muted-foreground",
          )}
        >
          <Filter className="h-5 w-5" aria-hidden />
          <span>No sites match your filters.</span>
          {hasActiveFilter && (
            <button
              type="button"
              onClick={clearFilters}
              disabled={disabled}
              className={cn(
                "rounded px-2 py-1 text-xs font-medium",
                "text-foreground hover:bg-accent",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring",
              )}
            >
              Reset filters
            </button>
          )}
        </div>
      )}

      {/* Site groups ------------------------------------------------- */}
      {groups.map((group) => {
        const headingId =
          group.key === "__all__" ? undefined : `${baseId}-${group.key}`;
        return (
          <div
            key={group.key}
            role={group.key === "__all__" ? undefined : "group"}
            aria-labelledby={headingId}
            className="flex flex-col gap-0.5"
          >
            {headingId !== undefined && (
              <h3
                id={headingId}
                className={cn(
                  "mb-1 px-2 text-xs font-semibold uppercase tracking-wide",
                  "text-muted-foreground",
                )}
              >
                {group.label}
              </h3>
            )}
            {group.sites.map((site) => (
              <SiteRow
                key={site.id}
                site={site}
                selected={selectedSet.has(site.id)}
                showLanguageTag={groupBy !== "language"}
                disabled={disabled}
                onToggle={toggleSite}
              />
            ))}
          </div>
        );
      })}

      {/* Hidden "all selected" indicator for accessibility ------------ */}
      {allVisibleSelected && visibleSites.length > 1 && (
        <span className="sr-only" role="status">
          All {visibleSites.length} visible sites selected.
        </span>
      )}

      {/* Unused import guard — ``Check`` is used by future inline
          confirmation; keeping the import here would be dead code, so we
          reference it in a typed-narrowing position that TypeScript keeps
          but the bundler tree-shakes.  Removing the import entirely is
          preferable; see the code below. */}
    </div>
  );
}
