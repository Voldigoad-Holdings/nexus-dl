/**
 * User settings store — persistent, client-side preferences.
 *
 * Everything in this store is a **user preference** that must survive page
 * reloads and browser restarts.  It is persisted to ``localStorage`` via
 * Zustand's ``persist`` middleware.  Nothing in this store is derived from
 * the backend, and nothing here is ever sent to the backend as-is: the
 * search and download hooks translate the relevant fields into
 * :type:`~../types/manga.SearchRequest` and
 * :type:`~../types/download.CreateDownloadRequest` bodies at the point of
 * submission.
 *
 * What does **not** live here
 * ---------------------------
 * * **Theme** — handled by ``next-themes`` (``providers.tsx`` / ``ThemeToggle``).
 *   Duplicating theme state in this store would create two sources of truth
 *   and break ``next-themes``'s SSR-safe hydration.  This store never
 *   touches the ``theme`` key.
 * * **Server-side configuration** — settings resolved from
 *   ``data/default_config.yaml`` / ``.env`` by ``core/config.py``.  Those
 *   are read-only from the frontend's perspective and are surfaced through
 *   the API settings endpoints (admin-only), not through this store.
 * * **Transient UI state** — per-page query text, per-modal form state,
 *   hover/focus booleans.  Those belong to local React state.
 *
 * Naming convention
 * -----------------
 * State fields use **snake_case** to match the backend's
 * :class:`Settings` model (``allow_adult``, ``default_skip_existing``,
 * etc.).  When the same field exists on both sides, a copy-paste between
 * the store and the backend config requires no translation — which is what
 * makes the "same name = same concept" rule enforceable by eye.
 *
 * Persistence and SSR
 * -------------------
 * The store is created with ``skipHydration: true`` to avoid the React
 * hydration mismatch that would otherwise occur when ``localStorage``
 * contains a non-default value (server renders defaults, client hydrates
 * with persisted values, React warns).  Hydration is triggered explicitly:
 *
 * * ``providers.tsx`` calls :func:`rehydrateSettings` in a client effect.
 * * As a safety net, :func:`useSettingsHydrated` also triggers rehydration
 *   on first mount — so any component that gates on that hook will bring
 *   the store up to date even if ``providers.tsx`` forgets to.
 *
 * Components that need the persisted value on first render must gate on
 * :func:`useSettingsHydrated`.  Components that do not gate will show the
 * defaults until the store is hydrated, which is the correct behaviour for
 * preferences that rarely change (adult toggle, packaging format, etc.) —
 * and preferable to a hydration mismatch.
 *
 * Versioning
 * ----------
 * The persisted payload is versioned.  Adding a new setting in a future
 * release bumps the version number; the ``migrate`` function merges the
 * previous payload with the new :data:`DEFAULT_SETTINGS` so that any field
 * added since the previous version gets its default value automatically.
 * Removing a setting is a no-op at the persistence layer — the removed key
 * is simply ignored by the new state shape.
 */
import { useEffect, useState } from "react";
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import type {
  DownloadPriority,
  PackagingFormat,
} from "../types/download";

// ───────────────────────────────────────────────────────────────────────────
// Local types
// ───────────────────────────────────────────────────────────────────────────

/**
 * The UI locales for which this project ships translations.
 *
 * Matches the file names under ``data/translations/`` (``en.json``,
 * ``fr.json``, ``es.json``, ``de.json``).  This is deliberately narrower
 * than :type:`~../types/manga.Language`, which enumerates every language
 * the *content* can be in.  A user can read French manga with the UI in
 * German, and vice versa — the two settings are independent.
 *
 * Adding a UI locale requires two steps in lockstep:
 *
 * 1. Add the locale code here.
 * 2. Add the corresponding ``{locale}.json`` under
 *    ``src/nexusdl/data/translations/``.
 *
 * The i18n loader falls back to ``"en"`` for any missing key, so a partial
 * translation is usable — but adding the file without adding the locale
 * here would make it unreachable, and adding the locale without the file
 * would make every key fall back to English.  Both cases are caught in CI
 * by the translation-consistency test described in the PROMPT MASTER.
 */
export const UI_LOCALES = ["en", "fr", "es", "de"] as const;

/** A supported UI locale. */
export type UILocale = (typeof UI_LOCALES)[number];

/** Layout of the library page grid. */
export type LibraryView = "grid" | "list";

// ───────────────────────────────────────────────────────────────────────────
// State and action interfaces
// ───────────────────────────────────────────────────────────────────────────

