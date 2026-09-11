from types import SimpleNamespace

import editor_preview
import pytest
from editor_model import (
    MAX_GAIN,
    AudioSetting,
    AudioTrack,
    EditorProject,
    Segment,
    Source,
    effective_audio_gain,
)
from editor_preview import (
    EditorPreview,
    TimelineComposition,
    _dispatch_level_message,
    _set_listening_volume,
    _set_track_gain,
)
from gi.repository import Gst


class FakeProject:
    output_duration_us = 10_000_000
    segments = [
        SimpleNamespace(
            source_start_us=1_000,
            source_end_us=10_001_000,
            duration_us=10_000_000,
        )
    ]

    @staticmethod
    def output_to_source_us(output_us):
        return output_us + 1_000


def test_preview_failure_logs_element_and_debug_context(monkeypatch):
    records = []
    monkeypatch.setattr(editor_preview._LOG, "error", lambda *args: records.append(args))
    message = SimpleNamespace(
        src=SimpleNamespace(get_path_string=lambda: "/pipeline/audio-sink"),
        parse_error=lambda: (RuntimeError("device disconnected"), "PipeWire connection lost"),
    )
    assert editor_preview._log_pipeline_error(message, "Source preview") == "device disconnected"
    assert records[0][2] == "/pipeline/audio-sink"
    assert records[0][4] == "PipeWire connection lost"


class FakePipeline:
    def __init__(self, position_ns=2_001_000_000):
        self.calls = []
        self.position_ns = position_ns

    def query_position(self, format_):
        self.calls.append(("query", format_))
        return True, self.position_ns

    def seek_simple(self, format_, flags, position):
        self.calls.append(("seek", format_, flags, position))
        return True

    def set_state(self, state):
        self.calls.append(("state", state))
        return Gst.StateChangeReturn.SUCCESS


class FakeVolume:
    def __init__(self):
        self.volume = None
        self.property_name = None

    def set_property(self, name, value):
        assert name in {"volume", "volume-full-range"}
        self.property_name = name
        self.volume = value


class FakeAudioSink:
    def __init__(self):
        self.properties = {}

    def find_property(self, name):
        return object() if name in {"volume", "mute"} else None

    def set_property(self, name, value):
        self.properties[name] = value


class FakeProperties:
    def __init__(self):
        self.properties = {}

    def set_property(self, name, value):
        self.properties[name] = value


class FakeSpinner:
    def __init__(self):
        self.visible = False
        self.spinning = False

    def set_visible(self, visible):
        self.visible = visible

    def set_spinning(self, spinning):
        self.spinning = spinning


class FakePaintable:
    def __init__(self, current_image):
        self.current_image = current_image

    def get_current_image(self):
        return self.current_image


class FakePicture:
    def __init__(self, paintable):
        self.paintable = paintable
        self.shown = []

    def get_paintable(self):
        return self.paintable

    def set_paintable(self, paintable):
        self.paintable = paintable
        self.shown.append(paintable)


class FakeLevelStructure:
    def __init__(self, peaks, decay=None, name="level"):
        self.peaks = peaks
        self.decay = peaks if decay is None else decay
        self.name = name

    def get_name(self):
        return self.name

    def get_value(self, field):
        return {"peak": self.peaks, "decay": self.decay}[field]


def make_preview(monkeypatch, position=2_000_000, playing=False):
    timers = {}
    monkeypatch.setattr(
        editor_preview.GLib,
        "timeout_add",
        lambda _interval, callback: timers.setdefault(1, callback) and 1,
    )
    monkeypatch.setattr(editor_preview.GLib, "source_remove", timers.pop)
    preview = EditorPreview.__new__(EditorPreview)
    preview.project = FakeProject()
    preview.changed_callback = None
    preview.state_changed_callback = None
    preview.output_position_us = position
    preview.playing = playing
    preview._resume_watchdog_id = None
    preview._timeline_composition = None
    preview._held_paintable = None
    preview._loading = False
    preview._legacy_paintable = "legacy"
    preview.picture = SimpleNamespace(set_paintable=lambda _paintable: None)
    preview.pipeline = FakePipeline()
    return preview, timers


def make_gap_project(*, first_gain=1.0):
    source = Source(
        path="/tmp/clipper-speculative-source.mkv",
        size=123,
        mtime_ns=456,
        duration_us=10_000_000,
        video_stream_index=0,
        audio_tracks=[AudioTrack("a0", 0, 1, "Audio")],
    )
    return EditorProject(
        version=1,
        project_id="speculative-project",
        source=source,
        segments=[
            Segment("first", 0, 2_000_000, {"a0": AudioSetting(first_gain)}),
            Segment("second", 6_000_000, 10_000_000, {"a0": AudioSetting()}),
        ],
    )


