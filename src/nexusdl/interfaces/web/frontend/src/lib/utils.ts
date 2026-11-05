/**
 * Universal, side-effect-free utilities for the NexusDL web frontend.
 *
 * This module contains **only** framework-agnostic helpers — no React, no
 * Zustand, no store access, no API calls.  Everything here is a pure
 * function or a constant, which makes it trivially testable and safe to
 * import from anywhere (including server components, Web Workers and
 * Node scripts) without pulling in UI or state dependencies.
 *
 * Categories
 * ----------
 * 1. **Class-name composition** — :func:`cn`, the canonical shadcn/ui
 *    helper.  Every ``components/ui/*`` file depends on it, so it must
 *    keep the exact shadcn signature.
 * 2. **Number formatting** — :func:`formatNumber`, :func:`formatBytes`,
 *    :func:`formatSpeed`.
 * 3. **Duration formatting** — :func:`formatDuration`, :func:`formatEta`.
 * 4. **Date formatting** — :func:`formatDate`, :func:`formatDateTime`,
 *    :func:`formatRelativeTime`.  All three accept an optional
 *    ``Locale`` from ``date-fns`` so the caller (which knows the user's
 *    UI locale) controls the output language.
 * 5. **String helpers** — :func:`truncate`, :func:`getInitials`,
 *    :func:`isNonEmptyString`.
 * 6. **Async helpers** — :func:`sleep`.
 * 7. **Collection helpers** — :func:`groupBy`, :func:`unique`,
 *    :func:`isDefined`.
 * 8. **Math helpers** — :func:`clampNumber`.
 * 9. **Type helpers** — :func:`assertNever`, :func:`isNil`.
 *
 * What is deliberately **not** here
 * ---------------------------------
 * * **Pluralisation / i18n** — handled by the translation layer
 *   (``data/translations/*.json``).  Baking English plural rules into a
 *   util would bypass the i18n system and produce inconsistent output.
 * * **Debounce / throttle** — those are React concerns (a ``useEffect``
 *   with ``setTimeout`` and a ``useRef`` is clearer than a generic
 *   implementation).  Every actual use case in this project is inside a
 *   React hook.
 * * **Retry / backoff** — belongs to the WebSocket client and to the
 *   search hook, where the retry policy is not a utility concern.
 * * **Domain-specific formatting** — e.g. converting a ``DownloadStatus``
 *   to a human label.  That is locale-sensitive and lives next to the
 *   consuming component.
 *
 * Locale handling
 * ---------------
 * The number formatters respect the browser's locale by default
 * (``Intl.NumberFormat(undefined, …)``).  The date formatters accept an
 * optional ``locale`` option so the caller can pass
 * ``date-fns/locale/fr``, ``date-fns/locale/de``, etc.  There is **no**
 * automatic mapping from the app's ``UILocale`` to a ``date-fns`` locale
 * object here — doing so would couple this universal module to the
 * frontend's settings store, which is exactly what we avoid.
 */
import { type ClassValue, clsx } from "clsx";
import {
  format as formatDateFns,
  formatDistanceToNow,
  type Locale,
} from "date-fns";
import { twMerge } from "tailwind-merge";

// ───────────────────────────────────────────────────────────────────────────
// Class-name composition
// ───────────────────────────────────────────────────────────────────────────

