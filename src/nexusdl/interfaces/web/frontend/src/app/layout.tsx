import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";

import { Providers } from "./providers";
import { Sidebar } from "../components/Sidebar";
import "./globals.css";

// ───────────────────────────────────────────────────────────────────────────
// Fonts
// ───────────────────────────────────────────────────────────────────────────
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
});

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

const APP_NAME = "NexusDL";

const APP_DESCRIPTION =
  "Multi-site manga, webtoon, comic and doujinshi downloader — CLI, TUI, desktop GUI and web.";

// ───────────────────────────────────────────────────────────────────────────
// Metadata
// ───────────────────────────────────────────────────────────────────────────

/**
 * Resolve the value of :data:`Metadata.metadataBase`.
 *
 * See the previous iteration's docstring for the full rationale.  Summary:
 * the value comes from ``NEXUSDL_PUBLIC_URL``; only ``http:`` and ``https:``
 * URLs are accepted; a missing or malformed value yields ``undefined`` in
 * production (Next.js's build-time warning is the correct signal) and
 * ``http://localhost:3000`` in development.
 */
function getMetadataBase(): URL | undefined {
  const raw = process.env.NEXUSDL_PUBLIC_URL?.trim();
  if (raw === undefined || raw.length === 0) {
    if (process.env.NODE_ENV === "development") {
      return new URL("http://localhost:3000");
    }
    return undefined;
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return undefined;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return undefined;
  }
  return url;
}

/**
 * Page metadata — applied to every route that does not override it.
 */
export const metadata: Metadata = {
  metadataBase: getMetadataBase(),

  title: {
    default: APP_NAME,
    template: `%s · ${APP_NAME}`,
  },
  description: APP_DESCRIPTION,
  applicationName: APP_NAME,
  generator: "Next.js",

  referrer: "strict-origin-when-cross-origin",

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

  manifest: "/manifest.json",
  appleWebApp: {
    capable: true,
    title: APP_NAME,
    statusBarStyle: "black-translucent",
  },

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

  twitter: {
    card: "summary_large_image",
    title: APP_NAME,
    description: APP_DESCRIPTION,
    images: ["/og-image.png"],
  },

  formatDetection: {
    telephone: false,
    email: false,
    address: false,
  },
};

// ───────────────────────────────────────────────────────────────────────────
// Viewport
// ───────────────────────────────────────────────────────────────────────────
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  colorScheme: "dark light",
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
 * Renders the **application shell**:
 *
 * * ``<Sidebar />`` — the primary desktop navigation, hidden below ``md``.
 *   Mobile navigation is delegated to the sidebar's own responsive
 *   behaviour (``hidden md:flex``) — when a ``TopBar`` / ``BottomNav``
 *   primitive lands, it will be added here as a sibling, but the shell's
 *   structure (``flex min-h-screen`` with the sidebar as the first child
 *   and ``<main>`` as the second) will not change.
 * * ``<main id="main-content" tabIndex={-1}>`` — the landmark target for
 *   "skip to content" links, focus management after navigation, and
 *   screen-reader heading navigation.  ``tabIndex={-1}`` makes the
 *   element focusable programmatically (so ``element.focus()`` works) but
 *   not in the tab order.
 * * ``<Providers>`` — the single client boundary (theme, query cache,
 *   WebSocket, toaster).
 *
 * ``suppressHydrationWarning`` is required on ``<html>`` because
 * ``next-themes`` sets ``class="dark"`` (or ``"light"``) **before** React
 * hydrates, via an inline script.  The suppression is safe: it applies to
 * that one attribute on that one element, and real mismatches inside the
 * tree are still reported.
 *
 * ``lang="en"`` reflects the default ``language: "en"`` in the settings
 * store.  A future i18n layer would update this via a client effect.
 */
export default function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={inter.className}>
        <Providers>
          <a
            href="#main-content"
            className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-primary-foreground"
          >
            Skip to content
          </a>
          <div className="flex min-h-screen">
            <Sidebar />
            <main
              id="main-content"
              tabIndex={-1}
              className="flex min-w-0 flex-1 flex-col focus:outline-none"
            >
              {children}
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
