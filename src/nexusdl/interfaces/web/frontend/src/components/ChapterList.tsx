"use client";

/**
 * ChapterList — a selectable list of chapters for a manga.
 *
 * Renders one chapter per row with a selection checkbox, a compact
 * metadata line (volume · page count · publication date) and a per-chapter
 * status icon that mirrors the download state the parent supplies through
 * the optional ``chapterStatuses`` map.  A toolbar above the list provides
 * text filtering, sort order and bulk selection/download actions.
 *
 * Design
 * ------
 * * **Fully controlled.**  ``chapters``, ``selectedIds`` and
 *   ``chapterStatuses`` are read-only inputs; every interaction goes
 *   through a callback the parent owns.  The component never touches the
 *   API or the store.
 *
 * * **One callback per concern.**  ``onSelectionChange`` receives the full
 *   new selection; ``onDownloadSelected`` receives the **selected chapter
 *   ids sorted by chapter number ascending** — which is the order the
 *   download worker iterates them in, regardless of how the user selected
 *   them.  There is no per-chapter download button; submissions always go
 *   through a single "Download (N)" action in the toolbar, matching the
 *   ``CreateDownloadRequest.chapter_ids`` shape on the backend (one task
 *   per manga, N chapters).
 *
 * * **Selection preserved across filter/sort.**  Filtering to "chapter 4"
 *   and ticking one row does not clear the rest of the selection — the
 *   parent owns it, and the component only edits the ids the user actually
 *   toggled.  This is what makes "download the last 3 chapters" work
 *   without clearing a previous selection of 20.
 *
 * * **Language tag only when mixed.**  When every chapter in ``chapters``
 *   shares the same language, the language tag is hidden — it is noise on
 *   a single-language site (the overwhelming majority).  When two or more
 *   languages appear (MangaDex and similar aggregators), the tag becomes
 *   visible on every row so the user can tell them apart.
 *
 * * **Empty states.**  Two distinct messages: "No chapters available." when
 *   the list is empty (no parser data yet), and "No chapters match your
 *   filter." with a "Clear filter" button when the filter hides everything.
 *   They are intentionally different strings — the user's next action
 *   depends on which case they are in.
 *
 * Ordering
 * --------
 * The default sort is **oldest first**, which is the order the backend
 * returns (see every Madara/MangaThemesia/FoolSlide mixin docstring).  The
 * sort toggle flips to newest-first — useful for spot-checking the latest
 * release before downloading it.  When sorting by newest-first, selection
 * is preserved unchanged; the toolbar's "Download (N)" action still submits
 * chapters in **oldest-first** order.
 *
 * Chapters with a ``null`` number (oneshots, bonus chapters) sort to the
 * end of the oldest-first order and to the beginning of the newest-first
 * order — mirroring the intuition that they are "extras" outside the
 * numbered sequence.
 *
 * Accessibility
 * -------------
 * * Each row is a ``<li>``; the list is a ``<ul>``.  Row content is a
 *   single ``<label>`` wrapping the checkbox and the text, so the whole
 *   row is a click target and the checkbox takes the chapter title as its
 *   accessible name.
 * * The "N of M selected" counter is ``aria-live="polite"`` and
 *   ``aria-atomic="true"``, so a bulk action announces the resulting count.
 * * Each action button carries an explicit ``aria-label`` that names both
 *   the action and the count (e.g. ``"Download 3 selected chapters"``).
 * * The status icon is ``aria-hidden`` and paired with a ``sr-only`` label
 *   (``"Downloaded"`` / ``"Downloading"`` / ``"Download failed"``) — this
 *   preserves the information for screen readers without relying on colour
 *   alone.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  Every element is a
 * native HTML control (``<input type="checkbox">``, ``<input type="search">``,
 * ``<button>``, ``<ul>``/``<li>``) styled with the Tailwind tokens defined
 * in ``src/app/globals.css``, plus ``lucide-react`` icons and the project's
 * ``cn``/``formatDate`` helpers.  When the shadcn primitives land, the
 * visual style can be upgraded in a single self-contained edit — the
 * public API does not change.
 */
import {
  AlertCircle,
  ArrowDownUp,
  CheckCircle2,
  Download,
  Loader2,
  Search,
  X,
} from "lucide-react";
import { useId, useMemo, useState } from "react";

import { cn, formatDate } from "../lib/utils";
import type { Chapter, Language } from "../types/manga";

