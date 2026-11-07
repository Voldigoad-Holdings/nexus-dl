"""Resolve a :class:`~nexusdl.core.models.enums.PackagingFormat` to a packager.

The factory is the single entry point every caller uses to obtain a
packager. It hides three concerns behind one method:

* **Which concrete class** implements a given format.
* **Whether that implementation ships** in this release.
* **When to import it** — the packagers are imported **lazily**, on first
  use, so importing :mod:`nexusdl.core.packaging.factory` succeeds even in
  a partial source tree (a contributor adding ``cbt_packager.py`` can run
  the ``ZIP`` and ``FOLDER`` tests before the ``CBT`` file exists).

Lazy imports also remove any risk of a circular import through the
packagers' own ``from nexusdl.core.packaging.base import BasePackager``
chain: ``factory`` imports ``base`` directly, and only touches a concrete
packager module at :meth:`PackagerFactory.create` time.

Registration model
------------------
The default registry maps every :class:`PackagingFormat` member to its
concrete class — including ``CBT``, which ships as
:mod:`nexusdl.core.packaging.cbt_packager`:

============== =============================== ===============================
Format         Module                          Class
============== =============================== ===============================
``FOLDER``     ``folder_packager``             ``FolderPackager``
``ZIP``        ``zip_packager``                ``ZipPackager``
``CBZ``        ``cbz_packager``                ``CbzPackager``
``CB7``        ``cb7_packager``                ``Cb7Packager``
``PDF``        ``pdf_packager``                ``PdfPackager``
``CBT``        ``cbt_packager``                ``CbtPackager``
============== =============================== ===============================

The PROMPT MASTER's ``PackagingFormat`` enum is the source of truth for
which formats exist; every member of that enum has a packager in this
release. If a future format is added to the enum **before** its packager
module ships, :meth:`PackagerFactory.create` will raise
:class:`~nexusdl.core.exceptions.PackagingError` naming the format and
the supported ones rather than falling back to ``ZIP`` (wrong extension)
or crashing on a ``KeyError``.

:meth:`PackagerFactory.register` lets a plugin, a test, or a future
release add or override a packager for any format without modifying this
file. The registered callable may be a class
(``PackagerFactory.register(PackagingFormat.CBZ, CbzPackager)``) or a
zero-argument factory (``register(fmt, lambda: MyPackager(config))``); the
factory only cares that calling it returns a
:class:`~nexusdl.core.packaging.base.BasePackager`.

No caching
----------
Packagers are stateless and cheap to construct (no ``__init__`` arguments,
no I/O, no resource acquisition). :meth:`create` therefore returns a fresh
instance on every call rather than caching one. This keeps the factory
free of internal state and safe to share across concurrent tasks, which
matters because :class:`~nexusdl.core.downloader.worker.DownloadWorker`
calls :meth:`create` once per chapter download.

Threading
---------
``PackagerFactory`` instances are **not** thread-safe for concurrent
mutation (``register`` / ``unregister``). Concurrent :meth:`create` calls
from any number of asyncio tasks are safe as long as no mutation happens
concurrently — which is the normal case: registration is a startup-time
operation, ``create`` runs during the download pipeline.

Dependency graph (no cycles)::

    core.exceptions              ─┐
    core.models.enums             ├──► core.packaging.factory
    core.packaging.base          ─┘
    (concrete packagers imported lazily at create() time)
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import Final

from loguru import logger

from nexusdl.core.exceptions import PackagingError
from nexusdl.core.models.enums import PackagingFormat
from nexusdl.core.packaging.base import BasePackager


__all__ = ["PackagerFactory"]


# --------------------------------------------------------------------------- #
# Default registry                                                            #
# --------------------------------------------------------------------------- #
# Format → (submodule name, class name). Every member of the
# ``PackagingFormat`` enum ships a packager in this release. Extending the
# enum means adding a line here and a sibling module; changing a class name
# means editing here and nowhere else.
# --------------------------------------------------------------------------- #

_DEFAULT_REGISTRY: Final[Mapping[PackagingFormat, tuple[str, str]]] = {
    PackagingFormat.FOLDER: ("folder_packager", "FolderPackager"),
    PackagingFormat.ZIP: ("zip_packager", "ZipPackager"),
    PackagingFormat.CBZ: ("cbz_packager", "CbzPackager"),
    PackagingFormat.CB7: ("cb7_packager", "Cb7Packager"),
    PackagingFormat.PDF: ("pdf_packager", "PdfPackager"),
    PackagingFormat.CBT: ("cbt_packager", "CbtPackager"),
}

#: Fully-qualified package prefix for lazy imports. Kept as a constant so a
#: future move to a differently named package only changes one line.
_PACKAGE_ROOT: Final[str] = "nexusdl.core.packaging"


_log = logger.bind(component="packaging.factory")


# --------------------------------------------------------------------------- #
# PackagerFactory                                                             #
# --------------------------------------------------------------------------- #


class PackagerFactory:
    """Resolve a :class:`PackagingFormat` to a ready-to-use packager.

    See the module docstring for the full contract.

    Example:
        >>> factory = PackagerFactory()
        >>> packager = factory.create(PackagingFormat.CBZ)
        >>> output = await packager.package(pages, path)
    """

    def __init__(self) -> None:
        """Initialise a factory with the default registry.

        The default registry covers every :class:`PackagingFormat`
        member (see :data:`_DEFAULT_REGISTRY`). Additional formats can
        be added with :meth:`register`.
        """
        self._registry: dict[
            PackagingFormat, Callable[[], BasePackager]
        ] = {
            fmt: _make_lazy_loader(module_name, class_name)
            for fmt, (module_name, class_name) in _DEFAULT_REGISTRY.items()
        }

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def create(self, fmt: PackagingFormat) -> BasePackager:
        """Return a packager for *fmt*.

        A fresh instance is created on every call — packagers are
        stateless, so this costs one object allocation and no resources.

        Args:
            fmt: The desired output format. Must be a
                :class:`~nexusdl.core.models.enums.PackagingFormat` member;
                strings are rejected (callers parse strings at their own
                boundary, where validation errors make sense).

        Returns:
            A concrete :class:`~nexusdl.core.packaging.base.BasePackager`.

        Raises:
            PackagingError: If *fmt* is not a
                :class:`PackagingFormat`, if no packager is registered for
                it, if the packager's module cannot be imported, if the
                module does not expose the expected class, or if the class
                does not return a :class:`BasePackager` instance.
        """
        if not isinstance(fmt, PackagingFormat):
            raise PackagingError(
                f"PackagerFactory.create expects a PackagingFormat, got "
                f"{type(fmt).__name__}"
            )

        loader = self._registry.get(fmt)
        if loader is None:
            raise PackagingError(self._missing_format_message(fmt))

        return loader()

    def register(
        self,
        fmt: PackagingFormat,
        factory: Callable[[], BasePackager],
    ) -> None:
        """Register a factory callable for *fmt*, overriding any default.

        Intended for plugins that add a new format and for tests that want
        to substitute a stub packager. The callable is invoked by
        :meth:`create` on every call; it should return a fresh instance
        (or a cached singleton if the packager is genuinely stateful and
        safe to share — but the packagers that ship with NexusDL are not).

        **Eager validation strategy.** A `factory` that is a
        :class:`BasePackager` *subclass* is validated via
        :func:`issubclass` — no instance is created, so a plugin whose
        ``__init__`` acquires resources (opens a DB, launches a browser)
        does not get its side effects triggered by registration alone.
        A `factory` that is a plain callable is **not** invoked at
        registration time — the validation defers to the first
        :meth:`create`, where a failure is still surfaced as a
        :class:`PackagingError` with the underlying exception chained.
        This is the correct trade-off: the common case (a class) is
        validated eagerly, while a plugin that genuinely needs its
        ``__init__`` side effects is never invoked twice by an accidental
        double registration.

        Args:
            fmt: The format the callable handles.
            factory: Zero-argument callable returning a
                :class:`~nexusdl.core.packaging.base.BasePackager`. A
                packager *class* works here — ``PackagerFactory.register(
                PackagingFormat.CBZ, CbzPackager)`` — because instantiating
                with no arguments is exactly what ``CbzPackager()`` does.

        Raises:
            PackagingError: If *fmt* is not a
                :class:`PackagingFormat`, if *factory* is not callable, or
                if *factory* is a class that does not subclass
                :class:`BasePackager`.
        """
        if not isinstance(fmt, PackagingFormat):
            raise PackagingError(
                f"PackagerFactory.register expects a PackagingFormat as "
                f"the first argument, got {type(fmt).__name__}"
            )
        if not callable(factory):
            raise PackagingError(
                f"PackagerFactory.register expects a callable as the "
                f"second argument, got {type(factory).__name__}"
            )

        # Eager validation for the common case: a class. ``issubclass``
        # is cheap, side-effect-free, and catches a mistyped packager
        # name at the call site rather than at the first ``create``.
        if inspect.isclass(factory) and not issubclass(
            factory, BasePackager
        ):
            raise PackagingError(
                f"registered class for format {fmt.value!r} "
                f"({factory.__name__}) does not subclass BasePackager"
            )
        # A non-class callable (a `lambda`, a `functools.partial`, a
        # custom factory) is accepted as-is. Its contract is enforced at
        # ``create`` time (any exception from `factory()` is wrapped in
        # `PackagingError`, and the returned object is type-checked).
        # Invoking it here just to "validate" would trigger `__init__`
        # side effects for no benefit — see the method docstring.

        self._registry[fmt] = factory
        _log.debug("Registered packager for format {!r}", fmt.value)

    def unregister(self, fmt: PackagingFormat) -> bool:
        """Remove the packager registration for *fmt*.

        After this call, :meth:`create` for *fmt* raises
        :class:`PackagingError` until either a new registration is made or
        the factory is reconstructed. Intended for tests and for
        explicitly disabling a shipped format.

        Args:
            fmt: The format to unregister.

        Returns:
            ``True`` iff a registration existed and was removed,
            ``False`` if *fmt* was not registered.
        """
        removed = self._registry.pop(fmt, None) is not None
        if removed:
            _log.debug("Unregistered packager for format {!r}", fmt.value)
        return removed

    def supported_formats(self) -> frozenset[PackagingFormat]:
        """Return the set of formats this factory can currently produce.

        Includes any format added via :meth:`register` and excludes any
        removed via :meth:`unregister`. Callers that want to know "which
        formats can I offer to the user?" should call this rather than
        hard-coding a set — a plugin may have registered an additional
        format.

        Returns:
            A frozenset of :class:`PackagingFormat` members. The set is a
            snapshot; later mutations of the factory do not affect it.
        """
        return frozenset(self._registry)

    def is_supported(self, fmt: PackagingFormat) -> bool:
        """Return ``True`` iff :meth:`create` would succeed for *fmt*.

        Equivalent to ``fmt in factory.supported_formats()``, provided as
        a method for readability at call sites. Does **not** attempt the
        import — a shipped packager whose module is missing (broken
        install) returns ``True`` here and fails at :meth:`create`; the
        cost of catching that at ``is_supported`` time is not worth the
        extra import on a hot path.
        """
        return fmt in self._registry

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _missing_format_message(self, fmt: PackagingFormat) -> str:
        """Build the error message for a format with no registered packager.

        The default registry ships every :class:`PackagingFormat`
        member, so this path is only reached when a caller has
        explicitly :meth:`unregister`-ed the format, or when a plugin
        has introduced a new format elsewhere without registering a
        packager here.

        Args:
            fmt: The format whose packager is missing.

        Returns:
            A human-readable error message.
        """
        supported = sorted(f.value for f in self._registry)
        return (
            f"no packager registered for format {fmt.value!r}; "
            f"supported formats: {supported}. "
            f"Register a custom packager with PackagerFactory.register "
            f"if you need {fmt.value!r}."
        )


# --------------------------------------------------------------------------- #
# Lazy loading helper                                                        #
# --------------------------------------------------------------------------- #


def _make_lazy_loader(
    module_name: str, class_name: str
) -> Callable[[], BasePackager]:
    """Return a callable that imports *class_name* from *module_name* on call.

    The returned callable resolves
    ``nexusdl.core.packaging.<module_name>.<class_name>`` at call time,
    instantiates the class with no arguments, and validates that the
    instance is a :class:`~nexusdl.core.packaging.base.BasePackager`.

    This is a module-level function rather than a method so it has no
    ``self`` and cannot accidentally capture mutable factory state. It
    closes over only its two string arguments, both of which are
    compile-time constants.

    Args:
        module_name: Submodule name (e.g. ``"cbz_packager"``). Resolved
            relative to :data:`_PACKAGE_ROOT`.
        class_name: Name of the class expected in the module
            (e.g. ``"CbzPackager"``).

    Returns:
        A zero-argument callable that produces a fresh
        :class:`BasePackager` each time it is invoked.
    """

    full_module_name = f"{_PACKAGE_ROOT}.{module_name}"

    def _load() -> BasePackager:
        try:
            module = importlib.import_module(full_module_name)
        except ImportError as exc:
            raise PackagingError(
                f"packager module {full_module_name!r} is not available; "
                f"the format may not be implemented in this release: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            cls = getattr(module, class_name)
        except AttributeError as exc:
            raise PackagingError(
                f"module {full_module_name!r} does not expose class "
                f"{class_name!r}"
            ) from exc

        if not isinstance(cls, type):
            raise PackagingError(
                f"{full_module_name}.{class_name} is not a class: "
                f"got {type(cls).__name__}"
            )

        try:
            instance = cls()
        except Exception as exc:  # noqa: BLE001 — surface the real cause
            raise PackagingError(
                f"cannot instantiate "
                f"{full_module_name}.{class_name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(instance, BasePackager):
            raise PackagingError(
                f"{full_module_name}.{class_name} does not subclass "
                f"BasePackager (got {type(instance).__name__})"
            )

        return instance

    return _load
