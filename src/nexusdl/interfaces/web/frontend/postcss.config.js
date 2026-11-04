/**
 * PostCSS configuration for the NexusDL web frontend.
 *
 * Next.js 14 runs every CSS import through PostCSS. This config wires the
 * two plugins required by TailwindCSS 3 + shadcn/ui:
 *
 * * ``tailwindcss``     — expands ``@tailwind base/components/utilities``
 *   in ``src/app/globals.css`` into the generated utility classes.
 * * ``autoprefixer``    — adds vendor prefixes based on ``browserslist``
 *   (sourced from ``package.json``, falling back to Next.js's default
 *   browserslist).
 *
 * No other plugin is required. Next.js already ships the built-in
 * ``postcss-flexbugs-fixes`` and ``postcss-preset-env`` equivalents in its
 * internal pipeline for non-Tailwind CSS, so adding them here would
 * duplicate work and diverge from the shadcn/ui baseline.
 *
 * The file is CommonJS (``module.exports``) because Next.js loads PostCSS
 * config synchronously during the build, before any ESM loader is set up.
 *
 * @type {import('postcss-load-config').Config}
 */
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
