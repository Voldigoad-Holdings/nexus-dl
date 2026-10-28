
Fingerprint: `XXXX XXXX XXXX XXXX XXXX  XXXX XXXX XXXX XXXX XXXX`

> If no PGP key is listed yet, please use GitHub Security Advisories.

---

## What to include in your report

To help us triage and fix the issue quickly, please include:

- **A clear description** of the vulnerability and its potential impact
- **Steps to reproduce** (as minimal as possible)
- **Affected versions** (commit hash, tag, or `nexusdl --version` output)
- **Your environment**: OS, Python version, installation method
- **Any proof-of-concept (PoC)** code or screenshots
- **Suggested fix** (optional, but appreciated)
- **Whether you plan to disclose publicly**, and if so, when

If you are unsure whether something is a vulnerability, **report it anyway**.
False positives are fine; false negatives are not.

---

## What counts as a security vulnerability

We consider the following as security vulnerabilities:

### Critical

- **Remote code execution (RCE)** via parser, plugin, or config input
- **Path traversal** allowing writes outside the download directory
- **Command injection** via crafted URLs, filenames, or metadata
- **Authentication bypass** in the Web API
- **Privilege escalation** between user roles
- **Secret leakage** (`.env`, `*.key`, `*.pem`, cookies) via logs, error
  messages, or API responses
- **SSRF** allowing access to internal networks via the parser layer
- **Insecure deserialization** (pickle, YAML unsafe load, etc.)

### High

- **Cross-Site Scripting (XSS)** in the Web UI (stored or reflected)
- **Cross-Site Request Forgery (CSRF)** on authenticated endpoints
- **SQL injection** in the local library
- **Denial of Service (DoS)** that can be triggered by a single request or a
  small set of downloaded files
- **Weak cryptography** in cookie encryption or JWT signing
- **Dependency vulnerabilities** with a known CVE rated High or Critical

### Medium

- **Information disclosure** that reveals secrets, internal paths, or
  configuration
- **Rate limit bypass** on the Web API
- **Missing security headers** with a plausible exploitation scenario
- **Weak password storage** for user accounts
- **Dependency vulnerabilities** with a known CVE rated Medium

### Low

- **Missing security headers** with no known exploitation scenario
- **Verbose error messages** that don't leak secrets
- **Dependency vulnerabilities** with a known CVE rated Low
- **Best-practice deviations** without a concrete attack scenario

---

## What is NOT a security vulnerability

The following are **out of scope** and should be reported as regular bugs or
feature requests:

- **Content of scraped sites** — NexusDL is a technical tool, we do not
  control third-party content
- **Site blocking or banning your IP** — this is expected behavior for scrapers
- **Cloudflare detecting NexusDL** — anti-bot is an ongoing cat-and-mouse game
- **Rate limits enforced by third-party sites** — respect `robots.txt` and
  ToS, but this is not a NexusDL vulnerability
- **Adult or sensitive content downloaded by the user** — user's
  responsibility, `NEXUSDL_ALLOW_ADULT` is opt-in
- **Theoretical attacks without a PoC** — we still want to know, but they are
  lower priority
- **Social engineering** of maintainers
- **Physical access** to the user's machine
- **Missing security features** (e.g. "NexusDL doesn't support 2FA") — these
  are feature requests
- **Self-XSS** requiring the user to paste code into DevTools
- **Vulnerabilities in the user's browser or OS** — report upstream
- **Vulnerabilities in third-party dependencies without a plausible path**
  through NexusDL — report upstream or track the CVE

---

## Our commitment (SLA)

We are a **small, volunteer-driven project**. We cannot guarantee enterprise
SLAs, but we commit to the following:

| Step | Target time | Best effort |
|---|---|---|
| **Acknowledge receipt** | 72 hours | Yes |
| **Initial triage** (severity, scope) | 7 days | Yes |
| **Fix for Critical** | 14 days | Yes |
| **Fix for High** | 30 days | Yes |
| **Fix for Medium** | 90 days | Best effort |
| **Fix for Low** | Next minor release | Best effort |
| **Public disclosure** | Coordinated with reporter | Yes |

If we cannot meet these targets (e.g. maintainer unavailable), we will tell
you as soon as possible and provide a realistic new timeline.

---

## Disclosure policy

We follow a **coordinated disclosure** model:

1. You report the vulnerability privately.
2. We acknowledge, triage, and develop a fix.
3. We prepare a release with the fix and, if applicable, a CVE request via
   GitHub Security Advisories.
