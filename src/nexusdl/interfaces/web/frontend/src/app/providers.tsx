"use client";

/**
 * Root client-side providers for the NexusDL web frontend.
 *
 * This file is the **single client boundary** at the top of the App Router
 * tree.  It composes every context provider the application needs — theme,
 * server-data cache, WebSocket, toasts — and wires the cross-cutting
 * behaviour that no individual page should be responsible for:
 *
 * 1. **Theme** — :class:`ThemeProvider` from ``next-themes`` sets
 *    ``class="dark"`` (or ``"light"``) on ``<html>``.  This is what makes
 *    the Tailwind ``dark:`` variant work and what makes the ``.dark`` /
 *    ``.light`` overrides in ``globals.css`` activate.  The default is
 *    ``"system"`` so that a fresh visitor gets their OS preference; the
 *    ``ThemeToggle`` in the sidebar lets them override it.
 *
 * 2. **Server-data cache** — a single :class:`QueryClient` provided by
 *    :class:`QueryClientProvider`.  Every page that fetches server data
 *    uses this client; sharing one cache is what makes navigating between
 *    ``/search`` and ``/manga/{id}`` reuse already-fetched results instead
 *    of refetching them.
 *
 * 3. **WebSocket** — a hidden :class:`WebSocketBridge` component runs
 *    :func:`~../hooks/useWebSocket.useWebSocket` on every client mount and
 *    wires the ``library.updated``, ``site.health.changed``,
 *    ``download.completed`` and ``download.failed`` events to cache
 *    invalidation and toasts.  The download-event-to-store wiring is
 *    already provided by ``useWebSocket`` itself; the bridge adds the
 *    cross-cutting concerns (React Query invalidation, user-visible
 *    notifications) that no single page can own.
 *
 * 4. **Toasts** — a :class:`Toaster` from ``sonner``, styled to follow the
 *    active theme.  Every success, error and informational notification in
 *    the app goes through it via the ``toast`` helper exported by sonner.
 *
 * 5. **Settings hydration** — calls
 *    :func:`~../store/settings.rehydrateSettings` on first client mount so
 *    that the persisted preferences (language, adult opt-in, download
 *    defaults, sidebar state, …) become active **before** the first paint
 *    that depends on them.
 *
 * 6. **Token tracking** — polls ``localStorage`` for the access token and
 *    re-mounts the WebSocket bridge when the token changes.  This is the
 *    bridge between the "token lives in localStorage" model of
 *    :mod:`../lib/api` and the "token is a reactive dependency of the
 *    WebSocket" model of :mod:`../lib/ws`.  It also tears down the
 *    WebSocket singleton on token rotation.  When the auth store is
 *    delivered, this mechanism is removed — the auth store will make the
 *    token reactive through Zustand and no polling is needed.
 *
 * Why a sub-component for the WebSocket
 * -------------------------------------
 * :func:`~../hooks/useWebSocket.useWebSocket` calls ``useQueryClient()``,
 * which is a React hook that reads from the ``<QueryClientProvider>``
 * context.  A hook cannot consume a context provided by an element that it
 * itself is rendered inside — so the bridge must be a **child** of
 * ``<QueryClientProvider>``, not a sibling of it.  Extracting it to a
 * separate component is the standard pattern for the same reason React
 * Query's own docs recommend it.
 *
 * Why ``useState`` for the ``QueryClient``
 * ----------------------------------------
 * The client must be created **once per browser session**, not once per
 * component render.  Using ``useState`` with a lazy initialiser gives us
 * exactly that: the factory runs once, and the same client instance
 * survives re-renders (including the extra re-render caused by React
 * Strict Mode in development).  Without this, every render would produce a
 * fresh cache and TanStack Query would lose all its state.
 *
 * Why polling for the token
 * -------------------------
 * The access token lives in ``localStorage`` so that a reload preserves
 * the session.  When the API client refreshes the token (transparently,
 * after a 401), it writes the new value to ``localStorage`` — but the
 * browser does not fire a ``storage`` event for writes performed by the
 * **same tab**, and the app has no reactive source for the token yet
 * (the auth store is a future iteration).  Polling every two seconds is a
 * cheap, deterministic way to detect the change:
 *
 * * One ``localStorage.getItem`` call per poll — negligible cost.
 * * The result is compared by value, so unchanged reads do not cause
 *   re-renders.
 * * ``storage`` events (from other tabs) and ``visibilitychange`` events
 *   (tab focus) also trigger an immediate check, so the worst-case delay
 *   is 2 seconds and the common case is instant.
 *
 * This is documented as **temporary infrastructure**: it will be replaced
 * by reactive token state when the auth store lands, and no page depends on
 * the polling mechanism itself — only on the resulting WebSocket behaviour.
 *
 * What this file does **not** do
 * ------------------------------
 * * **No authentication redirects.**  If the session expires, the API
 *   client surfaces a ``UNAUTHORIZED`` error and the page that issued the
 *   request is responsible for redirecting to ``/auth/login``.  Centralising
 *   that here would require an auth store that knows the current route —
 *   exactly what the future auth store will provide.
 * * **No i18n setup.**  The translation system in ``data/translations/``
 *   is a backend concern that a future frontend hook will consume.  Until
 *   then, components use the English defaults from ``data/translations/en.json``.
 * * **No service worker registration.**  The PWA manifest declares the app
 *   installable, but the offline story is a future iteration.  Registering
 *   a service worker that does nothing would be worse than not registering
 *   one at all.
 * * **No analytics, no error boundary, no Sentry.**  None of those are part
 *   of the current project scope, and each would require its own
 *   configuration that does not yet exist.
 */
