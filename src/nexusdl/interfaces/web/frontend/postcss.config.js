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
 * Plugin format
 * -------------
 * The plugins are declared as an **array with explicit ``require()``
 * calls**, not as an object (``{ tailwindcss: {}, autoprefixer: {} }``).
 *
 * The object form still works under PostCSS 8 thanks to a compatibility
 * shim, but it is **deprecated** and PostCSS 9 removes it. It also relies
 * on ``postcss-load-config``'s implicit module resolution, which produces
 * a cryptic error if a plugin is ever missing from ``node_modules``. The
 * array form fails immediately with a clear ``Cannot find module`` and is
 * the format TailwindCSS's own documentation recommends.
 *
 * Nesting
 * -------
 * ``tailwindcss/nesting`` is **not** included. The project's
 * ``globals.css`` does not use nested ``&`` syntax, and shadcn/ui's own
 * components do not require it either (they use ``@apply`` on flat
 * selectors). If a future component needs CSS nesting, add the plugin to
 * the array **before** ``tailwindcss``:
 *
 *     require("tailwindcss/nesting"),
 *     require("tailwindcss"),
 *
 * The file is CommonJS (``module.exports``) because Next.js loads PostCSS
 * config synchronously during the build, before any ESM loader is set up.
 * If the project ever adopts ``"type": "module"`` in ``package.json``,
 * rename this file to ``postcss.config.mjs`` and switch to
 * ``export default { plugins: [...] }``.
 *
 * @type {import('postcss-load-config').Config}
 */
module.exports = {
  plugins: [
    require("tailwindcss"),
    require("autoprefixer"),
  ],
};
