# Adding a Site

> **Audience**: contributors who want to support a **new site** (manga,
> webtoon, comic, doujinshi) in NexusDL.
>
> **Prerequisites**: read [Architecture](architecture.md) and
> [ADR-004: sites.yaml vs .env](decisions/004-sites-yaml-vs-env.md).

Adding a site to NexusDL has **two parts**:

1. **Declare the site in `data/sites.yaml`** — metadata, domains,
   capabilities. **This page.**
2. **Implement the parser** in `src/nexusdl/parsers/<language>/<site>.py`
   — scraping logic. → See [Adding a Parser](adding_parsers.md).

Both parts are required. A site without a parser fails at runtime with
`ParserNotFoundError`. A parser without a site entry is unreachable.

```{note}
This guide assumes the site is **public** (no login required). For sites
that require authentication, see the [Credentials](#credentials) section.
```

---

## Table of contents

1. [Before you start](#before-you-start)
2. [Should this site be added?](#should-this-site-be-added)
3. [Site anatomy](#site-anatomy)
4. [Choose the right category](#choose-the-right-category)
5. [Write the `sites.yaml` entry](#write-the-sitesyaml-entry)
6. [Capabilities reference](#capabilities-reference)
7. [Cookies and credentials](#cookies-and-credentials)
8. [Validate the entry](#validate-the-entry)
9. [Test the site](#test-the-site)
10. [Submit a PR](#submit-a-pr)
11. [Troubleshooting](#troubleshooting)

---

## Before you start

### Check if the site is already supported

```bash
# From the NexusDL repo
grep -i "<site-name>" src/nexusdl/data/sites.yaml
```

If the site is already listed, you don't need to add it — maybe the parser
is broken, or the domain changed. Open an issue instead.

### Read the legal and ethical guidelines

- **Respect `robots.txt`** — NexusDL enforces it by default
  (`NEXUSDL_RESPECT_ROBOTS_TXT=true`).
- **Do not scrape content that is illegal** in your jurisdiction or the
  maintainer's.
- **Do not bypass authentication** unless the site explicitly allows it.
- **Do not overload the site** — set a realistic `default_rate_limit_per_second`.
- **Do not add a site that is already dead** — verify it's alive and actively
  updated.

### Make sure you have the right tooling

```bash
# Python 3.12+
python --version

# jsonschema (for validation)
pip install jsonschema

# A browser (for inspecting the site)
# - Firefox Developer Tools
# - Chrome DevTools
```

---

## Should this site be added?

Not every site belongs in NexusDL. Before investing time, check:

| Criterion | Required | Notes |
|---|---|---|
| **Publicly accessible** | ✅ | No paywall, no invite-only |
| **Active** | ✅ | Updated in the last 6 months |
| **Legal in your jurisdiction** | ✅ | You are responsible |
| **Not a duplicate** | ✅ | If it mirrors an existing site, argue why it's worth adding |
| **Has a stable structure** | ⚠️ | Cloudflare / JS-heavy sites are OK, but harder |
| **Has a clean API or HTML** | ⚠️ | Not required, but easier |
| **Adult content** | ⚠️ | Allowed, but `adult: true` is mandatory |

**Sites we reject:**

- ❌ **Pirate aggregators** that mirror content without permission (unless
  the site itself is legitimate)
- ❌ **Sites behind a paywall** — use an API or a user-supplied cookie
  instead (see [Credentials](#credentials))
- ❌ **Sites that are dead or abandoned**
- ❌ **Sites that explicitly forbid scraping** in their ToS (unless the
  user has accepted the risk — document it)

---

## Site anatomy

Every site in `sites.yaml` has the following fields:

```yaml
sites:
  - id: string                          # unique, snake_case
    name: string                        # display name
    domains: list[HttpUrl]              # one or more base URLs
    parser_class: string                # module:Class path
    language: string                    # en, fr, kr, ja, adult, ...
    adult: bool                         # true if the site hosts adult content
    capabilities:                       # what the parser supports
      supports_search: bool
      supports_manga_info: bool
      supports_chapters: bool
      supports_pages: bool
      supports_download: bool
      requires_auth: bool
      requires_cloudflare_bypass: bool
      max_concurrent_downloads: int
      default_rate_limit_per_second: float
    default_headers: map[string, string]  # optional
    cookies_required: list[string]       # optional, cookie NAMES only
```

See [`sites_schema.json`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/data/sites_schema.json)
for the complete schema.

---

## Choose the right category

Sites are grouped by **language** in the filesystem:

| Folder | Language | Example sites |
|---|---|---|
| `parsers/fr/` | French | SushiScan, Anime-Sama |
| `parsers/en/` | English | MangaDex, AsuraScans |
| `parsers/kr/` | Korean | ManhwaClub |
| `parsers/adult/` | Adult | nhentai, HentaiZone |
| `parsers/custom/` | — | Template only (not registered) |

**Which folder?**

- If the site is **primarily in French** → `fr/`
- If the site is **primarily in English** → `en/`
- If the site is **primarily in Korean** → `kr/`
- If the site is **adult content** (any language) → `adult/`

For **multi-language sites** (e.g. MangaDex supports 20+ languages), pick
the **predominant** one. MangaDex is in `en/` because its primary API and
community are English.

**Language enum values:** `FR`, `EN`, `JA`, `KO`, `ZH`, `ES`, `PT`, `IT`,
`DE`, `RU`, `AR`, `UNKNOWN`.

**Adult category:** use `language: adult` and `adult: true`. The parser
will only be loaded if `NEXUSDL_ALLOW_ADULT=true`.

---

## Write the `sites.yaml` entry

Open `src/nexusdl/data/sites.yaml` and add your site under the `sites:`
key.

### Minimum viable entry

```yaml
sites:
  - id: mysite
    name: MySite
    domains:
      - https://mysite.example.com
    parser_class: "nexusdl.parsers.en.mysite:MySiteParser"
    language: en
    adult: false
    capabilities:
      supports_search: true
      supports_manga_info: true
      supports_chapters: true
      supports_pages: true
      supports_download: true
      requires_auth: false
      requires_cloudflare_bypass: false
      max_concurrent_downloads: 4
      default_rate_limit_per_second: 2.0
```

That's all you need for a **simple, public, HTML-based site**.

### Full entry with all fields

```yaml
sites:
  - id: mysite
    name: MySite
    domains:
      - https://mysite.example.com
      - https://mysite-mirror.example.org
    parser_class: "nexusdl.parsers.en.mysite:MySiteParser"
    language: en
    adult: false
    capabilities:
      supports_search: true
      supports_manga_info: true
      supports_chapters: true
      supports_pages: true
      supports_download: true
      requires_auth: false
      requires_cloudflare_bypass: false
      max_concurrent_downloads: 4
      default_rate_limit_per_second: 2.0
    default_headers:
      User-Agent: "NexusDL/0.1.0 (+https://github.com/Voldigoad-Holdings/nexus-dl)"
      Referer: "https://mysite.example.com"
      Accept-Language: "en-US,en;q=0.9"
    cookies_required: []
```

### Field-by-field guide

#### `id` — unique, snake_case

```yaml
id: mysite
```

- **Lowercase**, **snake_case** (no dashes, no dots)
- **Must match** `BaseParser.site_id` in the parser module
- **Must be unique** across all sites
- **Used in** URLs, logs, events, and the `NEXUSDL_<ID>_*` env prefix

#### `name` — display name

```yaml
name: MySite
```

- **Human-readable** name
- **Used in** the CLI, TUI, GUI, and Web UI
- **Not unique** (two sites can have the same name in theory, but avoid it)
- **Preserve original spelling** (`MangaDex`, not `Mangadex`)

#### `domains` — one or more base URLs

```yaml
domains:
  - https://mysite.example.com
  - https://mysite-mirror.example.org
```

- **Protocol required** (`https://` preferred)
- **No trailing slash**
- **Multiple domains** for mirrors, `.onion` sites, etc.
- **First domain** is the primary; used in `_resolve_url()` if relative

**Do NOT include** paths, query strings, or fragments. Just the origin.

#### `parser_class` — module:Class path

```yaml
parser_class: "nexusdl.parsers.en.mysite:MySiteParser"
```

- **Format**: `<module_path>:<ClassName>`
- **Module path** must match the file location
- **Class name** must match the class in the file

**Common mistakes:**

| Wrong | Correct |
|---|---|
| `nexusdl.parsers.en.mysite.MySiteParser` | `nexusdl.parsers.en.mysite:MySiteParser` |
| `parsers.en.mysite:MySiteParser` | `nexusdl.parsers.en.mysite:MySiteParser` |
| `nexusdl.parsers.en.mysite:mysiteparser` | `nexusdl.parsers.en.mysite:MySiteParser` |

#### `language` — primary language

```yaml
language: en
```

- **Lowercase**: `en`, `fr`, `kr`, `ja`, `zh`, `es`, `pt`, `it`, `de`, `ru`, `ar`
- **`adult`** is a special value: `language: adult`
- **Used in** filters (`nexusdl sites --language fr`)

#### `adult` — boolean

```yaml
adult: false
```

- **`false`** for general content (default)
- **`true`** for 18+ content
- **Adult sites are only loaded** if `NEXUSDL_ALLOW_ADULT=true`
- **Metadata**, not policy — see [ADR-004](decisions/004-sites-yaml-vs-env.md)

#### `capabilities` — what the parser supports

See [Capabilities reference](#capabilities-reference).

#### `default_headers` — optional HTTP headers

```yaml
default_headers:
  User-Agent: "NexusDL/0.1.0"
  Referer: "https://mysite.example.com"
  Accept-Language: "en-US,en;q=0.9"
```

- **Sent with every request** made by this parser
- **Merged** with `HttpSession`'s default headers (parser wins)
- **Case-sensitive** as per HTTP spec (`User-Agent`, not `user-agent`)

**Do NOT include** `Cookie`, `Authorization`, or anything secret. Those
live in `.env` (see [Credentials](#credentials)).

#### `cookies_required` — optional cookie names

```yaml
cookies_required:
  - cf_clearance
  - cf_bm
```

- **Names only**, not values
- **Values come from** `.env` (`NEXUSDL_<SITE>_<COOKIE_NAME>`)
- **Used by** `CookieManager` to build the `Cookie` header
- **Empty list** if the site works without cookies

---

## Capabilities reference

Every capability has a **required** boolean, plus two numeric tuning
parameters.

| Capability | Type | Default | Meaning |
|---|---|---|---|
| `supports_search` | bool | `true` | Parser can search by query |
| `supports_manga_info` | bool | `true` | Parser can return manga metadata |
| `supports_chapters` | bool | `true` | Parser can list chapters |
| `supports_pages` | bool | `true` | Parser can list pages of a chapter |
| `supports_download` | bool | `true` | Parser can download pages |
| `requires_auth` | bool | `false` | Site requires login (credentials in `.env`) |
| `requires_cloudflare_bypass` | bool | `false` | Site is behind Cloudflare; uses Playwright/FlareSolverr |
| `max_concurrent_downloads` | int | `4` | Max parallel page downloads for this site |
| `default_rate_limit_per_second` | float | `2.0` | Max requests per second for this site |

### Setting `max_concurrent_downloads`

**Rules of thumb:**

- **`1`** for slow, fragile sites (respect the site)
- **`2–4`** for most sites (default `4`)
- **`8`** for fast, robust sites (APIs)
- **`> 8`** almost never (bans incoming)

### Setting `default_rate_limit_per_second`

**Rules of thumb:**

- **`0.5`** for very slow sites (respect the site)
- **`1.0–2.0`** for most sites (default `2.0`)
- **`5.0`** for fast APIs (rare)
- **`> 10`** almost never (bans incoming)

If unsure, **start low** and increase based on observed behavior.

### Setting `requires_auth: true`

Set to `true` if the site requires login **for any operation** (search,
manga info, chapters, pages). If login is only required for adult content,
use `adult: true` instead.

For **optional auth** (login gives extra features but isn't required), set
`requires_auth: false` and document the optional login in the parser.

### Setting `requires_cloudflare_bypass: true`

Set to `true` if the site:

- Returns a Cloudflare challenge (5xx, "Just a moment...")
- Requires JavaScript rendering to show content
- Uses Cloudflare Turnstile

NexusDL will use **Playwright** (if `NEXUSDL_PLAYWRIGHT_ENABLED=true`) or
**FlareSolverr** (if `NEXUSDL_FLARESOLVERR_URL` is set) to bypass the
challenge.

See [Cloudflare mixin](adding_parsers.md#mixins) for implementation details.

---

## Cookies and credentials

Some sites require cookies (Cloudflare clearance) or credentials (login).
These **must not** be in `sites.yaml` — they go in `.env`.

### Cookies

Declare cookie **names** in `sites.yaml`:

```yaml
cookies_required:
  - cf_clearance
  - cf_bm
```

Provide cookie **values** in `.env`:

```dotenv
NEXUSDL_MYSITE_CF_CLEARANCE=<value-from-browser>
NEXUSDL_MYSITE_CF_BM=<value-from-browser>
```

`CookieManager` merges them into the `Cookie` header for every request.

### Credentials (login required)

Set `requires_auth: true` in `sites.yaml`:

```yaml
requires_auth: true
```

Provide credentials in `.env`:

```dotenv
NEXUSDL_MYSITE_USERNAME=alice
NEXUSDL_MYSITE_PASSWORD=<password>
```

The parser reads them via `settings.mysite_username` and
`settings.mysite_password`. See
[Adding a Parser → Authentication](adding_parsers.md#authentication) for
the implementation.

### Update `.env.example`

Whenever you add a new credential or cookie, also add it **commented out**
to `.env.example`:

```dotenv
# ── MySite ──
# NEXUSDL_MYSITE_USERNAME=
# NEXUSDL_MYSITE_PASSWORD=
# NEXUSDL_MYSITE_CF_CLEARANCE=
```

This documents the requirement without exposing real values.

---

## Validate the entry

NexusDL validates `sites.yaml` against `sites_schema.json` in CI and
pre-commit. Run the validator locally:

```bash
python scripts/validate_sites.py
```

**Expected output:**

```
✅ sites.yaml valid — 62 site(s) declared.
```

**Common errors:**

| Error | Cause | Fix |
|---|---|---|
| `'id' is a required property` | Missing field | Add the field |
| `'parser_class' does not match pattern` | Wrong format | Use `module:Class` |
| `'domains' must contain at least one item` | Empty list | Add at least one URL |
| `'language' must be one of [...]` | Invalid language | Use `en`, `fr`, `kr`, `adult`, etc. |
| `'adult' must be a boolean` | String instead of bool | Use `true` / `false` (no quotes) |
| `additionalProperties 'foo' not allowed` | Unknown field | Remove or rename |

Fix every error before committing.

---

## Test the site

After adding the `sites.yaml` entry **and** implementing the parser (see
[Adding a Parser](adding_parsers.md)), test it:

### 1. Verify the site is loaded

```bash
nexusdl sites --language en
# → must include "mysite"
```

### 2. Test search

```bash
nexusdl search "one piece" --site mysite
# → must return at least 1 result
```

### 3. Test manga info

```bash
nexusdl info "https://mysite.example.com/manga/one-piece"
# → must print title, author, chapters
```

### 4. Test download

```bash
nexusdl download "https://mysite.example.com/manga/one-piece" \
  --chapters 1 \
  --format CBZ \
  --dest /tmp/test
# → must produce /tmp/test/One Piece/Chapter 001.cbz
```

### 5. Test rate limit

```bash
nexusdl download "..." --chapters 1-5 --dest /tmp/test
# → must not trigger 429 or ban
```

### 6. Run the smoke test

```bash
pytest tests/unit/parsers/test_mysite_smoke.py -v
# → must pass
```

If any of these fail, see [Adding a Parser → Debugging](adding_parsers.md#debugging).

---

## Submit a PR

Follow the general [Contributing guide](contributing.md) and
[CONTRIBUTING.md](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md).

### PR checklist

- [ ] `sites.yaml` entry added
- [ ] `.env.example` updated (if new cookies/credentials)
- [ ] Parser implemented (see [Adding a Parser](adding_parsers.md))
- [ ] `python scripts/validate_sites.py` passes
- [ ] `python scripts/check_imports.py` passes
- [ ] `ruff check src tests` passes
- [ ] `ruff format --check src tests` passes
- [ ] `mypy src` passes
- [ ] `pytest tests/unit/parsers/test_mysite_smoke.py` passes
- [ ] Manual test: `nexusdl search "..." --site mysite` returns results
- [ ] Manual test: `nexusdl download "..." --chapters 1` produces a valid CBZ
- [ ] Rate limit is realistic (default `2.0`, not `10.0`)
- [ ] No secrets in `sites.yaml`
- [ ] No `nexusdl.dev` / `api.nexusdl` URLs (see [ADR-004](decisions/004-sites-yaml-vs-env.md))
- [ ] `CHANGELOG.md` updated (if notable)

### PR description template

```markdown
## Add site: MySite

- **ID**: `mysite`
- **Language**: `en`
- **Category**: `en/`
- **Adult**: ❌ no
- **Requires auth**: ❌ no
- **Requires Cloudflare bypass**: ❌ no
- **Max concurrent downloads**: `4`
- **Rate limit**: `2.0` req/s

### Sample URLs used for testing

- Search: `https://mysite.example.com/search?q=one+piece`
- Manga: `https://mysite.example.com/manga/one-piece`
- Chapter: `https://mysite.example.com/manga/one-piece/chapter-1`

### Known limitations

- None.

### Checklist

- [x] `sites.yaml` entry added
- [x] `.env.example` updated (N/A, no cookies)
- [x] Parser implemented
- [x] All CI checks pass
- [x] Manual test: search returns results
- [x] Manual test: download produces a valid CBZ
```

---

## Troubleshooting

### `ParserNotFoundError: No parser registered for site 'mysite'`

**Cause:** `sites.yaml` has the entry, but the parser module doesn't call
`register("mysite", MySiteParser)`.

**Fix:** add at the bottom of `mysite.py`:

```python
from nexusdl.parsers.factory import register
register("mysite", MySiteParser)
```

Then restart NexusDL (or re-run the command).

### `KeyError: 'mysite'` in `nexusdl sites`

**Cause:** `sites.yaml` doesn't have the entry, or the YAML is invalid.

**Fix:** run `python scripts/validate_sites.py` and fix the error.

### `ValueError: Invalid language: 'xx'`

**Cause:** `language` field is not a valid `Language` enum value.

**Fix:** use one of: `en`, `fr`, `kr`, `ja`, `zh`, `es`, `pt`, `it`, `de`,
`ru`, `ar`, `adult`.

### Site shows up in search but chapters fail

**Cause:** parser's `get_chapters()` is not implemented, or the site
changed its HTML.

**Fix:** see [Adding a Parser → Debugging](adding_parsers.md#debugging).

### Cloudflare blocks all requests

**Cause:** `requires_cloudflare_bypass: true` is set, but Playwright or
FlareSolverr is not enabled.

**Fix:**

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
# or
NEXUSDL_FLARESOLVERR_URL=http://localhost:8191/v1
```

Then re-run.

### Rate limit is triggered (`429 Too Many Requests`)

**Cause:** `default_rate_limit_per_second` is too high, or
`max_concurrent_downloads` is too high.

**Fix:** lower them:

```yaml
capabilities:
  max_concurrent_downloads: 2
  default_rate_limit_per_second: 1.0
```

---

## See also

- [Adding a Parser](adding_parsers.md) — implement the scraping logic
- [Architecture](architecture.md) — how parsers fit in the system
- [ADR-004: sites.yaml vs .env](decisions/004-sites-yaml-vs-env.md) — configuration split
- [Plugins](plugins.md) — add a site via a plugin (no core changes)
- [`sites.yaml`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/data/sites.yaml) — the current site list
- [`sites_schema.json`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/data/sites_schema.json) — the JSON Schema

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
