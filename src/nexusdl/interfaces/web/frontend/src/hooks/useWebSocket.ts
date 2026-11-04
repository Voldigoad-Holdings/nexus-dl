"use client";

/**
 * React hook for the ``/ws/v1/events`` WebSocket.
 *
 * The hook is the only place in the frontend where the WebSocket client is
 * (a) constructed, (b) wired into the download store, and (c) turned into
 * React state.  Everything below that — connection management, reconnection
 * backoff, heartbeats, typed dispatch — lives in :mod:`../lib/ws`.  Nothing
 * above it (components, pages, providers) imports the WS client directly;
 * they consume the hook, or ``getWebSocketClient()`` if they need to
 * subscribe to a specific event type beyond the built-in wiring.
 *
 * Responsibilities
 * ----------------
 * 1. **Lifecycle** — connect on mount (unless disabled), disconnect on
 *    unmount only when the hook itself caused the connection to open *and*
 *    no other consumer still needs it.  In practice the client is a
 *    process-wide singleton owned by the app; the hook does **not** close
 *    it on unmount, so React Strict Mode's double-mount and any HMR
 *    re-render do not interrupt the stream.
 * 2. **Store wiring** — subscribes every download event to the matching
 *    action on :class:`~../store/downloads.DownloadsStore`.  The wiring is
 *    reference-counted per client instance, so multiple hook consumers do
 *    not double-apply the same event and the wiring is torn down exactly
 *    when the last consumer unmounts or the token changes.
 * 3. **Settings gate** — reads ``web_events_enabled`` from the settings
 *    store.  When the user disables live events, the hook disconnects the
 *    client and reports ``state: "idle"``.  Enabling it again reconnects.
 * 4. **React state** — exposes the current connection state and the last
 *    fatal error as React state so that UI (badges, retry buttons, toasts)
 *    can react to changes.
 * 5. **Generic event fan-out** — an optional ``onEvent`` callback is
 *    invoked for every event, including types not handled by the download
 *    wiring.  Used by the app's provider to invalidate React Query caches
 *    on ``library.updated`` and to surface ``site.health.changed`` toasts.
 *
 * Token source
 * ------------
 * The hook needs a JWT to open the WebSocket.  Until the auth store is
 * delivered, the token is read from ``localStorage["nexusdl_access_token"]``
 * — the same key that :mod:`../lib/api` uses.  Callers may override this by
 * passing ``token`` explicitly; the app's provider will do so once the auth
 * store exists.  A missing token is **not** an error: the hook reports
 * ``state: "idle"``, ``isDisabled: true`` and does not connect.
 *
 * Callback identity
 * -----------------
 * ``onEvent`` is captured through a ref, so passing a new function on
 * every render does **not** re-subscribe the client.  This matters because
 * React Query cache invalidation handlers are typically recreated on every
 * render; without the ref, each render would tear down and rebuild the
 * subscription, losing events in the gap.
 *
 * The hook does **not** return the client itself.  Advanced callers that
 * need per-event subscriptions (rather than the catch-all ``onEvent``)
 * should call :func:`getWebSocketClient` directly with the same token —
 * the singleton ensures they receive the same instance the hook is using.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  getWebSocketClient,
  type ConnectionState,
  type NexusEvent,
  type NexusWebSocketClient,
  type NexusWebSocketError,
} from "../lib/ws";
import { useDownloadsStore } from "../store/downloads";
import { useSettingsStore } from "../store/settings";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * ``localStorage`` key for the access token.
 *
 * Matches :data:`nexusdl.interfaces.web.frontend.src.lib.api`'s private
 * constant.  Kept as a local constant (rather than imported) because the
 * API module does not export it; when the auth store is delivered, this
 * fallback path is removed entirely and the key lives in exactly one place.
 */
const ACCESS_TOKEN_STORAGE_KEY = "nexusdl_access_token";

// ───────────────────────────────────────────────────────────────────────────
// Types
// ───────────────────────────────────────────────────────────────────────────

/**
 * Options accepted by :func:`useWebSocket`.
 */
export interface UseWebSocketOptions {
  /**
   * JWT to authenticate the WebSocket.  When omitted, the hook reads it
   * from ``localStorage["nexusdl_access_token"]``.  Passing the token
   * explicitly is what the auth store will do once delivered, so that a
   * token refresh triggers a reconnect without waiting for a
   * ``localStorage`` poll.
   */
  token?: string;

  /**
   * Callback invoked for every event, **after** the built-in download
   * wiring has run.  Ordering is guaranteed: the download store is updated
   * before this callback fires, so a handler that reads the store sees
   * the post-event state.
   *
   * The callback may receive events whose type is not in
   * :type:`~../lib/ws.EventTypeMap` — this is intentional, so that a
   * handler written today keeps working when the backend adds a new event.
   * Narrow with a ``switch (event.type)`` and a default branch.
   */
  onEvent?: (event: NexusEvent) => void;

