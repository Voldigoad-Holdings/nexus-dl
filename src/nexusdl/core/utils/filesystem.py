"""Canonical filesystem primitives for atomic writes, cleanup, and validation.

The pipeline writes files in dozens of places — image optimizers, format
converters, cache entries, packagers (ZIP, CBZ, CB7, PDF, folder), the
encrypted cookie store. Every one of those writes has the same
requirement: **the target file must never be observable in a partially
written state**. A crash, a cancellation, a disk-full error, an
exception in the caller — none of them may leave the user with a
corrupt archive where a valid one used to be.

The implementation of that requirement is the same every time:

1. Write to a sibling temporary file.
2. ``fsync`` the temp file.
3. Atomically ``rename`` the temp file onto the target.

The atomically-renamed file is the standard POSIX idiom and the only
portable way to guarantee the invariant on every filesystem the pipeline
supports. It is also the piece of code that has been duplicated most
often in the pipeline's history — ``image/optimizer.py``,
``image/converter.py``, ``zip_packager.py``, ``cbz_packager.py``,
``cb7_packager.py``, ``pdf_packager.py``, ``folder_packager.py``,
``cache.py``, and ``cookie_manager.py`` each have their own copy, with
slightly different signatures, slightly different cleanup policies, and
slightly different bug surfaces.

This module is the **canonical** implementation. When a caller is next
opened for any reason, its private ``_atomic_write`` / ``_fsync_and_replace``
helper should be replaced by an import from here. Each substitution is
a one-line change, behaviour-preserving, and removes a small but real
source of divergence.

What this module provides
-------------------------
* **Atomic writes** — :func:`atomic_write_bytes`,
  :func:`atomic_write_text`, :func:`atomic_replace_directory`. All
  three guarantee the invariant described above, and all three clean up
  their temp file on **every** exit path — including
  ``asyncio.CancelledError``.
* **Safe deletion** — :func:`delete_file_quietly`. Best-effort,
  returns a ``bool`` so the caller can log without duplicating the
  error handling.
* **Safe metadata access** — :func:`get_file_size`. Returns ``None``
  for anything that is not a readable regular file, so a caller can
  sum sizes or report a total without wrapping every read in its own
  ``try/except``.
* **Directory helpers** — :func:`ensure_directory`,
  :func:`fsync_directory`. Both do the obvious thing and no more.
* **Permission helper** — :func:`chmod_owner_only`. POSIX-only
  semantics; a caller that needs the fallback (Windows) receives
  ``False`` and decides what to do.
* **Human-readable sizes** — :func:`human_readable_size`. For CLI
  output and log lines; the "1.50 MiB" that a user actually reads.
* **Filename validation** — :func:`validate_filename` /
  :func:`is_safe_filename`. Rejects traversal, reserved characters,
  and leading/trailing whitespace before a filename reaches the
  filesystem. The canonical implementation of the check that
  ``page.py::_validate_filename`` currently performs inline.

What this module deliberately does **not** do
---------------------------------------------
* **No logging.** Every other module in :mod:`nexusdl.core.utils` is a
  pure library; this one is the same. Functions that can fail in
  non-critical ways return a ``bool`` or ``None`` — the caller decides
  whether the failure is worth logging and how.
* **No path configuration.** No "cache directory" or "default
  downloads directory" concept. The caller provides the path; this
  module acts on it. Configuration belongs in
  :class:`~nexusdl.core.config.Settings`, not here.
* **No file watching, no polling, no background cleanup.** Each
  function performs one operation and returns.
* **No fallback for cross-filesystem renames.** Because the temp file
  is always created in the target's parent directory, the ``rename``
  always occurs on the same filesystem. A caller that manually creates
  a temp file on a different filesystem and calls
  :func:`atomic_replace_directory` gets the standard
  ``OSError: EXDEV`` from the kernel — that is a caller bug, not a
  condition this module silently works around.
* **No os-specific branching in the happy path.** Every atomic write
  goes through :func:`tempfile.mkstemp` (which is portable) and
  :meth:`pathlib.Path.replace` (which is atomic on every filesystem
  that supports POSIX rename semantics — ext4, APFS, NTFS, everything
  modern). The only OS branch in this module is in
  :func:`chmod_owner_only`, where it is unavoidable.
* **No async wrappers.** Atomic writes are fast (milliseconds for
  everything the pipeline writes) and the async call sites already run
  them in a worker thread via ``asyncio.to_thread`` when the payload
  could be large. Adding async variants would be ceremony without
  benefit. Callers needing to write from a coroutine use
  ``await asyncio.to_thread(atomic_write_bytes, path, payload)`` —
  a two-line call site, documented in each function's docstring.

Security notes
--------------
* **Temp-file permissions.** :func:`tempfile.mkstemp` creates the temp
  file with mode ``0o600`` on POSIX (owner-read-write only). The
  subsequent atomic rename preserves that mode, so the resulting file
  is **always** owner-read-write on POSIX, never group- or
  world-readable. This is the correct default for everything the
  pipeline writes (cookies, cache entries, archives) and it means
  :func:`chmod_owner_only` is **redundant** after an atomic write — it
  is provided for callers that write through some other path and need
  to enforce the mode post-hoc.
* **No followed symlinks.** All write helpers open the temp file, not
  the target. A caller that passes a symlink as the target gets a
  symlink whose *referent* is atomically replaced — matching
  :meth:`pathlib.Path.replace` semantics and the behaviour every tool
  in the ecosystem expects. A caller that needs to reject symlinks
  checks ``path.is_symlink()`` before calling.
* **Filename validation is not path sanitization.** :func:`validate_filename`
  rejects filenames that are not single path components. It does
  **not** sanitize arbitrary text into a safe filename — for that, see
  :func:`nexusdl.core.utils.text.slugify_safe`, which is the correct
  primitive for "turn a title into a filename".

Migration targets
-----------------
The following helpers are documented as refactor targets — one-line
substitution, behaviour-preserving, no forced change:

* ``image/optimizer.py::_atomic_write`` → :func:`atomic_write_bytes`
* ``image/converter.py::_atomic_write`` → :func:`atomic_write_bytes`
* ``zip_packager.py::_fsync_and_replace`` → :func:`atomic_write_bytes`
* ``cbz_packager.py::_fsync_and_replace`` → :func:`atomic_write_bytes`
* ``cb7_packager.py::_fsync_and_replace`` → :func:`atomic_write_bytes`
* ``pdf_packager.py::_fsync_and_replace`` → :func:`atomic_write_bytes`
* ``folder_packager.py::_fsync_directory`` → :func:`fsync_directory`
* ``folder_packager.py::_atomic_replace_directory`` → :func:`atomic_replace_directory`
* ``cache.py::_atomic_write_bytes`` → :func:`atomic_write_bytes`
* ``cookie_manager.py::_chmod_owner_only`` → :func:`chmod_owner_only`
* ``database.py::_delete_file_quietly`` → :func:`delete_file_quietly`

Dependency graph (no cycles)::

    os, pathlib, re, shutil, stat, tempfile, typing (stdlib)

This module is a **leaf**: it imports nothing from ``nexusdl`` and
nothing from third parties. It could be lifted into a standalone
utility library without modification.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Final
from uuid import uuid4


__all__ = [
    "atomic_replace_directory",
    "atomic_write_bytes",
    "atomic_write_text",
    "chmod_owner_only",
    "delete_file_quietly",
    "ensure_directory",
    "fsync_directory",
    "get_file_size",
    "human_readable_size",
    "is_safe_filename",
    "validate_filename",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Maximum number of bytes in the "human-readable" magnitude chain. Beyond
#: this the caller has bigger problems than reading the log line — this
#: constant exists only to bound the loop in :func:`human_readable_size`.
_SIZE_UNITS: Final[tuple[str, ...]] = (
    "KiB",
    "MiB",
    "GiB",
    "TiB",
    "PiB",
    "EiB",
)

#: Per-1024 multiplier. Named for readability in
#: :func:`human_readable_size`.
_SIZE_STEP: Final[float] = 1024.0

#: Pattern matching any character that is not allowed in a filename on any
#: platform NexusDL supports. The set is the union of:
#:
#: * Windows-reserved characters: ``< > : " / \ | ? *``
#: * The POSIX path separator: ``/`` (also in the Windows set)
#: * C0 control characters: ``\x00``–``\x1f``
#:
#: Rejecting the union on every platform is deliberate: a filename that
#: is safe on POSIX but breaks on Windows produces a bug report that is
#: impossible to reproduce on the reporter's machine. Rejecting early
#: keeps the pipeline portable.
_INVALID_FILENAME_CHARS: Final[re.Pattern[str]] = re.compile(
    r'[<>:"/\\|?*\x00-\x1f]'
)

#: Temp-file prefix used by the write helpers. Matches the convention used
#: by the packagers and the cache so a stray temp file is recognisable by
#: name alone.
_TEMP_FILE_PREFIX_FMT: Final[str] = ".{name}.tmp-"

#: Backup directory suffix used by :func:`atomic_replace_directory`.
_BACKUP_DIR_SUFFIX: Final[str] = ".old-"


# --------------------------------------------------------------------------- #
# Atomic writes                                                               #
# --------------------------------------------------------------------------- #


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write *payload* to *path*, atomically replacing any existing file.

    Creates a sibling temp file in ``path.parent``, writes *payload*,
    ``fsync``'s it, then atomically renames it onto *path*. The target
    file is either the previous content or the new content — never a
    partial write, on any exit path including ``asyncio.CancelledError``.

    On POSIX, the resulting file has mode ``0o600`` (owner-read-write
    only) because :func:`tempfile.mkstemp` creates the temp file with
    those permissions and the rename preserves them. This is the correct
    default for the pipeline (cookies, cache entries, archives are not
    world-readable). Callers that need a wider mode relax it explicitly
    via :func:`chmod_owner_only`'s inverse or :func:`os.chmod`.

    The parent directory is created if it does not exist. A caller that
    needs to prepare it in advance uses :func:`ensure_directory` and
    then calls this function — the implicit mkdir here is a
    convenience, not a policy.

    Args:
        path: Destination path. May refer to an existing file (which is
            replaced) or a new one (which is created). Intermediate
            directories are created.
        payload: The bytes to write. May be empty.

    Raises:
        OSError: If any step (temp creation, write, ``fsync``, or
            rename) fails. The previous target (if any) is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=_TEMP_FILE_PREFIX_FMT.format(name=path.name),
        suffix=".tmp",
    )
    temp_path = Path(raw_temp)

    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        temp_path.replace(path)
    except BaseException:
        # ``BaseException`` (not just ``Exception``) so the temp file is
        # removed even on ``CancelledError`` / ``KeyboardInterrupt``.
        # The exception is re-raised unchanged.
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write *text* to *path*, atomically replacing any existing file.

    Thin wrapper around :func:`atomic_write_bytes` that encodes the text
    first. See that function for the atomicity, permission, and exception
    contracts — they are identical.

    Args:
        path: Destination path.
        text: The text to write.
        encoding: Text encoding. Defaults to UTF-8, matching every
            other text write in the pipeline.

    Raises:
        OSError: If any filesystem operation fails.
        UnicodeEncodeError: If *text* contains characters the encoding
            cannot represent. UTF-8 is total over the Unicode range, so
            this only fires for an explicit non-UTF-8 *encoding*.
    """
    atomic_write_bytes(path, text.encode(encoding))


