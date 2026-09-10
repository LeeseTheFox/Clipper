import importlib.util
import sys
import types
from pathlib import Path


def _load_settings_module():
    module_name = "test_settings_view_isolated"
    module_path = Path(__file__).resolve().parents[1] / "ui" / "settings_view.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "gi",
            "gi.repository",
            "engine_client",
            "hotkeys",
        )
    }

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")
    timeouts = []
    removed_sources = []

    def timeout_add(_delay, callback):
        timeouts.append(callback)
        return len(timeouts)

    class StringList:
        @staticmethod
        def new(labels):
            return list(labels)

    repository.GLib = types.SimpleNamespace(
        timeout_add=timeout_add,
        source_remove=lambda source_id: removed_sources.append(source_id),
    )
    repository.Gtk = types.SimpleNamespace(
        Box=type("Box", (), {}),
        Window=type("Window", (), {}),
        StringList=StringList,
    )
    repository.Adw = types.SimpleNamespace()
    repository.Gdk = types.SimpleNamespace()
    gi.repository = repository

    engine_client = types.ModuleType("engine_client")
    engine_client.STATE_CONNECTED = 2
    engine_client.STATE_DISCONNECTED = 0

    hotkeys = types.ModuleType("hotkeys")
    hotkeys.HotkeyError = Exception
    hotkeys.display_hotkey = lambda hotkey: hotkey
    hotkeys.effective_hotkey_label = lambda preferred, **kwargs: (
        (kwargs.get("portal_label") or "Not set")
        if kwargs.get("portal_managed")
        else (preferred or "Not set")
    )
    hotkeys.hotkey_from_gdk_event = lambda *_args, **_kwargs: ""

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "engine_client": engine_client,
            "hotkeys": hotkeys,
        }
    )

    try:
        module = importlib.util.module_from_spec(spec)
        module._test_timeouts = timeouts
        module._test_removed_sources = removed_sources
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


settings_module = _load_settings_module()
SettingsView = settings_module.SettingsView
InlineHotkeyCapture = settings_module.InlineHotkeyCapture
STATE_CONNECTED = settings_module.STATE_CONNECTED


class EngineClientStub:
    def __init__(self, *, connected=False, response=None):
        self.connected = connected
        self.response = response or {
            "ok": True,
            "video_encoders": [
                {"id": "obs_x264", "name": "x264", "codec": "h264"},
                {"id": "ffmpeg_vaapi", "name": "FFmpeg VAAPI", "codec": "h264"},
            ],
            "audio_encoders": [
                {"id": "ffmpeg_aac", "name": "FFmpeg AAC", "codec": "aac"},
                {"id": "ffmpeg_opus", "name": "FFmpeg Opus", "codec": "opus"},
            ],
            "formats": ["mkv", "mp4"],
            "vaapi_devices": [{"id": "auto", "name": "Automatic"}],
        }
        self.sent = 0

    def is_connected(self):
        return self.connected

    def get_capabilities(self, callback):
        self.sent += 1
        callback(self.response)
        return True


class RowStub:
    def __init__(self):
        self.model = []
        self.selected = None
        self.sensitive = True
        self.visible = True
        self.title = ""
        self.subtitle = ""

    def set_title(self, title):
        self.title = title

    def set_subtitle(self, subtitle):
        self.subtitle = subtitle

    def set_model(self, model):
        self.model = list(model)

    def set_selected(self, selected):
        self.selected = selected

    def get_selected(self):
        return self.selected

    def set_sensitive(self, sensitive):
        self.sensitive = bool(sensitive)

    def set_visible(self, visible):
        self.visible = bool(visible)


class ValueStub:
    def __init__(self, value):
        self.value = value
        self.lower = None
        self.upper = None
        self.sensitive = True

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value

    def set_range(self, lower, upper):
        self.lower = lower
        self.upper = upper
        self.value = max(lower, min(upper, self.value))

    def set_sensitive(self, sensitive):
        self.sensitive = bool(sensitive)


class ActiveStub:
    def __init__(self, active=False):
        self.active = active
        self.sensitive = True

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = bool(active)

    def set_sensitive(self, sensitive):
        self.sensitive = bool(sensitive)


