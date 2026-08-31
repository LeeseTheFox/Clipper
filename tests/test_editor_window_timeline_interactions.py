from types import SimpleNamespace

import editor_window
import pytest
from editor_history import EditorHistory
from editor_model import AudioSetting, AudioTrack, EditorProject, Source
from editor_window import (
    EditorWindow,
    _clamp_viewport_pointer_x,
    _new_track_mute_badge,
    _playhead_x,
    _viewport_playhead_x,
    audio_selection_state,
    decibels_to_gain,
    format_gain_db,
    format_video_track_details,
    gain_db_after_drag,
    gain_feedback_badge_width,
    gain_feedback_badge_x,
    gain_line_decibels,
    gain_line_y,
    gain_to_decibels,
    peak_db_fraction,
    scale_value_after_drag,
    selection_title,
)


class FakeArea:
    @staticmethod
    def get_width():
        return 100


class WaveformContext:
    def __init__(self):
        self.current_path = None
        self.clips = []

    def clip_extents(self):
        return 0, 0, 100, 74

    def save(self):
        pass

    def restore(self):
        pass

    def clip(self):
        self.clips.append(self.current_path)
        self.current_path = None

    def fill(self):
        self.current_path = None

    def stroke(self):
        self.current_path = None

    def set_line_width(self, _width):
        pass

    def set_dash(self, _pattern):
        pass

    def move_to(self, *_point):
        pass

    def line_to(self, *_point):
        pass


class GainLineContext:
    def set_dash(self, _pattern):
        pass

    def set_line_width(self, _width):
        pass

    def move_to(self, *_point):
        pass

    def line_to(self, *_point):
        pass

    def stroke(self):
        pass


def test_track_mute_badges_do_not_claim_focus_on_pointer_click():
    badge = _new_track_mute_badge("A4")

    assert badge.get_focus_on_click() is False


def test_gain_adjusted_waveforms_are_clipped_to_the_existing_audio_clip_shape(
    monkeypatch,
):
    segment = SimpleNamespace(
        id="segment",
        source_start_us=0,
        source_end_us=1_000_000,
        audio={"audio": AudioSetting(gain=4.0)},
    )
    project = SimpleNamespace(
        output_duration_us=1_000_000,
        source=SimpleNamespace(duration_us=1_000_000),
    )
    window = SimpleNamespace(
        _timeline_clip_width=100,
        _waveforms={"audio": [(-32768, 32767)]},
        project=project,
        _segment_rects=lambda _width: [(0, segment, 0, 100)],
    )
    area = SimpleNamespace(get_color=lambda: object())
    context = WaveformContext()

    monkeypatch.setattr(editor_window, "_selection_ids", lambda _window: set())
    monkeypatch.setattr(editor_window, "_set_source_color", lambda *_args: None)
    monkeypatch.setattr(
        editor_window.Adw.StyleManager,
        "get_default",
        lambda: SimpleNamespace(get_accent_color_rgba=lambda: object()),
    )

    def rounded_rectangle(context, x, y, width, height, radius):
        context.current_path = (x, y, width, height, radius)

    monkeypatch.setattr(editor_window, "_rounded_rectangle", rounded_rectangle)

    EditorWindow._draw_waveform(window, area, context, 100, 74, "audio")

    assert context.clips == [(3, 5, 94, 64, 5)]


def test_reduced_gain_hides_the_default_waveform_reference(monkeypatch):
    segment = SimpleNamespace(
        id="segment",
        source_start_us=0,
        source_end_us=1_000_000,
        audio={"audio": AudioSetting(gain=0.5)},
    )
    project = SimpleNamespace(
        output_duration_us=1_000_000,
        source=SimpleNamespace(duration_us=1_000_000),
    )
    window = SimpleNamespace(
        _timeline_clip_width=100,
        _waveforms={"audio": [(-32768, 32767)]},
        project=project,
        _segment_rects=lambda _width: [(0, segment, 0, 100)],
    )
    foreground = object()
    area = SimpleNamespace(get_color=lambda: foreground)
    context = WaveformContext()

    monkeypatch.setattr(editor_window, "_selection_ids", lambda _window: set())
    colors = []
    monkeypatch.setattr(
        editor_window,
        "_set_source_color",
        lambda _context, color, alpha: colors.append((color, alpha)),
    )
    accent = object()
    monkeypatch.setattr(
        editor_window.Adw.StyleManager,
        "get_default",
        lambda: SimpleNamespace(get_accent_color_rgba=lambda: accent),
    )

    def rounded_rectangle(context, x, y, width, height, radius):
        context.current_path = (x, y, width, height, radius)

    monkeypatch.setattr(editor_window, "_rounded_rectangle", rounded_rectangle)

    EditorWindow._draw_waveform(window, area, context, 100, 74, "audio")

    assert context.clips == [(3, 5, 94, 64, 5)]
    assert (accent, 0.68) in colors
    assert (foreground, 0.10) not in colors


def test_muted_segment_shows_the_default_waveform_reference(monkeypatch):
    segment = SimpleNamespace(
        id="segment",
        source_start_us=0,
        source_end_us=1_000_000,
        audio={"audio": AudioSetting(gain=0.5, muted=True)},
    )
    project = SimpleNamespace(
        output_duration_us=1_000_000,
        source=SimpleNamespace(duration_us=1_000_000),
    )
    window = SimpleNamespace(
        _timeline_clip_width=100,
        _waveforms={"audio": [(-32768, 32767)]},
        project=project,
        _segment_rects=lambda _width: [(0, segment, 0, 100)],
    )
    foreground = object()
    area = SimpleNamespace(get_color=lambda: foreground)
    context = WaveformContext()
    colors = []

    monkeypatch.setattr(editor_window, "_selection_ids", lambda _window: set())
    monkeypatch.setattr(
        editor_window,
        "_set_source_color",
        lambda _context, color, alpha: colors.append((color, alpha)),
    )
    monkeypatch.setattr(
        editor_window.Adw.StyleManager,
        "get_default",
        lambda: SimpleNamespace(get_accent_color_rgba=lambda: object()),
    )

    def rounded_rectangle(context, x, y, width, height, radius):
        context.current_path = (x, y, width, height, radius)

    monkeypatch.setattr(editor_window, "_rounded_rectangle", rounded_rectangle)

    EditorWindow._draw_waveform(window, area, context, 100, 74, "audio")

    assert context.clips == [
        (3, 5, 94, 64, 5),
        (3, 5, 94, 64, 5),
    ]
    assert (foreground, 0.10) in colors


def test_gain_lines_use_the_focused_style_for_every_segment(monkeypatch):
    accent = object()
    foreground = object()
    segments = [
        SimpleNamespace(
            id="focused",
            audio={"audio": AudioSetting(gain=1.0)},
        ),
        SimpleNamespace(
            id="unfocused",
            audio={"audio": AudioSetting(gain=1.0)},
        ),
    ]
    window = SimpleNamespace(
        _gain_feedback={},
        _segment_rects=lambda _width: [
            (0, segments[0], 0, 50),
            (1, segments[1], 50, 100),
        ],
    )
    area = SimpleNamespace(get_color=lambda: foreground)
    context = GainLineContext()
    colors = []

    monkeypatch.setattr(
        editor_window.Adw.StyleManager,
        "get_default",
        lambda: SimpleNamespace(get_accent_color_rgba=lambda: accent),
    )
    monkeypatch.setattr(
        editor_window,
        "_set_source_color",
        lambda _context, color, alpha=1.0: colors.append((color, alpha)),
    )

    EditorWindow._draw_gain_lines(window, area, context, 100, 74, "audio")

    assert colors == [(accent, 0.28), (accent, 0.96)] * 2


