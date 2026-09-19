"""Dedicated, non-destructive single-clip editor window."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import gi
from i18n import _, ngettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")

from editor_drafts import save_draft
from editor_history import EditorHistory
from editor_model import (
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    EditorProject,
    effective_audio_gain,
)
from editor_preview import EditorPreview
from editor_timeline import (
    PLAYBACK_FOLLOW_POSITION,
    anchored_zoom_scroll_value,
    crossed_playback_follow_trigger,
    edge_scroll_delta,
    follow_scroll_value,
    timeline_clip_width,
    timeline_content_width,
    waveform_sample_step,
)
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo
from icon_names import (
    EXPORT_LINEAR,
    FULLSCREEN,
    HAMBURGER_MENU,
    PAUSE,
    PLAY,
    QUIT_FULLSCREEN,
    REDO,
    SCISSORS_OUTLINE,
    TRASH,
    UNDO,
    VOLUME,
    VOLUME_CROSS,
    VOLUME_MEDIUM,
    ZOOM_IN,
    ZOOM_OUT,
)
from icon_widgets import TintedIcon
from shortcut_dialog import (
    Shortcut,
    ShortcutSection,
    create_shortcuts_dialog,
    create_shortcuts_menu,
)

_EDITOR_CSS_INSTALLED = False
TRACK_HEADER_WIDTH = 208
RULER_HEIGHT = 34
VIDEO_TRACK_HEIGHT = 64
AUDIO_TRACK_HEIGHT = 74
TRACK_ROW_BORDER = 1
PEAK_METER_WIDTH = 10
PEAK_METER_HEIGHT = AUDIO_TRACK_HEIGHT + TRACK_ROW_BORDER
PEAK_METER_VERTICAL_MARGIN = 12.0
PEAK_METER_FLOOR_DB = -60.0
PEAK_METER_YELLOW_DB = -18.0
PEAK_METER_RED_DB = -6.0
SCALE_SNAP_RADIUS_PX = 2
GAIN_LINE_MARGIN = 10.0
GAIN_LINE_HIT_RADIUS = 7.0
NEGATIVE_GAIN_CURVE_EXPONENT = 2.0
GAIN_FEEDBACK_WIDTH = 96.0
GAIN_FEEDBACK_HEIGHT = 20.0
GAIN_FEEDBACK_CURSOR_GAP = 18.0
GAIN_FEEDBACK_HORIZONTAL_PADDING = 14.0
SCRUB_PREVIEW_INTERVAL_MS = 50
GAIN_WAVEFORM_REFRESH_MS = 33
ZOOM_TIMELINE_REFRESH_MS = 50
ZOOM_INTERACTION_SETTLE_MS = 180
WAVEFORM_DETAIL_RESTORE_MS = 16
MIXED_MUTE_TOOLTIP = _("Mixed mute · click to mute all")
AMBIENT_GLOW_BLUR_RADIUS = 52.0
AMBIENT_GLOW_OPACITY = 0.34
AMBIENT_GLOW_HORIZONTAL_SCALE = 0.22
AMBIENT_GLOW_MAX_EXTENT = 160.0
AMBIENT_GLOW_SAMPLE_WIDTH = 64
AMBIENT_GLOW_SAMPLE_INTERVAL_MS = 500
AMBIENT_GLOW_CROSSFADE_MS = 420
_VOLUME_STEP = 0.05
_VOLUME_SCROLL_STEP = 0.1

EDITOR_SHORTCUT_SECTIONS = (
    ShortcutSection(
        _("Editing"),
        (
            Shortcut(_("Select all segments"), "<Control>a"),
            Shortcut(_("Split at playhead"), "x"),
            Shortcut(
                _("Delete selected segments"), "Delete"
            ),
            Shortcut(_("Undo"), "<Control>z"),
            Shortcut(_("Redo"), "<Control><Shift>z"),
        ),
    ),
    ShortcutSection(
        _("Playback"),
        (
            Shortcut(_("Play or pause"), "space"),
            Shortcut(_("Seek backward 5 seconds"), "Left"),
            Shortcut(_("Seek forward 5 seconds"), "Right"),
            Shortcut(_("Seek backward one frame"), "comma"),
            Shortcut(_("Seek forward one frame"), "period"),
            Shortcut(_("Mute or unmute"), "m"),
        ),
    ),
    ShortcutSection(
        _("View"),
        (
            Shortcut(_("Toggle fullscreen"), "f"),
            Shortcut(_("Zoom in"), "<Control>plus"),
            Shortcut(_("Zoom out"), "<Control>minus"),
            Shortcut(
                _("Zoom in"),
                "Up",
            ),
            Shortcut(
                _("Zoom out"),
                "Down",
            ),
            Shortcut(_("Close or exit fullscreen"), "Escape"),
        ),
    ),
)

_EDITOR_CSS = """
.editor-workspace {
    background-color: @window_bg_color;
    color: @window_fg_color;
}

.editor-monitor-area {
    background-color: @view_bg_color;
    border-bottom: 1px solid alpha(currentColor, 0.12);
}

.editor-video-fullscreen .editor-monitor-area {
    background-color: #08090b;
    border-bottom: none;
}

.editor-monitor-frame {
    background-color: #08090b;
    border-radius: 10px;
    border: 1px solid alpha(currentColor, 0.14);
    box-shadow: 0 4px 16px alpha(black, 0.24);
}

.editor-video-fullscreen .editor-monitor-frame {
    border: none;
    border-radius: 0;
    box-shadow: none;
}

.editor-video-fullscreen .editor-workspace-paned > separator {
    opacity: 0;
}

.editor-monitor-picture {
    background-color: transparent;
    border-radius: 0;
}

.editor-loading-spinner {
    color: white;
}

.clipper-player-controls {
    min-height: 36px;
    padding: 6px 8px;
    color: white;
    background-color: alpha(#111419, 0.94);
    border: 1px solid alpha(white, 0.14);
    border-radius: 14px;
    box-shadow: 0 8px 28px alpha(black, 0.52);
}

.clipper-player-timeline {
    margin: 0 6px;
    padding: 0;
}

.clipper-player-timeline trough {
    min-height: 4px;
    border-radius: 9999px;
    background-color: alpha(white, 0.24);
}

.clipper-player-timeline highlight {
    min-height: 4px;
    border-radius: 9999px;
    background-color: @accent_bg_color;
}

.clipper-player-timeline slider {
    min-width: 14px;
    min-height: 14px;
    border-radius: 9999px;
}

.clipper-player-control-button {
    min-width: 36px;
    min-height: 36px;
    padding: 0;
    border-radius: 9999px;
}

.clipper-player-time {
    min-width: 96px;
    margin-left: 2px;
    margin-right: 4px;
    color: alpha(white, 0.82);
    font-size: 0.9em;
    font-variant-numeric: tabular-nums;
}

.clipper-player-volume {
    min-width: 88px;
    margin: 0 6px 0 4px;
    padding: 0;
}

.clipper-player-volume trough {
    min-height: 4px;
    border-radius: 9999px;
    background-color: alpha(white, 0.24);
}

.clipper-player-volume highlight {
    min-height: 4px;
    border-radius: 9999px;
    background-color: @accent_bg_color;
}

.clipper-player-volume slider {
    min-width: 12px;
    min-height: 12px;
    margin: -4px;
    padding: 0;
    border-radius: 9999px;
    background-color: white;
    border: 1px solid alpha(black, 0.2);
    box-shadow: 0 1px 3px alpha(black, 0.4);
}

.editor-workspace-paned > separator {
    min-height: 1px;
    background-color: alpha(currentColor, 0.12);
}

.editor-timeline-panel { background-color: @window_bg_color; }

.editor-timeline-toolbar {
    min-height: 42px;
    padding: 2px 0;
}

.editor-tool-button {
    padding-left: 10px;
    padding-right: 10px;
}

.editor-edit-actions button {
    min-height: 32px;
}

.editor-toolbar-separator {
    margin-left: 6px;
    margin-right: 6px;
}

.editor-history-actions button,
.editor-zoom-button {
    min-width: 32px;
    min-height: 32px;
    padding: 0;
}

.editor-zoom-button { border-radius: 8px; }

.editor-zoom-controls {
    padding: 0 2px;
}

.editor-zoom-scale { min-width: 112px; }

.editor-track-card {
    background-color: @view_bg_color;
    border: 1px solid alpha(currentColor, 0.14);
    border-radius: 10px;
    box-shadow: 0 2px 8px alpha(black, 0.12);
}

.editor-track-headers {
    background-color: @card_bg_color;
    border-right: 1px solid alpha(currentColor, 0.14);
}

.editor-track-header {
    padding: 0 12px;
    background-color: @card_bg_color;
    border-bottom: 1px solid alpha(currentColor, 0.10);
}

.editor-ruler-header,
.editor-ruler-lane {
    background-color: @headerbar_bg_color;
}

.editor-track-badge {
    min-width: 28px;
    min-height: 24px;
    padding: 0 4px;
    border-radius: 6px;
    background-color: color-mix(in srgb, var(--accent-color) 18%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-color) 28%, transparent);
    color: @window_fg_color;
    font-weight: 700;
}

.editor-track-badge:checked {
    color: alpha(@window_fg_color, 0.55);
    background-color: alpha(@window_fg_color, 0.08);
    border-color: alpha(@window_fg_color, 0.16);
}

.editor-track-badge.mixed {
    border-style: dashed;
}

.editor-peak-meter {
    min-width: 10px;
    min-height: 75px;
    color: @success_color;
}

.editor-timeline-lane {
    background-color: @view_bg_color;
    border-bottom: 1px solid alpha(currentColor, 0.10);
}

.editor-ruler-lane { color: alpha(@window_fg_color, 0.72); }
.editor-video-lane,
.editor-audio-lane { color: @window_fg_color; }
.editor-empty-audio-lane { color: alpha(@window_fg_color, 0.58); }
.editor-playhead { color: @error_color; }
"""


def _install_editor_css():
    global _EDITOR_CSS_INSTALLED
    if _EDITOR_CSS_INSTALLED or Gdk.Display.get_default() is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_EDITOR_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _EDITOR_CSS_INSTALLED = True


def format_time(microseconds: int) -> str:
    seconds = max(0, microseconds) // 1_000_000
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_transport_time(microseconds: int) -> str:
    seconds = max(0, int(microseconds // 1_000_000))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def gain_to_decibels(gain: float) -> float:
    """Convert linear amplitude to the editor's bounded dB range."""
    try:
        gain = float(gain)
    except (TypeError, ValueError):
        return MIN_GAIN_DB
    if not math.isfinite(gain) or gain <= 0:
        return MIN_GAIN_DB
    return max(MIN_GAIN_DB, min(MAX_GAIN_DB, 20 * math.log10(gain)))


def decibels_to_gain(decibels: float) -> float:
    """Convert the editor's bounded dB value to linear amplitude."""
    decibels = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(decibels)))
    return 10 ** (decibels / 20)


def gain_line_y(decibels: float, height: float) -> float:
    """Place unity in the lane center with an exponential negative-side curve."""
    height = max(1.0, float(height))
    top = min(GAIN_LINE_MARGIN, height / 2)
    bottom = max(height - GAIN_LINE_MARGIN, height / 2)
    midpoint = height / 2
    decibels = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(decibels)))
    if decibels >= 0:
        travel = max(1.0, midpoint - top)
        return midpoint - decibels / MAX_GAIN_DB * travel
    travel = max(1.0, bottom - midpoint)
    negative_fraction = decibels / MIN_GAIN_DB
    return midpoint + negative_fraction ** (1 / NEGATIVE_GAIN_CURVE_EXPONENT) * travel


def gain_line_decibels(y: float, height: float) -> float:
    """Convert an in-lane vertical line position back to decibels."""
    height = max(1.0, float(height))
    top = min(GAIN_LINE_MARGIN, height / 2)
    bottom = max(height - GAIN_LINE_MARGIN, height / 2)
    midpoint = height / 2
    y = max(top, min(bottom, float(y)))
    if y <= midpoint:
        travel = max(1.0, midpoint - top)
        return (midpoint - y) / travel * MAX_GAIN_DB
    travel = max(1.0, bottom - midpoint)
    negative_fraction = (y - midpoint) / travel
    return negative_fraction**NEGATIVE_GAIN_CURVE_EXPONENT * MIN_GAIN_DB


def gain_db_after_drag(start_db: float, offset_y: float, height: float) -> float:
    """Map relative vertical pointer movement to dB and snap at unity."""
    y = gain_line_y(start_db, height) + float(offset_y)
    midpoint = max(1.0, float(height)) / 2
    if abs(y - midpoint) <= SCALE_SNAP_RADIUS_PX:
        y = midpoint
    return gain_line_decibels(y, height)


def format_gain_db(decibels: float) -> str:
    decibels = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(decibels)))
    if abs(decibels) < 0.05:
        decibels = 0.0
    return (
        _("Gain %(decibels)+.1f dB") % {"decibels": decibels}
        if decibels
        else _("Gain 0.0 dB")
    )


def gain_feedback_badge_x(pointer_x, badge_width, visible_left, visible_right):
    """Place gain feedback beside the cursor while keeping it in view."""
    padding = 2.0
    minimum = visible_left + padding
    maximum = max(minimum, visible_right - badge_width - padding)
    right = pointer_x + GAIN_FEEDBACK_CURSOR_GAP
    if right <= maximum:
        return right
    left = pointer_x - GAIN_FEEDBACK_CURSOR_GAP - badge_width
    if left >= minimum:
        return left
    return max(minimum, min(maximum, right))


def gain_feedback_badge_width(text_width, available_width):
    """Return enough badge width for the label while respecting the viewport."""
    available_width = max(1.0, float(available_width) - 4)
    requested_width = max(
        GAIN_FEEDBACK_WIDTH,
        float(text_width) + GAIN_FEEDBACK_HORIZONTAL_PADDING,
    )
    return min(requested_width, available_width)


def _disable_hold_to_fine_tune(scale) -> None:
    """Remove GtkRange's private long-press fine-tuning gesture."""
    model = scale.observe_controllers()
    controllers = [model.get_item(index) for index in range(model.get_n_items())]
    for controller in controllers:
        if isinstance(controller, Gtk.GestureLongPress):
            scale.remove_controller(controller)


def _prevent_scroll_seeking(scale) -> None:
    """Consume scroll events before GtkRange can adjust the playback position."""
    controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    controller.connect("scroll", lambda *_args: True)
    scale.add_controller(controller)


def format_video_track_details(source) -> str:
    if not source.width or not source.height:
        return _("Source video")
    details = f"{source.width}x{source.height}"
    if source.frame_rate_num > 0 and source.frame_rate_den > 0:
        frame_rate = source.frame_rate_num / source.frame_rate_den
        frame_rate_text = f"{frame_rate:.2f}".rstrip("0").rstrip(".")
        details += f"@{frame_rate_text}"
    return details


def format_segment_label(number: int) -> str:
    """Return the localized label used for a numbered video segment."""
    return _("Segment %(number)d") % {"number": number}


def _ordered_segment_ids(project, segment_ids):
    requested = set(segment_ids)
    return tuple(
        segment.id
        for segment in project.segments
        if hasattr(segment, "id") and segment.id in requested
    )


def _selection_ids(window):
    selected = getattr(window, "selected_segment_ids", None)
    if selected is None:
        primary = getattr(window, "selected_segment_id", None)
        if primary is not None:
            return {primary}
        return {None} if window.project.segments else set()
    return set(_ordered_segment_ids(window.project, selected))


def _audio_target_ids(project, target):
    if isinstance(target, str):
        target = (target,)
    if not hasattr(project, "segments"):
        ordered = tuple(dict.fromkeys(target))
        if not ordered:
            raise ValueError("At least one segment must be selected")
        return ordered
    ordered = _ordered_segment_ids(project, target)
    if not ordered:
        raise ValueError("At least one segment must be selected")
    return ordered


def selection_title(project, selected_ids, primary_id=None):
    """Return a compact selection label that never enumerates segment numbers."""
    ordered = _ordered_segment_ids(project, selected_ids)
    if not ordered and primary_id is not None:
        return format_segment_label(project.segment_index(primary_id) + 1)
    if len(ordered) == 1:
        segment_id = primary_id if primary_id in ordered else ordered[0]
        return format_segment_label(project.segment_index(segment_id) + 1)
    if len(ordered) == len(project.segments):
        return _("All segments selected")
    return ngettext(
        "%(count)d segment selected", "%(count)d segments selected", len(ordered)
    ) % {"count": len(ordered)}


