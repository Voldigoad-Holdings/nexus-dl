"""Canonical definitions and introspection helpers for NexusDL plugin hooks.

This module is the **single source of truth** for every hook that makes up
the :class:`~nexusdl.plugins.api.NexusPlugin` protocol. The name of every
hook, its sync/async flavour, and its one-line summary live here and here
only. Anywhere else that needs to reference a hook imports from this module
— never a string literal, never a duplicate constant.

Consumers
---------
* :mod:`nexusdl.plugins.validators` — uses :data:`HookName` to type
  ``manifest.hooks`` and :func:`check_hook_flavour` to validate each declared
  hook against the plugin class.
* :mod:`nexusdl.plugins.loader` — imports the six ``HOOK_ON_*`` constants and
  uses them everywhere it dispatches.
* CLI / docs (future) — use :data:`HOOK_SPECS` and its helpers to render
  ``nexusdl plugins list`` / ``nexusdl plugins info`` and the generated
  "Plugin hooks" section of ``docs/source/development/plugins.md``.
* Tests — call :func:`verify_protocol_consistency` to catch drift between
  this module and :mod:`nexusdl.plugins.api`.

Contract
--------
Two hooks are **mandatory** for every plugin: :data:`HOOK_ON_LOAD` and
:data:`HOOK_ON_UNLOAD`. Both are synchronous. They are the only hooks
declared on :class:`~nexusdl.plugins.api.NexusPlugin` itself, because a
``Protocol`` cannot express "optional method".

The remaining four hooks are **optional** and asynchronous. A plugin opts
into them by listing them in the ``hooks:`` field of its
``nexus.plugin.yaml``; the loader dispatches only to plugins that opted in.

Adding, renaming, or changing the flavour of a hook requires editing exactly
two files: this one and :mod:`nexusdl.plugins.api`. Every other module in
the package adapts automatically, and :func:`verify_protocol_consistency`
guarantees the two stay in sync.

Single source of truth
----------------------
Two representations of the hook set coexist in this module:

* the six ``HOOK_ON_*`` **string constants**, each annotated
  ``Final[HookName]`` so that renaming a constant without updating
  :data:`HookName` fails under ``mypy --strict``; and
* :data:`HookName`, a ``Literal`` union that is the compiler-visible
  contract for anything that takes "a hook" as a parameter.

These two cannot be mechanically derived from each other — Python's
``Literal`` is a static construct, while the constants are runtime values
— so :func:`verify_protocol_consistency` also asserts
``set(get_args(HookName)) == ALL_HOOK_NAMES``. That check is what turns a
silent drift between the two representations into a CI failure.

Dependency graph (no cycles)::

    core.exceptions.PluginError
                  ▲
                  │
              hooks.py              ← you are here
                  ▲
                  │
          validators.py  ─►  loader.py  ─►  interfaces/*
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    Final,
    Literal,
    TypeAlias,
    TypeGuard,
    get_args,
)

from nexusdl.core.exceptions import PluginError


__all__ = [
    # Canonical hook names
    "HOOK_ON_LOAD",
    "HOOK_ON_UNLOAD",
    "HOOK_ON_BEFORE_DOWNLOAD",
    "HOOK_ON_AFTER_DOWNLOAD",
    "HOOK_ON_PAGE_DOWNLOADED",
    "HOOK_ON_ERROR",
    # Types
    "HookFlavour",
    "HookName",
    "HookSpec",
    # Aggregated views
    "ALL_HOOK_NAMES",
    "ASYNC_HOOK_NAMES",
    "HOOK_SPECS",
    "HOOK_SPECS_BY_NAME",
    "SYNC_HOOK_NAMES",
    # Introspection
    "check_hook_flavour",
    "get_hook_flavour",
    "get_hook_spec",
    "is_async_hook",
    "is_known_hook",
    "is_sync_hook",
    # Test helper
    "verify_protocol_consistency",
]


# --------------------------------------------------------------------------- #
# Canonical hook names                                                        #
# --------------------------------------------------------------------------- #
# These literals are the source of truth. Never write "on_load" (or any other
# hook name) as a string literal elsewhere in the codebase — import the
# constant instead.
#
# Each constant is annotated ``Final[HookName]`` so that renaming it to a
# value not present in ``HookName`` below fails under ``mypy --strict``. The
# reverse direction (adding a name to ``HookName`` without a constant) is
# caught by :func:`verify_protocol_consistency` at test time.
# --------------------------------------------------------------------------- #

#: Closed union of every hook name. Used wherever a function takes or returns
#: "a hook" so ``mypy --strict`` catches typos at compile time.
#:
#: Kept in **alphabetical** order (``on_after_download`` first) so that
#: additions are always inserted at the same position. The lifecycle order
#: used by the CLI listing is the order of :data:`HOOK_SPECS`, not this one.
HookName: TypeAlias = Literal[
    "on_after_download",
    "on_before_download",
    "on_error",
    "on_load",
    "on_page_downloaded",
    "on_unload",
]

#: Whether a hook must be declared as a regular ``def`` or an ``async def``.
HookFlavour: TypeAlias = Literal["sync", "async"]


HOOK_ON_LOAD: Final[HookName] = "on_load"
HOOK_ON_UNLOAD: Final[HookName] = "on_unload"
HOOK_ON_BEFORE_DOWNLOAD: Final[HookName] = "on_before_download"
HOOK_ON_AFTER_DOWNLOAD: Final[HookName] = "on_after_download"
HOOK_ON_PAGE_DOWNLOADED: Final[HookName] = "on_page_downloaded"
HOOK_ON_ERROR: Final[HookName] = "on_error"


# --------------------------------------------------------------------------- #
# Hook specifications                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HookSpec:
    """Static description of a single plugin hook.

    ``summary`` is user-facing: it is rendered verbatim by the CLI
    (``nexusdl plugins info``) and by the generated "Plugin hooks" section of
    the developer documentation. Keep it one sentence, imperative, no
    trailing period, no code formatting.

    Attributes:
        name: The hook's method name on the plugin class.
        flavour: ``"sync"`` for :data:`HOOK_ON_LOAD` / :data:`HOOK_ON_UNLOAD`,
            ``"async"`` for the four optional pipeline hooks.
        summary: One-line description shown in CLI output and docs.
        mandatory: ``True`` for hooks every plugin must implement. Mandatory
            hooks are also declared on
            :class:`~nexusdl.plugins.api.NexusPlugin`; optional hooks are not.
    """

    name: HookName
    flavour: HookFlavour
    summary: str
    mandatory: bool


#: Every hook, in lifecycle order: load → pipeline → unload. Order matters for
#: the CLI listing but is otherwise not significant.
HOOK_SPECS: Final[tuple[HookSpec, ...]] = (
    HookSpec(
        name=HOOK_ON_LOAD,
        flavour="sync",
        summary="Receive the plugin context and initialize state",
        mandatory=True,
    ),
    HookSpec(
        name=HOOK_ON_BEFORE_DOWNLOAD,
        flavour="async",
        summary="Inspect or transform a task before it starts downloading",
        mandatory=False,
    ),
    HookSpec(
        name=HOOK_ON_AFTER_DOWNLOAD,
        flavour="async",
        summary="Observe or transform a completed download result",
        mandatory=False,
    ),
    HookSpec(
        name=HOOK_ON_PAGE_DOWNLOADED,
        flavour="async",
        summary="Be notified of each page written to disk",
        mandatory=False,
    ),
    HookSpec(
        name=HOOK_ON_ERROR,
        flavour="async",
        summary="Be notified when a download task fails",
        mandatory=False,
    ),
    HookSpec(
        name=HOOK_ON_UNLOAD,
        flavour="sync",
        summary="Persist state and release resources before shutdown",
        mandatory=True,
    ),
)

#: Immutable name→spec lookup. Typed as ``Mapping[str, HookSpec]`` (rather
#: than ``Mapping[HookName, HookSpec]``) so callers can look up an arbitrary
#: string and get ``None`` on miss; callers who *know* the value is a
#: ``HookName`` lose nothing. The keys are guaranteed to be ``HookName``
#: because ``HOOK_SPECS`` types its ``name`` field as ``HookName``.
HOOK_SPECS_BY_NAME: Final[Mapping[str, HookSpec]] = MappingProxyType(
    {spec.name: spec for spec in HOOK_SPECS}
)

#: Every hook name, as a frozen set of ``str``. Used by callers that need to
#: validate arbitrary strings (e.g. ``manifest.hooks`` cross-checks).
ALL_HOOK_NAMES: Final[frozenset[str]] = frozenset(HOOK_SPECS_BY_NAME)

#: Hooks that must be declared as a regular ``def``.
SYNC_HOOK_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in HOOK_SPECS if spec.flavour == "sync"
)

#: Hooks that must be declared as an ``async def``.
ASYNC_HOOK_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in HOOK_SPECS if spec.flavour == "async"
)


# --------------------------------------------------------------------------- #
# Static introspection (name-based)                                           #
# --------------------------------------------------------------------------- #


def is_known_hook(name: str) -> TypeGuard[HookName]:
    """Return ``True`` iff *name* is a hook declared in :data:`HOOK_SPECS`.

    The return type is :class:`typing.TypeGuard` so that a caller doing
    ``if is_known_hook(x): ...`` narrows ``x`` to :data:`HookName` in the
    true branch — no cast needed, no risk of passing an arbitrary string to
    a function that requires ``HookName``.

    Args:
        name: Candidate hook name (arbitrary string).

    Returns:
        ``True`` for the six canonical hooks, ``False`` for anything else
        (including names that *look* like hooks, e.g. ``"on_startup"``).
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
            valid name so callers can fix the typo without opening another
            file.
    """
    spec = HOOK_SPECS_BY_NAME.get(name)
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
    spec = HOOK_SPECS_BY_NAME.get(name)
    return spec is not None and spec.flavour == "sync"


def is_async_hook(name: str) -> bool:
    """Return ``True`` iff *name* is a known hook with ``flavour == "async"``.

    Args:
        name: Candidate hook name. Unknown names return ``False``.
    """
    spec = HOOK_SPECS_BY_NAME.get(name)
    return spec is not None and spec.flavour == "async"


# --------------------------------------------------------------------------- #
# Dynamic introspection (class-based)                                         #
# --------------------------------------------------------------------------- #


def check_hook_flavour(cls: type, name: HookName) -> None:
    """Validate the sync/async flavour of *cls*'s implementation of *name*.

    Called by :func:`nexusdl.plugins.validators.validate_class_matches_manifest`
    for every hook declared in the plugin's manifest, and reusable from any
    other site that wants the same guarantee (e.g. a plugin-author test
    helper).

    Args:
        cls: The plugin class to inspect.
        name: A known hook name (typed ``HookName`` so a caller passing an
            unknown literal fails under ``mypy --strict``; a caller passing a
            dynamic string should call :func:`is_known_hook` first to narrow
            it — the :class:`TypeGuard` return type makes that narrowing
            automatic).

    Raises:
        PluginError: If *cls* does not implement *name*, or if the
            implementation's flavour (``def`` vs ``async def``) does not
            match :data:`HOOK_SPECS_BY_NAME`.
    """
    spec = get_hook_spec(name)
    attr = getattr(cls, spec.name, None)
    qualname = f"{cls.__module__}.{cls.__qualname__}.{spec.name}"

    if not callable(attr):
        raise PluginError(
            f"{qualname} must be implemented (declared in manifest hooks)"
        )

    is_async = inspect.iscoroutinefunction(attr)
    if spec.flavour == "sync" and is_async:
        raise PluginError(
            f"{qualname} must be a regular ``def`` (got ``async def``)"
        )
    if spec.flavour == "async" and not is_async:
        raise PluginError(
            f"{qualname} must be declared ``async def`` "
            f"(got a regular function)"
        )


# --------------------------------------------------------------------------- #
# Drift detection — used by the test suite only                               #
# --------------------------------------------------------------------------- #


def verify_protocol_consistency() -> None:
    """Assert that :class:`~nexusdl.plugins.api.NexusPlugin` matches this module.

    Five things are verified:

    0. **Self-consistency** — the two representations of the hook set in
       this module agree: ``set(get_args(HookName)) == ALL_HOOK_NAMES``.
       This catches a developer who adds a hook constant without adding it
       to the ``Literal`` (or vice-versa) — a drift that ``mypy`` cannot
       see because the ``Literal`` is static and the constants are runtime
       values.
    1. Every hook declared on ``NexusPlugin`` is a known hook in
       :data:`ALL_HOOK_NAMES` (no typo or stale name in the protocol).
    2. Every **mandatory** sync hook (:data:`SYNC_HOOK_NAMES`) is declared on
       ``NexusPlugin``. Optional async hooks are allowed to be absent.
    3. Every hook declared on ``NexusPlugin`` has the same sync/async flavour
       as its :data:`HookSpec`.

    Intended to be called from a single test
    (``tests/unit/plugins/test_hooks.py::test_protocol_consistency``) so that
    editing ``api.py`` without mirroring the change here — or vice versa —
    fails CI with a precise message instead of surfacing later as a
    mysterious dispatch bug.

    Import of :mod:`nexusdl.plugins.api` is deferred so this module can be
    imported during partial bootstrapping.

    Raises:
        AssertionError: On any drift. The message names the offending hook
            and, where relevant, its two conflicting flavours.
    """
    # 0. Self-consistency: the Literal and the runtime constants agree.
    literal_names = frozenset(get_args(HookName))
    if literal_names != ALL_HOOK_NAMES:
        missing_from_literal = ALL_HOOK_NAMES - literal_names
        missing_from_specs = literal_names - ALL_HOOK_NAMES
        raise AssertionError(
            f"HookName and HOOK_SPECS disagree. "
            f"In HOOK_SPECS but not in HookName: {sorted(missing_from_literal)}. "
            f"In HookName but not in HOOK_SPECS: {sorted(missing_from_specs)}."
        )

    from nexusdl.plugins.api import NexusPlugin  # noqa: PLC0415

    protocol_hooks: frozenset[str] = frozenset(
        name
        for name in dir(NexusPlugin)
        # Filter out Python's own dunders (``__init__``, ``__subclasshook__``,
        # ``__class_getitem__``, …) — none of them is a hook.
        if name.startswith("on_") and not (name.startswith("__") and name.endswith("__"))
    )

    # 1. No unknown hook in the protocol.
    unknown_in_protocol = protocol_hooks - ALL_HOOK_NAMES
    if unknown_in_protocol:
        raise AssertionError(
            f"NexusPlugin declares hook(s) not present in hooks.py: "
            f"{sorted(unknown_in_protocol)}. "
            f"Known hooks: {sorted(ALL_HOOK_NAMES)}."
        )

    # 2. Every mandatory hook is declared on the protocol.
    missing_mandatory = SYNC_HOOK_NAMES - protocol_hooks
    if missing_mandatory:
        raise AssertionError(
            f"Mandatory hook(s) declared in hooks.py but missing from "
            f"NexusPlugin: {sorted(missing_mandatory)}."
        )

    # 3. Flavour matches between api.py and hooks.py.
    for hook_name in protocol_hooks:
        attr = getattr(NexusPlugin, hook_name)
        proto_is_async = inspect.iscoroutinefunction(attr)
        spec_is_async = is_async_hook(hook_name)
        if proto_is_async != spec_is_async:
            raise AssertionError(
                f"Flavour mismatch for hook {hook_name!r}: "
                f"NexusPlugin declares "
                f"{'async' if proto_is_async else 'sync'}, "
                f"hooks.py declares "
                f"{'async' if spec_is_async else 'sync'}."
            )
