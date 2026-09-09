"""
Settings view - application configuration
"""

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from config import (
    OUTPUT_FORMATS,
    REPLAY_BUFFER_SIZE_DEFAULT_MB,
    REPLAY_BUFFER_SIZE_MAX_MB,
    REPLAY_BUFFER_SIZE_MIN_MB,
)
from engine_client import STATE_CONNECTED, STATE_DISCONNECTED
from gi.repository import Adw, Gdk, GLib, Gtk
from hotkeys import (
    HotkeyError,
    display_hotkey,
    effective_hotkey_label,
    hotkey_from_gdk_event,
)
from language_row import create_language_row
from quality_controls import CBR_BITRATE_SUBTITLE as _CBR_BITRATE_SUBTITLE
from quality_controls import CBR_BITRATE_TITLE as _CBR_BITRATE_TITLE
from quality_controls import (
    CQP_DEFAULT as _CQP_DEFAULT,
)
from quality_controls import (
    CQP_HIGH_QUALITY as _CQP_HIGH_QUALITY,
)
from quality_controls import (
    CQP_LOW_QUALITY as _CQP_LOW_QUALITY,
)
from quality_controls import (
    CQP_MEDIUM_QUALITY as _CQP_MEDIUM_QUALITY,
)
from quality_controls import MAX_BITRATE_SUBTITLE as _MAX_BITRATE_SUBTITLE
from quality_controls import MAX_BITRATE_TITLE as _MAX_BITRATE_TITLE
from quality_controls import QUALITY_SUBTITLE as _QUALITY_SUBTITLE
from quality_controls import RATE_CONTROL_OPTIONS as _RATE_CONTROL_OPTIONS
from quality_controls import RATE_CONTROL_SUBTITLE as _RATE_CONTROL_SUBTITLE
from quality_controls import VBR_BITRATE_SUBTITLE as _VBR_BITRATE_SUBTITLE
from quality_controls import VBR_BITRATE_TITLE as _VBR_BITRATE_TITLE
from quality_controls import (
    cqp_to_slider_value as _cqp_to_slider_value,
)
from quality_controls import (
    slider_value_to_cqp as _slider_value_to_cqp,
)
from text_helpers import middle_truncate_text

# Map combo index → config value (must stay in sync with StringList items)
_FPS_VALUES = [30, 60]
_RESOLUTION_VALUES = ["1280x720", "1920x1080", "2560x1440", "3840x2160"]
_FALLBACK_FORMAT_OPTIONS = [
    (_("Matroska video (.mkv)"), "mkv"),
    (_("MPEG-4 (.mp4)"), "mp4"),
    (_("QuickTime (.mov)"), "mov"),
    (_("MPEG-TS (.ts)"), "ts"),
]
_FALLBACK_VIDEO_ENCODER_OPTIONS = [
    (_("x264"), "obs_x264"),
    (_("FFmpeg VAAPI"), "ffmpeg_vaapi"),
]
_FALLBACK_AUDIO_ENCODER_OPTIONS = [
    (_("FFmpeg AAC"), "ffmpeg_aac"),
    (_("FFmpeg Opus"), "ffmpeg_opus"),
    (_("FFmpeg FLAC"), "ffmpeg_flac"),
]
_FALLBACK_VAAPI_DEVICE_OPTIONS = [
    (_("Automatic"), "auto"),
    ("/dev/dri/renderD128", "/dev/dri/renderD128"),
]
_FORMAT_VALUES = [value for _, value in _FALLBACK_FORMAT_OPTIONS]
_VIDEO_ENCODER_VALUES = [value for _, value in _FALLBACK_VIDEO_ENCODER_OPTIONS]
_AUDIO_ENCODER_VALUES = [value for _, value in _FALLBACK_AUDIO_ENCODER_OPTIONS]
_VAAPI_DEVICE_VALUES = [value for _, value in _FALLBACK_VAAPI_DEVICE_OPTIONS]
_RATE_CONTROL_VALUES = [value for _, value in _RATE_CONTROL_OPTIONS]
_CAPABILITY_LOADING_LABEL = _("Detecting…")
_CAPABILITY_RESPONSE_RETRY_LIMIT = 3
_CAPABILITY_RESPONSE_RETRY_DELAY_MS = 250
_BITRATE_MIN = 1000
_BITRATE_MEDIUM = 12000
_BITRATE_HIGH = 20000
_BITRATE_MAX = 50000
_BITRATE_STEP = 500
_BITRATE_PAGE = 2000
_ROW_DYNAMIC_TEXT_MAX_CHARS = 80
_HOTKEY_MODIFIER_ORDER = ("ctrl", "alt", "shift", "super")
_HOTKEY_MODIFIER_LABELS = {
    "ctrl": _("Ctrl"),
    "alt": _("Alt"),
    "shift": _("Shift"),
    "super": _("Super"),
}
_HOTKEY_MODIFIER_KEY_NAMES = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "ISO_Level3_Shift": "alt",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Super_L": "super",
    "Super_R": "super",
    "Meta_L": "super",
    "Meta_R": "super",
    "Hyper_L": "super",
    "Hyper_R": "super",
}


def _index_of(lst, value, default=0):
    """Return first index of *value* in *lst*, or *default* if not found."""
    try:
        return lst.index(value)
    except ValueError:
        return default


def _vaapi_device_label(device):
    """Translate the synthetic automatic option but preserve device names."""
    if device.get("id") == "auto":
        return _("Automatic")
    return device.get("name") or device["id"]


def _parse_resolution(value):
    if not isinstance(value, str) or "x" not in value:
        return None

    width, height = value.split("x", 1)
    try:
        width = int(width)
        height = int(height)
    except ValueError:
        return None

    if width <= 0 or height <= 0:
        return None

    return width, height


def _format_resolution(width, height):
    return f"{int(width)}x{int(height)}"


def _resolution_sort_key(value):
    parsed = _parse_resolution(value)
    if parsed is None:
        return (10**9, 10**9)
    width, height = parsed
    return (height, width)


def _monitor_scale(monitor):
    for method_name in ("get_scale", "get_scale_factor"):
        method = getattr(monitor, method_name, None)
        if method is None:
            continue
        try:
            scale = float(method())
        except (TypeError, ValueError):
            continue
        if scale > 0:
            return scale
    return 1.0


def _monitor_resolution(monitor):
    geometry_method = getattr(monitor, "get_geometry", None)
    if geometry_method is None:
        return None

    geometry = geometry_method()
    width = getattr(geometry, "width", None)
    height = getattr(geometry, "height", None)
    if width is None or height is None:
        return None

    scale = _monitor_scale(monitor)
    width = round(int(width) * scale)
    height = round(int(height) * scale)
    if width <= 0 or height <= 0:
        return None

    return _format_resolution(width, height)


def _iter_display_monitors(display):
    monitors_method = getattr(display, "get_monitors", None)
    if monitors_method is None:
        return []

    monitors = monitors_method()
    if monitors is None:
        return []
    if isinstance(monitors, (list, tuple)):
        return list(monitors)

    item_count = getattr(monitors, "get_n_items", None)
    get_item = getattr(monitors, "get_item", None)
    if item_count is None or get_item is None:
        return []

    return [get_item(index) for index in range(item_count())]