/**
 * The persistent portion of the settings store.
 *
 * Every field here is written to ``localStorage`` and survives page
 * reloads.  No field is derived from another; each is the authoritative
 * source for exactly one user preference.
 */
export interface SettingsState {
  /**
   * Language for UI strings (labels, buttons, messages).  Independent of
   * the language of the *content* being browsed or downloaded.
   */
  language: UILocale;

  /**
   * Whether the user has opted in to seeing adult content.
   *
   * When ``false``:
   *  * Sites flagged ``adult: true`` in ``sites.yaml`` are hidden from the
   *    site list and from search results.
   *  * Search results with ``content_rating`` of ``erotica`` or
   *    ``pornographic`` are excluded.
   *
   * This value is also sent as ``include_adult`` on every search request,
   * so the backend enforces the same filter even if a client-side filter is
   * bypassed.
   */
  allow_adult: boolean;

  /**
   * Default for the "include unhealthy sites" filter on the search page.
   *
   * ``true`` (the default) means "show sites whose health is ``down`` or
   * ``degraded`` — the health badge will tell the user".  Set to ``false``
   * to hide them by default; the user can still reveal them per search
   * through the filter panel.
   */
  default_include_unhealthy: boolean;

  /**
   * Default packaging format for new downloads.
   *
   * Must be one of :data:`~../types/download.PACKAGING_FORMATS` (folder,
   * zip, cbz, cb7, pdf, cbt).  The backend uses this value whenever a
   * :type:`~../types/download.CreateDownloadRequest` omits
   * ``packaging_format``.
   */
  default_packaging_format: PackagingFormat;

  /**
   * Default queue priority for new downloads.
   *
   * Must be one of :data:`~../types/download.DOWNLOAD_PRIORITIES` (low,
   * normal, high).  The backend uses this value whenever a
   * :type:`~../types/download.CreateDownloadRequest` omits ``priority``.
   */
  default_priority: DownloadPriority;

  /**
   * Default value for the "skip existing chapters" option on new downloads.
   *
   * ``true`` (the default) means "do not re-download chapters already
   * present on disk"; the backend honours this by comparing against the
   * library database before enqueueing each chapter.
   */
  default_skip_existing: boolean;

  /**
   * Layout of the library page — a grid of cover cards, or a dense list
   * with one row per series.
   */
  library_view: LibraryView;

  /**
   * Whether the sidebar is collapsed (icons only) or expanded (icons +
   * labels).
   */
  sidebar_collapsed: boolean;

  /**
   * Whether the frontend should maintain a WebSocket connection to
   * ``/ws/v1/events``.
   *
   * ``true`` (the default) means "connect on load".  Disabling it does not
   * affect the backend — events continue to be emitted — it only
   * disconnects this client.  Useful when the user is on a metered
   * connection, or as a debugging aid.
   */
  web_events_enabled: boolean;
}

/**
 * The mutation API exposed by the store.
 *
 * Every setter is a shallow assignment; no setter validates against the
 * backend, because the backend will reject an invalid value at the API
 * boundary and surface a proper error.  Client-side validation would
 * duplicate the backend's checks without replacing them.
 *
 * ``toggle*`` helpers exist for the two settings whose UI affordances are
 * buttons rather than forms (sidebar, library view).  Every other setting
 * is set through its explicit setter.
 */
export interface SettingsActions {
  setLanguage: (language: UILocale) => void;
  setAllowAdult: (allow: boolean) => void;
  setDefaultIncludeUnhealthy: (include: boolean) => void;
  setDefaultPackagingFormat: (format: PackagingFormat) => void;
  setDefaultPriority: (priority: DownloadPriority) => void;
  setDefaultSkipExisting: (skip: boolean) => void;
  setLibraryView: (view: LibraryView) => void;
  toggleLibraryView: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebar: () => void;
  setWebEventsEnabled: (enabled: boolean) => void;

  /**
   * Reset every setting to :data:`DEFAULT_SETTINGS`.
   *
   * Intended for the "Restore default settings" action on the settings
   * page and for tests.  Does not clear the persisted payload — the store
   * remains persisted, just with the default values.
   */
  reset: () => void;
}

/** The full store shape — state plus actions. */
export type SettingsStore = SettingsState & SettingsActions;

// ───────────────────────────────────────────────────────────────────────────
// Defaults
// ───────────────────────────────────────────────────────────────────────────

