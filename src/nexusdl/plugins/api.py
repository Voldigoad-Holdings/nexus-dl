"""Public contract for NexusDL plugins — the :class:`NexusPlugin` protocol
and the :class:`PluginContext` handed to every plugin at load time.

This module is the **single source of truth** for the plugin interface at
the type level:

* :class:`PluginContext` — the frozen bundle of framework services passed to
  ``on_load``. A plugin never instantiates its own logger, settings, event
  bus, or HTTP session; it receives them here and stores what it needs.
* :class:`NexusPlugin` — a structural :class:`typing.Protocol` describing
  the **mandatory** members every plugin must declare. The four optional
  pipeline hooks are documented below but are **not** declared on the
  protocol itself, because a ``Protocol`` cannot express "optional method"
  and requiring them would force every minimal plugin to write no-op stubs.

Why the protocol is minimal
---------------------------
The loader decides which optional hooks to dispatch by reading the plugin's
manifest (``nexus.plugin.yaml``, field ``hooks:``) and then calling only
those. This means a plugin that implements only ``on_load`` and ``on_unload``
is perfectly valid at runtime **and** satisfies the protocol at type-check
time. It also means :func:`nexusdl.plugins.hooks.verify_protocol_consistency`
can assert a precise, stable relationship between this module and
:mod:`nexusdl.plugins.hooks` — see that function's docstring for the exact
invariants.

Why the protocol is not ``@runtime_checkable``
----------------------------------------------
:class:`typing.Protocol` with data members (here ``name`` and ``version``)
raises ``TypeError`` when ``issubclass()`` is called against it, and
``isinstance()`` only checks the presence of members on *instances* — not on
*classes* — which is not what the loader needs (it inspects the class before
instantiation). The loader therefore performs an explicit structural check
(:func:`nexusdl.plugins.loader._find_plugin_class`) and reports precise
errors. Static conformance is enforced by ``mypy --strict`` on each plugin
author's code.

Why ``name`` and ``version`` are ``ClassVar``
---------------------------------------------
Both attributes are declared ``ClassVar[str]`` because they are class-level
constants, not instance state:

* The loader's discovery step (:func:`_find_plugin_class`) inspects the
  class *before* instantiation — ``hasattr(plugin_cls, "name")`` must be
  ``True``. A plugin that assigns ``self.name = "…"`` in ``__init__``
  fails discovery, and ``ClassVar`` is what makes that failure visible to
  ``mypy --strict`` at the plugin author's code, not just at runtime.
* The contract says the values are immutable — they must match the
  manifest, and the manifest is validated once. Instance-level mutation
  would break the loader's assumption that a plugin's identity cannot
  change after ``on_load``.

See :mod:`nexusdl.plugins.hooks` for the canonical list of hook names and
their sync/async flavour. This module does not duplicate that information;
it only declares the two mandatory hook signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from loguru import Logger

    from nexusdl.core.config import Settings
    from nexusdl.core.events import EventBus
    from nexusdl.core.models.download import DownloadResult, DownloadTask
    from nexusdl.core.models.page import Page
    from nexusdl.core.session.http_session import HttpSession


__all__ = ["NexusPlugin", "PluginContext"]


# --------------------------------------------------------------------------- #
# Plugin context                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Immutable bundle of framework services handed to each plugin by the loader.

    The context is created once, immediately before ``on_load`` is called,
    and lives for the plugin's entire lifetime. Every field references a live
    service owned by the framework — a plugin never instantiates its own
    ``HttpSession``, ``EventBus``, or ``Settings``; it reuses these.

    The dataclass is ``frozen=True`` to make the read-only contract explicit:
    a plugin that needs mutable state stores it on itself, not on the context.
    The ``slots=True`` decorator keeps the object small (a plugin may hold a
    reference to it forever, but the object itself has no ``__dict__``).

    Adding a field here is a **breaking change** for every plugin author who
    type-annotates against this class, so the set of fields is deliberately
    minimal: only what a plugin cannot reasonably obtain otherwise.

    Attributes:
        logger: A :mod:`loguru` logger already bound to ``plugin=<name>``, so
            every line the plugin emits is tagged with its origin. Plugins
            must use this logger; they must not create a bare ``logger = ...``
            of their own.
        settings: The application-wide :class:`~nexusdl.core.config.Settings`.
            Plugins read configuration from here; they never import ``.env``
            or ``sites.yaml`` directly, and they never mutate ``Settings``.
        event_bus: The shared :class:`~nexusdl.core.events.EventBus`. Plugins
            that emit custom events must namespace them
            (``plugin.<name>.<event>``) to avoid collisions with framework
            events (``download.*``, ``chapter.*``, ``library.*``, …).
        plugin_dir: The directory the plugin was loaded from — read-only by
            convention. Use it to locate bundled data files (templates,
            locales, schemas, …).
        data_dir: A writable per-plugin directory under
            ``<data_root>/<plugin_name>/``, guaranteed to exist before
            ``on_load`` is called. This is the **only** path a plugin may
            write to; writing anywhere else is a bug.
        http_session: The shared HTTP session, or ``None`` in contexts
            without network access (e.g. an offline TUI settings screen).
            Plugins that perform network I/O reuse this session rather than
            opening their own client, so shared rate limiting, caching,
            circuit breaking, and cookie management all apply to their
            requests too. **A plugin that declares network dependencies in
            its manifest should check this field is not ``None`` at the top
            of ``on_load`` and raise a clear error if it is** — a
            ``NoneType`` ``AttributeError`` deep inside a request is much
            harder to diagnose than an explicit ``PluginError`` at startup.
    """

    logger: Logger
    settings: Settings
    event_bus: EventBus
    plugin_dir: Path
    data_dir: Path
    http_session: HttpSession | None = None


