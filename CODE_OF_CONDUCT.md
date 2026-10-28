# Contributor Covenant Code of Conduct

## Our Pledge

We as members, contributors, and leaders pledge to make participation in our
community a harassment-free experience for everyone, regardless of age, body
size, visible or invisible disability, ethnicity, sex characteristics, gender
identity and expression, level of experience, education, socio-economic status,
nationality, personal appearance, race, caste, color, religion, or sexual
identity and orientation.

We pledge to act and interact in ways that contribute to an open, welcoming,
diverse, inclusive, and healthy community.

We also recognize that **NexusDL is a technical tool** that legitimately
supports adult and mature content (18+). This technical reality does **not**
justify inappropriate behavior in community spaces: discussions, issues,
pull requests, and chats must remain professional, respectful, and focused on
the code and its use cases.

---

## Our Standards

Examples of behavior that contributes to a positive environment for our
community include:

- Demonstrating empathy and kindness toward other people
- Being respectful of differing opinions, viewpoints, and experiences
- Giving and gracefully accepting constructive feedback
- Accepting responsibility and apologizing to those affected by our mistakes,
  and learning from the experience
- Focusing on what is best not just for us as individuals, but for the overall
  community
- Keeping technical discussions on-topic and actionable
- Respecting the user's jurisdiction: NexusDL supports content that may be
  legal in one country and restricted in another. Discussions must acknowledge
  this diversity without advocating for illegal use.
- Providing context and reproduction steps when reporting bugs
- Crediting others' work when building on it (parsers, mixins, documentation)

Examples of unacceptable behavior include:

- The use of sexualized language or imagery, and sexual attention or advances
  of any kind in community spaces (issues, PRs, discussions, chat)
- Trolling, insulting or derogatory comments, and personal or political attacks
- Public or private harassment
- Publishing others' private information, such as a physical or email address,
  without their explicit permission
- Other conduct which could reasonably be considered inappropriate in a
  professional setting
- Sharing, requesting, or linking to pirated or copyrighted material in a way
  that violates applicable law
- Sharing credentials, API keys, tokens, cookies, or other secrets belonging
  to others
- Deliberately introducing malicious code, backdoors, or telemetry into
  parsers, plugins, or the core
- Attempting to bypass content filters or safety mechanisms that the project
  exposes to its users (this must remain a user choice, not a community
  decision)
- Spamming issues, pull requests, or discussions with low-effort, duplicate,
  or off-topic content
- Impersonating maintainers, contributors, or other community members

---

## Scope

This Code of Conduct applies within all community spaces, and also applies
when an individual is officially representing the community in public spaces.
Examples of representing our community include using an official email
address, posting via an official social media account, or acting as an
appointed representative at an online or offline event.

### Community spaces include, but are not limited to

- The GitHub repository (`Voldigoad-Holdings/nexus-dl`):
  - Issues
  - Pull requests
  - Discussions
  - Code review comments
  - Commit messages
  - Wiki
- Any official chat (Discord, Matrix, IRC, Telegram, etc.)
- Any official social media account
- Any official mailing list
- Any in-person event, meetup, or conference where NexusDL is represented

---

## Enforcement Responsibilities

Community leaders are responsible for clarifying and enforcing our standards
of acceptable behavior and will take appropriate and fair corrective action in
response to any behavior that they deem inappropriate, threatening, offensive,
or harmful.

Community leaders have the right and responsibility to remove, edit, or reject
comments, commits, code, wiki edits, issues, and other contributions that are
not aligned to this Code of Conduct, and will communicate reasons for
moderation decisions when appropriate.

---

## Enforcement

Instances of abusive, harassing, or otherwise unacceptable behavior may be
reported to the community leaders responsible for enforcement at:

**conduct@voldigoad-holdings.example** <!-- TODO: replace with real address -->

All complaints will be reviewed and investigated promptly and fairly.

All community leaders are obligated to respect the privacy and security of the
reporter of any incident.

### What to include in a report

To help us investigate, please include as much of the following as possible:

- **Your contact information** (so we can follow up)
- **Names (real, nicknames, or pseudonyms)** of any individuals involved
- **When and where** the incident occurred (URL, timestamp, context)
- **Description of what happened**, in your own words
- **Any additional context** (screenshots, links, witnesses)
- **Whether you have previously reported** this person or incident
- **Any other information** you believe is relevant

You can report anonymously if you prefer, but it may limit our ability to
investigate and respond.

---

## Enforcement Guidelines

Community leaders will follow these Community Impact Guidelines in
determining the consequences for any action they deem in violation of this
Code of Conduct:

### 1. Correction

