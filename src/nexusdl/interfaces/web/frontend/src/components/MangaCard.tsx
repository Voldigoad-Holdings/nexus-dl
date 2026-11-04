"use client";

/**
 * MangaCard — cover + title tile for a manga or search result.
 *
 * Used everywhere a manga is displayed as a card: search result grids,
 * library grids, "recently added" panels, "similar manga" rails.  The
 * component is deliberately a **single tile**, not a wrapper grid — grid
 * layout, column counts and spacing belong to the calling page.  This keeps
 * the card's dimensions predictable (a 2:3 cover plus two text lines) and
 * lets every caller decide how to arrange the tiles.
 *
 * Input shape
 * -----------
 * The card accepts **either** a :type:`~../types/manga.Manga` (from
 * ``GET /api/v1/manga/{id}``) **or** a
 * :type:`~../types/manga.SearchResult` (from ``POST /api/v1/search``).
 * Both types carry the same fields the card needs — cover, title, site
 * name, content rating, status, year — with one difference: ``Manga``
 * exposes its identifier as ``id``, ``SearchResult`` as ``manga_id``.  The
 * card normalises that difference internally so callers never convert.
 *
 * Visual design
 * -------------
 * * **Cover** — a 2:3 aspect-ratio box (the standard manga cover ratio),
 *   filled edge-to-edge, with a subtle zoom on hover (``scale-105`` over
 *   300 ms) that signals interactivity without being flashy.
 * * **Fallback** — when ``cover_url`` is ``null`` (parsers occasionally
 *   fail to extract one) a muted background with an ``ImageOff`` icon
 *   takes the cover's place.  The card's footprint is unchanged, so the
 *   grid stays aligned.
 * * **Adult badge** — an "18+" pill is overlaid on the top-right corner of
 *   the cover when ``content_rating`` is ``erotica`` or ``pornographic``.
 *   It uses :func:`isAdultRating` from ``types/manga.ts`` so the mapping
 *   from rating → badge is the single source of truth, shared with every
 *   other adult-content decision in the app.
 * * **Title** — truncated to two lines with ``line-clamp-2``; a native
 *   ``title`` tooltip reveals the full title on hover, so a long series
 *   name is never lost.
 * * **Meta line** — ``SiteName · Status · Year``, with empty parts
 *   omitted.  A ``"unknown"`` status is omitted (it is not informative —
 *   it just means the source site did not publish a status).  The whole
 *   line is truncated to a single line with ``truncate`` so it never
 *   wraps on a narrow grid.
 *
 * Accessibility
 * -------------
 * * The whole card is a single ``<Link>`` — one tab stop, one click target.
 * * The cover image is ``alt=""`` because the title below it names the
 *   same entity; screen readers skip the image and read the title once.
 * * The title is rendered in an ``<h3>`` so that a screen reader's heading
 *   navigation sees the card list as a set of headings, matching the
 *   visual hierarchy of a card grid.
 * * The ``<Link>`` has a focus ring via ``focus-visible:ring-*``, driven by
 *   the project's ``--ring`` token so it works in both light and dark
 *   themes.
 * * ``aria-label`` on the link is intentionally **not** set: the link's
 *   accessible name comes from its content (title + meta), which is more
 *   informative than a synthetic label and requires no duplication.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  The card is a
 * ``<Link>`` styled with Tailwind tokens, ``lucide-react`` icons and the
 * project's ``cn`` helper.  When the shadcn primitives land, the visual
 * style can be upgraded in a single self-contained edit — the public API
 * (``data``, ``className``) does not change.
 */
import { ImageOff } from "lucide-react";
import Image from "next/image";
import Link from "next/link";

import { cn } from "../lib/utils";
import {
  isAdultRating,
  type Manga,
  type MangaStatus,
  type SearchResult,
} from "../types/manga";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * Human-readable label for each status.
 *
 * ``"unknown"`` maps to an empty string so that the meta line can omit it
 * without a conditional at every call site.  The :data:`STATUS_LABELS`
 * lookup followed by ``|| null`` is the pattern used below.
 */
const STATUS_LABELS: Record<MangaStatus, string> = {
  ongoing: "Ongoing",
  completed: "Completed",
  hiatus: "Hiatus",
  cancelled: "Cancelled",
  unknown: "",
};

/**
 * Responsive sizes hint for ``next/image``.
 *
 * Matches the expected grid layouts:
 *
 * * ``< 640 px``  — 2 columns → 50 vw per card.
 * * ``< 1024 px`` — 3 columns → 33 vw per card.
 * * ``>= 1024 px`` — 4–6 columns → ~20 vw per card.
 *
 * Next.js uses this to pick the smallest raster variant that still looks
 * crisp on the device's pixel ratio, which halves the bandwidth of a
 * result grid on mobile without any visual difference.
 */