def configure_speculative_preview(monkeypatch, *, position=1_000_000):
    preview, _old_timers = make_preview(monkeypatch, position=position)
    project = make_gap_project()
    preview.project = project
    preview._speculative_composition = None
    preview._idle_prepare_id = None
    preview._idle_prepare_deadline_id = None
    preview._scheduled_prepare_identity = None
    preview._speculative_identity = None
    preview._preparation_generation = 0
    preview._source_valid = True
    preview._closing = False
    preview._loading_generation = 0
    preview._project_structure_snapshot = preview._project_structure(project)
    preview._project_cache_snapshot = preview._project_identity(project, 0)
    preview.loading_spinner = FakeSpinner()
    preview.picture = FakePicture("paused-frame")

    timers = {}
    removed = []
    next_id = [1]

    def timeout_add(interval, callback):
        timer_id = next_id[0]
        next_id[0] += 1
        timers[timer_id] = (interval, callback)
        return timer_id

    def source_remove(timer_id):
        removed.append(timer_id)
        timers.pop(timer_id, None)

    monkeypatch.setattr(editor_preview.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(editor_preview.GLib, "source_remove", source_remove)
    return preview, timers, removed


class FakeSpeculativeComposition:
    instances = []
    ready = False

    def __init__(
        self,
        project,
        output_start_us,
        eos_callback,
        error_callback,
        ready_callback,
    ):
        self.project = project
        self.output_start_us = output_start_us
        self.eos_callback = eos_callback
        self.error_callback = error_callback
        self.ready_callback = ready_callback
        self.paintable = "timeline-frame"
        self.cleanup_calls = 0
        self.prepare_calls = 0
        self.start_calls = 0
        self.stop_growth_calls = 0
        self.loading_generation = 0
        self.preparation_generation = 0
        self.cache_identity = None
        self.audio_updates = []
        type(self).instances.append(self)

    def prepare(self):
        self.prepare_calls += 1
        return True

    def start(self):
        self.start_calls += 1
        return True

    def ready_for_playback(self):
        return self.ready

    def stop_speculative_growth(self):
        self.stop_growth_calls += 1

    def cleanup(self):
        self.cleanup_calls += 1

    def update_audio_setting(self, project, segment_id, track_id):
        self.project = project
        self.audio_updates.append((segment_id, track_id))
        return True

    def update_audio(self, project):
        self.project = project
        self.audio_updates.append("all")
        return True


def test_level_messages_report_stereo_post_gain_peaks_by_track():
    reported = []
    message = SimpleNamespace(
        type=Gst.MessageType.ELEMENT,
        src=SimpleNamespace(get_name=lambda: "game-level"),
        get_structure=lambda: FakeLevelStructure(
            [-8.5, -13.25],
            [-4.0, -9.0],
        ),
    )

    handled = _dispatch_level_message(
        message,
        {"game-level": "game"},
        lambda track_id, peaks, held: reported.append((track_id, peaks, held)),
    )

    assert handled is True
    assert reported == [("game", (-8.5, -13.25), (-4.0, -9.0))]


def test_level_messages_duplicate_mono_peaks_for_the_stereo_meter():
    reported = []
    message = SimpleNamespace(
        type=Gst.MessageType.ELEMENT,
        src=SimpleNamespace(get_name=lambda: "microphone-level"),
        get_structure=lambda: FakeLevelStructure([-22.0]),
    )

    _dispatch_level_message(
        message,
        {"microphone-level": "microphone"},
        lambda track_id, peaks, held: reported.append((track_id, peaks, held)),
    )

    assert reported == [("microphone", (-22.0, -22.0), (-22.0, -22.0))]


def test_timeline_branch_sends_only_one_seek_after_all_pads_are_discovered():
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._cleaned = False
    source = object()
    video_pad = object()
    audio_pad = object()
    branch = {
        "seek_scheduled": False,
        "no_more_pads": True,
        "linked_pads": {video_pad, audio_pad},
        "initially_blocked_pads": set(),
        "source": source,
    }
    calls = []
    composition._seek_branch = lambda candidate, item: calls.append((candidate, item))

    composition._maybe_seek_branch(branch)
    composition._maybe_seek_branch(branch)

    assert branch["seek_scheduled"] is True
    assert calls == [(source, branch)]


def test_timeline_readiness_probe_blocks_media_buffers_not_stream_events():
    probes = []

    class FakePad:
        def add_probe(self, probe_type, callback, branch):
            probes.append((probe_type, callback, branch))
            return 19

    pad = FakePad()
    composition = TimelineComposition.__new__(TimelineComposition)
    branch = {"linked_pads": set(), "probe_ids": {}}

    composition._watch_branch_pad(pad, branch)

    assert probes == [
        (
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER,
            composition._on_branch_pad_blocked,
            branch,
        )
    ]
    assert branch["probe_ids"] == {pad: 19}


def test_timeline_source_contains_async_preparation_state_changes():
    source = FakeProperties()

    editor_preview._configure_timeline_source(source, "file:///clip.mkv")

    assert source.properties == {
        "uri": "file:///clip.mkv",
        "async-handling": True,
    }


def test_timeline_video_sink_allows_brief_cut_transition_lateness():
    sink = FakeProperties()

    editor_preview._configure_timeline_video_sink(sink)

    assert sink.properties == {
        "max-lateness": editor_preview.TIMELINE_VIDEO_MAX_LATENESS_NS,
    }


def test_timeline_prerolls_only_the_current_segment_before_playback():
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.output_start_us = 3_100_000
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(
                source_start_us=index * 1_000_000,
                duration_us=2_000_000,
            )
            for index in range(5)
        ]
    )
    composition._branches = []
    appended = []
    composition._segment_at_output = lambda _output_us: (1, 100_000)
    composition._promote_active_branch = lambda _branch: None

    def append(index, source_start_us):
        appended.append((index, source_start_us))
        composition._branches.append({"index": index})

    composition._append_branch = append

    composition._build_branches()

    assert appended == [(1, 1_100_000)]
    assert composition._startup_end_index == 4


def test_timeline_prepares_only_the_immediate_next_segment():
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(source_start_us=index * 1_000_000)
            for index in range(5)
        ]
    )
    composition._branches = [{"index": 1}]
    appended = []

    def append(index, source_start_us):
        appended.append((index, source_start_us))
        composition._branches.append({"index": index})

    composition._append_branch = append

    composition._prepare_next_branch(1)

    assert appended == [(2, 2_000_000)]


def test_timeline_keeps_source_contiguous_splits_as_independent_branches():
    setting = {"a0": AudioSetting(gain=0.8)}
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(source_start_us=0, source_end_us=100_000, audio=setting),
            SimpleNamespace(
                source_start_us=100_000,
                source_end_us=133_334,
                audio={"a0": AudioSetting(gain=0.8)},
            ),
            SimpleNamespace(
                source_start_us=133_334,
                source_end_us=183_335,
                audio={"a0": AudioSetting(gain=0.8)},
            ),
            SimpleNamespace(
                source_start_us=300_000,
                source_end_us=500_000,
                audio=setting,
            ),
        ]
    )

    assert composition._coalesced_end_index(0) == 0
    assert composition._coalesced_end_index(3) == 3


def test_timeline_ready_window_counts_playback_time_across_dense_splits():
    audio = {"a0": AudioSetting()}
    durations = [
        1_750_035,
        750_015,
        666_680,
        1_216_691,
        900_018,
        1_250_025,
        183_337,
        33_334,
        200_004,
        50_001,
        316_673,
        33_334,
        33_334,
        150_003,
        333_340,
        50_001,
        216_671,
        166_670,
        150_003,
        4_466_756,
    ]
    source_starts = [
        0,
        4_233_418,
        7_016_807,
        7_683_487,
        10_033_534,
        15_616_979,
        22_967_126,
    ]
    dense_start_us = source_starts[-1]
    source_starts.extend(
        dense_start_us + sum(durations[6:index]) for index in range(7, 19)
    )
    source_starts.append(29_633_926)
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(
                source_start_us=start,
                source_end_us=start + duration,
                duration_us=duration,
                audio=audio,
            )
            for start, duration in zip(source_starts, durations, strict=False)
        ]
    )

    # Even source-contiguous segments stay separate so each one can have an
    # independent live gain. The ready window remains bounded by its branch
    # limit when dense cuts would otherwise exceed it.
    assert composition._coalesced_end_index(6) == 6
    assert composition._ready_window_end_index(0) == 7


def test_subframe_timeline_branch_becomes_ready_with_one_decoded_buffer():
    class Queue:
        @staticmethod
        def get_property(name):
            return {"current-level-time": 0, "current-level-buffers": 1}[name]

    composition = TimelineComposition.__new__(TimelineComposition)
    composition._cleaned = False
    composition.project = SimpleNamespace(
        source=SimpleNamespace(frame_duration_us=16_667)
    )
    branch = {
        "ready": False,
        "duration_us": 10_000,
        "preroll_target_us": 1,
        "queue_fill_timer_id": 7,
        "video_queue": Queue(),
    }
    marked = []
    composition._mark_branch_ready = lambda item, **options: marked.append(
        (item, options)
    )

    assert composition._finish_branch_queue_preroll(branch) is False
    assert branch["queue_fill_timer_id"] is None
    assert marked == [(branch, {"cancel_timer": False})]