class FakeProject:
    output_duration_us = 10_000_000
    segments = [SimpleNamespace(id="first"), SimpleNamespace(id="second")]

    @staticmethod
    def segment_index(segment_id):
        return 0 if segment_id == "first" else 1

    @staticmethod
    def segment_at_output(output_us):
        segment_id = "first" if output_us < 4_000_000 else "second"
        return SimpleNamespace(id=segment_id), 0


class FakeAdjustment:
    def __init__(self, value=500, page_size=1_000, upper=5_000):
        self.value = value
        self.page_size = page_size
        self.upper = upper
        self.value_changed_callback = None

    def get_value(self):
        return self.value

    def get_page_size(self):
        return self.page_size

    def get_upper(self):
        return self.upper

    def set_upper(self, value):
        self.upper = value

    def get_step_increment(self):
        return 50

    def set_value(self, value):
        self.value = value
        if self.value_changed_callback is not None:
            self.value_changed_callback(self)


class FakeScrollController:
    def __init__(self, state):
        self.state = state

    def get_current_event_state(self):
        return self.state


class FakeZoomScale:
    def __init__(self):
        self.ranges = []
        self.clear_count = 0
        self.marks = []

    def set_range(self, lower, upper):
        self.ranges.append((lower, upper))

    def clear_marks(self):
        self.clear_count += 1

    def add_mark(self, value, position, label):
        self.marks.append((value, position, label))


@pytest.mark.parametrize(
    ("decibels", "expected"),
    (
        (float("-inf"), 0.0),
        (-60, 0.0),
        (-30, 0.5),
        (0, 1.0),
        (6, 1.0),
        (None, 0.0),
    ),
)
def test_peak_db_fraction_uses_a_readable_logarithmic_meter_scale(
    decibels,
    expected,
):
    assert peak_db_fraction(decibels) == expected


@pytest.mark.parametrize(
    ("position_us", "expected_x"),
    ((0, 0), (5_000_000, 50), (10_000_000, 99)),
)
def test_playhead_line_and_handle_use_one_edge_safe_coordinate(
    position_us,
    expected_x,
):
    assert _playhead_x(position_us, 10_000_000, 100) == expected_x


def test_viewport_playhead_position_is_independent_of_scroll_child_allocation():
    content_x = _playhead_x(4_777_777, 10_000_000, 11_000)
    scroll_value = content_x - 317.25

    assert _viewport_playhead_x(
        4_777_777,
        10_000_000,
        11_000,
        scroll_value,
    ) == pytest.approx(317.25)


@pytest.mark.parametrize(
    ("pointer_x", "expected"),
    ((-120, 0), (400, 400), (1_200, 999)),
)
def test_drag_pointer_is_clamped_inside_the_timeline_viewport(pointer_x, expected):
    assert _clamp_viewport_pointer_x(pointer_x, 1_000) == expected


@pytest.mark.parametrize(
    ("content_x", "expected_speed", "expected_pointer_x", "expected_content_x"),
    (
        (380, -36, 0, 500),
        (1_700, 36, 999, 1_499),
    ),
)
def test_scrubbing_outside_the_window_pins_the_playhead_to_the_visible_edge(
    content_x,
    expected_speed,
    expected_pointer_x,
    expected_content_x,
):
    adjustment = FakeAdjustment(value=500, page_size=1_000, upper=5_000)
    edge_scroll = []
    scrub_positions = []
    window = SimpleNamespace(
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        _set_edge_scroll=lambda speed, pointer_x, area: edge_scroll.append(
            (speed, pointer_x, area)
        ),
        _apply_scrub_position=lambda x, area: scrub_positions.append((x, area)),
    )
    area = FakeArea()

    EditorWindow._scrub_at(window, content_x, area)

    assert edge_scroll == [(expected_speed, expected_pointer_x, area)]
    assert scrub_positions == [(expected_content_x, area)]


def test_track_click_selects_a_segment_without_moving_the_playhead():
    selections = []
    window = SimpleNamespace(
        project=FakeProject(),
        selected_segment_id="first",
        _playhead_us=1_250_000,
        _select=lambda _button, segment_id: selections.append(segment_id),
    )

    EditorWindow._track_released(window, None, 1, 75, 20, FakeArea())

    assert selections == ["second"]
    assert window._playhead_us == 1_250_000


def test_ctrl_click_toggles_segments_without_allowing_an_empty_selection():
    refreshes = []
    window = SimpleNamespace(
        project=FakeProject(),
        selected_segment_id="first",
        selected_segment_ids={"first"},
        _refresh=lambda **kwargs: refreshes.append(kwargs),
    )

    EditorWindow._select(window, None, "second", toggle=True)
    assert window.selected_segment_ids == {"first", "second"}
    assert window.selected_segment_id == "second"

    EditorWindow._select(window, None, "first", toggle=True)
    assert window.selected_segment_ids == {"second"}

    EditorWindow._select(window, None, "second", toggle=True)
    assert window.selected_segment_ids == {"second"}
    assert refreshes == [
        {"update_preview": False},
        {"update_preview": False},
    ]


def test_plain_click_collapses_a_multi_selection_to_the_clicked_segment():
    refreshes = []
    window = SimpleNamespace(
        project=FakeProject(),
        selected_segment_id="second",
        selected_segment_ids={"first", "second"},
        _refresh=lambda **kwargs: refreshes.append(kwargs),
    )

    EditorWindow._select(window, None, "first")

    assert window.selected_segment_ids == {"first"}
    assert window.selected_segment_id == "first"
    assert refreshes == [{"update_preview": False}]


def test_shift_click_selects_the_range_from_the_last_selection_anchor():
    refreshes = []
    segments = [
        SimpleNamespace(id=name)
        for name in ("first", "second", "third", "fourth")
    ]
    project = SimpleNamespace(
        segments=segments,
        segment_index=lambda segment_id: next(
            index for index, segment in enumerate(segments) if segment.id == segment_id
        ),
    )
    window = SimpleNamespace(
        project=project,
        selected_segment_id="second",
        selected_segment_ids={"second"},
        _selection_anchor_id="second",
        _refresh=lambda **kwargs: refreshes.append(kwargs),
    )

    EditorWindow._select(window, None, "fourth", extend=True)

    assert window.selected_segment_ids == {"second", "third", "fourth"}
    assert window.selected_segment_id == "fourth"
    assert window._selection_anchor_id == "second"
    assert refreshes == [{"update_preview": False}]


def test_ctrl_shift_click_adds_the_anchor_range_to_the_selection():
    segments = [
        SimpleNamespace(id=name)
        for name in ("first", "second", "third", "fourth")
    ]
    project = SimpleNamespace(
        segments=segments,
        segment_index=lambda segment_id: next(
            index for index, segment in enumerate(segments) if segment.id == segment_id
        ),
    )
    window = SimpleNamespace(
        project=project,
        selected_segment_id="second",
        selected_segment_ids={"first", "second"},
        _selection_anchor_id="second",
        _refresh=lambda **_kwargs: None,
    )

    EditorWindow._select(window, None, "fourth", extend=True, preserve=True)

    assert window.selected_segment_ids == {"first", "second", "third", "fourth"}
    assert window.selected_segment_id == "fourth"
    assert window._selection_anchor_id == "second"


