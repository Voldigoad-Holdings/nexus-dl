"""Logger configuration for the entire NexusDL pipeline.

:mod:`loguru` is the pipeline's logging library. Every module binds a
contextual logger with ``logger.bind(component="...")`` and logs through
it; this module is the **single place** the global loguru configuration
is set up. It is called exactly once by the composition root at startup,
before any other logging happens.

Why a dedicated configuration module
------------------------------------
loguru ships a working default configuration — a colored stderr sink at
``INFO`` level. That default is appropriate for a **library**, not for a
**pipeline** that also runs as a server, a CLI, a GUI, and a TUI. The
pipeline needs:

* **Structured logging in production** — JSON on stderr so a container
  log collector can parse every line without regex.
* **Human-readable logging in development** — colored output that a
  developer can read in a terminal.
* **Optional file logging** — with rotation, retention, and compression,
  so a long-running deployment does not fill its disk with logs.
* **Interception of stdlib ``logging``** — so ``httpx``, ``sqlalchemy``,
  ``aiosqlite``, and every other third-party library that logs through
  :mod:`logging` has its output routed through loguru's format and
  sinks, instead of producing a second, differently-formatted stream.

This module provides all four. It performs no side effects on import
beyond importing :mod:`loguru` itself — the caller must invoke
:func:`configure_logging` (or :func:`configure_logging_from_settings`)
to change the configuration.

Idempotency
-----------
:func:`configure_logging` calls :func:`loguru.logger.remove` before
adding any sink, so calling it twice replaces the configuration
cleanly — no duplicate handlers, no leaked file descriptors. This makes
it safe to call from a test fixture or from a composition root that
reconfigures based on a command-line flag.

The **stdlib interception** is equally idempotent: it uses
``logging.basicConfig(force=True)``, which removes any handlers a
previous call (or a third-party library) installed on the root logger
before installing the intercept handler. A caller that has installed
its own stdlib handlers on the root logger **will lose them** — this is
deliberate, because two differently-formatted logging streams are worse
than losing custom handlers, and a caller that needs a custom stdlib
handler installs it *after* calling this function.

Format resolution
-----------------
The ``format`` argument selects the output formatter:

* ``"pretty"`` — the colored human-readable format (loguru's default
  pattern). Suitable for terminals, TUIs, GUIs, and any human-facing
  output.
* ``"json"`` — one JSON object per line. Suitable for containers, log
  collectors, and any environment where logs are parsed rather than
  read.
* ``None`` — auto-selected from ``debug``: ``"pretty"`` when
  ``debug=True``, ``"json"`` when ``debug=False``.

The auto-selection prioritises production observability. **A local
first-time user should be aware of this** — running
``nexusdl search "one piece"`` on a fresh install produces JSON on
stderr, not colored text, because ``debug`` defaults to ``False``.
Setting ``debug=true`` in ``.env`` (or
``NEXUSDL_LOGGING__FORMAT=pretty``) restores the colored format. This is
documented at the :func:`configure_logging` call site as well.

Exception context
-----------------
``backtrace=True`` and ``diagnose=True`` are **only enabled when
``debug=True``**. In production:

* ``backtrace`` is disabled — no full call stack, just the error
  message and cause.
* ``diagnose`` is disabled — no local variable inspection.

The second is a security matter, not a cosmetic one:
``diagnose=True`` renders the values of every local variable in a
traceback, and a caller that has a plaintext secret in scope (an
``httpx.AsyncClient`` auth tuple, a raw password before it is hashed)
would find it in the log. ``SecretStr`` values are redacted by their
own ``__repr__``, but not every secret in the pipeline is a
``SecretStr`` — ``diagnose=True`` in production is a footgun this
module avoids.

Testing
-------
Tests that assert on emitted log lines should use pytest's
``capfd`` fixture (fd-level capture) rather than ``capsys`` (sys-level
capture). Loguru captures the ``sys.stderr`` **object reference** at
:func:`configure_logging` time, so a test that replaces ``sys.stderr``
**after** configuration does not affect the sink. ``capfd`` captures at
the file-descriptor level and sees the output regardless.

What this module does **not** do
--------------------------------
* **No log filtering for secrets.** The pipeline relies on the caller
  not logging secrets. A ``NEXUSDL_SECRET_KEY=...`` that appears in a
  log line appears in the log; the fix is at the call site, not here.
* **No log sampling, rate limiting, or aggregation.** Each line is
  written as produced. A caller with a noisy loop wraps the loop, not
  the logger.
* **No structured metadata injection.** The ``extra`` dict on a log
  line is set per-call via ``logger.bind()`` — the composition root
  sets the module-level bindings, each call site sets its own.
* **No log file rotation on demand.** Rotation is configured once and
  enforced by loguru's ``rotation=`` handler. There is no
  ``rotate_now()`` API — loguru does not support one, and the need for
  it has not surfaced.
* **No ``config/logging.yaml`` consumption.** That file exists in the
  repository as a template for a deployment that uses stdlib
  :mod:`logging` directly; NexusDL uses loguru, so the file is
  informational. A caller that wants to configure stdlib logging
  themselves does so after calling :func:`configure_logging`.

Dependency graph (no cycles)::

    loguru       ──► core.logger
    logging, sys, pathlib (stdlib)

``core.config`` is imported only under ``TYPE_CHECKING`` for the
:func:`configure_logging_from_settings` signature — the module does not
import any other ``nexusdl.core`` module at runtime, and could be lifted
into a standalone package together with ``loguru``.

Type-annotation note
--------------------
``loguru`` exposes only the ``logger`` **instance** at the top level —
the ``Logger`` class is not exported, so ``from loguru import Logger``
raises :class:`ImportError`. The annotation on :func:`get_logger` needs
the class, so it is imported from ``loguru._logger`` under
:data:`typing.TYPE_CHECKING`. Because this module uses
``from __future__ import annotations``, the annotation is never evaluated
at runtime — the import is only visible to mypy/pyright, not to the
interpreter. See the module-level import block for the exact form.

Frozen-environment note
-----------------------
The :class:`_InterceptHandler`'s frame-walking loop compares each
frame's ``co_filename`` against :data:`_STDLIB_LOGGING_FILE`, the
value of ``logging.__file__`` captured once at import time. In a
frozen binary (PyInstaller, cx_Freeze, Nuitka) or an embedded
interpreter, ``logging.__file__`` may be ``None``; the constant then
holds ``None`` and the walk short-circuits to the standard depth,
which is the best-effort behaviour — the stdlib frames cannot be
identified without the path, so loguru's default two-frame skip is
used. This is documented in the handler's ``emit`` docstring as well.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Final, Literal

from loguru import logger

if TYPE_CHECKING:
    # ``loguru`` does not re-export ``Logger`` at the top level (only the
    # ``logger`` instance). The class lives in the private
    # ``loguru._logger`` module. Importing it under ``TYPE_CHECKING``
    # keeps the runtime import list minimal while letting mypy resolve
    # the ``-> Logger`` annotation on :func:`get_logger`. See the module
    # docstring's "Type-annotation note" section.
    from loguru._logger import Logger

    from pathlib import Path

    from nexusdl.core.config import Settings


__all__ = [
    "configure_logging",
    "configure_logging_from_settings",
    "get_logger",
    "logger",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: The pretty (human-readable) log format. Roughly loguru's default, with
#: the timestamp extended to millisecond precision and the log level
#: padded to a consistent width so columns align in a terminal.
#:
#: Exposed as a private constant rather than inlined so that a caller
#: who wants to reference the format (a plugin building its own sink, a
#: test asserting format) has a sanctioned way to do so, and so that a
#: future format change has one place to live.
_PRETTY_FORMAT: Final[str] = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)

#: Value of ``logging.__file__``, captured once at import time so the
#: frame-walking loop in :meth:`_InterceptHandler.emit` does not
#: re-read the attribute on every record (a negligible cost, but the
#: captured value is also useful as a module-level constant that a test
#: can monkeypatch). ``None`` in a frozen binary or embedded
#: interpreter — the loop tolerates it. See the module docstring's
#: "Frozen-environment note" section.
_STDLIB_LOGGING_FILE: Final[str | None] = getattr(
    logging, "__file__", None
)

#: Type alias for the ``format`` parameter, matching
#: :attr:`nexusdl.core.config.LoggingSection.format`.
_FormatChoice = Literal["pretty", "json"] | None


# --------------------------------------------------------------------------- #
# stdlib logging interception                                                 #
# --------------------------------------------------------------------------- #


class _InterceptHandler(logging.Handler):
    """A ``logging.Handler`` that forwards every record to loguru.

    Installed on the root logger by :func:`configure_logging` so that
    every third-party library that logs via :mod:`logging` (``httpx``,
    ``sqlalchemy``, ``aiosqlite``, …) has its output routed through
    loguru's configured sinks, format, and level filter — instead of
    producing a second, differently-formatted stream on stderr.

    The forwarding uses loguru's standard "intercept" recipe:

    1. Read the record's level name if loguru knows it; otherwise pass
       the numeric level through unchanged.
    2. Walk the caller's stack to find the frame that actually issued
       the ``logging`` call (a few frames are loguru-internal), so the
       rendered ``name:function:line`` in the output identifies the
       third-party caller, not this handler.
    3. Log through loguru with the exception info preserved.

    The handler is **not** installed unless :func:`configure_logging` is
    called — a caller that imports ``nexusdl.core.logger`` but never
    configures it gets loguru's default behavior (stdlib libraries log
    via their own handlers, mixed with loguru output).
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a stdlib record to loguru.

        Args:
            record: The stdlib log record to forward.
        """
        # Map the stdlib level to a loguru level name where possible;
        # fall back to the numeric level so a custom stdlib level (a
        # library using level 15, say) still logs at the right place.
        try:
            level: str | int = logger.level(record.levelname).name
        except (ValueError, AttributeError):
            level = record.levelno

        # Find the frame that issued the logging call, skipping this
        # handler and loguru's own machinery. Two frames is loguru's
        # documented "traceback depth" for direct calls; the loop keeps
        # walking while we are still inside the stdlib logging module
        # (some libraries wrap logging via a shim that adds more frames).
        #
        # ``_STDLIB_LOGGING_FILE`` is captured once at import time (see
        # its definition). When it is ``None`` — frozen binaries,
        # embedded interpreters — the comparison below can never match,
        # so the loop exits on the first iteration and ``depth`` stays
        # at 2, which is loguru's documented default. The output then
        # names the immediate stdlib handler as the caller rather than
        # the user code; that is a cosmetic degradation, not a crash,
        # and the alternative (walking blindly by depth) would be
        # wrong on any other stdlib-wrapper shim.
        frame: Any = logging.currentframe()
        depth = 2
        if _STDLIB_LOGGING_FILE is not None:
            while frame is not None:
                if frame.f_code.co_filename == _STDLIB_LOGGING_FILE:
                    frame = frame.f_back
                    depth += 1
                else:
                    break

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def configure_logging(
    *,
    level: str = "INFO",
    format: _FormatChoice = None,
    file: Path | None = None,
    rotation: str = "10 MB",
    retention: str = "7 days",
    compression: str | None = "gz",
    debug: bool = False,
) -> None:
    """Configure loguru's global sinks for the current process.

    Idempotent — calling this twice replaces the configuration cleanly.
    Safe to call from a test fixture.

    See the module docstring for the format-resolution rules, the
    debug/backtrace/diagnose contract, and the stdlib interception
    policy.

    Args:
        level: Minimum log level, as a loguru level name
            (``"TRACE"``, ``"DEBUG"``, ``"INFO"``, ``"SUCCESS"``,
            ``"WARNING"``, ``"ERROR"``, ``"CRITICAL"``). Case-sensitive;
            callers using :func:`configure_logging_from_settings` get
            the upper-cased value from
            :attr:`~nexusdl.core.config.LoggingSection.level`.
        format: Output format selector. ``"pretty"`` forces the
            colored human-readable format; ``"json"`` forces the
            one-object-per-line format. ``None`` selects the format
            automatically from *debug*: ``"pretty"`` when
            ``debug=True``, ``"json"`` when ``debug=False``. See the
            module docstring's "Format resolution" section.
        file: Optional path to a log file. When ``None`` (the default),
            logs go only to stderr. The parent directory is created if
            it does not exist.
        rotation: Rotation policy for the log file, in loguru's
            combined size/time syntax (``"10 MB"``, ``"1 day"``,
            ``"00:00"``). Ignored when *file* is ``None``.
        retention: Retention policy for rotated files
            (``"7 days"``, ``"10 files"``). Ignored when *file* is
            ``None``.
        compression: Compression applied to rotated files
            (``"gz"``, ``"zip"``, ``"xz"``, or ``None``). Ignored when
            *file* is ``None``.
        debug: Debug mode. When ``True``:

            * ``format`` defaults to ``"pretty"`` when left as ``None``
              (developer-friendly output on stderr).
            * ``backtrace=True`` and ``diagnose=True`` are set on both
              sinks — full tracebacks and local variable inspection.
              **Do not enable in production** — see the module
              docstring's "Exception context" section.

            When ``False`` (the default):

            * ``format`` defaults to ``"json"`` when left as ``None``.
            * Tracebacks are concise; local variables are not shown.

    Raises:
        ValueError: If *level* is not a loguru level name. Raised by
            :meth:`loguru.Logger.add`, not by this function directly.
        OSError: If *file*'s parent directory cannot be created, or
            the file cannot be opened for writing.
    """
    resolved_format: Literal["pretty", "json"] = _resolve_format(
        format, debug
    )

    # Clear every handler (loguru's default stderr handler plus any
    # handler a previous configure_logging call added). This is the
    # idempotency mechanism: reconfiguring replaces, never accumulates.
    logger.remove()

    # --- stderr sink ------------------------------------------------ #
    if resolved_format == "json":
        logger.add(
            sys.stderr,
            level=level,
            serialize=True,
            backtrace=debug,
            diagnose=debug,
        )
    else:
        logger.add(
            sys.stderr,
            level=level,
            format=_PRETTY_FORMAT,
            colorize=True,
            backtrace=debug,
            diagnose=debug,
        )

    # --- file sink (optional) --------------------------------------- #
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        if resolved_format == "json":
            logger.add(
                file,
                level=level,
                serialize=True,
                rotation=rotation,
                retention=retention,
                compression=compression,
                backtrace=debug,
                diagnose=debug,
            )
        else:
            # ``colorize=False`` — a log file should never contain ANSI
            # escape sequences; ``less``, ``grep``, and log collectors
            # do not strip them.
            logger.add(
                file,
                level=level,
                format=_PRETTY_FORMAT,
                colorize=False,
                rotation=rotation,
                retention=retention,
                compression=compression,
                backtrace=debug,
                diagnose=debug,
            )

    # --- stdlib interception ---------------------------------------- #
    # ``force=True`` removes any pre-existing handlers on the root
    # logger before installing the intercept handler. Two differently-
    # formatted stdlib streams are worse than losing a caller's custom
    # handlers; a caller that needs a custom handler installs it after
    # this function returns.
    #
    # ``level=0`` sets the root logger's level to NOTSET so that every
    # record reaches ``_InterceptHandler.emit``. Loguru's own level
    # filter (the ``level=`` argument on the sinks above) is what
    # actually gates output — the stdlib level is deliberately not a
    # second filter. Passing anything other than 0 here would silently
    # drop DEBUG/TRACE records from third-party libraries before they
    # ever reach loguru.
    logging.basicConfig(
        handlers=[_InterceptHandler()],
        level=0,
        force=True,
    )


def configure_logging_from_settings(settings: Settings) -> None:
    """Configure logging from a :class:`~nexusdl.core.config.Settings` object.

    Convenience wrapper around :func:`configure_logging` that reads
    every argument from ``settings.logging`` and ``settings.app.debug``.
    This is the function the composition root calls:

    .. code-block:: python

        from nexusdl.core.config import Settings
        from nexusdl.core.logger import configure_logging_from_settings

        settings = Settings()
        configure_logging_from_settings(settings)

    Args:
        settings: The application settings. Only the ``logging`` section
            and ``app.debug`` are read.
    """
    configure_logging(
        level=settings.logging.level,
        format=settings.logging.format,
        file=settings.logging.file,
        rotation=settings.logging.rotation,
        retention=settings.logging.retention,
        compression=settings.logging.compression,
        debug=settings.app.debug,
    )


def get_logger(component: str) -> Logger:
    """Return a loguru logger bound to *component*.

    The pipeline's convention is that every module binds a logger to its
    dotted path (``"downloader.worker"``, ``"session.http"``), so log
    lines from different modules are visually distinguishable and
    greppable. This function is a thin wrapper around
    ``loguru.logger.bind(component=...)`` that documents the convention
    and gives a single place to change the binding key if the pipeline
    ever adopts a different one.

    Existing modules were written before this helper existed and use
    ``logger.bind(component="...")`` directly. Both forms are valid;
    new code should prefer ``get_logger(__name__)`` — it produces a
    bound logger keyed by the module's import path, which is what a
    reader expects from a Python log line.

    Args:
        component: The binding value, conventionally the module's
            dotted import path (``__name__`` at the call site).

    Returns:
        A :class:`loguru.Logger` with ``component=<component>`` bound
        to its context. Every log line emitted through it carries the
        binding, which loguru renders as ``extra['component']`` in JSON
        mode and as part of the ``name`` in pretty mode.
    """
    return logger.bind(component=component)


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #


def _resolve_format(
    format_: _FormatChoice,
    debug: bool,
) -> Literal["pretty", "json"]:
    """Resolve the effective output format from the arguments.

    Explicit values win; ``None`` auto-selects from *debug* per the
    module docstring's "Format resolution" section.

    Args:
        format_: The caller-supplied format, or ``None``.
        debug: The debug flag.

    Returns:
        ``"pretty"`` or ``"json"``.
    """
    if format_ == "pretty":
        return "pretty"
    if format_ == "json":
        return "json"
    # ``format_`` is ``None`` — auto-select from debug.
    return "pretty" if debug else "json"
