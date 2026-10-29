# ADR-002: No CBR Support

- **Status**: ✅ Accepted
- **Date**: 2026-10-28
- **Deciders**: Voldigoad-Holdings (lead maintainer)
- **Supersedes**: —
- **Superseded by**: —

---

## Context

NexusDL targets **six packaging formats** for manga chapters:

| Format | Extension | Container | License of the format |
|---|---|---|---|
| `FOLDER` | (none) | Directory | — |
| `ZIP` | `.zip` | ZIP | Public domain (PKWARE APPNOTE) |
| `CBZ` | `.cbz` | ZIP (renamed) | Public domain (PKWARE APPNOTE) |
| `CB7` | `.cb7` | 7z | LGPL (7-Zip / p7zip) |
| `PDF` | `.pdf` | PDF | ISO 32000 (public standard) |
| `CBT` | `.cbt` | tar | POSIX standard |

The initial specification (from the legacy "SushiDL" project) listed
**`CBR`** (Comic Book RAR) as a supported format. During the architecture
review for NexusDL, we re-evaluated this choice.

### What is CBR?

CBR is a **Comic Book RAR** archive: a `.rar` file renamed to `.cbr`,
recognized by comic readers (Komga, Kavita, YACReader, CDisplayEx, etc.)
via the extension and, optionally, a `ComicInfo.xml` metadata file.

It is the **de facto standard** on Windows for comic book archives, and
many legacy libraries are stored as `.cbr`.

### Why is CBR problematic?

#### 1. RAR is a **proprietary, closed format**

The **RAR compression format** is owned by **Alexander Roshal** and
commercialized by **RARLAB** (formerly win.rar GmbH). Key facts:

- The **RAR specification is published**, but **decompression requires
  reverse-engineering** for third parties. RARLAB provides the **official
  `unrar` source**, but under a **restrictive license**.
- **`unrar` is NOT free software**. Its license forbids:
  - Use in a **commercial product**
  - **Redistribution** of the binary without permission
  - **Modification** of the source
  - Use to **create a RAR-compatible archiver** (only decompression is
    tolerated, and only for **interoperability**)
- RARLAB's official statement: *"You must not use UnRAR source to develop
  a RAR-compatible archiver."*

#### 2. `unrar` is **incompatible with GPL-3.0**

NexusDL is licensed under **GPL-3.0-or-later**. The GPL-3.0 is a
**copyleft** license that requires:

- **Freedom to use** the software for any purpose
- **Freedom to study and modify** the source code
- **Freedom to redistribute** copies
- **Freedom to distribute modified versions**

The `unrar` license **violates all four freedoms**:

- It **forbids** redistribution without permission
- It **forbids** modification
- It **forbids** commercial use
- It **forbids** using the source to build a compatible archiver

**Linking GPL-3.0 code with `unrar` (statically or dynamically) would make
NexusDL non-distributable** — both the FSF and RARLAB agree on this
incompatibility.

#### 3. Legal precedent

This is not a theoretical concern:

- **Debian** does not ship `unrar` in its **main** repository. It is
  available only in **`non-free`** (or `multiverse` on Ubuntu).
- **Fedora** refuses `unrar` in its official repos. It is available via
  **RPM Fusion non-free**.
- **Arch Linux** ships `unrar` in **`[multilib]`**, not `[core]`.
- **Gentoo** puts `unrar` in **`games-util`**, and requires the user to
  manually accept the license (`ACCEPT_LICENSE="unRAR"`).
- **Alpine Linux** does not ship `unrar` at all.

A GPL-3.0 project that depends on `unrar` would be **uninstallable** on
Debian main, Fedora main, and Alpine — three of the most common targets
for Python applications.

#### 4. There is **no free-software RAR decompressor**

Despite years of effort, **no fully-free RAR decompressor exists**:

- **`libarchive`** supports RAR **read** only via `unrar` (dynamic
  linking at runtime), not a reimplementation
- **`patool`** calls `unrar` as a subprocess
- **`rarfile`** (Python) also calls `unrar` as a subprocess
- **`unar`** (The Unarchiver) is **not** RAR-licensed and reimplements
  only **RAR 2.x** (not RAR 5.x, which is the current format)

**Conclusion**: any Python project that reads RAR files depends, directly
or indirectly, on `unrar`.

#### 5. Alternative formats cover the same use case

Modern comic readers support **CBZ, CB7, CBT**, and sometimes **CBZ with
AVIF** or **CBZ with JPEG XL**:

| Reader | CBZ | CB7 | CBT | CBR |
|---|---|---|---|---|
| **Komga** | ✅ | ✅ | ✅ | ⚠️ (needs `unrar`) |
| **Kavita** | ✅ | ✅ | ❌ | ⚠️ (needs `unrar`) |
| **YACReader** | ✅ | ✅ | ✅ | ⚠️ (needs `unrar`) |
| **CDisplayEx** | ✅ | ✅ | ⚠️ | ✅ |
| **Mihon / Tachiyomi** | ✅ | ⚠️ | ❌ | ⚠️ |
| **Calibre** | ✅ | ✅ | ⚠️ | ⚠️ (needs `unrar`) |