def test_ctrl_deselecting_range_edge_moves_anchor_to_nearest_selection():
    segments = [
        SimpleNamespace(id=f"segment-{index}") for index in range(1, 7)
    ]
    project = SimpleNamespace(
        segments=segments,
        segment_index=lambda segment_id: next(
            index for index, segment in enumerate(segments) if segment.id == segment_id
        ),
    )
    window = SimpleNamespace(
        project=project,
        selected_segment_id="segment-2",
        selected_segment_ids={"segment-2", "segment-5"},
        _selection_anchor_id="segment-2",
        _refresh=lambda **_kwargs: None,
    )

    EditorWindow._select(window, None, "segment-6", extend=True)
    EditorWindow._select(window, None, "segment-6", toggle=True)
    EditorWindow._select(window, None, "segment-1", extend=True)

    assert window.selected_segment_ids == {
        "segment-1",
        "segment-2",
        "segment-3",
        "segment-4",
        "segment-5",
    }
    assert window.selected_segment_id == "segment-1"
    assert window._selection_anchor_id == "segment-5"


def test_select_all_keeps_the_primary_segment_and_refreshes_only_the_ui():
    refreshes = []
    window = SimpleNamespace(
        project=FakeProject(),
        selected_segment_id="second",
        selected_segment_ids={"second"},
        _refresh=lambda **kwargs: refreshes.append(kwargs),
    )

    EditorWindow._select_all(window)

    assert window.selected_segment_ids == {"first", "second"}
    assert window.selected_segment_id == "second"
    assert refreshes == [{"update_preview": False}]


def test_ctrl_click_modifier_is_forwarded_by_the_track_gesture():
    selections = []
    gesture = SimpleNamespace(
        get_current_event_state=lambda: editor_window.Gdk.ModifierType.CONTROL_MASK
    )
    window = SimpleNamespace(
        project=FakeProject(),
        _select=lambda _button, segment_id, **options: selections.append(
            (segment_id, options)
        ),
    )

    EditorWindow._track_released(window, gesture, 1, 75, 20, FakeArea())

    assert selections == [("second", {"toggle": True})]


def test_shift_click_modifier_is_forwarded_by_the_track_gesture():
    selections = []
    gesture = SimpleNamespace(
        get_current_event_state=lambda: editor_window.Gdk.ModifierType.SHIFT_MASK
    )
    window = SimpleNamespace(
        project=FakeProject(),
        _select=lambda _button, segment_id, **options: selections.append(
            (segment_id, options)
        ),
    )

    EditorWindow._track_released(window, gesture, 1, 75, 20, FakeArea())

    assert selections == [("second", {"extend": True, "preserve": False})]


def test_ctrl_shift_click_modifier_adds_a_range():
    selections = []
    gesture = SimpleNamespace(
        get_current_event_state=lambda: (
            editor_window.Gdk.ModifierType.CONTROL_MASK
            | editor_window.Gdk.ModifierType.SHIFT_MASK
        )
    )
    window = SimpleNamespace(
        project=FakeProject(),
        _select=lambda _button, segment_id, **options: selections.append(
            (segment_id, options)
        ),
    )

    EditorWindow._track_released(window, gesture, 1, 75, 20, FakeArea())

    assert selections == [("second", {"extend": True, "preserve": True})]


def test_selection_indicator_stays_compact_for_more_than_twenty_segments():
    segments = [SimpleNamespace(id=f"segment-{index}") for index in range(25)]
    project = SimpleNamespace(
        segments=segments,
        segment_index=lambda segment_id: next(
            index for index, segment in enumerate(segments) if segment.id == segment_id
        ),
    )

    assert selection_title(project, {segments[20].id}, segments[20].id) == "Segment 21"
    assert (
        selection_title(
            project,
            {segment.id for segment in segments[:21]},
            segments[20].id,
        )
        == "21 segments selected"
    )
    assert (
        selection_title(
            project,
            {segment.id for segment in segments},
            segments[20].id,
        )
        == "All segments selected"
    )


def test_ruler_scrubbing_moves_the_playhead_without_changing_selection():
    positions = []
    current_labels = []
    window = SimpleNamespace(
        project=FakeProject(),
        selected_segment_id="second",
        _playhead_us=0,
        time_label=SimpleNamespace(set_text=current_labels.append),
        _update_playhead=lambda: None,
        _queue_scrub_preview=positions.append,
    )
    window._update_transport_time = lambda output_us: EditorWindow._update_transport_time(
        window, output_us
    )

    EditorWindow._apply_scrub_position(window, 20, FakeArea())

    assert window._playhead_us == 2_000_000
    assert window.selected_segment_id == "second"
    assert positions == [2_000_000]
    assert current_labels == ["0:02 / 0:10"]


def test_scrub_preview_seeks_are_coalesced_and_request_exact_frames(monkeypatch):
    scheduled = []
    seeks = []
    monkeypatch.setattr(
        editor_window.GLib,
        "timeout_add",
        lambda interval, callback: scheduled.append((interval, callback)) or 17,
    )
    window = SimpleNamespace(
        _scrubbing=True,
        _scrub_seek_id=None,
        _pending_scrub_preview_us=None,
        preview=SimpleNamespace(
            seek_scrub_frame=seeks.append,
        ),
    )
    window._flush_scrub_preview_seek = lambda: EditorWindow._flush_scrub_preview_seek(
        window
    )

    EditorWindow._queue_scrub_preview(window, 1_000_000)
    EditorWindow._queue_scrub_preview(window, 2_000_000)

    assert len(scheduled) == 1
    assert scheduled[0][0] == editor_window.SCRUB_PREVIEW_INTERVAL_MS
    assert scheduled[0][1]() is True
    assert seeks == [2_000_000]


@pytest.mark.parametrize(
    ("playhead_us", "variable_frame_rate", "expected"),
    (
        (39_999, False, False),
        (40_000, False, True),
        (960_000, False, True),
        (960_001, False, False),
        (1, True, True),
        (999_999, True, True),
    ),
)
def test_split_sensitivity_matches_the_model_edge_rule(
    playhead_us,
    variable_frame_rate,
    expected,
):
    segment = SimpleNamespace(
        source_start_us=2_000_000,
        source_end_us=3_000_000,
        duration_us=1_000_000,
    )
    project = SimpleNamespace(
        output_duration_us=1_000_000,
        source=SimpleNamespace(
            variable_frame_rate=variable_frame_rate,
            frame_duration_us=40_000,
        ),
        segment_at_output=lambda output_us: (segment, output_us),
    )
    window = SimpleNamespace(project=project, _playhead_us=playhead_us)

    assert EditorWindow._can_split_at_playhead(window) is expected


def test_mixed_audio_selection_reports_an_indeterminate_mute_state():
    segments = [
        SimpleNamespace(audio={"audio": AudioSetting(gain=1.0)}),
        SimpleNamespace(audio={"audio": AudioSetting(gain=0.0, muted=True)}),
        SimpleNamespace(audio={"audio": AudioSetting(gain=4.0)}),
    ]

    state = audio_selection_state(segments, "audio")

    assert state.mute_mixed is True


