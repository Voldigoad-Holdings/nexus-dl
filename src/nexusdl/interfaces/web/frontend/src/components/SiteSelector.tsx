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
 * directly.  Every toggle invokes ``onChange`` with a fresh array.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  This component is
 * therefore built exclusively with native HTML form controls, Tailwind
 * utility classes, the project's ``cn`` helper, ``lucide-react`` icons,
 * and the domain types from ``types/site.ts`` / ``types/manga.ts``.  When
 * those primitives land, the visual style can be upgraded in a single
 * self-contained edit — the public API does not change.
 *
 * Orphan ids
 * ----------
 * ``value`` may contain ids not present in ``sites`` (e.g. a site disabled
 * after selection).  Those ids are preserved by every toggle, so the
 * caller controls whether to prune them.
 *
 * Adult filtering
 * ---------------
 * Caller-controlled via ``includeAdult`` (default ``false``).  Selected
 * ids pointing at adult sites are preserved when the filter is toggled.
 *
 * Accessibility
 * -------------
 * * Each row is a ``<label>`` wrapping its ``<input type="checkbox">`` —
 *   the whole row is a click target, and the checkbox takes its accessible
 *   name from the label text.
 * * Groups are ``role="group"`` with an ``<h3>`` heading linked via
 *   ``aria-labelledby``.
 * * The "N of M selected" counter is ``aria-live="polite"``.
 * * The search input has an explicit ``aria-label``.
 */
import { Filter, Search, X } from "lucide-react";
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

const HEALTH_LABELS: Record<SiteHealth, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  down: "Down",
  unknown: "Unknown",
};

const HEALTH_BADGE_CLASSES: Record<SiteHealthTone, string> = {
  ok: "bg-emerald-500/10 text-emerald-700 ring-emerald-600/20 dark:text-emerald-400",
  warn: "bg-amber-500/10 text-amber-700 ring-amber-600/20 dark:text-amber-400",
  bad: "bg-red-500/10 text-red-700 ring-red-600/20 dark:text-red-400",
  neutral: "bg-muted text-muted-foreground ring-border",
};

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

const LANGUAGE_ORDER: readonly Language[] = LANGUAGES;
const HEALTH_ORDER: readonly SiteHealth[] = SITE_HEALTHS;

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

export interface SiteSelectorProps {
  sites: ReadonlyArray<Site>;
  value: ReadonlyArray<string>;
  onChange: (ids: string[]) => void;
  includeAdult?: boolean;
  groupBy?: "language" | "health" | "none";
  className?: string;
  disabled?: boolean;
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

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
          "h-4 w-4 shrink-0 cursor-pointer rounded accent-primary",
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
            "bg-muted font-mono text-xs uppercase text-muted-foreground",
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
  const [languageFilter, setLanguageFilter] = useState<Language | "all">("all");
  const baseId = useId();

  const adultFilteredSites = useMemo(
    () => (includeAdult ? sites : sites.filter((site) => !site.adult)),
    [sites, includeAdult],
  );

  const availableLanguages = useMemo(() => {
    const present = new Set<Language>();
    for (const site of adultFilteredSites) present.add(site.language);
    return LANGUAGE_ORDER.filter((language) => present.has(language));
  }, [adultFilteredSites]);

  const visibleSites = useMemo(() => {
    let filtered = adultFilteredSites;
    if (languageFilter !== "all") {
      filtered = filtered.filter((site) => site.language === languageFilter);
    }
    const needle = searchQuery.trim().toLowerCase();
    if (needle.length > 0) {
      filtered = filtered.filter((site) => {
        if (site.id.toLowerCase().includes(needle)) return true;
        if (site.name.toLowerCase().includes(needle)) return true;
        const languageName = LANGUAGE_LABELS[site.language] ?? site.language;
        return languageName.toLowerCase().includes(needle);
      });
    }
    return filtered;
  }, [adultFilteredSites, languageFilter, searchQuery]);

  const selectedSet = useMemo(() => new Set(value), [value]);

  const visibleSelectedCount = useMemo(() => {
    let count = 0;
    for (const site of visibleSites) if (selectedSet.has(site.id)) count += 1;
    return count;
  }, [visibleSites, selectedSet]);

  const allVisibleSelected =
    visibleSites.length > 0 && visibleSelectedCount === visibleSites.length;

  const groups = useMemo<Array<{ key: string; label: string; sites: Site[] }>>(
    () => {
      const byName = (a: Site, b: Site): number => a.name.localeCompare(b.name);
      if (groupBy === "none") {
        return [
          { key: "__all__", label: "", sites: [...visibleSites].sort(byName) },
        ];
      }
      const buckets = new Map<string, Site[]>();
      for (const site of visibleSites) {
        const key = groupBy === "language" ? site.language : site.health;
        const bucket = buckets.get(key);
        if (bucket === undefined) buckets.set(key, [site]);
        else bucket.push(site);
      }
      const order: readonly string[] =
        groupBy === "language" ? LANGUAGE_ORDER : HEALTH_ORDER;
      const labels: Record<string, string> =
        groupBy === "language" ? LANGUAGE_LABELS : HEALTH_LABELS;
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

  const toggleSite = (siteId: string): void => {
    if (selectedSet.has(siteId)) onChange(value.filter((id) => id !== siteId));
    else onChange([...value, siteId]);
  };

  const selectAllVisible = (): void => {
    const merged = new Set<string>(value);
    for (const site of visibleSites) merged.add(site.id);
    onChange(Array.from(merged));
  };

  const clearAll = (): void => onChange([]);

  const clearFilters = (): void => {
    setSearchQuery("");
    setLanguageFilter("all");
  };

  const hasActiveFilter =
    searchQuery.trim().length > 0 || languageFilter !== "all";

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
              "rounded-full px-2.5 py-1 text-xs font-medium transition-colors",
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
                "rounded-full px-2.5 py-1 text-xs font-medium uppercase transition-colors",
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
            disabled={disabled || visibleSites.length === 0 || allVisibleSelected}
            className={cn(
              "rounded px-2 py-1 font-medium text-foreground hover:bg-accent",
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
    </div>
  );
}