class ButtonStub:
    def __init__(self):
        self.label = ""
        self.sensitive = True
        self.focused = False

    def set_label(self, label):
        self.label = label

    def set_sensitive(self, sensitive):
        self.sensitive = bool(sensitive)

    def get_sensitive(self):
        return self.sensitive

    def grab_focus(self):
        self.focused = True
        return True


class ConfigStub:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.saved = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.saved.append((key, value))


class BannerStub:
    def __init__(self):
        self.revealed = False

    def set_revealed(self, revealed):
        self.revealed = bool(revealed)


def _make_view(
    *,
    connected=False,
    capabilities_requested=None,
    capabilities_finished=None,
    capabilities_cache=None,
):
    view = object.__new__(SettingsView)
    view._config = None
    view._engine_client = EngineClientStub(connected=connected)
    view._output_folder_changed_callback = None
    view._start_on_boot_changed_callback = None
    view._display_target_change_callback = None
    view._tray_available_callback = None
    view._engine_restart_required_callback = lambda: False
    view._capabilities_requested_callback = capabilities_requested
    view._capabilities_finished_callback = capabilities_finished
    view._expecting_restart = False
    view._capability_start_requested = False
    view._capability_retry_id = None
    view._capability_retry_count = 0
    view._capability_request_in_flight = False
    view._capability_response_retry_count = 0
    view._capabilities_cache = (
        capabilities_cache if capabilities_cache is not None else {}
    )
    view._pending_slider_keys = set()
    view._format_values = []
    view._video_encoder_values = []
    view._audio_encoder_values = []
    view._vaapi_device_values = []
    view._resolution_values = list(settings_module._RESOLUTION_VALUES)
    view._suppress_signals = False
    view._hotkey_changed_callback = None
    view._hotkey_capture_state_callback = None
    view._start_on_boot_request_pending = False
    view._restart_banner = BannerStub()
    view._clip_spin = ValueStub(60)
    view._buffer_size_spin = ValueStub(1024)
    view._fps_row = RowStub()
    view._resolution_row = RowStub()
    view._format_row = RowStub()
    view._encoder_row = RowStub()
    view._audio_row = RowStub()
    view._vaapi_row = RowStub()
    view._rate_control_row = RowStub()
    view._rate_control_row.set_selected(0)
    view._quality_row = RowStub()
    view._bitrate_row = RowStub()
    view._max_bitrate_row = RowStub()
    view._quality_scale = ValueStub(23)
    view._bitrate_spin = ValueStub(12000)
    view._max_bitrate_spin = ValueStub(20000)
    view._folder_row = RowStub()
    view._save_hotkey_button = ButtonStub()
    view._boot_switch = ActiveStub()
    view._minimize_to_tray_switch = ActiveStub()
    view._remember_window_sizes_switch = ActiveStub()
    view._notify_on_clip_saved_switch = ActiveStub()
    view._play_sound_on_clip_saved_switch = ActiveStub()
    view._clip_sound_volume_scale = ValueStub(1.0)
    view._show_toast = lambda _message: None
    return view


def test_minimize_to_tray_is_rejected_when_tray_support_is_unavailable():
    view = _make_view()
    view._config = ConfigStub()
    view._tray_available_callback = lambda: False
    messages = []
    view._show_toast = messages.append
    switch = ActiveStub(True)

    view._on_minimize_to_tray_changed(switch, None)

    assert switch.get_active() is False
    assert view._config.saved == []
    assert messages and "unavailable" in messages[0]


def test_minimize_to_tray_is_saved_when_tray_support_is_available():
    view = _make_view()
    view._config = ConfigStub()
    view._tray_available_callback = lambda: True
    switch = ActiveStub(True)

    view._on_minimize_to_tray_changed(switch, None)

    assert view._config.saved == [("minimize_to_tray_on_close", True)]


def test_capture_target_button_opens_system_picker_without_confirmation():
    view = _make_view()
    view._capture_target_button = ButtonStub()
    view._capture_target_row = RowStub()
    results = []
    toasts = []

    def open_picker(callback):
        results.append(callback)
        return True

    view._display_target_change_callback = open_picker
    view._show_toast = toasts.append

    view._on_change_capture_target(None)

    assert len(results) == 1
    assert view._capture_target_button.get_sensitive() is False
    assert "Waiting" in view._capture_target_row.subtitle

    results[0](True, "Capture display selected successfully.")

    assert view._capture_target_button.get_sensitive() is True
    assert toasts == ["Capture display selected successfully."]