@pytest.mark.parametrize(
    ("gain", "decibels"),
    (
        (0.0, -100.0),
        (0.00001, -100.0),
        (0.5, -6.0205999),
        (1.0, 0.0),
        (10.0, 20.0),
        (31.6227766, 30.0),
    ),
)
def test_gain_and_decibel_conversions_cover_the_full_editor_range(gain, decibels):
    assert gain_to_decibels(gain) == pytest.approx(decibels)
    if gain > 0:
        assert decibels_to_gain(decibels) == pytest.approx(gain)


def test_gain_line_centers_unity_with_full_db_range_above_and_below():
    assert gain_line_y(30, 74) == 10
    assert gain_line_y(0, 74) == 37
    assert gain_line_y(-100, 74) == 64
    assert gain_line_decibels(10, 74) == 30
    assert gain_line_decibels(37, 74) == 0
    assert gain_line_decibels(64, 74) == -100


def test_gain_line_drag_uses_an_exponential_negative_range_and_snaps_to_unity():
    assert gain_db_after_drag(0, -27, 74) == 30
    assert gain_db_after_drag(0, 27, 74) == -100
    assert gain_db_after_drag(0, -13.5, 74) == 15
    assert gain_db_after_drag(0, 13.5, 74) == -25
    assert gain_db_after_drag(15, 13.5, 74) == 0
    assert gain_db_after_drag(0, 1.9, 74) == 0
    assert gain_db_after_drag(0, 3, 74) == pytest.approx(-100 / 81)


def test_negative_gain_line_mapping_is_inverse_and_gives_finer_control_near_unity():
    for decibels in (-1, -6, -25, -64, -100):
        y = gain_line_y(decibels, 74)
        assert gain_line_decibels(y, 74) == pytest.approx(decibels)

    assert gain_db_after_drag(0, 3, 74) > -100 / 9


@pytest.mark.parametrize(
    ("decibels", "expected"),
    (
        (-100, "Gain -100.0 dB"),
        (-12.34, "Gain -12.3 dB"),
        (0, "Gain 0.0 dB"),
        (30, "Gain +30.0 dB"),
    ),
)
def test_gain_db_feedback_is_explicit_and_signed(decibels, expected):
    assert format_gain_db(decibels) == expected


@pytest.mark.parametrize(
    ("pointer_x", "expected_x"),
    ((600, 618), (950, 836)),
)
def test_gain_feedback_sits_beside_the_cursor_and_stays_in_view(
    pointer_x,
    expected_x,
):
    badge_x = gain_feedback_badge_x(pointer_x, 96, 500, 1_000)

    assert badge_x == expected_x
    assert not badge_x <= pointer_x <= badge_x + 96


def test_gain_feedback_badge_expands_for_long_localized_labels():
    assert gain_feedback_badge_width(70, 400) == 96
    assert gain_feedback_badge_width(120, 400) == 134
    assert gain_feedback_badge_width(120, 100) == 96


def test_gain_line_hit_target_is_limited_to_the_visible_segment_line():
    segment = SimpleNamespace(audio={"audio": AudioSetting(gain=1.0)})
    area = SimpleNamespace(get_width=lambda: 100, get_height=lambda: 74)
    window = SimpleNamespace(
        _timeline_clip_width=100,
        _segment_rects=lambda _width: [(0, segment, 0, 100)],
    )

    assert EditorWindow._gain_line_hit(window, 50, 37, area, "audio") is segment
    assert EditorWindow._gain_line_hit(window, 50, 50, area, "audio") is None
    assert EditorWindow._gain_line_hit(window, 3, 37, area, "audio") is None


def test_in_track_gain_line_drag_updates_selected_segments_in_db(monkeypatch):
    segment = SimpleNamespace(
        id="segment",
        audio={"audio": AudioSetting(gain=1.0)},
    )
    project = SimpleNamespace(segments=[segment])
    cursor_names = []
    redraws = []
    area = SimpleNamespace(
        get_height=lambda: 74,
        set_cursor_from_name=cursor_names.append,
        update_property=lambda *_args: None,
        queue_draw=lambda: redraws.append(True),
    )
    gesture_states = []
    gesture = SimpleNamespace(set_state=gesture_states.append)
    begins = []
    changes = []
    ends = []
    scheduled = []
    monkeypatch.setattr(
        editor_window.GLib,
        "idle_add",
        lambda callback: scheduled.append(callback) or 7,
    )
    window = SimpleNamespace(
        project=project,
        selected_segment_ids={"segment"},
        _gain_lane_drag=None,
        _gain_lane_click_suppressed=False,
        _gain_feedback={},
        _gain_line_hit=lambda *_args: segment,
        _gain_drag_begin=lambda area, targets, track: begins.append(
            (area, targets, track)
        ),
        _gain_value=lambda value, targets, track: changes.append(
            (value, targets, track)
        ),
        _gain_drag_end=lambda area, targets, track: ends.append(
            (area, targets, track)
        ),
        _finish_gain_lane_drag=lambda: False,
    )

    EditorWindow._gain_lane_drag_begin(window, gesture, 50, 37, area, "audio")
    EditorWindow._gain_lane_drag_update(window, gesture, 0, -13.5, area, "audio")
    EditorWindow._gain_lane_drag_end(window, gesture, 0, -13.5, area, "audio")

    assert gesture_states == [editor_window.Gtk.EventSequenceState.CLAIMED]
    assert begins == [(area, ("segment",), "audio")]
    assert changes == [(pytest.approx(decibels_to_gain(15)), ("segment",), "audio")]
    assert ends == [(area, ("segment",), "audio")]
    assert window._gain_feedback["audio"] == {
        "area": area,
        "segment_id": "segment",
        "decibels": 15.0,
        "x": 50,
    }
    assert cursor_names == ["ns-resize", "pointer"]
    assert len(redraws) == 3
    assert scheduled == [window._finish_gain_lane_drag]


def test_minus_100_db_gain_line_endpoint_uses_zero_gain_to_trigger_mute():
    changes = []
    redraws = []
    area = SimpleNamespace(
        update_property=lambda *_args: None,
        queue_draw=lambda: redraws.append(True),
    )
    window = SimpleNamespace(
        _gain_feedback={},
        _gain_lane_drag={
            "area": area,
            "track_id": "audio",
            "target_ids": ("segment",),
            "segment_id": "segment",
            "start_x": 50,
            "start_db": 0,
            "height": 74,
            "decibels": 0,
        },
        _gain_value=lambda value, targets, track: changes.append(
            (value, targets, track)
        ),
    )

    EditorWindow._gain_lane_drag_update(window, None, 0, 27, area, "audio")

    assert changes == [(0.0, ("segment",), "audio")]
    assert window._gain_feedback["audio"]["decibels"] == -100.0
    assert redraws == [True]


