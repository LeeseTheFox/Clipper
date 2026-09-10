from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "4.0")

from editor_window import (
    _EDITOR_CSS,
    AMBIENT_GLOW_MAX_EXTENT,
    AMBIENT_GLOW_SAMPLE_WIDTH,
    AUDIO_TRACK_HEIGHT,
    MIXED_MUTE_TOOLTIP,
    PEAK_METER_HEIGHT,
    PEAK_METER_VERTICAL_MARGIN,
    PEAK_METER_WIDTH,
    TRACK_ROW_BORDER,
    StereoPeakMeter,
    _ambient_glow_enabled,
    _ambient_glow_mask_stops,
    ambient_glow_bounds,
    peak_meter_bar_geometry,
)
from gi.repository import Gdk, Gtk

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_engine_ready_status_uses_adwaita_semantic_success_color():
    source = (REPO_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")

    ready_rule = source.split(".engine-status-indicator.ready {", 1)[1].split("}", 1)[0]
    assert "color: @success_color;" in ready_rule
    assert "@accent_color" not in ready_rule


def test_editor_save_and_export_actions_use_adwaita_semantic_styles():
    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")

    assert 'self.export_button.add_css_class("suggested-action")' in source
    assert 'self.export_button.add_css_class("success")' not in source
    assert "EXPORT_LINEAR" in source
    assert 'widget.add_css_class("success")' in source
    assert 'dialog.connect("map", self._style_save_dialog)' in source
    assert '"discard", Adw.ResponseAppearance.DESTRUCTIVE' in source
    assert '"save", Adw.ResponseAppearance.SUGGESTED' in source


def test_editor_css_parses_and_uses_theme_aware_surfaces():
    errors = []
    provider = Gtk.CssProvider()
    provider.connect(
        "parsing-error",
        lambda _provider, section, error: errors.append((section, error)),
    )
    provider.load_from_string(_EDITOR_CSS)

    assert errors == []
    for color_name in (
        "@window_bg_color",
        "@window_fg_color",
        "@view_bg_color",
        "@card_bg_color",
        "@headerbar_bg_color",
        "@accent_color",
        "@error_color",
    ):
        assert color_name in _EDITOR_CSS
    for old_color in ("#15181d", "#1b1f25", "#20242b", "#101318"):
        assert old_color not in _EDITOR_CSS


def test_editor_ambient_glow_expands_only_the_video_sides():
    assert ambient_glow_bounds(640, 360) == pytest.approx((-140.8, -79.2, 921.6, 518.4))
    assert ambient_glow_bounds(1920, 1080) == (
        -AMBIENT_GLOW_MAX_EXTENT,
        -90.0,
        1920 + AMBIENT_GLOW_MAX_EXTENT * 2,
        1260.0,
    )
    assert ambient_glow_bounds(-1, -1) == (0.0, 0.0, 0.0, 0.0)

    stops = _ambient_glow_mask_stops()
    assert [stop.offset for stop in stops] == pytest.approx((0.0, 0.2, 0.8, 1.0))
    assert [stop.color.alpha for stop in stops] == pytest.approx((0.0, 1.0, 1.0, 0.0))


def test_editor_ambient_glow_has_a_private_benchmark_switch():
    assert _ambient_glow_enabled({}) is True
    assert _ambient_glow_enabled({"CLIPPER_DISABLE_AMBIENT_GLOW": "0"}) is True
    for value in ("1", "true", "YES", "on"):
        assert _ambient_glow_enabled({"CLIPPER_DISABLE_AMBIENT_GLOW": value}) is False

    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")
    assert 'paintable.connect("invalidate-contents"' in source
    assert "current_image.snapshot(sample_snapshot, sample_width, sample_height)" in source
    assert "renderer.render_texture(node, sample_bounds)" in source
    assert "snapshot.push_cross_fade(self._transition_progress())" in source
    assert "snapshot.push_blur(AMBIENT_GLOW_BLUR_RADIUS)" in source
    assert "snapshot.push_mask(Gsk.MaskMode.ALPHA)" in source
    assert AMBIENT_GLOW_SAMPLE_WIDTH == 64
    assert "picture_frame.set_child(ambient_overlay)" in source


def test_editor_monitor_frame_clips_the_ambient_glow_to_its_black_surface():
    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")

    assert "picture_frame.set_overflow(Gtk.Overflow.HIDDEN)" in source


def test_editor_tracks_use_in_track_gain_lines_and_explicit_visual_states():
    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")

    assert "class GainDial" not in source
    assert "Gtk.Scale.new_with_range" in source
    assert '"editor-track-card"' in source
    assert '"editor-playhead"' in source
    assert '"Waveform unavailable"' in source
    assert '"Loading waveform…"' in source
    assert "Gtk.AccessibleProperty.LABEL" in source
    assert "badge = _new_track_mute_badge(badge_label)" in source
    assert "badge.set_focus_on_click(False)" in source
    assert "class DeterministicScale(Gtk.Box):" in source
    assert "self.scale.set_inverted(inverted)" in source
    assert "self.scale.set_round_digits(0)" in source
    assert source.count("DeterministicScale(") == 2
    assert "self.scale.set_can_target(False)" in source
    assert "Gtk.GestureDrag.new()" in source
    assert '"audio_gain"' in source
    assert 'set_cursor_from_name("ns-resize")' in source
    assert "EditorWindow._draw_gain_lines(" in source
    assert "EditorWindow._show_gain_feedback(" in source
    assert "gain_db_after_drag(" in source
    assert '"Drag a gain line up or down"' not in source
    assert "drag up or down" not in source
    feedback_drawing = source.split("if feedback_bounds is None", 1)[1].split(
        "def _on_preview_position",
        1,
    )[0]
    assert '"window_bg_color"' in feedback_drawing
    assert '"window_fg_color"' in feedback_drawing
    assert '"accent_bg_color"' not in feedback_drawing
    assert "StereoPeakMeter()" in source
    assert "StereoPeakMeter(gain.scale)" not in source
    assert "gain_percentage" not in source
    assert '"editor-mute-button"' not in source
    assert 'label="Zoom"' not in source
    assert 'label="Mute"' not in source
    assert '"Unsaved changes" if self.dirty else "Saved"' not in source
    assert "editor-selection-indicator" not in source
    assert "selection_title(" in source
    assert "self.append(self.scale)" in source
    assert "self.scale.set_opacity" not in source
    assert 'badge.set_label(f"{badge_label}—")' not in source
    assert MIXED_MUTE_TOOLTIP == "Mixed mute · click to mute all"
    assert ".editor-gain-scale" not in _EDITOR_CSS
    assert source.count("+ TRACK_ROW_BORDER") == 3
    assert "self.timeline_view.add_overlay(self.playhead_overlay)" in source
    assert "self.timeline_overlay.add_overlay(self.playhead_overlay)" not in source

    track_header_rule = _EDITOR_CSS.split(".editor-track-header {", 1)[1].split("}", 1)[0]
    assert "padding: 0 12px;" in track_header_rule

    monitor_picture_rule = _EDITOR_CSS.split(".editor-monitor-picture {", 1)[1].split("}", 1)[0]
    assert "border-radius: 0;" in monitor_picture_rule

    separator_rule = _EDITOR_CSS.split(".editor-toolbar-separator {", 1)[1].split("}", 1)[0]
    assert "margin-left: 6px;" in separator_rule
    assert "margin-right: 6px;" in separator_rule


def test_editor_gain_control_does_not_consume_track_header_width():
    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")

    assert 'interaction == "audio_gain"' in source
    assert "header.append(meter)" in source
    assert "header.append(gain" not in source


def test_editor_stereo_peak_meter_stays_compact_inside_the_audio_track():
    provider = Gtk.CssProvider()
    provider.load_from_string(_EDITOR_CSS)
    display = Gdk.Display.get_default()
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    try:
        meter = StereoPeakMeter()
        minimum_width, natural_width, *_baselines = meter.measure(
            Gtk.Orientation.HORIZONTAL,
            -1,
        )
        minimum_height, natural_height, *_baselines = meter.measure(
            Gtk.Orientation.VERTICAL,
            -1,
        )
    finally:
        Gtk.StyleContext.remove_provider_for_display(display, provider)

    assert minimum_width == natural_width == PEAK_METER_WIDTH
    assert PEAK_METER_WIDTH == 10
    assert peak_meter_bar_geometry(PEAK_METER_WIDTH) == (4.0, 2.0)
    assert minimum_height == natural_height == PEAK_METER_HEIGHT
    assert PEAK_METER_HEIGHT == AUDIO_TRACK_HEIGHT + TRACK_ROW_BORDER
    assert natural_height == AUDIO_TRACK_HEIGHT + TRACK_ROW_BORDER

    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")
    for semantic_color in ("success_color", "warning_color", "error_color"):
        assert f'area, "{semantic_color}"' in source
    assert "white = Gdk.RGBA(" in source


def test_editor_peak_meter_keeps_the_original_compact_vertical_range():
    range_rect = Gdk.Rectangle()
    range_rect.x = 0
    range_rect.y = 0
    range_rect.width = 18
    range_rect.height = 51
    range_widget = type(
        "RangeWidget",
        (),
        {
            "get_range_rect": lambda _self: range_rect,
            "get_height": lambda _self: 51,
        },
    )()
    meter = StereoPeakMeter(range_widget)

    assert meter._track_bounds(PEAK_METER_HEIGHT) == (12.0, 51.0)
    assert StereoPeakMeter()._track_bounds(PEAK_METER_HEIGHT) == (12.0, 51.0)
    assert PEAK_METER_VERTICAL_MARGIN == 12

    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")
    assert "StereoPeakMeter()" in source


def test_scroll_containers_leave_clearance_for_card_shadows():
    expected_margin_targets = {
        "audio_tracks_view.py": ("content_box", "settings_box"),
        "clips_view.py": ("self", "controls_box", "self.clips_list"),
        "dialogs.py": ("self.process_list", "self.games_list"),
        "settings_view.py": ("content_box", "settings_box"),
        "whitelist_view.py": ("self", "controls_box", "self.games_list"),
    }

    for filename, targets in expected_margin_targets.items():
        source = (REPO_ROOT / "ui" / filename).read_text(encoding="utf-8")
        for target in targets:
            assert f"{target}.set_margin_start(6)" in source, (filename, target)
            assert f"{target}.set_margin_end(6)" in source, (filename, target)

    for filename, target in (
        ("audio_tracks_view.py", "settings_box"),
        ("clips_view.py", "self.clips_list"),
        ("dialogs.py", "self.process_list"),
        ("dialogs.py", "self.games_list"),
        ("settings_view.py", "settings_box"),
        ("whitelist_view.py", "self.games_list"),
    ):
        source = (REPO_ROOT / "ui" / filename).read_text(encoding="utf-8")
        assert f"{target}.set_margin_top(6)" in source, (filename, target)
        assert f"{target}.set_margin_bottom(6)" in source, (filename, target)


def test_clip_and_game_metadata_use_restrained_value_highlights():
    window_source = (REPO_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    clips_source = (REPO_ROOT / "ui" / "clips_view.py").read_text(encoding="utf-8")
    games_source = (REPO_ROOT / "ui" / "whitelist_view.py").read_text(encoding="utf-8")

    assert ".clipper-metadata-chip" in window_source
    assert ".clipper-accent-chip" in window_source
    assert 'duration_label.add_css_class("clipper-accent-chip")' in clips_source
    assert 'game_box.add_css_class("clipper-metadata-chip")' in clips_source
    assert 'source_box.add_css_class("clipper-metadata-chip")' in games_source
    assert 'capture_label.add_css_class("clipper-accent-chip")' in games_source
    assert 'add_button.add_css_class("suggested-action")' in games_source
