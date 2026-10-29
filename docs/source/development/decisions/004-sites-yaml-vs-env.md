# ADR-004: `sites.yaml` vs `.env` Separation

- **Status**: ✅ Accepted
- **Date**: 2026-10-28
- **Deciders**: Voldigoad-Holdings (lead maintainer)
- **Supersedes**: —
- **Superseded by**: —
- **Related**: [ADR-001: Hexagonal Architecture](001-hexagonal-architecture.md)

---

## Context

NexusDL supports **60+ sites**, each with:

- A unique ID (`mangadex`, `sushiscan`, `nhentai`, …)
- Multiple **domains** (`https://mangadex.org`, mirror URLs)
- A **parser class** (`nexusdl.parsers.en.mangadex:MangaDexParser`)
- **Capabilities** (search, chapters, Cloudflare, rate limit, …)
- A **language** (`en`, `fr`, `kr`, `adult`, …)
- Default **headers** (User-Agent, Referer, …)
- Optional **cookies** (`cf_clearance`, `_cf_bm`, …)
- **Optional** credentials for authenticated sites

Additionally, NexusDL itself has:

- **Secrets** (`SECRET_KEY`, `JWT_SECRET`, `COOKIE_ENCRYPTION_KEY`)
- **Deployment URLs** (`NEXUSDL_PUBLIC_URL`, `NEXUSDL_API_BASE_URL`,
  `NEXUSDL_FRONTEND_URL`)
- **Runtime config** (`LOG_LEVEL`, `MAX_CONCURRENT_DOWNLOADS`, …)
- **Feature flags** (`ALLOW_ADULT`, `PLAYWRIGHT_ENABLED`, …)

We need to decide **where each piece of configuration lives**.

### The naive approach — everything in `.env`

```dotenv
NEXUSDL_MANGADEX_URL=https://mangadex.org
NEXUSDL_MANGADEX_PARSER=nexusdl.parsers.en.mangadex:MangaDexParser
NEXUSDL_MANGADEX_LANGUAGE=en
NEXUSDL_MANGADEX_ADULT=false
NEXUSDL_MANGADEX_SUPPORTS_SEARCH=true
NEXUSDL_MANGADEX_SUPPORTS_CHAPTERS=true
NEXUSDL_MANGADEX_REQUIRES_CLOUDFLARE=false
NEXUSDL_MANGADEX_MAX_CONCURRENT=4
NEXUSDL_MANGADEX_RATE_LIMIT=2.0
NEXUSDL_MANGADEX_HEADERS={"User-Agent":"NexusDL/0.1.0"}
# ... 60 sites × 10+ variables = 600+ lines
# ... plus mirror URLs, cookies, credentials, etc.
```

This approach creates **five concrete problems**.

### Problem 1 — `.env` is **not versioned**

`.env` is in `.gitignore` (correctly). So if the site list lives in `.env`:

- Every new contributor must **manually recreate** 600+ lines
- CI can't run parser tests without a **fixture `.env`**
- Docker images can't ship the default site list
- The community can't review or improve the site list
- A typo in one variable = a broken site, invisible to CI

### Problem 2 — Secrets and metadata are mixed

`.env` is where **secrets** live. If site URLs and capability flags are
also in `.env`, then:

- Every `.env` must be treated as a **secret file** (permissions `600`)
- A site URL in a CI log = **false positive** for secret leak
- A developer sharing their `.env` for debugging = **potential secret leak**
- Any tool that scans `.env` for secrets (Dependabot, GitHub secret
  scanning) will **flag 60+ site URLs** as false positives

### Problem 3 — Debugging a broken site is painful

If `mangadex` fails, the fix must go into `.env`:

- `.env` is not in Git → **no PR review**
- `.env` is not in GitHub search → **no way to find similar issues**
- `.env` is user-specific → **no way to reproduce the bug**

With YAML, a broken site is a **Git commit** with a **PR review**, a
**GitHub issue link**, and a **reproduction case** (the YAML entry).

### Problem 4 — Feature flags are scattered

