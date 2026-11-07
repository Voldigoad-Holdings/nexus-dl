/**
 * Manga domain types — wire format for the NexusDL REST API and WebSocket
 * events.
 *
 * Every interface here mirrors a corresponding Pydantic schema in
 * ``src/nexusdl/interfaces/web/backend/schemas/{requests,responses}.py``
 * and the underlying domain model in ``core/models/``:
 *
 *  | TypeScript            | Pydantic model          | Core model           |
 *  |-----------------------|-------------------------|----------------------|
 *  | :type:`Language`      | ``Language``            | ``core.models.enums``|
 *  | :type:`ContentRating` | ``ContentRating``       | ``core.models.enums``|
 *  | :type:`MangaStatus`   | ``MangaStatus``         | ``core.models.enums``|
 *  | :type:`SiteHealth`    | ``SiteHealth``          | ``core.models.enums``|
 *  | :type:`Page`          | ``PageSchema``          | ``core.models.page`` |
 *  | :type:`Chapter`       | ``ChapterSchema``       | ``core.models.chapter`` |
 *  | :type:`Manga`         | ``MangaSchema``         | ``core.models.manga``|
 *  | :type:`Site`          | ``SiteSchema``          | ``core.models.site`` |
 *
 * Field names are ``snake_case`` throughout — the API payload uses the
 * backend's wire format verbatim, so a network tab or log line matches the
 * Pydantic model field-for-field.
 *
 * Enums are modelled as **string literal unions**, not TypeScript
 * ``enum``s, for the same reasons as in :mod:`./download`:
 *
 *  - Structural typing: any JSON value equal to a member is assignable
 *    without a cast.
 *  - Tree-shaking: the values exist only at the comparison sites.
 *  - Interop with the ``*_VALUES`` tuples below, which drive UI dropdowns
 *    and validation.
 *
 * Enum wire values are **lowercase**, matching the Python enums' ``.value``
 * (Pydantic serialises enums by value) and the ``language: en`` /
 * ``adult: false`` shape of ``data/sites.yaml``.
 *
 * Timestamps are ISO 8601 strings.  The frontend uses ``date-fns`` for
 * formatting at render time; no branded ``IsoDateTime`` type is defined,
 * because doing so would force every ``JSON.parse`` result through a
 * converter and provide no runtime safety.
 *
 * IDs are opaque strings on the wire.  ``Manga.id`` and ``Chapter.id`` are
 * 16-hex SHA-256 digests (documented in the PROMPT MASTER); the frontend
 * never inspects their structure, only passes them to the backend.
 */

// ───────────────────────────────────────────────────────────────────────────
// Enumerations
// ───────────────────────────────────────────────────────────────────────────

/**
 * Language of a site, a manga or a chapter.
 *
 * Mirrors ``Language`` in ``core/models/enums.py``.  ``"unknown"`` is the
 * value emitted by parsers that cannot determine the language of a series
 * (mixed-language releases, missing metadata, etc.).  It is **not** the
 * same as "not yet classified" — the backend's ``SiteRegistry`` uses
 * ``"unknown"`` only when the parser reported it, never as a default.
 */
export type Language =
  | "fr"
  | "en"
  | "ja"
  | "ko"
  | "zh"
  | "es"
  | "pt"
  | "it"
  | "de"
  | "ru"
  | "ar"
  | "unknown";

/**
 * Content rating of a manga.
 *
 * Mirrors ``ContentRating`` in ``core/models/enums.py``.  Filtering on the
 * web frontend is governed by ``Settings.content.allow_adult``; the
 * frontend hides ``"erotica"`` and ``"pornographic"`` results by default
 * unless the current user's settings explicitly enable them.
 */
export type ContentRating =
  | "safe"
  | "suggestive"
  | "erotica"
  | "pornographic";

/**
 * Publication status of a manga.
 *
 * Mirrors ``MangaStatus`` in ``core/models/enums.py``.  ``"unknown"`` is
 * the value emitted by parsers that cannot determine the status from the
 * source site — it is common on aggregators and on sites that do not
 * publish status metadata.
 */
export type MangaStatus =
  | "ongoing"
  | "completed"
  | "hiatus"
  | "cancelled"
  | "unknown";

/**
 * Health of a site, as observed by the backend's health checker.
 *
 * Mirrors ``SiteHealth`` in ``core/models/enums.py``.
 *
 *  - ``healthy``  — the site responded successfully.
 *  - ``degraded`` — the site responded but rejected the request
 *    (auth wall, rate limit, unsolved challenge).
 *  - ``down``     — the site did not respond (transport error, 5xx).
 *  - ``unknown``  — the site has not yet been checked, or the response
 *    could not be interpreted.
 */
