"""Textual screen hosting the live log viewer.

The logs screen is a thin full-screen wrapper around
:class:`~nexusdl.interfaces.cli.widgets.log_viewer.LogViewer`. It adds
three things the widget cannot provide on its own:

* **Screen-level chrome** — a title bar above the widget and a hint bar
  below it, so the user knows which screen they are on and how to leave
  it.
* **Lifecycle wiring** — the widget is seeded from a shared log router's
  recent-entry buffer on mount, and detached from that router on unmount.
  Without the wrapper, each fresh :class:`LogViewer` would start empty
  every time the screen was pushed.
* **Back navigation** — the ``Esc`` / ``q`` binding every screen in this
  package uses.

Everything else — filtering, pausing, clearing, level colouring,
per-component tags — is the widget's job. The screen does not touch the
widget's internals, does not read its ring buffer, and does not know how
it renders.

Log routing
-----------
The composition root owns a **log router** — an object that:

1. Receives every :class:`LogEntry` from the loguru sink installed in
   :func:`~nexusdl.core.logger.configure_logging`'s composite handler.
2. Keeps a bounded ring buffer of recent entries for seeding new
   viewers.
3. Forwards every new entry to every currently attached
   :class:`~nexusdl.interfaces.cli.widgets.log_viewer.LogViewer`.

The screen is the **consumer** of the router; it never constructs one.
The interface it depends on is captured by the
:class:`LogRouter` :class:`typing.Protocol` declared in this module.
The composition root implements that protocol; the screen does not know
or care how.

Atomic seed-and-attach
----------------------
A naive integration would call ``recent_entries()`` first (to seed) and
``attach()`` second (to go live). That ordering has a race: an entry
emitted between the two calls reaches the router's ring buffer (so it
is in the next ``recent_entries()`` snapshot) but never reaches the
viewer's queue (which was not yet attached). One log line, silently
lost, in the small window between two consecutive Python statements.

The protocol therefore exposes **only** :meth:`LogRouter.attach_with_seed`,
which the router implements atomically: it takes the seed snapshot, adds
the viewer to the live-forward list, and pushes the seed entries to the
viewer — all under the router's own lock. No entry is missed, none is
duplicated.

The protocol method additionally documents a **transactional guarantee**:
if it raises, the viewer is not attached — a broken implementation that
partially attaches and then raises would leave a dead viewer in the
router's forwarding list, with no detach callback in the screen to clean
it up. This is a contract on the router, not something the screen can
recover from. See the method's docstring.

Reopening the screen
--------------------
Textual creates a **new screen instance** on every ``push_screen`` call;
a screen is not reused. Every opening therefore constructs a fresh
:class:`LogViewer`, seeds it from the router's ring buffer, and attaches
it. The previous viewer was detached on pop and is now garbage —
no state leaks between openings, no duplicate log lines are shown, no
memory accumulates.

The seed limit (``_DEFAULT_SEED_LIMIT`` entries) bounds the initial
history a fresh viewer receives. A larger value surfaces more history
but costs a bigger initial push to the viewer's queue (which the widget
then renders on the next drain tick). 500 is a good balance — a typical
TUI session sees fewer than 500 entries before the user opens the
screen. The limit is clamped to :data:`_MAX_SEED_LIMIT` at construction
time so a typo (``seed_limit=1_000_000``) cannot turn the first opening
of the screen into a multi-second freeze.

Why a full screen, not a panel
------------------------------
Every other screen in this package is a **two-pane composition**
(settings, help, library, search, download). The logs screen is not,
because the widget's own layout is already complex — a header with
controls, a rich log body, per-line styling — and squeezing it into a
narrow pane would make the log lines wrap unpredictably. The logs
screen is a dedicated full-screen destination, matching the pattern of
``htop``'s log view and every TUI's dedicated "logs" tab.

Localisation
------------
The screen introduces two keys under ``logs.screen.*``:

* ``logs.screen.title`` — the header title.
* ``logs.screen.hint`` — the footer hint line, naming the two most
  useful bindings.

The widget already declares its own ``logs.viewer.*`` keys; the screen
does not redeclare them. A missing key renders the key verbatim,
matching the contract every other widget and screen in this package
follows. See :mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.i18n                                     ─┐
    nexusdl.interfaces.cli.widgets.log_viewer              ├──► interfaces.cli.screens.logs
    textual                                                │

The screen imports exactly one widget from its sibling package
(:class:`LogViewer`) and one translation utility from the core
(:class:`Translator`). Everything else is Textual.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Final, Protocol

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Label

from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.interfaces.cli.widgets.log_viewer import LogEntry, LogViewer


__all__ = ["LogRouter", "LogsScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so the CSS below and the
#: ``query_one`` calls in the code cannot drift apart.
_ID_TITLE: Final[str] = "logs-screen-title"
_ID_HINT: Final[str] = "logs-screen-hint"

#: Number of historical entries a freshly attached viewer is seeded
#: with. Bounded so the initial push to the viewer's queue stays cheap;
#: tuned so a typical TUI session sees most of its history on first
#: opening. A caller can override via ``seed_limit``.
_DEFAULT_SEED_LIMIT: Final[int] = 500

#: Hard upper bound on the seed limit, applied at construction time.
#: A caller that passes ``seed_limit=1_000_000`` (a typo, a config
#: mistake) would otherwise make the first opening of the screen push
#: a million entries to the viewer's queue — a multi-second freeze
#: that looks like a hang. Clamping at construction keeps the failure
#: mode "history beyond the cap is not seeded" rather than "the screen
#: freezes".
_MAX_SEED_LIMIT: Final[int] = 10_000


# --------------------------------------------------------------------------- #
# LogRouter protocol                                                          #
# --------------------------------------------------------------------------- #


class LogRouter(Protocol):
    """Interface the composition root implements for the logs screen.

    The screen needs exactly one method — a **combined** seed-and-attach
    operation that is atomic with respect to the log sink. See the
    module docstring's "Atomic seed-and-attach" section for why a
    separate ``recent_entries()`` + ``attach()`` sequence would be
    incorrect.

    A canonical implementation lives in the composition root::

        class RouterImpl:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self._recent: deque[LogEntry] = deque(maxlen=1000)
                self._viewers: list[LogViewer] = []

            def sink(self, entry: LogEntry) -> None:
                with self._lock:
                    self._recent.append(entry)
                    for viewer in self._viewers:
                        viewer.push_entry(entry)

            def attach_with_seed(
                self, viewer: LogViewer, *, limit: int = 500
            ) -> Callable[[], None]:
                with self._lock:
                    seed = list(self._recent)[-limit:]
                    for entry in seed:
                        viewer.push_entry(entry)
                    self._viewers.append(viewer)
                def detach() -> None:
                    with self._lock:
                        if viewer in self._viewers:
                            self._viewers.remove(viewer)
                return detach

    The composition root installs ``RouterImpl.sink`` as the loguru sink
    via :meth:`loguru.Logger.add` — see
    :func:`nexusdl.core.logger.configure_logging`'s documentation for
    the composite sink pattern.

    Not marked ``@runtime_checkable`` — no call site needs
    ``isinstance`` checks against it. A structural conformance failure
    would surface as an ``AttributeError`` on the first
    ``attach_with_seed`` call, which is the correct loud failure for a
    missing wiring. A caller that genuinely needs ``isinstance`` (for a
    plugin-verification tool, for example) can decorate the class with
    :func:`typing.runtime_checkable` locally — the class is small
    enough that the runtime check is cheap — without affecting the
    shipped code.
    """

    def attach_with_seed(
        self,
        viewer: LogViewer,
        *,
        limit: int = _DEFAULT_SEED_LIMIT,
    ) -> Callable[[], None]:
        """Seed *viewer* with recent entries and attach it for live updates.

        **Must be atomic with respect to the router's log sink.** The
        implementation guarantees that:

        * every entry that was in the router's ring buffer at the
          moment the method was called is delivered to *viewer* (via
          :meth:`LogViewer.push_entry`) before the method returns, and
        * every entry that arrives after the method returns is
          delivered to *viewer* exactly once.

        No entry may be missed or duplicated in the transition.

        **Transactional guarantee.** If this method raises, the router
        **must not** have attached the viewer. The caller sees either a
        fully-attached viewer with a valid detach callback, or an
        exception with **no attachment** — the router's forwarding
        list must not contain the viewer on the exception path. This is
        a contract on the router, not something the screen can recover
        from: on the exception path the screen has no detach callback
        to clean up with, so a partially-attached viewer would leak
        forever in the router's forwarding list.

        Args:
            viewer: The viewer to seed and attach. The router does not
                take ownership; the caller remains responsible for
                calling the returned detach callback.
            limit: Maximum number of historical entries to seed.
                Implementations that keep a bounded ring buffer of
                fewer than ``limit`` entries seed whatever is retained.

        Returns:
            A zero-argument callable that detaches *viewer* from the
            router. The callback is **idempotent** — calling it twice
            has no further effect. After the callback runs, the viewer
            stops receiving new entries but retains every entry it has
            already received.
        """
        ...


# --------------------------------------------------------------------------- #
# LogsScreen                                                                  #
# --------------------------------------------------------------------------- #


class LogsScreen(Screen[None]):
    """A Textual screen hosting a full-screen :class:`LogViewer`.

    See the module docstring for the design contract, the routing
    integration, and the atomic seed-and-attach guarantee.

    Args:
        log_router: The log router supplied by the composition root.
            See :class:`LogRouter`.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        seed_limit: Maximum number of historical entries to seed the
            viewer with on mount. Defaults to
            :data:`_DEFAULT_SEED_LIMIT`. Values above
            :data:`_MAX_SEED_LIMIT` are clamped, and negative values
            are treated as zero. The clamp is a defence against a
            typo turning the first opening of the screen into a
            multi-second freeze; the message is still honest about
            what was seeded (the clamped value, not the caller's
            request).
        name: Optional Textual screen name.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: ``Esc`` and ``q`` pop the screen. The logs screen is never the
    #: last screen on the stack — it is pushed by whichever screen the
    #: user invoked it from — so the action is always valid.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
    ]

    #: Default CSS. The widget's own ``DEFAULT_CSS`` sizes the log body
    #: to ``1fr``; the screen docks the title and hint bars around it.
    DEFAULT_CSS: ClassVar[str] = f"""
    LogsScreen {{
        background: $surface;
    }}
    #{_ID_TITLE} {{
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        text-style: bold;
    }}
    #{_ID_HINT} {{
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }}
    LogViewer {{
        height: 1fr;
    }}
    """

    def __init__(
        self,
        *,
        log_router: LogRouter,
        translator: Translator | None = None,
        seed_limit: int = _DEFAULT_SEED_LIMIT,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._log_router = log_router
        self._translator = (
            translator if translator is not None else get_default_translator()
        )
        self._seed_limit = max(0, min(seed_limit, _MAX_SEED_LIMIT))

        #: The viewer this screen hosts. Created once in ``__init__`` so
        #: :meth:`compose` can yield it, seeded and attached in
        #: :meth:`on_mount`, detached in :meth:`on_unmount`. Its lifetime
        #: is exactly the screen's lifetime — no cross-screen state.
        self._viewer = LogViewer(translator=self._translator)

        #: The router's detach callback, stored so :meth:`on_unmount` can
        #: call it. ``None`` between ``__init__`` and ``on_mount`` and
        #: again after ``on_unmount``.
        self._detach: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the title, the widget, and the hint.

        The order of children combined with the ``dock`` CSS rules
        places the title at the top, the hint at the bottom, and the
        viewer filling the middle.
        """
        yield Label(
            self._translator.translate("logs.screen.title"),
            id=_ID_TITLE,
        )
        yield self._viewer
        yield Label(
            self._translator.translate("logs.screen.hint"),
            id=_ID_HINT,
        )

    def on_mount(self) -> None:
        """Seed and attach the viewer to the router.

        Uses :meth:`LogRouter.attach_with_seed` — the atomic combined
        operation documented in the module docstring. The returned
        detach callback is stored for :meth:`on_unmount`.

        No try/except around the attach: a router that fails to provide
        the expected method is a **composition root wiring bug**, and
        failing loudly is the correct outcome. The screen's job is to
        trust the interface it was given — and the interface's
        transactional guarantee (see
        :meth:`LogRouter.attach_with_seed`'s docstring) is what makes
        "failing loudly" safe: on the exception path the viewer was
        not attached, so there is no detach callback the screen would
        need to remember.
        """
        self._detach = self._log_router.attach_with_seed(
            self._viewer, limit=self._seed_limit
        )

    def on_unmount(self) -> None:
        """Detach the viewer from the router.

        Called automatically by Textual when the screen is popped. The
        detach callback is idempotent, so calling it here after a
        previous call (should that ever happen) is a no-op. Resetting
        :attr:`_detach` to ``None`` ensures a second ``on_unmount``
        invocation (should it ever occur) does not call a stale
        callback.

        The ``finally`` guarantees the reset even if the detach itself
        raises — a router whose detach callback throws must not leave
        the screen in a state where a second ``on_unmount`` would call
        the same broken callback again.
        """
        if self._detach is not None:
            try:
                self._detach()
            finally:
                self._detach = None

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def action_back(self) -> None:
        """Pop this screen off the stack, returning to the previous one."""
        self.app.pop_screen()