def test_gain_drag_defers_history_snapshots_and_coalesces_live_redraws(monkeypatch):
    history_calls = []
    gain_changes = []
    preview_updates = []
    redraws = []
    scheduled = []
    monkeypatch.setattr(
        editor_window.GLib,
        "timeout_add",
        lambda interval, callback: scheduled.append((interval, callback)) or 17,
    )
    history = SimpleNamespace(
        begin_live_mutation=lambda kind, **kwargs: history_calls.append(
            ("begin", kind, kwargs)
        ),
        commit_live_mutation=lambda: history_calls.append(("commit",)),
        mutate=lambda *_args, **_kwargs: history_calls.append(("mutate",)),
    )
    project = SimpleNamespace(
        set_gain=lambda segment_id, track_id, value: gain_changes.append(
            (segment_id, track_id, value)
        )
    )
    window = SimpleNamespace(
        _syncing=False,
        _gain_drag_key=None,
        _gain_redraw_id=None,
        _pending_gain_redraw_track_id=None,
        history=history,
        project=project,
        preview=SimpleNamespace(
            update_audio_setting=lambda *args: preview_updates.append(args)
        ),
        _audio_lanes={"audio": SimpleNamespace(queue_draw=lambda: redraws.append("audio"))},
        timeline_lanes=SimpleNamespace(get_first_child=lambda: None),
    )
    window._redraw_audio_lane = lambda track_id: EditorWindow._redraw_audio_lane(
        window, track_id
    )
    window._queue_gain_redraw = lambda track_id: EditorWindow._queue_gain_redraw(
        window, track_id
    )
    window._flush_gain_redraw = lambda: EditorWindow._flush_gain_redraw(window)
    window._cancel_gain_redraw = lambda: EditorWindow._cancel_gain_redraw(window)

    EditorWindow._gain_drag_begin(window, None, "segment", "audio")
    EditorWindow._gain_value(window, 0.8, "segment", "audio")
    EditorWindow._gain_value(window, 0.6, "segment", "audio")
    assert redraws == []
    assert len(scheduled) == 1
    assert scheduled[0][0] == editor_window.GAIN_WAVEFORM_REFRESH_MS
    assert scheduled[0][1]() is False
    assert redraws == ["audio"]
    EditorWindow._gain_drag_end(window, None, "segment", "audio")

    assert history_calls == [
        (
            "begin",
            "gain",
            {"coalesce_key": ("gain", "segment", "audio")},
        ),
        ("commit",),
    ]
    assert gain_changes == [
        ("segment", "audio", 0.8),
        ("segment", "audio", 0.6),
    ]
    assert preview_updates == [
        (project, "segment", "audio"),
        (project, "segment", "audio"),
    ]
    assert redraws == ["audio", "audio"]
    assert window._gain_drag_key is None


def test_setting_a_mixed_gain_applies_one_absolute_value_to_every_selection():
    source = Source(
        path="/clip.mkv",
        size=10,
        mtime_ns=20,
        duration_us=9_000_000,
        video_stream_index=0,
        audio_tracks=[AudioTrack("audio", 0, 1, "Game audio")],
        frame_rate_num=25,
    )
    project = EditorProject.new(source)
    _first, remainder = project.split(project.segments[0].id, 3_000_000)
    project.split(remainder, 6_000_000)
    segment_ids = tuple(segment.id for segment in project.segments)
    project.set_gain(segment_ids[0], "audio", 1.0)
    project.set_gain(segment_ids[1], "audio", 0.0)
    project.set_gain(segment_ids[2], "audio", 4.0)
    history = EditorHistory(project)
    preview_updates = []
    redraws = []
    window = SimpleNamespace(
        _syncing=False,
        _gain_drag_key=None,
        history=history,
        project=history.project,
        preview=SimpleNamespace(
            update_project=lambda project, **options: preview_updates.append(
                (project, options)
            )
        ),
        _redraw_audio_lane=redraws.append,
    )

    EditorWindow._gain_value(window, 1.25, segment_ids, "audio")

    assert [
        segment.audio["audio"].gain for segment in history.project.segments
    ] == [1.25, 1.25, 1.25]
    assert len(history.undo_stack) == 1
    assert preview_updates == [(history.project, {"seek": False})]
    assert redraws == ["audio"]


def test_activating_a_mixed_mute_badge_mutes_every_selected_segment():
    source = Source(
        path="/clip.mkv",
        size=10,
        mtime_ns=20,
        duration_us=6_000_000,
        video_stream_index=0,
        audio_tracks=[AudioTrack("audio", 0, 1, "Game audio")],
        frame_rate_num=25,
    )
    project = EditorProject.new(source)
    project.split(project.segments[0].id, 3_000_000)
    segment_ids = tuple(segment.id for segment in project.segments)
    project.set_gain(segment_ids[0], "audio", 0.5)
    project.set_gain(segment_ids[1], "audio", 1.5)
    history = EditorHistory(project)
    preview_updates = []
    refreshes = []
    window = SimpleNamespace(
        _syncing=False,
        history=history,
        project=history.project,
        preview=SimpleNamespace(
            update_project=lambda project, **options: preview_updates.append(
                (project, options)
            )
        ),
        _refresh=lambda **options: refreshes.append(options),
    )
    button = SimpleNamespace(get_active=lambda: True)

    EditorWindow._mute(window, button, segment_ids, "audio")

    assert all(
        segment.audio["audio"] == AudioSetting(gain=0, muted=True)
        for segment in history.project.segments
    )
    assert len(history.undo_stack) == 1
    assert preview_updates == [(history.project, {"seek": False})]
    assert refreshes == [{"update_preview": False}]


def test_zoom_drag_tracks_horizontally_and_snaps_at_100_percent():
    args = (110, 25, 300, 1, (100,))
    assert scale_value_after_drag(75, 10, *args) == 100
    assert scale_value_after_drag(100, 1, *args) == 100
    assert scale_value_after_drag(100, 3, *args) == 107.5
    assert scale_value_after_drag(200, -10, *args) == 175


def test_video_track_details_include_frame_rate():
    source = SimpleNamespace(
        width=2560,
        height=1440,
        frame_rate_num=60,
        frame_rate_den=1,
    )
    ntsc_source = SimpleNamespace(
        width=1920,
        height=1080,
        frame_rate_num=60_000,
        frame_rate_den=1001,
    )

    assert format_video_track_details(source) == "2560x1440@60"
    assert format_video_track_details(ntsc_source) == "1920x1080@59.94"


def test_video_lane_segment_labels_use_active_translation(monkeypatch):
    segments = [
        SimpleNamespace(id="one", duration_us=1_000_000),
        SimpleNamespace(id="two", duration_us=2_000_000),
    ]
    window = SimpleNamespace(
        _timeline_clip_width=300,
        project=SimpleNamespace(segments=segments, output_duration_us=3_000_000),
        selected_segment_ids={"one"},
        _segment_rects=lambda _width: [
            (0, segments[0], 0, 100),
            (1, segments[1], 100, 300),
        ],
    )
    area = SimpleNamespace(get_color=lambda: object())
    context = SimpleNamespace(
        fill=lambda: None,
        stroke=lambda: None,
        set_line_width=lambda _width: None,
        save=lambda: None,
        rectangle=lambda *_args: None,
        clip=lambda: None,
        restore=lambda: None,
    )
    labels = []

    monkeypatch.setattr(editor_window, "_", lambda text: {
        "Segment %(number)d": "Сегмент %(number)d",
    }.get(text, text))
    monkeypatch.setattr(editor_window, "_selection_ids", lambda _window: {"one"})
    monkeypatch.setattr(editor_window, "_set_source_color", lambda *_args: None)
    monkeypatch.setattr(editor_window, "_rounded_rectangle", lambda *_args: None)
    monkeypatch.setattr(
        editor_window,
        "_draw_text",
        lambda _area, _context, text, *_args, **_kwargs: labels.append(text),
    )
    monkeypatch.setattr(
        editor_window.Adw.StyleManager,
        "get_default",
        lambda: SimpleNamespace(get_accent_color_rgba=lambda: object()),
    )

    EditorWindow._draw_video_lane(window, area, context, 300, 64, None)

    assert labels[:2] == ["Сегмент 1", "0:01"]
    assert labels[2:4] == ["Сегмент 2", "0:02"]


