"""Localisation (i18n) for every user-facing string in NexusDL.

The pipeline ships four translation catalogs — English, French, Spanish,
German — under ``nexusdl.data/translations/``. Every one of them has the
same nested JSON structure (defined by ``en.json``, the source of truth).
This module loads them lazily, resolves a translation key against a
locale with a documented fallback chain, and interpolates ``{var}``
placeholders safely.

The API is small by design
--------------------------
The pipeline's UI surfaces (CLI, TUI, GUI, Web) need exactly three
operations:

* **Translate a key** — :meth:`Translator.translate`. Walks a
  dot-separated path through the locale's JSON, interpolates named
  parameters, falls back through the candidate chain when the key is
  missing, and returns the key itself as a last resort so a missing
  translation is visible rather than silent.
* **Translate with a plural form** — :meth:`Translator.plural`. Looks up
  ``<key>_one`` or ``<key>_other`` depending on the count, then falls
  back to the base ``<key>`` (a translator that provides only the base
  key still works, just without pluralisation).
* **Check availability** — :meth:`Translator.has` and
  :meth:`Translator.available_locales`. Used by a UI that wants to
  enable a locale picker only for locales that have a shipped catalog.

Every other operation a caller might want (list all keys, dump raw JSON,
iterate the catalog) is provided by the underlying
:class:`TranslationCatalog` — but the pipeline does not need it, and the
module deliberately does not add convenience helpers for it.

Fallback chain
--------------
A caller asks for a specific locale (or accepts the translator's
default). The resolution order is:

1. The exact locale (``en-GB``).
2. The language-only prefix of that locale (``en``).
3. The translator's configured fallback locale (typically ``en``).

Duplicates are dropped (a caller passing ``"en"`` does not trigger the
language-prefix step twice). The first catalog that contains the key
wins. If none does, the key itself is returned — a missing translation
is visible in the UI (``errors.parser.failed`` appears verbatim) and
greppable in a translation file, rather than silently replaced with an
empty string.

The fallback chain means a French user sees French when a key is
translated, and English when it is not. The English catalog is
therefore a **contract**: every key the pipeline references must exist
in ``en.json``. A missing key in ``en.json`` is a bug — the fallback
chain has run out and the raw key will appear in the UI.

Plural forms — simplified CLDR
------------------------------
:meth:`Translator.plural` selects ``_one`` when the count is exactly
``1`` and ``_other`` for every other value (including ``0`` and
negatives). This is the CLDR rule for English, German, and Spanish.
French strictly also treats ``0`` as ``_one``; the module does **not**
special-case French, because the difference matters only for
zero-item messages ("0 pages" vs "0 page") and every French catalog the
pipeline ships is authored with the ``count == 1`` rule in mind. A
future improvement that adds proper CLDR plural categories per locale
would change :func:`_plural_suffix`, not this docstring's callers.

This simplification is documented here so a translator who spots the
"0 pages" case in French knows that the module is behaving as designed,
not broken.

Interpolation
-------------
Placeholders use the :meth:`str.format` mini-language: ``{name}``,
``{count}``, ``{site}``, plus the two extensions that mini-language
offers — an optional conversion (``{name!r}``, ``{name!s}``,
``{name!a}``) and an optional format spec (``{count:>3}``,
``{price:.2f}``).

The interpolation contract for **missing** parameters is deliberately
conservative:

* The **whole placeholder** is left unchanged — ``"Hello {name}"``
  with no ``name`` stays ``"Hello {name}"``, and so does
  ``"Hello {name!r}"`` with no ``name`` (the conversion is dropped,
  the placeholder survives verbatim). This is true **regardless of
  whether other parameters are present**: a template with a mix of
  present and absent placeholders interpolates the present ones and
  leaves the absent ones untouched.
* A placeholder whose **nested** access fails — ``{user.name}`` with
  no ``user``, ``{items[0]}`` with no ``items``, ``{user.name}`` with
  a ``user`` that has no ``name`` attribute — is also left unchanged,
  **including the whole field name**: ``{user.name}`` and
  ``{items[0]}`` survive verbatim, not ``{user}`` and ``{items}``.
  The other placeholders in the same template still interpolate.
* Malformed templates (unclosed braces, invalid format specs) are
  returned verbatim. The caller sees their own bug rather than
  crashing the UI.

The ``count`` parameter is injected automatically by
:meth:`Translator.plural`, so a plural template can reference
``{count}`` without the caller having to pass it explicitly::

    # downloads/page_count_one.json: "one page"
    # downloads/page_count_other.json: "{count} pages"
    t.plural("downloads.page_count", 5)  # → "5 pages"
    t.plural("downloads.page_count", 1)  # → "one page"

Threading and lifecycle
-----------------------
A :class:`Translator` is a **service**: constructed once by the
composition root with the settings-driven default locale, then passed to
every consumer that needs to localise strings. It is thread-safe — the
catalog cache is guarded by a :class:`threading.Lock`, and the loaded
:class:`TranslationCatalog` instances are immutable after construction.

Loading is **lazy**: constructing a :class:`Translator` performs no I/O.
The first call to :meth:`translate` (or :meth:`plural`, or
:meth:`has`) for a locale loads that locale's catalog. A caller that
never touches a locale never loads its file.

:func:`get_default_translator` provides a process-wide fallback for
code that cannot receive a translator via dependency injection (CLI
subcommands launched before the composition root has run, error
handlers in exception paths). The composition root overrides it with
:func:`configure_default_translator` once :class:`~nexusdl.core.config.Settings`
has been loaded. Prefer injection when possible — the default
translator exists as an escape hatch, not as the intended pattern.

What this module deliberately does **not** do
---------------------------------------------
* **No translation file validation.** A catalog missing keys that
  ``en.json`` has is a translator's oversight, not this module's
  problem — :meth:`Translator.has` lets a CI check verify consistency
  across locales, but the loader itself does not enforce it.
* **No automatic fallback to ``en.json`` for missing keys in
  ``en.json``.** If ``en.json`` itself is missing a key, that is a bug
  in the shipped catalog; the raw key surfaces in the UI. The module
  never synthesises a placeholder like ``"???"`` — a caller who wants
  a visible fallback uses :meth:`Translator.has` to branch explicitly.
* **No message catalogue versioning.** ``en.json`` has a ``_meta``
  section with a ``language`` and ``name``; the pipeline reads neither.
  A future migration that changes the JSON structure would be gated by
  a version field added to ``_meta``, not by this module.
* **No ICU MessageFormat.** Plural and interpolation rules are simple
  enough that :meth:`str.format` covers every case the pipeline
  needs. ICU adds a parser, a formatter, and a compatibility surface
  that is orders of magnitude larger than this module.
* **No locale negotiation from an HTTP request.** A Web deployment that
  wants to select a locale from ``Accept-Language`` parses the header
  at its own boundary and passes the chosen locale to
  :meth:`Translator.translate` explicitly. The ``locale`` keyword is
  the whole interface.
* **No global mutation from within this module.** The default
  translator changes only through the explicit
  :func:`configure_default_translator` call, which the composition root
  invokes. No import-time side effects, no environment variable
  reading.

Dependency graph (no cycles)::

    importlib.resources, json, string, threading (stdlib)

This module is a **pure leaf** on the project side — it imports nothing
from ``nexusdl`` and nothing from third parties. It could be lifted into
a standalone utility library together with the
``nexusdl/data/translations/`` files without modification.
"""

