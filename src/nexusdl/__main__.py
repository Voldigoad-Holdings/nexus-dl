"""Entry point for ``python -m nexusdl``.

Universal launcher for the NexusDL command-line interface.  It delegates to
:func:`nexusdl.interfaces.cli.cli_entry.main`, which is the CLI composition
root — this file exists purely to make the ``python -m nexusdl`` invocation
work, and to translate low-level failures (missing dependencies, interrupts,
unusual ``SystemExit`` payloads) into the exit codes that POSIX shells,
systemd units, Docker ``ENTRYPOINT`` directives and CI pipelines expect.

It also serves as the reference implementation for the ``nexusdl`` console
script declared in ``pyproject.toml`` under ``[project.scripts]``, which
resolves to ``nexusdl.interfaces.cli.cli_entry:main``.  Both paths reach
exactly the same code — this file adds nothing that the console script
lacks, and the console script adds nothing that this file lacks.

Exit codes
----------
* ``0``   — the CLI ran and reported success.
* ``1``   — the CLI failed, or its dependencies are missing.
* ``130`` — the process was interrupted with ``Ctrl+C`` (128 + SIGINT),
  matching the POSIX convention that shells display as ``"Interrupt"``.

Exit codes the CLI itself produces (``--help`` returning 0, a sub-command
returning 2 for a usage error, …) are honoured unchanged: if the delegated
``main()`` raises ``SystemExit`` with an integer code, it is propagated
as-is.  When it raises ``SystemExit`` with a string payload (a Python idiom
meaning "print this to stderr and exit 1"), the string is printed and the
exit code is normalised to ``1`` — matching Python's own default handler
for that case.

What this file does **not** do
------------------------------
* **No argument parsing.**  ``sys.argv`` is passed through untouched;
  Typer/Click handle it inside ``cli_entry.main()``.
* **No configuration loading.**  ``Settings`` is constructed inside the
  CLI composition root, where the correct precedence
  (``default_config.yaml`` → ``config/config.yaml`` → ``.env`` →
  ``kwargs``) is applied.
* **No asyncio event loop management.**  The core is async-first, and
  ``cli_entry.main()`` is responsible for running the async sub-command
  through ``asyncio.run()``.  Managing the event loop here would fight
  with the loop the CLI already owns.
* **No generic exception handling.**  An unexpected exception is a bug,
  and the user should see the traceback so that a bug report is
  actionable.  Expected failures (``NexusDLError`` and its subclasses)
  are the CLI's responsibility — it catches and renders them cleanly
  before control ever reaches this file.  A traceback surfacing here
  therefore means "the CLI missed a case", which is exactly what the
  operator needs to see.
* **No eager imports.**  The CLI imports Typer, Rich and (for the TUI)
  Textual; deferring the import until ``_run()`` is called keeps
  ``import nexusdl.__main__`` essentially free.  This matters for tools
  that walk the module graph without running the CLI — import-scanning
  linters, ``pyright``'s background analysis, or a deployment script
  that only wants to check "is the CLI installed?".
* **No signal-handling overrides.**  ``SIGINT`` is left to Python's
  default handler (raise ``KeyboardInterrupt``), which is what lets the
  CLI's async tasks cancel cooperatively.  Installing a custom handler
  here would preempt the CLI's own cleanup logic.
"""

from __future__ import annotations

import sys
from typing import Final

__all__ = ["main"]


#: Exit code for a successful run.
_EXIT_SUCCESS: Final[int] = 0

#: Exit code for a failed run, matching ``sys.exit(1)`` and Python's
#: default handler for an uncaught exception.
_EXIT_ERROR: Final[int] = 1

#: Exit code for an interrupted run — ``128 + SIGINT (2)``, the POSIX
#: convention that shells display as ``"Interrupt"``.  Using 130 here
#: means a shell script doing ``python -m nexusdl … || echo "failed"``
#: sees the same code it would see if the process had been killed by a
#: real ``SIGINT``, which is what systemd's ``Restart=on-failure`` and
#: Docker's ``--restart`` policies key on.
_EXIT_INTERRUPTED: Final[int] = 130


