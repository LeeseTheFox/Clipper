"""Compact native GTK video player for recorded clips."""

from __future__ import annotations

import argparse
import ctypes
import gc
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import gi
import i18n

# The player is launched as a standalone helper process, so it must initialize
# gettext before its module-level shortcut labels are constructed.
i18n.bootstrap()

from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")

try:
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland
except (ImportError, ValueError):
    GdkWayland = None

from app_icon import register_app_icon
from gi.repository import Adw, Gdk, Gio, GLib, Gst, GstPbutils, Gtk
from icon_names import (
    APP_ID,
    FULLSCREEN,
    HAMBURGER_MENU,
    PAUSE,
    PLAY,
    QUIT_FULLSCREEN,
    WARNING,
)
from playback_controls import (
    TRANSPORT_CSS,
    PlaybackControls,
    _matches_base_key,
)
from shortcut_dialog import (
    SHORTCUTS_ACCELERATOR,
    Shortcut,
    ShortcutSection,
    create_shortcuts_dialog,
    create_shortcuts_menu,
    matches_shortcuts_key,
)
from video_output import ClipperVideoSink, make_clipper_video_sink

_PLAYER_CSS_INSTALLED = False
_MAX_PLAYER_DIMENSION = 1280
_SEEK_PREVIEW_INTERVAL_SECONDS = 0.1
_SEEK_PREVIEW_MAX_DIMENSION = 1280
_SEEK_COMMIT_TIMEOUT_NS = 5 * Gst.SECOND
_GST_PLAY_FLAG_FORCE_SW_DECODERS = 1 << 12

PLAYER_SHORTCUT_SECTIONS = (
    ShortcutSection(
        _("Playback"),
        (
            Shortcut(_("Play or pause"), "space"),
            Shortcut(_("Seek backward 5 seconds"), "Left"),
            Shortcut(_("Seek forward 5 seconds"), "Right"),
            Shortcut(_("Raise volume"), "Up"),
            Shortcut(_("Lower volume"), "Down"),
            Shortcut(_("Mute or unmute"), "m"),
        ),
    ),
    ShortcutSection(
        _("Window"),
        (
            Shortcut(_("Toggle fullscreen"), "f"),
            Shortcut(_("Close or exit fullscreen"), "Escape"),
            Shortcut(_("Keyboard shortcuts"), SHORTCUTS_ACCELERATOR),
        ),
    ),
)

_PLAYER_CSS = TRANSPORT_CSS + """
.clipper-player-surface {
    background-color: #08090a;
}

.clipper-player-loading {
    padding: 10px 14px;
    border-radius: 9999px;
    color: white;
    background-color: alpha(black, 0.68);
    border: 1px solid alpha(white, 0.12);
    box-shadow: 0 3px 12px alpha(black, 0.4);
}

.clipper-player-timeline slider {
    min-width: 14px;
    min-height: 14px;
    margin: -5px;
    padding: 0;
    border-radius: 9999px;
    color: white;
    background-color: white;
    border: 1px solid alpha(black, 0.2);
    box-shadow: 0 1px 4px alpha(black, 0.45);
}

.clipper-player-error {
    background-color: var(--view-bg-color);
}
"""

try:
    _MALLOC_TRIM = getattr(ctypes.CDLL(None), "malloc_trim", None)
except OSError:
    _MALLOC_TRIM = None


def _reclaim_released_media_memory() -> bool:
    """Collect detached pipelines and return their large frame arenas to Linux."""
    gc.collect()
    if _MALLOC_TRIM is not None:
        try:
            _MALLOC_TRIM(0)
        except Exception:
            pass
    return GLib.SOURCE_REMOVE