export type SiteHealth = "healthy" | "degraded" | "down" | "unknown";

// ───────────────────────────────────────────────────────────────────────────
// Canonical value lists and lookup tables
// ───────────────────────────────────────────────────────────────────────────

/** Every value of :type:`Language`, in display order. */
export const LANGUAGES: readonly Language[] = [
  "fr",
  "en",
  "ja",
  "ko",
  "zh",
  "es",
  "pt",
  "it",
  "de",
  "ru",
  "ar",
  "unknown",
] as const;

/** Every value of :type:`ContentRating`, ordered least to most explicit. */
export const CONTENT_RATINGS: readonly ContentRating[] = [
  "safe",
  "suggestive",
  "erotica",
  "pornographic",
] as const;

/** Ratings considered adult for filtering purposes. */
export const ADULT_CONTENT_RATINGS: readonly ContentRating[] = [
  "erotica",
  "pornographic",
] as const;

/** Every value of :type:`MangaStatus`, in display order. */
export const MANGA_STATUSES: readonly MangaStatus[] = [
  "ongoing",
  "completed",
  "hiatus",
  "cancelled",
  "unknown",
] as const;

/** Every value of :type:`SiteHealth`, ordered from best to worst. */
export const SITE_HEALTHS: readonly SiteHealth[] = [
  "healthy",
  "degraded",
  "down",
  "unknown",
] as const;

/** Statuses that represent a usable site for download purposes. */
export const USABLE_SITE_HEALTHS: readonly SiteHealth[] = [
  "healthy",
  "degraded",
] as const;

// ───────────────────────────────────────────────────────────────────────────
// Type guards
// ───────────────────────────────────────────────────────────────────────────

