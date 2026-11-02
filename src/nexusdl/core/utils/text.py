"""Canonical text utilities for the entire NexusDL pipeline.

Every layer of NexusDL manipulates text: parsers clean scraped HTML,
the library decodes descriptions from ComicInfo, the cache hashes
canonical strings, the CLI truncates long titles for table output, the
env-var parser normalises cookie names, and the worker builds safe
filenames via slugification. Historically each call site did its own
``.strip().lower()``, its own naive comma-split, its own truncation
loop — with two inevitable consequences:

* Two call sites produce slightly different results for the same input.
* A subtle bug (a description containing a stray ``<br>``, a chapter
  number "5.5" parsed as ``5``, an env-var name with mixed case never
  matched) surfaces only when the code path is first exercised.

This module centralises the pure-text transformations the pipeline
needs, behind a small, documented, testable API.

Guiding principles
------------------
* **Pure and synchronous.** No I/O, no logging, no async. Every function
  is a transformation from ``str`` (or ``str | None``) to a scalar. This
  is what makes the module importable from anywhere — parsers,
  migrations, CLI helpers, tests — without pulling in httpx or
  pydantic.
* **Deterministic.** Two calls with the same input return the same
  output. No environment-dependent behaviour (no locale-sensitive case
  mapping, no filesystem path assumptions).
* **Lenient parsing, strict transformation.** :func:`parse_bool` and
  :func:`extract_number` return ``None`` / a default when the input is
  unrecognised — a caller scraping "unknown" from a page should not
  crash. :func:`truncate` and :func:`split_csv` raise on misuse
  (negative limits) — a broken caller is a bug to fix, not a condition
  to silently absorb.
* **Unicode-first.** Case operations use :meth:`str.casefold` (not
  :meth:`str.lower`), truncation counts codepoints (not bytes), and
  :func:`strip_html` decodes HTML entities. A description containing
  ``"Café"`` or ``"日本語"`` comes out intact.

What this module deliberately does **not** do
---------------------------------------------
* **No slug generation from scratch.** Slugs are inherently
  locale-and-policy dependent (``"C++"`` → ``"c"`` or ``"cpp"``?).
  :func:`slugify_safe` wraps ``python-slugify`` — the same library the
  worker and manager already use — with a guaranteed-fallback wrapper.
  Reinventing slugification is a rabbit hole.
* **No full HTML parser.** :func:`strip_html` uses a regex to remove
  tags after converting block elements to whitespace. It is
  intentionally **not** a sanitizer and **not** a replacement for
  :mod:`html.parser` on attacker-controlled input. The target use case
  is scraping-tool output (descriptions, summaries) where the HTML is
  well-formed and the caller wants plain text, not safety guarantees.
* **No fuzzy matching / Levenshtein / normalisation for comparison.**
  That is a content-moderation concern, not a text utility. Callers
  that need it use a dedicated library.
* **No translation / i18n.** ``i18n`` lives in ``core/i18n.py`` and
  consumes translations from ``core/data/translations/``.
* **No number formatting** (thousands separators, currency). Locale-
  dependent, belongs in the presentation layer.
* **No case-preserving replacement.** No ``replace_all``; the stdlib
  ``str.replace`` covers every use case.

Migration notes
---------------
* ``comic_info.py::_split_csv`` — replace with :func:`split_csv`. The
  behaviour is identical for the common case (``"a, b"``) and **more
  correct** for the edge case (``'"a, b", c'`` → ``("a, b", "c")``,
  where the current implementation produces three items including a
  stray quote). The refactor should be applied when ``comic_info.py`` is
  next opened.
* ``database.py::_decode_json_array`` and ``_encode_json_array`` — these
  are JSON operations, not text; no substitution here. Left as-is.
* Any inline ``x.strip().lower()`` used for case-insensitive comparison
  — replace with :func:`casefold_key` for correct Unicode folding.

Dependency graph (no cycles)::

    python-slugify  ──► core.utils.text
    csv, html, re, unicodedata (stdlib)

This module is a **leaf** on the project side: it imports no other
``nexusdl`` module. ``python-slugify`` is already a runtime dependency
(used by ``worker.py`` and ``manager.py``) — the import is not a new
requirement.
"""

