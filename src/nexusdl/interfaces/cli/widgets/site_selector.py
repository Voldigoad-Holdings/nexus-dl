"""Textual widget for picking a source site from the registry.

The site selector is the entry point of every TUI flow that talks to a
source site: the search screen uses it to pick which site to search, the
download screen uses it to pick which site to pull a chapter list from,
the settings screen uses it to inspect a site's capabilities. The widget
owns three responsibilities and only three:

* **Present** the sites the composition root has already loaded into the
  :class:`~nexusdl.core.registry.site_registry.SiteRegistry`, with a
  rich label that surfaces the information a user needs to pick one
  (name, language, adult flag, JS requirement, disabled state).
* **Emit** a :class:`SiteSelector.SiteSelected` message carrying the
  full :class:`~nexusdl.core.models.site.SiteConfig` when the user
  picks a site — not just the id, so the parent screen does not have to
  look the site up again.
* **Manage** the filter state (language, adult content, disabled sites)
  via actions the parent screen can trigger.

Everything else is deliberately out of scope. The widget does **not**
call :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.reload` —
reloading is the composition root's responsibility, and the widget's
job is to display whatever the registry currently holds. It does not
perform network I/O, does not write to disk, and does not manage any
state that outlives the widget's lifecycle. A caller that wants a
filter preset constructs the widget with the right kwargs; a caller
that wants a filter control installs its own controls and calls
:meth:`SiteSelector.set_language` / :meth:`SiteSelector.toggle_adult`.

Assumption on the registry
--------------------------
The widget assumes :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.initialize`
has already been awaited — that is what the composition root does at
startup, before any screen is pushed. :meth:`SiteRegistry.list_sites`
raises :class:`~nexusdl.core.exceptions.ConfigError` if the registry is
not initialized; the widget does **not** catch that exception, because
a screen appearing before the registry is ready is a composition-root
bug that deserves a loud failure, not a hidden empty list.

Message handler name
--------------------
:class:`SiteSelector.SiteSelected` is a nested message, matching the
Textual convention used by every built-in widget. The parent screen
handles it with::

    def on_site_selector_site_selected(
        self, event: SiteSelector.SiteSelected
    ) -> None:
        site = event.site
        ...

The long handler name (`on_site_selector_site_selected`) is the correct
outcome of Textual's nested-message convention — it disambiguates the
message from any other widget's `SiteSelected` in the same app.

No markup injection
-------------------
The label for each site is built with :meth:`textual.content.Text.append`,
not :meth:`textual.content.Text.from_markup`. A site's ``name`` comes
from ``sites.yaml`` (validated by :mod:`nexusdl.core.registry.validator`
but still free-text) and could legitimately contain ``[`` or ``]`` — a
markup-based label would interpret those as Textual markup tags and
render garbage, or worse, hide part of the name. Using ``.append()``
treats every character literally.

Reactive state
--------------
The filter values are exposed as read-only properties rather than
Textual ``reactive`` attributes with watchers. A watcher would trigger
``_refresh`` on every assignment, including the ones made during
``__init__`` before the widget is composed — an order-of-operations bug
that only shows up when the constructor's defaults differ from the
shipped defaults, which is exactly the case a test exercises. Explicit
methods (:meth:`set_language`, :meth:`toggle_adult`) make the
refreshes visible at the call site, and the "no watchers, no cascades"
rule is what keeps the widget predictable in a screen that may run
several reconfigurations in a row.

Dependency graph (no cycles)::

    nexusdl.core.i18n                        ─┐
    nexusdl.core.models.enums                 │
    nexusdl.core.models.site                  ├──► interfaces.cli.widgets.site_selector
    nexusdl.core.registry.site_registry       │
    textual                                   │
                                              │

The widget imports from :mod:`nexusdl.core` only through that
package's public surface. It does not touch any private attribute of a
core module, and it does not depend on any other widget or screen in
:mod:`nexusdl.interfaces` — a caller that wants to use it in isolation
(a widget test, a minimal TUI) can do so without pulling in the rest of
the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Text
from textual.message import Message
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.models.site import SiteConfig
from nexusdl.core.registry.site_registry import SiteRegistry

if TYPE_CHECKING:
    from nexusdl.core.models.enums import Language


__all__ = ["SiteSelector"]


#: Textual IDs and CSS classes used by the widget. Kept as private
#: constants so a future CSS refactor has a single place to update the
#: selectors, and so the ``DEFAULT_CSS`` string below cannot drift from
#: the ``query_one`` calls in the code.
_ID_ROOT: Final[str] = "site-selector"
_ID_HEADER: Final[str] = "site-selector-header"
_ID_TITLE: Final[str] = "site-selector-title"
_ID_STATUS: Final[str] = "site-selector-status"
_ID_LIST: Final[str] = "site-selector-list"
_ID_EMPTY: Final[str] = "site-selector-empty"
_CLASS_HIDDEN: Final[str] = "hidden"


class SiteSelector(Vertical):
    """A Textual widget for selecting a source site from the registry.

    See the module docstring for the design contract, the empty-state
    behaviour, and the handler name a parent screen must implement to
    react to a selection.

    The widget is keyboard-first: ``↑`` / ``↓`` navigate the list,
    ``Enter`` selects, ``Ctrl+R`` refreshes the list from the registry,
    ``Ctrl+T`` toggles the adult-content filter. The bindings are
    declared at class level so a parent screen can override them via
    Textual's standard ``BINDINGS``-merge mechanism.

    Args:
        registry: The loaded site registry. The composition root
            constructs it and awaits
            :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.initialize`
            before composing any screen.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        include_adult: Initial state of the adult-content filter.
            ``False`` (the default) hides sites flagged
            ``SiteConfig.adult == True``.
        include_disabled: Initial state of the disabled-sites filter.
            ``False`` (the default) hides sites the registry reports as
            disabled — see
            :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.is_disabled`.
            A site hidden by this filter is not offered for selection.
        language: Initial language filter. ``None`` (the default)
            disables the language filter and shows every language.
        id: Optional Textual widget id. Typically the parent screen
            assigns one so it can query the widget later.
        classes: Optional Textual CSS classes. Same purpose as *id*.
    """

    #: Default CSS for the widget, applied automatically by Textual. The
    #: application's ``theme.tcss`` may override any of these rules by
    #: targeting ``SiteSelector`` or the ``#site-selector-*`` ids.
    DEFAULT_CSS: ClassVar[str] = f"""
    SiteSelector {{
        height: 1fr;
        border: solid $primary;
        padding: 0;
    }}
    #{_ID_HEADER} {{
        height: 1;
        padding: 0 1;
        background: $surface;
    }}
    #{_ID_TITLE} {{
        width: auto;
        text-style: bold;
    }}
    #{_ID_STATUS} {{
        width: 1fr;
        text-align: right;
        color: $text-muted;
    }}
    #{_ID_LIST} {{
        height: 1fr;
    }}
    #{_ID_EMPTY} {{
        height: 1fr;
        padding: 2;
        content-align: center middle;
        color: $text-muted;
        text-align: center;
    }}
    .{_CLASS_HIDDEN} {{
        display: none;
    }}
    """

    #: Keyboard bindings. ``Ctrl+R`` and ``Ctrl+T`` are safe additions to
    #: the app's keymap — neither conflicts with Textual's built-in
    #: focus, help, or quit bindings. A parent screen that wants a
    #: different binding overrides via its own ``BINDINGS`` list.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+r", "refresh", "Refresh sites", show=True),
        Binding("ctrl+t", "toggle_adult", "Toggle adult filter", show=False),
    ]

    # ------------------------------------------------------------------ #
    # Message                                                            #
    # ------------------------------------------------------------------ #

    class SiteSelected(Message):
        """Emitted when the user picks a site from the list.

        The message carries the **full** :class:`SiteConfig` — not just
        its id — so the parent screen can act on it (read the site's
        language, capabilities, or adult flag) without a second lookup
        through the registry. The registry may be reloaded between the
        message being posted and the handler running; carrying the
        config snapshot at selection time is the only way to keep the
        handler's view coherent with what the user saw.
        """

        def __init__(self, site: SiteConfig) -> None:
            """Store the selected site on the message.

            Args:
                site: The site the user selected. The config is
                    immutable, so the message holds a safe snapshot.
            """
            self.site = site
            super().__init__()

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        registry: SiteRegistry,
        *,
        translator: Translator | None = None,
        include_adult: bool = False,
        include_disabled: bool = False,
        language: Language | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._registry = registry
        self._translator = (
            translator if translator is not None else get_default_translator()
        )
        self._include_adult = include_adult
        self._include_disabled = include_disabled
        self._language = language
        #: Option id → :class:`SiteConfig`. Repopulated on every
        #: :meth:`_refresh`; the option-selected handler uses it to
        #: resolve the widget-independent site the user picked.
        self._sites_by_id: dict[str, SiteConfig] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the widget's children.

        The widget is a two-row vertical container: a header with the
        title and a live count, and a scrollable option list (or an
        empty-state placeholder when the list is empty). The header
        stays visible when the list is empty so the user understands
        *why* there are no options — a status line above the empty
        placeholder is more informative than an unqualified
        "No items".
        """
        with Horizontal(id=_ID_HEADER):
            yield Label(
                self._translator.translate("site.selector.title"),
                id=_ID_TITLE,
            )
            yield Label("", id=_ID_STATUS)
        yield OptionList(id=_ID_LIST)
        yield Static("", id=_ID_EMPTY, classes=_CLASS_HIDDEN)

    def on_mount(self) -> None:
        """Populate the option list and focus it.

        Runs once, after Textual has mounted every child yielded by
        :meth:`compose`. Focusing the list immediately means the user
        can start navigating with the arrow keys without first tabbing
        into the widget — the standard behaviour of a Textual
        selector list.
        """
        self._refresh()
        self.query_one(f"#{_ID_LIST}", OptionList).focus()

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Resolve the selected option to a :class:`SiteConfig` and post it.

        Textual's ``OptionSelected`` message carries the ``Option``
        object; the widget maps the option's ``id`` back to the site it
        was built from and posts the widget-level
        :class:`SiteSelected` message with the full config. Ignoring a
        selection whose id is no longer in the map (the registry was
        reloaded between the option being built and the user pressing
        Enter) is the correct defensive response — the user selected a
        site that no longer exists, and the next :meth:`action_refresh`
        will reconcile the list.
        """
        option = event.option
        if option.id is None:
            return
        site = self._sites_by_id.get(option.id)
        if site is None:
            return
        self.post_message(self.SiteSelected(site))

    # ------------------------------------------------------------------ #
    # Actions — wired to BINDINGS                                        #
    # ------------------------------------------------------------------ #

    def action_refresh(self) -> None:
        """Reload the option list from the registry.

        Bound to ``Ctrl+R``. A caller that reloaded the registry
        (``await registry.reload()``) uses this to bring the widget's
        view back in sync; the widget itself never triggers a reload.
        """
        self._refresh()

    def action_toggle_adult(self) -> None:
        """Flip the adult-content filter and reload the list.

        Bound to ``Ctrl+T``. The filter flips from its current state to
        the opposite — a toggle, not a set. A caller that wants to
        force a specific state calls :meth:`set_include_adult` directly.
        """
        self._include_adult = not self._include_adult
        self._refresh()

    # ------------------------------------------------------------------ #
    # Public API — for parent screens that own their own controls        #
    # ------------------------------------------------------------------ #

    def set_language(self, language: Language | None) -> None:
        """Replace the language filter and reload the list.

        A parent screen that owns a language dropdown calls this on
        every change. ``None`` clears the filter and shows every
        language.

        Args:
            language: The new language filter, or ``None`` for no
                filter.
        """
        if self._language == language:
            return
        self._language = language
        self._refresh()

    def set_include_adult(self, include_adult: bool) -> None:
        """Set the adult-content filter and reload the list.

        Unlike :meth:`action_toggle_adult`, this method sets the state
        to a specific value. A parent screen bound to a checkbox uses
        this on every change.

        Args:
            include_adult: The new filter state.
        """
        if self._include_adult == include_adult:
            return
        self._include_adult = include_adult
        self._refresh()

    def set_include_disabled(self, include_disabled: bool) -> None:
        """Set the disabled-sites filter and reload the list.

        Args:
            include_disabled: The new filter state.
        """
        if self._include_disabled == include_disabled:
            return
        self._include_disabled = include_disabled
        self._refresh()

    @property
    def selected_site(self) -> SiteConfig | None:
        """Return the currently highlighted site, or ``None``.

        A convenience for a parent screen that wants to read "what the
        user would select right now" without waiting for them to press
        Enter — a preview panel that shows details for the highlighted
        site, for example. Returns ``None`` when the option list is
        empty or the highlighted option no longer maps to a live site.
        """
        list_widget = self.query_one(f"#{_ID_LIST}", OptionList)
        index = list_widget.highlighted
        if index is None:
            return None
        option = list_widget.get_option_at_index(index)
        if option.id is None:
            return None
        return self._sites_by_id.get(option.id)

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _refresh(self) -> None:
        """Repopulate the option list from the registry's current state.

        Queries the registry, then displays either the list of sites or
        the empty-state placeholder. If the list of sites is empty
        (because the registry contains none at all, or because the
        current filters excluded every site), the placeholder is shown
        with a message that distinguishes the two cases so the user
        knows whether to clear a filter or to populate the registry.
        """
        list_widget = self.query_one(f"#{_ID_LIST}", OptionList)
        empty_widget = self.query_one(f"#{_ID_EMPTY}", Static)
        status_widget = self.query_one(f"#{_ID_STATUS}", Label)

        sites = self._fetch_sites()

        # Rebuild the option-to-site map before populating the list so
        # that a fast user pressing Enter during the update sees a
        # coherent map, not a partially-rebuilt one.
        self._sites_by_id = {site.id: site for site in sites}

        if not sites:
            list_widget.clear_options()
            list_widget.add_class(_CLASS_HIDDEN)
            empty_widget.update(self._empty_message())
            empty_widget.remove_class(_CLASS_HIDDEN)
        else:
            list_widget.clear_options()
            list_widget.add_options(
                [
                    Option(self._site_label(site), id=site.id)
                    for site in sites
                ]
            )
            empty_widget.add_class(_CLASS_HIDDEN)
            list_widget.remove_class(_CLASS_HIDDEN)
            list_widget.highlighted = 0

        status_widget.update(self._status_text(sites))

    def _fetch_sites(self) -> list[SiteConfig]:
        """Return the sites the widget should display, sorted by name.

        Delegates the actual filtering to
        :meth:`SiteRegistry.list_sites`, which already implements the
        language / adult / disabled filters. Sorting is done here
        because the widget's contract is "sorted by display name" and
        the registry's ordering is an internal detail a future change
        could alter.
        """
        sites = self._registry.list_sites(
            language=self._language,
            include_adult=self._include_adult,
            include_disabled=self._include_disabled,
        )
        return sorted(sites, key=lambda s: s.name.casefold())

    def _site_label(self, site: SiteConfig) -> Text:
        """Build the rich-text label shown for one site.

        Uses :meth:`Text.append` (not ``Text.from_markup``) so that any
        ``[``, ``]``, or other markup-metacharacter in the site's name
        renders literally. The label carries four pieces of information
        in a fixed order:

        * name (bold, or dim-and-struck if disabled),
        * language tag (dim),
        * adult-content badge (bold red) when the site is flagged adult,
        * JS-required badge (dim yellow) when the site's capabilities
          declare JS rendering — the user knows the site will be slow
          to search.

        Args:
            site: The site to render.

        Returns:
            A :class:`~textual.content.Text` suitable as an
            :class:`Option` prompt.
        """
        text = Text()
        is_disabled = self._registry.is_disabled(site.id)

        if is_disabled:
            text.append("⊘ ", style="dim red")
            text.append(site.name, style="dim strike")
        else:
            text.append(site.name, style="bold")

        text.append(f"  ({site.language.value})", style="dim")

        if site.adult:
            text.append("  · 18+", style="bold red")

        if site.capabilities.requires_js_rendering:
            text.append("  · JS", style="dim yellow")

        return text

    def _status_text(self, sites: list[SiteConfig]) -> Text:
        """Build the header status line describing the current filter state.

        Shows the number of visible sites and, parenthetically, any
        active filters — so a user looking at an empty list knows
        whether the registry is empty or whether their own filter
        excluded everything.
        """
        status = Text()
        status.append(
            self._translator.plural("site.selector.count", len(sites))
        )

        filters: list[str] = []
        if self._language is not None:
            filters.append(self._language.value)
        if self._include_adult:
            filters.append("18+")
        if self._include_disabled:
            filters.append("disabled")

        if filters:
            status.append("  (", style="dim")
            status.append(", ".join(filters), style="dim")
            status.append(")", style="dim")

        return status

    def _empty_message(self) -> str:
        """Build the empty-state message.

        Distinguishes "no sites are configured at all" from "the
        current filter excluded every site" — the fix is different in
        each case, and conflating them would leave the user guessing
        which button to press.
        """
        total = len(
            self._registry.list_sites(
                include_adult=True, include_disabled=True
            )
        )
        if total == 0:
            return self._translator.translate("site.selector.empty.registry")
        return self._translator.translate("site.selector.empty.filtered")