import {
  QueryClient,
  QueryClientProvider,
  useQueryClient,
} from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { ThemeProvider, useTheme } from "next-themes";
import {
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Toaster, toast } from "sonner";

import { useWebSocket } from "../hooks/useWebSocket";
import { NexusApiError } from "../lib/api";
import {
  resetWebSocketClient,
  type NexusEvent,
} from "../lib/ws";
import { useDownloadsStore } from "../store/downloads";
import { rehydrateSettings } from "../store/settings";
import type {
  DownloadCompletedPayload,
  DownloadFailedPayload,
} from "../types/download";
import type {
  LibraryUpdatedPayload,
  SiteHealthChangedPayload,
  SiteListResponse,
} from "../types/manga";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * ``localStorage`` key for the access token.
 *
 * Matches the key used by :mod:`../lib/api` and
 * :mod:`../hooks/useWebSocket`.  Kept as a local constant because neither
 * module exports it — when the auth store lands, the key moves to a single
 * shared location and this constant is removed.
 */
const ACCESS_TOKEN_STORAGE_KEY = "nexusdl_access_token";

/**
 * Polling interval for the access token, in milliseconds.
 *
 * A two-second interval is a trade-off between responsiveness (the
 * WebSocket should pick up a refreshed token reasonably quickly) and cost
 * (one ``localStorage.getItem`` call per poll is negligible — but there is
 * no reason to poll more often than that).  When the auth store lands, this
 * constant and the polling mechanism both disappear.
 */
const TOKEN_POLL_INTERVAL_MS = 2_000;

// ───────────────────────────────────────────────────────────────────────────
// Query client factory
// ───────────────────────────────────────────────────────────────────────────

/**
 * Build a new :class:`QueryClient` with project-wide defaults.
 *
 * Called exactly once per browser session via ``useState``'s lazy
 * initialiser — never on re-render.  The defaults are chosen for a
 * download-management UI:
 *
 * * ``staleTime: 30_000`` — server data is considered fresh for 30 seconds.
 *   Individual queries override this where a longer or shorter window is
 *   appropriate (``sites`` uses 5 minutes, ``version`` uses ``Infinity``).
 * * ``gcTime: 300_000`` — unused query data is garbage-collected after five
 *   minutes.  A user navigating between pages of a search should be able to
 *   go back without refetching; a user returning to the app an hour later
 *   should get fresh data.
 * * ``refetchOnWindowFocus: true`` — TanStack Query's default, kept because
 *   it is the right behaviour for every data source in this app (sites,
 *   library, manga metadata) except those that override it explicitly.
 * * ``retry: 3 on 5xx/network, 0 on 4xx`` — a retry budget that helps with
 *   transient failures (rate limits, brief upstream outages, network blips)
 *   without wasting requests on errors that retrying cannot fix (bad
 *   credentials, missing resources, validation failures).  ``429`` in
 *   particular is excluded because retrying it makes rate limiting worse,
 *   not better.
 * * ``mutations.retry: false`` — mutations are not idempotent by default
 *   (creating a download twice creates two tasks), so they must never be
 *   retried automatically.
 */
function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
        retry: (failureCount, error) => {
          if (error instanceof NexusApiError) {
            // Client errors (4xx) are deterministic — retrying them will
            // produce the same result and just delays the error display.
            // The only exception is 408 (Request Timeout), which is a
            // transport-level condition masquerading as a 4xx.
            // 429 (RATE_LIMIT_EXCEEDED) is *also* excluded from the 4xx
            // mass exclusion above — retrying it would amplify the
            // rate-limit window instead of riding it out.  It is folded
            // into the exclusion automatically because it is a 4xx.
            if (
              error.status >= 400 &&
              error.status < 500 &&
              error.status !== 408
            ) {
              return false;
            }
          }
          return failureCount < 3;
        },
      },
      mutations: {
        retry: false,
      },
    },
  });
}