def test_settings_hotkey_action_starts_inline_capture():
    view = _make_view()
    starts = []
    view._save_hotkey_capture = types.SimpleNamespace(
        start=lambda: starts.append(True)
    )

    view.on_change_hotkey("save")

    assert starts == [True]


def test_inline_hotkey_capture_updates_held_modifiers_and_saves_chord(monkeypatch):
    button = ButtonStub()
    row = RowStub()
    requested = []
    capture_states = []
    capture = object.__new__(InlineHotkeyCapture)
    capture._button = button
    capture._row = row
    capture._selected_callback = lambda hotkey: requested.append(hotkey) or False
    capture._restore_display_callback = lambda: None
    capture._restore_subtitle_callback = lambda: "Configured subtitle"
    capture._capture_state_callback = lambda *state: capture_states.append(state)
    capture._active = False
    capture._pressed_modifiers = set()

    masks = types.SimpleNamespace(
        CONTROL_MASK=1 << 0,
        ALT_MASK=1 << 1,
        SHIFT_MASK=1 << 2,
        SUPER_MASK=1 << 3,
        META_MASK=1 << 4,
    )
    fake_gdk = types.SimpleNamespace(
        ModifierType=masks,
        keyval_name=lambda keyval: {
            1: "Control_L",
            2: "Shift_L",
            3: "s",
        }[keyval],
    )
    monkeypatch.setattr(settings_module, "Gdk", fake_gdk)
    monkeypatch.setattr(
        settings_module,
        "hotkey_from_gdk_event",
        lambda *_args: types.SimpleNamespace(
            canonical="ctrl+shift+s", display="Ctrl+Shift+S"
        ),
    )
    monkeypatch.setattr(
        settings_module,
        "display_hotkey",
        lambda hotkey: "Ctrl+Shift+S" if hotkey else "Not set",
    )

    capture.start()
    capture._on_key_pressed(None, 1, 37, 0)
    assert button.label == "Ctrl"

    capture._on_key_pressed(None, 2, 50, masks.CONTROL_MASK)
    assert button.label == "Ctrl+Shift"

    capture._on_key_pressed(
        None, 3, 39, masks.CONTROL_MASK | masks.SHIFT_MASK
    )

    assert requested == ["ctrl+shift+s"]
    assert button.label == "Ctrl+Shift+S"
    assert capture._active is False
    assert row.subtitle == "Configured subtitle"
    assert capture_states[0][0] is True
    assert capture_states[-1] == (False, None)


def test_settings_keeps_effective_label_while_portal_approval_is_pending():
    view = _make_view()
    view._config = ConfigStub({"save_hotkey": "ctrl+alt+s"})
    view._save_hotkey_button.label = "Ctrl+Alt+S"
    requested = []
    view._hotkey_changed_callback = lambda hotkey: requested.append(hotkey) or True

    view._on_save_hotkey_selected("ctrl+bracketleft")

    assert requested == ["ctrl+bracketleft"]
    assert view._save_hotkey_button.label == "Ctrl+Alt+S"
    assert view._config.saved == []


def test_settings_renders_portal_shortcut_label_without_parsing_it():
    view = _make_view()
    view._save_hotkey_row = RowStub()
    view._config = ConfigStub(
        {
            "save_hotkey": "ctrl+bracketleft",
            "save_hotkey_portal_managed": True,
            "save_hotkey_portal_label": "Strg+[",
        }
    )

    assert view._configured_save_hotkey_label() == "Strg+["

    view.set_save_hotkey_from_portal(None)
    assert view._save_hotkey_button.label == "Not set"


class MonitorStub:
    def __init__(self, width, height, *, primary=False, scale=1):
        self._geometry = types.SimpleNamespace(width=width, height=height)
        self._primary = primary
        self._scale = scale

    def get_geometry(self):
        return self._geometry

    def get_scale_factor(self):
        return self._scale

    def is_primary(self):
        return self._primary


class MonitorListStub:
    def __init__(self, monitors):
        self._monitors = monitors

    def get_n_items(self):
        return len(self._monitors)

    def get_item(self, index):
        return self._monitors[index]


