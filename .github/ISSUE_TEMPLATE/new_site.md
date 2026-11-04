---
name: New site request
about: Request integration of a new manga / webtoon / comic / doujinshi source
title: "[SITE] "
labels: ["new-site", "triage"]
assignees: []
---

<!--
  ════════════════════════════════════════════════════════════════════
  ⚠️  USE THIS TEMPLATE ONLY FOR NEW SITE REQUESTS
  ════════════════════════════════════════════════════════════════════

  For anything else:
    * Feature request     → .github/ISSUE_TEMPLATE/feature_request.md
    * Bug report          → .github/ISSUE_TEMPLATE/bug_report.md
    * Security issue      → SECURITY.md (private, never public)

  ════════════════════════════════════════════════════════════════════
  ⚠️  LEGAL — read before submitting
  ════════════════════════════════════════════════════════════════════

  NexusDL is a **downloader**, not a content host.  It fetches pages
  that are already publicly accessible from the browser and packages
  them locally.  Adding a site integration:

    * does NOT host, mirror or redistribute any content;
    * does NOT bypass paywalls, DRM, or age gates that the site itself
      enforces;
    * does NOT circumvent the site's robots.txt if the operator has
      explicitly opted in to respecting it (``NEXUSDL_RESPECT_ROBOTS``).

  Requests for sites that require **authentication bypass**, **paywall
  circumvention**, **DRM removal**, or **paid content extraction** will
  be **closed without action**.  If the site requires a login, that is
  fine — the user logs in with their own credentials and NexusDL stores
  the cookies locally (never sent anywhere else).  If the site requires
  **paid** credentials the user does not legitimately own, it is out
  of scope.

  By submitting this issue, you confirm that the requested site is
  publicly accessible without paid authentication.

  ════════════════════════════════════════════════════════════════════
  ⚠️  SECRETS — REDACT BEFORE POSTING
  ════════════════════════════════════════════════════════════════════

  Remove every secret from any example URL, screenshot or log you
  attach: cookies, session tokens, API keys, user credentials.
-->


## Pre-flight checklist

- [ ] I have read the [documentation on adding a site](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/source/development/adding_sites.md).
- [ ] I have searched [existing issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues?q=label%3Anew-site) and this site is not already requested.
- [ ] The site is not already supported — I checked `data/sites.yaml` and the [supported sites list](https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs).
- [ ] The site is publicly accessible **without paid authentication** (see the Legal notice above).
- [ ] The site does **not** require bypassing a paywall, DRM, or paid credentials that I do not own.
- [ ] I have read the capability fields requested below and can fill them in or say "I don't know".


## Site identification

<!--
  Fill in one row per field.  If a field does not apply, write "N/A".

  The ``site_id`` must match the pattern ``^[a-z][a-z0-9_-]*$``
  (lowercase letters, digits, underscores, hyphens; starting with a
  letter).  Convention: the site's own name, snake_cased, with any
  ``.tld`` suffix replaced by an underscore and the tld if it aids
  disambiguation.

  Examples of existing site_ids:
      mangadex
      comick
      asurascans
      x_manga_org
      scan_hentai
      sushiscan
-->

| Field | Value |
|---|---|
| **Site name** (as displayed) | <!-- e.g. "Asura Scans" --> |
| **Proposed site_id** | <!-- e.g. asurascans --> |
| **Primary domain (HTTPS)** | <!-- e.g. https://asuracomic.net --> |
| **Alternative domains / mirrors** | <!-- comma-separated, or N/A --> |
| **Content language** | <!-- fr / en / ja / ko / zh / es / pt / it / de / ru / ar / unknown --> |
| **Adult-only site** | <!-- yes / no — "no" if the site hosts a mix of content --> |


## Content type

<!--
  NexusDL supports four content shapes.  Tick the ones the site
  publishes.  This determines which reader layout the parser must
  handle and which packagers make sense.

  * Manga / comic    — pages are single images (JPEG, PNG, WebP, …)
  * Webtoon          — pages are a single very tall strip, split into
                       vertical chunks; the reader is a continuous
                       scroll
  * Doujinshi        — manga-style, often with gallery viewers and
                       download gating (nhentai-style)
  * Novel / text     — pages are paragraphs of text, not images
                       (NOT YET SUPPORTED — see the note below)

  If you tick "novel / text", understand that NexusDL does not yet
  support text-based reading.  The request will be kept open as a
  marker but is unlikely to be implemented soon.
-->

- [ ] Manga / comic (image pages)
- [ ] Webtoon (vertical strip)
- [ ] Doujinshi (gallery-style)
- [ ] Novel / text (⚠️ not yet supported — will be parked)


## Underlying stack