def _install_player_css() -> None:
    """Install the small amount of styling around GTK's native video UI."""
    global _PLAYER_CSS_INSTALLED
    if _PLAYER_CSS_INSTALLED:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(_PLAYER_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _PLAYER_CSS_INSTALLED = True


def fit_video_window_size(
    source_width: int,
    source_height: int,
    chrome_height: int,
    *,
    max_dimension: int = _MAX_PLAYER_DIMENSION,
) -> tuple[int, int]:
    """Fit a video's natural size inside a square maximum, including window chrome."""
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Video dimensions must be positive")
    if max_dimension <= 0:
        raise ValueError("Maximum window dimension must be positive")

    chrome_height = min(max(0, chrome_height), max_dimension - 1)
    available_video_height = max_dimension - chrome_height
    scale = min(
        1.0,
        max_dimension / source_width,
        available_video_height / source_height,
    )
    video_width = max(1, round(source_width * scale))
    video_height = max(1, round(source_height * scale))
    return min(max_dimension, video_width), min(max_dimension, video_height + chrome_height)


def discover_video_dimensions(
    clip_path: Path,
    *,
    discoverer_factory: Callable[[int], object] = GstPbutils.Discoverer.new,
) -> tuple[int, int] | None:
    """Read display dimensions before presenting the player window."""
    path = Path(clip_path)
    if not path.is_file():
        return None

    Gst.init(None)
    try:
        discoverer = discoverer_factory(2 * Gst.SECOND)
        info = discoverer.discover_uri(path.resolve().as_uri())
        for stream in info.get_video_streams():
            width = int(stream.get_width())
            height = int(stream.get_height())
            if width <= 0 or height <= 0:
                continue
            par_numerator = int(stream.get_par_num()) or 1
            par_denominator = int(stream.get_par_denom()) or 1
            display_width = max(1, round(width * par_numerator / par_denominator))
            return display_width, height
    except Exception:
        return None
    return None


class VideoPlayerWindow(Adw.Window):
    """Play one clip at a time in a lightweight transient window."""

    DEFAULT_WIDTH = 720
    DEFAULT_HEIGHT = 450
    SEEK_STEP_NS = 5 * Gst.SECOND
    POSITION_UPDATE_MS = 100

    def __init__(
        self,
        transient_for=None,
        *,
        config=None,
        pipeline_factory: Callable[[], object] | None = None,
        sink_factory: Callable[[], object] | None = None,
        audio_sink_factory: Callable[[], object] | None = None,
        idle_add: Callable[..., int] = GLib.idle_add,
        closed_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(title=_("Clip player"), transient_for=transient_for)
        _install_player_css()

        if transient_for is not None:
            application = transient_for.get_application()
            if application is not None:
                self.set_application(application)

        self.set_default_size(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.set_size_request(420, 280)
        from window_state import WindowSizeManager

        self._window_size = WindowSizeManager(
            self,
            config,
            "player",
            minimum_size=(420, 280),
        )

        Gst.init(None)
        self._pipeline_factory = pipeline_factory or self._new_pipeline
        self._sink_factory = sink_factory or self._new_video_sink
        self._audio_sink_factory = audio_sink_factory or self._new_audio_sink
        self._idle_add = idle_add
        self._closed_callback = closed_callback
        self._load_source_id = None
        self._position_source_id = None
        self._load_generation = 0
        self._pipeline = None
        self._video_sink = None
        self._paintable_sink = None
        self._audio_sink = None
        self._bus = None
        self._bus_handler_id = None
        self._video_paintable = None
        self._paintable_size_handler_id = None
        self._video_dimensions = None
        self._duration_ns = 0
        self._updating_seek = False
        self._seek_range_dragging = False
        self._seek_press_pipeline = None
        self._resume_after_seek_press = False
        self._seek_drag_target_ns = None
        self._seek_worker_lock = threading.Lock()
        self._seek_worker_thread = None
        self._seek_worker_target_ns = None
        self._seek_worker_final = False
        self._seek_worker_resume = False
        self._seek_worker_generation = 0
        self._seek_worker_wakeup = threading.Event()
        self._seek_preview_pipeline = None
        self._seek_preview_sink = None
        self._seek_preview_sample_handler_id = None
        self._seek_preview_accept_frames = False
        self._ended = False
        self._clip_path = None
        self._closed = False

        self._build_ui()
        self._install_actions()
        self._install_keyboard_controls()
        self.connect("notify::fullscreened", self._on_fullscreen_changed)
        # A retained Adw.Window closes by hiding; close-request and destroy are
        # not emitted in that lifecycle. Stop playback on the signal that is
        # guaranteed for titlebar close, Escape, and programmatic close alike.
        self.connect("hide", self._on_hide)

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._header = header
        self._window_title = Adw.WindowTitle(title=_("Clip player"), subtitle="")
        header.set_title_widget(self._window_title)
        self._menu_button = Gtk.MenuButton()
        self._menu_button.set_icon_name(HAMBURGER_MENU)
        self._menu_button.set_tooltip_text(_("Player menu"))
        self._menu_button.set_menu_model(create_shortcuts_menu())
        header.pack_end(self._menu_button)
        toolbar.add_top_bar(header)

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        # Users may freely resize the window to any shape. Keep the complete
        # video visible and letterbox any space outside its aspect ratio.
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.add_css_class("clipper-player-surface")

        self._loading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._loading.set_halign(Gtk.Align.CENTER)
        self._loading.set_valign(Gtk.Align.CENTER)
        self._loading.set_can_target(False)
        self._loading.add_css_class("clipper-player-loading")

        spinner = Gtk.Spinner()
        spinner.start()
        self._loading.append(spinner)
        self._loading.append(Gtk.Label(label=_("Loading clip…")))

        video_overlay = Gtk.Overlay()
        video_overlay.add_css_class("clipper-player-surface")
        video_overlay.set_child(self._picture)
        video_overlay.add_overlay(self._loading)

        self._transport = PlaybackControls(
            on_play=self._toggle_playback,
            on_fullscreen=self._toggle_fullscreen,
            on_seek=self._on_seek_change_value,
            on_seek_drag=self._on_seek_css_classes_changed,
            on_volume=self._apply_volume,
            playing=True,
            can_mute=lambda: self._pipeline is not None,
        )
        self._controls_revealer = self._transport._controls_revealer
        self._controls = self._transport._controls
        self._play_button = self._transport._play_button
        self._time_label = self._transport._time_label
        self._seek_scale = self._transport._seek_scale
        self._control_spacer = self._transport._control_spacer
        self._volume_control = self._transport._volume_control
        self._volume_revealer = self._transport._volume_revealer
        self._volume_scale = self._transport._volume_scale
        self._mute_button = self._transport._mute_button
        self._fullscreen_button = self._transport._fullscreen_button
        video_overlay.add_overlay(self._controls_revealer)

        video_hover = Gtk.EventControllerMotion()
        video_hover.connect("enter", self._on_video_hover_enter)
        video_hover.connect("leave", self._on_video_hover_leave)
        video_overlay.add_controller(video_hover)

        video_click = Gtk.GestureClick()
        video_click.set_button(Gdk.BUTTON_PRIMARY)
        video_click.connect("pressed", self._on_video_pressed)
        self._picture.add_controller(video_click)

        player_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        player_box.append(video_overlay)

        self._error_page = Adw.StatusPage()
        self._error_page.set_icon_name(WARNING)
        self._error_page.add_css_class("clipper-player-error")

        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._content_stack.set_transition_duration(160)
        self._content_stack.add_named(player_box, "video")
        self._content_stack.add_named(self._error_page, "error")
        self._content_stack.set_visible_child_name("video")

        toolbar.set_content(self._content_stack)
        self.set_content(toolbar)

    def _install_actions(self) -> None:
        self._window_actions = Gio.SimpleActionGroup()
        shortcuts_action = Gio.SimpleAction.new("shortcuts", None)
        shortcuts_action.connect("activate", self._show_shortcuts)
        self._window_actions.add_action(shortcuts_action)
        self.insert_action_group("win", self._window_actions)

    def _show_shortcuts(self, *_args) -> None:
        dialog = create_shortcuts_dialog(PLAYER_SHORTCUT_SECTIONS)
        self._shortcuts_dialog = dialog
        dialog.present(self)

    def _install_keyboard_controls(self) -> None:
        self._key_controller = Gtk.EventControllerKey()
        # Capture Escape before media controls can consume it. This guarantees
        # there is always a keyboard route out of the player window.
        self._key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(self._key_controller)

    def open_clip(
        self,
        clip_path: Path,
        display_name: str | None = None,
        *,
        video_dimensions: tuple[int, int] | None = None,
    ) -> None:
        """Size for known metadata and queue playback for the next main-loop turn."""
        path = Path(clip_path)
        self._load_generation += 1
        generation = self._load_generation
        self._cancel_pending_load()
        self._release_media()

        self._clip_path = path
        self._video_dimensions = None
        title = str(display_name or path.name)
        self.set_title(_("%(title)s — Clipper") % {"title": title})
        self._window_title.set_title(title)
        self._window_title.set_subtitle(_("Clip playback"))
        self._loading.set_visible(True)
        self._controls_revealer.set_reveal_child(False)
        self._volume_revealer.set_reveal_child(False)
        self._transport.reset_volume()
        self._content_stack.set_visible_child_name("video")
        if video_dimensions is not None:
            self._apply_video_dimensions(*video_dimensions)

        self._load_source_id = self._idle_add(self._load_clip, path, generation)

    @staticmethod
    def _new_pipeline():
        return Gst.ElementFactory.make(
            "playbin3", "clipper-clip-player"
        ) or Gst.ElementFactory.make("playbin", "clipper-clip-player")

    @staticmethod
    def _new_video_sink():
        return make_clipper_video_sink("clipper-clip-player-sink")

    @staticmethod
    def _new_audio_sink():
        sink = Gst.ElementFactory.make("pulsesink", "clipper-clip-player-audio")
        if sink is None:
            return None
        # Give the helper its own PipeWire restore identity instead of sharing
        # Python's generic stream state with unrelated applications.
        sink.set_property("client-name", "Clipper player")
        return sink

    @staticmethod
    def _new_seek_preview_sink():
        """Return a bounded CPU-frame sink for non-blocking scrub previews."""
        preview_bin = Gst.Bin.new("clipper-seek-preview-bin")
        convert = Gst.ElementFactory.make("videoconvert")
        scale = Gst.ElementFactory.make("videoscale")
        caps_filter = Gst.ElementFactory.make("capsfilter")
        app_sink = Gst.ElementFactory.make("appsink")
        if any(
            element is None
            for element in (preview_bin, convert, scale, caps_filter, app_sink)
        ):
            return None, None

        # Keep frame conversion and the native-buffer copy away from GTK's
        # render thread, and cap the result to the player's maximum display
        # size. videoscale preserves the source display aspect ratio while it
        # fixes these bounded caps.
        caps_filter.set_property(
            "caps",
            Gst.Caps.from_string(
                "video/x-raw,format=RGBA,"
                f"width=(int)[1,{_SEEK_PREVIEW_MAX_DIMENSION}],"
                f"height=(int)[1,{_SEEK_PREVIEW_MAX_DIMENSION}],"
                "pixel-aspect-ratio=(fraction)1/1"
            ),
        )
        app_sink.set_property("emit-signals", True)
        app_sink.set_property("max-buffers", 1)
        app_sink.set_property("drop", True)
        app_sink.set_property("sync", False)
        app_sink.set_property("wait-on-eos", False)

        for element in (convert, scale, caps_filter, app_sink):
            preview_bin.add(element)
        if not (
            convert.link(scale)
            and scale.link(caps_filter)
            and caps_filter.link(app_sink)
        ):
            return None, None
        sink_pad = convert.get_static_pad("sink")
        if sink_pad is None or not preview_bin.add_pad(Gst.GhostPad.new("sink", sink_pad)):
            return None, None
        return preview_bin, app_sink

    @staticmethod
    def _new_seek_preview_pipeline():
        # The normal player intentionally uses playbin3 and hardware decode.
        # Scrubbing uses a separate playbin with software decoding so repeated
        # random seeks cannot monopolize the GPU that GTK needs to render the
        # pointer and slider at the display refresh rate.
        pipeline = Gst.ElementFactory.make("playbin", "clipper-seek-preview")
        if pipeline is not None:
            flags = int(pipeline.get_property("flags"))
            pipeline.set_property(
                "flags",
                flags | _GST_PLAY_FLAG_FORCE_SW_DECODERS,
            )
        return pipeline

    def _load_clip(self, path: Path, generation: int) -> bool:
        self._load_source_id = None
        if self._closed or generation != self._load_generation:
            return GLib.SOURCE_REMOVE

        if not path.is_file():
            self._show_error(
                _("Clip unavailable"), _("The video file could not be found.")
            )
            return GLib.SOURCE_REMOVE

        try:
            pipeline = self._pipeline_factory()
            sink_result = self._sink_factory()
            if isinstance(sink_result, ClipperVideoSink):
                sink = sink_result.element
                paintable_sink = sink_result.paintable_sink
            else:
                # Keep the injected factory contract used by focused player
                # tests and downstream callers that provide a direct sink.
                sink = sink_result
                paintable_sink = sink_result
            audio_sink = self._audio_sink_factory()
            if pipeline is None or sink is None or paintable_sink is None:
                raise RuntimeError("The GStreamer video player is unavailable")

            pipeline.set_property("video-sink", sink)
            if audio_sink is not None:
                pipeline.set_property("audio-sink", audio_sink)
            pipeline.set_property("uri", path.resolve().as_uri())
            # PipeWire may restore a mute value persisted by an earlier stream.
            # Every newly opened clip should begin audible.
            pipeline.set_property("volume", self._volume_scale.get_value())
            pipeline.set_property("mute", False)
            self._pipeline = pipeline
            self._video_sink = sink
            self._paintable_sink = paintable_sink
            self._audio_sink = audio_sink
            self._set_video_paintable(paintable_sink.get_property("paintable"))

            self._bus = pipeline.get_bus()
            self._bus.add_signal_watch()
            self._bus_handler_id = self._bus.connect("message", self._on_bus_message, generation)
            result = pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("GStreamer rejected the clip")

            self._ended = False
            self._set_playing_ui(True)
            self._position_source_id = GLib.timeout_add(
                self.POSITION_UPDATE_MS,
                self._update_position,
                generation,
            )
        except Exception as error:  # Media loading should fail inside the player window.
            print(f"Could not play clip {path.name}: {error}")
            self._release_media()
            self._show_playback_error()

        return GLib.SOURCE_REMOVE

    def _set_video_paintable(self, paintable) -> None:
        previous = self._video_paintable
        handler_id = self._paintable_size_handler_id
        self._video_paintable = paintable
        self._paintable_size_handler_id = None

        if previous is not None and handler_id is not None:
            try:
                previous.disconnect(handler_id)
            except Exception:
                pass

        self._picture.set_paintable(paintable)
        if paintable is None:
            return

        try:
            self._paintable_size_handler_id = paintable.connect(
                "invalidate-size", self._on_video_size_invalidated
            )
        except Exception:
            self._paintable_size_handler_id = None
        self._resize_for_video(paintable)

    def _on_video_size_invalidated(self, paintable) -> None:
        self._resize_for_video(paintable)

    def _measure_player_chrome_height(self, window_width: int) -> int:
        """Return the vertical space used by non-overlay window chrome."""
        allocated_window_height = self.get_height()
        allocated_video_height = self._picture.get_height()
        if allocated_window_height > 0 and allocated_video_height > 0:
            return max(0, allocated_window_height - allocated_video_height)

        _minimum, natural, _minimum_baseline, _natural_baseline = self._header.measure(
            Gtk.Orientation.VERTICAL, window_width
        )
        return natural

    def _on_video_hover_enter(self, *_args) -> None:
        self._controls_revealer.set_reveal_child(True)

    def _on_video_hover_leave(self, *_args) -> None:
        self._controls_revealer.set_reveal_child(False)

    def _on_volume_control_enter(self, *_args):
        return self._transport._on_volume_control_enter()

    def _on_volume_control_leave(self, *_args):
        return self._transport._on_volume_control_leave()

    def _on_video_pressed(self, _gesture, press_count, _x, _y) -> None:
        if press_count == 1:
            self._toggle_playback()
        elif press_count == 2:
            # GTK reports a double-click as press counts 1 and 2. Toggle on
            # both so fullscreen preserves the playback state from before the
            # gesture without delaying the first click.
            self._toggle_playback()
            self._toggle_fullscreen()

    def _resize_for_video(self, paintable) -> None:
        """Resize once the sink publishes the decoded video's natural dimensions."""
        try:
            source_width = int(paintable.get_intrinsic_width())
            source_height = int(paintable.get_intrinsic_height())
        except Exception:
            return
        if source_width <= 0 or source_height <= 0:
            return

        try:
            intrinsic_aspect_ratio = float(paintable.get_intrinsic_aspect_ratio())
        except Exception:
            intrinsic_aspect_ratio = 0
        if intrinsic_aspect_ratio > 0:
            source_width = max(1, round(source_height * intrinsic_aspect_ratio))

        self._apply_video_dimensions(source_width, source_height)

    def _apply_video_dimensions(self, source_width: int, source_height: int) -> None:
        """Apply known display dimensions to initial and runtime window sizing."""
        if source_width <= 0 or source_height <= 0:
            return
        dimensions = (source_width, source_height)
        if dimensions == self._video_dimensions:
            return

        # A remembered user size takes precedence over automatic first-open
        # sizing derived from the video's aspect ratio.
        if self._window_size.restored:
            self._video_dimensions = dimensions
            return

        chrome_height = self._measure_player_chrome_height(source_width)
        window_width, window_height = fit_video_window_size(
            source_width,
            source_height,
            chrome_height,
        )
        self._video_dimensions = dimensions
        self.set_default_size(window_width, window_height)

    def _on_bus_message(self, _bus, message, generation: int) -> None:
        if self._closed or generation != self._load_generation:
            return

        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            clip_name = self._clip_path.name if self._clip_path is not None else "clip"
            print(f"Could not play clip {clip_name}: {error}")
            self._release_media()
            self._show_playback_error()
            return

        if message.type in (Gst.MessageType.ASYNC_DONE, Gst.MessageType.DURATION_CHANGED):
            self._loading.set_visible(False)
            self._content_stack.set_visible_child_name("video")
            self._update_position(generation)
        elif message.type == Gst.MessageType.EOS:
            self._ended = True
            self._set_playing_ui(False)
            self._update_position(generation)
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == self._pipeline:
            _old_state, new_state, _pending = message.parse_state_changed()
            self._set_playing_ui(new_state == Gst.State.PLAYING)

    def _update_position(self, generation: int) -> bool:
        pipeline = self._pipeline
        if self._closed or generation != self._load_generation or pipeline is None:
            self._position_source_id = None
            return GLib.SOURCE_REMOVE

        position_ok, position_ns = pipeline.query_position(Gst.Format.TIME)
        duration_ok, duration_ns = pipeline.query_duration(Gst.Format.TIME)
        if duration_ok and duration_ns > 0:
            self._duration_ns = duration_ns
            self._seek_scale.set_sensitive(True)
        if not position_ok:
            position_ns = 0

        duration_ns = self._duration_ns
        if self._seek_press_pipeline is None:
            self._updating_seek = True
            try:
                fraction = position_ns / duration_ns if duration_ns > 0 else 0
                self._seek_scale.set_value(max(0, min(1000, fraction * 1000)))
            finally:
                self._updating_seek = False
        displayed_position_ns = (
            self._seek_drag_target_ns
            if self._seek_press_pipeline is not None
            and self._seek_drag_target_ns is not None
            else position_ns
        )
        self._time_label.set_label(
            f"{self._format_time(displayed_position_ns)} / "
            f"{self._format_time(duration_ns)}"
        )
        self._loading.set_visible(False)
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _format_time(value_ns: int) -> str:
        seconds = max(0, int(value_ns // Gst.SECOND))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _on_seek_change_value(self, _scale, _scroll, value) -> bool:
        if self._updating_seek or self._pipeline is None or self._duration_ns <= 0:
            return False
        target_ns = round(self._duration_ns * max(0, min(1000, value)) / 1000)
        if self._seek_range_dragging:
            # The scale paints this value immediately. Preview decoder work is
            # coalesced on a worker so it cannot stall GTK's pointer/render path.
            self._seek_drag_target_ns = target_ns
            self._time_label.set_label(
                f"{self._format_time(target_ns)} / "
                f"{self._format_time(self._duration_ns)}"
            )
            self._queue_seek_work(target_ns, final=False)
        else:
            self._seek_to(target_ns)
        return False

    def _queue_seek_work(self, target_ns: int, *, final: bool) -> None:
        pipeline = self._seek_press_pipeline
        if pipeline is None or pipeline is not self._pipeline:
            return

        self._ended = False
        with self._seek_worker_lock:
            if self._seek_worker_final and not final:
                return
            self._seek_worker_target_ns = target_ns
            if final:
                self._seek_worker_final = True
                self._seek_worker_resume = self._resume_after_seek_press
            if self._seek_worker_thread is not None:
                if final:
                    self._seek_worker_wakeup.set()
                return

            generation = self._seek_worker_generation
            self._seek_worker_wakeup.clear()
            worker = threading.Thread(
                target=self._run_seek_worker,
                args=(pipeline, generation),
                name="clipper-player-seek",
                daemon=True,
            )
            self._seek_worker_thread = worker
        worker.start()

    def _run_seek_worker(self, pipeline, generation: int) -> None:
        # On Linux, niceness is per thread and is inherited by the decoder
        # threads GStreamer creates from here. Preview decoding must yield to
        # GTK/compositor rendering under load; failure is harmless on systems
        # that do not expose setpriority with these semantics.
        try:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
        except (AttributeError, OSError):
            pass
        while True:
            with self._seek_worker_lock:
                if generation != self._seek_worker_generation:
                    return
                target_ns = self._seek_worker_target_ns
                final = self._seek_worker_final
                resume = self._seek_worker_resume
                self._seek_worker_target_ns = None
                if target_ns is None:
                    self._seek_worker_thread = None
                    return

            flags = (
                Gst.SeekFlags.ACCURATE
                if final
                else Gst.SeekFlags.KEY_UNIT
                | Gst.SeekFlags.TRICKMODE_KEY_UNITS
                | Gst.SeekFlags.TRICKMODE_NO_AUDIO
            )
            try:
                if final:
                    seek_ok = self._seek_pipeline_exact(pipeline, target_ns)
                    if seek_ok:
                        # playbin3 accepts seeks asynchronously. Keep the main
                        # pipeline paused until every sink has flushed and
                        # prerolled at the new position; otherwise an old
                        # position query or an incompletely reset A/V clock can
                        # escape into resumed playback.
                        pipeline.get_state(_SEEK_COMMIT_TIMEOUT_NS)
                else:
                    preview_pipeline = self._ensure_seek_preview_pipeline(generation)
                    if preview_pipeline is not None:
                        with self._seek_worker_lock:
                            if generation != self._seek_worker_generation:
                                return
                            if self._seek_worker_final:
                                continue
                            if self._seek_worker_target_ns is not None:
                                target_ns = self._seek_worker_target_ns
                                self._seek_worker_target_ns = None
                            self._seek_preview_accept_frames = True
                        preview_ok = preview_pipeline.seek_simple(
                            Gst.Format.TIME,
                            Gst.SeekFlags.FLUSH | flags,
                            target_ns,
                        )
                        if preview_ok:
                            preview_pipeline.get_state(2 * Gst.SECOND)
            except Exception:
                pass

            if final:
                with self._seek_worker_lock:
                    if generation != self._seek_worker_generation:
                        return
                    if self._seek_worker_target_ns is not None:
                        # A release from a newer drag arrived while this seek
                        # was prerolling. It supersedes this completion, so
                        # serialize the newer exact seek before any resume.
                        continue
                    self._seek_worker_thread = None
                    self._seek_worker_final = False
                    self._seek_worker_resume = False
                    self._seek_worker_wakeup.clear()
                self._release_seek_preview_pipeline(generation)
                GLib.idle_add(
                    self._finish_seek_hold,
                    pipeline,
                    generation,
                    resume,
                )
                return

            # Let the decoder/presentation path settle between previews. New
            # pointer values keep replacing the queued target without waking
            # this wait; a final release does wake it for an immediate exact seek.
            self._seek_worker_wakeup.wait(_SEEK_PREVIEW_INTERVAL_SECONDS)
            self._seek_worker_wakeup.clear()

    def _ensure_seek_preview_pipeline(self, generation: int):
        """Build the non-GTK preview decoder from the seek worker."""
        with self._seek_worker_lock:
            if generation != self._seek_worker_generation:
                return None
            if self._seek_preview_pipeline is not None:
                return self._seek_preview_pipeline
            clip_path = self._clip_path
        if clip_path is None:
            return None

        preview_pipeline = None
        preview_sink = None
        sample_handler_id = None
        try:
            preview_pipeline = self._new_seek_preview_pipeline()
            preview_video_sink, preview_sink = self._new_seek_preview_sink()
            audio_sink = Gst.ElementFactory.make("fakesink")
            if (
                preview_pipeline is None
                or preview_video_sink is None
                or preview_sink is None
                or audio_sink is None
            ):
                raise RuntimeError("The GStreamer scrub preview is unavailable")
            preview_pipeline.set_property("video-sink", preview_video_sink)
            preview_pipeline.set_property("audio-sink", audio_sink)
            preview_pipeline.set_property("uri", clip_path.resolve().as_uri())
            preview_pipeline.set_property("mute", True)
            sample_handler_id = preview_sink.connect(
                "new-preroll",
                self._on_seek_preview_preroll,
                preview_pipeline,
                generation,
            )
            result = preview_pipeline.set_state(Gst.State.PAUSED)
            if result == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("GStreamer rejected the scrub preview")
            _result, state, _pending = preview_pipeline.get_state(2 * Gst.SECOND)
            if state != Gst.State.PAUSED:
                raise RuntimeError("GStreamer did not prepare the scrub preview")
        except Exception:
            if preview_sink is not None and sample_handler_id is not None:
                try:
                    preview_sink.disconnect(sample_handler_id)
                except Exception:
                    pass
            if preview_pipeline is not None:
                try:
                    preview_pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
            return None

        with self._seek_worker_lock:
            if generation != self._seek_worker_generation:
                stale = True
            else:
                stale = False
                self._seek_preview_pipeline = preview_pipeline
                self._seek_preview_sink = preview_sink
                self._seek_preview_sample_handler_id = sample_handler_id
                self._seek_preview_accept_frames = False
        if stale:
            preview_sink.disconnect(sample_handler_id)
            preview_pipeline.set_state(Gst.State.NULL)
            return None

        return preview_pipeline

    def _on_seek_preview_preroll(
        self,
        preview_sink,
        preview_pipeline,
        generation: int,
    ):
        """Copy a completed preview frame without touching any GTK object."""
        sample = preview_sink.emit("pull-preroll")
        if sample is None:
            return Gst.FlowReturn.OK
        with self._seek_worker_lock:
            accept_frame = (
                generation == self._seek_worker_generation
                and preview_pipeline is self._seek_preview_pipeline
                and self._seek_preview_accept_frames
            )
        if not accept_frame:
            return Gst.FlowReturn.OK

        try:
            caps = sample.get_caps()
            structure = caps.get_structure(0)
            width = int(structure.get_value("width"))
            height = int(structure.get_value("height"))
            buffer = sample.get_buffer()
            mapped, map_info = buffer.map(Gst.MapFlags.READ)
            if not mapped:
                return Gst.FlowReturn.OK
            try:
                frame_bytes = bytes(map_info.data)
            finally:
                buffer.unmap(map_info)
            stride = len(frame_bytes) // height
            if width <= 0 or height <= 0 or stride < width * 4:
                return Gst.FlowReturn.OK
            # GBytes owns the immutable CPU frame across the thread handoff;
            # constructing the small GdkTexture wrapper is all GTK must do.
            frame_data = GLib.Bytes.new(frame_bytes)
        except Exception:
            return Gst.FlowReturn.OK

        GLib.idle_add(
            self._publish_seek_preview_frame,
            frame_data,
            width,
            height,
            stride,
            preview_pipeline,
            generation,
        )
        return Gst.FlowReturn.OK

    def _publish_seek_preview_frame(
        self,
        frame_data,
        width: int,
        height: int,
        stride: int,
        preview_pipeline,
        generation: int,
    ) -> bool:
        if (
            generation != self._seek_worker_generation
            or preview_pipeline is not self._seek_preview_pipeline
            or not self._seek_range_dragging
        ):
            return GLib.SOURCE_REMOVE
        frame = Gdk.MemoryTexture.new(
            width,
            height,
            Gdk.MemoryFormat.R8G8B8A8,
            frame_data,
            stride,
        )
        self._picture.set_paintable(frame)
        return GLib.SOURCE_REMOVE

    def _release_seek_preview_pipeline(self, generation: int) -> None:
        with self._seek_worker_lock:
            if generation != self._seek_worker_generation:
                return
            preview_pipeline = self._seek_preview_pipeline
            preview_sink = self._seek_preview_sink
            sample_handler_id = self._seek_preview_sample_handler_id
            self._seek_preview_pipeline = None
            self._seek_preview_sink = None
            self._seek_preview_sample_handler_id = None
            self._seek_preview_accept_frames = False
        if preview_sink is not None and sample_handler_id is not None:
            try:
                preview_sink.disconnect(sample_handler_id)
            except Exception:
                pass
        if preview_pipeline is not None:
            try:
                preview_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

    def _finish_seek_hold(self, pipeline, generation: int, resume: bool) -> bool:
        if (
            generation != self._seek_worker_generation
            or pipeline is not self._pipeline
            or pipeline is not self._seek_press_pipeline
        ):
            return GLib.SOURCE_REMOVE
        with self._seek_worker_lock:
            seek_work_pending = (
                self._seek_worker_thread is not None
                or self._seek_worker_final
                or self._seek_worker_target_ns is not None
            )
        if self._seek_range_dragging or seek_work_pending:
            # A quick re-grab turns the previous release into an intermediate
            # seek. Its completion must never clear or resume the newer hold.
            return GLib.SOURCE_REMOVE

        self._seek_press_pipeline = None
        self._resume_after_seek_press = False
        self._picture.set_paintable(self._video_paintable)
        if resume:
            pipeline.set_state(Gst.State.PLAYING)
            self._set_playing_ui(True)
        return GLib.SOURCE_REMOVE

    def _on_seek_css_classes_changed(self, scale, _property) -> None:
        # GtkRange adds this class when it takes ownership of the slider press
        # and removes it from the same release/cancellation cleanup path. Use
        # both edges so pause and resume cannot observe different gestures.
        dragging = scale.has_css_class("dragging")
        if dragging == self._seek_range_dragging:
            return
        self._seek_range_dragging = dragging
        if dragging:
            self._begin_seek_hold()
        else:
            self._end_seek_hold()

    def _begin_seek_hold(self) -> None:
        pipeline = self._pipeline
        if pipeline is None or self._seek_press_pipeline is not None:
            return

        self._seek_press_pipeline = pipeline
        _result, state, pending = pipeline.get_state(0)
        self._resume_after_seek_press = (
            state == Gst.State.PLAYING or pending == Gst.State.PLAYING
        )
        if self._resume_after_seek_press:
            pipeline.set_state(Gst.State.PAUSED)
            self._set_playing_ui(False)

    def _end_seek_hold(self) -> None:
        pressed_pipeline = self._seek_press_pipeline
        resume = self._resume_after_seek_press
        final_target_ns = self._seek_drag_target_ns
        self._seek_drag_target_ns = None
        if final_target_ns is not None and self._pipeline is pressed_pipeline:
            self._queue_seek_work(final_target_ns, final=True)
            return
        self._finish_seek_hold(
            pressed_pipeline,
            self._seek_worker_generation,
            resume,
        )

    def _seek_to(self, target_ns: int) -> bool:
        pipeline = self._pipeline
        if pipeline is None:
            return False
        target_ns = max(0, min(self._duration_ns or target_ns, target_ns))
        self._ended = False
        return self._seek_pipeline_exact(pipeline, target_ns)

    @staticmethod
    def _seek_pipeline_exact(pipeline, target_ns: int) -> bool:
        """Start a normal-rate exact seek with an unbounded playback segment."""
        return bool(
            pipeline.seek(
                1.0,
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                Gst.SeekType.SET,
                target_ns,
                Gst.SeekType.NONE,
                # PyGObject exposes CLOCK_TIME_NONE as unsigned UINT64_MAX,
                # while Gst.Element.seek() takes a signed gint64 here.
                -1,
            )
        )

    def _seek_relative(self, offset_ns: int) -> bool:
        pipeline = self._pipeline
        if pipeline is None:
            return False
        success, position_ns = pipeline.query_position(Gst.Format.TIME)
        if not success:
            return False
        return self._seek_to(position_ns + offset_ns)

    def _toggle_playback(self) -> None:
        pipeline = self._pipeline
        if pipeline is None or self._seek_press_pipeline is pipeline:
            return
        _result, state, _pending = pipeline.get_state(0)
        if state == Gst.State.PLAYING:
            pipeline.set_state(Gst.State.PAUSED)
            self._set_playing_ui(False)
            return
        if self._ended:
            self._seek_to(0)
        pipeline.set_state(Gst.State.PLAYING)
        self._set_playing_ui(True)

    def _set_playing_ui(self, playing: bool) -> None:
        self._play_button.set_icon_name(PAUSE if playing else PLAY)
        self._play_button.set_tooltip_text(_("Pause") if playing else _("Play"))

    def _apply_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.set_property("volume", volume)
            pipeline.set_property("mute", volume == 0)

    def _toggle_muted(self, _button=None):
        if self._pipeline is None:
            return
        return self._transport._toggle_muted()

    def _change_volume(self, direction):
        return self._transport._change_volume(direction)

    def _on_mute_button_scroll(self, _controller, _dx, dy):
        return self._transport._on_mute_button_scroll(_controller, _dx, dy)

    def _toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_args) -> None:
        fullscreen = self.is_fullscreen()
        self._header.set_visible(not fullscreen)
        self._fullscreen_button.set_icon_name(QUIT_FULLSCREEN if fullscreen else FULLSCREEN)
        self._fullscreen_button.set_tooltip_text(
            _("Exit fullscreen") if fullscreen else _("Fullscreen")
        )

    def _show_playback_error(self) -> None:
        self._show_error(
            _("Couldn’t play this clip"),
            _("The file may use an unsupported video or audio format."),
        )

    def _show_error(self, title: str, description: str) -> None:
        self._loading.set_visible(False)
        self._error_page.set_title(title)
        self._error_page.set_description(description)
        self._content_stack.set_visible_child_name("error")

    def _on_key_pressed(self, _controller, keyval, keycode, _state) -> bool:
        if matches_shortcuts_key(keyval, keycode, _state):
            return self.activate_action("win.shortcuts", None)

        if keyval == Gdk.KEY_Escape:
            if self.is_fullscreen():
                self.unfullscreen()
                return True
            self.set_visible(False)
            return True

        if self._pipeline is None:
            return False

        if _matches_base_key(keyval, keycode, Gdk.KEY_m):
            self._toggle_muted()
            return True

        if _matches_base_key(keyval, keycode, Gdk.KEY_f):
            self._toggle_fullscreen()
            return True

        if keyval == Gdk.KEY_space:
            self._toggle_playback()
            return True

        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            direction = -1 if keyval == Gdk.KEY_Left else 1
            self._seek_relative(direction * self.SEEK_STEP_NS)
            return True

        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_KP_Up, Gdk.KEY_KP_Down):
            direction = 1 if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up) else -1
            self._change_volume(direction)
            return True

        return False

    def close(self) -> None:
        """Close through the same deterministic hide-and-stop path as the titlebar."""
        self.set_visible(False)

    def _cancel_pending_load(self) -> None:
        if self._load_source_id is None:
            return
        GLib.source_remove(self._load_source_id)
        self._load_source_id = None

    def _release_media(self) -> None:
        if self._position_source_id is not None:
            GLib.source_remove(self._position_source_id)
            self._position_source_id = None

        pipeline = self._pipeline
        bus = self._bus
        bus_handler_id = self._bus_handler_id
        with self._seek_worker_lock:
            preview_pipeline = self._seek_preview_pipeline
            preview_sink = self._seek_preview_sink
            sample_handler_id = self._seek_preview_sample_handler_id
            self._seek_worker_generation += 1
            self._seek_worker_thread = None
            self._seek_worker_target_ns = None
            self._seek_worker_final = False
            self._seek_worker_resume = False
            self._seek_worker_wakeup.set()
            self._seek_preview_pipeline = None
            self._seek_preview_sink = None
            self._seek_preview_sample_handler_id = None
            self._seek_preview_accept_frames = False
        self._seek_range_dragging = False
        self._seek_press_pipeline = None
        self._resume_after_seek_press = False
        self._seek_drag_target_ns = None
        self._pipeline = None
        self._video_sink = None
        self._paintable_sink = None
        self._audio_sink = None
        self._bus = None
        self._bus_handler_id = None
        self._duration_ns = 0
        try:
            self._set_video_paintable(None)
        except Exception:
            pass

        if bus is not None:
            if bus_handler_id is not None:
                try:
                    bus.disconnect(bus_handler_id)
                except Exception:
                    pass
            try:
                bus.remove_signal_watch()
            except Exception:
                pass

        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:  # The window must still close after backend failure.
                pass
        if preview_sink is not None and sample_handler_id is not None:
            try:
                preview_sink.disconnect(sample_handler_id)
            except Exception:
                pass
        if preview_pipeline is not None:
            try:
                preview_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

        # PyGObject and GStreamer form collectible cycles, while glibc keeps
        # freed decoder arenas for reuse by default. Reclaim both on idle,
        # after this method's last strong pipeline references have gone away.
        GLib.idle_add(_reclaim_released_media_memory)

    def _on_hide(self, *_args) -> None:
        """Stop all playback whenever the retained player window is hidden."""
        if self._closed_callback is not None:
            self._closed_callback()
            return
        self._load_generation += 1
        self._cancel_pending_load()
        self._release_media()


def _parent_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 1 or os.getppid() != parent_pid:
        return False
    try:
        os.kill(parent_pid, 0)
    except OSError:
        return False
    return True


def _configure_desktop_identity() -> None:
    """Make the isolated process group with Clipper in Wayland shells."""
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Clipper")


def _set_exported_parent(window, parent_handle: str) -> bool:
    """Relate the helper to Clipper so the compositor places it over its parent."""
    if not parent_handle or GdkWayland is None:
        return False
    try:
        window.realize()
        surface = window.get_surface()
        if not isinstance(surface, GdkWayland.WaylandToplevel):
            return False
        return bool(surface.set_transient_for_exported(parent_handle))
    except Exception:
        return False


def run_player_helper(
    clip_path: Path,
    display_name: str,
    parent_pid: int,
    parent_handle: str = "",
) -> int:
    """Run the player in an isolated process with fail-safe close semantics."""
    _configure_desktop_identity()
    video_dimensions = discover_video_dimensions(clip_path)
    application = Adw.Application(
        # NON_UNIQUE preserves process isolation while the canonical ID makes
        # the compositor match Clipper's desktop file, icon, and taskbar group.
        application_id=APP_ID,
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    holder = {}
    from config import ClipperConfig

    config = ClipperConfig()

    def exit_player() -> None:
        # This process owns only ephemeral playback state. Exiting here lets
        # Linux and PipeWire tear down every decoder and audio stream even if
        # a GStreamer state transition would otherwise deadlock.
        os._exit(0)

    def on_activate(app) -> None:
        register_app_icon()
        window = holder.get("window")
        if window is not None:
            window.present()
            return
        window = VideoPlayerWindow(config=config, closed_callback=exit_player)
        window.set_application(app)
        _set_exported_parent(window, parent_handle)
        holder["window"] = window
        window.open_clip(
            clip_path,
            display_name,
            video_dimensions=video_dimensions,
        )
        window.present()

        def check_parent() -> bool:
            if not _parent_is_alive(parent_pid):
                exit_player()
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add_seconds(1, check_parent)

    application.connect("activate", on_activate)
    return application.run([])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=_("Play a Clipper video"))
    parser.add_argument("clip_path", type=Path)
    parser.add_argument("--title", default="")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-handle", default="")
    args = parser.parse_args(argv)
    return run_player_helper(
        args.clip_path,
        args.title or args.clip_path.name,
        args.parent_pid,
        args.parent_handle,
    )


if __name__ == "__main__":
    # Avoid interpreter/GObject finalization retaining or blocking on a media
    # backend. The helper owns no persistent data, so process exit is cleanup.
    os._exit(main(sys.argv[1:]))
