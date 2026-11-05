/**
 * Store barrel — single import path for every Zustand store in the frontend.
 *
 * Why a barrel file
 * -----------------
 * The frontend has several stores, each owning a distinct slice of client
 * state:
 *
 *  * :mod:`./downloads` — live download tasks, fed by REST snapshots and
 *    WebSocket events.
 *  * :mod:`./settings` — persistent user preferences (language, adult opt-in,
 *    default packaging format, sidebar state, etc.).
 *
 * Components frequently need a value from more than one store — the
 * download dialog needs ``default_packaging_format`` and the current
 * download queue; the search page needs ``allow_adult`` and the download
 * submission action.  Without a barrel, every such component would import
 * from two files, and any future store split (e.g. extracting a dedicated
 * ``library`` store) would ripple through the whole ``components/`` tree.
 *
 * The barrel collapses that to a single import::
 *
 *     import { useSettingsStore, useDownloadsStore } from "@/store";
 *
 * The underlying modules remain the source of truth — the barrel only
 * re-exports.  Consumers that need to know *which* module a symbol comes
 * from can still import directly; the barrel is a convenience, not a
 * gatekeeper.
 *
 * Re-export strategy
 * ------------------
 * Both re-exports use ``export *``, which forwards every named export
 * (types, values, and named re-exports) from the target module.  This is
 * deliberate:
 *
 *  * **No symbol list to maintain.**  When ``downloads.ts`` grows a new
 *    selector or ``settings.ts`` gains a new action, the barrel does not
 *    need to be touched.  The alternative — listing every symbol
 *    explicitly — would force a change to this file for every change to a
 *    sibling file, which is precisely the "reopen a delivered file"
 *    failure mode this project forbids.
 *
 *  * **Type safety is preserved.**  ``export *`` is fully typed in
 *    TypeScript; it does not erase, widen or lose type information.  A
 *    consumer importing ``useSettingsStore`` from ``@/store`` gets exactly
 *    the same inferred types as a consumer importing it from
 *    ``@/store/settings``.
 *
 *  * **No default exports.**  Neither store module has a default export —
 *    the project's convention is named exports only, so that an import
 *    site always names what it is importing.  ``export *`` therefore does
 *    not silently miss any export.
 *
 * Name collisions
 * ---------------
 * If a future change introduces the same symbol name in both modules (e.g.
 * a shared ``selectActive`` selector), TypeScript does **not** raise an
 * error at this file — it silently excludes the ambiguous name from the
 * barrel's exports.  Consumers that try to import the ambiguous name from
 * ``@/store`` will then get a "not exported" error and must import it from
 * the specific module.  This is the standard, well-understood TypeScript
 * behaviour, and it is preferrable to the alternative of a compile-time
 * failure at the barrel site: the failure is always caught on first use,
 * so a stray ambiguity never reaches production.
 *
 * To minimise the risk, the two stores follow a shared naming convention:
 * store-specific selectors are prefixed with what they select
 * (``selectLanguage``, ``selectLibraryView``, ``selectDownloadById``,
 * ``selectActiveDownloads``).  Cross-store name collisions are therefore
 * structurally unlikely.
 *
 * What is **not** re-exported
 * ---------------------------
 * * **Wire-format types** (``DownloadTask``, ``DownloadStatus``,
 *   ``Site``, ``Manga``, …).  Those live in :mod:`../types/*` and are the
 *   single source of truth for the backend contract.  Re-exporting them
 *   from the store barrel would create a second import path with the same
 *   symbol, which is exactly the kind of ambiguity this project avoids.
 *   Import them from ``@/types/download``, ``@/types/manga``,
 *   ``@/types/site``.
 * * **Persistent-storage internals** (``createJSONStorage``, ``persist``).
 *   Those are implementation details of the individual store modules;
 *   exposing them from the barrel would encourage consumers to build
 *   their own persistence wrappers around shared state, which violates
 *   the single-owner principle.
 * * **Hydration helpers** (``rehydrateSettings``,
 *   ``useSettingsHydrated``).  These are exported by
 *   :mod:`./settings` and therefore forwarded by ``export *`` — they are
 *   *not* re-exported separately because doing so would be redundant.  A
 *   consumer importing ``useSettingsHydrated`` from ``@/store`` gets it
 *   via the ``settings`` forward, identically to importing it from
 *   ``@/store/settings``.
 *
 * Server-side rendering
 * ---------------------
 * This module is safe to import on the server.  ``export *`` only
 * resolves bindings; it does not evaluate the target modules' top-level
 * side effects beyond what a normal import would trigger.  Both store
 * modules are already SSR-safe by construction:
 *
 *  * ``settings.ts`` guards ``localStorage`` behind a client-only storage
 *    factory and defers hydration.
 *  * ``downloads.ts`` stores only in-memory state and never touches
 *    ``window`` at module scope.
 *
 * Next.js therefore tree-shakes and server-renders the barrel exactly as
 * it would the individual modules.
 */

// Store modules — re-exported wholesale so that adding a store or a new
// export inside an existing store never requires a change to this file.
export * from "./downloads";
export * from "./settings";