**Community Impact**: Use of inappropriate language or other behavior deemed
unprofessional or unwelcome in the community.

**Consequence**: A private, written warning from community leaders, providing
clarity around the nature of the violation and an explanation of why the
behavior was inappropriate. A public apology may be requested.

### 2. Warning

**Community Impact**: A violation through a single incident or series of
actions.

**Consequence**: A warning with consequences for continued behavior. No
interaction with the people involved, including unsolicited interaction with
those enforcing the Code of Conduct, for a specified period of time. This
includes avoiding interactions in community spaces as well as external
channels like social media. Violating these terms may lead to a temporary or
permanent ban.

### 3. Temporary Ban

**Community Impact**: A serious violation of community standards, including
sustained inappropriate behavior.

**Consequence**: A temporary ban from any sort of interaction or public
communication with the community for a specified period of time. No public or
private interaction with the people involved, including unsolicited
interaction with those enforcing the Code of Conduct, is allowed during this
period. Violating these terms may lead to a permanent ban.

### 4. Permanent Ban

**Community Impact**: Demonstrating a pattern of violation of community
standards, including sustained inappropriate behavior, harassment of an
individual, or aggression toward or disparagement of classes of individuals.

**Consequence**: A permanent ban from any sort of public interaction within
the community.

---

## NexusDL-specific policies

### Content and legality

NexusDL is a technical tool. It does **not** host, distribute, or endorse any
content. Users are responsible for the content they download and for complying
with the laws of their jurisdiction.

- Do **not** request or share links to specific adult, gore, or otherwise
  sensitive manga/comic chapters in issues or PRs. Use generic examples.
- Do **not** use NexusDL community spaces to distribute or promote piracy.
- Do **not** attempt to have the project removed from GitHub, PyPI, Docker Hub,
  or any other platform by lobbying maintainers with legal threats. File a
  proper legal request through the channels that platform provides.

### Secrets and credentials

- **Never** post API keys, tokens, cookies, passwords, `.env` contents, or
  private logs in issues, PRs, or chat.
- If you accidentally expose a secret, **revoke it immediately** at the source
  (e.g. GitHub, AWS, Google Cloud) and inform the maintainers.
- Maintainers will **never** ask you for a secret in a public channel.

### Reporting vulnerabilities

Security issues are handled separately. See [`SECURITY.md`](SECURITY.md).

### Contributing parsers

- If you add a parser, make sure you have the right to reverse-engineer the
  site's public API or HTML. Respect the site's `robots.txt` and Terms of
  Service where applicable.
- Document rate limits, required cookies, and whether the parser needs
  Playwright or FlareSolverr.
- Do not hardcode site URLs, credentials, or selector strings in Python code.
  Use `src/nexusdl/data/sites.yaml` for site configuration.
- Do not scrape content that is illegal in the maintainer's jurisdiction or
  the contributor's jurisdiction. If in doubt, ask.

### Contributing to the frontend

- The frontend is a **sub-project** (`src/nexusdl/interfaces/web/frontend/`).
  It has its own conventions (ESLint, Prettier, Tailwind).
- Do not mix Python changes and TypeScript changes in the same PR unless the
  change is truly atomic.
- Accessibility (WCAG 2.1 AA) is required for user-facing components.

---

## Attribution

This Code of Conduct is adapted from the
[Contributor Covenant](https://www.contributor-covenant.org), version 2.1,
available at
[https://www.contributor-covenant.org/version/2/1/code_of_conduct.html](https://www.contributor-covenant.org/version/2/1/code_of_conduct.html).

Community Impact Guidelines were inspired by
[Mozilla's code of conduct enforcement ladder](https://github.com/mozilla/inclusion).

For answers to common questions about this code of conduct, see the FAQ at
[https://www.contributor-covenant.org/faq](https://www.contributor-covenant.org/faq).
Translations are available at
[https://www.contributor-covenant.org/translations](https://www.contributor-covenant.org/translations).

The NexusDL-specific sections (content and legality, secrets and credentials,
reporting vulnerabilities, contributing parsers, contributing to the frontend)
are original additions.

---

## Contact

- **Code of Conduct violations**: conduct@voldigoad-holdings.example
  <!-- TODO: replace with real address -->
- **Security vulnerabilities**: see [`SECURITY.md`](SECURITY.md)
- **General questions**: open a [discussion](https://github.com/Voldigoad-Holdings/nexus-dl/discussions)
- **Bug reports**: open an [issue](https://github.com/Voldigoad-Holdings/nexus-dl/issues)

---

*This Code of Conduct is effective as of the project's public release and
applies to all past, present, and future contributors and community members.*