// ───────────────────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Normalise a ``next-themes`` value to one of the three theme strings
 * accepted by sonner's :class:`Toaster`.
 *
 * ``next-themes`` returns ``string | undefined``; sonner accepts
 * ``"light" | "dark" | "system" | undefined``.  Mapping anything unexpected
 * to ``"system"`` keeps the toast visible with a coherent colour scheme
 * even if a future ``next-themes`` configuration adds a fourth theme.
 */
function normalizeTheme(
  theme: string | undefined,
): "light" | "dark" | "system" {
  if (theme === "light" || theme === "dark") {
    return theme;
  }
  return "system";
}

/**
 * Resolve a site id to its display name using the cached sites list.
 *
 * The WebSocket payloads (``site.health.changed``, ``download.completed``,
 * ``download.failed``) carry only the site's ``site_id`` — the backend
 * resolver that would add a ``site_name`` to those payloads is not yet part
 * of the wire contract.  Rather than showing raw ids in user-facing
 * toasts ("mangadex is down"), this helper reads the already-cached sites
 * list and returns the human-readable name.  When the site is not in the
 * cache (first load, race), the raw id is returned as a graceful fallback
 * — a toast with the id is still better than no toast at all.
 *
 * A future iteration should add ``site_name`` to both payloads, at which
 * point this helper becomes a no-op and can be removed.  The function is
 * kept small and side-effect free so the removal is a single-call-site
 * change.
 */
function resolveSiteName(
  queryClient: QueryClient,
  siteId: string,
): string {
  const cached = queryClient.getQueryData<SiteListResponse>(["sites"]);
  if (cached !== undefined) {
    for (const site of cached.items) {
      if (site.id === siteId) {
        return site.name;
      }
    }
  }
  return siteId;
}

// ───────────────────────────────────────────────────────────────────────────
// Root provider
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`Providers`.
 */
export interface ProvidersProps {
  /** The application tree.  Passed through unchanged. */
  children: ReactNode;
}

/**
 * Root provider — wraps every route in the App Router.
 *
 * See the module docstring for the full contract.  This component is
 * rendered exactly once by ``src/app/layout.tsx``; every route beneath it
 * inherits its context.
 */
