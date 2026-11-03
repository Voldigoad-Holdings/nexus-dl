"""Textual screen presenting the active configuration as a diagnostic view.

The settings screen is a **read-only inspector** for the currently loaded
:class:`~nexusdl.core.config.Settings` object. It does not edit anything:
settings come from ``.env``, ``config.yaml``, and the bundled defaults,
all of which are edited outside the application. What the screen provides
is what ``nexusdl config`` (a hypothetical CLI subcommand) would print on
a terminal — but interactive, section-based, and rendered with the same
theme as the rest of the TUI.

Why read-only
-------------
:class:`~nexusdl.core.config.Settings` is a ``frozen=True`` Pydantic
model. Editing it in-process would require mutating that frozen state
or reloading the whole application; neither is what a user expects from
a "settings" screen. A user who wants to change a setting edits
``.env`` or ``config.yaml`` and restarts the app. The screen makes the
current state visible so that a user can *decide* what to change, then
provides the paths where those files live so they know where to go.

Layout
------
A two-panel split:

* **Left pane** — an :class:`~textual.widgets.OptionList` listing every
  section (Overview, Paths, Downloads, Network, Content, Logging,
  Integrations, Server, Plugins, Advanced). Navigation is
  keyboard-first (``↑`` / ``↓``), and the content pane updates as the
  highlight moves — no ``Enter`` required.
* **Right pane** — a scrollable
  :class:`~textual.containers.VerticalScroll` wrapping a
  :class:`~textual.widgets.Static` whose content is the rendered
  section. The content is built as a :class:`~textual.content.Text`
  object, so every character — including a path containing ``[`` or a
  language code with unusual Unicode — renders literally, never as
  Textual markup.

Section grouping
----------------
The screen's ten sections are a **logical grouping** of the twenty-four
sections that :class:`~nexusdl.core.config.Settings` declares. The
mapping is deliberate, not incidental:

* **Overview** — app identity, the three root URLs, and the three
  secrets (masked). What a user needs to confirm before anything else.
* **Paths** — every filesystem path the pipeline uses, with an
  existence and size indicator for each. The most-frequently-requested
  diagnostic ("where does NexusDL keep my downloads?").
* **Downloads** — downloader, packaging, image sections. The knobs
  a user tunes to control download behaviour.
* **Network** — http, rate limits, session cache and circuit breaker,
  proxy. Everything a user needs when a site starts behaving badly.
* **Content** — content filtering and robots.txt compliance. The two
  sections a user consults when deciding what the app is allowed to
  fetch.
* **Logging** — the logging section alone. A short, self-contained
  view.
* **Integrations** — Playwright and FlareSolverr. Whether each is
  enabled, and where it points.
* **Server** — the API server, WebSocket tuning, and the TUI / GUI
  preferences. What a deployment serving HTTP needs to confirm.
* **Plugins** — plugin roots, disabled plugins, and runtime site
  overrides. What a plugin author or operator looks at.
* **Advanced** — performance, development, storage, and library. The
  remaining knobs, grouped so a user who does not need them never
  needs to scroll through them.

Secret masking
--------------
The screen displays three classes of secret:

* **Root secrets** — ``secret_key``, ``jwt_secret``,
  ``cookie_encryption_key`` (all :class:`~pydantic.SecretStr`). Shown
  as ``"*** (N chars)"`` — the length confirms the value is set and
  plausible without revealing it.
* **Proxy URLs** — ``proxy.url`` and each ``proxy.urls`` entry (also
  :class:`~pydantic.SecretStr`). Shown as ``"***"`` regardless of
  length; a proxy URL can embed credentials, and a "length" hint would
  narrow a brute-force. The distinction from the root secrets is
  deliberate — those are cryptographic material with a well-defined
  expected length; proxy URLs are opaque strings.
* **Site cookies** — not displayed by this screen at all. The cookie
  store is an encrypted binary file; the screen shows its path (via
  the Paths section) but never reads its content.

An empty or unset secret renders ``"(not set)"`` or ``"(empty)"`` — a
user who has not run ``nexusdl admin init`` sees immediately that the
configuration is incomplete, instead of seeing a working-looking screen
that crashes on the first signature operation.

Action surface
--------------
Two bindings, one action:

* ``Escape`` and ``q`` — return to the previous screen. The screen
  itself is **never** the last one on the stack (the main screen is
  pushed at app startup and the settings screen is pushed on demand),
  so the action is always valid.
* ``r`` — no-op today. Reserved for a future "reload from disk"
  capability that would reload :class:`Settings` and re-render without
  restarting the app. Not implemented because :class:`Settings` is
  frozen and reloading would require restarting the process, which the
  TUI cannot do.

There is no save action, no cancel action, and no confirmation dialog —
because there is nothing to save. The screen is a viewer.

Localisation
------------
The screen introduces twenty-one translation keys under
``settings.*``. Each section name, the "set / unset" markers, and the
path-existence markers are all localised. The full set:

* ``settings.title``
* ``settings.section.{overview,paths,downloads,network,content,logging,integrations,server,plugins,advanced}``
* ``settings.value.none`` — ``"(none)"`` placeholder for empty lists
* ``settings.value.empty`` — ``"(empty)"`` for empty strings
* ``settings.value.not_set`` — ``"(not set)"`` for unset secrets
* ``settings.value.masked`` — the plain ``"***"`` marker
* ``settings.value.masked_with_size`` — ``"*** ({count} chars)"``
* ``settings.path.exists`` — ``"exists"``
* ``settings.path.missing`` — ``"missing"``
* ``settings.path.directory`` — ``"directory"``
* ``settings.path.file`` — ``"file, {size}"``
* ``settings.value.yes`` / ``settings.value.no`` — for booleans
* ``settings.value.unknown`` — for values that could not be read

A missing key renders the key verbatim, matching the contract every
other widget and screen in this package follows. See
:mod:`nexusdl.core.i18n`.

Defensive section rendering
---------------------------
Each section builder reads fields from :class:`Settings` and formats
them. A builder that raises — a future field rename, a
:class:`SecretStr` that somehow did not survive Pydantic validation, a
pathological value in a dict-typed field — is caught by
:meth:`SettingsScreen._render_section` and replaced with an error
placeholder that names the exception. A broken section does not take
the screen down; the user sees that the section is broken and can
report it, while the other nine sections render normally.

Dependency graph (no cycles)::

    nexusdl.core.config         ─┐
    nexusdl.core.constants       │
    nexusdl.core.i18n            ├──► interfaces.cli.screens.settings
    nexusdl.core.paths           │
    nexusdl.core.utils.filesystem│
    textual                      │

No private attribute of any core module is touched. The screen reads
only the ``Settings`` object it was constructed with.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Text
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from nexusdl.core.constants import APP_NAME, APP_SLUG, DATA_PACKAGE
from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.paths import describe as describe_paths
from nexusdl.core.utils.filesystem import human_readable_size

if TYPE_CHECKING:
    from pydantic import SecretStr

    from nexusdl.core.config import Settings


__all__ = ["SettingsScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so a future CSS refactor has a
#: single place to update the selectors and the ``DEFAULT_CSS`` string
#: below cannot drift from the ``query_one`` calls.
_ID_SECTIONS: Final[str] = "settings-sections"
_ID_CONTENT: Final[str] = "settings-content"

#: The list of section ids, in display order. The screen's dispatch
#: (``_render_section``) uses the ids; the labels come from i18n.
_SECTION_IDS: Final[tuple[str, ...]] = (
    "overview",
    "paths",
    "downloads",
    "network",
    "content",
    "logging",
    "integrations",
    "server",
    "plugins",
    "advanced",
)


# --------------------------------------------------------------------------- #
# SettingsScreen                                                              #
# --------------------------------------------------------------------------- #


class SettingsScreen(Screen[None]):
    """A Textual screen presenting the loaded settings as a diagnostic view.

    See the module docstring for the design contract, the section
    grouping, the secret masking rules, and the defensive rendering
    guarantee.

    Args:
        settings: The application settings to display. The screen holds
            a reference for its own lifetime; it does not re-read the
            settings from disk.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        name: Optional Textual screen name. Passed through to
            :class:`~textual.screen.Screen`.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: Bindings for the screen. Both the exit binding and the reserved
    #: reload binding are declared here; the reload action is a no-op
    #: today.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
    ]

    #: Default CSS. The application's ``theme.tcss`` may override any
    #: rule by targeting ``SettingsScreen`` or the ``#settings-*`` ids.
    DEFAULT_CSS: ClassVar[str] = """
    SettingsScreen {
        background: $surface;
    }
    #settings-sections {
        width: 22;
        height: 1fr;
        border-right: solid $primary;
        background: $panel;
    }
    #settings-content {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        settings: Settings,
        *,
        translator: Translator | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._settings = settings
        self._translator = (
            translator if translator is not None else get_default_translator()
        )

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the two-pane layout.

        The OptionList is populated with one ``Option`` per section, in
        the fixed order defined by :data:`_SECTION_IDS`. Labels are
        translated; ids are stable identifiers used by the renderer.
        """
        with Horizontal():
            yield OptionList(
                *[
                    Option(
                        self._translator.translate(f"settings.section.{sid}"),
                        id=sid,
                    )
                    for sid in _SECTION_IDS
                ],
                id=_ID_SECTIONS,
            )
            yield VerticalScroll(
                Static("", id=_ID_CONTENT),
            )

    def on_mount(self) -> None:
        """Highlight the first section and render its content."""
        sections = self.query_one(f"#{_ID_SECTIONS}", OptionList)
        sections.highlighted = 0
        self._render_into_content(_SECTION_IDS[0])
        sections.focus()

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        """Render the section whose row the user highlighted.

        Fires on arrow-key navigation and on programmatic
        ``highlighted`` assignment. The content pane updates as the
        highlight moves — no ``Enter`` required, matching the standard
        settings-panel UX.
        """
        if event.option.id is None:
            return
        self._render_into_content(event.option.id)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Re-render on explicit selection (Enter / click).

        Redundant with the highlight handler in most cases, but
        guarantees the content is correct after a click that did not
        move the highlight (a click on the already-highlighted row
        does not emit a new highlight event in some Textual versions).
        """
        if event.option.id is None:
            return
        self._render_into_content(event.option.id)

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def action_back(self) -> None:
        """Pop this screen off the stack, returning to the previous one."""
        self.app.pop_screen()

    # ------------------------------------------------------------------ #
    # Content dispatch                                                   #
    # ------------------------------------------------------------------ #

    def _render_into_content(self, section_id: str) -> None:
        """Render *section_id* and set it on the content widget."""
        content = self.query_one(f"#{_ID_CONTENT}", Static)
        content.update(self._render_section(section_id))

    def _render_section(self, section_id: str) -> Text:
        """Dispatch to the section builder for *section_id*.

        A builder that raises is caught and replaced with an error
        placeholder — a broken section must not take the screen down.
        See the module docstring's "Defensive section rendering"
        section.
        """
        builders = {
            "overview": self._section_overview,
            "paths": self._section_paths,
            "downloads": self._section_downloads,
            "network": self._section_network,
            "content": self._section_content,
            "logging": self._section_logging,
            "integrations": self._section_integrations,
            "server": self._section_server,
            "plugins": self._section_plugins,
            "advanced": self._section_advanced,
        }
        builder = builders.get(section_id)
        if builder is None:
            return Text(f"Unknown section: {section_id}", style="red")

        try:
            return builder()
        except Exception as exc:  # noqa: BLE001 — see docstring
            text = Text()
            text.append(
                f"Error rendering section {section_id!r}\n",
                style="bold red",
            )
            text.append(f"{type(exc).__name__}: {exc}", style="red")
            return text

    # ------------------------------------------------------------------ #
    # Section builders                                                   #
    # ------------------------------------------------------------------ #

    def _section_overview(self) -> Text:
        """Application identity, root URLs, and secret presence."""
        text = Text()

        self._append_group(
            text,
            "Application",
            [
                ("name", self._settings.app.name),
                ("slug", APP_SLUG),
                ("bundled_name", APP_NAME),
                ("data_package", DATA_PACKAGE),
                ("debug", self._bool(self._settings.app.debug)),
                ("language", self._settings.app.language),
                ("timezone", self._settings.app.timezone),
            ],
        )

        self._append_group(
            text,
            "URLs",
            [
                ("public_url", self._settings.public_url),
                ("api_base_url", self._settings.api_base_url),
                ("frontend_url", self._settings.frontend_url),
            ],
        )

        self._append_group(
            text,
            "Secrets",
            [
                (
                    "secret_key",
                    self._mask_root_secret(self._settings.secret_key),
                ),
                (
                    "jwt_secret",
                    self._mask_root_secret(self._settings.jwt_secret),
                ),
                (
                    "cookie_encryption_key",
                    self._mask_root_secret(
                        self._settings.cookie_encryption_key
                    ),
                ),
            ],
        )

        return text

    def _section_paths(self) -> Text:
        """Every filesystem path, with an existence and size marker."""
        text = Text()
        paths = describe_paths(self._settings)
        items: list[tuple[str, str]] = []
        for key, path_str in paths.items():
            path = Path(path_str)
            items.append((key, self._format_path_entry(path, path_str)))
        self._append_group(text, "Paths", items)
        return text

    def _section_downloads(self) -> Text:
        """Downloader, packaging, and image pipeline configuration."""
        text = Text()

        d = self._settings.downloader
        self._append_group(
            text,
            "Downloader",
            [
                ("max_concurrent_tasks", str(d.max_concurrent_tasks)),
                ("max_concurrent_pages", str(d.max_concurrent_pages)),
                ("default_format", d.default_format.value),
                ("skip_existing", self._bool(d.skip_existing)),
                ("retry.max_attempts", str(d.retry.max_attempts)),
                (
                    "retry.initial_delay",
                    f"{d.retry.initial_delay}s",
                ),
                ("retry.max_delay", f"{d.retry.max_delay}s"),
            ],
        )

        p = self._settings.packaging
        compression = (
            self._t("settings.value.none")
            if p.compression_level is None
            else str(p.compression_level)
        )
        self._append_group(
            text,
            "Packaging",
            [
                ("compression_level", compression),
                ("embed_comic_info", self._bool(p.embed_comic_info)),
            ],
        )

        i = self._settings.image
        self._append_group(
            text,
            "Image",
            [
                ("validate", self._bool(i.validate)),
                ("optimize", self._bool(i.optimize)),
                ("optimize_jpeg_quality", str(i.optimize_jpeg_quality)),
                (
                    "optimize_png_compress_level",
                    str(i.optimize_png_compress_level),
                ),
                ("optimize_webp_quality", str(i.optimize_webp_quality)),
            ],
        )

        return text

    def _section_network(self) -> Text:
        """HTTP, rate limits, session cache and circuit breaker, proxy."""
        text = Text()

        h = self._settings.http
        self._append_group(
            text,
            "HTTP",
            [
                ("connect_timeout", f"{h.connect_timeout}s"),
                ("read_timeout", f"{h.read_timeout}s"),
                ("write_timeout", f"{h.write_timeout}s"),
                ("pool_timeout", f"{h.pool_timeout}s"),
                ("http2", self._bool(h.http2)),
                ("follow_redirects", self._bool(h.follow_redirects)),
                ("max_connections", str(h.max_connections)),
                (
                    "max_keepalive_connections",
                    str(h.max_keepalive_connections),
                ),
                (
                    "user_agent",
                    h.user_agent or self._t("settings.value.none"),
                ),
            ],
        )

        rl = self._settings.rate_limits
        rate_items: list[tuple[str, str]] = [
            ("default_per_second", f"{rl.default_per_second}/s"),
            ("burst_multiplier", str(rl.burst_multiplier)),
        ]
        if rl.per_site:
            for site_id, rate in rl.per_site.items():
                rate_items.append((f"per_site[{site_id}]", f"{rate}/s"))
        else:
            rate_items.append(
                ("per_site", self._t("settings.value.none"))
            )
        self._append_group(text, "Rate limits", rate_items)

        cb = self._settings.session.circuit_breaker
        self._append_group(
            text,
            "Circuit breaker",
            [
                ("failure_threshold", str(cb.failure_threshold)),
                ("recovery_timeout", f"{cb.recovery_timeout}s"),
                (
                    "half_open_success_threshold",
                    str(cb.half_open_success_threshold),
                ),
                (
                    "half_open_max_attempts",
                    str(cb.half_open_max_attempts),
                ),
            ],
        )

        c = self._settings.session.cache
        self._append_group(
            text,
            "HTTP cache",
            [
                ("enabled", self._bool(c.enabled)),
                ("max_size_bytes", human_readable_size(c.max_size_bytes)),
                ("default_ttl", f"{c.default_ttl}s"),
                ("eviction_target_ratio", str(c.eviction_target_ratio)),
                (
                    "per_site_ttl",
                    (
                        self._t("settings.value.none")
                        if not c.per_site_ttl
                        else ", ".join(
                            f"{sid}={ttl}s"
                            for sid, ttl in c.per_site_ttl.items()
                        )
                    ),
                ),
            ],
        )

        p = self._settings.proxy
        proxy_items: list[tuple[str, str]] = [
            ("enabled", self._bool(p.enabled)),
            ("strategy", p.strategy),
            ("sticky", self._bool(p.sticky)),
            ("failure_threshold", str(p.failure_threshold)),
            ("cooldown_seconds", f"{p.cooldown_seconds}s"),
            (
                "url",
                self._t("settings.value.masked")
                if p.url is not None
                else self._t("settings.value.none"),
            ),
            (
                "urls",
                (
                    ", ".join(
                        self._t("settings.value.masked") for _ in p.urls
                    )
                    if p.urls
                    else self._t("settings.value.none")
                ),
            ),
        ]
        self._append_group(text, "Proxy", proxy_items)

        return text

    def _section_content(self) -> Text:
        """Content filtering and robots.txt compliance."""
        text = Text()

        c = self._settings.content
        self._append_group(
            text,
            "Content",
            [
                ("allow_adult", self._bool(c.allow_adult)),
                ("default_language", c.default_language.value),
                ("max_content_rating", c.max_content_rating.value),
            ],
        )

        co = self._settings.compliance
        self._append_group(
            text,
            "Compliance",
            [
                ("respect_robots_txt", self._bool(co.respect_robots_txt)),
                (
                    "user_agent",
                    co.user_agent or self._t("settings.value.none"),
                ),
                ("cache_ttl_seconds", f"{co.cache_ttl_seconds}s"),
                ("fetch_timeout_seconds", f"{co.fetch_timeout_seconds}s"),
            ],
        )

        return text

    def _section_logging(self) -> Text:
        """Logging configuration."""
        text = Text()

        lg = self._settings.logging
        items: list[tuple[str, str]] = [
            ("level", lg.level),
            (
                "format",
                lg.format or self._t("settings.value.none"),
            ),
            (
                "file",
                str(lg.file)
                if lg.file is not None
                else self._t("settings.value.none"),
            ),
            ("rotation", lg.rotation),
            ("retention", lg.retention),
            (
                "compression",
                lg.compression or self._t("settings.value.none"),
            ),
        ]
        self._append_group(text, "Logging", items)
        return text

    def _section_integrations(self) -> Text:
        """Playwright and FlareSolverr configuration."""
        text = Text()

        pl = self._settings.playwright
        self._append_group(
            text,
            "Playwright",
            [
                ("enabled", self._bool(pl.enabled)),
                ("max_contexts", str(pl.max_contexts)),
                ("headless", self._bool(pl.headless)),
                ("timeout_ms", str(pl.timeout_ms)),
            ],
        )

        fs = self._settings.flaresolverr
        self._append_group(
            text,
            "FlareSolverr",
            [
                ("enabled", self._bool(fs.enabled)),
                ("endpoint", fs.endpoint),
                ("timeout_ms", str(fs.timeout_ms)),
            ],
        )

        return text

    def _section_server(self) -> Text:
        """API server, WebSocket tuning, and TUI / GUI preferences."""
        text = Text()

        a = self._settings.api
        self._append_group(
            text,
            "API server",
            [
                ("host", a.host),
                ("port", str(a.port)),
                ("workers", str(a.workers)),
                (
                    "cors_origins",
                    (
                        ", ".join(a.cors_origins)
                        if a.cors_origins
                        else self._t("settings.value.none")
                    ),
                ),
                ("jwt_algorithm", a.jwt_algorithm),
                ("jwt_expire_minutes", str(a.jwt_expire_minutes)),
            ],
        )

        w = self._settings.websocket
        self._append_group(
            text,
            "WebSocket",
            [
                ("ping_interval", f"{w.ping_interval}s"),
                ("ping_timeout", f"{w.ping_timeout}s"),
                ("max_message_size", human_readable_size(w.max_message_size)),
            ],
        )

        t = self._settings.tui
        self._append_group(
            text,
            "TUI",
            [
                ("theme", t.theme),
                ("animations", self._bool(t.animations)),
            ],
        )

        g = self._settings.gui
        self._append_group(
            text,
            "GUI",
            [
                ("theme", g.theme),
                ("window_width", str(g.window_width)),
                ("window_height", str(g.window_height)),
            ],
        )

        return text

    def _section_plugins(self) -> Text:
        """Plugin roots, disabled plugins, and runtime site overrides."""
        text = Text()

        p = self._settings.plugins
        self._append_group(
            text,
            "Plugins",
            [
                (
                    "dirs",
                    (
                        ", ".join(str(d) for d in p.dirs)
                        if p.dirs
                        else self._t("settings.value.none")
                    ),
                ),
                (
                    "disabled",
                    (
                        ", ".join(p.disabled)
                        if p.disabled
                        else self._t("settings.value.none")
                    ),
                ),
                (
                    "data_dir",
                    (
                        str(p.data_dir)
                        if p.data_dir is not None
                        else self._t("settings.value.none")
                    ),
                ),
            ],
        )

        s = self._settings.sites
        self._append_group(
            text,
            "Sites",
            [
                (
                    "disabled",
                    (
                        ", ".join(s.disabled)
                        if s.disabled
                        else self._t("settings.value.none")
                    ),
                ),
            ],
        )

        return text

    def _section_advanced(self) -> Text:
        """Performance, development, storage, and library settings."""
        text = Text()

        perf = self._settings.performance
        self._append_group(
            text,
            "Performance",
            [
                ("asyncio_debug", self._bool(perf.asyncio_debug)),
                ("http_pool_size", str(perf.http_pool_size)),
                ("image_workers", str(perf.image_workers)),
            ],
        )

        dev = self._settings.development
        self._append_group(
            text,
            "Development",
            [
                ("is_development", self._bool(dev.is_development)),
                ("show_traceback", self._bool(dev.show_traceback)),
                ("mock_sites", self._bool(dev.mock_sites)),
            ],
        )

        st = self._settings.storage
        self._append_group(
            text,
            "Storage",
            [
                ("downloads_dir", str(st.downloads_dir)),
                (
                    "temporary_dir",
                    (
                        str(st.temporary_dir)
                        if st.temporary_dir is not None
                        else self._t("settings.value.none")
                    ),
                ),
            ],
        )

        lib = self._settings.library
        self._append_group(
            text,
            "Library",
            [
                (
                    "scanner_max_concurrent",
                    str(lib.scanner_max_concurrent),
                ),
            ],
        )

        return text

    # ------------------------------------------------------------------ #
    # Formatting helpers                                                 #
    # ------------------------------------------------------------------ #

    def _append_group(
        self,
        text: Text,
        title: str,
        items: list[tuple[str, str]],
    ) -> None:
        """Append one titled group of key-value pairs to *text*.

        The title is underlined and bold; each key is bold; the value
        is appended literally, so a path containing ``[`` renders as
        ``[`` and never as a Textual markup tag.
        """
        text.append(title, style="bold underline")
        text.append("\n")
        for key, value in items:
            text.append("  ", style="")
            text.append(key, style="bold")
            text.append(": ", style="")
            text.append(value, style="")
            text.append("\n")
        text.append("\n")

    def _format_path_entry(self, path: Path, path_str: str) -> str:
        """Return the display string for one filesystem path.

        * **File, present** — path followed by ``(file, <size>)``.
        * **Directory, present** — path followed by ``(directory)``.
        * **Missing** — path followed by ``(missing)``.

        The size query is a single ``stat`` call per path. Failure to
        stat (permission denied, race) is treated as "missing".
        """
        try:
            if path.is_file():
                size = human_readable_size(path.stat().st_size)
                return (
                    f"{path_str}  ({self._t('settings.path.file')}"
                    f" — {size})"
                )
            if path.is_dir():
                return (
                    f"{path_str}  ({self._t('settings.path.directory')})"
                )
        except OSError:
            pass
        return f"{path_str}  ({self._t('settings.path.missing')})"

    def _mask_root_secret(self, secret: SecretStr) -> str:
        """Render a root :class:`~pydantic.SecretStr` with a length hint.

        A root secret's length is safe to reveal — cryptographic keys
        have well-defined expected lengths (a Fernet key is exactly 44
        characters), so the length helps the user confirm "yes, the key
        is set" without revealing the value. This is a deliberate
        difference from the proxy URL masking, which reveals nothing —
        a proxy URL's length is not diagnostic and can hint at the
        embedded credentials' shape.
        """
        value = secret.get_secret_value()
        if not value:
            return self._t("settings.value.empty")
        return self._t(
            "settings.value.masked_with_size",
            count=len(value),
        )

    def _bool(self, value: bool) -> str:
        """Render a boolean as a translated yes/no marker."""
        key = (
            "settings.value.yes" if value else "settings.value.no"
        )
        return self._t(key)

    def _t(self, key: str, **params: object) -> str:
        """Translate *key*, forwarding *params*.

        Thin wrapper around :meth:`Translator.translate` so the
        builders read as ``self._t("settings.path.missing")`` instead of
        ``self._translator.translate("settings.path.missing")``.
        """
        return self._translator.translate(key, **params)