def _primary_monitor(display):
    primary_method = getattr(display, "get_primary_monitor", None)
    if primary_method is not None:
        primary = primary_method()
        if primary is not None:
            return primary

    monitors = _iter_display_monitors(display)
    for monitor in monitors:
        is_primary = getattr(monitor, "is_primary", None)
        if is_primary is not None and is_primary():
            return monitor

    return monitors[0] if monitors else None


def _primary_display_resolution(display=None):
    if display is None:
        display_class = getattr(Gdk, "Display", None)
        get_default = getattr(display_class, "get_default", None)
        if get_default is None:
            return None
        display = get_default()

    if display is None:
        return None

    monitor = _primary_monitor(display)
    if monitor is None:
        return None

    return _monitor_resolution(monitor)


def _resolution_options(configured_resolution=None, detected_resolution=None):
    options = list(_RESOLUTION_VALUES)
    for resolution in (detected_resolution, configured_resolution):
        if (
            resolution is not None
            and _parse_resolution(resolution) is not None
            and resolution not in options
        ):
            options.append(resolution)
    return sorted(options, key=_resolution_sort_key)


def _set_widget_tooltip(widget, text: str | None) -> None:
    setter = getattr(widget, "set_tooltip_text", None)
    if setter is not None:
        setter(text or None)


class InlineHotkeyCapture:
    """Capture a shortcut directly in an action-row button."""

    def __init__(
        self,
        button,
        row,
        selected_callback,
        restore_display_callback,
        restore_subtitle_callback,
        capture_state_callback=None,
    ) -> None:
        self._button = button
        self._row = row
        self._selected_callback = selected_callback
        self._restore_display_callback = restore_display_callback
        self._restore_subtitle_callback = restore_subtitle_callback
        self._capture_state_callback = capture_state_callback
        self._active = False
        self._pressed_modifiers: set[str] = set()

        key_controller = Gtk.EventControllerKey.new()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        key_controller.connect("key-released", self._on_key_released)
        button.add_controller(key_controller)

        focus_controller = Gtk.EventControllerFocus.new()
        focus_controller.connect("leave", self._on_focus_left)
        button.add_controller(focus_controller)

    def start(self) -> None:
        if self._active:
            return

        self._active = True
        self._pressed_modifiers.clear()
        self._button.set_label(_("Press shortcut"))
        self._row.set_subtitle(
            _("Press a shortcut · Backspace clears · Esc cancels")
        )
        self._button.grab_focus()
        if self._capture_state_callback:
            self._capture_state_callback(True, self._accept_current_hotkey)

    def _on_key_pressed(self, _controller, keyval, keycode, state):
        if not self._active:
            return False

        key_name = Gdk.keyval_name(keyval) or ""
        if key_name == "Escape":
            self._finish(restore_display=True)
            return True

        modifiers = self._modifiers_from_state(state)
        modifier = _HOTKEY_MODIFIER_KEY_NAMES.get(key_name)
        if modifier:
            modifiers.add(modifier)
            self._pressed_modifiers = modifiers
            self._show_pressed_modifiers()
            return True

        self._pressed_modifiers = modifiers
        if key_name in {"BackSpace", "Delete"} and not (
            modifiers - {"shift"}
        ):
            pending = self._selected_callback("")
            self._finish(restore_display=bool(pending))
            return True

        self._show_pressed_key(keyval, key_name, state)
        try:
            hotkey = hotkey_from_gdk_event(keyval, state, keycode)
        except HotkeyError as exc:
            self._row.set_subtitle(str(exc))
            return True

        if hotkey is None:
            self._show_pressed_modifiers()
            return True

        self._button.set_label(hotkey.display)
        pending = self._selected_callback(hotkey.canonical)
        self._finish(restore_display=bool(pending))
        return True

    def _on_key_released(self, _controller, keyval, _keycode, state):
        if not self._active:
            return

        modifiers = self._modifiers_from_state(state)
        key_name = Gdk.keyval_name(keyval) or ""
        released = _HOTKEY_MODIFIER_KEY_NAMES.get(key_name)
        if released:
            modifiers.discard(released)
        self._pressed_modifiers = modifiers
        self._show_pressed_modifiers()

    def _on_focus_left(self, _controller) -> None:
        if self._active:
            self._finish(restore_display=True)

    def _modifiers_from_state(self, state) -> set[str]:
        modifiers = set()
        modifier_type = Gdk.ModifierType
        if state & modifier_type.CONTROL_MASK:
            modifiers.add("ctrl")
        if state & modifier_type.ALT_MASK:
            modifiers.add("alt")
        if state & modifier_type.SHIFT_MASK:
            modifiers.add("shift")
        if state & modifier_type.SUPER_MASK or state & modifier_type.META_MASK:
            modifiers.add("super")
        return modifiers

    def _show_pressed_modifiers(self) -> None:
        labels = [
            _HOTKEY_MODIFIER_LABELS[modifier]
            for modifier in _HOTKEY_MODIFIER_ORDER
            if modifier in self._pressed_modifiers
        ]
        self._button.set_label("+".join(labels) or _("Press shortcut"))

    def _show_pressed_key(self, keyval, key_name: str, state) -> None:
        accelerator_label = getattr(Gtk, "accelerator_get_label", None)
        label = accelerator_label(keyval, state) if accelerator_label else ""
        if not label:
            parts = [
                _HOTKEY_MODIFIER_LABELS[modifier]
                for modifier in _HOTKEY_MODIFIER_ORDER
                if modifier in self._pressed_modifiers
            ]
            key_label = key_name.upper() if len(key_name) == 1 else key_name
            if key_label:
                parts.append(key_label.replace("_", " "))
            label = "+".join(parts)
        self._button.set_label(label or _("Press shortcut"))

    def _accept_current_hotkey(self) -> None:
        """Finish when Wayland reports the already-bound global shortcut."""
        if self._active:
            self._finish(restore_display=True)

    def _finish(self, *, restore_display: bool) -> None:
        if not self._active:
            return

        self._active = False
        self._pressed_modifiers.clear()
        if restore_display:
            self._restore_display_callback()
        self._row.set_subtitle(self._restore_subtitle_callback())
        if self._capture_state_callback:
            self._capture_state_callback(False, None)


