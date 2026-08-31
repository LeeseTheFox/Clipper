"""GTK-independent non-destructive editor project model."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, field

PROJECT_VERSION = 1
MAX_AUDIO_TRACKS = 6
MIN_GAIN = 0.0
MIN_GAIN_DB = -100.0
MAX_GAIN_DB = 30.0
MAX_GAIN = 10 ** (MAX_GAIN_DB / 20)


class ProjectValidationError(ValueError):
    pass


@dataclass
class AudioTrack:
    id: str
    ordinal: int
    stream_index: int
    label: str
    codec: str = ""
    channels: int = 0
    language: str = "und"
    title: str = ""
    channel_layout: str = ""
    disposition: dict[str, int] = field(default_factory=dict)


@dataclass
class Source:
    path: str
    size: int
    mtime_ns: int
    duration_us: int
    video_stream_index: int
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    frame_rate_num: int = 0
    frame_rate_den: int = 1
    variable_frame_rate: bool = False
    video_codec: str = ""
    width: int = 0
    height: int = 0
    rotation: int = 0
    sample_aspect_ratio_num: int = 1
    sample_aspect_ratio_den: int = 1

    @property
    def frame_duration_us(self) -> int:
        if self.frame_rate_num <= 0:
            return 1
        return max(1, round(1_000_000 * self.frame_rate_den / self.frame_rate_num))

    def stream_signature(self) -> list[tuple]:
        return [
            (track.ordinal, track.stream_index, track.codec, track.channels)
            for track in self.audio_tracks
        ]


@dataclass
class AudioSetting:
    gain: float = 1.0
    muted: bool = False


def effective_audio_gain(setting: AudioSetting) -> float:
    return 0.0 if setting.muted else setting.gain


@dataclass
class Segment:
    id: str
    source_start_us: int
    source_end_us: int
    audio: dict[str, AudioSetting] = field(default_factory=dict)

    @property
    def duration_us(self) -> int:
        return self.source_end_us - self.source_start_us


@dataclass
class EditorProject:
    version: int
    project_id: str
    source: Source
    segments: list[Segment]

    @classmethod
    def new(cls, source: Source) -> EditorProject:
        settings = {track.id: AudioSetting() for track in source.audio_tracks}
        project = cls(
            PROJECT_VERSION,
            str(uuid.uuid4()),
            source,
            [Segment(str(uuid.uuid4()), 0, source.duration_us, settings)],
        )
        project.validate()
        return project

    @classmethod
    def from_dict(cls, value: dict) -> EditorProject:
        try:
            source_data = dict(value["source"])
            source_data["audio_tracks"] = [
                AudioTrack(**track) for track in source_data.get("audio_tracks", [])
            ]
            source = Source(**source_data)
            segments = []
            for raw in value["segments"]:
                raw = dict(raw)
                raw["audio"] = {
                    key: AudioSetting(**setting) for key, setting in raw["audio"].items()
                }
                segments.append(Segment(**raw))
            project = cls(
                version=value["version"],
                project_id=value["project_id"],
                source=source,
                segments=segments,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProjectValidationError(f"Invalid project data: {error}") from error
        project.validate()
        return project

    def to_dict(self) -> dict:
        return asdict(self)

    def clone(self) -> EditorProject:
        return copy.deepcopy(self)

    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def output_duration_us(self) -> int:
        return sum(segment.duration_us for segment in self.segments)

    def validate(self) -> None:
        if self.version != PROJECT_VERSION:
            raise ProjectValidationError(f"Unsupported project version {self.version}")
        if not self.project_id or self.source.duration_us <= 0:
            raise ProjectValidationError("Project and source durations must be valid")
        if not 0 <= len(self.source.audio_tracks) <= MAX_AUDIO_TRACKS:
            raise ProjectValidationError("Only zero to six audio tracks are supported")
        track_ids = [track.id for track in self.source.audio_tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ProjectValidationError("Audio track IDs must be unique")
        if [track.ordinal for track in self.source.audio_tracks] != list(range(len(track_ids))):
            raise ProjectValidationError("Audio track ordinals must be contiguous")
        if not self.segments:
            raise ProjectValidationError("The output timeline cannot be empty")
        segment_ids = [segment.id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ProjectValidationError("Segment IDs must be unique")
        previous_end = 0
        for segment in self.segments:
            if not 0 <= segment.source_start_us < segment.source_end_us <= self.source.duration_us:
                raise ProjectValidationError("Segment range is outside the source")
            if segment.source_start_us < previous_end:
                raise ProjectValidationError(
                    "Segments must remain non-overlapping and in source order"
                )
            previous_end = segment.source_end_us
            if set(segment.audio) != set(track_ids):
                raise ProjectValidationError("Segment audio mappings are stale")
            for setting in segment.audio.values():
                if (
                    not math.isfinite(setting.gain)
                    or not MIN_GAIN <= setting.gain <= MAX_GAIN
                    or not isinstance(setting.muted, bool)
                ):
                    raise ProjectValidationError("Invalid audio setting")

    def segment_index(self, segment_id: str) -> int:
        for index, segment in enumerate(self.segments):
            if segment.id == segment_id:
                return index
        raise KeyError(segment_id)

    def output_start_us(self, segment_id: str) -> int:
        index = self.segment_index(segment_id)
        return sum(segment.duration_us for segment in self.segments[:index])

    def segment_at_output(self, output_us: int) -> tuple[Segment, int]:
        if not 0 <= output_us <= self.output_duration_us:
            raise ValueError("Output time is outside the timeline")
        cursor = 0
        for index, segment in enumerate(self.segments):
            end = cursor + segment.duration_us
            if output_us < end or (output_us == end and index == len(self.segments) - 1):
                return segment, min(output_us - cursor, segment.duration_us)
            cursor = end
        raise ValueError("Output time is outside the timeline")

    def output_to_source_us(self, output_us: int) -> int:
        segment, offset = self.segment_at_output(output_us)
        return min(segment.source_end_us, segment.source_start_us + offset)

    def split(self, segment_id: str, source_us: int) -> tuple[str, str]:
        index = self.segment_index(segment_id)
        segment = self.segments[index]
        minimum = 1 if self.source.variable_frame_rate else self.source.frame_duration_us
        if not segment.source_start_us + minimum <= source_us <= segment.source_end_us - minimum:
            raise ValueError("Cut is too close to a segment edge")
        if not self.source.variable_frame_rate and self.source.frame_rate_num > 0:
            frame = self.source.frame_duration_us
            source_us = round(source_us / frame) * frame
            source_us = max(
                segment.source_start_us + frame,
                min(source_us, segment.source_end_us - frame),
            )
        left = Segment(
            str(uuid.uuid4()),
            segment.source_start_us,
            source_us,
            copy.deepcopy(segment.audio),
        )
        right = Segment(
            str(uuid.uuid4()),
            source_us,
            segment.source_end_us,
            copy.deepcopy(segment.audio),
        )
        self.segments[index : index + 1] = [left, right]
        self.validate()
        return left.id, right.id

    def delete_many(self, segment_ids) -> str:
        """Delete a non-empty subset and return the nearest surviving segment."""
        requested = set(segment_ids)
        if not requested:
            raise ValueError("At least one segment must be selected")
        known_ids = {segment.id for segment in self.segments}
        unknown_ids = requested - known_ids
        if unknown_ids:
            raise KeyError(next(iter(unknown_ids)))
        if len(requested) >= len(self.segments):
            raise ValueError("The final segment cannot be deleted")
        first_index = min(self.segment_index(segment_id) for segment_id in requested)
        self.segments[:] = [
            segment for segment in self.segments if segment.id not in requested
        ]
        self.validate()
        return self.segments[min(first_index, len(self.segments) - 1)].id

    def delete(self, segment_id: str) -> str:
        return self.delete_many((segment_id,))

    def set_gain(self, segment_id: str, track_id: str, gain: float) -> None:
        if not math.isfinite(gain) or not MIN_GAIN <= gain <= MAX_GAIN:
            raise ValueError(
                f"Gain must be between {MIN_GAIN} and {MAX_GAIN}"
            )
        setting = self.segments[self.segment_index(segment_id)].audio[track_id]
        setting.gain = float(gain)
        setting.muted = setting.gain == MIN_GAIN

    def set_muted(self, segment_id: str, track_id: str, muted: bool) -> None:
        setting = self.segments[self.segment_index(segment_id)].audio[track_id]
        setting.muted = bool(muted)
        if setting.muted:
            setting.gain = MIN_GAIN
        elif setting.gain == MIN_GAIN:
            setting.gain = 1.0
