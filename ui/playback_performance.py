"""Opt-in playback diagnostics, sampled from the active GTK video sink."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk, Pango

PERFORMANCE_ACTION = "playback-performance"
PERFORMANCE_MENU_ITEMS = ((_("Playback performance overlay"), f"win.{PERFORMANCE_ACTION}"),)

PERFORMANCE_CSS = """
.playback-performance {
    background: alpha(black, 0.78);
    color: white;
    border-radius: 2px;
    padding: 8px 10px;
    font-family: monospace;
    font-size: 11px;
}
.playback-performance .performance-key { color: #cccccc; }
.playback-performance .performance-warning { color: #f8e45c; }
"""
_CSS_INSTALLED = False


@dataclass(frozen=True)
class PlaybackTarget:
    pipeline: object
    sink: object
    playing: bool


@dataclass(frozen=True)
class PlaybackSample:
    fps: float | None = None
    rendered: int | None = None
    dropped: int | None = None
    video: str = "—"
    decoder: str = "—"
    playing: bool = False


def _video_details(sink) -> str:
    pad = sink.get_static_pad("sink")
    caps = pad.get_current_caps() if pad is not None else None
    if caps is None or caps.is_empty() or caps.is_any():
        return "—"
    structure = caps.get_structure(0)
    width = structure.get_value("width")
    height = structure.get_value("height")
    if not width or not height:
        return "—"
    resolution = f"{width} × {height}"
    ok, numerator, denominator = structure.get_fraction("framerate")
    if ok and numerator > 0 and denominator > 0:
        return _("%(size)s · %(fps)s fps") % {
            "size": resolution,
            "fps": f"{numerator / denominator:.2f}".rstrip("0").rstrip("."),
        }
    return resolution


def _video_decoders(pipeline) -> str:
    # Inspect only on the main thread while visible. No streaming callbacks or
    # probes are installed, and speculative editor pipelines are never sampled.
    iterator = pipeline.iterate_recurse()
    names = set()
    for _attempt in range(256):
        result, element = iterator.next()
        if result == Gst.IteratorResult.RESYNC:
            names.clear()
            iterator.resync()
            continue
        if result != Gst.IteratorResult.OK:
            break
        factory = element.get_factory()
        if factory is not None:
            category = factory.get_metadata("klass") or ""
            if "Decoder" in category and "Video" in category:
                names.add(factory.get_name())
    return ", ".join(sorted(names)) or "—"


class PlaybackSampler:
    """Measure delivered frames, with a fresh baseline after pause/reset/switch."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self):
        self._sink = None
        self._previous = None

    def sample(self, target: PlaybackTarget | None) -> PlaybackSample:
        if target is None:
            self.reset()
            return PlaybackSample()
        sink = target.sink
        stats = sink.get_property("stats") if sink.find_property("stats") else None
        rendered = stats.get_value("rendered") if stats is not None else None
        dropped = stats.get_value("dropped") if stats is not None else None
        now = self._clock()
        fps = None
        if rendered is not None and dropped is not None:
            previous = self._previous if sink is self._sink else None
            if previous is not None:
                stamp, old_rendered, old_dropped, was_playing = previous
                if (target.playing and was_playing and now > stamp
                        and rendered >= old_rendered and dropped >= old_dropped):
                    fps = (rendered - old_rendered) / (now - stamp)
            self._previous = (now, rendered, dropped, target.playing)
        else:
            self._previous = None
        self._sink = sink
        return PlaybackSample(
            fps=fps, rendered=rendered, dropped=dropped,
            video=_video_details(sink), decoder=_video_decoders(target.pipeline),
            playing=target.playing,
        )


class PlaybackPerformanceOverlay(Gtk.Box):
    """A click-through corner HUD whose timer follows visibility and ownership."""

    def __init__(self, target: Callable[[], PlaybackTarget | None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        global _CSS_INSTALLED
        display = Gdk.Display.get_default()
        if display is not None and not _CSS_INSTALLED:
            provider = Gtk.CssProvider()
            provider.load_from_string(PERFORMANCE_CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            _CSS_INSTALLED = True
        self._target = target
        self._sampler = PlaybackSampler()
        self._source_id = None
        self._cleaned = False
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)
        self.set_margin_start(8)
        self.set_margin_top(8)
        self.set_margin_end(8)
        self.set_can_target(False)
        self.set_focusable(False)
        self.add_css_class("playback-performance")

        grid = Gtk.Grid(column_spacing=16, row_spacing=2)
        self._values = {}
        for row, (key, label) in enumerate((
            ("status", _("Playback")),
            ("fps", _("Frame rate")),
            ("rendered", _("Rendered frames")),
            ("dropped", _("Dropped frames")),
            ("video", _("Video")),
            ("decoder", _("Decoder")),
        )):
            caption = Gtk.Label(label=label, xalign=0)
            caption.add_css_class("performance-key")
            grid.attach(caption, 0, row, 1, 1)
            value = Gtk.Label(label="—", xalign=1, hexpand=True)
            value.set_ellipsize(Pango.EllipsizeMode.END)
            value.set_max_width_chars(24)
            grid.attach(value, 1, row, 1, 1)
            self._values[key] = value
        self.append(grid)
        self._status = self._values["status"]
        self._fps = self._values["fps"]
        self.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Playback performance overlay")],
        )
        self.action = Gio.SimpleAction.new_stateful(
            PERFORMANCE_ACTION, None, GLib.Variant.new_boolean(False),
        )
        self.action.connect("change-state", self._change_state)
        self.set_visible(False)
        self.connect("map", self._start)
        self.connect("unmap", self._stop)

    def _change_state(self, action, value):
        if self._cleaned:
            return
        action.set_state(value)
        self.set_visible(value.get_boolean())

    def _start(self, *_args):
        if self._cleaned or self._source_id is not None:
            return
        self._refresh()
        self._source_id = GLib.timeout_add(500, self._refresh)

    def _stop(self, *_args):
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None
        self._sampler.reset()

    def _refresh(self):
        sample = self._sampler.sample(self._target())
        self._status.set_text(_("Playing") if sample.playing else _("Paused"))
        self._fps.set_text(_("%(fps)s FPS") % {
            "fps": f"{sample.fps:.1f}" if sample.fps is not None else "—",
        })
        self._values["rendered"].set_text(
            str(sample.rendered) if sample.rendered is not None else "—",
        )
        dropped = "—"
        if sample.dropped is not None and sample.rendered is not None:
            total = sample.rendered + sample.dropped
            percent = 100 * sample.dropped / total if total else 0
            dropped = _("%(count)s · %(percent)s%%") % {
                "count": str(sample.dropped), "percent": f"{percent:.1f}",
            }
        self._values["dropped"].set_text(dropped)
        if sample.dropped:
            self._values["dropped"].add_css_class("performance-warning")
        else:
            self._values["dropped"].remove_css_class("performance-warning")
        self._values["video"].set_text(sample.video)
        self._values["decoder"].set_text(sample.decoder)
        return GLib.SOURCE_CONTINUE

    def reset(self):
        """Release references before the owner tears down its media."""
        self._sampler.reset()

    def cleanup(self):
        self._cleaned = True
        self._stop()
        self.set_visible(False)
        self.action.set_enabled(False)
