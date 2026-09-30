from pathlib import Path
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gsk", "4.0")

import clip_player_overlay as overlay_module
import video_player as player_module
from clip_player_overlay import ClipPlayerOverlay
from gi.repository import Gdk, GLib, Graphene, Gsk, Gst, Gtk


@pytest.fixture
def embedded_player(monkeypatch):
    animations = []
    workers = []
    scheduled = []
    states = []
    properties = {}

    class Animation:
        def set_easing(self, _easing):
            pass

        def connect(self, _signal, callback):
            self.done = callback

        def play(self):
            pass

        def pause(self):
            pass

    def new_animation(*_args):
        animation = Animation()
        animations.append(animation)
        return animation

    class DeferredThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            workers.append(self)

    monkeypatch.setattr(overlay_module.Adw.TimedAnimation, "new", new_animation)
    monkeypatch.setattr(player_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(player_module.GLib, "source_remove", lambda _source: True)
    monkeypatch.setattr(player_module.GLib, "timeout_add", lambda *_args: 99)
    root = Gtk.Window()
    host = Gtk.Overlay()
    source = Gtk.Button()
    host.set_child(source)
    root.set_child(host)
    closed = []
    overlay = ClipPlayerOverlay(closed_callback=lambda: closed.append(True))
    host.add_overlay(overlay)
    monkeypatch.setattr(overlay, "get_mapped", lambda: True)
    player = overlay._player
    player._idle_add = lambda callback, *args: scheduled.append((callback, args)) or 41
    bus = SimpleNamespace(
        add_signal_watch=lambda: None,
        connect=lambda *_args: 17,
        disconnect=lambda _handler: None,
        remove_signal_watch=lambda: None,
    )
    pipeline = SimpleNamespace(
        set_property=lambda name, value: properties.__setitem__(name, value),
        set_state=lambda state: states.append(state) or Gst.StateChangeReturn.ASYNC,
        get_bus=lambda: bus,
    )
    player._pipeline_factory = lambda: pipeline
    player._sink_factory = lambda: SimpleNamespace(get_property=lambda _name: None)
    player._audio_sink_factory = lambda: None
    yield SimpleNamespace(
        overlay=overlay, player=player, root=root, source=source,
        animations=animations, scheduled=scheduled, states=states,
        properties=properties, pipeline=pipeline, closed=closed,
    )
    overlay.cleanup()
    for worker in workers:
        worker.target(*worker.args)
    root.destroy()


def test_reopening_during_close_rejects_stale_loads_and_animation_completion(
    embedded_player, tmp_path
):
    state = embedded_player
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    first.touch()
    second.touch()
    state.overlay.present_clip({"path": first}, state.source)
    state.overlay._set_progress(0.5)
    state.overlay.dismiss()
    closing = state.animations[-1]

    state.overlay.present_clip({"path": second}, state.source)
    closing.done(closing)
    assert state.overlay.get_visible()
    assert state.player.get_root() is state.root
    assert state.closed == []

    stale_load, latest_load = state.scheduled
    callback, args = stale_load
    assert callback(*args) == GLib.SOURCE_REMOVE
    assert state.states == []
    assert state.player._load_source_id == 41
    callback, args = latest_load
    callback(*args)
    assert state.properties["uri"] == second.as_uri()
    assert state.states == [Gst.State.PAUSED]

    state.overlay.dismiss(animate=False)
    assert state.player._pipeline is None
    assert state.properties["mute"] is True
    assert state.closed == [True]


def test_slow_first_frame_stays_covered_and_paused_until_zoom_and_reveal_finish(
    embedded_player, tmp_path
):
    state = embedded_player
    clip = tmp_path / "slow.mkv"
    clip.touch()
    state.overlay.present_clip({"path": clip}, state.source)
    callback, args = state.scheduled[0]
    callback(*args)
    assert state.states == [Gst.State.PAUSED]

    # Decoder state changes and elapsed zoom time alone cannot reveal an empty
    # paintable, or advance video/audio underneath the still-visible poster.
    opening = state.animations[-1]
    state.overlay._set_progress(1.0)
    opening.done(opening)
    assert state.overlay._reveal_animation is None
    assert state.states == [Gst.State.PAUSED]
    empty = Gdk.Paintable.new_empty(0, 0)
    state.player._set_video_paintable(empty)
    assert state.overlay._reveal_animation is None

    frame = Gdk.MemoryTexture.new(
        4, 4, Gdk.MemoryFormat.R8G8B8A8,
        GLib.Bytes.new(bytes([255, 0, 0, 255]) * 16), 16,
    )
    state.player._set_video_paintable(frame)
    reveal = state.animations[-1]
    assert state.states == [Gst.State.PAUSED]
    state.overlay._set_reveal_progress(1.0)
    reveal.done(reveal)
    assert state.states == [Gst.State.PAUSED, Gst.State.PLAYING]

    # A cancelled reveal belongs to the old clip, never a subsequently opened
    # one. Its delayed completion must not start the replacement early.
    state.overlay.present_clip({"path": clip}, state.source)
    reveal.done(reveal)
    assert state.states == [Gst.State.PAUSED, Gst.State.PLAYING]


@pytest.mark.parametrize("was_fullscreen", [False, True])
def test_host_hide_stops_playback_and_restores_focus_and_fullscreen(
    embedded_player, monkeypatch, was_fullscreen
):
    state = embedded_player
    window_state = {"fullscreen": was_fullscreen}
    monkeypatch.setattr(state.root, "is_fullscreen", lambda: window_state["fullscreen"])
    monkeypatch.setattr(
        state.root, "unfullscreen", lambda: window_state.__setitem__("fullscreen", False)
    )
    state.source.grab_focus()
    previous_focus = state.root.get_focus()
    state.overlay.present_clip({"path": Path("/tmp/clip.mkv")}, state.source)
    state.player._pipeline = state.pipeline
    window_state["fullscreen"] = True
    # Changing fullscreen decorations can temporarily unmap the content while
    # the host stays visible. It must not close playback or restore the gallery.
    state.overlay.emit("unmap")
    assert state.overlay.get_visible()
    assert state.player._pipeline is state.pipeline
    state.overlay._on_host_hidden()

    assert not state.overlay.get_visible()
    assert state.player._pipeline is None
    assert state.properties["mute"] is True
    assert state.closed == [True]
    assert state.root.get_focus() is previous_focus
    assert window_state["fullscreen"] is was_fullscreen
    callback, args = state.scheduled[0]
    assert callback(*args) == GLib.SOURCE_REMOVE
    assert state.states == []


def test_zoom_clip_preserves_visible_preview_pixels(embedded_player):
    # Gsk.RoundedRect.init_from_rect returns a borrowed alias. Dropping its
    # owner before snapshotting can clip away the entire player.
    overlay = embedded_player.overlay
    overlay.set_visible(True)
    embedded_player.root.set_default_size(800, 600)
    embedded_player.root.present()
    overlay._origin = (17.0, 29.0, 144.0, 81.0)
    overlay._poster = Gdk.MemoryTexture.new(
        4, 4, Gdk.MemoryFormat.R8G8B8A8,
        GLib.Bytes.new(bytes([255, 0, 0, 255]) * 16), 16,
    )
    overlay.allocate(800, 600, -1, None)
    snapshot = Gtk.Snapshot()
    overlay.do_snapshot(snapshot)
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    try:
        texture = renderer.render_texture(
            snapshot.to_node(), Graphene.Rect().init(0, 0, 800, 600)
        )
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        pixels = data.get_data()
        center = 69 * stride + 89 * 4
        assert pixels[center:center + 4] == bytes([255, 0, 0, 255])

        # The opaque decoded frame and fading poster must share the same
        # placement and brightness throughout their handoff.
        overlay._player._set_video_paintable(overlay._poster)
        overlay._set_progress(1.0)
        overlay._set_reveal_progress(0.5)
        overlay.allocate(800, 600, -1, None)
        snapshot = Gtk.Snapshot()
        overlay.do_snapshot(snapshot)
        texture = renderer.render_texture(
            snapshot.to_node(), Graphene.Rect().init(0, 0, 800, 600)
        )
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        pixels = data.get_data()
        valid, video_bounds = overlay._player._picture.compute_bounds(overlay)
        assert valid
        poster_bounds = overlay._preview_bounds(video_bounds)
        assert poster_bounds.equal(video_bounds)
        center = round(video_bounds.get_y() + video_bounds.get_height() / 2) * stride + 400 * 4
        assert pixels[center:center + 4] == bytes([255, 0, 0, 255])
    finally:
        renderer.unrealize()