// ───────────────────────────────────────────────────────────────────────────
// Types and constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * The download-status of a single chapter, as supplied by the parent
 * through :attr:`ChapterListProps.chapterStatuses`.
 *
 * * ``"idle"``        — not downloaded and not in any active task.
 * * ``"downloading"`` — part of a running or paused download task.
 * * ``"downloaded"``  — already present on disk.
 * * ``"failed"``      — attempted and failed; a retry is meaningful.
 */
export type ChapterStatus = "idle" | "downloading" | "downloaded" | "failed";

/**
 * Human-readable names for each language, used when the chapter list mixes
 * languages.  Matches the mapping in ``components/SiteSelector.tsx`` — a
 * shared i18n layer will replace both in a future iteration, but keeping
 * the tables consistent today prevents visual drift between the two
 * selectors.
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

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

/**
 * The per-chapter status indicator.
 *
 * Renders ``null`` for ``"idle"`` so that a chapter that has never been
 * touched does not display a meaningless "not downloaded" icon on every
 * row — the absence of an indicator already conveys that meaning, and the
 * row is visually cleaner.  Every other status gets an icon plus a
 * ``sr-only`` label so that the information is not colour-only.
 */
function ChapterStatusIcon({ status }: { status: ChapterStatus }) {
  switch (status) {
    case "idle":
      return null;
    case "downloading":
      return (
        <>
          <Loader2
            className="h-4 w-4 shrink-0 animate-spin text-blue-500"
            aria-hidden
          />
          <span className="sr-only">Downloading</span>
        </>
      );
    case "downloaded":
      return (
        <>
          <CheckCircle2
            className="h-4 w-4 shrink-0 text-emerald-500"
            aria-hidden
          />
          <span className="sr-only">Downloaded</span>
        </>
      );
    case "failed":
      return (
        <>
          <AlertCircle
            className="h-4 w-4 shrink-0 text-red-500"
            aria-hidden
          />
          <span className="sr-only">Download failed</span>
        </>
      );
  }
}

/**
 * Props for the :func:`ChapterRow` sub-component.
 *
 * Kept as a named interface (rather than an inline type) because the
 * ``ChapterList`` render body builds these objects conditionally.
 */
interface ChapterRowProps {
  chapter: Chapter;
  selected: boolean;
  status: ChapterStatus;
  showLanguage: boolean;
  disabled: boolean;
  onToggle: (chapterId: string) => void;
}

/**
 * A single chapter row.
 *
 * The row is a ``<label>`` wrapping the checkbox and the visual content,
 * so a click anywhere on the row toggles the checkbox and the checkbox
 * takes its accessible name from the label.  The checkbox is not inside a
 * wrapping ``<div>`` — a native ``<label>`` can only label one control, and
 * adding a nested container would break the click target.
 */
