"use client";

/**
 * ThemeToggle — cycles the active theme through ``light`` → ``dark`` →
 * ``system`` on each click.
 *
 * Why a cycle and not a dropdown
 * ------------------------------
 * The canonical shadcn/ui ``ThemeToggle`` uses a ``DropdownMenu`` with three
 * explicit options.  That requires the ``DropdownMenu`` primitive from
 * ``components/ui/``, which is not yet part of this tree.  A cycle button
 * achieves the same outcome — every user can reach every theme — with
 * **zero** shadcn dependencies.  It uses:
 *
 *   * ``next-themes`` (already a ``package.json`` dependency);
 *   * ``lucide-react`` icons (already a ``package.json`` dependency);
 *   * the project's ``cn`` helper (already delivered in ``../lib/utils``);
 *   * the Tailwind tokens defined in ``src/app/globals.css``.
 *
 * When ``components/ui/dropdown-menu.tsx`` lands, this component can be
 * upgraded in a single self-contained edit without affecting any of its
 * callers — the public API is just ``<ThemeToggle />`` with an optional
 * ``className``.
 *
 * Hydration
 * ---------
 * ``next-themes`` populates ``theme`` on the client, after ``<ThemeProvider>``
 * has read ``localStorage``.  Rendering the real button during SSR would
 * produce a hydration mismatch (server has no theme).  The component
 * therefore renders a **non-interactive placeholder** with the same
 * footprint until mounted, avoiding both the mismatch and the layout shift
 * that ``return null`` would cause.  The placeholder is a ``<div>``, not a
 * disabled ``<button>``: hiding an interactive element from assistive
 * technology with ``aria-hidden`` is an ARIA violation, whereas a plain
 * spacer is not.
 *
 * Accessibility
 * -------------
 * * The button's ``aria-label`` describes the **action**, not the current
 *   state (``"Switch to dark mode"`` rather than ``"Light theme"``), which
 *   is what a screen reader user needs when activating it.
 * * The icon is ``aria-hidden`` — it is decorative.
 * * ``title`` duplicates the ``aria-label`` so a hovering sighted user
 *   gets the same guidance.
 * * Focus ring uses the project's ``--ring`` token so it works in both
 *   themes without a second set of classes.
 *
 * Relationship to the settings store
 * ----------------------------------
 * The theme is **not** in the settings store.  It is owned by
 * ``next-themes``, persisted under its own ``localStorage`` key
 * (``"theme"``), and driven by ``<ThemeProvider>`` in
 * ``src/app/providers.tsx``.  Duplicating it in the store would create two
 * sources of truth and break ``next-themes``'s SSR-safe hydration — see the
 * note on the ``settings.ts`` store docstring.
 */
import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { cn } from "../lib/utils";

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * The three theme choices, in cycle order.
 *
 * Order matters: clicking from ``light`` goes to ``dark``, from ``dark``
 * to ``system``, from ``system`` back to ``light``.  ``light`` is the
 * first option so that the initial appearance (before ``next-themes``
 * resolves a stored preference) reads naturally as "start at light".
 */
const THEME_CYCLE = ["light", "dark", "system"] as const;

/** A single value of :data:`THEME_CYCLE`. */
type ThemeChoice = (typeof THEME_CYCLE)[number];

/**
 * Per-theme presentation metadata.
 *
 * ``icon`` is the icon shown **for the current theme**; ``nextLabel`` is
 * the accessible label describing **what clicking will do** — i.e. the
 * theme that follows the current one in the cycle.
 *
 * Written as a static table rather than an inline ``switch`` so that
 * adding a fourth theme is a two-line change (one entry here, one entry
 * in :data:`THEME_CYCLE`) rather than a scattered edit.
 */
const THEME_META: Record<
  ThemeChoice,
  { icon: LucideIcon; nextLabel: string }
> = {
  light: { icon: Sun, nextLabel: "Switch to dark mode" },
  dark: { icon: Moon, nextLabel: "Switch to system mode" },
  system: { icon: Monitor, nextLabel: "Switch to light mode" },
};

// ───────────────────────────────────────────────────────────────────────────
// Type guards
// ───────────────────────────────────────────────────────────────────────────

/**
 * Narrow a ``next-themes`` value to a :type:`ThemeChoice`.
 *
 * ``useTheme().theme`` is typed as ``string | undefined`` — it is either
 * one of the three values we registered or ``undefined`` during the
 * pre-hydration render.  Some custom ``ThemeProvider`` configurations
 * could expose a different string (e.g. ``"high-contrast"``); treating it
 * as ``"system"`` is the safest fallback because the resulting button will
 * just cycle into ``"light"`` on the next click, which is a valid theme
 * regardless of what the caller registered.
 */
function asThemeChoice(value: string | undefined): ThemeChoice {
  if (value === "light" || value === "dark" || value === "system") {
    return value;
  }
  return "system";
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`ThemeToggle`.
 *
 * Only ``className`` — every other visual decision is driven by the CSS
 * variables and the theme cycle.
 */
export interface ThemeToggleProps {
  /**
   * Additional Tailwind classes merged onto the button.
   *
   * Passed through :func:`cn` so that the caller's classes win on
   * conflicts — e.g. ``<ThemeToggle className="mr-2" />`` inserts a margin
   * without fighting the button's own ``h-9 w-9``.
   */
  className?: string;
}

/**
 * Cycle the theme through ``light`` → ``dark`` → ``system`` on click.
 *
 * See the module docstring for the design, hydration and accessibility
 * rationale.
 *
 * @example Standard usage in a header or sidebar:
 *   <ThemeToggle />
 *
 * @example With extra spacing in a toolbar:
 *   <ThemeToggle className="mr-2" />
 */
export function ThemeToggle({ className }: ThemeToggleProps) {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  // Defer the real render until after hydration.  ``next-themes`` only
  // knows the theme on the client, so rendering it during SSR would
  // produce a mismatch.
  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    // Non-interactive spacer with the exact footprint of the real button.
    // ``aria-hidden`` is correct here because this element has no function
    // — it is purely layout-stable whitespace.  A ``<div>`` is used rather
    // than a disabled ``<button>`` because hiding a real button from AT is
    // an ARIA violation.
    return (
      <div
        aria-hidden
        className={cn("h-9 w-9", className)}
      />
    );
  }

  const current = asThemeChoice(theme);
  const currentIndex = THEME_CYCLE.indexOf(current);
  const next = THEME_CYCLE[(currentIndex + 1) % THEME_CYCLE.length];

  const { icon: Icon, nextLabel } = THEME_META[current];

  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={nextLabel}
      title={nextLabel}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-md",
        "text-foreground transition-colors",
        "hover:bg-accent hover:text-accent-foreground",
        "focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background",
        "disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </button>
  );
}