def atomic_replace_directory(tmpdir: Path, target: Path) -> Path | None:
    """Atomically replace *target* with *tmpdir*, preserving content.

    Two cases:

    * ``target`` does not exist — a single ``rename`` puts *tmpdir* in
      place. Genuinely atomic.
    * ``target`` exists — the existing directory is renamed to a unique
      backup name, *tmpdir* is renamed to *target*, and the backup is
      deleted. The window where *target* does not exist is bounded by
      two ``rename`` syscalls (microseconds) and contains no other
      filesystem work, so a concurrent reader sees the previous content
      or the new content or ``ENOENT`` — never a partially-replaced
      directory.

    Unlike :func:`atomic_write_bytes`, this helper **cannot** roll back
    if the second rename fails and the restore also fails: the caller is
    left in a state where *target* does not exist and a backup does.
    That state is reported via the return value so the caller can log
    it and instruct the operator. In practice this only happens on a
    filesystem that has become read-only between the two renames —
    exceptional enough that surviving it correctly is more valuable than
    pretending it cannot occur.

    Args:
        tmpdir: The freshly-built directory. Must exist and be complete
            — this helper does not populate it. On return, it no longer
            exists at this path (unless the operation failed before the
            first rename).
        target: The final destination. May or may not exist.

    Returns:
        ``None`` if the operation was fully clean. A :class:`Path` to
        the leftover backup directory if *target* was correctly replaced
        but the backup could not be deleted — the target is correct;
        the backup is a stray directory the caller can clean up at its
        leisure.

    Raises:
        OSError: If any rename fails and the previous state cannot be
            restored. The exception message names both the failing
            operation and the location of any backup that survived.
    """
    if not target.exists():
        tmpdir.rename(target)
        return None

    backup = target.parent / (
        f"{target.name}{_BACKUP_DIR_SUFFIX}{uuid4().hex}"
    )
    target.rename(backup)

    try:
        tmpdir.rename(target)
    except OSError as rename_exc:
        # The new content could not be moved into place. Try to restore
        # the previous state; if restore also fails, both the caller and
        # the operator need to know that the backup is what survived.
        try:
            backup.rename(target)
        except OSError as restore_exc:
            raise OSError(
                f"cannot replace directory {target!s} and cannot "
                f"restore previous state from backup at {backup!s}: "
                f"rename failed with {rename_exc!r}; restore failed "
                f"with {restore_exc!r}"
            ) from rename_exc
        raise

    try:
        shutil.rmtree(backup)
    except OSError:
        # Target is correct; backup is leftover. Return the path so the
        # caller can log it — this module does not log.
        return backup

    return None


