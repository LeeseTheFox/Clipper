"""Shared playback transport widgets and volume interaction for video windows."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk
from i18n import _
from icon_names import FULLSCREEN, PAUSE, PLAY, VOLUME, VOLUME_CROSS, VOLUME_MEDIUM

_VOLUME_STEP = 0.05
_VOLUME_SCROLL_STEP = 0.1

TRANSPORT_CSS = """
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

"""


def _volume_icon_name(volume: float) -> str:
    """Return the requested high, non-high, or muted volume icon."""
    volume = max(0.0, min(1.0, float(volume)))
    if volume == 0:
        return VOLUME_CROSS
    if volume <= 2 / 3:
        return VOLUME_MEDIUM
    return VOLUME


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


def _matches_base_key(keyval, keycode, *base_keyvals) -> bool:
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


class PlaybackControls:
    """Build the player controls with callbacks supplied by each playback backend.

    The owner places the revealer and controls seek/fullscreen lifecycle. Volume
    state belongs here so slider, keyboard and mute always follow the same rules.
    """

    def __init__(
        self, *, on_play, on_fullscreen, on_seek, on_seek_drag, on_volume,
        playing=False, time_text="0:00 / 0:00", seek_sensitive=False,
        seek_visible=True, can_mute=lambda: True,
    ):
        self._updating_volume = False
        self._volume_before_mute = None
        self._on_volume = on_volume
        self._can_mute = can_mute
        self._controls_revealer = Gtk.Revealer()
        self._controls_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self._controls_revealer.set_transition_duration(180)
        self._controls_revealer.set_reveal_child(False)
        self._controls_revealer.set_halign(Gtk.Align.FILL)
        self._controls_revealer.set_valign(Gtk.Align.END)
        self._controls_revealer.set_margin_start(16)
        self._controls_revealer.set_margin_end(16)
        self._controls_revealer.set_margin_bottom(16)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._controls = controls
        controls.add_css_class("clipper-player-controls")
        controls.add_css_class("osd")

        self._play_button = Gtk.Button.new_from_icon_name(PAUSE if playing else PLAY)
        self._play_button.set_tooltip_text(_("Pause") if playing else _("Play"))
        self._play_button.add_css_class("circular")
        self._play_button.add_css_class("flat")
        self._play_button.add_css_class("clipper-player-control-button")
        self._play_button.connect("clicked", lambda _button: on_play())
        controls.append(self._play_button)

        self._time_label = Gtk.Label(label=time_text)
        self._time_label.set_halign(Gtk.Align.START)
        self._time_label.set_valign(Gtk.Align.CENTER)
        self._time_label.set_xalign(0)
        self._time_label.add_css_class("clipper-player-time")
        controls.append(self._time_label)

        self._seek_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self._seek_scale.set_draw_value(False)
        self._seek_scale.set_hexpand(True)
        self._seek_scale.set_valign(Gtk.Align.CENTER)
        self._seek_scale.set_sensitive(seek_sensitive)
        self._seek_scale.set_visible(seek_visible)
        self._seek_scale.add_css_class("clipper-player-timeline")
        self._seek_scale.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Playback position")]
        )
        self._seek_scale.connect("change-value", on_seek)
        _disable_hold_to_fine_tune(self._seek_scale)
        _prevent_scroll_seeking(self._seek_scale)
        self._seek_scale.connect(
            "notify::css-classes", on_seek_drag
        )
        controls.append(self._seek_scale)

        self._control_spacer = Gtk.Box()
        self._control_spacer.set_hexpand(True)
        self._control_spacer.set_visible(not seek_visible)
        controls.append(self._control_spacer)

        self._volume_control = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        self._volume_control.set_valign(Gtk.Align.CENTER)

        self._volume_revealer = Gtk.Revealer()
        self._volume_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
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
            [_("Volume")],
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
        self._fullscreen_button.connect("clicked", lambda _button: on_fullscreen())
        controls.append(self._fullscreen_button)

        self._controls_revealer.set_child(controls)

    def reset_volume(self):
        self._volume_before_mute = None
        self._set_volume(1)

    def _apply_volume(self, volume):
        self._on_volume(volume)
        self._set_volume_ui(volume)

    def _on_volume_control_enter(self, *_args) -> None:
        self._volume_revealer.set_reveal_child(True)

    def _on_volume_control_leave(self, *_args) -> None:
        self._volume_revealer.set_reveal_child(False)

    def _on_volume_changed(self, scale) -> None:
        if self._updating_volume:
            return

        volume = max(0.0, min(1.0, float(scale.get_value())))
        # Reaching zero through the slider is an explicit manual mute. Unlike
        # the mute button, it intentionally has no earlier value to restore.
        self._volume_before_mute = None
        self._apply_volume(volume)

    def _set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        self._updating_volume = True
        try:
            self._volume_scale.set_value(volume)
        finally:
            self._updating_volume = False
        self._apply_volume(volume)

    def _toggle_muted(self, _button=None) -> None:
        if not self._can_mute():
            return
        volume = max(0.0, min(1.0, float(self._volume_scale.get_value())))
        if volume == 0:
            restore_volume = self._volume_before_mute or 1.0
            self._volume_before_mute = None
            self._set_volume(restore_volume)
            return

        self._volume_before_mute = volume
        self._set_volume(0)

    def _change_volume(self, direction: int) -> None:
        volume = self._volume_scale.get_value()
        self._volume_scale.set_value(
            max(0.0, min(1.0, round(volume + direction * _VOLUME_STEP, 2)))
        )

    def _on_mute_button_scroll(self, _controller, _dx, dy) -> bool:
        if dy:
            volume = self._volume_scale.get_value()
            direction = 1 if dy < 0 else -1
            self._volume_scale.set_value(
                max(0.0, min(1.0, round(volume + direction * _VOLUME_SCROLL_STEP, 2)))
            )
        return True

    def _set_volume_ui(self, volume: float) -> None:
        muted = volume <= 0
        self._mute_button.set_icon_name(_volume_icon_name(volume))
        self._mute_button.set_tooltip_text(_("Unmute") if muted else _("Mute"))