@dataclass(frozen=True)
class AudioSelectionState:
    muted: bool
    mute_mixed: bool


def audio_selection_state(segments, track_id):
    """Summarize one audio track without disguising mixed selected values."""
    settings = [segment.audio[track_id] for segment in segments]
    if not settings:
        raise ValueError("At least one segment must be selected")
    muted_values = [setting.muted for setting in settings]
    return AudioSelectionState(
        muted=muted_values[0],
        mute_mixed=any(
            muted != muted_values[0] for muted in muted_values[1:]
        ),
    )


def _new_track_mute_badge(label):
    """Create a mute badge that does not disturb track scrolling when clicked."""
    badge = Gtk.ToggleButton(label=label)
    # Refreshing the timeline replaces every track header. If a pointer click
    # gives this soon-to-be-removed widget focus, GTK moves focus to another
    # header and scrolls the shared vertical adjustment to reveal it.
    badge.set_focus_on_click(False)
    return badge


def _matches_base_key(keyval, keycode, *base_keyvals):
    """Match a physical key against its base-layout keyval when available."""
    display = Gdk.Display.get_default()
    if display is not None and keycode is not None and hasattr(display, "map_keycode"):
        keyvals = ()
        try:
            found, _keys, keyvals = display.map_keycode(keycode)
        except Exception:
            found = False
        if found:
            return any(candidate in base_keyvals for candidate in keyvals)
    return Gdk.keyval_to_lower(keyval) in base_keyvals


def _set_source_color(context, color, alpha=1.0):
    context.set_source_rgba(color.red, color.green, color.blue, color.alpha * alpha)


def _rounded_rectangle(context, x, y, width, height, radius):
    radius = max(0.0, min(radius, width / 2, height / 2))
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, math.pi * 1.5)
    context.close_path()


def _playhead_x(position_us, duration_us, width):
    """Return the single drawable x coordinate shared by the line and handle."""
    if width <= 1 or duration_us <= 0:
        return 0
    fraction = max(0.0, min(1.0, position_us / duration_us))
    return round(fraction * (width - 1))


def _viewport_playhead_x(position_us, duration_us, content_width, scroll_value):
    """Return the playhead coordinate in the non-scrolling viewport."""
    return _playhead_x(position_us, duration_us, content_width) - scroll_value


def _clamp_viewport_pointer_x(pointer_x, viewport_width):
    """Keep a horizontal drag coordinate inside the drawable viewport."""
    return max(0.0, min(max(0.0, viewport_width - 1), pointer_x))


def _draw_text(
    widget,
    context,
    text,
    x,
    y,
    *,
    size=10,
    weight=Pango.Weight.NORMAL,
    max_width=None,
):
    layout = widget.create_pango_layout(text)
    description = layout.get_context().get_font_description().copy()
    description.set_size(round(size * Pango.SCALE))
    description.set_weight(weight)
    layout.set_font_description(description)
    if max_width is not None:
        layout.set_width(max(1, round(max_width * Pango.SCALE)))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_single_paragraph_mode(True)
    context.move_to(x, y)
    PangoCairo.show_layout(context, layout)


def _text_pixel_width(widget, text, *, size=10, weight=Pango.Weight.NORMAL):
    """Measure one line of text using the same font settings as ``_draw_text``."""
    layout = widget.create_pango_layout(text)
    description = layout.get_context().get_font_description().copy()
    description.set_size(round(size * Pango.SCALE))
    description.set_weight(weight)
    layout.set_font_description(description)
    return layout.get_pixel_size()[0]


def _nice_tick_step(duration_seconds, width, target_spacing=92):
    """Choose a readable 1/2/5-based ruler interval for the current zoom."""
    target_ticks = max(1.0, width / target_spacing)
    raw_step = max(0.001, duration_seconds / target_ticks)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    if normalized <= 1:
        factor = 1
    elif normalized <= 2:
        factor = 2
    elif normalized <= 5:
        factor = 5
    else:
        factor = 10
    return factor * magnitude


def _format_ruler_time(seconds, tick_step):
    minutes = int(seconds // 60)
    seconds_in_minute = seconds - minutes * 60
    if tick_step < 1:
        return f"{minutes}:{seconds_in_minute:04.1f}"
    return f"{minutes}:{int(round(seconds_in_minute)):02d}"


def scale_value_after_drag(
    start_value,
    pointer_offset,
    travel,
    minimum,
    maximum,
    direction,
    snap_values=(),
):
    """Map a relative pointer drag to a scale value with magnetic marks."""
    if travel <= 0:
        return max(minimum, min(maximum, start_value))
    value = start_value + direction * pointer_offset * (maximum - minimum) / travel
    snap_radius = SCALE_SNAP_RADIUS_PX * (maximum - minimum) / travel
    available_snaps = [snap for snap in snap_values if minimum <= snap <= maximum]
    if available_snaps:
        closest_snap = min(available_snaps, key=lambda snap: abs(value - snap))
        if abs(value - closest_snap) <= snap_radius:
            value = closest_snap
    return max(minimum, min(maximum, value))


def peak_db_fraction(decibels):
    """Map a dBFS peak onto the meter's logarithmic -60 dB to 0 dB scale."""
    try:
        decibels = float(decibels)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(decibels) or decibels <= PEAK_METER_FLOOR_DB:
        return 0.0
    return min(1.0, decibels / -PEAK_METER_FLOOR_DB + 1.0)


def peak_meter_bar_geometry(width):
    """Return the width of each stereo bar and the gap between them."""
    bar_width = min(4.0, max(1.0, (float(width) - 2.0) / 2.0))
    return bar_width, max(1.0, float(width) - bar_width * 2)


def _ambient_glow_enabled(env=None):
    """Allow controlled A/B measurements without exposing a preference."""
    env = os.environ if env is None else env
    return str(env.get("CLIPPER_DISABLE_AMBIENT_GLOW", "")).strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def ambient_glow_bounds(width, height):
    """Return a uniformly enlarged sample rectangle around the video."""
    width = max(0.0, float(width))
    height = max(0.0, float(height))
    extent = min(AMBIENT_GLOW_MAX_EXTENT, width * AMBIENT_GLOW_HORIZONTAL_SCALE)
    scale = (width + extent * 2.0) / width if width else 1.0
    glow_height = height * scale
    return -extent, (height - glow_height) / 2.0, width + extent * 2.0, glow_height


def _ambient_glow_mask_stops():
    transparent = Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=0.0)
    opaque = Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=1.0)
    stops = []
    for offset, color in (
        (0.0, transparent),
        (0.2, opaque),
        (0.8, opaque),
        (1.0, transparent),
    ):
        stop = Gsk.ColorStop()
        stop.offset = offset
        stop.color = color
        stops.append(stop)
    return stops


class AmbientVideoGlow(Gtk.Widget):
    """Draw the current video texture once more as a soft side glow."""

    def __init__(self, picture, *, enabled=True):
        super().__init__()
        self._picture = picture
        self._enabled = bool(enabled)
        self._paintable = None
        self._paintable_handlers = []
        self._sample = None
        self._previous_sample = None
        self._sample_pending = False
        self._transition_started = 0.0
        self._transition_tick_id = None
        self._sample_timer_id = None
        self._picture_handler = picture.connect(
            "notify::paintable",
            self._on_picture_paintable_changed,
        )
        self.set_can_target(False)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.VISIBLE)
        self._set_paintable(picture.get_paintable())
        if self._enabled:
            self._sample_timer_id = GLib.timeout_add(
                AMBIENT_GLOW_SAMPLE_INTERVAL_MS,
                self._capture_sample,
            )

    def _on_picture_paintable_changed(self, picture, _property):
        self._set_paintable(picture.get_paintable())

    def _set_paintable(self, paintable):
        if paintable is self._paintable:
            return
        if self._paintable is not None:
            for handler_id in self._paintable_handlers:
                self._paintable.disconnect(handler_id)
        self._paintable = paintable
        self._paintable_handlers = []
        if paintable is not None:
            self._paintable_handlers = [
                paintable.connect("invalidate-contents", self._on_paintable_invalidated),
                paintable.connect("invalidate-size", self._on_paintable_size_invalidated),
            ]
        self._sample_pending = paintable is not None
        if paintable is None:
            self._sample = None
            self._previous_sample = None
        self.queue_draw()

    def _on_paintable_invalidated(self, _paintable):
        self._sample_pending = True

    def _on_paintable_size_invalidated(self, _paintable):
        self._sample_pending = True
        self.queue_resize()

    def _capture_sample(self):
        if not self._enabled or not self._sample_pending or self._paintable is None:
            return GLib.SOURCE_CONTINUE
        native = self.get_native()
        renderer = native.get_renderer() if native is not None else None
        if renderer is None:
            return GLib.SOURCE_CONTINUE

        current_image = self._paintable.get_current_image()
        intrinsic_width = current_image.get_intrinsic_width()
        intrinsic_height = current_image.get_intrinsic_height()
        aspect = (
            intrinsic_width / intrinsic_height
            if intrinsic_width > 0 and intrinsic_height > 0
            else max(1.0, self.get_width()) / max(1.0, self.get_height())
        )
        sample_width = AMBIENT_GLOW_SAMPLE_WIDTH
        sample_height = max(1, round(sample_width / aspect))
        sample_bounds = Graphene.Rect().init(
            0.0,
            0.0,
            float(sample_width),
            float(sample_height),
        )
        sample_snapshot = Gtk.Snapshot()
        current_image.snapshot(sample_snapshot, sample_width, sample_height)
        node = sample_snapshot.to_node()
        if node is None:
            return GLib.SOURCE_CONTINUE

        texture = renderer.render_texture(node, sample_bounds)
        self._sample_pending = False
        if self._sample is None:
            self._sample = texture
        else:
            self._previous_sample = self._sample
            self._sample = texture
            self._transition_started = time.monotonic()
            if self._transition_tick_id is None:
                self._transition_tick_id = self.add_tick_callback(
                    self._advance_transition
                )
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _advance_transition(self, _widget, _frame_clock):
        if self._transition_progress() >= 1.0:
            self._previous_sample = None
            self._transition_tick_id = None
            self.queue_draw()
            return GLib.SOURCE_REMOVE
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _transition_progress(self):
        if self._previous_sample is None:
            return 1.0
        elapsed_ms = (time.monotonic() - self._transition_started) * 1000.0
        return min(1.0, max(0.0, elapsed_ms / AMBIENT_GLOW_CROSSFADE_MS))

    @staticmethod
    def _append_sample(snapshot, texture, bounds):
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR, bounds)

    def do_snapshot(self, snapshot):
        texture = self._sample
        width = self.get_width()
        height = self.get_height()
        if not self._enabled or texture is None or width <= 0 or height <= 0:
            return

        x, y, glow_width, glow_height = ambient_glow_bounds(width, height)
        glow_bounds = Graphene.Rect().init(x, y, glow_width, glow_height)
        mask_bounds = Graphene.Rect().init(x, 0.0, glow_width, float(height))
        mask_start = Graphene.Point().init(x, 0.0)
        mask_end = Graphene.Point().init(x + glow_width, 0.0)

        snapshot.push_mask(Gsk.MaskMode.ALPHA)
        snapshot.append_linear_gradient(
            mask_bounds,
            mask_start,
            mask_end,
            _ambient_glow_mask_stops(),
        )
        snapshot.pop()
        snapshot.push_opacity(AMBIENT_GLOW_OPACITY)
        snapshot.push_blur(AMBIENT_GLOW_BLUR_RADIUS)
        if self._previous_sample is not None:
            snapshot.push_cross_fade(self._transition_progress())
            self._append_sample(snapshot, self._previous_sample, glow_bounds)
            snapshot.pop()
            self._append_sample(snapshot, texture, glow_bounds)
            snapshot.pop()
        else:
            self._append_sample(snapshot, texture, glow_bounds)
        snapshot.pop()
        snapshot.pop()
        snapshot.pop()

    def do_dispose(self):
        if self._sample_timer_id is not None:
            GLib.source_remove(self._sample_timer_id)
            self._sample_timer_id = None
        if self._transition_tick_id is not None:
            self.remove_tick_callback(self._transition_tick_id)
            self._transition_tick_id = None
        self._set_paintable(None)
        if self._picture is not None:
            self._picture.disconnect(self._picture_handler)
            self._picture = None
        Gtk.Widget.do_dispose(self)


class DeterministicScale(Gtk.Box):
    """Native GTK scale rendering with deterministic relative dragging."""

    def __init__(
        self,
        orientation,
        minimum,
        maximum,
        step,
        value,
        *,
        inverted=False,
        size_request=(-1, -1),
        increments=None,
        css_class=None,
        snap_values=(),
    ):
        super().__init__(orientation=orientation)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._drag_start_value = float(value)
        self._drag_travel = 1
        self._orientation = orientation
        self._direction = -1 if inverted else 1
        self._snap_values = tuple(snap_values)
        self._drag_begin_callbacks = []
        self._drag_end_callbacks = []

        self.scale = Gtk.Scale.new_with_range(
            orientation,
            minimum,
            maximum,
            step,
        )
        self.scale.set_size_request(*size_request)
        self.scale.set_halign(Gtk.Align.CENTER)
        self.scale.set_valign(Gtk.Align.CENTER)
        self.scale.set_inverted(inverted)
        self.scale.set_draw_value(False)
        self.scale.set_round_digits(0)
        if increments is not None:
            self.scale.set_increments(*increments)
        self.scale.set_value(value)
        if css_class:
            self.scale.add_css_class(css_class)
        self.scale.set_can_target(False)
        self.append(self.scale)

        drag = Gtk.GestureDrag.new()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.set_exclusive(True)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

    def connect_value_changed(self, callback, *args):
        return self.scale.connect("value-changed", callback, *args)

    def connect_drag_begin(self, callback, *args):
        self._drag_begin_callbacks.append((callback, args))

    def connect_drag_end(self, callback, *args):
        self._drag_end_callbacks.append((callback, args))

    def get_value(self):
        return self.scale.get_value()

    def set_value(self, value):
        self.scale.set_value(value)

    def set_range(self, minimum, maximum):
        self.scale.set_range(minimum, maximum)

    def clear_marks(self):
        self.scale.clear_marks()

    def add_mark(self, value, position, label):
        self.scale.add_mark(value, position, label)

    def _drag_begin(self, _gesture, _start_x, _start_y):
        self.scale.grab_focus()
        range_rect = self.scale.get_range_rect()
        self._drag_start_value = self.scale.get_value()
        if self._orientation == Gtk.Orientation.HORIZONTAL:
            self._drag_travel = max(1, range_rect.width - range_rect.height)
        else:
            self._drag_travel = max(1, range_rect.height - range_rect.width)
        for callback, args in self._drag_begin_callbacks:
            callback(self, *args)

    def _drag_update(self, _gesture, offset_x, offset_y):
        adjustment = self.scale.get_adjustment()
        pointer_offset = (
            offset_x
            if self._orientation == Gtk.Orientation.HORIZONTAL
            else offset_y
        )
        self.scale.set_value(
            scale_value_after_drag(
                self._drag_start_value,
                pointer_offset,
                self._drag_travel,
                adjustment.get_lower(),
                adjustment.get_upper(),
                self._direction,
                self._snap_values,
            )
        )

    def _drag_end(self, _gesture, _offset_x, _offset_y):
        self._drag_start_value = self.scale.get_value()
        for callback, args in self._drag_end_callbacks:
            callback(self, *args)


