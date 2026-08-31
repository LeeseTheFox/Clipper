from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")

from editor_window import EditorWindow, _prevent_scroll_seeking, format_transport_time
from gi.repository import Gtk


class FakeLabel:
    def __init__(self):
        self.text = None

    def set_text(self, text):
        self.text = text


class FakeRevealer:
    def __init__(self):
        self.revealed = None

    def set_reveal_child(self, revealed):
        self.revealed = revealed


def test_monitor_hover_reveals_and_hides_transport():
    window = SimpleNamespace(transport_revealer=FakeRevealer())

    EditorWindow._on_monitor_hover_enter(window)
    assert window.transport_revealer.revealed is True

    EditorWindow._on_monitor_hover_leave(window)
    assert window.transport_revealer.revealed is False


def test_video_double_click_toggles_fullscreen_without_changing_playback_state():
    calls = []
    window = SimpleNamespace(
        _toggle_playback=lambda: calls.append("playback"),
        _toggle_fullscreen=lambda: calls.append("fullscreen"),
    )

    EditorWindow._on_video_pressed(window, None, 1, 0, 0)
    EditorWindow._on_video_pressed(window, None, 2, 0, 0)

    assert calls == ["playback", "playback", "fullscreen"]


def test_single_video_click_toggles_playback_immediately():
    calls = []
    window = SimpleNamespace(_toggle_playback=lambda: calls.append("playback"))

    EditorWindow._on_video_pressed(window, None, 1, 0, 0)

    assert calls == ["playback"]


def test_transport_combines_position_and_duration_like_the_player():
    window = SimpleNamespace(
        time_label=FakeLabel(),
        project=SimpleNamespace(output_duration_us=125_678_000),
    )

    EditorWindow._update_transport_time(window, 3_042_000)

    assert window.time_label.text == "0:03 / 2:05"


def test_transport_time_does_not_display_negative_values():
    assert format_transport_time(-1) == "0:00"


def test_transport_time_matches_player_format_for_long_clips():
    assert format_transport_time(3_725_000_000) == "1:02:05"


def test_editor_saves_window_size_before_cleanup_and_destruction():
    calls = []
    window = SimpleNamespace(
        _block_close_during_export=lambda: False,
        _window_size=SimpleNamespace(save=lambda: calls.append("size")),
        cleanup=lambda: calls.append("cleanup"),
        finished_callback=lambda _window: calls.append("finished"),
    )

    EditorWindow._finish(window)

    assert calls == ["size", "cleanup", "finished"]


def test_editor_progress_slider_consumes_scroll_wheel_input():
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
    _prevent_scroll_seeking(scale)
    model = scale.observe_controllers()
    controllers = [model.get_item(index) for index in range(model.get_n_items())]
    scroll_controller = next(
        controller
        for controller in controllers
        if isinstance(controller, Gtk.EventControllerScroll)
        and controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    )

    assert scroll_controller.emit("scroll", 0.0, 1.0) is True


def test_transport_progress_tracks_the_cut_aware_output_position():
    values = []
    window = SimpleNamespace(
        project=SimpleNamespace(output_duration_us=20_000_000),
        _transport_seek_dragging=False,
        _updating_transport_seek=False,
        _seek_scale=SimpleNamespace(set_value=values.append),
    )

    EditorWindow._update_transport_seek(window, 5_000_000)

    assert values == [250]
    assert window._updating_transport_seek is False


def test_transport_progress_click_seeks_to_the_output_timeline_position():
    calls = []
    window = SimpleNamespace(
        project=SimpleNamespace(output_duration_us=20_000_000),
        preview=SimpleNamespace(
            seek_output=lambda output_us: calls.append(("seek", output_us)),
            finish_seek=lambda: calls.append("finish"),
        ),
        _updating_transport_seek=False,
        _transport_seek_dragging=False,
        _source_invalid=False,
        _playhead_us=0,
        _playback_follow_active=True,
        _update_transport_time=lambda output_us: calls.append(("time", output_us)),
        _update_playhead=lambda: calls.append("playhead"),
    )

    assert EditorWindow._on_seek_change_value(window, None, None, 375) is False

    assert window._playhead_us == 7_500_000
    assert calls == [
        ("time", 7_500_000),
        "playhead",
        ("seek", 7_500_000),
        "finish",
    ]
    assert window._playback_follow_active is False


