"""Exception hierarchy for every error the pipeline raises.

Every exception NexusDL raises is a subclass of :class:`NexusDLError`. A
caller that catches this base class catches every pipeline failure —
network, parsing, packaging, database, compliance, session, plugin — with
one ``except`` clause. The subclasses exist so that a caller who wants to
react to a **specific** failure mode (a rate limit, a 404, a corrupt
archive) can do so without inspecting strings or status codes.

The hierarchy is intentionally shallow (three levels max) and enumerable
— a reader can see every exception the pipeline raises by looking at this
file. A new exception class is a deliberate addition, not an incidental
one: the codebase currently has no exception subclasses outside this
module, and it should not gain one without a corresponding update here.

The hierarchy
-------------
::

    NexusDLError (base)
    ├── ConfigError
    ├── ParserError
    │   ├── ParserNotFoundError
    │   ├── ParserResponseError
    │   └── ParserParseError
    ├── DownloadError
    │   ├── ChapterDownloadError
    │   ├── PageDownloadError
    │   └── EmptyChapterError
    ├── PackagingError
    ├── LibraryError
    ├── ComplianceError
    ├── SessionError
    │   ├── SessionTimeoutError
    │   ├── SessionRateLimitError
    │   ├── SessionCloudflareError
    │   ├── CookieEncryptionError
    │   ├── CircuitBreakerOpenError
    │   ├── RateLimitTimeoutError
    │   └── NexusDLPlaywrightError
    ├── RetryError
    │   └── RetryTimeoutError
    ├── PluginError
    ├── InvalidStateTransitionError
    ├── ImageValidationError
    └── ImageConversionError

Notes on the hierarchy relative to the PROMPT MASTER
----------------------------------------------------
Both of the following classes exist to support the PROMPT MASTER's
requirements, but neither is a silent addition — one is in the spec's
tree diagram (with a non-obvious classification rationale), and the
other is specified in prose only and deserves a note here so a reader
of the tree does not think it is a missing class:

* :class:`NexusDLPlaywrightError` — **in the PROMPT MASTER's prose,
  absent from its tree diagram**. The spec's ``PlaywrightPool`` section
  states: *"``start()`` vérifie Chromium installé (sinon
  ``NexusDLPlaywrightError``)"*. A ``SessionError`` subclass is the
  natural home (the failure is a session-layer concern), and it is
  **not** retryable by default — a Playwright that failed to launch
  with "not installed" will fail again one second later. Callers that
  want retryable Playwright failure handling wrap the launch in
  :func:`~nexusdl.core.utils.retry.retry_async` explicitly. This class
  is therefore *in the spec* but not *in the tree diagram*; adding it
  here keeps the module's tree diagram self-contained.

* :class:`RateLimitTimeoutError` — **in the PROMPT MASTER's tree
  diagram**, placed as a sibling of :class:`SessionRateLimitError`
  under :class:`SessionError`. The classification is **deliberate**
  and non-obvious: :class:`SessionRateLimitError` is "the server said
  429", retryable after backoff; :class:`RateLimitTimeoutError` is
  "our own limiter gave up after the caller-specified timeout", which
  means the caller has already waited the configured window and a
  retry would just wait again. Merging the two would confuse the retry
  classifier's intent, so they remain siblings.

Retry classification
--------------------
The download pipeline's
:func:`~nexusdl.core.downloader.retry.is_retryable` classifies exceptions
into transient (retried) and permanent (not retried). That
classification depends on this hierarchy:

**Transient** — retried by the tenacity controller:
``SessionTimeoutError``, ``SessionRateLimitError``,
``SessionCloudflareError``, ``CircuitBreakerOpenError``,
``ParserResponseError``, ``PageDownloadError``, ``ImageValidationError``.

**Permanent** — fail fast:
``ConfigError``, ``ComplianceError``, ``ParserNotFoundError``,
``ParserParseError``.

**Unclassified** — not retried by default:
``NexusDLError``, ``ParserError``, ``DownloadError``,
``ChapterDownloadError``, ``EmptyChapterError``, ``PackagingError``,
``LibraryError``, ``SessionError``, ``RateLimitTimeoutError``,
``NexusDLPlaywrightError``, ``RetryError``, ``RetryTimeoutError``,
``PluginError``, ``InvalidStateTransitionError``, ``ImageConversionError``,
``CookieEncryptionError``.

The two modules — this one and
:mod:`nexusdl.core.downloader.retry` — must stay in lock-step for the
classified entries. A future class that ought to be retried is added to
``retry.py``'s tuples, not to this file; the classification is a
**policy** of the retry layer, not a property of the exception. This file
defines what can be raised; the retry layer decides what to do about it.

Construction
------------
Every exception takes a message as its first positional argument,
matching Python's :class:`Exception` contract — so existing
``raise SessionError("message")`` calls work unchanged. Several
exceptions additionally accept **optional keyword arguments** that
surface structured context. Those fields are exposed as attributes so a
caller can inspect ``exc.site_id``, ``exc.url``, ``exc.status_code``
without parsing the message. When a caller does not supply them, the
attributes are ``None`` — matching the "best-effort context" contract
used everywhere in the pipeline.

Every exception subclass takes at most one positional argument (the
message) plus keyword-only extras. Positional extras are deliberately
not supported — the pipeline's existing call sites all use a single
positional message, and adding positional extras would invite a caller
to pass the wrong argument order in a code path that has no test for it.

Diagnostic attributes on the base
---------------------------------
:class:`NexusDLError` exposes:

* :attr:`NexusDLError.message` — the original message, as a ``str``.
  Equal to ``str(exc)`` and to ``exc.args[0]``; provided as a named
  attribute so callers that want to inspect a raised exception's
  message **without** converting it to a string have a sanctioned way
  to do so.
* :attr:`NexusDLError.context` — a read-only mapping of the **keyword
  arguments that the concrete class passed through to the base
  constructor via ``**context``**. It is **not** a dump of every field
  the exception carries: a subclass that declares a field (e.g.
  ``status_code`` on :class:`ParserResponseError`,
  ``elapsed_seconds`` on :class:`RetryTimeoutError`) exposes it as a
  named attribute and *may or may not* mirror it in ``context``
  depending on whether the subclass forwarded it to ``**context``.
  Concretely:

  - :class:`ParserError` forwards ``url`` and ``site_id`` — both appear
    in ``context`` and as attributes.
  - :class:`ParserResponseError` adds ``status_code`` as an attribute
    but does **not** forward it to ``**context`` — so
    ``exc.context['status_code']`` raises :class:`KeyError` while
    ``exc.status_code`` works.
  - :class:`NexusDLError` constructed directly with ``**context``
    exposes those kwargs in ``context`` and nowhere else.

  The mapping exists for the base constructor's ``**context``
  mechanism and for diagnostic endpoints that want a partial, JSON-
  friendly view of an exception; callers that need a **specific**
  attribute always use the attribute, never the mapping.

Pickling
--------
:class:`NexusDLError` overrides :meth:`__reduce__` and provides
:meth:`__getstate__` / :meth:`__setstate__` so that **every** slot
declared on any subclass survives a pickle round-trip. Without this
override, the base :meth:`BaseException.__reduce__` would return a
two-tuple ``(type, args)`` — every declared attribute would be dropped,
because none of them are in ``args``. Exceptions cross process
boundaries in :class:`concurrent.futures.ProcessPoolExecutor` and in
message brokers that pickle payloads; silent data loss on the error path
is exactly the bug that makes a production incident un-diagnosable.

**A note on ``__dict__`` and ``__slots__``.** Every class in this module
declares ``__slots__``, which is the standard way to avoid the per-instance
``__dict__`` overhead. **However**, ``__slots__`` only *removes* the
``__dict__`` when **every** class in the MRO defines ``__slots__``. The
base :class:`BaseException` defines a ``__dict__`` — so instances of
:class:`NexusDLError` still have a (normally empty) ``__dict__`` that
costs one pointer per instance. The ``__slots__`` declarations are still
worth keeping:

* they document the attributes the exception exposes;
* they make ``__getstate__``'s enumeration unambiguous — the loop over
  ``__mro__`` ``__slots__`` picks up exactly the intended fields, and
  any attribute the caller assigned dynamically (``exc.foo = 1``) lands
  in ``__dict__`` and is *deliberately* not pickled, since it is not
  part of the exception's contract.

For a project that raises hundreds of exceptions per session, the
residual ``__dict__`` overhead is negligible (one pointer per instance,
~100 KB across 1000 live exceptions). There is no way to eliminate it
without changing the base class to ``Exception.__slots__ = ()`` in a
subclass — which is not what ``Exception`` does.

The pickle representation deliberately does **not** try to preserve the
``MappingProxyType`` wrapper — the wrapper is not picklable, and its
immutability contract is re-established by :meth:`__setstate__` on the
receiving side.

Chaining
--------
The pipeline uses ``raise X(...) from exc`` throughout to preserve the
underlying cause. The base class does **not** suppress chaining, so
``exc.__cause__`` and ``exc.__context__`` are populated by Python
automatically. Callers inspecting the cause use the standard
``__cause__`` attribute; this module adds nothing on top of the
mechanism. Note that ``__cause__`` and ``__context__`` are preserved
across pickling by :class:`BaseException` itself; our ``__reduce__``
does not interfere with that.

Not defined here
----------------
* **``KeyboardInterrupt``, ``SystemExit``, ``asyncio.CancelledError``**
  — all are ``BaseException`` subclasses, not ``Exception``. Every
  ``except Exception`` clause in the codebase relies on this property
  (cancellation propagates; shutdown signals shut down). A pipeline
  error must never be one of these.
* **Subclasses of the stdlib exceptions** (``FileNotFoundError``,
  ``ValueError``, etc.) raised by library calls that the pipeline
  passes through. Where the pipeline catches and translates such an
  exception, the translation goes into the pipeline's own hierarchy —
  e.g. ``OSError`` from a file write becomes ``PackagingError``. Where
  the pipeline does not catch it, the stdlib exception propagates
  unchanged; wrapping every stdlib error in a NexusDL subclass would
  hide information the caller needs.

Dependency graph (no cycles)::

    (no imports beyond typing and types)

This module is a **pure leaf** — zero dependencies, zero I/O, zero
logging. It can be imported from anywhere, including from an early
bootstrap phase before the rest of the pipeline is importable.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final


__all__ = [
    "ChapterDownloadError",
    "CircuitBreakerOpenError",
    "ComplianceError",
    "ConfigError",
    "CookieEncryptionError",
    "DownloadError",
    "EmptyChapterError",
    "ImageConversionError",
    "ImageValidationError",
    "InvalidStateTransitionError",
    "LibraryError",
    "NexusDLError",
    "NexusDLPlaywrightError",
    "PackagingError",
    "PageDownloadError",
    "ParserError",
    "ParserNotFoundError",
    "ParserParseError",
    "ParserResponseError",
    "PluginError",
    "RateLimitTimeoutError",
    "RetryError",
    "RetryTimeoutError",
    "SessionCloudflareError",
    "SessionError",
    "SessionRateLimitError",
    "SessionTimeoutError",
]


# Shared empty context proxy — avoids creating a fresh empty dict and
# MappingProxyType for every exception raised without context, which is
# the common case for the pipeline's "best-effort context" contract.
_EMPTY_CONTEXT: Final[MappingProxyType[Any]] = MappingProxyType({})


# --------------------------------------------------------------------------- #
# Base                                                                        #
# --------------------------------------------------------------------------- #


class NexusDLError(Exception):
    """Base class for every exception NexusDL raises.

    A caller that catches this class catches every pipeline failure.
    Subclasses narrow the failure mode — see the module docstring for
    the full hierarchy and the retry classification.

    Attributes:
        message: The original message, as a ``str``. Equal to
            ``str(exc)`` and to ``exc.args[0]``.
        context: A read-only mapping of the keyword arguments the
            concrete class forwarded to this base constructor via
            ``**context``. **Not** a dump of every attribute the
            exception carries — see the module docstring's
            "Diagnostic attributes on the base" section for the
            distinction. Empty when the concrete class forwarded none,
            which is the common case.
    """

    __slots__ = ("context", "message")

    def __init__(self, message: str, /, **context: Any) -> None:
        """Construct an exception with a message and optional context.

        Args:
            message: The human-readable message. Rendered by
                ``str(exc)`` and preserved as :attr:`message`.
            **context: Additional structured context, exposed via
                :attr:`context` as a read-only mapping. Concrete
                subclasses that accept named keyword arguments (e.g.
                :class:`ParserError`'s ``url`` and ``site_id``) set
                those as attributes **and** forward them here; the two
                views are complementary. A subclass that just wants to
                expose unstructured context can pass it directly here.
        """
        super().__init__(message)
        self.message = message
        # ``dict(context)`` then wrapped — a caller that later mutates
        # the dict they passed to ``**context`` does not affect the
        # exception's view. Empty context reuses a shared proxy rather
        # than constructing a new one on every raise.
        self.context = (
            MappingProxyType(dict(context)) if context else _EMPTY_CONTEXT
        )

    def __str__(self) -> str:
        """Return the message, unchanged.

        Overridden to make ``str(exc)`` explicit: the base
        :class:`Exception` already returns ``args[0]`` when there is a
        single argument, but pinning the behaviour here means a future
        change to ``args`` composition does not silently change what
        ``str(exc)`` returns for every caller that logs it.
        """
        return self.message

    # ------------------------------------------------------------------ #
    # Pickle support                                                     #
    # ------------------------------------------------------------------ #

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle the exception with every slot preserved.

        The default :meth:`BaseException.__reduce__` returns a two-tuple
        ``(type, args)`` — which drops the ``context`` mapping and every
        subclass-specific field (``url``, ``status_code``,
        ``timeout_seconds``, …) across a pickle round-trip, because none
        of them are in ``args``.

        Returning a three-tuple ``(type, args, state)`` with a
        pickle-friendly plain-dict ``state`` forces the receiving side
        through :meth:`__setstate__`, which re-wraps ``context`` in a
        :class:`~types.MappingProxyType` and restores every slot.
        """
        return (type(self), self.args, self.__getstate__())

    def __getstate__(self) -> dict[str, Any]:
        """Return a pickle-friendly representation of every instance field.

        Enumerates every ``__slots__`` declared on the class and its
        ancestors, so a subclass added tomorrow is pickled correctly
        without touching this method. Slot attributes that are not set
        (because the subclass's ``__init__`` skipped them) are omitted
        rather than written as ``None``, so a partially-initialised
        exception round-trips as a partially-initialised exception.

        Attributes assigned dynamically by the caller (``exc.foo = 1``)
        land in ``__dict__`` and are deliberately **not** pickled — they
        are not part of the exception's contract.
        """
        state: dict[str, Any] = {
            "message": self.message,
            "context": dict(self.context),
        }
        extra: dict[str, Any] = {}
        for cls in type(self).__mro__:
            for slot in getattr(cls, "__slots__", ()):
                if slot in state:
                    continue
                try:
                    extra[slot] = getattr(self, slot)
                except AttributeError:
                    continue
        state["extra"] = extra
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore state produced by :meth:`__getstate__`."""
        self.message = state["message"]
        self.context = MappingProxyType(state["context"])
        for key, value in state.get("extra", {}).items():
            setattr(self, key, value)


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #


class ConfigError(NexusDLError):
    """Configuration is invalid or missing.

    Raised by the settings loader, the sites registry, the YAML validator,
    and the plugin manifest validator. Non-retryable — a configuration
    that is broken at startup will still be broken one second later; the
    correct response is to fix the config, not to retry.
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# Parsers                                                                     #
# --------------------------------------------------------------------------- #


class ParserError(NexusDLError):
    """Base class for parser failures.

    Concrete subclasses distinguish the three cases a caller cares about:
    a missing parser (:class:`ParserNotFoundError`), a network-level
    failure while fetching from the site (:class:`ParserResponseError`,
    retryable), and a content-level failure while extracting data from
    the response (:class:`ParserParseError`, permanent).

    Attributes:
        url: The URL the parser was operating on, when available.
        site_id: The site identifier, when available.
    """

    __slots__ = ("site_id", "url")

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.url = url
        self.site_id = site_id


class ParserNotFoundError(ParserError):
    """No parser is registered for the requested site, or the registered
    parser cannot be constructed.

    Raised by
    :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.get_parser`
    and by the injected :class:`ParserFactory`. Permanent — a site with
    no parser will still have no parser on retry.
    """

    __slots__ = ()


class ParserResponseError(ParserError):
    """The site returned a response the parser cannot process.

    Typically a truncated body, a 5xx behind the scraper's tolerance, or
    a WAF interstitial. Transient — retrying after a backoff is usually
    worth it, which is why
    :func:`~nexusdl.core.downloader.retry.is_retryable` classifies it as
    retryable.

    Attributes:
        status_code: The HTTP status of the response, when the parser
            could read one. ``None`` for a network-level failure that
            never received a response.

            This field is exposed as an **attribute only** — it is not
            forwarded to :attr:`NexusDLError.context`. See the module
            docstring's "Diagnostic attributes on the base" section for
            the distinction.
    """

    __slots__ = ("status_code",)

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.status_code = status_code


class ParserParseError(ParserError):
    """The parser received a response but could not extract data from it.

    Almost always a site that has changed its HTML structure or JSON
    schema. Permanent — the parser's code needs to be updated; retrying
    the same request will produce the same result.
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# Downloads                                                                   #
# --------------------------------------------------------------------------- #


class DownloadError(NexusDLError):
    """Base class for download-pipeline failures.

    Raised at the chapter or page level when a task cannot be completed.
    The worker wraps an unexpected exception into one of the concrete
    subclasses so the manager always sees a ``DownloadError`` from a
    failed task.

    Attributes:
        task_id: The task's id, as a string, when available.
    """

    __slots__ = ("task_id",)

    def __init__(
        self,
        message: str,
        /,
        *,
        task_id: str | None = None,
    ) -> None:
        super().__init__(message, task_id=task_id)
        self.task_id = task_id


class ChapterDownloadError(DownloadError):
    """A chapter-level download failed.

    Raised by the worker when the chapter's pages cannot all be
    downloaded — the underlying cause is one or more
    :class:`PageDownloadError` instances, chained via ``__cause__`` when
    a single one predominates.
    """

    __slots__ = ()


class PageDownloadError(DownloadError):
    """A single page download failed.

    Raised by the parser's ``download_page`` method and by the worker's
    image validator when the response does not constitute a valid image.
    Transient — retryable through the pipeline's retry controller.
    """

    __slots__ = ()


class EmptyChapterError(DownloadError):
    """A chapter has no pages.

    Raised by the worker when the parser returns an empty page list.
    Distinct from :class:`PageDownloadError` because there is nothing to
    retry — the chapter is genuinely empty in the source, and the
    correct response is to skip it, not to wait for pages to appear.
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# Packaging                                                                   #
# --------------------------------------------------------------------------- #


class PackagingError(NexusDLError):
    """A packager failed to produce its output.

    Raised by every packager (ZIP, CBZ, CB7, PDF, FOLDER) and by the
    :class:`~nexusdl.core.packaging.factory.PackagerFactory` when a
    format has no shipped implementation. Not retried by default — most
    packaging failures are deterministic (missing source file, wrong
    output extension, unsupported format), and the correct response is
    to surface the problem.

    Attributes:
        format: The :class:`~nexusdl.core.models.enums.PackagingFormat`
            being produced, as its string value (``"cbz"``), when
            available.
    """

    __slots__ = ("format",)

    def __init__(
        self,
        message: str,
        /,
        *,
        format: str | None = None,
    ) -> None:
        super().__init__(message, format=format)
        self.format = format


# --------------------------------------------------------------------------- #
# Library                                                                     #
# --------------------------------------------------------------------------- #


class LibraryError(NexusDLError):
    """A library database or metadata operation failed.

    Raised by the database layer (schema violation, missing row, invalid
    migration file), by the scanner (unreadable archive), and by the
    metadata layer (unparseable ``ComicInfo.xml``). Not retried — library
    operations are local and deterministic, so a failure is a bug or a
    corrupt file to fix, not a transient condition.
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# Compliance                                                                  #
# --------------------------------------------------------------------------- #


class ComplianceError(NexusDLError):
    """A ``robots.txt`` policy refuses the requested fetch.

    Raised by
    :meth:`~nexusdl.core.compliance.ComplianceChecker.check` when the
    target URL is disallowed. The retry classifier treats it as
    permanent — retrying a disallowed URL wastes time and continues to
    violate the site's declared policy. Callers must **not** catch and
    retry this exception; if the URL is disallowed, the correct response
    is to skip it.

    Attributes:
        url: The URL that was disallowed, when available.
        site_id: The site identifier, when available.
    """

    __slots__ = ("site_id", "url")

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.url = url
        self.site_id = site_id


# --------------------------------------------------------------------------- #
# Session                                                                     #
# --------------------------------------------------------------------------- #


class SessionError(NexusDLError):
    """Base class for HTTP session failures.

    Raised by
    :class:`~nexusdl.core.session.http_session.HttpSession` on any
    network-level failure that is not otherwise classified. The
    concrete subclasses distinguish the failure modes a caller can act
    on: timeouts (retry), rate limits (retry after backoff), Cloudflare
    challenges (bypass), and circuit-breaker rejections (wait for the
    site to recover).

    Attributes:
        url: The request URL, when available.
        site_id: The site identifier, when available.
    """

    __slots__ = ("site_id", "url")

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.url = url
        self.site_id = site_id


class SessionTimeoutError(SessionError):
    """A request timed out, or the connection failed.

    Retryable — the site may be temporarily slow or the network may have
    blipped. The retry controller includes it in ``_TRANSIENT_ERRORS``.

    Attributes:
        timeout_seconds: The timeout value that fired, when the caller
            knows it. Attribute only, not mirrored in ``context``.
    """

    __slots__ = ("timeout_seconds",)

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.timeout_seconds = timeout_seconds


class SessionRateLimitError(SessionError):
    """The site returned ``429 Too Many Requests``.

    Retryable — a backoff before the next attempt usually succeeds. The
    retry controller includes it in ``_TRANSIENT_ERRORS``.

    Attributes:
        retry_after: The server's ``Retry-After`` header value, in
            seconds, when present. ``None`` when the server did not
            provide one (the retry controller falls back to its own
            backoff policy). Attribute only, not mirrored in
            ``context``.
    """

    __slots__ = ("retry_after",)

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.retry_after = retry_after


class SessionCloudflareError(SessionError):
    """A Cloudflare challenge or WAF block.

    Raised by the session when the response is a 403 or 503 carrying
    Cloudflare's challenge headers, and by
    :class:`~nexusdl.core.session.flaresolverr.FlareSolverrClient` when
    a solve attempt did not succeed. Retryable — but only after a
    challenge bypass (Playwright, FlareSolverr) has been attempted; a
    naive retry will hit the same wall.
    """

    __slots__ = ()


class CookieEncryptionError(SessionError):
    """A cookie store operation failed due to an encryption problem.

    Raised by
    :class:`~nexusdl.core.session.cookie_manager.CookieManager` when
    the encrypted cookie file cannot be decrypted (wrong key, corrupted
    file) or when it decrypts to an invalid payload. Permanent — the
    correct response is to fix the key or delete the file, not to retry.
    """

    __slots__ = ()


class CircuitBreakerOpenError(SessionError):
    """The site's circuit breaker is open.

    Raised by
    :class:`~nexusdl.core.session.http_session.HttpSession` before the
    request is made, when the site has failed too many times in a row.
    Retryable — the breaker transitions to half-open after its recovery
    timeout, and a retry after a longer wait usually succeeds.

    Attributes:
        opened_until: A description of when the breaker will allow the
            next trial request, when available. Typically a
            human-readable string like ``"in 25 seconds"``; the concrete
            value is a diagnostic aid, not a value to compute against.
    """

    __slots__ = ("opened_until",)

    def __init__(
        self,
        message: str,
        /,
        *,
        url: str | None = None,
        site_id: str | None = None,
        opened_until: str | None = None,
    ) -> None:
        super().__init__(message, url=url, site_id=site_id)
        self.opened_until = opened_until


class RateLimitTimeoutError(SessionError):
    """The rate limiter timed out waiting for a free slot.

    Raised by
    :meth:`~nexusdl.core.session.rate_limiter.RateLimiter.acquire` when
    the caller-supplied timeout expires before a token becomes
    available. **Not** retryable by default — the caller already waited
    the configured window, and a naive retry would just wait again. A
    caller that genuinely wants to retry (e.g. a background job with
    time to spare) wraps the call in
    :func:`~nexusdl.core.utils.retry.retry_async` explicitly.

    Deliberately **not** a subclass of :class:`SessionRateLimitError`:
    the two represent different situations and have different retry
    semantics. A ``SessionRateLimitError`` means the *server* said "slow
    down"; a ``RateLimitTimeoutError`` means *our own limiter* gave up
    waiting. Merging them would confuse the retry classifier's intent.
    """

    __slots__ = ()


class NexusDLPlaywrightError(SessionError):
    """Playwright is unavailable, unstartable, or failed to launch.

    Raised by
    :meth:`~nexusdl.core.session.playwright_pool.PlaywrightPool.start`
    when Playwright is not importable, when Chromium is not installed,
    or when launch fails for any other reason. Permanent for the common
    "not installed" case — retrying will not install Playwright.
    Callers with a transient Playwright issue (a browser that crashed
    mid-session) use the pool's own recovery path (``stop()`` then
    ``start()``) rather than a retry loop.
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# Retry                                                                       #
# --------------------------------------------------------------------------- #


