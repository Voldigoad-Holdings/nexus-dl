"""NexusDL top-level CLI dispatcher — the ``python -m nexusdl`` entrypoint.

Running ``nexusdl`` (or ``python -m nexusdl``) routes to one of four
interface adapters based on the first positional argument:

    nexusdl                    → CLI (default)
    nexusdl <cli-cmd> ...      → CLI (Typer)
    nexusdl cli <cmd> ...      → CLI (explicit escape hatch)
    nexusdl tui                → TUI (Textual)
    nexusdl gui                → GUI (CustomTkinter)
    nexusdl web [flags...]     → Web (FastAPI + uvicorn)

Plus, at the top level:

    nexusdl --version / -V / version    → print version
    nexusdl --help    / -h / help       → print this help text

Lazy loading contract
=====================

This module **never** imports Typer, Textual, CustomTkinter, FastAPI, or
any of the four interface packages at module load.  ``python -m nexusdl
--version`` touches only the standard library.

The chosen interface is imported **once**, **at the moment of dispatch**,
through the following preference order:

1. **Import the module** (``importlib.import_module``) and call its
   ``main()`` function if it exposes one.  This is the preferred path for
   composition-root modules like ``nexusdl.interfaces.cli.cli_entry``,
   ``nexusdl.interfaces.cli.app`` (TUI), and
   ``nexusdl.interfaces.web.backend.main``.
2. **Fall back to ``runpy.run_module``** with ``run_name="__main__"`` for
   modules that don't expose a ``main()`` function but do have an
   ``if __name__ == "__main__":`` block.  This handles
   ``nexusdl.interfaces.gui.__main__`` (if it exists) and any other
   ``__main__.py`` wrapper.

Both paths honour the same contract:

* the sub-interface runs with a clean ``sys.argv`` (with a friendly
  ``argv[0]`` so help/error output reads ``nexusdl <iface> ...`` rather
  than ``/usr/bin/python``),
* ``sys.exit`` codes propagate to the caller,
* ``sys.argv`` is restored on exit (safe to chain dispatches in tests),
* the current namespace is **not** polluted with the sub-interface's
  globals.

Dispatch table
==============

Each interface maps to a dotted module path in :data:`_DISPATCH`:

* ``cli``  → :mod:`nexusdl.interfaces.cli.__main__`     (thin wrapper)
* ``tui``  → :mod:`nexusdl.interfaces.cli.app`          (Textual app)
* ``gui``  → :mod:`nexusdl.interfaces.gui.__main__`     (thin wrapper)
* ``web``  → :mod:`nexusdl.interfaces.web.backend.main` (uvicorn launcher)

The two ``__main__`` entries are the conventional Python entrypoint for
their packages.  If they do not exist yet in a given tree, either:

* **create them** as thin wrappers (recommended, five lines each), or
* **change** :data:`_DISPATCH` to point to the composition-root modules
  (``cli.cli_entry``, ``gui.app``) — the dispatcher's ``main()``
  preference handles both transparently.

Both the ``main()`` and the ``if __name__ == "__main__":`` contracts are
supported at dispatch time; the choice is per-interface and does not
affect this module's behaviour.

Errors
======

* **Missing extras** (``nexusdl gui`` on a headless server without
  Tkinter): the dispatcher catches the :class:`ImportError` raised by the
  import machinery **and** distinguishes two cases:

  - the submodule itself does not exist (``ModuleNotFoundError`` with
    ``exc.name == dotted``) → the message points at the dispatch table,
    not at ``pip install``.  This catches composition-root mistakes.
  - a **transitive** dependency is missing (``exc.name`` is ``tkinter``,
    ``fastapi``, …) → the message points at the relevant ``pip install
    'nexusdl[gui]'`` line.  This catches missing extras.

* **Ctrl+C**: exits 130 (Unix convention: 128 + SIGINT=2).
* **BrokenPipeError** (``nexusdl <cmd> | head``): exits 141 silently
  (128 + SIGPIPE=13).
* **Any other exception**: propagates normally with a traceback and
  exit code 1.  That is the interface's bug, not the dispatcher's —
  surfacing it loudly is correct.

Web flags
=========

``nexusdl web`` translates a small set of uvicorn-style flags into the
corresponding ``NEXUSDL_*`` env vars, which
:class:`nexusdl.core.config.Settings` reads at construction time:

    nexusdl web --host 0.0.0.0      → NEXUSDL_API_HOST=0.0.0.0
    nexusdl web --port 9000         → NEXUSDL_API_PORT=9000

Translated flags are **consumed** — they are removed from the argv
forwarded to the web launcher, so the launcher only sees its own
arguments.  (Before this change they were forwarded but harmlessly
ignored; the fix makes the behaviour explicit and keeps the launcher's
argv clean.)

Two common uvicorn flags are **not** supported by this launcher because
they require the ``uvicorn --factory`` invocation form:

    --reload     → use `uvicorn nexusdl.interfaces.web:create_app_from_env --factory --reload`
    --workers N  → use `uvicorn nexusdl.interfaces.web:create_app_from_env --factory --workers N`

The dispatcher prints a hint pointing at the correct command when either
of those is passed.  For multi-worker production, the canonical deployment
pattern is uvicorn's ``--factory`` (documented in
:mod:`nexusdl.interfaces.web`), which gives each worker its own services
container.
"""
from __future__ import annotations

