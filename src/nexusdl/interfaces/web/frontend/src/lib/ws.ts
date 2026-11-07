/**
 * WebSocket client for ``/ws/v1/events``.
 *
 * This module is the frontend's single point of contact with the backend's
 * real-time event stream.  It is a **framework-agnostic library** — the
 * React integration lives in :mod:`../hooks/useWebSocket`, and the store
 * wiring (dispatch → ``useDownloadsStore`` actions) lives in the app's
 * provider.  This file's only job is to maintain the connection, apply the
 * reconnection policy, dispatch typed events, and surface the connection
 * state.
 *
 * Contract
 * --------
 * The endpoint is ``/ws/v1/events?token=<jwt>``.  The URL is derived from
 * ``window.location.host`` so the browser only ever sees a same-origin URL
 * — the reverse proxy (Caddy / nginx in production, the Next.js rewrite in
 * development) forwards ``/ws/*`` to the backend.  No hardcoded host, no
 * ``NEXT_PUBLIC_*`` variable, no CORS configuration.
 *
 * Event envelope (backend → client)::
 *
 *     {
 *       "type": "download.progress",
 *       "timestamp": "2026-11-04T10:30:00Z",
 *       "payload": { "task_id": "...", "progress": { ... } },
 *       "id": "550e8400-e29b-41d4-a716-446655440000"
 *     }
 *
 * Heartbeats
 * ----------
 * The client sends ``{"type": "ping"}`` every ``heartbeat.intervalMs`` and
 * treats a pong (or any other message) as evidence of liveness.  When no
 * pong has been observed for ``2 * intervalMs + timeoutMs``, the connection
 * is considered stale: the client closes it with code ``4000`` (a
 * retriable app-specific code) and reconnects with backoff.  The server
 * may also send pings — the client responds with pong unconditionally,
 * regardless of who started the exchange.
 *
 * Reconnection policy
 * -------------------
 * Reconnection is enabled by default with an exponential backoff
 * (initial 1 s, factor 1.5, cap 30 s, ±25 % jitter).  Two categories of
 * close code are distinguished:
 *
 *  * **Fatal** — ``1008`` (Policy Violation) and app-specific ``4001`` –
 *    ``4003`` (auth required / expired / forbidden).  The client stops
 *    retrying, transitions to ``"failed"``, and exposes the reason
 *    through :attr:`NexusWebSocketClient.lastError`.  The app is expected
 *    to react by refreshing the token and calling :meth:`connect` again
 *    (which resets the backoff).
 *
 *  * **Retriable** — everything else, including ``1000`` (Normal Closure,
 *    often a server restart), ``1001`` (Going Away), ``1006`` (Abnormal
 *    Closure, network failure), ``1011`` – ``1014`` (server errors), and
 *    app-specific ``4000``.  The client schedules a reconnect with
 *    backoff.  A successful open resets the backoff counter.
 *
 * A user-initiated :meth:`disconnect` transitions to ``"closed"`` and
 * never triggers a reconnect.
 *
 * **Internal close events are ignored.**  When the client itself closes
 * the socket — on disconnect, on heartbeat timeout, or on a fatal error
 * — the corresponding ``close`` event must not be interpreted as a
 * *server-initiated* close.  The client tracks this with
 * ``_expectingClose``: it is set to ``true`` just before any internal
 * ``ws.close(...)`` and cleared when the ``close`` event fires, which
 * short-circuits ``_handleClose``.  Without this guard, a fatal-error
 * close (code ``4000``, retriable) would immediately schedule a reconnect,
 * resurrecting a client that had just transitioned to ``"failed"``.
 *
 * What this client does **not** do
 * --------------------------------
 * * It does not touch the download store, the settings store, the auth
 *   store or React.  That wiring belongs to
 *   :mod:`../hooks/useWebSocket` and the app's provider.
 * * It does not parse payloads beyond unwrapping the envelope.  Payload
 *   shape is enforced (at compile time) by :type:`EventTypeMap`; runtime
 *   validation of individual payload fields is the responsibility of the
 *   handler that consumes them — the backend is the source of truth and a
 *   malformed payload is a backend bug, not a client-side failure to
 *   recover from.
 * * It does not retry forever.  ``reconnect.maxAttempts`` caps the
 *   retries; once exhausted, the client transitions to ``"failed"`` and
 *   the app decides whether to attempt again (e.g. after the user
 *   navigates back online).
 */
