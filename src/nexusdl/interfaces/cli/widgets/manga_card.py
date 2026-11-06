"""Textual widget for displaying a single manga as a card.

A :class:`MangaCard` renders one :class:`~nexusdl.core.models.manga.Manga`
in a compact, focused-card layout. It is the visual unit a search
results screen, a library listing, or a "recently read" panel composes
one-per-item to build its page.

The widget is **display-plus-activation**: it renders the manga it was
constructed with, and it posts a :class:`MangaCard.Selected` message
when the user activates it — either by pressing ``Enter`` while it is
focused, or by clicking on it. It does not navigate, does not fetch
data, and does not react to events from the rest of the pipeline.
Navigation belongs to the parent screen; the card's job stops at "the
user picked this manga".

What the card shows
-------------------
Five lines, top to bottom, several of which are conditional on the
manga's data:

1. **Title** — always. Appended with a ``18+`` badge (bold red) when
   the manga's :attr:`~Manga.content_rating` is
   :attr:`~nexusdl.core.models.enums.ContentRating.EROTICA` or
   :attr:`~ContentRating.PORNOGRAPHIC`.
2. **Meta** — always. Status (colored by state: ongoing / completed /
   hiatus / cancelled / unknown), language code, chapter count, and
   year when present. Separated by ``·``.
3. **Author** — only when the manga has one.
4. **Description** — always. The manga's synopsis, truncated at a word
   boundary to a fixed character budget. When the manga has none, the
   line renders a dim italic placeholder.
5. **Tags** — only when the manga has any. Joined with ``·``; only the
   first few are shown, with a ``+N`` indicator for the remainder.

The card is therefore **taller for richer mangas and shorter for
sparser ones** — not all cards are the same height. That is deliberate:
a card that reserves space for data the manga does not have wastes the
user's vertical real estate, and the fixed-border layout keeps the
cards visually coherent regardless of height.

Activation
----------
Two activation paths, one message:

* **Keyboard** — when the card has focus (it is focusable via
  ``can_focus = True`` and the parent screen's focus navigation), the
  ``Enter`` binding fires :meth:`MangaCard.action_select`, which posts
  :class:`MangaCard.Selected`. The ``Space`` binding is **not** on the
  card: ``Space`` is reserved for page-down scrolling in the
  scrollable card list, and binding it to activation would surprise a
  user who is used to ``less``-style scroll keys. A parent screen that
  wants ``Space`` for activation can add it via its own ``BINDINGS``.
* **Mouse** — a click fires ``on_click(event)``, which stops the
  event from bubbling to the parent and delegates to the same action.
  A click **also** focuses the card (Textual's default behaviour for
  focusable widgets) before the message is posted, so a subsequent
  keyboard activation acts on the just-clicked card.

The parent screen handles the message with::

    def on_manga_card_selected(
        self, event: MangaCard.Selected
    ) -> None:
        manga = event.manga
        ...

The long handler name (``on_manga_card_selected``) is the correct
outcome of Textual's nested-message convention — it disambiguates the
message from any other widget's ``Selected`` in the same app.

No markup injection
-------------------
Every piece of text the card displays is built with
:meth:`textual.content.Text.append`. A manga title containing ``[`` or
``]`` (which is entirely legitimate — a manga can be named ``"[[Live]]"``)
renders literally instead of being interpreted as a Textual markup tag.
This matches the policy of the sibling :mod:`site_selector` and
:mod:`progress_bar` widgets.

No image rendering
------------------
The card does not render the manga's cover. Terminals vary widely in
their image support (iTerm2, Kitty, and modern tmux support sixel /
Kitty graphics; most do not), and a card that only renders its cover on
*some* terminals would look inconsistent across the deployment. A
future enhancement that adds an opt-in image region is out of scope for
this file.

Localisation
------------
The card introduces nine translation keys under ``manga.card.*``:

* ``manga.card.status.ongoing`` / ``.completed`` / ``.hiatus`` /
  ``.cancelled`` / ``.unknown`` — the display names of the five
  :class:`~nexusdl.core.models.enums.MangaStatus` values.
* ``manga.card.by_author`` — the ``"by"`` prefix on the author line.
* ``manga.card.no_description`` — the placeholder shown when the
  manga has no synopsis.
* ``manga.card.chapters_one`` / ``.chapters_other`` — plural forms for
  the chapter count, resolved via
  :meth:`~nexusdl.core.i18n.Translator.plural`.

The language code on the meta line (``"en"``, ``"fr"``, ``"ja"``) is
rendered as its ISO 639-1 value rather than a translated name: the
codes are universal, short, and unambiguous, while a translated name
("Anglais") would overflow the column on most terminals. A future
release that adds a `manga.card.language.*` key group for a
settings-driven "verbose language" mode would extend the meta-line
builder, not this docstring's contract.

Like every other widget in this package, a missing key renders the key
verbatim, which makes the missing translation visible in the UI and
greppable in the catalogs. The contract is documented by
:mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.i18n           ─┐
    nexusdl.core.models.enums    ├──► interfaces.cli.widgets.manga_card
    nexusdl.core.models.manga    │
    nexusdl.core.utils.text      │
    textual                      │

No private attribute of any core module is touched. The card could be
lifted into a standalone widget library that imports ``nexusdl.core``
through its public sub-package surface only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Text
from textual.message import Message
from textual.widgets import Static

from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.models.enums import ContentRating, MangaStatus
from nexusdl.core.models.manga import Manga
from nexusdl.core.utils.text import truncate_words

if TYPE_CHECKING:
    from collections.abc import Iterable


__all__ = ["MangaCard"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Maximum length of the description line, in characters. Long enough
#: to convey the mangaka's premise in one or two wrapped lines at a
#: typical 80-column terminal, short enough that a card never dominates
#: the screen. Word-boundary truncation is applied via
#: :func:`~nexusdl.core.utils.text.truncate_words`.
_DESCRIPTION_MAX_CHARS: Final[int] = 160

#: Maximum number of tags shown on the tags line. Beyond this the card
#: shows a ``+N`` indicator — a card with twenty tags is a card whose
#: tags are not being read.
_MAX_TAGS_VISIBLE: Final[int] = 6

#: Style for the ``18+`` badge.
_ADULT_BADGE_STYLE: Final[str] = "bold red"

#: The ratings that earn the ``18+`` badge. Ratings not in this set —
#: :attr:`ContentRating.SAFE` and :attr:`ContentRating.SUGGESTIVE` —
#: render without the badge. The set is deliberately narrower than
#: ``ContentRating != SAFE``: a suggestive manga does not need the
#: badge a suggestive-content user already knows they are looking at.
_ADULT_RATINGS: Final[frozenset[ContentRating]] = frozenset(
    {ContentRating.EROTICA, ContentRating.PORNOGRAPHIC}
)

#: Rich-text style used for each status label. Chosen so that a scan of
#: a card list immediately distinguishes ongoing from completed from
#: cancelled, without requiring the user to read the label.
_STATUS_STYLE: Final[dict[MangaStatus, str]] = {
    MangaStatus.ONGOING: "cyan",
    MangaStatus.COMPLETED: "green",
    MangaStatus.HIATUS: "yellow",
    MangaStatus.CANCELLED: "red",
    MangaStatus.UNKNOWN: "dim",
}

#: Translation key for each status label. Kept separate from
#: ``_STATUS_STYLE`` so that a future translation update is a
#: single-file edit.
_STATUS_I18N: Final[dict[MangaStatus, str]] = {
    MangaStatus.ONGOING: "manga.card.status.ongoing",
    MangaStatus.COMPLETED: "manga.card.status.completed",
    MangaStatus.HIATUS: "manga.card.status.hiatus",
    MangaStatus.CANCELLED: "manga.card.status.cancelled",
    MangaStatus.UNKNOWN: "manga.card.status.unknown",
}

#: Separator between meta segments. Non-breaking-adjacent in practice —
#: a middle dot with thin spaces on each side is the standard "list of
#: distinct short values" separator in every TUI the pipeline targets.
_SEPARATOR: Final[str] = "  ·  "


# --------------------------------------------------------------------------- #
# MangaCard                                                                   #
# --------------------------------------------------------------------------- #


class MangaCard(Vertical):
    """A Textual widget displaying one manga as a focusable card.

    See the module docstring for the design contract, the field
    coverage, and the activation paths.

    Args:
        manga: The manga to display. The card holds a reference for its
            own lifetime; it does not re-read the manga from anywhere.
            A parent that wants to reflect an updated manga constructs
            a new :class:`MangaCard`.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        id: Optional Textual widget id. Typically the parent screen
            assigns one when it wants to query the card later.
        classes: Optional Textual CSS classes. Same purpose as *id*.
    """

    #: The card is focusable — the parent screen's focus navigation
    #: (arrow keys, Tab) moves between cards, and the focused card is
    #: the one the ``Enter`` binding activates.
    can_focus = True

    #: Default CSS for the card. Textual applies it automatically; the
    #: application's ``theme.tcss`` may override any rule by targeting
    #: ``MangaCard`` or the ``.manga-card-*`` classes.
    DEFAULT_CSS: ClassVar[str] = """
    MangaCard {
        height: auto;
        border: round $primary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    MangaCard:focus {
        border: round $accent;
        background: $boost;
    }
    .manga-card-title {
        height: auto;
        text-style: bold;
    }
    .manga-card-meta {
        height: auto;
    }
    .manga-card-author {
        height: auto;
        text-style: italic;
    }
    .manga-card-description {
        height: auto;
    }
    .manga-card-tags {
        height: auto;
        color: $text-muted;
    }
    """

    #: Keyboard bindings. Only ``Enter`` — ``Space`` is deliberately
    #: absent because the parent screens mount cards inside a
    #: ``VerticalScroll``, where ``Space`` is conventionally a
    #: page-down key in every TUI the pipeline targets. A user who
    #: presses ``Space`` expecting to scroll would activate the focused
    #: card instead — a worse surprise than the loss of one keystroke
    #: shortcut. A parent that wants ``Space`` for activation can add
    #: the binding on its own screen's ``BINDINGS`` list, where the
    #: scroll-vs-activate trade-off is a deliberate choice at the call
    #: site rather than a hidden widget default.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "select", "Select", show=False),
    ]

    # ------------------------------------------------------------------ #
    # Message                                                            #
    # ------------------------------------------------------------------ #

    class Selected(Message):
        """Emitted when the user activates the card.

        Carries the full :class:`~nexusdl.core.models.manga.Manga` the
        card was constructed with — not just its id — so the parent
        screen can act on it (open the chapter list, enqueue a
        download, display the manga's metadata) without a second
        lookup. The manga model is immutable, so the message holds a
        safe snapshot.
        """

        def __init__(self, manga: Manga) -> None:
            """Store the selected manga on the message.

            Args:
                manga: The manga this card displays.
            """
            self.manga = manga
            super().__init__()

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        manga: Manga,
        *,
        translator: Translator | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._manga = manga
        self._translator = (
            translator if translator is not None else get_default_translator()
        )

    @property
    def manga(self) -> Manga:
        """Return the manga this card displays.

        A read-only accessor so a parent screen (or a test) can inspect
        the card's data without reaching into a private attribute.
        """
        return self._manga

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the card's lines.

        The author line and the tags line are yielded **only when the
        manga has data for them**, so a sparse card collapses to three
        lines instead of five and does not waste vertical real estate
        on empty placeholders.
        """
        yield Static(
            self._title_line(), classes="manga-card-title"
        )
        yield Static(self._meta_line(), classes="manga-card-meta")
        if self._manga.author:
            yield Static(
                self._author_line(), classes="manga-card-author"
            )
        yield Static(
            self._description_line(),
            classes="manga-card-description",
        )
        if self._manga.tags:
            yield Static(self._tags_line(), classes="manga-card-tags")

    # ------------------------------------------------------------------ #
    # Activation                                                         #
    # ------------------------------------------------------------------ #

    def action_select(self) -> None:
        """Post a :class:`MangaCard.Selected` message for this card's manga.

        Bound to ``Enter``; called by :meth:`on_click` for mouse
        activation. The single entry point for every activation path
        keeps the "one card = one message" invariant trivially
        auditable.
        """
        self.post_message(self.Selected(self._manga))

    def on_click(self, event: events.Click) -> None:
        """Activate the card on a mouse click.

        Delegates to :meth:`action_select` so the mouse and keyboard
        paths are the same. Textual's default focus handling has
        already focused the card before this handler runs, so a
        click-then-Enter flow acts on the clicked card.

        The ``event`` parameter is **required**: Textual 2.x and later
        pass the ``Click`` event to the handler, and a handler
        declared without it raises ``TypeError`` at dispatch time —
        the click would be silently unhandled. The event is stopped
        from bubbling so the parent screen's own click handler (if
        any) does not double-fire on the same gesture.
        """
        event.stop()
        self.action_select()

    # ------------------------------------------------------------------ #
    # Line builders                                                      #
    # ------------------------------------------------------------------ #

    def _title_line(self) -> Text:
        """Return the title line, with the adult badge when applicable.

        The title is rendered literally — no markup is interpreted, so
        a manga titled ``"[[Live]]"`` displays as-is. The badge is
        appended after a small gap, styled bold red.
        """
        text = Text()
        text.append(self._manga.title, style="bold")
        if self._manga.content_rating in _ADULT_RATINGS:
            text.append("  ", style="")
            text.append("18+", style=_ADULT_BADGE_STYLE)
        return text

    def _meta_line(self) -> Text:
        """Return the meta line: status, language, chapter count, year.

        Every segment after the status is optional (chapter count only
        appears for a manga with chapters, year only when known), so a
        very sparse manga's meta line is just its status. The status
        is the only mandatory segment; it is what tells the user, at
        a glance, whether the manga is worth starting.
        """
        text = Text()

        status = self._manga.status
        text.append(
            self._translator.translate(_STATUS_I18N[status]),
            style=_STATUS_STYLE[status],
        )

        text.append(_SEPARATOR, style="dim")
        text.append(self._manga.language.value, style="dim")

        if self._manga.chapter_count > 0:
            text.append(_SEPARATOR, style="dim")
            text.append(
                self._translator.plural(
                    "manga.card.chapters", self._manga.chapter_count
                ),
                style="dim",
            )

        if self._manga.year is not None:
            text.append(_SEPARATOR, style="dim")
            text.append(str(self._manga.year), style="dim")

        return text

    def _author_line(self) -> Text:
        """Return the author line, with the localised "by" prefix.

        Only called when :attr:`Manga.author` is set — see
        :meth:`compose`. The method still falls back to an empty
        string if ``author`` turns out to be ``None`` (a defensive
        guard for a future model change that makes ``author``
        nullable in a way the caller's narrowing does not cover);
        :meth:`Text.append` requires a ``str``, and passing ``None``
        would raise.
        """
        text = Text()
        text.append(
            self._translator.translate("manga.card.by_author"),
            style="italic",
        )
        text.append(" ", style="")
        text.append(self._manga.author or "", style="")
        return text

    def _description_line(self) -> Text:
        """Return the description line.

        When the manga has a synopsis, it is truncated at a word
        boundary and rendered verbatim. When it does not, a dim italic
        placeholder is shown so the card structure remains visible
        even for sparse data.
        """
        if not self._manga.description:
            text = Text()
            text.append(
                self._translator.translate(
                    "manga.card.no_description"
                ),
                style="dim italic",
            )
            return text
        return Text(
            truncate_words(
                self._manga.description, _DESCRIPTION_MAX_CHARS
            )
        )

    def _tags_line(self) -> Text:
        """Return the tags line, joining the first few tags with ``·``.

        Only called when :attr:`Manga.tags` is non-empty — see
        :meth:`compose`. When the manga has more tags than
        :data:`_MAX_TAGS_VISIBLE`, a ``+N`` indicator is appended so
        the user knows the card is not showing every tag.
        """
        return _join_tags(
            self._manga.tags,
            max_visible=_MAX_TAGS_VISIBLE,
            separator=_SEPARATOR.strip(),
        )


# --------------------------------------------------------------------------- #
# Module-level helpers                                                        #
# --------------------------------------------------------------------------- #


def _join_tags(
    tags: Iterable[str],
    *,
    max_visible: int,
    separator: str,
) -> Text:
    """Join up to *max_visible* tags with *separator*, plus a ``+N`` indicator.

    Args:
        tags: The tags to render. Assumed non-empty; callers with an
            empty sequence short-circuit at :meth:`MangaCard.compose`.
        max_visible: The maximum number of tags to show inline. Must
            be ``>= 1``; the ``+N`` indicator is only appended when
            there are more tags than this.
        separator: The separator to insert between tags. Passed by the
            caller rather than looked up here so the module keeps a
            single definition of the card's ``·`` separator.

    Returns:
        A :class:`~textual.content.Text` object ready to be yielded by
        a :class:`Static`.
    """
    materialized = list(tags)
    visible = materialized[:max_visible]
    hidden_count = len(materialized) - len(visible)

    text = Text()
    for index, tag in enumerate(visible):
        if index > 0:
            text.append(separator, style="dim")
        text.append(tag, style="")
    if hidden_count > 0:
        text.append(separator, style="dim")
        text.append(f"+{hidden_count}", style="dim")
    return text
