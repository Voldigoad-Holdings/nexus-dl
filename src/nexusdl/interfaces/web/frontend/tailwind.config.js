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
      padding: "2rem",
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
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
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