import type {
  ChapterCompletedPayload,
  ChapterFailedPayload,
  ChapterStartedPayload,
  DownloadCancelledPayload,
  DownloadCompletedPayload,
  DownloadFailedPayload,
  DownloadPausedPayload,
  DownloadProgressPayload,
  DownloadResumedPayload,
  DownloadStartedPayload,
} from "../types/download";
import type {
  LibraryUpdatedPayload,
  SiteHealthChangedPayload,
} from "../types/manga";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/** Path of the WebSocket endpoint, relative to ``window.location.host``. */
const WS_ENDPOINT_PATH = "/ws/v1/events";

/**
 * Close codes that must **not** trigger a reconnect.
 *
 * ``1008`` is the WebSocket standard for policy violations — the backend
 * uses it for authentication failures (missing, expired or invalid token).
 * ``4001`` – ``4003`` are app-specific codes reserved for the same category
 * (``auth_required``, ``auth_expired``, ``forbidden``).  Everything else is
 * treated as retriable.
 */
const FATAL_CLOSE_CODES: ReadonlySet<number> = new Set([
  1008, // Policy Violation (WebSocket standard, used for auth)
  4001, // Auth required (app-specific)
  4002, // Auth expired (app-specific)
  4003, // Forbidden (app-specific)
]);

/** Close code the client uses for its own stale-connection closes. */
const CLIENT_STALE_CLOSE_CODE = 4000;

/** Close reason the client uses for its own stale-connection closes. */
const CLIENT_STALE_CLOSE_REASON = "heartbeat timeout";

// ───────────────────────────────────────────────────────────────────────────
// Types
// ───────────────────────────────────────────────────────────────────────────

/**
 * Public connection state.
 *
 * The distinction between ``"closed"`` and ``"failed"`` matters for the
 * UI: ``"closed"`` means the user (or an explicit app action) disconnected
 * and no error should be shown; ``"failed"`` means an unrecoverable error
 * occurred and the UI should offer a retry.
 */
export type ConnectionState =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "closed"
  | "failed";

/**
 * Wire-format event envelope, as delivered by the backend.
 *
 * The defaults on the generics let callers write ``NexusEvent`` when they
 * do not care about narrowing, and ``NexusEvent<"download.progress",
 * DownloadProgressPayload>`` when they do.
 */
export interface NexusEvent<T extends string = string, P = unknown> {
  type: T;
  timestamp: string;
  payload: P;
  id: string;
}

/**
 * Map of every known event type to its payload.
 *
 * This is the single source of truth for typed event dispatch.  Adding a
 * new event to the backend requires adding a single line here — no other
 * change is needed to the client.
 */
export interface EventTypeMap {
  "download.started": DownloadStartedPayload;
  "download.progress": DownloadProgressPayload;
  "download.completed": DownloadCompletedPayload;
  "download.failed": DownloadFailedPayload;
  "download.paused": DownloadPausedPayload;
  "download.resumed": DownloadResumedPayload;
  "download.cancelled": DownloadCancelledPayload;
  "chapter.started": ChapterStartedPayload;
  "chapter.completed": ChapterCompletedPayload;
  "chapter.failed": ChapterFailedPayload;
  "library.updated": LibraryUpdatedPayload;
  "site.health.changed": SiteHealthChangedPayload;
}

/** Union of every known event type. */
export type EventType = keyof EventTypeMap;

/**
 * Discriminated union of every typed event, useful for exhaustive
 * ``switch`` statements in handlers.
 */
export type KnownEvent = {
  [K in EventType]: NexusEvent<K, EventTypeMap[K]>;
}[EventType];

/** Typed event handler for a specific event type. */
export type EventHandler<T extends EventType> = (
  payload: EventTypeMap[T],
  event: NexusEvent<T, EventTypeMap[T]>,
) => void;

/**
 * Catch-all handler for every event, including types not declared in
 * :type:`EventTypeMap`.  Used by logging and by forward-compatible
 * consumers that do not want to break when the backend adds a new event.
 */
export type AnyEventHandler = (event: NexusEvent) => void;

/** Listener for connection state transitions. */
export type StateListener = (state: ConnectionState) => void;

/** Returned by every ``subscribe`` / ``on*`` method — call to unsubscribe. */
export type Unsubscribe = () => void;

