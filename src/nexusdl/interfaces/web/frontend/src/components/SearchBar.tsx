"use client";

/**
 * SearchBar — the input + submit control for aggregated site search.
 *
 * Designed to plug directly into :func:`../hooks/useSearch`::
 *
 *     const search = useSearch();
 *     <SearchBar
 *       value={search.query}
 *       onChange={search.setQuery}
 *       onSubmit={search.submit}
 *       isLoading={search.isFetching}
 *     />
 *
 * Behaviours
 * ----------
 * * **Enter submits, Escape clears.**  The input is wrapped in a
 *   ``<form role="search">``, so pressing Enter fires the form's submit
 *   handler — the standard, accessible way to trigger search from a text
 *   input.  Escape clears the input when it is non-empty and is a no-op
 *   otherwise, so it still bubbles up to close a surrounding dialog when
 *   there is nothing to clear.
 * * **Submit requires non-blank input.**  The submit button is disabled
 *   while the trimmed query is empty.  Pressing Enter with a blank input is
 *   silently ignored — this mirrors the backend's behaviour of rejecting
 *   empty search requests with ``VALIDATION_ERROR`` and avoids an
 *   unnecessary round-trip.
 * * **Loading state.**  When ``isLoading`` is true, the submit button
 *   shows a spinner and the label "Searching…", and becomes disabled.  The
 *   input itself stays editable so the user can prepare the next query
 *   while the current one is in flight.
 * * **Clear button.**  Rendered only when the input is non-empty and the
 *   bar is not fully ``disabled``.  Clicking it fires ``onChange("")`` —
 *   the caller's :func:`useSearch` ``setQuery`` — without submitting.
 *
 * Accessibility
 * -------------
 * * ``<form role="search" aria-label="Site search form">`` — a search
 *   landmark with a unique, descriptive name.  The landmark is labelled
 *   distinctly from the input ("Site search form" vs "Search sites") so
 *   that a screen reader announces two distinct regions on the page:
 *   the search *form* and the search *field* inside it.
 * * The input has an ``sr-only`` ``<label>``, so its accessible name is
 *   "Search sites" regardless of the placeholder.
 * * The clear button has ``aria-label="Clear search"`` and a matching
 *   ``title`` — the icon itself is ``aria-hidden``.
 * * ``enterKeyHint="search"`` sets the mobile keyboard's return key to
 *   "Search" instead of "Return", matching the action.
 * * ``aria-busy`` on the form is set while ``isLoading`` is true, so
 *   assistive technology announces the in-flight state without the caller
 *   having to wire ``aria-live`` themselves.
 *
 * Refs and focus
 * --------------
 * The input is exposed via :func:`forwardRef`, so a caller that wants to
 * programmatically focus it (e.g. after opening a command palette) can
 * pass a ref.  ``autoFocus`` is also supported as a convenience, though it
 * is rarely the right choice outside of dedicated search pages.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  The bar is built from
 * ``<form>``, ``<input>`` and ``<button>`` primitives styled with the
 * Tailwind tokens defined in ``src/app/globals.css``, plus ``lucide-react``
 * icons and the project's ``cn`` helper.  When the shadcn primitives land,
 * the visual style can be upgraded in a single self-contained edit — the
 * public API does not change.
 */
