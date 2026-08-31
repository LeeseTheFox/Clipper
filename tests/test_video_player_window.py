from types import SimpleNamespace

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")

import video_player_window as player_module
from gi.repository import Gdk, Gst, Gtk
from video_output import ClipperVideoSink
from video_player_window import (
    _PLAYER_CSS,
    VideoPlayerWindow,
    _volume_icon_name,
    discover_video_dimensions,
    fit_video_window_size,
)


def test_player_window_follows_landscape_video_size_up_to_1280_pixels():
    assert fit_video_window_size(1920, 1080, 96) == (1280, 816)
    assert fit_video_window_size(3840, 2160, 96) == (1280, 816)


def test_player_window_caps_portrait_video_and_chrome_at_1280_pixels():
    width, height = fit_video_window_size(1080, 1920, 96)

    assert (width, height) == (666, 1280)
    assert width <= 1280
    assert height <= 1280


def test_player_window_keeps_smaller_video_at_its_natural_size():
    assert fit_video_window_size(800, 600, 96) == (800, 696)


def test_player_letterboxes_to_keep_the_whole_frame_visible_at_any_window_ratio():
    window = VideoPlayerWindow()

    assert window.get_resizable() is True
    assert window._picture.get_content_fit() == Gtk.ContentFit.CONTAIN

    window.destroy()


def test_player_controls_float_over_video_and_follow_hover():
    window = VideoPlayerWindow()

    video_overlay = window._controls_revealer.get_parent()
    assert isinstance(video_overlay, Gtk.Overlay)
    assert video_overlay.get_child() is window._picture
    assert window._controls_revealer.get_reveal_child() is False
    assert window._controls_revealer.get_halign() == Gtk.Align.FILL
    assert window._controls_revealer.get_valign() == Gtk.Align.END
    assert window._controls_revealer.get_margin_start() == 16
    assert window._controls_revealer.get_margin_end() == 16
    assert window._controls_revealer.get_margin_bottom() == 16

    window._on_video_hover_enter()
    assert window._controls_revealer.get_reveal_child() is True

    window._on_video_hover_leave()
    assert window._controls_revealer.get_reveal_child() is False
    window.destroy()


def test_player_transport_is_one_line_with_time_next_to_playback():
    window = VideoPlayerWindow()
    children = []
    child = window._controls.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()

    assert window._controls.get_orientation() == Gtk.Orientation.HORIZONTAL
    assert children == [
        window._play_button,
        window._time_label,
        window._seek_scale,
        window._volume_control,
        window._fullscreen_button,
    ]
    assert window._time_label.has_css_class("monospace") is False
    assert window._time_label.has_css_class("caption") is False
    window.destroy()