If `ALLOW_ADULT` lives in `.env` (correct — it's deployment-specific),
but `sites[nhentai].adult` also lives in `.env`, then:

- Two places must agree (`ALLOW_ADULT=true` AND `nhentai.adult=true`)
- A user who wants to **add** an adult site must edit `.env` (not
  versioned) AND `sites.yaml` (versioned)
- The **rule** "adult sites are opt-in" is in **two different files**

With YAML:

- `sites[nhentai].adult: true` is **metadata** (the site IS adult)
- `ALLOW_ADULT: false` is **policy** (this deployment allows adult)

The two are **orthogonal**, not redundant. ✅

### Problem 5 — Testing requires a real `.env`

Unit tests for `SiteRegistry` would need to construct 60 `SiteConfig`
objects from `.env` variables:

```python
# Ugly, verbose, error-prone
site = SiteConfig(
    id=os.environ["NEXUSDL_MANGADEX_ID"],
    url=os.environ["NEXUSDL_MANGADEX_URL"],
    parser=os.environ["NEXUSDL_MANGADEX_PARSER"],
    language=os.environ["NEXUSDL_MANGADEX_LANGUAGE"],
    # ... 10 more
)
```

With YAML:

```python
# Clean, one line
site = registry.get_site("mangadex")
```

### Requirement

We need a separation that:

1. **Keeps secrets** in `.env` (not versioned)
2. **Keeps site metadata** in a versioned, reviewable file
3. **Does not mix** secrets with public metadata
4. **Allows** the community to add sites via PRs
5. **Allows** CI to run tests without a real `.env`
6. **Allows** Docker images to ship the default site list
7. **Keeps** the boundary **explicit** and **enforceable**

---

## Decision

We adopt the following **strict separation**:

| Category | Where | Versioned? | Example |
|---|---|---|---|
| **Secrets** | `.env` | ❌ | `NEXUSDL_JWT_SECRET=...` |
| **Deployment URLs** | `.env` | ❌ | `NEXUSDL_PUBLIC_URL=https://nexusdl.example.com` |
| **App branding** | `.env` | ❌ | `NEXUSDL_APP_NAME=NexusDL` |
| **Feature flags** | `.env` | ❌ | `NEXUSDL_ALLOW_ADULT=false` |
| **Runtime tuning** | `.env` | ❌ | `NEXUSDL_LOG_LEVEL=INFO` |
| **Site metadata** | `sites.yaml` | ✅ | `mangadex.domains`, `mangadex.capabilities` |
| **Site capabilities** | `sites.yaml` | ✅ | `supports_search: true` |
| **Site default headers** | `sites.yaml` | ✅ | `User-Agent: "NexusDL/0.1.0"` |
| **Site language** | `sites.yaml` | ✅ | `language: en` |
| **Site adult flag** | `sites.yaml` | ✅ | `adult: false` |
| **Optional site cookies** | `.env` | ❌ | `NEXUSDL_NHENTAI_CF_CLEARANCE=...` |
| **Optional site credentials** | `.env` | ❌ | `NEXUSDL_NHENTAI_USERNAME=...` |
| **Default config** | `data/default_config.yaml` | ✅ | `max_concurrent_downloads: 3` |

### The rule

> **If a value is public, stable, and identical across deployments → `sites.yaml`.**
> **If a value is a secret, deployment-specific, or varies by user → `.env`.**

### Decision grid

| Value | Public? | Stable? | Same across deploys? | Where |
|---|---|---|---|---|
| Site URL (`https://mangadex.org`) | ✅ | ✅ | ✅ | `sites.yaml` |
| Site parser class | ✅ | ✅ | ✅ | `sites.yaml` |
| Site capabilities | ✅ | ✅ | ✅ | `sites.yaml` |
| Site language | ✅ | ✅ | ✅ | `sites.yaml` |
| Site adult flag (metadata) | ✅ | ✅ | ✅ | `sites.yaml` |
| JWT secret | ❌ | ✅ | ❌ | `.env` |
| Cookie encryption key | ❌ | ✅ | ❌ | `.env` |
| Public URL | ✅ | ❌ | ❌ | `.env` |
| Log level | ✅ | ❌ | ❌ | `.env` |
| `ALLOW_ADULT` (policy) | ✅ | ✅ | ❌ | `.env` |
| nhentai username | ❌ | ❌ | ❌ | `.env` |
| nhentai cookie | ❌ | ❌ | ❌ | `.env` |

The two `.env` entries for adult content are **orthogonal**:

- `sites.yaml[nhentai].adult: true` — the **site** is adult (metadata)
- `NEXUSDL_ALLOW_ADULT=false` — this **deployment** allows adult (policy)

Both must be true for nhentai to appear. A user who doesn't want adult
content never sees nhentai. A user who wants it flips one env var.
**Zero code change.**

### File structure

```
src/nexusdl/data/
├── sites.yaml              ← ✅ versioned, public metadata
├── sites_schema.json       ← ✅ versioned, JSON Schema for validation
├── default_config.yaml     ← ✅ versioned, runtime defaults
└── ...

.env                        ← ❌ not versioned, secrets + deployment config
.env.example                ← ✅ versioned, template (no real values)
```

### `sites.yaml` — public metadata

```yaml
# src/nexusdl/data/sites.yaml
sites:
  - id: mangadex
    name: MangaDex
    domains:
      - https://mangadex.org
    parser_class: "nexusdl.parsers.en.mangadex:MangaDexParser"
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
    cookies_required: []

  - id: nhentai
    name: nhentai
    domains:
      - https://nhentai.net
    parser_class: "nexusdl.parsers.adult.nhentai:NhentaiParser"
    language: adult
    adult: true                     # ← metadata, not policy
    capabilities:
      supports_search: true
      requires_cloudflare_bypass: true
      # ...
    cookies_required:
      - cf_clearance                # ← which cookie is needed, not its value
```

**Key observations:**

- **No secrets.** Nothing in this file would hurt if leaked.
- **No deployment URLs.** `nexusdl.dev` is **not** in this file.
- **No credentials.** `nhentai` username is **not** in this file.
- **`cookies_required` lists cookie *names***, not values. The **values**
  are in `.env` (see below).

### `.env.example` — template (no real values)

```dotenv
# Secrets (generate with: python -c "import secrets; print(secrets.token_hex(32))")
NEXUSDL_SECRET_KEY=CHANGE_ME_64_HEX_CHARS
NEXUSDL_JWT_SECRET=CHANGE_ME_64_HEX_CHARS
NEXUSDL_COOKIE_ENCRYPTION_KEY=CHANGE_ME_64_HEX_CHARS

# Deployment URLs
NEXUSDL_PUBLIC_URL=https://nexusdl.example.com
NEXUSDL_API_BASE_URL=https://api.nexusdl.example.com
NEXUSDL_FRONTEND_URL=https://nexusdl.example.com

# Feature flags
NEXUSDL_ALLOW_ADULT=false

# Optional site credentials (uncomment if the site requires auth)
# NEXUSDL_NHENTAI_USERNAME=
# NEXUSDL_NHENTAI_PASSWORD=
# NEXUSDL_MANGADEX_CLIENT_ID=
# NEXUSDL_MANGADEX_CLIENT_SECRET=
```

### How optional site cookies work

For `nhentai`, `sites.yaml` declares:

```yaml
cookies_required:
  - cf_clearance
```

This means: "The `cf_clearance` cookie is required for the parser to work."
The **value** is read from `.env`:

```dotenv
NEXUSDL_NHENTAI_CF_CLEARANCE=<value-from-browser>
```

`CookieManager` (in `core/session/`) merges:

1. Cookies from `sites.yaml[site].cookies_required` (names only)
2. Cookie values from `.env` (`NEXUSDL_<SITE>_<COOKIE_NAME>`)
3. Cookies from the persistent store (`~/.config/nexusdl/cookies.enc`)

This separation means:

- **`sites.yaml`** says "nhentai needs a `cf_clearance` cookie" (public,
  versioned, reviewable)
- **`.env`** says "here is the cf_clearance value for my session"
  (private, not versioned)
- **Cookie store** persists cookies across runs (encrypted)

### How `Settings` reads both

```python
# src/nexusdl/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEXUSDL_",
        env_file=".env",
        extra="forbid",
    )

    # Deployment (from .env)
    app_name: str = "NexusDL"
    public_url: HttpUrl
    api_base_url: HttpUrl
    frontend_url: HttpUrl
    secret_key: SecretStr
    jwt_secret: SecretStr
    cookie_encryption_key: SecretStr
    allow_adult: bool = False
    log_level: str = "INFO"

    # Optional site credentials (from .env)
    nhentai_username: str | None = None
    nhentai_password: SecretStr | None = None
    nhentai_cf_clearance: SecretStr | None = None
    mangadex_client_id: str | None = None
    mangadex_client_secret: SecretStr | None = None
```

```python
# src/nexusdl/core/registry/site_registry.py
class SiteRegistry:
    def _load_sites(self) -> None:
        """Load sites.yaml (metadata) and merge with .env (secrets)."""
        raw = yaml.safe_load(
            importlib.resources.files("nexusdl.data")
            .joinpath("sites.yaml")
            .read_text(encoding="utf-8")
        )
        for entry in raw["sites"]:
            site = SiteConfig.model_validate(entry)

            # Merge optional cookies from .env
            for cookie_name in site.cookies_required:
                env_var = f"{site.id.upper()}_{cookie_name.upper()}"
                value = getattr(self._settings, env_var.lower(), None)
                if value is not None:
                    site.cookies[cookie_name] = (
                        value.get_secret_value()
                        if hasattr(value, "get_secret_value")
                        else value
                    )

            self._sites[site.id] = site
```

### Enforcement

The separation is **enforced** by two mechanisms:

#### 1. `scripts/check_imports.py` — detects hardcoded URLs/secrets

```python
# scripts/check_imports.py (excerpt)
FORBIDDEN_PATTERNS = [
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("NexusDL prod URL", re.compile(r"https?://(?:api\.)?nexusdl\.[a-z]+")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
    ("Hardcoded password", re.compile(
        r"""(?:password|passwd|pwd)\s*=\s*["'][^"']{6,}["']""", re.IGNORECASE
    )),
]
```

If a Python file contains `https://nexusdl.dev` or `ghp_...` **hardcoded**,
the CI fails. ✅ This prevents the very error the ADR is trying to avoid.

#### 2. `scripts/validate_sites.py` — validates `sites.yaml` against schema

```bash
$ python scripts/validate_sites.py
✅ sites.yaml valid — 61 site(s) declared.
```

The JSON Schema (`sites_schema.json`) **forbids**:

- `secret`, `password`, `token`, `credential` fields (any casing)
- URLs containing `nexusdl.` (our own app)
- Unknown keys (`additionalProperties: false`)

If a contributor accidentally adds a secret to `sites.yaml`, the validator
**rejects** it. ✅

### Anti-patterns — what NOT to do

```yaml
# ❌ WRONG — secret in sites.yaml
- id: nhentai
  username: alice
  password: my-secret-password

# ❌ WRONG — deployment URL in sites.yaml
- id: my-nexusdl
  domains:
    - https://nexusdl.dev

# ❌ WRONG — feature flag in sites.yaml
- id: mangadex
  enabled: true        # ← policy, not metadata

# ✅ RIGHT — cookies_required lists NAMES, not values
- id: nhentai
  cookies_required:
    - cf_clearance
```

```dotenv
# ❌ WRONG — site URL in .env
NEXUSDL_MANGADEX_URL=https://mangadex.org

# ❌ WRONG — site capabilities in .env
NEXUSDL_MANGADEX_SUPPORTS_SEARCH=true

# ✅ RIGHT — only secrets and deployment config
NEXUSDL_JWT_SECRET=...
NEXUSDL_PUBLIC_URL=https://nexusdl.example.com
NEXUSDL_ALLOW_ADULT=false
NEXUSDL_NHENTAI_CF_CLEARANCE=<value>
```

---

## Consequences

### Positive

- ✅ **Single source of truth** — `sites.yaml` is the canonical site list
- ✅ **Secrets stay secret** — no secret ever appears in `sites.yaml`
- ✅ **Community contributions** — adding a site is a **PR** on
  `sites.yaml`, reviewed by maintainers
- ✅ **CI can test** — `sites.yaml` is present in the repo, no `.env`
  fixture needed
- ✅ **Docker images** — ship the default `sites.yaml` (60 sites work
  out-of-the-box)
- ✅ **Debugging** — a broken site is a Git commit, with `git blame`,
  `git log`, and a PR review
- ✅ **Security scanning** — no false positives from 60+ site URLs
- ✅ **Deployment flexibility** — `ALLOW_ADULT`, `PLAYWRIGHT_ENABLED`,
  URLs can differ between deployments **without touching `sites.yaml`**
- ✅ **Schema validation** — `sites_schema.json` prevents mistakes (typos,
  unknown keys, secrets in metadata)
- ✅ **Orthogonal policies** — `sites[nhentai].adult` (metadata) and
  `ALLOW_ADULT` (policy) are independent

### Negative

- ⚠️ **Two places to update** — adding a site requires editing
  `sites.yaml` (metadata) and, if credentials are needed, `.env.example`
  (template). Mitigated by clear docs and a **PR template** checklist.
- ⚠️ **Learning curve** — new contributors must understand the
  distinction. Mitigated by this ADR, `CONTRIBUTING.md`, and clear error
  messages.
- ⚠️ **`sites.yaml` is public** — site URLs are visible to all. This is
  **intentional**: these are public websites, not secrets. The only
  sensitive data (cookies, credentials) lives in `.env`.
- ⚠️ **Community PRs on `sites.yaml`** — maintainers must review carefully.
  Mitigated by `scripts/validate_sites.py` in CI.

### Neutral

- 🟡 **`cookies_required` uses names, not values** — this is a slight
  indirection. A contributor must understand that `cookies_required:
  [cf_clearance]` means "the user needs to put
  `NEXUSDL_NHENTAI_CF_CLEARANCE` in `.env`". Mitigated by docs.
- 🟡 **`sites.yaml` size** — 60+ sites ≈ 1500 lines. Acceptable for a
  single YAML file. If it grows to 200+, we may split per language
  (`sites_fr.yaml`, `sites_en.yaml`, …). **Future enhancement, not a
  blocker.**

---

## Alternatives considered

### Alternative A — Everything in `.env` (rejected)

**Rejected because:** unversionable, mixes secrets with metadata, no CI,
no Docker defaults, no community PRs. See "Problem 1–5" above.

### Alternative B — Everything in `sites.yaml` (rejected)

**Idea:** put secrets in `sites.yaml` too, with a `secrets:` section.

**Rejected because:**

- **Versioning secrets is catastrophic** — they end up in Git history
  forever, even if removed later.
- **`sites.yaml` is committed** — every contributor sees every secret.
- **CI can't distinguish** real secrets from fake ones for tests.
- **GitHub secret scanning** would flag every `sites.yaml` commit.
- **This violates GPL-3.0 best practices** and general security
  hygiene.

### Alternative C — Separate `sites.yaml` and `secrets.yaml` (rejected)

**Idea:** two YAML files, `sites.yaml` (public) and `secrets.yaml`
(private, `.gitignore`d).

**Rejected because:**

- **`secrets.yaml` still has the `.env` problems** — unversionable, no
  CI, no Docker defaults.
- **`.env` is the ecosystem standard** — tools like Dependabot, GitHub
  Actions, Docker Compose, Kubernetes all natively support `.env` or
  env vars. YAML secrets would require custom tooling.
- **`.env` is more portable** — every deployment platform (Heroku,
  Railway, Fly.io, Vercel, AWS ECS) supports env vars, not YAML secrets.

### Alternative D — Environment variables for everything, no files (rejected)

**Idea:** pass every value via env vars at runtime, no config files.

**Rejected because:**

- **60 sites × 10 values = 600 env vars** — unmanageable.
- **No validation** — typos cause silent failures.
- **No documentation** — contributors can't see the list of sites.
- **No PR review** — adding a site requires editing a deployment config,
  not a Git file.

### Alternative E — Database-backed config (rejected)

**Idea:** store sites in a SQLite table, admin UI to edit them.

**Rejected because:**

- **Overkill** for 60 static site definitions.
- **No PRs** — changes are invisible to reviewers.
- **No version control** — you'd need to commit database dumps.
- **Bootstrap problem** — how do you seed the DB on first run? With YAML.
- **Not portable** — a Docker image would need to rebuild the DB.

### Alternative F — Env vars for secrets + YAML for sites (chosen)

**Chosen because:** it's the **simplest** separation that satisfies all 7
requirements. It aligns with the ecosystem (`.env` is standard), it
supports PR reviews (`sites.yaml`), it keeps secrets safe (`.env` is
gitignored), and it allows CI to run (no `.env` fixture needed).

---

## Examples

### Example 1 — Adding a new site (community PR)

Contributor wants to add `MySite`:

```yaml
# PR: src/nexusdl/data/sites.yaml
sites:
  # ... existing sites ...
  - id: mysite
    name: MySite
    domains:
      - https://mysite.example.com
    parser_class: "nexusdl.parsers.en.mysite:MySiteParser"
    language: en
    adult: false
    capabilities:
      supports_search: true
      requires_cloudflare_bypass: false
      max_concurrent_downloads: 4
      default_rate_limit_per_second: 2.0
    default_headers:
      User-Agent: "NexusDL/0.1.0 (+https://github.com/Voldigoad-Holdings/nexus-dl)"
    cookies_required: []
```

```python
# PR: src/nexusdl/parsers/en/mysite.py
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register


class MySiteParser(BaseParser):
    site_id = "mysite"
    language = "en"
    adult = False

    async def search(self, query, *, page=1):
        ...
    # ...


register("mysite", MySiteParser)
```

```bash
# CI runs automatically:
$ python scripts/check_imports.py
✅ Architecture OK

$ python scripts/validate_sites.py
✅ sites.yaml valid — 62 site(s) declared.

$ pytest tests/unit/parsers/test_mysite_smoke.py
✅ Smoke test passed
```

**No `.env` change needed.** The site appears in `nexusdl sites`.

### Example 2 — Enabling adult content (user policy)

```dotenv
# User's .env (not versioned)
NEXUSDL_ALLOW_ADULT=true
```

```bash
$ nexusdl sites --include-adult
# nhentai, hentaizone, pururin, scan-hentai, x-manga
```

**No `sites.yaml` change needed.** The sites were always there; they were
filtered out by policy.

### Example 3 — Adding a Cloudflare cookie for nhentai

```dotenv
# User's .env
NEXUSDL_NHENTAI_CF_CLEARANCE=<value-from-browser-cookies>
```

```yaml
# sites.yaml (unchanged)
- id: nhentai
  cookies_required:
    - cf_clearance
```

The parser reads the cookie from `.env`, merges it with `cookies_required`,
and injects it into the `HttpSession`. ✅

### Example 4 — Deploying to a custom domain

```dotenv
# Production .env
NEXUSDL_PUBLIC_URL=https://my-nexusdl.example.org
NEXUSDL_API_BASE_URL=https://api.my-nexusdl.example.org
NEXUSDL_FRONTEND_URL=https://my-nexusdl.example.org
NEXUSDL_LOG_LEVEL=WARNING
NEXUSDL_ALLOW_ADULT=false
```

**No `sites.yaml` change needed.** The site list is identical, only the
deployment URLs and log level change.

---

## References

### Patterns

- **12-Factor App** — *Factor III: Config*:
  <https://12factor.net/config>
- **The Twelve-Factor App** (Wiggins, 2011) — general principles
- **GitHub Secret Scanning**:
  <https://docs.github.com/en/code-security/secret-scanning>
- **JSON Schema** (for `sites_schema.json`):
  <https://json-schema.org/>

### Ecosystem

- **`pydantic-settings`** (env var loading):
  <https://docs.pydantic.dev/latest/concepts/pydantic_settings/>
- **`python-dotenv`** (`.env` loading): <https://github.com/theskumar/python-dotenv>
- **Docker Compose env vars**:
  <https://docs.docker.com/compose/environment-variables/>
- **Kubernetes ConfigMaps and Secrets**:
  <https://kubernetes.io/docs/concepts/configuration/>

### Related ADRs

- [ADR-001: Hexagonal Architecture](001-hexagonal-architecture.md)
- [ADR-002: No CBR Support](002-no-cbr.md)
- [ADR-003: Parser Factory DI](003-parser-factory-di.md)
- [ADR-005: Multi-platform builds](005-multi-platform-builds.md)

### Related docs

- `docs/source/usage/configuration.md` — user-facing configuration guide
- `.env.example` — the exhaustive template
- `src/nexusdl/data/sites.yaml` — the canonical site list
- `src/nexusdl/data/sites_schema.json` — the JSON Schema
- `scripts/validate_sites.py` — the validator
- `scripts/check_imports.py` — the secret scanner

---

## Review and amendments

This ADR is **living** — if we later split `sites.yaml` per language or
adopt a database-backed registry, we will write a new ADR (e.g.
`006-split-sites-by-language.md`) that **extends** this one.

**Amendments log:**

| Date | Change | ADR |
|---|---|---|
| 2026-10-28 | Initial version | 004 |
| — | (none yet) | — |

---

## How to verify compliance

```bash
# 1. Ensure no secrets in sites.yaml
grep -iE "password|secret|token|credential|api[_-]?key" \
  src/nexusdl/data/sites.yaml \
  && echo "❌ Secret found in sites.yaml" \
  || echo "✅ No secret in sites.yaml"

# 2. Ensure no deployment URLs in sites.yaml
grep -E "nexusdl\.(dev|com|org|io)" src/nexusdl/data/sites.yaml \
  && echo "❌ Deployment URL in sites.yaml" \
  || echo "✅ No deployment URL in sites.yaml"

# 3. Ensure no hardcoded site URLs in Python code
grep -rE "https?://[a-z0-9.-]+\.(org|net|com)" \
  src/nexusdl/ --include="*.py" \
  | grep -v "example.com" \
  | grep -v "test" \
  && echo "⚠️ Hardcoded URL in Python (verify manually)" \
  || echo "✅ No hardcoded URL in Python"

# 4. Validate sites.yaml against schema
python scripts/validate_sites.py

# 5. Ensure .env.example is present and has no real secrets
test -f .env.example \
  && echo "✅ .env.example present" \
  || echo "❌ .env.example missing"

grep -E "CHANGE_ME|your-|example" .env.example > /dev/null \
  && echo "✅ .env.example has no real secrets" \
  || echo "⚠️ .env.example may contain real values"

# 6. Ensure .env is gitignored
grep -qE "^\.env$|^\.env\.\*" .gitignore \
  && echo "✅ .env is gitignored" \
  || echo "❌ .env is NOT gitignored (CRITICAL)"
```

Expected output:

```
✅ No secret in sites.yaml
✅ No deployment URL in sites.yaml
✅ No hardcoded URL in Python
✅ sites.yaml valid — N site(s) declared.
✅ .env.example present
✅ .env.example has no real secrets
✅ .env is gitignored
```

---

## Summary

| Aspect | Decision |
|---|---|
| Secrets | `.env` (not versioned) |
| Deployment URLs | `.env` (not versioned) |
| Feature flags | `.env` (not versioned) |
| Site metadata | `sites.yaml` (versioned) |
| Site capabilities | `sites.yaml` (versioned) |
| Site credentials | `.env` prefixed (`NEXUSDL_<SITE>_<COOKIE>`) |
| Site cookies | `.env` (values) + `sites.yaml` (names) |
| Default config | `data/default_config.yaml` (versioned) |
| Validation | `scripts/validate_sites.py` (JSON Schema) |
| Enforcement | `scripts/check_imports.py` (secret scanner) |
| Alternative rejected | All-in-env, all-in-yaml, separate secrets.yaml, env-only, database-backed |

---

*This ADR is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../../LICENSE).*