**CB7 provides the same compression ratio as CBR** (7z is comparable or
better than RAR for images) while remaining **100% free software**.

#### 6. Read vs write

NexusDL is a **downloader**, so it primarily **writes** archives. Writing
RAR would require a **RAR-compatible archiver**, which the `unrar` license
**explicitly forbids** building. So we would only ever need **read**
support (for adding files to an existing `.cbr`), which is already niche
and still requires `unrar`.

---

## Decision

**NexusDL does NOT support CBR natively.**

Instead:

1. **Supported formats** remain: `FOLDER`, `ZIP`, `CBZ`, `CB7`, `PDF`, `CBT`.
2. **CB7 replaces CBR** as the "7z-based comic format". It offers the same
   compression benefits with a fully free-software stack (`py7zr`).
3. **CBR is available via an optional external plugin**, distributed
   separately, under a different license if needed. This plugin is **not**
   part of NexusDL core, and NexusDL does not endorse or ship it.
4. **Documentation** (README, `docs/source/usage/formats.md`) explicitly
   states: *"CBR is not supported natively because RAR is proprietary and
   incompatible with GPL-3.0."*

### What happens if a user tries to use CBR

If a user passes `--format cbr`:

```bash
$ nexusdl download <url> --format cbr
Error: Unsupported format 'cbr'.
Supported formats: FOLDER, ZIP, CBZ, CB7, PDF, CBT.
CBR is not supported because RAR is proprietary.
See docs/source/development/decisions/002-no-cbr.md for details.
```

The error message **explains the reason** and links to this ADR, so users
understand the decision is intentional.

### Plugin escape hatch

Users who **really** need CBR can install a third-party plugin:

```bash
# Hypothetical third-party plugin
pip install nexusdl-cbr-plugin
```

The plugin:

- Is **not** maintained by Voldigoad-Holdings
- Is **not** covered by this ADR's scope
- Must comply with the `unrar` license **itself**
- Is responsible for its own legal compliance
- Is **not** shipped in Docker images
- Is **not** tested in NexusDL CI

This preserves the **freedom** of users while keeping **NexusDL core**
legally clean.

---

## Consequences

### Positive

- ✅ **GPL-3.0 compliance** — no license conflict, no need for legal review
- ✅ **Debian / Fedora / Alpine compatibility** — the package installs
  cleanly from `main` / `core` / `main` repos, without `non-free`
- ✅ **Docker image size** — no `unrar` binary (~10 MB saved)
- ✅ **No CVE tracking** — no `unrar` CVE exposure (there have been many)
- ✅ **No reverse-engineering concerns** — no gray-zone legal risk
- ✅ **Simpler CI** — no need to install `unrar` on 3 platforms × 2 Python
  versions
- ✅ **Community trust** — a GPL project without GPL-incompatible
  dependencies

### Negative

- ⚠️ **No native CBR** — users with existing `.cbr` libraries must convert
  them (`unrar x file.cbr` manually, or use a third-party plugin)
- ⚠️ **Some Windows users** expect `.cbr` (it is the de facto standard on
  Windows) — mitigated by README + error message + link to this ADR
- ⚠️ **Third-party plugin maintenance** — if the plugin breaks, NexusDL
  core is not responsible, but users may complain — mitigated by clear
  separation in docs
- ⚠️ **Migration friction** — existing SushiDL users who relied on CBR may
  be surprised — mitigated by `CHANGELOG.md` and migration guide

### Neutral

- 🟡 **CB7 adoption** — 7z is less familiar than RAR on Windows, but
  modern readers (Komga, Kavita, YACReader) support it fully
- 🟡 **Compression ratio** — CB7 is **equal or better** than CBR (7z
  outperforms RAR on image data), so no quality loss
- 🟡 **File size** — `.cb7` files are on average **5–15 % smaller** than
  equivalent `.cbr` files

---

## Alternatives considered

### Alternative A — Support CBR via `unrar` (rejected)

**Rejected because:** license incompatibility with GPL-3.0, unavailable
in Debian main / Fedora main / Alpine.

### Alternative B — Support CBR via a reimplementation (rejected)

**Rejected because:** no fully-free RAR decompressor exists. Building one
would require reverse-engineering the proprietary format, which is:

- A **massive** undertaking (years of work for a small team)
- **Legally risky** (RARLAB has sent DMCA notices to reimplementations)
- **Not the project's mission**

### Alternative C — Support CBR conditionally at runtime (rejected)

**Idea:** if `unrar` is present on the system, use it; otherwise, error.

