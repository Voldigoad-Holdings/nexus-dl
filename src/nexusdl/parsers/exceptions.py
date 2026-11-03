"""Parser-layer exceptions — re-export module.

This module is a **convenience import path** for the exceptions that parser
implementations, mixins, factories, tests and CLI sub-commands are expected
to raise or catch.  Every class it exports is **defined** in
:mod:`nexusdl.core.exceptions`; nothing new is declared here.

Why a re-export module?
-----------------------
The project's hexagonal architecture enforces two rules that would
otherwise scatter these imports across the codebase:

* ``core/`` may **never** depend on ``parsers/`` (R1 in the PROMPT MASTER).
  The parser exception hierarchy therefore cannot live under ``parsers/`` —
  it must live in ``core/exceptions.py`` so that library code, downloaders
  and interfaces can catch parser failures without importing the adapter
  layer.
* ``parsers/`` **may** depend on ``core/`` (R2 explicitly allows it).  Parser
  modules can therefore freely import from ``core.exceptions``.

That leaves a small ergonomic gap: parser code (and the tests / CLI that
exercise it) constantly needs a handful of exceptions that, conceptually,
belong to the parser layer even though they are *defined* in core.  This
module closes that gap by re-exporting them under the parser package's
namespace, so that callers can write::

    from nexusdl.parsers.exceptions import ParserNotFoundError

instead of reaching across the architectural boundary::

    from nexusdl.core.exceptions import ParserNotFoundError

Both imports resolve to the **same class object**, so ``except`` clauses work
identically regardless of the path used.  There is no behavioural
difference; the re-export is purely a convenience and an architectural
marker.

What lives where
----------------
The exception hierarchy (see ``core/exceptions.py``) is:

.. code-block:: text

    NexusDLError                     ← base for every project exception
    ├── ParserError                  ← base for parser failures
    │   ├── ParserNotFoundError      ← factory cannot find a parser class
    │   ├── ParserResponseError      ← upstream transport / HTTP error
    │   └── ParserParseError         ← upstream returned malformed data
    ├── SessionError
    │   ├── SessionCloudflareError   ← Cloudflare challenge unsolved
    │   └── NexusDLPlaywrightError   ← Playwright unavailable or failed
    └── (domain errors — ConfigError, DownloadError, etc.)

The four ``Parser*`` classes are the parser-layer's primary public
exceptions; ``SessionCloudflareError`` and ``NexusDLPlaywrightError`` are
re-exported here because every Cloudflare-protected or JavaScript-rendered
parser needs to catch or re-raise them and it would be tedious to import
them from ``core`` at every call site.

Domain errors (``ConfigError``, ``DownloadError``, ``PackagingError``,
``LibraryError``, ``ComplianceError``, ``PluginError``,
``InvalidStateTransitionError``, ``ImageValidationError``,
``ImageConversionError``) are **not** re-exported here.  They are unrelated
to parser behaviour, and importing them from the parser package would blur
the layering the project is built around.

Catch-all handler
-----------------
Code that needs a single ``except`` clause covering every possible failure
from a parser call should catch :class:`NexusDLError` from
:mod:`nexusdl.core.exceptions` directly.  It is deliberately **not**
re-exported here — doing so would encourage callers to treat
:mod:`nexusdl.parsers.exceptions` as a general-purpose exception module,
which it is not.
"""
from __future__ import annotations

from nexusdl.core.exceptions import (
    NexusDLPlaywrightError,
    ParserError,
    ParserNotFoundError,
    ParserParseError,
    ParserResponseError,
    SessionCloudflareError,
)

__all__ = [
    "NexusDLPlaywrightError",
    "ParserError",
    "ParserNotFoundError",
    "ParserParseError",
    "ParserResponseError",
    "SessionCloudflareError",
]