def test_timeline_reports_the_first_buffer_after_playback_actually_starts(
    monkeypatch,
):
    callbacks = []
    monkeypatch.setattr(
        editor_preview.GLib,
        "idle_add",
        lambda callback, composition, generation: callbacks.append(
            (callback, composition, generation)
        )
        or 1,
    )
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._playing = False
    composition._playback_reported = False
    composition._playback_generation = 7
    composition.playback_started_callback = object()
    info = SimpleNamespace(get_buffer=lambda: object())

    assert composition._on_video_output(None, info) == Gst.PadProbeReturn.OK
    assert callbacks == []

    composition._playing = True
    assert composition._on_video_output(None, info) == Gst.PadProbeReturn.OK
    assert callbacks == [
        (composition.playback_started_callback, composition, 7)
    ]

    composition._on_video_output(None, info)
    assert len(callbacks) == 1


def test_loading_handoff_holds_frame_until_preroll_and_first_playing_frame(
    monkeypatch,
):
    timers = {}
    monkeypatch.setattr(
        editor_preview.GLib,
        "timeout_add",
        lambda interval, callback: timers.setdefault(1, (interval, callback)) and 1,
    )
    monkeypatch.setattr(editor_preview.GLib, "source_remove", timers.pop)
    preview = EditorPreview.__new__(EditorPreview)
    frozen = object()
    preview.picture = FakePicture(FakePaintable(frozen))
    preview.loading_spinner = FakeSpinner()
    preview._held_paintable = None
    preview._loading = False
    preview._legacy_frame_reported = False
    composition = SimpleNamespace(paintable="target-frame")
    preview._timeline_composition = composition

    preview._hold_current_frame()
    preview._set_loading(True)

    assert preview.picture.paintable is frozen
    assert preview.loading_spinner.visible is False
    assert preview.loading_spinner.spinning is False
    assert timers[1][0] == editor_preview.LOADING_SPINNER_DELAY_MS

    assert timers.pop(1)[1]() is False
    assert preview.loading_spinner.visible is True
    assert preview.loading_spinner.spinning is True

    preview._on_timeline_ready(composition)

    assert preview.picture.paintable == "target-frame"
    assert preview._loading is True

    preview._on_timeline_playback_started(composition)

    assert preview.loading_spinner.visible is False
    assert preview.loading_spinner.spinning is False


def test_brief_loading_never_shows_spinner(monkeypatch):
    timers = {}
    removed = []

    def timeout_add(interval, callback):
        timers[1] = (interval, callback)
        return 1

    def source_remove(timer_id):
        removed.append(timer_id)
        timers.pop(timer_id, None)

    monkeypatch.setattr(editor_preview.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(editor_preview.GLib, "source_remove", source_remove)
    preview = EditorPreview.__new__(EditorPreview)
    preview.loading_spinner = FakeSpinner()
    preview._loading = False
    preview._legacy_frame_reported = False

    preview._set_loading(True)
    delayed_reveal = timers[1][1]
    preview._set_loading(False)

    assert removed == [1]
    assert preview.loading_spinner.visible is False
    assert preview.loading_spinner.spinning is False
    assert delayed_reveal() is False
    assert preview.loading_spinner.visible is False
    assert preview.loading_spinner.spinning is False


def test_stale_timeline_playback_callback_cannot_hide_current_loading_indicator(
    monkeypatch,
):
    timers = {}
    monkeypatch.setattr(
        editor_preview.GLib,
        "timeout_add",
        lambda interval, callback: timers.setdefault(1, (interval, callback)) and 1,
    )
    monkeypatch.setattr(editor_preview.GLib, "source_remove", timers.pop)
    preview = EditorPreview.__new__(EditorPreview)
    preview.loading_spinner = FakeSpinner()
    preview._loading = False
    preview._legacy_frame_reported = False
    current = object()
    preview._timeline_composition = current
    preview._set_loading(True)
    timers.pop(1)[1]()

    preview._on_timeline_playback_started(object())

    assert preview._loading is True
    assert preview.loading_spinner.visible is True

    preview._on_timeline_playback_started(current, generation=0)

    assert preview._loading is True
    assert preview.loading_spinner.visible is True


def test_timeline_waits_for_a_nearby_successor_before_starting():
    states = []
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._start_requested = False
    composition._started = False
    composition._playing = False
    composition._current_prerolled = True
    composition._start_index = 1
    composition._start_offset_us = 900_000
    composition._startup_end_index = 2
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(duration_us=2_000_000),
            SimpleNamespace(duration_us=1_000_000),
            SimpleNamespace(duration_us=3_000_000),
        ]
    )
    successor = {"index": 2, "ready": False}
    composition._branches = [{"index": 1, "ready": True}, successor]
    composition.prepare = lambda: True
    composition.pipeline = SimpleNamespace(
        set_state=lambda state: states.append(state) or Gst.StateChangeReturn.SUCCESS
    )
    composition.error_callback = lambda _message: None

    assert composition.start() is True
    assert states == []
    assert composition._started is False

    successor["ready"] = True
    assert composition.start() is True
    assert states == [Gst.State.PLAYING]
    assert composition._started is True


def test_timeline_does_not_delay_start_when_the_next_cut_has_headroom():
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._start_index = 0
    composition._start_offset_us = 100_000
    composition._startup_end_index = 1
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(duration_us=2_000_000),
            SimpleNamespace(duration_us=3_000_000),
        ]
    )
    composition._branches = [
        {"index": 0, "ready": True},
        {"index": 1, "ready": True},
    ]

    assert composition._startup_successor_ready() is True


def test_ready_branch_extends_bounded_window_before_start():
    prepared = []
    start_readiness = []
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._cleaned = False
    composition._current_prerolled = True
    composition._start_requested = True
    composition._start_index = 2
    composition._startup_end_index = 4
    composition._lookahead_end_index = 4
    composition._branches = []
    composition.diagnostics = {"branches_ready": 0}
    composition._remove_decode_throttle = lambda _branch: None

    def prepare_next(index):
        prepared.append(index)
        composition._branches.append({"index": index + 1, "ready": False})

    composition._prepare_next_branch = prepare_next
    composition.start = lambda: start_readiness.append(
        composition._startup_successor_ready()
    )
    short_successor = {
        "index": 3,
        "ready": False,
        "queue_fill_timer_id": None,
    }
    composition._branches.append(short_successor)

    composition._mark_branch_ready(short_successor)

    assert prepared == [3]
    assert start_readiness == [False]


def test_timeline_starts_settle_timer_only_when_video_moves_to_a_new_segment(
    monkeypatch,
):
    scheduled = []
    monkeypatch.setattr(
        editor_preview.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 7,
    )
    first_pad = object()
    second_pad = object()
    active = {"pad": first_pad}
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.video_concat = SimpleNamespace(
        get_property=lambda _name: active["pad"]
    )
    composition._branches = [
        {"index": 1, "video_concat_pad": first_pad, "ready": True},
        {"index": 2, "video_concat_pad": second_pad, "ready": True},
    ]
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(duration_us=1_000_000),
            SimpleNamespace(duration_us=1_000_000),
            SimpleNamespace(duration_us=3_916_745),
        ]
    )
    composition._last_active_index = 1
    composition._start_index = 1
    composition._advance_pending = False
    composition._advance_timer_id = None
    composition._promote_active_branch = lambda _branch: None

    composition._on_active_pad_changed()
    active["pad"] = second_pad
    composition._on_active_pad_changed()

    assert scheduled == [
        (
            editor_preview._timeline_prepare_delay_ms(3_916_745),
            composition._advance_branches,
        )
    ]
    assert composition._last_active_index == 2