  /**
   * Whether to emit ``console.debug`` diagnostics from the underlying
   * client.  Defaults to ``false``.  Passed straight through to
   * :class:`~../lib/ws.NexusWebSocketClient`.
   */
  debug?: boolean;
}

/**
 * The value returned by :func:`useWebSocket`.
 */
export interface UseWebSocketReturn {
  /**
   * Current connection state, or ``"idle"`` when the hook is disabled
   * (no token, or ``web_events_enabled === false``).
   */
  state: ConnectionState;

  /** Convenience flag — ``state === "connected"``. */
  isConnected: boolean;

  /**
   * ``true`` when the hook is not maintaining a connection because there
   * is no token, or the user disabled live events.  Callers can use this
   * to render a "Live updates disabled" notice rather than a "Connecting…"
   * spinner.
   */
  isDisabled: boolean;

  /**
   * The most recent fatal error (auth failure, retry exhaustion, URL
   * construction failure), or ``null`` when no fatal error has occurred
   * since the last successful ``connect()``.
   */
  lastError: NexusWebSocketError | null;

  /**
   * Re-open the connection.
   *
   * No-op when the hook is disabled.  When the client is in a failed or
   * closed state, this resets the backoff counter and the ``lastError``
   * before attempting again — it is the equivalent of the client's own
   * ``connect()``.
   */
  reconnect: () => void;

  /**
   * Close the connection and stop reconnection attempts.
   *
   * This affects the process-wide singleton, so **every** consumer of the
   * WebSocket is disconnected — including the app's provider.  Intended
   * for an explicit "Disable live updates" action; normal unmount does not
   * disconnect (see the module docstring).
   */
  disconnect: () => void;
}

// ───────────────────────────────────────────────────────────────────────────
// Token helper
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return the access token from ``localStorage``, or ``null``.
 *
 * Returns ``null`` in non-browser environments (SSR, tests) and whenever
 * ``localStorage`` access throws (privacy mode, blocked storage).  A
 * missing token is a normal state — the app can function without live
 * events.
 */
function readStoredToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const token = window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
    if (token === null || token.length === 0) {
      return null;
    }
    return token;
  } catch {
    return null;
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Download-event wiring (reference-counted per client)
// ───────────────────────────────────────────────────────────────────────────
//
// Wiring is per **client instance**, not per hook, so that:
//
//  * Two hook consumers do not double-apply the same event.
//  * A token change (which replaces the client) tears down the old wiring
//    and installs a fresh one on the new client.
//  * React Strict Mode's mount → unmount → mount cycle does not leave a
//    dangling subscription on a stale client.
//
// The wiring is kept in a WeakMap keyed by the client so that a client
// that has been discarded (token change, reset) can be garbage-collected
// once its wiring's refcount drops to zero.

interface DownloadWiring {
  unsubscribe: () => void;
  refCount: number;
}

const downloadWirings = new WeakMap<NexusWebSocketClient, DownloadWiring>();

/**
 * Aggregate multiple unsubscribe functions into a single one.
 *
 * Every underlying unsubscribe is invoked exactly once, even when the
 * aggregator itself is called more than once — each underlying function
 * already has that property, so calling them in sequence is safe.
 */
function combineUnsubscribes(...unsubs: Array<() => void>): () => void {
  return () => {
    for (const unsub of unsubs) {
      unsub();
    }
  };
}

/**
 * Install (or bump the refcount of) the download-event wiring for *client*.
 *
 * Returns an unsubscribe that decrements the refcount and, at zero,
 * removes the wiring from the client and from the WeakMap.  The caller is
 * expected to invoke the returned function exactly once on cleanup.
 */
function acquireDownloadWiring(
  client: NexusWebSocketClient,
): () => void {
  let wiring = downloadWirings.get(client);
  if (wiring === undefined) {
    const unsubs: Array<() => void> = [
      client.subscribe("download.started", (payload) => {
        useDownloadsStore.getState().upsertTask(payload.task);
      }),
      client.subscribe("download.progress", (payload) => {
        useDownloadsStore
          .getState()
          .updateProgress(payload.task_id, payload.progress);
      }),
      client.subscribe("download.completed", (payload) => {
        useDownloadsStore
          .getState()
          .markCompleted(payload.task_id, payload.result);
      }),
      client.subscribe("download.failed", (payload) => {
        useDownloadsStore
          .getState()
          .markFailed(payload.task_id, payload.error);
      }),
      client.subscribe("download.paused", (payload) => {
        useDownloadsStore.getState().markPaused(payload.task_id);
      }),
      client.subscribe("download.resumed", (payload) => {
        useDownloadsStore.getState().markResumed(payload.task_id);
      }),
      client.subscribe("download.cancelled", (payload) => {
        useDownloadsStore.getState().markCancelled(payload.task_id);
      }),
      client.subscribe("chapter.started", (payload) => {
        useDownloadsStore
          .getState()
          .updateChapterStarted(
            payload.task_id,
            payload.chapter_id,
            payload.chapter_number,
          );
      }),
      client.subscribe("chapter.completed", (payload) => {
        useDownloadsStore
          .getState()
          .updateChapterCompleted(
            payload.task_id,
            payload.chapter_id,
            payload.chapter_number,
            payload.output_path,
          );
      }),
      client.subscribe("chapter.failed", (payload) => {
        useDownloadsStore
          .getState()
          .updateChapterFailed(
            payload.task_id,
            payload.chapter_id,
            payload.chapter_number,
            payload.error,
          );
      }),
    ];
    wiring = {
      unsubscribe: combineUnsubscribes(...unsubs),
      refCount: 0,
    };
    downloadWirings.set(client, wiring);
  }

  wiring.refCount += 1;

  return () => {
    const current = downloadWirings.get(client);
    if (current === undefined) {
      return;
    }
    current.refCount -= 1;
    if (current.refCount <= 0) {
      current.unsubscribe();
      downloadWirings.delete(client);
    }
  };
}

// ───────────────────────────────────────────────────────────────────────────
// Hook
// ───────────────────────────────────────────────────────────────────────────

/**
 * Subscribe the app to the backend WebSocket.  See the module docstring
 * for the full contract.
 *
 * @example Standard wiring (once, in the app's provider):
 *   useWebSocket({
 *     onEvent: (event) => {
 *       switch (event.type) {
 *         case "library.updated":
 *           queryClient.invalidateQueries({ queryKey: ["library"] });
 *           break;
 *         case "site.health.changed":
 *           toast.info(`Site ${event.payload.site_id} is now ${event.payload.current_health}`);
 *           break;
 *       }
 *     },
 *   });
 *
 * @example A component that only needs the connection status:
 *   const { isConnected, lastError } = useWebSocket();
 */
export function useWebSocket(
  options: UseWebSocketOptions = {},
): UseWebSocketReturn {
  const token = options.token ?? readStoredToken();
  const webEventsEnabled = useSettingsStore((s) => s.web_events_enabled);
  const debug = options.debug ?? false;

  const [clientState, setClientState] = useState<ConnectionState>("idle");
  const [lastError, setLastError] = useState<NexusWebSocketError | null>(
    null,
  );

  // ``onEvent`` lives in a ref so that a caller passing a freshly-created
  // callback on every render does not tear down and rebuild the
  // subscription.
  const onEventRef = useRef(options.onEvent);
  useEffect(() => {
    onEventRef.current = options.onEvent;
  }, [options.onEvent]);

  const isDisabled =
    !webEventsEnabled || token === null || token.length === 0;

  useEffect(() => {
    if (isDisabled) {
      // No connection is maintained when disabled.  Any previously wired
      // client was already torn down by the previous effect's cleanup.
      setClientState("idle");
      setLastError(null);
      return;
    }

    // ``token`` is guaranteed non-null here by the ``isDisabled`` guard.
    const liveToken = token as string;
    const client = getWebSocketClient({ token: liveToken, debug });

    // Sync initial state, in case the client is already connected from a
    // previous mount of a sibling hook (singleton reuse).
    setClientState(client.state);
    setLastError(client.lastError);

    const unsubState = client.onStateChange((next) => {
      setClientState(next);
      // ``lastError`` is only populated when the client transitions to
      // ``"failed"``; reading it on every state change is cheap and keeps
      // the two fields in lockstep.
      setLastError(client.lastError);
    });

    const unsubDownloadWiring = acquireDownloadWiring(client);

    const unsubOnEvent = client.onEvent((event) => {
      onEventRef.current?.(event);
    });

    // Trigger connection.  If the client is already connected (singleton
    // reuse), this is a no-op.
    client.connect();

    return () => {
      unsubState();
      unsubDownloadWiring();
      unsubOnEvent();
    };
  }, [token, isDisabled, debug]);

  const reconnect = useCallback(() => {
    if (isDisabled) {
      return;
    }
    const liveToken = token as string;
    getWebSocketClient({ token: liveToken }).connect();
  }, [isDisabled, token]);

  const disconnect = useCallback(() => {
    if (isDisabled) {
      return;
    }
    const liveToken = token as string;
    getWebSocketClient({ token: liveToken }).disconnect();
  }, [isDisabled, token]);

  return {
    state: isDisabled ? "idle" : clientState,
    isConnected: !isDisabled && clientState === "connected",
    isDisabled,
    lastError: isDisabled ? null : lastError,
    reconnect,
    disconnect,
  };
}