import importlib
import os
import runpy
import sys
from typing import Final

__all__ = ["main"]


# ===========================================================================
# Dispatch configuration
# ===========================================================================

#: Canonical dispatch table — subcommand → dotted module to run.
#:
#: Adding a new interface = one entry here + the corresponding runnable
#: module in ``nexusdl.interfaces``.  No other file must change.
#:
#: The ``cli`` and ``gui`` entries point to ``__main__`` packages, which
#: is the conventional Python convention for ``python -m``-style entry
#: points.  Either those packages must exist (as thin wrappers around the
#: composition root), or the entries can be changed to point directly at
#: the composition-root modules — :func:`_run_module` supports both.
_DISPATCH: Final[dict[str, str]] = {
    "cli": "nexusdl.interfaces.cli.__main__",
    "tui": "nexusdl.interfaces.cli.app",
    "gui": "nexusdl.interfaces.gui.__main__",
    "web": "nexusdl.interfaces.web.backend.main",
}

#: Interface used when the user provides no positional argument.
_DEFAULT_INTERFACE: Final[str] = "cli"

#: First positional arguments reserved as interface selectors.  Any other
#: first argument is treated as a CLI subcommand and forwarded to the CLI
#: with the whole argv intact.
_RESERVED: Final[frozenset[str]] = frozenset(_DISPATCH.keys())

#: ``nexusdl web`` flag → env var translations.  Applied **before** the
#: web launcher runs so that ``Settings()`` (constructed inside
#: ``main()``) sees the overrides.  Translated flags are **consumed** (not
#: forwarded to the launcher).
_WEB_FLAG_TO_ENV: Final[dict[str, str]] = {
    "--host": "NEXUSDL_API_HOST",
    "--port": "NEXUSDL_API_PORT",
}

#: uvicorn flags that require the ``uvicorn --factory`` invocation form
#: and are therefore **not** supported by ``nexusdl web``.  Passing them
#: produces a hint pointing at the correct command.
_WEB_UNSUPPORTED_FLAGS: Final[frozenset[str]] = frozenset(
    {"--reload", "--workers"}
)


# ===========================================================================
# Public entrypoint
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    """Dispatch to the appropriate interface based on ``argv``.

    This is the callable that ``if __name__ == "__main__":`` invokes.  It
    is also what any host that wants to embed the NexusDL CLI (a plugin, a
    test harness, an integration script) should call.

    Args:
        argv: Optional argv (without the program name).  Defaults to
            ``sys.argv[1:]``.  Accepting this as an argument makes the
            dispatcher testable without spawning a subprocess.

    Returns:
        The exit code to propagate.  ``0`` on success, ``130`` on Ctrl+C,
        ``141`` on broken pipe, ``1`` on failure, or the exit code the
        sub-interface passed to ``sys.exit``.
    """
    args = list(argv if argv is not None else sys.argv[1:])

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        # Unix convention: 128 + SIGINT(2) = 130.  Print a trailing
        # newline because the terminal swallowed our last line.
        sys.stderr.write("\n")
        return 130
    except BrokenPipeError:
        # Shell convention: 128 + SIGPIPE(13) = 141.  Silent — the user
        # piped into `head`, `grep -m1`, etc. and expects silence.
        return 141
    except SystemExit as exc:
        # ``sys.exit(...)`` from a sub-interface.  Preserve the code
        # exactly (int or string) and let the caller decide.
        code = exc.code
        if isinstance(code, int):
            return code
        if code is None:
            return 0
        sys.stderr.write(f"{code}\n")
        return 1


# ===========================================================================
# Dispatcher internals
# ===========================================================================

