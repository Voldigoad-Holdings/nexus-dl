"use client";

/**
 * Sidebar — primary navigation for the NexusDL web frontend.
 *
 * The sidebar is the desktop entry point for every top-level route in the
 * App Router tree:
 *
 *  * ``/``          — Home (dashboard)
 *  * ``/search``    — Aggregated search across every enabled site
 *  * ``/downloads`` — Active and finished download tasks
 *  * ``/library``   — Locally tracked manga
 *  * ``/settings``  — User preferences
 *
 * Each nav entry is an icon plus a label, with the label hidden by ``sr-only``
 * when the sidebar is collapsed — this keeps the link's accessible name
 * stable across both visual states, so screen-reader users always hear
 * "Search, link" rather than "link" once the sidebar narrows.
 *
 * Responsive behaviour
 * --------------------
 * The sidebar is ``hidden md:flex``: it is invisible on mobile and appears
 * from the ``md`` breakpoint up.  Mobile navigation is provided by the app's
 * main layout (typically a top bar or a bottom tab strip) — this is the
 * standard split between desktop and mobile navigation and keeps the
 * Sidebar component focused on the one case it can serve well without a
 * shadcn ``Sheet``/``Drawer`` primitive.
 *
 * Collapse behaviour
 * ------------------
 * On desktop the sidebar honours the ``sidebar_collapsed`` user preference
 * from the settings store.  Clicking the toggle at the bottom of the sidebar
 * calls ``toggleSidebar()`` and flips between the two widths:
 *
 *  * ``w-64`` (expanded) — icons and labels.
 *  * ``w-16`` (collapsed) — icons only, centred.
 *
 * The width transition is animated (200 ms, ease-out) so the layout shift
 * feels intentional rather than janky.  The setting is persisted to
 * ``localStorage`` via the settings store, so a reload restores the last
 * state.
 *
 * Hydration
 * ---------
 * The settings store uses ``skipHydration: true``, so it returns the
 * defaults (``sidebar_collapsed: false``) on **both** the server and the
 * first client render.  Once ``providers.tsx`` triggers rehydration, the
 * store updates to the persisted value and the sidebar re-renders with the
 * correct width.  There is **no** hydration mismatch — this is why the
 * component does not gate on ``useSettingsHydrated()`` and shows the
 * default expanded width for one frame before collapsing, if that is what
 * the user has chosen.
 *
 * Active route detection
 * ----------------------
 * ``usePathname()`` drives the active state.  A nav item is active when:
 *
 *  * ``exact: true`` (Home) — the pathname is identical.
 *  * Otherwise — the pathname is identical **or** a descendant
 *    (``/search/results`` activates ``/search``).
 *
 * The boundary check uses ``item.href + "/"`` rather than ``item.href``
 * alone so that ``/searching`` does **not** activate ``/search``.
 *
 * No shadcn/ui dependencies
 * -------------------------
 * ``components/ui/*`` is not yet part of the tree.  The sidebar is built
 * from ``<aside>``, ``<nav>``, ``<Link>`` and ``<button>`` primitives styled
 * with Tailwind tokens defined in ``src/app/globals.css``.  When the shadcn
 * primitives land, the visual style can be upgraded in a single
 * self-contained edit — the public API (``<Sidebar className={...} />``)
 * does not change.
 *
 * Accessibility
 * -------------
 * * ``<nav aria-label="Main">`` — multiple navs on a page (sidebar + mobile
 *   top bar) each get a distinct label.
 * * Active link carries ``aria-current="page"``.
 * * Collapse toggle button carries ``aria-label`` and ``title`` describing
 *   the **action** ("Expand sidebar" / "Collapse sidebar"), not the current
 *   state.
 * * Icons are ``aria-hidden`` — decorative, since the label provides the
 *   accessible name.
 * * The brand link keeps its accessible name in collapsed mode via the
 *   ``sr-only`` span, so screen-reader users always hear "NexusDL, link".
 */
import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Download,
  Home,
  Search,
  Settings as SettingsIcon,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "../lib/utils";
import { useSettingsStore } from "../store/settings";
import { ThemeToggle } from "./ThemeToggle";

// ───────────────────────────────────────────────────────────────────────────
// Navigation model
// ───────────────────────────────────────────────────────────────────────────

/**
 * A single entry in the sidebar navigation.
 *
 * ``exact`` exists for the Home entry: ``/`` is a prefix of every route, so
 * only an exact match should highlight the Home link.  All other entries
 * activate on prefix matches (``/search/results`` activates ``/search``).
 */
interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  exact?: boolean;
}

/**
 * The navigation items, in display order.
 *
 * Ordering mirrors the user's mental flow through the app: start (Home) →
 * discover (Search) → consume (Downloads, Library) → configure (Settings).
 *
 * The order and set of items are static — a future RBAC system that hides
 * admin-only entries can filter this array at render time, but the canonical
 * list lives here so that changing the sidebar structure touches exactly
 * one place.
 */
const NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "Home", icon: Home, exact: true },
  { href: "/search", label: "Search", icon: Search },
  { href: "/downloads", label: "Downloads", icon: Download },
  { href: "/library", label: "Library", icon: BookOpen },
  { href: "/settings", label: "Settings", icon: SettingsIcon },
];

/**
 * Return ``true`` when *item* should be rendered as the active link for
 * *pathname*.
 *
 * ``usePathname()`` is typed as ``string | null`` in Next.js 14 — it is
 * ``null`` only in edge cases (e.g. during some static-rendering fallbacks)
 * and treating it as "no route" is the safe behaviour.  The prefix boundary
 * check uses ``item.href + "/"`` rather than a bare ``startsWith`` so that
 * ``/searching`` does not match ``/search``.
 */
