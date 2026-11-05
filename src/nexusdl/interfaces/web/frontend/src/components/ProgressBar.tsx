"use client";

/**
 * ProgressBar — a determinate or indeterminate progress indicator.
 *
 * Two visual modes
 * ----------------
 * * **Determinate** (`value` is a number) — the fill spans `value / max`
 *   percent of the track, animated with a 300 ms width transition so that
 *   rapid progress updates feel smooth rather than jittery.
 * * **Indeterminate** (`value` is `null`) — a pulsing full-width fill
 *   communicates "busy, but progress is not measurable".  Used while a
 *   task has not yet reported any progress, and as a defensive fallback
 *   when the caller cannot compute a percentage (e.g. `bytes_total` is
 *   zero because the server did not send `Content-Length`).
 *
 * Sizing and colour
 * -----------------
 * * Three heights via `size`: `"sm"` (4 px), `"md"` (8 px), `"lg"` (12 px).
 * * Four fill colours via `variant`: `"default"` (primary), `"success"`
 *   (emerald), `"warning"` (amber), `"danger"` (red).  `"default"` is the
 *   right choice for a neutral "in progress" bar; the semantic colours are
 *   for cases where the status is meaningful (a download that reached 100 %
 *   should be `"success"`, one that failed at 73 % should be `"danger"`).
 *
 * Why no `"auto"` variant
 * -----------------------
 * It is tempting to derive the colour from the percentage (`< 30 %` red,
 * `> 80 %` green, …), but that is the wrong mapping for the vast majority
 * of uses: a download at 10 % is **not** failing — it has simply started.
 * Semantic colour belongs to the state machine (`running` → `"default"`,
 * `done` → `"success"`, `failed` → `"danger"`), not to the raw percentage.
 * Callers map status → variant; the component does not guess.
 *
 * Clamping
 * --------
 * `value` and `max` are coerced through `clampNumber` from
 * `../lib/utils`:
 *
 * * `value < 0` → `0`
 * * `value > max` → `max`
 * * `max <= 0` → treated as indeterminate (division by zero is not
 *   allowed to produce `NaN`)
 * * non-finite values → treated as indeterminate
 *
 * This is what lets the component be dropped onto a `DownloadProgress`
 * without defensive checks: a transient `NaN` or a counter that
 * momentarily overshoots its total renders as a coherent bar, not a broken
 * one.
 *
 * Accessibility
 * -------------
 * The track is a `role="progressbar"` element with the full ARIA
 * value-trio:
 *
 * * `aria-valuemin` = `0`
 * * `aria-valuemax` = `max`
 * * `aria-valuenow` = `value` (omitted when indeterminate, per ARIA)
 * * `aria-valuetext` = `valueText` if provided, else `${percent}%`
 * * `aria-label` = `ariaLabel` if provided, else `label`
 * * `aria-busy` = `true` when indeterminate
 *
 * The percentage text shown next to a visible label is purely visual —
 * assistive technology hears the same value through `aria-valuenow` /
 * `aria-valuetext`.  This avoids the double-announcement that would occur
 * if the text were also in the accessible name.
 *
 * When `valueText` is not provided and no `label` is set, the progressbar
 * has no accessible name.  That is acceptable inside a `DownloadItem`
 * card where the surrounding content names it ("Chapter 42 — 73 %"), but
 * callers in less obvious contexts should pass `ariaLabel`.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * `components/ui/*` is not yet part of the tree.  The bar is a pair of
 * `<div>` elements styled with the Tailwind tokens defined in
 * `src/app/globals.css`.  When a shadcn `Progress` primitive lands, the
 * visual style can be upgraded in a single self-contained edit — the
 * public API does not change.
 */
import { clampNumber, cn } from "../lib/utils";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * Track and fill heights for each size.
 *
 * Both the track and the fill use the same height, so the fill always
 * occupies the full vertical space of the track — the alternative (a fill
 * thinner than the track) looks like a "loading line" rather than a
 * progress bar and is not what callers expect from this API.
 */
const SIZE_CLASSES = {
  sm: "h-1",
  md: "h-2",
  lg: "h-3",
} as const;

/**
 * Fill colour for each variant.
 *
 * Uses the project's semantic tokens where possible (`bg-primary`) and
 * hard-codes the three status colours that have no Tailwind token in
 * `src/app/globals.css`.  A future design pass that introduces those
 * tokens would replace `bg-emerald-500` with `bg-success`, etc., in one
 * place.
 */
const VARIANT_CLASSES = {
  default: "bg-primary",
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  danger: "bg-red-500",
} as const;

// ───────────────────────────────────────────────────────────────────────────
// Types
// ───────────────────────────────────────────────────────────────────────────

/** The visual size of the bar. */
export type ProgressBarSize = keyof typeof SIZE_CLASSES;

/** The fill colour of the bar. */
export type ProgressBarVariant = keyof typeof VARIANT_CLASSES;

/**
 * Props accepted by :func:`ProgressBar`.
 */
export interface ProgressBarProps {
  /**
   * The current progress, or `null` for an indeterminate bar.
   *
   * When a number, it is clamped into `[0, max]` before rendering.  Pass
   * `null` while a task has not yet reported any measurable progress —
   * the bar then pulses instead of showing a misleading 0 %.
   */
  value: number | null;