class StereoPeakMeter(Gtk.DrawingArea):
    """A narrow pair of zoned post-gain bars with peak-hold markers."""

    def __init__(self, range_widget=None):
        super().__init__()
        self._range_widget = range_widget
        self._levels = (0.0, 0.0)
        self._held_levels = (0.0, 0.0)
        self.set_size_request(PEAK_METER_WIDTH, PEAK_METER_HEIGHT)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)
        self.set_tooltip_text(_("Stereo peak level"))
        self.add_css_class("editor-peak-meter")
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Stereo peak meter")],
        )
        self.set_draw_func(self._draw)

    def set_peaks(self, peaks_db, held_peaks_db=None):
        levels = tuple(peak_db_fraction(value) for value in peaks_db[:2])
        if len(levels) == 1:
            levels *= 2
        elif not levels:
            levels = (0.0, 0.0)
        held_levels = tuple(
            peak_db_fraction(value)
            for value in (held_peaks_db if held_peaks_db is not None else peaks_db)[:2]
        )
        if len(held_levels) == 1:
            held_levels *= 2
        elif not held_levels:
            held_levels = (0.0, 0.0)
        if levels != self._levels or held_levels != self._held_levels:
            self._levels = levels
            self._held_levels = held_levels
            self.queue_draw()

    def reset(self):
        silent = (PEAK_METER_FLOOR_DB, PEAK_METER_FLOOR_DB)
        self.set_peaks(silent, silent)

    @staticmethod
    def _theme_color(area, name, fallback):
        found, color = area.get_style_context().lookup_color(name)
        if found:
            return color
        color = Gdk.RGBA()
        color.parse(fallback)
        return color

    def _track_bounds(self, height):
        """Align the visible bars with the slider's drawable range, not its widget."""
        if self._range_widget is not None:
            range_rect = self._range_widget.get_range_rect()
            if range_rect.height > 0:
                widget_height = self._range_widget.get_height()
                centered_offset = max(0.0, (height - widget_height) / 2)
                top = max(0.0, centered_offset + float(range_rect.y))
                bottom = min(float(height), top + float(range_rect.height))
                if bottom > top:
                    return top, bottom - top
        top = min(PEAK_METER_VERTICAL_MARGIN, height / 2)
        return top, max(1.0, height - top * 2)

    def _draw(self, area, context, width, height):
        bar_width, gap = peak_meter_bar_geometry(width)
        top, track_height = self._track_bounds(height)
        yellow = peak_db_fraction(PEAK_METER_YELLOW_DB)
        red = peak_db_fraction(PEAK_METER_RED_DB)
        zones = (
            (0.0, yellow, self._theme_color(area, "success_color", "#2ec27e")),
            (yellow, red, self._theme_color(area, "warning_color", "#f5c211")),
            (red, 1.0, self._theme_color(area, "error_color", "#e01b24")),
        )
        white = Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=1.0)
        for index, level in enumerate(self._levels):
            x = index * (bar_width + gap)
            for lower, upper, color in zones:
                zone_top = top + track_height * (1.0 - upper)
                zone_height = track_height * (upper - lower)
                context.rectangle(x, zone_top, bar_width, zone_height)
                _set_source_color(context, color, 0.16)
                context.fill()
                active_upper = min(level, upper)
                if active_upper <= lower:
                    continue
                active_height = track_height * (active_upper - lower)
                context.rectangle(
                    x,
                    top + track_height * (1.0 - active_upper),
                    bar_width,
                    active_height,
                )
                _set_source_color(context, color)
                context.fill()
            held_level = self._held_levels[index]
            if held_level > 0:
                marker_y = top + track_height * (1.0 - held_level)
                context.rectangle(x, marker_y - 0.75, bar_width, 1.5)
                _set_source_color(context, white)
                context.fill()