/**
 * Compose TailwindCSS class names, resolving conflicts and conditionals.
 *
 * This is the **canonical shadcn/ui helper**, copied verbatim from the
 * upstream template because every file in ``components/ui/`` imports it
 * by this exact name and signature.  Do not change its behaviour: doing
 * so would break every shadcn primitive.
 *
 * The composition is:
 *
 * 1. :func:`clsx` — flattens arrays, objects (``{ "text-red": isError }``),
 *    strings and falsy values (``false``, ``null``, ``undefined``) into a
 *    single space-separated string.
 * 2. :func:`twMerge` — resolves **conflicting** Tailwind classes by
 *    keeping the last one, so a caller can override a default without
 *    fighting specificity.
 *
 * Usage::
 *
 *     <div className={cn("p-4 text-sm", isError && "text-red-500", className)} />
 *
 * @example
 * cn("px-2 py-1", "px-4")                 // → "py-1 px-4"
 * cn("text-sm", { "text-red-500": hasError })  // → "text-sm text-red-500"
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// ───────────────────────────────────────────────────────────────────────────
// Number formatting
// ───────────────────────────────────────────────────────────────────────────

/**
 * Format a number with locale-aware grouping.
 *
 * Uses the browser's locale by default, so ``1234567`` renders as
 * ``"1,234,567"`` in en-US and ``"1 234 567"`` in fr-FR without the caller
 * having to know which one applies.
 *
 * Args:
 *   value: The number to format.
 *   locale: Optional BCP 47 locale identifier (``"fr-FR"``, ``"de"``).
 *     When omitted, the browser's default is used.
 *
 * @example
 * formatNumber(1234567)          // → "1,234,567"  (en-US)
 * formatNumber(1234567, "fr-FR") // → "1 234 567"
 */
export function formatNumber(
  value: number,
  locale?: string,
): string {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: 0,
  }).format(value);
}

/** Ordered binary size units, from smallest to largest. */
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

/** Binary multiplier between consecutive byte units (``2 ** 10``). */
const BYTE_STEP = 1024;

/**
 * Format a byte count as a human-readable size.
 *
 * Uses **binary** units (1 KB = 1024 B), matching the convention used by
 * operating systems, browsers and download managers — which is what users
 * expect to see next to a file.  The label is ``"KB"`` / ``"MB"`` / … for
 * readability, even though the correct SI symbol for a binary prefix
 * would be ``"KiB"`` / ``"MiB"``.  Using the correct SI symbols would
 * confuse non-technical users and diverge from every mainstream tool.
 *
 * Formatting rules:
 *
 * * Bytes (``< 1024``) are integers, no decimal part: ``"512 B"``.
 * * KB and above get at most one decimal digit: ``"1.5 MB"``.
 * * A trailing ``".0"`` is stripped: ``"2 MB"`` rather than ``"2.0 MB"``.
 * * Negative values are formatted as ``"0 B"`` — a negative byte count is
 *   always a bug, but returning a valid string is preferable to throwing
 *   from a formatter.
 *
 * @example
 * formatBytes(0)              // → "0 B"
 * formatBytes(512)            // → "512 B"
 * formatBytes(1024)           // → "1 KB"
 * formatBytes(1_500_000)      // → "1.4 MB"
 * formatBytes(2_147_483_648)  // → "2 GB"
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }

  let value = bytes;
  let unitIndex = 0;
  while (value >= BYTE_STEP && unitIndex < BYTE_UNITS.length - 1) {
    value /= BYTE_STEP;
    unitIndex += 1;
  }

  const unit = BYTE_UNITS[unitIndex];
  if (unitIndex === 0) {
    return `${Math.round(value)} ${unit}`;
  }
  // ``toFixed(1)`` always yields one decimal digit; strip ``.0`` so that
  // "1.0 MB" becomes "1 MB".  ``Intl.NumberFormat`` is avoided here to
  // keep the numeric part compact (no thousands separators for values
  // like 1,024 MB, which would look odd next to a unit).
  const formatted = value.toFixed(1).replace(/\.0$/, "");
  return `${formatted} ${unit}`;
}

/**
 * Format a byte-per-second rate as a human-readable speed.
 *
 * Delegates to :func:`formatBytes` and appends the ``"/s"`` suffix.
 * ``formatSpeed(0)`` returns ``"0 B/s"`` rather than an empty string, so
 * a paused download renders a coherent value instead of a blank cell.
 *
 * @example
 * formatSpeed(0)             // → "0 B/s"
 * formatSpeed(524_288)       // → "512 KB/s"
 * formatSpeed(10_485_760)    // → "10 MB/s"
 */