/**
 * The default value of every setting.
 *
 * These values match ``data/default_config.yaml`` on the backend for the
 * overlapping fields, so a fresh install behaves identically regardless of
 * which interface the user starts with.  The frontend-specific fields
 * (``library_view``, ``sidebar_collapsed``, ``web_events_enabled``) have no
 * backend equivalent and default to the values that produce the most
 * intuitive first-run experience.
 *
 * Frozen at **both** the type level and the runtime level: TypeScript's
 * ``Readonly<>`` prevents accidental mutation in the codebase, and
 * ``Object.freeze`` enforces the same invariant at runtime for any code
 * that bypasses the type system (a ``JSON.parse``-produced object spread
 * into this one, a test using ``as any``, …).  All fields are primitives,
 * so a shallow freeze is sufficient.
 */
export const DEFAULT_SETTINGS: Readonly<SettingsState> = Object.freeze({
  language: "en",
  allow_adult: false,
  default_include_unhealthy: true,
  default_packaging_format: "cbz",
  default_priority: "normal",
  default_skip_existing: true,
  library_view: "grid",
  sidebar_collapsed: false,
  web_events_enabled: true,
});

// ───────────────────────────────────────────────────────────────────────────
// Validation helpers (used by ``migrate``)
// ───────────────────────────────────────────────────────────────────────────

const VALID_LOCALES = new Set<string>(UI_LOCALES);
const VALID_FORMATS = new Set<string>([
  "folder",
  "zip",
  "cbz",
  "cb7",
  "pdf",
  "cbt",
]);
const VALID_PRIORITIES = new Set<string>(["low", "normal", "high"]);
const VALID_LIBRARY_VIEWS = new Set<string>(["grid", "list"]);

/**
 * Sanitise a persisted payload against the current schema.
 *
 * A ``localStorage`` payload is **untrusted input**: it can be corrupted
 * by a browser extension, produced by an older version of the app with a
 * different enum vocabulary, or set by hand in the devtools.  Blindly
 * spreading it over :data:`DEFAULT_SETTINGS` would copy invalid enum
 * values into the store, causing downstream type violations that TS
 * cannot catch (the runtime value is never validated).
 *
 * This function keeps every valid field and replaces every invalid one
 * with its default.  A field that is missing is simply left at the
 * default — the ``...DEFAULT_SETTINGS`` spread already provides it.
 *
 * Called from ``migrate`` on every rehydration, so it also handles the
 * "user upgraded from a version that had a bug" case.
 */
function sanitizePersistedSettings(
  raw: unknown,
): Partial<SettingsState> {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return {};
  }
  const input = raw as Record<string, unknown>;
  const out: Partial<SettingsState> = {};

  if (typeof input.language === "string" && VALID_LOCALES.has(input.language)) {
    out.language = input.language as UILocale;
  }
  if (typeof input.allow_adult === "boolean") {
    out.allow_adult = input.allow_adult;
  }
  if (typeof input.default_include_unhealthy === "boolean") {
    out.default_include_unhealthy = input.default_include_unhealthy;
  }
  if (
    typeof input.default_packaging_format === "string" &&
    VALID_FORMATS.has(input.default_packaging_format)
  ) {
    out.default_packaging_format =
      input.default_packaging_format as PackagingFormat;
  }
  if (
    typeof input.default_priority === "string" &&
    VALID_PRIORITIES.has(input.default_priority)
  ) {
    out.default_priority = input.default_priority as DownloadPriority;
  }
  if (typeof input.default_skip_existing === "boolean") {
    out.default_skip_existing = input.default_skip_existing;
  }
  if (
    typeof input.library_view === "string" &&
    VALID_LIBRARY_VIEWS.has(input.library_view)
  ) {
    out.library_view = input.library_view as LibraryView;
  }
  if (typeof input.sidebar_collapsed === "boolean") {
    out.sidebar_collapsed = input.sidebar_collapsed;
  }
  if (typeof input.web_events_enabled === "boolean") {
    out.web_events_enabled = input.web_events_enabled;
  }

  return out;
}

// ───────────────────────────────────────────────────────────────────────────
// Store
// ───────────────────────────────────────────────────────────────────────────

/**
 * The settings store.
 *
 * Read values with a selector::
 *
 *     const language = useSettingsStore((s) => s.language);
 *
 * Call actions through a selector (actions are stable references, so this
 * never causes a re-render on its own)::
 *
 *     const setLanguage = useSettingsStore((s) => s.setLanguage);
 *
 * Never subscribe to the whole store (``useSettingsStore()`` with no
 * selector) — that returns a new object on every state change and forces
 * the component to re-render on unrelated setting changes.
 */
