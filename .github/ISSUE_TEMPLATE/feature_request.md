
---
name: Feature request
about: Suggest a new feature, an improvement, or a new site integration for NexusDL
title: "[FEATURE] "
labels: ["enhancement", "triage"]
assignees: []
---

<!--
  ════════════════════════════════════════════════════════════════════
  ⚠️  BEFORE YOU POST — pick the right template
  ════════════════════════════════════════════════════════════════════

  This template is for **feature requests**.  Two categories have their
  own template and must not be filed here:

  * **New site integration** — use the "New site" template
    (`.github/ISSUE_TEMPLATE/new_site.md`).  Site requests have their
    own schema because they need URLs, capabilities, selectors and a
    legal-compliance statement.

  * **Bug report** — use the "Bug report" template.  A feature request
    that describes existing behaviour as broken is a bug.

  Posting a site request or a bug in this template is the most common
  cause of a request being closed without action.

  ════════════════════════════════════════════════════════════════════
  ⚠️  SCOPE — read before proposing
  ════════════════════════════════════════════════════════════════════

  NexusDL is a downloader.  Its scope is explicit:

  IN SCOPE:
    - Downloading manga, webtoons, comics and doujinshi from supported
      sites.
    - Packaging the downloaded content (CBZ, CB7, PDF, ZIP, CBT, folder).
    - A local library with search, reading progress and metadata.
    - CLI, TUI, GUI and Web front-ends.
    - Plugin system for user extensions.

  OUT OF SCOPE (will be closed):
    - Hosting content (NexusDL has no server-side library).
    - Reading chapters inside NexusDL (open the archive in your reader).
    - Anything that requires redistributing copyrighted content.
    - Bypassing paywalls or DRM.
    - Auto-updating the source site content in the background (out of
      scope for v3.0; the "watch for new chapters" feature is tracked
      as a separate, larger design discussion).

  Requests that fall outside the scope will be closed with a link to
  this section.

  ════════════════════════════════════════════════════════════════════
  ⚠️  SECRETS — REDACT BEFORE POSTING
  ════════════════════════════════════════════════════════════════════

  If your request includes example logs, configuration dumps or
  screenshots, remove every secret first: API tokens, cookies,
  passwords, encryption keys, email addresses, IP addresses.
-->


## Pre-flight checklist

- [ ] I have read the [documentation](https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs).
- [ ] I have searched [existing issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues) and this is not a duplicate.
- [ ] I have read the **Scope** section above and confirmed my request is in scope.
- [ ] This is **not** a new-site request (those go to the "New site" template).
- [ ] This is **not** a bug report (those go to the "Bug report" template).
- [ ] I have removed all secrets from the content of this issue.


## Summary

<!--
  One or two sentences describing the feature.  Imagine you are
  writing the line that will appear in the CHANGELOG when the feature
  ships — that is exactly the level of concision this section should
  be at.

  Good: "Allow filtering search results by publication year."
  Good: "Add a `--dry-run` flag to `nexusdl download` that lists the
        chapters that would be downloaded without downloading them."
  Bad:  "Make the app better." (not actionable)
-->


## Motivation

<!--
  Why does this feature matter?  What problem does it solve?  Who
  benefits?  A feature request without a motivation is a solution
  looking for a problem, and will usually be closed with a request to
  reframe it as a user need.

  Answering these questions explicitly saves a round-trip:

  1. What are you trying to accomplish?
  2. What do you currently do instead, and why is that inadequate?
  3. Who else would benefit?  (Just you, a class of users, everyone?)
  4. Does this unlock a workflow that is currently impossible, or does
     it merely make an existing workflow faster?

  Example:

    I download from mangadex weekly and I want to skip chapters I have
    already read.  Today, I do this manually by opening the library
    and comparing the chapter numbers by eye.  With 300+ chapters
    this is impractical and I have re-downloaded chapters by mistake
    several times.  A `--skip-read` flag would let the download
    worker read the reading progress from the library and skip every
    chapter whose index is below the current progress page.  This
    would benefit any user who downloads long-running series.
-->


## Proposed solution

<!--
  How would you like this feature to work from the user's perspective?
  Describe the desired interface, not the implementation — we will
  choose the implementation ourselves.

  Include concrete examples:

  * For CLI features: the exact command and expected output.
  * For TUI/GUI features: the exact widget, label, and behaviour.
  * For Web features: the exact HTTP endpoint and JSON shape.
  * For Python library features: the exact import path and function
    signature.

  Example (CLI):

      $ nexusdl download --site mangadex --manga-id 3b7c1a2f \\
          --skip-read
      Skipping 47 chapters already marked as read
      Downloading chapter 48... [====] 100%

  Example (Python API):

      from nexusdl.core.downloader import DownloadManager
      manager.download_manga(
          manga,
          skip_read=True,  # new keyword argument
      )
-->


## Alternatives considered

<!--
  What other approaches have you thought about?  Why is the proposed
  solution better than them?  This is where you demonstrate that you
  have thought about the design space, not just the first idea that
  came to mind.

  If there is a workaround (a shell script, a plugin, a manual
  procedure), describe it.  We will ask about it anyway — pre-empting
  the question saves a round-trip.

  Example:

    Alternative A: expose reading progress through a REST endpoint
    and let users script their own skip logic.  Rejected because
    every user would need to write the script, and the download
    worker already has access to the library.

    Alternative B: add the feature to the plugin API rather than
    the core.  Rejected because the feature needs to hook into
    the download worker's chapter-selection logic before any
    downloads start, and the plugin hooks run after download
    submission.
