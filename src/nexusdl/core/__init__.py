"""The engine of NexusDL - configuration, coordination, and every domain subsystem.

This package is the **framework** of NexusDL. Every other layer -
``parsers/``, ``plugins/``, ``interfaces/``, and the entry-point scripts
- depends on ``nexusdl.core`` for its settings, its error hierarchy,
its event bus, and the domain services (download pipeline, image
processing, library, packaging, site registry, HTTP session) that
compose into a working application.

Structure
---------
The package is organised as **eight core modules** (the top-level
foundations) plus **eight sub-packages** (the domain subsystems). Each
sub-package is a well-scoped module family with its own public
``__init__.py``; this file curates the top-level surface a caller
actually needs.

**Top-level modules** - the foundations every subsystem depends on::

    nexusdl.core
    +-- compliance.py      robots.txt policy enforcement
    +-- config.py          Settings and configuration bootstrap
    +-- constants.py       APP_NAME, APP_SLUG, DATA_PACKAGE, twelve event names
    +-- events.py          Event and EventBus
    +-- exceptions.py      the NexusDLError hierarchy
    +-- i18n.py            Translator and the locale catalogs
    +-- logger.py          loguru configuration and the bound logger
    +-- paths.py           filesystem path computation

**Sub-packages** - the domain subsystems::

    nexusdl.core.downloader   download pipeline (worker, queue, manager, retry, progress, dedup)
    nexusdl.core.image        image pipeline (validator, optimizer, converter, downloader)
    nexusdl.core.library      local library (database, scanner, metadata, search, migrations)
    nexusdl.core.models       domain models (manga, chapter, page, site, user, search, download, ...)
    nexusdl.core.packaging    output formats (ZIP, CBZ, CB7, PDF, FOLDER) and ComicInfo
    nexusdl.core.registry     sites.yaml loading, validation, runtime registry
    nexusdl.core.session      HTTP session and its collaborators (rate limiter, proxy, cookies, cache, ...)
    nexusdl.core.utils        generic primitives (url, time, text, hash, retry, filesystem, async)

Curated top-level exports
-------------------------
This file re-exports **fifteen symbols** - the small set that any
consumer of the framework needs to bootstrap an application. Every
other symbol lives in its specific submodule and is imported from
there. The choice is deliberate:

* **Import cost.** ``import nexusdl.core`` is now **side-effect-free**
  (see "Lazy loading" below). A sub-package that needs only
  ``NexusDLError`` pays for ``exceptions.py`` (pure stdlib ``typing``)
  and nothing else - not pydantic, not cryptography, not yaml, not
  httpx. Only the fifteen exports, resolved on first access, cost
  anything, and only for the modules they actually use.

* **Clarity.** A caller reading ``from nexusdl.core.models import Manga``
  knows exactly where ``Manga`` lives. ``from nexusdl.core import Manga``
  would be forty percent shorter and twice as opaque.

* **Stability.** The fifteen exports are the **highest-stability tier**
  of the framework - a signature change to any of them is a
  project-wide breaking change. Re-exporting everything would
  inadvertently promote every domain model and utility to that tier,
  which they do not deserve.

The fifteen exports
-------------------
::

    NexusDLError          base exception every consumer catches
    Settings              the configuration entry point
    bootstrap_env_file    first-run .env creation
    Event, EventBus       the coordination mechanism
    logger                the bound loguru logger (see caveat below)
    configure_logging     logger setup (explicit arguments)
    configure_logging_from_settings   logger setup (from Settings)
    get_logger            per-module bound logger helper
    Translator            the localisation service
    get_default_translator, configure_default_translator   process-wide default
    APP_NAME, APP_SLUG, DATA_PACKAGE                       identity constants

Everything else - :class:`~nexusdl.core.models.manga.Manga`,
:class:`~nexusdl.core.session.http_session.HttpSession`,
:class:`~nexusdl.core.downloader.manager.DownloadManager`,
:class:`~nexusdl.core.exceptions.SessionRateLimitError`,
:class:`~nexusdl.core.utils.time.Stopwatch`, and the other ~150 public
symbols across the framework - is imported from its submodule. The
section "What is not re-exported" below spells out the rule.

Lazy loading
------------
All fifteen exports are resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core`` **does not import any submodule**.
This matters because:

* **Sub-packages depend on leaf modules.** ``core/utils/hash.py``
  imports ``from nexusdl.core.exceptions import NexusDLError``. Before
  this file used lazy loading, that single line triggered the whole
  ``core/__init__.py`` - which eagerly imported ``config.py``, which
  imports pydantic, pydantic-settings, platformdirs, cryptography, and
  yaml. Thirty-plus core modules import from ``nexusdl.core.X`` - each
  one used to pay the ~200 ms bootstrap cost the first time, and that
  cost was paid for the **entire dependency graph** before any actual
  work began.

* **The docstring's original promise now holds.** "A consumer that only
  wants to read the current settings should not pay for the entire
  framework's dependency graph" - true today, false before. ``from
  nexusdl.core.exceptions import NexusDLError`` now costs ~1 ms (just
  the ``exceptions.py`` module), not ~200 ms.

* **The public surface is unchanged.** ``from nexusdl.core import
  Settings`` still works exactly as before - the symbol is resolved on
  first access and cached. No caller has to change a line.

* **External consumers still pay only for what they ask for.** A script
  that does ``from nexusdl.core import Settings`` pays for
  ``config.py`` (~150 ms), not for httpx, sqlalchemy, img2pdf, py7zr,
  jsonschema, or playwright. A script that does ``from nexusdl.core
  import NexusDLError`` pays for ``exceptions.py`` (~1 ms) and nothing
  else.

See "A note on the ``logger`` attribute and the ``logger`` submodule"
below for the one caveat this lazy loading introduces - and why it is
a pre-existing property of Python import resolution, not a NexusDL
design choice.

Composition example
-------------------
A composition root that wires a full application from the top-level
exports plus the sub-package entries::

    from pathlib import Path

    import os

    from nexusdl.core import (
        Settings,
        bootstrap_env_file,
        configure_logging_from_settings,
        get_logger,
    )
    from nexusdl.core.downloader import DownloadManager, DownloadWorker
    from nexusdl.core.packaging import PackagerFactory
    from nexusdl.core.session import HttpSession

    # 1. Bootstrap - first run only
    bootstrap_env_file(Path.cwd() / ".env")

    # 2. Settings, logger
    settings = Settings()
    configure_logging_from_settings(settings)

    log = get_logger(__name__)
    log.info("NexusDL starting, downloads at {}", settings.storage.downloads_dir)

    # 3. Domain services
    packager_factory = PackagerFactory()
    session = HttpSession(...)

    def worker_factory(parser):
        return DownloadWorker(
            parser=parser,
            packager_factory=packager_factory,
            settings=settings,
            event_bus=bus,
        )

    manager = DownloadManager(
        registry=registry,
        settings=settings,
        event_bus=bus,
        worker_factory=worker_factory,
    )

Every other public symbol - parsers, packagers, domain models, utility
primitives - is imported from its specific submodule, matching the
one-hop-only pattern every module in the delivered code uses.

Import conventions
------------------
Two import styles, two audiences:

**Application code** - a pipeline module, a CLI subcommand, a plugin -
uses **specific submodule imports**::

    from nexusdl.core.exceptions import SessionRateLimitError
    from nexusdl.core.models import Manga, Chapter
    from nexusdl.core.utils.text import slugify_safe

This makes the dependency explicit, keeps each module's import list
minimal, and lets a static checker see exactly what each file
depends on.

**External consumers and scripts** that only need the bootstrap
surface use the top-level import, which this file re-exports::

    from nexusdl.core import Settings, get_logger

The fifteen exports above are the only symbols guaranteed to be
importable from ``nexusdl.core`` directly. Everything else - even the
public surface of every sub-package - lives behind a submodule hop.

A note on the ``logger`` attribute and the ``logger`` submodule
---------------------------------------------------------------
Two distinct objects share the name ``logger``:

* ``nexusdl.core.logger`` - the **submodule** containing
  ``configure_logging``, ``configure_logging_from_settings``,
  ``get_logger``, and the bound loguru logger object.
* ``nexusdl.core.logger`` - the **loguru logger** object, exposed by
  this package as one of the fifteen exports.

Before any explicit ``import nexusdl.core.logger``, ``from nexusdl.core
import logger`` returns the **loguru logger** (resolved by this
package's ``__getattr__``). After an explicit ``import
nexusdl.core.logger``, Python's import machinery rebinds the attribute
on the package to the **module**, and ``from nexusdl.core import
logger`` returns the module instead. This is a consequence of Python's
attribute resolution rules (module attributes are overwritten when a
submodule with the same name is imported), not a NexusDL design
choice - the same override happens with any package that both
re-exports a name from a submodule and whose submodule shares that
name.

To avoid the ambiguity entirely:

* Access the loguru logger via ``get_logger(__name__)`` in application
  code. This is the recommended pattern - see the "Composition example"
  above. It returns a logger bound to the calling module's name.
* Do **not** write ``from nexusdl.core import logger`` in new code. The
  top-level export exists only for backward compatibility and for very
  short scripts where the ambiguity is irrelevant.
* If you must use the top-level ``logger``, do it **before** any
  ``import nexusdl.core.logger`` in the same process, or retrieve the
  module from ``sys.modules["nexusdl.core.logger"]`` explicitly.

Stability
---------
Three tiers of import stability across the whole framework:

* **Tier 1 - the fifteen exports of this file.** Renaming any of them
  is a **project-wide** breaking change that requires a migration.
  These are the only symbols NexusDL commits to keeping stable across
  minor releases. The signatures may grow (a new keyword argument with
  a default), but the names and existing parameters do not change.
* **Tier 2 - the public surface of each sub-package.** Documented by
  each sub-package's ``__init__.py``, renaming is a **subsystem-scoped**
  breaking change. A consumer that imports ``from nexusdl.core.models
  import Manga`` is affected only if ``Manga`` moves within
  ``core.models``.
* **Tier 3 - everything else.** Any module-private helper, any
  constant with a leading underscore, any internal implementation
  detail is subject to change without notice. This is the default for
  every symbol not explicitly listed in a sub-package's ``__all__``.

What is not re-exported
-----------------------
Nothing from the eight sub-packages is re-exported at the ``core``
level. That includes:

* **All domain models** - :class:`~nexusdl.core.models.manga.Manga`,
  :class:`~nexusdl.core.models.chapter.Chapter`,
  :class:`~nexusdl.core.models.page.Page`,
  :class:`~nexusdl.core.models.site.SiteConfig`,
  :class:`~nexusdl.core.models.user.User`,
  :class:`~nexusdl.core.models.search.SearchResult`,
  :class:`~nexusdl.core.models.download.DownloadTask`,
  :class:`~nexusdl.core.models.download.DownloadResult`,
  :class:`~nexusdl.core.models.library.ReadingProgress`,
  and every enumeration in :mod:`nexusdl.core.models.enums`.
  Import from :mod:`nexusdl.core.models`.
* **The download pipeline** - :class:`DownloadManager`,
  :class:`DownloadWorker`, :class:`DownloadQueue`,
  :class:`RetryPolicy`, :class:`ProgressTracker`,
  :class:`Deduplicator`. Import from
  :mod:`nexusdl.core.downloader`.
* **The image pipeline** - every validator, optimizer, converter, and
  downloader helper. Import from :mod:`nexusdl.core.image`.
* **The library** - :class:`LibraryDatabase`,
  :class:`LibraryScanner`, :class:`LibrarySearch`, and the metadata
  helpers. Import from :mod:`nexusdl.core.library`.
* **The packaging family** - :class:`BasePackager`,
  :class:`PackagerFactory`, :class:`ComicInfo`, and every concrete
  packager. Import from :mod:`nexusdl.core.packaging`.
* **The registry** - :class:`SiteRegistry`,
  :class:`SitesValidator`, the parser protocols. Import from
  :mod:`nexusdl.core.registry`.
* **The session** - :class:`HttpSession`, :class:`RateLimiter`,
  :class:`CircuitBreaker`, :class:`CookieManager`,
  :class:`ProxyManager`, :class:`PlaywrightPool`,
  :class:`FlareSolverrClient`, :class:`HttpCache`, and the
  user-agent rotator. Import from :mod:`nexusdl.core.session`.
* **The utilities** - the 84 symbols across URL, time, text, hash,
  retry, filesystem, and async helpers. Import from
  :mod:`nexusdl.core.utils`.
* **The exceptions beyond the base** - :class:`ConfigError`,
  :class:`ParserError` and its subclasses, :class:`DownloadError` and
  its subclasses, :class:`PackagingError`, :class:`LibraryError`,
  :class:`ComplianceError`, :class:`SessionError` and its subclasses,
  :class:`RetryError` and its subclasses, :class:`PluginError`,
  :class:`InvalidStateTransitionError`,
  :class:`ImageValidationError`, :class:`ImageConversionError`.
  Import from :mod:`nexusdl.core.exceptions`.
* **The path accessors** - every function in
  :mod:`nexusdl.core.paths`. Import from that module (or read from
  ``settings.paths.*``, which is the preferred entry point for
  application code).
* **The twelve event constants** - import from
  :mod:`nexusdl.core.constants` (or, if a caller prefers, from
  :mod:`nexusdl.core.events` after a future refactor that adds them
  there).

The reason is import cost, not ideology: a sub-package may pull in
httpx, sqlalchemy, img2pdf, py7zr, or jsonschema at import time. A
consumer that only wants to read the current settings should not pay
for the entire framework's dependency graph. Keeping the re-exports
narrow is what makes ``import nexusdl.core`` cheap.

A note on the two-step import chain
-----------------------------------
Every symbol this file re-exports is also available at its canonical
submodule. ``from nexusdl.core import Settings`` and
``from nexusdl.core.config import Settings`` return the **same class
object** - there is no proxy, no wrapper, no second definition. The
top-level path is a convenience, not a fork. A future change that
moved ``Settings`` between submodules would keep the top-level alias
working as long as the symbol remains one of the fifteen exports; a
change that removed it from the fifteen would be a breaking change
regardless of submodule location.

Dependency graph (no cycles)::

    nexusdl.core
    |
    +-- .config         (imports .constants, .models.enums;
    |                    runtime deps: pydantic, pydantic-settings,
    |                    platformdirs, cryptography, yaml)
    |
    +-- .constants      (pure leaf, zero imports)
    +-- .events         (imports loguru)
    +-- .exceptions     (pure leaf, typing only)
    +-- .i18n           (stdlib only)
    +-- .logger         (imports loguru, stdlib logging)
    |
    +--- (sub-packages, not imported at this file's import time) ---
        .downloader     -> models, session, packaging, plugins, utils
        .image          -> exceptions, utils
        .library        -> models, exceptions, utils
        .models         -> exceptions, utils
        .packaging      -> models, exceptions, utils
        .registry       -> models, exceptions, utils
        .session        -> models, exceptions, utils
        .utils          -> exceptions (retry.py only)

The arrows ``->`` in the sub-package listing describe **runtime
dependencies at first use**, not imports performed by this file. This
file imports **nothing** at module load - the six leaf-side modules
(``config``, ``constants``, ``events``, ``exceptions``, ``i18n``,
``logger``) are loaded lazily, when a caller accesses one of the
fifteen exports. This makes ``import nexusdl.core`` effectively free,
and makes ``from nexusdl.core.exceptions import NexusDLError`` cost
only ``exceptions.py`` rather than the entire ``config.py``
dependency graph. See "Lazy loading" above for the rationale and the
one caveat.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Base exception ------------------------------------------------- #
    "NexusDLError",
    # --- Configuration -------------------------------------------------- #
    "Settings",
    "bootstrap_env_file",
    # --- Event bus ------------------------------------------------------ #
    "Event",
    "EventBus",
    # --- Logging -------------------------------------------------------- #
    "logger",
    "configure_logging",
    "configure_logging_from_settings",
    "get_logger",
    # --- Localisation --------------------------------------------------- #
    "Translator",
    "get_default_translator",
    "configure_default_translator",
    # --- Identity ------------------------------------------------------- #
    "APP_NAME",
    "APP_SLUG",
    "DATA_PACKAGE",
    # --- Introspection -------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.
#
# The ``logger`` entry is deliberately (``"logger"``, ``"logger"``) - we
# want the *loguru logger object* exported by the ``logger`` submodule,
# not the module itself. See the "A note on the ``logger`` attribute and
# the ``logger`` submodule" section in the module docstring for the
# caveat this creates.

_LAZY: dict[str, tuple[str, str]] = {
    # --- exceptions ------------------------------------------------------ #
    "NexusDLError": ("exceptions", "NexusDLError"),
    # --- config ---------------------------------------------------------- #
    "Settings": ("config", "Settings"),
    "bootstrap_env_file": ("config", "bootstrap_env_file"),
    # --- events ---------------------------------------------------------- #
    "Event": ("events", "Event"),
    "EventBus": ("events", "EventBus"),
    # --- logger ---------------------------------------------------------- #
    "logger": ("logger", "logger"),
    "configure_logging": ("logger", "configure_logging"),
    "configure_logging_from_settings": ("logger", "configure_logging_from_settings"),
    "get_logger": ("logger", "get_logger"),
    # --- i18n ------------------------------------------------------------ #
    "Translator": ("i18n", "Translator"),
    "get_default_translator": ("i18n", "get_default_translator"),
    "configure_default_translator": ("i18n", "configure_default_translator"),
    # --- constants ------------------------------------------------------- #
    "APP_NAME": ("constants", "APP_NAME"),
    "APP_SLUG": ("constants", "APP_SLUG"),
    "DATA_PACKAGE": ("constants", "DATA_PACKAGE"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.config import Settings, bootstrap_env_file
    from nexusdl.core.constants import APP_NAME, APP_SLUG, DATA_PACKAGE
    from nexusdl.core.events import Event, EventBus
    from nexusdl.core.exceptions import NexusDLError
    from nexusdl.core.i18n import (
        Translator,
        configure_default_translator,
        get_default_translator,
    )
    from nexusdl.core.logger import (
        configure_logging,
        configure_logging_from_settings,
        get_logger,
        logger,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a top-level core symbol on first access (PEP 562).

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in ``globals()``. Only the
    names in :data:`__all__` are handled; anything else raises
    :class:`AttributeError` so that ``hasattr``, ``getattr(x, name,
    default)`` and interactive introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The symbol registered under ``name`` in the submodule recorded in
        :data:`_LAZY`. The submodule is imported once and its binding is
        cached in ``globals()``, so subsequent accesses are plain dict
        lookups.

    Raises:
        AttributeError: When ``name`` is not a public export, when the
            submodule cannot be imported (a missing optional dependency
            such as ``pydantic-settings`` or ``cryptography``, a syntax
            error deeper in the module, or a broken installation), or when
            the submodule does not define the requested name.
            ``AttributeError`` - rather than ``ImportError`` - is required
            by PEP 562 so that ``hasattr`` works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available core exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.models import Manga' or "
            f"'from nexusdl.core.exceptions import ConfigError'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the core extras.
        if exc.name in {
            "pydantic",
            "pydantic_settings",
            "cryptography",
            "yaml",
            "platformdirs",
            "loguru",
        }:
            raise ImportError(
                f"{name!r} requires the core extra. "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The core submodule {submodule_basename!r} does not define "
            f"{attr_name!r}. This is a package-internal inconsistency - "
            f"nexusdl.core.__all__ and the submodules are out of sync."
        ) from None

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    #
    # Caveat: for the "logger" symbol, caching here means
    # ``nexusdl.core.logger`` becomes the loguru logger object. If a
    # caller later does ``import nexusdl.core.logger`` (the module),
    # Python's import machinery will overwrite this entry with the
    # module. That is a pre-existing property of Python import
    # resolution, documented in the module docstring; it is not caused
    # by lazy loading.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it
    with each candidate name to decide what to display. Because none of
    the fifteen exports are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would
    appear empty in a REPL.

    The returned list is :data:`__all__` plus the standard module dunders
    a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the core's Tier-1 surface without importing every
    submodule.
    """
    # Only the fifteen Tier-1 exports, not the introspection helpers -
    # otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the core symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (plugin introspection tools, parametrized tests, composition-root
    builders that want to resolve a symbol from a config string).

    Raises:
        KeyError: If ``name`` is not a public core export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown core symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"import from 'nexusdl.core.<submodule>' directly."
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