<!--
  This is the single most useful field of this template.  NexusDL
  ships reusable mixins for the three most common manga reading
  stacks, and a site that runs on one of them requires only a
  five-line parser file.  A site that runs on a custom stack requires
  a bespoke parser that takes days to write and maintain.

  How to determine the stack:
    1. Open the site in a browser.
    2. Right-click → "View Page Source".
    3. Search for these strings:
         "wp-content/themes/madara"          → WordPress Madara
         "wp-content/themes/mangathemesia"   → WordPress MangaThemesia
         "foolslide"                         → FoolSlide
         "wp-content" but no theme match     → WordPress, custom theme
         nothing obvious, page loads empty   → JavaScript-rendered SPA

  If you are not sure, say "I don't know" — that is a valid answer and
  the maintainer will investigate.
-->

- [ ] **WordPress Madara** — the parser can inherit from `WordPressMadaraMixin`
      and only needs to declare `site_id`, `language` and `adult`.
      (Fastest possible integration — one file, five lines.)
- [ ] **WordPress MangaThemesia** — the parser can inherit from
      `MangaThemesiaMixin`.  Same as above.
- [ ] **FoolSlide** — the parser can inherit from `FoolSlideMixin`.
      Same as above.
- [ ] **Custom WordPress theme** — needs a bespoke parser, but the
      patterns from the Madara/MangaThemesia mixins can be reused.
- [ ] **JavaScript SPA** — the parser will need `JsRenderedMixin`
      (Playwright) to obtain the DOM.
- [ ] **Custom non-WordPress stack** — needs a fully bespoke parser.
- [ ] **I don't know** — please investigate.

<!--
  If the stack is one of the three reusable ones (Madara, MangaThemesia,
  FoolSlide), you can stop filling in the selectors section below —
  the mixin already handles them.
-->


## Cloudflare / anti-bot protection

<!--
  Cloudflare and equivalent protections (Turnstile, DataDome,
  PerimeterX, hCaptcha) determine whether the parser needs
  ``CloudflareMixin`` and therefore whether it will require FlareSolverr
  or Playwright to be configured by every user of the site.

  How to check:
    1. Open the site in a private browser window.
    2. If you see a "Checking your browser…" or "Just a moment…"
       interstitial before the content loads, the site is behind a
       JavaScript challenge.
    3. If you see a puzzle or a "Verify you are human" checkbox, it
       is an interactive challenge (Turnstile / hCaptcha).

  A site behind Cloudflare can still be integrated — several shipped
  parsers are.  But it is important to know upfront so that the
  documentation can tell users "this site requires FlareSolverr".
-->

- [ ] No protection — plain HTML, no challenge, no rate limits.
- [ ] Rate limiting only — the site rate-limits but does not challenge.
- [ ] JavaScript challenge (Cloudflare "Just a moment…") — parser needs
      `CloudflareMixin` + FlareSolverr or Playwright.
- [ ] Interactive challenge (Turnstile, hCaptcha, DataDome, …) — the
      parser needs `CloudflareMixin` and the user must configure
      FlareSolverr (a browser is not enough).
- [ ] I don't know.


## Login / cookies

<!--
  Some sites serve their public catalogue without login but require a
  session to read chapters.  Others require login for everything.
  This determines what the parser must declare in
  ``sites.yaml.cookies_required`` and what the user must do to use it.

  IMPORTANT: NexusDL stores cookies **encrypted** on the user's
  machine (Fernet, see `core/session/cookie_manager.py`).  They never
  leave the machine.  You do not need to share any cookie in this
  issue.
-->

- [ ] No login required for any feature.
- [ ] Login required for some features (e.g. chapters, but not search).
      Cookie names required (leave blank if unsure): `<!-- cookie_name_1, cookie_name_2 -->`
- [ ] Login required for every feature (search included).
      Cookie names required: `<!-- cookie_name_1, cookie_name_2 -->`
- [ ] I don't know.


## Capabilities

<!--
  Fill this table using the nine capability fields that
  ``data/sites.yaml`` expects.  These directly determine what the
  parser declares, what the site picker shows, and what search
  features are available.

  If you are not sure, mark "I don't know" — the maintainer will fill
  it in during the audit.
-->

| Capability | Supported? | Notes |
|---|---|---|
| **search** — the site has a working search box | yes / no / unknown | |
| **pagination** — search results are paginated | yes / no / unknown | |
| **filters** — search supports filter parameters (language, genre, status, …) | yes / no / unknown | If yes, list them: |
| **metadata** — manga pages expose authors, description, publication year, status | yes / no / unknown | |
| **cover_art** — manga pages (and ideally search results) expose a cover image URL | yes / no / unknown | |
| **chapter_list** — the manga page lists every chapter | yes / no / unknown | |
| **reader_images** — the chapter reader lists every page as an image | yes / no / unknown | |
| **cloudflare** — Cloudflare challenge is present (see section above) | yes / no / unknown | |
| **js_rendered** — reading pages requires JavaScript execution to obtain the reader images | yes / no / unknown | |