class DisplayStub:
    def __init__(self, monitors, primary_monitor=None):
        self._monitors = monitors
        self._primary_monitor = primary_monitor

    def get_monitors(self):
        return MonitorListStub(self._monitors)

    def get_primary_monitor(self):
        return self._primary_monitor


def test_resolution_options_include_nonstandard_900p_primary_display():
    options = settings_module._resolution_options(
        detected_resolution=settings_module._primary_display_resolution(
            DisplayStub([MonitorStub(1600, 900)])
        )
    )

    assert "1600x900" in options
    assert options.index("1280x720") < options.index("1600x900")
    assert options.index("1600x900") < options.index("1920x1080")


def test_resolution_detection_uses_primary_display_not_vertical_secondary():
    primary = MonitorStub(2560, 1440)
    secondary = MonitorStub(1080, 1920)
    display = DisplayStub([secondary, primary], primary_monitor=primary)

    options = settings_module._resolution_options(
        detected_resolution=settings_module._primary_display_resolution(display)
    )

    assert "2560x1440" in options
    assert "1080x1920" not in options


def test_resolution_detection_uses_monitor_primary_flag_when_available():
    secondary = MonitorStub(1080, 1920)
    primary = MonitorStub(1600, 900, primary=True)
    display = DisplayStub([secondary, primary])

    assert settings_module._primary_display_resolution(display) == "1600x900"


def test_resolution_options_include_ultrawide_and_super_ultrawide_displays():
    ultrawide = settings_module._resolution_options(detected_resolution="3440x1440")
    super_ultrawide = settings_module._resolution_options(detected_resolution="5120x1440")

    assert "3440x1440" in ultrawide
    assert "5120x1440" in super_ultrawide
    assert ultrawide.index("2560x1440") < ultrawide.index("3440x1440")
    assert super_ultrawide.index("2560x1440") < super_ultrawide.index("5120x1440")


def test_resolution_options_include_old_4_by_3_display():
    options = settings_module._resolution_options(detected_resolution="1024x768")

    assert "1024x768" in options
    assert options.index("1280x720") < options.index("1024x768")
    assert options.index("1024x768") < options.index("1920x1080")


def test_monitor_resolution_converts_hidpi_geometry_to_physical_pixels():
    monitor = MonitorStub(1920, 1080, scale=2)

    assert settings_module._monitor_resolution(monitor) == "3840x2160"


def test_saved_custom_resolution_is_kept_and_selected_on_load():
    view = _make_view()
    view._config = ConfigStub({"resolution": "1600x900"})
    view._resolution_values = settings_module._resolution_options("1600x900")

    view._load_from_config()

    assert view._resolution_row.selected == view._resolution_values.index("1600x900")


def test_sound_feedback_setting_is_loaded_into_switch():
    view = _make_view()
    view._config = ConfigStub(
        {"play_sound_on_clip_saved": True, "clip_sound_volume": 1.65}
    )

    view._load_from_config()

    assert view._play_sound_on_clip_saved_switch.get_active() is True
    assert view._clip_sound_volume_scale.get_value() == 1.65
    assert view._clip_sound_volume_scale.sensitive is True


def test_sound_feedback_switch_controls_volume_slider_and_is_saved():
    view = _make_view()
    view._config = ConfigStub()
    switch = ActiveStub(True)

    view._on_clip_sound_enabled_changed(switch, None)

    assert view._clip_sound_volume_scale.sensitive is True
    assert view._config.saved == [("play_sound_on_clip_saved", True)]


def test_sound_feedback_volume_is_saved_as_gain():
    view = _make_view()
    view._config = ConfigStub()
    view._clip_sound_volume_scale.set_value(1.35)

    view._save_slider("clip_sound_volume")

    assert view._config.saved == [("clip_sound_volume", 1.35)]


def test_replay_buffer_size_is_loaded_from_config():
    view = _make_view()
    view._config = ConfigStub({"replay_buffer_size_mb": 2048})

    view._load_from_config()

    assert view._buffer_size_spin.get_value() == 2048


