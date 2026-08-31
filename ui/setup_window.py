"""First-run setup flow for Clipper."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from config import (
    REPLAY_BUFFER_SIZE_DEFAULT_MB,
    REPLAY_BUFFER_SIZE_MAX_MB,
    REPLAY_BUFFER_SIZE_MIN_MB,
)
from gi.repository import Adw, GLib, Gtk
from hotkeys import display_hotkey, effective_hotkey_label
from icon_names import APP_ICON
from language_row import create_language_row
from settings_view import (
    _FALLBACK_AUDIO_ENCODER_OPTIONS,
    _FALLBACK_FORMAT_OPTIONS,
    _FALLBACK_VIDEO_ENCODER_OPTIONS,
    _FPS_VALUES,
    InlineHotkeyCapture,
    _index_of,
    _primary_display_resolution,
    _resolution_options,
)

_CAPABILITY_RETRY_MS = 250
_CAPABILITY_RETRY_LIMIT = 40


def _resolution_label(value: str) -> str:
    return value.replace("x", " × ")


def _encoder_label(encoder: dict[str, Any]) -> str:
    name = str(encoder.get("name") or encoder.get("id") or _("Unknown encoder"))
    codec = encoder.get("codec")
    return f"{name} ({codec})" if codec else name


class SetupWindow(Adw.ApplicationWindow):
    """Native, multi-page first-run configuration window."""

    def __init__(
        self,
        *,
        application,
        config,
        engine_client=None,
        capabilities_requested_callback: Callable[[], bool] | None = None,
        capabilities_finished_callback: Callable[[], None] | None = None,
        hotkey_changed_callback: Callable[[str], bool] | None = None,
        hotkey_capture_state_callback: Callable[..., None] | None = None,
        display_target_callback: Callable[[Callable[[bool, str], None]], bool]
        | None = None,
        show_display_target_controls: bool = True,
        completed_callback: Callable[[], None] | None = None,
        language_changed_callback: Callable[[str, Any], None] | None = None,
    ) -> None:
        super().__init__(application=application, title=_("Set up Clipper"))
        self.set_default_size(760, 620)
        self.set_size_request(620, 500)

        self._config = config
        self._engine_client = engine_client
        self._capabilities_requested_callback = capabilities_requested_callback
        self._capabilities_finished_callback = capabilities_finished_callback
        self._hotkey_changed_callback = hotkey_changed_callback
        self._hotkey_capture_state_callback = hotkey_capture_state_callback
        self._display_target_callback = display_target_callback
        self._show_display_target_controls = bool(show_display_target_controls)
        self._completed_callback = completed_callback
        self._language_changed_callback = language_changed_callback
        self._pages: list[Gtk.Widget] = []
        self._page_index = 0
        self._suppress_signals = False
        self._capability_probe_requested = False
        self._capability_retry_id: int | None = None
        self._capability_retry_count = 0
        self._display_request_active = False

        self._resolution_values = _resolution_options(
            self._config.get("resolution", "1920x1080"),
            _primary_display_resolution(),
        )
        self._format_values = [value for _, value in _FALLBACK_FORMAT_OPTIONS]
        self._video_encoder_values = [
            value for _, value in _FALLBACK_VIDEO_ENCODER_OPTIONS
        ]
        self._audio_encoder_values = [
            value for _, value in _FALLBACK_AUDIO_ENCODER_OPTIONS
        ]

        self._build_ui()
        self.connect("close-request", self._on_close_request)
        self._request_capabilities()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Set up Clipper"), subtitle=""))
        root.append(header)

        # Gtk.Stack guarantees that only the active setup page is allocated and
        # interactive. A carousel can reveal adjacent fixed-width children when
        # its window is made very wide, which is unsuitable for a setup wizard.
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._stack.set_transition_duration(250)
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        self._append_page(self._create_welcome_page())
        self._append_page(self._create_clips_page())
        self._append_page(self._create_encoding_page())
        if self._show_display_target_controls:
            self._append_page(self._create_display_page())
        self._append_page(self._create_complete_page())
        root.append(self._stack)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        footer.set_margin_start(18)
        footer.set_margin_end(18)
        footer.set_margin_top(10)
        footer.set_margin_bottom(18)

        self._step_label = Gtk.Label(
            label=_("Step %(current)d of %(total)d")
            % {"current": 1, "total": len(self._pages)}
        )
        self._step_label.add_css_class("caption")
        self._step_label.add_css_class("dim-label")
        footer.append(self._step_label)

        step_dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        step_dots.set_halign(Gtk.Align.CENTER)
        self._step_dots: list[Gtk.Label] = []
        for _page in self._pages:
            dot = Gtk.Label(label="●")
            dot.add_css_class("dim-label")
            self._step_dots.append(dot)
            step_dots.append(dot)
        footer.append(step_dots)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._skip_button = Gtk.Button(label=_("Skip setup"))
        self._skip_button.connect("clicked", self._on_finish_clicked)
        actions.append(self._skip_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)

        self._back_button = Gtk.Button(label=_("Back"))
        self._back_button.set_sensitive(False)
        self._back_button.connect("clicked", self._on_back_clicked)
        actions.append(self._back_button)

        self._next_button = Gtk.Button(label=_("Next"))
        self._next_button.add_css_class("suggested-action")
        self._next_button.connect("clicked", self._on_next_clicked)
        actions.append(self._next_button)
        footer.append(actions)

        root.append(Gtk.Separator())
        root.append(footer)
        self.set_content(root)
        self._show_page(0, animate=False)

    def _append_page(self, page: Gtk.Widget) -> None:
        self._pages.append(page)
        self._stack.add_named(page, f"setup-step-{len(self._pages)}")

    def _create_welcome_page(self) -> Gtk.Widget:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(620)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_start(36)
        content.set_margin_end(36)
        content.set_margin_top(48)
        content.set_margin_bottom(36)
        content.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name(APP_ICON)
        icon.set_pixel_size(112)
        icon.set_halign(Gtk.Align.CENTER)
        content.append(icon)

        title = Gtk.Label(label=_("Welcome to Clipper"))
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        content.append(title)

        if self._show_display_target_controls:
            setup_description = _(
                "Set up clip length, quality, codecs, your save shortcut, and "
                "the display used for capture. Every choice can be changed later."
            )
        else:
            setup_description = _(
                "Set up clip length, quality, codecs, and your save shortcut. "
                "Every choice can be changed later."
            )
        description = Gtk.Label(
            label=setup_description
        )
        description.add_css_class("dim-label")
        description.set_halign(Gtk.Align.CENTER)
        description.set_justify(Gtk.Justification.CENTER)
        description.set_wrap(True)
        description.set_max_width_chars(58)
        content.append(description)

        if self._language_changed_callback is not None:
            language_changed_callback = self._language_changed_callback
            language_group = Adw.PreferencesGroup()
            self._language_row = create_language_row(
                self._config,
                lambda language: language_changed_callback(language, self),
            )
            language_group.add(self._language_row)
            content.append(language_group)

        clamp.set_child(content)
        return clamp

    def _page_scaffold(
        self, title_text: str, description_text: str
    ) -> tuple[Gtk.Widget, Gtk.Box]:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(680)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(30)
        content.set_margin_bottom(30)

        title = Gtk.Label(label=title_text)
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.START)
        content.append(title)

        description = Gtk.Label(label=description_text)
        description.add_css_class("dim-label")
        description.set_halign(Gtk.Align.START)
        description.set_wrap(True)
        description.set_max_width_chars(72)
        content.append(description)

        clamp.set_child(content)
        scrolled.set_child(clamp)
        return scrolled, content

    def _create_clips_page(self) -> Gtk.Widget:
        page, content = self._page_scaffold(
            _("Clips and shortcut"),
            _("Choose the clip length and how to save it."),
        )
        group = Adw.PreferencesGroup()

        length_row = Adw.ActionRow()
        length_row.set_title(_("Clip length"))
        length_row.set_subtitle(_("Clip length in seconds"))
        adjustment = Gtk.Adjustment.new(
            float(self._config.get("replay_buffer_length", 60)),
            5,
            600,
            5,
            30,
            0,
        )
        self._clip_spin = Gtk.SpinButton(adjustment=adjustment, digits=0)
        self._clip_spin.set_valign(Gtk.Align.CENTER)
        self._clip_spin.set_numeric(True)
        self._clip_spin.connect("value-changed", self._on_clip_length_changed)
        length_row.add_suffix(self._clip_spin)
        length_row.set_activatable_widget(self._clip_spin)
        group.add(length_row)

        buffer_size_row = Adw.ActionRow()
        buffer_size_row.set_title(_("File size limit"))
        buffer_size_row.set_subtitle(_("Maximum size retained for each clip"))
        buffer_size_adjustment = Gtk.Adjustment.new(
            float(
                self._config.get(
                    "replay_buffer_size_mb", REPLAY_BUFFER_SIZE_DEFAULT_MB
                )
            ),
            REPLAY_BUFFER_SIZE_MIN_MB,
            REPLAY_BUFFER_SIZE_MAX_MB,
            128,
            1024,
            0,
        )
        self._buffer_size_spin = Gtk.SpinButton(
            adjustment=buffer_size_adjustment, digits=0
        )
        self._buffer_size_spin.set_valign(Gtk.Align.CENTER)
        self._buffer_size_spin.set_numeric(True)
        self._buffer_size_spin.connect(
            "value-changed", self._on_buffer_size_changed
        )
        buffer_size_row.add_suffix(self._buffer_size_spin)

        mebibytes_label = Gtk.Label(label=_("MiB"))
        mebibytes_label.add_css_class("dim-label")
        mebibytes_label.set_valign(Gtk.Align.CENTER)
        buffer_size_row.add_suffix(mebibytes_label)
        buffer_size_row.set_activatable_widget(self._buffer_size_spin)
        group.add(buffer_size_row)

        self._hotkey_row = Adw.ActionRow()
        self._hotkey_row.set_title(_("Save shortcut"))
        self._hotkey_row.set_subtitle(self._configured_hotkey_subtitle())
        self._hotkey_button = Gtk.Button(
            label=effective_hotkey_label(
                self._config.get("save_hotkey", ""),
                portal_managed=bool(
                    self._config.get("save_hotkey_portal_managed", False)
                ),
                portal_label=self._config.get("save_hotkey_portal_label", ""),
            )
        )
        self._hotkey_button.set_valign(Gtk.Align.CENTER)
        self._hotkey_button.connect("clicked", self._on_hotkey_clicked)
        self._hotkey_capture = InlineHotkeyCapture(
            self._hotkey_button,
            self._hotkey_row,
            self._on_hotkey_selected,
            self.refresh_save_hotkey_from_config,
            self._configured_hotkey_subtitle,
            self._hotkey_capture_state_callback,
        )
        self._hotkey_row.add_suffix(self._hotkey_button)
        self._hotkey_row.set_activatable_widget(self._hotkey_button)
        group.add(self._hotkey_row)

        content.append(group)
        return page

    def _create_encoding_page(self) -> Gtk.Widget:
        page, content = self._page_scaffold(
            _("Video and audio"),
            _("Set the output size, frame rate, container, and encoders used for clips."),
        )
        group = Adw.PreferencesGroup()

        self._resolution_row = self._combo_row(
            group,
            _("Resolution"),
            _("Output dimensions"),
            [_resolution_label(value) for value in self._resolution_values],
            self._resolution_values,
            "resolution",
            "1920x1080",
        )
        self._fps_row = self._combo_row(
            group,
            _("Frame rate"),
            _("Frames recorded each second"),
            [_("%(fps)d FPS") % {"fps": value} for value in _FPS_VALUES],
            _FPS_VALUES,
            "fps",
            60,
        )
        self._format_row = self._combo_row(
            group,
            _("Container"),
            _("File format used when a clip is saved"),
            [label for label, _ in _FALLBACK_FORMAT_OPTIONS],
            self._format_values,
            "format",
            "mkv",
        )
        self._video_encoder_row = self._combo_row(
            group,
            _("Video codec"),
            _("Encoder used for the picture"),
            [label for label, _ in _FALLBACK_VIDEO_ENCODER_OPTIONS],
            self._video_encoder_values,
            "video_encoder",
            "obs_x264",
        )
        self._audio_encoder_row = self._combo_row(
            group,
            _("Audio codec"),
            _("Encoder used for sound"),
            [label for label, _ in _FALLBACK_AUDIO_ENCODER_OPTIONS],
            self._audio_encoder_values,
            "audio_encoder",
            "ffmpeg_aac",
        )
        content.append(group)
        return page

    def _combo_row(
        self,
        group,
        title: str,
        subtitle: str,
        labels: list[str],
        values: list[Any],
        config_key: str,
        default_value: Any,
    ):
        row = Adw.ComboRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_model(Gtk.StringList.new(labels))
        row.set_selected(
            _index_of(values, self._config.get(config_key, default_value))
        )
        row.connect(
            "notify::selected",
            self._on_combo_changed,
            config_key,
            values,
        )
        group.add(row)
        return row

    def _create_display_page(self) -> Gtk.Widget:
        page, content = self._page_scaffold(
            _("Capture display"),
            _(
                "Choose the primary display, or the display where your games will appear. "
                "The system picker controls this permission."
            ),
        )
        group = Adw.PreferencesGroup()
        display_row = Adw.ActionRow()
        display_row.set_title(_("Target capture display"))
        display_row.set_subtitle(_("Opens the system display picker"))
        self._display_button = Gtk.Button(label=_("Pick display…"))
        self._display_button.add_css_class("suggested-action")
        self._display_button.set_valign(Gtk.Align.CENTER)
        self._display_button.connect("clicked", self._on_display_clicked)
        display_row.add_suffix(self._display_button)
        display_row.set_activatable_widget(self._display_button)
        group.add(display_row)
        content.append(group)

        self._display_status = Gtk.Label(label="")
        self._display_status.add_css_class("dim-label")
        self._display_status.set_halign(Gtk.Align.START)
        self._display_status.set_wrap(True)
        self._display_status.set_visible(False)
        content.append(self._display_status)
        return page

    def _create_complete_page(self) -> Gtk.Widget:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(620)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_start(36)
        content.set_margin_end(36)
        content.set_margin_top(48)
        content.set_margin_bottom(36)
        content.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name(APP_ICON)
        icon.set_pixel_size(112)
        icon.set_halign(Gtk.Align.CENTER)
        content.append(icon)

        title = Gtk.Label(label=_("All set!"))
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        content.append(title)

        description = Gtk.Label(
            label=_(
                "Clipper is ready to save your recent gameplay. You can change "
                "these settings at any time."
            )
        )
        description.add_css_class("dim-label")
        description.set_halign(Gtk.Align.CENTER)
        description.set_justify(Gtk.Justification.CENTER)
        description.set_wrap(True)
        description.set_max_width_chars(58)
        content.append(description)

        clamp.set_child(content)
        return clamp

    def _on_clip_length_changed(self, spin) -> None:
        if not self._suppress_signals:
            self._config.set("replay_buffer_length", int(spin.get_value()))

    def _on_buffer_size_changed(self, spin) -> None:
        if not self._suppress_signals:
            self._config.set("replay_buffer_size_mb", int(spin.get_value()))

    def _on_combo_changed(self, row, _param, config_key: str, values: list[Any]) -> None:
        if self._suppress_signals:
            return
        selected = int(row.get_selected())
        if 0 <= selected < len(values):
            self._config.set(config_key, values[selected])

    def _on_hotkey_clicked(self, _button) -> None:
        self._hotkey_capture.start()

    def _on_hotkey_selected(self, hotkey: str) -> bool:
        if self._hotkey_changed_callback:
            pending = bool(self._hotkey_changed_callback(hotkey))
            if not pending:
                self._hotkey_button.set_label(display_hotkey(hotkey))
            return pending
        else:
            self._config.set("save_hotkey", hotkey)
            self._hotkey_button.set_label(display_hotkey(hotkey))
            return False

    def _configured_hotkey_subtitle(self) -> str:
        if self._config.get("save_hotkey_portal_managed", False):
            return _("Managed by your desktop's global shortcut settings")
        return _("Your desktop may ask you to approve or change this shortcut")

    def set_save_hotkey_from_portal(self, hotkey: str | None) -> None:
        """Reflect the shortcut actually selected in the system editor."""
        self._hotkey_button.set_label(
            effective_hotkey_label("", portal_managed=True, portal_label=hotkey)
        )
        self._hotkey_row.set_subtitle(
            _("Managed by your desktop's global shortcut settings")
        )

    def refresh_save_hotkey_from_config(self) -> None:
        """Restore the effective label after a rejected binding request."""
        self._hotkey_button.set_label(
            effective_hotkey_label(
                self._config.get("save_hotkey", ""),
                portal_managed=bool(
                    self._config.get("save_hotkey_portal_managed", False)
                ),
                portal_label=self._config.get("save_hotkey_portal_label", ""),
            )
        )
        self._hotkey_row.set_subtitle(self._configured_hotkey_subtitle())

    def _on_display_clicked(self, _button) -> None:
        if self._display_target_callback is None or self._display_request_active:
            return
        self._display_request_active = True
        self._display_button.set_sensitive(False)
        self._display_status.set_visible(True)
        self._display_status.set_label(_("Waiting for the system display picker…"))
        if not self._display_target_callback(self._on_display_target_result):
            if self._display_request_active:
                self._on_display_target_result(
                    False, "Could not open the system display picker."
                )

    def _on_display_target_result(self, success: bool, message: str) -> None:
        self._display_request_active = False
        self._display_button.set_sensitive(True)
        self._display_status.set_visible(True)
        self._display_status.set_label(message)
        if success:
            self._display_status.remove_css_class("error")
            self._display_status.add_css_class("success")
            self._show_page(len(self._pages) - 1)
        else:
            self._display_status.remove_css_class("success")
            self._display_status.add_css_class("error")

    def _show_page(self, index: int, *, animate: bool = True) -> None:
        previous_index = self._page_index
        self._page_index = max(0, min(int(index), len(self._pages) - 1))
        if animate:
            transition = (
                Gtk.StackTransitionType.SLIDE_LEFT
                if self._page_index > previous_index
                else Gtk.StackTransitionType.SLIDE_RIGHT
            )
        else:
            transition = Gtk.StackTransitionType.NONE
        self._stack.set_transition_type(transition)
        self._stack.set_visible_child(self._pages[self._page_index])

        page_count = len(self._pages)
        self._step_label.set_label(
            _("Step %(current)d of %(total)d")
            % {"current": self._page_index + 1, "total": page_count}
        )
        for index, dot in enumerate(self._step_dots):
            if index == self._page_index:
                dot.remove_css_class("dim-label")
            else:
                dot.add_css_class("dim-label")
        self._back_button.set_sensitive(self._page_index > 0)
        last_page = self._page_index == page_count - 1
        self._skip_button.set_visible(not last_page)
        self._next_button.set_label(_("Start using Clipper") if last_page else _("Next"))

    def _on_back_clicked(self, _button) -> None:
        if self._page_index > 0:
            self._show_page(self._page_index - 1)

    def _on_next_clicked(self, _button) -> None:
        if self._page_index >= len(self._pages) - 1:
            self._finish_setup()
            return
        self._show_page(self._page_index + 1)

    def _on_finish_clicked(self, _button) -> None:
        self._config.set("save_hotkey", "")
        self._hotkey_button.set_label(_("Not set"))
        if self._hotkey_changed_callback:
            self._hotkey_changed_callback("")
        self._finish_setup()

    def _finish_setup(self) -> None:
        self._config.set("setup_completed", True)
        self._config.set("display_capture_guidance_seen", True)
        self.cleanup()
        if self._completed_callback:
            self._completed_callback()

    def _request_capabilities(self) -> None:
        if self._capability_probe_requested:
            return
        self._capability_probe_requested = True
        self._capability_retry_count = 0
        if self._engine_client and self._engine_client.is_connected():
            self._fetch_capabilities()
            return
        if (
            self._capabilities_requested_callback is None
            or not self._capabilities_requested_callback()
        ):
            self._finish_capability_probe()
            return
        self._capability_retry_id = GLib.timeout_add(
            _CAPABILITY_RETRY_MS, self._retry_capabilities
        )

    def _retry_capabilities(self) -> bool:
        if not self._capability_probe_requested:
            self._capability_retry_id = None
            return False
        self._capability_retry_count += 1
        if self._engine_client and self._engine_client.is_connected():
            self._capability_retry_id = None
            self._fetch_capabilities()
            return False
        if self._capability_retry_count >= _CAPABILITY_RETRY_LIMIT:
            self._capability_retry_id = None
            self._finish_capability_probe()
            return False
        return True

    def _fetch_capabilities(self) -> None:
        if not self._engine_client or not self._engine_client.get_capabilities(
            self._on_capabilities
        ):
            self._finish_capability_probe()

    def _on_capabilities(self, response: dict[str, Any]) -> None:
        if response.get("ok"):
            video_encoders = [
                encoder
                for encoder in response.get("video_encoders", [])
                if isinstance(encoder, dict) and encoder.get("id")
            ]
            audio_encoders = [
                encoder
                for encoder in response.get("audio_encoders", [])
                if isinstance(encoder, dict) and encoder.get("id")
            ]
            if video_encoders:
                # ComboRow's signal carries this list object as user data.
                # Keep its identity stable when replacing fallback options.
                self._video_encoder_values[:] = [
                    str(encoder["id"]) for encoder in video_encoders
                ]
                self._replace_combo_options(
                    self._video_encoder_row,
                    [_encoder_label(encoder) for encoder in video_encoders],
                    self._video_encoder_values,
                    "video_encoder",
                    "obs_x264",
                )
            if audio_encoders:
                self._audio_encoder_values[:] = [
                    str(encoder["id"]) for encoder in audio_encoders
                ]
                self._replace_combo_options(
                    self._audio_encoder_row,
                    [_encoder_label(encoder) for encoder in audio_encoders],
                    self._audio_encoder_values,
                    "audio_encoder",
                    "ffmpeg_aac",
                )
        self._finish_capability_probe()

    def _replace_combo_options(
        self,
        row,
        labels: list[str],
        values: list[str],
        config_key: str,
        default_value: str,
    ) -> None:
        selected_value = self._config.get(config_key, default_value)
        self._suppress_signals = True
        try:
            row.set_model(Gtk.StringList.new(labels))
            row.set_selected(_index_of(values, selected_value))
        finally:
            self._suppress_signals = False

    def _finish_capability_probe(self) -> None:
        if not self._capability_probe_requested:
            return
        self._capability_probe_requested = False
        if self._capabilities_finished_callback:
            self._capabilities_finished_callback()

    def cleanup(self) -> None:
        if self._capability_retry_id is not None:
            GLib.source_remove(self._capability_retry_id)
            self._capability_retry_id = None
        self._finish_capability_probe()

    def _on_close_request(self, _window) -> bool:
        self.cleanup()
        return False
