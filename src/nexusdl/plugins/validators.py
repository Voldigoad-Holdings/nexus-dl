"""Validation utilities for NexusDL plugin manifests and plugin classes.

Three layers, all synchronous and side-effect-free:

1. **Structural** — :func:`load_manifest` parses ``nexus.plugin.yaml`` into a
   :class:`PluginManifest` (Pydantic v2, ``extra="forbid"``). The Pydantic
   model *is* the source of truth: it supersedes the ``plugin_schema.json``
   referenced by the manifest's header comment (which may be regenerated at
   any time via :func:`export_json_schema`).
2. **Compatibility** — :func:`validate_manifest_compatibility` checks the
   manifest against the running host version and interpreter, using
   ``packaging`` for PEP 440 / semver comparisons.
3. **Conformance** — :func:`validate_class_matches_manifest` checks that the
   plugin class's ``name`` / ``version`` attributes match the manifest and
   that every declared hook is actually implemented, with the correct
   sync/async flavour.

:func:`validate_plugin` runs all three in order and returns the parsed
manifest for the loader to consume.

Only :mod:`nexusdl.plugins.loader` is expected to import these functions.
Every failure is raised as :class:`~nexusdl.core.exceptions.PluginError` with
a message that points at the offending file, field, and plugin.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from nexusdl.core.exceptions import PluginError
from nexusdl.version import __version__ as _HOST_VERSION_STR

if TYPE_CHECKING:
    from collections.abc import Iterable


__all__ = [
    "ASYNC_HOOKS",
    "HOOK_NAMES",
    "HOST_VERSION",
    "MANIFEST_FILENAME",
    "PluginManifest",
    "SYNC_HOOKS",
    "export_json_schema",
    "get_declared_hooks",
    "get_implemented_hooks",
    "load_manifest",
    "validate_class_matches_manifest",
    "validate_manifest_compatibility",
    "validate_plugin",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Manifest filename expected alongside ``plugin.py``.
MANIFEST_FILENAME: Final[str] = "nexus.plugin.yaml"

#: Plugin identifier pattern — lowercase, kebab/snake, must start with a letter.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Hook names that must be *synchronous* per the :class:`NexusPlugin` protocol.
SYNC_HOOKS: Final[frozenset[str]] = frozenset({"on_load", "on_unload"})

#: Host version parsed once, used as the default in compatibility checks.
HOST_VERSION: Final[Version] = Version(_HOST_VERSION_STR)


def _discover_hook_names() -> frozenset[str]:
    """Return the set of hook names declared by :class:`NexusPlugin`.

    Derived from the protocol itself so this module never drifts if the
    protocol gains a hook. Falls back to a static list when
    :mod:`nexusdl.plugins.api` cannot be imported (e.g. during a partial
    bootstrap of the package).
    """
    try:
        from nexusdl.plugins.api import NexusPlugin  # noqa: PLC0415
    except ImportError:
        return frozenset(
            {
                "on_load",
                "on_unload",
                "on_before_download",
                "on_after_download",
                "on_page_downloaded",
                "on_error",
            }
        )
    return frozenset(
        name
        for name in dir(NexusPlugin)
        if name.startswith("on_")
        and not name.startswith("_")
        and callable(getattr(NexusPlugin, name, None))
    )


#: All hook names a plugin manifest may declare.
HOOK_NAMES: Final[frozenset[str]] = _discover_hook_names()

#: Hooks that must be declared ``async def`` on the plugin class.
ASYNC_HOOKS: Final[frozenset[str]] = HOOK_NAMES - SYNC_HOOKS


# --------------------------------------------------------------------------- #
# Manifest model                                                              #
# --------------------------------------------------------------------------- #


class PluginManifest(BaseModel):
    """Validated representation of a ``nexus.plugin.yaml`` file.

    Pydantic is the schema: ``extra="forbid"`` rejects typos in field names,
    every string is stripped of surrounding whitespace, and the model is
    frozen so the loader can safely cache manifests across reloads.

    Attributes:
        name: Plugin identifier, matching ``^[a-z][a-z0-9_-]*$``.
        version: Semver-like version string (PEP 440 compatible).
        author: Human author name.
        email: Optional contact address (free-form, not validated).
        homepage: Optional project URL (free-form, not validated).
        description: One-paragraph summary, rendered in CLI/UI listings.
        license: SPDX identifier.
        min_nexusdl_version: Inclusive lower bound on the host version.
        max_nexusdl_version: Exclusive upper bound on the host version,
            or ``None`` for "no upper bound".
        python_requires: PEP 440 specifier against the interpreter, or
            ``None`` for "no constraint".
        hooks: Subset of :data:`HOOK_NAMES` that the plugin opts into.
        dependencies: PEP 508 requirement strings the loader must find
            satisfied before activating the plugin.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    name: str
    version: str
    author: str
    email: str | None = None
    homepage: str | None = None
    description: str
    license: str
    min_nexusdl_version: str
    max_nexusdl_version: str | None = None
    python_requires: str | None = None
    hooks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)

    # --- Field validators ------------------------------------------------- #

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_PATTERN.match(value):
            raise ValueError(
                f"plugin name must match {_NAME_PATTERN.pattern!r}, got {value!r}"
            )
        return value

    @field_validator("version", "min_nexusdl_version")
    @classmethod
    def _validate_required_version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"invalid version {value!r}: {exc}") from exc
        return value

    @field_validator("max_nexusdl_version")
    @classmethod
    def _validate_optional_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"invalid version {value!r}: {exc}") from exc
        return value

    @field_validator("python_requires")
    @classmethod
    def _validate_python_requires(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(
                f"invalid python_requires specifier {value!r}: {exc}"
            ) from exc
        return value

    @field_validator("hooks")
    @classmethod
    def _validate_hooks(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            duplicates = sorted({h for h in value if value.count(h) > 1})
            raise ValueError(f"hooks list contains duplicates: {duplicates}")
        unknown = set(value) - HOOK_NAMES
        if unknown:
            raise ValueError(
                f"unknown hook(s) {sorted(unknown)}; valid hooks are "
                f"{sorted(HOOK_NAMES)}"
            )
        return value

    @field_validator("dependencies")
    @classmethod
    def _validate_dependencies(cls, value: list[str]) -> list[str]:
        for dep in value:
            try:
                Requirement(dep)
            except InvalidRequirement as exc:
                raise ValueError(
                    f"invalid PEP 508 dependency {dep!r}: {exc}"
                ) from exc
        return value

    # --- Model validator -------------------------------------------------- #

    @model_validator(mode="after")
    def _validate_version_range(self) -> PluginManifest:
        if self.max_nexusdl_version is not None:
            min_v = Version(self.min_nexusdl_version)
            max_v = Version(self.max_nexusdl_version)
            if max_v <= min_v:
                raise ValueError(
                    f"max_nexusdl_version ({max_v}) must be strictly greater "
                    f"than min_nexusdl_version ({min_v})"
                )
        return self


# --------------------------------------------------------------------------- #
# Structural validation                                                       #
# --------------------------------------------------------------------------- #


def load_manifest(path: Path) -> PluginManifest:
    """Parse and validate a ``nexus.plugin.yaml`` file.

    Args:
        path: Path to the manifest. May be either the manifest itself or the
            directory containing it (in which case ``nexus.plugin.yaml`` is
            appended).

    Returns:
        The validated manifest.

    Raises:
        PluginError: If the file is missing, cannot be parsed as YAML, does
            not contain a mapping, or fails schema validation. The error
            message includes the offending field and value.
    """
    if path.is_dir():
        path = path / MANIFEST_FILENAME
    if not path.is_file():
        raise PluginError(f"plugin manifest not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginError(f"cannot read plugin manifest {path}: {exc}") from exc

    try:
        parsed: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PluginError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise PluginError(
            f"{path} must contain a YAML mapping at the top level, "
            f"got {type(parsed).__name__}"
        )

    try:
        return PluginManifest.model_validate(parsed)
    except ValidationError as exc:
        raise PluginError(
            f"invalid plugin manifest {path}:\n{exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Compatibility validation                                                    #
# --------------------------------------------------------------------------- #


def _running_python_version() -> Version:
    """Return the running interpreter version as a ``packaging.Version``."""
    info = sys.version_info
    return Version(f"{info.major}.{info.minor}.{info.micro}")


def validate_manifest_compatibility(
    manifest: PluginManifest,
    *,
    host_version: Version | None = None,
    python_version: Version | None = None,
) -> None:
    """Check a manifest against the host NexusDL and interpreter versions.

    The upper bound is **exclusive**: a manifest declaring
    ``max_nexusdl_version: "4.0.0"`` accepts hosts up to (but not including)
    ``4.0.0``.

    Args:
        manifest: Manifest returned by :func:`load_manifest`.
        host_version: Host NexusDL version to check against. Defaults to
            :data:`HOST_VERSION`. Injectable for tests.
        python_version: Interpreter version to check against. Defaults to
            the running interpreter. Injectable for tests.

    Raises:
        PluginError: If any bound is violated. The message names the plugin,
            the constraint, and the offending value.
    """
    if host_version is None:
        host_version = HOST_VERSION
    if python_version is None:
        python_version = _running_python_version()

    min_v = Version(manifest.min_nexusdl_version)
    if host_version < min_v:
        raise PluginError(
            f"plugin {manifest.name!r} requires nexusdl >= {min_v}, "
            f"host is {host_version}"
        )

    if manifest.max_nexusdl_version is not None:
        max_v = Version(manifest.max_nexusdl_version)
        if host_version >= max_v:
            raise PluginError(
                f"plugin {manifest.name!r} requires nexusdl < {max_v}, "
                f"host is {host_version}"
            )

    if manifest.python_requires is not None:
        spec = SpecifierSet(manifest.python_requires)
        if not spec.contains(python_version):
            raise PluginError(
                f"plugin {manifest.name!r} requires Python "
                f"{manifest.python_requires}, running {python_version}"
            )


# --------------------------------------------------------------------------- #
# Conformance validation                                                      #
# --------------------------------------------------------------------------- #


def get_declared_hooks(manifest: PluginManifest) -> frozenset[str]:
    """Return the set of hooks the manifest opts into.

    Args:
        manifest: A validated manifest.

    Returns:
        A frozen set of hook names (possibly empty).
    """
    return frozenset(manifest.hooks)


def get_implemented_hooks(plugin_cls: type) -> frozenset[str]:
    """Return the set of hooks the plugin class actually implements.

    A hook is considered *implemented* when the class exposes a callable
    attribute with that exact name — whether the method is declared
    explicitly or inherited from a mixin.

    Args:
        plugin_cls: The plugin class to inspect.

    Returns:
        A frozen set of hook names, subset of :data:`HOOK_NAMES`.
    """
    return frozenset(
        hook
        for hook in HOOK_NAMES
        if callable(getattr(plugin_cls, hook, None))
    )


def validate_class_matches_manifest(
    plugin_cls: type,
    manifest: PluginManifest,
) -> None:
    """Check that a plugin class matches its manifest.

    Three things are verified:

    1. The class declares ``name`` equal to ``manifest.name``.
    2. The class declares ``version`` equal to ``manifest.version``.
    3. Every hook listed in ``manifest.hooks`` exists on the class, and its
       sync/async flavour matches :data:`SYNC_HOOKS` / :data:`ASYNC_HOOKS`.

    Hooks present on the class but absent from the manifest are *not* an
    error — the loader simply will not dispatch them.

    Args:
        plugin_cls: The plugin class to validate.
        manifest: The manifest the class is expected to match.

    Raises:
        PluginError: On any mismatch, with a message identifying the
            offending attribute and both values.
    """
    if not isinstance(plugin_cls, type):
        raise PluginError(
            f"expected a class, got {type(plugin_cls).__name__}: {plugin_cls!r}"
        )

    qualname = f"{plugin_cls.__module__}.{plugin_cls.__qualname__}"

    cls_name = getattr(plugin_cls, "name", None)
    if cls_name != manifest.name:
        raise PluginError(
            f"{qualname}.name is {cls_name!r}, "
            f"manifest declares name={manifest.name!r}"
        )

    cls_version = getattr(plugin_cls, "version", None)
    if cls_version != manifest.version:
        raise PluginError(
            f"{qualname}.version is {cls_version!r}, "
            f"manifest declares version={manifest.version!r}"
        )

    for hook in manifest.hooks:
        attr = getattr(plugin_cls, hook, None)
        if not callable(attr):
            raise PluginError(
                f"manifest declares hook {hook!r} but {qualname} "
                f"does not implement it"
            )
        is_async = asyncio.iscoroutinefunction(attr)
        if hook in SYNC_HOOKS and is_async:
            raise PluginError(
                f"{qualname}.{hook} must be a regular ``def`` "
                f"(got ``async def``)"
            )
        if hook in ASYNC_HOOKS and not is_async:
            raise PluginError(
                f"{qualname}.{hook} must be declared ``async def`` "
                f"(got a regular function)"
            )


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #


def validate_plugin(
    manifest_path: Path,
    plugin_cls: type,
    *,
    host_version: Version | None = None,
    python_version: Version | None = None,
) -> PluginManifest:
    """Run the full validation pipeline for a plugin.

    Convenience wrapper combining :func:`load_manifest`,
    :func:`validate_manifest_compatibility`, and
    :func:`validate_class_matches_manifest`.

    Args:
        manifest_path: Path to ``nexus.plugin.yaml`` (or the directory
            containing it).
        plugin_cls: The already-imported plugin class to validate.
        host_version: Override for :data:`HOST_VERSION` (for tests).
        python_version: Override for the running interpreter version
            (for tests).

    Returns:
        The validated manifest. The loader uses it as the source of truth
        for ``name``, ``version``, and the set of hooks to dispatch.

    Raises:
        PluginError: On any structural, compatibility, or conformance
            failure.
    """
    manifest = load_manifest(manifest_path)
    validate_manifest_compatibility(
        manifest,
        host_version=host_version,
        python_version=python_version,
    )
    validate_class_matches_manifest(plugin_cls, manifest)
    return manifest


# --------------------------------------------------------------------------- #
# Tooling helpers                                                             #
# --------------------------------------------------------------------------- #


def export_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`PluginManifest`.

    Intended for tooling: editors, ``scripts/validate_sites.py``-style
    helpers, and CI can consume it to validate manifests without importing
    the full framework. The output is deterministic for a given Pydantic
    version.

    Returns:
        A JSON-schema-compatible dictionary.
    """
    return PluginManifest.model_json_schema()


# --------------------------------------------------------------------------- #
# Internal helpers — re-exported only for tests                               #
# --------------------------------------------------------------------------- #


def _iter_manifest_fields() -> Iterable[str]:
    """Yield the name of every declared manifest field.

    Kept private; used by tests to assert the manifest field set matches
    the documentation. Exposed via ``__all__`` only for the constant
    ``MANIFEST_FILENAME``, not this helper.
    """
    yield from PluginManifest.model_fields
