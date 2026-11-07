"""Domain model for a single page of a chapter.

A :class:`Page` is the smallest unit the download pipeline moves around: one
image file, identified by its position within a chapter. It is a **pure
data structure** — it does not know its parent chapter, its parent manga,
or its on-disk location, and it performs no I/O. Every collaborator that
needs contextual information (the chapter, the destination path, the
client) carries it separately.

Where a page fits
-----------------
::

    parser.get_pages(chapter) ──► list[Page]
                                      │
                                      ▼
    worker._download_pages(pages, temp_dir)
        per page:
            parser.download_page(page, dest)
            validate_file(dest)
            plugin_dispatcher.dispatch_on_page_downloaded(page, dest)

The parser produces the list, the worker consumes it. Neither the chapter
nor the manga is attached to the :class:`Page` — the caller always has
that context in scope, and attaching it would duplicate information that
can drift if a page were ever moved between chapters (an operation that
does not exist, but which would otherwise leave :class:`Page` lying).

Required fields vs optional fields
----------------------------------
The pipeline strictly needs only two fields:

* :attr:`Page.index` — 0-based position within the chapter (PROMPT MASTER
  contract). The worker uses it to order pages and to build zero-padded
  filenames for CBZ readers (Komga, Kavita, Mihon, YACReader).
* :attr:`Page.url` — the absolute HTTPS URL the parser will download the
  page from.

Everything else is optional metadata that the parser provides when the
site exposes it, and that downstream code (CBZ ``ComicInfo.Pages``,
readers with lazy layout) can consume to avoid a second HTTP round-trip:

* :attr:`Page.filename` — the parser's preferred on-disk filename. When
  ``None``, the worker derives a zero-padded index + extension from
  :attr:`Page.url`. When set, the value **must** be a single-component
  filename (no separators, no traversal); this is enforced at
  construction time.
* :attr:`Page.width` / :attr:`Page.height` — pixel dimensions, when the
  site reports them in the chapter listing (MangaDex, Comick, and most
  API-based sites do; HTML scrapers usually do not).

Why no ``chapter_id`` / ``manga_id``
------------------------------------
Adding those fields would make every :class:`Page` a mini-aggregate. It
would also mean the ``parser.get_pages(chapter)`` contract has to accept a
chapter that pages can then *disagree with*, which is a state that has no
caller and no test. The rule for value objects applies: attach only what
the object can validate. ``Page`` cannot validate that its ``chapter_id``
matches a live chapter row — so it does not carry one.

HTTPS enforcement
-----------------
:attr:`Page.url` **must** be a well-formed absolute HTTPS URL. Every source
site NexusDL supports serves HTTPS, and a plaintext image URL is a MITM
surface for the page content. A site that genuinely serves HTTP images is
a bug to fix upstream (or to fix with a proxy), not a compatibility issue
the model should absorb. The URL is parsed with
:func:`urllib.parse.urlparse` rather than a bare string-prefix check, so
malformed inputs (``"https://"`` with no host) are rejected before they
reach the worker's HTTP client.

Filename safety
---------------
:attr:`Page.filename` (when set) is used verbatim by the worker to name
files inside the temporary directory and inside the final CBZ. The model
rejects every character class that:

* is a path separator on POSIX or Windows (``/`` ``\\``),
* is reserved by Windows (``<`` ``>`` ``:`` ``"`` ``|`` ``?`` ``*``),
* is a C0 or C1 control character (``\\x00``–``\\x1f``, ``\\u0080``–``\\u009f``),
* is a bidi control (``\\u202a``–``\\u202e``, ``\\u2066``–``\\u2069``) — these
  can spoof the visual representation of a filename,
* is leading or trailing whitespace.

The 255-character limit (not bytes) matches the filename limit of ext4,
NTFS, and APFS. A filename longer than that would fail at write time with
a platform-specific ``OSError``; failing at construction time instead
gives the parser a clear error message and a stack trace pointing at the
offending site.

Identity and equality
---------------------
The default dataclass equality (all fields, since ``frozen=True`` and
``eq`` is left implicit) is deliberate. Two pages with the same ``index``
and ``url`` but different ``width`` are **not** equal — the extra
dimension information changes the value. Callers who want to deduplicate
by "position within the chapter" should key on ``(page.index, page.url)``
explicitly, which documents the intent at the call site.

Dependency graph (no cycles)::

    (no imports)

This file has **zero** in-package dependencies. It could be lifted into a
standalone library without modification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse


__all__ = ["Page"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Expected scheme for :attr:`Page.url`. Compared case-insensitively via
#: :func:`urllib.parse.urlparse` (RFC 3986 §3.1).
_HTTPS_SCHEME: Final[str] = "https"

#: Filename length limit. Matches ext4, NTFS, and APFS — the shortest
#: filename limit across the platforms NexusDL supports. A longer name
#: would fail at write time with a platform-specific ``OSError``.
_MAX_FILENAME_LENGTH: Final[int] = 255

#: Characters forbidden in :attr:`Page.filename`. Matches the union of:
#:
#: * Windows-reserved characters (``<>:"/\|?*``),
#: * POSIX path separator (``/``),
#: * C0 control characters (``\x00``–``\x1f``).
#:
#: Rejecting the Windows set even on POSIX is deliberate — NexusDL is
#: cross-platform, and a parser that produces a valid-POSIX-but-invalid-
#: Windows filename would produce an unreproducible bug report on a
#: user's machine.
_INVALID_FILENAME_CHARS: Final[re.Pattern[str]] = re.compile(
    r'[<>:"/\\|?*\x00-\x1f]'
)

#: Additional characters rejected from :attr:`Page.filename`:
#:
#: * C1 control characters (``\u0080``–``\u009f``) — invisible and
#:   unprintable, but not caught by the ASCII-range regex above.
#: * Bidi control characters (``\u202a``–``\u202e`` and
#:   ``\u2066``–``\u2069``) — these can make a filename display
#:   differently from its actual byte content, which is a visual
#:   spoofing surface even when the parser is trusted code.
_UNSAFE_UNICODE_CHARS: Final[re.Pattern[str]] = re.compile(
    "[\u0080-\u009f\u202a-\u202e\u2066-\u2069]"
)


def _is_https_url(value: str) -> bool:
    """Return ``True`` iff *value* is a well-formed absolute HTTPS URL.

    Performs a full parse rather than a string-prefix check so that
    ``"https://"`` (no host) and ``"https:// space"`` (whitespace in
    netloc) are rejected. Scheme is compared case-insensitively —
    ``"HTTPS://x"`` is accepted per RFC 3986 §3.1.

    Args:
        value: The candidate URL.

    Returns:
        ``True`` for well-formed absolute HTTPS URLs with a non-empty host.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() == _HTTPS_SCHEME and bool(parsed.netloc)