def main() -> None:
    """Entry point for ``python -m nexusdl``.

    Wraps :func:`_run` in ``SystemExit`` so that both
    ``python -m nexusdl`` and ``from nexusdl.__main__ import main;
    main()`` terminate the process with the correct exit code.  Any
    exception raised by :func:`_run` propagates unchanged — see the
    module docstring for the rationale.
    """
    raise SystemExit(_run())


def _run() -> int:
    """Run the CLI, translating every expected failure into an exit code.

    Returns:
        The process exit code: :data:`_EXIT_SUCCESS` on success,
        :data:`_EXIT_ERROR` on missing dependencies, :data:`_EXIT_INTERRUPTED`
        on ``KeyboardInterrupt``, or whatever integer code the CLI itself
        raised through ``SystemExit``.
    """
    try:
        from nexusdl.interfaces.cli.cli_entry import main as _cli_main
    except ImportError as exc:
        _report_import_error(exc)
        return _EXIT_ERROR

    try:
        _cli_main()
        return _EXIT_SUCCESS
    except KeyboardInterrupt:
        # A Ctrl+C during CLI startup (before the CLI installs its own
        # handler) or during synchronous sub-commands surfaces here.
        # Async sub-commands catch ``KeyboardInterrupt`` inside
        # ``cli_entry.main`` before it can bubble up, so this branch only
        # fires for the pre-CLI window and for sync commands — both of
        # which are correctly exit-130 conditions.
        return _EXIT_INTERRUPTED
    except SystemExit as exc:
        return _normalise_system_exit(exc)


def _normalise_system_exit(exc: SystemExit) -> int:
    """Extract a numeric exit code from *exc*.

    ``SystemExit`` accepts ``None`` (success), an ``int`` (the exit code
    itself) or any other object (Python prints it to stderr and exits
    with 1).  Typer and Click raise ``SystemExit`` with an ``int`` in
    every documented case, but a plugin or a sub-command that calls
    ``sys.exit("error message")`` produces the string form.  Both are
    handled here so that the process always exits with a coherent code.
    """
    code = exc.code
    if code is None:
        return _EXIT_SUCCESS
    if isinstance(code, int):
        return code
    # Non-int payload — Python's default handler would print it to stderr
    # and exit 1.  Replicate that behaviour so a caller running us under
    # ``subprocess`` sees the same output and exit code it would have
    # seen without this wrapper.
    sys.stderr.write(f"{code}\n")
    return _EXIT_ERROR


def _report_import_error(exc: ImportError) -> None:
    """Print a clear, actionable message when the CLI cannot be imported.

    Two failure modes are distinguished:

    * :class:`ModuleNotFoundError` — either the CLI module itself
      (``nexusdl.interfaces.cli.cli_entry``) or one of its dependencies
      (Typer, Rich, Textual) is not installed.  The fix is almost always
      to reinstall with the ``[cli]`` extra; the message tells the
      operator exactly which command to run.
    * Plain :class:`ImportError` — the module exists but does not export
      ``main``.  This is a **bug in the project**, not an installation
      issue, and the message says so explicitly so that the operator
      files an issue rather than trying to install something that is
      already there.
    """
    if isinstance(exc, ModuleNotFoundError):
        sys.stderr.write(
            "NexusDL could not start: the CLI dependencies are missing.\n"
            f"\nUnderlying error: {exc}\n"
            "\nInstall them with one of:\n"
            "    pip install 'nexusdl[cli]'\n"
            "    pip install -e '.[cli]'\n"
            "\nIf you installed NexusDL from a wheel, the `[cli]` extra "
            "must be\n"
            "requested explicitly.  Headless deployments that only use "
            "the web\n"
            "interface can install `[web]` instead.\n"
        )
        return

    sys.stderr.write(
        "NexusDL could not start: the CLI entry point is missing.\n"
        f"\nUnderlying error: {exc}\n"
        "\nThis is a bug in the installed NexusDL package, not a "
        "configuration\n"
        "issue.  Expected a `main` callable in "
        "`nexusdl.interfaces.cli.cli_entry`.\n"
        "Please report this at "
        "https://github.com/Voldigoad-Holdings/nexus-dl/issues.\n"
    )


if __name__ == "__main__":
    main()