def test_dense_cuts_extend_decoder_lookahead_while_settle_is_pending(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        editor_preview.GLib,
        "idle_add",
        lambda callback: scheduled.append(callback) or 11,
    )
    segments = [
        SimpleNamespace(
            source_start_us=index * 1_000_000,
            source_end_us=index * 1_000_000 + 50_000,
            duration_us=50_000,
            audio={"a0": AudioSetting(gain=index / 20)},
        )
        for index in range(17)
    ]
    active_pad = object()
    active = {
        "index": 6,
        "end_index": 7,
        "duration_us": 216_671,
        "video_concat_pad": active_pad,
        "video_queue": FakeProperties(),
        "ready": True,
    }
    branches = [active]
    branches.extend(
        {
            "index": index,
            "end_index": index,
            "video_concat_pad": object(),
            "ready": True,
        }
        for index in range(8, 15)
    )
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.project = SimpleNamespace(segments=segments)
    composition.video_concat = SimpleNamespace(
        get_property=lambda _name: active_pad,
    )
    composition._branches = branches
    composition._last_active_index = 5
    composition._advance_pending = True
    composition._advance_timer_id = 7
    composition._lookahead_end_index = 14
    composition._lookahead_prepare_id = None
    composition._lookahead_request_index = None
    composition._speculative_growth_stopped = False
    composition._start_requested = True
    composition._cleaned = False
    composition.diagnostics = {"unready_activations": 0}
    prepared = []
    composition._prepare_next_branch = prepared.append

    composition._on_active_pad_changed()

    assert composition._last_active_index == 6
    assert len(scheduled) == 1
    assert scheduled[0]() is False
    assert composition._lookahead_end_index == 15
    assert prepared == [14]


def test_timeline_does_not_falsely_mark_an_empty_activated_branch_ready(monkeypatch):
    monkeypatch.setattr(editor_preview.GLib, "timeout_add", lambda *_args: 7)
    active_pad = object()
    branch = {
        "index": 2,
        "video_concat_pad": active_pad,
        "video_queue": FakeProperties(),
        "ready": False,
    }
    composition = TimelineComposition.__new__(TimelineComposition)
    composition.video_concat = SimpleNamespace(
        get_property=lambda _name: active_pad
    )
    composition._branches = [branch]
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(duration_us=1_000_000),
            SimpleNamespace(duration_us=1_000_000),
            SimpleNamespace(duration_us=1_000_000),
        ]
    )
    composition._last_active_index = 1
    composition._advance_pending = False
    composition._advance_timer_id = None
    composition.diagnostics = {"unready_activations": 0}

    composition._on_active_pad_changed()

    assert branch["ready"] is False
    assert composition.diagnostics["unready_activations"] == 1


def test_timeline_prepares_late_in_long_segments_but_not_too_late_in_short_ones():
    assert editor_preview._timeline_prepare_delay_ms(3_916_745) == 2_167
    assert editor_preview._timeline_prepare_delay_ms(1_583_365) == 300
    assert editor_preview._timeline_prepare_delay_ms(400_000) == 300


def test_later_timeline_branch_paces_video_decoder_input_only():
    probes = []

    class FakePad:
        def add_probe(self, probe_type, callback, branch):
            probes.append((probe_type, callback, branch))
            return 42

    video_pad = FakePad()
    decoder = SimpleNamespace(
        get_factory=lambda: SimpleNamespace(
            get_metadata=lambda _name: "Codec/Decoder/Video/Hardware"
        ),
        get_static_pad=lambda name: video_pad if name == "sink" else None,
    )
    composition = TimelineComposition.__new__(TimelineComposition)
    branch = {
        "throttle_decode": True,
        "decode_throttle_probe_id": None,
        "decode_throttle_pad": None,
    }

    composition._on_deep_element_added(None, None, decoder, branch)

    assert probes == [
        (Gst.PadProbeType.BUFFER, composition._on_decode_input, branch)
    ]
    assert branch["decode_throttle_pad"] is video_pad
    assert branch["decode_throttle_probe_id"] == 42


