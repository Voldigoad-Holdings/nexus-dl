"""Settings view for the NexusDL GUI.

Schema-driven, storage-agnostic settings editor.

This view is a *pure editor*: it does **not** read :class:`Settings`, does
**not** persist anything, does **not** know about :class:`EventBus` or any
concrete configuration model. The hosting screen injects:

* a :class:`SettingsSchema` describing *what* to edit,
* a snapshot ``dict[str, Any]`` of the current values (dotted keys),
* an ``on_apply(changes) -> ApplyResult`` callback that owns validation
  server-side, persistence and any restart orchestration.

Because the contract is generic, the same view can edit:

* global ``Settings``,
* per-site overrides,
* plugin configuration,
* a user profile's preferences,

without any code change here. Adding a new setting is a new
:class:`SettingField` entry — nothing else.

Design notes:
    * ``FieldKind`` + :class:`_BindingFactory` form an open registry. A new
      widget type (``LIST``, ``DICT``, ``MULTI_ENUM``, ``CRON``…) is a new
      enum member plus a :class:`_BaseBinding` subclass, then a single
      registry line.
    * **Secrets are never read**. The current-values snapshot must provide
      ``True``/``False`` ("is this secret set?") rather than the raw value.
      The view only sends a value back to the host if the user explicitly
      changed it. A no-touch apply is therefore a no-op for secrets — no
      leak, no accidental overwrite.
    * **Local validation runs before** :attr:`on_apply` so the host receives
      only structurally valid data (int/float parsed, paths optionally
      checked, per-field ``validator`` passed). Business validation stays
      server-side, surfacing through :class:`ApplyResult.errors`.
    * **Dirty tracking is debounced** and broadcast via
      :attr:`on_dirty_change` so a host screen can decorate its title.
    * **Search is cross-section** — results are clickable and jump to the
      target section with a brief field flash. No widget duplication.
    * **Sections are lazy-built then stacked-and-hidden** — opening a long
      section does not slow down the others.
    * **Theme follows CTk appearance mode** via a 1 s poll (ttk/ttk-adjacent
      widgets do not re-theme themselves).
    * Callbacks are isolated — a misbehaving host never breaks Tk.
    * Cleans up after itself on ``<Destroy>``.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from tkinter import filedialog
from typing import Any, Protocol

import customtkinter as ctk
from loguru import logger as _logger

_log = _logger.bind(component="gui.settings_view")


# ---------------------------------------------------------------------------
# Data contracts (public)
# ---------------------------------------------------------------------------

class FieldKind(str, Enum):
    """Widget kind to render for a :class:`SettingField`.

    Adding a new kind requires a new :class:`_BaseBinding` subclass and one
    entry in :attr:`_BindingFactory._registry`.
    """

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    SECRET = "secret"
    ENUM = "enum"
    PATH_DIR = "path_dir"
    PATH_FILE = "path_file"
    READONLY = "readonly"


@dataclass(frozen=True, slots=True)
class SettingField:
    """Description of a single editable setting.

    Attributes:
        key: Dotted path (e.g. ``"content.allow_adult"``). Must be unique
            across the whole schema.
        label: Displayed label (short, human-readable).
        kind: Widget kind to render.
        default: Value used by *Reset to default*.
        description: Optional muted help text under the label.
        choices: Required for ``ENUM`` — ordered list of allowed values.
        min_value: Optional minimum (``INT`` / ``FLOAT``).
        max_value: Optional maximum (``INT`` / ``FLOAT``).
        must_exist: ``PATH_*`` only — validate the path exists locally.
        requires_restart: Whether a change takes effect only after restart.
        readonly: Renders read-only (ignored for ``BOOL``/``SECRET``).
        validator: Optional local validator ``(value) -> error_msg|None``.
        placeholder: Optional placeholder text for text-like widgets.
    """

    key: str
    label: str
    kind: FieldKind
    default: Any = None
    description: str = ""
    choices: tuple[str, ...] = ()
    min_value: float | int | None = None
    max_value: float | int | None = None
    must_exist: bool = False
    requires_restart: bool = False
    readonly: bool = False
    validator: Callable[[Any], str | None] | None = None
    placeholder: str = ""


@dataclass(frozen=True, slots=True)
class SettingGroup:
    """Optional visual grouping of fields inside a section."""

    id: str
    label: str
    field_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SettingSection:
    """A single settings section (one nav entry).

    If ``groups`` is empty, the section's fields are rendered as a flat list
    using ``field_keys``. If ``groups`` is set, they define the rendering
    order and grouping; ``field_keys`` is then optional and should contain
    the flat list of keys belonging to the section.
    """

    id: str
    label: str
    field_keys: tuple[str, ...] = ()
    groups: tuple[SettingGroup, ...] = ()
    description: str = ""
    icon: str = ""


@dataclass(frozen=True, slots=True)
class SettingsSchema:
    """The full editable schema.

    Attributes:
        sections: Ordered list of sections.
        fields: Flat lookup of all fields by their dotted key.
    """

    sections: tuple[SettingSection, ...]
    fields: Mapping[str, SettingField]

    def all_field_keys(self) -> tuple[str, ...]:
        """Return every field key in deterministic order."""
        return tuple(self.fields.keys())


@dataclass(slots=True)
class ApplyResult:
    """Result returned by :attr:`SettingsView.on_apply`.

    Attributes:
        success: Whether the host accepted the changes.
        errors: Optional mapping ``field_key -> error message`` for
            server-side validation failures. Keys not present here are
            considered valid.
        new_values: Optional authoritative values for the changed keys
            (allowing the host to normalize). When provided, the view
            adopts these as its new originals.
        requires_restart: Whether any applied change requires a restart.
        message: Optional global message ("Saved to config.yaml").
    """

    success: bool
    errors: dict[str, str] = field(default_factory=dict)
    new_values: dict[str, Any] | None = None
    requires_restart: bool = False
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SettingsLabels:
    """All user-visible strings used by :class:`SettingsView`."""

    title: str = "Settings"
    search_placeholder: str = "Search settings…"
    reload: str = "Reload"
    apply: str = "Apply"
    discard: str = "Discard"
    reset_all: str = "Reset all"
    reset_field: str = "Reset to default"
    no_section: str = "Select a section"
    no_matches: str = "No matching settings"
    unsaved_one: str = "1 unsaved change"
    unsaved_other: str = "{n} unsaved changes"
    no_changes: str = "No unsaved changes"
    apply_success: str = "Settings applied"
    apply_error: str = "Failed to apply settings"
    apply_validation_error: str = "{n} field(s) have error(s)"
    restart_required: str = "Some changes will take effect after a restart."
    restart_now: str = "Restart now"
    dismiss: str = "Dismiss"
    secret_set_placeholder: str = "••••••••"
    secret_show: str = "Show"
    secret_hide: str = "Hide"
    secret_clear: str = "Clear"
    browse: str = "Browse…"
    readonly_tag: str = "read-only"
    requires_restart_tag: str = "restart needed"
    search_results_header: str = "{n} match(es)"


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_THEME_LIGHT = {
    "bg": "#F5F5F5",
    "panel_bg": "#FFFFFF",
    "section_nav_bg": "#ECECEC",
    "nav_item_bg": "#ECECEC",
    "nav_item_hover": "#DDDDDD",
    "nav_item_active": "#3B8ED0",
    "nav_item_active_fg": "#FFFFFF",
    "fg": "#1A1A1A",
    "muted_fg": "#666666",
    "border": "#CCCCCC",
    "error_fg": "#C0392B",
    "flash_bg": "#FFF3B0",
    "banner_bg": "#FFF3B0",
    "banner_fg": "#7A5A00",
    "success_bg": "#D4F0DC",
    "success_fg": "#1E6E39",
}

_THEME_DARK = {
    "bg": "#1A1A1A",
    "panel_bg": "#242424",
    "section_nav_bg": "#1F1F1F",
    "nav_item_bg": "#1F1F1F",
    "nav_item_hover": "#2E2E2E",
    "nav_item_active": "#1F6AA5",
    "nav_item_active_fg": "#FFFFFF",
    "fg": "#DCDCDC",
    "muted_fg": "#909090",
    "border": "#333333",
    "error_fg": "#E74C3C",
    "flash_bg": "#4A3A00",
    "banner_bg": "#4A3A00",
    "banner_fg": "#FFD966",
    "success_bg": "#1E4A32",
    "success_fg": "#A4E5B8",
}

_SELECT_DEBOUNCE_MS = 60
_THEME_POLL_MS = 1000
_FLASH_MS = 900
_TOAST_MS = 2600
_NAV_WIDTH = 200


# ---------------------------------------------------------------------------
# Internal sentinel for "user has not touched this secret"
# ---------------------------------------------------------------------------

class _SecretState(str, Enum):
    UNTOUCHED = "untouched"
    CLEARED = "cleared"
    SET = "set"


# ---------------------------------------------------------------------------
# Field binding protocol + implementations
# ---------------------------------------------------------------------------

class _FieldBinding(Protocol):
    """Interface every field binding must implement."""

    widget: ctk.CTkBaseClass
    field: SettingField

    def is_dirty(self) -> bool: ...
    def validate(self) -> str | None: ...
    def get_pending_value(self) -> Any: ...
    def set_value(self, value: Any) -> None: ...
    def reset_to_default(self) -> None: ...
    def set_error(self, message: str | None) -> None: ...
    def flash(self) -> None: ...


class _BaseBinding:
    """Common scaffolding for all bindings."""

    widget: ctk.CTkBaseClass

    def __init__(
        self,
        field: SettingField,
        *,
        original_value: Any,
        container: ctk.CTkFrame,
        on_change: Callable[[], None],
        labels: SettingsLabels,
        theme_provider: Callable[[], dict],
    ) -> None:
        self.field = field
        self._original = original_value
        self._container = container
        self._on_change = on_change
        self._labels = labels
        self._theme = theme_provider
        self._error_label: ctk.CTkLabel | None = None
        self._flash_after_id: str | None = None

    # ----- subclass hooks ---------------------------------------------------

    def _current_value(self) -> Any:
        raise NotImplementedError

    def _apply_value(self, value: Any) -> None:
        raise NotImplementedError

    def _local_error(self, value: Any) -> str | None:
        return None

    # ----- public API -------------------------------------------------------

    def is_dirty(self) -> bool:
        try:
            return self._current_value() != self._original
        except Exception:
            _log.exception("Binding is_dirty failed for {!r}", self.field.key)
            return False

    def validate(self) -> str | None:
        try:
            value = self._current_value()
        except Exception:
            return "Invalid value"
        local = self._local_error(value)
        if local is not None:
            return local
        if self.field.validator is not None:
            try:
                result = self.field.validator(value)
            except Exception:
                _log.exception(
                    "SettingField.validator raised for {!r}", self.field.key
                )
                return "Validator crashed"
            if result is not None:
                return result
        return None

    def get_pending_value(self) -> Any:
        return self._current_value()

    def set_value(self, value: Any) -> None:
        try:
            self._apply_value(value)
        except Exception:
            _log.exception("Binding set_value failed for {!r}", self.field.key)
        self._on_change()

    def reset_to_default(self) -> None:
        try:
            self._apply_value(self.field.default)
        except Exception:
            _log.exception("Binding reset_to_default failed for {!r}", self.field.key)
        self._on_change()

    def set_error(self, message: str | None) -> None:
        self._ensure_error_label()
        assert self._error_label is not None
        if message:
            self._error_label.configure(
                text=message, text_color=self._theme()["error_fg"]
            )
            self._error_label.pack(fill="x", pady=(2, 0))
        else:
            self._error_label.pack_forget()

    def flash(self) -> None:
        """Briefly highlight the binding's container to help the user find it."""
        try:
            original = self._container.cget("fg_color")
        except Exception:
            return
        theme = self._theme()
        try:
            self._container.configure(fg_color=theme["flash_bg"])
        except Exception:
            return
        if self._flash_after_id is not None:
            try:
                self._container.after_cancel(self._flash_after_id)
            except Exception:
                pass
        self._flash_after_id = self._container.after(
            _FLASH_MS, lambda: self._restore_flash(original)
        )

    def _restore_flash(self, color: Any) -> None:
        self._flash_after_id = None
        try:
            self._container.configure(fg_color=color)
        except Exception:
            pass

    # ----- helpers ----------------------------------------------------------

    def _ensure_error_label(self) -> None:
        if self._error_label is not None:
            return
        self._error_label = ctk.CTkLabel(
            self._container,
            text="",
            anchor="w",
            font=("", 11),
            text_color=self._theme()["error_fg"],
        )


