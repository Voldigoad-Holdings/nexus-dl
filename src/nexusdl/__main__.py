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
via :func:`runpy.run_module` with ``run_name="__main__"``.  That mechanism:

* runs the sub-interface's ``if __name__ == "__main__":`` block,
* sets ``sys.argv`` appropriately (with a clean ``argv[0]``),
* returns when it returns, propagating ``sys.exit`` codes,
* restores ``sys.argv`` on exit (safe to chain dispatches in tests),
* does **not** pollute the current namespace with the sub-interface's
  globals.

Dispatch table
==============

Each interface package is expected to expose a runnable module at the
path listed in :data:`_DISPATCH`:

* ``cli``  → :mod:`nexusdl.interfaces.cli.__main__`     (Typer app)
* ``tui``  → :mod:`nexusdl.interfaces.cli.app`          (Textual app)
* ``gui``  → :mod:`nexusdl.interfaces.gui.__main__`     (CustomTkinter launcher)
* ``web``  → :mod:`nexusdl.interfaces.web.backend.main` (uvicorn launcher)

"Runnable" means the module has an ``if __name__ == "__main__":`` block
(or is a package with a ``__main__.py``) that performs the actual work.
All four follow this contract already — see their respective files for
the exact entry points.

Errors
======

* **Missing extras** (``nexusdl gui`` on a headless server without
  Tkinter): the dispatcher catches the :class:`ImportError` raised by
  :func:`runpy.run_module` and prints a clean hint pointing at the
  relevant ``pip install 'nexusdl[gui]'`` line, then exits 1.
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
#: ``main()``) sees the overrides.
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
        _apply_web_flag_env(rest)
    return _run_module(dotted, argv=rest, prog=prog)


def _run_module(dotted: str, *, argv: list[str], prog: str) -> int:
    """Run ``dotted`` as ``__main__`` with a controlled ``sys.argv``.

    Uses :func:`runpy.run_module` so the sub-interface sees a clean
    ``__name__ == "__main__"`` — the same environment it would see under
    ``python -m <dotted>``.  Restores the original ``sys.argv`` on exit
    so callers can chain dispatches in tests.

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
        runpy.run_module(dotted, run_name="__main__", alter_sys=False)
        return 0
    except ImportError as exc:
        return _report_missing_extra(dotted, exc)
    finally:
        # ``finally`` guarantees restoration even on SystemExit /
        # KeyboardInterrupt — no leaked global state across dispatches.
        sys.argv = old_argv


def _apply_web_flag_env(args: list[str]) -> None:
    """Translate a small subset of uvicorn-style flags into env vars.

    Only the flags in :data:`_WEB_FLAG_TO_ENV` are translated.  The two
    flags in :data:`_WEB_UNSUPPORTED_FLAGS` (``--reload`` and
    ``--workers``) are silently consumed and produce a hint pointing at
    the correct uvicorn invocation, since supporting them would require
    the ``--factory`` form (see the module docstring).

    Other flags fall through untouched — the current web launcher reads
    from ``Settings``, so they are effectively ignored, which is the same
    behaviour as before this dispatcher existed.
    """
    i = 0
    unsupported: list[str] = []
    while i < len(args):
        flag = args[i]
        target_env = _WEB_FLAG_TO_ENV.get(flag)
        if target_env is not None and i + 1 < len(args):
            os.environ[target_env] = args[i + 1]
            i += 2
            continue
        if flag in _WEB_UNSUPPORTED_FLAGS:
            unsupported.append(flag)
        i += 1

    if unsupported:
        sys.stderr.write(
            f"nexusdl web: flag(s) {', '.join(unsupported)} are not supported "
            f"by this launcher.\n"
            f"Use uvicorn directly instead:\n"
            f"    uvicorn nexusdl.interfaces.web:create_app_from_env "
            f"--factory {' '.join(unsupported)} <other-flags>\n"
        )


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

def _report_missing_extra(dotted: str, exc: ImportError) -> int:
    """Print a clean hint when an interface extra is not installed."""
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