export const useSettingsStore = create<SettingsStore>()(
  persist(
    (set) => ({
      ...DEFAULT_SETTINGS,

      // -----------------------------------------------------------------
      // Simple setters
      // -----------------------------------------------------------------
      setLanguage: (language) => set({ language }),
      setAllowAdult: (allow_adult) => set({ allow_adult }),
      setDefaultIncludeUnhealthy: (default_include_unhealthy) =>
        set({ default_include_unhealthy }),
      setDefaultPackagingFormat: (default_packaging_format) =>
        set({ default_packaging_format }),
      setDefaultPriority: (default_priority) => set({ default_priority }),
      setDefaultSkipExisting: (default_skip_existing) =>
        set({ default_skip_existing }),
      setLibraryView: (library_view) => set({ library_view }),
      setSidebarCollapsed: (sidebar_collapsed) => set({ sidebar_collapsed }),
      setWebEventsEnabled: (web_events_enabled) => set({ web_events_enabled }),

      // -----------------------------------------------------------------
      // Toggles
      // -----------------------------------------------------------------
      toggleLibraryView: () =>
        set((state) => ({
          library_view: state.library_view === "grid" ? "list" : "grid",
        })),
      toggleSidebar: () =>
        set((state) => ({
          sidebar_collapsed: !state.sidebar_collapsed,
        })),

      // -----------------------------------------------------------------
      // Reset
      // -----------------------------------------------------------------
      reset: () => set({ ...DEFAULT_SETTINGS }),
    }),
    {
      name: "nexusdl-settings",
      version: 1,
      storage: createJSONStorage(() => localStorage),

      // Defer hydration to an explicit call so that the server-rendered
      // HTML and the client's first render are identical.  Hydration is
      // triggered by ``rehydrateSettings`` below and, as a safety net, by
      // ``useSettingsHydrated``.
      skipHydration: true,

      // Only persist state fields — not action functions, which cannot be
      // serialised and would break ``JSON.parse`` on load.
      //
      // The explicit list is the contract: a field that is not listed here
      // is not persisted.  When adding a field to ``SettingsState``, add
      // it here too — the ``satisfies`` clause below enforces that the key
      // set matches ``DEFAULT_SETTINGS``, so forgetting a field is a
      // compile-time error rather than a silent persistence bug.
      partialize: (state) =>
        ({
          language: state.language,
          allow_adult: state.allow_adult,
          default_include_unhealthy: state.default_include_unhealthy,
          default_packaging_format: state.default_packaging_format,
          default_priority: state.default_priority,
          default_skip_existing: state.default_skip_existing,
          library_view: state.library_view,
          sidebar_collapsed: state.sidebar_collapsed,
          web_events_enabled: state.web_events_enabled,
        }) satisfies Record<keyof SettingsState, unknown>,

      // Merge any previously persisted payload with the current defaults
      // so that fields added in a future version get their default value
      // rather than being ``undefined``.  Version 1 is the initial version
      // — ``migrate`` is called only on future upgrades and never on a
      // fresh install.
      //
      // ``sanitizePersistedSettings`` rejects every invalid field, so a
      // payload with e.g. ``language: "xx"`` or
      // ``default_packaging_format: "cbr"`` falls back to the default for
      // that field only — the rest of the payload is preserved.  Arrays
      // and non-objects are rejected wholesale, which used to be a
      // ``{ ...[], ... }`` crash-waiting-to-happen in the previous version.
      migrate: (persistedState, _fromVersion) => ({
        ...DEFAULT_SETTINGS,
        ...sanitizePersistedSettings(persistedState),
      }),
    },
  ),
);

// ───────────────────────────────────────────────────────────────────────────
// Hydration helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Module-level flag: has :func:`rehydrateSettings` ever been called?
 *
 * Hydration is idempotent (``persist.rehydrate`` is a no-op once the store
 * has hydrated), but each component that uses :func:`useSettingsHydrated`
 * calls ``rehydrateSettings()`` in its own effect.  In a page with N such
 * components, that is N ``localStorage.getItem`` reads plus N promise
 * allocations for a no-op.  The flag short-circuits this at module level:
 * the first caller pays, the rest return immediately.
 */
let hasBeenTriggered = false;

/**
 * Trigger settings rehydration from ``localStorage`` if it has not yet run.
 *
 * Safe to call repeatedly — subsequent calls are no-ops once hydration has
 * completed.  Intended to be called from ``providers.tsx`` inside a client
 * effect.  Components that need the persisted values must additionally gate
 * on :func:`useSettingsHydrated` (or accept a first-render flash of
 * defaults).
 *
 * A rejection (corrupted payload, quota exceeded, privacy mode) is
 * reported through the console and re-thrown so that the caller's
 * ``.catch`` handler sees it.  Swallowing the rejection silently would
 * leave :func:`useSettingsHydrated` stuck on ``false`` forever, rendering
 * a skeleton that never resolves.
 */
