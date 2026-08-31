import pytest
from editor_timeline import (
    BASE_PIXELS_PER_SECOND,
    anchored_zoom_scroll_value,
    crossed_playback_follow_trigger,
    edge_scroll_delta,
    effective_zoom,
    follow_scroll_value,
    pixel_to_time,
    time_to_pixel,
    timeline_clip_width,
    timeline_content_width,
    waveform_sample_step,
)

LEGACY_BASE_PIXELS_PER_SECOND = 110.0


@pytest.mark.parametrize(
    ("zoom", "legacy_zoom"),
    ((1.0, 0.67), (0.10, 0.067)),
)
def test_zoom_density_matches_the_previous_scale_at_the_requested_levels(
    zoom,
    legacy_zoom,
):
    duration_us = 20_000_000

    assert timeline_clip_width(zoom, duration_us) == timeline_clip_width(
        legacy_zoom,
        duration_us,
        base_pixels_per_second=LEGACY_BASE_PIXELS_PER_SECOND,
    )


def test_zoom_base_is_calibrated_to_the_previous_67_percent_density():
    assert BASE_PIXELS_PER_SECOND == pytest.approx(110.0 * 0.67)


def test_short_timeline_keeps_requested_zoom_below_viewport_width():
    duration_us = 20_000_000
    viewport_width = 770
    width, zoom = timeline_content_width(0.25, duration_us, viewport_width)

    assert width == viewport_width
    assert zoom == pytest.approx(0.25)
    assert timeline_clip_width(zoom, duration_us) == 368


def test_maximum_zoom_stops_percentage_when_content_hits_limit():
    width, zoom = timeline_content_width(8, 600_000_000, 900, max_content_width=20_000)

    assert width == 20_000
    assert zoom == pytest.approx(20_000 / (600 * BASE_PIXELS_PER_SECOND))
    assert effective_zoom(zoom * 1.25, 600_000_000, 900, max_content_width=20_000) == pytest.approx(
        zoom
    )


def test_playhead_and_click_mapping_use_same_allocated_width():
    duration_us = 20_000_000
    allocated_width = 1_120
    target_us = 15_000_000
    pixel = target_us / duration_us * allocated_width

    assert pixel_to_time(pixel, allocated_width / 20) == target_us
    assert time_to_pixel(target_us, allocated_width / 20) == pixel


def test_resizing_viewport_does_not_change_zoom_or_clip_width():
    duration_us = 20_000_000

    small_width, small_zoom = timeline_content_width(0.25, duration_us, 700)
    large_width, large_zoom = timeline_content_width(0.25, duration_us, 1_100)

    assert small_width == 700
    assert large_width == 1_100
    assert small_zoom == large_zoom == 0.25
    assert timeline_clip_width(small_zoom, duration_us) == 368


def test_playback_follow_starts_at_steady_anchor_and_clamps_to_end():
    assert follow_scroll_value(800, 0, 1_000, 5_000) == 0
    assert follow_scroll_value(801, 0, 1_000, 5_000) == pytest.approx(1)
    assert follow_scroll_value(4_950, 3_900, 1_000, 5_000) == pytest.approx(4_000)


def test_playback_follow_does_not_move_for_centered_playhead():
    assert follow_scroll_value(1_500, 1_000, 1_000, 5_000) == 1_000


def test_suspended_follow_resumes_only_when_playhead_crosses_lead_threshold():
    assert crossed_playback_follow_trigger(790, 810, 1_000) is True
    assert crossed_playback_follow_trigger(500, 790, 1_000) is False
    assert crossed_playback_follow_trigger(900, 950, 1_000) is False


def test_edge_scroll_direction_and_strength():
    assert edge_scroll_delta(28, 1_000) == pytest.approx(-18)
    assert edge_scroll_delta(972, 1_000) == pytest.approx(18)
    assert edge_scroll_delta(500, 1_000) == 0
    assert edge_scroll_delta(-20, 1_000) == -36
    assert edge_scroll_delta(1_020, 1_000) == 36


def test_zoom_is_capped_at_three_hundred_percent():
    width, zoom = timeline_content_width(8, 20_000_000, 900)

    assert zoom == 3
    assert width == 4_422


def test_short_timeline_uses_empty_lane_space_at_three_hundred_percent():
    width, zoom = timeline_content_width(1, 1_000_000, 900)

    assert zoom == 1
    assert width == 900
    assert timeline_clip_width(zoom, 1_000_000) == 74


def test_zoom_keeps_playhead_at_same_viewport_anchor():
    target = anchored_zoom_scroll_value(
        playhead_fraction=0.5,
        content_width=4_000,
        anchor_x=600,
        page_size=1_000,
        upper=4_000,
    )

    assert target == 1_400
    assert 4_000 * 0.5 - target == 600


def test_zoom_anchor_uses_the_same_rounded_pixel_as_the_playhead():
    target = anchored_zoom_scroll_value(
        playhead_fraction=1 / 3,
        content_width=5_000,
        anchor_x=317.25,
        page_size=1_000,
        upper=5_000,
    )

    drawn_playhead_x = round((1 / 3) * (5_000 - 1))
    assert drawn_playhead_x - target == pytest.approx(317.25)


def test_waveform_sampling_does_not_redraw_repeated_source_buckets():
    assert waveform_sample_step(2_200, 20_000_000, 20_000_000, 1_000) == 2
    assert waveform_sample_step(6_600, 20_000_000, 20_000_000, 1_000) == 7
    assert waveform_sample_step(900, 20_000_000, 20_000_000, 1_000) == 1