def test_start_on_boot_is_saved_only_after_platform_registration_succeeds():
    view = _make_view()
    view._config = ConfigStub({"start_on_boot": False})
    requests = []
    toasts = []
    view._start_on_boot_changed_callback = (
        lambda enabled, callback: requests.append((enabled, callback))
    )
    view._show_toast = toasts.append
    view._boot_switch.set_active(True)

    view._on_start_on_boot_changed(view._boot_switch, None)

    assert requests[0][0] is True
    assert view._boot_switch.sensitive is False
    assert view._config.saved == []

    requests[0][1](True, None)

    assert view._boot_switch.sensitive is True
    assert view._config.saved == [
        ("start_on_boot", True),
        ("autostart_background_mode_configured", True),
    ]
    assert toasts == []


def test_start_on_boot_rolls_back_when_platform_registration_fails():
    view = _make_view()
    view._config = ConfigStub({"start_on_boot": False})
    toasts = []
    view._start_on_boot_changed_callback = (
        lambda _enabled, callback: callback(False, "Autostart was denied")
    )
    view._show_toast = toasts.append
    view._boot_switch.set_active(True)

    view._on_start_on_boot_changed(view._boot_switch, None)

    assert view._boot_switch.get_active() is False
    assert view._boot_switch.sensitive is True
    assert view._config.saved == []
    assert toasts == ["Autostart was denied"]


def test_invalid_custom_resolution_is_ignored():
    options = settings_module._resolution_options(
        configured_resolution="wide",
        detected_resolution="0x900",
    )

    assert options == sorted(
        settings_module._RESOLUTION_VALUES,
        key=settings_module._resolution_sort_key,
    )


def test_settings_capability_probe_retries_until_engine_connects():
    requested = []
    finished = []
    view = _make_view(
        capabilities_requested=lambda: requested.append(True) or True,
        capabilities_finished=lambda: finished.append(True),
    )

    view._request_capabilities()

    assert view._encoder_row.model == ["Detecting…"]
    assert view._encoder_row.sensitive is False

    view._engine_client.connected = True
    settings_module._test_timeouts[-1]()

    assert requested == [True]
    assert view._engine_client.sent == 1
    assert finished == [True]
    assert view._capability_start_requested is False
    assert view._encoder_row.sensitive is True


def test_recording_formats_exclude_containers_without_multi_track_audio():
    assert settings_module._FORMAT_VALUES == ["mkv", "mp4", "mov", "ts"]


def test_runtime_recording_formats_are_limited_to_supported_containers():
    view = _make_view(connected=True)
    response = dict(view._engine_client.response)
    response["formats"] = [
        "flv",
        "mkv",
        "hybrid_mp4",
        "mp4",
        "mov",
        "fragmented_mov",
        "ts",
        "hls",
    ]

    view._on_capabilities(response)

    assert view._format_values == ["mkv", "mp4", "mov", "ts"]
    assert view._format_row.model == [
        "Matroska video (.mkv)",
        "MPEG-4 (.mp4)",
        "QuickTime (.mov)",
        "MPEG-TS (.ts)",
    ]


def test_runtime_recording_format_labels_are_translated(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "_",
        lambda message: f"TRANSLATED:{message}",
    )

    view = _make_view()

    assert view._format_label("mkv") == "TRANSLATED:Matroska video (.mkv)"


def test_runtime_vaapi_automatic_option_is_translated(monkeypatch):
    view = _make_view(connected=True)
    response = dict(view._engine_client.response)
    response["vaapi_devices"] = [
        {"id": "auto", "name": "Automatic"},
        {"id": "/dev/dri/renderD128", "name": "Render device"},
    ]
    monkeypatch.setattr(
        settings_module,
        "_",
        lambda message: "AUTOMATIC_TRANSLATED" if message == "Automatic" else message,
    )

    view._on_capabilities(response)

    assert view._vaapi_row.model == ["AUTOMATIC_TRANSLATED", "Render device"]
    assert view._vaapi_device_values == ["auto", "/dev/dri/renderD128"]


def test_settings_capability_probe_synchronous_connect_does_not_leave_retry_timer():
    finished = []
    holder = {}

    def connect_immediately():
        view = holder["view"]
        view._engine_client.connected = True
        view._on_engine_state_change(STATE_CONNECTED)
        return True

    view = _make_view(
        capabilities_requested=connect_immediately,
        capabilities_finished=lambda: finished.append(True),
    )
    holder["view"] = view
    timeout_count = len(settings_module._test_timeouts)

    view._request_capabilities()

    assert finished == [True]
    assert view._engine_client.sent == 1
    assert view._capability_start_requested is False
    assert len(settings_module._test_timeouts) == timeout_count


