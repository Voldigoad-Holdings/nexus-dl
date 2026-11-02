"""Validation utilities for NexusDL plugin manifests and plugin classes.

Three layers, all synchronous and side-effect-free (apart from the single
file read in :func:`load_manifest`):

1. **Structural** — :func:`load_manifest` parses ``nexus.plugin.yaml`` into a
   :class:`PluginManifest` (Pydantic v2, ``extra="forbid"``, ``frozen=True``,
   every field immutable). The Pydantic model *is* the schema:
   :func:`export_json_schema` lets CI and editors regenerate a JSON Schema
   from it on demand, so no hand-maintained schema file can ever drift.

2. **Compatibility** — :func:`validate_manifest_compatibility` checks the
   manifest against the running host version and interpreter, using
   ``packaging`` for PEP 440 / semver comparisons.

3. **Conformance** — :func:`validate_class_matches_manifest` checks that the
   plugin class's ``name`` / ``version`` attributes match the manifest and
   that every declared hook is actually implemented with the correct
   sync/async flavour.

:func:`validate_plugin` runs all three in order and returns the parsed
manifest for the loader to consume.

Hook names and their sync/async flavour come **exclusively** from
:mod:`nexusdl.plugins.hooks` — this module never hardcodes a hook string, and
never inspects :class:`~nexusdl.plugins.api.NexusPlugin` directly. Adding,
renaming, or changing the flavour of a hook requires editing only
:mod:`nexusdl.plugins.hooks` and :mod:`nexusdl.plugins.api`; every consumer
of this module adapts automatically.

Dependency graph (no cycles)::

    core.exceptions.PluginError
                  ▲
                  │
              hooks.py            ← canonical hook names + flavours
                  ▲
                  │
          validators.py  ─►  loader.py  ─►  interfaces/*

Only :mod:`nexusdl.plugins.loader` and interface modules are expected to
import the public API of this module. Every failure is raised as
:class:`~nexusdl.core.exceptions.PluginError` with a message that points at
the offending file, field, and plugin.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Final

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from nexusdl.core.exceptions import PluginError
from nexusdl.plugins.hooks import HookName, check_hook_flavour
from nexusdl.version import __version__ as _HOST_VERSION_STR


__all__ = [
    "HOST_VERSION",
    "MANIFEST_FILENAME",
    "PluginManifest",
    "export_json_schema",
    "load_manifest",
    "validate_class_matches_manifest",
    "validate_manifest_compatibility",
    "validate_plugin",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Manifest filename expected alongside ``plugin.py`` in a plugin directory.
MANIFEST_FILENAME: Final[str] = "nexus.plugin.yaml"

#: Host NexusDL version, parsed once at import for use as the default in
#: compatibility checks. Injected overridable in tests via the ``host_version``
#: argument of :func:`validate_manifest_compatibility`.
HOST_VERSION: Final[Version] = Version(_HOST_VERSION_STR)

#: Plugin identifier pattern — lowercase start, then lowercase/digit/`_`/`-`.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]*$")


# --------------------------------------------------------------------------- #
# Manifest model                                                              #
# --------------------------------------------------------------------------- #


class PluginManifest(BaseModel):
    """Validated representation of a ``nexus.plugin.yaml`` file.

    Pydantic is the schema: ``extra="forbid"`` rejects typos in field names,
    strings are stripped of surrounding whitespace, and the model is frozen so
    the loader can safely cache manifests across reloads. Collection fields
    are ``tuple`` (not ``list``) so ``frozen=True`` is genuinely airtight — a
    caller cannot mutate a manifest's hook list behind the loader's back.

    Attributes:
        name: Plugin identifier, matching ``^[a-z][a-z0-9_-]*$``.
        version: Plugin version (PEP 440 compatible string).
        author: Human author name.
        email: Optional contact address (free-form, not validated).
        homepage: Optional project URL (free-form, not validated).
        description: One-paragraph summary, rendered in CLI/UI listings.
        license: SPDX identifier (e.g. ``GPL-3.0-or-later``).
        min_nexusdl_version: **Inclusive** lower bound on the host version.
        max_nexusdl_version: **Exclusive** upper bound on the host version,
            or ``None`` for "no upper bound".
        python_requires: PEP 440 specifier against the interpreter, or
            ``None`` for "no constraint".
        hooks: Subset of :data:`nexusdl.plugins.hooks.ALL_HOOK_NAMES` that
            the plugin opts into. Validated against the :data:`HookName`
            ``Literal`` so a typo fails at load, not at dispatch time.
        dependencies: PEP 508 requirement strings. The loader verifies these
            are satisfied before activating the plugin; it never auto-installs.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    # --- Identity -------------------------------------------------------- #

    name: str
    version: str

    # --- Metadata (all free-form, none validated as a URL or email) ------ #

    author: str
    email: str | None = None
    homepage: str | None = None
    description: str
    license: str

    # --- Compatibility bounds ------------------------------------------- #

    min_nexusdl_version: str
    max_nexusdl_version: str | None = None
    python_requires: str | None = None

    # --- Declared hooks and dependencies -------------------------------- #

    hooks: tuple[HookName, ...] = ()
    dependencies: tuple[str, ...] = ()

    # --- Field validators ------------------------------------------------ #

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_PATTERN.match(value):
            raise ValueError(
                f"plugin name must match {_NAME_PATTERN.pattern!r}, "
                f"got {value!r}"
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

    @field_validator("hooks", "dependencies", mode="before")
    @classmethod
    def _none_to_empty_tuple(cls, value: Any) -> Any:
        # A YAML key with no value (e.g. ``hooks:``) parses to ``None``.
        # Treat that as an empty collection rather than a validation error.
        return () if value is None else value

    @field_validator("hooks")
    @classmethod
    def _validate_hooks_no_duplicates(
        cls, value: tuple[HookName, ...]
    ) -> tuple[HookName, ...]:
        if len(value) != len(set(value)):
            seen: set[str] = set()
            duplicates: list[str] = []
            for hook in value:
                if hook in seen and hook not in duplicates:
                    duplicates.append(hook)
                else:
                    seen.add(hook)
            raise ValueError(
                f"hooks contains duplicate entries: {sorted(duplicates)}"
            )
        return value

    @field_validator("dependencies")
    @classmethod
    def _validate_dependencies(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for dep in value:
            try:
                Requirement(dep)
            except InvalidRequirement as exc:
                raise ValueError(
                    f"invalid PEP 508 dependency {dep!r}: {exc}"
                ) from exc
        return value

    # --- Model-level validators ----------------------------------------- #

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
        path: Path to the manifest, or to the directory containing it (in
            which case :data:`MANIFEST_FILENAME` is appended).

    Returns:
        The validated, immutable manifest.

    Raises:
        PluginError: If the file is missing, cannot be read, is not valid
            YAML, does not contain a mapping, or fails schema validation.
            The message includes the offending field and value, so a CI
            failure is actionable without opening a debugger.
    """
    if path.is_dir():
        path = path / MANIFEST_FILENAME
    if not path.is_file():
        raise PluginError(f"plugin manifest not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginError(
            f"cannot read plugin manifest {path}: {exc}"
        ) from exc

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
    """Return the running interpreter version as a :class:`packaging.version.Version`.

    Returns:
        E.g. ``Version("3.12.5")``.
    """
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
    ``4.0.0``. This matches the semantics documented in the manifest template
    and in ``docs/source/development/plugins.md``.

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


def validate_class_matches_manifest(
    plugin_cls: type,
    manifest: PluginManifest,
) -> None:
    """Check that a plugin class matches its manifest.

    Three things are verified:

    1. The class declares ``name`` equal to ``manifest.name``.
    2. The class declares ``version`` equal to ``manifest.version``.
    3. Every hook listed in ``manifest.hooks`` is implemented on the class
       with the correct sync/async flavour. The flavour check is delegated to
       :func:`nexusdl.plugins.hooks.check_hook_flavour` so this module never
       duplicates the sync/async knowledge.

    Hooks present on the class but absent from the manifest are *not* an
    error: the loader will simply not dispatch them.

    Args:
        plugin_cls: The plugin class to validate.
        manifest: The manifest the class is expected to match.

    Raises:
        PluginError: On any mismatch, with a message identifying the
            offending attribute and both values.
    """
    if not isinstance(plugin_cls, type):
        raise PluginError(
            f"expected a class, got {type(plugin_cls).__name__}: "
            f"{plugin_cls!r}"
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
        check_hook_flavour(plugin_cls, hook)


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
    :func:`validate_class_matches_manifest`, in that order.

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
        PluginError: On any structural, compatibility, or conformance failure.
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
    helpers, and CI can consume the output to validate manifests without
    importing the full framework. The output is deterministic for a given
    Pydantic version, so it can be committed to ``data/plugin_schema.json``
    and regenerated by a ``make schema`` target without spurious diffs.

    Returns:
        A JSON-schema-compatible dictionary (draft 2020-12, as emitted by
        Pydantic v2).
    """
    return PluginManifest.model_json_schema()
