"""Textual screen displaying the keyboard shortcuts available in the TUI.

The help screen is a **catalog of keybindings** — the reference a user
opens when they cannot remember how to mark a chapter, clear the log,
or return to the previous screen. It is a documentation surface, not an
operational one: it performs no I/O, holds no state that outlives the
screen, and posts no messages.

Two-pane layout
---------------
A two-pane split, matching the pattern established by
:class:`~nexusdl.interfaces.cli.screens.settings.SettingsScreen`:

* **Left pane** — an :class:`~textual.widgets.OptionList` listing every
  category (Global, Navigation, Search, Download, Settings, Logs).
  Navigation is keyboard-first (``↑`` / ``↓``); the content pane
  updates as the highlight moves.
* **Right pane** — a scrollable
  :class:`~textual.containers.VerticalScroll` wrapping a
  :class:`~textual.widgets.Static` whose content is the rendered
  category. Each entry is a two-column list: the keys on the left
  (bold cyan, aligned) and the description on the right.

The screen is dismissable from any state with ``Escape`` or ``q`` — the
same binding every other screen in this package uses, so the gesture is
consistent across the TUI.

A persistent hint line at the bottom of the screen (``help.hint``)
surfaces the "press Esc or q to close" affordance **without** requiring
the user to look at the Global category. The information is redundant
with the ``help.binding.back`` row inside Global — deliberately so:
a first-time user who opens Help to learn a different binding must not
have to navigate to a specific category to learn how to leave the
screen.

Binding catalog
---------------
The screen renders a **static catalog**, defined at module scope in
:data:`_CATALOG`. The catalog is the single source of truth for what
the help screen displays; it is **not** introspected from the running
app. Two reasons:

* **Legibility.** Introspecting the app would surface Textual's
  built-in bindings (``Ctrl+P`` command palette, ``Ctrl+\\`` quit on
  some platforms, …) alongside the application's own, which is noise
  in a user-facing help.
* **Documentation of intent.** The catalog documents what the TUI is
  **designed** to do. If a screen gains a new binding without the
  catalog being updated, that is a bug in the catalog, not a hidden
  feature. The catalog's presence makes the omission visible during
  review.

A developer adding a binding to a screen or widget must extend the
catalog here. The rule is enforced by review, not by automation —
automating it would require every screen to expose its bindings to
this screen, which is a coupling that buys nothing (the catalog is 40
lines).

Not-a-message-source
--------------------
The help screen posts **no messages**. It has no consumer and no
parent screen logic depends on it; the only action it exposes is
``back``, which is handled entirely inside the screen. A parent
screen that pushes the help screen does not need a handler.

Localisation
------------
The screen introduces **24 translation keys** under ``help.*``. The
category titles, the description of every binding, the screen title,
and the persistent hint are all localised; the key names themselves
(``Esc``, ``Ctrl+Q``, ``↑``, …) are not — they are symbols, not
prose, and every locale uses the same ones. The split is:

* **6 category titles** — ``help.category.{global,navigation,search,download,settings,logs}``.
* **16 binding descriptions** — ``help.binding.*``. Several bindings
  share a key (``help.binding.back`` is used by four categories): the
  key count is 24, not 26, because sharing is how the catalog avoids
  drift between two categories that legitimately use the same
  gesture.
* **2 structural keys** — ``help.title`` (the screen header) and
  ``help.hint`` (the persistent bottom line).

A missing key renders the key verbatim, matching the contract every
other widget and screen in this package follows. See
:mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.i18n    ─┐
    textual               ├──► interfaces.cli.screens.help
                          │

The screen imports no other screen and no widget from this package —
its content is entirely defined by :data:`_CATALOG`. That isolation is
deliberate: the help screen must not break because a widget's API
changed, only because the catalog was left out of date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Text
from textual.screen import Screen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from nexusdl.core.i18n import Translator, get_default_translator


__all__ = ["BindingCategory", "BindingInfo", "HelpScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so the CSS below and the
#: ``query_one`` calls in the code cannot drift apart.
_ID_TITLE: Final[str] = "help-title"
_ID_CATEGORIES: Final[str] = "help-categories"
_ID_CONTENT: Final[str] = "help-content"
_ID_HINT: Final[str] = "help-hint"

#: Width, in cells, of the keys column. Sixteen is wide enough for the
#: longest realistic binding notation (``"Shift+Ctrl+Alt+X"``, eleven
#: characters, with five cells of visual gap) while keeping the
#: description column close to the keys — a help list that requires
#: scanning across a wide gap is harder to read than one that does not.
_KEYS_COLUMN_WIDTH: Final[int] = 16


# --------------------------------------------------------------------------- #
# Catalog model                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BindingInfo:
    """One row of the help catalog.

    Plain data — no state, no behaviour. The screen renders these rows
    directly; a test can inspect the catalog without instantiating the
    screen.

    Attributes:
        keys: The key notation, exactly as the user must type it
            (``"Esc / q"``, ``"Ctrl+Q"``, ``"↑ / ↓"``). Not localised —
            symbols are locale-independent. Must be a non-empty
            :class:`str`; a tuple of keys would render as
            ``"('Esc', 'q')"`` (an implementation artefact, not user
            copy), so the field is validated at construction time.
        description_key: The translation key for the action
            description. Rendered via
            :meth:`~nexusdl.core.i18n.Translator.translate`.
    """

    keys: str
    description_key: str

    def __post_init__(self) -> None:
        """Validate the field types.

        Raises:
            TypeError: If ``keys`` or ``description_key`` is not a
                :class:`str`. A tuple of keys, or a ``None``
                description, would break the renderer at best or
                produce misleading user copy at worst; failing at
                construction time keeps the catalog's shape honest.
            ValueError: If ``keys`` is empty — an empty key column
                row conveys no information.
        """
        if not isinstance(self.keys, str):
            raise TypeError(
                f"BindingInfo.keys must be a str, got "
                f"{type(self.keys).__name__}; a tuple of keys renders "
                f"as an implementation artefact, not user copy"
            )
        if not self.keys:
            raise ValueError("BindingInfo.keys must be non-empty")
        if not isinstance(self.description_key, str):
            raise TypeError(
                f"BindingInfo.description_key must be a str, got "
                f"{type(self.description_key).__name__}"
            )


@dataclass(frozen=True, slots=True)
class BindingCategory:
    """One category of the help catalog.

    A group of bindings that share a **context** — a category is what a
    user opens the help screen to look up. The grouping is by screen or
    universal behaviour, not by key modifier.

    Attributes:
        id: The stable identifier. Used as the option's id in the
            category list, and as the lookup key in
            :data:`_CATALOG`.
        title_key: The translation key for the category name, shown both
            in the left-pane list and as the content pane's header.
        bindings: The bindings in this category, in the order they
            should appear. The ordering is deliberate — the most-used
            binding first, so a user skimming the panel sees the
            primary action at the top.
    """

    id: str
    title_key: str
    bindings: tuple[BindingInfo, ...]


#: The full catalog rendered by the screen. Ordered top-to-bottom by the
#: order a user typically needs the information: universal bindings
#: first (help a first-time user most), then navigation (needed on
#: every screen), then per-screen groups.
_CATALOG: Final[tuple[BindingCategory, ...]] = (
    BindingCategory(
        id="global",
        title_key="help.category.global",
        bindings=(
            BindingInfo("Esc / q", "help.binding.back"),
            BindingInfo("Ctrl+Q", "help.binding.quit"),
            BindingInfo("?", "help.binding.help"),
        ),
    ),
    BindingCategory(
        id="navigation",
        title_key="help.category.navigation",
        bindings=(
            BindingInfo("↑ / ↓", "help.binding.move"),
            BindingInfo("Tab", "help.binding.next_widget"),
            BindingInfo("Shift+Tab", "help.binding.prev_widget"),
            BindingInfo("Enter", "help.binding.activate"),
            BindingInfo("Space", "help.binding.toggle"),
        ),
    ),
    BindingCategory(
        id="search",
        title_key="help.category.search",
        bindings=(
            BindingInfo("Enter", "help.binding.submit_query"),
            BindingInfo("Esc / q", "help.binding.back"),
        ),
    ),
    BindingCategory(
        id="download",
        title_key="help.category.download",
        bindings=(
            BindingInfo("Space", "help.binding.mark"),
            BindingInfo("A", "help.binding.mark_all"),
            BindingInfo("Ctrl+D", "help.binding.clear_marks"),
            BindingInfo("Enter", "help.binding.open_chapter"),
            BindingInfo("Esc / q", "help.binding.back"),
        ),
    ),
    BindingCategory(
        id="settings",
        title_key="help.category.settings",
        bindings=(
            BindingInfo("↑ / ↓", "help.binding.section_nav"),
            BindingInfo("Esc / q", "help.binding.back"),
        ),
    ),
    BindingCategory(
        id="logs",
        title_key="help.category.logs",
        bindings=(
            BindingInfo("↑ / ↓", "help.binding.scroll"),
            BindingInfo("Ctrl+L", "help.binding.clear_log"),
        ),
    ),
)


#: Lookup table for :func:`_category_by_id`. Built once at module load
#: so the handler's lookup is O(1) instead of O(n) — n is six today, so
#: the difference is immeasurable, but the dict makes the intent ("this
#: is a lookup, not a scan") obvious and would scale if the catalog grew.
_CATALOG_BY_ID: Final[dict[str, BindingCategory]] = {
    category.id: category for category in _CATALOG
}


# --------------------------------------------------------------------------- #
# HelpScreen                                                                  #
# --------------------------------------------------------------------------- #


class HelpScreen(Screen[None]):
    """A Textual screen listing the TUI's keyboard shortcuts.

    See the module docstring for the two-pane layout, the binding
    catalog, and the "not a message source" contract.

    Args:
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        name: Optional Textual screen name.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: Dismissable from any state. The screen is never the last one on
    #: the stack — it is pushed by whichever screen the user invoked
    #: help from — so the action is always valid.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
    ]

    #: Default CSS.
    DEFAULT_CSS: ClassVar[str] = f"""
    HelpScreen {{
        background: $surface;
    }}
    #{_ID_TITLE} {{
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        text-style: bold;
    }}
    #{_ID_CATEGORIES} {{
        width: 24;
        height: 1fr;
        border-right: solid $primary;
        background: $panel;
    }}
    #{_ID_CONTENT} {{
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }}
    #{_ID_HINT} {{
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }}
    """

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        *,
        translator: Translator | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._translator = (
            translator if translator is not None else get_default_translator()
        )

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the title, the two panes, and the hint."""
        yield Label(
            self._translator.translate("help.title"),
            id=_ID_TITLE,
        )
        with Horizontal():
            yield OptionList(
                *[
                    Option(
                        self._translator.translate(category.title_key),
                        id=category.id,
                    )
                    for category in _CATALOG
                ],
                id=_ID_CATEGORIES,
            )
            yield VerticalScroll(
                Static("", id=_ID_CONTENT),
            )
        yield Label(
            self._translator.translate("help.hint"),
            id=_ID_HINT,
        )

    def on_mount(self) -> None:
        """Highlight the first category and render its content."""
        categories = self.query_one(f"#{_ID_CATEGORIES}", OptionList)
        categories.highlighted = 0
        self._render_into_content(_CATALOG[0])
        categories.focus()

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        """Render the category whose row the user highlighted.

        Fires on arrow-key navigation and on programmatic
        ``highlighted`` assignment — the content pane updates as the
        highlight moves, matching the settings screen's behaviour.
        """
        if event.option.id is None:
            return
        category = _CATALOG_BY_ID.get(event.option.id)
        if category is not None:
            self._render_into_content(category)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Re-render on explicit selection (Enter / click).

        Redundant with the highlight handler in most cases, but
        guarantees the content is correct after a click that did not
        move the highlight (a click on the already-highlighted row does
        not emit a new highlight event in some Textual versions).
        """
        if event.option.id is None:
            return
        category = _CATALOG_BY_ID.get(event.option.id)
        if category is not None:
            self._render_into_content(category)

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def action_back(self) -> None:
        """Pop this screen off the stack, returning to the previous one."""
        self.app.pop_screen()

    # ------------------------------------------------------------------ #
    # Rendering                                                          #
    # ------------------------------------------------------------------ #

    def _render_into_content(self, category: BindingCategory) -> None:
        """Render *category* and set it on the content widget."""
        content = self.query_one(f"#{_ID_CONTENT}", Static)
        content.update(self._category_text(category))

    def _category_text(self, category: BindingCategory) -> Text:
        """Build the styled content for *category*.

        The layout is a two-column list: keys left-aligned in a fixed
        width column, description following. Every value is appended
        literally — no markup is interpreted, so an unexpected character
        in a future binding description cannot break the layout.
        """
        text = Text()

        text.append(
            self._translator.translate(category.title_key),
            style="bold underline",
        )
        text.append("\n\n")

        for binding in category.bindings:
            text.append("  ", style="")
            text.append(
                binding.keys.ljust(_KEYS_COLUMN_WIDTH),
                style="bold cyan",
            )
            text.append("  ", style="")
            text.append(
                self._translator.translate(binding.description_key),
                style="",
            )
            text.append("\n")

        return text