export function Providers({ children }: ProvidersProps) {
  // ``useState`` with a lazy initialiser — runs the factory exactly once,
  // on first mount, and keeps the same client instance across re-renders.
  // Using ``useMemo`` here would be wrong: ``useMemo`` is a hint, not a
  // guarantee, and TanStack Query's cache must survive every re-render.
  const [queryClient] = useState(createQueryClient);

  // Token revision — used as a ``key`` on the WebSocket bridge so that a
  // token change forces the bridge to re-mount and re-evaluate the
  // connection with the new token.  See ``useTokenRevision`` below.
  const tokenRevision = useTokenRevision();

  // Rehydrate persisted user settings on first client mount.  This is the
  // point at which ``localStorage``-persisted preferences become active —
  // before this effect runs, the store returns its defaults (which is what
  // the server rendered, so there is no hydration mismatch).  A failure
  // here (corrupt localStorage, quota exceeded) must not crash the app;
  // the user simply gets the defaults and the error is visible in the
  // browser console for debugging.
  useEffect(() => {
    void rehydrateSettings().catch((err: unknown) => {
      // eslint-disable-next-line no-console
      console.error("Failed to rehydrate settings:", err);
    });
  }, []);

  return (
    <ThemeProvider
      // Tailwind's ``darkMode: ["class"]`` and the ``.dark`` / ``.light``
      // selectors in ``globals.css`` both key off the ``class`` attribute.
      attribute="class"
      // Respect the OS preference unless the user explicitly overrides it.
      defaultTheme="system"
      enableSystem
      // Prevent the brief CSS transition flicker that would otherwise
      // occur when the theme changes (React re-renders before the
      // browser has applied the new class to ``<html>``).
      disableTransitionOnChange
      // A project-scoped key so that the theme does not collide with
      // another Next.js app served from the same domain (rare, but cheap
      // insurance).
      storageKey="nexusdl-theme"
    >
      <QueryClientProvider client={queryClient}>
        {/*
          The WebSocket bridge is keyed on the token revision so that a
          login, logout or token refresh forces it to re-mount with a fresh
          token.  It does not render any UI of its own.
        */}
        <WebSocketBridge key={tokenRevision} />
        <ThemedToaster />
        {children}
        {process.env.NODE_ENV === "development" && (
          <ReactQueryDevtools
            initialIsOpen={false}
            buttonPosition="bottom-left"
          />
        )}
      </QueryClientProvider>
    </ThemeProvider>
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Themed toaster
// ───────────────────────────────────────────────────────────────────────────

/**
 * Sonner's :class:`Toaster`, styled to follow the active theme.
 *
 * Extracted into its own component because :func:`useTheme` must be called
 * from a **descendant** of ``<ThemeProvider>``, not from the same component
 * that renders it.
 *
 * The position (bottom-right), duration (4 seconds), and rich colours are
 * project-wide choices — a page that wants a toast with a different look
 * should not need to configure the Toaster itself.  Per-toast overrides
 * remain possible through sonner's ``toast.success(..., { ... })`` options.
 */
function ThemedToaster() {
  const { theme } = useTheme();
  return (
    <Toaster
      theme={normalizeTheme(theme)}
      position="bottom-right"
      richColors
      closeButton
      duration={4_000}
      // Visible above the sidebar's z-index and above any dialog backdrop.
      // Sonner's own stacking ensures multiple toasts do not overlap.
      expand
      visibleToasts={4}
    />
  );
}

// ───────────────────────────────────────────────────────────────────────────
// WebSocket bridge
// ───────────────────────────────────────────────────────────────────────────

/**
 * Cross-cutting WebSocket handler — no UI, only side effects.
 *
 * Mounted once per token revision.  On mount:
 *
 * 1. Calls :func:`~../hooks/useWebSocket.useWebSocket` with an ``onEvent``
 *    callback that reacts to the events **not** already handled by the
 *    hook's internal download-store wiring.
 * 2. That is the entire bridge — the hook does the actual connection
 *    management, and the download store is updated internally.
 *
 * Events handled here (with the reason each one needs a handler beyond the
 * hook's built-in wiring):
 *
 * * ``library.updated`` — invalidates the ``["library"]`` query and the
 *   affected manga's query and chapter list.  The hook does not know about
 *   React Query; without this handler, adding a manga from another tab
 *   would leave this tab showing stale data.
 * * ``site.health.changed`` — invalidates the ``["sites"]`` query (so the
 *   site list badge refreshes) and shows a toast on down → up transitions
 *   (and up → down, but only once per transition — see the ``previous_health``
 *   guard).  The site id is resolved to its display name via the cached
 *   sites list so the toast reads "MangaDex" rather than "mangadex".
 * * ``download.completed`` — a success toast with the manga title.  The
 *   title is read from the downloads store, which the hook's built-in
 *   wiring has already updated by the time this callback runs.
 * * ``download.failed`` — an error toast, same source for the title.
 *
 * Every other event type is handled by the hook's internal download wiring
 * and does not need a bridge-level handler.
 */
function WebSocketBridge() {
  const queryClient = useQueryClient();

  const onEvent = useCallback(
    (event: NexusEvent) => {
      switch (event.type) {
        case "library.updated": {
          const payload = event.payload as LibraryUpdatedPayload;
          void queryClient.invalidateQueries({ queryKey: ["library"] });
          void queryClient.invalidateQueries({
            queryKey: ["manga", payload.manga_id],
          });
          void queryClient.invalidateQueries({
            queryKey: ["manga-chapters", payload.manga_id],
          });
          break;
        }

        case "site.health.changed": {
          const payload = event.payload as SiteHealthChangedPayload;
          void queryClient.invalidateQueries({ queryKey: ["sites"] });

          // Resolve the display name from the cached sites list so the
          // toast reads "MangaDex" rather than "mangadex".  A missing
          // site (first load, race) falls back to the raw id, which is
          // still better than no toast at all.
          const siteName = resolveSiteName(queryClient, payload.site_id);

          // Only notify on transitions that the user can act on:
          //  * down → not down: site came back online (positive)
          //  * not down → down: site went down (negative)
          // Transitions to or from "unknown" or "degraded" are more
          // noise than signal — they resolve on their own within a
          // health-check cycle and would produce a stream of toasts
          // during transient blips.
          if (
            payload.current_health === "healthy" &&
            payload.previous_health !== "healthy"
          ) {
            toast.success(`${siteName} is back online`);
          } else if (
            payload.current_health === "down" &&
            payload.previous_health !== "down"
          ) {
            toast.error(`${siteName} is down`);
          }
          break;
        }

        case "download.completed": {
          const payload = event.payload as DownloadCompletedPayload;
          // The hook's internal download wiring runs first (it is
          // registered as a typed subscription, which fires before the
          // any-handler callback), so the store is up to date here.
          const task = useDownloadsStore.getState().tasks[payload.task_id];
          const title = task?.manga_title ?? "Download";
          toast.success(`${title} is complete`);
          break;
        }

        case "download.failed": {
          const payload = event.payload as DownloadFailedPayload;
          const task = useDownloadsStore.getState().tasks[payload.task_id];
          const title = task?.manga_title ?? "Download";
          toast.error(`${title} failed`, {
            description: payload.error.message,
          });
          break;
        }

        // Every other event type is handled by the hook's internal
        // download wiring (task progress, chapter transitions, etc.)
        // and does not need a bridge-level handler.
      }
    },
    [queryClient],
  );

  // The hook is called for its side effect only — the connection itself
  // and the download-store wiring live inside it.  Its returned object is
  // intentionally discarded: the provider does not render connection
  // status.  Individual pages that need it (the downloads page's header
  // pill) call ``useWebSocket()`` themselves — the singleton ensures they
  // observe the same connection.
  useWebSocket({ onEvent });

  return null;
}

// ───────────────────────────────────────────────────────────────────────────
// Token tracking
// ───────────────────────────────────────────────────────────────────────────

/**
 * Track the access token in ``localStorage`` and return a revision number
 * that changes whenever the token does.
 *
 * Why this exists
 * ---------------
 * The API client stores tokens in ``localStorage`` so that they survive a
 * reload.  When it refreshes an expired token transparently (on a 401), it
 * writes the new token to ``localStorage`` — but the browser does not fire
 * a ``storage`` event for writes performed by the same tab, and no React
 * state changes as a result.  Without this hook, the WebSocket would keep
 * using the pre-refresh token until the user navigated to a page that
 * re-mounted the entire provider tree (which, in practice, means a reload).
 *
 * What it does
 * ------------
 * * Reads the current token once on mount.
 * * Polls ``localStorage`` every :data:`TOKEN_POLL_INTERVAL_MS`.
 * * Also listens for ``storage`` events (cross-tab changes, instant) and
 *   ``visibilitychange`` events (tab focus, instant for the common case of
 *   returning to the app after a session refresh in another tab).
 * * When the token changes, bumps the returned revision number **and**
 *   always calls :func:`~../lib/ws.resetWebSocketClient` so the singleton
 *   is torn down before the bridge re-mounts.  Doing this on *every*
 *   change (not just logout) also covers the "logout + login as another
 *   user in under two seconds" case, where the token goes from user A to
 *   user B without ever passing through ``null``.
 *
 * The hook is **self-correcting**: a token change detected by the poll and
 * a token change detected by a ``storage`` event both converge on the same
 * ``next !== currentToken`` comparison, so duplicate notifications are
 * impossible by construction.
 *
 * When the auth store is delivered, this hook is **deleted**: the auth
 * store will hold the token as reactive state, and the provider will use
 * that state directly.  Nothing else in the codebase depends on this hook
 * — it is entirely local to this file.
 */
function useTokenRevision(): number {
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    // Read the current value once, outside of any poll, so that the effect
    // never bumps the revision on the first mount (which would cause a
    // spurious re-mount of the WebSocket bridge).
    let currentToken: string | null;
    try {
      currentToken = window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
    } catch {
      currentToken = null;
    }

    const check = (): void => {
      let next: string | null;
      try {
        next = window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
      } catch {
        next = null;
      }
      if (next === currentToken) {
        return;
      }

      // The token changed — always tear down the singleton.  This covers
      // not only logout (auth → null) but also token refresh
      // (old-token → new-token) and account switching
      // (user-A-token → user-B-token, which never passes through null).
      // The bridge re-mounts via ``key={revision}`` and reconnects with
      // the new token, so the reset must happen *before* that re-mount.
      resetWebSocketClient();

      currentToken = next;
      setRevision((value) => value + 1);
    };

    const onStorage = (event: StorageEvent): void => {
      // ``event.key === null`` fires on ``clear()`` — treat it as a
      // potential token change too.
      if (
        event.key === ACCESS_TOKEN_STORAGE_KEY ||
        event.key === null
      ) {
        check();
      }
    };

    const onVisibility = (): void => {
      if (document.visibilityState === "visible") {
        check();
      }
    };

    const intervalId = window.setInterval(check, TOKEN_POLL_INTERVAL_MS);
    window.addEventListener("storage", onStorage);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("storage", onStorage);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  return revision;
}
