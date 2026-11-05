"""Public surface of the NexusDL command-line interface package.

This package is the **CLI and TUI interface layer** — the adapters that
turn the framework in :mod:`nexusdl.core` into a usable, interactive
terminal experience. It is divided into four sub-systems, each with its
own composition root and its own collaborators:

* :mod:`~nexusdl.interfaces.cli.cli_entry` — the **CLI entry point and
  composition root**. Defines the root :class:`typer.Typer` application
  (:data:`~nexusdl.interfaces.cli.cli_entry.app`), the lazy
  :class:`~nexusdl.interfaces.cli.cli_entry.CLIState` container that
  builds every service the CLI commands need, and the
  :func:`~nexusdl.interfaces.cli.cli_entry.main` function invoked by the
  ``nexusdl`` console script.

* :mod:`~nexusdl.interfaces.cli.commands` — the **command groups**.
  Seven sub-:class:`typer.Typer` instances (``search_app``,
  ``download_app``, ``library_app``, ``config_app``, ``sites_app``,
  ``plugins_app``, ``admin_app``), registered on the root app by
  ``cli_entry``.

* :mod:`~nexusdl.interfaces.cli.app` — the **Textual TUI application**.
  The :class:`~nexusdl.interfaces.cli.app.NexusDLApp` subclass, the
  internal ``_TuiLogRouter`` that feeds the live log viewer, and the
  :func:`~nexusdl.interfaces.cli.app.run_app` async entry point invoked
  by ``cli_entry._run_tui_async``.

* :mod:`~nexusdl.interfaces.cli.config` — the **TUI's own config**.
  :class:`~nexusdl.interfaces.cli.config.TuiConfig` and
  :func:`~nexusdl.interfaces.cli.config.build_tui_config`, distinct from
  the application-wide :class:`~nexusdl.core.config.Settings`.

Plus two sub-packages, each with its own public ``__init__.py``:

* :mod:`~nexusdl.interfaces.cli.screens` — the full-screen destinations
  the TUI user navigates between (``MainScreen``, ``SearchScreen``, …).
* :mod:`~nexusdl.interfaces.cli.widgets` — the reusable widgets those
  screens compose (``SiteSelector``, ``ProgressBar``, …).

There is one non-Python file at this level: :file:`theme.tcss`, the
Textual CSS stylesheet loaded by
:attr:`~nexusdl.interfaces.cli.app.NexusDLApp.CSS_PATH`. Its resolution
is documented in :mod:`~nexusdl.interfaces.cli.app`.

Why this ``__init__.py`` re-exports nothing
-------------------------------------------
The natural convenience pattern — ``from nexusdl.interfaces.cli import
main`` — would create a **bootstrap-ordering trap**: importing
``nexusdl.interfaces.cli`` would trigger ``cli_entry.py``'s import of
:mod:`typer`, every core module, and
:mod:`nexusdl.interfaces.cli.commands` (which itself imports
:mod:`rich`, etc.), **before** any symbol from the package could be
used. That is fine for a caller running the CLI, but it is a real cost
for a caller that only wants, say,
:class:`~nexusdl.interfaces.cli.config.TuiConfig` — they would pay for
``typer`` and the whole CLI command tree just to reach a six-field
dataclass.

Re-exporting nothing keeps **every submodule independently importable**
without triggering the others:

* ``from nexusdl.interfaces.cli.config import TuiConfig`` — imports
  only :mod:`textual.theme`, :mod:`loguru`, and
  :mod:`nexusdl.core.constants`.
* ``from nexusdl.interfaces.cli.app import run_app`` — imports
  :mod:`textual` and the TUI screens/widgets, but **not** ``typer``.
* ``from nexusdl.interfaces.cli.cli_entry import main`` — imports
  ``typer``, the CLI commands, and every core module, but **not**
  :mod:`textual`.

The two sub-packages :mod:`~nexusdl.interfaces.cli.screens` and
:mod:`~nexusdl.interfaces.cli.widgets` **do** re-export their full
public surface. They are leaves, and their imports are cheap (a handful
of :mod:`textual` modules each). The asymmetry is deliberate and matches
the discipline the rest of the project uses: :mod:`nexusdl.core`
re-exports only its fifteen bootstrap symbols, each of its sub-packages
re-exports its full public surface, and every consumer decides its own
import cost by choosing the submodule it imports.

Consequence: ``from nexusdl.interfaces.cli import *`` imports **nothing**.
Use ``import nexusdl.interfaces.cli`` (safe and cheap — executes this
docstring only) as a namespace anchor, or import from a specific
submodule.

Adding a new command
--------------------
The pattern is established:

1. Define the command in a suitable sub-``Typer`` in
   :mod:`~nexusdl.interfaces.cli.commands`, or add a new sub-``Typer``
   if the command belongs to a new group.
2. If the sub-``Typer`` is new, register it on the root app in
   :mod:`~nexusdl.interfaces.cli.cli_entry` via ``app.add_typer``.
3. Update this file's docstring's sub-system list if the new group
   deserves a mention (most do not — the list stays short on purpose).

No change to this ``__init__.py`` is required for either step: the
submodule files are the ones that change. The sub-packages
:mod:`screens` and :mod:`widgets` follow the same rule for their own
content.

Dependency graph (no cycles)::

    nexusdl.interfaces.cli
    ├── cli_entry  → typer, all core.*, .commands
    ├── commands   → typer, rich, all core.*
    ├── app        → textual, .config, .screens.main, .widgets.log_viewer
    ├── config     → textual.theme, loguru, core.constants
    ├── screens    → textual, .widgets, core.*
    └── widgets    → textual, core.*

The submodules are **independent at the module level** — no submodule
imports a sibling except ``cli_entry → commands`` and
``app → config / screens / widgets``. Both of those are one-directional
and match the composition-root pattern: the entry point is the only
authority that assembles its collaborators.
"""

from __future__ import annotations


__all__: list[str] = []