class SettingsView(Gtk.Box):
    """View for application settings"""

    def __init__(
        self,
        config=None,
        engine_client=None,
        output_folder_changed_callback=None,
        hotkey_changed_callback=None,
        hotkey_capture_state_callback=None,
        capabilities_requested_callback=None,
        capabilities_finished_callback=None,
        engine_restart_required_callback=None,
        start_on_boot_changed_callback=None,
        display_target_change_callback=None,
        show_display_target_controls=True,
        tray_available_callback=None,
        capabilities_cache=None,
        language_changed_callback=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._config = config
        self._engine_client = engine_client
        self._output_folder_changed_callback = output_folder_changed_callback
        self._hotkey_changed_callback = hotkey_changed_callback
        self._hotkey_capture_state_callback = hotkey_capture_state_callback
        self._capabilities_requested_callback = capabilities_requested_callback
        self._capabilities_finished_callback = capabilities_finished_callback
        self._engine_restart_required_callback = engine_restart_required_callback
        self._start_on_boot_changed_callback = start_on_boot_changed_callback
        self._display_target_change_callback = display_target_change_callback
        self._show_display_target_controls = bool(show_display_target_controls)
        self._tray_available_callback = tray_available_callback
        self._language_changed_callback = language_changed_callback
        self._suppress_signals = False  # guard against feedback loops on load
        self._start_on_boot_request_pending = False
        self._pending_slider_keys = set()
        self._restart_needed = False
        self._expecting_restart = False  # Track if we're expecting an engine restart
        self._capability_start_requested = False
        self._capability_retry_id = None
        self._capability_retry_count = 0
        self._capability_request_in_flight = False
        self._capability_response_retry_count = 0
        self._capabilities_cache = (
            capabilities_cache if capabilities_cache is not None else {}
        )
        self._format_values = list(_FORMAT_VALUES)
        self._video_encoder_values = list(_VIDEO_ENCODER_VALUES)
        self._audio_encoder_values = list(_AUDIO_ENCODER_VALUES)
        self._vaapi_device_values = list(_VAAPI_DEVICE_VALUES)
        configured_resolution = self._config.get("resolution") if self._config else None
        self._resolution_values = _resolution_options(
            configured_resolution, _primary_display_resolution()
        )

        # Register state change callback for reconnection handling
        if self._engine_client:
            self._engine_client.on_state_change(self._on_engine_state_change)

        self.setup_ui()

    def setup_ui(self):
        """Build the settings view UI"""
        # Restart warning banner (initially hidden)
        self._restart_banner = Adw.Banner()
        self._restart_banner.set_title(_("Engine restart required for changes to take effect"))
        self._restart_banner.set_button_label(_("Restart engine"))
        self._restart_banner.connect("button-clicked", self.on_restart_engine)
        self._restart_banner.set_revealed(False)
        self.append(self._restart_banner)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_margin_start(6)
        content_box.set_margin_end(6)
        content_box.set_margin_top(6)
        content_box.set_margin_bottom(6)
        content_box.set_vexpand(True)
        self.append(content_box)

        # Scrolled window for settings
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Settings container
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        settings_box.set_margin_start(6)
        settings_box.set_margin_end(6)
        settings_box.set_margin_top(6)
        settings_box.set_margin_bottom(6)
        scrolled.set_child(settings_box)

        # Recording settings group
        recording_group = self.create_recording_settings()
        settings_box.append(recording_group)

        # Saving settings group (output location and hotkeys)
        saving_group = self.create_saving_settings()
        settings_box.append(saving_group)

        # App settings group
        app_group = self.create_app_settings()
        settings_box.append(app_group)

        content_box.append(scrolled)

        # Load persisted values once all widgets are built
        self._load_from_config()
        self._update_rate_control_visibility()
        self._request_capabilities()

    # ------------------------------------------------------------------
    # Config load / save helpers
    # ------------------------------------------------------------------

    def _load_from_config(self):
        """Populate widgets from the current config (no-op if no config)."""
        if self._config is None:
            return

        self._suppress_signals = True
        try:
            self._clip_spin.set_value(self._config.get("replay_buffer_length", 60))
            self._buffer_size_spin.set_value(
                self._config.get(
                    "replay_buffer_size_mb", REPLAY_BUFFER_SIZE_DEFAULT_MB
                )
            )
            self._fps_row.set_selected(_index_of(_FPS_VALUES, self._config.get("fps", 60)))
            self._resolution_row.set_selected(
                _index_of(
                    self._resolution_values,
                    self._config.get("resolution", "1920x1080"),
                    _index_of(self._resolution_values, "1920x1080"),
                )
            )
            self._format_row.set_selected(
                _index_of(self._format_values, self._config.get("format", "mkv"))
            )
            self._encoder_row.set_selected(
                _index_of(
                    self._video_encoder_values,
                    self._config.get("video_encoder", "obs_x264"),
                )
            )
            self._audio_row.set_selected(
                _index_of(
                    self._audio_encoder_values,
                    self._config.get("audio_encoder", "ffmpeg_aac"),
                )
            )
            self._rate_control_row.set_selected(
                _index_of(_RATE_CONTROL_VALUES, self._config.get("rate_control", "cqp"))
            )
            self._quality_scale.set_value(
                _cqp_to_slider_value(self._config.get("quality_cqp", _CQP_DEFAULT))
            )
            self._bitrate_spin.set_value(self._config.get("video_bitrate", _BITRATE_MEDIUM))
            self._max_bitrate_spin.set_value(self._config.get("video_max_bitrate", _BITRATE_HIGH))
            self._update_rate_control_visibility()
            self._vaapi_row.set_selected(
                _index_of(self._vaapi_device_values, self._config.get("vaapi_device", "auto"))
            )
            output_folder = self._config.get("output_folder", "~/Videos/Clipper")
            self._set_output_folder_subtitle(output_folder)
            self._set_save_hotkey_display_from_config()
            self._boot_switch.set_active(self._config.get("start_on_boot", False))
            self._minimize_to_tray_switch.set_active(
                self._config.get("minimize_to_tray_on_close", False)
            )
            self._remember_window_sizes_switch.set_active(
                self._config.get("remember_window_sizes", True)
            )
            self._notify_on_clip_saved_switch.set_active(
                self._config.get("notify_on_clip_saved", True)
            )
            self._play_sound_on_clip_saved_switch.set_active(
                self._config.get("play_sound_on_clip_saved", False)
            )
        finally:
            self._suppress_signals = False

    def reload_from_config(self):
        """Refresh the displayed values after the configuration is reset."""
        self._load_from_config()

    def _save_key(self, key, value):
        """Save a single key to config (no-op if no config attached)."""
        if self._config is not None and not self._suppress_signals:
            self._config.set(key, value)
            if key == "output_folder" and self._output_folder_changed_callback:
                self._output_folder_changed_callback(value)
            # Show restart banner only when a live capture engine must reload config.
            if (
                key
                in (
                    "replay_buffer_length",
                    "replay_buffer_size_mb",
                    "fps",
                    "resolution",
                    "format",
                    "video_encoder",
                    "audio_encoder",
                    "rate_control",
                    "quality_cqp",
                    "video_bitrate",
                    "video_max_bitrate",
                    "vaapi_device",
                    "output_folder",
                )
                and self._engine_restart_required()
            ):
                self._restart_needed = True
                self._restart_banner.set_revealed(True)

    def _engine_restart_required(self) -> bool:
        if self._engine_restart_required_callback is None:
            return bool(self._engine_client and self._engine_client.is_connected())
        return bool(self._engine_restart_required_callback())

    # ------------------------------------------------------------------
    # Capabilities helpers
    # ------------------------------------------------------------------

    def _request_capabilities(self):
        """Ask the engine for runtime OBS capabilities when available."""
        if (
            self._capabilities_cache.get("ok")
            and self._capability_response_has_encoders(self._capabilities_cache)
        ):
            self._apply_capabilities(self._capabilities_cache)
            return

        if self._engine_client and self._engine_client.is_connected():
            if self._capability_request_in_flight:
                return
            if self._capability_retry_id is not None:
                GLib.source_remove(self._capability_retry_id)
                self._capability_retry_id = None
            self._capability_request_in_flight = True
            self._capability_response_retry_count = 0
            self._show_capability_loading_options()
            if not self._engine_client.get_capabilities(self._on_capabilities):
                self._capability_request_in_flight = False
                self._restore_fallback_capability_options()
            return

        if self._capability_start_requested:
            return

        if self._capabilities_requested_callback:
            self._capability_start_requested = True
            self._capability_retry_count = 0
            self._capability_response_retry_count = 0
            if not self._capabilities_requested_callback():
                self._capability_start_requested = False
                return

            if not self._capability_start_requested:
                return
            if self._engine_client and self._engine_client.is_connected():
                self._request_capabilities()
                return

            self._show_capability_loading_options()
            if self._capability_retry_id is None:
                self._capability_retry_id = GLib.timeout_add(250, self._retry_capabilities)

    def _retry_capabilities(self):
        if not self._capability_start_requested:
            self._capability_retry_id = None
            return False

        self._capability_retry_count += 1
        if self._engine_client and self._engine_client.is_connected():
            self._capability_retry_id = None
            self._request_capabilities()
            return False

        if self._capability_retry_count >= 40:
            self._capability_retry_id = None
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return False

        return True

    def _finish_capability_probe(self):
        if not self._capability_start_requested:
            return

        self._capability_start_requested = False
        if self._capabilities_finished_callback:
            self._capabilities_finished_callback()

    def _capability_rows(self):
        return (
            self._format_row,
            self._encoder_row,
            self._audio_row,
            self._vaapi_row,
        )

    def _set_capability_rows_sensitive(self, sensitive):
        for row in self._capability_rows():
            row.set_sensitive(sensitive)

    def _show_capability_loading_options(self):
        """Avoid briefly showing static fallbacks while runtime probing starts."""
        self._suppress_signals = True
        try:
            for row in self._capability_rows():
                row.set_model(Gtk.StringList.new([_CAPABILITY_LOADING_LABEL]))
                row.set_selected(0)
            self._set_capability_rows_sensitive(False)
        finally:
            self._suppress_signals = False

    def _restore_fallback_capability_options(self):
        self._format_values = list(_FORMAT_VALUES)
        self._video_encoder_values = list(_VIDEO_ENCODER_VALUES)
        self._audio_encoder_values = list(_AUDIO_ENCODER_VALUES)
        self._vaapi_device_values = list(_VAAPI_DEVICE_VALUES)
        self._set_combo_options(
            self._format_row,
            [label for label, _ in _FALLBACK_FORMAT_OPTIONS],
            self._format_values,
            "format",
            "mkv",
        )
        self._set_combo_options(
            self._encoder_row,
            [label for label, _ in _FALLBACK_VIDEO_ENCODER_OPTIONS],
            self._video_encoder_values,
            "video_encoder",
            "obs_x264",
        )
        self._set_combo_options(
            self._audio_row,
            [label for label, _ in _FALLBACK_AUDIO_ENCODER_OPTIONS],
            self._audio_encoder_values,
            "audio_encoder",
            "ffmpeg_aac",
        )
        self._set_combo_options(
            self._vaapi_row,
            [label for label, _ in _FALLBACK_VAAPI_DEVICE_OPTIONS],
            self._vaapi_device_values,
            "vaapi_device",
            "auto",
        )

    def _set_combo_options(self, row, labels, values, config_key, default_value):
        """Replace a ComboRow model while preserving the configured selection."""
        if not labels or not values or len(labels) != len(values):
            return

        display_labels = [
            middle_truncate_text(label, _ROW_DYNAMIC_TEXT_MAX_CHARS) for label in labels
        ]
        selected_value = (
            self._config.get(config_key, default_value) if self._config else default_value
        )
        self._suppress_signals = True
        try:
            row.set_sensitive(True)
            row.set_model(Gtk.StringList.new(display_labels))
            row.set_selected(_index_of(values, selected_value))
            selected = _index_of(values, selected_value)
            _set_widget_tooltip(
                row,
                labels[selected] if 0 <= selected < len(labels) else None,
            )
        finally:
            self._suppress_signals = False

    def _set_output_folder_subtitle(self, path):
        path = str(path or "")
        self._folder_row.set_subtitle(
            middle_truncate_text(path, _ROW_DYNAMIC_TEXT_MAX_CHARS)
        )
        _set_widget_tooltip(self._folder_row, path)

    def _format_label(self, fmt):
        labels = {
            "mkv": _("Matroska video (.mkv)"),
            "mp4": _("MPEG-4 (.mp4)"),
            "mov": _("QuickTime (.mov)"),
            "ts": _("MPEG-TS (.ts)"),
        }
        return labels.get(fmt, fmt.upper())

    def _encoder_label(self, encoder):
        name = encoder.get("name") or encoder.get("id") or "Unknown encoder"
        codec = encoder.get("codec")
        if codec:
            return f"{name} ({codec})"
        return name

    def _capability_response_has_encoders(self, response):
        """Return True only for a real capabilities payload, not any ok response."""
        for key in ("video_encoders", "audio_encoders"):
            encoders = response.get(key)
            if not isinstance(encoders, list):
                return False
            if not any(isinstance(enc, dict) and enc.get("id") for enc in encoders):
                return False
        return True

    def _retry_capability_response(self):
        if not self._engine_client or not self._engine_client.is_connected():
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return False

        self._capability_request_in_flight = True
        if not self._engine_client.get_capabilities(self._on_capabilities):
            self._capability_request_in_flight = False
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
        return False

    def _on_capabilities(self, response):
        """Update OBS-dependent dropdowns from the engine capability response."""
        self._capability_request_in_flight = False
        if not response.get("ok"):
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return

        if not self._capability_response_has_encoders(response):
            if (
                self._capability_response_retry_count
                < _CAPABILITY_RESPONSE_RETRY_LIMIT
            ):
                self._capability_response_retry_count += 1
                GLib.timeout_add(
                    _CAPABILITY_RESPONSE_RETRY_DELAY_MS,
                    self._retry_capability_response,
                )
                return

            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return

        self._capability_response_retry_count = 0
        self._capabilities_cache.clear()
        self._capabilities_cache.update(response)
        self._finish_capability_probe()
        self._apply_capabilities(response)

    def _apply_capabilities(self, response):
        """Populate capability-backed settings from a probed or cached response."""
        self._restore_fallback_capability_options()

        formats = [
            fmt
            for fmt in response.get("formats", [])
            if isinstance(fmt, str) and fmt in OUTPUT_FORMATS
        ]
        if formats:
            self._format_values = formats
            self._set_combo_options(
                self._format_row,
                [self._format_label(fmt) for fmt in formats],
                self._format_values,
                "format",
                "mkv",
            )

        video_encoders = [
            enc
            for enc in response.get("video_encoders", [])
            if isinstance(enc, dict) and enc.get("id")
        ]
        if video_encoders:
            self._video_encoder_values = [enc["id"] for enc in video_encoders]
            self._set_combo_options(
                self._encoder_row,
                [self._encoder_label(enc) for enc in video_encoders],
                self._video_encoder_values,
                "video_encoder",
                "obs_x264",
            )

        audio_encoders = [
            enc
            for enc in response.get("audio_encoders", [])
            if isinstance(enc, dict) and enc.get("id")
        ]
        if audio_encoders:
            self._audio_encoder_values = [enc["id"] for enc in audio_encoders]
            self._set_combo_options(
                self._audio_row,
                [self._encoder_label(enc) for enc in audio_encoders],
                self._audio_encoder_values,
                "audio_encoder",
                "ffmpeg_aac",
            )

        vaapi_devices = [
            dev
            for dev in response.get("vaapi_devices", [])
            if isinstance(dev, dict) and dev.get("id")
        ]
        if vaapi_devices:
            self._vaapi_device_values = [dev["id"] for dev in vaapi_devices]
            self._set_combo_options(
                self._vaapi_row,
                [_vaapi_device_label(dev) for dev in vaapi_devices],
                self._vaapi_device_values,
                "vaapi_device",
                "auto",
            )

    def _save_combo_selection(self, key, values, selected):
        """Persist a ComboRow selection if it maps to a known value."""
        if 0 <= selected < len(values):
            self._save_key(key, values[selected])

    def _current_rate_control(self):
        selected = self._rate_control_row.get_selected()
        if 0 <= selected < len(_RATE_CONTROL_VALUES):
            return _RATE_CONTROL_VALUES[selected]
        return "cqp"

    def _save_key_if_changed(self, key, value, default=None):
        if self._config is not None and self._config.get(key, default) == value:
            return
        self._save_key(key, value)

    def _set_widget_value_silently(self, widget, value):
        was_suppressed = self._suppress_signals
        self._suppress_signals = True
        try:
            widget.set_value(value)
        finally:
            self._suppress_signals = was_suppressed

    def _set_max_bitrate_minimum(self, minimum):
        was_suppressed = self._suppress_signals
        self._suppress_signals = True
        try:
            self._max_bitrate_spin.set_range(minimum, _BITRATE_MAX)
        finally:
            self._suppress_signals = was_suppressed

    def _ensure_vbr_max_bitrate_at_least_target(self, *, save=False):
        target = int(self._bitrate_spin.get_value())
        maximum = int(self._max_bitrate_spin.get_value())
        self._set_max_bitrate_minimum(target)
        if maximum >= target:
            return

        self._set_widget_value_silently(self._max_bitrate_spin, target)
        if save:
            self._save_key_if_changed("video_max_bitrate", target, _BITRATE_HIGH)

    def _update_rate_control_visibility(self, rate_control=None):
        rate_control = rate_control or self._current_rate_control()
        self._quality_row.set_visible(rate_control == "cqp")
        self._bitrate_row.set_visible(rate_control in ("cbr", "vbr"))
        self._max_bitrate_row.set_visible(rate_control == "vbr")
        if rate_control == "cbr":
            self._bitrate_row.set_title(_CBR_BITRATE_TITLE)
            self._bitrate_row.set_subtitle(_CBR_BITRATE_SUBTITLE)
        else:
            self._bitrate_row.set_title(_VBR_BITRATE_TITLE)
            self._bitrate_row.set_subtitle(_VBR_BITRATE_SUBTITLE)
        if rate_control == "vbr":
            self._ensure_vbr_max_bitrate_at_least_target()
        else:
            self._set_max_bitrate_minimum(_BITRATE_MIN)

    def _on_rate_control_selected(self, row, _param):
        selected = row.get_selected()
        if not (0 <= selected < len(_RATE_CONTROL_VALUES)):
            return

        value = _RATE_CONTROL_VALUES[selected]
        self._update_rate_control_visibility(value)
        if self._suppress_signals:
            return

        self._save_key_if_changed("rate_control", value, "cqp")

    def _make_scale(self, minimum, maximum, step, value, marks):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimum, maximum, step)
        scale.set_value(value)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_size_request(200, -1)
        scale.get_adjustment().set_page_increment(step)
        for mark_value, mark_label in marks:
            scale.add_mark(mark_value, Gtk.PositionType.BOTTOM, mark_label)
        return scale

    def _connect_deferred_scale(self, scale, key):
        scale.connect("value-changed", self._on_deferred_scale_value_changed, key)
        pointer_events = Gtk.EventControllerLegacy.new()
        pointer_events.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pointer_events.connect("event", self._on_deferred_scale_pointer_event, key)
        scale.add_controller(pointer_events)

        keys = Gtk.EventControllerKey.new()
        keys.connect("key-released", self._on_deferred_scale_key_released, key)
        scale.add_controller(keys)

    def _on_deferred_scale_value_changed(self, scale, key):
        if not self._suppress_signals:
            self._pending_slider_keys.add(key)

    def _on_deferred_scale_pointer_event(self, controller, event, key):
        event = event or controller.get_current_event()
        if event is None:
            return False

        if event.get_event_type() in (
            Gdk.EventType.BUTTON_RELEASE,
            Gdk.EventType.TOUCH_END,
            Gdk.EventType.TOUCH_CANCEL,
        ):
            self._save_pending_slider(key)

        return False

    def _on_deferred_scale_key_released(self, controller, keyval, keycode, state, key):
        self._save_pending_slider(key)

    def _save_pending_slider(self, key):
        if key not in self._pending_slider_keys:
            return

        self._pending_slider_keys.discard(key)
        self._save_slider(key)

    def _save_slider(self, key):
        if self._suppress_signals:
            return

        if key == "quality_cqp":
            value = _slider_value_to_cqp(self._quality_scale.get_value())
            self._save_key_if_changed("quality_cqp", value, _CQP_DEFAULT)

    def _format_quality_scale_value(self, _scale, value, _user_data=None):
        return str(_slider_value_to_cqp(value))

    def _make_bitrate_spin(self, value):
        spin = Gtk.SpinButton()
        spin.set_range(_BITRATE_MIN, _BITRATE_MAX)
        spin.set_increments(_BITRATE_STEP, _BITRATE_PAGE)
        spin.set_value(value)
        spin.set_numeric(True)
        spin.set_digits(0)
        spin.set_valign(Gtk.Align.CENTER)
        spin.set_width_chars(6)
        return spin

    def _on_bitrate_value_changed(self, spin):
        if self._suppress_signals:
            return

        value = int(spin.get_value())
        if self._current_rate_control() == "vbr":
            self._ensure_vbr_max_bitrate_at_least_target(save=True)
        self._save_key_if_changed("video_bitrate", value, _BITRATE_MEDIUM)

    def _on_max_bitrate_value_changed(self, spin):
        if self._suppress_signals:
            return

        if self._current_rate_control() == "vbr":
            self._ensure_vbr_max_bitrate_at_least_target()
        value = int(self._max_bitrate_spin.get_value())
        self._save_key_if_changed("video_max_bitrate", value, _BITRATE_HIGH)

    # ------------------------------------------------------------------
    # Widget factory methods
    # ------------------------------------------------------------------

    def create_recording_settings(self):
        """Create recording settings group"""
        group = Adw.PreferencesGroup()
        group.set_title(_("Recording"))
        group.set_description(_("Configure clip and encoding settings"))

        # Clip length
        clip_row = Adw.ActionRow()
        clip_row.set_title(_("Clip length"))
        clip_row.set_subtitle(_("How many seconds to keep in memory"))

        self._clip_spin = Gtk.SpinButton()
        self._clip_spin.set_range(5, 300)
        self._clip_spin.set_increments(5, 30)
        self._clip_spin.set_value(60)
        self._clip_spin.set_valign(Gtk.Align.CENTER)
        self._clip_spin.connect(
            "value-changed",
            lambda w: self._save_key("replay_buffer_length", int(w.get_value())),
        )
        clip_row.add_suffix(self._clip_spin)

        seconds_label = Gtk.Label(label=_("seconds"))
        seconds_label.add_css_class("dim-label")
        seconds_label.set_valign(Gtk.Align.CENTER)
        clip_row.add_suffix(seconds_label)

        group.add(clip_row)

        # File size limit
        buffer_size_row = Adw.ActionRow()
        buffer_size_row.set_title(_("File size limit"))
        buffer_size_row.set_subtitle(_("Maximum size retained for each clip"))

        self._buffer_size_spin = Gtk.SpinButton()
        self._buffer_size_spin.set_range(
            REPLAY_BUFFER_SIZE_MIN_MB, REPLAY_BUFFER_SIZE_MAX_MB
        )
        self._buffer_size_spin.set_increments(128, 1024)
        self._buffer_size_spin.set_value(REPLAY_BUFFER_SIZE_DEFAULT_MB)
        self._buffer_size_spin.set_valign(Gtk.Align.CENTER)
        self._buffer_size_spin.set_numeric(True)
        self._buffer_size_spin.connect(
            "value-changed",
            lambda w: self._save_key(
                "replay_buffer_size_mb", int(w.get_value())
            ),
        )
        buffer_size_row.add_suffix(self._buffer_size_spin)

        mebibytes_label = Gtk.Label(label=_("MiB"))
        mebibytes_label.add_css_class("dim-label")
        mebibytes_label.set_valign(Gtk.Align.CENTER)
        buffer_size_row.add_suffix(mebibytes_label)

        group.add(buffer_size_row)

        # Frame rate
        self._fps_row = Adw.ComboRow()
        self._fps_row.set_title(_("Frame rate"))
        self._fps_row.set_subtitle(_("Frames per second"))
        self._fps_row.set_model(Gtk.StringList.new([_("30 FPS"), _("60 FPS")]))
        self._fps_row.set_selected(1)
        self._fps_row.connect(
            "notify::selected",
            lambda w, _: self._save_key("fps", _FPS_VALUES[w.get_selected()]),
        )
        group.add(self._fps_row)

        # Resolution
        self._resolution_row = Adw.ComboRow()
        self._resolution_row.set_title(_("Resolution"))
        self._resolution_row.set_subtitle(_("Output video resolution"))
        self._resolution_row.set_model(Gtk.StringList.new(self._resolution_values))
        self._resolution_row.set_selected(0)
        self._resolution_row.connect(
            "notify::selected",
            lambda w, _: self._save_combo_selection(
                "resolution", self._resolution_values, w.get_selected()
            ),
        )
        group.add(self._resolution_row)

        # Recording format
        self._format_row = Adw.ComboRow()
        self._format_row.set_title(_("Recording format"))
        self._format_row.set_subtitle(_("Container format for output file"))
        self._format_row.set_model(
            Gtk.StringList.new([label for label, _ in _FALLBACK_FORMAT_OPTIONS])
        )
        self._format_row.set_selected(0)
        self._format_row.connect(
            "notify::selected",
            lambda w, _: self._save_combo_selection(
                "format", self._format_values, w.get_selected()
            ),
        )
        group.add(self._format_row)

        # Video encoder
        self._encoder_row = Adw.ComboRow()
        self._encoder_row.set_title(_("Video encoder"))
        self._encoder_row.set_subtitle(_("Encoder implementation"))
        self._encoder_row.set_model(
            Gtk.StringList.new([label for label, _ in _FALLBACK_VIDEO_ENCODER_OPTIONS])
        )
        self._encoder_row.set_selected(0)
        self._encoder_row.connect(
            "notify::selected",
            lambda w, _: self._save_combo_selection(
                "video_encoder", self._video_encoder_values, w.get_selected()
            ),
        )
        group.add(self._encoder_row)

        # Audio encoder
        self._audio_row = Adw.ComboRow()
        self._audio_row.set_title(_("Audio encoder"))
        self._audio_row.set_subtitle(_("Audio compression format"))
        self._audio_row.set_model(
            Gtk.StringList.new([label for label, _ in _FALLBACK_AUDIO_ENCODER_OPTIONS])
        )
        self._audio_row.set_selected(0)
        self._audio_row.connect(
            "notify::selected",
            lambda w, _: self._save_combo_selection(
                "audio_encoder", self._audio_encoder_values, w.get_selected()
            ),
        )
        group.add(self._audio_row)

        # Rate control
        self._rate_control_row = Adw.ComboRow()
        self._rate_control_row.set_title(_("Rate control"))
        self._rate_control_row.set_subtitle(_RATE_CONTROL_SUBTITLE)
        self._rate_control_row.set_model(
            Gtk.StringList.new([label for label, _ in _RATE_CONTROL_OPTIONS])
        )
        self._rate_control_row.set_selected(0)
        self._rate_control_row.connect("notify::selected", self._on_rate_control_selected)
        group.add(self._rate_control_row)

        # Quality slider (CQP)
        self._quality_row = Adw.ActionRow()
        self._quality_row.set_title(_("Quality"))
        self._quality_row.set_subtitle(_QUALITY_SUBTITLE)

        quality_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        quality_box.set_valign(Gtk.Align.CENTER)

        self._quality_scale = self._make_scale(
            _CQP_HIGH_QUALITY,
            _CQP_LOW_QUALITY,
            1,
            _cqp_to_slider_value(_CQP_DEFAULT),
            (
                (_cqp_to_slider_value(_CQP_LOW_QUALITY), _("Low")),
                (_cqp_to_slider_value(_CQP_MEDIUM_QUALITY), _("Medium")),
                (_cqp_to_slider_value(_CQP_HIGH_QUALITY), _("High")),
            ),
        )
        self._quality_scale.set_format_value_func(self._format_quality_scale_value)
        self._connect_deferred_scale(self._quality_scale, "quality_cqp")

        quality_box.append(self._quality_scale)
        self._quality_row.add_suffix(quality_box)
        group.add(self._quality_row)

        # Bitrate (CBR/VBR)
        self._bitrate_row = Adw.ActionRow()
        self._bitrate_row.set_title(_VBR_BITRATE_TITLE)
        self._bitrate_row.set_subtitle(_VBR_BITRATE_SUBTITLE)

        self._bitrate_spin = self._make_bitrate_spin(_BITRATE_MEDIUM)
        self._bitrate_spin.connect("value-changed", self._on_bitrate_value_changed)
        self._bitrate_row.add_suffix(self._bitrate_spin)

        bitrate_label = Gtk.Label(label=_("Kbps"))
        bitrate_label.add_css_class("dim-label")
        bitrate_label.set_valign(Gtk.Align.CENTER)
        self._bitrate_row.add_suffix(bitrate_label)

        group.add(self._bitrate_row)

        # Max bitrate (VBR)
        self._max_bitrate_row = Adw.ActionRow()
        self._max_bitrate_row.set_title(_MAX_BITRATE_TITLE)
        self._max_bitrate_row.set_subtitle(_MAX_BITRATE_SUBTITLE)

        self._max_bitrate_spin = self._make_bitrate_spin(_BITRATE_HIGH)
        self._max_bitrate_spin.connect("value-changed", self._on_max_bitrate_value_changed)
        self._max_bitrate_row.add_suffix(self._max_bitrate_spin)

        max_bitrate_label = Gtk.Label(label=_("Kbps"))
        max_bitrate_label.add_css_class("dim-label")
        max_bitrate_label.set_valign(Gtk.Align.CENTER)
        self._max_bitrate_row.add_suffix(max_bitrate_label)

        group.add(self._max_bitrate_row)

        # VAAPI device
        self._vaapi_row = Adw.ComboRow()
        self._vaapi_row.set_title(_("VAAPI device"))
        self._vaapi_row.set_subtitle(_("Hardware acceleration device"))
        self._vaapi_row.set_model(
            Gtk.StringList.new([label for label, _ in _FALLBACK_VAAPI_DEVICE_OPTIONS])
        )
        self._vaapi_row.set_selected(0)
        self._vaapi_row.connect(
            "notify::selected",
            lambda w, _: self._save_combo_selection(
                "vaapi_device", self._vaapi_device_values, w.get_selected()
            ),
        )
        group.add(self._vaapi_row)

        if self._show_display_target_controls:
            self._capture_target_row = Adw.ActionRow()
            self._capture_target_row.set_title(_("Change capture target display"))
            self._capture_target_row.set_subtitle(_("Opens the system display picker"))
            self._capture_target_button = Gtk.Button(label=_("Pick display…"))
            self._capture_target_button.set_valign(Gtk.Align.CENTER)
            self._capture_target_button.connect("clicked", self._on_change_capture_target)
            self._capture_target_row.add_suffix(self._capture_target_button)
            self._capture_target_row.set_activatable_widget(self._capture_target_button)
            group.add(self._capture_target_row)

        return group

    def create_saving_settings(self):
        """Create saving settings group"""
        group = Adw.PreferencesGroup()
        group.set_title(_("Saving"))
        group.set_description(_("Output location, shortcuts and feedback"))

        # Output folder
        self._folder_row = Adw.ActionRow()
        self._folder_row.set_title(_("Output folder"))
        output_folder = (
            self._config.get("output_folder", "~/Videos/Clipper")
            if self._config
            else "~/Videos/Clipper"
        )
        self._set_output_folder_subtitle(output_folder)

        folder_button = Gtk.Button(label=_("Choose…"))
        folder_button.set_valign(Gtk.Align.CENTER)
        folder_button.connect("clicked", self.on_choose_folder)
        self._folder_row.add_suffix(folder_button)

        group.add(self._folder_row)

        # Save clip hotkey
        self._save_hotkey_row = Adw.ActionRow()
        self._save_hotkey_row.set_title(_("Hotkey"))
        self._save_hotkey_row.set_subtitle(
            _("Your desktop may ask you to approve or change this shortcut")
        )

        self._save_hotkey_button = Gtk.Button(
            label=self._configured_save_hotkey_label()
        )
        self._save_hotkey_button.set_valign(Gtk.Align.CENTER)
        self._save_hotkey_button.connect(
            "clicked", lambda _button: self.on_change_hotkey("save")
        )
        self._save_hotkey_capture = InlineHotkeyCapture(
            self._save_hotkey_button,
            self._save_hotkey_row,
            self._on_save_hotkey_selected,
            self._set_save_hotkey_display_from_config,
            self._configured_save_hotkey_subtitle,
            self._hotkey_capture_state_callback,
        )

        self._save_hotkey_row.add_suffix(self._save_hotkey_button)
        self._save_hotkey_row.set_activatable_widget(self._save_hotkey_button)

        group.add(self._save_hotkey_row)

        # Notification feedback
        notification_row = Adw.ActionRow()
        notification_row.set_title(_("Show notification when a clip is captured"))
        notification_row.set_subtitle(_("Display a system notification after saving a clip"))

        self._notify_on_clip_saved_switch = Gtk.Switch()
        self._notify_on_clip_saved_switch.set_valign(Gtk.Align.CENTER)
        self._notify_on_clip_saved_switch.connect(
            "notify::active",
            lambda w, _: self._save_key("notify_on_clip_saved", w.get_active()),
        )
        notification_row.add_suffix(self._notify_on_clip_saved_switch)
        notification_row.set_activatable_widget(self._notify_on_clip_saved_switch)

        group.add(notification_row)

        # Sound feedback
        sound_row = Adw.ActionRow()
        sound_row.set_title(_("Play sound when a clip is captured"))
        sound_row.set_subtitle(_("Play audio feedback after saving a clip"))

        self._play_sound_on_clip_saved_switch = Gtk.Switch()
        self._play_sound_on_clip_saved_switch.set_valign(Gtk.Align.CENTER)
        self._play_sound_on_clip_saved_switch.connect(
            "notify::active",
            lambda w, _: self._save_key("play_sound_on_clip_saved", w.get_active()),
        )
        sound_row.add_suffix(self._play_sound_on_clip_saved_switch)
        sound_row.set_activatable_widget(self._play_sound_on_clip_saved_switch)

        group.add(sound_row)

        return group

    def create_app_settings(self):
        """Create app settings group"""
        group = Adw.PreferencesGroup()
        group.set_title(_("App"))
        group.set_description(_("Startup and window behavior"))
        update_row = Adw.SwitchRow(title=_("Automatically check for updates"),
                                  subtitle=_("Check GitHub daily for new Clipper releases"))
        update_row.set_active(
            self._config.get("auto_check_updates", True) if self._config else True
        )
        update_row.connect("notify::active", lambda row, _param:
                           self._save_key("auto_check_updates", row.get_active()))
        group.add(update_row)

        if self._config is not None and self._language_changed_callback is not None:
            self._language_row = create_language_row(
                self._config, self._language_changed_callback
            )
            group.add(self._language_row)

        # Start on boot
        boot_row = Adw.ActionRow()
        boot_row.set_title(_("Start on boot"))
        boot_row.set_subtitle(_("Launch Clipper automatically when you log in"))

        self._boot_switch = Gtk.Switch()
        self._boot_switch.set_valign(Gtk.Align.CENTER)
        self._boot_switch.connect(
            "notify::active",
            self._on_start_on_boot_changed,
        )
        boot_row.add_suffix(self._boot_switch)
        boot_row.set_activatable_widget(self._boot_switch)

        group.add(boot_row)

        minimize_row = Adw.ActionRow()
        minimize_row.set_title(_("Minimize to tray instead of close"))
        minimize_row.set_subtitle(
            _("Hide Clipper to the tray when the window close button is pressed")
        )

        self._minimize_to_tray_switch = Gtk.Switch()
        self._minimize_to_tray_switch.set_valign(Gtk.Align.CENTER)
        self._minimize_to_tray_switch.connect(
            "notify::active",
            self._on_minimize_to_tray_changed,
        )
        minimize_row.add_suffix(self._minimize_to_tray_switch)
        minimize_row.set_activatable_widget(self._minimize_to_tray_switch)

        group.add(minimize_row)

        remember_sizes_row = Adw.ActionRow()
        remember_sizes_row.set_title(_("Remember window sizes"))
        remember_sizes_row.set_subtitle(
            _("Restore sizes for the main window, video editor and video player")
        )

        self._remember_window_sizes_switch = Gtk.Switch()
        self._remember_window_sizes_switch.set_valign(Gtk.Align.CENTER)
        self._remember_window_sizes_switch.connect(
            "notify::active",
            lambda switch, _param: self._save_key(
                "remember_window_sizes", switch.get_active()
            ),
        )
        remember_sizes_row.add_suffix(self._remember_window_sizes_switch)
        remember_sizes_row.set_activatable_widget(self._remember_window_sizes_switch)

        group.add(remember_sizes_row)

        return group

    def _on_change_capture_target(self, _button) -> None:
        """Open the system display picker immediately."""
        callback = self._display_target_change_callback
        if callback is None:
            self._show_toast(_("The system display picker is unavailable"))
            return

        self._capture_target_button.set_sensitive(False)
        self._capture_target_row.set_subtitle(_("Waiting for the system display picker…"))
        if not callback(self._on_capture_target_changed):
            if not self._capture_target_button.get_sensitive():
                self._on_capture_target_changed(
                    False, _("Could not open the system display picker.")
                )

    def _on_capture_target_changed(self, success: bool, message: str) -> None:
        self._capture_target_button.set_sensitive(True)
        self._capture_target_row.set_subtitle(_("Opens the system display picker"))
        if success:
            self._show_toast(message)
        else:
            self._show_toast(
                _("Capture display was not changed: %(message)s") % {"message": message}
            )

    def _on_minimize_to_tray_changed(self, switch, _param) -> None:
        if self._suppress_signals:
            return

        enabled = bool(switch.get_active())
        tray_available = (
            bool(self._tray_available_callback())
            if self._tray_available_callback is not None
            else True
        )
        if enabled and not tray_available:
            self._suppress_signals = True
            try:
                switch.set_active(False)
            finally:
                self._suppress_signals = False
            self._show_toast(_("Tray unavailable. Clipper will quit when closed."))
            return

        self._save_key("minimize_to_tray_on_close", enabled)

    def _on_start_on_boot_changed(self, switch, _param) -> None:
        """Apply login startup before persisting the displayed preference."""
        if self._suppress_signals or self._start_on_boot_request_pending:
            return

        enabled = bool(switch.get_active())
        if self._start_on_boot_changed_callback is None:
            self._save_key("start_on_boot", enabled)
            return

        self._start_on_boot_request_pending = True
        switch.set_sensitive(False)

        def on_applied(success: bool, error: str | None = None) -> None:
            self._start_on_boot_request_pending = False
            switch.set_sensitive(True)
            if success:
                self._save_key("start_on_boot", enabled)
                self._save_key("autostart_background_mode_configured", enabled)
                return

            previous = bool(
                self._config.get("start_on_boot", False) if self._config else False
            )
            self._suppress_signals = True
            try:
                switch.set_active(previous)
            finally:
                self._suppress_signals = False
            self._show_toast(error or _("Could not update the start-on-boot setting"))

        try:
            self._start_on_boot_changed_callback(enabled, on_applied)
        except Exception as exc:  # noqa: BLE001
            on_applied(
                False,
                _("Could not update the start-on-boot setting: %(error)s")
                % {"error": exc},
            )

    def on_choose_folder(self, button):
        """Open folder chooser dialog"""
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Choose output folder"))
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        """Handle folder selection result"""
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                path = folder.get_path()
                self._set_output_folder_subtitle(path)
                self._save_key("output_folder", path)
        except GLib.Error:
            pass  # user cancelled

    def on_change_hotkey(self, action):
        """Start capturing the save hotkey directly in the settings row."""
        if action != "save":
            return
        self._save_hotkey_capture.start()

    def _on_save_hotkey_selected(self, hotkey: str):
        if self._hotkey_changed_callback:
            pending = bool(self._hotkey_changed_callback(hotkey))
            if not pending:
                self._set_save_hotkey_display(hotkey)
            return pending
        else:
            self._set_save_hotkey_display(hotkey)
            self._save_key("save_hotkey", hotkey)
            return False

    def _set_save_hotkey_display(self, hotkey: str):
        self._save_hotkey_button.set_label(display_hotkey(hotkey))

    def _configured_save_hotkey_label(self) -> str:
        if self._config is None:
            return _("Not set")
        return effective_hotkey_label(
            self._config.get("save_hotkey", ""),
            portal_managed=bool(
                self._config.get("save_hotkey_portal_managed", False)
            ),
            portal_label=self._config.get("save_hotkey_portal_label", ""),
        )

    def _configured_save_hotkey_subtitle(self) -> str:
        if self._config and self._config.get(
            "save_hotkey_portal_managed", False
        ):
            return _("Managed by your desktop's global shortcut settings")
        return _("Your desktop may ask you to approve or change this shortcut")

    def _set_save_hotkey_display_from_config(self) -> None:
        self._save_hotkey_button.set_label(self._configured_save_hotkey_label())

    def refresh_save_hotkey_from_config(self) -> None:
        """Restore the effective label after a rejected binding request."""
        self._set_save_hotkey_display_from_config()

    def set_save_hotkey_from_portal(self, hotkey: str | None) -> None:
        """Reflect a shortcut changed by the desktop's portal UI."""
        self._save_hotkey_button.set_label(
            effective_hotkey_label("", portal_managed=True, portal_label=hotkey)
        )
        self._save_hotkey_row.set_subtitle(
            _("Managed by your desktop's global shortcut settings")
        )

    def on_restart_engine(self, banner):
        """Handle engine restart button click"""
        if self._engine_client and self._engine_client.is_connected():
            # Send shutdown command with restart flag
            self._engine_client.shutdown(self._on_engine_shutdown, restart=True)
            banner.set_revealed(False)
            self._restart_needed = False
        else:
            # Engine not connected, just hide the banner
            banner.set_revealed(False)
            self._restart_needed = False

    def _on_engine_shutdown(self, response):
        """Handle engine shutdown response"""
        if response.get("ok"):
            # Engine will now disconnect and auto-reconnect
            self._expecting_restart = True
            self._show_toast(_("Restarting engine…"))
        else:
            self._show_toast(
                _("Engine shutdown failed: %(error)s")
                % {"error": response.get("error", _("Unknown error"))}
            )

    def _on_engine_state_change(self, new_state):
        """Handle engine connection state changes"""
        if new_state == STATE_CONNECTED and self._expecting_restart:
            # Engine successfully restarted and reconnected
            self._expecting_restart = False
            self._request_capabilities()
            self._show_toast(_("Engine restarted successfully"))
        elif new_state == STATE_CONNECTED:
            self._request_capabilities()
        else:
            self._capability_request_in_flight = False

        if new_state == STATE_DISCONNECTED and self._expecting_restart:
            # Engine restart failed after max retry attempts
            self._expecting_restart = False
            self._show_toast(_("Failed to restart engine. Please restart manually."))
        elif new_state == STATE_DISCONNECTED:
            self._finish_capability_probe()

    def _show_toast(self, message):
        """Show a toast notification"""
        window = self.get_root()
        if window and hasattr(window, "show_toast"):
            window.show_toast(message)