4. We agree with you on a **disclosure date** (typically 90 days after the
   fix is released, or sooner if the vulnerability is already public).
5. On the agreed date, we publish a security advisory with credit to you
   (unless you prefer to remain anonymous).

We will **never** disclose your identity without your explicit consent.

If you prefer to publish a write-up or blog post about the vulnerability, we
only ask that you **link to the GitHub Security Advisory** and **respect the
coordinated disclosure timeline**.

---

## Bug bounty

NexusDL does **not** currently offer a bug bounty program. We are a
volunteer-run, open-source project without a revenue stream.

However:

- We will publicly credit you in the security advisory (with your consent).
- We will add your name to [`SECURITY_HALL_OF_FAME.md`](SECURITY_HALL_OF_FAME.md).
- We will link to your website, GitHub profile, or social media (on request).
- We will write a personal thank-you note.

If you are a company and want to sponsor a bug bounty program for NexusDL,
please [open a discussion](https://github.com/Voldigoad-Holdings/nexus-dl/discussions).

---

## Security practices in NexusDL

For transparency, here are the security practices we follow in the codebase.
These are **not** claims of perfection — they are our commitments.

### Secrets management

- ✅ **All secrets via `pydantic.SecretStr`** — never `str` for secrets
- ✅ **All secrets loaded from `.env`** — never hardcoded
- ✅ **`.env` is git-ignored** — checked in CI
- ✅ **`scripts/check_imports.py`** fails CI on any hardcoded GitHub token,
  AWS key, or private key block
- ✅ **`no-token-leak` pre-commit hook** blocks accidental token commits
- ✅ **No secrets in logs** — Loguru's `bind()` is used carefully
- ✅ **No secrets in error messages** — custom exception hierarchy separates
  user-facing messages from internal details
- ✅ **Cookies encrypted at rest** — `cryptography.Fernet` (AES-128-CBC +
  HMAC-SHA256)

### Authentication & authorization

- ✅ **JWT with HS256 minimum** (RS256 recommended for production)
- ✅ **Bcrypt password hashing** via `passlib[bcrypt]`
- ✅ **Role-based access control** (`GUEST`, `USER`, `ADMIN`)
- ✅ **CSRF protection** on session-based endpoints
- ✅ **CORS restricted** to configured origins
- ✅ **Rate limiting** on authentication endpoints

### Input validation

- ✅ **All API inputs validated** with Pydantic v2
- ✅ **All file paths sanitized** before writing
- ✅ **No `eval()`, `exec()`, or `pickle.load()`** on untrusted input
- ✅ **`yaml.safe_load()`** only — never `yaml.load()` without a safe loader
- ✅ **URL validation** with `pydantic.HttpUrl`
- ✅ **Filename sanitization** via `python-slugify` + `Unidecode`
- ✅ **Path traversal protection** in archive extraction (CBZ, CB7)

### Transport security

- ✅ **HTTPS enforced** for the Web API in production
- ✅ **HTTP/2** preferred via `httpx[http2]`
- ✅ **TLS certificate verification** on by default
- ✅ **Custom proxy support** with certificate validation

### Dependency management

- ✅ **`uv.lock`** locked versions
- ✅ **Dependabot** enabled (daily checks for vulnerabilities)
- ✅ **`pip-audit`** run in CI on every push
- ✅ **Minimal dependency surface** — no unnecessary libraries

### Container security

- ✅ **Multi-stage builds** — minimal runtime image
- ✅ **Non-root user** in the container (`nexusdl`)
- ✅ **Read-only filesystem** where possible
- ✅ **No secrets baked into images** (`.dockerignore` excludes `.env`)
- ✅ **Pinned base images** (`python:3.12-slim-bookworm`)

### Operational security

- ✅ **Optional `robots.txt` enforcement** (`NEXUSDL_RESPECT_ROBOTS_TXT=true`)
- ✅ **Optional content filtering** (`NEXUSDL_ALLOW_ADULT=false` by default)
- ✅ **Optional Playwright** — disabled by default
- ✅ **Optional FlareSolverr** — disabled by default
- ✅ **Graceful degradation** if a site is down or blocked

---

## Secure deployment checklist

If you are deploying NexusDL for yourself or others, please follow this
checklist:

### Before first run

- [ ] Copied `.env.example` to `.env`
- [ ] Generated **three distinct secrets** for `SECRET_KEY`,
      `JWT_SECRET`, `COOKIE_ENCRYPTION_KEY`
- [ ] `chmod 600 .env` (Unix) or restricted NTFS permissions (Windows)
- [ ] Set `NEXUSDL_APP_ENVIRONMENT=prod`
- [ ] Set `NEXUSDL_LOG_LEVEL=INFO` or `WARNING` (not `DEBUG`)
- [ ] Set `NEXUSDL_LOG_FORMAT=json` (not `pretty`)
- [ ] Configured `NEXUSDL_API_CORS_ORIGINS` to your frontend only
- [ ] Disabled `NEXUSDL_RESPECT_ROBOTS_TXT` **only** if you understand the
      legal implications
- [ ] Enabled HTTPS via a reverse proxy (Nginx, Caddy, Traefik)
- [ ] Set up a firewall (only 80/443 exposed)
- [ ] Set up regular backups of `data/library.db`

### Ongoing

- [ ] Update NexusDL monthly (`pip install --upgrade nexusdl`)
- [ ] Monitor Dependabot alerts
- [ ] Rotate `JWT_SECRET` if a leak is suspected
- [ ] Rotate `COOKIE_ENCRYPTION_KEY` only after deleting `cookies.enc`
- [ ] Review logs for anomalies
- [ ] Subscribe to GitHub Security Advisories for the repo

### Hardening (recommended)

- [ ] Run in Docker with `--read-only` and a tmpfs for `/tmp`
- [ ] Use a non-root user in the container
- [ ] Set resource limits (`--memory`, `--cpus`)
- [ ] Use `--security-opt=no-new-privileges`
- [ ] Put NexusDL behind a VPN if not intended for public access
- [ ] Enable audit logging on the host

---

## Known limitations

For transparency, here are the **known limitations** we are aware of and are
working on (or have accepted):

| Limitation | Status | Notes |
|---|---|---|
| No CSRF protection on the FastAPI backend when using JWT in headers | ✅ Mitigated | JWT in `Authorization: Bearer` is not CSRF-vulnerable |
| Playwright-based scrapers can be fingerprinted | 🟡 Ongoing | Anti-bot is a cat-and-mouse game |
| Cloudflare Turnstile may require manual cookie refresh | 🟡 Accepted | Some sites are intentionally hard to scrape |
| No sandbox for third-party plugins | 🟠 Planned | Plugin system is trusted-only for now (see ADR) |
| SQLite FTS5 requires a specific SQLite build | 🟡 Documented | Fallback to `LIKE` if FTS5 unavailable |
| No SSRF protection on user-provided URLs to `get_manga()` | 🟠 Planned | Local-only API for now, will be hardened before public hosting |

For the full list, see [open security issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues?q=label%3Asecurity).

---

## Security-related dependencies

NexusDL relies on the following security-critical dependencies. If any of
these has a critical CVE, treat it as a NexusDL vulnerability:

| Dependency | Purpose | Criticality |
|---|---|---|
| `cryptography` | Cookie encryption, JWT | 🔴 Critical |
| `python-jose` | JWT signing/verification | 🔴 Critical |
| `passlib[bcrypt]` | Password hashing | 🔴 Critical |
| `pydantic` | Input validation | 🔴 Critical |
| `httpx` | HTTP client | 🟠 High |
| `PyYAML` | Config parsing | 🟠 High |
| `fastapi` | Web API | 🟠 High |
| `sqlalchemy` | Database | 🟠 High |
| `playwright` | Browser automation | 🟡 Medium |
| `Pillow` | Image processing | 🟡 Medium |

We pin minimum versions and use `pip-audit` in CI.

---

## Reporting a bug that is NOT a security issue

If your issue is:

- A crash without a security impact
- A broken parser
- A UI glitch
- A performance problem
- A missing feature

…please open a regular [GitHub Issue](https://github.com/Voldigoad-Holdings/nexus-dl/issues)
or a [Discussion](https://github.com/Voldigoad-Holdings/nexus-dl/discussions).

Misusing the security reporting channel for non-security issues slows down
triage for real vulnerabilities.

---

## Thank you

Security research is **hard** and often **unpaid**. If you took the time to
report a vulnerability to NexusDL, you have our sincere gratitude. We will
do our best to make the disclosure process smooth and to credit you
appropriately.

If you have any questions about this policy, please contact the maintainers
via [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions).

---

## References

- [GitHub Security Advisories documentation](https://docs.github.com/en/code-security/security-advisories)
- [Coordinated disclosure (ISO/IEC 29147)](https://www.iso.org/standard/72396.html)
- [CVE Program](https://www.cve.org/)
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Google's "Shift Left" philosophy](https://sre.google/sre-book/release-engineering/)

---

*Last updated: 2026 — This policy is reviewed at each major release.*