from __future__ import annotations

import json
import string
import threading
from collections.abc import Mapping
from importlib.resources import files as _resource_files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final


__all__ = [
    "DEFAULT_LOCALE",
    "FALLBACK_LOCALE",
    "TRANSLATIONS_PACKAGE",
    "TRANSLATIONS_SUBDIR",
    "TranslationCatalog",
    "Translator",
    "configure_default_translator",
    "get_default_translator",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default locale used when a :class:`Translator` is constructed without
#: an explicit override. Matches :attr:`nexusdl.core.config.AppSection.language`'s
#: shipped default.
DEFAULT_LOCALE: Final[str] = "en"

#: Fallback locale used when a key is missing from the requested locale.
#: Must be a locale for which a **complete** catalog ships — today that
#: is ``en``. Changing this value requires shipping a complete catalog
#: under the new fallback locale.
FALLBACK_LOCALE: Final[str] = "en"

#: Python package path of the bundled translation catalogs.
TRANSLATIONS_PACKAGE: Final[str] = "nexusdl.data"

#: Subdirectory of :data:`TRANSLATIONS_PACKAGE` holding the catalogs.
TRANSLATIONS_SUBDIR: Final[str] = "translations"


# --------------------------------------------------------------------------- #
# TranslationCatalog                                                          #
# --------------------------------------------------------------------------- #


class TranslationCatalog:
    """One locale's loaded translations.

    Immutable after construction — the nested structure is deep-frozen
    into read-only mappings at load time so a caller that inspects
    :attr:`data` cannot mutate what other callers see. Lookup is a
    straightforward walk over the dot-separated key path; a key that
    resolves to a non-string (a nested section like ``"downloads"``)
    is treated as not-found, because translation values are always
    strings.

    Not usually constructed directly — the :class:`Translator` loads
    catalogs from files. Exposed publicly so a caller can inspect a
    loaded catalog (a diagnostic CLI, a test asserting key coverage)
    without going through the translator's private machinery.

    Attributes:
        code: The locale identifier this catalog represents
            (``"en"``, ``"fr"``, …).
        data: The parsed, deep-frozen JSON document. Read-only at every
            level.
    """

    __slots__ = ("code", "data")

    def __init__(self, code: str, data: Mapping[str, Any]) -> None:
        """Construct a catalog from parsed JSON.

        Args:
            code: The locale identifier.
            data: The parsed JSON document. Must be a mapping. The
                caller is expected to have validated this before
                passing it in — :func:`_load_catalog` does so.
        """
        self.code = code
        self.data = _deep_freeze(data)

    def lookup(self, key: str) -> str | None:
        """Return the translated string for *key*, or ``None``.

        Args:
            key: A dot-separated path into the catalog, e.g.
                ``"errors.parser.failed"``.

        Returns:
            The resolved string when the path exists and points at a
            string; ``None`` when the path does not exist, points at a
            nested object (a section rather than a leaf), or points at
            a non-string value (a number, a boolean, ``null``).
        """
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, Mapping):
                return None
            node = node.get(part)
            if node is None:
                return None
        if not isinstance(node, str):
            return None
        return node

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return f"TranslationCatalog(code={self.code!r})"


