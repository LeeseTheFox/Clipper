from types import SimpleNamespace

import setup_window as _setup_window_module
from setup_window import (
    SetupWindow,
    _encoder_label,
    _initial_setup_resolution,
    _resolution_label,
)


class ConfigStub:
    def __init__(self) -> None:
        self.saved = []
        self.values = {"save_hotkey": "ctrl+alt+s"}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value
        self.saved.append((key, value))


class ButtonStub:
    def __init__(self) -> None:
        self.sensitive = True
        self.visible = True

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = bool(sensitive)

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)

    def set_label(self, label: str) -> None:
        self.label = label


class LabelStub:
    def __init__(self) -> None:
        self.label = ""
        self.classes = set()
        self.visible = True

    def set_label(self, label: str) -> None:
        self.label = label

    def add_css_class(self, css_class: str) -> None:
        self.classes.add(css_class)

    def remove_css_class(self, css_class: str) -> None:
        self.classes.discard(css_class)

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)


class StackStub:
    def __init__(self) -> None:
        self.visible_child = None
        self.transition = None

    def set_transition_type(self, transition) -> None:
        self.transition = transition

    def set_visible_child(self, child) -> None:
        self.visible_child = child


def test_setup_labels_present_resolution_and_runtime_codec_clearly():
    assert _resolution_label("2560x1440") == "2560 × 1440"
    assert _encoder_label(
        {"id": "ffmpeg_vaapi", "name": "FFmpeg VAAPI", "codec": "h264"}
    ) == "FFmpeg VAAPI (h264)"


def test_new_setup_prefers_detected_native_resolution():
    assert (
        _initial_setup_resolution(
            "1920x1080", "2560x1440", config_was_loaded=False
        )
        == "2560x1440"
    )


def test_setup_preserves_resolution_from_existing_config():
    assert (
        _initial_setup_resolution(
            "1920x1080", "2560x1440", config_was_loaded=True
        )
        == "1920x1080"
    )


def test_new_setup_falls_back_when_display_resolution_is_unavailable():
    assert (
        _initial_setup_resolution("1920x1080", None, config_was_loaded=False)
        == "1920x1080"
    )


def test_new_setup_selects_and_saves_detected_native_resolution(monkeypatch):
    monkeypatch.setattr(
        _setup_window_module, "_primary_display_resolution", lambda: "2560x1440"
    )
    config = ConfigStub()

    setup = SetupWindow(application=None, config=config)

    assert config.values["resolution"] == "2560x1440"
    assert setup._resolution_values[setup._resolution_row.get_selected()] == "2560x1440"
    setup.destroy()


def test_setup_welcome_page_has_the_catalog_driven_language_picker():
    config = ConfigStub()
    selected = []
    setup = SetupWindow(
        application=None,
        config=config,
        language_changed_callback=lambda language, parent: selected.append(
            (language, parent)
        ),
    )

    model = setup._language_row.get_model()
    assert [model.get_string(index) for index in range(model.get_n_items())] == [
        "System language",
        "English",
        "Русский",
        "Українська",
    ]

    setup._language_row.set_selected(1)

    assert selected == [("en", setup)]
    setup.destroy()


def test_setup_combo_changes_are_saved_immediately():
    config = ConfigStub()
    setup = SimpleNamespace(_suppress_signals=False, _config=config)
    row = SimpleNamespace(get_selected=lambda: 1)

    SetupWindow._on_combo_changed(setup, row, None, "fps", [30, 60])

    assert config.saved == [("fps", 60)]


def test_setup_replay_buffer_size_is_saved_immediately():
    config = ConfigStub()
    setup = SimpleNamespace(_suppress_signals=False, _config=config)
    spin = SimpleNamespace(get_value=lambda: 2048)

    SetupWindow._on_buffer_size_changed(setup, spin)

    assert config.saved == [("replay_buffer_size_mb", 2048)]