import { Loader2, Search, X } from "lucide-react";
import {
  forwardRef,
  useId,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { cn } from "../lib/utils";

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`SearchBar`.
 */
export interface SearchBarProps {
  /** Current text in the input.  Controlled — never mutate directly. */
  value: string;

  /**
   * Called on every keystroke with the new input value.  The caller
   * typically passes :func:`useSearch`'s ``setQuery``.
   */
  onChange: (value: string) => void;

  /**
   * Called when the user submits the form — pressing Enter, clicking the
   * submit button, or (on a mobile keyboard) hitting the "Search" key.
   * **Not** called with a blank trimmed value.  The caller typically
   * passes :func:`useSearch`'s ``submit``.
   */
  onSubmit: () => void;

  /** Placeholder text shown when the input is empty. */
  placeholder?: string;

  /**
   * When ``true``, the submit button shows a spinner, its label switches
   * to "Searching…", and it is disabled.  The input stays editable.
   */
  isLoading?: boolean;

  /**
   * When ``true``, both the input and the submit button are disabled.
   * Used while the page is not yet ready (e.g. sites list still loading).
   * Distinct from :attr:`isLoading`, which only blocks submission.
   */
  disabled?: boolean;

  /** Focus the input on mount.  Rarely appropriate outside dedicated pages. */
  autoFocus?: boolean;

  /**
   * Additional Tailwind classes merged onto the form via :func:`cn`.
   * Typically used to control width: ``className="max-w-xl"``.
   */
  className?: string;
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * The input + submit control for aggregated site search.
 *
 * See the module docstring for the design, behaviours and accessibility
 * rationale.
 *
 * @example Standard usage on the search page:
 *   const search = useSearch();
 *   <SearchBar
 *     value={search.query}
 *     onChange={search.setQuery}
 *     onSubmit={search.submit}
 *     isLoading={search.isFetching}
 *     className="max-w-2xl"
 *   />
 *
 * @example Inside a command-palette overlay, with an external trigger:
 *   const inputRef = useRef<HTMLInputElement>(null);
 *   useEffect(() => { inputRef.current?.focus(); }, []);
 *   <SearchBar ref={inputRef} value={q} onChange={setQ} onSubmit={run} />
 */
export const SearchBar = forwardRef<HTMLInputElement, SearchBarProps>(
  function SearchBar(
    {
      value,
      onChange,
      onSubmit,
      placeholder = "Search across every site…",
      isLoading = false,
      disabled = false,
      autoFocus = false,
      className,
    },
    ref,
  ) {
    const inputId = useId();
    const trimmed = value.trim();
    const canSubmit = trimmed.length > 0 && !isLoading && !disabled;
    const showClear = value.length > 0 && !disabled;

    const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
      event.preventDefault();
      // The ``canSubmit`` guard covers ``isLoading`` and ``disabled`` too,
      // but spelling out the early return documents the intent: while a
      // search is in flight, pressing Enter does nothing.  The alternative
      // (re-submitting the same query) would race the in-flight request
      // and produce a flash of stale results.
      if (!canSubmit) {
        return;
      }
      onSubmit();
    };

    const handleKeyDown = (
      event: KeyboardEvent<HTMLInputElement>,
    ): void => {
      // Escape clears a non-empty input.  When the input is empty, we let
      // the event bubble so a surrounding dialog can close.  When the
      // input is non-empty, ``preventDefault`` stops the browser from
      // doing anything else (Safari's native "clear on Escape" for
      // ``type="search"`` inputs, which we suppress via
      // ``[&::-webkit-search-cancel-button]:appearance-none``) but also
      // stops propagation — a dialog layered above will require a second
      // Escape press to close, which is the standard search-bar behaviour
      // users expect.
      if (event.key === "Escape" && value.length > 0) {
        event.preventDefault();
        onChange("");
      }
    };

    return (
      <form
        role="search"
        // Distinct from the input's ``aria-label`` ("Search sites") so
        // that a screen reader hears two distinct regions: the search
        // *form* and the search *field* inside it.
        aria-label="Site search form"
        aria-busy={isLoading}
        onSubmit={handleSubmit}
        className={cn("flex w-full items-center gap-2", className)}
      >
        <div className="relative flex-1">
          <Search
            className={cn(
              "pointer-events-none absolute left-3 top-1/2 h-4 w-4",
              "-translate-y-1/2 text-muted-foreground",
            )}
            aria-hidden
          />

          <label htmlFor={inputId} className="sr-only">
            Search sites
          </label>

          <input
            ref={ref}
            id={inputId}
            type="search"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            autoFocus={autoFocus}
            disabled={disabled}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            enterKeyHint="search"
            className={cn(
              "h-10 w-full rounded-md border bg-background pl-9 pr-9 text-sm",
              "placeholder:text-muted-foreground",
              "transition-colors",
              "focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring focus-visible:ring-offset-2",
              "focus-visible:ring-offset-background",
              "disabled:cursor-not-allowed disabled:opacity-50",
              // Hide the native "clear" affordance on Safari / Edge — we
              // provide our own ``X`` button with a consistent style and a
              // dedicated aria-label.
              "[&::-webkit-search-cancel-button]:appearance-none",
            )}
          />

          {showClear && (
            <button
              type="button"
              onClick={() => onChange("")}
              aria-label="Clear search"
              title="Clear search"
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
          type="submit"
          disabled={!canSubmit}
          className={cn(
            "inline-flex h-10 shrink-0 items-center gap-2 rounded-md px-4",
            "bg-primary text-sm font-medium text-primary-foreground",
            "transition-colors hover:bg-primary/90",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          {isLoading ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              <span>Searching…</span>
            </>
          ) : (
            <>
              <Search className="h-4 w-4" aria-hidden />
              <span>Search</span>
            </>
          )}
        </button>
      </form>
    );
  },
);