def test_player_volume_slider_expands_and_controls_pipeline_volume():
    state = {"volume": 1.0, "mute": False}
    pipeline = SimpleNamespace(
        get_property=lambda name: state[name],
        set_property=lambda name, value: state.__setitem__(name, value),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline

    assert window._volume_revealer.get_reveal_child() is False
    window._on_volume_control_enter()
    assert window._volume_revealer.get_reveal_child() is True

    window._volume_scale.set_value(0.35)
    assert state == {"volume": 0.35, "mute": False}
    assert window._mute_button.get_icon_name() == player_module.VOLUME_MEDIUM

    window._toggle_muted()
    assert state == {"volume": 0.0, "mute": True}
    assert window._volume_scale.get_value() == 0
    assert window._mute_button.get_icon_name() == player_module.VOLUME_CROSS
    window._toggle_muted()
    assert state == {"volume": 0.35, "mute": False}
    assert window._volume_scale.get_value() == 0.35

    window._volume_scale.set_value(0)
    assert state == {"volume": 0.0, "mute": True}
    window._toggle_muted()
    assert state == {"volume": 1.0, "mute": False}
    assert window._volume_scale.get_value() == 1

    window._on_volume_control_leave()
    assert window._volume_revealer.get_reveal_child() is False
    window._pipeline = None
    window.destroy()


def test_player_mute_button_scroll_adjusts_volume():
    state = {"volume": 0.5, "mute": False}
    pipeline = SimpleNamespace(
        get_property=lambda name: state[name],
        set_property=lambda name, value: state.__setitem__(name, value),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline
    window._volume_scale.set_value(0.5)
    model = window._mute_button.observe_controllers()
    controllers = [model.get_item(index) for index in range(model.get_n_items())]
    scroll_controller = next(
        controller
        for controller in controllers
        if isinstance(controller, Gtk.EventControllerScroll)
    )

    assert scroll_controller.emit("scroll", 0.0, -1.0) is True
    assert state == {"volume": 0.6, "mute": False}
    assert scroll_controller.emit("scroll", 0.0, 1.0) is True
    assert state == {"volume": 0.5, "mute": False}
    window._pipeline = None
    window.destroy()


def test_player_sliders_disable_gtk_hold_to_fine_tune():
    window = VideoPlayerWindow()

    for scale in (window._seek_scale, window._volume_scale):
        model = scale.observe_controllers()
        controllers = [
            model.get_item(index) for index in range(model.get_n_items())
        ]
        assert not any(
            isinstance(controller, Gtk.GestureLongPress)
            for controller in controllers
        )

    window.destroy()


def test_player_progress_slider_consumes_scroll_wheel_input():
    window = VideoPlayerWindow()
    model = window._seek_scale.observe_controllers()
    controllers = [model.get_item(index) for index in range(model.get_n_items())]
    scroll_controller = next(
        controller
        for controller in controllers
        if isinstance(controller, Gtk.EventControllerScroll)
        and controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    )

    assert scroll_controller.emit("scroll", 0.0, 1.0) is True
    window.destroy()


def test_seek_hold_pauses_without_movement_and_resumes_only_previous_playback():
    state = {"current": Gst.State.PLAYING}
    calls = []

    def set_state(value):
        calls.append(value)
        state["current"] = value

    pipeline = SimpleNamespace(
        get_state=lambda _timeout: (
            Gst.StateChangeReturn.SUCCESS,
            state["current"],
            Gst.State.VOID_PENDING,
        ),
        set_state=set_state,
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline

    window._begin_seek_hold()
    assert calls == [Gst.State.PAUSED]
    window._end_seek_hold()
    assert calls == [Gst.State.PAUSED, Gst.State.PLAYING]

    calls.clear()
    state["current"] = Gst.State.PAUSED
    window._begin_seek_hold()
    window._end_seek_hold()
    assert calls == []

    window._pipeline = None
    window.destroy()


def test_gtk_range_dragging_state_drives_both_seek_hold_edges():
    calls = []
    state = {"dragging": False}
    window = SimpleNamespace(
        _seek_range_dragging=False,
        _begin_seek_hold=lambda: calls.append("press"),
        _end_seek_hold=lambda: calls.append("release"),
    )
    scale = SimpleNamespace(
        has_css_class=lambda name: name == "dragging" and state["dragging"]
    )

    state["dragging"] = True
    VideoPlayerWindow._on_seek_css_classes_changed(window, scale, None)
    VideoPlayerWindow._on_seek_css_classes_changed(window, scale, None)
    state["dragging"] = False
    VideoPlayerWindow._on_seek_css_classes_changed(window, scale, None)

    assert calls == ["press", "release"]


def test_real_scale_dragging_class_pauses_and_resumes_pipeline():
    state = {"current": Gst.State.PLAYING}
    calls = []

    def set_state(value):
        calls.append(value)
        state["current"] = value

    window = VideoPlayerWindow()
    window._pipeline = SimpleNamespace(
        get_state=lambda _timeout: (
            Gst.StateChangeReturn.SUCCESS,
            state["current"],
            Gst.State.VOID_PENDING,
        ),
        set_state=set_state,
    )

    window._seek_scale.add_css_class("dragging")
    window._seek_scale.remove_css_class("dragging")

    assert calls == [Gst.State.PAUSED, Gst.State.PLAYING]
    window._pipeline = None
    window.destroy()


def test_old_seek_completion_cannot_resume_underneath_a_new_drag():
    calls = []
    pipeline = SimpleNamespace(set_state=calls.append)
    window = VideoPlayerWindow()
    window._pipeline = pipeline
    window._seek_press_pipeline = pipeline
    window._seek_range_dragging = True

    window._finish_seek_hold(
        pipeline,
        window._seek_worker_generation,
        resume=True,
    )

    assert calls == []
    assert window._seek_press_pipeline is pipeline

    window._seek_range_dragging = False
    window._seek_worker_thread = object()
    window._finish_seek_hold(
        pipeline,
        window._seek_worker_generation,
        resume=True,
    )
    assert calls == []
    assert window._seek_press_pipeline is pipeline

    window._seek_worker_thread = None
    window._finish_seek_hold(
        pipeline,
        window._seek_worker_generation,
        resume=True,
    )
    assert calls == [Gst.State.PLAYING]
    assert window._seek_press_pipeline is None
    window._pipeline = None
    window.destroy()


def test_seek_worker_prerolls_every_superseding_commit_before_resume(monkeypatch):
    idle_callbacks = []
    seeks = []
    states = []
    window = VideoPlayerWindow()

    def get_state(timeout):
        if timeout == player_module._SEEK_COMMIT_TIMEOUT_NS and len(seeks) == 1:
            window._queue_seek_work(20 * Gst.SECOND, final=True)
        return (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PAUSED,
            Gst.State.VOID_PENDING,
        )

    pipeline = SimpleNamespace(
        seek=lambda rate, format_, flags, start_type, start, stop_type, stop: (
            seeks.append((rate, start)) or True
        ),
        get_state=get_state,
        set_state=states.append,
    )
    monkeypatch.setattr(player_module.os, "setpriority", lambda *_args: None)
    monkeypatch.setattr(
        player_module.GLib,
        "idle_add",
        lambda callback, *args: idle_callbacks.append((callback, args)) or 41,
    )
    window._pipeline = pipeline
    window._seek_press_pipeline = pipeline
    window._resume_after_seek_press = True
    window._seek_worker_thread = object()
    window._seek_worker_target_ns = 10 * Gst.SECOND
    window._seek_worker_final = True
    window._seek_worker_resume = True

    window._run_seek_worker(pipeline, window._seek_worker_generation)

    assert seeks == [(1.0, 10 * Gst.SECOND), (1.0, 20 * Gst.SECOND)]
    assert states == []
    assert len(idle_callbacks) == 1
    callback, args = idle_callbacks.pop()
    callback(*args)
    assert states == [Gst.State.PLAYING]
    assert window._seek_press_pipeline is None
    window._pipeline = None
    window.destroy()


def test_player_uses_requested_volume_icons():
    assert _volume_icon_name(0) == "speaker-0-symbolic"
    assert _volume_icon_name(0.01) == "speaker-2-symbolic"
    assert _volume_icon_name(1 / 3) == "speaker-2-symbolic"
    assert _volume_icon_name(0.34) == "speaker-2-symbolic"
    assert _volume_icon_name(2 / 3) == "speaker-2-symbolic"
    assert _volume_icon_name(0.67) == "speaker-3-symbolic"
    assert _volume_icon_name(1) == "speaker-3-symbolic"


def test_fullscreen_toggle_and_video_double_click_use_the_same_action():
    calls = []
    window = SimpleNamespace(
        is_fullscreen=lambda: False,
        fullscreen=lambda: calls.append("fullscreen"),
        unfullscreen=lambda: calls.append("unfullscreen"),
    )

    VideoPlayerWindow._toggle_fullscreen(window)
    assert calls == ["fullscreen"]

    window._toggle_playback = lambda: calls.append("playback")
    window._toggle_fullscreen = lambda: calls.append("double-click")
    VideoPlayerWindow._on_video_pressed(window, None, 1, 0, 0)
    VideoPlayerWindow._on_video_pressed(window, None, 2, 0, 0)
    assert calls == ["fullscreen", "playback", "playback", "double-click"]


def test_single_video_click_toggles_playback_immediately():
    calls = []
    window = SimpleNamespace(_toggle_playback=lambda: calls.append("playback"))

    VideoPlayerWindow._on_video_pressed(window, None, 1, 0, 0)

    assert calls == ["playback"]


def test_escape_leaves_fullscreen_before_it_closes_the_player():
    calls = []
    window = SimpleNamespace(
        is_fullscreen=lambda: True,
        unfullscreen=lambda: calls.append("unfullscreen"),
        set_visible=lambda visible: calls.append(("visible", visible)),
    )

    assert VideoPlayerWindow._on_key_pressed(window, None, Gdk.KEY_Escape, 0, 0) is True
    assert calls == ["unfullscreen"]


def test_player_control_css_parses_cleanly():
    errors = []
    provider = Gtk.CssProvider()
    provider.connect(
        "parsing-error",
        lambda _provider, section, error: errors.append((section, error)),
    )

    provider.load_from_string(_PLAYER_CSS)

    assert errors == []
    assert ".clipper-player-timeline trough" in _PLAYER_CSS
    assert ".clipper-player-timeline highlight" in _PLAYER_CSS
    assert ".clipper-player-timeline slider" in _PLAYER_CSS
    assert ".clipper-player-volume slider" in _PLAYER_CSS


def test_player_resizes_when_the_paintable_reports_video_dimensions():
    paintable = SimpleNamespace(
        get_intrinsic_width=lambda: 2560,
        get_intrinsic_height=lambda: 1440,
    )
    window = VideoPlayerWindow()
    window._measure_player_chrome_height = lambda _width: 96

    window._resize_for_video(paintable)

    assert window.get_default_size() == (1280, 816)
    window.destroy()


def test_player_can_be_sized_from_metadata_before_it_is_presented(tmp_path):
    scheduled = []
    window = VideoPlayerWindow(
        idle_add=lambda callback, *args: scheduled.append((callback, args)) or 41
    )
    window._measure_player_chrome_height = lambda _width: 96
    clip_path = tmp_path / "clip.mkv"

    window.open_clip(clip_path, video_dimensions=(1920, 1080))

    assert window.get_visible() is False
    assert window.get_default_size() == (1280, 816)
    window._load_source_id = None
    window.destroy()


def test_video_discovery_uses_pixel_aspect_ratio(tmp_path):
    clip_path = tmp_path / "anamorphic.mkv"
    clip_path.touch()
    stream = SimpleNamespace(
        get_width=lambda: 720,
        get_height=lambda: 576,
        get_par_num=lambda: 64,
        get_par_denom=lambda: 45,
    )
    info = SimpleNamespace(get_video_streams=lambda: [stream])
    calls = []
    discoverer = SimpleNamespace(
        discover_uri=lambda uri: calls.append(uri) or info,
    )

    dimensions = discover_video_dimensions(
        clip_path,
        discoverer_factory=lambda timeout: calls.append(timeout) or discoverer,
    )

    assert dimensions == (1024, 576)
    assert calls == [2 * Gst.SECOND, clip_path.resolve().as_uri()]


def test_missing_clip_uses_a_clear_in_window_error_state(tmp_path):
    scheduled = []
    window = VideoPlayerWindow(
        idle_add=lambda callback, *args: scheduled.append((callback, args)) or 41
    )
    missing_clip = tmp_path / "missing.mkv"

    window.open_clip(missing_clip, "Missing clip.mkv")

    assert window.get_title() == "Missing clip.mkv — Clipper"
    assert len(scheduled) == 1
    callback, args = scheduled.pop()
    callback(*args)
    assert window._content_stack.get_visible_child_name() == "error"
    assert window._error_page.get_title() == "Clip unavailable"
    assert window._error_page.get_description() == "The video file could not be found."

    # The fake scheduler returned an ID after invoking no GLib source. Avoid
    # asking GLib to remove it when the test tears down the window.
    window._load_source_id = None
    window.destroy()


def test_player_keyboard_controls_toggle_and_seek_media():
    calls = []

    pipeline = SimpleNamespace(
        get_state=lambda _timeout: (None, Gst.State.PLAYING, Gst.State.VOID_PENDING),
        set_state=lambda state: calls.append(("state", state)),
        query_position=lambda _format: (True, 8 * Gst.SECOND),
        seek=lambda _rate, _format, _flags, _start_type, timestamp, _stop_type, _stop: (
            calls.append(("seek", timestamp)) or True
        ),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline
    window._duration_ns = 30 * Gst.SECOND

    assert window._on_key_pressed(None, Gdk.KEY_space, 0, 0) is True
    assert window._on_key_pressed(None, Gdk.KEY_Left, 0, 0) is True
    assert window._on_key_pressed(None, Gdk.KEY_Right, 0, 0) is True

    assert calls == [
        ("state", Gst.State.PAUSED),
        ("seek", 3 * Gst.SECOND),
        ("seek", 13 * Gst.SECOND),
    ]

    window._pipeline = None
    window.destroy()


def test_player_seeks_to_exact_timestamps_instead_of_nearby_keyframes():
    seeks = []
    pipeline = SimpleNamespace(
        seek=lambda rate, format_, flags, start_type, start, stop_type, stop: (
            seeks.append((rate, format_, flags, start_type, start, stop_type, stop))
            or True
        ),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline
    window._duration_ns = 30 * Gst.SECOND

    assert window._seek_to(13 * Gst.SECOND) is True

    assert seeks == [
        (
            1.0,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            13 * Gst.SECOND,
            Gst.SeekType.NONE,
            -1,
        )
    ]
    window._pipeline = None
    window.destroy()


def test_seek_drag_coalesces_preview_work_off_gtk_thread_and_finishes_exactly(
    monkeypatch,
    tmp_path,
):
    workers = []
    waits = []
    preview_factory_calls = []
    priorities = []

    class DeferredThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            workers.append(self)

    class WakeupStub:
        def set(self):
            pass

        def clear(self):
            pass

        def wait(self, timeout):
            waits.append(timeout)
            return False

    monkeypatch.setattr(player_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        player_module.os,
        "setpriority",
        lambda which, who, value: priorities.append((which, who, value)),
    )
    monkeypatch.setattr(
        player_module.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    seeks = []
    preview_seeks = []
    preview_states = []
    preview_properties = {}
    preview_sink_connections = []
    pipeline = SimpleNamespace(
        seek=lambda rate, format_, flags, start_type, start, stop_type, stop: (
            seeks.append((rate, format_, flags, start_type, start, stop_type, stop))
            or True
        ),
        get_state=lambda _timeout: (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PLAYING,
            Gst.State.VOID_PENDING,
        ),
        set_state=lambda _state: None,
    )
    preview_pipeline = SimpleNamespace(
        set_property=lambda name, value: preview_properties.__setitem__(name, value),
        seek_simple=lambda format_, flags, timestamp: (
            preview_seeks.append((format_, flags, timestamp)) or True
        ),
        set_state=lambda state: preview_states.append(state)
        or Gst.StateChangeReturn.ASYNC,
        get_state=lambda _timeout: (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PAUSED,
            Gst.State.VOID_PENDING,
        ),
    )
    preview_sink = SimpleNamespace(
        connect=lambda signal, callback, *args: (
            preview_sink_connections.append(("connect", signal, callback, args)) or 19
        ),
        disconnect=lambda handler_id: preview_sink_connections.append(
            ("disconnect", handler_id)
        ),
    )
    preview_video_sink = object()

    def make_preview_pipeline():
        preview_factory_calls.append("pipeline")
        return preview_pipeline

    window = VideoPlayerWindow()
    window._new_seek_preview_pipeline = make_preview_pipeline
    window._new_seek_preview_sink = lambda: (preview_video_sink, preview_sink)
    window._seek_worker_wakeup = WakeupStub()
    window._pipeline = pipeline
    window._clip_path = tmp_path / "clip.mkv"
    window._duration_ns = 100 * Gst.SECOND
    window._seek_range_dragging = True
    window._begin_seek_hold()

    window._on_seek_change_value(None, None, 200)
    window._on_seek_change_value(None, None, 350)

    assert seeks == []
    assert len(workers) == 1
    # The GTK change-value path only records the target and starts a worker;
    # no decoder construction or frame extraction happens on that thread.
    assert preview_factory_calls == []
    assert window._seek_drag_target_ns == 35 * Gst.SECOND
    assert window._time_label.get_label() == "0:35 / 1:40"
    worker = workers.pop()
    worker.target(*worker.args)
    assert preview_factory_calls == ["pipeline"]
    assert priorities == [(player_module.os.PRIO_PROCESS, 0, 10)]
    assert seeks == []
    assert preview_seeks == [
        (
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH
            | Gst.SeekFlags.KEY_UNIT
            | Gst.SeekFlags.TRICKMODE_KEY_UNITS
            | Gst.SeekFlags.TRICKMODE_NO_AUDIO,
            35 * Gst.SECOND,
        )
    ]
    assert waits == [player_module._SEEK_PREVIEW_INTERVAL_SECONDS]

    window._on_seek_change_value(None, None, 420)
    window._seek_range_dragging = False
    window._end_seek_hold()
    assert len(workers) == 1
    worker = workers.pop()
    worker.target(*worker.args)
    assert seeks[-1] == (
        1.0,
        Gst.Format.TIME,
        Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
        Gst.SeekType.SET,
        42 * Gst.SECOND,
        Gst.SeekType.NONE,
        -1,
    )
    assert preview_states == [Gst.State.PAUSED, Gst.State.NULL]
    assert preview_sink_connections[-1] == ("disconnect", 19)
    assert seeks == [
        (
            1.0,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            42 * Gst.SECOND,
            Gst.SeekType.NONE,
            -1,
        )
    ]
    window._pipeline = None
    window.destroy()


def test_seek_preview_builds_a_separate_muted_cpu_frame_graph(tmp_path):
    properties = {}
    states = []
    waits = []
    sample_connections = []

    class AppSinkStub:
        def connect(self, signal, callback, *args):
            sample_connections.append(("connect", signal, callback, args))
            return 19

        def disconnect(self, handler_id):
            sample_connections.append(("disconnect", handler_id))

    preview_sink = AppSinkStub()
    preview_video_sink = object()
    preview_pipeline = SimpleNamespace(
        set_property=lambda name, value: properties.__setitem__(name, value),
        set_state=lambda state: states.append(state) or Gst.StateChangeReturn.ASYNC,
        get_state=lambda timeout: (
            waits.append(timeout)
            or (
                Gst.StateChangeReturn.SUCCESS,
                Gst.State.PAUSED,
                Gst.State.VOID_PENDING,
            )
        ),
    )
    window = VideoPlayerWindow()
    window._new_seek_preview_pipeline = lambda: preview_pipeline
    window._new_seek_preview_sink = lambda: (preview_video_sink, preview_sink)
    window._clip_path = tmp_path / "clip.mkv"

    result = window._ensure_seek_preview_pipeline(window._seek_worker_generation)

    assert result is preview_pipeline
    assert properties["video-sink"] is preview_video_sink
    assert properties["audio-sink"].get_factory().get_name() == "fakesink"
    assert properties["uri"] == window._clip_path.resolve().as_uri()
    assert properties["mute"] is True
    assert states == [Gst.State.PAUSED]
    assert waits == [2 * Gst.SECOND]
    assert sample_connections[0][0:2] == ("connect", "new-preroll")

    window._release_seek_preview_pipeline(window._seek_worker_generation)
    assert states == [Gst.State.PAUSED, Gst.State.NULL]
    assert sample_connections[-1] == ("disconnect", 19)
    window.destroy()


def test_seek_preview_pipeline_forces_software_decoding_away_from_the_gpu():
    pipeline = VideoPlayerWindow._new_seek_preview_pipeline()

    assert pipeline is not None
    assert pipeline.get_factory().get_name() == "playbin"
    assert (
        int(pipeline.get_property("flags"))
        & player_module._GST_PLAY_FLAG_FORCE_SW_DECODERS
    )
    pipeline.set_state(Gst.State.NULL)


def test_seek_preview_publishes_a_completed_cpu_frame():
    displayed = []
    preview_pipeline = object()
    window = SimpleNamespace(
        _seek_worker_generation=7,
        _seek_preview_pipeline=preview_pipeline,
        _seek_range_dragging=True,
        _picture=SimpleNamespace(set_paintable=displayed.append),
    )
    frame_data = player_module.GLib.Bytes.new(bytes(16 * 9 * 4))

    result = VideoPlayerWindow._publish_seek_preview_frame(
        window,
        frame_data,
        16,
        9,
        16 * 4,
        preview_pipeline,
        7,
    )

    assert result == player_module.GLib.SOURCE_REMOVE
    assert len(displayed) == 1
    assert displayed[0].get_width() == 16
    assert displayed[0].get_height() == 9


def test_player_keyboard_shortcuts_mute_fullscreen_and_adjust_volume():
    calls = []
    window = SimpleNamespace(
        _pipeline=object(),
        _toggle_muted=lambda: calls.append("mute"),
        _toggle_fullscreen=lambda: calls.append("fullscreen"),
        _change_volume=lambda direction: calls.append(("volume", direction)),
        _toggle_playback=lambda: calls.append("playback"),
        _seek_relative=lambda offset: calls.append(("seek", offset)),
        is_fullscreen=lambda: False,
        set_visible=lambda visible: calls.append(("visible", visible)),
    )

    for keyval in (Gdk.KEY_m, Gdk.KEY_f, Gdk.KEY_Up, Gdk.KEY_Down):
        assert VideoPlayerWindow._on_key_pressed(window, None, keyval, 0, 0) is True

    assert calls == [
        "mute",
        "fullscreen",
        ("volume", 1),
        ("volume", -1),
    ]


def test_player_letter_shortcuts_use_physical_base_layout_keys(monkeypatch):
    class FakeDisplay:
        def map_keycode(self, keycode):
            keyvals = {
                41: [Gdk.KEY_Cyrillic_a, Gdk.KEY_f],
                58: [Gdk.KEY_Cyrillic_softsign, Gdk.KEY_m],
            }[keycode]
            return True, [], keyvals

    monkeypatch.setattr(
        player_module.Gdk.Display,
        "get_default",
        lambda: FakeDisplay(),
    )
    calls = []
    window = SimpleNamespace(
        _pipeline=object(),
        _toggle_muted=lambda: calls.append("mute"),
        _toggle_fullscreen=lambda: calls.append("fullscreen"),
        is_fullscreen=lambda: False,
        set_visible=lambda visible: calls.append(("visible", visible)),
    )

    assert VideoPlayerWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_a, 41, 0)
    assert VideoPlayerWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_softsign, 58, 0)

    assert calls == ["fullscreen", "mute"]


def test_volume_keyboard_step_is_five_percent_and_clamped():
    state = {"volume": 0.5, "mute": False}
    pipeline = SimpleNamespace(
        set_property=lambda name, value: state.__setitem__(name, value),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline
    window._volume_scale.set_value(0.5)

    window._change_volume(1)
    assert window._volume_scale.get_value() == 0.55
    window._change_volume(-1)
    assert window._volume_scale.get_value() == 0.5

    window._volume_scale.set_value(0.98)
    window._change_volume(1)
    assert window._volume_scale.get_value() == 1

    window._pipeline = None
    window.destroy()


def test_releasing_player_stops_and_clears_media():
    calls = []
    pipeline = SimpleNamespace(
        set_state=lambda state: calls.append(("state", state)),
    )
    bus = SimpleNamespace(
        disconnect=lambda handler_id: calls.append(("disconnect", handler_id)),
        remove_signal_watch=lambda: calls.append("remove-watch"),
    )
    picture = SimpleNamespace(set_paintable=lambda value: calls.append(("paintable", value)))
    window = VideoPlayerWindow()
    window._picture = picture
    window._pipeline = pipeline
    window._bus = bus
    window._bus_handler_id = 11

    window._release_media()

    assert window._pipeline is None
    assert window._bus is None
    assert window._bus_handler_id is None
    assert calls == [
        ("paintable", None),
        ("disconnect", 11),
        "remove-watch",
        ("state", Gst.State.NULL),
    ]
    window.destroy()


def test_escape_is_captured_before_controls_and_stops_active_pipeline():
    calls = []
    pipeline = SimpleNamespace(
        set_state=lambda state: calls.append(("state", state)),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline

    assert window._key_controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    window.set_visible(True)
    assert window._on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
    assert window.get_visible() is False
    assert window._pipeline is None
    assert calls == [("state", Gst.State.NULL)]


def test_window_close_hides_and_stops_active_pipeline():
    calls = []
    pipeline = SimpleNamespace(
        set_state=lambda state: calls.append(("state", state)),
    )
    window = VideoPlayerWindow()
    window._pipeline = pipeline
    window.set_visible(True)

    window.close()

    assert window.get_visible() is False
    assert window._pipeline is None
    assert calls == [("state", Gst.State.NULL)]


def test_helper_close_callback_bypasses_blocking_in_process_teardown():
    calls = []
    pipeline = SimpleNamespace(
        set_property=lambda *_args: calls.append("unexpected mute"),
        set_state=lambda *_args: calls.append("unexpected state change"),
    )
    window = VideoPlayerWindow(closed_callback=lambda: calls.append("exit helper"))
    window._pipeline = pipeline

    window._on_hide()

    assert calls == ["exit helper"]
    assert window._pipeline is pipeline
    window._pipeline = None
    window.destroy()


def test_player_audio_sink_uses_a_dedicated_restore_identity():
    sink = VideoPlayerWindow._new_audio_sink()

    assert sink is not None
    assert sink.get_property("client-name") == "Clipper player"


def test_loading_clip_clears_any_persisted_mute(monkeypatch, tmp_path):
    properties = []
    bus = SimpleNamespace(
        add_signal_watch=lambda: None,
        connect=lambda *_args: 17,
    )
    pipeline = SimpleNamespace(
        set_property=lambda name, value: properties.append((name, value)),
        get_bus=lambda: bus,
        set_state=lambda _state: Gst.StateChangeReturn.ASYNC,
    )
    sink = SimpleNamespace(get_property=lambda _name: None)
    window = VideoPlayerWindow(
        pipeline_factory=lambda: pipeline,
        sink_factory=lambda: sink,
        audio_sink_factory=lambda: None,
    )
    window._load_generation = 4
    monkeypatch.setattr(player_module.GLib, "timeout_add", lambda *_args: 99)
    clip_path = tmp_path / "clip.mkv"
    clip_path.touch()

    window._load_clip(clip_path, 4)

    assert ("mute", False) in properties
    assert ("volume", 1.0) in properties
    window._pipeline = None
    window._bus = None
    window._bus_handler_id = None
    window._position_source_id = None
    window.destroy()


def test_loading_clip_uses_outer_color_bin_and_inner_gtk_paintable(monkeypatch, tmp_path):
    properties = {}
    bus = SimpleNamespace(
        add_signal_watch=lambda: None,
        connect=lambda *_args: 17,
    )
    pipeline = SimpleNamespace(
        set_property=lambda name, value: properties.__setitem__(name, value),
        get_bus=lambda: bus,
        set_state=lambda _state: Gst.StateChangeReturn.ASYNC,
    )
    outer_sink = object()
    paintable = object()
    inner_sink = SimpleNamespace(
        get_property=lambda name: paintable if name == "paintable" else None
    )
    sink_result = ClipperVideoSink(outer_sink, inner_sink, True)
    window = VideoPlayerWindow(
        pipeline_factory=lambda: pipeline,
        sink_factory=lambda: sink_result,
        audio_sink_factory=lambda: None,
    )
    window._load_generation = 3
    monkeypatch.setattr(player_module.GLib, "timeout_add", lambda *_args: 99)
    monkeypatch.setattr(
        window,
        "_set_video_paintable",
        lambda value: properties.__setitem__("paintable", value),
    )
    clip_path = tmp_path / "clip.mkv"
    clip_path.touch()

    window._load_clip(clip_path, 3)

    assert properties["video-sink"] is outer_sink
    assert properties["paintable"] is paintable
    assert window._paintable_sink is inner_sink
    window._pipeline = None
    window._bus = None
    window._bus_handler_id = None
    window._position_source_id = None
    window.destroy()


def test_helper_uses_clipper_desktop_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        player_module.GLib,
        "set_prgname",
        lambda value: calls.append(("program", value)),
    )
    monkeypatch.setattr(
        player_module.GLib,
        "set_application_name",
        lambda value: calls.append(("application", value)),
    )

    player_module._configure_desktop_identity()

    assert calls == [
        ("program", player_module.APP_ID),
        ("application", "Clipper"),
    ]


def test_helper_imports_exported_wayland_parent_before_presenting(monkeypatch):
    calls = []

    class SurfaceStub:
        def set_transient_for_exported(self, parent_handle):
            calls.append(("parent", parent_handle))
            return True

    surface = SurfaceStub()
    window = SimpleNamespace(
        realize=lambda: calls.append("realize"),
        get_surface=lambda: surface,
    )
    monkeypatch.setattr(
        player_module,
        "GdkWayland",
        SimpleNamespace(WaylandToplevel=SurfaceStub),
    )

    assert player_module._set_exported_parent(window, "clipper-parent-handle") is True
    assert calls == ["realize", ("parent", "clipper-parent-handle")]


def test_released_pipeline_memory_is_collected_and_trimmed(monkeypatch):
    calls = []
    monkeypatch.setattr(player_module.gc, "collect", lambda: calls.append("collect"))
    monkeypatch.setattr(player_module, "_MALLOC_TRIM", lambda padding: calls.append(padding))

    assert player_module._reclaim_released_media_memory() == player_module.GLib.SOURCE_REMOVE
    assert calls == ["collect", 0]