function isActive(pathname: string | null, item: NavItem): boolean {
  if (pathname === null) {
    return false;
  }
  if (item.exact === true) {
    return pathname === item.href;
  }
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}

// ───────────────────────────────────────────────────────────────────────────
// Props
// ───────────────────────────────────────────────────────────────────────────

/**
 * Props accepted by :func:`Sidebar`.
 */
export interface SidebarProps {
  /**
   * Additional Tailwind classes merged onto the root element via
   * :func:`cn`.  Typical uses:
   *
   *  * offsetting the sidebar below a fixed header:
   *    ``className="top-14 h-[calc(100vh-3.5rem)]"``
   *  * changing the background: ``className="bg-muted/30"``
   *
   * Because the component uses ``cn`` (clsx + twMerge), any conflicting
   * utility the caller passes wins — including ``w-*`` and ``h-*``.
   */
  className?: string;
}

// ───────────────────────────────────────────────────────────────────────────
// Component
// ───────────────────────────────────────────────────────────────────────────

/**
 * Primary desktop navigation for the NexusDL web frontend.
 *
 * See the module docstring for the design, collapse behaviour, hydration
 * and accessibility rationale.
 *
 * @example Standard usage inside the app shell:
 *   <div className="flex min-h-screen">
 *     <Sidebar />
 *     <main className="flex-1">{children}</main>
 *   </div>
 *
 * @example Sidebar below a fixed 56 px header:
 *   <Sidebar className="top-14 h-[calc(100vh-3.5rem)]" />
 */
export function Sidebar({ className }: SidebarProps) {
  const pathname = usePathname();
  const collapsed = useSettingsStore((s) => s.sidebar_collapsed);
  const toggleSidebar = useSettingsStore((s) => s.toggleSidebar);

  return (
    <aside
      className={cn(
        // Desktop only — mobile navigation is provided by the app shell.
        "hidden shrink-0 md:flex md:flex-col",
        // Sticky at the top of the viewport so the sidebar does not scroll
        // with long page content.
        "sticky top-0 h-screen",
        "border-r bg-background",
        // Animated width — the only property that changes on toggle.
        "transition-[width] duration-200 ease-out",
        collapsed ? "w-16" : "w-64",
        className,
      )}
    >
      {/* Brand --------------------------------------------------- */}
      <div className="flex h-14 shrink-0 items-center border-b px-3">
        <Link
          href="/"
          className={cn(
            "flex items-center gap-2 overflow-hidden rounded-md",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "grid h-8 w-8 shrink-0 place-items-center rounded-md",
              "bg-primary text-sm font-bold text-primary-foreground",
            )}
          >
            N
          </span>
          {/*
            The accessible name for the brand link is "NexusDL" in both
            modes.  When collapsed the text is ``sr-only`` — invisible
            visually but present to assistive tech — so the link never
            degrades to an unnamed "N" glyph.
          */}
          <span
            className={cn(
              "text-base font-semibold leading-none",
              collapsed && "sr-only",
            )}
          >
            NexusDL
          </span>
        </Link>
      </div>

      {/* Navigation ---------------------------------------------- */}
      <nav
        aria-label="Main"
        className="flex-1 space-y-1 overflow-y-auto p-2"
      >
        {NAV_ITEMS.map((item) => {
          const active = isActive(pathname, item);
          const Icon = item.icon;

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              /*
                ``title`` is only set when collapsed, where the visible
                label is hidden.  It provides a native tooltip for sighted
                mouse users without duplicating the already-visible text in
                expanded mode.
              */
              title={collapsed ? item.label : undefined}
              className={cn(
                "flex items-center gap-3 rounded-md py-2 text-sm font-medium",
                "transition-colors",
                "focus-visible:outline-none focus-visible:ring-2",
                "focus-visible:ring-ring focus-visible:ring-offset-2",
                "focus-visible:ring-offset-background",
                active
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                // Collapsed: centre the icon inside the (now icon-width)
                // link, and drop the horizontal padding that would push
                // it off-centre.
                collapsed ? "justify-center px-0" : "px-3",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" aria-hidden />
              {/*
                The label is always rendered so that the link's accessible
                name is stable across the two visual states — collapsed mode
                hides it visually via ``sr-only`` rather than removing it.
              */}
              <span className={cn("truncate", collapsed && "sr-only")}>
                {item.label}
              </span>
            </Link>
          );
        })}
      </nav>

      {/* Footer — theme + collapse toggle ------------------------- */}
      <div
        className={cn(
          "flex shrink-0 items-center gap-1 border-t p-2",
          // Collapsed: stack the two buttons vertically and centre them
          // inside the icon-width sidebar.
          collapsed ? "flex-col items-center" : "justify-between",
        )}
      >
        <ThemeToggle />
        <button
          type="button"
          onClick={toggleSidebar}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={cn(
            "inline-flex h-9 w-9 items-center justify-center rounded-md",
            "text-muted-foreground transition-colors",
            "hover:bg-accent hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2",
            "focus-visible:ring-ring focus-visible:ring-offset-2",
            "focus-visible:ring-offset-background",
          )}
        >
          {collapsed ? (
            <ChevronRight className="h-4 w-4" aria-hidden />
          ) : (
            <ChevronLeft className="h-4 w-4" aria-hidden />
          )}
        </button>
      </div>
    </aside>
  );
}