/** Return ``true`` when *value* is a valid :type:`Language`. */
export function isLanguage(value: unknown): value is Language {
  return (
    typeof value === "string" &&
    (LANGUAGES as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *value* is a valid :type:`ContentRating`. */
export function isContentRating(value: unknown): value is ContentRating {
  return (
    typeof value === "string" &&
    (CONTENT_RATINGS as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *value* is a valid :type:`MangaStatus`. */
export function isMangaStatus(value: unknown): value is MangaStatus {
  return (
    typeof value === "string" &&
    (MANGA_STATUSES as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *value* is a valid :type:`SiteHealth`. */
export function isSiteHealth(value: unknown): value is SiteHealth {
  return (
    typeof value === "string" &&
    (SITE_HEALTHS as readonly string[]).includes(value)
  );
}

/** Return ``true`` when *rating* is considered adult content. */
export function isAdultRating(rating: ContentRating): boolean {
  return (ADULT_CONTENT_RATINGS as readonly string[]).includes(rating);
}

/** Return ``true`` when *health* represents a usable site. */
export function isUsableSiteHealth(health: SiteHealth): boolean {
  return (USABLE_SITE_HEALTHS as readonly string[]).includes(health);
}

// ───────────────────────────────────────────────────────────────────────────
// Domain entities
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single page of a chapter.
 *
 * Pages are cheap to produce (the backend derives them from the chapter's
 * reader page) and are only fetched by the UI when a download has been
 * submitted or when the user previews a chapter.  The frontend does not
 * download page images itself — it delegates to the backend's download
 * pipeline.
 *
 * ``width``, ``height``, ``format`` and ``size_bytes`` are ``null`` when
 * the backend has not yet probed the image (this happens for pages listed
 * by an API-driven parser but not yet downloaded).  They become populated
 * once the image has been downloaded and validated.
 */
export interface Page {
  /** 0-based index within the chapter. */
  index: number;
  /** Parent chapter identifier (16-hex). */
  chapter_id: string;
  /** Absolute image URL. */
  url: string;
  /** Pixel width of the image, if known. */
  width: number | null;
  /** Pixel height of the image, if known. */
  height: number | null;
  /** Image format as reported by Pillow, e.g. ``"JPEG"``, ``"PNG"``, ``"WEBP"``. */
  format: string | null;
  /** Payload size in bytes, if known. */
  size_bytes: number | null;
}

/**
 * A chapter of a manga.
 *
 * ``number`` is ``null`` for chapters that the source site does not number
 * (oneshots, special chapters, bonus chapters).  The library sorts
 * ``null``-numbered chapters after numbered ones, preserving the source
 * site's ordering.
 */
export interface Chapter {
  /** Chapter identifier (16-hex SHA-256 digest). */
  id: string;
  /** Parent manga identifier (16-hex SHA-256 digest). */
  manga_id: string;
  /** Site the chapter belongs to. */
  site_id: string;
  /** Display title as rendered by the source site, e.g. ``"Chapter 48"``. */
  title: string;
  /** Absolute chapter URL on the source site. */
  url: string;
  /** Numeric chapter number, if the source site publishes one. */
  number: number | null;
  /** Volume number, if the source site publishes one. */
  volume: number | null;
  /** Language of this chapter.  May differ from the manga's language. */
  language: Language;
  /** ISO 8601 publication timestamp, when the source site provides one. */
  published_at: string | null;
  /** Number of pages, if already known (``null`` before ``get_pages``). */
  page_count: number | null;
}

/**
 * A manga, as exposed by the REST API.
 *
 * Fields that the source site did not publish are ``null`` (scalars) or
 * empty arrays — never omitted, so that the frontend never has to
 * distinguish "missing" from "empty".
 *
 * The ``id`` is derived from ``site_id`` and the source URL's last path
 * segment (see ``BaseParser._make_manga_id``); it is stable across
 * sessions and matches the library database exactly.
 */
export interface Manga {
  /** 16-hex SHA-256 digest of ``site_id:source_id``. */
  id: string;
  /** Site the manga belongs to. */
  site_id: string;
  /** Display name of the site, pre-resolved by the backend. */
  site_name: string;
  /** Title as rendered on the source site. */
  title: string;
  /** Absolute manga URL on the source site. */
  url: string;
  /** Cover image URL, or ``null`` when the parser could not extract one. */
  cover_url: string | null;
  /** Primary language of the series. */
  language: Language;
  /**
   * ``true`` when the site is flagged as adult in ``sites.yaml``.  This is
   * a property of the **site**, not of the specific series; a general site
   * can host adult series and will still report ``adult: false`` here.  Use
   * :type:`ContentRating` for per-series filtering.
   */
  adult: boolean;
  /** Content rating of the series. */
  content_rating: ContentRating;
  /** Publication status of the series. */
  status: MangaStatus;
  /** Synopsis, or ``null`` when the source site does not publish one. */
  description: string | null;
  /** Author(s).  Empty when the source site does not publish them. */
  authors: string[];
  /** Artist(s).  Empty when the source site does not publish them. */
  artists: string[];
  /** Genres as free-form strings.  Empty when not published. */
  genres: string[];
  /** Tags as free-form strings.  Superset of ``genres`` on most sites. */
  tags: string[];
  /** Alternative titles.  Empty when not published. */
  alt_titles: string[];
  /** Year of first publication, when known. */
  year: number | null;
  /** Number of chapters currently known, or ``null`` when not yet enumerated. */
  chapter_count: number | null;
  /**
   * Site-specific metadata the parser wants to surface (serialisation
   * status, translator group, original publisher, etc.).  Keys are opaque
   * to the frontend; render as ``key: value`` pairs only when non-empty.
   *
   * Always an object on the wire — an empty ``{}`` when the parser
   * produced nothing.  Never ``null``: that distinction would force every
   * consumer to handle two "no metadata" shapes for no benefit.
   */
  metadata: Record<string, unknown>;
}

/**
 * Capabilities declared by a site.
 *
 * Mirrors :class:`~nexusdl.parsers.capabilities.ParserCapabilities` and the
 * nine booleans under ``capabilities`` in ``data/sites.yaml``.  The
 * frontend uses these to decide which UI features to expose (e.g. hide the
 * "Filters" panel when ``filters`` is ``false``).
 */
export interface SiteCapabilities {
  /** The site supports search. */
  search: boolean;
  /** Search results are paginated. */
  pagination: boolean;
  /** Search supports filter parameters. */
  filters: boolean;
  /** The parser returns rich metadata (authors, description, etc.). */
  metadata: boolean;
  /** The parser extracts cover art. */
  cover_art: boolean;
  /** The parser enumerates chapter lists. */
  chapter_list: boolean;
  /** The parser enumerates reader images. */
  reader_images: boolean;
  /** The site requires Cloudflare handling. */
  cloudflare: boolean;
  /** The site requires JavaScript rendering. */
  js_rendered: boolean;
}

/**
 * A site, as exposed by the REST API.
 *
 * The frontend never sees ``parser_class``, ``default_headers`` or any
 * other implementation detail — those belong to the registry and are not
 * serialised.  What the frontend needs is the public face: identity,
 * language, adult flag, capabilities and current health.
 */
export interface Site {
  /** Canonical site id, e.g. ``"mangadex"``. */
  id: string;
  /** Human-readable site name, e.g. ``"MangaDex"``. */
  name: string;
  /** Primary domain(s) the site serves from, HTTPS only. */
  domains: string[];
  /** Primary language of the site's content. */
  language: Language;
  /** Whether the site is flagged as adult in ``sites.yaml``. */
  adult: boolean;
  /** Declared capabilities. */
  capabilities: SiteCapabilities;
  /** Last observed health of the site. */
  health: SiteHealth;
}

// ───────────────────────────────────────────────────────────────────────────
// Search
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single search result.
 *
 * This is the lean shape returned by ``POST /api/v1/search`` — one row per
 * matching manga across every queried site.  It carries the minimum needed
 * to render a card and to navigate to the manga detail page via
 * ``manga_id``; richer metadata (description, tags, authors) is fetched
 * separately from ``GET /api/v1/manga/{id}``.
 *
 * The backend computes ``manga_id`` and resolves ``site_name`` so the
 * frontend does not need to join against the sites list.
 *
 * **Consumers that need the full manga shape** — e.g.
 * ``components/MangaCard`` when used on ``/library`` — must not receive a
 * ``SearchResult`` directly; they must fetch the manga resource (or, in
 * the library case, already have it) and pass a :type:`Manga`.  Any
 * adaptation between the two shapes belongs to the component, not to the
 * wire type.
 */
export interface SearchResult {
  /** 16-hex SHA-256 digest of ``site_id:source_id``. */
  manga_id: string;
  /** Site the result comes from. */
  site_id: string;
  /** Display name of the site, pre-resolved by the backend. */
  site_name: string;
  /** Title as rendered on the source site. */
  title: string;
  /** Absolute manga URL on the source site. */
  url: string;
  /** Cover image URL, or ``null`` when the parser could not extract one. */
  cover_url: string | null;
  /** Primary language of the series. */
  language: Language;
  /** ``true`` when the site is flagged adult. */
  adult: boolean;
  /** Content rating of the series. */
  content_rating: ContentRating;
  /** Publication status of the series. */
  status: MangaStatus;
  /** Year of first publication, when known. */
  year: number | null;
}

/**
 * Body for ``POST /api/v1/search``.
 *
 * Search is **aggregated across sites**: the backend queries every enabled
 * site whose language matches the request and yields results as they come
 * in.  A site that fails mid-search does not fail the whole request — its
 * failure is logged server-side and the search continues with the
 * remaining sites.
 *
 * All filter fields default to "no filter".  Passing an empty array for
 * ``sites`` or ``languages`` is equivalent to omitting the field.
 */
export interface SearchRequest {
  /**
   * Search terms.
   *
   * Empty string is **allowed by this type** and is documented as yielding
   * recent releases on most sites.  Whether the backend accepts an empty
   * ``query`` is a backend contract — see
   * ``interfaces/web/backend/schemas/requests.py``.  Callers that submit
   * an empty query from a UI form should validate before sending, so that
   * a stricter backend does not surface a ``VALIDATION_ERROR`` at the
   * point of use.
   */
  query: string;
  /** Restrict to these site ids.  ``null`` / omitted means "all enabled sites". */
  sites?: string[] | null;
  /** Restrict to these languages.  ``null`` / omitted means "all languages". */
  languages?: Language[] | null;
  /**
   * Include adult-rated series.  When ``false`` or omitted, ``erotica``
   * and ``pornographic`` series are excluded, and sites flagged adult in
   * ``sites.yaml`` are skipped entirely.  This is a request-level override
   * of the user's setting, typically only used by admin tools.
   */
  include_adult?: boolean;
  /** 1-based page number. */
  page?: number;
  /** Results per page (backend caps at 100). */
  per_page?: number;
}

/**
 * Response for ``POST /api/v1/search``.
 *
 * Follows the project-wide pagination envelope
 * (``items/total/page/per_page/total_pages/has_next/has_prev``).  Because
 * results are aggregated across sites, ``total`` is the number of results
 * the backend was able to collect from the sites that responded — a site
 * that timed out reduces ``total`` without failing the request.
 *
 * **``failed_sites`` contains site **ids**, not display names.**  The
 * frontend is expected to resolve them against the sites catalogue (which
 * it already has cached) before rendering them to the user — showing raw
 * ids like ``"mangadex"`` instead of ``"MangaDex"`` is a bug, not a
 * deliberate degradation.
 */
export interface SearchResponse {
  items: SearchResult[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
  /**
   * Site ids that failed to respond during this search.  Empty when none
   * failed.  Resolve the display name from the sites catalogue before
   * rendering.
   */
  failed_sites: string[];
}

// ───────────────────────────────────────────────────────────────────────────
// Manga and chapter endpoints
// ───────────────────────────────────────────────────────────────────────────

/**
 * Response for ``GET /api/v1/manga/{id}``.
 *
 * The manga resource is returned as-is; the wrapper exists so that the
 * endpoint can grow sibling fields (``chapters``, ``reading_progress``)
 * without a breaking change.  Today the payload is exactly one
 * :type:`Manga`.
 */
export interface MangaResponse {
  manga: Manga;
}

/**
 * Response for ``GET /api/v1/manga/{id}/chapters``.
 *
 * Chapter lists are **not** paginated — a manga's chapter list is bounded
 * in practice (a long-running weekly series reaches ~1000 chapters over
 * 20 years) and the list is returned in full so that the UI can render a
 * scrollable sidebar without paging.  Callers that need pagination should
 * page themselves client-side.
 */
export interface MangaChaptersResponse {
  manga_id: string;
  items: Chapter[];
}

/**
 * Response for ``GET /api/v1/chapters/{id}``.
 *
 * A chapter resource is returned as-is; the wrapper exists for the same
 * forward-compatibility reason as :type:`MangaResponse`.
 */
export interface ChapterResponse {
  chapter: Chapter;
}

/**
 * Response for ``GET /api/v1/chapters/{id}/pages``.
 *
 * Page lists are not paginated (same rationale as chapter lists).  Pages
 * are ordered by ``index``.
 */
export interface ChapterPagesResponse {
  chapter_id: string;
  items: Page[];
}

/**
 * Response for ``GET /api/v1/sites``.
 *
 * Returns every enabled site, without pagination.  The registry is small
 * (roughly 60 entries) and the response is cached aggressively on the
 * frontend — the site list changes only when the operator edits
 * ``sites.yaml`` and restarts the backend.
 */
export interface SiteListResponse {
  items: Site[];
}

/**
 * Response for ``GET /api/v1/sites/{id}``.
 *
 * A site resource is returned as-is.
 */
export interface SiteResponse {
  site: Site;
}

/**
 * Response for ``GET /api/v1/sites/{id}/health``.
 *
 * The ``checked_at`` field is the timestamp of the **last** check the
 * backend performed, not the time of this request — health checks run on a
 * background schedule, so a fresh request returns the cached result.
 * Callers that need an on-demand check should use ``POST
 * /api/v1/sites/{id}/health/check`` (not in this API version) rather than
 * hammering this endpoint.
 */
export interface SiteHealthResponse {
  site_id: string;
  status: SiteHealth;
  checked_at: string;
  /** Human-readable detail, or ``null`` when the check succeeded silently. */
  message: string | null;
  /** Round-trip latency in milliseconds, when the check measured one. */
  latency_ms: number | null;
}

// ───────────────────────────────────────────────────────────────────────────
// WebSocket event payloads
// ───────────────────────────────────────────────────────────────────────────

/**
 * Payload of the ``site.health.changed`` event.
 *
 * Emitted when a background health check transitions a site to a different
 * :type:`SiteHealth` value.  Repeated checks that confirm the current state
 * do **not** emit this event — only transitions do.
 */
export interface SiteHealthChangedPayload {
  site_id: string;
  previous_health: SiteHealth;
  current_health: SiteHealth;
  checked_at: string;
  message: string | null;
}

/**
 * Payload of the ``library.updated`` event.
 *
 * Emitted whenever a manga is added to, updated in or removed from the
 * local library.  The frontend uses this to invalidate its library query
 * cache without polling.
 */
export interface LibraryUpdatedPayload {
  manga_id: string;
  /** What changed. */
  action: "added" | "updated" | "deleted";
}

// ───────────────────────────────────────────────────────────────────────────
// Manga-related event type discriminator
// ───────────────────────────────────────────────────────────────────────────

/**
 * The subset of WS event names that carry a manga / site payload.
 *
 * Mirrors the role of :type:`DownloadEventType` in :mod:`./download`.
 * Combined with :type:`DownloadEventType`, this covers every event the
 * backend emits.
 */
export type MangaEventType =
  | "site.health.changed"
  | "library.updated";