const COVER_SIZES =
  "(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 20vw";

// ───────────────────────────────────────────────────────────────────────────
// Type guard
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return ``true`` when *data* is a :type:`SearchResult`.
 *
 * ``SearchResult`` is the only one of the two input types that exposes a
 * ``manga_id`` field.  The guard is the single place the id normalisation
 * happens, so adding a third input type in the future touches only this
 * function.
 */
function isSearchResult(data: Manga | SearchResult): data is SearchResult {
  return "manga_id" in data;
}

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`MangaCard`.
 */
export interface MangaCardProps {
  /**
   * The manga or search result to display.  Both shapes are accepted —
   * see the module docstring.
   */
  data: Manga | SearchResult;

  /**
   * Additional Tailwind classes merged onto the card via :func:`cn`.
   *
   * Typically used by the grid to control width or spacing
   * (``className="w-40"``, ``className="col-span-2"``).  Because ``cn``
   * uses ``tailwind-merge``, any conflicting utility the caller passes
   * wins — including ``bg-*``, ``border-*`` and ``rounded-*``.
   */
  className?: string;
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single manga tile.  See the module docstring for the full contract.
 *
 * @example Search results grid:
 *   <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
 *     {search.results.map((result) => (
 *       <MangaCard key={result.manga_id} data={result} />
 *     ))}
 *   </div>
 *
 * @example Library grid:
 *   <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
 *     {library.items.map((entry) => (
 *       <MangaCard key={entry.manga.id} data={entry.manga} />
 *     ))}
 *   </div>
 */
export function MangaCard({ data, className }: MangaCardProps) {
  // Normalise the identifier — the only difference between the two input
  // shapes that matters to the card.
  const mangaId = isSearchResult(data) ? data.manga_id : data.id;
  const href = `/manga/${mangaId}`;

  const showAdultBadge = isAdultRating(data.content_rating);

  // Build the meta line, omitting empty parts so that a series with no
  // status and no year renders "MangaDex" rather than "MangaDex ·  · ".
  const statusLabel = STATUS_LABELS[data.status];
  const metaParts: string[] = [data.site_name];
  if (statusLabel.length > 0) {
    metaParts.push(statusLabel);
  }
  if (data.year !== null) {
    metaParts.push(String(data.year));
  }

  return (
    <Link
      href={href}
      className={cn(
        // Card frame — flex column so the cover sits above the text.
        "group flex flex-col gap-2 overflow-hidden rounded-lg",
        "border bg-card text-card-foreground shadow-sm",
        "transition-[box-shadow,border-color] duration-150",
        "hover:border-foreground/20 hover:shadow-md",
        // Focus ring — driven by the theme's ``--ring`` token, so the
        // visible colour changes with the theme without a second set of
        // classes.
        "focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background",
        className,
      )}
    >
      {/* Cover --------------------------------------------------- */}
      <div className="relative aspect-[2/3] w-full overflow-hidden bg-muted">
        {data.cover_url !== null ? (
          <Image
            src={data.cover_url}
            // The title below the image names the same entity — the
            // cover is decorative and an empty alt is correct, both for
            // screen-reader users (no duplication) and for SEO (crawlable
            // text via the adjacent <h3>).
            alt=""
            fill
            sizes={COVER_SIZES}
            className={cn(
              "object-cover transition-transform duration-300",
              "group-hover:scale-105",
            )}
          />
        ) : (
          <div
            className="flex h-full w-full items-center justify-center text-muted-foreground"
            aria-hidden
          >
            <ImageOff className="h-8 w-8" />
          </div>
        )}

        {showAdultBadge && (
          <span
            className={cn(
              "absolute right-1.5 top-1.5 inline-flex items-center",
              "rounded bg-red-600 px-1.5 py-0.5",
              "text-[10px] font-bold uppercase leading-none text-white",
              "shadow",
            )}
            title="Adult content"
          >
            18+
          </span>
        )}
      </div>

      {/* Text ---------------------------------------------------- */}
      <div className="flex flex-col gap-1 px-2 pb-2">
        <h3
          className="line-clamp-2 text-sm font-medium leading-snug"
          title={data.title}
        >
          {data.title}
        </h3>
        {metaParts.length > 0 && (
          <p className="truncate text-xs text-muted-foreground">
            {metaParts.join(" · ")}
          </p>
        )}
      </div>
    </Link>
  );
}