class _ReadonlyBinding(_BaseBinding):
    def __init__(self, field, *, original_value, container, on_change, labels, theme_provider):
        super().__init__(
            field,
            original_value=original_value,
            container=container,
            on_change=on_change,
            labels=labels,
            theme_provider=theme_provider,
        )
        display = "" if original_value is None else str(original_value)
        self.widget = ctk.CTkLabel(
            container,
            text=display,
            anchor="w",
            justify="left",
            text_color=theme_provider()["muted_fg"],
            font=("", 12, "italic"),
        )

    def _current_value(self) -> Any:
        return self._original

    def _apply_value(self, value: Any) -> None:
        self._original = value
        try:
            self.widget.configure(text="" if value is None else str(value))
        except Exception:
            pass

    def is_dirty(self) -> bool:
        return False


class _BoolBinding(_BaseBinding):
    def __init__(self, field, *, original_value, container, on_change, labels, theme_provider):
        super().__init__(
            field,
            original_value=bool(original_value),
            container=container,
            on_change=on_change,
            labels=labels,
            theme_provider=theme_provider,
        )
        self._var = ctk.BooleanVar(master=container, value=bool(original_value))
        self.widget = ctk.CTkSwitch(
            container,
            text="",
            variable=self._var,
            command=self._on_change,
        )

    def _current_value(self) -> Any:
        return bool(self._var.get())

    def _apply_value(self, value: Any) -> None:
        v = bool(value)
        try:
            if v:
                self.widget.select()
            else:
                self.widget.deselect()
        except Exception:
            self._var.set(v)