class RetryError(NexusDLError):
    """Base class for retry-layer failures.

    Currently only one concrete subclass exists
    (:class:`RetryTimeoutError`); the base class is exposed so that a
    future retry-layer failure (an exhausted-budget condition distinct
    from a wall-clock timeout) has a natural home without expanding the
    top-level hierarchy.
    """

    __slots__ = ()


class RetryTimeoutError(RetryError):
    """A retry loop's wall-clock budget was exhausted.

    Raised by
    :func:`~nexusdl.core.utils.retry.retry_async` and
    :func:`~nexusdl.core.utils.retry.retry_sync` when the configured
    ``max_elapsed_seconds`` would be exceeded by the next backoff sleep.
    The last exception raised by the operation is chained as
    ``__cause__``. Not retried — the caller explicitly bounded the
    budget, and a retry would ignore that bound.

    Attributes:
        elapsed_seconds: How long the loop actually ran, when available.
        max_elapsed_seconds: The configured budget, when available.
    """

    __slots__ = ("elapsed_seconds", "max_elapsed_seconds")

    def __init__(
        self,
        message: str,
        /,
        *,
        elapsed_seconds: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            elapsed_seconds=elapsed_seconds,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        self.elapsed_seconds = elapsed_seconds
        self.max_elapsed_seconds = max_elapsed_seconds


# --------------------------------------------------------------------------- #
# Plugins                                                                     #
# --------------------------------------------------------------------------- #


class PluginError(NexusDLError):
    """A plugin failed to load, validate, or dispatch.

    Raised by
    :class:`~nexusdl.plugins.loader.PluginLoader` (load failure),
    :mod:`~nexusdl.plugins.validators` (manifest or class validation),
    and :mod:`~nexusdl.plugins.hooks` (unknown hook name).
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# State machines                                                              #
# --------------------------------------------------------------------------- #


class InvalidStateTransitionError(NexusDLError):
    """A state machine refused a transition.

    Raised by
    :class:`~nexusdl.core.downloader.queue.DownloadQueue` when a caller
    tries to move a task between states that are not adjacent in the
    documented transition table.

    Attributes:
        from_status: The status the entity was in, as its string value,
            when available.
        to_status: The status the caller tried to transition to, as its
            string value, when available.
    """

    __slots__ = ("from_status", "to_status")

    def __init__(
        self,
        message: str,
        /,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        super().__init__(
            message,
            from_status=from_status,
            to_status=to_status,
        )
        self.from_status = from_status
        self.to_status = to_status


# --------------------------------------------------------------------------- #
# Images                                                                      #
# --------------------------------------------------------------------------- #


class ImageValidationError(NexusDLError):
    """A downloaded image failed validation.

    Raised by :mod:`nexusdl.core.image.validator` when the bytes are not
    a valid image — empty, oversized, undersized, a disallowed format,
    or (the common case) an HTML/Cloudflare page masquerading as an
    image. Transient — retrying after a fresh request often returns the
    correct content.

    Attributes:
        path: The path the invalid image was written to, when available.
    """

    __slots__ = ("path",)

    def __init__(
        self,
        message: str,
        /,
        *,
        path: str | None = None,
    ) -> None:
        super().__init__(message, path=path)
        self.path = path


class ImageConversionError(NexusDLError):
    """An image conversion failed.

    Raised by :mod:`nexusdl.core.image.converter` when the source cannot
    be decoded, when the target format's codec is unavailable, or when
    the write fails. Not retried by default — conversions are
    deterministic, so a failure is a bug or a corrupt input to fix.
    """

    __slots__ = ()


# --------------------------------------------------------------------------- #
# Sanity: the module's __all__ must match the classes defined here            #
# --------------------------------------------------------------------------- #
# This assertion runs at import time and would fail on any future edit that
# adds a class without exporting it, or exports a name that does not exist.
# The cost is a single dict lookup at import; the benefit is that an
# incomplete exception hierarchy is caught the moment the module loads, not
# on the first call site that tries to import a missing name.
#
# The ``__module__ == __name__`` guard ensures that a re-exported class from
# another module (a future ``from nexusdl.somewhere import SomeError`` at
# the top of this file) is NOT counted — it belongs to the other module's
# ``__all__``, not this one. It also immunises the check against
# ``typing.Any``-like objects that some Python versions expose as types.
# --------------------------------------------------------------------------- #

_defined: Final[frozenset[str]] = frozenset(
    name
    for name in dir()
    if (obj := globals().get(name)) is not None
    and isinstance(obj, type)
    and issubclass(obj, Exception)
    and getattr(obj, "__module__", None) == __name__
)

if _defined != frozenset(__all__):
    _missing_from_all = _defined - frozenset(__all__)
    _missing_from_module = frozenset(__all__) - _defined
    raise RuntimeError(
        "exceptions.py: __all__ does not match the defined exception "
        f"classes. Missing from __all__: {sorted(_missing_from_all)}. "
        f"Listed but not defined: {sorted(_missing_from_module)}."
    )