export function formatSpeed(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`;
}

// ───────────────────────────────────────────────────────────────────────────
// Duration formatting
// ───────────────────────────────────────────────────────────────────────────

/**
 * Format a duration in seconds as a compact human-readable string.
 *
 * Uses a cascading format that keeps at most two units, chosen to match
 * what a user needs at a glance:
 *
 * * ``< 60 s``  → ``"45s"``
 * * ``< 1 h``   → ``"1m 30s"``
 * * ``< 1 d``   → ``"1h 5m"``
 * * ``>= 1 d``  → ``"1d 2h"``
 *
 * Seconds are dropped once hours are present because a duration measured
 * in hours does not benefit from second-level precision, and appending
 * them ("1h 5m 3s") makes the column visually noisy.
 *
 * Negative or non-finite inputs yield ``"0s"`` — formatters never throw.
 *
 * @example
 * formatDuration(0)      // → "0s"
 * formatDuration(45)     // → "45s"
 * formatDuration(90)     // → "1m 30s"
 * formatDuration(3665)   // → "1h 1m"
 * formatDuration(90000)  // → "1d 1h"
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "0s";
  }

  const totalSeconds = Math.floor(seconds);
  if (totalSeconds < 60) {
    return `${totalSeconds}s`;
  }

  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) {
    const remainingSeconds = totalSeconds % 60;
    return `${totalMinutes}m ${remainingSeconds}s`;
  }

  const totalHours = Math.floor(totalMinutes / 60);
  if (totalHours < 24) {
    const remainingMinutes = totalMinutes % 60;
    return `${totalHours}h ${remainingMinutes}m`;
  }

  const totalDays = Math.floor(totalHours / 24);
  const remainingHours = totalHours % 24;
  return `${totalDays}d ${remainingHours}h`;
}

/**
 * Format an estimated time remaining (ETA) in seconds.
 *
 * Identical to :func:`formatDuration` except for the ``null`` case: an
 * unknown ETA renders as ``"—"`` (em-dash) rather than ``"0s"``.  The
 * distinction matters in the UI — a task that has just started has an
 * unknown ETA, not a zero-duration one, and the em-dash conveys "not
 * ready yet" without inventing a value.
 *
 * @example
 * formatEta(null)   // → "—"
 * formatEta(0)      // → "0s"
 * formatEta(90)     // → "1m 30s"
 */
export function formatEta(seconds: number | null): string {
  if (seconds === null) {
    return "—";
  }
  return formatDuration(seconds);
}

// ───────────────────────────────────────────────────────────────────────────
// Date formatting
// ───────────────────────────────────────────────────────────────────────────

/**
 * Placeholder returned by date formatters when the input cannot be parsed.
 *
 * Centralised so a future design change (e.g. switching to ``"n/a"`` or a
 * localised placeholder) touches exactly one constant.
 */
const INVALID_DATE_PLACEHOLDER = "—";

/**
 * Coerce a ``string | Date`` into a ``Date``.
 *
 * Returns ``null`` when the value cannot represent a valid date.
 * ``date-fns`` throws on invalid dates, so every public formatter funnels
 * its input through this helper and short-circuits to
 * :data:`INVALID_DATE_PLACEHOLDER` when it returns ``null``.
 */
function toDate(value: string | Date): Date | null {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value !== "string" || value.length === 0) {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/**
 * Format an ISO date as a date-only string, localised.
 *
 * Uses the ``date-fns`` ``"PP"`` token, which resolves to
 * ``"Nov 4, 2026"`` in en-US and ``"4 nov. 2026"`` in fr-FR.
 *
 * Accepts an ISO 8601 string (the wire format) or a ``Date`` object
 * (handy for tests and mocks).  Invalid input yields ``"—"``.
 *
 * @example
 * formatDate("2026-11-04T10:30:00Z")           // → "Nov 4, 2026"
 * formatDate("2026-11-04T10:30:00Z", frLocale) // → "4 nov. 2026"
 */
export function formatDate(
  value: string | Date,
  options: { locale?: Locale } = {},
): string {
  const date = toDate(value);
  if (date === null) {
    return INVALID_DATE_PLACEHOLDER;
  }
  return formatDateFns(date, "PP", { locale: options.locale });
}

/**
 * Format an ISO datetime as a localised date-and-time string.
 *
 * Uses the ``date-fns`` ``"PPp"`` token, which resolves to
 * ``"Nov 4, 2026, 10:30 AM"`` in en-US and ``"4 nov. 2026, 10:30"`` in
 * fr-FR.  The time is rendered in the user's **local** timezone (``Date``
 * always converts to local time when stringified) — which is what users
 * expect for "when was this published" and "when did this finish".
 *
 * Invalid input yields ``"—"``.
 *
 * @example
 * formatDateTime("2026-11-04T10:30:00Z")
 * // → "Nov 4, 2026, 10:30 AM" (approximately, in the user's locale and TZ)
 */
export function formatDateTime(
  value: string | Date,
  options: { locale?: Locale } = {},
): string {
  const date = toDate(value);
  if (date === null) {
    return INVALID_DATE_PLACEHOLDER;
  }
  return formatDateFns(date, "PPp", { locale: options.locale });
}

/**
 * Format an ISO datetime as a relative-time string ("2 minutes ago").
 *
 * Uses ``date-fns``'s :func:`formatDistanceToNow`, which produces natural
 * language that respects the supplied locale.  The ``addSuffix`` option
 * defaults to ``true``, producing "3 hours ago" rather than "3 hours" —
 * which is what every user expects in a download or library list.
 *
 * Invalid input yields ``"—"``.
 *
 * @example
 * formatRelativeTime("2026-11-04T10:28:00Z")  // → "2 minutes ago"
 * formatRelativeTime(iso, { addSuffix: false })  // → "2 minutes"
 * formatRelativeTime(iso, { locale: frLocale })  // → "il y a 2 minutes"
 */
export function formatRelativeTime(
  value: string | Date,
  options: {
    addSuffix?: boolean;
    locale?: Locale;
  } = {},
): string {
  const date = toDate(value);
  if (date === null) {
    return INVALID_DATE_PLACEHOLDER;
  }
  return formatDistanceToNow(date, {
    addSuffix: options.addSuffix ?? true,
    locale: options.locale,
  });
}

// ───────────────────────────────────────────────────────────────────────────
// String helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Truncate a string to *maxLength* characters, appending an ellipsis.
 *
 * The returned string is **at most** *maxLength* characters long,
 * including the ellipsis.  When *text* already fits, it is returned
 * unchanged (no ellipsis appended).  The ellipsis is ``"…"`` (U+2026),
 * not three dots, because it is a single glyph that does not wrap and
 * renders consistently across fonts.
 *
 * Edge cases:
 *
 * * ``maxLength < 1`` → ``""`` (there is no room for anything meaningful).
 * * ``maxLength === 1`` → ``"…"`` (only the ellipsis fits).
 * * Empty input → ``""``.
 *
 * @example
 * truncate("Hello, world!", 10)  // → "Hello, wo…"
 * truncate("Short", 10)          // → "Short"
 * truncate("Hello", 1)           // → "…"
 */
export function truncate(text: string, maxLength: number): string {
  if (maxLength <= 0) {
    return "";
  }
  if (maxLength === 1) {
    return "…";
  }
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
}

/**
 * Return up to two uppercase initials derived from a name.
 *
 * Used as an avatar fallback when a cover or user image is missing.
 * Splits on whitespace, takes the first letter of the first two words, and
 * uppercases them.  Handles common edge cases:
 *
 * * Empty or whitespace-only input → ``"?"`` (a visible placeholder
 *   rather than a blank cell).
 * * Single-word input → the first letter only (``"NexusDL"`` → ``"N"``).
 * * Names containing non-BMP characters (emoji, CJK) — the helper uses
 *   ``Array.from(name)[0]`` rather than ``name[0]`` so surrogate pairs
 *   are treated as a single character.
 *
 * @example
 * getInitials("Nexus DL")     // → "ND"
 * getInitials("NexusDL")      // → "N"
 * getInitials("  jean luc  ") // → "JL"
 * getInitials("")             // → "?"
 */
export function getInitials(name: string): string {
  const trimmed = name.trim();
  if (trimmed.length === 0) {
    return "?";
  }
  const words = trimmed.split(/\s+/u).filter((w) => w.length > 0);
  if (words.length === 0) {
    return "?";
  }
  const firstChar = (word: string): string =>
    Array.from(word)[0] ?? "";
  const first = firstChar(words[0] ?? "");
  const second = words.length > 1 ? firstChar(words[1] ?? "") : "";
  return `${first}${second}`.toUpperCase() || "?";
}

/**
 * Return ``true`` when *value* is a string containing at least one
 * non-whitespace character.
 *
 * Combines the two checks that are almost always performed together —
 * "is this a string?" and "is this non-blank?" — into a single type guard
 * so callers do not need to write ``typeof x === "string" && x.trim() !== ""``
 * at every call site.
 *
 * @example
 * isNonEmptyString("hello")   // → true
 * isNonEmptyString("   ")     // → false
 * isNonEmptyString(null)      // → false
 * isNonEmptyString(42)        // → false
 */
export function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

// ───────────────────────────────────────────────────────────────────────────
// Async helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Resolve after *ms* milliseconds.
 *
 * Used for retry backoff, test pacing, and any other place where an async
 * delay is needed without pulling in a dependency.  Unlike
 * ``setTimeout(fn, ms)``, this returns a ``Promise`` that can be awaited
 * inline.
 *
 * @example
 * await sleep(500);  // wait half a second
 * await retry(() => fetchSomething(), { onRetry: () => sleep(1000) });
 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

// ───────────────────────────────────────────────────────────────────────────
// Collection helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Group the items of *items* into a record keyed by *keyFn(item)*.
 *
 * The order of items within each group matches their order in the input
 * array — this helper does **not** sort.  Callers that want sorted groups
 * should sort the input first (or sort each group afterwards).
 *
 * The return type is ``Record<string, T[]>``.  In practice, only keys
 * present in the input appear in the output; accessing a key that no item
 * produced yields ``undefined`` at runtime despite the type.  This matches
 * the behaviour of ``lodash.groupBy`` and is the pragmatic choice for
 * ``Object.entries(groupBy(...))`` iteration in JSX.
 *
 * @example
 * groupBy([1, 2, 3, 4], (n) => (n % 2 === 0 ? "even" : "odd"))
 * // → { odd: [1, 3], even: [2, 4] }
 *
 * @example
 * const byLanguage = groupBy(sites, (s) => s.language);
 * // → { en: [...], fr: [...] }
 */
export function groupBy<T>(
  items: ReadonlyArray<T>,
  keyFn: (item: T) => string,
): Record<string, T[]> {
  const groups: Record<string, T[]> = {};
  for (const item of items) {
    const key = keyFn(item);
    const existing = groups[key];
    if (existing === undefined) {
      groups[key] = [item];
    } else {
      existing.push(item);
    }
  }
  return groups;
}

/**
 * Return a new array with duplicate values removed.
 *
 * Preserves the order of first occurrences.  Uses ``Set`` for O(n)
 * membership, so this is efficient even for large inputs.  Values are
 * compared with ``SameValueZero`` (the ``Set`` algorithm) — which means
 * ``NaN`` equals ``NaN``, ``+0`` equals ``-0``, and objects are compared
 * by reference.  For deduplicating objects by a key, use
 * :func:`uniqueBy` — which is intentionally **not** provided here, because
 * no current caller needs it and adding unused helpers violates the
 * project's "no dead code" rule.
 *
 * @example
 * unique([1, 2, 2, 3, 1])      // → [1, 2, 3]
 * unique(["a", "b", "a"])      // → ["a", "b"]
 */
export function unique<T>(items: ReadonlyArray<T>): T[] {
  return Array.from(new Set(items));
}

/**
 * Type guard that filters out ``null`` and ``undefined`` from an array.
 *
 * Designed to be passed directly to ``Array.prototype.filter``::
 *
 *     const valid = values.filter(isDefined);  // values: (T | null | undefined)[]
 *     // inferred type of `valid`: T[]
 *
 * Using a plain ``(v) => v !== null && v !== undefined`` inline does not
 * narrow the type because TypeScript cannot see through the lambda — it
 * needs the explicit type predicate signature that this function provides.
 *
 * @example
 * const x: (number | null | undefined)[] = [1, null, 2, undefined, 3];
 * const y = x.filter(isDefined);  // y: number[] = [1, 2, 3]
 */
export function isDefined<T>(value: T | null | undefined): value is T {
  return value !== null && value !== undefined;
}

// ───────────────────────────────────────────────────────────────────────────
// Math helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Clamp *value* into the inclusive range ``[min, max]``.
 *
 * Returns *min* when ``value < min``, *max* when ``value > max``, and
 * *value* otherwise.  When ``min > max`` the range is empty and the
 * function returns *min* — this is the same convention as Python's
 * ``max(min, min(value, max))`` and matches what most call sites expect
 * (a defensive default rather than a thrown exception).
 *
 * Used primarily for progress-bar widths, which must stay in ``[0, 100]``
 * even when a backend sends a slightly-off value due to floating-point
 * rounding.
 *
 * @example
 * clampNumber(50, 0, 100)    // → 50
 * clampNumber(-10, 0, 100)   // → 0
 * clampNumber(150, 0, 100)   // → 100
 * clampNumber(NaN, 0, 100)   // → 0
 */
export function clampNumber(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) {
    return min;
  }
  if (min > max) {
    return min;
  }
  return Math.min(Math.max(value, min), max);
}

// ───────────────────────────────────────────────────────────────────────────
// Type helpers
// ───────────────────────────────────────────────────────────────────────────

/**
 * Exhaustiveness check for discriminated unions.
 *
 * Place a call to this function in the ``default`` branch of a ``switch``
 * over a discriminated union.  When every case is handled, TypeScript
 * narrows the argument to ``never`` and the call compiles.  When a case is
 * missing, TypeScript reports a type error at that call site — turning a
 * potential runtime surprise into a compile-time failure.
 *
 * The function also throws at runtime as a defence in depth: if a
 * discriminated value ever arrives from the wire with an unrecognised tag,
 * the throw makes the failure loud rather than letting the switch fall
 * through silently.
 *
 * @example
 * function label(status: DownloadStatus): string {
 *   switch (status) {
 *     case "pending":   return "Pending";
 *     case "running":   return "Running";
 *     case "paused":    return "Paused";
 *     case "done":      return "Done";
 *     case "failed":    return "Failed";
 *     case "cancelled": return "Cancelled";
 *     default:          return assertNever(status);
 *   }
 * }
 */
export function assertNever(value: never): never {
  throw new Error(
    `Unexpected value in exhaustive switch: ${JSON.stringify(value)}`,
  );
}

/**
 * Type guard that returns ``true`` when *value* is ``null`` or
 * ``undefined``.
 *
 * The complement of :func:`isDefined`, provided separately because some
 * call sites read more naturally as "if it is nil, do X" than as "if it
 * is not defined, do X".  Both predicates narrow identically.
 *
 * @example
 * if (isNil(value)) return;  // value is narrowed to `null | undefined`
 * doSomething(value);        // value is narrowed to the non-nil type
 */
export function isNil(value: unknown): value is null | undefined {
  return value === null || value === undefined;
}