**Rejected because:**

- Still requires `unrar` to be installed, which violates the "install
  cleanly from `pip`" promise
- Would break Docker images (no `unrar` in base image)
- Would break CI on Ubuntu / macOS / Windows (would need to install
  `unrar` on all three)
- Would **violate GPL-3.0** by dynamically linking to a non-free library
- Would make it **impossible to audit** whether the app is GPL-compliant
  for redistribution

### Alternative D — Support CBR via a subprocess (rejected)

**Idea:** shell out to `unrar` if available.

**Rejected because:**

- Still requires `unrar` installed
- `subprocess` calls a non-free binary → **GPL-3.0 "aggregate" violation**
  in the FSF's view (some argue it's OK, but it's a gray area)
- Security risk (subprocess injection if filenames are crafted)
- Platform-dependent (no `unrar` on Windows by default)

### Alternative E — Only support CBR in a plugin (chosen)

**Chosen because:**

- Keeps core GPL-3.0-clean
- Users who need CBR can install a plugin at their own risk
- Complies with the *spirit* of the GPL (freedom) while keeping the
  *letter* clean (no non-free linkage)
- Plugin can be maintained by a third party, with its own legal review

### Alternative F — Wait for a free RAR implementation (rejected)

**Rejected because:** no free RAR 5.x implementation exists, and none is
on the horizon. We cannot block NexusDL 1.0.0 on an event that may never
happen.

---

## References

### Legal / licensing

- **RARLAB** — *UnRAR license*:
  <https://www.rarlab.com/license.htm>
- **GNU Project** — *GPL-3.0 and non-free libraries*:
  <https://www.gnu.org/licenses/gpl-faq.html#GPLIncompatibleLibs>
- **Debian** — *non-free firmware and non-free software*:
  <https://www.debian.org/social_contract#guidelines>
- **Fedora** — *Software License Guidelines*:
  <https://docs.fedoraproject.org/en-US/legal/licensing-policy/>

### Technical

- **RAR 5.0 format specification**:
  <https://www.rarlab.com/technote.htm>
- **`py7zr`** (CB7): <https://py7zr.readthedocs.io/>
- **`libarchive`** (RAR support): <https://www.libarchive.org/>
- **`rarfile`** (Python RAR reader, depends on `unrar`):
  <https://rarfile.readthedocs.io/>
- **`patool`** (Python archive wrapper): <https://wummel.github.io/patool/>

### Related ADRs

- **ADR-001**: Hexagonal Architecture
- **ADR-003**: Parser Factory Dependency Injection
- **ADR-004**: `sites.yaml` vs `.env` separation
- **ADR-005**: Multi-platform builds

### Community discussions

- **Tachiyomi / Mihon** — no CBR support:
  <https://github.com/mihonapp/mihon/issues> (search "CBR")
- **Komga** — CBR requires `unrar`:
  <https://komga.org/installation/faq.html>
- **Kavita** — CBR requires `unrar`:
  <https://wiki.kavitareader.com/installation/>

---

## Review and amendments

This ADR is **living** — if a fully-free RAR decompressor appears, we can
revisit this decision via a **new ADR** (e.g.
`006-add-cbr-support-via-free-implementation.md`). Until then, this ADR
stands.

**Amendments log:**

| Date | Change | ADR |
|---|---|---|
| 2026-10-28 | Initial version | 002 |
| — | (none yet) | — |

---

## How to verify compliance

```bash
# Ensure no CBR code or dependency exists
grep -rE "unrar|rarfile|\.cbr" src/ scripts/ --include="*.py" \
  | grep -v "no-cbr.md" \
  | grep -v "002-no-cbr" \
  && echo "❌ CBR reference found" \
  || echo "✅ No CBR code or dependency"

# Ensure py7zr is present (CB7 support)
grep -q "py7zr" pyproject.toml && echo "✅ py7zr present" || echo "❌ py7zr missing"

# Ensure unrar is NOT in dependencies
grep -qE "unrar|rarfile" pyproject.toml \
  && echo "❌ unrar/rarfile in dependencies" \
  || echo "✅ No unrar/rarfile in dependencies"
```

Expected output:

```
✅ No CBR code or dependency
✅ py7zr present
✅ No unrar/rarfile in dependencies
```

---

## Summary

| Aspect | Decision |
|---|---|
| CBR support | ❌ Not supported natively |
| Reason | RAR is proprietary, incompatible with GPL-3.0 |
| Alternative | CB7 (`py7zr`), fully free software |
| Escape hatch | Third-party plugin (`nexusdl-cbr-plugin`), not maintained by us |
| Error message | Explains the reason and links to this ADR |
| Migration | `unrar x file.cbr` manually, or use a plugin |
| Reversible? | Yes, via a new ADR if a free RAR implementation appears |

---

*This ADR is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../../LICENSE).*