def test_finishing_a_scrub_seeks_exactly_and_prepares_timeline_playback_once():
    calls = []
    window = SimpleNamespace(
        _scrubbing=True,
        _playhead_us=2_500_000,
        preview=SimpleNamespace(
            seek_output=lambda position: calls.append(("seek", position)),
            finish_seek=lambda: calls.append("finish"),
        ),
        _playback_follow_active=True,
        _cancel_scrub_preview_seek=lambda: calls.append("cancel"),
        _stop_edge_scroll=lambda: calls.append("stop"),
        _viewport_refresh_pending=False,
        _refresh_after_scrub=True,
        _refresh=lambda: calls.append("refresh"),
    )

    EditorWindow._finish_scrub(window)

    assert window._scrubbing is False
    assert window._playback_follow_active is False
    assert calls == ["cancel", ("seek", 2_500_000), "finish", "stop", "refresh"]
    assert window._refresh_after_scrub is False

    EditorWindow._finish_scrub(window)
    assert calls == ["cancel", ("seek", 2_500_000), "finish", "stop", "refresh"]


def test_splitting_during_a_scrub_defers_the_timeline_rebuild_until_release():
    refreshes = []
    redraws = []
    summaries = []
    segment_labels = []
    segment = SimpleNamespace(id="segment", source_start_us=1_000_000)
    project = SimpleNamespace(
        output_duration_us=10_000_000,
        segments=[object(), object(), object()],
        segment_index=lambda _segment_id: 2,
        segment_at_output=lambda _output_us: (segment, 500_000),
        split=lambda segment_id, source_us: refreshes.append((segment_id, source_us)),
    )
    window = SimpleNamespace(
        project=project,
        history=SimpleNamespace(mutate=lambda _label, mutation: mutation(project)),
        _playhead_us=2_000_000,
        _scrubbing=True,
        _refresh_after_scrub=False,
        _can_split_at_playhead=lambda: True,
        _refresh=lambda: refreshes.append("refresh"),
        _redraw_timeline_lanes=lambda: redraws.append(True),
        timeline_summary=SimpleNamespace(set_text=summaries.append),
        _timeline_segment_label=SimpleNamespace(set_text=segment_labels.append),
    )

    EditorWindow._split(window)

    assert refreshes == [("segment", 1_500_000)]
    assert redraws == [True]
    assert summaries == ["3 segments · 0:10"]
    assert segment_labels == ["Segment 3"]
    assert window._refresh_after_scrub is True


def test_mass_delete_is_one_history_step_and_selects_the_nearest_survivor():
    source = Source(
        path="/clip.mkv",
        size=10,
        mtime_ns=20,
        duration_us=10_000_000,
        video_stream_index=0,
        frame_rate_num=25,
    )
    project = EditorProject.new(source)
    _first, remainder = project.split(project.segments[0].id, 2_000_000)
    _second, remainder = project.split(remainder, 4_000_000)
    _third, remainder = project.split(remainder, 6_000_000)
    project.split(remainder, 8_000_000)
    original_ids = [segment.id for segment in project.segments]

    class Window:
        def __init__(self):
            self.history = EditorHistory(project)
            self.selected_segment_ids = {original_ids[1], original_ids[3]}
            self.selected_segment_id = original_ids[3]
            self._playhead_us = 9_000_000
            self.refreshes = 0

        @property
        def project(self):
            return self.history.project

        def _refresh(self):
            self.refreshes += 1

    window = Window()

    EditorWindow._delete(window)

    assert [segment.id for segment in window.project.segments] == [
        original_ids[0],
        original_ids[2],
        original_ids[4],
    ]
    assert window.selected_segment_ids == {original_ids[2]}
    assert window.selected_segment_id == original_ids[2]
    assert window._playhead_us == 6_000_000
    assert len(window.history.undo_stack) == 1
    assert window.refreshes == 1


def test_delete_and_split_sensitivity_accounts_for_the_whole_selection():
    class FakeButton:
        def __init__(self):
            self.sensitive = None
            self.tooltip = None

        def set_sensitive(self, sensitive):
            self.sensitive = sensitive

        def set_tooltip_text(self, tooltip):
            self.tooltip = tooltip

    segments = [SimpleNamespace(id=f"segment-{index}") for index in range(3)]
    split_button = FakeButton()
    delete_button = FakeButton()
    window = SimpleNamespace(
        project=SimpleNamespace(segments=segments),
        selected_segment_ids={segments[0].id, segments[1].id},
        split_button=split_button,
        delete_button=delete_button,
        _can_split_at_playhead=lambda: True,
    )

    EditorWindow._update_edit_action_sensitivity(window)

    assert split_button.sensitive is False
    assert split_button.tooltip == "Select one segment to split"
    assert delete_button.sensitive is True

    window.selected_segment_ids.add(segments[2].id)
    EditorWindow._update_edit_action_sensitivity(window)

    assert delete_button.sensitive is False
    assert delete_button.tooltip == "At least one segment must remain"


def test_selection_refresh_does_not_seek_or_replace_the_preview_project():
    refresh_calls = []
    window = SimpleNamespace(
        selected_segment_id="first",
        _refresh=lambda **kwargs: refresh_calls.append(kwargs),
    )

    EditorWindow._select(window, None, "second")

    assert window.selected_segment_id == "second"
    assert refresh_calls == [{"update_preview": False}]