def test_standby_video_decoder_input_is_rate_limited_until_ready(monkeypatch):
    interval_ns = editor_preview.TIMELINE_STANDBY_DECODE_INTERVAL_NS
    monotonic_values = iter((0, 1_000_000, interval_ns))
    sleeps = []
    monkeypatch.setattr(
        editor_preview.time,
        "monotonic_ns",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(editor_preview.time, "sleep", sleeps.append)
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._cleaned = False
    branch = {
        "ready": False,
        "decode_throttle_next_ns": None,
        "decode_throttle_interval_ns": interval_ns,
        "decode_throttle_buffers": 0,
    }

    assert composition._on_decode_input(None, None, branch) == Gst.PadProbeReturn.OK
    assert composition._on_decode_input(None, None, branch) == Gst.PadProbeReturn.OK

    assert sleeps == [(interval_ns - 1_000_000) / Gst.SECOND]
    assert branch["decode_throttle_buffers"] == 2
    assert branch["decode_throttle_next_ns"] == interval_ns * 2

    branch["ready"] = True
    assert composition._on_decode_input(None, None, branch) == Gst.PadProbeReturn.REMOVE


def test_retired_timeline_branch_cannot_restart_with_parent_pipeline():
    calls = []
    element = SimpleNamespace(
        set_locked_state=lambda locked: calls.append(("locked", locked)),
        set_state=lambda state: calls.append(("state", state)),
    )
    branch = {"retired": False, "elements": [element]}

    TimelineComposition._retire_branch(branch)

    assert branch["retired"] is True
    assert calls == [("locked", True), ("state", Gst.State.NULL)]


def test_promoting_active_branch_replaces_tiny_preroll_with_time_headroom():
    queue = FakeProperties()

    TimelineComposition._promote_active_branch({"video_queue": queue})

    assert queue.properties == {
        "max-size-buffers": 0,
        "max-size-time": editor_preview.TIMELINE_ACTIVE_VIDEO_BUFFER_US * 1000,
    }


def test_resuming_preserves_decoder_state_when_playback_advances(monkeypatch):
    preview, timers = make_preview(monkeypatch)

    preview.play()

    assert [call[0] for call in preview.pipeline.calls] == ["query", "state"]
    assert preview.pipeline.calls[1] == ("state", Gst.State.PLAYING)
    assert preview.playing is True

    preview.pipeline.position_ns += 100_000_000
    assert timers[1]() is False
    assert "seek" not in [call[0] for call in preview.pipeline.calls]


def test_stalled_resume_seeks_to_the_exact_captured_output_time(monkeypatch):
    preview, timers = make_preview(monkeypatch)
    positions = []
    preview.changed_callback = positions.append

    preview.play()
    assert timers[1]() is False

    seek = next(call for call in preview.pipeline.calls if call[0] == "seek")
    assert seek[3] == 2_001_000_000
    assert positions[-1] == 2_000_000
    assert preview.output_position_us == 2_000_000


def test_source_position_maps_to_the_correct_output_time_across_segments(monkeypatch):
    preview, _timers = make_preview(monkeypatch)
    preview.project = SimpleNamespace(
        segments=[
            SimpleNamespace(
                source_start_us=1_000_000,
                source_end_us=3_000_000,
                duration_us=2_000_000,
            ),
            SimpleNamespace(
                source_start_us=6_000_000,
                source_end_us=9_000_000,
                duration_us=3_000_000,
            ),
        ]
    )
    preview.pipeline.position_ns = 6_500_000_000
    positions = []
    preview.changed_callback = positions.append

    source_us = preview._sync_position_from_pipeline()

    assert source_us == 6_500_000
    assert preview.output_position_us == 2_500_000
    assert positions == [2_500_000]


def test_playback_jumps_over_a_deleted_middle_segment(monkeypatch):
    preview, _timers = make_preview(monkeypatch, position=1_900_000, playing=True)
    preview.project = SimpleNamespace(
        output_duration_us=5_000_000,
        segments=[
            SimpleNamespace(
                source_start_us=0,
                source_end_us=2_000_000,
                duration_us=2_000_000,
            ),
            SimpleNamespace(
                source_start_us=6_000_000,
                source_end_us=9_000_000,
                duration_us=3_000_000,
            ),
        ],
        output_to_source_us=lambda output_us: (
            output_us if output_us < 2_000_000 else output_us + 4_000_000
        ),
        segment_at_output=lambda output_us: (
            (
                SimpleNamespace(audio={})
                if output_us < 2_000_000
                else SimpleNamespace(audio={})
            ),
            0,
        ),
    )
    preview.pipeline.position_ns = 2_010_000_000
    positions = []
    prepared = []
    preview.changed_callback = positions.append
    preview._prepare_timeline = lambda **options: prepared.append(options) or True

    source_us = preview._sync_position_from_pipeline()

    assert source_us == 6_000_000
    assert preview.output_position_us == 2_000_000
    assert positions == [2_000_000]
    assert prepared == [{"play_requested": True}]
    assert "seek" not in [call[0] for call in preview.pipeline.calls]
    assert preview.playing is True


def test_interactive_seek_uses_fast_keyframe_flags(monkeypatch):
    preview, _timers = make_preview(monkeypatch)

    preview.seek_output(3_000_000, accurate=False)

    seek = next(call for call in preview.pipeline.calls if call[0] == "seek")
    assert seek[2] == Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
    assert seek[3] == 3_001_000_000


def test_scrub_frame_uses_accurate_paused_decoder_without_timeline_rebuild(
    monkeypatch,
):
    preview, _timers = make_preview(monkeypatch, playing=True)
    preview.project = SimpleNamespace(
        output_duration_us=10_000_000,
        segments=[
            SimpleNamespace(
                source_start_us=0,
                source_end_us=2_000_000,
                duration_us=2_000_000,
                audio={},
            ),
            SimpleNamespace(
                source_start_us=6_000_000,
                source_end_us=14_000_000,
                duration_us=8_000_000,
                audio={},
            ),
        ],
        output_to_source_us=lambda output_us: (
            output_us if output_us < 2_000_000 else output_us + 4_000_000
        ),
        segment_at_output=lambda _output_us: (SimpleNamespace(audio={}), 0),
    )
    cleaned = []
    preview._timeline_composition = SimpleNamespace(
        cleanup=lambda: cleaned.append(True)
    )
    preview._hold_current_frame = lambda: None
    preview._set_loading = lambda _loading: None

    preview.seek_scrub_frame(7_000_000)

    assert cleaned == [True]
    assert preview._timeline_composition is None
    assert preview.playing is True
    assert preview.pipeline.calls[0] == ("state", Gst.State.PAUSED)
    seek = next(call for call in preview.pipeline.calls if call[0] == "seek")
    assert seek[2] == Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
    assert seek[3] == 11_000_000_000
    assert ("state", Gst.State.PLAYING) not in preview.pipeline.calls


def test_paused_accurate_seek_uses_lightweight_preview_until_play(monkeypatch):
    preview, _timers = make_preview(monkeypatch, playing=False)
    preview.project = SimpleNamespace(
        output_duration_us=10_000_000,
        segments=[
            SimpleNamespace(
                source_start_us=0,
                source_end_us=2_000_000,
                duration_us=2_000_000,
                audio={},
            ),
            SimpleNamespace(
                source_start_us=6_000_000,
                source_end_us=14_000_000,
                duration_us=8_000_000,
                audio={},
            ),
        ],
        output_to_source_us=lambda output_us: (
            output_us if output_us < 2_000_000 else output_us + 4_000_000
        ),
        segment_at_output=lambda _output_us: (SimpleNamespace(audio={}), 0),
    )
    prepared = []
    paintables = []
    preview.picture = SimpleNamespace(set_paintable=paintables.append)
    composition = SimpleNamespace(cleanup=lambda: None)
    preview._timeline_composition = composition
    preview._prepare_timeline = lambda **options: prepared.append(options) or True

    preview.seek_output(7_000_000)
    preview.finish_seek()

    assert prepared == []
    assert preview._timeline_composition is None
    assert paintables == ["legacy"]
    seek = next(call for call in preview.pipeline.calls if call[0] == "seek")
    assert seek[2] == Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
    assert seek[3] == 11_000_000_000


def test_play_uses_a_prerolled_original_media_timeline_for_source_gaps(monkeypatch):
    preview, _timers = make_preview(monkeypatch, position=1_000_000)
    preview.project = SimpleNamespace(
        output_duration_us=5_000_000,
        segments=[
            SimpleNamespace(
                source_start_us=0,
                source_end_us=2_000_000,
                duration_us=2_000_000,
            ),
            SimpleNamespace(
                source_start_us=6_000_000,
                source_end_us=9_000_000,
                duration_us=3_000_000,
            ),
        ],
    )
    preview._timeline_composition = None
    preview._legacy_paintable = "legacy"
    paintables = []
    preview.picture = SimpleNamespace(set_paintable=paintables.append)
    created = []

    class FakeComposition:
        paintable = "timeline"

        def __init__(
            self,
            project,
            output_start_us,
            eos_callback,
            error_callback,
            ready_callback,
        ):
            created.append(
                (
                    project,
                    output_start_us,
                    eos_callback,
                    error_callback,
                    ready_callback,
                )
            )
            self.started = False

        @staticmethod
        def prepare():
            return True

        def start(self):
            self.started = True
            return True

    monkeypatch.setattr(editor_preview, "TimelineComposition", FakeComposition)

    preview.play()

    assert created[0][:2] == (preview.project, 1_000_000)
    assert preview._timeline_composition.started is True
    assert paintables == []
    created[0][4](preview._timeline_composition)
    assert paintables == ["timeline"]
    assert preview.pipeline.calls == [("state", Gst.State.PAUSED)]
    assert preview.playing is True


def test_play_resumes_an_existing_timeline_without_rebuilding(monkeypatch):
    preview, _timers = make_preview(monkeypatch, position=3_100_000)
    preview.project = SimpleNamespace(
        output_duration_us=5_000_000,
        segments=[
            SimpleNamespace(source_start_us=0, source_end_us=2_000_000),
            SimpleNamespace(source_start_us=6_000_000, source_end_us=9_000_000),
        ],
    )
    calls = []
    composition = SimpleNamespace(
        paintable="timeline",
        start=lambda: calls.append("start") or True,
    )
    preview._timeline_composition = composition
    preview.picture = SimpleNamespace(
        set_paintable=lambda paintable: calls.append(("paintable", paintable))
    )
    preview._prepare_timeline = lambda **_options: calls.append("rebuild")

    preview.play()

    assert calls == [("paintable", "timeline"), "start"]
    assert preview._timeline_composition is composition
    assert preview.playing is True


def test_timeline_position_stays_at_the_last_frame_after_pipeline_pauses():
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._started = True
    composition._last_position_us = 5_000_000
    composition.pipeline = SimpleNamespace(
        get_state=lambda _timeout: (
            Gst.StateChangeReturn.SUCCESS,
            Gst.State.PAUSED,
            Gst.State.VOID_PENDING,
        )
    )

    assert composition.position_us() == 5_000_000


def test_pausing_timeline_keeps_the_prepared_decoder_and_paintable(monkeypatch):
    preview, _timers = make_preview(monkeypatch, position=3_100_000, playing=True)
    states = []
    positions = []
    preview.state_changed_callback = states.append
    preview.changed_callback = positions.append

    class FakeComposition:
        cleaned = False

        @staticmethod
        def position_us():
            return 3_125_000

        @staticmethod
        def pause():
            return True

    composition = FakeComposition()
    preview._timeline_composition = composition

    preview.pause()

    assert preview._timeline_composition is composition
    assert composition.cleaned is False
    assert preview.output_position_us == 3_125_000
    assert preview.playing is False
    assert positions == [3_125_000]
    assert states == [False]


def test_stale_timeline_callbacks_cannot_stop_the_current_composition(monkeypatch):
    preview, _timers = make_preview(monkeypatch, playing=True)
    old_composition = object()
    current_composition = object()
    preview._timeline_composition = current_composition

    preview._on_timeline_eos(old_composition)
    preview._on_timeline_error(old_composition, "late decoder error")

    assert preview._timeline_composition is current_composition
    assert preview.playing is True
    assert preview.pipeline.calls == []


def test_deleting_a_segment_during_timeline_playback_stops_transport(monkeypatch):
    preview, _timers = make_preview(monkeypatch, position=3_000_000, playing=True)
    segments = [
        SimpleNamespace(
            id="first",
            source_start_us=0,
            source_end_us=2_000_000,
            duration_us=2_000_000,
        ),
        SimpleNamespace(
            id="middle",
            source_start_us=4_000_000,
            source_end_us=6_000_000,
            duration_us=2_000_000,
        ),
        SimpleNamespace(
            id="last",
            source_start_us=8_000_000,
            source_end_us=10_000_000,
            duration_us=2_000_000,
        ),
    ]
    project = SimpleNamespace(
        segments=segments,
        output_duration_us=6_000_000,
        output_to_source_us=lambda output_us: (
            output_us
            if output_us < 2_000_000
            else output_us + 2_000_000
            if output_us < 4_000_000
            else output_us + 4_000_000
        ),
        segment_at_output=lambda _output_us: (SimpleNamespace(audio={}), 0),
    )
    preview.project = project
    preview._project_structure_snapshot = preview._project_structure(project)
    states = []
    paintables = []
    preview.state_changed_callback = states.append
    preview._legacy_paintable = "legacy"
    preview.picture = SimpleNamespace(set_paintable=paintables.append)

    class FakeComposition:
        def __init__(self):
            self.cleaned = False

        @staticmethod
        def position_us():
            return 3_000_000

        def cleanup(self):
            self.cleaned = True

    composition = FakeComposition()
    preview._timeline_composition = composition
    prepared = []
    preview._prepare_timeline = lambda **options: prepared.append(options) or True

    del project.segments[1]
    project.output_duration_us = 4_000_000
    project.output_to_source_us = lambda output_us: (
        output_us if output_us < 2_000_000 else output_us + 6_000_000
    )
    preview.update_project(project)

    assert composition.cleaned is True
    assert preview._timeline_composition is None
    assert preview.playing is False
    assert states == [False]
    assert paintables == ["legacy"]
    # Structural edits now settle on the lightweight preview before the
    # speculative composition is built by the debounce callback.
    assert prepared == []
    assert ("state", Gst.State.PLAYING) not in preview.pipeline.calls
    assert ("state", Gst.State.PAUSED) in preview.pipeline.calls
    assert preview.pipeline.calls[-1][0] == "seek"


def test_playback_stops_at_the_end_of_the_last_kept_segment(monkeypatch):
    preview, _timers = make_preview(monkeypatch, position=9_900_000, playing=True)
    preview.pipeline.position_ns = 10_001_000_000
    positions = []
    states = []
    preview.changed_callback = positions.append
    preview.state_changed_callback = states.append

    source_us = preview._sync_position_from_pipeline()

    assert source_us == 10_001_000
    assert preview.output_position_us == FakeProject.output_duration_us
    assert preview.pipeline.calls[-1] == ("state", Gst.State.PAUSED)
    assert positions == [FakeProject.output_duration_us]
    assert states == [False]
    assert preview.playing is False


def test_preview_applies_each_tracks_effective_gain_in_real_time(monkeypatch):
    preview, _timers = make_preview(monkeypatch)
    preview.audio_volumes = {"a0": FakeVolume(), "a1": FakeVolume()}
    segment = SimpleNamespace(
        audio={
            "a0": AudioSetting(gain=1.4),
            "a1": AudioSetting(gain=0.7, muted=True),
        }
    )

    preview._apply_audio_settings(segment)

    assert preview.audio_volumes["a0"].volume == 1.4
    assert preview.audio_volumes["a1"].volume == 0
    assert preview.audio_volumes["a0"].property_name == "volume-full-range"


def test_track_gain_uses_gstreamers_full_range_property_for_plus_30_db():
    volume = FakeVolume()

    _set_track_gain(volume, MAX_GAIN)

    assert volume.property_name == "volume-full-range"
    assert volume.volume == pytest.approx(31.6227766)


def test_preview_listening_volume_does_not_change_edited_track_gains(monkeypatch):
    preview, _timers = make_preview(monkeypatch)
    preview.master_volume = FakeVolume()
    preview.audio_volumes = {"a0": FakeVolume()}
    active = SimpleNamespace(volumes=[])
    active.set_volume = active.volumes.append
    speculative = SimpleNamespace(volumes=[])
    speculative.set_volume = speculative.volumes.append
    preview._timeline_composition = active
    preview._speculative_composition = speculative

    preview.set_volume(0.35)

    assert preview.volume == 0.35
    assert preview.master_volume.volume == 0.35
    assert preview.audio_volumes["a0"].volume is None
    assert active.volumes == [0.35]
    assert speculative.volumes == [0.35]


def test_listening_volume_updates_the_audio_stream_without_buffered_gain_delay():
    master = FakeVolume()
    sink = FakeAudioSink()

    _set_listening_volume(master, sink, 1)
    assert sink.properties == {"volume": 1.0, "mute": False}
    assert master.volume is None

    _set_listening_volume(master, sink, 0.35)
    assert sink.properties == {"volume": 0.35, "mute": False}
    assert master.volume is None

    _set_listening_volume(master, sink, 0)
    assert sink.properties == {"volume": 0.0, "mute": True}
    assert master.volume is None


def test_preview_updates_only_the_edited_live_gain(monkeypatch):
    preview, _timers = make_preview(monkeypatch)
    preview._timeline_composition = None
    preview.audio_volumes = {"a0": FakeVolume(), "a1": FakeVolume()}
    segment = SimpleNamespace(
        id="current",
        audio={
            "a0": AudioSetting(gain=1.4),
            "a1": AudioSetting(gain=0.7),
        },
    )
    project = SimpleNamespace(segment_at_output=lambda _position: (segment, 0))

    preview.update_audio_setting(project, "current", "a0")

    assert preview.audio_volumes["a0"].volume == 1.4
    assert preview.audio_volumes["a1"].volume is None


def test_timeline_composition_updates_only_the_matching_branch_and_track():
    composition = TimelineComposition.__new__(TimelineComposition)
    current_a0 = FakeVolume()
    current_a1 = FakeVolume()
    other_a0 = FakeVolume()
    composition._branches = [
        {
            "segment_id": "current",
            "volumes": {"a0": current_a0, "a1": current_a1},
        },
        {"segment_id": "other", "volumes": {"a0": other_a0}},
    ]
    project = SimpleNamespace(
        segments=[
            SimpleNamespace(
                id="current",
                audio={
                    "a0": AudioSetting(gain=1.6),
                    "a1": AudioSetting(gain=0.8),
                },
            ),
            SimpleNamespace(
                id="other",
                audio={"a0": AudioSetting(gain=0.5)},
            ),
        ]
    )

    composition.update_audio_setting(project, "current", "a0")

    assert current_a0.volume == 1.6
    assert current_a1.volume is None
    assert other_a0.volume is None


def test_timeline_audio_edits_keep_each_adjacent_boundary_independent():
    """A split and its inverse must never make one live volume span two cuts."""
    first_a0 = FakeVolume()
    second_a0 = FakeVolume()
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._branches = [
        {
            "segment_id": "first",
            "segment_ids": ("first",),
            "volumes": {"a0": first_a0},
        },
        {
            "segment_id": "second",
            "segment_ids": ("second",),
            "volumes": {"a0": second_a0},
        },
    ]
    project = SimpleNamespace(
        segments=[
            SimpleNamespace(
                id="first",
                audio={"a0": AudioSetting(gain=0.8)},
            ),
            SimpleNamespace(
                id="second",
                audio={"a0": AudioSetting(gain=1.7)},
            ),
        ]
    )

    composition.update_audio_setting(project, "second", "a0")
    assert first_a0.volume is None
    assert second_a0.volume == 1.7

    # Matching settings do not alter the branch topology or accidentally make
    # the first branch own the second segment's future edits.
    project.segments[1].audio["a0"] = AudioSetting(gain=0.8)
    composition.update_audio(project)
    assert first_a0.volume == 0.8
    assert second_a0.volume == 0.8
    assert [
        branch["segment_ids"] for branch in composition._branches
    ] == [("first",), ("second",)]


def test_preview_updates_active_and_speculative_compositions_without_rebuild(
    monkeypatch,
):
    preview, _timers = make_preview(monkeypatch)
    preview._project_cache_snapshot = preview._project_identity(preview.project, 0)
    active = FakeSpeculativeComposition(
        preview.project,
        preview.output_position_us,
        None,
        None,
        None,
    )
    speculative = FakeSpeculativeComposition(
        preview.project,
        preview.output_position_us,
        None,
        None,
        None,
    )
    preview._timeline_composition = active
    preview._speculative_composition = speculative
    preview._speculative_identity = preview._project_identity(
        preview.project,
        preview.output_position_us,
    )
    pipeline_calls = list(preview.pipeline.calls)

    project = SimpleNamespace(
        segments=[
            SimpleNamespace(
                id="current",
                source_start_us=1_000,
                source_end_us=10_001_000,
                audio={"a0": AudioSetting(gain=1.5)},
            )
        ],
        output_duration_us=10_000_000,
    )
    preview.update_audio_setting(project, "current", "a0")

    assert active.audio_updates == [("current", "a0")]
    assert speculative.audio_updates == [("current", "a0")]
    assert preview.pipeline.calls == pipeline_calls


def test_preview_updates_all_compositions_for_multi_target_gain_edit(monkeypatch):
    preview, _timers, _removed = configure_speculative_preview(monkeypatch)
    active = FakeSpeculativeComposition(
        preview.project,
        preview.output_position_us,
        None,
        None,
        None,
    )
    speculative = FakeSpeculativeComposition(
        preview.project,
        preview.output_position_us,
        None,
        None,
        None,
    )
    preview._timeline_composition = active
    preview._speculative_composition = speculative

    project = preview.project.clone()
    project.set_gain("first", "a0", 1.4)
    project.set_gain("second", "a0", 1.6)
    preview.update_project(project, seek=False)

    assert active.audio_updates == ["all"]
    assert speculative.audio_updates == ["all"]
    assert preview._timeline_composition is active
    assert preview._speculative_composition is speculative


def test_muted_audio_has_the_same_effective_gain_as_minimum_gain():
    assert effective_audio_gain(AudioSetting(gain=1.8, muted=True)) == 0
    assert effective_audio_gain(AudioSetting(gain=0, muted=False)) == 0


def test_stationary_paused_seek_prepares_once_after_200_ms_without_ui_handoff(
    monkeypatch,
):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    FakeSpeculativeComposition.ready = False
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )

    preview.finish_seek()
    preview.finish_seek()

    assert [(interval, timer_id) for timer_id, (interval, _cb) in timers.items()] == [
        (editor_preview.IDLE_PREPARE_DEBOUNCE_MS, 1)
    ]
    timers.pop(1)[1]()

    assert len(FakeSpeculativeComposition.instances) == 1
    assert preview._speculative_composition is FakeSpeculativeComposition.instances[0]
    assert preview._timeline_composition is None
    assert preview.picture.paintable == "paused-frame"
    assert preview.loading_spinner.visible is False
    assert [interval for interval, _callback in timers.values()] == [
        editor_preview.IDLE_PREPARE_BUDGET_MS
    ]


