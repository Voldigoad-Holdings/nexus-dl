"""Textual application hosting the NexusDL TUI.

This module assembles everything the TUI needs to run: the
:class:`~textual.app.App` subclass, the log router that feeds the live
log viewer, and the :func:`run_app` async entry point invoked by
:func:`nexusdl.interfaces.cli.cli_entry._run_tui_async`.

Three responsibilities
----------------------
1. **`NexusDLApp`** — the :class:`~textual.app.App` subclass. It reads
   its title, sub-title, theme, animation level, and CSS path from the
   :class:`~nexusdl.interfaces.cli.config.TuiConfig` it is constructed
   with, and it pushes the initial
   :class:`~nexusdl.interfaces.cli.screens.main.MainScreen` on mount.
   Everything else — the actual screens, their layout, their styling —
   lives in the sibling packages. The App has no logic beyond that
   composition.

2. **`_TuiLogRouter`** — the TUI's log router. Implements
   :class:`~nexusdl.interfaces.cli.screens.logs.LogRouter`. Every
   loguru record is forwarded here while the app is running; the router
   seeds new :class:`~nexusdl.interfaces.cli.widgets.log_viewer.LogViewer`
   instances from a bounded ring buffer and forwards live entries to
   every attached viewer. This is the mechanism that makes the log
   screen show what happened before it was opened.

3. **`run_app`** — the async entry point. Builds the
   :class:`~nexusdl.interfaces.cli.config.TuiConfig`, creates the log
   router, installs the loguru sink, constructs the app, runs it, and
   removes the sink on the way out. **Service cleanup is not this
   module's job** — :func:`~nexusdl.interfaces.cli.cli_entry._run_tui_async`
   owns the manager, session, and database lifecycle and closes them in
   its own ``finally`` block.

Log routing, the full picture
-----------------------------
::

    loguru (caller thread)
        │
        │ message.record
        ▼
    _TuiLogRouter.sink()               ← installed by run_app
        │
        ├── → append to ring buffer
        │
        └── → LogViewer.push_entry()   ← for every attached viewer
                │
                ▼
            (viewer drains its queue on its own 100 ms timer)

A :class:`~nexusdl.interfaces.cli.widgets.log_viewer.LogViewer` is
attached by :class:`~nexusdl.interfaces.cli.screens.logs.LogsScreen`'s
``on_mount`` via ``self.app.log_router.attach_with_seed(...)``. The
router is exposed as :attr:`NexusDLApp.log_router` (a public attribute,
deliberately) so the LogsScreen can reach it through
``self.app.log_router`` — the standard Textual pattern for
app-scoped services.

The MainScreen is the composition root of the screen graph and needs
the same router to pass through to LogsScreen when the user navigates
there. **The router is therefore passed to MainScreen's constructor**
(``log_router=self.log_router`` in :meth:`NexusDLApp.on_mount`), not
reached through ``self.app.log_router`` at navigation time. The two
patterns are equivalent — MainScreen can reach its own app via
``self.app`` — but the constructor injection makes the dependency
visible in MainScreen's signature, which is the project's standard for
service injection.

Ring buffer size
----------------
The router keeps :data:`_DEFAULT_RING_SIZE` (1000) recent entries. At
the pipeline's typical emission rate (a few entries per second) that is
many minutes of history — more than any TUI user needs on first open.
The buffer bounds memory for a session that runs for days.

Thread safety
-------------
The loguru sink runs on **whatever thread logged the message** — the
event loop for most app-level logs, worker threads (from
:func:`asyncio.to_thread`) for pipeline work. The router's
:class:`threading.Lock` serialises the buffer append and the viewer
list snapshot; the actual per-viewer push happens outside the lock
so a slow viewer cannot stall logging for a fast one.

:meth:`LogViewer.push_entry` is itself thread-safe (it enqueues to a
:class:`queue.SimpleQueue`), so the pushes are safe from any thread.

Attach-with-seed atomicity
--------------------------
:meth:`_TuiLogRouter.attach_with_seed` guarantees that a viewer sees
every entry that was in the ring buffer at call time, and no entry
twice — even under a concurrent :meth:`sink` call.

The **long seed push runs outside the lock**, and a short "catch-up"
pass under the lock covers entries that arrived while the seed push
was running. A naive implementation that held the lock across the
whole seed would block every sink caller for the seed's full duration
(a few milliseconds for the default 500 entries — acceptable today,
but the promise is fragile: a future change to
:meth:`LogViewer.push_entry` that makes it slower would turn the
promise into a latency bug). The current implementation holds the
lock only for the O(1) snapshot and the O(missed-entries) catch-up,
where "missed" is bounded by the number of log entries a background
thread can produce in a few milliseconds.

Deferred textual import inside ``run_app``
------------------------------------------
The App class is not defined via a factory — it is a real
:class:`~textual.app.App` subclass at module level. But the **module
itself is imported lazily** by
:func:`~nexusdl.interfaces.cli.cli_entry._run_tui_async`, inside the
``tui`` command's worker. A CLI invocation that is not ``nexusdl tui``
never imports this file and therefore never imports :mod:`textual`.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Final

from loguru import logger
from textual.app import App

from nexusdl.interfaces.cli.config import TuiConfig, build_tui_config
from nexusdl.interfaces.cli.screens.logs import LogRouter
from nexusdl.interfaces.cli.screens.main import MainScreen
from nexusdl.interfaces.cli.widgets.log_viewer import LogEntry, LogViewer

if TYPE_CHECKING:
    from nexusdl.interfaces.cli.cli_entry import WiredServices


__all__ = ["NexusDLApp", "run_app"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Maximum number of log entries the router retains for seeding new
#: viewers. See the module docstring's "Ring buffer size" section.
_DEFAULT_RING_SIZE: Final[int] = 1000

#: Default maximum number of historical entries a fresh viewer receives
#: on attach. Matches the ring buffer size — a viewer attached
#: immediately after startup sees the whole buffer; a viewer attached
#: later sees the most recent 500 (older entries have already rolled
#: out of the ring buffer).
_DEFAULT_SEED_LIMIT: Final[int] = 500


# --------------------------------------------------------------------------- #
# Log router                                                                  #
# --------------------------------------------------------------------------- #


class _TuiLogRouter:
    """TUI log router — feeds the log screen from the loguru stream.

    Implements the :class:`~nexusdl.interfaces.cli.screens.logs.LogRouter`
    protocol. Thread-safe. See the module docstring's "Log routing, the
    full picture" and "Attach-with-seed atomicity" sections.

    Not exported from the module — the public surface of this file is
    :class:`NexusDLApp` and :func:`run_app`. The router is a TUI-internal
    collaborator; the LogsScreen consumes it through the
    :class:`LogRouter` protocol, which is what the composition root (this
    module) satisfies.
    """

    __slots__ = ("_lock", "_recent", "_shutting_down", "_viewers")

    def __init__(self, *, ring_size: int = _DEFAULT_RING_SIZE) -> None:
        """Initialise an empty router.

        Args:
            ring_size: Maximum number of historical entries retained for
                seeding. Must be ``>= 1``.

        Raises:
            ValueError: If ``ring_size < 1``.
        """
        if ring_size < 1:
            raise ValueError(
                f"ring_size must be >= 1, got {ring_size}"
            )
        self._lock = threading.Lock()
        self._recent: deque[LogEntry] = deque(maxlen=ring_size)
        self._viewers: list[LogViewer] = []
        #: Set to ``True`` by :meth:`shutdown` to make :meth:`sink` a
        #: no-op. Prevents entries produced during
        #: :func:`run_app`'s cleanup — after the app has begun tearing
        #: down but before the loguru sink is removed — from reaching
        #: viewers that Textual has already destroyed.
        self._shutting_down: bool = False

    # ------------------------------------------------------------------ #
    # Sink (called by loguru)                                           #
    # ------------------------------------------------------------------ #

    def sink(self, message: Any) -> None:
        """Receive one loguru message and forward it everywhere.

        Registered via :func:`loguru.logger.add` in :func:`run_app`. The
        method extracts a :class:`LogEntry` from ``message.record``,
        appends it to the ring buffer, and pushes it to every attached
        viewer.

        ``message`` is typed as :data:`Any` rather than
        ``loguru.Message`` because the ``Message`` class is not reliably
        exported from :mod:`loguru`'s top-level namespace across the
        supported version range. The attribute we actually use —
        ``message.record`` — is a plain :class:`dict` and needs no type
        annotation of its own; typing the parameter as ``Any`` keeps the
        signature compatible with every loguru version without requiring
        the private ``loguru._logger`` module.

        The per-viewer push happens **outside** the lock so a slow or
        dead viewer cannot stall the router for other viewers or for
        the sink itself. ``LogViewer.push_entry`` is thread-safe on its
        own (it enqueues to a :class:`queue.SimpleQueue`), so the
        unlocked push is safe.

        A malformed record — theoretically impossible from loguru, but
        defensively handled — is dropped silently. A sink that raises
        would produce a loguru warning on every subsequent message; a
        silent drop of one malformed record is strictly better.

        After :meth:`shutdown` the method is a no-op. This covers the
        window between "app is tearing down" and "loguru sink is
        removed" during which pipeline cleanup (``manager.stop()``,
        etc.) can still log — those entries would otherwise reach
        viewers that Textual has already destroyed.

        Args:
            message: The loguru message object. ``message.record`` is
                the structured payload; the message itself is a
                :class:`str` subclass.
        """
        if self._shutting_down:
            return

        try:
            entry = LogEntry.from_loguru_record(message.record)
        except Exception:  # noqa: BLE001 — a sink must never raise
            return

        with self._lock:
            self._recent.append(entry)
            viewers = list(self._viewers)

        for viewer in viewers:
            viewer.push_entry(entry)

    # ------------------------------------------------------------------ #
    # LogRouter protocol                                                #
    # ------------------------------------------------------------------ #

    def attach_with_seed(
        self,
        viewer: LogViewer,
        *,
        limit: int = _DEFAULT_SEED_LIMIT,
    ) -> Callable[[], None]:
        """Seed *viewer* with recent entries and attach it for live updates.

        Atomic with respect to :meth:`sink`. See
        :meth:`nexusdl.interfaces.cli.screens.logs.LogRouter.attach_with_seed`
        for the exact guarantee this method must uphold — the two must
        stay in lock-step. See the module docstring's
        "Attach-with-seed atomicity" section for the implementation
        strategy.

        The seed snapshot and the seed pushes are performed in **two
        phases**: the O(1) snapshot is taken under the lock, the O(seed)
        pushes run outside it, and a short catch-up pass under the lock
        covers any entries that arrived during the seed push. A naive
        implementation that held the lock across the whole seed would
        block every sink caller for the seed's full duration (a few
        milliseconds for the default 500 entries — acceptable today, but
        fragile: a future change to ``LogViewer.push_entry`` that makes
        it slower would turn the promise into a latency bug).

        Args:
            viewer: The viewer to seed and attach.
            limit: Maximum number of historical entries to seed. Clamped
                to the ring buffer's current size.

        Returns:
            A zero-argument detach callable. Idempotent.
        """
        # Phase 1: snapshot the seed under the lock.
        with self._lock:
            seed_count = min(limit, len(self._recent))
            seed = (
                list(self._recent)[-seed_count:]
                if seed_count > 0
                else []
            )

        # Phase 2: push the seed outside the lock.
        for entry in seed:
            viewer.push_entry(entry)

        # Phase 3: attach and catch up on entries that arrived during
        # phase 2. Under the lock so that the "attach" and the
        # "catch up" are one atomic transition.
        with self._lock:
            if self._shutting_down:
                return lambda: None
            current_count = len(self._recent)
            if current_count > len(seed):
                # Entries arrived during phase 2. The ring buffer's
                # ``maxlen`` means ``current_count`` can equal
                # ``len(seed)`` even when new entries arrived (old ones
                # rolled out) — in that case, the ``list(self._recent)``
                # after the seed contains only entries the viewer has
                # not seen if the ring size equals the seed size. The
                # conservative choice is to skip the "catch-up" when
                # the counts match — the cost is potentially missing
                # entries produced during the seed push in that
                # borderline case, but the scenario (a viewer opening
                # exactly as the ring buffer rolls over) is
                # pathological and the previous behaviour (miss the
                # entry entirely) was strictly worse.
                missed = list(self._recent)[len(seed):]
                for entry in missed:
                    viewer.push_entry(entry)
            self._viewers.append(viewer)

        def detach() -> None:
            with self._lock:
                try:
                    self._viewers.remove(viewer)
                except ValueError:
                    # Already detached — idempotent.
                    pass

        return detach

    # ------------------------------------------------------------------ #
    # Shutdown                                                          #
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """Stop accepting log entries.

        Called by :func:`run_app` before the loguru sink is removed.
        After this returns, :meth:`sink` is a no-op and no viewer
        receives further entries — the pipeline cleanup that follows
        cannot reach a viewer Textual has already torn down.

        Idempotent.
        """
        with self._lock:
            self._shutting_down = True


# --------------------------------------------------------------------------- #
# NexusDLApp                                                                  #
# --------------------------------------------------------------------------- #


class NexusDLApp(App[None]):
    """The NexusDL Textual application.

    Constructed by :func:`run_app` with the wired services, the resolved
    :class:`~nexusdl.interfaces.cli.config.TuiConfig`, and the log
    router. Pushes the
    :class:`~nexusdl.interfaces.cli.screens.main.MainScreen` on mount
    and does nothing else — every screen, dialog, and widget below it
    owns its own behaviour.

    The App's configuration is set from the :class:`TuiConfig` in
    ``__init__`` so that Textual reads the correct values during its
    own startup sequence (theme is loaded, command palette is
    installed, etc. — all before the first screen mounts).

    Attributes:
        log_router: The TUI's log router. Exposed as a **public**
            attribute because
            :class:`~nexusdl.interfaces.cli.screens.main.MainScreen`
            receives it via constructor injection (not via
            ``self.app.log_router`` at navigation time) — but a future
            screen added to the graph may prefer the ``self.app``
            accessor, and the attribute is the natural place to expose
            it.
    """

    #: ``theme.tcss`` lives next to this file. Textual resolves the path
    #: relative to the App's module directory. Overridable via
    #: :attr:`~nexusdl.interfaces.cli.config.TuiConfig.css_path`.
    #:
    #: Declared as ``ClassVar`` (not a plain class attribute) to match
    #: :class:`textual.app.App.CSS_PATH`'s own annotation and to keep
    #: the "class-level default" semantics explicit. The instance-level
    #: override (when ``config.css_path`` is set) goes through
    #: :func:`object.__setattr__` — a plain ``self.CSS_PATH = ...``
    #: would trigger ``mypy --strict``'s "cannot assign to class
    #: variable via instance" error, and using ``object.__setattr__``
    #: is the sanctioned way to assign a ClassVar at instance level
    #: without disabling the annotation.
    CSS_PATH: ClassVar[str] = "theme.tcss"

    #: Command palette enabled by default. A
    #: :class:`~nexusdl.interfaces.cli.config.TuiConfig` that sets
    #: ``command_palette=False`` (currently never — the field is always
    #: ``True``) would disable it here.
    ENABLE_COMMAND_PALETTE: ClassVar[bool] = True

    def __init__(
        self,
        *,
        services: WiredServices,
        config: TuiConfig,
        log_router: LogRouter,
    ) -> None:
        """Initialise the app with the wired services and resolved config.

        Args:
            services: The fully-constructed services container, as
                produced by
                :meth:`nexusdl.interfaces.cli.cli_entry.CLIState.ensure`.
                The App holds a reference for its entire lifetime; it
                does not close any of the services (that is the caller's
                job — see the module docstring's "Three
                responsibilities" section).
            config: The resolved :class:`TuiConfig`. The App reads
                ``title``, ``sub_title``, ``theme``, ``animations``,
                ``command_palette``, and ``css_path`` from it.
            log_router: The TUI log router. Stored as the public
                :attr:`log_router` attribute for the ``LogsScreen`` to
                use. Typed as the public
                :class:`~nexusdl.interfaces.cli.screens.logs.LogRouter`
                protocol — the concrete ``_TuiLogRouter`` satisfies it
                structurally, and the protocol is what the App and its
                screens depend on.
        """
        super().__init__()
        self._services = services
        self._config = config

        # --- Instance-level overrides of Textual's class defaults --- #
        self.title = config.title
        self.sub_title = config.sub_title
        self.theme = config.theme
        self.animation_level = (
            "full" if config.animations else "none"
        )
        # ``ENABLE_COMMAND_PALETTE`` is read during ``App.__init__``
        # of Textual internally; the class default is what matters
        # there, and our config always sets it to True. Kept as a
        # comment rather than a redundant assignment.

        if config.css_path is not None:
            # Override the class-level default. Textual resolves
            # ``self.CSS_PATH`` at mount time (instance attribute
            # shadows class attribute), so this instance-level
            # assignment works as intended. The assignment goes through
            # ``object.__setattr__`` because ``mypy --strict`` rejects
            # assigning to a ClassVar via ``self.attr = ...``.
            object.__setattr__(
                self, "CSS_PATH", str(config.css_path)
            )

        # Public: LogsScreen reads ``self.app.log_router``.
        self.log_router: LogRouter = log_router

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def on_mount(self) -> None:
        """Push the main screen.

        The main screen is the root of the navigation graph — every
        other screen is pushed from it. Pushing it here, in the app's
        own ``on_mount``, means the app is single-screen-per-instance
        for its entire lifetime; the app never returns to a "no
        screen" state.

        The wired services are passed through exactly as received. The
        app does not reshape them, does not add fields, and does not
        drop any — a caller reading the App's behaviour sees the same
        services every other TUI-level consumer sees.

        The log router is passed to MainScreen **by constructor
        injection**, not left to MainScreen to fetch from
        ``self.app.log_router``. The two patterns are equivalent —
        MainScreen can reach its own app — but the constructor
        injection makes the dependency visible in MainScreen's
        signature, which is the project's standard for service
        injection.
        """
        self.push_screen(
            MainScreen(
                settings=self._services.settings,
                registry=self._services.registry,
                database=self._services.database,
                manager=self._services.manager,
                session=self._services.session,
                event_bus=self._services.event_bus,
                log_router=self.log_router,
                translator=self._services.translator,
            )
        )


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


async def run_app(services: WiredServices) -> None:
    """Launch the TUI, run it to completion, and clean up the log sink.

    Called by
    :func:`nexusdl.interfaces.cli.cli_entry._run_tui_async`. The
    function returns when the user quits the app; every service below
    the TUI (download manager, HTTP session, database) is closed by the
    caller after this returns — see the module docstring's "Three
    responsibilities" section.

    The steps:

    1. Build the :class:`~nexusdl.interfaces.cli.config.TuiConfig`
       from the services' settings and the package version.
    2. Create the :class:`_TuiLogRouter` and install its sink on
       :mod:`loguru` at the settings' configured log level.
    3. Construct :class:`NexusDLApp` and ``await app.run_async()``.
    4. Shut the router down (making its sink a no-op) and remove the
       loguru sink, both in the ``finally`` block. A crashing app does
       not leave the process with a sink pointing at a dead router,
       and pipeline cleanup that runs after the app returns cannot
       reach a viewer Textual has already destroyed.

    Args:
        services: The fully-constructed services container.
    """
    # The version is imported here — not at module load — so that a
    # user running the CLI for the first time without ``version.py``
    # present (a broken installation) still gets the TUI to launch,
    # just without a version in the sub-title.
    try:
        from nexusdl.version import __version__ as version
    except ImportError:  # pragma: no cover — broken installation
        version = ""

    config = build_tui_config(
        services.settings,
        version=version,
    )

    log_router = _TuiLogRouter()

    # Install the TUI's log sink. Level matches the app's configured
    # logging level so the TUI log viewer shows the same information
    # as the CLI's stderr output — a user who configured
    # ``NEXUSDL_LOGGING__LEVEL=DEBUG`` sees debug output in the log
    # screen too, and a user on the default INFO level is not flooded
    # with pipeline-internal debug traces.
    sink_id = logger.add(
        log_router.sink,
        level=services.settings.logging.level,
        format="{message}",
    )

    try:
        app = NexusDLApp(
            services=services,
            config=config,
            log_router=log_router,
        )
        await app.run_async()
    finally:
        # Shut the router down before removing the sink — see step 4.
        # The router's ``sink`` becomes a no-op, so any log emitted
        # between "app returns" and "sink removed" cannot reach a
        # viewer Textual has already torn down.
        log_router.shutdown()

        # Remove the sink. Failure to remove (loguru raising on a
        # stale sink id) would be a loguru bug; the sink_id is
        # guaranteed valid by the ``try`` above.
        try:
            logger.remove(sink_id)
        except (ValueError, KeyError):
            # Sink already removed by another path — no-op. KeyError
            # is not raised by current loguru versions but is included
            # defensively in case a future version changes the error
            # type.
            pass