# --------------------------------------------------------------------------- #
# Plugin protocol                                                             #
# --------------------------------------------------------------------------- #


class NexusPlugin(Protocol):
    """Structural contract for every NexusDL plugin.

    A class *is* a plugin (structurally) when it declares:

    * the two class attributes :attr:`name` and :attr:`version`, matching the
      values in ``nexus.plugin.yaml``;
    * the two synchronous lifecycle hooks :meth:`on_load` and
      :meth:`on_unload`.

    The four optional pipeline hooks are **not** part of this protocol:

    * :meth:`on_before_download` — async, transform a task before download
    * :meth:`on_after_download` — async, transform a completed result
    * :meth:`on_page_downloaded` — async, observe each page written to disk
    * :meth:`on_error` — async, observe a task failure

    A plugin opts into any subset of them by listing their names in the
    ``hooks:`` field of its manifest. The loader dispatches only to plugins
    that opted in. See :mod:`nexusdl.plugins.hooks` for the canonical list
    of hook names and their sync/async flavour.

    Inheritance from this protocol is **not** required; the contract is
    purely structural. The canonical reference implementation lives in
    ``nexusdl/plugins/_example/plugin.py``.

    Guarantees provided by the loader
    ---------------------------------
    The loader enforces the following lifecycle, in this order:

    1. ``on_load(context)`` is called **exactly once**, before any other
       hook. If it raises, the loader attempts ``on_unload()`` once for
       rollback and reports the plugin as failed.
    2. Optional pipeline hooks are dispatched in the order the download
       pipeline invokes them. Each is called only if the plugin declared it
       in its manifest.
    3. ``on_unload()`` is called **exactly once**, in reverse load order,
       during shutdown. Failures are logged and swallowed.

    Exceptions raised by any hook are logged and isolated — a plugin cannot
    break the download pipeline. The sole exception is
    :class:`asyncio.CancelledError`, which always propagates.

    Attributes:
        name: Plugin identifier, matching ``^[a-z][a-z0-9_-]*$`` and the
            ``name:`` field of the manifest. The loader cross-checks both
            values; a mismatch aborts the load with
            :class:`~nexusdl.core.exceptions.PluginError`. Declared
            ``ClassVar`` because the loader inspects the **class** before
            instantiation — a plugin whose ``name`` is only set in
            ``__init__`` fails discovery.
        version: Plugin version, matching the ``version:`` field of the
            manifest. Cross-checked the same way. Declared ``ClassVar`` for
            the same reason as :attr:`name`.
    """

    name: ClassVar[str]
    version: ClassVar[str]

    def on_load(self, context: PluginContext) -> None:
        """Receive the plugin context and initialize state.

        Called exactly once per plugin lifetime, before any other hook.
        Synchronous by contract — perform only cheap setup here (register
        listeners, open local files, restore counters). Anything that would
        block meaningfully (network I/O, large file reads, waiting on a
        lock) belongs in the first pipeline hook or in a background task
        started here with :func:`asyncio.create_task`.

        Args:
            context: Framework-provided services. Store the fields the plugin
                needs on ``self``; do not mutate the context (it is frozen).
        """
        ...

    def on_unload(self) -> None:
        """Persist state and release resources.

        Called exactly once per plugin lifetime, in **reverse load order**,
        during framework shutdown. Synchronous by contract. Failures are
        logged and swallowed — the framework is shutting down and raising
        accomplishes nothing. Idempotent: if ``on_load`` raised, the loader
        calls ``on_unload`` once during rollback, then never again.
        """
        ...