def test_playhead_motion_resets_debounce_and_discards_only_speculative_composition(
    monkeypatch,
):
    preview, timers, removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )

    preview.finish_seek()
    stale_callback = timers[1][1]
    preview.seek_output(1_250_000)
    preview.finish_seek()

    assert 1 in removed
    stale_callback()
    assert FakeSpeculativeComposition.instances == []
    current_timer_id = preview._idle_prepare_id
    timers.pop(current_timer_id)[1]()
    composition = preview._speculative_composition

    preview.seek_scrub_frame(1_500_000)

    assert composition.cleanup_calls == 1
    assert preview._speculative_composition is None
    assert preview._timeline_composition is None


def test_play_adopts_in_progress_speculation_and_retains_frame_until_ready(
    monkeypatch,
):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    FakeSpeculativeComposition.ready = False
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview._hold_current_frame = lambda: None

    preview.finish_seek()
    timers.pop(preview._idle_prepare_id)[1]()
    composition = preview._speculative_composition
    preview.play()

    assert preview._timeline_composition is composition
    assert preview._speculative_composition is None
    assert composition.start_calls == 1
    assert len(FakeSpeculativeComposition.instances) == 1
    assert preview.picture.paintable == "paused-frame"
    assert preview.loading_spinner.visible is False
    assert timers[preview._loading_spinner_delay_id][0] == (
        editor_preview.LOADING_SPINNER_DELAY_MS
    )

    timers.pop(preview._loading_spinner_delay_id)[1]()
    assert preview.loading_spinner.visible is True

    preview._on_timeline_playback_started(
        composition,
        generation=preview._loading_generation,
    )
    assert preview.picture.paintable == "timeline-frame"
    assert preview.loading_spinner.visible is False