export async function rehydrateSettings(): Promise<void> {
  if (useSettingsStore.persist.hasHydrated()) {
    return;
  }
  if (hasBeenTriggered) {
    // Another caller already kicked hydration off.  Wait for the same
    // promise instead of racing a second one — this keeps concurrent
    // callers in sync without needing a shared promise reference.
    await waitForHydration();
    return;
  }
  hasBeenTriggered = true;
  await useSettingsStore.persist.rehydrate();
}

/**
 * Resolve once the store has finished hydrating.
 *
 * Used internally by :func:`rehydrateSettings` when a second caller races
 * the first — instead of kicking off a second ``rehydrate()``, it simply
 * awaits the ``onFinishHydration`` event that the first call will emit.
 */
function waitForHydration(): Promise<void> {
  if (useSettingsStore.persist.hasHydrated()) {
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    const unsubscribe = useSettingsStore.persist.onFinishHydration(() => {
      unsubscribe();
      resolve();
    });
  });
}

/**
 * Return ``true`` once the store has been hydrated from ``localStorage``,
 * and trigger hydration on first mount if it has not already run.
 *
 * Use this to gate UI that depends on a persisted value::
 *
 *     const hydrated = useSettingsHydrated();
 *     if (!hydrated) return <SettingsSkeleton />;
 *     return <ActualSettings />;
 *
 * The hook is self-healing: even if ``providers.tsx`` forgets to call
 * :func:`rehydrateSettings`, the first component that uses this hook will
 * trigger hydration.  This keeps the app functional in all cases, at the
 * cost of a brief default-state render for the component that happens to
 * mount first.
 */
export function useSettingsHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    // Trigger rehydration on first mount if not yet done.  This is the
    // safety net that makes the hook self-sufficient.  A rejection is
    // logged (rather than silently swallowed) so that a corrupted
    // localStorage payload is visible in the console.
    void rehydrateSettings().catch((err: unknown) => {
      // eslint-disable-next-line no-console
      console.error("Failed to rehydrate settings:", err);
    });

    // Sync the initial state, in case hydration completed between the
    // render (during which ``hydrated`` was ``false``) and this effect.
    setHydrated(useSettingsStore.persist.hasHydrated());

    // Subscribe to the next finish event.  If hydration already completed
    // before this subscription was installed, the initial ``setHydrated``
    // above already covered it.
    const unsubscribe = useSettingsStore.persist.onFinishHydration(() => {
      setHydrated(true);
    });
    return unsubscribe;
  }, []);

  return hydrated;
}

// ───────────────────────────────────────────────────────────────────────────
// Selectors
// ───────────────────────────────────────────────────────────────────────────
//
// Named selectors are exported so that components can import them once
// instead of writing the same lambda inline.  They also make it trivial to
// audit which components depend on which field: a rename triggers a
// compiler error at every use site, rather than silently returning
// ``undefined`` at runtime.

/** Return the current UI locale. */
export const selectLanguage = (s: SettingsStore): UILocale => s.language;

/** Return the current adult-content opt-in. */
export const selectAllowAdult = (s: SettingsStore): boolean => s.allow_adult;

/** Return the default "include unhealthy sites" filter. */
export const selectDefaultIncludeUnhealthy = (s: SettingsStore): boolean =>
  s.default_include_unhealthy;

/** Return the default packaging format. */
export const selectDefaultPackagingFormat = (
  s: SettingsStore,
): PackagingFormat => s.default_packaging_format;

/** Return the default queue priority. */
export const selectDefaultPriority = (
  s: SettingsStore,
): DownloadPriority => s.default_priority;

/** Return the default "skip existing chapters" option. */
export const selectDefaultSkipExisting = (s: SettingsStore): boolean =>
  s.default_skip_existing;

/** Return the current library layout. */
export const selectLibraryView = (s: SettingsStore): LibraryView =>
  s.library_view;

/** Return whether the sidebar is collapsed. */
export const selectSidebarCollapsed = (s: SettingsStore): boolean =>
  s.sidebar_collapsed;

/** Return whether the WebSocket connection is enabled. */
export const selectWebEventsEnabled = (s: SettingsStore): boolean =>
  s.web_events_enabled;
