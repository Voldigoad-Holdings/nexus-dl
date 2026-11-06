"""Runtime configuration for the NexusDL Textual TUI.

This module is the **TUI's own configuration** — distinct from
:mod:`nexusdl.core.config`, which holds the application-wide settings
shared by every interface. The distinction matters:

* :class:`~nexusdl.core.config.Settings` describes the whole app: HTTP
  timeouts, download concurrency, packaging formats, database paths,
  logging sinks. It is loaded from ``.env``, ``config.yaml``, and the
  bundled defaults, and it is consumed by the CLI, the TUI, the web
  backend, and every pipeline module — not just the TUI.
* :class:`TuiConfig` — the object this module builds — describes only
  what the Textual :class:`~textual.app.App` needs to launch: the
  window title, the version string in the sub-title, the CSS path, the
  Textual theme name, and a couple of behaviour flags. Everything in
  it is derived from :class:`Settings` plus a few constants that only
  the TUI cares about.

Keeping the two separate means that:

1. ``app.py`` never has to reach into ``Settings`` to pluck the fields
   it needs — it receives a single, small, fully-resolved
   :class:`TuiConfig` object.
2. A future TUI-specific knob (e.g. a "compact layout" toggle) lands
   here, not in ``core/config.py``, where it would be visible to the
   web backend and the CLI for no reason.
3. Tests can construct a :class:`TuiConfig` directly without having to
   load a full :class:`Settings` object, which requires the three
   required secrets.

What is derived from Settings
-----------------------------
Every field of :class:`TuiConfig`:

===============================  ======================================  ================================
:class:`TuiConfig` field         Source                                   Default (when not set)
===============================  ======================================  ================================
``title``                        ``settings.app.name``                    ``"NexusDL"``
``sub_title``                    the explicit ``version`` argument        ``""``
``theme``                        ``settings.tui.theme``                   ``"textual-dark"``
``animations``                   ``settings.tui.animations``              ``True``
``command_palette``              ``True`` (fixed)                         ``True``
``css_path``                     the ``css_path`` argument                ``None`` (use the App's default)
===============================  ======================================  ================================

The two fields that are **not** derived from Settings are deliberate:

* ``command_palette`` has no corresponding :class:`Settings` field
  because every shipped deployment wants Textual's built-in command
  palette (``Ctrl+P``) enabled — a TUI without it loses its most
  useful discoverability feature, and no operator has ever asked to
  turn it off. When one does, a field is added to ``core/config.py``
  and this module starts reading it; until then, the constant lives
  here.
* ``css_path`` is a filesystem concern that the composition root
  controls. ``app.py`` sets ``CSS_PATH = "theme.tcss"`` as a class
  attribute; :class:`TuiConfig.css_path` allows an override for testing
  or for a deployment that ships its own theme file. In production, the
  field is ``None`` and Textual resolves the default.

The ``animations`` → ``App.animation_level`` transformation
-----------------------------------------------------------
:attr:`TuiConfig.animations` is a :class:`bool`; Textual's
:attr:`~textual.app.App.animation_level` is a
:class:`~typing.Literal` of ``"none"``, ``"basic"``, or ``"full"``.
The mapping is performed by :mod:`nexusdl.interfaces.cli.app` at
construction time::

    app.animation_level = "full" if config.animations else "none"

The field is a :class:`bool` rather than a Textual ``Literal`` because
the pipeline's :attr:`~nexusdl.core.config.TuiSection.animations` field
is a :class:`bool`, and a simple on/off toggle is what the TUI actually
needs. A caller that wants the ``"basic"`` level (which is rarely
useful — it only disables *decorative* animations, not functional ones)
sets ``app.animation_level`` directly after construction; the
:class:`TuiConfig` does not expose a level-3 knob.

Theme name validation
---------------------
Textual's :attr:`~textual.app.App.theme` setter raises
:class:`~textual.theme.InvalidThemeError` when the requested theme name
does not exist. That happens at App construction time, which is **after**
the composition root has done every other piece of work — settings
loaded, plugins loaded, database migrated, HTTP session built. A typo in
``NEXUSDL_TUI__THEME`` would therefore crash the TUI after a
multi-second startup.

:func:`build_tui_config` **pre-validates** the theme name and falls
back to :data:`_FALLBACK_THEME` (``"textual-dark"``) with a warning when
the requested name is not one of Textual's built-in themes. The user
sees a working TUI plus a warning about the typo, instead of a
half-booted app that crashes on the first frame.

The list of valid themes is discovered **at import time** from
:mod:`textual.theme` — see :data:`_KNOWN_THEMES`. If the module cannot
be imported (an old Textual version), the fallback list is the two
themes every supported Textual version has shipped since 0.86
(``textual-dark`` and ``textual-light``), and theme validation becomes
a no-op (any name is passed through). That is intentional: refusing to
launch because the theme registry is missing would be worse than
letting Textual's own validation run at mount.

Custom themes and the fallback
------------------------------
The fallback replaces an unknown theme name with
:data:`_FALLBACK_THEME` **before** the :class:`TuiConfig` is constructed
— so a user who has registered a custom theme on the
:class:`~textual.app.App` **after** calling :func:`build_tui_config` and
**before** ``app.run_async()`` will see their theme applied normally,
because the App's own theme setter runs after the fallback was chosen.

The scenario where the custom theme **does not** work is different and
worth naming: a caller that wants the App to start with the custom
theme applied must register it **and** set ``App.theme`` explicitly
after construction. :class:`TuiConfig.theme` is a **default**, not a
binding: it is what ``app.py`` assigns to ``App.theme`` at
construction time, and a subsequent ``app.theme = "my-custom-theme"``
(from the composition root's ``on_mount``, or from a test) overrides
it cleanly.

Put differently: :func:`build_tui_config` cannot know about themes
registered by a plugin or a future release, so it treats an unknown
name as a "likely typo". The correct way for a deployment to use a
custom theme is to (a) let ``build_tui_config`` fall back to
``"textual-dark"``, (b) register the custom theme on the App, and (c)
set ``App.theme`` to the custom name after construction. The
``WARNING`` that ``build_tui_config`` logs is the only signal that the
custom theme was not picked up by the default path — an operator who
wants to silence it sets ``NEXUSDL_TUI__THEME=textual-dark`` (or any
built-in name) and overrides at the App level.

CSS path resolution
-------------------
``app.py`` declares ``CSS_PATH = "theme.tcss"`` as a class attribute,
which Textual resolves **relative to the module's file**. The
:file:`theme.tcss` file lives next to ``app.py`` in
``nexusdl/interfaces/cli/``. This is Textual's standard resolution rule
and the one the tree was designed around.

:class:`TuiConfig.css_path` exists to allow a caller (a test, a
deployment that ships its own theme) to override that resolution with
an explicit path. When the field is ``None``, ``app.py`` leaves the
class attribute in place and Textual resolves it as usual.

Dependency graph (no cycles)::

    nexusdl.core.config      ─┐
    nexusdl.core.constants    ├──► interfaces.cli.config
    textual                  ─┘

``nexusdl.core.config`` is imported **under** :data:`TYPE_CHECKING` only
— :func:`build_tui_config` accepts a :class:`~nexusdl.core.config.Settings`
and reads five of its fields, but this module never constructs one, so
it does not need to import the full settings-loading machinery at
runtime. A non-TUI consumer that imports :class:`TuiConfig` for a test
pays only for :mod:`textual.theme` (a tiny module).

Not shipped yet
---------------
The tree declares :file:`src/nexusdl/interfaces/cli/theme.tcss` — the
CSS file ``app.py`` sets ``CSS_PATH`` to. It is a separate file to be
delivered in the "theme.tcss" step; this module references it only by
name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from loguru import logger

from nexusdl.core.constants import APP_NAME

if TYPE_CHECKING:
    from nexusdl.core.config import Settings


__all__ = ["TuiConfig", "build_tui_config"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the CSS file, relative to ``app.py``'s module directory.
#: Referenced by :class:`~textual.app.App.CSS_PATH`; kept here so both
#: the class attribute (in ``app.py``) and any diagnostic that shows the
#: expected path (a future ``nexusdl config paths`` extension for TUI
#: assets) reference the same string.
_CSS_FILENAME: Final[str] = "theme.tcss"

#: Textual's default dark theme name. The fallback used when
#: :attr:`~nexusdl.core.config.TuiSection.theme` names a theme that
#: Textual does not know about.
_FALLBACK_THEME: Final[str] = "textual-dark"

#: The complete set of built-in theme names Textual recognises, plus
#: the two names that every supported version has shipped since 0.86.
#: Discovered once at import time — see the module docstring's "Theme
#: name validation" section for the fallback when the discovery fails.
#:
#: Note that this set is **not** a hard contract — a user can register
#: a custom theme on the :class:`~textual.app.App` before launching, and
#: the resulting name would be valid at :meth:`App.run_async` time even
#: though it is not in this set. :func:`build_tui_config` treats an
#: unknown name as a "likely typo" (falls back to the default and warns)
#: rather than as an error — the user has a clear, actionable message
#: and can still override at runtime. See the module docstring's
#: "Custom themes and the fallback" section for the correct pattern.
def _discover_known_themes() -> frozenset[str]:
    """Return the set of theme names Textual recognises at import time.

    Reads from :mod:`textual.theme`'s ``BUILTIN_THEMES`` mapping when
    the module is available. On an older Textual version (pre-0.86) the
    mapping does not exist; the fallback set covers the two themes
    every supported version has.

    Returns:
        A frozenset of lowercase theme names. Empty is never returned —
        the fallback guarantees at least :data:`_FALLBACK_THEME`.
    """
    try:
        from textual.theme import BUILTIN_THEMES  # noqa: PLC0415
    except ImportError:
        return frozenset({_FALLBACK_THEME, "textual-light"})
    return frozenset(BUILTIN_THEMES.keys()) | {_FALLBACK_THEME}


_KNOWN_THEMES: Final[frozenset[str]] = _discover_known_themes()

_log = logger.bind(component="interfaces.cli.config")


# --------------------------------------------------------------------------- #
# TuiConfig                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TuiConfig:
    """Resolved runtime configuration for the NexusDL TUI.

    Immutable after construction — the fields are set once by
    :func:`build_tui_config` and read by
    :mod:`nexusdl.interfaces.cli.app` when it constructs the
    :class:`~textual.app.App`. A caller that wants a different
    configuration builds a new :class:`TuiConfig`; there is no mutation
    path.

    The fields map to properties the App exposes:

    * :attr:`title` → :attr:`textual.app.App.TITLE` (direct mapping)
    * :attr:`sub_title` → :attr:`textual.app.App.SUB_TITLE` (direct
      mapping)
    * :attr:`theme` → :attr:`textual.app.App.theme` (direct mapping;
      the App's setter re-validates, so an unknown custom theme set
      after construction still works — see the module docstring's
      "Custom themes and the fallback" section)
    * :attr:`animations` → **inverse of**
      :attr:`textual.app.App.animation_level` (``"full"`` when
      ``True``, ``"none"`` when ``False``); the transformation is
      performed by ``app.py``, not by this module — see the module
      docstring's "The ``animations`` → ``App.animation_level``
      transformation" section
    * :attr:`command_palette` → :attr:`textual.app.App.ENABLE_COMMAND_PALETTE`
      (direct mapping)
    * :attr:`css_path` → :attr:`textual.app.App.CSS_PATH` when not
      ``None``; the class attribute when ``None``

    Attributes:
        title: The window title. Derived from
            ``settings.app.name`` — the display name a deployment can
            override via ``NEXUSDL_APP__NAME``. An empty name is
            replaced by :data:`nexusdl.core.constants.APP_NAME` so the
            App always has a non-empty title.
        sub_title: The sub-title (rendered next to the title in the
            app's footer). Typically ``"v{version}"``. Empty string
            when the version is not available — matching the
            convention that ``App.SUB_TITLE`` accepts an empty string.
        theme: The Textual theme name. Validated against
            :data:`_KNOWN_THEMES` at build time; an unknown name is
            replaced by :data:`_FALLBACK_THEME` before this object is
            returned. The App's own theme setter runs **after**
            construction, so a caller that assigns a custom theme
            explicitly still applies it.
        animations: Whether Textual's animations are enabled. The App
            sets ``animation_level = "full"`` when this is ``True``,
            ``"none"`` when ``False`` — the mapping is done in
            ``app.py``, not here.
        command_palette: Whether Textual's ``Ctrl+P`` command palette is
            enabled. Always ``True`` in this release — see the module
            docstring for the rationale.
        css_path: Optional explicit CSS path, overriding
            ``app.py``'s ``CSS_PATH`` class attribute. ``None`` (the
            default) means "use the class attribute, resolved by
            Textual relative to ``app.py``". A caller with a custom
            theme file passes the absolute path explicitly.
    """

    title: str
    sub_title: str
    theme: str
    animations: bool
    command_palette: bool
    css_path: Path | None


# --------------------------------------------------------------------------- #
# Factory                                                                     #
# --------------------------------------------------------------------------- #


def build_tui_config(
    settings: Settings,
    *,
    version: str = "",
    css_path: Path | None = None,
) -> TuiConfig:
    """Build a :class:`TuiConfig` from a :class:`Settings` object.

    Reads the fields documented in the module docstring's "What is
    derived from Settings" table. The theme name is validated against
    :data:`_KNOWN_THEMES`; an unknown name is replaced by
    :data:`_FALLBACK_THEME` and a ``WARNING`` is logged — see the
    module docstring's "Theme name validation" and "Custom themes and
    the fallback" sections.

    Args:
        settings: The application settings. Only
            ``settings.app.name``, ``settings.tui.theme``, and
            ``settings.tui.animations`` are read.
        version: The version string rendered in the sub-title. The
            caller passes :data:`nexusdl.version.__version__` (which
            is not imported here so this module can be tested without
            the version package). Empty string yields an empty
            sub-title — ``App.SUB_TITLE`` accepts that.
        css_path: Optional explicit CSS path. ``None`` (the default)
            means "use ``app.py``'s ``CSS_PATH`` class attribute", which
            Textual resolves to :file:`theme.tcss` next to ``app.py``.

    Returns:
        A fully-resolved :class:`TuiConfig`, ready to be passed to
        :class:`~nexusdl.interfaces.cli.app.NexusDLApp`.
    """
    return TuiConfig(
        title=settings.app.name or APP_NAME,
        sub_title=f"v{version}" if version else "",
        theme=_resolve_theme(settings.tui.theme),
        animations=settings.tui.animations,
        command_palette=True,
        css_path=css_path,
    )


# --------------------------------------------------------------------------- #
# Theme resolution                                                            #
# --------------------------------------------------------------------------- #


def _resolve_theme(requested: str) -> str:
    """Return a Textual theme name that is guaranteed to exist.

    When *requested* is one of :data:`_KNOWN_THEMES`, it is returned
    unchanged. Otherwise a ``WARNING`` is logged — naming the bad value
    and the fallback — and :data:`_FALLBACK_THEME` is returned. See the
    module docstring's "Theme name validation" section for why a
    warning rather than an error.

    The fallback is a **default**, not a binding: an App that registers
    a custom theme and then sets ``App.theme`` explicitly after
    construction still applies the custom theme. The warning is the
    signal that the custom theme was not picked up by the default
    path; see the module docstring's "Custom themes and the fallback"
    section for the correct deployment pattern.

    Args:
        requested: The theme name from
            :attr:`~nexusdl.core.config.TuiSection.theme`.

    Returns:
        A theme name that Textual recognises.
    """
    if requested in _KNOWN_THEMES:
        return requested
    _log.warning(
        "Unknown TUI theme {!r} (expected one of {}); "
        "falling back to {!r}",
        requested,
        ", ".join(sorted(_KNOWN_THEMES)),
        _FALLBACK_THEME,
    )
    return _FALLBACK_THEME