/**
 * Options accepted by :class:`NexusWebSocketClient`.
 *
 * Only ``token`` is required; every other field has a sensible default
 * (documented in the class docstring).  The options are resolved once, at
 * construction time; a client is immutable after creation.  To change the
 * token, disconnect and construct a new client (or use
 * :func:`resetWebSocketClient` followed by :func:`getWebSocketClient`).
 */
export interface WebSocketClientOptions {
  /** JWT the backend uses to authenticate the connection. */
  token: string;

  /**
   * Explicit WebSocket URL override.
   *
   * When omitted, the client builds ``ws(s)://{window.location.host}/ws/v1/events?token=…``
   * from the current window.  Set this only in tests or non-browser
   * environments where ``window`` is unavailable.
   */
  url?: string;

  /** Reconnection policy overrides. */
  reconnect?: {
    /** Enable auto-reconnect.  Default: ``true``. */
    enabled?: boolean;
    /** Initial retry delay in ms.  Default: ``1000``. */
    initialDelayMs?: number;
    /** Maximum retry delay in ms.  Default: ``30000``. */
    maxDelayMs?: number;
    /** Backoff multiplier.  Default: ``1.5``. */
    factor?: number;
    /** Jitter amplitude as a fraction of the delay (0–1).  Default: ``0.25``. */
    jitterFactor?: number;
    /**
     * Maximum consecutive retry attempts before transitioning to
     * ``"failed"``.  ``0`` means "unlimited".  Default: ``10``.
     */
    maxAttempts?: number;
  };

  /** Heartbeat policy overrides. */
  heartbeat?: {
    /** Enable heartbeats.  Default: ``true``. */
    enabled?: boolean;
    /** Ping interval in ms.  Default: ``30000``. */
    intervalMs?: number;
    /** Extra tolerance beyond ``2 * intervalMs`` before declaring a timeout.  Default: ``10000``. */
    timeoutMs?: number;
  };

  /** Enable ``console.debug`` logging for connection lifecycle events.  Default: ``false``. */
  debug?: boolean;
}

// ───────────────────────────────────────────────────────────────────────────
// Error
// ───────────────────────────────────────────────────────────────────────────

/**
 * Error thrown by the client, or exposed through
 * :attr:`NexusWebSocketClient.lastError`.
 *
 * Carries the close code and reason when the error originated from a
 * server close, so that the app can distinguish auth failures (which
 * require a token refresh) from transient network failures (which just
 * need a retry).
 *
 * ``cause`` is stored on the instance directly when the running engine
 * does not support ``ErrorOptions.cause`` (Node < 16.9, Safari < 15).  The
 * explicit assignment produces the same property on engines that do
 * support the option, so the class is portable across runtimes.
 */
export class NexusWebSocketError extends Error {
  /** WebSocket close code, or ``null`` when the error is not close-related. */
  public readonly closeCode: number | null;
  /** Server-supplied close reason, or ``null``. */
  public readonly closeReason: string | null;
  /** The original exception, when the error originated below the WS layer. */
  public override readonly cause: unknown;

