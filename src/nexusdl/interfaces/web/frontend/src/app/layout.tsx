import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";

import { Providers } from "./providers";
import "./globals.css";

// ───────────────────────────────────────────────────────────────────────────
// Fonts
// ───────────────────────────────────────────────────────────────────────────
//
// ``next/font/google`` self-hosts the font at build time — no runtime
// request to Google Fonts, which means no external origin in the CSP's
// ``font-src`` and no privacy concern.  ``display: "swap"`` renders the
// fallback font immediately and swaps in Inter when it is ready, avoiding
// the invisible-text flash (FOIT) that ``display: "block"`` would cause.
//
// The font is applied directly via ``inter.className`` on ``<body>`` rather
// than through a CSS variable — globals.css does not declare ``--font-sans``,
// and adding one would require editing that file, which this iteration does
// not do.  ``inter.className`` is the simplest shape that works with the
// existing stylesheet unchanged.
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
});

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

/**
 * Display name of the application.
 *
 * Fixed at the project level (PROMPT MASTER §"Contexte du projet":
 * *nom affiché: NexusDL*), and used identically by the web manifest
 * (``public/manifest.json``), the sidebar brand, the login page and here.
 * Keeping the four in sync is a project invariant; a future refactor could
 * hoist it to a shared constant module, but there is exactly one canonical
 * value today.
 */
const APP_NAME = "NexusDL";

/** One-line description, matching ``public/manifest.json``. */
const APP_DESCRIPTION =
  "Multi-site manga, webtoon, comic and doujinshi downloader — CLI, TUI, desktop GUI and web.";

// ───────────────────────────────────────────────────────────────────────────
// Metadata
// ───────────────────────────────────────────────────────────────────────────

/**
 * Resolve the value of :data:`Metadata.metadataBase`.
 *
 * ``metadataBase`` is the absolute URL that Next.js prepends to every
 * relative URL in the metadata — most importantly the ``/og-image.png``
 * referenced below.  Without it, Next.js falls back to a placeholder and
 * logs a warning at build time; with it, social-sharing previews resolve
 * to a real hostname.
 *
 * The value comes from ``NEXUSDL_PUBLIC_URL`` (PROMPT MASTER §"Principes
 * fondamentaux" rule 1: *URLs app → ``.env``*).  Hardcoding a domain here
 * would violate rule 3 of the same section.  This is why the fallback is
 * not a URL guess but rather:
 *
 *  * ``http://localhost:3000`` in development — matches ``next dev``'s
 *    default port, so local screenshots and sharing previews work;
 *  * ``undefined`` in production when the env var is missing — Next.js
 *    then emits the correct build-time warning telling the operator to
 *    set the variable, rather than silently producing wrong absolute
 *    URLs.
 */
function getMetadataBase(): URL | undefined {
  const raw = process.env.NEXUSDL_PUBLIC_URL?.trim();
  if (raw === undefined || raw.length === 0) {
    if (process.env.NODE_ENV === "development") {
      return new URL("http://localhost:3000");
    }
    return undefined;
  }
  try {
    return new URL(raw);
  } catch {
    // A malformed URL in the environment is a deployment bug — treat it
    // like a missing one.  The build-time warning is the correct signal;
    // throwing here would break ``next build`` on a typo, which is not
    // the failure mode the operator deserves.
    return undefined;
  }
}

/**
 * Page metadata — applied to every route that does not override it.
 *
 * Server Component export (Next.js 14 App Router).  Because this is
 * exported from a Server Component, adding ``"use client"`` here would
 * disable it entirely — which is why only ``<Providers>`` (a child
 * component) is marked as a client boundary.
 */