# --------------------------------------------------------------------------- #
# Translator                                                                  #
# --------------------------------------------------------------------------- #


class Translator:
    """Localisation service with a documented fallback chain.

    See the module docstring for the resolution order, the plural
    simplification, the interpolation contract, and the threading
    model. Instances are thread-safe and designed to be shared.

    Args:
        default_locale: The locale used when a call does not specify
            one. Typically ``settings.app.language``. Two-letter ISO
            639-1 code or BCP-47 tag; the language-prefix fallback is
            applied automatically when a caller requests a more specific
            tag.
        fallback_locale: The locale consulted when a key is missing from
            the requested locale. Defaults to :data:`FALLBACK_LOCALE`
            (``"en"``).
        translations_dir: Optional directory holding ``<locale>.json``
            files, overriding the bundled ``nexusdl.data/translations/``
            path. Used by tests and by a deployment that ships its own
            catalogs.

    Raises:
        ValueError: If either locale argument is empty.
    """

    __slots__ = (
        "_catalogs",
        "_default_locale",
        "_fallback_locale",
        "_lock",
        "_translations_dir",
    )

    def __init__(
        self,
        *,
        default_locale: str = DEFAULT_LOCALE,
        fallback_locale: str = FALLBACK_LOCALE,
        translations_dir: Path | None = None,
    ) -> None:
        if not default_locale:
            raise ValueError("default_locale must be non-empty")
        if not fallback_locale:
            raise ValueError("fallback_locale must be non-empty")

        self._default_locale = default_locale
        self._fallback_locale = fallback_locale
        self._translations_dir = translations_dir
        #: Locale → catalog, or locale → ``None`` for a locale whose
        #: file was missing or malformed. The ``None`` entries are
        #: cached too so a missing file is not re-read on every call.
        self._catalogs: dict[str, TranslationCatalog | None] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def translate(
        self,
        key: str,
        *,
        locale: str | None = None,
        **params: Any,
    ) -> str:
        """Translate *key* into the effective locale.

        Applies the fallback chain documented in the module docstring,
        interpolates ``{var}`` placeholders with *params*, and returns
        the key itself when no catalog contains it.

        Args:
            key: Dot-separated translation key, e.g.
                ``"errors.parser.failed"``.
            locale: The locale to translate into. ``None`` uses
                :attr:`_default_locale`.
            **params: Named values substituted into ``{var}``
                placeholders in the translated string.

        Returns:
            The translated, interpolated string, or *key* itself when no
            catalog contains it.

        Raises:
            TypeError: If *key* is not a string.
            ValueError: If *key* is empty.
        """
        _validate_key(key)
        loc = locale or self._default_locale

        for candidate in self._candidates(loc):
            catalog = self._get_catalog(candidate)
            if catalog is None:
                continue
            value = catalog.lookup(key)
            if value is not None:
                return _safe_format(value, params)
        return key

    def plural(
        self,
        key: str,
        count: int,
        *,
        locale: str | None = None,
        **params: Any,
    ) -> str:
        """Translate a pluralised key, choosing the form based on *count*.

        Looks up ``<key>_one`` for ``count == 1`` and ``<key>_other``
        for every other value. When the plural form is absent from a
        catalog, falls back to the base ``<key>`` (a translator that
        provides only the base key still works, without pluralisation).
        The interpolation params include ``count`` automatically, so a
        plural template can reference ``{count}`` without the caller
        passing it explicitly.

        Args:
            key: Dot-separated base translation key, e.g.
                ``"downloads.page_count"``. The actual key looked up
                is ``key + "_one"`` or ``key + "_other"``.
            count: The number driving the plural choice. Injected into
                the interpolation params as ``"count"``.
            locale: The locale to translate into. ``None`` uses
                :attr:`_default_locale`.
            **params: Named values substituted into ``{var}``
                placeholders. ``params["count"]`` is overwritten by
                the explicit *count* argument.

        Returns:
            The translated, interpolated string, or *key* itself when no
            catalog contains either the plural form or the base key.

        Raises:
            TypeError: If *key* is not a string, or *count* is not an
                integer.
            ValueError: If *key* is empty.
        """
        _validate_key(key)
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError(
                f"count must be an int, got {type(count).__name__}"
            )

        loc = locale or self._default_locale
        merged: dict[str, Any] = {**params, "count": count}
        suffix = _plural_suffix(count)

        for candidate in self._candidates(loc):
            catalog = self._get_catalog(candidate)
            if catalog is None:
                continue
            value = catalog.lookup(key + suffix)
            if value is not None:
                return _safe_format(value, merged)
            # Fall back to the base key within the same catalog before
            # moving to the next candidate — a translator who provides
            # only ``downloads.page_count`` still gets their string.
            value = catalog.lookup(key)
            if value is not None:
                return _safe_format(value, merged)
        return key

    def has(self, key: str, *, locale: str | None = None) -> bool:
        """Return ``True`` iff *key* resolves under the effective locale.

        Uses the same fallback chain as :meth:`translate`. A caller
        checking "can I show a specific string here?" uses this method
        to branch, rather than parsing the returned key for the
        presence of a dot.

        Args:
            key: Dot-separated translation key.
            locale: The locale to check. ``None`` uses
                :attr:`_default_locale`.

        Returns:
            ``True`` iff any catalog in the fallback chain contains the
            key as a string leaf.
        """
        _validate_key(key)
        loc = locale or self._default_locale
        for candidate in self._candidates(loc):
            catalog = self._get_catalog(candidate)
            if catalog is None:
                continue
            if catalog.lookup(key) is not None:
                return True
        return False

    def available_locales(self) -> tuple[str, ...]:
        """Return the locales with a shipped catalog, sorted alphabetically.

        Reads from the effective catalog source — the bundled package
        when :attr:`_translations_dir` is ``None``, the explicit
        directory otherwise. A catalog that has been loaded does not
        count twice; the return value reflects what is on disk, not
        what is cached.

        Returns:
            A sorted tuple of locale codes. Empty when the effective
            source contains no ``.json`` files (a broken installation,
            or an empty ``translations_dir``).
        """
        if self._translations_dir is not None:
            try:
                return tuple(
                    sorted(
                        path.stem
                        for path in self._translations_dir.glob("*.json")
                        if path.is_file()
                    )
                )
            except OSError:
                return ()
        try:
            root = (
                _resource_files(TRANSLATIONS_PACKAGE) / TRANSLATIONS_SUBDIR
            )
            return tuple(
                sorted(
                    child.name[: -len(".json")]
                    for child in root.iterdir()
                    if child.name.endswith(".json") and child.is_file()
                )
            )
        except (ModuleNotFoundError, OSError, AttributeError):
            return ()

    def clear_cache(self) -> None:
        """Drop every cached catalog.

        The next :meth:`translate` call for a locale reloads its file.
        Intended for tests that modify the bundled catalogs between
        test cases; production code never calls this — the catalogs
        are immutable and loaded once.
        """
        with self._lock:
            self._catalogs.clear()

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        with self._lock:
            loaded = sum(
                1 for cat in self._catalogs.values() if cat is not None
            )
        return (
            f"Translator(default={self._default_locale!r}, "
            f"fallback={self._fallback_locale!r}, "
            f"loaded={loaded})"
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _candidates(self, locale: str) -> tuple[str, ...]:
        """Return the ordered fallback chain for *locale*, deduplicated.

        The chain is:

        1. *locale* exactly (``en-GB``).
        2. Its language prefix (``en``) when different from step 1.
        3. :attr:`_fallback_locale`.

        Duplicates are dropped while preserving order, so a caller
        passing ``"en"`` produces ``("en",)`` and not ``("en", "en",
        "en")``.
        """
        result: list[str] = []
        seen: set[str] = set()
        for candidate in (
            locale,
            locale.split("-", 1)[0],
            self._fallback_locale,
        ):
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return tuple(result)

    def _get_catalog(self, code: str) -> TranslationCatalog | None:
        """Return *code*'s catalog, loading it on first access.

        A missing or malformed file is cached as ``None`` so it is not
        re-read on every subsequent call. This is a deliberate choice:
        a broken catalog is a bug to fix, not a condition to retry.

        The lock is held during the load. For the four bundled catalogs
        this is fine — each is under 50 KiB and the load happens once
        per process. If the catalog set ever grows to dozens of files,
        the load should be hoisted outside the lock (double-checked
        pattern, like :mod:`nexusdl.core.compliance`'s ``_get_robots``).
        """
        with self._lock:
            if code in self._catalogs:
                return self._catalogs[code]
            catalog = _load_catalog(code, self._translations_dir)
            self._catalogs[code] = catalog
            return catalog


# --------------------------------------------------------------------------- #
# Module-level default translator                                             #
# --------------------------------------------------------------------------- #
# A process-wide singleton exists so that code which cannot receive a
# Translator via injection — CLI subcommands that run before the
# composition root, exception handlers that want to localise a message
# without context — still has a working translator. The composition
# root overrides it once Settings is loaded via
# ``configure_default_translator``. Tests reset it with
# ``configure_default_translator(None)``.
# --------------------------------------------------------------------------- #

_default_translator: Translator | None = None
_default_lock = threading.Lock()


def get_default_translator() -> Translator:
    """Return the process-wide default :class:`Translator`.

    The translator is created lazily on first access with
    :data:`DEFAULT_LOCALE` as its default locale. The composition root
    overrides it via :func:`configure_default_translator` once the
    settings-driven default locale is known. Prefer dependency
    injection when possible — the default translator exists as an
    escape hatch for code paths that cannot receive one.

    Returns:
        The current default translator, creating one if none has been
        configured yet.
    """
    global _default_translator
    # Fast path — the common case after the composition root has run.
    if _default_translator is not None:
        return _default_translator
    with _default_lock:
        # Double-check under the lock — another coroutine may have
        # initialised it while we were acquiring.
        if _default_translator is None:
            _default_translator = Translator()
        return _default_translator


def configure_default_translator(
    translator: Translator | None,
) -> None:
    """Replace the process-wide default :class:`Translator`.

    The composition root calls this once, after constructing a
    :class:`Translator` with the settings-driven default locale::

        settings = Settings()
        translator = Translator(default_locale=settings.app.language)
        configure_default_translator(translator)

    A test that wants to isolate its own translator calls
    ``configure_default_translator(None)`` in a fixture teardown to
    reset the module to its lazy default.

    Args:
        translator: The translator to install, or ``None`` to reset the
            module to its lazily-created default.
    """
    global _default_translator
    with _default_lock:
        _default_translator = translator


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #


def _validate_key(key: str) -> None:
    """Raise on a malformed translation key.

    Args:
        key: The candidate key.

    Raises:
        TypeError: If *key* is not a string.
        ValueError: If *key* is empty.
    """
    if not isinstance(key, str):
        raise TypeError(
            f"translation key must be a str, got {type(key).__name__}"
        )
    if not key:
        raise ValueError("translation key must be non-empty")


def _plural_suffix(count: int) -> str:
    """Return the plural-form suffix appropriate for *count*.

    Uses the simplified CLDR rule documented in the module docstring:
    ``_one`` for ``count == 1``, ``_other`` for every other value
    (including ``0`` and negatives).

    Args:
        count: The count.

    Returns:
        ``"_one"`` or ``"_other"``.
    """
    return "_one" if count == 1 else "_other"


class _MissingPlaceholder:
    """Marker for a format field whose value is absent from the params.

    Carries the **full field name** — ``"name"`` for ``{name}``,
    ``"user.name"`` for ``{user.name}``, ``"items[0]"`` for
    ``{items[0]}`` — so that the placeholder survives verbatim
    regardless of nested access. The marker is intercepted by
    :meth:`_SafeFormatter.get_field` (which wraps the whole failed
    field), :meth:`_SafeFormatter.convert_field` (so ``!r``, ``!s``,
    ``!a`` are not applied), and :meth:`_SafeFormatter.format_field`
    (so ``format_spec`` is not applied).

    The net effect: ``"{user.name}"`` with no ``user`` stays
    ``"{user.name}"`` (not ``"{user}"``), which is the documented
    interpolation contract.
    """

    __slots__ = ("field_name",)

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def render(self) -> str:
        """Return the placeholder as ``"{field_name}"``."""
        return "{" + self.field_name + "}"


class _SafeFormatter(string.Formatter):
    """A :class:`string.Formatter` that leaves missing fields untouched.

    Four hooks are overridden so that a missing or failing field:

    * does not trigger a :class:`KeyError` / :class:`IndexError` /
      :class:`AttributeError` / :class:`TypeError`
      (:meth:`get_field` catches every exception from the parent and
      returns a :class:`_MissingPlaceholder` carrying the full
      ``field_name``),
    * does not have its ``!r`` / ``!s`` / ``!a`` conversion applied
      (:meth:`convert_field` short-circuits),
    * does not have its ``format_spec`` applied
      (:meth:`format_field` short-circuits).

    The net effect is that ``"{name}"``, ``"{name!r}"``,
    ``"{name:>10}"``, ``"{user.name}"``, and ``"{items[0]}"`` all
    survive **verbatim** when their field is missing — which is the
    documented interpolation contract.
    """

    def get_field(
        self,
        field_name: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> tuple[Any, str]:
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, IndexError, AttributeError, TypeError):
            # Any failure to resolve the field — a missing root, a
            # missing attribute, a missing index, a value that does
            # not support the accessor — produces a placeholder
            # carrying the *whole* field name, so the placeholder
            # survives verbatim. Other placeholders in the same
            # template still interpolate, because this hook only
            # replaces the failing one.
            return (_MissingPlaceholder(field_name), field_name)

    def convert_field(
        self,
        value: Any,
        conversion: str | None,
    ) -> Any:
        if isinstance(value, _MissingPlaceholder):
            return value
        return super().convert_field(value, conversion)

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, _MissingPlaceholder):
            return value.render()
        return super().format_field(value, format_spec)