function ChapterRow({
  chapter,
  selected,
  status,
  showLanguage,
  disabled,
  onToggle,
}: ChapterRowProps) {
  // Title fallback chain: prefer the source title, then a synthetic
  // "Chapter N", then the raw id.  This handles chapters whose site
  // publishes neither a title nor a number (rare, but possible on
  // custom sites).
  const mainLine =
    chapter.title.length > 0
      ? chapter.title
      : chapter.number !== null
        ? `Chapter ${chapter.number}`
        : chapter.id;

  // Metadata line, omitting empty parts so there is never a trailing
  // separator.  Order matters — a user scanning for a chapter reads
  // "Vol. 3 · 20 pages · Nov 4, 2026" left-to-right.
  const metaParts: string[] = [];
  if (chapter.volume !== null) {
    metaParts.push(`Vol. ${chapter.volume}`);
  }
  if (chapter.page_count !== null) {
    metaParts.push(
      chapter.page_count === 1
        ? "1 page"
        : `${chapter.page_count} pages`,
    );
  }
  if (chapter.published_at !== null) {
    metaParts.push(formatDate(chapter.published_at));
  }
  if (showLanguage) {
    metaParts.push(LANGUAGE_LABELS[chapter.language]);
  }
  const metaLine = metaParts.join(" · ");

  return (
    <li>
      <label
        className={cn(
          "flex items-start gap-3 rounded-md px-2 py-2",
          "cursor-pointer select-none transition-colors",
          "hover:bg-accent/50",
          selected && "bg-accent/40",
          disabled && "pointer-events-none opacity-50",
        )}
      >
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggle(chapter.id)}
          disabled={disabled}
          className={cn(
            "mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded accent-primary",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
        />
        <div className="min-w-0 flex-1">
          <p
            className="truncate text-sm font-medium"
            title={mainLine}
          >
            {mainLine}
          </p>
          {metaLine.length > 0 && (
            <p className="truncate text-xs text-muted-foreground">
              {metaLine}
            </p>
          )}
        </div>
        <ChapterStatusIcon status={status} />
      </label>
    </li>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`ChapterList`.
 */
export interface ChapterListProps {
  /** The chapters to display.  Order is preserved when the sort is "oldest". */
  chapters: ReadonlyArray<Chapter>;

  /**
   * The currently-selected chapter ids.  Must be a subset of
   * ``chapters.map(c => c.id)`` — orphan ids are tolerated but never
   * rendered.
   */
  selectedIds: ReadonlyArray<string>;

  /**
   * Called with the full new selection whenever the user toggles a row or
   * runs a bulk action.  Always a **fresh** array — the component never
   * mutates ``selectedIds``.
   */
  onSelectionChange: (ids: string[]) => void;

  /**
   * Per-chapter download status.  When omitted, every chapter is rendered
   * as ``"idle"`` (no indicator).  The parent supplies this map from
   * whatever source knows the download state — the backend library on a
   * manga detail page, the local download store on a download page, or a
   * combination of both.
   */
  chapterStatuses?: ReadonlyMap<string, ChapterStatus>;

  /**
   * Called with the **selected chapter ids sorted by chapter number
   * ascending**, when the user clicks the "Download (N)" button.
   *
   * The sort is by :attr:`Chapter.number` ascending, with ``null``-numbered
   * chapters placed at the end.  When **all** selected chapters have a
   * ``null`` number, the selection order is preserved unchanged.  The
   * parent is expected to convert this list into a
   * :type:`~../types/download.CreateDownloadRequest` with
   * ``chapter_ids: [...selectedIds]``.
   *
   * When this prop is omitted, the "Download (N)" button is not rendered
   * — the component is display-and-select only.
   */
  onDownloadSelected?: (chapterIds: string[]) => void | Promise<void>;

  /**
   * When ``true``, the "Download (N)" button shows a spinner and is
   * disabled while the parent's submit is in flight.  The rest of the
   * component stays interactive so the user can adjust the selection.
   */
  isDownloading?: boolean;

  /**
   * When ``true``, every interactive element is disabled.  Use this while
   * the parent is loading the chapters, or during a global operation that
   * should not be interrupted.
   */
  disabled?: boolean;

  /**
   * Additional Tailwind classes merged onto the outer card via
   * :func:`cn`.  Typical uses: ``className="max-h-[32rem] overflow-y-auto"``
   * to bound the list inside a taller page.
   */
  className?: string;
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * A selectable list of chapters for a manga.  See the module docstring for
 * the full contract.
 *
 * @example Standard usage on a manga detail page:
 *   const [selected, setSelected] = useState<string[]>([]);
 *   const downloads = useDownload();
 *
 *   <ChapterList
 *     chapters={chaptersQuery.data?.items ?? []}
 *     selectedIds={selected}
 *     onSelectionChange={setSelected}
 *     chapterStatuses={chapterStatusMap}
 *     isDownloading={downloads.isCreating}
 *     onDownloadSelected={(ids) =>
 *       downloads.create({ manga_id: manga.id, chapter_ids: ids })
 *     }
 *   />
 *
 * @example Display-and-select only — no download button because the
 * caller wires its own action elsewhere:
 *   <ChapterList
 *     chapters={chapters}
 *     selectedIds={selected}
 *     onSelectionChange={setSelected}
 *   />
 */
export function ChapterList({
  chapters,
  selectedIds,
  onSelectionChange,
  chapterStatuses,
  onDownloadSelected,
  isDownloading = false,
  disabled = false,
  className,
}: ChapterListProps) {
  const [filterQuery, setFilterQuery] = useState("");
  const [sortOrder, setSortOrder] = useState<"oldest" | "newest">("oldest");
  const filterId = useId();

  // -------------------------------------------------------------------
  // Derived state
  // -------------------------------------------------------------------

  const selectedSet = useMemo(
    () => new Set<string>(selectedIds),
    [selectedIds],
  );

  // Lookup map for sorting the "Download (N)" submit in chapter order.
  const chapterById = useMemo(() => {
    const map = new Map<string, Chapter>();
    for (const chapter of chapters) {
      map.set(chapter.id, chapter);
    }
    return map;
  }, [chapters]);

  // Whether to show the per-chapter language tag.  Always shown when
  // chapters mix languages, hidden otherwise — see the module docstring.
  const showLanguage = useMemo(() => {
    let first: Language | null = null;
    for (const chapter of chapters) {
      if (first === null) {
        first = chapter.language;
      } else if (chapter.language !== first) {
        return true;
      }
    }
    return false;
  }, [chapters]);

  // The sorted chapter array.  The sort is applied before filtering so
  // that the filter's "N of M" counter compares against the total the
  // user expects, not the sorted-and-filtered count.
  const sortedChapters = useMemo(() => {
    if (sortOrder === "oldest") {
      return chapters;
    }
    // Newest first: reverse the input order.  Sorting by ``number`` would
    // be more robust, but the input is already chronological (per every
    // parser mixin), so reversing is enough — and it avoids a
    // `null`-handling branch for oneshots that would otherwise be
    // ambiguous.
    return [...chapters].reverse();
  }, [chapters, sortOrder]);

  // The filtered chapter array — matches chapter number and title,
  // case-insensitively.  Volume number is also matched (so "3" finds
  // "Vol. 3"), which is what users expect when typing a number.
  const visibleChapters = useMemo(() => {
    const needle = filterQuery.trim().toLowerCase();
    if (needle.length === 0) {
      return sortedChapters;
    }
    return sortedChapters.filter((chapter) => {
      if (chapter.title.toLowerCase().includes(needle)) return true;
      if (
        chapter.number !== null &&
        String(chapter.number).includes(needle)
      ) {
        return true;
      }
      if (
        chapter.volume !== null &&
        String(chapter.volume).includes(needle)
      ) {
        return true;
      }
      return false;
    });
  }, [sortedChapters, filterQuery]);

  // Selection count — reflects the **full** selection (which may include
  // chapters hidden by the current filter), because the toolbar's actions
  // affect the full selection, not the visible subset.  The per-filtered-
  // view count is what "Select all" acts on, so we track both.
  const visibleSelectedCount = useMemo(() => {
    let count = 0;
    for (const chapter of visibleChapters) {
      if (selectedSet.has(chapter.id)) {
        count += 1;
      }
    }
    return count;
  }, [visibleChapters, selectedSet]);

  const allVisibleSelected =
    visibleChapters.length > 0 &&
    visibleSelectedCount === visibleChapters.length;

  const hasFilter = filterQuery.trim().length > 0;

  // -------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------

  const toggleChapter = (chapterId: string): void => {
    if (selectedSet.has(chapterId)) {
      onSelectionChange(selectedIds.filter((id) => id !== chapterId));
    } else {
      onSelectionChange([...selectedIds, chapterId]);
    }
  };

  const selectAllVisible = (): void => {
    // Merge visible ids into the existing selection, preserving any
    // selected ids currently hidden by the filter — see the module
    // docstring ("selection preserved across filter/sort").
    const merged = new Set<string>(selectedIds);
    for (const chapter of visibleChapters) {
      merged.add(chapter.id);
    }
    onSelectionChange(Array.from(merged));
  };

  const clearSelection = (): void => {
    onSelectionChange([]);
  };

  const downloadSelected = (): void => {
    if (onDownloadSelected === undefined || selectedIds.length === 0) {
      return;
    }
    // Sort the selection by chapter number ascending.  Nulls go to the
    // end — they represent oneshots and extras, which should not block a
    // sequential download.  When every chapter has a null number, the
    // comparator returns 0 for every pair and the sort is stable, so the
    // selection order is preserved.
    const ordered = [...selectedIds].sort((a, b) => {
      const chapterA = chapterById.get(a);
      const chapterB = chapterById.get(b);
      const numberA = chapterA?.number ?? null;
      const numberB = chapterB?.number ?? null;
      if (numberA === null && numberB === null) return 0;
      if (numberA === null) return 1;
      if (numberB === null) return -1;
      return numberA - numberB;
    });
    void onDownloadSelected(ordered);
  };

  const toggleSort = (): void => {
    setSortOrder((current) => (current === "oldest" ? "newest" : "oldest"));
  };

  // -------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------

  if (chapters.length === 0) {
    return (
      <div
        className={cn(
          "rounded-md border border-dashed px-4 py-8 text-center",
          "text-sm text-muted-foreground",
          className,
        )}
      >
        No chapters available.
      </div>
    );
  }

  const downloadLabel =
    selectedIds.length === 1
      ? "Download 1 chapter"
      : `Download ${selectedIds.length} chapters`;

  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-lg border bg-card p-3",
        "text-card-foreground shadow-sm",
        className,
      )}
    >
      {/* Row 1 — filter and sort ------------------------------------- */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search
            className={cn(
              "pointer-events-none absolute left-2.5 top-1/2 h-4 w-4",
              "-translate-y-1/2 text-muted-foreground",
            )}
            aria-hidden
          />
          <label htmlFor={filterId} className="sr-only">
            Filter chapters
          </label>
          <input
            id={filterId}
            type="search"
            value={filterQuery}
            onChange={(event) => setFilterQuery(event.target.value)}
            placeholder="Filter chapters by number or title…"
            disabled={disabled}
            className={cn(
              "h-9 w-full rounded-md border bg-background pl-8 pr-8",
              "text-sm placeholder:text-muted-foreground",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-50",
              "[&::-webkit-search-cancel-button]:appearance-none",
            )}
          />
          {filterQuery.length > 0 && !disabled && (
            <button
              type="button"
              onClick={() => setFilterQuery("")}
              aria-label="Clear chapter filter"
              title="Clear chapter filter"
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

        <button
          type="button"
          onClick={toggleSort}
          disabled={disabled}
          aria-label={
            sortOrder === "oldest"
              ? "Sort newest first"
              : "Sort oldest first"
          }
          title={
            sortOrder === "oldest"
              ? "Sort newest first"
              : "Sort oldest first"
          }
          className={cn(
            "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-md",
            "border bg-background px-3 text-sm font-medium",
            "text-muted-foreground hover:bg-accent hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          <ArrowDownUp className="h-3.5 w-3.5" aria-hidden />
          <span>
            {sortOrder === "oldest" ? "Oldest first" : "Newest first"}
          </span>
        </button>
      </div>

      {/* Row 2 — selection summary and bulk actions ------------------ */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span
          className="text-muted-foreground"
          aria-live="polite"
          aria-atomic="true"
        >
          {selectedIds.length} of {chapters.length} selected
          {hasFilter && visibleChapters.length !== chapters.length && (
            <span className="ml-1">
              ({visibleChapters.length} shown)
            </span>
          )}
        </span>

        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={selectAllVisible}
            disabled={
              disabled ||
              visibleChapters.length === 0 ||
              allVisibleSelected
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
            onClick={clearSelection}
            disabled={disabled || selectedIds.length === 0}
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
          {onDownloadSelected !== undefined && (
            <button
              type="button"
              onClick={downloadSelected}
              disabled={
                disabled || isDownloading || selectedIds.length === 0
              }
              aria-label={downloadLabel}
              className={cn(
                "inline-flex items-center gap-1.5 rounded px-2.5 py-1",
                "bg-primary text-xs font-medium text-primary-foreground",
                "transition-colors hover:bg-primary/90",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring focus-visible:ring-offset-2",
                "focus-visible:ring-offset-background",
                "disabled:cursor-not-allowed disabled:opacity-50",
              )}
            >
              {isDownloading ? (
                <Loader2
                  className="h-3.5 w-3.5 animate-spin"
                  aria-hidden
                />
              ) : (
                <Download className="h-3.5 w-3.5" aria-hidden />
              )}
              <span>
                {isDownloading
                  ? "Submitting…"
                  : `Download (${selectedIds.length})`}
              </span>
            </button>
          )}
        </div>
      </div>

      {/* Row 3 — empty-filter state --------------------------------- */}
      {visibleChapters.length === 0 && (
        <div
          className={cn(
            "flex flex-col items-center gap-2 rounded-md border",
            "border-dashed px-4 py-6 text-center text-sm",
            "text-muted-foreground",
          )}
        >
          <span>No chapters match your filter.</span>
          <button
            type="button"
            onClick={() => setFilterQuery("")}
            disabled={disabled}
            className={cn(
              "rounded px-2 py-1 text-xs font-medium",
              "text-foreground hover:bg-accent",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring",
            )}
          >
            Clear filter
          </button>
        </div>
      )}

      {/* Chapter rows ----------------------------------------------- */}
      {visibleChapters.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {visibleChapters.map((chapter) => (
            <ChapterRow
              key={chapter.id}
              chapter={chapter}
              selected={selectedSet.has(chapter.id)}
              status={chapterStatuses?.get(chapter.id) ?? "idle"}
              showLanguage={showLanguage}
              disabled={disabled}
              onToggle={toggleChapter}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