def _dispatch(args: list[str]) -> int:
    """Route ``args`` to the correct interface and run it."""
    # No arguments → default interface (CLI).
    if not args:
        return _run_interface(_DEFAULT_INTERFACE, rest=[], prog="nexusdl")

    first = args[0]

    # Top-level informational flags.
    if first in ("--version", "-V", "version"):
        return _print_version()

    if first in ("--help", "-h", "help"):
        return _print_help()

    # Leading flag → CLI default (``nexusdl --foo`` is almost certainly
    # a CLI global option, not an interface selector).
    if first.startswith("-"):
        return _run_interface(_DEFAULT_INTERFACE, rest=args, prog="nexusdl")

    # Explicit interface selector.
    if first in _RESERVED:
        return _run_interface(first, rest=args[1:], prog=f"nexusdl {first}")

    # Otherwise — a CLI subcommand.  Forward the whole argv so the CLI's
    # Typer app re-parses it as if invoked directly.
    return _run_interface(_DEFAULT_INTERFACE, rest=args, prog="nexusdl")


def _run_interface(
    name: str, *, rest: list[str], prog: str
) -> int:
    """Run the interface registered under ``name`` with ``rest`` as argv."""
    dotted = _DISPATCH.get(name)
    if dotted is None:
        # Programmer error — the dispatch table and _RESERVED went out of
        # sync.  This should never happen at runtime.
        raise RuntimeError(
            f"internal error: no dispatch entry for interface {name!r}"
        )

    if name == "web":
        rest = _consume_web_flags(rest)
    return _run_module(dotted, argv=rest, prog=prog)


def _run_module(dotted: str, *, argv: list[str], prog: str) -> int:
    """Run ``dotted`` as ``__main__`` with a controlled ``sys.argv``.

    Two execution strategies, tried in order:

    1. **``main()`` preference** — import the module and call its
       ``main()`` function if it exposes one.  This is the composition-
       root path: ``cli_entry.main()``, ``app.main()``, etc.
    2. **``runpy`` fallback** — for modules without a ``main()`` function
       but with an ``if __name__ == "__main__":`` block (e.g. a thin
       ``__main__.py`` wrapper).

    The distinction between "the module itself is missing" and "a
    transitive dependency is missing" is preserved in the error
    reporting: a missing module points the operator at the dispatch
    table; a missing transitive dep points at the ``[extra]`` install
    hint.  See :func:`_report_missing_extra`.

    Args:
        dotted: Dotted module path to run as ``__main__``.
        argv: Arguments presented to the sub-interface (without the
            program name; this function synthesizes ``sys.argv[0]``).
        prog: Friendly name placed in ``sys.argv[0]`` so that help and
            error output from the sub-interface read ``nexusdl web ...``
            rather than ``/usr/bin/python``.

    Returns:
        ``0`` on success, or the exit code the sub-interface raised
        through :class:`SystemExit`.
    """
    old_argv = sys.argv
    sys.argv = [prog, *argv]
    try:
        # Strategy 1 — import and call ``main()`` if present.
        try:
            module = importlib.import_module(dotted)
        except ModuleNotFoundError as exc:
            # ``exc.name == dotted`` → the module itself is missing.
            # ``exc.name`` anything else → a transitive dep is missing.
            return _report_missing_extra(dotted, exc)

        main_fn = getattr(module, "main", None)
        if callable(main_fn):
            code = main_fn()
            return 0 if code is None else int(code)

        # Strategy 2 — fall back to running the module as ``__main__``
        # so its ``if __name__ == "__main__":`` block executes.
        try:
            runpy.run_module(dotted, run_name="__main__", alter_sys=False)
            return 0
        except ModuleNotFoundError as exc:
            return _report_missing_extra(dotted, exc)
    finally:
        # ``finally`` guarantees restoration even on SystemExit /
        # KeyboardInterrupt — no leaked global state across dispatches.
        sys.argv = old_argv


def _consume_web_flags(args: list[str]) -> list[str]:
    """Translate web flags into env vars and remove them from ``args``.

    Returns a **new** list with ``--host <addr>`` and ``--port <n>``
    consumed (translated into ``NEXUSDL_API_HOST`` / ``NEXUSDL_API_PORT``
    and removed from the returned argv).  Unsupported flags
    (``--reload``, ``--workers``) are also removed and reported via a
    hint on stderr — the current web launcher reads from ``Settings``,
    so they cannot take effect through it.

    Other flags are passed through untouched, which matches the previous
    behaviour: the web launcher ignores unknown flags by design.
    """
    out: list[str] = []
    unsupported: list[str] = []

    i = 0
    while i < len(args):
        flag = args[i]
        target_env = _WEB_FLAG_TO_ENV.get(flag)

        if target_env is not None and i + 1 < len(args):
            os.environ[target_env] = args[i + 1]
            i += 2
            continue

        if flag in _WEB_UNSUPPORTED_FLAGS:
            unsupported.append(flag)
            # Also drop the next token if it looks like the flag's value
            # (``--workers 4`` → drop ``4``; ``--reload`` → no value).
            if flag == "--workers" and i + 1 < len(args):
                i += 2
            else:
                i += 1
            continue

        out.append(flag)
        i += 1

    if unsupported:
        sys.stderr.write(
            f"nexusdl web: flag(s) {', '.join(unsupported)} are not "
            f"supported by this launcher.\n"
            f"Use uvicorn directly instead:\n"
            f"    uvicorn nexusdl.interfaces.web:create_app_from_env "
            f"--factory {' '.join(unsupported)} <other-flags>\n"
        )

    return out