def test_play_before_debounce_cancels_request_and_builds_only_one_composition(
    monkeypatch,
):
    preview, timers, removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    FakeSpeculativeComposition.ready = False
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview._hold_current_frame = lambda: None

    preview.finish_seek()
    debounce_id = preview._idle_prepare_id
    stale_callback = timers[debounce_id][1]
    preview.play()
    stale_callback()

    assert debounce_id in removed
    assert len(FakeSpeculativeComposition.instances) == 1
    assert preview._timeline_composition is FakeSpeculativeComposition.instances[0]
    assert preview._speculative_composition is None


def test_play_adopts_completed_speculation_without_building_again(monkeypatch):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    FakeSpeculativeComposition.ready = True
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview._hold_current_frame = lambda: None

    preview.finish_seek()
    timers.pop(preview._idle_prepare_id)[1]()
    composition = preview._speculative_composition
    preview.play()

    assert preview._timeline_composition is composition
    assert composition.start_calls == 1
    assert len(FakeSpeculativeComposition.instances) == 1
    assert preview.picture.paintable == "timeline-frame"


def test_stale_speculative_callbacks_cannot_change_visible_or_loading_state(
    monkeypatch,
):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview.finish_seek()
    timers.pop(preview._idle_prepare_id)[1]()
    old = preview._speculative_composition

    preview.seek_output(1_750_000)
    paintable_after_seek = preview.picture.paintable
    preview._loading = True
    preview.loading_spinner.visible = True
    old.ready_callback(old)
    old.error_callback(old, "late error")
    preview._on_timeline_playback_started(old, generation=0)

    assert old.cleanup_calls == 1
    assert preview.picture.paintable == paintable_after_seek
    assert preview.loading_spinner.visible is True