  /**
   * The maximum value of the scale.  Defaults to `100` (percentage mode).
   *
   * Set to another value (e.g. `chapters_total`, `bytes_total`) and pass
   * a matching `value` to display a non-percentage bar.  `0` or negative
   * values are treated as "max unknown" and force indeterminate mode.
   */
  max?: number;

  /**
   * Visible label rendered above the bar.  When `ariaLabel` is not set,
   * this string is also used as the progressbar's accessible name.
   */
  label?: string;

  /**
   * Accessible name for the progressbar element.  Overrides `label` for
   * accessibility purposes only — the visible text still uses `label`.
   * Use this when the label is long and a shorter announcement is clearer
   * for screen-reader users.
   */
  ariaLabel?: string;

  /**
   * Human-readable form of the current value, used for both
   * `aria-valuetext` and the text rendered next to `label`.
   *
   * Typical uses: `"5 of 10 chapters"`, `"3.2 MB of 10 MB"`.  When
   * omitted, the bar renders and announces the raw percentage
   * (`"42%"`).
   */
  valueText?: string;

  /**
   * Whether to display the value text next to the label.  Defaults to
   * `true` when the bar is determinate — a progress bar with no numbers
   * is useful for dashboards but confusing in most UI.  Set to `false`
   * for compact contexts (e.g. inline badges).
   */
  showValue?: boolean;

  /** The fill colour.  Defaults to `"default"` (primary). */
  variant?: ProgressBarVariant;

  /** The bar's thickness.  Defaults to `"md"` (8 px). */
  size?: ProgressBarSize;

  /**
   * Additional Tailwind classes merged onto the root wrapper via
   * :func:`cn`.  The wrapper is the *outer* `<div>` — the element that
   * also contains the label and value text.
   */
  className?: string;
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * A determinate or indeterminate progress bar.  See the module docstring
 * for the full contract.
 *
 * @example Determinate, percentage mode:
 *   <ProgressBar value={42} label="Downloading" />
 *
 * @example Determinate, chapter mode with a custom value label:
 *   <ProgressBar
 *     value={5}
 *     max={10}
 *     label="Berserk"
 *     valueText="5 of 10 chapters"
 *   />
 *
 * @example Status-driven colour, in a DownloadItem:
 *   const variant =
 *     task.status === "done"    ? "success"
 *     : task.status === "failed" ? "danger"
 *     : task.status === "paused" ? "warning"
 *     : "default";
 *   <ProgressBar
 *     value={percent}
 *     variant={variant}
 *     label={`Chapter ${task.progress.current_chapter_number ?? "—"}`}
 *     size="sm"
 *   />
 *
 * @example Indeterminate while the task is being initialised:
 *   <ProgressBar value={null} label="Preparing download" />
 */
export function ProgressBar({
  value,
  max = 100,
  label,
  ariaLabel,
  valueText,
  showValue = true,
  variant = "default",
  size = "md",
  className,
}: ProgressBarProps) {
  // -------------------------------------------------------------------
  // Determine mode and normalise the value.
  // -------------------------------------------------------------------
  //
  // Indeterminate when:
  //  * ``value`` is ``null`` (explicit indeterminate request);
  //  * ``value`` or ``max`` is not a finite number;
  //  * ``max`` is not strictly positive (division would be undefined).
  const isIndeterminate =
    value === null ||
    !Number.isFinite(value) ||
    !Number.isFinite(max) ||
    max <= 0;

  const clampedValue = isIndeterminate
    ? 0
    : clampNumber(value, 0, max);

  const percent = isIndeterminate ? 0 : (clampedValue / max) * 100;

  // The rounded percentage shown in the UI.  ``Math.round`` (not
  // ``Math.floor``) is correct here — a bar that has visually reached
  // "100 %" in width should say "100 %", not "99 %".
  const roundedPercent = Math.round(percent);

  // -------------------------------------------------------------------
  // Build the display / aria text.
  // -------------------------------------------------------------------
  const displayValue = valueText ?? `${roundedPercent}%`;
  const ariaValueText = isIndeterminate ? undefined : (valueText ?? `${roundedPercent}%`);
  const accessibleLabel = ariaLabel ?? label;

  const showHeader = label !== undefined || (showValue && !isIndeterminate);

  return (
    <div className={cn("w-full", className)}>
      {showHeader && (
        <div className="mb-1.5 flex items-center justify-between gap-2 text-xs">
          {label !== undefined && (
            <span className="truncate text-muted-foreground">{label}</span>
          )}
          {showValue && !isIndeterminate && (
            <span className="shrink-0 font-mono tabular-nums text-foreground">
              {displayValue}
            </span>
          )}
        </div>
      )}

      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={isIndeterminate ? undefined : max}
        aria-valuenow={isIndeterminate ? undefined : clampedValue}
        aria-valuetext={ariaValueText}
        aria-label={accessibleLabel}
        aria-busy={isIndeterminate || undefined}
        className={cn(
          "relative w-full overflow-hidden rounded-full",
          "bg-muted",
          SIZE_CLASSES[size],
        )}
      >
        <div
          style={isIndeterminate ? undefined : { width: `${percent}%` }}
          className={cn(
            "h-full rounded-full transition-[width] duration-300 ease-out",
            VARIANT_CLASSES[variant],
            isIndeterminate && "w-full animate-pulse",
          )}
        />
      </div>
    </div>
  );
}