## Reader mechanics (only if the stack is custom)

<!--
  Skip this section if you selected Madara, MangaThemesia or FoolSlide
  above — the mixin already handles the reader.

  Otherwise, describe how the reader works.  Any of these is fine:

    * Single <img> per page, all in the DOM.
    * <img> elements with lazy-loading — the real URL is in data-src,
      data-lazy-src, or data-cfsrc.
    * Reader is an iframe pointing to a third-party host.
    * Reader loads images via an AJAX call after the page loads.
    * Reader obfuscates image URLs in a JavaScript variable
      (e.g. ``var pages = [...]`` like FoolSlide, or a custom
      obfuscation scheme).
    * Reader requires clicking through individual pages (one image
      per request).
    * Something else — describe.

  If the images are hosted on a third-party CDN, list the CDN domain.
-->

<!-- Free-form description -->


## Example URLs

<!--
  Paste 3–5 example URLs from the site, one per line.  These let the
  maintainer audit the site without needing to search for examples
  themselves.  Redact any personal information from the URLs.
-->

```
# Search result page:
https://...

# Manga detail page:
https://...

# Chapter list page (if different):
https://...

# Reader page (first chapter):
https://...
```


## Existing implementation reference (optional but very helpful)

<!--
  If a community extension already exists for this site — for Mihon
  (formerly Tachiyomi), for Keiyoushi, for Aidoku, for Suwayomi, for
  Paperback, for Hakuneko, for Komga, or for any other downloader —
  paste a link here.  These extensions are excellent references
  because they already encode the site's selectors and routing, and
  they save the maintainer hours of reverse-engineering.

  If the extension is a Madara / MangaThemesia / FoolSlide derivative,
  say so explicitly — that tells the maintainer exactly which mixin to
  inherit.
-->

- [ ] **Mihon / Tachiyomi / Keiyoushi extension exists** — link:
- [ ] **Aidoku source exists** — link:
- [ ] **Suwayomi / Tachidesk extension exists** — link:
- [ ] **Hakuneko connector exists** — link:
- [ ] **Komga extension exists** — link:
- [ ] **Paperback source exists** — link:
- [ ] **No existing implementation I am aware of.**


## Site stability

<!--
  Some sites are stable for years.  Others change domain, change
  stack, or disappear every few months.  This affects how much effort
  the maintainer should invest — a parser for a site that may not
  exist in three months is a smaller win than a parser for a site
  that is clearly here to stay.
-->

- [ ] **Very stable** — the site has been online for 2+ years, the
      domain has not changed, and the maintainers are known to be
      responsive.
- [ ] **Stable** — the site has been online for 1+ year.
- [ ] **Recently launched** — less than 1 year old.
- [ ] **Unstable** — the site has changed domain or stack at least
      once in the last 12 months.
- [ ] **I don't know.**


## If the site is not one of the three reusable stacks

<!--
  Adding a custom site parser is a significant piece of work (typically
  200–400 lines plus tests).  Custom parsers are accepted when:

    * The site is stable (see above).
    * No existing mixer covers the stack.
    * A reference implementation exists (Mihon, Aidoku, …).
    * You are willing to submit a PR (see "Contribution" below).

  If none of the above, the maintainer may ask you to reduce the scope
  by targeting only the "best effort" subset — i.e. no filter support,
  no metadata, only search + chapter list + reader.
-->


## Contribution

<!--
  Honest answers help prioritise.  A site request that comes with a
  working PR is typically merged within a week.  A request that
  depends entirely on maintainer time may sit in the backlog for
  months.  Neither is a rejection — many accepted sites are
  implemented by maintainers — but it is useful information.
-->

- [ ] **I can write the parser PR myself** — I have read
      `docs/source/development/adding_parsers.md` and
      `docs/source/development/adding_sites.md`.
- [ ] **I can help** — I can write the test fixtures, run manual
      smoke tests, and validate the parser against the live site.
- [ ] **I am only suggesting the site** — I cannot contribute code.


## Additional context

<!--
  Anything else.  For example:
    - Why you think this site is worth integrating.
    - A comparison with existing supported sites.
    - Unusual behaviour that will trip up a naive parser.
    - Links to community discussions about this site.
-->


<!--
  ════════════════════════════════════════════════════════════════════
  Thank you for taking the time to gather this information.  A complete
  new-site request — with a stack identification, capabilities, example
  URLs, and a reference implementation — can be turned into a working
  parser in under an hour.

  A request that says "please add this site: <url>" typically sits in
  the backlog until someone has the free time to investigate from
  scratch.  The difference is entirely in how much work you do upfront.
  ════════════════════════════════════════════════════════════════════
-->