def test_compatible_audio_edit_updates_speculative_volume_and_cache_identity(
    monkeypatch,
):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview.finish_seek()
    timers.pop(preview._idle_prepare_id)[1]()
    composition = preview._speculative_composition
    old_identity = preview._speculative_identity

    preview.project.set_gain("first", "a0", 1.5)
    preview.update_audio_setting(preview.project, "first", "a0")

    assert preview._speculative_composition is composition
    assert composition.audio_updates == [("first", "a0")]
    assert preview._speculative_identity != old_identity
    assert composition.cleanup_calls == 0


def test_source_invalidation_and_cleanup_cancel_timers_and_release_decoders(
    monkeypatch,
):
    preview, timers, removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview.finish_seek()
    debounce_id = preview._idle_prepare_id
    timers.pop(debounce_id)[1]()
    composition = preview._speculative_composition
    deadline_id = preview._idle_prepare_deadline_id

    preview.invalidate_source()

    assert composition.cleanup_calls == 1
    assert deadline_id in removed
    assert preview._source_valid is False
    assert preview._speculative_composition is None
    assert preview._idle_prepare_id is None
    assert preview._idle_prepare_deadline_id is None


def test_five_second_deadline_stops_idle_growth_but_preserves_composition(
    monkeypatch,
):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    FakeSpeculativeComposition.instances = []
    monkeypatch.setattr(
        editor_preview,
        "TimelineComposition",
        FakeSpeculativeComposition,
    )
    preview.finish_seek()
    timers.pop(preview._idle_prepare_id)[1]()
    composition = preview._speculative_composition
    deadline_id = preview._idle_prepare_deadline_id

    timers.pop(deadline_id)[1]()

    assert composition.stop_growth_calls == 1
    assert composition.cleanup_calls == 0
    assert preview._speculative_composition is composition
    assert preview._idle_prepare_deadline_id is None


def test_stopped_speculation_does_not_append_branches_until_play_is_requested():
    composition = TimelineComposition.__new__(TimelineComposition)
    composition._speculative_growth_stopped = True
    composition._start_requested = False
    composition.project = SimpleNamespace(
        segments=[
            SimpleNamespace(source_start_us=0),
            SimpleNamespace(source_start_us=4_000_000),
        ]
    )
    composition._branches = [{"index": 0, "end_index": 0}]
    appended = []
    composition._append_branch = lambda index, source_us: appended.append(
        (index, source_us)
    )

    composition._prepare_next_branch(0)
    composition._start_requested = True
    composition._prepare_next_branch(0)

    assert appended == [(1, 4_000_000)]


def test_gapless_project_never_schedules_speculative_pipeline(monkeypatch):
    preview, timers, _removed = configure_speculative_preview(monkeypatch)
    preview.project.segments[1].source_start_us = 2_000_000

    preview.finish_seek()

    assert timers == {}
    assert preview._speculative_composition is None


def test_toggle_uses_one_playback_state_and_notifies_the_ui(monkeypatch):
    preview, _timers = make_preview(monkeypatch)
    states = []
    preview.state_changed_callback = states.append

    preview.toggle_playback()
    preview.toggle_playback()

    assert states == [True, False]
    assert ("state", Gst.State.PAUSED) in preview.pipeline.calls
    assert preview.pipeline.calls[-1] == ("query", Gst.Format.TIME)
    assert preview.playing is False


def test_playing_from_the_end_restarts_at_the_beginning(monkeypatch):
    preview, _timers = make_preview(
        monkeypatch,
        position=FakeProject.output_duration_us,
    )
    preview.pipeline.position_ns = 10_001_000_000

    preview.play()

    assert preview.output_position_us == 0
    seek = next(call for call in preview.pipeline.calls if call[0] == "seek")
    assert seek[3] == 1_000_000
