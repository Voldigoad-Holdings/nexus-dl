"""Parser package — public API and automatic registry population.

Importing :mod:`nexusdl.parsers` is the canonical way to make every shipped
parser available to the application.  The import has two effects:

1. **Public API re-export** — the base class, mixins, factory API,
   exceptions and capability helpers are re-exported so that callers can
   write::

       from nexusdl.parsers import (
           BaseParser,
           register,
           DefaultParserFactory,
           WordPressMadaraMixin,
       )

   instead of reaching into the individual submodules.  Every re-export
   resolves to the same class / function object as its original definition —
   there is no behavioural difference, only a shorter import path.

2. **Automatic registration** — importing the five site sub-packages
   (:mod:`nexusdl.parsers.adult`, :mod:`nexusdl.parsers.custom`,
   :mod:`nexusdl.parsers.en`, :mod:`nexusdl.parsers.fr`,
   :mod:`nexusdl.parsers.kr`) triggers the module-scope
   ``register(site_id, ParserClass)`` call in each parser module.  After
   this import completes, the factory registry is fully populated and
   :class:`~nexusdl.parsers.factory.DefaultParserFactory` can resolve any
   site id declared under ``data/sites.yaml`` with a matching parser class.

   The sub-package imports are wrapped with a noqa comment because they
   are **not** meant to be used at this scope — they exist purely for
   their registration side effect.  Do not remove them.

Import ordering
---------------
The module imports in the order below for a reason:

* ``base`` and ``factory`` are imported first, so that when the site
  sub-packages are imported at the bottom of this file, every parser
  module can resolve ``from nexusdl.parsers.base import BaseParser`` and
  ``from nexusdl.parsers.factory import register`` from ``sys.modules``
  without triggering further (partial) imports of :mod:`nexusdl.parsers`.
* ``mixins`` follows immediately after, so that concrete parsers that
  combine mixins with ``BaseParser`` see the mixins already loaded.
* The site sub-packages come last, because their import is what populates
  the registry and may (in the case of :mod:`nexusdl.parsers.custom`)
  discover and load user-added parser modules dynamically.

Any deviation from this order risks a partial-import cycle in which a
parser module tries to import :mod:`nexusdl.parsers.factory` while
:mod:`nexusdl.parsers` itself is still loading.  Python tolerates that
case, but it makes the import graph harder to reason about — this
module's ordering removes the ambiguity entirely.

On ``reset_registry()`` and module caching
------------------------------------------
The registry is populated **once**, when the parser modules are first
imported.  Python caches loaded modules in ``sys.modules``; a subsequent
``import nexusdl.parsers`` does **not** re-run the module-scope
``register(...)`` calls.

As a result, calling
:func:`~nexusdl.parsers.factory.reset_registry` in a test does **not**
leave the process in a state where the next ``import nexusdl.parsers``
re-registers everything.  Tests that need a repopulated registry must use
:func:`importlib.reload` on the relevant parser module (or on the whole
:mod:`nexusdl.parsers` package).  This caveat is documented on
:func:`~nexusdl.parsers.factory.reset_registry` itself and repeated here
because it is easy to trip over.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Public API — re-exported for convenience.
# ---------------------------------------------------------------------------
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.capabilities import (
    CAPABILITY_NAMES,
    ParserCapabilities,
    ParserCapability,
    infer_capabilities,
    validate_capabilities,
)
from nexusdl.parsers.exceptions import (
    NexusDLPlaywrightError,
    ParserError,
    ParserNotFoundError,
    ParserParseError,
    ParserResponseError,
    SessionCloudflareError,
)
from nexusdl.parsers.factory import (
    DefaultParserFactory,
    get_registered_parser,
    list_registered_parsers,
    register,
    reset_registry,
    unregister,
)
from nexusdl.parsers.mixins import (
    ApiBasedMixin,
    CloudflareMixin,
    FoolSlideMixin,
    JsRenderedMixin,
    MangaThemesiaMixin,
    WordPressMadaraMixin,
)

# ---------------------------------------------------------------------------
# Site sub-packages — imported for their registration side effect.
#
# Each sub-package's ``__init__`` imports every parser module it owns, and
# every parser module calls ``register(site_id, ParserClass)`` at module
# scope.  The net effect of the five imports below is that the factory
# registry contains an entry for every shipped parser.
#
# These imports are deliberately placed *after* the public API re-exports
# above so that ``base`` and ``factory`` are already loaded when a parser
# module tries to import from them.  Do not reorder.
# ---------------------------------------------------------------------------
import nexusdl.parsers.adult  # noqa: F401
import nexusdl.parsers.custom  # noqa: F401
import nexusdl.parsers.en  # noqa: F401
import nexusdl.parsers.fr  # noqa: F401
import nexusdl.parsers.kr  # noqa: F401


__all__ = [
    # Base class
    "BaseParser",
    # Factory API
    "DefaultParserFactory",
    "get_registered_parser",
    "list_registered_parsers",
    "register",
    "reset_registry",
    "unregister",
    # Mixins
    "ApiBasedMixin",
    "CloudflareMixin",
    "FoolSlideMixin",
    "JsRenderedMixin",
    "MangaThemesiaMixin",
    "WordPressMadaraMixin",
    # Capabilities
    "CAPABILITY_NAMES",
    "ParserCapabilities",
    "ParserCapability",
    "infer_capabilities",
    "validate_capabilities",
    # Exceptions
    "NexusDLPlaywrightError",
    "ParserError",
    "ParserNotFoundError",
    "ParserParseError",
    "ParserResponseError",
    "SessionCloudflareError",
]
