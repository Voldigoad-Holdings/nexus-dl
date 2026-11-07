"""NexusDL - multi-site manga, webtoon, comic and doujinshi downloader.

NexusDL is a modular, async-first downloader that fetches manga, webtoons,
comics and doujinshi from sixty-plus supported sites and packages them
locally as CBZ, CB7, PDF, ZIP, CBT archives or plain folders. It ships four
front-ends - CLI, TUI, desktop GUI and web - all built on the same
hexagonal-architecture core.

Package layout
--------------
The top-level package is a **container**, not an aggregator. Every layer
below lives in its own subpackage and is imported through its own
canonical path:

* :mod:`nexusdl.core`       - the domain: models, downloader, packaging,
  library, session, registry, events, config, compliance, exceptions,
  utils. It never imports from the adapter layers (``parsers``,
  ``plugins``, ``interfaces``); it is the innermost hexagon.
* :mod:`nexusdl.parsers`    - the site adapters (adapters secondaires):
  the sixty-plus concrete parsers plus the reusable mixins that most of
  them delegate to. Depends only on :mod:`nexusdl.core`.
* :mod:`nexusdl.plugins`    - the plugin loader and hooks. Depends only on
  :mod:`nexusdl.core`.
* :mod:`nexusdl.interfaces` - the four inbound adapters (CLI, TUI, GUI,
  web). The composition roots live here - this is the only layer allowed
  to import every other layer.
* :mod:`nexusdl.data`       - static configuration (``sites.yaml``,
  ``default_config.yaml``, ``comic_info_template.xml``, translations, ...).
  No Python module of its own beyond ``__init__.py``; never imported as
  code, only loaded as files.

The dependency rules that hold between these layers are documented in
:mod:`nexusdl.core`'s package docstring and are enforced by
``scripts/check_imports.py``.

Why this file is deliberately minimal
-------------------------------------
Every other ``__init__.py`` in the project either re-exports a layer's
public API (e.g. :mod:`nexusdl.parsers`) or installs a specific behaviour
(e.g. :mod:`nexusdl.interfaces` lazy-loads its subpackages). This one does
**not** - and that is intentional. The project's import discipline calls
for exactly one canonical import path per symbol, and the container package
is the last place where re-exporting would be justified:

* **No core re-exports.** ``from nexusdl import Settings`` would be a
  second path for ``from nexusdl.core.config import Settings``. It would
  also pull in ``pydantic-settings``, ``loguru``, ``httpx`` and the whole
  core import graph the moment anyone typed ``import nexusdl`` - a
  non-trivial cost for a symbol the caller might not even want.
* **No subpackage aliases.** ``import nexusdl; nexusdl.core.Settings``
  would seem convenient, but the same access is available via
  ``import nexusdl.core; nexusdl.core.Settings`` - one extra word typed
  once per module, in exchange for keeping this file free of ``__getattr__``
  magic and guaranteeing that ``import nexusdl`` has no side effects.
  Python's own import machinery already makes ``from nexusdl import core``
  resolve to ``nexusdl.core`` without any help from this module.
* **No configuration.** ``Settings`` is built from
  ``data/default_config.yaml`` -> ``config/config.yaml`` -> ``.env`` ->
  kwargs, and that precedence is applied inside
  :mod:`nexusdl.core.config` - never at import time of the container
  package.
* **No logging setup.** :mod:`nexusdl.core.logger` installs the loguru
  handlers when it is imported. Importing it from here would install
  handlers for every process that so much as looks at the package - a
  surprising side effect for a library.
* **No console script entry.** The ``nexusdl`` console script declared in
  ``pyproject.toml`` resolves to
  ``nexusdl.interfaces.cli.cli_entry:main`` directly. The
  ``python -m nexusdl`` invocation goes through :mod:`nexusdl.__main__`,
  which delegates to the CLI composition root. Neither path needs an
  import hook here.

The only thing this module **does** export is :data:`__version__`, because
``import nexusdl; nexusdl.__version__`` is such a universal convention
that leaving it out would surprise every Python developer who tries it.
The version string lives in :mod:`nexusdl.version` - the single source of
truth - and is re-exported here so that ``nexusdl.__version__`` and
``nexusdl.version.__version__`` always agree.

**Import cost.** The one eager import below
(``from nexusdl.version import __version__``) loads
:mod:`nexusdl.version`, a single-constant module with no third-party
dependencies. It is the **only** thing loaded by ``import nexusdl``;
no core symbol, no Pydantic schema, no FastAPI app. A caller that only
wants the version pays for one trivial module load; a caller that needs
more imports the specific subpackage directly and pays for exactly that
subpackage.

Where to go next
----------------
* To use NexusDL programmatically: import a specific subpackage
  (``from nexusdl.core import ...``, ``from nexusdl.parsers import ...``).
* To use the CLI: run ``nexusdl`` or ``python -m nexusdl``.
* To launch the TUI: run ``nexusdl tui``.
* To launch the GUI: run ``nexusdl gui`` (requires the ``gui`` extra).
* To run the web interface programmatically or via a launcher:

  .. code-block:: shell

      # Single worker (dev, staging, or small deployments)
      nexusdl web

      # Multi-worker production (canonical deployment pattern)
      uvicorn nexusdl.interfaces.web:create_app_from_env \\
          --factory --workers 4 --host 0.0.0.0 --port 8000

  .. note::

     The ``create_app_from_env`` factory referenced above must be
     defined at ``nexusdl/interfaces/web/__init__.py`` (or in
     ``backend/main.py`` and re-exported from the package's
     ``__init__``). It takes no arguments and reads the configuration
     from the environment via
     :class:`~nexusdl.core.config.Settings` — that is what makes the
     uvicorn ``--factory`` flag work: uvicorn calls the factory once
     per worker, and each worker builds its own
     :class:`~nexusdl.interfaces.web.backend.dependencies.AppServices`
     container (own connection manager, own rate-limit store, own
     Playwright pool). **A module-level ``app`` instance is
     deliberately not exposed** — sharing an ``AsyncEngine``, a
     connection manager, or a Playwright pool across uvicorn workers
     is unsafe (uvicorn forks, and fork is not async-safe for these
     objects). Deployments that use a WSGI/ASGI server other than
     uvicorn follow the same rule: call the factory in the worker
     process, do not import a pre-built app.

  **Multi-worker caveat.** The per-worker rate-limit store is
  :class:`~nexusdl.interfaces.web.backend.middleware.rate_limit.InMemoryRateLimitStore`
  by default — each worker counts its own requests, so the effective
  global limit is ``N_workers × per_worker_limit``. Deployments that
  need a **global** limit (auth brute-force protection, expensive
  download quotas) must provide a shared store (Redis) via
  :class:`~nexusdl.interfaces.web.backend.middleware.rate_limit.RateLimitConfig`'s
  ``store`` keyword — see
  :class:`~nexusdl.interfaces.web.backend.middleware.rate_limit.RateLimitConfig`.
  Similarly, the default
  :class:`~nexusdl.interfaces.web.backend.security.InMemoryRevocationStore`
  is per-worker; JWT revocation requires a shared store in multi-worker
  deployments. Both are documented in the respective modules.

* To write a plugin: see :mod:`nexusdl.plugins`.
* To add a parser: see :mod:`nexusdl.parsers` and its shipped mixins.
"""

from __future__ import annotations

from nexusdl.version import __version__

__all__ = ["__version__"]