class EditorWindow(Adw.ApplicationWindow):
    """A compact editor surface; project operations remain GTK-independent."""

    def __init__(self, application, project, config, finished_callback, history=None):
        super().__init__(application=application, title=f"Edit {Path(project.source.path).name}")
        self.history = history if history is not None else EditorHistory(project)
        project = self.history.project
        self.config = config
        self.finished_callback = finished_callback
        self.selected_segment_id: str = project.segments[0].id
        self.selected_segment_ids = {self.selected_segment_id}
        self._selection_anchor_id = self.selected_segment_id
        self._syncing = False
        self._cleaned = False
        self._waveform_cancel = threading.Event()
        self._waveform_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="clipper-waveform"
        )
        self._waveforms = {}
        self._peak_meters = {}
        self._source_invalid = False
        self._exporter = None
        self._export_dialog = None
        self._export_options = None
        self._close_cancelled_callback = None
        self._ui_ready = False
        self._viewport_refresh_pending = False
        self._refresh_after_scrub = False
        self._scrubbing = False
        self._scrub_seek_id = None
        self._pending_scrub_preview_us = None
        self._gain_drag_key = None
        self._gain_lane_drag = None
        self._gain_lane_click_suppressed = False
        self._gain_feedback = {}
        self._gain_redraw_id = None
        self._pending_gain_redraw_track_id = None
        self._zoom_dragging = False
        self._zoom_discrete_active = False
        self._zoom_update_id = None
        self._pending_zoom_percent = None
        self._zoom_settle_id = None
        self._waveform_restore_id = None
        self._pending_waveform_restore_lanes = []
        self._edge_scroll_id = None
        self._edge_scroll_speed = 0.0
        self._edge_scroll_area = None
        self._edge_scroll_pointer_x = 0.0
        self._playback_follow_active = False
        self._playback_follow_suspended = False
        self._playback_follow_visible_x = None
        self._setting_timeline_scroll = False
        self._space_pressed = False
        self._pending_zoom_anchor = None
        self._pending_zoom_fraction = None
        self._next_zoom_anchor = None
        self._pending_scroll_value = None
        self._timeline_pointer_x = None
        self._zoom_anchor_timeout_id = None
        self._zoom_anchor_attempts = 0
        self._updating_volume = False
        self._volume_before_mute = None
        self._updating_transport_seek = False
        self._transport_seek_dragging = False
        _install_editor_css()
        self.set_default_size(1080, 768)
        self.set_size_request(760, 560)
        from window_state import WindowSizeManager

        self._window_size = WindowSizeManager(
            self,
            self.config,
            "editor",
            minimum_size=(760, 560),
        )
        self._build_ui()
        self._monitor_source()
        self._install_actions()
        self._ui_ready = True
        self.connect("notify::fullscreened", self._on_fullscreen_changed)
        self.connect("close-request", self._on_close_request)
        self._refresh()
        self._start_waveforms()

    @property
    def project(self):
        return self.history.project

    @property
    def dirty(self):
        return self.history.dirty

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._root = root
        root.add_css_class("editor-workspace")

        header = Adw.HeaderBar()
        self._header = header
        self.title_label = Adw.WindowTitle(
            title=Path(self.project.source.path).name,
            subtitle=_("Video editor"),
        )
        header.set_title_widget(self.title_label)

        self.export_button = Gtk.Button()
        export_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        export_content.set_halign(Gtk.Align.CENTER)
        export_content.append(TintedIcon(EXPORT_LINEAR))
        self.export_button_label = Gtk.Label(label=_("Export"))
        export_content.append(self.export_button_label)
        self.export_button.set_child(export_content)
        self.export_button.add_css_class("suggested-action")
        self.export_button.connect("clicked", self._show_export)
        header.pack_end(self.export_button)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_icon_name(HAMBURGER_MENU)
        self.menu_button.set_tooltip_text(_("Editor menu"))
        menu = create_shortcuts_menu(
            ((_('Wipe all edits'), "win.wipe-all-edits"),)
        )
        self.menu_button.set_menu_model(menu)
        header.pack_end(self.menu_button)
        root.append(header)

        self.preview = EditorPreview(
            self.project,
            self._on_preview_position,
            self._on_preview_state_changed,
            self._on_audio_peaks,
        )
        workspace = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._workspace = workspace
        workspace.add_css_class("editor-workspace-paned")
        workspace.set_vexpand(True)
        workspace.set_position(390)
        workspace.set_resize_start_child(True)
        workspace.set_shrink_start_child(False)
        workspace.set_resize_end_child(True)
        workspace.set_shrink_end_child(False)

        monitor_area = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._monitor_area = monitor_area
        monitor_area.set_size_request(-1, 260)
        monitor_area.add_css_class("editor-monitor-area")
        monitor_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        monitor_column.set_hexpand(True)
        monitor_column.set_halign(Gtk.Align.FILL)
        ratio = (
            self.project.source.width / self.project.source.height
            if self.project.source.height
            else 16 / 9
        )
        picture_frame = Gtk.AspectFrame(xalign=0.5, yalign=0.5, ratio=ratio, obey_child=False)
        self._picture_frame = picture_frame
        picture_frame.set_hexpand(True)
        picture_frame.set_vexpand(True)
        picture_frame.set_margin_start(12)
        picture_frame.set_margin_end(12)
        picture_frame.set_margin_top(12)
        picture_frame.set_margin_bottom(12)
        # The ambient layer intentionally extends beyond the video image, but
        # must remain inside this black monitor surface.
        picture_frame.set_overflow(Gtk.Overflow.HIDDEN)
        picture_frame.add_css_class("editor-monitor-frame")
        self.preview.picture.set_hexpand(True)
        self.preview.picture.set_vexpand(True)

        picture_overlay = Gtk.Overlay()
        picture_overlay.set_child(self.preview.picture)
        picture_overlay.set_margin_start(1)
        picture_overlay.set_margin_end(1)
        picture_overlay.set_margin_top(1)
        picture_overlay.set_margin_bottom(1)
        picture_overlay.add_overlay(self.preview.loading_spinner)

        ambient_layer = AmbientVideoGlow(
            self.preview.picture,
            enabled=_ambient_glow_enabled(),
        )
        ambient_overlay = Gtk.Overlay()
        ambient_overlay.set_child(ambient_layer)
        ambient_overlay.add_overlay(picture_overlay)

        self.transport_revealer = Gtk.Revealer()
        self._controls_revealer = self.transport_revealer
        self.transport_revealer.set_transition_type(
            Gtk.RevealerTransitionType.CROSSFADE
        )
        self.transport_revealer.set_transition_duration(180)
        self.transport_revealer.set_reveal_child(False)
        self.transport_revealer.set_halign(Gtk.Align.FILL)
        self.transport_revealer.set_valign(Gtk.Align.END)
        self.transport_revealer.set_margin_start(16)
        self.transport_revealer.set_margin_end(16)
        self.transport_revealer.set_margin_bottom(16)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._controls = controls
        controls.add_css_class("clipper-player-controls")
        controls.add_css_class("osd")

        self._play_button = Gtk.Button.new_from_icon_name(PLAY)
        self.playback_button = self._play_button
        self._play_button.set_tooltip_text(_("Play"))
        self._play_button.add_css_class("circular")
        self._play_button.add_css_class("flat")
        self._play_button.add_css_class("clipper-player-control-button")
        self._play_button.connect("clicked", self._toggle_playback)
        controls.append(self._play_button)

        self._time_label = Gtk.Label(
            label=(
                f"{format_transport_time(0)} / "
                f"{format_transport_time(self.project.output_duration_us)}"
            )
        )
        self.time_label = self._time_label
        self.time_label.set_halign(Gtk.Align.START)
        self.time_label.set_valign(Gtk.Align.CENTER)
        self.time_label.set_xalign(0)
        self.time_label.add_css_class("clipper-player-time")
        controls.append(self.time_label)

        self._seek_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            0,
            1000,
            1,
        )
        self._seek_scale.set_draw_value(False)
        self._seek_scale.set_hexpand(True)
        self._seek_scale.set_valign(Gtk.Align.CENTER)
        self._seek_scale.set_sensitive(self.project.output_duration_us > 0)
        self._seek_scale.set_visible(False)
        self._seek_scale.add_css_class("clipper-player-timeline")
        self._seek_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Playback position')],
        )
        self._seek_scale.connect("change-value", self._on_seek_change_value)
        _disable_hold_to_fine_tune(self._seek_scale)
        _prevent_scroll_seeking(self._seek_scale)
        self._seek_scale.connect(
            "notify::css-classes",
            self._on_seek_css_classes_changed,
        )
        controls.append(self._seek_scale)

        control_spacer = Gtk.Box()
        self._control_spacer = control_spacer
        control_spacer.set_hexpand(True)
        controls.append(control_spacer)

        self._volume_control = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        self._volume_control.set_valign(Gtk.Align.CENTER)

        self._volume_revealer = Gtk.Revealer()
        self._volume_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_LEFT
        )
        self._volume_revealer.set_transition_duration(160)
        self._volume_revealer.set_reveal_child(False)

        self._volume_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            0,
            1,
            0.01,
        )
        self._volume_scale.set_value(1)
        self._volume_scale.set_draw_value(False)
        self._volume_scale.set_valign(Gtk.Align.CENTER)
        self._volume_scale.add_css_class("clipper-player-volume")
        self._volume_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Volume')],
        )
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        _disable_hold_to_fine_tune(self._volume_scale)
        self._volume_revealer.set_child(self._volume_scale)
        self._volume_control.append(self._volume_revealer)

        self._mute_button = Gtk.Button.new_from_icon_name(VOLUME)
        self._mute_button.set_tooltip_text(_("Mute"))
        self._mute_button.add_css_class("circular")
        self._mute_button.add_css_class("flat")
        self._mute_button.add_css_class("clipper-player-control-button")
        self._mute_button.connect("clicked", self._toggle_muted)
        mute_button_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        mute_button_scroll.connect("scroll", self._on_mute_button_scroll)
        self._mute_button.add_controller(mute_button_scroll)
        self._volume_control.append(self._mute_button)

        volume_hover = Gtk.EventControllerMotion()
        volume_hover.connect("enter", self._on_volume_control_enter)
        volume_hover.connect("leave", self._on_volume_control_leave)
        self._volume_control.add_controller(volume_hover)

        volume_focus = Gtk.EventControllerFocus()
        volume_focus.connect("enter", self._on_volume_control_enter)
        volume_focus.connect("leave", self._on_volume_control_leave)
        self._volume_control.add_controller(volume_focus)
        controls.append(self._volume_control)

        self._fullscreen_button = Gtk.Button.new_from_icon_name(FULLSCREEN)
        self._fullscreen_button.set_tooltip_text(_("Fullscreen"))
        self._fullscreen_button.add_css_class("circular")
        self._fullscreen_button.add_css_class("flat")
        self._fullscreen_button.add_css_class("clipper-player-control-button")
        self._fullscreen_button.connect(
            "clicked", lambda _button: self._toggle_fullscreen()
        )
        controls.append(self._fullscreen_button)

        self.transport_revealer.set_child(controls)
        picture_overlay.add_overlay(self.transport_revealer)

        monitor_hover = Gtk.EventControllerMotion()
        monitor_hover.connect("enter", self._on_monitor_hover_enter)
        monitor_hover.connect("leave", self._on_monitor_hover_leave)
        picture_overlay.add_controller(monitor_hover)

        video_click = Gtk.GestureClick()
        video_click.set_button(Gdk.BUTTON_PRIMARY)
        video_click.connect("pressed", self._on_video_pressed)
        self.preview.picture.add_controller(video_click)

        picture_frame.set_child(ambient_overlay)
        monitor_column.append(picture_frame)
        monitor_area.append(monitor_column)
        workspace.set_start_child(monitor_area)

        timeline_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._timeline_panel = timeline_panel
        timeline_panel.set_size_request(-1, 220)
        timeline_panel.add_css_class("editor-timeline-panel")

        timeline_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        timeline_content.set_margin_start(12)
        timeline_content.set_margin_end(12)
        timeline_content.set_margin_top(6)
        timeline_content.set_margin_bottom(12)
        timeline_content.set_vexpand(True)

        timeline_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        timeline_toolbar.add_css_class("editor-timeline-toolbar")

        timeline_heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        timeline_heading.set_valign(Gtk.Align.CENTER)
        timeline_title = Gtk.Label(label=_("Timeline"), xalign=0)
        timeline_title.add_css_class("heading")
        timeline_heading.append(timeline_title)
        self.timeline_summary = Gtk.Label(xalign=0)
        self.timeline_summary.add_css_class("caption")
        self.timeline_summary.add_css_class("dim-label")
        timeline_heading.append(self.timeline_summary)
        timeline_toolbar.append(timeline_heading)

        toolbar_separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        toolbar_separator.add_css_class("editor-toolbar-separator")
        timeline_toolbar.append(toolbar_separator)

        history_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        history_actions.add_css_class("linked")
        history_actions.add_css_class("editor-history-actions")
        undo = Gtk.Button.new_from_icon_name(UNDO)
        undo.set_action_name("win.undo")
        undo.set_tooltip_text(_("Undo (Ctrl+Z)"))
        history_actions.append(undo)
        redo = Gtk.Button.new_from_icon_name(REDO)
        redo.set_action_name("win.redo")
        redo.set_tooltip_text(_("Redo (Ctrl+Shift+Z)"))
        history_actions.append(redo)
        timeline_toolbar.append(history_actions)

        edit_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        edit_actions.add_css_class("linked")
        edit_actions.add_css_class("editor-edit-actions")
        self.split_button = self._captioned_tool_button(
            SCISSORS_OUTLINE,
            _("Split"),
            _("Split at playhead (X)"),
        )
        self.split_button.connect("clicked", self._split)
        edit_actions.append(self.split_button)
        self.delete_button = self._captioned_tool_button(
            TRASH,
            _("Delete"),
            _("Delete selected segments (Del)"),
        )
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._delete)
        edit_actions.append(self.delete_button)
        timeline_toolbar.append(edit_actions)

        toolbar_spacer = Gtk.Box()
        toolbar_spacer.set_hexpand(True)
        timeline_toolbar.append(toolbar_spacer)

        zoom_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        zoom_controls.set_valign(Gtk.Align.CENTER)
        zoom_controls.add_css_class("editor-zoom-controls")
        zoom_out_button = Gtk.Button.new_from_icon_name(ZOOM_OUT)
        zoom_out_button.add_css_class("editor-zoom-button")
        zoom_out_button.set_tooltip_text(_("Zoom out"))
        zoom_out_button.connect(
            "clicked", lambda _button: self._change_timeline_zoom(-1)
        )
        zoom_controls.append(zoom_out_button)
        self.zoom_scale = DeterministicScale(
            Gtk.Orientation.HORIZONTAL,
            10,
            300,
            1,
            100,
            size_request=(120, -1),
            increments=(1, 25),
            css_class="editor-zoom-scale",
            snap_values=(100,),
        )
        self._set_zoom_scale_range(10)
        self.zoom_scale.set_tooltip_text(_("Zoom 100%"))
        self.zoom_scale.connect_value_changed(self._on_zoom_changed)
        self.zoom_scale.connect_drag_begin(self._zoom_drag_begin)
        self.zoom_scale.connect_drag_end(self._zoom_drag_end)
        zoom_controls.append(self.zoom_scale)
        zoom_in_button = Gtk.Button.new_from_icon_name(ZOOM_IN)
        zoom_in_button.add_css_class("editor-zoom-button")
        zoom_in_button.set_tooltip_text(_("Zoom in"))
        zoom_in_button.connect(
            "clicked", lambda _button: self._change_timeline_zoom(1)
        )
        zoom_controls.append(zoom_in_button)
        self.zoom_percentage = Gtk.Label(label="100%")
        self.zoom_percentage.set_width_chars(4)
        self.zoom_percentage.set_xalign(1)
        self.zoom_percentage.add_css_class("monospace")
        zoom_controls.append(self.zoom_percentage)
        timeline_toolbar.append(zoom_controls)
        timeline_content.append(timeline_toolbar)

        tracks = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tracks.set_vexpand(True)
        tracks.set_overflow(Gtk.Overflow.HIDDEN)
        tracks.add_css_class("editor-track-card")
        adjustment = Gtk.Adjustment()
        headers_scroll = Gtk.ScrolledWindow(vadjustment=adjustment)
        headers_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        headers_scroll.set_size_request(TRACK_HEADER_WIDTH, -1)
        headers_scroll.set_min_content_width(TRACK_HEADER_WIDTH)
        headers_scroll.set_max_content_width(TRACK_HEADER_WIDTH)
        headers_scroll.set_hexpand(False)
        headers_scroll.add_css_class("editor-track-headers")
        self.track_headers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.track_headers.set_size_request(TRACK_HEADER_WIDTH, -1)
        headers_scroll.set_child(self.track_headers)
        tracks.append(headers_scroll)
        self.timeline_scroll = Gtk.ScrolledWindow(vadjustment=adjustment)
        self.timeline_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.timeline_scroll.set_hexpand(True)
        self.timeline_scroll.set_vexpand(True)
        self.timeline_scroll.connect("notify::width", self._on_timeline_viewport_changed)
        timeline_scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        timeline_scroll_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        timeline_scroll_controller.connect("scroll", self._on_timeline_scroll)
        self.timeline_scroll.add_controller(timeline_scroll_controller)
        pointer_motion = Gtk.EventControllerMotion()
        pointer_motion.connect("motion", self._on_timeline_pointer_motion)
        pointer_motion.connect("leave", self._on_timeline_pointer_leave)
        self.timeline_scroll.add_controller(pointer_motion)
        self.timeline_scroll.get_hadjustment().connect(
            "value-changed",
            self._on_timeline_scroll_changed,
        )
        self.timeline_view = Gtk.Overlay()
        self.timeline_view.set_hexpand(True)
        self.timeline_view.set_vexpand(True)
        self.timeline_view.set_child(self.timeline_scroll)
        self.timeline_overlay = Gtk.Overlay()
        self.timeline_overlay.set_halign(Gtk.Align.START)
        self.timeline_overlay.set_hexpand(False)
        self.timeline_lanes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.timeline_lanes.set_halign(Gtk.Align.START)
        self.timeline_lanes.set_hexpand(False)
        self.timeline_overlay.set_child(self.timeline_lanes)
        self.playhead_overlay = Gtk.DrawingArea()
        self.playhead_overlay.set_halign(Gtk.Align.FILL)
        self.playhead_overlay.set_valign(Gtk.Align.FILL)
        self.playhead_overlay.set_hexpand(True)
        self.playhead_overlay.set_vexpand(True)
        self.playhead_overlay.set_can_target(False)
        self.playhead_overlay.add_css_class("editor-playhead")
        self.playhead_overlay.set_draw_func(self._draw_playhead)
        self.timeline_scroll.set_child(self.timeline_overlay)
        self.timeline_view.add_overlay(self.playhead_overlay)
        tracks.append(self.timeline_view)
        timeline_content.append(tracks)
        timeline_panel.append(timeline_content)
        workspace.set_end_child(timeline_panel)
        root.append(workspace)
        self._zoom = 1.0
        self._playhead_us = 0
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        key_controller.connect("key-released", self._on_key_released)
        self.add_controller(key_controller)

    @staticmethod
    def _captioned_tool_button(icon_name, caption, tooltip):
        button = Gtk.Button()
        button.set_hexpand(False)
        button.set_tooltip_text(tooltip)
        button.add_css_class("editor-tool-button")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_halign(Gtk.Align.CENTER)
        content.append(Gtk.Image.new_from_icon_name(icon_name))
        label = Gtk.Label(label=caption)
        content.append(label)
        button.set_child(content)
        return button

    def _on_monitor_hover_enter(self, *_args):
        self.transport_revealer.set_reveal_child(True)

    def _on_monitor_hover_leave(self, *_args):
        self.transport_revealer.set_reveal_child(False)

    def _on_video_pressed(self, _gesture, press_count, _x, _y):
        if press_count == 1:
            self._toggle_playback()
        elif press_count == 2:
            # GTK reports a double-click as press counts 1 and 2. Toggle on
            # both so fullscreen preserves the playback state from before the
            # gesture without delaying the first click.
            self._toggle_playback()
            self._toggle_fullscreen()

    def _update_transport_time(self, output_us):
        self.time_label.set_text(
            f"{format_transport_time(output_us)} / "
            f"{format_transport_time(self.project.output_duration_us)}"
        )

    def _update_transport_seek(self, output_us):
        seek_scale = getattr(self, "_seek_scale", None)
        if seek_scale is None or getattr(self, "_transport_seek_dragging", False):
            return
        duration_us = self.project.output_duration_us
        fraction = output_us / duration_us if duration_us > 0 else 0
        self._updating_transport_seek = True
        try:
            seek_scale.set_value(max(0, min(1000, fraction * 1000)))
        finally:
            self._updating_transport_seek = False

    def _on_seek_change_value(self, _scale, _scroll, value):
        duration_us = self.project.output_duration_us
        if self._updating_transport_seek or self._source_invalid or duration_us <= 0:
            return False
        output_us = round(duration_us * max(0, min(1000, value)) / 1000)
        self._playhead_us = output_us
        self._update_transport_time(output_us)
        self._update_playhead()
        if self._transport_seek_dragging:
            self._queue_scrub_preview(output_us)
        else:
            self.preview.seek_output(output_us)
            self.preview.finish_seek()
            self._playback_follow_active = False
        return False

    def _on_seek_css_classes_changed(self, scale, _property):
        dragging = scale.has_css_class("dragging")
        if dragging == self._transport_seek_dragging:
            return
        self._transport_seek_dragging = dragging
        if dragging:
            self._scrubbing = True
        else:
            self._finish_scrub()

    def _on_volume_control_enter(self, *_args):
        self._volume_revealer.set_reveal_child(True)

    def _on_volume_control_leave(self, *_args):
        self._volume_revealer.set_reveal_child(False)

    def _on_volume_changed(self, scale):
        if self._updating_volume:
            return
        volume = max(0.0, min(1.0, float(scale.get_value())))
        self._volume_before_mute = None
        self.preview.set_volume(volume)
        self._set_volume_ui(volume)

    def _set_volume(self, volume):
        volume = max(0.0, min(1.0, float(volume)))
        self._updating_volume = True
        try:
            self._volume_scale.set_value(volume)
        finally:
            self._updating_volume = False
        self.preview.set_volume(volume)
        self._set_volume_ui(volume)

    def _toggle_muted(self, _button=None):
        volume = max(0.0, min(1.0, float(self._volume_scale.get_value())))
        if volume == 0:
            restore_volume = self._volume_before_mute or 1.0
            self._volume_before_mute = None
            self._set_volume(restore_volume)
            return
        self._volume_before_mute = volume
        self._set_volume(0)

    def _change_volume(self, direction):
        volume = self._volume_scale.get_value()
        self._volume_scale.set_value(
            max(0.0, min(1.0, round(volume + direction * _VOLUME_STEP, 2)))
        )

    def _on_mute_button_scroll(self, _controller, _dx, dy):
        if dy:
            volume = self._volume_scale.get_value()
            direction = 1 if dy < 0 else -1
            self._volume_scale.set_value(
                max(0.0, min(1.0, round(volume + direction * _VOLUME_SCROLL_STEP, 2)))
            )
        return True

    def _set_volume_ui(self, volume):
        muted = volume <= 0
        if muted:
            icon_name = VOLUME_CROSS
        elif volume <= 2 / 3:
            icon_name = VOLUME_MEDIUM
        else:
            icon_name = VOLUME
        self._mute_button.set_icon_name(icon_name)
        self._mute_button.set_tooltip_text(_("Unmute") if muted else _("Mute"))

    def _toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_args):
        fullscreen = self.is_fullscreen()
        self._header.set_visible(not fullscreen)
        self._timeline_panel.set_visible(not fullscreen)
        self._seek_scale.set_visible(fullscreen)
        self._control_spacer.set_visible(not fullscreen)
        if fullscreen:
            self._root.add_css_class("editor-video-fullscreen")
        else:
            self._root.remove_css_class("editor-video-fullscreen")
        margin = 0 if fullscreen else 12
        self._picture_frame.set_margin_start(margin)
        self._picture_frame.set_margin_end(margin)
        self._picture_frame.set_margin_top(margin)
        self._picture_frame.set_margin_bottom(margin)
        self._fullscreen_button.set_icon_name(
            QUIT_FULLSCREEN if fullscreen else FULLSCREEN
        )
        self._fullscreen_button.set_tooltip_text(
            _("Exit fullscreen") if fullscreen else _("Fullscreen")
        )

    @staticmethod
    def _style_save_dialog(dialog):
        """Apply the success palette to the lazily-created save response."""
        pending = [dialog.get_first_child()]
        while pending:
            widget = pending.pop()
            if widget is None:
                continue
            if isinstance(widget, Gtk.Button) and widget.get_label() == _("Save draft"):
                widget.add_css_class("success")
                return
            child = widget.get_first_child()
            while child:
                pending.append(child)
                child = child.get_next_sibling()

    def _install_actions(self):
        for name, callback, accelerators in (
            ("undo", self._undo, ["<Control>z"]),
            ("redo", self._redo, ["<Control><Shift>z"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.get_application().set_accels_for_action(f"win.{name}", accelerators)
        self.undo_action = self.lookup_action("undo")
        self.redo_action = self.lookup_action("redo")
        self.wipe_edits_action = Gio.SimpleAction.new("wipe-all-edits", None)
        self.wipe_edits_action.connect("activate", self._confirm_wipe_edits)
        self.add_action(self.wipe_edits_action)
        shortcuts_action = Gio.SimpleAction.new("shortcuts", None)
        shortcuts_action.connect("activate", self._show_shortcuts)
        self.add_action(shortcuts_action)

    def _show_shortcuts(self, *_args):
        dialog = create_shortcuts_dialog(EDITOR_SHORTCUT_SECTIONS)
        self._shortcuts_dialog = dialog
        dialog.present(self)

    def _monitor_source(self):
        try:
            source_file = Gio.File.new_for_path(self.project.source.path)
            self._source_monitor = source_file.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._source_monitor.connect("changed", self._on_source_changed)
        except Exception:
            self._source_monitor = None

    def _on_source_changed(self, *_args):
        try:
            stat = Path(self.project.source.path).stat()
            valid = (
                stat.st_size == self.project.source.size
                and stat.st_mtime_ns == self.project.source.mtime_ns
            )
        except OSError:
            valid = False
        if not valid:
            self._source_invalid = True
            self.preview.invalidate_source()
            self.playback_button.set_sensitive(False)
            self._seek_scale.set_sensitive(False)
            self._waveform_cancel.set()
            self.export_button.set_sensitive(False)
            self._show_toast(_("Source clip changed or is unavailable"))

    @staticmethod
    def _clear_box(box):
        child = box.get_first_child()
        while child:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    def _normalize_selection(self):
        selected_ids = _selection_ids(self)
        if not selected_ids:
            selected_ids = {self.project.segments[0].id}
        ordered = _ordered_segment_ids(self.project, selected_ids)
        primary = getattr(self, "selected_segment_id", None)
        if not isinstance(primary, str) or primary not in selected_ids:
            primary = ordered[0]
        self.selected_segment_ids = set(ordered)
        self.selected_segment_id = primary
        return ordered

    def _set_selection(self, segment_ids, primary_id: str | None = None):
        ordered = _ordered_segment_ids(self.project, segment_ids)
        if not ordered and self.project.segments and not any(
            hasattr(segment, "id") for segment in self.project.segments
        ):
            ordered = tuple(dict.fromkeys(segment_ids))
        if not ordered:
            raise ValueError("At least one segment must remain selected")
        self.selected_segment_ids = set(ordered)
        self.selected_segment_id = (
            primary_id if primary_id in self.selected_segment_ids else ordered[0]
        )

    def _refresh(self, update_preview=True):
        self._syncing = True
        if (
            self._ui_ready
            and self._pending_zoom_anchor is None
            and self._pending_scroll_value is None
        ):
            self._pending_scroll_value = self.timeline_scroll.get_hadjustment().get_value()
        self.undo_action.set_enabled(self.history.can_undo)
        self.redo_action.set_enabled(self.history.can_redo)
        self._update_transport_time(self._playhead_us)
        EditorWindow._update_transport_seek(self, self._playhead_us)
        selected_ids = self._normalize_selection()
        selected_index = self.project.segment_index(self.selected_segment_id)
        selected_segments = [
            segment for segment in self.project.segments if segment.id in selected_ids
        ]
        self._clear_box(self.track_headers)
        self._clear_box(self.timeline_lanes)
        self._audio_lanes = {}
        self._gain_feedback = {}
        self._peak_meters = {}
        viewport_width = self._timeline_viewport_width()
        self._last_timeline_viewport_width = viewport_width
        width, self._zoom = timeline_content_width(
            self._zoom,
            self.project.output_duration_us,
            viewport_width,
        )
        self._timeline_clip_width = timeline_clip_width(
            self._zoom,
            self.project.output_duration_us,
        )
        self._requested_timeline_width = width
        self._resize_timeline_lanes(width)
        self._set_zoom_scale_range(10)
        self.zoom_scale.set_value(round(self._zoom * 100))
        self.zoom_percentage.set_text(
            _("%(percentage)d%%") % {"percentage": round(self._zoom * 100)}
        )
        self._timeline_segment_label = self._append_timeline_row(
            None,
            selection_title(
                self.project,
                selected_ids,
                self.selected_segment_id,
            ),
            None,
            RULER_HEIGHT,
            self._draw_ruler,
            width,
            "scrub",
            header_class="editor-ruler-header",
            lane_class="editor-ruler-lane",
        )
        self._update_timeline_labels(selected_index)
        source_dimensions = format_video_track_details(self.project.source)
        self._append_timeline_row(
            "V1",
            _("Video"),
            source_dimensions,
            VIDEO_TRACK_HEIGHT,
            self._draw_video_lane,
            width,
            "select",
            lane_class="editor-video-lane",
        )
        for track in self.project.source.audio_tracks:
            audio_state = audio_selection_state(selected_segments, track.id)
            target_ids = tuple(selected_ids)
            selection_count = len(target_ids)
            selection_target = (
                _("segment %(number)d") % {"number": selected_index + 1}
                if selection_count == 1
                else ngettext(
                    "%(count)d selected segment",
                    "%(count)d selected segments",
                    selection_count,
                )
                % {"count": selection_count}
            )
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            header.set_size_request(
                TRACK_HEADER_WIDTH,
                AUDIO_TRACK_HEIGHT + TRACK_ROW_BORDER,
            )
            header.add_css_class("editor-track-header")
            badge_label = f"A{track.ordinal + 1}"
            badge = _new_track_mute_badge(badge_label)
            badge.set_valign(Gtk.Align.CENTER)
            badge.set_active(
                audio_state.muted if not audio_state.mute_mixed else False
            )
            badge.add_css_class("caption")
            badge.add_css_class("editor-track-badge")
            if audio_state.mute_mixed:
                badge.add_css_class("mixed")
                mute_tooltip = MIXED_MUTE_TOOLTIP
                mute_accessible_label = mute_tooltip
            else:
                action = _("Unmute") if audio_state.muted else _("Mute")
                mute_tooltip = _("%(action)s %(track)s for %(selection)s") % {
                    "action": action,
                    "track": track.label,
                    "selection": selection_target,
                }
                mute_accessible_label = _("%(action)s %(track)s") % {
                    "action": action,
                    "track": track.label,
                }
            badge.set_tooltip_text(mute_tooltip)
            badge.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [mute_accessible_label],
            )
            badge.connect("toggled", self._mute, target_ids, track.id)
            header.append(badge)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            text.set_hexpand(True)
            text.set_valign(Gtk.Align.CENTER)
            title = Gtk.Label(
                label=_("Audio %(number)d") % {"number": track.ordinal + 1},
                xalign=0,
            )
            title.add_css_class("heading")
            text.append(title)
            name = Gtk.Label(
                label=track.label,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            name.set_max_width_chars(18)
            name.add_css_class("caption")
            name.add_css_class("dim-label")
            name.set_tooltip_text(track.label)
            text.append(name)
            header.append(text)
            meter = StereoPeakMeter()
            self._peak_meters[track.id] = meter
            header.append(meter)
            self.track_headers.append(header)
            lane = self._new_lane(
                AUDIO_TRACK_HEIGHT,
                self._draw_waveform,
                width,
                track.id,
                "audio_gain",
                "editor-audio-lane",
                f"Audio {track.ordinal + 1} track timeline",
            )
            self._audio_lanes[track.id] = lane
            self.timeline_lanes.append(lane)
        if not self.project.source.audio_tracks:
            self._append_timeline_row(
                "A1",
                _("Audio"),
                _("No audio tracks"),
                AUDIO_TRACK_HEIGHT,
                self._draw_empty_audio,
                width,
                "select",
                lane_class="editor-empty-audio-lane",
            )
        if update_preview:
            self.preview.update_project(self.project)
        self._update_playhead()
        GLib.idle_add(self._update_playhead)
        self._schedule_zoom_anchor()
        self._syncing = False

    def _update_timeline_labels(self, selected_index=None):
        """Refresh the labels that describe the current timeline selection."""
        if selected_index is None:
            selected_index = self.project.segment_index(self.selected_segment_id)
        segment_count = len(self.project.segments)
        segment_text = ngettext(
            "%(count)d segment", "%(count)d segments", segment_count
        ) % {"count": segment_count}
        self.timeline_summary.set_text(
            _("%(segments)s · %(duration)s")
            % {
                "segments": segment_text,
                "duration": format_time(self.project.output_duration_us),
            }
        )
        timeline_segment_label = getattr(self, "_timeline_segment_label", None)
        if timeline_segment_label is not None:
            selected_ids = getattr(self, "selected_segment_ids", None)
            title = (
                format_segment_label(selected_index + 1)
                if selected_ids is None
                else selection_title(
                    self.project,
                    selected_ids,
                    self.selected_segment_id,
                )
            )
            timeline_segment_label.set_text(title)
            if hasattr(timeline_segment_label, "set_tooltip_text"):
                timeline_segment_label.set_tooltip_text(title)

    def _append_timeline_row(
        self,
        identifier,
        title,
        subtitle,
        height,
        draw_func,
        width,
        interaction,
        *,
        header_class=None,
        lane_class=None,
    ):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_size_request(TRACK_HEADER_WIDTH, height + TRACK_ROW_BORDER)
        header.add_css_class("editor-track-header")
        if header_class:
            header.add_css_class(header_class)
        if identifier:
            badge = Gtk.Label(label=identifier)
            badge.set_valign(Gtk.Align.CENTER)
            badge.add_css_class("caption")
            badge.add_css_class("editor-track-badge")
            header.append(badge)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text.set_valign(Gtk.Align.CENTER)
        text.set_hexpand(True)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        text.append(title_label)
        if subtitle:
            subtitle_label = Gtk.Label(
                label=subtitle,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            subtitle_label.add_css_class("caption")
            subtitle_label.add_css_class("dim-label")
            text.append(subtitle_label)
        header.append(text)
        self.track_headers.append(header)
        self.timeline_lanes.append(
            self._new_lane(
                height,
                draw_func,
                width,
                interaction=interaction,
                css_class=lane_class,
                accessible_label=(
                    _("Timeline ruler")
                    if interaction == "scrub"
                    else _("%(title)s track timeline") % {"title": title}
                ),
            )
        )
        return title_label

    def _new_lane(
        self,
        height,
        draw_func,
        width,
        user_data=None,
        interaction=None,
        css_class=None,
        accessible_label=None,
    ):
        area = Gtk.DrawingArea()
        area.set_content_width(width)
        area.set_content_height(height)
        area.set_hexpand(False)
        area.add_css_class("editor-timeline-lane")
        if css_class:
            area.add_css_class(css_class)
        area.set_accessible_role(Gtk.AccessibleRole.GROUP)
        if accessible_label:
            area.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [accessible_label],
            )
        area.set_draw_func(draw_func, user_data)
        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        if interaction == "scrub":
            area.set_cursor_from_name("ew-resize")
            click.connect("pressed", self._timeline_pressed, area)
            click.connect("released", self._timeline_released)
            area.add_controller(click)
            drag = Gtk.GestureDrag()
            drag.set_button(Gdk.BUTTON_PRIMARY)
            drag.connect("drag-begin", self._timeline_drag_begin, area)
            drag.connect("drag-update", self._timeline_drag_update, area)
            drag.connect("drag-end", self._timeline_drag_end)
            area.add_controller(drag)
        elif interaction == "select":
            area.set_cursor_from_name("pointer")
            click.connect("released", self._track_released, area)
            area.add_controller(click)
        elif interaction == "audio_gain":
            area.set_cursor_from_name("pointer")
            click.connect("released", self._audio_lane_released, area)
            area.add_controller(click)
            drag = Gtk.GestureDrag()
            drag.set_button(Gdk.BUTTON_PRIMARY)
            drag.set_exclusive(True)
            drag.connect("drag-begin", self._gain_lane_drag_begin, area, user_data)
            drag.connect("drag-update", self._gain_lane_drag_update, area, user_data)
            drag.connect("drag-end", self._gain_lane_drag_end, area, user_data)
            area.add_controller(drag)
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self._gain_lane_motion, area, user_data)
            motion.connect("leave", self._gain_lane_leave, area, user_data)
            area.add_controller(motion)
        return area

    def _on_zoom_changed(self, scale):
        zoom_percent = round(scale.get_value())
        zoom_control = getattr(self, "zoom_scale", None)
        if zoom_control is not None:
            zoom_control.set_tooltip_text(
                _("Zoom %(percentage)d%%") % {"percentage": zoom_percent}
            )
        if self._syncing:
            return
        if getattr(self, "_zoom_dragging", False) or getattr(
            self,
            "_zoom_discrete_active",
            False,
        ):
            self.zoom_percentage.set_text(
                _("%(percentage)d%%") % {"percentage": zoom_percent}
            )
            EditorWindow._queue_zoom_update(self, zoom_percent)
            return
        EditorWindow._apply_zoom_percent(self, zoom_percent)

    def _apply_zoom_percent(self, zoom_percent):
        new_zoom = zoom_percent / 100
        if abs(new_zoom - self._zoom) < 0.0001:
            self.zoom_percentage.set_text(
                _("%(percentage)d%%") % {"percentage": round(self._zoom * 100)}
            )
            return
        adjustment = self.timeline_scroll.get_hadjustment()
        page_size = adjustment.get_page_size()
        if self._next_zoom_anchor is not None:
            zoom_fraction, anchor_x = self._next_zoom_anchor
            self._next_zoom_anchor = None
        else:
            width = max(
                1,
                getattr(self, "_timeline_clip_width", self.timeline_lanes.get_width()),
            )
            playhead_x = _playhead_x(
                self._playhead_us,
                self.project.output_duration_us,
                width,
            )
            visible_x = playhead_x - adjustment.get_value()
            zoom_fraction = self._playhead_us / self.project.output_duration_us
            anchor_x = visible_x if 0 <= visible_x <= page_size else page_size / 2
        self._pending_zoom_fraction = zoom_fraction
        self._pending_zoom_anchor = anchor_x
        width, self._zoom = timeline_content_width(
            new_zoom,
            self.project.output_duration_us,
            self._timeline_viewport_width(),
        )
        self._timeline_clip_width = timeline_clip_width(
            self._zoom,
            self.project.output_duration_us,
        )
        self.zoom_percentage.set_text(
            _("%(percentage)d%%") % {"percentage": round(self._zoom * 100)}
        )
        self._requested_timeline_width = width
        self._resize_timeline_lanes(width)
        adjustment.set_upper(max(width, page_size))
        target = anchored_zoom_scroll_value(
            zoom_fraction,
            self._timeline_clip_width,
            anchor_x,
            page_size,
            width,
        )
        self._set_timeline_scroll_value(target)
        self._update_playhead(width)
        self._schedule_zoom_anchor()

    def _queue_zoom_update(self, zoom_percent):
        self._pending_zoom_percent = zoom_percent
        if self._zoom_update_id is None:
            self._zoom_update_id = GLib.timeout_add(
                ZOOM_TIMELINE_REFRESH_MS,
                self._flush_zoom_update,
            )

    def _flush_zoom_update(self):
        self._zoom_update_id = None
        zoom_percent = self._pending_zoom_percent
        self._pending_zoom_percent = None
        if zoom_percent is not None:
            EditorWindow._apply_zoom_percent(self, zoom_percent)
        return False

    def _cancel_zoom_update(self):
        if self._zoom_update_id is not None:
            GLib.source_remove(self._zoom_update_id)
            self._zoom_update_id = None
        self._pending_zoom_percent = None

    def _zoom_drag_begin(self, _scale):
        EditorWindow._cancel_discrete_zoom_settle(self)
        EditorWindow._cancel_waveform_detail_restore(self)
        self._zoom_discrete_active = False
        self._zoom_dragging = True

    def _zoom_drag_end(self, scale):
        final_percent = round(scale.get_value())
        pending_percent = self._pending_zoom_percent
        self._cancel_zoom_update()
        self._zoom_dragging = False
        if (
            pending_percent is not None
            or abs(final_percent / 100 - self._zoom) >= 0.0001
        ):
            EditorWindow._apply_zoom_percent(self, final_percent)
        else:
            self._redraw_timeline_lanes()

    def _set_zoom_scale_range(self, minimum_percent):
        self.zoom_scale.set_range(minimum_percent, 300)
        self.zoom_scale.clear_marks()
        if minimum_percent <= 100:
            self.zoom_scale.add_mark(100, Gtk.PositionType.BOTTOM, None)

    def _change_timeline_zoom(self, direction, anchor=None):
        """Move timeline zoom by one slider step."""
        if not direction:
            return
        EditorWindow._begin_discrete_zoom(self)
        self._next_zoom_anchor = anchor
        previous = self.zoom_scale.get_value()
        self.zoom_scale.set_value(previous + direction * 25)
        if self.zoom_scale.get_value() == previous:
            self._next_zoom_anchor = None
            self._zoom_discrete_active = False
            EditorWindow._cancel_discrete_zoom_settle(self)
            return
        EditorWindow._schedule_discrete_zoom_settle(self)

    def _begin_discrete_zoom(self):
        EditorWindow._cancel_waveform_detail_restore(self)
        self._zoom_discrete_active = True

    def _schedule_discrete_zoom_settle(self):
        EditorWindow._cancel_discrete_zoom_settle(self)
        self._zoom_settle_id = GLib.timeout_add(
            ZOOM_INTERACTION_SETTLE_MS,
            self._finish_discrete_zoom,
        )

    def _cancel_discrete_zoom_settle(self):
        if self._zoom_settle_id is not None:
            GLib.source_remove(self._zoom_settle_id)
            self._zoom_settle_id = None

    def _finish_discrete_zoom(self):
        self._zoom_settle_id = None
        pending_percent = self._pending_zoom_percent
        self._cancel_zoom_update()
        if pending_percent is not None:
            EditorWindow._apply_zoom_percent(self, pending_percent)
            self._zoom_settle_id = GLib.timeout_add(
                ZOOM_TIMELINE_REFRESH_MS,
                self._complete_discrete_zoom,
            )
            return False
        return self._complete_discrete_zoom()

    def _complete_discrete_zoom(self):
        self._zoom_settle_id = None
        self._zoom_discrete_active = False
        EditorWindow._schedule_waveform_detail_restore(self)
        return False

    def _schedule_waveform_detail_restore(self):
        EditorWindow._cancel_waveform_detail_restore(self)
        self._pending_waveform_restore_lanes = list(
            getattr(self, "_audio_lanes", {}).values()
        )
        if self._pending_waveform_restore_lanes:
            self._waveform_restore_id = GLib.timeout_add(
                WAVEFORM_DETAIL_RESTORE_MS,
                self._restore_next_waveform_detail,
            )

    def _restore_next_waveform_detail(self):
        if not self._pending_waveform_restore_lanes:
            self._waveform_restore_id = None
            return False
        self._pending_waveform_restore_lanes.pop(0).queue_draw()
        if self._pending_waveform_restore_lanes:
            return True
        self._waveform_restore_id = None
        return False

    def _cancel_waveform_detail_restore(self):
        if self._waveform_restore_id is not None:
            GLib.source_remove(self._waveform_restore_id)
            self._waveform_restore_id = None
        self._pending_waveform_restore_lanes = []

    def _on_timeline_scroll(self, controller, _dx, dy):
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            if not dy:
                return True
            adjustment = self.timeline_scroll.get_hadjustment()
            page_size = adjustment.get_page_size()
            pointer_x = self._timeline_pointer_x
            if pointer_x is None:
                pointer_x = page_size / 2
            pointer_x = max(0.0, min(page_size, pointer_x))
            width = max(
                1,
                getattr(self, "_timeline_clip_width", self._requested_timeline_width),
            )
            zoom_fraction = (adjustment.get_value() + pointer_x) / width
            self._change_timeline_zoom(
                1 if dy < 0 else -1,
                (max(0.0, min(1.0, zoom_fraction)), pointer_x),
            )
            return True

        if state & Gdk.ModifierType.SHIFT_MASK:
            adjustment = self.timeline_scroll.get_vadjustment()
        else:
            adjustment = self.timeline_scroll.get_hadjustment()

        if not dy:
            return True
        step = adjustment.get_step_increment() or adjustment.get_page_size() / 10
        target = adjustment.get_value() + dy * step
        target = max(0.0, min(adjustment.get_upper() - adjustment.get_page_size(), target))
        if state & Gdk.ModifierType.SHIFT_MASK:
            adjustment.set_value(target)
        else:
            # This is user input, so let value-changed reach the follow-state
            # handler just as it does when the scrollbar is dragged manually.
            adjustment.set_value(target)
        return True

    def _on_timeline_pointer_motion(self, _controller, x, _y):
        self._timeline_pointer_x = x

    def _on_timeline_pointer_leave(self, _controller):
        self._timeline_pointer_x = None

    def _resize_timeline_lanes(self, width):
        self.timeline_lanes.set_size_request(width, -1)
        self.timeline_overlay.set_size_request(width, -1)
        child = self.timeline_lanes.get_first_child()
        while child:
            if isinstance(child, Gtk.DrawingArea):
                child.set_content_width(width)
                child.queue_draw()
            child = child.get_next_sibling()

    def _schedule_zoom_anchor(self):
        if self._pending_zoom_anchor is None and self._pending_scroll_value is None:
            return
        if self._zoom_anchor_timeout_id is not None:
            GLib.source_remove(self._zoom_anchor_timeout_id)
        self._zoom_anchor_attempts = 0
        self._zoom_anchor_timeout_id = GLib.timeout_add(16, self._apply_zoom_anchor)

    def _apply_zoom_anchor(self):
        if self._cleaned or (
            self._pending_zoom_anchor is None and self._pending_scroll_value is None
        ):
            self._zoom_anchor_timeout_id = None
            return False
        allocated_width = max(1, self.timeline_lanes.get_width())
        adjustment = self.timeline_scroll.get_hadjustment()
        self._zoom_anchor_attempts += 1
        geometry_ready = not (
            abs(allocated_width - self._requested_timeline_width) > 2
            or abs(adjustment.get_upper() - allocated_width) > 2
        )
        if not geometry_ready and self._zoom_anchor_attempts < 30:
            return True
        if self._pending_zoom_anchor is not None:
            zoom_fraction = self._pending_zoom_fraction
            if zoom_fraction is None:
                zoom_fraction = self._playhead_us / self.project.output_duration_us
            target = anchored_zoom_scroll_value(
                zoom_fraction,
                self._timeline_clip_width,
                self._pending_zoom_anchor,
                adjustment.get_page_size(),
                adjustment.get_upper(),
            )
        else:
            scroll_value = self._pending_scroll_value or 0.0
            target = max(
                0.0,
                min(
                    adjustment.get_upper() - adjustment.get_page_size(),
                    scroll_value,
                ),
            )
        self._set_timeline_scroll_value(target)
        self._pending_zoom_anchor = None
        self._pending_zoom_fraction = None
        self._pending_scroll_value = None
        self._zoom_anchor_timeout_id = None
        return False

    def _timeline_viewport_width(self):
        width = self.timeline_scroll.get_width()
        return width if width > 1 else 900

    def _set_timeline_scroll_value(self, value):
        self._setting_timeline_scroll = True
        try:
            self.timeline_scroll.get_hadjustment().set_value(value)
        finally:
            self._setting_timeline_scroll = False

    def _on_timeline_scroll_changed(self, adjustment):
        playhead_overlay = getattr(self, "playhead_overlay", None)
        if playhead_overlay is not None:
            playhead_overlay.queue_draw()
        if (
            self._setting_timeline_scroll
            or getattr(self, "_pending_zoom_anchor", None) is not None
            or getattr(self, "_pending_scroll_value", None) is not None
            or not self.preview.playing
        ):
            return
        width = max(1, self.timeline_lanes.get_width())
        playhead_x = _playhead_x(
            self._playhead_us,
            self.project.output_duration_us,
            width,
        )
        self._playback_follow_active = False
        self._playback_follow_suspended = True
        self._playback_follow_visible_x = playhead_x - adjustment.get_value()

    def _on_timeline_viewport_changed(self, *_args):
        if not self._ui_ready:
            return
        width = self._timeline_viewport_width()
        previous = getattr(self, "_last_timeline_viewport_width", 0)
        # Scrollbars can change the viewport by roughly 15 px while zooming.
        # That is not a window resize and must not rebuild/reset the timeline.
        if abs(width - previous) > 24:
            self._last_timeline_viewport_width = width
            if not self._viewport_refresh_pending:
                self._viewport_refresh_pending = True
                GLib.idle_add(self._refresh_after_viewport_change)

    def _refresh_after_viewport_change(self):
        if self._scrubbing:
            return False
        self._viewport_refresh_pending = False
        if not self._cleaned:
            self._refresh()
        return False

    def _timeline_pressed(self, _gesture, _count, x, _y, area):
        self._scrubbing = True
        self._scrub_at(x, area)

    def _timeline_released(self, *_args):
        self._finish_scrub()

    def _timeline_drag_begin(self, gesture, x, _y, area):
        self._scrubbing = True
        self._timeline_drag_start_x = x
        self._scrub_at(x, area)

    def _timeline_drag_update(self, gesture, offset_x, _offset_y, area):
        self._scrub_at(self._timeline_drag_start_x + offset_x, area)

    def _timeline_drag_end(self, *_args):
        self._finish_scrub()

    def _track_released(self, gesture, _count, x, _y, area):
        segment_id = EditorWindow._segment_at_lane_x(self, x, area).id
        state = (
            gesture.get_current_event_state()
            if gesture is not None and hasattr(gesture, "get_current_event_state")
            else 0
        )
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if shift:
            self._select(None, segment_id, extend=True, preserve=control)
        elif control:
            self._select(None, segment_id, toggle=True)
        else:
            self._select(None, segment_id)

    def _segment_at_lane_x(self, x, area):
        width = max(1, getattr(self, "_timeline_clip_width", area.get_width()))
        output_us = round(
            max(0, min(1, x / width)) * self.project.output_duration_us
        )
        output_us = min(output_us, self.project.output_duration_us - 1)
        return self.project.segment_at_output(output_us)[0]

    def _gain_line_hit(self, x, y, area, track_id):
        width = max(1, getattr(self, "_timeline_clip_width", area.get_width()))
        segment = next(
            (
                segment
                for _index, segment, start, end in self._segment_rects(width)
                if start + 7 <= x <= end - 7
            ),
            None,
        )
        if segment is None:
            return None
        height = area.get_height() or AUDIO_TRACK_HEIGHT
        line_y = gain_line_y(
            gain_to_decibels(segment.audio[track_id].gain),
            height,
        )
        return segment if abs(float(y) - line_y) <= GAIN_LINE_HIT_RADIUS else None

    def _gain_lane_motion(self, _motion, x, y, area, track_id):
        segment = self._gain_line_hit(x, y, area, track_id)
        if segment is None:
            area.set_cursor_from_name("pointer")
            EditorWindow._hide_gain_feedback(self, area, track_id)
            return
        area.set_cursor_from_name("ns-resize")
        EditorWindow._show_gain_feedback(
            self,
            area,
            track_id,
            segment.id,
            gain_to_decibels(segment.audio[track_id].gain),
            x,
        )

    def _gain_lane_leave(self, _motion, area, track_id):
        area.set_cursor_from_name("pointer")
        drag = self._gain_lane_drag
        if drag is None or drag["area"] is not area:
            EditorWindow._hide_gain_feedback(self, area, track_id)

    def _show_gain_feedback(
        self,
        area,
        track_id,
        segment_id,
        decibels,
        x,
    ):
        self._gain_feedback[track_id] = {
            "area": area,
            "segment_id": segment_id,
            "decibels": decibels,
            "x": x,
        }
        area.queue_draw()

    def _hide_gain_feedback(self, area, track_id):
        feedback = self._gain_feedback.get(track_id)
        if feedback is not None and feedback["area"] is area:
            del self._gain_feedback[track_id]
            area.queue_draw()

    def _audio_lane_released(self, gesture, count, x, y, area):
        if self._gain_lane_click_suppressed or self._gain_lane_drag is not None:
            self._gain_lane_click_suppressed = False
            return
        self._track_released(gesture, count, x, y, area)

    def _gain_lane_drag_begin(self, gesture, x, y, area, track_id):
        segment = self._gain_line_hit(x, y, area, track_id)
        if segment is None:
            if hasattr(gesture, "set_state"):
                gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        selected_ids = _selection_ids(self)
        target_ids = (
            _ordered_segment_ids(self.project, selected_ids)
            if segment.id in selected_ids
            else (segment.id,)
        )
        height = area.get_height() or AUDIO_TRACK_HEIGHT
        start_db = gain_to_decibels(segment.audio[track_id].gain)
        self._gain_lane_drag = {
            "area": area,
            "track_id": track_id,
            "target_ids": target_ids,
            "segment_id": segment.id,
            "start_x": x,
            "start_db": start_db,
            "height": height,
            "decibels": start_db,
        }
        self._gain_lane_click_suppressed = True
        area.set_cursor_from_name("ns-resize")
        if hasattr(gesture, "set_state"):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        EditorWindow._show_gain_feedback(
            self,
            area,
            track_id,
            segment.id,
            start_db,
            x,
        )
        self._gain_drag_begin(area, target_ids, track_id)

    def _gain_lane_drag_update(self, _gesture, offset_x, offset_y, area, track_id):
        drag = self._gain_lane_drag
        if drag is None or drag["area"] is not area or drag["track_id"] != track_id:
            return
        decibels = round(
            gain_db_after_drag(drag["start_db"], offset_y, drag["height"]),
            1,
        )
        EditorWindow._show_gain_feedback(
            self,
            area,
            track_id,
            drag["segment_id"],
            decibels,
            drag["start_x"] + offset_x,
        )
        feedback_text = format_gain_db(decibels)
        area.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("%(gain)s for selected audio") % {"gain": feedback_text}],
        )
        if decibels == drag["decibels"]:
            return
        drag["decibels"] = decibels
        self._gain_value(
            0.0 if decibels <= MIN_GAIN_DB else decibels_to_gain(decibels),
            drag["target_ids"],
            track_id,
        )

    def _gain_lane_drag_end(self, _gesture, _offset_x, _offset_y, area, track_id):
        drag = self._gain_lane_drag
        if drag is None or drag["area"] is not area or drag["track_id"] != track_id:
            return
        self._gain_drag_end(area, drag["target_ids"], track_id)
        self._gain_lane_drag = None
        area.set_cursor_from_name("pointer")
        area.queue_draw()
        GLib.idle_add(self._finish_gain_lane_drag)

    def _finish_gain_lane_drag(self):
        self._gain_lane_click_suppressed = False
        if not self._cleaned:
            self._refresh(update_preview=False)
        return False

    def _scrub_at(self, x, area):
        adjustment = self.timeline_scroll.get_hadjustment()
        scroll_value = adjustment.get_value()
        page_size = adjustment.get_page_size()
        pointer_x = x - scroll_value
        speed = edge_scroll_delta(pointer_x, page_size)
        visible_pointer_x = _clamp_viewport_pointer_x(pointer_x, page_size)
        self._set_edge_scroll(speed, visible_pointer_x, area)
        self._apply_scrub_position(scroll_value + visible_pointer_x, area)

    def _apply_scrub_position(self, x, area):
        width = max(1, getattr(self, "_timeline_clip_width", area.get_width()))
        self._playhead_us = round(max(0, min(1, x / width)) * self.project.output_duration_us)
        self._update_transport_time(self._playhead_us)
        self._update_playhead()
        self._queue_scrub_preview(self._playhead_us)

    def _queue_scrub_preview(self, output_us):
        """Coalesce exact-frame decoder work while pointer motion stays fluid."""
        self._pending_scrub_preview_us = output_us
        if self._scrub_seek_id is None:
            self._scrub_seek_id = GLib.timeout_add(
                SCRUB_PREVIEW_INTERVAL_MS,
                self._flush_scrub_preview_seek,
            )

    def _flush_scrub_preview_seek(self):
        if not self._scrubbing:
            self._scrub_seek_id = None
            return False
        output_us = self._pending_scrub_preview_us
        self._pending_scrub_preview_us = None
        if output_us is not None:
            self.preview.seek_scrub_frame(output_us)
        return True

    def _cancel_scrub_preview_seek(self):
        if self._scrub_seek_id is not None:
            GLib.source_remove(self._scrub_seek_id)
            self._scrub_seek_id = None
        self._pending_scrub_preview_us = None

    def _finish_scrub(self):
        if not self._scrubbing:
            return
        self._scrubbing = False
        self._cancel_scrub_preview_seek()
        # Resume from the same exact frame already displayed during scrubbing.
        self.preview.seek_output(self._playhead_us)
        self.preview.finish_seek()
        self._playback_follow_active = False
        self._stop_edge_scroll()
        if self._viewport_refresh_pending or self._refresh_after_scrub:
            self._viewport_refresh_pending = False
            self._refresh_after_scrub = False
            self._refresh()

    def _set_edge_scroll(self, speed, pointer_x, area):
        self._edge_scroll_speed = speed
        self._edge_scroll_pointer_x = pointer_x
        self._edge_scroll_area = area
        if speed and self._edge_scroll_id is None:
            self._edge_scroll_id = GLib.timeout_add(30, self._run_edge_scroll)
        elif not speed:
            self._stop_edge_scroll()

    def _run_edge_scroll(self):
        if not self._scrubbing or not self._edge_scroll_speed:
            self._edge_scroll_id = None
            return False
        adjustment = self.timeline_scroll.get_hadjustment()
        current = adjustment.get_value()
        maximum = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        target = max(0.0, min(maximum, current + self._edge_scroll_speed))
        if abs(target - current) < 0.01:
            self._edge_scroll_id = None
            return False
        self._set_timeline_scroll_value(target)
        if self._edge_scroll_area is not None:
            self._apply_scrub_position(
                target + self._edge_scroll_pointer_x,
                self._edge_scroll_area,
            )
        return True

    def _stop_edge_scroll(self):
        if self._edge_scroll_id is not None:
            GLib.source_remove(self._edge_scroll_id)
            self._edge_scroll_id = None
        self._edge_scroll_speed = 0.0
        self._edge_scroll_area = None

    def _redraw_timeline_lanes(self):
        child = self.timeline_lanes.get_first_child()
        while child:
            child.queue_draw()
            child = child.get_next_sibling()

    def _update_playhead(self, width_override=None):
        playhead_overlay = getattr(self, "playhead_overlay", None)
        if playhead_overlay is not None:
            playhead_overlay.queue_draw()
        self._update_edit_action_sensitivity()
        return False

    def _draw_playhead(self, area, context, width, height):
        content_width = max(
            1,
            getattr(self, "_timeline_clip_width", self.timeline_lanes.get_width()),
        )
        x = _viewport_playhead_x(
            self._playhead_us,
            self.project.output_duration_us,
            content_width,
            self.timeline_scroll.get_hadjustment().get_value(),
        )
        if x < -5 or x > width + 5:
            return
        scrollbar = self.timeline_scroll.get_hscrollbar()
        if scrollbar is not None and scrollbar.get_mapped():
            height = max(0, height - scrollbar.get_height())
        color = area.get_color()
        _set_source_color(context, color)
        context.set_line_width(2)
        context.move_to(x, 0)
        context.line_to(x, height)
        context.stroke()
        context.arc(x, min(height, 5), min(5, height / 2), 0, math.tau)
        context.fill()

    def _update_edit_action_sensitivity(self):
        split_button = getattr(self, "split_button", None)
        delete_button = getattr(self, "delete_button", None)
        selected_count = len(_selection_ids(self))
        segment_count = len(self.project.segments)
        if delete_button is not None:
            delete_button.set_sensitive(0 < selected_count < segment_count)
            if selected_count <= 0:
                delete_tooltip = _("Select a segment to delete")
            elif selected_count >= segment_count:
                delete_tooltip = _("At least one segment must remain")
            elif selected_count == 1:
                delete_tooltip = _("Delete selected segment (Del)")
            else:
                delete_tooltip = ngettext(
                    "Delete %(count)d selected segment (Del)",
                    "Delete %(count)d selected segments (Del)",
                    selected_count,
                ) % {"count": selected_count}
            delete_button.set_tooltip_text(delete_tooltip)
        if split_button is None:
            return
        split_button.set_sensitive(
            selected_count == 1 and self._can_split_at_playhead()
        )
        split_button.set_tooltip_text(
            _("Split at playhead (X)")
            if selected_count == 1
            else _("Select one segment to split")
        )

    def _can_split_at_playhead(self):
        output_us = self._playhead_us
        if not 0 < output_us < self.project.output_duration_us:
            return False
        segment, offset = self.project.segment_at_output(output_us)
        minimum = (
            1
            if self.project.source.variable_frame_rate
            else self.project.source.frame_duration_us
        )
        source_us = segment.source_start_us + offset
        return (
            segment.source_start_us + minimum
            <= source_us
            <= segment.source_end_us - minimum
        )

    def _start_waveforms(self):
        from editor_waveform import generate_waveform

        for track in self.project.source.audio_tracks:
            future = self._waveform_pool.submit(
                generate_waveform,
                self.project.source,
                track,
                self._waveform_cancel,
            )

            def completed(done, track_id=track.id):
                try:
                    buckets = done.result()
                    GLib.idle_add(self._waveform_ready, track_id, buckets, None)
                except Exception as error:
                    GLib.idle_add(self._waveform_ready, track_id, None, str(error))

            future.add_done_callback(completed)

    def _waveform_ready(self, track_id, buckets, error):
        if not self._cleaned:
            self._waveforms[track_id] = buckets if buckets is not None else error
            self._redraw_timeline_lanes()
        return False

    def _segment_rects(self, width):
        cursor = 0
        for index, segment in enumerate(self.project.segments):
            start = cursor / self.project.output_duration_us * width
            cursor += segment.duration_us
            end = cursor / self.project.output_duration_us * width
            yield index, segment, start, end

    def _draw_ruler(self, area, context, width, height, _user_data):
        width = min(width, self._timeline_clip_width)
        duration_seconds = max(0.001, self.project.output_duration_us / 1_000_000)
        major_step = _nice_tick_step(duration_seconds, width)
        minor_step = major_step / 5
        foreground = area.get_color()
        context.set_line_width(1)
        tick = 0
        tick_count = math.floor(duration_seconds / minor_step) + 1
        while tick <= tick_count:
            second = tick * minor_step
            x = second / duration_seconds * width
            is_major = tick % 5 == 0
            _set_source_color(context, foreground, 0.34 if is_major else 0.16)
            tick_height = 9 if is_major else 4
            context.move_to(x + 0.5, height - tick_height)
            context.line_to(x + 0.5, height)
            context.stroke()
            if is_major:
                _set_source_color(context, foreground, 0.72)
                _draw_text(
                    area,
                    context,
                    _format_ruler_time(second, major_step),
                    x + (10 if tick == 0 else 5),
                    5,
                    size=9,
                    max_width=78,
                )
            tick += 1
        _set_source_color(context, foreground, 0.14)
        context.move_to(0, height - 0.5)
        context.line_to(width, height - 0.5)
        context.stroke()

    def _draw_video_lane(self, area, context, width, height, _user_data):
        width = min(width, self._timeline_clip_width)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        foreground = area.get_color()
        selected_ids = _selection_ids(self)
        for index, segment, start, end in self._segment_rects(width):
            selected = segment.id in selected_ids
            x = start + 3
            segment_width = max(1, end - start - 6)
            y = 5
            segment_height = height - 10
            _rounded_rectangle(context, x, y, segment_width, segment_height, 6)
            _set_source_color(context, accent, 0.28 if selected else 0.12)
            context.fill()
            _rounded_rectangle(context, x, y, segment_width, segment_height, 6)
            _set_source_color(context, accent, 0.95 if selected else 0.34)
            context.set_line_width(2 if selected else 1)
            context.stroke()
            if segment_width < 28:
                continue
            context.save()
            context.rectangle(x + 1, y + 1, max(1, segment_width - 2), segment_height - 2)
            context.clip()
            _set_source_color(context, foreground, 0.94 if selected else 0.82)
            _draw_text(
                area,
                context,
                format_segment_label(index + 1),
                x + 9,
                y + 9,
                size=10,
                weight=Pango.Weight.SEMIBOLD,
                max_width=segment_width - 18,
            )
            _set_source_color(context, foreground, 0.58)
            _draw_text(
                area,
                context,
                format_time(segment.duration_us),
                x + 9,
                y + 31,
                size=9,
                max_width=segment_width - 18,
            )
            context.restore()

    def _draw_empty_audio(self, area, context, _width, height, _user_data):
        foreground = area.get_color()
        _set_source_color(context, foreground, 0.68)
        _draw_text(
            area,
            context,
            "This clip has no audio tracks",
            14,
            height / 2 - 7,
            size=10,
        )

    def _draw_waveform(self, area, context, width, height, track_id):
        width = min(width, self._timeline_clip_width)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        foreground = area.get_color()
        selected_ids = _selection_ids(self)
        for _index, segment, start, end in self._segment_rects(width):
            selected = segment.id in selected_ids
            x = start + 3
            segment_width = max(1, end - start - 6)
            y = 5
            segment_height = height - 10
            _rounded_rectangle(context, x, y, segment_width, segment_height, 5)
            _set_source_color(context, accent, 0.11 if selected else 0.035)
            context.fill()
            if selected:
                _rounded_rectangle(context, x, y, segment_width, segment_height, 5)
                _set_source_color(context, accent, 0.52)
                context.set_line_width(1)
                context.stroke()
            if segment.audio[track_id].muted:
                context.save()
                _rounded_rectangle(context, x, y, segment_width, segment_height, 5)
                context.clip()
                _set_source_color(context, foreground, 0.08)
                context.set_line_width(1)
                hatch_x = x - segment_height
                while hatch_x < x + segment_width:
                    context.move_to(hatch_x, y + segment_height)
                    context.line_to(hatch_x + segment_height, y)
                    hatch_x += 9
                context.stroke()
                context.restore()

        buckets = self._waveforms.get(track_id)
        if not isinstance(buckets, list) or not buckets:
            _set_source_color(context, foreground, 0.18)
            context.set_line_width(1)
            context.set_dash([3, 4])
            context.move_to(12, height / 2 + 0.5)
            context.line_to(max(12, width - 12), height / 2 + 0.5)
            context.stroke()
            context.set_dash([])
            _set_source_color(context, foreground, 0.48)
            message = (
                "Waveform unavailable"
                if isinstance(buckets, str)
                else "Loading waveform…"
            )
            _draw_text(area, context, message, 14, 8, size=9, max_width=160)
            EditorWindow._draw_gain_lines(
                self,
                area,
                context,
                width,
                height,
                track_id,
            )
            return

        midpoint = height / 2
        peak_height = max(1, midpoint - 10)
        bucket_count = len(buckets)
        clip_start, _y1, clip_end, _y2 = context.clip_extents()
        step = waveform_sample_step(
            width,
            self.project.output_duration_us,
            self.project.source.duration_us,
            bucket_count,
        )
        lightweight_zoom = getattr(self, "_zoom_dragging", False) or getattr(
            self,
            "_zoom_discrete_active",
            False,
        )
        if lightweight_zoom:
            step = max(step, 3)
        context.set_line_width(max(1, step * 0.68))

        def trace_segment(segment, start, end, gain):
            left = max(start + 4, clip_start, 0)
            right = min(end - 4, clip_end, width)
            if left >= right:
                return False
            first_x = start + math.ceil((left - start) / step) * step
            x = max(left, first_x)
            while x < right:
                offset_us = round(
                    (x - start) / max(1, width) * self.project.output_duration_us
                )
                source_us = min(
                    segment.source_end_us,
                    segment.source_start_us + offset_us,
                )
                bucket_index = min(
                    bucket_count - 1,
                    source_us * bucket_count // self.project.source.duration_us,
                )
                low, high = buckets[bucket_index]
                context.move_to(x + 0.5, midpoint - high / 32768 * peak_height * gain)
                context.line_to(x + 0.5, midpoint - low / 32768 * peak_height * gain)
                x += step
            return True

        def stroke_segment(segment, start, end, gain):
            context.save()
            _rounded_rectangle(
                context,
                start + 3,
                5,
                max(1, end - start - 6),
                height - 10,
                5,
            )
            context.clip()
            if trace_segment(segment, start, end, gain):
                context.stroke()
            context.restore()

        for _index, segment, start, end in self._segment_rects(width):
            setting = segment.audio[track_id]
            gain = effective_audio_gain(setting)
            if not lightweight_zoom:
                if setting.muted:
                    _set_source_color(
                        context,
                        foreground,
                        0.10,
                    )
                    stroke_segment(segment, start, end, 1.0)
            if gain <= 0:
                continue
            selected = segment.id in selected_ids
            _set_source_color(context, accent, 0.90 if selected else 0.68)
            stroke_segment(segment, start, end, gain)

        EditorWindow._draw_gain_lines(
            self,
            area,
            context,
            width,
            height,
            track_id,
        )

    def _draw_gain_lines(
        self,
        area,
        context,
        width,
        height,
        track_id,
    ):
        """Draw one consistently styled, directly draggable dB line per segment."""
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        feedback = getattr(self, "_gain_feedback", {}).get(track_id)
        feedback_bounds = None
        for _index, segment, start, end in self._segment_rects(width):
            left = start + 7
            right = end - 7
            if right <= left:
                continue
            setting = segment.audio[track_id]
            y = gain_line_y(gain_to_decibels(setting.gain), height) + 0.5
            context.set_dash([3, 3] if setting.muted else [])
            _set_source_color(context, accent, 0.28)
            context.set_line_width(4)
            context.move_to(left, y)
            context.line_to(right, y)
            context.stroke()
            _set_source_color(context, accent, 0.96)
            context.set_line_width(1.5)
            context.move_to(left, y)
            context.line_to(right, y)
            context.stroke()
            if (
                feedback is not None
                and feedback["area"] is area
                and feedback["segment_id"] == segment.id
            ):
                feedback_bounds = y
        context.set_dash([])
        if feedback_bounds is None or feedback is None:
            return
        feedback_text = format_gain_db(feedback["decibels"])
        line_y = feedback_bounds
        visible_left = 0.0
        visible_right = float(width)
        timeline_scroll = getattr(self, "timeline_scroll", None)
        if timeline_scroll is not None:
            adjustment = timeline_scroll.get_hadjustment()
            visible_left = max(0.0, min(float(width), adjustment.get_value()))
            visible_right = max(
                visible_left,
                min(float(width), visible_left + adjustment.get_page_size()),
            )
        badge_width = gain_feedback_badge_width(
            _text_pixel_width(area, feedback_text, size=9),
            visible_right - visible_left,
        )
        badge_x = gain_feedback_badge_x(
            feedback["x"],
            badge_width,
            visible_left,
            visible_right,
        )
        badge_y = (
            line_y - GAIN_FEEDBACK_HEIGHT - 4
            if line_y >= height / 2
            else line_y + 4
        )
        badge_y = max(2.0, min(height - GAIN_FEEDBACK_HEIGHT - 2, badge_y))
        background = StereoPeakMeter._theme_color(
            area,
            "window_bg_color",
            "#242424",
        )
        text_color = StereoPeakMeter._theme_color(
            area,
            "window_fg_color",
            "#ffffff",
        )
        shadow = Gdk.RGBA(red=0.0, green=0.0, blue=0.0, alpha=1.0)
        _rounded_rectangle(
            context,
            badge_x + 1,
            badge_y + 2,
            badge_width,
            GAIN_FEEDBACK_HEIGHT,
            5,
        )
        _set_source_color(context, shadow, 0.34)
        context.fill()
        _rounded_rectangle(
            context,
            badge_x,
            badge_y,
            badge_width,
            GAIN_FEEDBACK_HEIGHT,
            5,
        )
        _set_source_color(context, background, 0.96)
        context.fill()
        _rounded_rectangle(
            context,
            badge_x + 0.5,
            badge_y + 0.5,
            max(1.0, badge_width - 1),
            GAIN_FEEDBACK_HEIGHT - 1,
            4.5,
        )
        _set_source_color(context, text_color, 0.32)
        context.set_line_width(1)
        context.stroke()
        _set_source_color(context, text_color)
        _draw_text(
            area,
            context,
            feedback_text,
            badge_x + 7,
            badge_y + 3,
            size=9,
            max_width=badge_width - 14,
        )

    def _on_preview_position(self, output_us):
        if self._scrubbing:
            return
        self._playhead_us = min(output_us, self.project.output_duration_us)
        self._update_transport_time(output_us)
        EditorWindow._update_transport_seek(self, output_us)
        self._update_playhead()
        if self.preview.playing:
            self._follow_playhead_scroll()

    def _toggle_playback(self, *_args):
        if self._source_invalid:
            return
        self.preview.toggle_playback()

    def _seek_relative_to_playhead(self, offset_us):
        """Seek by an output-timeline offset, keeping the target in bounds."""
        output_us = max(
            0,
            min(self.project.output_duration_us, self._playhead_us + offset_us),
        )
        if output_us == self._playhead_us:
            return
        self.preview.seek_output(output_us)
        self.preview.finish_seek()

    def _on_preview_state_changed(self, playing):
        self.playback_button.set_icon_name(PAUSE if playing else PLAY)
        self.playback_button.set_tooltip_text(_("Pause") if playing else _("Play"))
        if not playing:
            self._playback_follow_suspended = False
            self._playback_follow_visible_x = None
            self._reset_audio_peaks()

    def _on_audio_peaks(self, track_id, peaks_db, held_peaks_db):
        meter = self._peak_meters.get(track_id)
        if meter is not None:
            meter.set_peaks(peaks_db, held_peaks_db)

    def _reset_audio_peaks(self):
        for meter in self._peak_meters.values():
            meter.reset()

    def _on_key_pressed(self, _controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape and not state:
            if getattr(self, "is_fullscreen", lambda: False)():
                self.unfullscreen()
                return True
            export_dialog = getattr(self, "_export_dialog", None)
            if export_dialog is not None:
                if getattr(self, "_exporter", None) is not None:
                    export_dialog.show_close_warning()
                else:
                    export_dialog.close()
                return True
            GLib.idle_add(self.request_close)
            return True
        control_pressed = bool(state & Gdk.ModifierType.CONTROL_MASK)
        disallowed_zoom_modifiers = (
            Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
            | Gdk.ModifierType.META_MASK
        )
        if (
            control_pressed
            and not state
            & (disallowed_zoom_modifiers | Gdk.ModifierType.SHIFT_MASK)
            and _matches_base_key(keyval, keycode, Gdk.KEY_a)
        ):
            self._select_all()
            return True
        if control_pressed and not state & disallowed_zoom_modifiers:
            if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self._change_timeline_zoom(1)
                return True
            if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self._change_timeline_zoom(-1)
                return True
        blocked_modifiers = (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
            | Gdk.ModifierType.META_MASK
        )
        if (
            keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_KP_Up, Gdk.KEY_KP_Down)
            and not state & blocked_modifiers
        ):
            direction = 1 if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up) else -1
            if getattr(self, "is_fullscreen", lambda: False)():
                self._change_volume(direction)
            else:
                self._change_timeline_zoom(direction)
            return True
        if state & blocked_modifiers:
            return False
        if _matches_base_key(keyval, keycode, Gdk.KEY_m):
            self._toggle_muted()
            return True
        if _matches_base_key(keyval, keycode, Gdk.KEY_f):
            self._toggle_fullscreen()
            return True
        if _matches_base_key(keyval, keycode, Gdk.KEY_x):
            self._split()
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            if len(_selection_ids(self)) < len(self.project.segments):
                self._delete()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self._seek_relative_to_playhead(-5_000_000)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self._seek_relative_to_playhead(5_000_000)
            return True
        if _matches_base_key(keyval, keycode, Gdk.KEY_comma):
            self._seek_relative_to_playhead(-self.project.source.frame_duration_us)
            return True
        if _matches_base_key(keyval, keycode, Gdk.KEY_period):
            self._seek_relative_to_playhead(self.project.source.frame_duration_us)
            return True
        if keyval != Gdk.KEY_space or state & blocked_modifiers:
            return False
        if not self._space_pressed:
            self._space_pressed = True
            self._toggle_playback()
        return True

    def _on_key_released(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_space:
            self._space_pressed = False

    def _follow_playhead_scroll(self):
        adjustment = self.timeline_scroll.get_hadjustment()
        page_size = adjustment.get_page_size()
        upper = adjustment.get_upper()
        current = adjustment.get_value()
        width = max(
            1,
            getattr(self, "_timeline_clip_width", self.timeline_lanes.get_width()),
        )
        # Follow the same integer pixel that _draw_playhead renders. Using the
        # fractional timeline position here makes the visible coordinate vary
        # by a subpixel as the drawn coordinate rounds from one pixel to the next.
        playhead_x = _playhead_x(
            self._playhead_us,
            self.project.output_duration_us,
            width,
        )
        visible_x = playhead_x - current
        if self._playback_follow_suspended:
            crossed_trigger = crossed_playback_follow_trigger(
                self._playback_follow_visible_x,
                visible_x,
                page_size,
            )
            self._playback_follow_visible_x = visible_x
            if not crossed_trigger:
                return
            self._playback_follow_suspended = False
            self._playback_follow_active = True
        if self._playback_follow_active:
            target = max(
                0.0,
                min(
                    upper - page_size,
                    playhead_x - page_size * PLAYBACK_FOLLOW_POSITION,
                ),
            )
        else:
            target = follow_scroll_value(playhead_x, current, page_size, upper)
            if (
                visible_x > page_size * PLAYBACK_FOLLOW_POSITION
                and target > current
            ):
                self._playback_follow_active = True
        if abs(target - current) >= 0.5:
            self._set_timeline_scroll_value(target)

    def _select(
        self,
        _button,
        segment_id: str,
        *,
        toggle=False,
        extend=False,
        preserve=False,
    ):
        if not hasattr(self, "project"):
            self.selected_segment_id = segment_id
            self.selected_segment_ids = {segment_id}
            self._selection_anchor_id = segment_id
            self._refresh(update_preview=False)
            return
        current = {
            selected_id
            for selected_id in _selection_ids(self)
            if isinstance(selected_id, str)
        }
        if extend:
            anchor_id = getattr(self, "_selection_anchor_id", None)
            segment_ids = {segment.id for segment in self.project.segments}
            if not isinstance(anchor_id, str) or anchor_id not in segment_ids:
                anchor_id = getattr(self, "selected_segment_id", segment_id)
            if not isinstance(anchor_id, str) or anchor_id not in segment_ids:
                anchor_id = segment_id
            anchor_index = self.project.segment_index(anchor_id)
            target_index = self.project.segment_index(segment_id)
            lower, upper = sorted((anchor_index, target_index))
            ranged = {
                segment.id for segment in self.project.segments[lower : upper + 1]
            }
            current = current | ranged if preserve else ranged
            primary = segment_id
        elif toggle:
            if segment_id in current:
                if len(current) == 1:
                    return
                current.remove(segment_id)
                primary = getattr(self, "selected_segment_id", None)
                clicked_index = self.project.segment_index(segment_id)
                self._selection_anchor_id = min(
                    current,
                    key=lambda selected_id: (
                        abs(self.project.segment_index(selected_id) - clicked_index),
                        self.project.segment_index(selected_id),
                    ),
                )
                if primary not in current:
                    primary = self._selection_anchor_id
            else:
                current.add(segment_id)
                primary = segment_id
                self._selection_anchor_id = segment_id
        else:
            self._selection_anchor_id = segment_id
            if current == {segment_id}:
                return
            current = {segment_id}
            primary = segment_id
        EditorWindow._set_selection(self, current, primary)
        self._refresh(update_preview=False)

    def _select_all(self):
        segment_ids = tuple(segment.id for segment in self.project.segments)
        if _selection_ids(self) == set(segment_ids):
            return
        primary = getattr(self, "selected_segment_id", None)
        EditorWindow._set_selection(self, segment_ids, primary)
        self._refresh(update_preview=False)

    def _split(self, *_args):
        if getattr(self, "is_fullscreen", lambda: False)():
            return
        if len(_selection_ids(self)) != 1 or not self._can_split_at_playhead():
            return
        output_us = self._playhead_us
        segment, offset = self.project.segment_at_output(output_us)
        self.history.mutate(
            "split", lambda project: project.split(segment.id, segment.source_start_us + offset)
        )
        selected_segment_id = self.project.segment_at_output(
            min(output_us, self.project.output_duration_us - 1)
        )[0].id
        EditorWindow._set_selection(self, (selected_segment_id,), selected_segment_id)
        if self._scrubbing:
            # The existing lanes draw from the current project, so redraw them
            # now to show the split. Rebuilding the ruler would replace its
            # active gesture and make GTK end the pointer drag.
            self._redraw_timeline_lanes()
            EditorWindow._update_timeline_labels(self)
            self._refresh_after_scrub = True
        else:
            self._refresh()

    def _delete(self, *_args):
        if getattr(self, "is_fullscreen", lambda: False)():
            return
        selected_ids = _ordered_segment_ids(self.project, _selection_ids(self))
        if not selected_ids or len(selected_ids) >= len(self.project.segments):
            return
        next_segment_id = [None]

        def delete_selected(project):
            next_segment_id[0] = project.delete_many(selected_ids)

        self.history.mutate(
            "delete",
            delete_selected,
        )
        EditorWindow._set_selection(
            self,
            (next_segment_id[0],),
            next_segment_id[0],
        )
        self._playhead_us = min(self._playhead_us, self.project.output_duration_us)
        self._refresh()

    def _mute(self, button, segment_ids, track_id):
        if not self._syncing:
            target_ids = _audio_target_ids(self.project, segment_ids)
            muted = button.get_active()

            def set_muted(project):
                for segment_id in target_ids:
                    project.set_muted(segment_id, track_id, muted)

            self.history.mutate(
                "mute",
                set_muted,
            )
            EditorWindow._update_audio_targets(self, target_ids, track_id)
            self._refresh(update_preview=False)

    def _gain_value(self, value, segment_ids, track_id):
        if not self._syncing:
            target_ids = _audio_target_ids(self.project, segment_ids)
            target_key = target_ids[0] if len(target_ids) == 1 else target_ids
            key = (target_key, track_id)

            def set_gain(project):
                for segment_id in target_ids:
                    project.set_gain(segment_id, track_id, value)

            if self._gain_drag_key == key:
                set_gain(self.project)
            else:
                self.history.mutate(
                    "gain",
                    set_gain,
                    coalesce_key=("gain", target_key, track_id),
                )
            EditorWindow._update_audio_targets(self, target_ids, track_id)
            if self._gain_drag_key == key:
                self._queue_gain_redraw(track_id)
            else:
                self._redraw_audio_lane(track_id)

    def _update_audio_targets(self, target_ids, track_id):
        if len(target_ids) == 1:
            self.preview.update_audio_setting(self.project, target_ids[0], track_id)
        else:
            self.preview.update_project(self.project, seek=False)

    def _redraw_audio_lane(self, track_id):
        lane = getattr(self, "_audio_lanes", {}).get(track_id)
        if lane is not None:
            lane.queue_draw()

    def _queue_gain_redraw(self, track_id):
        """Keep live waveform feedback bounded so video can keep presenting."""
        self._pending_gain_redraw_track_id = track_id
        if self._gain_redraw_id is None:
            self._gain_redraw_id = GLib.timeout_add(
                GAIN_WAVEFORM_REFRESH_MS,
                self._flush_gain_redraw,
            )

    def _flush_gain_redraw(self):
        self._gain_redraw_id = None
        track_id = self._pending_gain_redraw_track_id
        self._pending_gain_redraw_track_id = None
        if track_id is not None:
            self._redraw_audio_lane(track_id)
        return False

    def _cancel_gain_redraw(self):
        if self._gain_redraw_id is not None:
            GLib.source_remove(self._gain_redraw_id)
            self._gain_redraw_id = None
        self._pending_gain_redraw_track_id = None

    def _gain_drag_begin(self, _scale, segment_ids, track_id):
        if self._gain_drag_key is not None:
            return
        target_ids = _audio_target_ids(self.project, segment_ids)
        target_key = target_ids[0] if len(target_ids) == 1 else target_ids
        self._gain_drag_key = (target_key, track_id)
        self.history.begin_live_mutation(
            "gain",
            coalesce_key=("gain", target_key, track_id),
        )

    def _gain_drag_end(self, _scale, segment_ids, track_id):
        target_ids = _audio_target_ids(self.project, segment_ids)
        target_key = target_ids[0] if len(target_ids) == 1 else target_ids
        if self._gain_drag_key != (target_key, track_id):
            return
        self.history.commit_live_mutation()
        self._gain_drag_key = None
        self._cancel_gain_redraw()
        self._redraw_audio_lane(track_id)

    def _undo(self, *_args):
        if getattr(self, "is_fullscreen", lambda: False)():
            return
        self.history.undo()
        self._refresh()

    def _redo(self, *_args):
        if getattr(self, "is_fullscreen", lambda: False)():
            return
        self.history.redo()
        self._refresh()

    def save(self):
        save_draft(self.project, history=self.history)
        self.history.mark_saved()
        self._refresh()

    def _confirm_wipe_edits(self, *_args):
        """Offer to remove the current clip's saved and in-memory edits."""
        if self._block_close_during_export():
            return

        from editor_wipe import present_wipe_edits_confirmation

        present_wipe_edits_confirmation(
            Adw,
            self,
            Path(self.project.source.path).name,
            self._on_wipe_edits_confirmed,
        )

    def _on_wipe_edits_confirmed(self, _dialog, response):
        if response != "wipe":
            return

        from editor_drafts import delete_editor_data

        fresh_history = EditorHistory(EditorProject.new(self.project.source))
        if not delete_editor_data(self.project.source.path):
            self._show_toast(_("Could not wipe saved edits"))
            return
        self._reset_to_unedited_project(fresh_history)

    def _reset_to_unedited_project(self, fresh_history):
        """Replace all editor state while keeping the current window and source."""
        self.preview.pause()
        self._scrubbing = False
        self._transport_seek_dragging = False
        self._cancel_scrub_preview_seek()
        self._stop_edge_scroll()
        self._gain_drag_key = None
        self._gain_lane_drag = None
        self._gain_lane_click_suppressed = False
        self._cancel_gain_redraw()
        self._cancel_zoom_update()
        self._cancel_discrete_zoom_settle()
        self._cancel_waveform_detail_restore()
        if self._zoom_anchor_timeout_id is not None:
            GLib.source_remove(self._zoom_anchor_timeout_id)
            self._zoom_anchor_timeout_id = None

        self.history = fresh_history
        self.selected_segment_id = self.project.segments[0].id
        self.selected_segment_ids = {self.selected_segment_id}
        self._selection_anchor_id = self.selected_segment_id
        self._playhead_us = 0
        self._zoom = 1.0
        self._zoom_dragging = False
        self._zoom_discrete_active = False
        self._pending_zoom_anchor = None
        self._pending_zoom_fraction = None
        self._next_zoom_anchor = None
        self._pending_scroll_value = 0
        self._playback_follow_active = False
        self._playback_follow_suspended = False
        self._playback_follow_visible_x = None
        self._viewport_refresh_pending = False
        self._refresh_after_scrub = False
        self._refresh()
        self.preview.seek_output(0)
        self.preview.finish_seek()

    def _show_export(self, *_args):
        from editor_export_dialog import EditorExportDialog

        if self._exporter is not None:
            return
        dialog = EditorExportDialog(
            self,
            self.project,
            self.config,
            self._start_export,
            self._cancel_export,
            initial_options=self._export_options,
        )
        self._export_dialog = dialog
        dialog.connect("closed", self._on_export_dialog_closed)
        dialog.present(self)

    def _on_export_dialog_closed(self, dialog):
        if self._export_dialog is dialog:
            if not self._cleaned:
                options = dialog.snapshot_options()
                if options is not None:
                    self._export_options = options
            self._export_dialog = None

    def _start_export(self, options):
        from editor_export import ExportCancelledError, ExportProcess

        self._export_options = options
        try:
            self.save()
            exporter = ExportProcess(self.project.clone(), options)
        except Exception:
            logging.getLogger("clipper.editor.export").exception(
                "Export setup failed: options=%s", options
            )
            raise
        self._exporter = exporter
        wipe_edits_action = getattr(self, "wipe_edits_action", None)
        if wipe_edits_action is not None:
            wipe_edits_action.set_enabled(False)
        self.export_button_label.set_text(_("Exporting…"))
        self.export_button.set_sensitive(False)
        self.set_deletable(False)
        export_dialog = self._export_dialog

        def update_progress(progress):
            if self._cleaned or self._exporter is not exporter:
                return False
            if self._export_dialog is export_dialog and export_dialog is not None:
                export_dialog.update_export_progress(progress)
            return False

        def report_progress(progress):
            GLib.idle_add(update_progress, progress)

        def worker():
            try:
                exporter.start()
                destination = exporter.finish(report_progress)
                GLib.idle_add(done, destination, None)
            except Exception as error:
                exporter.cleanup()
                GLib.idle_add(done, None, error)

        def done(destination, error):
            if self._exporter is exporter:
                self._exporter = None
            if self._cleaned:
                return False
            self.export_button_label.set_text(_("Export"))
            self.export_button.set_sensitive(not self._source_invalid)
            if wipe_edits_action is not None:
                wipe_edits_action.set_enabled(True)
            self.set_deletable(True)
            active_dialog = (
                export_dialog
                if self._export_dialog is export_dialog
                else None
            )
            if destination:
                if active_dialog is not None:
                    active_dialog.complete_export(destination)
                else:
                    self._show_toast(
                        _("Exported to %(name)s") % {"name": destination.name}
                    )
            elif isinstance(error, ExportCancelledError):
                if active_dialog is not None:
                    active_dialog.export_cancelled()
                else:
                    self._show_toast(_("Export cancelled"))
            else:
                detail = str(error) or _("Unknown error")
                message = _("Export failed: %(error)s") % {"error": detail}
                if active_dialog is not None:
                    active_dialog.fail_export(message)
                else:
                    self._show_toast(message)
            return False

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            if self._exporter is exporter:
                self._exporter = None
            self.export_button_label.set_text(_("Export"))
            self.export_button.set_sensitive(not self._source_invalid)
            if wipe_edits_action is not None:
                wipe_edits_action.set_enabled(True)
            self.set_deletable(True)
            exporter.cleanup()
            raise

    def _cancel_export(self, *_args):
        exporter = self._exporter
        if exporter is None:
            return
        exporter.cancel()

    def _show_toast(self, message):
        toast = Adw.Toast.new(GLib.markup_escape_text(str(message)))
        toast.set_timeout(5)
        self.toast_overlay.add_toast(toast)

    def request_close(self, *, cancelled_callback=None):
        if self._block_close_during_export():
            return
        if self.dirty:
            self._close_cancelled_callback = cancelled_callback
            dialog = Adw.AlertDialog.new(_("Save this edit?"), _("You have unsaved changes."))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("discard", _("Discard"))
            dialog.add_response("save", _("Save draft"))
            dialog.set_response_appearance(
                "discard", Adw.ResponseAppearance.DESTRUCTIVE
            )
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("save")
            dialog.set_close_response("cancel")
            dialog.connect("map", self._style_save_dialog)
            dialog.choose(self, None, self._close_response)
            return
        self._finish()

    def _close_response(self, dialog, result):
        response = dialog.choose_finish(result)
        cancelled_callback = self._close_cancelled_callback
        self._close_cancelled_callback = None
        if response == "save":
            self.save()
            self._finish()
        elif response == "discard":
            self._finish()
        elif cancelled_callback is not None:
            cancelled_callback()

    def _on_close_request(self, *_args):
        self.request_close()
        return True

    def _finish(self):
        if self._block_close_during_export():
            return
        # The application destroys editor windows directly after this callback,
        # which does not reliably pass through Gtk.Widget::hide first.
        self._window_size.save()
        self.cleanup()
        self.finished_callback(self)

    def _block_close_during_export(self):
        if self._exporter is None:
            return False
        dialog = self._export_dialog
        if dialog is not None:
            dialog.show_close_warning()
        else:
            self._show_toast(_("Cancel the export before closing the editor"))
        return True

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        self._export_options = None
        if self._gain_drag_key is not None:
            self.history.commit_live_mutation()
            self._gain_drag_key = None
        self._gain_lane_drag = None
        self._gain_lane_click_suppressed = False
        self._cancel_gain_redraw()
        self._cancel_zoom_update()
        self._cancel_discrete_zoom_settle()
        self._cancel_waveform_detail_restore()
        self._waveform_cancel.set()
        self._cancel_scrub_preview_seek()
        self._stop_edge_scroll()
        if self._zoom_anchor_timeout_id is not None:
            GLib.source_remove(self._zoom_anchor_timeout_id)
            self._zoom_anchor_timeout_id = None
        self._waveform_pool.shutdown(wait=False, cancel_futures=True)
        exporter = getattr(self, "_exporter", None)
        if exporter is not None:
            exporter.cancel()
        monitor = getattr(self, "_source_monitor", None)
        if monitor is not None:
            monitor.cancel()
        self.preview.cleanup()