  constructor(
    message: string,
    options: {
      closeCode?: number | null;
      closeReason?: string | null;
      cause?: unknown;
    } = {},
  ) {
    super(message);
    this.name = "NexusWebSocketError";
    this.closeCode = options.closeCode ?? null;
    this.closeReason = options.closeReason ?? null;
    this.cause = options.cause;
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Type guards
// ───────────────────────────────────────────────────────────────────────────

/**
 * Return ``true`` when *value* looks like a Nexus event envelope.
 *
 * Deliberately structural and lenient: the goal is to filter out obvious
 * garbage (non-objects, missing fields) before dispatch, not to enforce a
 * strict schema.  Payload shape is enforced by :type:`EventTypeMap` at the
 * type level, and the backend is the source of truth for runtime shape.
 */
function isNexusEvent(value: unknown): value is NexusEvent {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const v = value as Record<string, unknown>;
  return (
    typeof v.type === "string" &&
    typeof v.timestamp === "string" &&
    typeof v.id === "string" &&
    "payload" in v
  );
}

// ───────────────────────────────────────────────────────────────────────────
// Client
// ───────────────────────────────────────────────────────────────────────────

/**
 * The WebSocket client.
 *
 * See the module docstring for the connection, reconnection, heartbeat and
 * error-handling policies.  The class is not final — subclasses may override
 * :meth:`_buildUrl` to change URL derivation, :meth:`_shouldReconnect` to
 * customise the retry policy, or :meth:`_dispatchEvent` to add per-event
 * logging — but this is discouraged outside of tests.  Site- or app-specific
 * logic belongs in the hook that owns the client, not here.
 */
export class NexusWebSocketClient {
  // ─────────────────────────────────────────────────────────────────────
  // Internal state
  // ─────────────────────────────────────────────────────────────────────

  private readonly _options: ResolvedOptions;
  private _state: ConnectionState = "idle";
  private _lastError: NexusWebSocketError | null = null;
  private _ws: WebSocket | null = null;
  private _userDisconnected = false;
  /**
   * Set just before any **internal** ``ws.close(...)`` call and cleared
   * when the corresponding ``close`` event fires.
   *
   * Distinguishes the client closing its own socket (disconnect, heartbeat
   * timeout, fatal error) from the server initiating the close.  Without
   * this, a fatal-error close (code 4000, retriable) would be interpreted
   * by :meth:`_handleClose` as a server-initiated close and would schedule
   * a reconnect — resurrecting a client that had just transitioned to
   * ``"failed"``.
   */
  private _expectingClose = false;
  private _reconnectAttempts = 0;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private _lastInboundAt = 0;
  private readonly _handlers = new Map<string, Set<EventHandler<EventType>>>();
  private readonly _anyHandlers = new Set<AnyEventHandler>();
  private readonly _stateHandlers = new Set<StateListener>();

  constructor(options: WebSocketClientOptions) {
    this._options = resolveOptions(options);
  }

  // ─────────────────────────────────────────────────────────────────────
  // Public properties
  // ─────────────────────────────────────────────────────────────────────

  /** Current connection state. */
  public get state(): ConnectionState {
    return this._state;
  }

  /**
   * The most recent fatal error, if any.
   *
   * Set when the client transitions to ``"failed"``; cleared on the next
   * :meth:`connect` call **and** on every successful :meth:`_handleOpen`.
   * The app inspects this field to decide whether to refresh the token
   * (``closeCode === 1008`` or ``4001`` / ``4002``) or to show a generic
   * error message (any other fatal code).
   */
  public get lastError(): NexusWebSocketError | null {
    return this._lastError;
  }

  /** The token this client was constructed with. */
  public get token(): string {
    return this._options.token;
  }

  // ─────────────────────────────────────────────────────────────────────
  // Connection lifecycle
  // ─────────────────────────────────────────────────────────────────────

  /**
   * Open the connection, or reconnect after a previous failure.
   *
   * No-op when the client is already ``"connecting"`` or ``"connected"``
   * — calling ``connect()`` twice in a row does not open a second
   * connection.  When the previous state was ``"failed"`` or ``"closed"``,
   * this resets the backoff counter and clears :attr:`lastError` before
   * attempting again.
   */
  public connect(): void {
    if (this._state === "connecting" || this._state === "connected") {
      return;
    }

    this._userDisconnected = false;
    this._reconnectAttempts = 0;
    this._lastError = null;
    this._open();
  }

  /**
   * Close the connection and stop any pending reconnect.
   *
   * Idempotent.  Safe to call when the client is ``"idle"``.  After this
   * call the state is ``"closed"`` and will remain so until :meth:`connect`
   * is called again.
   */
  public disconnect(): void {
    this._userDisconnected = true;
    this._clearReconnectTimer();
    this._stopHeartbeat();

    const ws = this._ws;
    this._ws = null;

    if (ws !== null) {
      this._expectingClose = true;
      try {
        ws.close(1000, "client disconnect");
      } catch (err) {
        // ``ws.close`` can throw if the socket is already closing — that is
        // a benign race, not an error.  Clear the flag ourselves because
        // no ``close`` event will fire in that case.
        this._expectingClose = false;
        this._debug("Error during disconnect:", err);
      }
    }

    this._setState("closed");
  }

  // ─────────────────────────────────────────────────────────────────────
  // Subscriptions
  // ─────────────────────────────────────────────────────────────────────

  /**
   * Subscribe to a specific event type.
   *
   * The handler receives the payload first, then the full event envelope
   * (useful when the caller needs ``timestamp`` or ``id``).  Multiple
   * handlers may be registered for the same type; they are invoked in the
   * order they were added.  A handler that throws does **not** affect the
   * other handlers — the error is caught and logged.
   *
   * Returns an unsubscribe function.  Idempotent — calling it more than
   * once is safe.
   */
  public subscribe<T extends EventType>(
    type: T,
    handler: EventHandler<T>,
  ): Unsubscribe {
    let set = this._handlers.get(type);
    if (set === undefined) {
      set = new Set();
      this._handlers.set(type, set);
    }
    // The cast is safe because the only way to add to a typed set is through
    // this method, which is itself typed.  ``EventHandler<T>`` and
    // ``EventHandler<EventType>`` are structurally identical at runtime.
    set.add(handler as EventHandler<EventType>);

    return () => {
      const current = this._handlers.get(type);
      if (current === undefined) {
        return;
      }
      current.delete(handler as EventHandler<EventType>);
      if (current.size === 0) {
        this._handlers.delete(type);
      }
    };
  }

  /**
   * Subscribe to every event, including types not declared in
   * :type:`EventTypeMap`.
   *
   * Used by logging, by the app's provider (to wire events into the
   * download store), and by forward-compatible consumers that want to
   * observe new events without a client upgrade.
   */
  public onEvent(handler: AnyEventHandler): Unsubscribe {
    this._anyHandlers.add(handler);
    return () => {
      this._anyHandlers.delete(handler);
    };
  }

  /**
   * Subscribe to connection state transitions.
   *
   * The handler is **not** invoked immediately with the current state —
   * use :attr:`state` for that.  This is deliberate: subscribing to state
   * changes should not, by itself, cause a re-render or an imperative
   * side effect, so the handler only fires on transitions.
   */
  public onStateChange(handler: StateListener): Unsubscribe {
    this._stateHandlers.add(handler);
    return () => {
      this._stateHandlers.delete(handler);
    };
  }

  // ─────────────────────────────────────────────────────────────────────
  // Open / close internals
  // ─────────────────────────────────────────────────────────────────────

  private _open(): void {
    // Defensive: if a previous ``_open`` left a socket behind (which should
    // not happen given the state guard below, but a future refactor could
    // introduce one), close it before opening a new one.  Without this,
    // listeners would accumulate on a zombie socket.
    if (this._ws !== null) {
      const stale = this._ws;
      this._ws = null;
      this._expectingClose = true;
      try {
        stale.close(1000, "replaced");
      } catch (err) {
        this._expectingClose = false;
        this._debug("Error closing stale WebSocket before open:", err);
      }
    }

    this._setState(
      this._reconnectAttempts === 0 ? "connecting" : "reconnecting",
    );

    let url: string;
    try {
      url = this._buildUrl();
    } catch (err) {
      this._fail(
        new NexusWebSocketError(
          `Cannot build WebSocket URL: ${
            err instanceof Error ? err.message : String(err)
          }`,
          { cause: err },
        ),
      );
      return;
    }

    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      // Constructing a WebSocket can throw if the URL is syntactically
      // invalid — a programming error, but handled the same way as a
      // connection failure so the client cannot get stuck in a bad state.
      this._fail(
        new NexusWebSocketError(
          `Failed to construct WebSocket: ${
            err instanceof Error ? err.message : String(err)
          }`,
          { cause: err },
        ),
      );
      return;
    }

    ws.addEventListener("open", () => this._handleOpen());
    ws.addEventListener("message", (event) => this._handleMessage(event.data));
    ws.addEventListener("close", (event) =>
      this._handleClose(event.code, event.reason),
    );
    ws.addEventListener("error", () => {
      // The browser fires ``error`` before ``close``; the close handler is
      // where reconnection is actually decided.  Log for diagnostics only.
      this._debug("WebSocket transport error");
    });

    this._ws = ws;
  }

  private _handleOpen(): void {
    if (this._ws === null) {
      // The socket was closed between ``new WebSocket`` and the ``open``
      // event — nothing to do.
      return;
    }

    this._reconnectAttempts = 0;
    this._lastInboundAt = Date.now();
    // A successful open is the strongest possible evidence that any
    // previous fatal error is now resolved.  Leaving ``lastError`` set
    // would make a "live" connection render with a "Disconnected" badge.
    this._lastError = null;
    this._setState("connected");
    this._startHeartbeat();
    this._debug("WebSocket connected");
  }

  private _handleClose(code: number, reason: string): void {
    const wasExpected = this._expectingClose;
    this._expectingClose = false;
    this._ws = null;
    this._stopHeartbeat();

    if (wasExpected) {
      // The client closed this socket itself (disconnect, heartbeat
      // timeout, fatal error, or replacement before ``_open``).  Whatever
      // state transition the caller already made is final; do not
      // reschedule a reconnect.
      return;
    }

    if (this._userDisconnected) {
      // Defensive: ``disconnect()`` sets this flag and clears ``_ws``
      // before calling ``close``, but a close event fired before the flag
      // took effect can still reach here.  Do nothing.
      return;
    }

    if (FATAL_CLOSE_CODES.has(code)) {
      this._fail(
        new NexusWebSocketError(
          `WebSocket closed with fatal code ${code}: ${
            reason || "(no reason)"
          }`,
          { closeCode: code, closeReason: reason || null },
        ),
      );
      return;
    }

    this._debug(
      `WebSocket closed with retriable code ${code}: ${
        reason || "(no reason)"
      }`,
    );
    this._scheduleReconnect();
  }

  private _fail(error: NexusWebSocketError): void {
    this._lastError = error;
    this._clearReconnectTimer();
    this._stopHeartbeat();
    this._setState("failed");
    if (this._ws !== null) {
      const ws = this._ws;
      this._ws = null;
      // Mark the close as expected so that ``_handleClose`` — which will
      // be triggered by this close — does not interpret code ``4000`` as
      // a retriable server-close and schedule a reconnect.  This was the
      // bug that resurrected a "failed" client.
      this._expectingClose = true;
      try {
        ws.close(CLIENT_STALE_CLOSE_CODE, CLIENT_STALE_CLOSE_REASON);
      } catch (err) {
        this._expectingClose = false;
        this._debug("Error closing failed WebSocket:", err);
      }
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Reconnect
  // ─────────────────────────────────────────────────────────────────────

  private _scheduleReconnect(): void {
    if (!this._options.reconnect.enabled) {
      this._fail(
        new NexusWebSocketError(
          "Connection lost and reconnection is disabled.",
        ),
      );
      return;
    }

    if (
      this._options.reconnect.maxAttempts > 0 &&
      this._reconnectAttempts >= this._options.reconnect.maxAttempts
    ) {
      this._fail(
        new NexusWebSocketError(
          `Connection lost; gave up after ${this._reconnectAttempts} ` +
            `attempts.`,
        ),
      );
      return;
    }

    this._setState("reconnecting");

    const baseDelay = Math.min(
      this._options.reconnect.initialDelayMs *
        Math.pow(this._options.reconnect.factor, this._reconnectAttempts),
      this._options.reconnect.maxDelayMs,
    );
    const jitter =
      baseDelay *
      this._options.reconnect.jitterFactor *
      (Math.random() * 2 - 1);
    const delay = Math.max(0, baseDelay + jitter);

    this._reconnectAttempts += 1;
    this._debug(
      `Scheduling reconnect in ${Math.round(delay)} ms ` +
        `(attempt ${this._reconnectAttempts})`,
    );

    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (this._state === "reconnecting" && !this._userDisconnected) {
        this._open();
      }
    }, delay);
  }

  private _clearReconnectTimer(): void {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Heartbeat
  // ─────────────────────────────────────────────────────────────────────

  private _startHeartbeat(): void {
    if (!this._options.heartbeat.enabled) {
      return;
    }
    this._stopHeartbeat();
    this._lastInboundAt = Date.now();

    this._heartbeatTimer = setInterval(() => {
      if (this._state !== "connected") {
        return;
      }

      const elapsed = Date.now() - this._lastInboundAt;
      const threshold =
        this._options.heartbeat.intervalMs * 2 +
        this._options.heartbeat.timeoutMs;

      if (elapsed > threshold) {
        this._debug(
          `Heartbeat stale after ${elapsed} ms; closing to trigger reconnect.`,
        );
        const ws = this._ws;
        if (ws !== null) {
          this._ws = null;
          // Mark the close as expected: the state machine should observe
          // a single, retriable disconnect from the caller's point of
          // view, not a raw server-close.  ``_handleClose`` will see
          // ``_expectingClose`` and short-circuit — we schedule the
          // reconnect explicitly here instead.
          this._expectingClose = true;
          try {
            ws.close(CLIENT_STALE_CLOSE_CODE, CLIENT_STALE_CLOSE_REASON);
          } catch (err) {
            this._expectingClose = false;
            this._debug("Error closing stale WebSocket:", err);
          }
          this._scheduleReconnect();
        }
        return;
      }

      this._send({ type: "ping" });
    }, this._options.heartbeat.intervalMs);
  }

  private _stopHeartbeat(): void {
    if (this._heartbeatTimer !== null) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Message handling
  // ─────────────────────────────────────────────────────────────────────

  private _handleMessage(data: unknown): void {
    this._lastInboundAt = Date.now();

    if (typeof data !== "string") {
      // Binary frames are not part of the protocol.  Log and drop.
      this._debug("Ignoring non-text WebSocket frame.");
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch (err) {
      this._debug("Invalid JSON from WebSocket:", data, err);
      return;
    }

    if (typeof parsed !== "object" || parsed === null) {
      this._debug("Non-object WebSocket message:", parsed);
      return;
    }

    const message = parsed as Record<string, unknown>;
    const type = message.type;

    if (typeof type !== "string") {
      this._debug("Message without a type field:", parsed);
      return;
    }

    // Ping/pong are handled before envelope validation because the backend
    // may send them in a minimal shape (``{"type": "ping"}``).
    if (type === "ping") {
      this._send({ type: "pong" });
      return;
    }
    if (type === "pong") {
      // Arrival time already updated above; nothing else to do.
      return;
    }

    if (!isNexusEvent(parsed)) {
      this._debug(`Malformed event envelope for type ${type}:`, parsed);
      return;
    }

    this._dispatchEvent(parsed);
  }

  private _dispatchEvent(event: NexusEvent): void {
    // Narrow the widened ``NexusEvent`` back to its typed form.
    //
    // ``NexusEvent<T, P>`` defaults ``P`` to ``unknown``, and the
    // ``_handlers`` map is keyed only by the event-type string, so the
    // type relationship between ``event.type`` and the payload shape is
    // lost at this point.  That relationship was guaranteed when the
    // handler was registered in :meth:`subscribe` — the only way to add
    // to a set at ``type`` is through ``subscribe<T>(type, handler)``,
    // which enforces ``EventHandler<T>`` at the call site.  Re-asserting
    // the relationship here is safe and restores the compiler's ability
    // to check the handler invocation below.
    const typedEvent = event as NexusEvent<EventType, EventTypeMap[EventType]>;

    const handlers = this._handlers.get(event.type);
    if (handlers !== undefined) {
      for (const handler of handlers) {
        try {
          handler(typedEvent.payload, typedEvent);
        } catch (err) {
          // A throwing handler must not take down the client or prevent
          // other handlers from running.  Log and move on.  ``console.error``
          // (rather than ``_debug``) is intentional: a throwing handler is
          // a developer bug, not a diagnostic, and should be loud even in
          // production.
          console.error(
            `[NexusWS] Handler for "${event.type}" threw:`,
            err,
          );
        }
      }
    }

    for (const handler of this._anyHandlers) {
      try {
        handler(event);
      } catch (err) {
        console.error("[NexusWS] Any-event handler threw:", err);
      }
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Sending
  // ─────────────────────────────────────────────────────────────────────

  private _send(message: object): void {
    const ws = this._ws;
    // ``ws.readyState === WebSocket.OPEN`` would reference the global
    // ``WebSocket`` class, which is undefined in Node without ``ws`` or
    // in an SSR context.  We only reach this line with a non-null ``ws``
    // (which implies a WebSocket global existed when it was constructed),
    // but comparing to the literal ``1`` is robust regardless.
    if (ws === null || ws.readyState !== 1 /* OPEN */) {
      return;
    }
    try {
      ws.send(JSON.stringify(message));
    } catch (err) {
      this._debug("Failed to send WebSocket message:", err);
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // State transitions
  // ─────────────────────────────────────────────────────────────────────

  private _setState(next: ConnectionState): void {
    if (this._state === next) {
      return;
    }
    this._state = next;
    for (const handler of this._stateHandlers) {
      try {
        handler(next);
      } catch (err) {
        console.error("[NexusWS] State handler threw:", err);
      }
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // URL and logging
  // ─────────────────────────────────────────────────────────────────────

  private _buildUrl(): string {
    if (this._options.url !== undefined) {
      return this._options.url;
    }
    if (typeof window === "undefined") {
      throw new NexusWebSocketError(
        "Cannot construct a WebSocket URL outside a browser environment. " +
          "Pass an explicit `url` option when running in tests or Node.",
      );
    }
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const base = `${scheme}//${window.location.host}`;
    // The token is URL-encoded so that a JWT containing ``+``, ``/`` or
    // ``=`` does not corrupt the query string.  The URL is never logged
    // by this client, only by the app if it chooses to.
    return `${base}${WS_ENDPOINT_PATH}?token=${encodeURIComponent(
      this._options.token,
    )}`;
  }

  private _debug(...args: unknown[]): void {
    if (this._options.debug) {
      // ``console.debug`` is preserved by the production build (see
      // ``next.config.js``'s ``compiler.removeConsole.exclude``) but is
      // only emitted when the option is enabled, so it stays off by
      // default.
      console.debug("[NexusWS]", ...args);
    }
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Options resolution
// ───────────────────────────────────────────────────────────────────────────

/** Fully-resolved options — every field is set. */
interface ResolvedOptions {
  token: string;
  url: string | undefined;
  reconnect: {
    enabled: boolean;
    initialDelayMs: number;
    maxDelayMs: number;
    factor: number;
    jitterFactor: number;
    maxAttempts: number;
  };
  heartbeat: {
    enabled: boolean;
    intervalMs: number;
    timeoutMs: number;
  };
  debug: boolean;
}

function resolveOptions(options: WebSocketClientOptions): ResolvedOptions {
  return {
    token: options.token,
    url: options.url,
    reconnect: {
      enabled: options.reconnect?.enabled ?? true,
      initialDelayMs: options.reconnect?.initialDelayMs ?? 1_000,
      maxDelayMs: options.reconnect?.maxDelayMs ?? 30_000,
      factor: options.reconnect?.factor ?? 1.5,
      jitterFactor: options.reconnect?.jitterFactor ?? 0.25,
      maxAttempts: options.reconnect?.maxAttempts ?? 10,
    },
    heartbeat: {
      enabled: options.heartbeat?.enabled ?? true,
      intervalMs: options.heartbeat?.intervalMs ?? 30_000,
      timeoutMs: options.heartbeat?.timeoutMs ?? 10_000,
    },
    debug: options.debug ?? false,
  };
}

// ───────────────────────────────────────────────────────────────────────────
// Singleton accessors
// ───────────────────────────────────────────────────────────────────────────
//
// The app needs exactly one WebSocket connection — one per browser tab, one
// per user session.  These helpers make that invariant easy to enforce:
//
//  * ``getWebSocketClient({ token })`` returns the process-wide singleton,
//    creating it on first call.  If the token differs from the existing
//    instance's token, the existing instance is disconnected and replaced.
//    This means a token refresh (which produces a new JWT string) always
//    yields a fresh, working client without the caller needing to call
//    ``resetWebSocketClient`` explicitly.
//
//  * ``resetWebSocketClient()`` disconnects the current instance and
//    clears the singleton.  Call it on logout, on test teardown, or when a
//    token rotation is intentionally not handled by ``getWebSocketClient``.
//
// The singleton is module-scoped, so it survives React Strict Mode's
// double-mount and HMR re-renders within a single page session — which is
// what we want (a WebSocket should not be opened, closed and reopened just
// because React remounted a component).

let _singleton: NexusWebSocketClient | null = null;

/**
 * Return the process-wide WebSocket client, creating it on first use.
 *
 * When *options.token* differs from the current instance's token, the
 * current instance is disconnected and a fresh one is created.  When
 * *options.token* is identical, *options* (including ``reconnect``,
 * ``heartbeat``, ``debug`` and ``url``) is **ignored** — a running client
 * cannot be reconfigured without a reconnect, and silently applying new
 * options would surprise the caller.  Call :func:`resetWebSocketClient`
 * first if you need to change non-token options on a live client.
 *
 * The returned client is **not** automatically connected.  The caller (in
 * practice, :mod:`../hooks/useWebSocket`) is responsible for calling
 * ``connect()``.  This separation keeps the singleton accessor free of
 * side effects beyond instance lifecycle, which makes it safe to call
 * from React render paths.
 */
export function getWebSocketClient(
  options: WebSocketClientOptions,
): NexusWebSocketClient {
  if (_singleton !== null && _singleton.token !== options.token) {
    _singleton.disconnect();
    _singleton = null;
  }
  if (_singleton === null) {
    _singleton = new NexusWebSocketClient(options);
  }
  return _singleton;
}

/**
 * Disconnect the process-wide WebSocket client and clear the singleton.
 *
 * Safe to call when no client exists.  Idempotent — a second call is a
 * no-op.  Synchronous: the returned ``void`` makes the sync contract
 * explicit at every call site (``resetWebSocketClient()``, not
 * ``await resetWebSocketClient()``).
 */
export function resetWebSocketClient(): void {
  if (_singleton !== null) {
    _singleton.disconnect();
    _singleton = null;
  }
}