# --------------------------------------------------------------------------- #
# Page                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a chapter.

    Immutable. To produce a page with extra metadata filled in — e.g. when
    a parser learns ``width`` / ``height`` after an initial listing — use
    :func:`dataclasses.replace`::

        updated = replace(page, width=1200, height=1800)

    Attributes:
        index: **0-based** position of the page within the chapter. The
            first page has ``index=0``. The value must be ``>= 0`` and
            unique within a chapter's page list; the model checks the
            former and leaves the latter to the parser (which is the only
            layer that knows the full list).

            Downstream code that displays a 1-based page number should
            use :attr:`page_number` rather than ``index + 1`` inline —
            it reads better and avoids off-by-one mistakes at call sites.
        url: Well-formed absolute ``https://`` URL the parser will
            download the page from. Not a ``file://`` URL and not a
            relative path — pages are always fetched from a remote source
            by the worker. A ``file://`` URL would mean the downloader is
            a no-op, which is not what the pipeline is for; a local
            library scanner that imports pre-existing archives does not
            use :class:`Page` at all (it uses
            :class:`~nexusdl.core.library.metadata.ExtractedMetadata`).
        filename: Optional filename to use on disk, overriding the
            default (``"0001.jpg"``, ``"0002.jpg"``, …) that the worker
            would otherwise derive from :attr:`index` and :attr:`url`.
            When set, the value **must** be a single-component filename:
            no path separators, no ``.`` or ``..``, no C0 / C1 control
            characters, no bidi control characters, no Windows-reserved
            characters, no leading or trailing whitespace, and at most
            255 characters. The worker uses the value verbatim; the
            parser is responsible for making it unique within the
            chapter.
        width: Optional pixel width of the page, when the site exposes
            it. Positive integer when set. Consumed by future
            ``ComicInfo.Pages`` enrichment; not required by the current
            pipeline.
        height: Optional pixel height. Same contract as :attr:`width`.
    """

    index: int
    url: str
    filename: str | None = None
    width: int | None = None
    height: int | None = None

    # ------------------------------------------------------------------ #
    # Validation                                                         #
    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        """Validate field types and value constraints.

        Raises:
            TypeError: If a field has the wrong Python type.
            ValueError: If ``index`` is negative, ``url`` is not a
                well-formed HTTPS URL, ``filename`` is not a valid
                single-component filename, or ``width`` / ``height`` is
                non-positive.
        """
        # --- index --------------------------------------------------- #
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise TypeError(
                f"Page.index must be an int, got {type(self.index).__name__}"
            )
        if self.index < 0:
            raise ValueError(
                f"Page.index must be >= 0, got {self.index}"
            )

        # --- url ----------------------------------------------------- #
        if not isinstance(self.url, str):
            raise TypeError(
                f"Page.url must be a str, got {type(self.url).__name__}"
            )
        if not _is_https_url(self.url):
            raise ValueError(
                f"Page.url must be a well-formed absolute HTTPS URL, "
                f"got {self.url!r}"
            )

        # --- filename ------------------------------------------------ #
        if self.filename is not None:
            if not isinstance(self.filename, str):
                raise TypeError(
                    f"Page.filename must be a str or None, got "
                    f"{type(self.filename).__name__}"
                )
            self._validate_filename(self.filename)

        # --- width / height ------------------------------------------ #
        for field_name, value in (
            ("width", self.width),
            ("height", self.height),
        ):
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"Page.{field_name} must be an int or None, got "
                    f"{type(value).__name__}"
                )
            if value < 1:
                raise ValueError(
                    f"Page.{field_name} must be >= 1, got {value}"
                )

    @staticmethod
    def _validate_filename(filename: str) -> None:
        """Reject filenames that are empty, traversal, or platform-hostile.

        Args:
            filename: The candidate filename.

        Raises:
            ValueError: If the name is empty, is ``.`` or ``..``, exceeds
                :data:`_MAX_FILENAME_LENGTH`, contains a path separator, a
                Windows-reserved character, a C0 / C1 control character,
                a bidi control character, or has leading / trailing
                whitespace.
        """
        if not filename:
            raise ValueError(
                "Page.filename must be non-empty when provided"
            )
        if filename in (".", ".."):
            raise ValueError(
                f"Page.filename must not be {filename!r}"
            )
        if len(filename) > _MAX_FILENAME_LENGTH:
            raise ValueError(
                f"Page.filename must be at most "
                f"{_MAX_FILENAME_LENGTH} characters, got {len(filename)} "
                f"(was the site-supplied title unusually long?)"
            )
        if _INVALID_FILENAME_CHARS.search(filename):
            raise ValueError(
                f"Page.filename contains invalid ASCII characters: "
                f"{filename!r}"
            )
        if _UNSAFE_UNICODE_CHARS.search(filename):
            raise ValueError(
                f"Page.filename contains control or bidi characters: "
                f"{filename!r}"
            )
        if filename != filename.strip():
            raise ValueError(
                f"Page.filename must not have leading or trailing "
                f"whitespace: {filename!r}"
            )

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    @property
    def page_number(self) -> int:
        """Return the **1-based** page number for display.

        Equivalent to ``index + 1``. Provided as a named property so that
        UI code reads naturally (``f"Page {page.page_number}"``) and so
        that off-by-one calculations are not duplicated at call sites.
        """
        return self.index + 1

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the page.

        Rendered for the ``GET /api/v1/chapters/{id}/pages`` endpoint.
        The mapping includes both the 0-based :attr:`index` (matches the
        internal model, the ``ComicInfo.Pages`` schema, and Mihon's
        reader) and the 1-based :attr:`page_number` (matches user-facing
        display and Kohana-style URLs), so clients can pick the one they
        need without doing arithmetic.

        :attr:`filename` is deliberately **not** included: it is an
        internal detail of the worker's packaging pipeline, and exposing
        it would leak a coupling that has no benefit for API consumers.
        A web reader fetches images by URL, not by on-disk filename.

        Returns:
            A dict with ``index``, ``page_number``, ``url``, ``width``,
            and ``height``. ``width`` and ``height`` are ``None`` when
            the parser did not provide them.
        """
        return {
            "index": self.index,
            "page_number": self.page_number,
            "url": self.url,
            "width": self.width,
            "height": self.height,
        }