class _TextBinding(_BaseBinding):
    """Shared implementation for INT / FLOAT / STR / PATH kinds."""

    _path_kind: FieldKind | None = None

    def __init__(self, field, *, original_value, container, on_change, labels, theme_provider):
        super().__init__(
            field,
            original_value=self._normalize(original_value),
            container=container,
            on_change=on_change,
            labels=labels,
            theme_provider=theme_provider,
        )
        self._path_kind = (
            field.kind if field.kind in (FieldKind.PATH_DIR, FieldKind.PATH_FILE) else None
        )

        row = ctk.CTkFrame(container, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1)

        self._entry = ctk.CTkEntry(
            row,
            placeholder_text=field.placeholder,
        )
        self._entry.grid(row=0, column=0, sticky="ew")
        self._entry.insert(0, self._display_of(self._original))
        self._entry.bind("<KeyRelease>", lambda _e: self._on_change())

        if self._path_kind is not None:
            browse = ctk.CTkButton(
                row,
                text=labels.browse,
                width=90,
                command=self._browse,
            )
            browse.grid(row=0, column=1, padx=(6, 0))

        self.widget = self._entry

    @staticmethod
    def _normalize(value: Any) -> Any:
        if value is None:
            return ""
        return value

    @staticmethod
    def _display_of(value: Any) -> str:
        return "" if value is None else str(value)

    def _current_value(self) -> Any:
        raw = self._entry.get()
        if self.field.kind == FieldKind.INT:
            try:
                return int(raw)
            except (ValueError, TypeError):
                return raw
        if self.field.kind == FieldKind.FLOAT:
            try:
                return float(raw)
            except (ValueError, TypeError):
                return raw
        return raw

    def _apply_value(self, value: Any) -> None:
        text = self._display_of(value)
        try:
            self._entry.delete(0, "end")
            self._entry.insert(0, text)
        except Exception:
            pass

    def _local_error(self, value: Any) -> str | None:
        if self.field.kind == FieldKind.INT:
            if not isinstance(value, int):
                return "Must be an integer"
            if self.field.min_value is not None and value < self.field.min_value:
                return f"Must be ≥ {self.field.min_value}"
            if self.field.max_value is not None and value > self.field.max_value:
                return f"Must be ≤ {self.field.max_value}"
        elif self.field.kind == FieldKind.FLOAT:
            if not isinstance(value, float):
                return "Must be a number"
            if self.field.min_value is not None and value < self.field.min_value:
                return f"Must be ≥ {self.field.min_value}"
            if self.field.max_value is not None and value > self.field.max_value:
                return f"Must be ≤ {self.field.max_value}"
        elif self._path_kind is not None and self.field.must_exist and value:
            try:
                exists = (
                    Path(value).is_dir()
                    if self._path_kind == FieldKind.PATH_DIR
                    else Path(value).is_file()
                )
            except Exception:
                exists = False
            if not exists:
                return "Path does not exist"
        return None

    def _browse(self) -> None:
        if self._path_kind == FieldKind.PATH_DIR:
            chosen = filedialog.askdirectory()
        else:
            chosen = filedialog.askopenfilename()
        if chosen:
            try:
                self._entry.delete(0, "end")
                self._entry.insert(0, chosen)
            except Exception:
                pass
            self._on_change()


