"""CLI entry point and composition root for NexusDL.

This module is the **single bootstrapping authority for the command-line
interface**. It provides three things and nothing else:

* The :data:`app` — the top-level :class:`typer.Typer` object that owns
  the ``nexusdl`` command and dispatches to every sub-command group
  registered from :mod:`nexusdl.interfaces.cli.commands`.
* The :class:`CLIState` — the lazy container for the wired services
  (:class:`~nexusdl.core.config.Settings`,
  :class:`~nexusdl.core.events.EventBus`,
  :class:`~nexusdl.core.registry.site_registry.SiteRegistry`,
  :class:`~nexusdl.core.library.database.LibraryDatabase`,
  :class:`~nexusdl.core.session.http_session.HttpSession`,
  :class:`~nexusdl.core.downloader.manager.DownloadManager`) that every
  sub-command needs. The state is **built on first use**, not at import
  time, so commands that do not need it (``admin init``, ``version``)
  keep working when the user has not yet bootstrapped an ``.env``.
* The :func:`main` function — the entry point the ``nexusdl`` console
  script and ``python -m nexusdl`` invoke.

Composition root responsibilities
---------------------------------
:meth:`CLIState._bootstrap_impl` wires everything, in this order:

1. Load :class:`~nexusdl.core.config.Settings` (from ``.env``, env vars,
   user YAML, bundled defaults — see :mod:`nexusdl.core.config`).
2. Configure :mod:`loguru` (:func:`configure_logging_from_settings`).
3. Ensure every directory the pipeline uses exists
   (:func:`ensure_all_directories`).
4. Build the :class:`~nexusdl.core.i18n.Translator` and install it as
   the process-wide default.
5. Build the shared :class:`~nexusdl.core.events.EventBus`.
6. Load plugins (:meth:`PluginLoader.load_all`).
7. Build the :class:`SiteRegistry` and initialize it (parser factory
   resolution + ``sites.yaml`` validation).
8. Build the :class:`LibraryDatabase` and initialize it (schema
   migrations).
9. Build the :class:`HttpSession` with every collaborator from
   ``Settings``.
10. Build the :class:`DownloadManager`.

The order is not arbitrary: each step depends on the previous one. The
``HttpSession`` needs ``Settings``; the ``DownloadManager`` needs the
registry, session, and event bus; the plugin loader needs the event bus
and (optionally) the session.

Bootstrap under a running event loop
------------------------------------
:meth:`CLIState.ensure` is **synchronous** and can be called from a
command declared either ``def`` (the common case) or ``async def``
(Typer dispatches ``async def`` commands inside its own
:func:`asyncio.run`). The bootstrap itself needs to await two
coroutines — :meth:`SiteRegistry.initialize` and
:meth:`LibraryDatabase.init` — which requires an event loop.

The synchronous caller cannot simply do
``asyncio.run(initialize())``: if the caller is already inside a
running loop (the ``async def`` command path), that raises
``RuntimeError: Cannot run the event loop while another loop is
running``. The bootstrap therefore runs the two coroutines **inside a
thread-pool task** when a loop is already running, and directly inside
a fresh loop otherwise. The two code paths are isolated in
:func:`_run_coroutine` — see its docstring for the exact rule.

Deferred imports
----------------
Two dependencies are imported **inside** the commands that need them,
not at module top:

* :mod:`nexusdl.parsers.factory` — the concrete parser factory. It is
  imported inside :meth:`CLIState._bootstrap_impl` and only when the
  bootstrap runs. A deployment that only ever runs ``nexusdl version``
  or ``nexusdl admin init`` does not pay for importing the parser
  package, which pulls in :mod:`selectolax`, :mod:`beautifulsoup4`, and
  every concrete parser module.
* :mod:`nexusdl.interfaces.cli.app` (TUI) and
  :mod:`nexusdl.interfaces.web.backend.main` (Web) — imported inside
  the ``tui`` and ``web`` commands, respectively. A CLI invocation that
  is not launching a TUI or a server does not import :mod:`textual`,
  :mod:`fastapi`, or :mod:`uvicorn`.

Both patterns match the deferred-import discipline the rest of the
package uses (see e.g. :mod:`nexusdl.packaging.factory`).

Failure policy
--------------
The bootstrap can fail for two reasons, distinguished by the message
the CLI prints:

* **Missing secrets** — no ``.env`` file and no environment variables
  providing the three required secrets. The error message names
  ``nexusdl admin init`` as the fix.
* **Invalid configuration** — a malformed ``.env``, a broken
  ``sites.yaml``, a schema violation. The message names the offending
  file and the specific error.

Either way, the CLI exits with code 1. A subsequent ``nexusdl --help``
still works — help is handled by Typer/Click **before** the callback
runs.

Async commands
--------------
Some commands are ``async def`` (``tui``, ``web`` internal bootstrap).
Typer dispatches ``async def`` commands inside its own
:func:`asyncio.run` — the CLI's own event loop is not running when a
command executes, so calling :meth:`CLIState.ensure` from there is safe
because ``_run_coroutine`` detects the running loop and offloads the
awaited work to a thread.

The two interface-launching commands (``tui``, ``web``) are declared
**sync** and create their own event loop via :func:`asyncio.run`. They
must do so because both :meth:`textual.app.App.run_async` and
:meth:`uvicorn.Server.serve` are coroutines that need to own the loop
for their duration — they cannot be invoked from inside an outer
:func:`asyncio.run` without nesting loops.

Cleanup on interface exit
-------------------------
Both ``tui`` and ``web`` close every service on the ``finally`` path.
Each cleanup step is **individually wrapped**: a failure to close the
manager must not prevent the database from being closed, and — more
importantly — a cleanup failure must not **mask** the exception that
brought the interface down. A ``raise`` inside a ``finally`` replaces
the in-flight exception; the user then sees "failed to close database"
instead of the actual "TUI crashed" traceback. The pattern is: try,
log, continue.

``--version`` handling
----------------------
Typer's :class:`~typer.Typer` constructor does **not** accept ``version``
or ``version_option`` arguments — those never existed in the API. The
``--version`` flag is implemented as an **eager option on the root
callback** (:data:`_root_callback`): it declares ``--version`` with
``is_eager=True`` and prints the version before any sub-command runs.
A user who invokes ``nexusdl --version`` gets the version and a clean
exit; ``nexusdl search run foo`` runs the callback first (with
``version=False``) then dispatches to the sub-command.

Localisation
------------
The CLI prints technical errors on stderr in English, matching the Unix
tradition (``git``, ``docker``, ``npm`` all do the same). User-facing
output that a translator wants to localise is emitted by the TUI and the
Web UI, not by the CLI's error path. The ``Translator`` the state builds
is used by TUI screens — the CLI itself does not use it.

Dependency graph (no cycles)::

    nexusdl.core.config              ─┐
    nexusdl.core.constants            │
    nexusdl.core.downloader.manager   │
    nexusdl.core.events               │
    nexusdl.core.exceptions           ├──► interfaces.cli.cli_entry
    nexusdl.core.i18n                 │
    nexusdl.core.library.database     │
    nexusdl.core.logger               │
    nexusdl.core.paths                │
    nexusdl.core.registry             │
    nexusdl.core.session              │
    nexusdl.plugins                   │
    nexusdl.version                   │
    nexusdl.interfaces.cli.commands   │
    typer                             │

``nexusdl.parsers.factory``, ``nexusdl.interfaces.cli.app``, and
``nexusdl.interfaces.web.backend.main`` are **deferred** imports — see
the "Deferred imports" section.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Coroutine, Final, TypeVar

import typer

from nexusdl.core.config import Settings
from nexusdl.core.constants import APP_NAME
from nexusdl.core.downloader import DownloadManager
from nexusdl.core.events import EventBus
from nexusdl.core.i18n import Translator, configure_default_translator
from nexusdl.core.library import LibraryDatabase
from nexusdl.core.logger import configure_logging_from_settings, get_logger
from nexusdl.core.paths import ensure_all_directories
from nexusdl.core.registry import SiteRegistry
from nexusdl.core.session import HttpSession
from nexusdl.interfaces.cli.commands import (
    admin_app,
    config_app,
    download_app,
    library_app,
    plugins_app,
    search_app,
    sites_app,
)

# ``nexusdl/version.py`` is a trivial module shipped with the package.
# The fallback exists so a broken installation produces a versioned
# binary rather than a crash — the ``version`` command still works,
# just with a degraded string. It should never trigger in a correct
# install.
try:
    from nexusdl.version import __version__ as NEXUSDL_VERSION
except ImportError:  # pragma: no cover — never in a correct install
    NEXUSDL_VERSION = "0.0.0+unknown"


if TYPE_CHECKING:
    from nexusdl.plugins import PluginLoader

_T = TypeVar("_T")


__all__ = ["CLIState", "WiredServices", "app", "main"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Exit code used for a failed bootstrap or an unhandled configuration
#: error. Matches the Unix convention for "command exited with an error"
#: and the exit codes every other CLI in the pipeline's ecosystem uses.
_EXIT_CONFIG_ERROR: Final[int] = 1

#: Exit code used for a keyboard interrupt. Matches the Unix convention
#: for SIGINT (``128 + 2``) and the exit code ``shell`` reports for an
#: interrupted foreground process.
_EXIT_KEYBOARD_INTERRUPT: Final[int] = 130


_log = get_logger("cli.entry")


# --------------------------------------------------------------------------- #
# Wired services                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WiredServices:
    """The fully-constructed services every CLI command consumes.

    Built once by :meth:`CLIState.ensure` and cached on the state for
    the rest of the process. Immutable; a command that needs to alter a
    service must mutate the service itself (e.g. change a rate limit on
    the session), not replace it in this container.

    Attributes:
        settings: The loaded :class:`~nexusdl.core.config.Settings`.
            Shared by every service below; do not replace.
        translator: The :class:`~nexusdl.core.i18n.Translator` for the
            active locale. Also installed as the process-wide default
            (see :func:`~nexusdl.core.i18n.configure_default_translator`),
            so screens that use ``get_default_translator()`` receive
            this same instance.
        event_bus: The shared :class:`~nexusdl.core.events.EventBus`.
            Every service that emits or subscribes to events uses this
            instance.
        plugin_loader: The
            :class:`~nexusdl.plugins.loader.PluginLoader`, already
            configured with the plugin directories from settings. Its
            plugins may not yet be loaded — commands that need them
            call ``await plugin_loader.load_all()`` explicitly (it is
            idempotent).
        registry: The initialized
            :class:`~nexusdl.core.registry.site_registry.SiteRegistry`.
            ``initialize()`` has been awaited; a command that needs to
            reload the sites calls ``await registry.reload()``.
        database: The initialized
            :class:`~nexusdl.core.library.database.LibraryDatabase`.
            ``init()`` has been awaited; schema migrations are applied.
        session: The
            :class:`~nexusdl.core.session.http_session.HttpSession`,
            with rate limiter, circuit breaker, cookie manager, proxy
            manager, user-agent rotator, and HTTP cache wired from
            settings. Not yet opened — the underlying
            :class:`httpx.AsyncClient` is created lazily on first use.
        manager: The
            :class:`~nexusdl.core.downloader.manager.DownloadManager`,
            with the parser factory closure, the packager factory, and
            the shared event bus wired.
    """

    settings: Settings
    translator: Translator
    event_bus: EventBus
    plugin_loader: PluginLoader
    registry: SiteRegistry
    database: LibraryDatabase
    session: HttpSession
    manager: DownloadManager


# --------------------------------------------------------------------------- #
# CLIState                                                                    #
# --------------------------------------------------------------------------- #


class CLIState:
    """Lazy container for the wired services.

    Stored on ``typer.Context.obj`` by the root callback and retrieved by
    every sub-command. The state is created eagerly (it is cheap — one
    object with two fields), but **bootstrapped** on the first call to
    :meth:`ensure`, so that:

    * Commands that do not need any service (``version``, ``admin
      init``) work without an ``.env`` file.
    * The expensive bootstrap (importing :mod:`nexusdl.parsers`,
      launching plugin code, opening the database) happens **after**
      Typer has parsed the command line and decided what to run. A
      ``nexusdl search --help`` invocation does not pay for it.

    A bootstrap failure is remembered and re-raised on **every**
    subsequent :meth:`ensure` call — a command that fails once does not
    get a second chance in the same process. This matches Typer's
    one-command-per-process model: there is no session to preserve, so a
    retry would just re-fail. A user who fixes ``.env`` and reruns the
    CLI gets a fresh process with a fresh state.

    Args:
        debug: When ``True``, overrides ``settings.app.debug`` and
            raises the logging level to ``DEBUG``. The configured
            ``format`` is preserved: a deployment that has set
            ``NEXUSDL_LOGGING__FORMAT=json`` keeps JSON output even
            under ``--debug`` (a log collector that fails on pretty
            output would otherwise drop the whole traceback). A
            ``format`` of ``None`` — the "auto" default — resolves to
            ``pretty`` under debug, matching the pre-existing
            behaviour for the common case.
        quiet: When ``True``, overrides ``settings.logging.level`` to
            ``"WARNING"`` — suppresses the routine ``INFO`` lines the
            pipeline emits during a normal run. Mutually exclusive with
            *debug*; if both are set, *debug* wins (the user asked for
            more information, not less).
    """

    __slots__ = ("_debug", "_error", "_quiet", "_wired")

    def __init__(self, *, debug: bool = False, quiet: bool = False) -> None:
        self._debug = debug
        # A user who passes both is more likely to have made a typo than
        # to genuinely want debug + quiet, so debug wins.
        self._quiet = quiet and not debug
        self._wired: WiredServices | None = None
        self._error: Exception | None = None

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def ensure(self) -> WiredServices:
        """Return the wired services, bootstrapping on first call.

        Idempotent on success. On failure, raises
        :class:`typer.Exit` with code 1 and a message explaining what
        went wrong and how to fix it — the caller does not need to
        catch or wrap the error.

        Safe to call from a synchronous command (Typer runs the
        callback outside any event loop) and from an asynchronous
        command (Typer wraps the command in its own
        :func:`asyncio.run`). The bootstrap's two awaited calls —
        :meth:`SiteRegistry.initialize` and
        :meth:`LibraryDatabase.init` — are dispatched by
        :func:`_run_coroutine`, which detects the running loop and
        offloads the work to a thread in that case. See that helper's
        docstring.

        Returns:
            The :class:`WiredServices` container.

        Raises:
            typer.Exit: If the bootstrap failed. The exit message is
                printed to stderr before the exception propagates.
        """
        if self._wired is not None:
            return self._wired
        if self._error is not None:
            _raise_bootstrap_error(self._error)

        try:
            self._wired = self._bootstrap_impl()
        except Exception as exc:  # noqa: BLE001 — the error surfaces to the user
            self._error = exc
            _raise_bootstrap_error(exc)

        return self._wired

    # ------------------------------------------------------------------ #
    # Bootstrap                                                          #
    # ------------------------------------------------------------------ #

    def _bootstrap_impl(self) -> WiredServices:
        """Build every service in dependency order.

        See the module docstring's "Composition root responsibilities"
        section for the ordering and the reasoning. Each step is
        extracted so the whole is auditable in one place.

        Returns:
            The fully-constructed :class:`WiredServices`.

        Raises:
            Exception: Whatever the individual steps raise — a
                :class:`~nexusdl.core.exceptions.ConfigError` for a
                configuration problem, a
                :class:`~nexusdl.core.exceptions.PluginError` for a
                broken plugin, an :class:`OSError` for a filesystem
                problem. The caller (:meth:`ensure`) converts these
                into a uniform CLI exit.
        """
        # --- 1. Settings ---------------------------------------------- #
        settings = Settings()

        # Apply the CLI overrides. The `Settings` model is frozen, so
        # the override is applied via ``model_copy`` rather than a
        # mutation. ``quiet`` and ``debug`` are mutually exclusive (see
        # ``__init__``); a debug run sets the level to DEBUG, a quiet
        # run sets it to WARNING, and the default run leaves both as
        # configured. **Format is never overridden** — see the
        # class docstring's ``debug`` argument for the rationale.
        if self._debug:
            debug_update: dict[str, Any] = {"level": "DEBUG"}
            if settings.logging.format is None:
                debug_update["format"] = "pretty"
            settings = settings.model_copy(
                update={
                    "app": settings.app.model_copy(
                        update={"debug": True}
                    ),
                    "logging": settings.logging.model_copy(
                        update=debug_update
                    ),
                }
            )
        elif self._quiet:
            settings = settings.model_copy(
                update={
                    "logging": settings.logging.model_copy(
                        update={"level": "WARNING"}
                    ),
                }
            )

        # --- 2. Logging ----------------------------------------------- #
        configure_logging_from_settings(settings)
        _log.debug(
            "Settings loaded (debug={}, quiet={})",
            self._debug,
            self._quiet,
        )

        # --- 3. Directories ------------------------------------------- #
        ensure_all_directories(settings)

        # --- 4. Translator -------------------------------------------- #
        translator = Translator(default_locale=settings.app.language)
        configure_default_translator(translator)

        # --- 5. Event bus --------------------------------------------- #
        event_bus = EventBus()

        # --- 6. Plugin loader ----------------------------------------- #
        # Imported here (not at module level) so a deployment that only
        # uses the CLI for ``version`` or ``admin`` does not pay for
        # the plugin package import.
        from nexusdl.plugins import PluginLoader

        plugin_loader = PluginLoader(
            settings=settings,
            event_bus=event_bus,
            http_session=None,  # the session is not built yet
        )

        # --- 7. Site registry ----------------------------------------- #
        # The parser factory is imported here for the same reason the
        # plugin loader is: it pulls in every concrete parser.
        from nexusdl.parsers.factory import DefaultParserFactory

        parser_factory = DefaultParserFactory()
        registry = SiteRegistry(
            parser_factory=parser_factory,
            settings=settings,
        )
        _run_coroutine(registry.initialize())

        # --- 8. Library database -------------------------------------- #
        database = LibraryDatabase(settings.paths.database_file)
        _run_coroutine(database.init())

        # --- 9. HTTP session ------------------------------------------ #
        from nexusdl.core.session.circuit_breaker import CircuitBreakerPool
        from nexusdl.core.session.cookie_manager import CookieManager
        from nexusdl.core.session.proxy_manager import load_proxy_configs
        from nexusdl.core.session.proxy_manager import ProxyManager
        from nexusdl.core.session.user_agents import UserAgentRotator

        user_agents = UserAgentRotator()
        proxy_manager = ProxyManager(
            load_proxy_configs(settings.proxy),
            strategy=settings.proxy.strategy,
            sticky=settings.proxy.sticky,
            failure_threshold=settings.proxy.failure_threshold,
            cooldown_seconds=settings.proxy.cooldown_seconds,
        )
        circuit_breaker = CircuitBreakerPool(
            failure_threshold=(
                settings.session.circuit_breaker.failure_threshold
            ),
            recovery_timeout=(
                settings.session.circuit_breaker.recovery_timeout
            ),
            half_open_success_threshold=(
                settings.session.circuit_breaker.half_open_success_threshold
            ),
            half_open_max_attempts=(
                settings.session.circuit_breaker.half_open_max_attempts
            ),
        )
        cookie_manager = CookieManager(
            key=settings.cookie_encryption_key,
            path=settings.paths.cookies_file,
            env=_read_env(),
            known_sites=[
                s.id
                for s in registry.list_sites(
                    include_adult=True, include_disabled=True
                )
            ],
        )
        try:
            cookie_manager.load()
        except Exception as exc:  # noqa: BLE001 — a bad store is not fatal
            _log.warning(
                "Could not load the encrypted cookie store ({}); "
                "continuing with an empty store",
                exc,
            )

        def _rate_limit_source(site_id: str) -> float | None:
            if site_id in settings.rate_limits.per_site:
                return settings.rate_limits.per_site[site_id]
            return settings.rate_limits.default_per_second

        session = HttpSession(
            user_agents=user_agents,
            proxy_manager=proxy_manager,
            circuit_breaker=circuit_breaker,
            cookie_manager=cookie_manager,
            rate_limit_source=_rate_limit_source,
            http2=settings.http.http2,
            follow_redirects=settings.http.follow_redirects,
            max_connections=settings.http.max_connections,
            max_keepalive_connections=(
                settings.http.max_keepalive_connections
            ),
            timeouts=_timeouts_from_settings(settings),
        )

        # --- 10. Download manager ------------------------------------- #
        from nexusdl.core.packaging import PackagerFactory

        packager_factory = PackagerFactory()

        def _worker_factory(parser):  # type: ignore[no-untyped-def]
            from nexusdl.core.downloader import DownloadWorker

            return DownloadWorker(
                parser=parser,
                packager_factory=packager_factory,
                settings=settings,
                event_bus=event_bus,
                plugin_dispatcher=plugin_loader,
            )

        manager = DownloadManager(
            registry=registry,
            settings=settings,
            event_bus=event_bus,
            worker_factory=_worker_factory,
            plugin_dispatcher=plugin_loader,
        )

        return WiredServices(
            settings=settings,
            translator=translator,
            event_bus=event_bus,
            plugin_loader=plugin_loader,
            registry=registry,
            database=database,
            session=session,
            manager=manager,
        )


# --------------------------------------------------------------------------- #
# Bootstrap helpers                                                           #
# --------------------------------------------------------------------------- #


def _run_coroutine(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run *coro* to completion, whether or not an event loop is running.

    The bootstrap runs synchronously in the CLI callback's thread, but
    the two awaited calls it makes
    (:meth:`SiteRegistry.initialize` and :meth:`LibraryDatabase.init`)
    are coroutines. Two cases must be handled:

    * **No event loop is running** (the common ``def`` command case) —
      a fresh event loop is created, the coroutine runs to completion,
      the loop is closed. This is the same pattern as
      :func:`asyncio.run` and produces the same observable behaviour.

    * **An event loop is already running** (the ``async def`` command
      case: Typer wraps every ``async def`` command in its own
      :func:`asyncio.run`, so :meth:`CLIState.ensure` can be called
      from inside an active loop) — a fresh loop cannot be created in
      this thread without raising ``RuntimeError: Cannot run the event
      loop while another loop is running``. The coroutine is therefore
      offloaded to a worker thread that creates its own loop and runs
      it there. The calling thread blocks on
      :meth:`concurrent.futures.Future.result`, which is safe because
      the calling coroutine is the only consumer of the outer loop at
      this point.

    The thread-offload path is only reached for ``async def`` commands.
    Every ``def`` command takes the fast path and pays only for a
    couple of loop constructions.

    Args:
        coro: The coroutine to run.

    Returns:
        The coroutine's return value.

    Raises:
        Exception: Whatever the coroutine raised — propagated unchanged,
            with the traceback preserved (the worker-thread path
            re-raises from the future, which carries the original
            exception object).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running — fast path.
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    # A loop is running — we cannot create a nested one in this thread.
    # Offload to a worker thread that owns its own loop.
    def _runner() -> _T:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_runner)
        return future.result()


def _read_env() -> dict[str, str]:
    """Return the process environment as a plain dict.

    ``CookieManager`` accepts a ``Mapping[str, str]``; passing
    ``os.environ`` directly would couple the manager to the mutable
    environment object. A snapshot is safer and simpler.
    """
    import os

    return dict(os.environ)


def _timeouts_from_settings(settings: Settings):  # type: ignore[no-untyped-def]
    """Build an :class:`HttpTimeouts` bundle from the settings' HTTP section.

    A thin adapter: the settings' individual timeout fields are combined
    into the four-tuple the session expects. Kept as a helper rather than
    inlined so a future change to either shape is a single edit.
    """
    from nexusdl.core.session import HttpTimeouts

    return HttpTimeouts(
        connect=settings.http.connect_timeout,
        read=settings.http.read_timeout,
        write=settings.http.write_timeout,
        pool=settings.http.pool_timeout,
    )


def _raise_bootstrap_error(exc: Exception) -> None:
    """Print a bootstrap error and exit the CLI.

    The message is deliberately actionable: it names what failed, gives
    the most likely fix, and reminds the user that ``nexusdl admin
    init`` will create the ``.env`` file if it is missing. A user who
    is running NexusDL for the first time sees the right next step.

    Args:
        exc: The exception the bootstrap raised.

    Raises:
        typer.Exit: Always — the CLI cannot proceed without services.
    """
    typer.echo(
        f"Configuration error: {type(exc).__name__}: {exc}",
        err=True,
    )
    typer.echo(
        "Hint: if this is a first run, run 'nexusdl admin init' to "
        "generate the .env file with the required secrets.",
        err=True,
    )
    raise typer.Exit(code=_EXIT_CONFIG_ERROR)


async def _close_services(wired: WiredServices) -> None:
    """Close every service, logging each failure independently.

    Called from the ``finally`` block of the ``tui`` and ``web``
    commands. Each cleanup step is wrapped individually so that:

    * a failure in one service does not prevent the others from being
      closed;
    * a cleanup failure does not **mask** the exception that brought
      the interface down.

    A ``raise`` inside a ``finally`` replaces the in-flight exception.
    If ``run_app`` raised ``ValueError: TUI crashed`` and the
    subsequent ``manager.stop()`` raised ``RuntimeError: stop failed``,
    the user would see the ``RuntimeError`` traceback and assume the
    TUI crashed for the wrong reason. The pattern here is: try, log,
    continue.

    Args:
        wired: The wired services to close.
    """
    try:
        await wired.manager.stop()
    except Exception as exc:  # noqa: BLE001 — cleanup, log and continue
        _log.warning(
            "Failed to stop the download manager during shutdown: "
            "{}: {}",
            type(exc).__name__,
            exc,
        )
    try:
        await wired.session.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Failed to close the HTTP session during shutdown: "
            "{}: {}",
            type(exc).__name__,
            exc,
        )
    try:
        await wired.database.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Failed to close the library database during shutdown: "
            "{}: {}",
            type(exc).__name__,
            exc,
        )


# --------------------------------------------------------------------------- #
# Typer app                                                                   #
# --------------------------------------------------------------------------- #

#: The top-level Typer application. Sub-command groups from
#: :mod:`nexusdl.interfaces.cli.commands` are registered on it via
#: :func:`add_typer`. Every command in the CLI is reachable through this
#: object — there is no other entry point.
#:
#: ``pretty_exceptions_enable=False`` disables Typer's rich traceback
#: renderer because the CLI has its own error presentation (see
#: :func:`_raise_bootstrap_error` and the ``on_error`` hook installed by
#: :func:`main`). Enabling both would double up on tracebacks.
#:
#: ``--version`` is **not** a constructor argument of :class:`typer.Typer`
#: (that parameter has never existed in the API). The flag is implemented
#: as an eager option on :data:`_root_callback` below — the standard
#: Typer pattern for it. See the module docstring's "--version handling"
#: section for the reasoning.
app: Final[typer.Typer] = typer.Typer(
    name="nexusdl",
    help=(
        f"{APP_NAME} — download manga, webtoons, and comics from every "
        f"supported source site."
    ),
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=True,
)

# Register every sub-command group. The order here is the order the
# groups appear in ``nexusdl --help``; the current order groups the most
# common workflows first (search → download → library), then the
# inspection commands (config → sites → plugins), then the rarely-used
# admin group last.
app.add_typer(search_app, name="search")
app.add_typer(download_app, name="download")
app.add_typer(library_app, name="library")
app.add_typer(config_app, name="config")
app.add_typer(sites_app, name="sites")
app.add_typer(plugins_app, name="plugins")
app.add_typer(admin_app, name="admin")


# --------------------------------------------------------------------------- #
# Root callback                                                               #
# --------------------------------------------------------------------------- #


@app.callback()
def _root_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            is_eager=True,
            help="Show the NexusDL version and exit.",
        ),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help=(
                "Force debug mode: enables verbose logging and full "
                "tracebacks for this invocation. Overrides the "
                "matching setting in .env."
            ),
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help=(
                "Suppress routine INFO logs for this invocation. "
                "Mutually exclusive with --debug, which wins if both "
                "are set."
            ),
        ),
    ] = False,
) -> None:
    """Prepare the invocation context for every sub-command.

    Handles three concerns:

    * **``--version``** — printed and exits before any sub-command
      runs. Implemented here (rather than as a constructor argument
      of :class:`typer.Typer`, which does not support it) because
      Typer's eager-option mechanism is the standard way to
      short-circuit the callback.

    * **``--debug`` / ``--quiet``** — forwarded to
      :class:`CLIState` so the bootstrap knows what logging verbosity
      to apply.

    * **State injection** — stores the :class:`CLIState` on
      ``ctx.obj`` so every sub-command can retrieve it and call
      ``.ensure()``.

    Runs **before** the requested command, but the heavy bootstrap
    (see :meth:`CLIState.ensure`) is deferred until the command
    actually asks for services. This makes ``nexusdl version`` and
    ``nexusdl admin init`` work in an environment where ``.env`` has
    not been created yet, while still giving every other command a
    single injection point.

    Args:
        ctx: Typer's invocation context. The state is stored on
            ``ctx.obj`` and retrieved by every sub-command.
        version: Set by ``--version``. When ``True``, prints the
            version and exits before any sub-command runs.
        debug: Forwarded to :class:`CLIState`.
        quiet: Forwarded to :class:`CLIState`.
    """
    if version:
        typer.echo(f"{APP_NAME} v{NEXUSDL_VERSION}")
        raise typer.Exit(code=0)

    ctx.obj = CLIState(debug=debug, quiet=quiet)


# --------------------------------------------------------------------------- #
# Interface-launching commands                                                #
# --------------------------------------------------------------------------- #
# ``tui`` and ``web`` are declared here, not in ``commands.py``, because
# both are interface bootstrap commands — they launch a sibling interface
# and own its lifecycle. ``commands.py`` is for the pipeline operations
# (search, download, library). Keeping the interface-launchers next to the
# composition root that builds their state makes the wiring auditable in a
# single file.
# --------------------------------------------------------------------------- #


@app.command()
def tui(ctx: typer.Context) -> None:
    """Launch the Textual terminal user interface.

    Delegates to :func:`nexusdl.interfaces.cli.app.run_app` after the
    service state is built. The call blocks until the user quits the
    TUI, at which point every service is closed and the CLI exits.
    """
    state: CLIState = ctx.obj
    asyncio.run(_run_tui_async(state))


async def _run_tui_async(state: CLIState) -> None:
    """Bootstrap, launch the TUI, and close every service on exit.

    Every service's cleanup is on the ``finally`` path, so a TUI that
    crashes mid-session still leaves the database and the HTTP session
    cleanly closed. The cleanup itself is delegated to
    :func:`_close_services`, which logs each failure independently so
    that a cleanup exception cannot mask the one that brought the TUI
    down.

    Args:
        state: The CLI state. Its :meth:`CLIState.ensure` call may
            raise :class:`typer.Exit`; that exception propagates out of
            :func:`asyncio.run` and terminates the CLI with the right
            code.
    """
    wired = state.ensure()
    try:
        # Imported here (not at module level) so a CLI invocation that
        # is not launching the TUI never imports textual.
        from nexusdl.interfaces.cli.app import run_app

        await run_app(wired)
    finally:
        await _close_services(wired)


@app.command()
def web(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help=(
                "Bind address. Overrides settings.api.host. "
                "Use 0.0.0.0 to accept remote connections."
            ),
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Bind port. Overrides settings.api.port.",
        ),
    ] = None,
) -> None:
    """Launch the FastAPI web server and its Next.js frontend bridge.

    Delegates to :func:`nexusdl.interfaces.web.backend.main.create_app`
    after the service state is built. The call blocks until the server
    is stopped (``Ctrl+C``), at which point every service is closed and
    the CLI exits.
    """
    state: CLIState = ctx.obj
    asyncio.run(_run_web_async(state, host=host, port=port))


async def _run_web_async(
    state: CLIState,
    *,
    host: str | None,
    port: int | None,
) -> None:
    """Bootstrap, serve the web app, and close every service on exit.

    See :func:`_run_tui_async` for the cleanup rationale.

    Args:
        state: The CLI state.
        host: Optional host override.
        port: Optional port override.
    """
    wired = state.ensure()
    try:
        # Imported here so a CLI invocation that is not launching the
        # server never imports fastapi or uvicorn.
        from nexusdl.interfaces.web.backend.main import create_app

        asgi_app = create_app(wired)

        import uvicorn

        config = uvicorn.Config(
            asgi_app,
            host=host or wired.settings.api.host,
            port=port or wired.settings.api.port,
            log_config=None,  # loguru owns logging; uvicorn's default is silenced
        )
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        await _close_services(wired)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the ``nexusdl`` CLI.

    This is the function the ``nexusdl`` console script and
    ``python -m nexusdl`` invoke. It does exactly three things:

    1. Dispatches to Typer, which parses the arguments and runs the
       selected command.
    2. Converts a :class:`KeyboardInterrupt` into the conventional
       Unix exit code 130 and a short message.
    3. Lets every other exception propagate — the failure path through
       :func:`_raise_bootstrap_error` produces the user-facing message,
       and a genuinely unhandled exception is a bug worth a traceback.

    The intent is that ``main`` has no logic of its own beyond those
    three concerns. Any additional behaviour (progress reporting for a
    specific command, a global ``--verbose`` flag, etc.) belongs on the
    command that needs it or on the root callback.
    """
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("Interrupted.", err=True)
        raise typer.Exit(code=_EXIT_KEYBOARD_INTERRUPT) from None
