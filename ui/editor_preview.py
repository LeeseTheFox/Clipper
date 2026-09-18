"""Bare GStreamer video monitor without GTK's built-in media controls."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import gi
from i18n import _

gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")

from editor_model import effective_audio_gain
from gi.repository import Adw, GLib, Gst, Gtk
from logs import diagnostic_tail
from video_output import make_clipper_video_sink

_LOG = logging.getLogger("clipper.editor.preview")


def _log_pipeline_error(message, context):
    error, debug = message.parse_error()
    source = message.src.get_path_string() if message.src is not None else "unknown"
    _LOG.error("%s: element=%s error=%s debug=%s",
               context, source, error, diagnostic_tail(debug or ""))
    return str(error)


RESUME_WATCHDOG_MS = 250
RESUME_PROGRESS_US = 10_000
SEEK_SETTLE_TOLERANCE_US = 250_000
POSITION_POLL_MS = 10
PAUSED_SEEK_POLL_SECONDS = 2.0
LOADING_SPINNER_DELAY_MS = 50
AUDIO_PREROLL_US = 200_000
LEVEL_INTERVAL_NS = 50_000_000
TIMELINE_VIDEO_MAX_LATENESS_NS = 33_333_333
TIMELINE_ACTIVE_VIDEO_BUFFER_US = 250_000
TIMELINE_ACTIVE_QUEUE_MIN_US = 180_000
TIMELINE_MIN_ADVANCE_SETTLE_MS = 300
TIMELINE_NEXT_PREPARE_LEAD_US = 1_750_000
TIMELINE_READY_WINDOW_US = 8_000_000
TIMELINE_READY_WINDOW_MAX_BRANCHES = 8
TIMELINE_STANDBY_DECODE_INTERVAL_NS = 12_000_000
TIMELINE_URGENT_DECODE_INTERVAL_NS = 4_000_000
TIMELINE_URGENT_PREDECESSOR_US = 1_500_000
TIMELINE_PREROLL_RECHECK_MS = 20
TIMELINE_QUEUE_RECHECK_MS = 50
TIMELINE_QUEUE_RECHECK_LIMIT = 10
IDLE_PREPARE_DEBOUNCE_MS = 200
IDLE_PREPARE_BUDGET_MS = 5_000
AUDIO_SINK_BUFFER_TIME_US = 50_000


def _make_element(factory, name):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(
            _("The GStreamer element %(element)s is unavailable")
            % {"element": factory}
        )
    return element


def _configure_timeline_source(source, uri):
    source.set_property("uri", uri)
    # A branch added while the composition is already playing prepares its
    # own decoder children asynchronously. Keep that state change inside
    # uridecodebin so it cannot pause the active timeline at a cut.
    source.set_property("async-handling", True)


def _configure_timeline_video_sink(sink):
    # gtk4paintablesink defaults to dropping at 5 ms lateness. That is overly
    # aggressive for an editor switching between pre-rolled decode branches:
    # even a frame that can still be shown in the next 60 fps interval is lost.
    sink.set_property("max-lateness", TIMELINE_VIDEO_MAX_LATENESS_NS)


def _timeline_prepare_delay_ms(segment_duration_us):
    """Schedule standby decoding near its deadline, not immediately after a cut."""
    return max(
        TIMELINE_MIN_ADVANCE_SETTLE_MS,
        (segment_duration_us - TIMELINE_NEXT_PREPARE_LEAD_US + 999) // 1000,
    )


def _configure_level(element):
    """Configure a low-overhead peak report suitable for an editor meter."""
    element.set_property("interval", LEVEL_INTERVAL_NS)
    element.set_property("post-messages", True)


def _make_preview_audio_sink(name):
    """Prefer a directly controllable, low-latency desktop audio stream."""
    sink = Gst.ElementFactory.make("pulsesink", name)
    if sink is None:
        return _make_element("autoaudiosink", name)
    sink.set_property("client-name", "Clipper editor")
    sink.set_property("buffer-time", AUDIO_SINK_BUFFER_TIME_US)
    return sink


def _set_listening_volume(master_volume, audio_sink, volume):
    """Apply volume at the sink when possible, ahead of already-buffered audio."""
    volume = max(0.0, min(1.0, float(volume)))
    if (
        audio_sink is not None
        and hasattr(audio_sink, "find_property")
        and audio_sink.find_property("volume") is not None
    ):
        audio_sink.set_property("volume", volume)
        if audio_sink.find_property("mute") is not None:
            audio_sink.set_property("mute", volume == 0)
        return
    if master_volume is not None:
        master_volume.set_property("volume", volume)


def _set_track_gain(volume, gain):
    """Use GStreamer's unbounded property for editor track amplification."""
    volume.set_property("volume-full-range", max(0.0, float(gain)))


def _stereo_levels(values):
    if values is None or len(values) == 0:
        return None
    left = float(values[0])
    return left, float(values[1]) if len(values) > 1 else left


def _dispatch_level_message(message, level_track_ids, callback):
    """Forward one level element's stereo peaks and peak-hold values."""
    if callback is None or message.type != Gst.MessageType.ELEMENT:
        return False
    structure = message.get_structure()
    if structure is None or structure.get_name() != "level":
        return False
    track_id = level_track_ids.get(message.src.get_name())
    peaks = _stereo_levels(structure.get_value("peak"))
    held_peaks = _stereo_levels(structure.get_value("decay"))
    if track_id is None or peaks is None:
        return False
    callback(track_id, peaks, held_peaks or peaks)
    return True


