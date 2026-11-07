"""URL utilities for parsing, normalizing, joining, and validating URLs.

Every part of NexusDL that deals with source-site URLs uses ``urlsplit``
from the standard library — either directly or, more often, by calling a
caller-specific combination of ``urlsplit`` calls, ``Path(...).suffix``
extractions, and ad-hoc ``.lower()`` normalizations. That pattern was
fine while the pipeline was small; it stops being fine as soon as two
call sites compute the same thing slightly differently and produce two
different results for what should be the same URL.

This module centralises that logic behind a **small, focused API**. Every
function here exists because a call site in the delivered code either
already needs it or is about to (a parser that needs to build absolute
URLs from relative ones, a cache that needs a canonical key, an image
downloader that needs to check a scheme).

Guiding principles
------------------
* **Pure and synchronous.** No I/O, no logging, no async, no external
  dependencies. Every function is a pure transformation from ``str`` to
  ``str`` / ``bool`` / ``int``. This is what makes the module importable
  from anywhere, including tests, scripts, and CLI helpers that have no
  business pulling in httpx or pydantic.
* **Lenient accessors, strict mutators.** Accessors (``get_scheme``,
  ``get_host``, …) return a safe default (``""`` / ``None``) for
  malformed input — a caller inspecting a URL never has to guard against
  ``ValueError``. Mutators (``normalize``, ``join``, ``ensure_https``)
  raise :class:`ValueError` for invalid input, because silently returning
  a broken URL would hide a parser bug at the worst possible moment.
* **No scheme restriction in the accessors.** ``get_scheme`` returns the
  scheme regardless of which schemes NexusDL supports. Scheme *support*
  is a policy decision, expressed separately in
  :func:`is_fetchable` / :func:`is_supported_scheme`.
* **Case rules follow RFC 3986.** Scheme and host are lowercased; path,
  query, and fragment are preserved verbatim.

What this module deliberately does **not** do
---------------------------------------------
* **No IDNA / punycode conversion.** A URL with a Unicode hostname is
  returned verbatim. If a site requires IDNA encoding, the caller (or
  httpx, which performs it automatically) handles it.
* **No percent-encoding normalization.** ``%2F`` and ``/`` are different
  in a URL path; re-encoding one into the other silently corrupts URLs.
* **No query-string sorting** unless the caller asks for it via
  ``normalize(url, sort_query=True)``. Query order can matter (rarely,
  but the risk is asymmetric — sorting is safe to opt into, dangerous to
  apply by default).
* **No registrable-domain extraction.** That requires a Public Suffix
  List; the caller that needs it imports ``publicsuffix2`` or
  equivalent. This module provides ``get_host``, which is enough for
  every call site in the codebase.

Dependency graph (no cycles)::

    urllib.parse, typing (stdlib)

This module is a **leaf**: it imports nothing from ``nexusdl`` and
nothing from third parties. It could be lifted into a standalone utility
library without modification.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

__all__ = [
    "DEFAULT_PORTS",
    "HTTP_SCHEMES",
    "SUPPORTED_SCHEMES",
    "ensure_https",
    "get_extension",
    "get_filename",
    "get_fragment",
    "get_host",
    "get_path",
    "get_port",
    "get_query",
    "get_scheme",
    "get_stem",
    "is_absolute",
    "is_fetchable",
    "is_file",
    "is_http",
    "is_https",
    "is_relative",
    "is_same_origin",
    "is_supported_scheme",
    "join",
    "normalize",
    "strip_fragment",
    "strip_query",
    "strip_userinfo",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Schemes that can be fetched over the network by NexusDL. Used by
#: parsers, the image downloader, and the HTTP session to validate that a
#: URL is one they can actually issue a request against.
HTTP_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Schemes that a :class:`~nexusdl.core.models.manga.Manga` or
#: :class:`~nexusdl.core.models.chapter.Chapter` URL may legitimately
#: use. ``file://`` is allowed because locally-scanned archives produce
#: it via :meth:`pathlib.Path.as_uri`; the site-based models accept it
#: for that reason (see the model docstrings).
SUPPORTED_SCHEMES: Final[frozenset[str]] = frozenset(
    {"http", "https", "file"}
)

#: Default port for each scheme. Used by :func:`is_same_origin` to treat
#: ``https://a`` and ``https://a:443`` as the same origin (they are) and
#: by :func:`normalize` to strip redundant default ports.
DEFAULT_PORTS: Final[MappingProxyType[str, int]] = MappingProxyType(
    {"http": 80, "https": 443}
)


# --------------------------------------------------------------------------- #
# Typed accessors                                                             #
# --------------------------------------------------------------------------- #


def _parse(url: str) -> SplitResult:
    """Return ``urlsplit(url)``, mapping any ``ValueError`` to an empty result.

    ``urlsplit`` is lenient — it accepts almost anything and returns
    whatever it could parse — but it raises :class:`ValueError` for
    malformed IPv6 bracket syntax (``http://[::1``) at parse time.

    **Sub-property exceptions are not caught here.** In particular,
    accessing ``parts.port`` on an otherwise-valid URL can raise
    :class:`ValueError` if the port is not numeric (``http://a:notaport``)
    or exceeds 65535. That access is **not** performed by ``urlsplit``
    itself — it happens when a caller reads the ``.port`` attribute.
    Each accessor that touches ``.port`` (``get_port``,
    ``normalize``, ``strip_userinfo``) has its own ``try/except`` for
    that case; the ``except ValueError`` here only covers errors thrown
    by ``urlsplit`` during parsing.
    """
    if not url:
        return SplitResult("", "", "", "", "")
    try:
        return urlsplit(url)
    except ValueError:
        return SplitResult("", "", "", "", "")


def get_scheme(url: str) -> str:
    """Return the URL's scheme, lowercased.

    Args:
        url: The URL to inspect.

    Returns:
        The scheme (``"https"``, ``"file"``, …) or ``""`` when the URL
        has no scheme (a relative URL) or is empty.
    """
    return _parse(url).scheme.lower()


def get_host(url: str) -> str:
    """Return the URL's hostname, lowercased.

    Userinfo (``user:pass@``) and port are stripped. For ``file://``
    URLs the result is always ``""`` — ``file:///path`` has no host.

    Args:
        url: The URL to inspect.

    Returns:
        The hostname (``"mangadex.org"``) or ``""`` when the URL has no
        host.
    """
    try:
        host = _parse(url).hostname
    except ValueError:
        return ""
    return (host or "").lower()


def get_port(url: str) -> int | None:
    """Return the URL's port as an integer, or ``None``.

    Args:
        url: The URL to inspect.

    Returns:
        The port (``443``, ``8080``) when explicitly present in the URL,
        ``None`` when the URL does not specify a port (including for
        malformed ports like ``"http://a:notaport"`` — the
        malformed-port case is indistinguishable from "no port" for the
        purposes of every caller in this codebase).
    """
    try:
        return _parse(url).port
    except ValueError:
        return None


def get_path(url: str) -> str:
    """Return the URL's path component.

    The path is returned **verbatim** — no trailing slash is added, none
    is removed, and percent-escapes are not decoded.

    Args:
        url: The URL to inspect.

    Returns:
        The path (``"/manga/xyz"``), or ``""`` when the URL has no path.
    """
    return _parse(url).path


def get_query(url: str) -> str:
    """Return the URL's query string, without the leading ``?``.

    Args:
        url: The URL to inspect.

    Returns:
        The query (``"page=2&sort=new"``), or ``""`` when the URL has no
        query.
    """
    return _parse(url).query


def get_fragment(url: str) -> str:
    """Return the URL's fragment, without the leading ``#``.

    Args:
        url: The URL to inspect.

    Returns:
        The fragment (``"section-2"``), or ``""`` when the URL has no
        fragment.
    """
    return _parse(url).fragment


def get_filename(url: str) -> str:
    """Return the last path segment of the URL, if any.

    A URL whose path is empty or ends with ``/`` represents a directory,
    not a file — the result is ``""`` for both.

    Args:
        url: The URL to inspect.

    Returns:
        The filename with its extension (``"page.jpg"``), or ``""`` for
        a directory URL or a URL with no path.
    """
    path = get_path(url)
    if not path or path.endswith("/"):
        return ""
    return path.rsplit("/", 1)[-1]


def get_extension(url: str) -> str:
    """Return the file extension of the URL's last path segment.

    Matches :attr:`pathlib.PurePosixPath.suffix` semantics: the leading
    dot is included, only the **last** suffix is returned (so
    ``"c.tar.gz"`` yields ``".gz"``), and hidden files (``".bashrc"``)
    have no extension.

    Args:
        url: The URL to inspect.

    Returns:
        The extension including the leading dot (``".jpg"``, lowercased),
        or ``""`` when there is none.
    """
    filename = get_filename(url)
    if not filename:
        return ""
    dot = filename.rfind(".")
    if dot <= 0:
        return ""
    return filename[dot:].lower()


def get_stem(url: str) -> str:
    """Return the URL's filename without its extension.

    The counterpart to :func:`get_extension`. ``"page.jpg"`` yields
    ``"page"``; ``"c.tar.gz"`` yields ``"c.tar"`` (last suffix removed);
    ``".bashrc"`` yields ``".bashrc"`` (no extension to remove).

    Args:
        url: The URL to inspect.

    Returns:
        The stem, or ``""`` when the URL has no filename.
    """
    filename = get_filename(url)
    if not filename:
        return ""
    dot = filename.rfind(".")
    if dot <= 0:
        return filename
    return filename[:dot]


# --------------------------------------------------------------------------- #
# Scheme predicates                                                           #
# --------------------------------------------------------------------------- #


def is_http(url: str) -> bool:
    """Return ``True`` iff the URL uses the ``http`` scheme (case-insensitive)."""
    return get_scheme(url) == "http"


def is_https(url: str) -> bool:
    """Return ``True`` iff the URL uses the ``https`` scheme (case-insensitive)."""
    return get_scheme(url) == "https"


def is_file(url: str) -> bool:
    """Return ``True`` iff the URL uses the ``file`` scheme (case-insensitive)."""
    return get_scheme(url) == "file"


def is_fetchable(url: str) -> bool:
    """Return ``True`` iff the URL can be issued as an HTTP request.

    Equivalent to ``get_scheme(url) in HTTP_SCHEMES``. Used by the image
    downloader and by any code that must reject ``file://`` before
    passing a URL to :class:`~nexusdl.core.session.http_session.HttpSession`.

    Args:
        url: The URL to check.

    Returns:
        ``True`` for ``http://`` and ``https://`` URLs, ``False``
        otherwise (including for empty URLs).
    """
    return get_scheme(url) in HTTP_SCHEMES


def is_supported_scheme(url: str) -> bool:
    """Return ``True`` iff the URL uses a scheme NexusDL supports.

    Wider than :func:`is_fetchable`: ``file://`` URLs are supported by
    the domain models (for locally-scanned archives) even though they
    cannot be fetched over the network.

    Args:
        url: The URL to check.

    Returns:
        ``True`` for ``http://``, ``https://``, and ``file://`` URLs,
        ``False`` otherwise.
    """
    return get_scheme(url) in SUPPORTED_SCHEMES


# --------------------------------------------------------------------------- #
# Absolute/relative predicates                                                #
# --------------------------------------------------------------------------- #


def is_absolute(url: str) -> bool:
    """Return ``True`` iff the URL is absolute.

    A URL is absolute when it has a scheme **and**, for schemes that
    require one, a host:

    * ``https://mangadex.org/manga/xyz`` — absolute.
    * ``file:///tmp/archive.cbz`` — absolute (``file`` scheme does not
      require a host).
    * ``/manga/xyz`` — relative.
    * ``//mangadex.org/manga/xyz`` — relative (protocol-relative, no
      scheme).
    * ``""`` — relative.

    Args:
        url: The URL to check.
    """
    scheme = get_scheme(url)
    if not scheme:
        return False
    if scheme == "file":
        return True
    return bool(get_host(url))


def is_relative(url: str) -> bool:
    """Return ``True`` iff :func:`is_absolute` returns ``False``."""
    return not is_absolute(url)


def is_same_origin(a: str, b: str) -> bool:
    """Return ``True`` iff two URLs share the same origin.

    Origin is ``(scheme, host, port)`` per RFC 6454, with default ports
    treated as equivalent to their absence (``https://a`` and
    ``https://a:443`` have the same origin). Both URLs must be absolute
    for the comparison to be meaningful — comparing two relative URLs
    returns ``False``, as does comparing an absolute URL with a relative
    one.

    Args:
        a: The first URL.
        b: The second URL.

    Returns:
        ``True`` iff the two URLs have the same scheme, host, and
        effective port.
    """
    if not is_absolute(a) or not is_absolute(b):
        return False
    scheme_a = get_scheme(a)
    scheme_b = get_scheme(b)
    if scheme_a != scheme_b:
        return False
    if get_host(a) != get_host(b):
        return False
    port_a = get_port(a)
    port_b = get_port(b)
    default_a = DEFAULT_PORTS.get(scheme_a)
    default_b = DEFAULT_PORTS.get(scheme_b)
    return (port_a if port_a is not None else default_a) == (
        port_b if port_b is not None else default_b
    )


# --------------------------------------------------------------------------- #
# Stripping helpers                                                           #
# --------------------------------------------------------------------------- #


def strip_fragment(url: str) -> str:
    """Return *url* without its fragment.

    Args:
        url: The URL.

    Returns:
        The URL with everything from the first ``#`` removed. An input
        that has no fragment is returned unchanged.
    """
    if not url:
        return url
    parts = _parse(url)
    if not parts.fragment:
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "")
    )


def strip_query(url: str) -> str:
    """Return *url* without its query string.

    Args:
        url: The URL.

    Returns:
        The URL with everything from the first ``?`` up to (but not
        including) the fragment removed. An input that has no query is
        returned unchanged.
    """
    if not url:
        return url
    parts = _parse(url)
    if not parts.query:
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "", parts.fragment)
    )


def strip_userinfo(url: str) -> str:
    """Return *url* without its ``user:password@`` component.

    Used before logging or persisting a URL that might embed credentials.
    The rest of the URL (scheme, host, port, path, query, fragment) is
    preserved verbatim.

    Args:
        url: The URL.

    Returns:
        The URL without userinfo. An input that had no userinfo is
        returned unchanged. A URL whose port is malformed is also
        returned unchanged — the helper is a defensive primitive and
        must never crash on input it cannot safely rebuild.
    """
    if not url:
        return url
    parts = _parse(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    # ``parts.port`` can raise ``ValueError`` on a malformed port
    # (``http://a:notaport``). We never want to crash a redaction
    # helper — the caller's intent is "strip credentials if any", not
    # "validate the port". If we cannot read it, rebuild without it.
    try:
        raw_port = parts.port
    except ValueError:
        raw_port = None
    port = f":{raw_port}" if raw_port is not None else ""
    return urlunsplit(
        (
            parts.scheme,
            f"{host}{port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


# --------------------------------------------------------------------------- #
# Normalization                                                               #
# --------------------------------------------------------------------------- #


def normalize(
    url: str,
    *,
    sort_query: bool = False,
    strip_fragment: bool = True,
) -> str:
    """Return the canonical form of *url*.

    The normalization rules:

    * Scheme is lowercased.
    * Hostname is lowercased.
    * A port equal to the scheme's default port is removed
      (``https://a:443`` becomes ``https://a``).
    * An empty path becomes ``"/"`` — matching how browsers and HTTP
      servers treat ``https://a`` and ``https://a/`` as equivalent.
    * The path's trailing slash is preserved (``/a/b`` and ``/a/b/`` are
      different URLs and are left distinct).
    * Userinfo is preserved (use :func:`strip_userinfo` first when the
      caller wants credentials excluded from the canonical form).
    * The query string is preserved verbatim unless ``sort_query=True``,
      in which case the ``key=value`` pairs are sorted lexicographically.
      Sorting is safe for deduplication keys; it is not safe for any URL
      whose consumer actually inspects the query order (this is rare, but
      it is why the default is ``False``).
    * The fragment is stripped unless ``strip_fragment=False``. Fragments
      are client-side anchors and do not affect the resource that the
      server returns; keeping them in a canonical form would falsely
      distinguish ``/page#section-1`` from ``/page#section-2``.

    The function **raises** on input that cannot be normalized: an empty
    string, or a URL that lacks a scheme. This is deliberate — a caller
    reaching :func:`normalize` has already established that it holds an
    absolute URL, and silently returning a broken string would hide the
    bug at a call site far from where it was made.

    Args:
        url: The URL to normalize.
        sort_query: When ``True``, sort the query parameters
            lexicographically. Defaults to ``False``.
        strip_fragment: When ``True`` (the default), drop the fragment.

    Returns:
        The normalized URL string.

    Raises:
        ValueError: If *url* is empty or does not have a scheme.

    Examples:
        >>> normalize("HTTPS://MangaDex.ORG")
        'https://mangadex.org/'
        >>> normalize("https://a:443/x?b=2&a=1", sort_query=True)
        'https://a/x?a=1&b=2'
        >>> normalize("https://a/x#section")
        'https://a/x'
    """
    if not url:
        raise ValueError("normalize() requires a non-empty URL")

    parts = _parse(url)
    scheme = parts.scheme.lower()
    if not scheme:
        raise ValueError(
            f"normalize() requires an absolute URL, got {url!r}"
        )

    # Rebuild netloc with userinfo, lowercase host, and default-port
    # stripping. ``parts.port`` may raise ValueError for a malformed
    # port; treat that as "no port" — every caller of normalize() has
    # already decided the URL is worth keeping, and a malformed port
    # would have been caught by validation upstream.
    try:
        raw_port = parts.port
    except ValueError:
        raw_port = None

    host = (parts.hostname or "").lower()

    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"

    default_port = DEFAULT_PORTS.get(scheme)
    if raw_port is None or raw_port == default_port:
        port_str = ""
    else:
        port_str = f":{raw_port}"

    netloc = f"{userinfo}{host}{port_str}"

    path = parts.path or "/"

    query = parts.query
    if sort_query and query:
        query = _sort_query(query)

    fragment = "" if strip_fragment else parts.fragment

    return urlunsplit((scheme, netloc, path, query, fragment))


def _sort_query(query: str) -> str:
    """Sort a raw query string lexicographically by parameter name.

    Parameters without ``=`` (a bare token like ``?debug``) are sorted
    with the same key (the token itself). Parameters with the same name
    keep their original relative order (Python's sort is stable).

    Args:
        query: The raw query string, without the leading ``?``.

    Returns:
        The sorted query string, without the leading ``?``. Empty input
        yields an empty result.
    """
    if not query:
        return ""
    pairs = query.split("&")
    # Sort by the parameter name only — the value does not affect order,
    # and using the whole ``name=value`` string would sort by value
    # within same-name parameters, which no caller wants.
    pairs.sort(key=lambda p: p.split("=", 1)[0])
    return "&".join(pairs)


# --------------------------------------------------------------------------- #
# Joining                                                                     #
# --------------------------------------------------------------------------- #


def join(base: str, ref: str) -> str:
    """Resolve *ref* against *base* and return the normalized result.

    Uses :func:`urllib.parse.urljoin` under the hood — the RFC 3986
    reference resolver — and applies :func:`normalize` to the result
    with the default options (fragment stripped, query preserved).

    The stdlib's ``urljoin`` semantics are preserved (this is important;
    parsers expect the standard behaviour):

    * ``join("https://a/b/c", "../d")`` → ``"https://a/d"``
    * ``join("https://a/b/c", "d")`` → ``"https://a/b/d"``
    * ``join("https://a/b/", "d")`` → ``"https://a/b/d"``
    * ``join("https://a/b", "d")`` → ``"https://a/d"`` — because ``b``
      has no trailing slash and is therefore treated as a "file"
    * ``join("https://a/b", "/c")`` → ``"https://a/c"``
    * ``join("https://a/b", "https://c/d")`` → ``"https://c/d"``

    A caller that wants directory semantics should ensure the base ends
    with ``/``: ``join(f"{base_url.rstrip('/')}/", "chapter/1")``.

    Args:
        base: The base URL. Must be absolute (has a scheme) — a relative
            base cannot be the anchor of a join.
        ref: The reference to resolve. May be empty, relative, absolute,
            protocol-relative (``//host/path``), or a bare fragment
            (``#section``).

    Returns:
        The resolved, normalized URL.

    Raises:
        ValueError: If *base* is empty, or does not have a scheme.
            *ref* is not validated — ``urljoin`` handles every well-formed
            reference and treats a malformed one as a literal path.
    """
    if not base:
        raise ValueError("join() requires a non-empty base URL")
    if not get_scheme(base):
        raise ValueError(
            f"join() requires an absolute base URL, got {base!r}"
        )

    # Import lazily to keep the module's import cost negligible when only
    # the cheap accessors are used. ``urljoin`` lives in the same
    # standard-library module as ``urlsplit`` and ``urlunsplit``, so the
    # runtime cost of the import is zero; the "lazy" here is stylistic,
    # keeping the top-level imports focused on the two functions that
    # every public symbol relies on.
    from urllib.parse import urljoin as _urljoin

    if not ref:
        return normalize(base)
    return normalize(_urljoin(base, ref))


# --------------------------------------------------------------------------- #
# Scheme upgrades                                                             #
# --------------------------------------------------------------------------- #


def ensure_https(url: str) -> str:
    """Return an HTTPS form of *url*, or the URL unchanged if already local.

    Three behaviours, one per scheme category:

    * ``https://`` — returned normalized, unchanged otherwise.
    * ``http://`` — **upgraded**: scheme becomes ``https``; any explicit
      port is dropped (it was the HTTP port; the HTTPS port is almost
      certainly ``443``). Userinfo, path, and query are preserved.
    * ``file://`` — returned normalized, unchanged otherwise. The caller
      treats local URLs as-is; NexusDL's security invariant is "every
      URL that goes over the network uses HTTPS", and a ``file://`` URL
      does not go over the network.

    Any other scheme — ``ftp://``, ``data:``, ``javascript:``, and
    anything else — raises :class:`ValueError`. A caller that reaches
    :func:`ensure_https` is asserting "this URL is supposed to be safe to
    fetch"; a scheme that cannot be upgraded is a bug at the call site,
    not a condition to handle.

    Args:
        url: The URL to upgrade.

    Returns:
        The upgraded (or unchanged) normalized URL.

    Raises:
        ValueError: If *url* is empty, has no scheme, or uses a scheme
            other than ``http``, ``https``, or ``file``.

    Examples:
        >>> ensure_https("http://mangadex.org/manga/xyz")
        'https://mangadex.org/manga/xyz'
        >>> ensure_https("https://mangadex.org/manga/xyz")
        'https://mangadex.org/manga/xyz'
        >>> ensure_https("file:///tmp/archive.cbz")
        'file:///tmp/archive.cbz'
    """
    if not url:
        raise ValueError("ensure_https() requires a non-empty URL")

    scheme = get_scheme(url)
    if not scheme:
        raise ValueError(
            f"ensure_https() requires an absolute URL, got {url!r}"
        )
    if scheme == "https":
        return normalize(url)
    if scheme == "file":
        return normalize(url)
    if scheme == "http":
        parts = _parse(url)
        host = (parts.hostname or "").lower()
        userinfo = ""
        if parts.username is not None:
            userinfo = parts.username
            if parts.password is not None:
                userinfo += f":{parts.password}"
            userinfo += "@"
        # Drop the port — it was the HTTP port, not the HTTPS one.
        path = parts.path or "/"
        return normalize(
            urlunsplit(
                ("https", f"{userinfo}{host}", path, parts.query, "")
            )
        )
    raise ValueError(
        f"ensure_https() cannot upgrade scheme {scheme!r} in {url!r}"
    )