def test_runtime_codec_values_replace_fallbacks_without_staling_signal_data():
    config = ConfigStub()
    fallback_video_values = ["obs_x264", "ffmpeg_vaapi"]
    fallback_audio_values = ["ffmpeg_aac", "ffmpeg_opus", "ffmpeg_flac"]
    setup = SimpleNamespace(
        _video_encoder_values=fallback_video_values,
        _audio_encoder_values=fallback_audio_values,
        _video_encoder_row=object(),
        _audio_encoder_row=object(),
        _replace_combo_options=lambda *_args: None,
        _finish_capability_probe=lambda: None,
    )

    SetupWindow._on_capabilities(
        setup,
        {
            "ok": True,
            "video_encoders": [
                {"id": "obs_x264", "name": "x264"},
                {"id": "ffmpeg_svt_av1", "name": "FFmpeg AV1"},
            ],
            "audio_encoders": [
                {"id": "ffmpeg_flac", "name": "FFmpeg FLAC"},
                {"id": "ffmpeg_pcm_s16le", "name": "PCM 16-bit"},
            ],
        },
    )

    assert setup._video_encoder_values is fallback_video_values
    assert setup._audio_encoder_values is fallback_audio_values

    selection = SimpleNamespace(get_selected=lambda: 1)
    saver = SimpleNamespace(_suppress_signals=False, _config=config)
    SetupWindow._on_combo_changed(
        saver, selection, None, "video_encoder", fallback_video_values
    )
    SetupWindow._on_combo_changed(
        saver, selection, None, "audio_encoder", fallback_audio_values
    )

    assert config.saved == [
        ("video_encoder", "ffmpeg_svt_av1"),
        ("audio_encoder", "ffmpeg_pcm_s16le"),
    ]


def test_setup_hotkey_button_starts_inline_capture():
    starts = []
    setup = SimpleNamespace(
        _hotkey_capture=SimpleNamespace(start=lambda: starts.append(True)),
    )

    SetupWindow._on_hotkey_clicked(setup, None)

    assert starts == [True]


def test_setup_hotkey_selection_delegates_extended_function_key():
    config = ConfigStub()
    changed = []
    setup = SimpleNamespace(
        _config=config,
        _hotkey_button=ButtonStub(),
        _hotkey_changed_callback=changed.append,
    )
    setup._hotkey_button.set_label = lambda label: setattr(
        setup._hotkey_button, "label", label
    )

    SetupWindow._on_hotkey_selected(setup, "F15")

    assert config.saved == []
    assert setup._hotkey_button.label == "F15"
    assert changed == ["F15"]


def test_setup_navigation_exposes_only_the_selected_stack_page():
    pages = [object() for _ in range(5)]
    dots = [LabelStub() for _ in pages]
    for dot in dots:
        dot.add_css_class("dim-label")
    setup = SimpleNamespace(
        _pages=pages,
        _page_index=0,
        _stack=StackStub(),
        _step_label=LabelStub(),
        _step_dots=dots,
        _back_button=ButtonStub(),
        _skip_button=ButtonStub(),
        _next_button=SimpleNamespace(set_label=lambda _label: None),
        _request_capabilities=lambda: None,
    )

    SetupWindow._show_page(setup, 4)

    assert setup._stack.visible_child is pages[4]
    assert setup._step_label.label == "Step 5 of 5"
    assert ["dim-label" in dot.classes for dot in dots] == [
        True,
        True,
        True,
        True,
        False,
    ]
    assert setup._skip_button.visible is False


def test_setup_display_button_opens_picker_and_advances_to_all_set():
    callbacks = []
    shown_pages = []
    setup = SimpleNamespace(
        _display_request_active=False,
        _display_button=ButtonStub(),
        _display_status=LabelStub(),
        _pages=[object() for _ in range(5)],
        _show_page=shown_pages.append,
    )
    setup._on_display_target_result = lambda success, message: (
        SetupWindow._on_display_target_result(setup, success, message)
    )
    setup._display_target_callback = lambda callback: callbacks.append(callback) or True

    SetupWindow._on_display_clicked(setup, None)

    assert len(callbacks) == 1
    assert setup._display_request_active is True
    assert setup._display_button.sensitive is False
    assert setup._display_status.visible is True
    assert "Waiting" in setup._display_status.label

    callbacks[0](True, "Capture display selected successfully.")

    assert setup._display_request_active is False
    assert setup._display_button.sensitive is True
    assert "success" in setup._display_status.classes
    assert shown_pages == [4]


def test_finishing_setup_marks_it_complete_before_entering_main_window():
    config = ConfigStub()
    completed = []
    setup = SimpleNamespace(
        _config=config,
        cleanup=lambda: None,
        _completed_callback=lambda: completed.append(True),
    )

    SetupWindow._finish_setup(setup)

    assert config.saved == [
        ("setup_completed", True),
        ("display_capture_guidance_seen", True),
    ]
    assert completed == [True]


def test_skipping_setup_clears_default_hotkey_before_completion():
    config = ConfigStub()
    changed = []
    completed = []
    setup = SimpleNamespace(
        _config=config,
        _hotkey_button=ButtonStub(),
        _hotkey_changed_callback=changed.append,
        _finish_setup=lambda: completed.append(True),
    )

    SetupWindow._on_finish_clicked(setup, None)

    assert config.values["save_hotkey"] == ""
    assert setup._hotkey_button.label == "Not set"
    assert changed == [""]
    assert completed == [True]