def test_completed_settings_capability_probe_ignores_stale_retry_timer():
    view = _make_view()
    view._capability_start_requested = False
    view._encoder_row.set_model(["Runtime encoder"])
    view._audio_row.set_model(["Runtime audio"])

    assert view._retry_capabilities() is False

    assert view._encoder_row.model == ["Runtime encoder"]
    assert view._audio_row.model == ["Runtime audio"]


def test_settings_capability_probe_times_out_and_finishes():
    finished = []
    view = _make_view(
        capabilities_requested=lambda: True,
        capabilities_finished=lambda: finished.append(True),
    )

    view._request_capabilities()
    retry = settings_module._test_timeouts[-1]
    keep_running = True
    for _ in range(40):
        keep_running = retry()

    assert keep_running is False
    assert finished == [True]
    assert view._capability_start_requested is False
    assert view._encoder_row.model == ["x264", "FFmpeg VAAPI"]
    assert view._audio_row.model == ["FFmpeg AAC", "FFmpeg Opus", "FFmpeg FLAC"]
    assert view._encoder_row.sensitive is True


def test_settings_capability_probe_retries_status_shaped_response():
    finished = []
    view = _make_view(
        connected=True,
        capabilities_finished=lambda: finished.append(True),
    )
    view._capability_start_requested = True
    view._engine_client.response = {
        "ok": True,
        "buffer_active": False,
        "capture_mode": "game_capture",
    }

    view._request_capabilities()

    assert finished == []
    assert view._capability_request_in_flight is False
    assert view._capability_response_retry_count == 1
    assert view._encoder_row.model == ["Detecting…"]

    view._engine_client.response = {
        "ok": True,
        "video_encoders": [
            {"id": "obs_x264", "name": "x264", "codec": "h264"},
        ],
        "audio_encoders": [
            {"id": "ffmpeg_aac", "name": "FFmpeg AAC", "codec": "aac"},
        ],
    }
    settings_module._test_timeouts[-1]()

    assert finished == [True]
    assert view._engine_client.sent == 2
    assert view._encoder_row.model == ["x264 (h264)"]
    assert view._audio_row.model == ["FFmpeg AAC (aac)"]


def test_engine_connect_during_settings_probe_fetches_capabilities_immediately():
    finished = []
    view = _make_view(capabilities_finished=lambda: finished.append(True))
    view._capability_start_requested = True
    view._capability_retry_id = 12
    view._engine_client.connected = True

    view._on_engine_state_change(STATE_CONNECTED)

    assert settings_module._test_removed_sources == [12]
    assert view._engine_client.sent == 1
    assert finished == [True]


def test_settings_capabilities_are_cached_across_background_window_recreation():
    shared_cache = {}
    view = _make_view(connected=True, capabilities_cache=shared_cache)

    view._request_capabilities()
    recreated_view = _make_view(connected=False, capabilities_cache=shared_cache)
    recreated_view._request_capabilities()

    assert view._engine_client.sent == 1
    assert recreated_view._engine_client.sent == 0
    assert recreated_view._encoder_row.model == [
        "x264 (h264)",
        "FFmpeg VAAPI (h264)",
    ]


def test_settings_probe_retries_after_reconnecting_state_clears_in_flight_request():
    finished = []
    view = _make_view(capabilities_finished=lambda: finished.append(True))
    view._capability_start_requested = True
    view._capability_request_in_flight = True

    view._on_engine_state_change(1)
    view._engine_client.connected = True
    view._on_engine_state_change(STATE_CONNECTED)

    assert view._engine_client.sent == 1
    assert finished == [True]


def test_settings_probe_final_disconnect_finishes_temporary_probe():
    finished = []
    view = _make_view(capabilities_finished=lambda: finished.append(True))
    view._capability_start_requested = True
    view._capability_request_in_flight = True

    view._on_engine_state_change(settings_module.STATE_DISCONNECTED)

    assert view._capability_request_in_flight is False
    assert finished == [True]