from __future__ import annotations

import csv
import html
import io
import re
from typing import Any, Final, TypeVar

from slugify import slugify as _slugify


__all__ = [
    "casefold_key",
    "coerce_str",
    "extract_number",
    "normalize_whitespace",
    "parse_bool",
    "slugify_safe",
    "split_csv",
    "strip_html",
    "truncate",
    "truncate_words",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Marker appended by :func:`truncate` / :func:`truncate_words` when the
#: input exceeded the length limit. The single-character ellipsis is
#: preferred over ``"..."`` because it counts as one character in
#: codepoint-based truncation — the output's length matches the caller's
#: ``max_length`` regardless of whether truncation occurred.
DEFAULT_TRUNCATION_SUFFIX: Final[str] = "…"

#: Fallback slug returned by :func:`slugify_safe` when the input slugs to
#: the empty string (an all-punctuation title such as ``"!!!"``, an
#: all-emoji title such as ``"🎉🎉"``, an empty string). Chosen so that a
#: filename built from the slug is never empty — which would produce an
#: unnamed file or a directory-named file on some filesystems.
DEFAULT_SLUG_FALLBACK: Final[str] = "untitled"

#: Default maximum length used by :func:`slugify_safe`. Long enough for a
#: readable filename on every filesystem (ext4: 255, NTFS: 255, APFS:
#: 255 — this leaves headroom for extensions and chapter numbers).
DEFAULT_SLUG_MAX_LENGTH: Final[int] = 100

#: Strings that :func:`parse_bool` accepts as ``True``. Case-insensitive;
#: covers the values emitted by Python's ``str(bool)`` (``"True"`` /
#: ``"False"``), by shell conventions (``"1"`` / ``"0"``, ``"yes"`` /
#: ``"no"``), by YAML 1.1 (``"on"`` / ``"off"``), and by HTTP conventions
#: (``"y"`` / ``"n"``). The union is deliberately larger than any single
#: standard, because :func:`parse_bool` is called on inputs from
#: heterogeneous sources.
_TRUE_STRINGS: Final[frozenset[str]] = frozenset(
    {"1", "true", "yes", "on", "y", "t"}
)

#: Strings that :func:`parse_bool` accepts as ``False``.
_FALSE_STRINGS: Final[frozenset[str]] = frozenset(
    {"0", "false", "no", "off", "n", "f"}
)

#: Block-level HTML tags whose closing tag should become whitespace, so
#: that ``"<p>a</p><p>b</p>"`` becomes ``"a b"`` and not ``"ab"``.
#: ``<br>`` is handled separately because it has no closing tag.
_BLOCK_CLOSING_TAGS: Final[re.Pattern[str]] = re.compile(
    r"</(?:p|div|li|tr|h[1-6]|br|hr|blockquote|pre|section|article|aside|"
    r"header|footer|nav|table|thead|tbody|tfoot|ul|ol|dl|dt|dd)\s*>",
    re.IGNORECASE,
)

#: ``<br>`` / ``<br/>`` / ``<br />`` — a line break that should become
#: whitespace so that multi-line content does not concatenate words.
_BR_TAGS: Final[re.Pattern[str]] = re.compile(
    r"<br\s*/?>", re.IGNORECASE
)

#: Any remaining HTML tag. Matches the common shapes ``<tag>``,
#: ``<tag attr="value">``, ``<tag/>``, and ``</tag>``. The regex is
#: deliberately non-greedy and does not handle pathological inputs (nested
#: ``<`` inside an attribute value) — see the module docstring on why
#: :func:`strip_html` is not a sanitizer.
_HTML_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")

#: Any run of Unicode whitespace. Used by :func:`normalize_whitespace`.
#: ``\s`` in Python's ``re`` matches ``[ \t\n\r\f\v]`` plus any character
#: whose Unicode category is ``Zs`` (space separator) — so a non-breaking
#: space, an ideographic space, and a zero-width space (``\u200b`` — not
#: whitespace in Unicode, so NOT matched) are all handled consistently.
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")

#: The first integer or decimal number in a string. Handles an optional
#: leading minus sign, an integer part, and an optional fractional part.
#: Deliberately **does not** match scientific notation (``"1e5"``) or
#: leading ``+`` — the pipeline's number extraction is for chapter
#: numbers (``"Chapter 5.5"``, ``"Vol. 2 Ch. 10"``) where those shapes do
#: not occur.
_NUMBER: Final[re.Pattern[str]] = re.compile(r"-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# Coercion                                                                    #
# --------------------------------------------------------------------------- #

#: Type variable used by :func:`coerce_str` so that the caller's intent
#: (a specific string type, in principle) is preserved when no coercion
#: is needed. Kept generic for future extension; today ``coerce_str``
#: always returns a plain ``str``.
_T = TypeVar("_T", bound=str)


def coerce_str(value: Any, *, default: str = "") -> str:
    """Coerce any value to a :class:`str`, never raising.

    Used wherever the caller receives a value of unknown type from an
    upstream source (a JSON payload, a scraped HTML attribute, a
    configuration mapping) and needs a string to pass onward. The
    behaviour depends on the input's type:

    * ``None`` → *default*.
    * :class:`str` → returned verbatim.
    * :class:`bytes` → decoded as UTF-8 with replacement on invalid
      sequences (a mangled character is friendlier than a raised
      :class:`UnicodeDecodeError` for a field that is cosmetic).
    * Anything else → ``str(value)``.

    Args:
        value: The value to coerce.
        default: The value to return when *value* is ``None`` or cannot
            be decoded. Defaults to the empty string. A caller that
            needs a visible placeholder passes ``default="unknown"``
            (or equivalent) explicitly.

    Returns:
        A string. Never ``None``.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — coercion must never raise
        # ``str(obj)`` calls ``obj.__str__`` which is user code and can
        # raise anything. A broken ``__str__`` is a bug in that object,
        # not in the caller of coerce_str — return the default rather
        # than propagating.
        return default


def casefold_key(text: str) -> str:
    """Normalise *text* for case-insensitive comparison.

    Trims leading and trailing whitespace, then applies
    :meth:`str.casefold` — the Unicode-aware case folding algorithm,
    which unlike :meth:`str.lower` handles case mappings that expand
    (``"ẞ"`` → ``"ss"``) and case mappings that are context-sensitive
    (Greek final sigma). The result is suitable as a dict key or as the
    operand of an equality comparison when case must be ignored.

    This is the **only** place in the codebase that should perform
    case-insensitive normalisation. A call site writing
    ``x.strip().lower()`` is producing subtly wrong behaviour for the
    handful of scripts where ``lower`` and ``casefold`` differ, and that
    difference is invisible until a Turkish or Greek user reports a bug.

    Args:
        text: The string to fold.

    Returns:
        The folded string. Empty when *text* is empty or whitespace-only.
    """
    return text.strip().casefold()


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def parse_bool(
    value: Any, *, default: bool | None = None
) -> bool | None:
    """Parse a string representation of a boolean, tolerantly.

    Accepts the union of truthy/falsy spellings used by Python,
    shells, YAML, and HTTP: ``"true"`` / ``"false"`` (any case),
    ``"1"`` / ``"0"``, ``"yes"`` / ``"no"``, ``"on"`` / ``"off"``,
    ``"y"`` / ``"n"``, ``"t"`` / ``"f"``. Non-string inputs
    (``None``, ints, bools, arbitrary objects) are handled as follows:

    * ``None`` → *default*.
    * :class:`bool` → returned as-is.
    * :class:`int` → ``True`` for non-zero, ``False`` for zero.
    * Anything else → :func:`str`-coerced and re-parsed; unrecognised
      values yield *default*.

    The function never raises. This is intentional — it is called on
    values from env vars, YAML fragments, and HTTP headers, all of which
    are heterogeneous and none of which is under the caller's control.

    Args:
        value: The value to parse.
        default: The value to return for unrecognised input. Defaults to
            ``None`` so the caller can distinguish "definitely not a
            boolean" from "explicitly ``False``".

    Returns:
        ``True``, ``False``, or *default*.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0

    if not isinstance(value, str):
        try:
            text = str(value)
        except Exception:  # noqa: BLE001 — same rationale as coerce_str
            return default
    else:
        text = value

    normalized = text.strip().casefold()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    return default


def extract_number(text: str | None) -> int | float | None:
    """Extract the first number in *text*, or ``None``.

    Recognises integers (``"5"``) and decimals (``"5.5"``), with an
    optional leading minus sign. Does **not** recognise scientific
    notation, an explicit ``+`` sign, or thousands separators — those
    shapes do not appear in the pipeline's primary use case (extracting
    chapter numbers from scraped text like ``"Chapter 5.5"`` or
    ``"Vol. 2 Ch. 10"``).

    The return type preserves the integer / float distinction so that
    ``"Chapter 5"`` and ``"Chapter 5.0"`` are distinguishable by the
    caller — the first yields ``5`` (``int``), the second ``5.0``
    (``float``). Most callers will treat them identically; a caller that
    wants to display the exact source form keeps both.

    Args:
        text: The string to search. ``None`` and the empty string yield
            ``None``.

    Returns:
        The first number in *text* as ``int`` (for an integer match) or
        ``float`` (for a decimal match), or ``None`` when *text* contains
        no number.
    """
    if not text:
        return None
    match = _NUMBER.search(text)
    if match is None:
        return None
    raw = match.group(0)
    if "." in raw:
        try:
            return float(raw)
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Normalisation                                                               #
# --------------------------------------------------------------------------- #


def normalize_whitespace(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip.

    Removes leading and trailing whitespace, converts internal newlines
    and tabs to single spaces, and collapses multiple consecutive
    whitespace characters to one. This is the canonical pre-comparison
    transform for text that came from HTML, YAML, or a scraped page —
    all of which produce unpredictable whitespace.

    The function is a no-op on already-normalized input (idempotent) and
    returns ``""`` for whitespace-only input.

    Args:
        text: The string to normalize.

    Returns:
        The string with all whitespace runs collapsed to single spaces
        and leading/trailing whitespace removed.
    """
    if not text:
        return ""
    return _WHITESPACE_RUN.sub(" ", text).strip()


# --------------------------------------------------------------------------- #
# Transformation                                                              #
# --------------------------------------------------------------------------- #


def strip_html(text: str) -> str:
    """Convert an HTML fragment to plain text.

    Performs four operations, in order:

    1. **Convert structural tags to whitespace.** ``<br>`` and every
       block-level closing tag (``</p>``, ``</div>``, ``</li>``, …) is
       replaced with a single space, so that paragraph breaks do not
       silently concatenate two words (``"<p>hello</p><p>world</p>"``
       becomes ``"hello world"``, not ``"helloworld"``).
    2. **Remove every remaining tag.** Inline tags (``<b>``, ``<i>``,
       ``<a href="…">``) are stripped entirely, preserving their text
       content.
    3. **Decode HTML entities.** ``&amp;`` → ``&``, ``&nbsp;`` → a
       space, ``&#8212;`` → ``—``. Uses :func:`html.unescape`.
    4. **Normalize whitespace.** Applies :func:`normalize_whitespace`
       to the result, so the output has no leading/trailing whitespace
       and no internal runs of multiple spaces.

    The function is **not** a sanitizer and should not be used on
    attacker-controlled HTML: a regex-based tag stripper cannot be
    robust against malformed input. The intended use case is
    scraping-tool descriptions and ComicInfo ``Summary`` fields — HTML
    that is well-formed and trusted.

    Args:
        text: The HTML fragment to convert.

    Returns:
        Plain text with no tags, no entities, and normalized whitespace.
        Returns the input verbatim when it is empty.
    """
    if not text:
        return text

    converted = _BR_TAGS.sub(" ", text)
    converted = _BLOCK_CLOSING_TAGS.sub(" ", converted)
    converted = _HTML_TAG.sub("", converted)
    converted = html.unescape(converted)
    return normalize_whitespace(converted)


def slugify_safe(
    text: str | None,
    *,
    max_length: int = DEFAULT_SLUG_MAX_LENGTH,
    fallback: str = DEFAULT_SLUG_FALLBACK,
) -> str:
    """Slugify *text*, guaranteeing a non-empty filename-safe result.

    Wraps :func:`slugify.slugify` — the same library ``worker.py`` and
    ``manager.py`` already use — with two guarantees the raw function
    does not provide:

    * **Never empty.** An all-punctuation or all-emoji input slugs to
      ``""`` from ``slugify``; :func:`slugify_safe` returns *fallback*
      instead. Building a filename from an empty slug would produce a
      file with no name, or a path that resolves to the parent
      directory.
    * **Never raises.** ``slugify`` itself is reliable, but the caller
      may pass a value that triggers a pathological case in its
      transliteration table. Any exception is caught and mapped to
      *fallback*.

    The output is guaranteed to be filesystem-safe: no path separators,
    no control characters, no Windows-reserved characters, no leading or
    trailing whitespace. Every caller that constructs a filename from a
    manga title or chapter title uses this function.

    Args:
        text: The source string. ``None`` returns *fallback*.
        max_length: Maximum length of the resulting slug. Must be
            ``>= 1``. ``slugify`` truncates at a character boundary and
            does not cut mid-escape. Defaults to
            :data:`DEFAULT_SLUG_MAX_LENGTH` (100).
        fallback: The string to return when *text* slugs to empty. Must
            be a valid slug (filesystem-safe, non-empty). Defaults to
            :data:`DEFAULT_SLUG_FALLBACK` (``"untitled"``).

    Returns:
        A non-empty filesystem-safe slug.

    Raises:
        ValueError: If *max_length* is less than ``1``, or if *fallback*
            is empty. Both are caller bugs that would silently produce an
            unusable filename downstream.
    """
    if max_length < 1:
        raise ValueError(
            f"max_length must be >= 1, got {max_length}"
        )
    if not fallback:
        raise ValueError("fallback must be non-empty")

    if not text:
        return fallback

    try:
        slug = _slugify(text, max_length=max_length)
    except Exception:  # noqa: BLE001 — slugify must never crash the pipeline
        return fallback

    return slug or fallback


# --------------------------------------------------------------------------- #
# Truncation                                                                  #
# --------------------------------------------------------------------------- #


def truncate(
    text: str,
    max_length: int,
    *,
    suffix: str = DEFAULT_TRUNCATION_SUFFIX,
) -> str:
    """Truncate *text* to at most *max_length* codepoints, adding a suffix.

    The suffix **counts toward** *max_length*: the returned string's
    ``len()`` is always ``<= max_length``. This is what every
    character-budget caller (a log line prefix, a UI column, a shell
    prompt) expects.

    Counting is by **codepoints**, not bytes: a string of 20 emoji is 20
    codepoints (80 UTF-8 bytes) and truncates as 20 codepoints. This
    matches Python's ``len`` and every UI framework's column computation.

    Edge cases:

    * ``len(text) <= max_length`` → the text is returned verbatim, no
      suffix appended.
    * ``max_length == 0`` → ``""`` (the suffix cannot fit).
    * ``len(suffix) > max_length`` → the suffix is itself truncated to
      *max_length* codepoints (a caller asking for ``truncate(x, 1)``
      gets the ellipsis, not an empty string, because the ellipsis is
      the honest marker of truncation).

    Args:
        text: The string to truncate.
        max_length: Maximum length of the returned string, in
            codepoints. Must be ``>= 0``.
        suffix: The marker appended after the truncated text. Defaults
            to ``"…"`` (single codepoint). Pass ``""`` to suppress the
            marker entirely.

    Returns:
        A string of length ``<= max_length``.

    Raises:
        ValueError: If *max_length* is negative.
    """
    if max_length < 0:
        raise ValueError(
            f"max_length must be >= 0, got {max_length}"
        )
    if len(text) <= max_length:
        return text
    if max_length == 0:
        return ""
    if len(suffix) >= max_length:
        return suffix[:max_length]
    return text[: max_length - len(suffix)] + suffix


def truncate_words(
    text: str,
    max_length: int,
    *,
    suffix: str = DEFAULT_TRUNCATION_SUFFIX,
) -> str:
    """Truncate *text* to *max_length* codepoints at a word boundary.

    Behaves like :func:`truncate` for the length budget and the suffix
    rules, but when truncation is needed, the cut is moved back to the
    last whitespace character that fits, so that words are not split in
    the middle. If no whitespace exists within the budget, the function
    falls back to :func:`truncate`'s behaviour (hard cut) rather than
    returning an empty string — a mid-word cut is preferable to losing
    the content entirely.

    The suffix still counts toward *max_length*; the returned string's
    ``len()`` is always ``<= max_length``. Trailing whitespace left by
    the word-boundary adjustment is stripped before the suffix is
    appended.

    Args:
        text: The string to truncate.
        max_length: Maximum length of the returned string, in
            codepoints. Must be ``>= 0``.
        suffix: The marker appended after the truncated text. Defaults
            to ``"…"``.

    Returns:
        A string of length ``<= max_length``, truncated at a word
        boundary when possible.

    Raises:
        ValueError: If *max_length* is negative.
    """
    if max_length < 0:
        raise ValueError(
            f"max_length must be >= 0, got {max_length}"
        )
    if len(text) <= max_length:
        return text
    if max_length == 0:
        return ""

    if len(suffix) >= max_length:
        return suffix[:max_length]

    budget = max_length - len(suffix)
    head = text[:budget]
    last_space = max(
        head.rfind(" "), head.rfind("\n"), head.rfind("\t")
    )
    if last_space <= 0:
        # No whitespace in the budget — hard cut, matching truncate().
        return head + suffix
    return head[:last_space].rstrip() + suffix


# --------------------------------------------------------------------------- #
# Splitting                                                                   #
# --------------------------------------------------------------------------- #


def split_csv(text: str | None) -> tuple[str, ...]:
    """Split a comma-separated value into trimmed, non-empty items.

    Handles quoted values correctly — ``'"one, two", three'`` yields
    ``("one, two", "three")``, not ``('"one', 'two"', 'three')``. This
    is the CSV-parsing behaviour of :mod:`csv`, applied to the single-
    line values that ComicInfo produces (``Genre``, ``Tags``,
    ``Characters``, ``Teams``, ``Locations`` — see
    :mod:`nexusdl.core.packaging.comic_info`).

    Each item is stripped of surrounding whitespace. Items that are
    empty after stripping are **dropped** — a caller writing
    ``"a, , b"`` gets ``("a", "b")``, not ``("a", "", "b")``. This
    matches the pipeline's expectation that a metadata list contains no
    empty elements.

    The parser is intentionally single-line: the pipeline's inputs are
    one-line CSV values, not multi-line documents. A trailing newline
    in *text* is stripped; embedded newlines inside quoted values are
    preserved as part of the value (which is correct CSV behaviour and
    the only way to represent a multi-line value in the format).

    Args:
        text: The CSV string. ``None`` and the empty string both yield
            ``()``.

    Returns:
        A tuple of trimmed, non-empty strings. Empty when *text* is
        ``None``, empty, whitespace-only, or contains only empty items.
    """
    if not text:
        return ()
    try:
        reader = csv.reader(io.StringIO(text.strip()))
        row = next(reader, None)
    except csv.Error:
        # A malformed CSV fragment (unbalanced quotes) is more usefully
        # treated as "no items" than as a raised exception — the caller
        # is processing metadata from an upstream source it does not
        # control, and the empty result is a valid fallback.
        return ()
    if row is None:
        return ()
    return tuple(
        item.strip() for item in row if item.strip()
    )
