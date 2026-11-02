"""Canonical definitions and introspection helpers for NexusDL plugin hooks.

This module is the **single source of truth** for the set of hooks that make
up the :class:`~nexusdl.plugins.api.NexusPlugin` protocol. Everything that
needs to know "which hooks exist, what flavour (sync/async) each one is, and
what the plugin class actually implements" imports from here:

* :mod:`nexusdl.plugins.validators` — validates ``manifest.hooks`` against
  :data:`ALL_HOOK_NAMES` and checks each declared hook's sync/async flavour
  against :data:`SYNC_HOOK_NAMES` / :data:`ASYNC_HOOK_NAMES`.
* :mod:`nexusdl.plugins.loader` — uses :func:`collect_implemented_hooks` in
  error messages and :func:`check_hook_flavour` for conformance.
* :mod:`nexusdl.interfaces.cli.commands` — uses :data:`HOOK_SPECS` to render
  ``nexusdl plugins list`` and ``nexusdl plugins info``.
* Tests — :func:`verify_protocol_consistency` catches drift between this
  module and :class:`~nexusdl.plugins.api.NexusPlugin`.

If you add, rename, or change the flavour of a hook, the only correct place
to start is **here** — then mirror the change in ``api.py`` and run
``verify_protocol_consistency`` (exposed as a test).

Dependency graph::

    core.exceptions.PluginError
                  ▲
                  │
              hooks.py            ← you are here (no plugins.* imports)
                  ▲
                  │
          validators.py  ─►  loader.py  ─►  interfaces/*

No module in this package may import from :mod:`nexusdl.plugins.hooks` at
import time except :mod:`validators`, :mod:`loader`, and interface modules —
:mod:`api` and :mod:`_example.*` must remain independent so ``hooks.py`` can
be imported without pulling in the concrete plugin protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

from nexusdl.core.exceptions import PluginError

if TYPE_CHECKING:
    from nexusdl.core.models.download import DownloadResult, DownloadTask
    from nexusdl.core.models.page import Page
    from nexusdl.plugins.api import PluginContext


__all__ = [
    # Canonical hook names (constants)
    "HOOK_ON_LOAD",
    "HOOK_ON_UNLOAD",
    "HOOK_ON_BEFORE_DOWNLOAD",
    "HOOK_ON_AFTER_DOWNLOAD",
    "HOOK_ON_PAGE_DOWNLOADED",
    "HOOK_ON_ERROR",
    # Aggregated views
    "ALL_HOOK_NAMES",
    "ASYNC_HOOK_NAMES",
    "SYNC_HOOK_NAMES",
    "HOOK_SPECS",
    "HOOK_SPECS_BY_NAME",
    # Type aliases
    "HookFlavour",
    "HookName",
    "HookSpec",
    # Introspection — static (name-based)
    "get_hook_flavour",
    "get_hook_spec",
    "is_async_hook",
    "is_known_hook",
    "is_sync_hook",
    # Introspection — dynamic (class-based)
    "check_hook_flavour",
    "collect_implemented_hooks",
    "implements_hook",
    # Test helper
    "verify_protocol_consistency",
]


# --------------------------------------------------------------------------- #
# Canonical hook names                                                        #
# --------------------------------------------------------------------------- #
# These literals are the source of truth. Do not stringify them elsewhere —
# always reference the constant. ``HookName`` below is a ``Literal`` of the
# *values*, so a typo in a caller triggers ``mypy --strict`` immediately.
# --------------------------------------------------------------------------- #

HOOK_ON_LOAD: Final = "on_load"
HOOK_ON_UNLOAD: Final = "on_unload"
HOOK_ON_BEFORE_DOWNLOAD: Final = "on_before_download"
HOOK_ON_AFTER_DOWNLOAD: Final = "on_after_download"
HOOK_ON_PAGE_DOWNLOADED: Final = "on_page_downloaded"
HOOK_ON_ERROR: Final = "on_error"


#: Closed union of every hook name. Use this type wherever a function takes
#: or returns "a hook" — it makes ``mypy --strict`` catch typos.
HookName: TypeAlias = Literal[
    "on_load",
    "on_unload",
    "on_before_download",
    "on_after_download",
    "on_page_downloaded",
    "on_error",
]

#: Whether a hook must be declared ``def`` or ``async def`` on the plugin class.
HookFlavour: TypeAlias = Literal["sync", "async"]


# --------------------------------------------------------------------------- #
# Hook specifications                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HookSpec:
    """Static description of a single plugin hook.

    Rendered verbatim by ``nexusdl plugins list`` / ``nexusdl plugins info``
    and by ``docs/source/development/plugins.md`` (generated section). The
    ``summary`` is user-facing: keep it one sentence, imperative, no trailing
    period.

    Attributes:
        name: The hook's method name on the plugin class.
        flavour: ``"sync"`` for :data:`HOOK_ON_LOAD` / :data:`HOOK_ON_UNLOAD`,
            ``"async"`` for the four pipeline hooks.
        summary: One-line description shown in CLI output and docs.
    """

    name: HookName
    flavour: HookFlavour
    summary: str


#: Ordered tuple of every hook, in lifecycle order: load → pipeline → unload.
#: Order matters for the CLI listing (``nexusdl plugins info`` renders them
#: in this order) but is otherwise not significant.
HOOK_SPECS: Final[tuple[HookSpec, ...]] = (
    HookSpec(
        name="on_load",
        flavour="sync",
        summary="Receive the plugin context and initialize state",
    ),
    HookSpec(
        name="on_before_download",
        flavour="async",
        summary="Inspect or transform a task before it starts downloading",
    ),
    HookSpec(
        name="on_after_download",
        flavour="async",
        summary="Observe or transform a completed download result",
    ),
    HookSpec(
        name="on_page_downloaded",
        flavour="async",
        summary="Be notified of each page written to disk",
    ),
    HookSpec(
        name="on_error",
        flavour="async",
        summary="Be notified when a download task fails",
    ),
    HookSpec(
        name="on_unload",
        flavour="sync",
        summary="Persist state and release resources before shutdown",
    ),
)

#: Immutable name→spec lookup, computed once at import.
HOOK_SPECS_BY_NAME: Final[Mapping[HookName, HookSpec]] = MappingProxyType(
    {spec.name: spec for spec in HOOK_SPECS}
)

#: Every hook name, as a frozen set. Used by :mod:`validators` to reject
#: unknown names in ``manifest.hooks``.
ALL_HOOK_NAMES: Final[frozenset[str]] = frozenset(HOOK_SPECS_BY_NAME)

#: Hooks that must be declared ``def`` (not ``async def``).
SYNC_HOOK_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in HOOK_SPECS if spec.flavour == "sync"
)

#: Hooks that must be declared ``async def``.
ASYNC_HOOK_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in HOOK_SPECS if spec.flavour == "async"
)


# --------------------------------------------------------------------------- #
# Static introspection (name-based)                                           #
# --------------------------------------------------------------------------- #


def is_known_hook(name: str) -> bool:
    """Return ``True`` iff *name* is a hook declared in :data:`HOOK_SPECS`.

    Args:
        name: Candidate hook name.

    Returns:
        ``True`` for the six canonical hooks, ``False`` for anything else
        (including names that look like hooks, e.g. ``"on_startup"``).
    """
    return name in HOOK_SPECS_BY_NAME


def get_hook_spec(name: str) -> HookSpec:
    """Return the :class:`HookSpec` for *name*.

    Args:
        name: A known hook name.

    Returns:
        The matching specification.

    Raises:
        PluginError: If *name* is not a known hook. The message lists every
            valid name so callers can fix their typo without consulting
            another file.
    """
    spec = HOOK_SPECS_BY_NAME.get(name)  # type: ignore[arg-type]
    if spec is None:
        raise PluginError(
            f"unknown plugin hook {name!r}; valid hooks are "
            f"{sorted(ALL_HOOK_NAMES)}"
        )
    return spec


def get_hook_flavour(name: str) -> HookFlavour:
    """Return ``"sync"`` or ``"async"`` for the named hook.

    Args:
        name: A known hook name.

    Returns:
        The declared flavour of *name*.

    Raises:
        PluginError: If *name* is not a known hook (via :func:`get_hook_spec`).
    """
    return get_hook_spec(name).flavour


def is_sync_hook(name: str) -> bool:
    """Return ``True`` iff *name* is a known hook with ``flavour == "sync"``.

    Args:
        name: Candidate hook name. Unknown names return ``False``.
    """
    spec = HOOK_SPECS_BY_NAME.get(name)  # type: ignore[arg-type]
    return spec is not None and spec.flavour == "sync"


def is_async_hook(name: str) -> bool:
    """Return ``True`` iff *name* is a known hook with ``flavour == "async"``.

    Args:
        name: Candidate hook name. Unknown names return ``False``.
    """
    spec = HOOK_SPECS_BY_NAME.get(name)  # type: ignore[arg-type]
    return spec is not None and spec.flavour == "async"


# --------------------------------------------------------------------------- #
# Dynamic introspection (class-based)                                         #
# --------------------------------------------------------------------------- #


def implements_hook(cls: type, name: str) -> bool:
    """Return ``True`` iff *cls* exposes a callable attribute named *name*.

    "Implemented" is defined structurally: the attribute exists on the class
    (whether declared explicitly or inherited from a mixin) and is callable.
    This matches how :mod:`loader` decides whether to dispatch.

    Args:
        cls: The plugin class to inspect.
        name: A known hook name.

    Returns:
        ``True`` iff the attribute exists and is callable. Unknown hook names
        return ``False`` (they are never implemented by a well-formed class).
    """
    if not is_known_hook(name):
        return False
    return callable(getattr(cls, name, None))


def collect_implemented_hooks(cls: type) -> dict[HookName, Callable[..., Any]]:
    """Return every hook implemented by *cls*, keyed by hook name.

    Iterates :data:`HOOK_SPECS` in canonical order, so the resulting dict is
    deterministic. Used by :mod:`loader` for diagnostic messages and by tests
    to assert conformance.

    Args:
        cls: The plugin class to inspect.

    Returns:
        A mapping ``hook_name -> callable`` for every implemented hook. The
        returned callables are the raw attributes (unbound functions for
        regular methods; the class attribute itself for ``staticmethod`` /
        ``classmethod``). Bind them on the instance if you need ``self``.
    """
    return {
        spec.name: getattr(cls, spec.name)
        for spec in HOOK_SPECS
        if implements_hook(cls, spec.name)
    }


def check_hook_flavour(cls: type, name: str) -> None:
    """Validate the sync/async flavour of *cls*'s implementation of *name*.

    Split out from :func:`~nexusdl.plugins.validators.validate_class_matches_manifest`
    so the same check can be reused from other call sites (e.g. plugin-author
    test helpers, ``nexusdl plugins check``).

    Args:
        cls: The plugin class to inspect.
        name: A known hook name that *cls* is expected to implement.

    Raises:
        PluginError: If *name* is unknown; if *cls* does not implement it; or
            if the implementation's flavour (``def`` vs ``async def``) does
            not match :data:`HOOK_SPECS_BY_NAME`.
    """
    spec = get_hook_spec(name)
    attr = getattr(cls, spec.name, None)
    qualname = f"{cls.__module__}.{cls.__qualname__}.{spec.name}"

    if not callable(attr):
        raise PluginError(
            f"{qualname} must be implemented (declared in manifest hooks)"
        )

    is_async = asyncio.iscoroutinefunction(attr)
    if spec.flavour == "sync" and is_async:
        raise PluginError(
            f"{qualname} must be a regular ``def`` (got ``async def``)"
        )
    if spec.flavour == "async" and not is_async:
        raise PluginError(
            f"{qualname} must be declared ``async def`` (got a regular function)"
        )


# --------------------------------------------------------------------------- #
# Drift detection — used by the test suite only                              #
# --------------------------------------------------------------------------- #


def verify_protocol_consistency() -> None:
    """Assert that :class:`~nexusdl.plugins.api.NexusPlugin` matches this module.

    Compares the protocol's public ``on_*`` members (methods and attributes)
    against :data:`ALL_HOOK_NAMES`. Intended to be called from a single test
    (``tests/unit/plugins/test_hooks.py::test_protocol_consistency``) so
    that adding a hook to one module without the other fails CI with a
    precise message rather than surfacing later as a mysterious dispatch bug.

    Not called from production code — kept side-effect-free so import order
    cannot accidentally trigger it.

    Raises:
        AssertionError: If the protocol and :data:`ALL_HOOK_NAMES` disagree.
            The message lists the symmetric difference.
    """
    # Imported lazily to keep ``hooks.py`` importable standalone (e.g. during
    # partial bootstrapping of ``nexusdl.plugins``).
    from nexusdl.plugins.api import NexusPlugin  # noqa: PLC0415

    protocol_hooks = frozenset(
        name
        for name in dir(NexusPlugin)
        if name.startswith("on_")
        and not name.startswith("_")
        and name in ALL_HOOK_NAMES | frozenset(dir(NexusPlugin))
    )
    # The dir() of a Protocol includes inherited ``object`` attributes, so
    # filter to names that *look* like hooks (``on_*`` and not dunder).
    protocol_hooks = frozenset(
        name for name in dir(NexusPlugin) if name.startswith("on_")
    )

    missing_in_hooks = protocol_hooks - ALL_HOOK_NAMES
    missing_in_protocol = ALL_HOOK_NAMES - protocol_hooks

    if missing_in_hooks or missing_in_protocol:
        raise AssertionError(
            "NexusPlugin protocol and plugins.hooks are out of sync. "
            f"Hooks in NexusPlugin but not in hooks.py: {sorted(missing_in_hooks)}. "
            f"Hooks in hooks.py but not in NexusPlugin: {sorted(missing_in_protocol)}."
        )
