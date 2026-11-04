// @ts-check

/**
 * Next.js 14 configuration for the NexusDL web frontend.
 *
 * This file wires the frontend to the FastAPI backend **without hardcoding
 * any URL**, in line with the project's rules (see PROMPT MASTER §"Principes
 * fondamentaux"):
 *
 *  - ``NEXUSDL_API_BASE_URL`` is read from the environment at server start
 *    time. It is used exclusively by the ``rewrites`` function below, so
 *    the URL never leaks into the client-side JS bundle.
 *  - The frontend calls ``/api/v1/...`` and ``/ws/v1/...`` using **relative**
 *    URLs, and Next.js proxies them to the backend. No CORS configuration
 *    is needed, and no NEXT_PUBLIC_* variable is exposed to the browser.
 *
 * Deployment shape:
 *
 *   Browser ──► Next.js (port 3000) ──► FastAPI (port 8000)
 *              │   /api/*   → rewrite
 *              │   /ws/*    → rewrite
 *              │   /*        → Next.js App Router
 *
 * In production, Caddy or nginx terminates TLS and routes ``/api/*`` and
 * ``/ws/*`` directly to the backend, bypassing the Next.js rewrite for
 * performance — the rewrite is only used in development and in
 * ``docker-compose.dev.yml``. Both paths produce the same browser-visible
 * URLs (``https://<frontend-host>/api/v1/...``), so the frontend code is
 * identical in dev and prod.
 *
 * @type {import('next').NextConfig}
 */

// ---------------------------------------------------------------------------
// Environment validation
// ---------------------------------------------------------------------------
const apiBaseUrl = (process.env.NEXUSDL_API_BASE_URL || "").replace(/\/+$/, "");
const isProduction = process.env.NODE_ENV === "production";
const isDev = !isProduction;

if (!apiBaseUrl) {
  // We deliberately do NOT throw here. ``next build`` runs this file at
  // build time, and the API URL is a **runtime** concern in Docker: the
  // same image must be deployable against dev, staging and prod backends.
  // Throwing at build time would couple the image to a specific backend.
  //
  // Instead, the ``rewrites`` function below returns an empty list when the
  // env var is missing, and the app starts with every ``/api/*`` and
  // ``/ws/*`` request returning 404. That is loud enough for a developer to
  // notice immediately, without blocking image builds or CI.
  console.warn(
    "[NexusDL] NEXUSDL_API_BASE_URL is not set. " +
      "API and WebSocket requests will NOT be proxied to the backend. " +
      "Set it in .env.local (development) or the deployment environment " +
      "(production)."
  );
}

// ---------------------------------------------------------------------------
// Content-Security-Policy
// ---------------------------------------------------------------------------
// The CSP is built from static components only — no dynamic URL is included.
// Rationale for each directive:
//
//   default-src 'self'          — everything defaults to same-origin.
//   script-src                  — Next.js injects inline hydration scripts,
//                                 so 'unsafe-inline' is required.
//                                 'unsafe-eval' is only added in dev for
//                                 webpack's eval-based source maps.
//   style-src                   — TailwindCSS + Radix UI use inline styles.
//   img-src                     — manga covers come from 60+ arbitrary
//                                 external sites; only image fetching is
//                                 opened to https/http. Everything else
//                                 stays locked to 'self'.
//   font-src                    — self-hosted fonts + data: URIs.
//   connect-src                 — same-origin for the API proxy, ws/wss for
//                                 the WebSocket. The proxy makes both
//                                 same-origin from the browser's perspective;
//                                 the ws/wss scheme qualifiers are needed
//                                 because 'self' does not cover WS schemes.
//   frame-ancestors 'none'      — defense-in-depth alongside X-Frame-Options.
//   base-uri 'self'             — prevent <base> tag injection attacks.
//   form-action 'self'          — prevent form submission to foreign origins.
//   object-src 'none'           — no Flash, Java applets, or <embed>.
//   upgrade-insecure-requests   — production only; forces HTTPS for any
//                                 accidental HTTP sub-resource.
const scriptSrc = isDev
  ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
  : "script-src 'self' 'unsafe-inline'";

const cspDirectives = [
  "default-src 'self'",
  scriptSrc,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https: http:",
  "font-src 'self' data:",
  "connect-src 'self' ws: wss:",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
];
if (isProduction) {
  cspDirectives.push("upgrade-insecure-requests");
}
const contentSecurityPolicy = cspDirectives.join("; ") + ";";