class _EnumBinding(_BaseBinding):
    def __init__(self, field, *, original_value, container, on_change, labels, theme_provider):
        super().__init__(
            field,
            original_value=str(original_value) if original_value is not None else "",
            container=container,
            on_change=on_change,
            labels=labels,
            theme_provider=theme_provider,
        )
        choices = list(field.choices) or [""]
        try:
            initial = str(original_value) if original_value is not None else choices[0]
        except Exception:
            initial = choices[0]
        if initial not in choices:
            initial = choices[0]
        self.widget = ctk.CTkOptionMenu(
            container,
            values=choices,
            command=lambda _v: self._on_change(),
        )
        try:
            self.widget.set(initial)
        except Exception:
            pass

    def _current_value(self) -> Any:
        try:
            return self.widget.get()
        except Exception:
            return ""

    def _apply_value(self, value: Any) -> None:
        try:
            self.widget.set("" if value is None else str(value))
        except Exception:
            pass


class _SecretBinding(_BaseBinding):
    """Never reads the secret. Only sends a value if explicitly changed."""

    def __init__(self, field, *, original_value, container, on_change, labels, theme_provider):
        # ``original_value`` for a secret is ``True`` (set) or ``False`` (unset).
        super().__init__(
            field,
            original_value=bool(original_value),
            container=container,
            on_change=on_change,
            labels=labels,
            theme_provider=theme_provider,
        )
        self._state: _SecretState = _SecretState.UNTOUCHED
        self._pending: str = ""
        self._visible = False

        row = ctk.CTkFrame(container, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1)

        self._entry = ctk.CTkEntry(row, show="•")
        self._entry.grid(row=0, column=0, sticky="ew")
        if self._original:
            self._entry.insert(0, labels.secret_set_placeholder)
        self._entry.bind("<KeyRelease>", self._on_entry_changed)

        self._toggle = ctk.CTkButton(
            row,
            text=labels.secret_show,
            width=64,
            command=self._toggle_visibility,
        )
        self._toggle.grid(row=0, column=1, padx=(6, 0))

        self._clear_btn = ctk.CTkButton(
            row,
            text=labels.secret_clear,
            width=64,
            fg_color=("#C0392B", "#922B21"),
            hover_color=("#A93226", "#7B241C"),
            command=self._on_clear,
        )
        self._clear_btn.grid(row=0, column=2, padx=(6, 0))

        self.widget = self._entry

    # ----- interactions -----------------------------------------------------

    def _on_entry_changed(self, _event=None) -> None:
        current = self._entry.get()
        if not current:
            self._state = (
                _SecretState.UNTOUCHED
                if not self._original
                else _SecretState.CLEARED
            )
            if self._original:
                self._state = _SecretState.CLEARED
            else:
                self._state = _SecretState.UNTOUCHED
            self._pending = ""
        else:
            self._state = _SecretState.SET
            self._pending = current
        self._on_change()

    def _on_clear(self) -> None:
        try:
            self._entry.delete(0, "end")
        except Exception:
            pass
        self._state = _SecretState.CLEARED
        self._pending = ""
        self._on_change()

    def _toggle_visibility(self) -> None:
        self._visible = not self._visible
        try:
            self._entry.configure(show="" if self._visible else "•")
            self._toggle.configure(
                text=self._labels.secret_hide if self._visible else self._labels.secret_show
            )
        except Exception:
            pass

    # ----- binding API ------------------------------------------------------

    def _current_value(self) -> Any:
        return self._state

    def is_dirty(self) -> bool:
        return self._state != _SecretState.UNTOUCHED

    def get_pending_value(self) -> Any:
        if self._state == _SecretState.CLEARED:
            return None
        if self._state == _SecretState.SET:
            return self._pending
        return self._original

    def validate(self) -> str | None:
        # Secret values are validated server-side only.
        return None

    def _apply_value(self, value: Any) -> None:
        # Reset to default semantics: default for a secret is "clear".
        if value is None and self._original:
            self._state = _SecretState.CLEARED
        else:
            self._state = _SecretState.UNTOUCHED
        try:
            self._entry.delete(0, "end")
            if self._state == _SecretState.UNTOUCHED and self._original:
                self._entry.insert(0, self._labels.secret_set_placeholder)
        except Exception:
            pass


class _BindingFactory:
    """Registry mapping :class:`FieldKind` to a binding class."""

    _registry: dict[FieldKind, type] = {
        FieldKind.BOOL: _BoolBinding,
        FieldKind.INT: _TextBinding,
        FieldKind.FLOAT: _TextBinding,
        FieldKind.STR: _TextBinding,
        FieldKind.PATH_DIR: _TextBinding,
        FieldKind.PATH_FILE: _TextBinding,
        FieldKind.ENUM: _EnumBinding,
        FieldKind.SECRET: _SecretBinding,
        FieldKind.READONLY: _ReadonlyBinding,
    }

    @classmethod
    def register(cls, kind: FieldKind, binding_cls: type) -> None:
        """Register a binding for a new field kind."""
        cls._registry[kind] = binding_cls

    @classmethod
    def build(
        cls,
        field: SettingField,
        *,
        original_value: Any,
        container: ctk.CTkFrame,
        on_change: Callable[[], None],
        labels: SettingsLabels,
        theme_provider: Callable[[], dict],
    ) -> _FieldBinding:
        binding_cls = cls._registry.get(field.kind)
        if binding_cls is None:
            raise ValueError(f"No binding registered for FieldKind {field.kind!r}")
        return binding_cls(
            field,
            original_value=original_value,
            container=container,
            on_change=on_change,
            labels=labels,
            theme_provider=theme_provider,
        )


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

