"""Reusable Textual widgets for the NexusDL TUI.

The five widgets in this package are the visual building blocks the TUI
screens compose together. Every screen in ``nexusdl.interfaces.cli.screens``
is built from these widgets (and Textual's own built-in widgets, of
course); nothing in a screen reimplements what a widget here provides.

Widgets in this package
-----------------------
::

    ChapterList      a manga's chapters as a selectable table
                     → ChapterList.ChapterActivated
                     → ChapterList.SelectionChanged

    LogViewer        a live stream of loguru records with level filter
    LogEntry         the plain-data record shape LogViewer consumes

    MangaCard        one manga rendered as a focusable card
                     → MangaCard.Selected

    ProgressBar      a live download-progress dashboard
                     → (display-only, no messages)

    SiteSelector     a source-site picker
                     → SiteSelector.SiteSelected

Three of the five widgets emit messages; two are display-only.
:class:`ChapterList` emits two distinct messages because it exposes two
distinct selection states (cursor and marked set — see its docstring).

Message handling convention
---------------------------
Every widget message is a **nested class** on the emitting widget,
following Textual's own convention for widget messages. The parent
screen's handler name is therefore
``on_<snake_case(widget_class)>_<snake_case(message_class)>``:

===================================  =====================================  =============================================
Widget                               Message                                Handler
===================================  =====================================  =============================================
``SiteSelector``                     ``SiteSelected``                       ``on_site_selector_site_selected``
``MangaCard``                        ``Selected``                           ``on_manga_card_selected``
``ChapterList``                      ``ChapterActivated``                   ``on_chapter_list_chapter_activated``
``ChapterList``                      ``SelectionChanged``                   ``on_chapter_list_selection_changed``
===================================  =====================================  =============================================

The long handler names are the correct outcome of Textual's convention
— they disambiguate each message from any other widget's message of
the same short name. A screen that has both a ``MangaCard`` and a
``SiteSelector`` handles ``on_manga_card_selected`` and
``on_site_selector_site_selected`` as two distinct methods even though
both messages are named ``...Selected``.

Composition contract
--------------------
Every widget in this package follows the same three conventions:

* **Translator injection.** Each widget's ``__init__`` accepts an
  optional ``translator: Translator | None = None`` argument. When
  ``None`` (the default), the widget uses
  :func:`~nexusdl.core.i18n.get_default_translator` — the process-wide
  translator the composition root configures at startup. A test that
  wants to assert on translated strings injects a test translator
  explicitly; a screen that already holds a translator passes it
  through so the whole screen uses the same instance.
* **Text-based rendering.** No widget builds a label with
  :meth:`~textual.content.Text.from_markup`. Every user-visible
  string — including strings sourced from ``sites.yaml`` (site names),
  ``mangas.title``, ``chapters.title``, and log messages — is
  appended literally via :meth:`~textual.content.Text.append`.
  A chapter titled ``"[[Live]]"`` displays as-is; the widget does not
  interpret ``[`` as a markup tag.
* **CSS override.** Every widget declares a ``DEFAULT_CSS`` that is
  self-sufficient out of the box. A deployment that wants a different
  theme overrides rules by targeting the widget class (``SiteSelector``)
  or the widget's stable ``#id`` attributes; a deployment that wants
  the default appearance does nothing. No widget requires any rule
  beyond its ``DEFAULT_CSS`` to function.

Localisation
------------
The five widgets introduce **32 translation keys** under four
namespaces (``site.selector.*``, ``downloads.progress.*``,
``manga.card.*``, ``logs.viewer.*``, ``chapter.list.*``). Each widget's
module docstring lists its own keys with example English values.

A key that is missing from the active catalog falls back through the
chain documented in :mod:`nexusdl.core.i18n` — a French catalog without
``chapter.list.empty`` shows the English value; an English catalog
without it shows the key verbatim (``"chapter.list.empty"``), which is
greppable in the catalogs and visible in the UI. The fallback is
intentional: a UI that displays a raw key is more useful to a
translator than a UI that displays an empty string.

The complete set of keys the pipeline's UI introduces is discoverable
by grepping ``self._translator.translate("`` (or
``self._translator.plural("``) across the five widget modules. A CI
check that a new key is added to every catalog is on the roadmap, not
in this package — see :mod:`nexusdl.core.i18n`'s "What this module
deliberately does not do" section.

Import cost
-----------
Importing this package imports :mod:`textual` (which itself imports
:mod:`rich` and a sizeable portion of the standard library's terminal
machinery) and then every widget in it. That cost is paid **once per
process** — Python caches modules — and is negligible compared to
Textual's own startup cost.

A **non-TUI** consumer that only needs one widget's type — a test that
wants to assert on :class:`ChapterList` without launching a TUI, for
example — can import from the specific submodule
(``from nexusdl.interfaces.cli.widgets.chapter_list import ChapterList``)
and pay for only that widget's dependencies. In practice, every
consumer of this package is a TUI screen, which loads every widget
anyway when the app renders the screen; the submodule-import case is a
test convenience, not a production optimization.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change. The re-exported symbols
fall into two tiers:

* **Tier 1 — the six widget and record classes.**
  :class:`ChapterList`, :class:`LogEntry`, :class:`LogViewer`,
  :class:`MangaCard`, :class:`ProgressBar`, :class:`SiteSelector`.
  Renaming any of them is a breaking change for every screen in
  ``nexusdl.interfaces.cli.screens``; the composition root, the tests,
  and any external tool that monkey-patches the TUI all reference them
  by name. Their constructor signatures may grow (a new keyword
  argument with a default) across minor releases, but the existing
  parameters do not change.
* **Tier 2 — the nested messages.** ``ChapterList.ChapterActivated``,
  ``ChapterList.SelectionChanged``, ``MangaCard.Selected``,
  ``SiteSelector.SiteSelected``. Renaming any of them is also a
  breaking change — the handler name a screen implements is derived
  from the message name, and Textual's dispatch mechanism binds them
  by convention. The messages are **not** re-exported at this
  package's ``__all__`` because the Textual convention is to reference
  them via the emitting widget (``MangaCard.Selected``, not
  ``Selected``); they are nevertheless part of the stable contract.

What is not re-exported
-----------------------
Nothing else. Every private helper, CSS class constant, and module-
level function (``_truncate`` in :mod:`progress_bar`, ``_join_tags`
in :mod:`manga_card`, ``_LEVEL_STYLE`` and friends in
:mod:`log_viewer`, …) is available from its specific submodule but is
not re-exported here. This follows the same rule the other
``__init__.py`` files in the codebase use: **the package's public
surface is what a screen consumes, and a screen never touches an
underscore-prefixed helper.**

The four CSS class-name prefixes (``manga-card-*``, ``task-*``,
``chapter-*`` — where the third is unused, see each widget's
``DEFAULT_CSS``) are **not** constants: they are literal strings that
appear in the widget's ``DEFAULT_CSS`` and in the widget's own code.
A deployment that wants to override a rule references the literal
class name (``.manga-card-title``), not a Python symbol. That is
Textual's convention, and mirroring the class names as Python
constants here would create the same two-source-of-truth hazard the
rest of the codebase works hard to avoid.

Dependency graph (no cycles)::

    nexusdl.core.downloader.progress  ─┐
    nexusdl.core.i18n                  │
    nexusdl.core.models.*              ├──► interfaces.cli.widgets
    nexusdl.core.utils.text            │
    textual                            │
                                       │

Every widget imports from :mod:`nexusdl.core` through its public
sub-packages only. No widget touches a private attribute of a core
module, and no widget imports another widget from this package — a
chapter list does not construct manga cards, a site selector does not
embed a progress bar. **Cross-widget composition is the screen's
responsibility**, not the widget's; the widgets stay independently
testable and independently themed.
"""

from __future__ import annotations

# --- Chapter list -------------------------------------------------------- #
from nexusdl.interfaces.cli.widgets.chapter_list import ChapterList

# --- Log viewer --------------------------------------------------------- #
from nexusdl.interfaces.cli.widgets.log_viewer import LogEntry, LogViewer

# --- Manga card --------------------------------------------------------- #
from nexusdl.interfaces.cli.widgets.manga_card import MangaCard

# --- Progress bar ------------------------------------------------------- #
from nexusdl.interfaces.cli.widgets.progress_bar import ProgressBar

# --- Site selector ------------------------------------------------------ #
from nexusdl.interfaces.cli.widgets.site_selector import SiteSelector

__all__ = [
    # --- Chapter list ---------------------------------------------------- #
    "ChapterList",
    # --- Log viewer ------------------------------------------------------ #
    "LogEntry",
    "LogViewer",
    # --- Manga card ------------------------------------------------------ #
    "MangaCard",
    # --- Progress bar ---------------------------------------------------- #
    "ProgressBar",
    # --- Site selector --------------------------------------------------- #
    "SiteSelector",
]