export const metadata: Metadata = {
  metadataBase: getMetadataBase(),

  // -------------------------------------------------------------------
  // Title and description
  // -------------------------------------------------------------------
  // The ``template`` is what turns a page's own ``metadata.title`` into
  // ``"<Page Title> · NexusDL"``.  None of the current routes export
  // their own ``metadata.title`` (they render their titles via ``<h1>``
  // inside client components), but the template is what a future
  // Server-Component route (e.g. a public landing page) would pick up
  // automatically.
  title: {
    default: APP_NAME,
    template: `%s · ${APP_NAME}`,
  },
  description: APP_DESCRIPTION,
  applicationName: APP_NAME,
  generator: "Next.js",

  // -------------------------------------------------------------------
  // Referrer policy
  // -------------------------------------------------------------------
  // Matches the header set in ``next.config.js``.  Repeating it here is
  // required so the policy is also emitted for the ``<meta name="referrer">``
  // tag that some older browsers consult.
  referrer: "strict-origin-when-cross-origin",

  // -------------------------------------------------------------------
  // Indexing
  // -------------------------------------------------------------------
  // Off by default.  NexusDL is a personal downloader whose library is
  // per-user and whose public catalogue page does not exist — indexing
  // would only serve to expose the installation URL.  An operator who
  // explicitly wants their instance indexed should reverse this decision
  // in a deployment-specific override, not by editing this file.
  robots: {
    index: false,
    follow: false,
    googleBot: {
      index: false,
      follow: false,
      "max-image-preview": "none",
      "max-snippet": -1,
      "max-video-preview": -1,
    },
  },

  // -------------------------------------------------------------------
  // Icons
  // -------------------------------------------------------------------
  // Every entry below corresponds to an actual file under ``public/``:
  //
  //   public/favicon.ico
  //   public/icons/favicon-16x16.png
  //   public/icons/favicon-32x32.png
  //   public/icons/apple-touch-icon.png
  //   public/logo.svg
  //
  // Browsers that support the SVG mask icon (Safari on macOS, Edge on
  // Windows) will use ``logo.svg`` for the pinned-tab and system-tray
  // icons.  The ``color`` is the dark-theme background, which the
  // SVG's fill inherits when Safari applies its mask.
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/icons/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/icons/favicon-32x32.png", sizes: "32x32", type: "image/png" },
      { url: "/logo.svg", type: "image/svg+xml" },
    ],
    apple: [
      {
        url: "/icons/apple-touch-icon.png",
        sizes: "180x180",
        type: "image/png",
      },
    ],
    other: [{ rel: "mask-icon", url: "/logo.svg", color: "#0a0a0a" }],
  },

  // -------------------------------------------------------------------
  // PWA
  // -------------------------------------------------------------------
  // The manifest declares the app installable; ``appleWebApp`` fills the
  // iOS-specific meta tags that Safari does not read from the manifest.
  manifest: "/manifest.json",
  appleWebApp: {
    capable: true,
    title: APP_NAME,
    statusBarStyle: "black-translucent",
  },

  // -------------------------------------------------------------------
  // OpenGraph
  // -------------------------------------------------------------------
  // Social sharing previews.  The ``/og-image.png`` file exists under
  // ``public/`` and is the canonical sharing image — a single static PNG
  // is sufficient for a PWA whose routes are not individually
  // shareable in the same way a blog post is.
  openGraph: {
    type: "website",
    siteName: APP_NAME,
    title: APP_NAME,
    description: APP_DESCRIPTION,
    images: [
      {
        url: "/og-image.png",
        width: 1200,
        height: 630,
        alt: APP_NAME,
      },
    ],
  },

  // -------------------------------------------------------------------
  // Twitter / X card
  // -------------------------------------------------------------------
  // ``summary_large_image`` uses the same ``og-image.png`` as the
  // OpenGraph preview, which is what every card renderer expects.
  twitter: {
    card: "summary_large_image",
    title: APP_NAME,
    description: APP_DESCRIPTION,
    images: ["/og-image.png"],
  },

  // -------------------------------------------------------------------
  // Format detection
  // -------------------------------------------------------------------
  // Safari on iOS auto-links anything that looks like a phone number, an
  // email address or a postal address.  Manga titles occasionally contain
  // such tokens ("Vol. 20-11-2024", "Chapter 3-1-2"), and auto-linking
  // them is always wrong on this UI — the tokens are titles, not contact
  // information.  Disabling detection globally on the document removes
  // that class of bug at the root.
  formatDetection: {
    telephone: false,
    email: false,
    address: false,
  },
};

// ───────────────────────────────────────────────────────────────────────────
// Viewport
// ───────────────────────────────────────────────────────────────────────────
//
// Exported separately from ``metadata`` — Next.js 14 split these so that
// ``viewport``-related tags can be re-evaluated on the client without
// shipping the whole ``metadata`` object.  ``themeColor`` in particular
// must be able to follow the active color scheme, which is why it is
// declared here as a media-query pair rather than as a single string in
// ``metadata``.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // ``maximumScale: 5`` rather than ``1`` — pinching to zoom is an
  // accessibility feature, not something to disable.  Five times the
  // initial scale is enough for reading page images at full resolution
  // without turning the whole UI into a scrollable trap.
  maximumScale: 5,
  // ``userScalable`` is intentionally **not** set to ``false``: iOS
  // ignores ``maximumScale: 5`` when ``userScalable`` is false, and
  // disabling zoom outright is an accessibility regression.
  colorScheme: "dark light",
  // Two theme-colour values keyed on the user's preference.  The dark
  // value matches ``public/manifest.json`` (``theme_color: "#0a0a0a"``).
  // The light value is the corresponding light background from
  // ``globals.css`` (``--background: 0 0% 100%``).
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
  ],
};

// ───────────────────────────────────────────────────────────────────────────
// Layout
// ───────────────────────────────────────────────────────────────────────────

/**
 * Root layout — wraps every route in the App Router.
 *
 * Rendered exactly once by Next.js.  It is a **Server Component** (no
 * ``"use client"`` at the top of this file) so that:
 *
 *  * ``export const metadata`` and ``export const viewport`` are honoured
 *    by Next.js — metadata exports are silently ignored in Client
 *    Components.
 *  * The layout ships no JavaScript beyond what ``<Providers>`` needs.
 *
 * The single client boundary in the tree is ``<Providers>`` (see
 * ``providers.tsx``).  Every other page in the App Router can be either a
 * Server Component or a Client Component; the split is decided by each
 * route's own needs, not forced by the layout.
 *
 * ``suppressHydrationWarning`` is **required** on ``<html>``:
 *
 *  * ``next-themes`` sets ``class="dark"`` (or ``"light"``) on ``<html>``
 *    **before** React hydrates, by injecting a tiny inline script that
 *    runs at document start.
 *  * The server-rendered HTML has no ``class`` attribute on ``<html>``.
 *  * Without ``suppressHydrationWarning``, React reports a hydration
 *    mismatch on every page load.
 *
 * The suppression is safe because it applies **only** to that one
 * attribute on that one element.  A real mismatch inside the tree would
 * still be reported.
 *
 * ``lang="en"`` reflects the default ``language: "en"`` in the settings
 * store.  A future i18n layer would update this via a client effect that
 * reads the user's preference, but until then the static default matches
 * both the manifest's ``lang`` field and the language of every shipped
 * translation string.
 */
export default function RootLayout({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={inter.className}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
