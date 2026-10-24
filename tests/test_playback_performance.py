from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk
from playback_performance import PlaybackPerformanceOverlay, PlaybackSampler, PlaybackTarget


def _target(rendered=0, dropped=0, *, playing=True):
    Gst.init(None)
    counts = {"rendered": rendered, "dropped": dropped}
    caps = Gst.Caps.from_string("video/x-raw,width=1920,height=1080,framerate=60000/1001")
    sink = SimpleNamespace(
        find_property=lambda _name: True,
        get_property=lambda _name: SimpleNamespace(get_value=counts.get),
        get_static_pad=lambda _name: SimpleNamespace(get_current_caps=lambda: caps),
    )
    iterator = SimpleNamespace(next=lambda: (Gst.IteratorResult.DONE, None))
    pipeline = SimpleNamespace(iterate_recurse=lambda: iterator)
    return PlaybackTarget(pipeline, sink, playing), counts


def test_fps_uses_rendered_frame_deltas_and_discards_paused_intervals():
    now = [0.0]
    sampler = PlaybackSampler(clock=lambda: now[0])
    target, counts = _target(100, 2)
    first = sampler.sample(target)
    assert first.fps is None
    assert (first.rendered, first.dropped) == (100, 2)
    assert "1920 × 1080" in first.video

    now[0] = 0.5
    counts["rendered"] += 30
    assert sampler.sample(target).fps == 60

    now[0] = 8
    paused = PlaybackTarget(target.pipeline, target.sink, False)
    assert sampler.sample(paused).fps is None
    now[0] = 10
    assert sampler.sample(target).fps is None
    now[0] = 10.5
    counts["rendered"] += 25
    assert sampler.sample(target).fps == 50


@pytest.mark.parametrize("reset", ["sink-switch", "seek-flush", "media-released"])
def test_sink_switch_and_counter_resets_do_not_report_stale_or_negative_fps(reset):
    now = [0.0]
    sampler = PlaybackSampler(clock=lambda: now[0])
    target, counts = _target(100, 5)
    sampler.sample(target)
    now[0] = 0.5
    if reset == "sink-switch":
        target, counts = _target(300, 10)
    elif reset == "seek-flush":
        counts.update(rendered=10, dropped=0)
    else:
        sampler.sample(None)
    assert sampler.sample(target).fps is None
    now[0] = 1
    counts["rendered"] += 30
    assert sampler.sample(target).fps == 60


def test_overlay_toggle_and_unmap_release_sampling_and_media_references(monkeypatch):
    scheduled = []
    removed = []
    monkeypatch.setattr(GLib, "timeout_add", lambda *args: scheduled.append(args) or 42)
    monkeypatch.setattr(GLib, "source_remove", removed.append)
    target, _counts = _target()
    overlay = PlaybackPerformanceOverlay(lambda: target)
    window = Gtk.Window()
    window.set_child(overlay)
    try:
        window.present()
        assert scheduled == []
        overlay.action.activate(None)
        assert overlay.action.get_state().get_boolean() is True
        assert len(scheduled) == 1
        assert overlay._sampler._sink is target.sink

        window.set_visible(False)
        assert removed == [42]
        assert overlay._sampler._sink is None
        window.present()
        assert len(scheduled) == 2

        overlay.action.activate(None)
        assert overlay.get_visible() is False
        assert removed == [42, 42]
        overlay.action.activate(None)
        overlay.cleanup()
        assert removed == [42, 42, 42]
        assert overlay._sampler._sink is None
    finally:
        overlay.cleanup()
        window.destroy()