def test_manual_scroll_suspends_follow_until_playhead_crosses_lead_position():
    adjustment = FakeAdjustment()
    scroll_values = []
    window = SimpleNamespace(
        _setting_timeline_scroll=False,
        _playback_follow_active=True,
        _playback_follow_suspended=False,
        _playback_follow_visible_x=None,
        preview=SimpleNamespace(playing=True),
        timeline_lanes=SimpleNamespace(get_width=lambda: 5_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        project=SimpleNamespace(output_duration_us=10_000_000),
        _playhead_us=2_000_000,
        _set_timeline_scroll_value=scroll_values.append,
    )

    EditorWindow._on_timeline_scroll_changed(window, adjustment)

    assert window._playback_follow_active is False
    assert window._playback_follow_suspended is True
    assert window._playback_follow_visible_x == 500

    window._playhead_us = 2_580_000
    EditorWindow._follow_playhead_scroll(window)
    assert scroll_values == []

    window._playhead_us = 2_620_000
    EditorWindow._follow_playhead_scroll(window)
    assert window._playback_follow_suspended is False
    assert window._playback_follow_active is True
    assert scroll_values == [pytest.approx(510)]


def test_zoom_geometry_changes_do_not_suspend_playback_follow():
    adjustment = FakeAdjustment()
    window = SimpleNamespace(
        _setting_timeline_scroll=False,
        _pending_zoom_anchor=500,
        _pending_scroll_value=None,
        _playback_follow_active=True,
        _playback_follow_suspended=False,
        _playback_follow_visible_x=None,
        preview=SimpleNamespace(playing=True),
        timeline_lanes=SimpleNamespace(get_width=lambda: 5_000),
        project=SimpleNamespace(output_duration_us=10_000_000),
        _playhead_us=2_000_000,
    )

    EditorWindow._on_timeline_scroll_changed(window, adjustment)

    assert window._playback_follow_active is True
    assert window._playback_follow_suspended is False
    assert window._playback_follow_visible_x is None


def test_scrolling_behind_playhead_does_not_snap_back_immediately():
    adjustment = FakeAdjustment()
    scroll_values = []
    window = SimpleNamespace(
        _playback_follow_active=False,
        _playback_follow_suspended=True,
        _playback_follow_visible_x=1_100,
        timeline_lanes=SimpleNamespace(get_width=lambda: 5_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        project=SimpleNamespace(output_duration_us=10_000_000),
        _playhead_us=3_400_000,
        _set_timeline_scroll_value=scroll_values.append,
    )

    EditorWindow._follow_playhead_scroll(window)

    assert window._playback_follow_suspended is True
    assert scroll_values == []


def test_inactive_follow_starts_without_jumping_back_from_the_trigger():
    adjustment = FakeAdjustment()
    scroll_values = []
    window = SimpleNamespace(
        _playback_follow_active=False,
        _playback_follow_suspended=False,
        timeline_lanes=SimpleNamespace(get_width=lambda: 5_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        project=SimpleNamespace(output_duration_us=10_000_000),
        _playhead_us=2_602_000,
        _set_timeline_scroll_value=scroll_values.append,
    )

    EditorWindow._follow_playhead_scroll(window)

    assert window._playback_follow_active is True
    assert scroll_values == [pytest.approx(501)]


def test_active_follow_keeps_the_drawn_playhead_at_one_exact_anchor():
    adjustment = FakeAdjustment(value=0, page_size=999, upper=5_000)
    visible_positions = []
    window = SimpleNamespace(
        _playback_follow_active=True,
        _playback_follow_suspended=False,
        timeline_lanes=SimpleNamespace(get_width=lambda: 5_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        project=SimpleNamespace(output_duration_us=10_000_000),
        _playhead_us=0,
    )

    def set_scroll_value(value):
        adjustment.set_value(value)
        visible_positions.append(
            _playhead_x(
                window._playhead_us,
                window.project.output_duration_us,
                window.timeline_lanes.get_width(),
            )
            - value
        )

    window._set_timeline_scroll_value = set_scroll_value
    for output_us in (3_333_333, 3_373_333, 3_413_333):
        window._playhead_us = output_us
        EditorWindow._follow_playhead_scroll(window)

    expected_anchor = adjustment.get_page_size() * 0.8
    assert visible_positions == [pytest.approx(expected_anchor)] * 3


def test_ctrl_scroll_zooms_at_the_mouse_position_and_consumes_the_event():
    from gi.repository import Gdk

    zoom_changes = []
    adjustment = FakeAdjustment(value=500, page_size=1_000, upper=5_000)
    window = SimpleNamespace(
        _timeline_pointer_x=250,
        _timeline_clip_width=5_000,
        _requested_timeline_width=5_000,
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        _change_timeline_zoom=lambda direction, anchor=None: zoom_changes.append(
            (direction, anchor)
        ),
    )
    controller = FakeScrollController(Gdk.ModifierType.CONTROL_MASK)

    assert EditorWindow._on_timeline_scroll(window, controller, 0, -1) is True
    assert EditorWindow._on_timeline_scroll(window, controller, 0, 1) is True
    assert zoom_changes == [(1, (0.15, 250)), (-1, (0.15, 250))]


def test_plain_scroll_moves_horizontally_and_suspends_playhead_follow():
    adjustment = FakeAdjustment(value=500, page_size=1_000, upper=5_000)
    window = SimpleNamespace(
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        timeline_lanes=SimpleNamespace(get_width=lambda: 5_000),
        project=SimpleNamespace(output_duration_us=10_000_000),
        preview=SimpleNamespace(playing=True),
        _playhead_us=2_000_000,
        _setting_timeline_scroll=False,
        _playback_follow_active=True,
        _playback_follow_suspended=False,
        _playback_follow_visible_x=None,
    )
    adjustment.value_changed_callback = lambda changed: (
        EditorWindow._on_timeline_scroll_changed(window, changed)
    )
    controller = FakeScrollController(0)

    assert EditorWindow._on_timeline_scroll(window, controller, 0, 1) is True
    assert adjustment.value == 550
    assert window._playback_follow_active is False
    assert window._playback_follow_suspended is True
    assert window._playback_follow_visible_x == 450


def test_shift_scroll_moves_the_video_and_audio_track_list_vertically():
    from gi.repository import Gdk

    vertical_adjustment = FakeAdjustment(value=100, page_size=300, upper=900)
    window = SimpleNamespace(
        timeline_scroll=SimpleNamespace(
            get_vadjustment=lambda: vertical_adjustment,
            get_hadjustment=lambda: None,
        ),
    )
    controller = FakeScrollController(Gdk.ModifierType.SHIFT_MASK)

    assert EditorWindow._on_timeline_scroll(window, controller, 0, 1) is True
    assert vertical_adjustment.value == 150


def test_ctrl_scroll_falls_back_to_viewport_center_without_pointer_motion():
    from gi.repository import Gdk

    anchors = []
    adjustment = FakeAdjustment(value=1_000, page_size=800, upper=4_000)
    window = SimpleNamespace(
        _timeline_pointer_x=None,
        _timeline_clip_width=4_000,
        _requested_timeline_width=4_000,
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        _change_timeline_zoom=lambda _direction, anchor=None: anchors.append(anchor),
    )
    controller = FakeScrollController(Gdk.ModifierType.CONTROL_MASK)

    EditorWindow._on_timeline_scroll(window, controller, 0, -1)

    assert anchors == [(0.35, 400)]


def test_zoom_scale_has_only_a_single_native_snap_mark_at_100_percent():
    from gi.repository import Gtk

    scale = FakeZoomScale()
    window = SimpleNamespace(zoom_scale=scale)

    EditorWindow._set_zoom_scale_range(window, 25)

    assert scale.ranges == [(25, 300)]
    assert scale.clear_count == 1
    assert scale.marks == [(100, Gtk.PositionType.BOTTOM, None)]


def test_zoom_scale_range_never_exceeds_its_maximum():
    scale = FakeZoomScale()
    window = SimpleNamespace(zoom_scale=scale)

    EditorWindow._set_zoom_scale_range(window, 25)

    assert scale.ranges == [(25, 300)]
    assert scale.clear_count == 1
    assert scale.marks != []


def _zoom_test_window(next_anchor=None):
    adjustment = FakeAdjustment(value=5_000, page_size=1_000, upper=11_000)
    scroll_values = []
    window = SimpleNamespace(
        _syncing=False,
        _zoom=1.0,
        _timeline_clip_width=11_000,
        _next_zoom_anchor=next_anchor,
        _pending_zoom_anchor=None,
        _pending_zoom_fraction=None,
        _playhead_us=50_000_000,
        project=SimpleNamespace(output_duration_us=100_000_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        timeline_lanes=SimpleNamespace(get_width=lambda: 11_000),
        zoom_percentage=SimpleNamespace(set_text=lambda _text: None),
        _timeline_viewport_width=lambda: 1_000,
        _resize_timeline_lanes=lambda _width: None,
        _set_timeline_scroll_value=scroll_values.append,
        _update_playhead=lambda _width: None,
        _schedule_zoom_anchor=lambda: None,
    )
    return window, scroll_values


def test_slider_zoom_defaults_to_the_playhead_anchor():
    window, scroll_values = _zoom_test_window()
    scale = SimpleNamespace(get_value=lambda: 200)

    EditorWindow._on_zoom_changed(window, scale)

    assert window._pending_zoom_fraction == 0.5
    assert window._pending_zoom_anchor == 500
    assert scroll_values == [6_870]


def test_slider_zoom_uses_the_exact_drawn_playhead_pixel_as_its_anchor():
    window, scroll_values = _zoom_test_window()
    window._playhead_us = 47_777_777
    old_playhead_x = _playhead_x(
        window._playhead_us,
        window.project.output_duration_us,
        window._timeline_clip_width,
    )
    old_visible_x = old_playhead_x - 5_000

    EditorWindow._on_zoom_changed(window, SimpleNamespace(get_value=lambda: 137))

    new_playhead_x = _playhead_x(
        window._playhead_us,
        window.project.output_duration_us,
        window._timeline_clip_width,
    )
    assert new_playhead_x - scroll_values[-1] == pytest.approx(old_visible_x)


def test_scroll_zoom_uses_the_one_shot_mouse_anchor():
    window, scroll_values = _zoom_test_window(next_anchor=(0.25, 200))
    scale = SimpleNamespace(get_value=lambda: 200)

    EditorWindow._on_zoom_changed(window, scale)

    assert window._next_zoom_anchor is None
    assert window._pending_zoom_fraction == 0.25
    assert window._pending_zoom_anchor == 200
    assert scroll_values == [3_485]


def test_zoom_drag_coalesces_expensive_timeline_resizes(monkeypatch):
    scheduled = []
    removed = []
    monkeypatch.setattr(
        editor_window.GLib,
        "timeout_add",
        lambda interval, callback: scheduled.append((interval, callback)) or 17,
    )
    monkeypatch.setattr(editor_window.GLib, "source_remove", removed.append)
    window, _scroll_values = _zoom_test_window()
    window._zoom_dragging = False
    window._zoom_discrete_active = False
    window._zoom_update_id = None
    window._pending_zoom_percent = None
    window._zoom_settle_id = None
    window._waveform_restore_id = None
    window._pending_waveform_restore_lanes = []
    window._flush_zoom_update = lambda: EditorWindow._flush_zoom_update(window)
    window._cancel_zoom_update = lambda: EditorWindow._cancel_zoom_update(window)
    window._redraw_timeline_lanes = lambda: None

    EditorWindow._zoom_drag_begin(window, None)
    EditorWindow._on_zoom_changed(window, SimpleNamespace(get_value=lambda: 125))
    EditorWindow._on_zoom_changed(window, SimpleNamespace(get_value=lambda: 150))

    assert window._zoom == 1.0
    assert len(scheduled) == 1
    assert scheduled[0][0] == editor_window.ZOOM_TIMELINE_REFRESH_MS
    assert scheduled[0][1]() is False
    assert window._zoom == 1.5

    EditorWindow._on_zoom_changed(window, SimpleNamespace(get_value=lambda: 175))
    assert len(scheduled) == 2
    EditorWindow._zoom_drag_end(window, SimpleNamespace(get_value=lambda: 175))

    assert removed == [17]
    assert window._zoom == 1.75
    assert window._zoom_dragging is False


def test_step_zoom_uses_the_same_coalesced_rendering_path(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        editor_window.GLib,
        "timeout_add",
        lambda interval, callback: scheduled.append((interval, callback))
        or len(scheduled),
    )
    monkeypatch.setattr(editor_window.GLib, "source_remove", lambda _source_id: None)
    window, _scroll_values = _zoom_test_window()
    window._zoom_dragging = False
    window._zoom_discrete_active = False
    window._zoom_update_id = None
    window._pending_zoom_percent = None
    window._zoom_settle_id = None
    window._waveform_restore_id = None
    window._pending_waveform_restore_lanes = []
    window._audio_lanes = {}
    window._flush_zoom_update = lambda: EditorWindow._flush_zoom_update(window)
    window._finish_discrete_zoom = lambda: EditorWindow._finish_discrete_zoom(window)
    window._complete_discrete_zoom = lambda: EditorWindow._complete_discrete_zoom(window)
    window._cancel_zoom_update = lambda: EditorWindow._cancel_zoom_update(window)

    class EmittingZoomScale:
        value = 100

        def get_value(self):
            return self.value

        def set_value(self, value):
            self.value = value
            EditorWindow._on_zoom_changed(window, self)

        def set_tooltip_text(self, _text):
            pass

    window.zoom_scale = EmittingZoomScale()

    EditorWindow._change_timeline_zoom(window, 1)

    assert window._zoom_discrete_active is True
    assert window._zoom == 1.0
    assert [interval for interval, _callback in scheduled] == [
        editor_window.ZOOM_TIMELINE_REFRESH_MS,
        editor_window.ZOOM_INTERACTION_SETTLE_MS,
    ]

    assert scheduled[0][1]() is False
    assert window._zoom == 1.25
    assert scheduled[1][1]() is False
    assert window._zoom_discrete_active is False


def test_short_clip_zoom_is_not_replaced_with_viewport_fit_zoom():
    adjustment = FakeAdjustment(value=0, page_size=1_000, upper=1_000)
    resized = []
    labels = []
    window = SimpleNamespace(
        _syncing=False,
        _zoom=1.0,
        _timeline_clip_width=110,
        _next_zoom_anchor=None,
        _pending_zoom_anchor=None,
        _pending_zoom_fraction=None,
        _playhead_us=500_000,
        project=SimpleNamespace(output_duration_us=1_000_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        timeline_lanes=SimpleNamespace(get_width=lambda: 1_000),
        zoom_percentage=SimpleNamespace(set_text=labels.append),
        _timeline_viewport_width=lambda: 1_000,
        _resize_timeline_lanes=resized.append,
        _set_timeline_scroll_value=lambda _value: None,
        _update_playhead=lambda _width: None,
        _schedule_zoom_anchor=lambda: None,
    )

    EditorWindow._on_zoom_changed(window, SimpleNamespace(get_value=lambda: 25))

    assert window._zoom == 0.25
    assert window._timeline_clip_width == 18
    assert resized == [1_000]
    assert labels == ["25%"]


def test_post_layout_zoom_anchor_is_applied_only_once():
    adjustment = FakeAdjustment(value=0, page_size=1_000, upper=4_000)
    scroll_values = []
    window = SimpleNamespace(
        _cleaned=False,
        _pending_zoom_anchor=600,
        _pending_zoom_fraction=0.5,
        _pending_scroll_value=None,
        _zoom_anchor_timeout_id=17,
        _zoom_anchor_attempts=0,
        _timeline_clip_width=4_000,
        _requested_timeline_width=4_000,
        timeline_lanes=SimpleNamespace(get_width=lambda: 4_000),
        timeline_scroll=SimpleNamespace(get_hadjustment=lambda: adjustment),
        project=SimpleNamespace(output_duration_us=10_000_000),
        _playhead_us=5_000_000,
        _set_timeline_scroll_value=scroll_values.append,
    )

    assert EditorWindow._apply_zoom_anchor(window) is False
    assert scroll_values == [1_400]
    assert window._pending_zoom_anchor is None
    assert window._pending_zoom_fraction is None
    assert window._zoom_anchor_timeout_id is None