# ===========================================================================
# --version / --help
# ===========================================================================

def _print_version() -> int:
    """Print ``nexusdl <version>`` and return 0."""
    print(f"nexusdl {_version_string()}")
    return 0


def _version_string() -> str:
    """Return the package version, or a safe fallback.

    Imports :mod:`nexusdl.version` lazily — a trivial module, but keeping
    the import local means ``import nexusdl.__main__`` costs nothing.
    """
    try:
        from nexusdl.version import __version__ as v
    except Exception:
        return "0.0.0+unknown"
    return str(v)


def _print_help() -> int:
    """Print the top-level help text and return 0."""
    version = _version_string()
    text = f"""NexusDL {version} — multi-site manga download platform.

Usage:
    nexusdl                   Start the CLI (interactive commands).
    nexusdl <command> ...     Run a CLI subcommand (search, download, …).
    nexusdl cli <command>...  Same as above; explicit escape hatch for
                              subcommand names that collide with an
                              interface name.
    nexusdl tui               Launch the Textual TUI.
    nexusdl gui               Launch the desktop GUI (requires the `gui`
                              extra and a display server).
    nexusdl web [flags...]    Start the FastAPI backend + WebSocket server
                              (single worker).

Global options:
    -h, --help, help          Show this help text and exit.
    -V, --version, version    Show the version and exit.

Web flags:
    --host <addr>             Bind address (default: 0.0.0.0).
    --port <n>                Bind port (default: 8000).

For multi-worker production deployments, use uvicorn directly:

    uvicorn nexusdl.interfaces.web:create_app_from_env \\
        --factory --workers 4 --host 0.0.0.0 --port 8000

Environment variables:
    NEXUSDL_API_HOST, NEXUSDL_API_PORT   Equivalent to the flags above.
    NEXUSDL_FRONTEND_URL                 CORS-allowed frontend origin.
    NEXUSDL_LOG_LEVEL                    loguru level (trace…critical).

Extras:
    pip install 'nexusdl[cli]'   Typer CLI + Textual TUI.
    pip install 'nexusdl[gui]'   CustomTkinter desktop GUI.
    pip install 'nexusdl[web]'   FastAPI backend + uvicorn + python-jose.

Documentation:
    https://github.com/Voldigoad-Holdings/nexus-dl
"""
    print(text)
    return 0


# ===========================================================================
# Error reporting
# ===========================================================================

def _report_missing_extra(dotted: str, exc: ModuleNotFoundError) -> int:
    """Report a missing module or missing transitive dependency.

    Two cases, two messages — the distinction matters because the fix is
    different:

    * **Module missing** (``exc.name == dotted``): the dispatch table
      points to a module that does not exist.  That is a
      composition-root bug, not an install issue.  The hint tells the
      operator to check :data:`_DISPATCH` or create the module.
    * **Transitive dependency missing** (``exc.name != dotted``): the
      module exists but imports something that is not installed.  The
      hint points at the relevant ``[extra]``.
    """
    if exc.name == dotted:
        sys.stderr.write(
            f"\nnexusdl: cannot start {dotted!r} — the module does not "
            f"exist in the installed tree.\n"
            f"This is a dispatch-table entry that points to a missing "
            f"module. Either:\n"
            f"  - create it as a thin wrapper, or\n"
            f"  - update the entry in nexusdl.__main__._DISPATCH to "
            f"point at the composition root.\n"
        )
        return 1

    extra = _extra_for(dotted)
    sys.stderr.write(
        f"\nnexusdl: cannot start {dotted!r}: {exc}\n"
        f"Hint: install the matching extras with:\n"
        f"    pip install 'nexusdl[{extra}]'\n"
    )
    return 1


def _extra_for(dotted: str) -> str:
    """Map a dispatch module path to the pip extra that installs it."""
    if dotted.startswith("nexusdl.interfaces.gui"):
        return "gui"
    if dotted.startswith("nexusdl.interfaces.web"):
        return "web"
    if dotted.startswith("nexusdl.interfaces.cli"):
        return "cli"
    return "all"


# ===========================================================================
# Module entrypoint
# ===========================================================================

if __name__ == "__main__":
    raise SystemExit(main())