def test_transport_progress_drag_uses_the_coalesced_scrub_preview_path():
    calls = []
    window = SimpleNamespace(
        project=SimpleNamespace(output_duration_us=10_000_000),
        preview=SimpleNamespace(
            seek_output=lambda output_us: calls.append(("seek", output_us)),
            finish_seek=lambda: calls.append("finish"),
        ),
        _updating_transport_seek=False,
        _transport_seek_dragging=True,
        _source_invalid=False,
        _playhead_us=0,
        _update_transport_time=lambda output_us: calls.append(("time", output_us)),
        _update_playhead=lambda: calls.append("playhead"),
        _queue_scrub_preview=lambda output_us: calls.append(("preview", output_us)),
    )

    EditorWindow._on_seek_change_value(window, None, None, 600)

    assert window._playhead_us == 6_000_000
    assert calls == [
        ("time", 6_000_000),
        "playhead",
        ("preview", 6_000_000),
    ]


def test_transport_volume_mutes_and_restores_the_previous_level():
    values = []
    preview = SimpleNamespace(set_volume=values.append)

    class FakeScale:
        value = 0.35

        def get_value(self):
            return self.value

        def set_value(self, value):
            self.value = value

    class FakeButton:
        icon_name = None

        def set_icon_name(self, name):
            self.icon_name = name

        def set_tooltip_text(self, _text):
            pass

    window = SimpleNamespace(
        preview=preview,
        _volume_scale=FakeScale(),
        _mute_button=FakeButton(),
        _volume_before_mute=None,
        _updating_volume=False,
    )
    window._set_volume = lambda volume: EditorWindow._set_volume(window, volume)
    window._set_volume_ui = lambda volume: EditorWindow._set_volume_ui(window, volume)

    EditorWindow._toggle_muted(window)
    assert values == [0.0]
    assert window._volume_scale.get_value() == 0
    assert window._mute_button.icon_name == "speaker-0-symbolic"

    EditorWindow._toggle_muted(window)
    assert values == [0.0, 0.35]
    assert window._volume_scale.get_value() == 0.35
    assert window._mute_button.icon_name == "speaker-2-symbolic"


def test_volume_keyboard_step_is_five_percent_and_clamped():
    class FakeScale:
        value = 0.5

        def get_value(self):
            return self.value

        def set_value(self, value):
            self.value = value

    window = SimpleNamespace(_volume_scale=FakeScale())

    EditorWindow._change_volume(window, 1)
    assert window._volume_scale.get_value() == 0.55
    EditorWindow._change_volume(window, -1)
    assert window._volume_scale.get_value() == 0.5

    window._volume_scale.set_value(0.98)
    EditorWindow._change_volume(window, 1)
    assert window._volume_scale.get_value() == 1


def test_editor_mute_button_scroll_adjusts_volume():
    values = []
    scale = SimpleNamespace(
        get_value=lambda: 0.5,
        set_value=values.append,
    )
    window = SimpleNamespace(_volume_scale=scale)

    assert EditorWindow._on_mute_button_scroll(window, None, 0, -1) is True
    assert EditorWindow._on_mute_button_scroll(window, None, 0, 1) is True

    assert values == [0.6, 0.4]


def test_video_fullscreen_hides_editor_chrome_and_removes_monitor_margins():
    class FakeWidget:
        def __init__(self):
            self.visible = True
            self.margins = []
            self.icon = None
            self.tooltip = None

        def set_visible(self, visible):
            self.visible = visible

        def set_margin_start(self, margin):
            self.margins.append(margin)

        set_margin_end = set_margin_start
        set_margin_top = set_margin_start
        set_margin_bottom = set_margin_start

        def set_icon_name(self, icon):
            self.icon = icon

        def set_tooltip_text(self, tooltip):
            self.tooltip = tooltip

    class FakeRoot:
        def __init__(self):
            self.classes = set()

        def add_css_class(self, name):
            self.classes.add(name)

        def remove_css_class(self, name):
            self.classes.discard(name)

    window = SimpleNamespace(
        is_fullscreen=lambda: True,
        _header=FakeWidget(),
        _timeline_panel=FakeWidget(),
        _seek_scale=FakeWidget(),
        _control_spacer=FakeWidget(),
        _picture_frame=FakeWidget(),
        _fullscreen_button=FakeWidget(),
        _root=FakeRoot(),
    )

    EditorWindow._on_fullscreen_changed(window)

    assert window._header.visible is False
    assert window._timeline_panel.visible is False
    assert window._seek_scale.visible is True
    assert window._control_spacer.visible is False
    assert window._picture_frame.margins == [0, 0, 0, 0]
    assert "editor-video-fullscreen" in window._root.classes
    assert window._fullscreen_button.tooltip == "Exit fullscreen"