_SAFE_FORMATTER: Final[_SafeFormatter] = _SafeFormatter()


def _safe_format(template: str, params: Mapping[str, Any]) -> str:
    """Interpolate *params* into *template*, tolerating missing fields.

    Present parameters are substituted; absent parameters leave the
    **whole placeholder** untouched — the field name (including any
    nested access like ``{user.name}``) survives verbatim and the
    ``!r`` / ``!s`` / ``!a`` conversion, if any, is dropped (dropping
    a conversion on a missing field is the only way to leave the
    placeholder intact; see the module docstring).

    A malformed template (unclosed brace, invalid format spec on a
    present field, non-string attribute access on a present field)
    that cannot be recovered placeholder-by-placeholder is returned
    verbatim — the caller sees their own bug rather than crashing the
    UI. The per-placeholder isolation in :class:`_SafeFormatter`
    means a single failing field does **not** abort the interpolation
    of the rest.

    Args:
        template: The translation string, with ``{var}`` placeholders.
        params: The parameter mapping.

    Returns:
        The interpolated string, or *template* unchanged when
        interpolation fails as a whole.
    """
    if not params:
        return template
    try:
        return _SAFE_FORMATTER.vformat(template, (), dict(params))
    except (ValueError, IndexError, AttributeError, TypeError):
        # ValueError: unclosed brace in the template itself (not a
        # field failure — those are caught inside ``get_field``).
        # The other exceptions should not occur because ``get_field``
        # intercepts them, but they are kept as a defence-in-depth
        # fallback for a ``str.format`` edge case we have not
        # anticipated.
        return template


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze *value* into read-only containers.

    Dicts become :class:`types.MappingProxyType`, lists and tuples
    become tuples, everything else is returned unchanged. Used by
    :class:`TranslationCatalog` so a caller that inspects
    :attr:`TranslationCatalog.data` cannot mutate what another caller
    sees.

    Args:
        value: The value to freeze.

    Returns:
        The frozen equivalent.
    """
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _load_catalog(
    code: str,
    translations_dir: Path | None,
) -> TranslationCatalog | None:
    """Load and parse a locale's catalog file.

    Reads from :data:`translations_dir` when provided, from the bundled
    ``nexusdl.data/translations/<code>.json`` otherwise. Any failure —
    a missing file, an unreadable file, invalid UTF-8, invalid JSON, a
    JSON document that is not a mapping — yields ``None`` rather than
    raising. The caller (:meth:`Translator._get_catalog`) caches the
    ``None`` so the failure is not retried.

    Args:
        code: The locale identifier.
        translations_dir: The directory to read from, or ``None`` to
            read from the bundled package.

    Returns:
        The loaded catalog, or ``None`` on any failure.
    """
    text: str | None = None
    if translations_dir is not None:
        try:
            text = (translations_dir / f"{code}.json").read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            return None
    else:
        try:
            resource = (
                _resource_files(TRANSLATIONS_PACKAGE)
                / TRANSLATIONS_SUBDIR
                / f"{code}.json"
            )
            text = resource.read_text(encoding="utf-8")
        except (
            ModuleNotFoundError,
            OSError,
            AttributeError,
            UnicodeDecodeError,
        ):
            return None

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    return TranslationCatalog(code, document)
