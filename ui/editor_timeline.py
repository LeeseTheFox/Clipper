"""Pure timeline geometry used by the GTK drawing widgets."""

from __future__ import annotations

MIN_ZOOM = 10.0
MAX_ZOOM = 1000.0
# Calibrate 100% to the density previously used by the 67% setting.
BASE_PIXELS_PER_SECOND = 73.7
MAX_CONTENT_WIDTH = 100_000
MIN_TIMELINE_ZOOM = 0.10
MAX_TIMELINE_ZOOM = 3.0
PLAYBACK_FOLLOW_POSITION = 0.8


def clamp_zoom(pixels_per_second: float) -> float:
    return max(MIN_ZOOM, min(MAX_ZOOM, float(pixels_per_second)))


def time_to_pixel(time_us: int, pixels_per_second: float, scroll_x: float = 0) -> float:
    return time_us / 1_000_000 * clamp_zoom(pixels_per_second) - scroll_x


def pixel_to_time(pixel: float, pixels_per_second: float, scroll_x: float = 0) -> int:
    return max(0, round((pixel + scroll_x) / clamp_zoom(pixels_per_second) * 1_000_000))


def content_width(duration_us: int, pixels_per_second: float, viewport_width: float = 0) -> float:
    return max(float(viewport_width), time_to_pixel(duration_us, pixels_per_second))


def segment_at_pixel(project, pixel: float, pixels_per_second: float, scroll_x: float = 0):
    time_us = pixel_to_time(pixel, pixels_per_second, scroll_x)
    if time_us >= project.output_duration_us:
        return None
    return project.segment_at_output(time_us)[0]


def effective_zoom(
    requested_zoom: float,
    duration_us: int,
    viewport_width: int,
    *,
    base_pixels_per_second: float = BASE_PIXELS_PER_SECOND,
    max_content_width: int = MAX_CONTENT_WIDTH,
    minimum_zoom: float = MIN_TIMELINE_ZOOM,
    maximum_zoom: float = MAX_TIMELINE_ZOOM,
) -> float:
    """Clamp zoom without making it depend on the current viewport size."""
    duration_seconds = max(0.001, duration_us / 1_000_000)
    content_maximum = max_content_width / (duration_seconds * base_pixels_per_second)
    maximum = max(minimum_zoom, min(maximum_zoom, content_maximum))
    return max(minimum_zoom, min(maximum, float(requested_zoom)))


def timeline_clip_width(
    zoom: float,
    duration_us: int,
    *,
    base_pixels_per_second: float = BASE_PIXELS_PER_SECOND,
    max_content_width: int = MAX_CONTENT_WIDTH,
) -> int:
    """Return the pixel width occupied by clip content at a given zoom."""
    duration_seconds = max(0.001, duration_us / 1_000_000)
    return max(
        1,
        min(max_content_width, round(duration_seconds * base_pixels_per_second * zoom)),
    )


def timeline_content_width(
    zoom: float,
    duration_us: int,
    viewport_width: int,
    *,
    base_pixels_per_second: float = BASE_PIXELS_PER_SECOND,
    max_content_width: int = MAX_CONTENT_WIDTH,
    minimum_zoom: float = MIN_TIMELINE_ZOOM,
    maximum_zoom: float = MAX_TIMELINE_ZOOM,
) -> tuple[int, float]:
    """Return the allocated width and the effective zoom represented by it."""
    actual_zoom = effective_zoom(
        zoom,
        duration_us,
        viewport_width,
        base_pixels_per_second=base_pixels_per_second,
        max_content_width=max_content_width,
        minimum_zoom=minimum_zoom,
        maximum_zoom=maximum_zoom,
    )
    width = timeline_clip_width(
        actual_zoom,
        duration_us,
        base_pixels_per_second=base_pixels_per_second,
        max_content_width=max_content_width,
    )
    return max(viewport_width, width), actual_zoom


def follow_scroll_value(
    playhead_x: float,
    current_value: float,
    page_size: float,
    upper: float,
    *,
    left_trigger: float = 0.12,
    right_trigger: float = PLAYBACK_FOLLOW_POSITION,
    left_anchor: float = 0.2,
    right_anchor: float = PLAYBACK_FOLLOW_POSITION,
) -> float:
    """Return a scroll position that keeps a moving playhead comfortably visible."""
    if page_size <= 0 or upper <= page_size:
        return 0.0
    visible_x = playhead_x - current_value
    target = current_value
    if visible_x > page_size * right_trigger:
        target = playhead_x - page_size * right_anchor
    elif visible_x < page_size * left_trigger:
        target = playhead_x - page_size * left_anchor
    return max(0.0, min(upper - page_size, target))


def crossed_playback_follow_trigger(
    previous_visible_x: float | None,
    visible_x: float,
    page_size: float,
    *,
    lead_position: float = PLAYBACK_FOLLOW_POSITION,
) -> bool:
    """Return whether a moving playhead newly crossed its steady lead position."""
    if previous_visible_x is None or page_size <= 0:
        return False
    trigger_x = page_size * lead_position
    return previous_visible_x < trigger_x <= visible_x


def edge_scroll_delta(
    pointer_x: float,
    viewport_width: float,
    *,
    edge_size: float = 56.0,
    maximum_speed: float = 36.0,
) -> float:
    """Return signed pixels per tick for a pointer inside an autoscroll edge."""
    if viewport_width <= 0 or edge_size <= 0:
        return 0.0
    if pointer_x < edge_size:
        strength = min(1.0, (edge_size - pointer_x) / edge_size)
        return -maximum_speed * strength
    right_edge = viewport_width - edge_size
    if pointer_x > right_edge:
        strength = min(1.0, (pointer_x - right_edge) / edge_size)
        return maximum_speed * strength
    return 0.0


def anchored_zoom_scroll_value(
    playhead_fraction: float,
    content_width: float,
    anchor_x: float,
    page_size: float,
    upper: float,
) -> float:
    """Keep a timeline position at the same viewport coordinate after a zoom."""
    width = max(1.0, content_width)
    playhead_x = round(
        max(0.0, min(1.0, playhead_fraction)) * max(0.0, width - 1)
    )
    maximum = max(0.0, upper - page_size)
    return max(0.0, min(maximum, playhead_x - anchor_x))


def waveform_sample_step(
    content_width: int,
    output_duration_us: int,
    source_duration_us: int,
    bucket_count: int,
) -> int:
    """Return the pixel width represented by one source waveform bucket."""
    if output_duration_us <= 0 or source_duration_us <= 0 or bucket_count <= 0:
        return 1
    bucket_width = (
        max(1, content_width)
        * source_duration_us
        / (output_duration_us * bucket_count)
    )
    return max(1, round(bucket_width))