// ---------------------------------------------------------------------------
// Next.js config
// ---------------------------------------------------------------------------
/** @type {import('next').NextConfig} */
const nextConfig = {
  // -----------------------------------------------------------------------
  // Deployment
  // -----------------------------------------------------------------------
  // ``standalone`` produces a self-contained ``.next/standalone/`` directory
  // with only the files needed to run, which is what the multi-stage
  // Dockerfile copies into the runtime image. See ``docker/Dockerfile.web``.
  output: "standalone",

  // Strict mode catches a class of React bugs (double-render side effects,
  // legacy lifecycle misuse, etc.) at development time. The project ships
  // strict mode on.
  reactStrictMode: true,

  // Remove the ``X-Powered-By: Next.js`` response header — free fingerprint
  // reduction.
  poweredByHeader: false,

  // Gzip compression at the Next.js server level. In production, Caddy or
  // nginx compresses the response anyway, but this keeps ``next start``
  // efficient in dev and in deployments without a reverse proxy.
  compress: true,

  // -----------------------------------------------------------------------
  // Build quality gates
  // -----------------------------------------------------------------------
  // The project's standard is ``mypy --strict`` on the backend and
  // ``tsc --noEmit`` on the frontend. Neither build ever ignores type or
  // lint errors.
  typescript: {
    ignoreBuildErrors: false,
  },
  eslint: {
    ignoreDuringBuilds: false,
  },

  // Strip ``console.*`` calls from the production bundle. The frontend uses
  // ``sonner`` for user-visible notifications; anything logged to the
  // console in development is developer-only noise in production.
  compiler: {
    removeConsole: isProduction
      ? { exclude: ["error", "warn"] }
      : false,
  },

  // -----------------------------------------------------------------------
  // Images
  // -----------------------------------------------------------------------
  // Manga covers come from 60+ external sites, so the domain allowlist
  // cannot be enumerated in advance. ``hostname: "**"`` allows any host;
  // restricting to https and http keeps the risk surface to image-fetching
  // only (``img-src`` in the CSP has the same shape). Next.js still runs
  // the cover through its optimizer (resize, WebP/AVIF conversion, cache),
  // which is what makes the library grid fast.
  images: {
    remotePatterns: [
      { protocol: "https", hostname: "**" },
      { protocol: "http", hostname: "**" },
    ],
    formats: ["image/avif", "image/webp"],
  },

  // -----------------------------------------------------------------------
  // Rewrites — the single point where NEXUSDL_API_BASE_URL is consumed
  // -----------------------------------------------------------------------
  // These rewrites proxy browser requests to the FastAPI backend. The
  // browser only ever sees same-origin URLs (``/api/v1/...``,
  // ``/ws/v1/events``); the cross-origin hop to ``NEXUSDL_API_BASE_URL``
  // happens server-side inside Next.js. This is what makes the frontend
  // free of hardcoded URLs and free of CORS configuration.
  //
  // When ``NEXUSDL_API_BASE_URL`` is missing, we return an empty list — the
  // app still starts (so ``next dev`` works even before ``.env.local`` has
  // been created) but every API and WS call returns 404, making the
  // misconfiguration immediately visible. We do **not** fall back to a
  // default URL: that would violate the "no hardcoded URL" rule.
  async rewrites() {
    if (!apiBaseUrl) {
      return [];
    }
    return [
      {
        source: "/api/:path*",
        destination: `${apiBaseUrl}/api/:path*`,
      },
      {
        source: "/ws/:path*",
        destination: `${apiBaseUrl}/ws/:path*`,
      },
    ];
  },

  // -----------------------------------------------------------------------
  // Security headers
  // -----------------------------------------------------------------------
  // Applied to every route. The CSP is the only directive that differs
  // between dev and prod (``'unsafe-eval'`` and ``upgrade-insecure-requests``).
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          {
            key: "Content-Security-Policy",
            value: contentSecurityPolicy,
          },
          {
            key: "X-Content-Type-Options",
            value: "nosniff",
          },
          {
            key: "X-Frame-Options",
            value: "DENY",
          },
          {
            key: "Referrer-Policy",
            value: "strict-origin-when-cross-origin",
          },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
          {
            key: "X-DNS-Prefetch-Control",
            value: "on",
          },
          // HSTS is only sent in production, and only via HTTPS. The
          // ``preload`` directive opts this origin into the HSTS preload
          // list once the operator submits it — the header itself is
            // harmless if they choose not to.
          ...(isProduction
            ? [
                {
                  key: "Strict-Transport-Security",
                  value: "max-age=63072000; includeSubDomains; preload",
                },
              ]
            : []),
        ],
      },
    ];
  },
};

module.exports = nextConfig;