class SettingsView(ctk.CTkFrame):
    """Schema-driven settings editor (embed-safe).

    Parameters:
        master: Parent widget.
        schema: The editable schema.
        current_values: Snapshot of the current values (dotted keys). Secrets
            must be provided as ``True``/``False`` (is set / unset), never as
            the raw value.
        on_apply: Called with the dict of *changed* values (secrets included
            only if explicitly edited). Must return an :class:`ApplyResult`.
        on_reload: Optional — invoked when the user clicks *Reload*. Must
            return a fresh ``dict[str, Any]`` snapshot; the view rebuilds on
            top of it.
        on_restart: Optional — invoked when the user clicks *Restart now*
            from the restart banner. If ``None`` the button is hidden.
        on_dirty_change: Optional ``(is_dirty: bool, count: int)`` callback
            fired (debounced) whenever dirty state changes.
        labels: Localized labels. ``None`` uses English defaults.
        initial_section: Section id to display first. Defaults to the first
            section in the schema.
    """

    def __init__(
        self,
        master,
        *,
        schema: SettingsSchema,
        current_values: Mapping[str, Any],
        on_apply: Callable[[dict[str, Any]], ApplyResult],
        on_reload: Callable[[], Mapping[str, Any]] | None = None,
        on_restart: Callable[[], None] | None = None,
        on_dirty_change: Callable[[bool, int], None] | None = None,
        labels: SettingsLabels | None = None,
        initial_section: str | None = None,
        **kwargs,
    ) -> None:
        if not schema.sections:
            raise ValueError("SettingsView requires at least one section")
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self._schema = schema
        self._labels = labels or SettingsLabels()
        self._on_apply_cb = on_apply
        self._on_reload_cb = on_reload
        self._on_restart_cb = on_restart
        self._on_dirty_change_cb = on_dirty_change

        self._values: dict[str, Any] = dict(current_values)
        self._bindings: dict[str, _FieldBinding] = {}
        self._built_sections: set[str] = set()

        self._current_section_id: str = (
            initial_section
            if initial_section in {s.id for s in schema.sections}
            else schema.sections[0].id
        )
        self._search_text: str = ""
        self._dirty_count: int = 0
        self._search_after_id: str | None = None
        self._theme_after_id: str | None = None
        self._toast_after_id: str | None = None
        self._restart_required: bool = False
        self._destroyed: bool = False
        self._last_theme_mode: str = self._current_mode()

        self._build()
        self._apply_theme()
        self._build_nav()
        self._show_section(self._current_section_id)
        self._refresh_dirty()

        self.bind("<Destroy>", self._on_destroy)
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_restart_required(self, required: bool) -> None:
        """External hook to show/hide the restart banner."""
        if self._destroyed:
            return
        self._restart_required = required
        self._refresh_restart_banner()

    def set_current_values(self, values: Mapping[str, Any]) -> None:
        """Replace the snapshot and rebuild — used after :attr:`on_reload`."""
        if self._destroyed:
            return
        self._values = dict(values)
        self._bindings.clear()
        self._built_sections.clear()
        for child in self._form_host.winfo_children():
            child.destroy()
        self._show_section(self._current_section_id)
        self._refresh_dirty()

    def go_to_field(self, field_key: str) -> None:
        """Jump to ``field_key``'s section and flash the field."""
        if self._destroyed:
            return
        field = self._schema.fields.get(field_key)
        if field is None:
            return
        section_id = self._find_section_of(field_key)
        if section_id is None:
            return
        self._search_text = ""
        self._search_var.set("")
        self._current_section_id = section_id
        self._show_section(section_id)
        binding = self._bindings.get(field_key)
        if binding is not None:
            binding.flash()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=_NAV_WIDTH)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # --- Top bar ---
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))
        top.grid_columnconfigure(0, weight=1)

        self._search_var = ctk.StringVar(master=self, value="")
        self._search_entry = ctk.CTkEntry(
            top,
            placeholder_text=self._labels.search_placeholder,
            textvariable=self._search_var,
        )
        self._search_entry.grid(row=0, column=0, sticky="ew")
        self._search_entry.bind("<KeyRelease>", self._on_search_changed)

        self._reload_btn = ctk.CTkButton(
            top,
            text=self._labels.reload,
            width=90,
            command=self._on_reload_click,
        )
        self._reload_btn.grid(row=0, column=1, padx=(8, 0))

        # --- Left nav ---
        self._nav = ctk.CTkScrollableFrame(
            self,
            width=_NAV_WIDTH,
            corner_radius=0,
            fg_color="transparent",
        )
        self._nav.grid(row=1, column=0, sticky="nsew")
        self._nav.grid_columnconfigure(0, weight=1)

        # --- Right form host ---
        self._form_host = ctk.CTkFrame(self, fg_color="transparent")
        self._form_host.grid(row=1, column=1, sticky="nsew")
        self._form_host.grid_columnconfigure(0, weight=1)
        self._form_host.grid_rowconfigure(0, weight=1)

        # --- Restart banner (hidden by default) ---
        self._banner = ctk.CTkFrame(self, fg_color="transparent")
        self._banner.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))
        self._banner.grid_columnconfigure(0, weight=1)
        self._banner_label = ctk.CTkLabel(
            self._banner, text=self._labels.restart_required, anchor="w"
        )
        self._banner_label.grid(row=0, column=0, sticky="w")
        if self._on_restart_cb is not None:
            self._banner_restart = ctk.CTkButton(
                self._banner,
                text=self._labels.restart_now,
                width=110,
                command=self._on_restart_click,
            )
            self._banner_restart.grid(row=0, column=1, padx=(8, 0))
        else:
            self._banner_restart = None
        self._banner_dismiss = ctk.CTkButton(
            self._banner,
            text=self._labels.dismiss,
            width=90,
            fg_color="transparent",
            border_width=1,
            command=lambda: self.set_restart_required(False),
        )
        self._banner_dismiss.grid(row=0, column=2, padx=(8, 0))
        self._banner.grid_remove()

        # --- Bottom bar ---
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10))
        bottom.grid_columnconfigure(0, weight=1)

        self._dirty_label = ctk.CTkLabel(bottom, text=self._labels.no_changes, anchor="w")
        self._dirty_label.grid(row=0, column=0, sticky="w")

        self._reset_all_btn = ctk.CTkButton(
            bottom,
            text=self._labels.reset_all,
            width=110,
            fg_color="transparent",
            border_width=1,
            command=self._on_reset_all_click,
        )
        self._reset_all_btn.grid(row=0, column=1, padx=(8, 0))

        self._discard_btn = ctk.CTkButton(
            bottom,
            text=self._labels.discard,
            width=90,
            fg_color="transparent",
            border_width=1,
            command=self._on_discard_click,
        )
        self._discard_btn.grid(row=0, column=2, padx=(8, 0))

        self._apply_btn = ctk.CTkButton(
            bottom,
            text=self._labels.apply,
            width=100,
            command=self._on_apply_click,
        )
        self._apply_btn.grid(row=0, column=3, padx=(8, 0))

        # --- Toast (hidden) ---
        self._toast = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            corner_radius=6,
            padx=10,
            pady=6,
        )
        self._toast.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        self._toast.grid_remove()

    def _build_nav(self) -> None:
        for child in self._nav.winfo_children():
            child.destroy()

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for section in self._schema.sections:
            text = f"{section.icon}  {section.label}".strip()
            button = ctk.CTkButton(
                self._nav,
                text=text,
                anchor="w",
                fg_color="transparent",
                hover_color=self._theme()["nav_item_hover"],
                text_color=self._theme()["fg"],
                command=lambda sid=section.id: self._on_nav_click(sid),
            )
            button.pack(fill="x", padx=6, pady=2)
            self._nav_buttons[section.id] = button

        self._refresh_nav_selection()

    def _refresh_nav_selection(self) -> None:
        theme = self._theme()
        for section_id, button in self._nav_buttons.items():
            active = section_id == self._current_section_id and not self._search_text
            try:
                button.configure(
                    fg_color=theme["nav_item_active"] if active else "transparent",
                    text_color=(
                        theme["nav_item_active_fg"] if active else theme["fg"]
                    ),
                    hover_color=theme["nav_item_hover"],
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Section rendering
    # ------------------------------------------------------------------

    def _on_nav_click(self, section_id: str) -> None:
        self._search_text = ""
        self._search_var.set("")
        self._show_section(section_id)

    def _show_section(self, section_id: str) -> None:
        if self._destroyed:
            return
        if self._search_text:
            self._render_search_results()
            return
        for child in self._form_host.winfo_children():
            child.grid_remove()
        self._current_section_id = section_id
        frame = self._ensure_section_frame(section_id)
        frame.grid()
        self._refresh_nav_selection()

    def _ensure_section_frame(self, section_id: str) -> ctk.CTkScrollableFrame:
        if section_id in self._built_sections:
            for child in self._form_host.winfo_children():
                if getattr(child, "_nexus_section_id", None) == section_id:
                    return child  # type: ignore[return-value]
        # Build it.
        section = next(s for s in self._schema.sections if s.id == section_id)
        frame = ctk.CTkScrollableFrame(self._form_host, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew")
        frame._nexus_section_id = section_id  # type: ignore[attr-defined]

        header = ctk.CTkLabel(
            frame,
            text=section.label,
            anchor="w",
            font=("", 16, "bold"),
            text_color=self._theme()["fg"],
        )
        header.pack(fill="x", padx=6, pady=(4, 2))
        if section.description:
            desc = ctk.CTkLabel(
                frame,
                text=section.description,
                anchor="w",
                justify="left",
                wraplength=560,
                text_color=self._theme()["muted_fg"],
                font=("", 11),
            )
            desc.pack(fill="x", padx=6, pady=(0, 8))

        if section.groups:
            for group in section.groups:
                self._render_group(frame, group.label, group.field_keys)
        else:
            self._render_group(frame, "", section.field_keys)

        self._built_sections.add(section_id)
        return frame

    def _render_group(
        self, parent: ctk.CTkFrame, group_label: str, field_keys: Iterable[str]
    ) -> None:
        if group_label:
            group_header = ctk.CTkLabel(
                parent,
                text=group_label,
                anchor="w",
                font=("", 12, "bold"),
                text_color=self._theme()["muted_fg"],
            )
            group_header.pack(fill="x", padx=6, pady=(10, 4))

        for key in field_keys:
            field = self._schema.fields.get(key)
            if field is None:
                _log.warning("Schema references unknown field {!r}", key)
                continue
            self._render_field(parent, field)

    def _render_field(self, parent: ctk.CTkFrame, field: SettingField) -> None:
        if field.key in self._bindings:
            # Already built (this happens when re-showing a section).
            container = self._bindings[field.key].widget.master
            if container is not None:
                return

        container = ctk.CTkFrame(
            parent,
            fg_color=self._theme()["panel_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self._theme()["border"],
        )
        container.pack(fill="x", padx=6, pady=3)
        container.grid_columnconfigure(0, weight=1)

        # Header row.
        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        header.grid_columnconfigure(0, weight=1)

        label_text = field.label
        if field.requires_restart:
            label_text += f"  ·  {self._labels.requires_restart_tag}"
        if field.readonly:
            label_text += f"  ·  {self._labels.readonly_tag}"

        label = ctk.CTkLabel(
            header,
            text=label_text,
            anchor="w",
            justify="left",
            font=("", 12, "bold"),
            text_color=self._theme()["fg"],
        )
        label.grid(row=0, column=0, sticky="w")

        # Reset-to-default button (hidden for readonly / secret handled by clear).
        if not field.readonly and field.kind != FieldKind.SECRET:
            reset_btn = ctk.CTkButton(
                header,
                text="↺",
                width=28,
                height=24,
                fg_color="transparent",
                border_width=1,
                command=lambda k=field.key: self._on_reset_field(k),
            )
            reset_btn.grid(row=0, column=1, padx=(6, 0))

        # Description.
        if field.description:
            desc = ctk.CTkLabel(
                container,
                text=field.description,
                anchor="w",
                justify="left",
                wraplength=560,
                font=("", 11),
                text_color=self._theme()["muted_fg"],
            )
            desc.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        # Widget area.
        widget_holder = ctk.CTkFrame(container, fg_color="transparent")
        widget_holder.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

        original = self._values.get(field.key, field.default)
        binding = _BindingFactory.build(
            field,
            original_value=original,
            container=widget_holder,
            on_change=self._schedule_dirty_refresh,
            labels=self._labels,
            theme_provider=self._theme,
        )
        # Bindings expect their widget to be packed inside ``widget_holder``
        # themselves in most cases; ensure it is.
        try:
            binding.widget.pack(fill="x")
        except Exception:
            try:
                binding.widget.grid(sticky="ew")
            except Exception:
                pass

        # For bools, we want the switch to be small and right-aligned.
        if field.kind == FieldKind.BOOL:
            try:
                binding.widget.pack_forget()
                binding.widget.pack(anchor="w")
            except Exception:
                pass

        self._bindings[field.key] = binding

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search_changed(self, _event=None) -> None:
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.after(_SELECT_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self) -> None:
        self._search_after_id = None
        if self._destroyed:
            return
        self._search_text = self._search_var.get().strip().lower()
        if self._search_text:
            self._render_search_results()
        else:
            self._show_section(self._current_section_id)

    def _matching_fields(self) -> list[SettingField]:
        needle = self._search_text
        out: list[SettingField] = []
        for field in self._schema.fields.values():
            haystacks = [field.key.lower(), field.label.lower(), field.description.lower()]
            if any(needle in h for h in haystacks):
                out.append(field)
        return out

    def _render_search_results(self) -> None:
        for child in self._form_host.winfo_children():
            child.grid_remove()

        frame = None
        for child in self._form_host.winfo_children():
            if getattr(child, "_nexus_section_id", None) == "__search__":
                frame = child
                break
        if frame is None:
            frame = ctk.CTkScrollableFrame(self._form_host, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            frame._nexus_section_id = "__search__"  # type: ignore[attr-defined]

        for c in frame.winfo_children():
            c.destroy()

        results = self._matching_fields()
        header = ctk.CTkLabel(
            frame,
            text=self._labels.search_results_header.format(n=len(results)),
            anchor="w",
            font=("", 14, "bold"),
            text_color=self._theme()["fg"],
        )
        header.pack(fill="x", padx=6, pady=(4, 8))

        if not results:
            empty = ctk.CTkLabel(
                frame,
                text=self._labels.no_matches,
                anchor="w",
                text_color=self._theme()["muted_fg"],
            )
            empty.pack(fill="x", padx=6, pady=8)
            frame.grid()
            return

        for field in results:
            section_id = self._find_section_of(field.key)
            section_label = next(
                (s.label for s in self._schema.sections if s.id == section_id), "?"
            )
            row = ctk.CTkButton(
                frame,
                text=f"{section_label}  ›  {field.label}",
                anchor="w",
                fg_color="transparent",
                hover_color=self._theme()["nav_item_hover"],
                text_color=self._theme()["fg"],
                command=lambda k=field.key: self.go_to_field(k),
            )
            row.pack(fill="x", padx=6, pady=1)

        frame.grid()
        self._refresh_nav_selection()

    def _find_section_of(self, field_key: str) -> str | None:
        for section in self._schema.sections:
            if field_key in section.field_keys:
                return section.id
            for group in section.groups:
                if field_key in group.field_keys:
                    return section.id
        return None

    # ------------------------------------------------------------------
    # Dirty tracking
    # ------------------------------------------------------------------

    def _schedule_dirty_refresh(self) -> None:
        if self._destroyed:
            return
        try:
            self.after(0, self._refresh_dirty)
        except Exception:
            pass

    def _refresh_dirty(self) -> None:
        if self._destroyed:
            return
        count = sum(1 for b in self._bindings.values() if b.is_dirty())
        changed = count != self._dirty_count
        self._dirty_count = count

        if count == 0:
            self._dirty_label.configure(
                text=self._labels.no_changes, text_color=self._theme()["muted_fg"]
            )
        elif count == 1:
            self._dirty_label.configure(
                text=self._labels.unsaved_one,
                text_color=self._theme()["fg"],
            )
        else:
            self._dirty_label.configure(
                text=self._labels.unsaved_other.format(n=count),
                text_color=self._theme()["fg"],
            )

        try:
            self._apply_btn.configure(state="normal" if count > 0 else "disabled")
            self._discard_btn.configure(state="normal" if count > 0 else "disabled")
        except Exception:
            pass

        if changed and self._on_dirty_change_cb is not None:
            try:
                self._on_dirty_change_cb(count > 0, count)
            except Exception:
                _log.exception("on_dirty_change callback raised")

    # ------------------------------------------------------------------
    # Apply / reset / reload
    # ------------------------------------------------------------------

    def _on_apply_click(self) -> None:
        if self._destroyed or self._dirty_count == 0:
            return

        # 1) Local validation.
        errors: dict[str, str] = {}
        for key, binding in self._bindings.items():
            err = binding.validate()
            binding.set_error(err)
            if err is not None:
                errors[key] = err
        if errors:
            self._show_toast(
                self._labels.apply_validation_error.format(n=len(errors)),
                kind="error",
            )
            self.go_to_field(next(iter(errors)))
            return

        # 2) Collect changes.
        changes: dict[str, Any] = {}
        for key, binding in self._bindings.items():
            if binding.is_dirty():
                changes[key] = binding.get_pending_value()
        if not changes:
            self._refresh_dirty()
            return

        # 3) Call the host.
        try:
            result = self._on_apply_cb(changes)
        except Exception as exc:
            _log.exception("on_apply callback raised")
            self._show_toast(f"{self._labels.apply_error}: {exc}", kind="error")
            return

        # 4) Handle errors.
        if not result.success:
            for key, msg in result.errors.items():
                binding = self._bindings.get(key)
                if binding is not None:
                    binding.set_error(msg)
            message = result.message or self._labels.apply_error
            self._show_toast(message, kind="error")
            if result.errors:
                self.go_to_field(next(iter(result.errors)))
            return

        # 5) Success — adopt new authoritative values.
        if result.new_values is not None:
            for key, new_value in result.new_values.items():
                self._values[key] = new_value
                binding = self._bindings.get(key)
                if binding is not None:
                    binding.set_value(new_value)
        else:
            for key, sent_value in changes.items():
                self._values[key] = sent_value
                binding = self._bindings.get(key)
                if binding is not None and binding.field.kind != FieldKind.SECRET:
                    # Sync originals by re-setting to same value (no-op change).
                    pass
                if binding is not None and binding.field.kind == FieldKind.SECRET:
                    # Secret now considered the new "original" state.
                    self._values[key] = bool(sent_value)

        # Rebuild internal originals.
        self._rebuild_originals()

        if result.requires_restart:
            self._restart_required = True
            self._refresh_restart_banner()
        self._show_toast(result.message or self._labels.apply_success, kind="success")
        self._refresh_dirty()

    def _rebuild_originals(self) -> None:
        """Recreate bindings on top of ``self._values`` to reset dirty state."""
        self._bindings.clear()
        for child in self._form_host.winfo_children():
            child.destroy()
        self._built_sections.clear()
        self._show_section(self._current_section_id)
        self._refresh_dirty()

    def _on_discard_click(self) -> None:
        if self._destroyed or self._dirty_count == 0:
            return
        self._bindings.clear()
        for child in self._form_host.winfo_children():
            child.destroy()
        self._built_sections.clear()
        self._show_section(self._current_section_id)
        self._refresh_dirty()

    def _on_reset_all_click(self) -> None:
        if self._destroyed:
            return
        for binding in self._bindings.values():
            if binding.field.readonly:
                continue
            binding.reset_to_default()
            binding.set_error(None)
        self._refresh_dirty()

    def _on_reset_field(self, field_key: str) -> None:
        binding = self._bindings.get(field_key)
        if binding is None or binding.field.readonly:
            return
        binding.reset_to_default()
        binding.set_error(None)
        self._refresh_dirty()

    def _on_reload_click(self) -> None:
        if self._destroyed:
            return
        if self._on_reload_cb is None:
            return
        if self._dirty_count > 0:
            # Simple confirmation via tkinter (kept dependency-free).
            from tkinter import messagebox

            ok = messagebox.askyesno(
                self._labels.reload,
                self._labels.unsaved_other.format(n=self._dirty_count),
                parent=self,
            )
            if not ok:
                return
        try:
            new_values = self._on_reload_cb()
        except Exception as exc:
            _log.exception("on_reload callback raised")
            self._show_toast(f"{self._labels.apply_error}: {exc}", kind="error")
            return
        self.set_current_values(new_values)

    def _on_restart_click(self) -> None:
        if self._destroyed or self._on_restart_cb is None:
            return
        try:
            self._on_restart_cb()
        except Exception:
            _log.exception("on_restart callback raised")

    # ------------------------------------------------------------------
    # Banner / toast
    # ------------------------------------------------------------------

    def _refresh_restart_banner(self) -> None:
        if not self._restart_required:
            self._banner.grid_remove()
            return
        theme = self._theme()
        try:
            self._banner.configure(fg_color=theme["banner_bg"], corner_radius=6)
            self._banner_label.configure(
                text_color=theme["banner_fg"],
                text=self._labels.restart_required,
            )
        except Exception:
            pass
        self._banner.grid()

    def _show_toast(self, message: str, *, kind: str = "info") -> None:
        theme = self._theme()
        if kind == "success":
            bg, fg = theme["success_bg"], theme["success_fg"]
        elif kind == "error":
            bg, fg = theme["banner_bg"], theme["error_fg"]
        else:
            bg, fg = theme["section_nav_bg"], theme["fg"]
        try:
            self._toast.configure(text=message, fg_color=bg, text_color=fg)
        except Exception:
            return
        self._toast.grid()
        if self._toast_after_id is not None:
            try:
                self.after_cancel(self._toast_after_id)
            except Exception:
                pass
        self._toast_after_id = self.after(_TOAST_MS, self._hide_toast)

    def _hide_toast(self) -> None:
        self._toast_after_id = None
        try:
            self._toast.grid_remove()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------

    def _current_mode(self) -> str:
        try:
            return ctk.get_appearance_mode()
        except Exception:
            return "Dark"

    def _theme(self) -> dict:
        return _THEME_LIGHT if self._last_theme_mode == "Light" else _THEME_DARK

    def _apply_theme(self) -> None:
        if self._destroyed:
            return
        theme = self._theme()
        try:
            self.configure(fg_color=theme["bg"])
        except Exception:
            pass
        try:
            self._nav.configure(fg_color=theme["section_nav_bg"])
        except Exception:
            pass
        self._refresh_nav_selection()
        # Rebuild surfaces that were already built for the old theme.
        self._bindings.clear()
        for child in self._form_host.winfo_children():
            child.destroy()
        self._built_sections.clear()
        self._show_section(self._current_section_id)
        self._refresh_dirty()
        self._refresh_restart_banner()

    def _schedule_theme_check(self) -> None:
        if self._destroyed:
            return
        self._theme_after_id = self.after(_THEME_POLL_MS, self._check_theme)

    def _check_theme(self) -> None:
        self._theme_after_id = None
        if self._destroyed:
            return
        current = self._current_mode()
        if current != self._last_theme_mode:
            self._last_theme_mode = current
            self._apply_theme()
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._destroyed = True
        for attr in ("_search_after_id", "_theme_after_id", "_toast_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