class TimelineComposition:
    """Pre-rolled original-media ranges joined into one continuous timeline."""

    def __init__(
        self,
        project,
        output_start_us,
        eos_callback,
        error_callback,
        ready_callback=None,
    ):
        self.project = project
        self.output_start_us = output_start_us
        self._last_position_us = output_start_us
        self.eos_callback = eos_callback
        self.error_callback = error_callback
        self.ready_callback = ready_callback
        self.level_callback = None
        self.pipeline = Gst.Pipeline.new("clipper-editor-timeline-preview")
        self.video_concat = _make_element("concat", "clipper-editor-video-concat")
        self.video_convert = _make_element(
            "videoconvert",
            "clipper-editor-timeline-video-convert",
        )
        video_output = make_clipper_video_sink("clipper-editor-timeline-sink")
        if video_output is None:
            raise RuntimeError(_("The GStreamer video preview is unavailable"))
        self.video_sink = video_output.element
        self.sink = video_output.paintable_sink
        _configure_timeline_video_sink(self.sink)
        for element in (self.video_concat, self.video_convert, self.video_sink):
            self.pipeline.add(element)
        if not self.video_concat.link(self.video_convert) or not self.video_convert.link(
            self.video_sink
        ):
            raise RuntimeError(_("The timeline video preview could not be linked"))
        self.video_convert.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_video_output,
        )

        self.audio_concats = {}
        self.audio_level_track_ids = {}
        self.audio_mixer = None
        self.master_volume = None
        self.audio_sink = None
        if project.source.audio_tracks:
            self._build_audio_output()

        self._branches = []
        self._advance_pending = False
        self._advance_timer_id = None
        self._advance_rechecks = 0
        self._lookahead_prepare_id = None
        self._lookahead_request_index = None
        self._last_active_index = None
        self.video_concat.connect("notify::active-pad", self._on_active_pad_changed)
        self._prepared = False
        self._current_prerolled = False
        self._start_requested = False
        self._started = False
        self._playing = False
        self._playback_reported = False
        self.loading_generation = 0
        self._playback_generation = 0
        self.playback_started_callback = None
        self.preparation_generation = 0
        self.cache_identity = None
        self._speculative_growth_stopped = False
        self._cleaned = False
        self.diagnostics = {
            "branches_created": 0,
            "branches_ready": 0,
            "accurate_seeks": 0,
            "qos_messages": 0,
            "unready_activations": 0,
        }
        self._build_branches()
        self._last_active_index = self._start_index

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self._bus_handler_id = self.bus.connect("message", self._on_bus_message)

    @property
    def paintable(self):
        return (
            self.sink.get_property("paintable")
            if self.sink.find_property("paintable")
            else None
        )

    def _on_video_output(self, _pad, info):
        if (
            info.get_buffer() is None
            or not self._playing
            or self._playback_reported
        ):
            return Gst.PadProbeReturn.OK
        self._playback_reported = True
        if self.playback_started_callback is not None:
            GLib.idle_add(
                self.playback_started_callback,
                self,
                self._playback_generation,
            )
        return Gst.PadProbeReturn.OK

    def _build_audio_output(self):
        self.audio_mixer = _make_element(
            "audiomixer",
            "clipper-editor-timeline-audio-mixer",
        )
        convert = _make_element(
            "audioconvert",
            "clipper-editor-timeline-audio-convert",
        )
        resample = _make_element(
            "audioresample",
            "clipper-editor-timeline-audio-resample",
        )
        self.master_volume = _make_element(
            "volume",
            "clipper-editor-timeline-master-volume",
        )
        self.audio_sink = _make_preview_audio_sink(
            "clipper-editor-timeline-audio-sink"
        )
        for element in (
            self.audio_mixer,
            convert,
            resample,
            self.master_volume,
            self.audio_sink,
        ):
            self.pipeline.add(element)
        if (
            not self.audio_mixer.link(convert)
            or not convert.link(resample)
            or not resample.link(self.master_volume)
            or not self.master_volume.link(self.audio_sink)
        ):
            raise RuntimeError(_("The timeline audio preview could not be linked"))
        for track in self.project.source.audio_tracks:
            concat = _make_element(
                "concat",
                f"clipper-editor-{track.id}-concat",
            )
            level = _make_element(
                "level",
                f"clipper-editor-{track.id}-timeline-level",
            )
            _configure_level(level)
            self.pipeline.add(concat)
            self.pipeline.add(level)
            if not concat.link(level) or not level.link(self.audio_mixer):
                raise RuntimeError(_("A timeline audio track could not be linked"))
            self.audio_concats[track.id] = concat
            self.audio_level_track_ids[level.get_name()] = track.id

    def _build_branches(self):
        start_index, start_offset_us = self._segment_at_output(self.output_start_us)
        self._start_index = start_index
        self._start_offset_us = start_offset_us
        self._startup_end_index = self._ready_window_end_index(
            start_index,
            start_offset_us,
        )
        self._lookahead_end_index = self._startup_end_index
        first_segment = self.project.segments[start_index]
        self._append_branch(
            start_index,
            first_segment.source_start_us + start_offset_us,
        )
        self._promote_active_branch(self._branches[-1])

    def _coalesced_end_index(self, index):
        """Return one segment so each editable range has its own audio gain."""
        # Audio settings are independently editable at every segment boundary.
        # A single volume element cannot represent two settings in one
        # coalesced decode branch: changing either setting would change the
        # entire branch. Keep the method (and branch end_index metadata) for
        # the timeline lookahead machinery, but never merge segments here.
        return index

    def _ready_window_end_index(self, start_index, start_offset_us=0):
        """Cover enough playback time without creating an unbounded decoder set."""
        segments = self.project.segments
        index = start_index
        duration_us = -start_offset_us
        branch_count = 0
        end_index = start_index
        while index < len(segments):
            end_index = self._coalesced_end_index(index)
            duration_us += sum(
                segment.duration_us for segment in segments[index : end_index + 1]
            )
            branch_count += 1
            if (
                duration_us >= TIMELINE_READY_WINDOW_US
                or branch_count >= TIMELINE_READY_WINDOW_MAX_BRANCHES
                or end_index == len(segments) - 1
            ):
                break
            index = end_index + 1
        return end_index

    def _prepare_next_branch(self, active_index):
        """Prepare only the immediate successor to bound decoder contention."""
        if getattr(self, "_speculative_growth_stopped", False) and not self._start_requested:
            return
        active = next(
            (branch for branch in self._branches if branch["index"] == active_index),
            None,
        )
        next_index = (
            active.get("end_index", active_index) + 1
            if active is not None
            else active_index + 1
        )
        if next_index >= len(self.project.segments) or any(
            branch["index"] == next_index for branch in self._branches
        ):
            return
        segment = self.project.segments[next_index]
        self._append_branch(next_index, segment.source_start_us)

    def _prepare_ready_window(self, active_index=None):
        """Keep a bounded run of future cuts decoded and buffered."""
        if getattr(self, "_speculative_growth_stopped", False) and not self._start_requested:
            return
        if active_index is not None:
            self._lookahead_end_index = max(
                self._lookahead_end_index,
                self._ready_window_end_index(active_index),
            )
        latest = max(self._branches, key=lambda branch: branch["index"])
        if (
            latest.get("end_index", latest["index"]) >= self._lookahead_end_index
            or not latest["ready"]
        ):
            return
        self._prepare_next_branch(latest["index"])

    def _schedule_ready_window_after(self, active):
        """Advance decoder lookahead independently of branch retirement."""
        next_index = active.get("end_index", active["index"]) + 1
        if next_index >= len(self.project.segments):
            return
        pending_index = getattr(self, "_lookahead_request_index", None)
        self._lookahead_request_index = max(
            next_index,
            pending_index if pending_index is not None else next_index,
        )
        if getattr(self, "_lookahead_prepare_id", None) is None:
            self._lookahead_prepare_id = GLib.idle_add(
                self._extend_ready_window,
            )

    def _extend_ready_window(self):
        self._lookahead_prepare_id = None
        next_index = self._lookahead_request_index
        self._lookahead_request_index = None
        if self._cleaned or next_index is None:
            return False
        self._prepare_ready_window(next_index)
        return False

    def _append_branch(self, index, source_start_us):
        segment = self.project.segments[index]
        end_index = self._coalesced_end_index(index)
        end_segment = self.project.segments[end_index]
        branch_duration_us = end_segment.source_end_us - source_start_us
        frame_duration_us = getattr(self.project.source, "frame_duration_us", 1)
        uri = Path(self.project.source.path).resolve().as_uri()
        branch = {
            "index": index,
            "end_index": end_index,
            "segment_id": segment.id,
            "segment_ids": tuple(
                item.id for item in self.project.segments[index : end_index + 1]
            ),
            "source_start_us": source_start_us,
            "source_end_us": end_segment.source_end_us,
            "duration_us": branch_duration_us,
            "preroll_target_us": min(
                TIMELINE_ACTIVE_QUEUE_MIN_US,
                max(1, branch_duration_us - frame_duration_us),
            ),
            "seek_scheduled": False,
            "seek_sent": False,
            "no_more_pads": False,
            "linked_pads": set(),
            "initially_blocked_pads": set(),
            "ready_pads": set(),
            "probe_ids": {},
            "ready": False,
            "preroll_unblocked": False,
            "queue_fill_timer_id": None,
            # The initial current/next pair must become ready immediately.
            # Later seeks have ample lead time, so pace their compressed video
            # input instead of letting a second hardware decoder consume an
            # entire GOP in one burst and stall the active decoder.
            "throttle_decode": len(self._branches) > 1,
            "decode_throttle_pad": None,
            "decode_throttle_probe_id": None,
            "decode_throttle_next_ns": None,
            "decode_throttle_interval_ns": (
                TIMELINE_URGENT_DECODE_INTERVAL_NS
                if index > 0
                and self.project.segments[index - 1].duration_us
                <= TIMELINE_URGENT_PREDECESSOR_US
                else TIMELINE_STANDBY_DECODE_INTERVAL_NS
            ),
            "decode_throttle_buffers": 0,
            "next_audio_track": 0,
            "volumes": {},
            "audio_concat_pads": {},
            "elements": [],
            "retired": False,
        }
        source = _make_element(
            "uridecodebin",
            f"clipper-editor-segment-{index}-source",
        )
        _configure_timeline_source(source, uri)
        source.connect("deep-element-added", self._on_deep_element_added, branch)
        source.connect("pad-added", self._on_pad_added, branch)
        source.connect("no-more-pads", self._on_no_more_pads, branch)
        branch["source"] = source
        branch["elements"].append(source)
        self.pipeline.add(source)

        video_queue = _make_element(
            "queue",
            f"clipper-editor-segment-{index}-video-queue",
        )
        video_queue.set_property("max-size-buffers", 0)
        video_queue.set_property("max-size-bytes", 0)
        video_queue.set_property(
            "max-size-time",
            TIMELINE_ACTIVE_VIDEO_BUFFER_US * 1000,
        )
        self.pipeline.add(video_queue)
        video_concat_pad = self.video_concat.request_pad_simple("sink_%u")
        if (
            video_concat_pad is None
            or video_queue.get_static_pad("src").link(video_concat_pad)
            != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError(_("A timeline video segment could not be linked"))
        branch["video_queue"] = video_queue
        branch["video_concat_pad"] = video_concat_pad
        branch["elements"].append(video_queue)
        for track in self.project.source.audio_tracks:
            concat_pad = self.audio_concats[track.id].request_pad_simple("sink_%u")
            if concat_pad is None:
                raise RuntimeError(_("A timeline audio segment could not be prepared"))
            branch["audio_concat_pads"][track.id] = concat_pad
        self._branches.append(branch)
        self.diagnostics["branches_created"] += 1
        if self._prepared:
            video_queue.sync_state_with_parent()
            source.sync_state_with_parent()

    def _on_active_pad_changed(self, *_args):
        video_concat = getattr(self, "video_concat", None)
        active_pad = (
            video_concat.get_property("active-pad")
            if video_concat is not None
            else None
        )
        active = next(
            (
                branch
                for branch in self._branches
                if branch.get("video_concat_pad") == active_pad
            ),
            None,
        )
        if active is None or active["index"] == self._last_active_index:
            return
        self._last_active_index = active["index"]
        self._promote_active_branch(active)
        if not active.get("ready", False):
            self.diagnostics["unready_activations"] += 1
        elif active.get(
            "duration_us",
            self.project.segments[active["index"]].duration_us,
        ) <= TIMELINE_NEXT_PREPARE_LEAD_US:
            # A settle timer can outlive several frame-sized cuts. Move the
            # decoder window immediately so those transitions cannot consume
            # every prepared branch before the timer gets another turn.
            self._schedule_ready_window_after(active)
        if not self._advance_pending:
            self._advance_pending = True
            self._advance_timer_id = GLib.timeout_add(
                _timeline_prepare_delay_ms(
                    active.get(
                        "duration_us",
                        self.project.segments[active["index"]].duration_us,
                    )
                ),
                self._advance_branches,
            )

    @staticmethod
    def _promote_active_branch(branch):
        """Give the active decoder enough headroom for brief standby bursts."""
        queue = branch["video_queue"]
        queue.set_property("max-size-buffers", 0)
        queue.set_property(
            "max-size-time",
            TIMELINE_ACTIVE_VIDEO_BUFFER_US * 1000,
        )

    def _on_deep_element_added(self, _bin, _sub_bin, element, branch):
        """Pace only standby video decoder input while it seeks to its cut."""
        if (
            branch.get("retired", False)
            or not branch["throttle_decode"]
            or branch["decode_throttle_probe_id"] is not None
        ):
            return
        factory = element.get_factory()
        if factory is None:
            return
        element_class = factory.get_metadata("klass") or ""
        if "Decoder" not in element_class or "Video" not in element_class:
            return
        sink_pad = element.get_static_pad("sink")
        if sink_pad is None:
            return
        branch["decode_throttle_pad"] = sink_pad
        branch["decode_throttle_probe_id"] = sink_pad.add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_decode_input,
            branch,
        )

    def _on_decode_input(self, _pad, _info, branch):
        if self._cleaned or branch["ready"]:
            return Gst.PadProbeReturn.REMOVE
        now_ns = time.monotonic_ns()
        branch["decode_throttle_buffers"] += 1
        next_ns = branch["decode_throttle_next_ns"]
        if next_ns is not None and next_ns > now_ns:
            time.sleep((next_ns - now_ns) / Gst.SECOND)
            now_ns = time.monotonic_ns()
        branch["decode_throttle_next_ns"] = (
            max(next_ns or now_ns, now_ns) + branch["decode_throttle_interval_ns"]
        )
        return Gst.PadProbeReturn.OK

    @staticmethod
    def _remove_decode_throttle(branch):
        pad = branch.get("decode_throttle_pad")
        probe_id = branch.get("decode_throttle_probe_id")
        if pad is not None and probe_id is not None:
            pad.remove_probe(probe_id)
        branch["decode_throttle_pad"] = None
        branch["decode_throttle_probe_id"] = None

    def _advance_branches(self):
        if self._cleaned:
            self._advance_pending = False
            self._advance_timer_id = None
            return False
        active_video_pad = self.video_concat.get_property("active-pad")
        active = next(
            (
                branch
                for branch in self._branches
                if branch["video_concat_pad"] == active_video_pad
            ),
            None,
        )
        if active is None:
            self._advance_pending = False
            self._advance_timer_id = None
            return False
        for track_id, concat in self.audio_concats.items():
            if concat.get_property("active-pad") != active["audio_concat_pads"][track_id]:
                self._advance_timer_id = GLib.timeout_add(
                    TIMELINE_QUEUE_RECHECK_MS,
                    self._advance_branches,
                )
                return False
        if (
            self._advance_rechecks < TIMELINE_QUEUE_RECHECK_LIMIT
            and active["video_queue"].get_property("current-level-time")
            < active.get("preroll_target_us", TIMELINE_ACTIVE_QUEUE_MIN_US) * 1000
        ):
            self._advance_rechecks += 1
            self._advance_timer_id = GLib.timeout_add(
                TIMELINE_QUEUE_RECHECK_MS,
                self._advance_branches,
            )
            return False
        for branch in self._branches:
            if branch["index"] < active["index"] and not branch["retired"]:
                self._retire_branch(branch)
        next_index = active.get("end_index", active["index"]) + 1
        if next_index < len(self.project.segments):
            # The active branch is already decoded and should not consume one
            # of the bounded future-branch slots.
            self._prepare_ready_window(next_index)
        self._advance_pending = False
        self._advance_timer_id = None
        self._advance_rechecks = 0
        return False

    @staticmethod
    def _retire_branch(branch):
        branch["retired"] = True
        for element in branch["elements"]:
            # A later asynchronous child can briefly change the parent
            # pipeline's state. Lock retired sources so that state ripple
            # cannot recreate their demuxers and decoders.
            element.set_locked_state(True)
            element.set_state(Gst.State.NULL)

    def _segment_at_output(self, output_us):
        cursor = 0
        for index, segment in enumerate(self.project.segments):
            end = cursor + segment.duration_us
            if output_us < end or index == len(self.project.segments) - 1:
                return index, min(output_us - cursor, segment.duration_us)
            cursor = end
        raise ValueError("Output time is outside the timeline")

    def _on_pad_added(self, _source, pad, branch):
        if self._cleaned or branch["retired"]:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or caps.get_size() == 0:
            return
        media_type = caps.get_structure(0).get_name()
        if media_type.startswith("video/"):
            sink_pad = branch["video_queue"].get_static_pad("sink")
            if sink_pad and not sink_pad.is_linked():
                if pad.link(sink_pad) == Gst.PadLinkReturn.OK:
                    self._watch_branch_pad(pad, branch)
        elif media_type.startswith("audio/"):
            if self._link_audio_pad(pad, branch):
                self._watch_branch_pad(pad, branch)

    def _watch_branch_pad(self, pad, branch):
        branch["linked_pads"].add(pad)
        probe_id = pad.add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER,
            self._on_branch_pad_blocked,
            branch,
        )
        branch["probe_ids"][pad] = probe_id

    def _on_branch_pad_blocked(self, pad, _info, branch):
        if branch["seek_sent"]:
            branch["ready_pads"].add(pad)
            GLib.idle_add(self._maybe_finish_branch_preroll, branch)
        else:
            branch["initially_blocked_pads"].add(pad)
            GLib.idle_add(self._maybe_seek_branch, branch)
        return Gst.PadProbeReturn.OK

    def _link_audio_pad(self, pad, branch):
        ordinal = branch["next_audio_track"]
        tracks = self.project.source.audio_tracks
        if ordinal >= len(tracks):
            return False
        branch["next_audio_track"] += 1
        track = tracks[ordinal]
        prefix = f"clipper-editor-segment-{branch['index']}-{track.id}"
        queue = _make_element("queue", f"{prefix}-queue")
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", AUDIO_PREROLL_US * 1000)
        convert = _make_element("audioconvert", f"{prefix}-convert")
        resample = _make_element("audioresample", f"{prefix}-resample")
        volume = _make_element("volume", f"{prefix}-volume")
        setting = self.project.segments[branch["index"]].audio[track.id]
        _set_track_gain(volume, effective_audio_gain(setting))
        for element in (queue, convert, resample, volume):
            self.pipeline.add(element)
            branch["elements"].append(element)
        if (
            not queue.link(convert)
            or not convert.link(resample)
            or not resample.link(volume)
        ):
            raise RuntimeError(_("A timeline audio segment could not be linked"))
        concat_pad = branch["audio_concat_pads"][track.id]
        if (
            concat_pad is None
            or volume.get_static_pad("src").link(concat_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError(_("A timeline audio segment could not be concatenated"))
        sink_pad = queue.get_static_pad("sink")
        if sink_pad is None or pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(_("A decoded timeline audio track could not be linked"))
        branch["volumes"][track.id] = volume
        for element in (queue, convert, resample, volume):
            element.sync_state_with_parent()
        return True

    def _on_no_more_pads(self, source, branch):
        # A flushing seek can make uridecodebin announce no-more-pads again.
        # Repeating the same seek flushes a branch just as concat activates it,
        # starving the preview and causing a burst of late frames.
        branch["no_more_pads"] = True
        if branch["seek_scheduled"]:
            return
        GLib.idle_add(self._maybe_seek_branch, branch)

    def _maybe_seek_branch(self, branch):
        if self._cleaned or branch["seek_scheduled"]:
            return False
        if not branch["no_more_pads"] or not branch["linked_pads"]:
            return False
        branch["seek_scheduled"] = True
        return self._seek_branch(branch["source"], branch)

    def _seek_branch(self, source, branch):
        if self._cleaned:
            return False
        event = Gst.Event.new_seek(
            1.0,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            branch["source_start_us"] * 1000,
            Gst.SeekType.SET,
            branch["source_end_us"] * 1000,
        )
        branch["seek_sent"] = True
        branch["ready_pads"].clear()
        if not source.send_event(event):
            self.error_callback(self, _("A timeline segment could not be prepared"))
            return False
        self.diagnostics["accurate_seeks"] += 1
        return False

    def _maybe_finish_branch_preroll(self, branch):
        if self._cleaned or branch["ready"] or branch["preroll_unblocked"]:
            return False
        if branch["ready_pads"] != branch["linked_pads"]:
            return False
        branch["preroll_unblocked"] = True
        for pad, probe_id in tuple(branch["probe_ids"].items()):
            pad.remove_probe(probe_id)
        branch["probe_ids"].clear()
        branch["queue_fill_timer_id"] = GLib.timeout_add(
            TIMELINE_PREROLL_RECHECK_MS,
            self._finish_branch_queue_preroll,
            branch,
        )
        return False

    def _finish_branch_queue_preroll(self, branch):
        if self._cleaned or branch["ready"]:
            branch["queue_fill_timer_id"] = None
            return False
        if (
            branch["video_queue"].get_property("current-level-time")
            < branch.get("preroll_target_us", TIMELINE_ACTIVE_QUEUE_MIN_US) * 1000
            and not (
                branch.get("duration_us", TIMELINE_ACTIVE_QUEUE_MIN_US)
                <= getattr(self.project.source, "frame_duration_us", 1)
                and branch["video_queue"].get_property("current-level-buffers") > 0
            )
        ):
            return True
        branch["queue_fill_timer_id"] = None
        self._mark_branch_ready(branch, cancel_timer=False)
        return False

    def _mark_branch_ready(self, branch, *, cancel_timer=True):
        if branch["ready"]:
            return
        timer_id = branch.get("queue_fill_timer_id")
        if cancel_timer and timer_id is not None:
            GLib.source_remove(timer_id)
            branch["queue_fill_timer_id"] = None
        branch["ready"] = True
        self._remove_decode_throttle(branch)
        self.diagnostics["branches_ready"] += 1
        if self._current_prerolled:
            self._prepare_ready_window()
        if self._start_requested:
            self.start()

    def _startup_successor_ready(self):
        return any(
            branch["index"] <= self._startup_end_index
            <= branch.get("end_index", branch["index"])
            and branch["ready"]
            for branch in self._branches
        )

    def prepare(self):
        if self._prepared:
            return True
        self._prepared = True
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self.error_callback(self, _("The timeline could not be prepared"))
            return False
        return True

    def start(self):
        self._start_requested = True
        # The five-second budget only bounds idle work. Once adopted, normal
        # playback lookahead must continue preparing the bounded ready window.
        self._speculative_growth_stopped = False
        if not self.prepare():
            return False
        if not self._current_prerolled or not self._startup_successor_ready():
            return True
        if self._playing:
            return True
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.error_callback(self, _("The pre-rolled timeline could not start"))
            return False
        self._started = True
        self._playing = True
        self._playback_reported = False
        self._playback_generation = getattr(self, "loading_generation", 0)
        video_concat = getattr(self, "video_concat", None)
        active_pad = (
            video_concat.get_property("active-pad")
            if video_concat is not None
            else None
        )
        active = (
            next(
                (
                    branch
                    for branch in self._branches
                    if branch.get("video_concat_pad") == active_pad
                ),
                None,
            )
            if active_pad is not None
            else None
        )
        if active is not None:
            duration_us = active.get(
                "duration_us",
                self.project.segments[active["index"]].duration_us,
            )
            if active.get("ready", False) and duration_us <= TIMELINE_NEXT_PREPARE_LEAD_US:
                self._schedule_ready_window_after(active)
            if not self._advance_pending:
                self._advance_pending = True
                self._advance_timer_id = GLib.timeout_add(
                    _timeline_prepare_delay_ms(duration_us),
                    self._advance_branches,
                )
        return True

    def stop_speculative_growth(self):
        """Stop adding idle branches without tearing down completed preroll."""
        self._speculative_growth_stopped = True

    def ready_for_playback(self):
        return self._current_prerolled and self._startup_successor_ready()

    def pause(self):
        self._start_requested = False
        self._last_position_us = self.position_us()
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            return False
        self._playing = False
        return True

    def position_us(self):
        if not self._started:
            return self._last_position_us
        _result, current, _pending = self.pipeline.get_state(0)
        if current != Gst.State.PLAYING:
            return self._last_position_us
        clock = self.pipeline.get_clock()
        if clock is None:
            return self._last_position_us
        running_ns = max(0, clock.get_time() - self.pipeline.get_base_time())
        self._last_position_us = min(
            self.project.output_duration_us,
            self.output_start_us + running_ns // 1000,
        )
        return self._last_position_us

    def update_audio(self, project):
        """Apply all edited gains to existing per-segment branches in place."""
        self.project = project
        by_id = {segment.id: segment for segment in project.segments}
        for branch in self._branches:
            segment_ids = branch.get("segment_ids", (branch["segment_id"],))
            segment = by_id.get(segment_ids[0])
            if segment is None:
                continue
            for track_id, volume in branch["volumes"].items():
                _set_track_gain(
                    volume,
                    effective_audio_gain(segment.audio[track_id]),
                )

    def set_volume(self, volume):
        """Set listening volume without changing any edited track gains."""
        _set_listening_volume(
            getattr(self, "master_volume", None),
            getattr(self, "audio_sink", None),
            volume,
        )

    def update_audio_setting(self, project, segment_id, track_id):
        """Apply one edited gain without changing live timeline topology."""
        self.project = project
        segment = next(
            (item for item in project.segments if item.id == segment_id),
            None,
        )
        if segment is None:
            return
        gain = effective_audio_gain(segment.audio[track_id])
        for branch in self._branches:
            segment_ids = branch.get("segment_ids", (branch["segment_id"],))
            if segment_id not in segment_ids:
                continue
            volume = branch["volumes"].get(track_id)
            if volume is not None:
                _set_track_gain(volume, gain)
            return

    def _on_bus_message(self, _bus, message):
        if _dispatch_level_message(
            message,
            self.audio_level_track_ids,
            self.level_callback,
        ):
            return
        if message.type == Gst.MessageType.ASYNC_DONE and message.src == self.pipeline:
            if not self._current_prerolled:
                self._current_prerolled = True
                self._prepare_ready_window()
                if self.ready_callback is not None:
                    self.ready_callback(self)
                if self._start_requested:
                    self.start()
        elif message.type == Gst.MessageType.QOS:
            self.diagnostics["qos_messages"] += 1
        elif message.type == Gst.MessageType.EOS:
            self._last_position_us = self.project.output_duration_us
            self.eos_callback(self)
        elif message.type == Gst.MessageType.ERROR:
            error = _log_pipeline_error(message, "Timeline pipeline failed")
            self.error_callback(self, error)

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        if self._advance_timer_id is not None:
            GLib.source_remove(self._advance_timer_id)
            self._advance_timer_id = None
        if self._lookahead_prepare_id is not None:
            GLib.source_remove(self._lookahead_prepare_id)
            self._lookahead_prepare_id = None
        self._lookahead_request_index = None
        for branch in self._branches:
            timer_id = branch.get("queue_fill_timer_id")
            if timer_id is not None:
                GLib.source_remove(timer_id)
                branch["queue_fill_timer_id"] = None
            self._remove_decode_throttle(branch)
        self.pipeline.set_state(Gst.State.NULL)
        self.bus.disconnect(self._bus_handler_id)
        self.bus.remove_signal_watch()


class EditorPreview:
    def __init__(
        self,
        project,
        changed_callback=None,
        state_changed_callback=None,
        level_callback=None,
    ):
        Gst.init(None)
        self.project = project
        self.changed_callback = changed_callback
        self.state_changed_callback = state_changed_callback
        self.level_callback = level_callback
        self.volume = 1.0
        self.output_position_us = 0
        self.playing = False
        self._resume_watchdog_id = None
        self._pending_source_seek_us = None
        self._timeline_composition = None
        self._speculative_composition = None
        self._idle_prepare_id = None
        self._idle_prepare_deadline_id = None
        self._scheduled_prepare_identity = None
        self._speculative_identity = None
        self._preparation_generation = 0
        self._source_valid = True
        self._closing = False
        self._held_paintable = None
        self._loading = False
        self._loading_spinner_delay_id = None
        self._legacy_frame_reported = False
        self._loading_generation = 0
        self._project_structure_snapshot = self._project_structure(project)
        self._project_cache_snapshot = self._project_identity(project, 0)
        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.add_css_class("editor-monitor-picture")
        self.loading_spinner = Adw.Spinner()
        self.loading_spinner.set_halign(Gtk.Align.CENTER)
        self.loading_spinner.set_valign(Gtk.Align.CENTER)
        self.loading_spinner.set_size_request(48, 48)
        self.loading_spinner.set_can_target(False)
        self.loading_spinner.set_tooltip_text(_("Loading video"))
        self.loading_spinner.add_css_class("editor-loading-spinner")
        self.loading_spinner.set_visible(False)
        self.pipeline = Gst.Pipeline.new("clipper-editor-preview")
        self.source = Gst.ElementFactory.make("uridecodebin", "clipper-editor-source")
        self.video_queue = Gst.ElementFactory.make("queue", "clipper-editor-video-queue")
        self.video_convert = Gst.ElementFactory.make(
            "videoconvert",
            "clipper-editor-video-convert",
        )
        video_output = make_clipper_video_sink("clipper-editor-sink")
        if video_output is None:
            raise RuntimeError(_("The GStreamer video preview is unavailable"))
        self.video_sink = video_output.element
        self.sink = video_output.paintable_sink
        required = (
            self.pipeline,
            self.source,
            self.video_queue,
            self.video_convert,
            self.video_sink,
            self.sink,
        )
        if any(element is None for element in required):
            raise RuntimeError(_("The GStreamer video preview is unavailable"))
        self.pipeline.add(self.source)
        self.pipeline.add(self.video_queue)
        self.pipeline.add(self.video_convert)
        self.pipeline.add(self.video_sink)
        if not self.video_queue.link(self.video_convert) or not self.video_convert.link(
            self.video_sink
        ):
            raise RuntimeError(_("The GStreamer video preview could not be linked"))
        self.video_convert.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_legacy_video_output,
        )
        self.source.set_property("uri", Path(project.source.path).resolve().as_uri())
        self.source.connect("pad-added", self._on_source_pad_added)
        self.audio_volumes = {}
        self.audio_level_track_ids = {}
        self._next_audio_track = 0
        self._audio_elements = []
        self.audio_mixer = None
        self.master_volume = None
        self.audio_sink = None
        if project.source.audio_tracks:
            self._build_audio_output()
        # PipeWire can restore the volume and mute state from a previous editor
        # stream.  Apply the editor's default listening volume explicitly so a
        # newly opened editor matches its 100% volume control.
        self.set_volume(self.volume)
        self._legacy_paintable = (
            self.sink.get_property("paintable")
            if self.sink.find_property("paintable")
            else None
        )
        self.picture.set_paintable(self._legacy_paintable)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self._bus_handler_id = self.bus.connect("message", self._on_bus_message)
        self.pipeline.set_state(Gst.State.PAUSED)
        self._position_timer = None

    def _ensure_position_timer(self):
        if getattr(self, "_closing", False) or not getattr(self, "_source_valid", True):
            return
        if getattr(self, "_position_timer", None) is None:
            self._position_timer = GLib.timeout_add(POSITION_POLL_MS, self._poll_position)

    def _stop_position_timer(self):
        timer = getattr(self, "_position_timer", None)
        self._position_timer = None
        if timer is not None:
            GLib.source_remove(timer)

    def _watch_source_seek(self, source_us):
        self._pending_source_seek_us = source_us
        self._position_poll_deadline = time.monotonic() + PAUSED_SEEK_POLL_SECONDS
        self._ensure_position_timer()

    def _hold_current_frame(self):
        """Keep the visible frame stable while another pipeline prepares."""
        picture = getattr(self, "picture", None)
        if picture is None or not hasattr(picture, "get_paintable"):
            return
        paintable = picture.get_paintable()
        if paintable is None or not hasattr(paintable, "get_current_image"):
            return
        try:
            held_paintable = paintable.get_current_image()
        except Exception:
            return
        if held_paintable is None:
            return
        self._held_paintable = held_paintable
        picture.set_paintable(held_paintable)

    def _show_paintable(self, paintable):
        self.picture.set_paintable(paintable)
        self._held_paintable = None

    def _set_loading(self, loading):
        was_loading = getattr(self, "_loading", False)
        if loading:
            self._loading_generation = getattr(self, "_loading_generation", 0) + 1
            self._legacy_frame_reported = False
        self._loading = loading
        spinner = getattr(self, "loading_spinner", None)
        if spinner is None:
            return
        if loading:
            if was_loading:
                return
            self._loading_spinner_delay_id = GLib.timeout_add(
                LOADING_SPINNER_DELAY_MS,
                self._show_loading_spinner,
            )
            return
        delay_id = getattr(self, "_loading_spinner_delay_id", None)
        if delay_id is not None:
            GLib.source_remove(delay_id)
            self._loading_spinner_delay_id = None
        spinner.set_visible(False)
        if hasattr(spinner, "set_spinning"):
            spinner.set_spinning(False)

    def _show_loading_spinner(self):
        self._loading_spinner_delay_id = None
        if not self._loading:
            return False
        spinner = getattr(self, "loading_spinner", None)
        if spinner is not None:
            spinner.set_visible(True)
            if hasattr(spinner, "set_spinning"):
                spinner.set_spinning(True)
        return False

    def _on_legacy_video_output(self, _pad, info):
        if (
            info.get_buffer() is not None
            and self.playing
            and self._timeline_composition is None
            and self._loading
            and not self._legacy_frame_reported
        ):
            self._legacy_frame_reported = True
            GLib.idle_add(
                self._finish_legacy_loading,
                self._loading_generation,
            )
        return Gst.PadProbeReturn.OK

    def _finish_legacy_loading(self, generation):
        if (
            generation == self._loading_generation
            and self._timeline_composition is None
        ):
            self._show_paintable(self._legacy_paintable)
            self._set_loading(False)
        return False

    def _build_audio_output(self):
        self.audio_mixer = Gst.ElementFactory.make("audiomixer", "clipper-editor-audio-mixer")
        self.master_volume = Gst.ElementFactory.make(
            "volume",
            "clipper-editor-master-volume",
        )
        convert = Gst.ElementFactory.make("audioconvert", "clipper-editor-audio-output-convert")
        resample = Gst.ElementFactory.make(
            "audioresample",
            "clipper-editor-audio-output-resample",
        )
        self.audio_sink = _make_preview_audio_sink("clipper-editor-audio-sink")
        elements = (
            self.audio_mixer,
            convert,
            resample,
            self.master_volume,
            self.audio_sink,
        )
        if any(element is None for element in elements):
            raise RuntimeError(_("The GStreamer audio preview is unavailable"))
        for element in elements:
            self.pipeline.add(element)
        if (
            not self.audio_mixer.link(convert)
            or not convert.link(resample)
            or not resample.link(self.master_volume)
            or not self.master_volume.link(self.audio_sink)
        ):
            raise RuntimeError(_("The GStreamer audio preview could not be linked"))
        self._audio_elements.extend(elements)

    def set_volume(self, volume):
        """Set preview listening volume without modifying the edit model."""
        self.volume = max(0.0, min(1.0, float(volume)))
        _set_listening_volume(
            getattr(self, "master_volume", None),
            getattr(self, "audio_sink", None),
            self.volume,
        )
        for composition in (
            getattr(self, "_timeline_composition", None),
            getattr(self, "_speculative_composition", None),
        ):
            if composition is not None and hasattr(composition, "set_volume"):
                composition.set_volume(self.volume)

    def _on_source_pad_added(self, _source, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or caps.get_size() == 0:
            return
        media_type = caps.get_structure(0).get_name()
        if media_type.startswith("video/"):
            sink_pad = self.video_queue.get_static_pad("sink")
            if sink_pad and not sink_pad.is_linked():
                pad.link(sink_pad)
        elif media_type.startswith("audio/"):
            self._link_audio_pad(pad)

    def _link_audio_pad(self, pad):
        if self.audio_mixer is None or self._next_audio_track >= len(
            self.project.source.audio_tracks
        ):
            return
        track = self.project.source.audio_tracks[self._next_audio_track]
        self._next_audio_track += 1
        queue = Gst.ElementFactory.make("queue", f"clipper-editor-{track.id}-queue")
        convert = Gst.ElementFactory.make("audioconvert", f"clipper-editor-{track.id}-convert")
        resample = Gst.ElementFactory.make("audioresample", f"clipper-editor-{track.id}-resample")
        volume = Gst.ElementFactory.make("volume", f"clipper-editor-{track.id}-volume")
        level = Gst.ElementFactory.make("level", f"clipper-editor-{track.id}-level")
        elements = (queue, convert, resample, volume, level)
        if any(element is None for element in elements):
            return
        _configure_level(level)
        for element in elements:
            self.pipeline.add(element)
        if not queue.link(convert) or not convert.link(resample) or not resample.link(volume):
            return
        if not volume.link(level) or not level.link(self.audio_mixer):
            return
        sink_pad = queue.get_static_pad("sink")
        if sink_pad is None or pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            return
        self._audio_elements.extend(elements)
        self.audio_volumes[track.id] = volume
        self.audio_level_track_ids[level.get_name()] = track.id
        for element in elements:
            element.sync_state_with_parent()
        self._apply_audio_settings()

    def _poll_position(self):
        if getattr(self, "_closing", False) or not getattr(self, "_source_valid", True):
            self._position_timer = None
            return False
        composition = getattr(self, "_timeline_composition", None)
        if composition is not None:
            previous = self.output_position_us
            self.output_position_us = composition.position_us()
            if self.changed_callback and previous != self.output_position_us:
                self.changed_callback(self.output_position_us)
        else:
            self._sync_position_from_pipeline()
        keep_polling = self.playing or (
            getattr(self, "_pending_source_seek_us", None) is not None
            and time.monotonic() < getattr(self, "_position_poll_deadline", 0)
        )
        if not keep_polling:
            self._position_timer = None
        return keep_polling

    def _on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            _log_pipeline_error(message, "Source preview pipeline failed")
        if (
            message.type == Gst.MessageType.ASYNC_DONE
            and not getattr(self, "_closing", False)
            and getattr(self, "_source_valid", True)
            and getattr(self, "_timeline_composition", None) is None
        ):
            # A delayed completion can outlive the bounded paused-seek timer.
            # Query the current target; an unrelated completion cannot clear it.
            self._sync_position_from_pipeline()
        _dispatch_level_message(
            message,
            self.audio_level_track_ids,
            self.level_callback,
        )

    def _sync_position_from_pipeline(self):
        previous = self.output_position_us
        success, source_ns = self.pipeline.query_position(Gst.Format.TIME)
        if not success:
            return None
        source_us = source_ns // 1000
        pending_source_us = getattr(self, "_pending_source_seek_us", None)
        if pending_source_us is not None:
            if abs(source_us - pending_source_us) > SEEK_SETTLE_TOLERANCE_US:
                return source_us
            self._pending_source_seek_us = None

        cursor = 0
        for index, segment in enumerate(self.project.segments):
            output_end_us = cursor + segment.duration_us
            if index + 1 < len(self.project.segments):
                next_segment = self.project.segments[index + 1]
                if (
                    self.playing
                    and segment.source_end_us < next_segment.source_start_us
                    and self.output_position_us < output_end_us
                    and source_us >= segment.source_end_us
                ):
                    self.seek_output(output_end_us)
                    return next_segment.source_start_us
            elif (
                self.playing
                and self.output_position_us < self.project.output_duration_us
                and source_us >= segment.source_end_us
            ):
                self._finish_playback()
                return segment.source_end_us

            is_final_segment = index == len(self.project.segments) - 1
            if segment.source_start_us <= source_us < segment.source_end_us or (
                is_final_segment and source_us == segment.source_end_us
            ):
                self.output_position_us = cursor + source_us - segment.source_start_us
                self._apply_audio_settings(segment)
                if self.changed_callback and previous != self.output_position_us:
                    self.changed_callback(self.output_position_us)
                break
            cursor += segment.duration_us
        return source_us

    def seek_output(self, output_us: int, *, accurate: bool = True) -> None:
        if self.playing:
            self._hold_current_frame()
            self._set_loading(True)
        had_composition = any(
            getattr(self, attribute, None) is not None
            for attribute in ("_timeline_composition", "_speculative_composition")
        )
        if had_composition and not self.playing:
            self._show_paintable(self._legacy_paintable)
        self._invalidate_preparation()
        if had_composition:
            self.pipeline.set_state(Gst.State.PAUSED)
        self.output_position_us = max(0, min(output_us, self.project.output_duration_us))
        self._apply_audio_settings()
        if accurate and self.playing and self._has_source_gap():
            self.pipeline.set_state(Gst.State.PAUSED)
            if self._prepare_timeline(play_requested=self.playing):
                if self.changed_callback:
                    self.changed_callback(self.output_position_us)
                return
            self._show_paintable(self._legacy_paintable)
            self._set_loading(False)
        source_us = self.project.output_to_source_us(self.output_position_us)
        self._watch_source_seek(source_us)
        seek_flags = Gst.SeekFlags.FLUSH | (
            Gst.SeekFlags.ACCURATE if accurate else Gst.SeekFlags.KEY_UNIT
        )
        self.pipeline.seek_simple(
            Gst.Format.TIME,
            seek_flags,
            source_us * 1000,
        )
        if self.playing:
            self.pipeline.set_state(Gst.State.PLAYING)
        if self.changed_callback:
            self.changed_callback(self.output_position_us)

    def seek_scrub_frame(self, output_us: int) -> None:
        """Show an exact frame without building or advancing the play timeline."""
        if self.playing:
            self._hold_current_frame()
            self._set_loading(True)
        had_composition = any(
            getattr(self, attribute, None) is not None
            for attribute in ("_timeline_composition", "_speculative_composition")
        )
        if had_composition:
            if not self.playing:
                self._show_paintable(self._legacy_paintable)
            self._invalidate_preparation()
        else:
            # Pointer motion must also make an already scheduled callback stale.
            self._invalidate_preparation()
        self.pipeline.set_state(Gst.State.PAUSED)
        self.output_position_us = max(
            0,
            min(output_us, self.project.output_duration_us),
        )
        self._apply_audio_settings()
        source_us = self.project.output_to_source_us(self.output_position_us)
        self._watch_source_seek(source_us)
        self.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            source_us * 1000,
        )
        if self.changed_callback:
            self.changed_callback(self.output_position_us)

    def play(self):
        if self.playing:
            return
        if self.output_position_us >= self.project.output_duration_us:
            self.seek_output(0)
        if self._has_source_gap():
            if self._play_timeline():
                return
        self._play_legacy()

    def _play_legacy(self):
        self._hold_current_frame()
        self._set_loading(True)
        if self.output_position_us >= self.project.output_duration_us:
            self.seek_output(0)
            resume_source_us = self.project.output_to_source_us(0)
        else:
            resume_source_us = self._sync_position_from_pipeline()
            if resume_source_us is None:
                resume_source_us = self.project.output_to_source_us(self.output_position_us)
        self._resume_output_us = self.output_position_us
        self._resume_source_us = resume_source_us
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._show_paintable(self._legacy_paintable)
            self._set_loading(False)
            return
        self._set_playing(True)
        self._cancel_resume_watchdog()
        self._resume_watchdog_id = GLib.timeout_add(
            RESUME_WATCHDOG_MS,
            self._recover_stalled_resume,
        )

    def _has_source_gap(self):
        return any(
            left.source_end_us < right.source_start_us
            for left, right in zip(
                self.project.segments, self.project.segments[1:], strict=False
            )
        )

    @staticmethod
    def _project_identity(project, output_start_us):
        """Return an immutable key for every input that shapes a composition."""
        source = getattr(project, "source", None)
        source_identity = (
            getattr(source, "path", None),
            getattr(source, "size", None),
            getattr(source, "mtime_ns", None),
        )
        track_ids = tuple(
            track.id for track in getattr(source, "audio_tracks", ())
        )
        segments = []
        for segment in project.segments:
            audio = getattr(segment, "audio", {})
            ordered_track_ids = track_ids or tuple(sorted(audio))
            segments.append(
                (
                    getattr(segment, "id", None),
                    segment.source_start_us,
                    segment.source_end_us,
                    tuple(
                        (
                            track_id,
                            float(audio[track_id].gain),
                            bool(audio[track_id].muted),
                        )
                        for track_id in ordered_track_ids
                    ),
                )
            )
        return (int(output_start_us), source_identity, tuple(segments))

    def _cancel_idle_preparation_timers(self):
        for attribute in ("_idle_prepare_id", "_idle_prepare_deadline_id"):
            timer_id = getattr(self, attribute, None)
            if timer_id is not None:
                GLib.source_remove(timer_id)
                setattr(self, attribute, None)
        self._scheduled_prepare_identity = None

    def _discard_composition(self, attribute):
        composition = getattr(self, attribute, None)
        if composition is None:
            return None
        setattr(self, attribute, None)
        active = getattr(self, "_timeline_composition", None)
        speculative = getattr(self, "_speculative_composition", None)
        if composition is not active and composition is not speculative:
            composition.cleanup()
        if attribute == "_speculative_composition":
            self._speculative_identity = None
        return composition

    def _invalidate_preparation(self, *, discard_active=True):
        """Cancel every pending request and retire invalid decoder state."""
        self._preparation_generation = getattr(self, "_preparation_generation", 0) + 1
        self._cancel_idle_preparation_timers()
        self._discard_composition("_speculative_composition")
        if discard_active:
            self._discard_composition("_timeline_composition")

    def _schedule_idle_preparation(self):
        if (
            self.playing
            or getattr(self, "_closing", False)
            or not getattr(self, "_source_valid", True)
            or not self._has_source_gap()
            or getattr(self, "_timeline_composition", None) is not None
        ):
            return False
        identity = self._project_identity(self.project, self.output_position_us)
        speculative = getattr(self, "_speculative_composition", None)
        if (
            speculative is not None
            and getattr(self, "_speculative_identity", None) == identity
        ):
            return True
        if (
            getattr(self, "_idle_prepare_id", None) is not None
            and getattr(self, "_scheduled_prepare_identity", None) == identity
        ):
            return True
        if (
            getattr(self, "_idle_prepare_id", None) is not None
            or getattr(self, "_speculative_composition", None) is not None
        ):
            self._preparation_generation = (
                getattr(self, "_preparation_generation", 0) + 1
            )
        self._cancel_idle_preparation_timers()
        self._discard_composition("_speculative_composition")
        generation = getattr(self, "_preparation_generation", 0)
        self._scheduled_prepare_identity = identity
        self._idle_prepare_id = GLib.timeout_add(
            IDLE_PREPARE_DEBOUNCE_MS,
            lambda: self._begin_speculative_preparation(generation, identity),
        )
        return True

    def _begin_speculative_preparation(self, generation, identity):
        if (
            generation != getattr(self, "_preparation_generation", 0)
            or identity != getattr(self, "_scheduled_prepare_identity", None)
        ):
            return False
        self._idle_prepare_id = None
        self._scheduled_prepare_identity = None
        if (
            self.playing
            or getattr(self, "_closing", False)
            or not getattr(self, "_source_valid", True)
            or not self._has_source_gap()
            or identity
            != self._project_identity(self.project, self.output_position_us)
        ):
            return False
        try:
            composition = TimelineComposition(
                self.project,
                self.output_position_us,
                self._on_timeline_eos,
                self._on_timeline_error,
                self._on_timeline_ready,
            )
        except Exception:
            return False
        if hasattr(composition, "set_volume"):
            composition.set_volume(getattr(self, "volume", 1.0))
        composition.level_callback = getattr(self, "level_callback", None)
        composition.playback_started_callback = self._on_timeline_playback_started
        composition.preparation_generation = generation
        composition.cache_identity = identity
        self._speculative_composition = composition
        self._speculative_identity = identity
        if not composition.prepare():
            self._discard_composition("_speculative_composition")
            return False
        self._idle_prepare_deadline_id = GLib.timeout_add(
            IDLE_PREPARE_BUDGET_MS,
            lambda: self._stop_speculative_growth(generation, composition),
        )
        return False

    def _stop_speculative_growth(self, generation, composition):
        if (
            generation == getattr(self, "_preparation_generation", 0)
            and composition is getattr(self, "_speculative_composition", None)
        ):
            self._idle_prepare_deadline_id = None
            composition.stop_speculative_growth()
        return False

    def _adopt_speculative_composition(self):
        composition = getattr(self, "_speculative_composition", None)
        identity = self._project_identity(self.project, self.output_position_us)
        if composition is None:
            return None
        if (
            getattr(self, "_speculative_identity", None) != identity
            or getattr(
                composition,
                "preparation_generation",
                getattr(self, "_preparation_generation", 0),
            )
            != getattr(self, "_preparation_generation", 0)
        ):
            self._discard_composition("_speculative_composition")
            return None
        self._cancel_idle_preparation_timers()
        self._speculative_composition = None
        self._speculative_identity = None
        self._timeline_composition = composition
        return composition

    def _play_timeline(self):
        self._cancel_resume_watchdog()
        self.pipeline.set_state(Gst.State.PAUSED)
        self._hold_current_frame()
        self._set_loading(True)
        composition = getattr(self, "_timeline_composition", None)
        adopted = False
        if composition is None:
            composition = self._adopt_speculative_composition()
            adopted = composition is not None
        if composition is not None:
            composition.loading_generation = self._loading_generation
            ready = (
                composition.ready_for_playback()
                if hasattr(composition, "ready_for_playback")
                else True
            )
            if not adopted or ready:
                self._show_paintable(composition.paintable)
            if not self.playing:
                self._set_playing(True)
            return composition.start()
        self._cancel_idle_preparation_timers()
        return self._prepare_timeline(play_requested=True)

    def _prepare_timeline(self, *, play_requested=False):
        if play_requested:
            self._hold_current_frame()
            self._set_loading(True)
        try:
            composition = TimelineComposition(
                self.project,
                self.output_position_us,
                self._on_timeline_eos,
                self._on_timeline_error,
                self._on_timeline_ready,
            )
        except Exception:
            _LOG.exception("Could not build timeline preview at position_us=%s",
                           self.output_position_us)
            self.pipeline.set_state(Gst.State.PAUSED)
            self._show_paintable(self._legacy_paintable)
            self._set_loading(False)
            return False
        if hasattr(composition, "set_volume"):
            composition.set_volume(getattr(self, "volume", 1.0))
        self._timeline_composition = composition
        composition.level_callback = getattr(self, "level_callback", None)
        composition.playback_started_callback = self._on_timeline_playback_started
        composition.loading_generation = self._loading_generation
        composition.preparation_generation = getattr(
            self,
            "_preparation_generation",
            0,
        )
        composition.cache_identity = self._project_identity(
            self.project,
            self.output_position_us,
        )
        if not composition.prepare():
            _LOG.warning("Timeline preparation failed at position_us=%s",
                         self.output_position_us)
            composition.cleanup()
            self._timeline_composition = None
            self._show_paintable(self._legacy_paintable)
            self._set_loading(False)
            return False
        if play_requested and not self.playing:
            self._set_playing(True)
        if play_requested:
            return composition.start()
        return True

    def _on_timeline_ready(self, composition):
        if (
            composition is not self._timeline_composition
            or getattr(
                composition,
                "preparation_generation",
                getattr(self, "_preparation_generation", 0),
            )
            != getattr(self, "_preparation_generation", 0)
        ):
            return
        self._show_paintable(composition.paintable)

    def _on_timeline_playback_started(self, composition, generation=None):
        if (
            composition is not self._timeline_composition
            or getattr(
                composition,
                "preparation_generation",
                getattr(self, "_preparation_generation", 0),
            )
            != getattr(self, "_preparation_generation", 0)
            or (
                generation is not None
                and generation != self._loading_generation
            )
        ):
            return False
        self._show_paintable(composition.paintable)
        self._set_loading(False)
        return False

    def pause(self):
        if not self.playing:
            return
        self._set_loading(False)
        composition = getattr(self, "_timeline_composition", None)
        if composition is not None:
            self.output_position_us = composition.position_us()
            if not composition.pause():
                return
            self._set_playing(False)
            if self.changed_callback:
                self.changed_callback(self.output_position_us)
            return
        self._cancel_resume_watchdog()
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            return
        self._set_playing(False)
        self._sync_position_from_pipeline()

    def _legacy_seek(self, output_us):
        self.output_position_us = max(
            0,
            min(output_us, self.project.output_duration_us),
        )
        self._apply_audio_settings()
        source_us = self.project.output_to_source_us(self.output_position_us)
        self._watch_source_seek(source_us)
        self.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            source_us * 1000,
        )
        if self.changed_callback:
            self.changed_callback(self.output_position_us)

    def finish_seek(self):
        if (
            self.playing
            and self._has_source_gap()
            and self._timeline_composition is None
        ):
            self.pipeline.set_state(Gst.State.PAUSED)
            self._prepare_timeline(play_requested=True)
        elif not self.playing:
            self._schedule_idle_preparation()

    def _on_timeline_eos(self, composition):
        if (
            composition is not self._timeline_composition
            or getattr(
                composition,
                "preparation_generation",
                getattr(self, "_preparation_generation", 0),
            )
            != getattr(self, "_preparation_generation", 0)
        ):
            return
        composition = self._timeline_composition
        if composition is None:
            return
        self.output_position_us = self.project.output_duration_us
        composition.pipeline.set_state(Gst.State.PAUSED)
        self._set_playing(False)
        if self.changed_callback:
            self.changed_callback(self.output_position_us)

    def _on_timeline_error(self, composition, _message):
        if composition is getattr(self, "_speculative_composition", None):
            _LOG.info("Discarding failed background timeline preparation")
            if (
                getattr(composition, "preparation_generation", None)
                == getattr(self, "_preparation_generation", 0)
            ):
                self._cancel_idle_preparation_timers()
                self._discard_composition("_speculative_composition")
            return
        if (
            composition is not self._timeline_composition
            or getattr(
                composition,
                "preparation_generation",
                getattr(self, "_preparation_generation", 0),
            )
            != getattr(self, "_preparation_generation", 0)
        ):
            return
        composition = self._timeline_composition
        if composition is None:
            return
        output_us = composition.position_us()
        was_playing = self.playing
        _LOG.warning("Falling back to source preview: position_us=%s playing=%s",
                     output_us, was_playing)
        composition.cleanup()
        self._timeline_composition = None
        self.pipeline.set_state(Gst.State.PAUSED)
        self._show_paintable(self._legacy_paintable)
        self._set_loading(False)
        self._legacy_seek(output_us)
        if was_playing:
            self._play_legacy()

    def _finish_playback(self):
        self._cancel_resume_watchdog()
        self._set_loading(False)
        self.output_position_us = self.project.output_duration_us
        self._apply_audio_settings(self.project.segments[-1])
        self.pipeline.set_state(Gst.State.PAUSED)
        self._set_playing(False)
        if self.changed_callback:
            self.changed_callback(self.output_position_us)

    def _recover_stalled_resume(self):
        self._resume_watchdog_id = None
        if not self.playing:
            return False
        success, source_ns = self.pipeline.query_position(Gst.Format.TIME)
        if success and source_ns // 1000 >= self._resume_source_us + RESUME_PROGRESS_US:
            return False
        # Some playbin/sink combinations occasionally fail to leave their paused
        # preroll. Retry from the captured output position only in that case.
        _LOG.warning("Preview resume stalled; retrying from position_us=%s",
                     self._resume_output_us)
        self.seek_output(self._resume_output_us)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._set_playing(False)
        return False

    def _cancel_resume_watchdog(self):
        if self._resume_watchdog_id is not None:
            GLib.source_remove(self._resume_watchdog_id)
            self._resume_watchdog_id = None

    def toggle_playback(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def _set_playing(self, playing):
        self.playing = playing
        if playing:
            self._ensure_position_timer()
        else:
            self._stop_position_timer()
        if not playing:
            self._set_loading(False)
        if self.state_changed_callback:
            self.state_changed_callback(playing)

    def _apply_audio_settings(self, segment=None):
        volumes = getattr(self, "audio_volumes", {})
        if not volumes:
            return
        if segment is None:
            segment = self.project.segment_at_output(self.output_position_us)[0]
        for track_id, volume in volumes.items():
            _set_track_gain(volume, effective_audio_gain(segment.audio[track_id]))

    def update_project(self, project, seek=True):
        was_playing = self.playing
        new_structure = self._project_structure(project)
        structure_changed = getattr(
            self,
            "_project_structure_snapshot",
            self._project_structure(self.project),
        ) != new_structure
        old_cache_snapshot = getattr(
            self,
            "_project_cache_snapshot",
            self._project_identity(self.project, 0),
        )
        new_cache_snapshot = self._project_identity(project, 0)
        source_changed = old_cache_snapshot[1] != new_cache_snapshot[1]
        audio_changed = (
            not structure_changed
            and not source_changed
            and old_cache_snapshot != new_cache_snapshot
        )
        self._project_structure_snapshot = new_structure
        self._project_cache_snapshot = new_cache_snapshot

        if structure_changed or source_changed:
            composition = getattr(self, "_timeline_composition", None)
            if was_playing:
                self._hold_current_frame()
                self._set_loading(True)
            if composition is not None:
                self.output_position_us = composition.position_us()
            self._invalidate_preparation()
            if source_changed:
                self._source_valid = False
            self.pipeline.set_state(Gst.State.PAUSED)
            if not was_playing:
                self._show_paintable(self._legacy_paintable)
        self.project = project
        self.output_position_us = min(
            self.output_position_us,
            project.output_duration_us,
        )

        if structure_changed or source_changed:
            self._cancel_resume_watchdog()
            if was_playing:
                self._set_playing(False)
                self._show_paintable(self._legacy_paintable)
            self._legacy_seek(self.output_position_us)
            if not source_changed:
                self._schedule_idle_preparation()
            return

        if audio_changed:
            for composition in (
                getattr(self, "_timeline_composition", None),
                getattr(self, "_speculative_composition", None),
            ):
                if composition is None:
                    continue
                composition.update_audio(project)
                composition.cache_identity = self._project_identity(
                    project,
                    getattr(composition, "output_start_us", self.output_position_us),
                )
            if getattr(self, "_speculative_composition", None) is not None:
                self._speculative_identity = self._project_identity(
                    project,
                    self.output_position_us,
                )

        if (
            getattr(self, "_timeline_composition", None) is None
            and getattr(self, "_speculative_composition", None) is None
        ):
            self._apply_audio_settings()
            if seek:
                self._legacy_seek(self.output_position_us)
                self._schedule_idle_preparation()

    def update_audio_setting(self, project, segment_id, track_id):
        """Apply a single gain/mute edit without rebuilding timeline state."""
        self.project = project
        if hasattr(project, "segments"):
            self._project_cache_snapshot = self._project_identity(project, 0)
        compositions = tuple(
            composition
            for composition in (
                getattr(self, "_timeline_composition", None),
                getattr(self, "_speculative_composition", None),
            )
            if composition is not None
        )
        for composition in compositions:
            composition.update_audio_setting(project, segment_id, track_id)
            composition.cache_identity = self._project_identity(
                project,
                getattr(composition, "output_start_us", self.output_position_us),
            )
        if getattr(self, "_speculative_composition", None) is not None:
            self._speculative_identity = self._project_identity(
                project,
                self.output_position_us,
            )
        if compositions:
            return
        segment = project.segment_at_output(self.output_position_us)[0]
        if getattr(segment, "id", None) == segment_id:
            volume = getattr(self, "audio_volumes", {}).get(track_id)
            if volume is not None:
                _set_track_gain(
                    volume,
                    effective_audio_gain(segment.audio[track_id]),
                )
        if hasattr(project, "segments"):
            self._schedule_idle_preparation()

    @staticmethod
    def _project_structure(project):
        return tuple(
            (
                getattr(segment, "id", None),
                segment.source_start_us,
                segment.source_end_us,
            )
            for segment in project.segments
        )

    def invalidate_source(self):
        """Stop all decoder work after the monitored source identity changes."""
        self._source_valid = False
        self._stop_position_timer()
        composition = getattr(self, "_timeline_composition", None)
        if composition is not None:
            self.output_position_us = composition.position_us()
            self._hold_current_frame()
        self._cancel_resume_watchdog()
        self._invalidate_preparation()
        self.pipeline.set_state(Gst.State.PAUSED)
        self._set_loading(False)
        if self.playing:
            self._set_playing(False)

    def cleanup(self):
        self._closing = True
        self._cancel_resume_watchdog()
        self._set_loading(False)
        self._set_playing(False)
        self._invalidate_preparation()
        self.pipeline.set_state(Gst.State.NULL)
        self.bus.disconnect(self._bus_handler_id)
        self.bus.remove_signal_watch()
        self.picture.set_paintable(None)
