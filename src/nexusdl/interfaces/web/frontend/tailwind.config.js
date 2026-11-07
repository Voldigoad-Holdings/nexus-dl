/**
 * TailwindCSS 3 configuration for the NexusDL web frontend.
 *
 * The configuration follows the **shadcn/ui** conventions used across the
 * frontend components (``ChapterList``, ``DownloadItem``, ``MangaCard``,
 * ``ProgressBar``, ``SearchBar``, ``Sidebar``, ``SiteSelector``,
 * ``ThemeToggle``): theming is driven by CSS variables defined in
 * ``src/app/globals.css``, and the ``darkMode: ["class"]`` strategy is what
 * powers ``ThemeToggle``.
 *
 * The ``tailwindcss-animate`` plugin is required by shadcn/ui's Radix-based
 * components (dialog, dropdown-menu, tooltip, accordion, etc.) and is
 * declared as a dependency in ``package.json``.
 *
 * Path aliases: the frontend uses the Next.js 14 ``src/`` convention, so the
 * ``content`` globs point to ``./src/**``.  This matches the ``@/*`` →
 * ``./src/*`` alias in ``tsconfig.json``.  No other content path is needed
 * — components, pages, hooks and stores all live under ``src/``.
 *
 * Token coverage
 * --------------
 * Every CSS variable declared in ``src/app/globals.css`` under
 * ``:root, .dark`` and ``.light`` is exposed here as a Tailwind colour,
 * background, shadow or border token, so that components never need to
 * inline ``hsl(var(--…))`` in arbitrary-value brackets.  This includes the
 * five chart colours (``--chart-1`` … ``--chart-5``) used by future
 * charting components, and the two neon glow colours (``--neon-glow``,
 * ``--neon-glow-secondary``) used by ``.neon-text``, ``.neon-card``,
 * ``.neon-border`` and the neon pulse/flicker utilities.
 *
 * @type {import('tailwindcss').Config}
 */
module.exports = {
  darkMode: ["class"],
  content: [
    "./src/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    container: {
      center: true,
      // Responsive padding: 1 rem on mobile, 1.5 rem on tablet, 2 rem on
      // desktop.  A flat ``2rem`` (the shadcn/ui default) wastes ~40 % of
      // a 320 px viewport on small screens — which matters for the
      // library grid and the manga detail page, both of which use
      // ``container`` at the page level.
      padding: {
        DEFAULT: "1rem",
        sm: "1.5rem",
        lg: "2rem",
      },
      screens: {
        "2xl": "1400px",
      },
    },
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // -- Chart colours -------------------------------------------------
        // Five neon hues declared in globals.css.  Used by future charting
        // components (stat cards, download speed graphs, etc.).  Exposing
        // them here means ``bg-chart-1`` / ``text-chart-2`` / … work
        // without arbitrary-value brackets.
        chart: {
          1: "hsl(var(--chart-1))",
          2: "hsl(var(--chart-2))",
          3: "hsl(var(--chart-3))",
          4: "hsl(var(--chart-4))",
          5: "hsl(var(--chart-5))",
        },
        // -- Neon glow palette --------------------------------------------
        // Exposed so that ``shadow-glow``, ``border-glow`` and
        // ``ring-glow`` work as first-class utilities.  The existing
        // ``.neon-text`` / ``.neon-card`` / ``.neon-border`` classes in
        // ``globals.css`` continue to work as-is — this just adds
        // alternatives that compose with Tailwind utilities.
        neon: {
          DEFAULT: "hsl(var(--neon-glow))",
          secondary: "hsl(var(--neon-glow-secondary))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      // -- Neon shadows ----------------------------------------------------
      // Short-hand shadows backed by the neon tokens above.  Used by
      // ``.neon-card`` / ``.neon-border`` today; available for future
      // components that want the neon feel without the CSS layer.
      boxShadow: {
        "glow-sm": "0 0 8px hsl(var(--neon-glow) / 0.4)",
        glow: "0 0 12px hsl(var(--neon-glow) / 0.5)",
        "glow-lg": "0 0 24px hsl(var(--neon-glow) / 0.6)",
        "glow-secondary":
          "0 0 12px hsl(var(--neon-glow-secondary) / 0.5)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