-->


## User impact

<!--
  Who benefits from this feature?  Be honest — a feature that only
  benefits one user is less likely to be prioritised than a feature
  that unblocks a whole class of workflows.

  Pick the closest match:
-->

- [ ] **Everyone** — the feature is useful to every NexusDL user.
- [ ] **A class of users** — e.g. "users with 500+ chapter libraries",
      "users on slow networks", "users who download from adult sites".
- [ ] **Just me** — I have an unusual workflow that this feature
      unblocks.

<!--
  If "A class of users" or "Everyone", describe the class in one
  sentence.  If "Just me", it is not a rejection — some of the best
  features start as "just one user".  But it affects the priority.
-->


## Scope and complexity

<!--
  A rough sense of how big a change this is.  It is not a commitment —
  it is a signal that helps us plan.  Mark the closest match:
-->

- [ ] **Cosmetic** — a label change, a small CLI flag, a wording fix.
      Likely a single-file change.
- [ ] **Small** — a new CLI flag, a new config option, a new field on
      an existing model.  Touches 2–5 files.
- [ ] **Medium** — a new method or module, a new endpoint, a new
      widget.  Touches 6–15 files.
- [ ] **Large** — a new subsystem, a new interface, a design change
      that spans layers.  Touches 15+ files or requires an ADR
      (Architecture Decision Record).
- [ ] **I have no idea** — that is fine, this is a rough guess.


## Which interface(s) would this affect?

<!-- Tick every interface the feature should appear in.  A feature
     that only affects the CLI can be implemented independently of
     one that also affects the Web.  This helps break the work into
     parallelisable pieces. -->

- [ ] **Core** — the feature belongs in `nexusdl.core` and is
      interface-agnostic.
- [ ] **CLI** — visible through `nexusdl <subcommand>`.
- [ ] **TUI** — visible in the Textual application (`nexusdl tui`).
- [ ] **GUI** — visible in the CustomTkinter application (`nexusdl gui`).
- [ ] **Web** — visible through the FastAPI + Next.js interface.
- [ ] **Python library** — the feature should be usable programmatically
      from `from nexusdl import ...`.
- [ ] **Plugins** — the feature should be exposed to plugins through
      `nexusdl.plugins.api`.


## Related to an existing ADR or decision?

<!--
  NexusDL documents its design decisions in
  `docs/source/development/decisions/`.  If your feature touches a
  decision that has already been made, link it here.  If your feature
  contradicts an existing ADR, say so explicitly — it may still be the
  right call, but the ADR must be revisited.

  Existing ADRs:
    001-hexagonal-architecture.md
    002-no-cbr.md
    003-parser-factory-di.md
    004-sites-yaml-vs-env.md
    005-multi-platform-builds.md
-->

- [ ] This feature **aligns with** an existing ADR.  ADR number: <!-- e.g. 002 -->
- [ ] This feature **contradicts** an existing ADR.  ADR number: <!-- e.g. 002 -->
- [ ] This feature is **orthogonal** to every existing ADR.
- [ ] This feature requires a **new ADR**.


## Compatibility and breaking changes

<!--
  Would this feature break any existing behaviour?  For example:
    - Would it change the shape of an API response?
    - Would it rename a CLI flag?
    - Would it change the signature of a public Python function?
    - Would it invalidate existing library databases or configs?

  NexusDL follows the principle "additive changes only within a major
  version".  A breaking change requires a major version bump and a
  migration path.
-->

- [ ] **No** — the feature is purely additive.
- [ ] **Yes** — the feature would break: <!-- describe -->


## Would you be willing to contribute a PR?

<!--
  Honest answers help us prioritise.  A well-scoped PR from the
  requester is often merged within a week; a feature that depends
  entirely on maintainer time can sit in the backlog for months.  This
  is not a hard gate — many accepted features are implemented by
  maintainers — but it is useful information.
-->

- [ ] **Yes**, I can write the implementation (see CONTRIBUTING.md).
- [ ] **Yes**, but I would need guidance — I have not contributed to
      this codebase before.
- [ ] **Maybe**, depending on the scope.
- [ ] **No** — I am only suggesting the feature.


## Mockups or examples (optional)

<!--
  Screenshots, ASCII mockups, links to similar features in other
  projects, or sketches of the proposed UI.  For GUI/Web features,
  even a rough sketch is more useful than paragraphs of description.
-->


## Additional context

<!--
  Anything else.  For example:
    - Link to a discussion where this feature was previously proposed.
    - Link to a related project that has the feature.
    - A proof-of-concept branch or a patch.
    - Relevant documentation pages.
-->


<!--
  ════════════════════════════════════════════════════════════════════
  Thank you for proposing a feature.  The most useful feature
  requests describe a *problem*, not just a *solution* — they give the
  maintainers the information to choose the best solution, which may
  not be the one first proposed.
  ════════════════════════════════════════════════════════════════════
-->
