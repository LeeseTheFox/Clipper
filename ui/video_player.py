"""Reusable native GTK video player for recorded clips."""

from __future__ import annotations

import ctypes
import gc
import os
import threading
from collections.abc import Callable
from pathlib import Path

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gst, Gtk
from icon_names import (
    BACK,
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


class VideoPlayer(Gtk.Box):
    """Play one clip at a time inside the owning application window."""

    __gsignals__ = {
        "prepared": (GObject.SignalFlags.RUN_LAST, None, ()),
        "content-ready": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    SEEK_STEP_NS = 5 * Gst.SECOND
    POSITION_UPDATE_MS = 100

    def __init__(
        self,
        *,
        pipeline_factory: Callable[[], object] | None = None,
        sink_factory: Callable[[], object] | None = None,
        audio_sink_factory: Callable[[], object] | None = None,
        idle_add: Callable[..., int] = GLib.idle_add,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _install_player_css()
        self.set_hexpand(True)
        self.set_vexpand(True)

        Gst.init(None)
        self._pipeline_factory = pipeline_factory or self._new_pipeline
        self._sink_factory = sink_factory or self._new_video_sink
        self._audio_sink_factory = audio_sink_factory or self._new_audio_sink
        self._idle_add = idle_add
        self._close_callback = close_callback
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
        self._paintable_contents_handler_id = None
        self._has_video_frame = False
        self._autoplay = True
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

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        back_button = Gtk.Button(icon_name=BACK, tooltip_text=_("Back"))
        back_button.connect("clicked", lambda _button: self.close())
        header.pack_start(back_button)
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
        self._picture.set_focusable(True)
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
        self.append(toolbar)

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
        # there is always a keyboard route back to the gallery.
        self._key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(self._key_controller)

    def open_clip(
        self,
        clip_path: Path,
        display_name: str | None = None,
        *,
        autoplay: bool = True,
    ) -> None:
        """Queue playback for the next main-loop turn."""
        if self._closed:
            return
        path = Path(clip_path)
        self._load_generation += 1
        generation = self._load_generation
        self._cancel_pending_load()
        self._release_media()
        self._autoplay = autoplay

        self._clip_path = path
        title = str(display_name or path.name)
        self._window_title.set_title(title)
        self._window_title.set_subtitle(_("Clip playback"))
        self._loading.set_visible(True)
        self._controls_revealer.set_reveal_child(False)
        self._volume_revealer.set_reveal_child(False)
        self._transport.reset_volume()
        self._content_stack.set_visible_child_name("video")
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
        if self._closed or generation != self._load_generation:
            return GLib.SOURCE_REMOVE
        self._load_source_id = None

        if not path.is_file():
            self._show_error(
                _("Clip unavailable"), _("The video file could not be found.")
            )
            self.emit("prepared")
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
            result = pipeline.set_state(Gst.State.PLAYING if self._autoplay else Gst.State.PAUSED)
            if result == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("GStreamer rejected the clip")

            self._ended = False
            self._set_playing_ui(self._autoplay)
            self._position_source_id = GLib.timeout_add(
                self.POSITION_UPDATE_MS,
                self._update_position,
                generation,
            )
        except Exception as error:  # Media failures belong inside the player.
            print(f"Could not play clip {path.name}: {error}")
            self._release_media()
            self._show_playback_error()

        self.emit("prepared")
        return GLib.SOURCE_REMOVE

    def _set_video_paintable(self, paintable) -> None:
        previous = self._video_paintable
        handler_id = self._paintable_size_handler_id
        contents_handler_id = self._paintable_contents_handler_id
        self._video_paintable = paintable
        self._paintable_size_handler_id = None
        self._paintable_contents_handler_id = None
        self._has_video_frame = False

        if previous is not None and handler_id is not None:
            try:
                previous.disconnect(handler_id)
            except Exception:
                pass
        if previous is not None and contents_handler_id is not None:
            previous.disconnect(contents_handler_id)

        self._picture.set_paintable(paintable)
        if paintable is None:
            return

        try:
            self._paintable_size_handler_id = paintable.connect(
                "invalidate-size", lambda *_args: self._picture.queue_resize()
            )
        except Exception:
            self._paintable_size_handler_id = None

        try:
            self._paintable_contents_handler_id = paintable.connect(
                "invalidate-contents", self._on_video_contents_invalidated
            )
            self._on_video_contents_invalidated(paintable)
        except Exception:
            self._paintable_contents_handler_id = None

    def _on_video_contents_invalidated(self, paintable) -> None:
        if self._has_video_frame or paintable is not self._video_paintable:
            return
        image = paintable.get_current_image()
        if image.get_intrinsic_width() <= 0 or image.get_intrinsic_height() <= 0:
            return
        self._has_video_frame = True
        self._loading.set_visible(False)
        self.emit("content-ready")

    def start_playback(self) -> None:
        """Start the prepared clip after the host has revealed its first frame."""
        if self._pipeline is None or self._closed:
            return
        self._pipeline.set_state(Gst.State.PLAYING)
        self._set_playing_ui(True)

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
        if self._has_video_frame:
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

    def is_fullscreen(self) -> bool:
        root = self.get_root()
        return isinstance(root, Gtk.Window) and root.is_fullscreen()

    def fullscreen(self) -> None:
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            root.fullscreen()

    def unfullscreen(self) -> None:
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            root.unfullscreen()

    def focus_video(self) -> None:
        self._picture.grab_focus()

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
        self.emit("content-ready")

    def _on_key_pressed(self, _controller, keyval, keycode, _state) -> bool:
        if matches_shortcuts_key(keyval, keycode, _state):
            return self.activate_action("win.shortcuts", None)

        if keyval == Gdk.KEY_Escape:
            if self.is_fullscreen():
                self.unfullscreen()
                return True
            self.close()
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
        """Stop media and let the host animate its return to the gallery."""
        if self._close_callback is not None:
            self._close_callback()
        else:
            self.stop()

    def stop(self) -> None:
        """Invalidate queued loads and seeks before detaching the media graph."""
        self._load_generation += 1
        self._cancel_pending_load()
        self._release_media()

    def cleanup(self) -> None:
        self._closed = True
        self.stop()

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
        paintable_sink = self._paintable_sink
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

        if preview_sink is not None and sample_handler_id is not None:
            try:
                preview_sink.disconnect(sample_handler_id)
            except Exception:
                pass
        pipelines = tuple(item for item in (pipeline, preview_pipeline) if item is not None)
        if pipelines:
            # State transitions may wait on decoder/GL threads. Detach the UI
            # first so a slow backend cannot freeze the gallery or its zoom.
            # The GTK sink owns objects guarded to the main thread. Stop and
            # lock that child here so the decoder worker cannot transition it
            # after the application has stopped owning the main context.
            if paintable_sink is not None:
                try:
                    paintable_sink.set_locked_state(True)
                    paintable_sink.set_state(Gst.State.NULL)
                except Exception:
                    pass
            if pipeline is not None:
                try:
                    pipeline.set_property("mute", True)
                except Exception:
                    pass
            threading.Thread(
                target=self._stop_pipelines,
                args=(pipelines, paintable_sink),
                name="clipper-player-stop",
                daemon=True,
            ).start()

    @staticmethod
    def _stop_pipelines(pipelines, paintable_sink) -> None:
        for pipeline in pipelines:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        # Retain the graph until an idle callback can drop its last references
        # on GTK's thread. Gtk4paintablesink also has guarded finalizers.
        GLib.idle_add(VideoPlayer._finish_pipeline_stop, pipelines, paintable_sink)

    @staticmethod
    def _finish_pipeline_stop(_pipelines, _paintable_sink) -> bool:
        GLib.idle_add(_reclaim_released_media_memory)
        return GLib.SOURCE_REMOVE
