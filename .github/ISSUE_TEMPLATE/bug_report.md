---
name: Bug report
about: Report a reproducible bug in NexusDL
title: "[BUG] "
labels: ["bug", "triage"]
assignees: []
---

<!--
  ════════════════════════════════════════════════════════════════════
  ⚠️  SECURITY NOTICE — READ BEFORE POSTING
  ════════════════════════════════════════════════════════════════════

  DO NOT report security vulnerabilities in a public issue.
  See SECURITY.md at the repository root for the private disclosure
  process.  Public security issues will be closed without comment.

  ════════════════════════════════════════════════════════════════════
  ⚠️  SECRETS — REDACT BEFORE POSTING
  ════════════════════════════════════════════════════════════════════

  Remove every secret from logs, config dumps, and screenshots before
  submitting.  This includes:
    - API tokens (JWT access tokens, refresh tokens)
    - Cookies (site session cookies, Cloudflare clearance cookies)
    - Passwords, encryption keys
    - Site credentials from .env or config.yaml
    - Real user email addresses or IP addresses

  Replace sensitive values with placeholders like <REDACTED>,
  <TOKEN>, <COOKIE>, <EMAIL>.
-->

## Pre-flight checklist

Please confirm the following before submitting — most "bugs" turn out to
be configuration issues or already-fixed problems:

- [ ] I have read the [documentation](https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs).
- [ ] I have searched [existing issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues) and this is not a duplicate.
- [ ] I am running the latest release (check with `nexusdl --version`).
- [ ] The problem is reproducible — it happens every time, or I have
      described the conditions under which it happens.
- [ ] I have removed all secrets from the content of this issue.


## Description

<!-- A clear and concise description of the bug.  What is happening vs.
     what you expected to happen.  One paragraph is usually enough. -->


## Steps to reproduce

<!--
  Numbered steps that lead to the bug.  Be specific enough that a
  developer can follow them exactly.  If the bug is only reproducible
  under certain conditions (a specific site, a specific network
  configuration, a specific OS), state those conditions explicitly.

  Example:

  1. Run `nexusdl download --site mangadex --manga-id 3b7c1a2f3e4d5b6a`
  2. Wait for the download to reach chapter 5
  3. Interrupt with Ctrl+C
  4. Run the same command again
  5. Observe that chapter 5 is downloaded twice
-->

1.
2.
3.


## Expected behaviour

<!-- What did you expect to happen? -->


## Actual behaviour

<!--
  What actually happened?  Include the exact error message if any.
  Paste the traceback if the bug manifests as an unhandled exception.
-->


## Environment

<!--
  Fill in the fields below.  The `nexusdl --version --verbose` command
  (or the Settings > About page in the GUI/Web) prints all of this
  information at once — copy-paste it here rather than typing it by hand.
-->

| Field | Value |
|---|---|
| **NexusDL version** | <!-- output of `nexusdl --version` --> |
| **Python version** | <!-- output of `python --version` --> |
| **Operating system** | <!-- e.g. Ubuntu 24.04, macOS 14.5, Windows 11 23H2, Debian 12 (Docker) --> |
| **Architecture** | <!-- x86_64, arm64, etc. --> |
| **Installation method** | <!-- pip / pipx / uv / Docker / source / other --> |
| **Interface used** | <!-- CLI / TUI / GUI / Web / Python library / multiple --> |
| **Playwright installed** | <!-- yes / no / not sure --> |
| **FlareSolverr installed** | <!-- yes / no / not sure --> |


## Configuration (optional)

<!--
  Paste the relevant sections of your effective configuration.
  Get the sanitized dump with:

      nexusdl config show

  or from the GUI: Settings > Server settings (already masked).
  from the Web:  GET /api/v1/settings (already masked).

  IMPORTANT: If you are pasting a raw config file instead of the masked
  dump, redact every secret first.
-->

<details>
<summary>Sanitized configuration</summary>

```yaml
# Paste sanitized config here
```

</details>


## Which site(s) does the bug affect?

<!--
  Fill in the site ids involved.  This is critical: many bugs are
  site-specific (a parser breaks when the site changes its HTML) while
  others are universal (a bug in the download manager, the packaging
  layer, or the library database).
-->

- [ ] The bug is **site-specific** — list the site id(s) below.
- [ ] The bug is **universal** — it affects every site, every manga,
      or happens before any site is contacted.
- [ ] The bug is **intermittent** — it affects some sites sometimes.

**Site id(s)** (from `data/sites.yaml` or the `--site` flag):

```
<!-- e.g. mangadex, comick, x_manga_org, sushiscan -->
```


## Logs

<!--
  Run the command again with maximum verbosity and paste the output
  below.  A stack trace without logs is almost never actionable.

  Commands:
      nexusdl --log-level DEBUG <your-command>
      nexusdl --log-level TRACE <your-command>  # very verbose

  For Web: check the browser console AND the backend logs.
  For GUI: check ~/.local/share/nexusdl/logs/ (Linux),
           ~/Library/Application Support/nexusdl/logs/ (macOS),
           %LOCALAPPDATA%\nexusdl\logs\ (Windows).

  Redact every secret.  If the log file is too large, attach it to the
  issue as a .txt file rather than pasting.
-->

<details>
<summary>Debug logs</summary>

```
# Paste logs here, or attach as a file
```

</details>


## Reproducibility

<!-- Pick one: -->

- [ ] **Always** — happens every single time with the steps above.
- [ ] **Sometimes** — happens under some conditions I have described.
- [ ] **Once** — happened once, cannot reproduce on demand.


## Does this happen with an older version?

<!-- If you recently upgraded, did the bug exist before?  This narrows
     the culprit to recent changes. -->

- [ ] Yes, it worked in an earlier version (version: <!-- e.g. 0.0.9 -->)
- [ ] No, it never worked
- [ ] I have not checked
- [ ] N/A — this is the first version I have installed


## Screenshots or recordings (optional)

<!-- For GUI or Web bugs, a screenshot is often more informative than
     text.  Redact any personal information before attaching. -->


## Possible fix (optional)

<!--
  If you have already investigated the bug and have an idea where it
  comes from, describe it here.  Reference specific files or functions
  if you know them (e.g. "this looks like a case that
  `DownloadWorker._package` does not handle — the `finally` block does
  not remove the temp directory when packaging fails").

  PRs are welcome.  See CONTRIBUTING.md for the workflow.
-->


## Additional context

<!--
  Anything else that might help.  For example:
    - Does it happen on a fresh install?
    - Does it happen without any plugins?
    - Does it happen with Playwright/FlareSolverr disabled?
    - Does it happen behind a proxy / VPN?
    - Recent changes to your configuration or environment?
-->


<!--
  ════════════════════════════════════════════════════════════════════
  Thank you for taking the time to file a detailed report.  A
  reproducible bug report with clean logs and a clear description is
  the single most useful contribution a user can make.
  ════════════════════════════════════════════════════════════════════
-->
