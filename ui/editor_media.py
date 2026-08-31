"""FFprobe normalization for editor projects."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from editor_model import MAX_AUDIO_TRACKS, AudioTrack, Source
from media_tools import MediaToolError, run_media_tool


class UnsupportedMediaError(ValueError):
    pass


def _duration_us(format_data: dict, video: dict) -> int:
    value = format_data.get("duration") or video.get("duration")
    try:
        if value is None:
            raise ValueError("missing duration")
        return round(float(value) * 1_000_000)
    except (TypeError, ValueError):
        duration_ts = video.get("duration_ts")
        time_base = video.get("time_base")
        if duration_ts is not None and time_base:
            return round(int(duration_ts) * float(Fraction(time_base)) * 1_000_000)
    raise UnsupportedMediaError("The clip duration could not be determined")


def _rate(value) -> tuple[int, int]:
    try:
        rate = Fraction(value)
        if rate > 0:
            return rate.numerator, rate.denominator
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return 0, 1


def normalize_probe(path: str | Path, data: dict, stat=None) -> Source:
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise UnsupportedMediaError("FFprobe returned no stream information")
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not videos:
        raise UnsupportedMediaError("The file has no video stream")
    if len(audio) > MAX_AUDIO_TRACKS:
        raise UnsupportedMediaError("Clips with more than six audio tracks are not supported")
    video = videos[0]
    canonical = Path(path).expanduser().resolve()
    stat = stat or canonical.stat()
    audio_tracks = []
    for ordinal, stream in enumerate(audio):
        tags = stream.get("tags") or {}
        title = str(tags.get("title") or "").strip()
        language = str(tags.get("language") or "und").strip() or "und"
        audio_tracks.append(
            AudioTrack(
                id=f"a{ordinal}",
                ordinal=ordinal,
                stream_index=int(stream["index"]),
                label=title or f"Track {ordinal + 1}",
                title=title,
                codec=str(stream.get("codec_name") or ""),
                channels=int(stream.get("channels") or 0),
                language=language,
                channel_layout=str(stream.get("channel_layout") or ""),
                disposition=dict(stream.get("disposition") or {}),
            )
        )
    avg = _rate(video.get("avg_frame_rate"))
    nominal = _rate(video.get("r_frame_rate"))
    raw_sample_aspect_ratio = video.get("sample_aspect_ratio")
    sample_aspect_ratio = _rate(
        str(raw_sample_aspect_ratio).replace(":", "/", 1)
        if raw_sample_aspect_ratio is not None
        else None
    )
    if sample_aspect_ratio[0] <= 0:
        sample_aspect_ratio = (1, 1)
    side_data = video.get("side_data_list") or []
    rotation = int((video.get("tags") or {}).get("rotate") or 0)
    for entry in side_data:
        if "rotation" in entry:
            rotation = int(entry["rotation"])
    return Source(
        path=str(canonical),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        duration_us=_duration_us(data.get("format") or {}, video),
        video_stream_index=int(video["index"]),
        audio_tracks=audio_tracks,
        frame_rate_num=avg[0],
        frame_rate_den=avg[1],
        variable_frame_rate=bool(avg[0] and nominal[0] and avg != nominal),
        video_codec=str(video.get("codec_name") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        rotation=rotation,
        sample_aspect_ratio_num=sample_aspect_ratio[0],
        sample_aspect_ratio_den=sample_aspect_ratio[1],
    )


def probe_media(path: str | Path) -> Source:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    result = run_media_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ],
        timeout=30,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MediaToolError("FFprobe returned invalid metadata") from error
    return normalize_probe(path, data)