def test_rate_control_visibility_switches_mode_specific_rows():
    view = _make_view()

    view._update_rate_control_visibility("cqp")
    assert view._quality_row.visible is True
    assert view._bitrate_row.visible is False
    assert view._max_bitrate_row.visible is False

    view._update_rate_control_visibility("cbr")
    assert view._quality_row.visible is False
    assert view._bitrate_row.visible is True
    assert view._max_bitrate_row.visible is False

    view._update_rate_control_visibility("vbr")
    assert view._quality_row.visible is False
    assert view._bitrate_row.visible is True
    assert view._max_bitrate_row.visible is True


def test_bitrate_row_copy_matches_rate_control_mode():
    view = _make_view()

    view._update_rate_control_visibility("cbr")
    assert view._bitrate_row.title == "Constant bitrate"
    assert view._bitrate_row.subtitle == "Fixed video bitrate used for the entire recording"

    view._update_rate_control_visibility("vbr")
    assert view._bitrate_row.title == "Target bitrate"
    assert view._bitrate_row.subtitle == "Target video bitrate the encoder will aim for"


def test_cqp_slider_uses_reversed_clean_quality_range():
    assert settings_module._CQP_HIGH_QUALITY == 20
    assert settings_module._CQP_MEDIUM_QUALITY == 25
    assert settings_module._CQP_LOW_QUALITY == 30
    assert settings_module._cqp_to_slider_value(30) == 20
    assert settings_module._cqp_to_slider_value(25) == 25
    assert settings_module._cqp_to_slider_value(20) == 30


def test_cqp_slider_saves_right_side_as_high_quality_cqp():
    view = _make_view()
    view._config = ConfigStub({"quality_cqp": 25})
    view._quality_scale.set_value(30)

    view._save_slider("quality_cqp")

    assert view._config.data["quality_cqp"] == 20
    assert ("quality_cqp", 20) in view._config.saved


def test_cqp_slider_saves_left_side_as_low_quality_cqp():
    view = _make_view()
    view._config = ConfigStub({"quality_cqp": 25})
    view._quality_scale.set_value(20)

    view._save_slider("quality_cqp")

    assert view._config.data["quality_cqp"] == 30
    assert ("quality_cqp", 30) in view._config.saved


def test_vbr_visibility_clamps_max_bitrate_to_target():
    view = _make_view()
    view._bitrate_spin.set_value(18000)
    view._max_bitrate_spin.set_value(12000)

    view._update_rate_control_visibility("vbr")

    assert view._max_bitrate_spin.get_value() == 18000
    assert view._max_bitrate_spin.lower == 18000
    assert view._max_bitrate_spin.upper == 50000


def test_non_vbr_visibility_restores_max_bitrate_minimum():
    view = _make_view()
    view._bitrate_spin.set_value(18000)

    view._update_rate_control_visibility("vbr")
    view._update_rate_control_visibility("cbr")

    assert view._max_bitrate_spin.lower == 1000
    assert view._max_bitrate_spin.upper == 50000


def test_vbr_bitrate_spin_persists_target_and_clamped_max():
    view = _make_view()
    view._config = ConfigStub(
        {
            "rate_control": "vbr",
            "video_bitrate": 12000,
            "video_max_bitrate": 14000,
        }
    )
    view._rate_control_row.set_selected(2)
    view._bitrate_spin.set_value(18000)
    view._max_bitrate_spin.set_value(14000)

    view._on_bitrate_value_changed(view._bitrate_spin)

    assert view._config.data["video_bitrate"] == 18000
    assert view._config.data["video_max_bitrate"] == 18000
    assert view._max_bitrate_spin.lower == 18000
    assert ("video_max_bitrate", 18000) in view._config.saved
    assert ("video_bitrate", 18000) in view._config.saved


def test_vbr_max_bitrate_spin_clamps_before_saving():
    view = _make_view()
    view._config = ConfigStub(
        {
            "rate_control": "vbr",
            "video_bitrate": 18000,
            "video_max_bitrate": 20000,
        }
    )
    view._rate_control_row.set_selected(2)
    view._bitrate_spin.set_value(18000)
    view._max_bitrate_spin.set_value(12000)

    view._on_max_bitrate_value_changed(view._max_bitrate_spin)

    assert view._config.data["video_max_bitrate"] == 18000
    assert ("video_max_bitrate", 18000) in view._config.saved
