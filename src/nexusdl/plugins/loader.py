"""Plugin discovery, loading, and hook dispatch for NexusDL.

The loader is the single entry point through which the framework learns
about external plugins. It performs five things, in this order:

1. **Discovery** — walk the configured plugin roots and identify candidate
   plugin directories. Directories whose name starts with ``_`` (such as
   the shipped ``_example/``), directories listed in ``settings.plugins.disabled``,
   and directories missing either ``nexus.plugin.yaml`` or ``plugin.py`` are
   skipped and recorded in the :class:`LoadReport`.
2. **Import** — dynamically load each ``plugin.py`` as a standalone module
   (not as a Python package), under a namespaced internal module name so
   plugins can never shadow framework modules.
3. **Conformance** — find exactly one top-level class implementing the
   mandatory members of :class:`~nexusdl.plugins.api.NexusPlugin`, then run
   the full validation pipeline
   (:func:`nexusdl.plugins.validators.load_manifest` →
   :func:`~nexusdl.plugins.validators.validate_manifest_compatibility` →
   :func:`~nexusdl.plugins.validators.validate_class_matches_manifest`).
4. **Lifecycle** — build a :class:`~nexusdl.plugins.api.PluginContext` and
   call ``on_load(context)``. If ``on_load`` raises, the plugin is rolled
   back (``on_unload`` is attempted, module is purged from ``sys.modules``)
   and reported as failed.
5. **Dispatch** — expose four coroutine dispatchers that the download
   pipeline calls (``on_before_download``, ``on_after_download``,
   ``on_page_downloaded``, ``on_error``). Each dispatcher invokes only the
   plugins that *declared* the hook in their manifest, isolates per-plugin
   failures, and re-raises :class:`asyncio.CancelledError` unchanged.

Design invariants
-----------------
* **Isolation** — one bad plugin never prevents others from loading, and no
  plugin exception ever breaks the download pipeline (it is logged instead).
* **Cancellation-safe** — ``asyncio.CancelledError`` always propagates.
* **Lock-free dispatch** — ``self._loaded`` is an immutable tuple swapped
  atomically on load/unload, so dispatchers need no lock and always observe
  a consistent snapshot.
* **No hook-name literals** — every hook name comes from
  :mod:`nexusdl.plugins.hooks`. Renaming a hook requires editing only
  ``hooks.py`` and ``api.py``.
* **Read-only paths** — plugin code lives in the plugin directory and is
  never written by the framework; mutable state goes under
  ``<data_root>/<plugin_name>/``, guaranteed to exist before ``on_load``.

This module is the only one that imports plugin ``plugin.py`` files. It is
also the only one that mutates ``sys.modules`` for plugins.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

import platformdirs
from loguru import logger

from nexusdl.core.exceptions import PluginError
from nexusdl.plugins.api import PluginContext
from nexusdl.plugins.hooks import (
    HOOK_ON_AFTER_DOWNLOAD,
    HOOK_ON_BEFORE_DOWNLOAD,
    HOOK_ON_ERROR,
    HOOK_ON_LOAD,
    HOOK_ON_PAGE_DOWNLOADED,
    HOOK_ON_UNLOAD,
)
from nexusdl.plugins.validators import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest,
    validate_class_matches_manifest,
    validate_manifest_compatibility,
)

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.config import Settings
    from nexusdl.core.events import EventBus
    from nexusdl.core.models.download import DownloadResult, DownloadTask
    from nexusdl.core.models.page import Page
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.plugins.api import NexusPlugin


__all__ = [
    "LoadReport",
    "LoadedPlugin",
    "PluginLoader",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the plugin implementation module, expected alongside the
#: manifest in each plugin directory.
PLUGIN_MODULE_FILENAME: Final[str] = "plugin.py"

#: Prefix for the internal module name under which each plugin is imported.
#: Prefixed with an underscore so plugins cannot accidentally shadow
#: first-party modules in ``sys.modules``.
_MODULE_PREFIX: Final[str] = "_nexusdl_plugin_"

#: Attributes every conforming plugin class must declare for discovery.
#: Imported from :mod:`nexusdl.plugins.hooks` so that a rename in the
#: canonical module propagates automatically. The four optional pipeline
#: hooks are **not** listed here — they are validated later against
#: ``manifest.hooks``.
_REQUIRED_ATTRS: Final[tuple[str, ...]] = (
    "name",
    "version",
    HOOK_ON_LOAD,
    HOOK_ON_UNLOAD,
)

#: Files that must exist in a directory for it to be considered a plugin.
_REQUIRED_FILES: Final[tuple[str, ...]] = (MANIFEST_FILENAME, PLUGIN_MODULE_FILENAME)


# --------------------------------------------------------------------------- #
# Data classes                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """A successfully loaded plugin and the state needed to dispatch to it.

    Instances are immutable: the loader replaces the entire
    :class:`LoadedPlugin` on hot-reload rather than mutating one in place,
    which keeps the dispatch snapshot consistent for concurrent callers.

    Attributes:
        name: Plugin identifier (matches ``manifest.name``).
        version: Plugin version (matches ``manifest.version``).
        instance: The instantiated plugin object.
        manifest: The validated manifest returned by
            :func:`nexusdl.plugins.validators.load_manifest`.
        module: The Python module the plugin class was imported from.
        directory: The plugin directory on disk (read-only by convention).
        context: The :class:`~nexusdl.plugins.api.PluginContext` passed to
            ``on_load``.
        logger: A :mod:`loguru` logger bound to ``plugin=<name>``, so every
            log line a plugin produces is tagged with its origin.
    """

    name: str
    version: str
    instance: NexusPlugin
    manifest: PluginManifest
    module: ModuleType
    directory: Path
    context: PluginContext
    logger: Logger


@dataclass(slots=True)
class LoadReport:
    """Aggregate outcome of a :meth:`PluginLoader.load_all` call.

    The loader never raises on individual plugin failures — it records them
    here and moves on. Callers (e.g. the CLI ``plugins list`` command) can
    render the report verbatim.

    Attributes:
        loaded: Names of plugins that loaded and started successfully.
        failed: Mapping ``name -> error message`` for plugins that failed to
            load. The plugin is **not** in the dispatch snapshot.
        skipped: Mapping ``name-or-path -> reason`` for directories that were
            not attempted (underscore prefix, disabled in settings, missing
            required files, unreadable directory).
    """

    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """``True`` iff no plugin failed to load."""
        return not self.failed

    @property
    def total(self) -> int:
        """Total number of plugin directories examined (all categories)."""
        return len(self.loaded) + len(self.failed) + len(self.skipped)


# --------------------------------------------------------------------------- #
# Loader                                                                      #
# --------------------------------------------------------------------------- #


class PluginLoader:
    """Discover, load, unload, and dispatch to NexusDL plugins.

    One instance is created by each composition root (CLI, TUI, GUI, Web) and
    is **not** shared across threads — it is asyncio-native and must be driven
    from a single event loop.

    Example:
        >>> loader = PluginLoader(
        ...     settings=settings,
        ...     event_bus=event_bus,
        ...     http_session=session,
        ... )
        >>> report = await loader.load_all()
        >>> if not report.ok:
        ...     print(report.failed)
        >>> task = await loader.dispatch_on_before_download(task)
    """

    def __init__(
        self,
        *,
        settings: Settings,
        event_bus: EventBus,
        http_session: HttpSession | None = None,
        plugin_dirs: Iterable[Path] | None = None,
        data_dir_root: Path | None = None,
        disabled: Iterable[str] | None = None,
    ) -> None:
        """Initialise the loader (no I/O, no imports).

        Args:
            settings: The application settings object. Used as the default
                source for ``plugin_dirs``, ``disabled``, and
                ``data_dir_root`` when those are not passed explicitly.
            event_bus: Shared :class:`~nexusdl.core.events.EventBus` passed
                to each plugin via its :class:`~nexusdl.plugins.api.PluginContext`.
            http_session: Optional shared
                :class:`~nexusdl.core.session.http_session.HttpSession`.
                Plugins that perform network I/O should reuse it rather than
                opening their own client. ``None`` in contexts without
                network access (e.g. an offline TUI settings screen).
            plugin_dirs: Roots to scan for plugin subdirectories. Defaults to
                ``settings.plugins.dirs`` if present, otherwise the empty
                tuple.
            data_dir_root: Root under which per-plugin data directories are
                created. Defaults to ``settings.plugins.data_dir`` if present,
                otherwise ``platformdirs.user_data_path("nexusdl") / "plugins"``.
            disabled: Plugin names to skip. Defaults to
                ``settings.plugins.disabled`` if present, otherwise empty.
        """
        self._settings = settings
        self._event_bus = event_bus
        self._http_session = http_session

        plugins_cfg = getattr(settings, "plugins", None)

        self._plugin_dirs: tuple[Path, ...] = tuple(
            Path(p)
            for p in (
                plugin_dirs
                if plugin_dirs is not None
                else getattr(plugins_cfg, "dirs", ()) or ()
            )
        )
        self._disabled: frozenset[str] = frozenset(
            disabled
            if disabled is not None
            else getattr(plugins_cfg, "disabled", ()) or ()
        )
        self._data_root: Path = Path(
            data_dir_root
            if data_dir_root is not None
            else getattr(plugins_cfg, "data_dir", None)
            or (Path(platformdirs.user_data_path("nexusdl")) / "plugins")
        )

        #: Immutable snapshot consumed lock-free by every dispatcher.
        self._loaded: tuple[LoadedPlugin, ...] = ()
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="plugins.loader")

    # ------------------------------------------------------------------ #
    # Public read API                                                     #
    # ------------------------------------------------------------------ #

    def list_plugins(self) -> list[LoadedPlugin]:
        """Return the currently loaded plugins, sorted by name.

        Returns:
            A new list; mutating it does not affect the loader.
        """
        return sorted(self._loaded, key=lambda p: p.name)

    def get_plugin(self, name: str) -> LoadedPlugin | None:
        """Return the loaded plugin with the given name, or ``None``.

        Args:
            name: Plugin identifier (as declared in ``nexus.plugin.yaml``).
        """
        for plugin in self._loaded:
            if plugin.name == name:
                return plugin
        return None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def load_all(
        self,
        *,
        plugin_dirs: Iterable[Path] | None = None,
        strict: bool = False,
    ) -> LoadReport:
        """Discover and load every eligible plugin under the given roots.

        Loading is additive: plugins already loaded are left untouched, and
        newly discovered plugins are appended to the dispatch snapshot.

        Args:
            plugin_dirs: Roots to scan. Defaults to the roots passed to the
                constructor.
            strict: If ``True``, the first plugin failure aborts the whole
                load and is raised as
                :class:`~nexusdl.core.exceptions.PluginError`. Used by CI
                and the ``nexusdl plugins check`` command. In the default
                (lenient) mode, failures are recorded in the report and
                other plugins still load.

        Returns:
            A :class:`LoadReport` describing what loaded, failed, or was
            skipped.

        Raises:
            PluginError: Only when ``strict=True`` and a plugin fails.
        """
        report = LoadReport()
        roots = tuple(plugin_dirs) if plugin_dirs is not None else self._plugin_dirs

        if not roots:
            self._log.debug("No plugin directories configured; nothing to load.")
            return report

        async with self._lock:
            for root in roots:
                if not root.is_dir():
                    report.skipped[str(root)] = "directory does not exist"
                    continue
                for plugin_dir in self._iter_plugin_dirs(root, report):
                    await self._load_one(plugin_dir, report, strict=strict)
        return report

    async def unload_all(self) -> None:
        """Unload every plugin and reset the dispatch snapshot.

        ``on_unload`` is invoked on each plugin in **reverse load order** so
        a plugin can rely on dependencies loaded after it still being alive.
        Exceptions raised by ``on_unload`` are logged and swallowed: unload
        runs during shutdown, where raising accomplishes nothing useful.
        """
        async with self._lock:
            self._unload_snapshot(self._loaded)
            self._loaded = ()

    async def unload_plugin(self, name: str) -> None:
        """Unload a single plugin by name.

        Used by ``nexusdl plugins disable`` and by tests. If no plugin with
        that name is loaded, this is a no-op.

        Args:
            name: Plugin identifier.
        """
        async with self._lock:
            keep: list[LoadedPlugin] = []
            for plugin in self._loaded:
                if plugin.name == name:
                    self._unload_snapshot((plugin,))
                else:
                    keep.append(plugin)
            self._loaded = tuple(keep)

    # ------------------------------------------------------------------ #
    # Hook dispatch — called by the download pipeline                     #
    # ------------------------------------------------------------------ #

    async def dispatch_on_before_download(
        self, task: DownloadTask
    ) -> DownloadTask:
        """Run ``on_before_download`` on every opt-in plugin, chaining results.

        Each plugin receives the task as transformed by the previous one. A
        plugin that returns ``None`` (against the Protocol contract) is
        ignored rather than corrupting the chain. Any exception raised by a
        plugin is logged and the next plugin sees the task unchanged.

        Args:
            task: The task about to be executed.

        Returns:
            The final task — equal to ``task`` if no plugin altered it.
        """
        current: DownloadTask = task
        for plugin in self._loaded:
            if HOOK_ON_BEFORE_DOWNLOAD not in plugin.manifest.hooks:
                continue
            try:
                transformed = await plugin.instance.on_before_download(current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — isolation is intentional
                plugin.logger.exception(
                    "on_before_download failed for task_id={}: {}",
                    getattr(current, "id", "<unknown>"),
                    exc,
                )
                continue
            if transformed is not None:
                current = transformed
        return current

    async def dispatch_on_after_download(
        self, result: DownloadResult
    ) -> DownloadResult:
        """Run ``on_after_download`` on every opt-in plugin, chaining results.

        Args:
            result: The outcome of a completed task.

        Returns:
            The final result — equal to ``result`` if no plugin altered it.
        """
        current: DownloadResult = result
        for plugin in self._loaded:
            if HOOK_ON_AFTER_DOWNLOAD not in plugin.manifest.hooks:
                continue
            try:
                transformed = await plugin.instance.on_after_download(current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                plugin.logger.exception(
                    "on_after_download failed: {}", exc
                )
                continue
            if transformed is not None:
                current = transformed
        return current

    async def dispatch_on_page_downloaded(
        self, page: Page, path: Path
    ) -> None:
        """Notify every opt-in plugin that a page has been written to disk.

        Observer-only: the return value of each hook is ignored. Failures are
        logged and never propagate — a plugin cannot break a download.

        Args:
            page: The downloaded page metadata (``page.index`` is 0-based).
            path: Filesystem location of the written image.
        """
        for plugin in self._loaded:
            if HOOK_ON_PAGE_DOWNLOADED not in plugin.manifest.hooks:
                continue
            try:
                await plugin.instance.on_page_downloaded(page, path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                plugin.logger.exception(
                    "on_page_downloaded failed for index={}: {}",
                    getattr(page, "index", "<unknown>"),
                    exc,
                )

    async def dispatch_on_error(
        self, task_id: str, error: Exception
    ) -> None:
        """Notify every opt-in plugin that a download task failed.

        Observer-only: the return value of each hook is ignored. Failures are
        logged and never propagate. ``CancelledError`` never reaches this
        dispatcher because the pipeline treats cancellation as a terminal
        state, not an error.

        Args:
            task_id: Identifier of the failed task.
            error: The exception raised by the worker.
        """
        for plugin in self._loaded:
            if HOOK_ON_ERROR not in plugin.manifest.hooks:
                continue
            try:
                await plugin.instance.on_error(task_id, error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                plugin.logger.exception(
                    "on_error failed for task_id={}: {}",
                    task_id,
                    exc,
                )

    # ------------------------------------------------------------------ #
    # Internals — discovery                                               #
    # ------------------------------------------------------------------ #

    def _iter_plugin_dirs(
        self, root: Path, report: LoadReport
    ) -> Iterable[Path]:
        """Yield candidate plugin directories under *root*.

        Filters applied (any match → skip, recorded in ``report.skipped``):

        * name starts with ``_`` (reserved, e.g. ``_example``);
        * name is in ``self._disabled``;
        * either ``nexus.plugin.yaml`` or ``plugin.py`` is missing.

        Args:
            root: Directory to scan (non-recursive, one level deep).
            report: Report to which skip reasons are appended.

        Yields:
            Paths of directories that pass the static checks.
        """
        try:
            entries = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError as exc:
            report.skipped[str(root)] = f"cannot list: {exc}"
            return

        for candidate in entries:
            if candidate.name.startswith("_"):
                report.skipped[candidate.name] = (
                    "name starts with '_' (reserved)"
                )
                continue
            if candidate.name in self._disabled:
                report.skipped[candidate.name] = "disabled in settings"
                continue
            missing = [
                f for f in _REQUIRED_FILES if not (candidate / f).is_file()
            ]
            if missing:
                report.skipped[candidate.name] = (
                    f"missing required file(s): {', '.join(missing)}"
                )
                continue
            yield candidate

    # ------------------------------------------------------------------ #
    # Internals — single-plugin load / unload                             #
    # ------------------------------------------------------------------ #

    async def _load_one(
        self,
        plugin_dir: Path,
        report: LoadReport,
        *,
        strict: bool,
    ) -> None:
        """Load a single plugin directory, recording the outcome in *report*.

        Args:
            plugin_dir: Directory containing ``nexus.plugin.yaml`` and
                ``plugin.py``.
            report: Report to update in place.
            strict: If ``True``, re-raise the first error as ``PluginError``.

        Raises:
            PluginError: Only when ``strict=True`` and loading fails.
        """
        name_guess = plugin_dir.name
        try:
            plugin = await self._load_plugin_instance(plugin_dir)
        except Exception as exc:  # noqa: BLE001 — one bad plugin must not abort the rest
            message = f"{type(exc).__name__}: {exc}"
            report.failed[name_guess] = message
            self._log.warning(
                "Failed to load plugin from {}: {}", plugin_dir, message
            )
            if strict:
                if isinstance(exc, PluginError):
                    raise
                raise PluginError(
                    f"failed to load plugin {name_guess!r}: {message}"
                ) from exc
            return

        report.loaded.append(plugin.name)
        self._loaded = (*self._loaded, plugin)
        self._log.info(
            "Loaded plugin {} v{} from {}",
            plugin.name,
            plugin.version,
            plugin_dir,
        )

    async def _load_plugin_instance(self, plugin_dir: Path) -> LoadedPlugin:
        """Execute the full load sequence for a single plugin directory.

        Steps: load manifest → import module → find class → validate →
        build context → call ``on_load``. Any failure triggers rollback
        (module purge + ``on_unload`` attempt) before the exception is
        re-raised.

        Args:
            plugin_dir: Directory containing the plugin.

        Returns:
            A fully initialized :class:`LoadedPlugin`.

        Raises:
            PluginError: On any validation or import failure.
            Exception: Any exception raised by ``on_load`` is propagated
                after rollback.
        """
        manifest = load_manifest(plugin_dir)
        name = manifest.name

        module = self._import_plugin_module(name, plugin_dir)
        instance: NexusPlugin | None = None
        try:
            cls = _find_plugin_class(module)

            # Re-run the two remaining validation layers against the loaded
            # class. The manifest has already been parsed above, so this
            # avoids a redundant read of ``nexus.plugin.yaml``.
            validate_manifest_compatibility(manifest)
            validate_class_matches_manifest(cls, manifest)

            plugin_logger = logger.bind(component="plugin", plugin=name)
            data_dir = self._data_root / name
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PluginError(
                    f"cannot create data directory {data_dir} for "
                    f"plugin {name!r}: {exc}"
                ) from exc

            context = PluginContext(
                logger=plugin_logger,
                settings=self._settings,
                event_bus=self._event_bus,
                plugin_dir=plugin_dir,
                data_dir=data_dir,
                http_session=self._http_session,
            )

            instance = cls()
            self._log.debug(
                "Calling on_load for plugin {} (dir={}, data={})",
                name,
                plugin_dir,
                data_dir,
            )
            instance.on_load(context)
        except BaseException:
            # Roll back: give the (possibly half-initialized) plugin a
            # chance to clean up, then purge the module so a reload starts
            # from a clean slate.
            if instance is not None:
                try:
                    instance.on_unload()
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.warning(
                        "on_unload raised during rollback of plugin {}: {}",
                        name,
                        cleanup_exc,
                    )
            self._purge_module(module.__name__)
            raise

        return LoadedPlugin(
            name=name,
            version=manifest.version,
            instance=instance,
            manifest=manifest,
            module=module,
            directory=plugin_dir,
            context=context,
            logger=plugin_logger,
        )

    def _unload_snapshot(self, plugins: Iterable[LoadedPlugin]) -> None:
        """Call ``on_unload`` on each plugin in *plugins* (in reverse order).

        Swallows and logs every exception: unload runs during shutdown.

        Args:
            plugins: The plugins to unload.
        """
        for plugin in reversed(tuple(plugins)):
            try:
                plugin.instance.on_unload()
            except Exception as exc:  # noqa: BLE001
                plugin.logger.exception(
                    "on_unload failed for plugin {}: {}", plugin.name, exc
                )
            finally:
                self._purge_module(plugin.module.__name__)

    # ------------------------------------------------------------------ #
    # Internals — module management                                       #
    # ------------------------------------------------------------------ #

    def _import_plugin_module(
        self, plugin_name: str, plugin_dir: Path
    ) -> ModuleType:
        """Import ``plugin.py`` as a standalone module under an internal name.

        The module is registered in ``sys.modules`` *before* execution so
        intra-plugin imports work, and purged on failure so a retry starts
        clean. Plugins are never imported as packages — a ``__init__.py``
        that may exist in a plugin directory (as in ``_example/``) is
        ignored.

        Args:
            plugin_name: Value of ``manifest.name``; used to build the module
                name ``_nexusdl_plugin_<name>``.
            plugin_dir: Directory containing ``plugin.py``.

        Returns:
            The imported module.

        Raises:
            PluginError: If the file is missing or the import fails.
        """
        plugin_py = plugin_dir / PLUGIN_MODULE_FILENAME
        if not plugin_py.is_file():
            raise PluginError(
                f"missing {PLUGIN_MODULE_FILENAME} in {plugin_dir}"
            )

        module_name = f"{_MODULE_PREFIX}{plugin_name}"
        if module_name in sys.modules:
            # Loaded earlier in this process — reuse the module so reloads
            # don't leak duplicate module objects.
            return sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, plugin_py)
        if spec is None or spec.loader is None:
            raise PluginError(
                f"cannot create import spec for plugin {plugin_name!r} "
                f"at {plugin_py}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginError(
                f"error importing plugin {plugin_name!r} from {plugin_py}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return module

    @staticmethod
    def _purge_module(module_name: str) -> None:
        """Remove *module_name* from ``sys.modules`` (idempotent).

        Args:
            module_name: Name of the module to purge.
        """
        sys.modules.pop(module_name, None)


# --------------------------------------------------------------------------- #
# Plugin class discovery                                                      #
# --------------------------------------------------------------------------- #


def _find_plugin_class(module: ModuleType) -> type:
    """Return the unique top-level class in *module* implementing the required members.

    A class is considered a candidate when:

    * it is a class object defined in ``module`` (not imported into it);
    * its name does not start with ``_``;
    * it declares all attributes in :data:`_REQUIRED_ATTRS`
      (``name``, ``version``, and the two sync lifecycle hooks).

    The check is **structural** rather than a runtime ``isinstance`` against
    :class:`~nexusdl.plugins.api.NexusPlugin`: the protocol intentionally
    avoids ``@runtime_checkable``, and structural inspection gives better
    error messages (we can report *which* required attributes are missing).

    Args:
        module: The imported ``plugin.py`` module.

    Returns:
        The single candidate class.

    Raises:
        PluginError: If zero or more than one candidate is found. The message
            names the module and, for the empty case, lists the plausible
            classes with their missing attributes.
    """
    candidates: list[type] = []
    rejected: list[tuple[str, list[str]]] = []

    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj: Any = getattr(module, attr_name)
        if not isinstance(obj, type):
            continue
        if obj.__module__ != module.__name__:
            # Imported from elsewhere — not the plugin's own class.
            continue
        missing = [a for a in _REQUIRED_ATTRS if not hasattr(obj, a)]
        if missing:
            rejected.append((obj.__name__, missing))
            continue
        candidates.append(obj)

    if not candidates:
        details = ""
        if rejected:
            details = " Candidates missing required attributes: " + "; ".join(
                f"{name} (missing: {', '.join(miss)})"
                for name, miss in rejected
            )
        raise PluginError(
            f"no NexusPlugin class found in module {module.__name__!r}. "
            f"Expected a top-level class declaring "
            f"{', '.join(_REQUIRED_ATTRS)}.{details}"
        )

    if len(candidates) > 1:
        raise PluginError(
            f"ambiguous plugin entry point in module {module.__name__!r}: "
            f"found {len(candidates)} candidate classes "
            f"({', '.join(sorted(c.__name__ for c in candidates))}). "
            f"Exactly one top-level class may implement NexusPlugin."
        )

    return candidates[0]