# --------------------------------------------------------------------------- #
# Sync helpers                                                                #
# --------------------------------------------------------------------------- #


def fsync_directory(directory: Path) -> None:
    """Flush *directory*'s entries to disk, best-effort.

    After a batch of file renames inside *directory*, calling
    ``fsync`` on the directory makes the new *names* durable — without
    it, a crash can lose the rename even though the file content was
    ``fsync``'d. The operation is best-effort because a directory
    ``fsync`` is unsupported on Windows and unreliable on some
    network filesystems; a caller that needs the durability guarantee
    treats it as advisory and does not raise on failure.

    On POSIX, a failed open (permission error, directory disappeared
    between the caller's check and this call) or a failed ``fsync`` is
    silently ignored — the caller's subsequent rename remains the
    atomicity boundary, and the durability of the directory entry itself
    is a nice-to-have, not a contract this helper can make on every
    supported filesystem.

    Args:
        directory: The directory whose entries to flush. Must exist.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        # ``os.open`` on a directory is unsupported on Windows and can
        # fail on POSIX for a directory the caller lost access to
        # between the previous operation and this call. Both are
        # acceptable; the rename is the atomicity boundary.
        return

    try:
        os.fsync(fd)
    except OSError:
        # Best-effort. See docstring.
        pass
    finally:
        os.close(fd)


def chmod_owner_only(path: Path) -> bool:
    """Set *path*'s permissions to owner-read-write only (``0o600``).

    On POSIX, ``os.chmod(path, 0o600)`` succeeds and the function
    returns ``True``. On Windows, ``os.chmod`` only sets the read-only
    bit — the "owner-only" guarantee is provided by the operating
    system's ACL model, not by this function. The call succeeds but
    is a no-op for the intended semantics; the function still returns
    ``True`` because the caller's contract (``"try to set 0o600"``) was
    honoured without error.

    Intended for callers that write to a path **outside** the atomic
    pipeline of this module. After a call to :func:`atomic_write_bytes`,
    the target already has mode ``0o600`` (because the rename preserved
    the temp file's mode) — the function is redundant in that case but
    harmless.

    The function does **not** log. A failure is reported via the return
    value; the caller decides whether the situation is worth logging at
    ``WARNING``, ``DEBUG``, or not at all.

    Args:
        path: The file to chmod.

    Returns:
        ``True`` if the ``chmod`` call succeeded (which is what
        "enforce 0o600" means on the current platform). ``False`` if
        the call raised ``OSError`` — a file that no longer exists, a
        path the caller cannot traverse, a filesystem that rejects the
        mode.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Deletion and directory creation                                             #
# --------------------------------------------------------------------------- #


def delete_file_quietly(path: Path) -> bool:
    """Delete *path* if it exists. Never raises.

    Returns whether the file is **gone** after the call — either because
    it was deleted or because it never existed. ``False`` means the file
    still exists (permission denied, path is a non-empty directory,
    filesystem error). The distinction matters: a caller cleaning up
    after a cancelled download wants to know whether the file it
    expected to remove actually went away, without wrapping the call in
    ``try/except`` at every site.

    A path that names a directory is **not** removed by this function.
    A caller that needs to delete a directory tree uses
    :func:`shutil.rmtree` directly — the difference between "delete a
    file" and "delete a tree" is worth surfacing at the call site.

    Args:
        path: The path to delete. A missing path is a success.

    Returns:
        ``True`` if *path* does not exist after the call, ``False``
        otherwise.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def ensure_directory(path: Path) -> Path:
    """Create *path* and any missing parents, then return *path*.

    A thin wrapper around :meth:`pathlib.Path.mkdir` with
    ``parents=True, exist_ok=True`` — the two flags every call site in
    the pipeline uses. Provided so that the call sites read as
    ``ensure_directory(p)`` rather than the triple-flag combination,
    and so that a future change to the creation semantics (adding an
    ownership fix, a permission fix) has a single place to live.

    Args:
        path: The directory to create.

    Returns:
        *path*, for chaining.

    Raises:
        FileExistsError: If *path* exists but is not a directory.
        NotADirectoryError: If a component of *path* (other than the
            final one) is a file.
        PermissionError: If the process lacks permission to create any
            of the intermediate directories.
        OSError: On any other filesystem failure.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# File metadata                                                               #
# --------------------------------------------------------------------------- #


def get_file_size(path: Path) -> int | None:
    """Return the size of *path* in bytes, or ``None`` for non-files.

    A **safe** ``stat`` wrapper: any error (the path does not exist, the
    caller cannot read the metadata, the path is a directory, a socket,
    a device) yields ``None`` instead of raising. Intended for summing
    sizes across a directory tree or reporting a total, where a single
    unreadable file should not abort the computation.

    For a **symlink to a regular file**, this function follows the
    symlink and returns the referent's size — matching
    :meth:`Path.stat` semantics. For a broken symlink or a symlink to a
    directory, it returns ``None``.

    Args:
        path: The path whose size to read.

    Returns:
        The size in bytes for a readable regular file, ``None``
        otherwise.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_size


def human_readable_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable binary-suffixed string.

    Uses binary prefixes (``KiB`` / ``MiB`` / … — 1 KiB = 1024 B), which
    match the constants the pipeline uses internally
    (``_MAX_CACHEABLE_BODY_SIZE = 10 * 1024 * 1024``, etc.). The output
    is what a user actually reads in a CLI or a diagnostic endpoint.

    Formatting rules:

    * ``< 1024`` → integer bytes: ``"512 B"``, ``"0 B"``.
    * ``≥ 1024`` → two decimal places, one binary suffix:
      ``"1.50 KiB"``, ``"500.00 MiB"``, ``"1.00 GiB"``.

    Two decimal places is the convention every file manager and shell
    uses for this range. Trailing zeros are not stripped — ``"1.00 GiB"``
    reads more clearly than ``"1 GiB"`` at a glance.

    Args:
        size_bytes: The number of bytes. Must be ``>= 0``.

    Returns:
        A human-readable string.

    Raises:
        ValueError: If *size_bytes* is negative — a negative file size
            is a caller bug (a computed value that overflowed, or a
            hardcoded constant with the wrong sign) and silently
            returning ``"0 B"`` would hide it.
    """
    if size_bytes < 0:
        raise ValueError(
            f"human_readable_size() requires size_bytes >= 0, got "
            f"{size_bytes}"
        )
    if size_bytes < 1024:
        return f"{size_bytes} B"

    value = float(size_bytes)
    for unit in _SIZE_UNITS:
        value /= _SIZE_STEP
        if value < _SIZE_STEP:
            return f"{value:.2f} {unit}"

    # A value that is still above _SIZE_STEP after the largest unit
    # would require at least an ZiB — the pipeline will never write a
    # file that large. Return the last unit anyway; a caller who
    # somehow reaches it gets a technically-correct number that will
    # look absurd, which is the correct signal.
    return f"{value:.2f} {_SIZE_UNITS[-1]}"


# --------------------------------------------------------------------------- #
# Filename validation                                                         #
# --------------------------------------------------------------------------- #


def validate_filename(name: str) -> None:
    """Raise :class:`ValueError` if *name* is not a safe single-component filename.

    Accepts any non-empty string that:

    * is not ``"."`` or ``".."`` (reserved);
    * contains none of ``< > : " / \\ | ? *`` or a C0 control character;
    * has no leading or trailing whitespace.

    The rejected character set is the union of the Windows-reserved set,
    the POSIX path separator, and C0 control characters. Rejecting the
    union on every platform (not just Windows) guarantees that a file
    written on one platform can be read on another without surprising
    the user. This is the canonical implementation of the check that
    :meth:`nexusdl.core.models.page.Page._validate_filename` currently
    performs inline.

    The check is a **validation**, not a sanitization. Callers that
    need to transform arbitrary text into a safe filename use
    :func:`nexusdl.core.utils.text.slugify_safe`, which produces a
    filename from a title; callers that have a filename and want to
    reject it if it is unsafe use this function.

    Args:
        name: The filename to validate.

    Raises:
        ValueError: If *name* fails any of the checks above. The
            message names the specific condition.
    """
    if not name:
        raise ValueError("filename must be non-empty")
    if name in (".", ".."):
        raise ValueError(
            f"filename must not be {name!r}"
        )
    if _INVALID_FILENAME_CHARS.search(name):
        raise ValueError(
            f"filename contains invalid characters: {name!r}"
        )
    if name != name.strip():
        raise ValueError(
            f"filename must not have leading or trailing whitespace: "
            f"{name!r}"
        )


def is_safe_filename(name: str) -> bool:
    """Return ``True`` iff :func:`validate_filename` would accept *name*.

    Boolean counterpart to :func:`validate_filename`, for call sites
    that want to branch on validity without wrapping the check in a
    ``try/except``. The two functions share the same rules — validating
    or checking always agrees.

    Args:
        name: The filename to check.

    Returns:
        ``True`` if the name is a valid single-component filename,
        ``False`` otherwise.
    """
    try:
        validate_filename(name)
    except ValueError:
        return False
    return True
