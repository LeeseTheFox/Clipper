"""Deterministic FFmpeg export graphs, codec options, and process ownership."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from editor_model import EditorProject, effective_audio_gain
from i18n import _
from logs import diagnostic_tail, probe_failure_reason
from media_tools import MediaToolError, clean_external_tool_env, run_media_tool
from quality_controls import (
    CQP_HIGH_QUALITY,
    CQP_LOW_QUALITY,
    RATE_CONTROL_LABELS,
)

FORMATS = {"mkv": ".mkv", "mp4": ".mp4", "mov": ".mov", "ts": ".ts"}
FORMAT_LABELS = {
    "mkv": _("Matroska video (.mkv)"),
    "mp4": _("MPEG-4 (.mp4)"),
    "mov": _("QuickTime (.mov)"),
    "ts": _("MPEG-TS (.ts)"),
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_VAAPI_DEVICE = re.compile(r"^/dev/dri/renderD\d+$")
_HARDWARE_PROBE_SOURCE = "color=c=black:s=640x360:r=30"
_BITRATE_MIN = 1
_BITRATE_MAX = 1_000_000
_OBS_AV1_VAAPI_QP_SCALE = 5
_EXPORT_SHORT_EDGES = (2160, 1440, 1080, 720, 480, 360, 240)
_VAAPI_HEVC_WIDTH_ALIGNMENT = 64
_VAAPI_HEVC_HEIGHT_ALIGNMENT = 16
_LOG = logging.getLogger("clipper.editor.export")


class ExportValidationError(ValueError):
    pass


class ExportCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoCodec:
    id: str
    label: str
    description: str
    encoder: str
    probe_codec: str
    formats: frozenset[str]
    encoder_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class AudioCodec:
    id: str
    label: str
    description: str
    encoder: str
    probe_codec: str
    formats: frozenset[str]
    encoder_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class HardwareEncoderCapability:
    """A hardware encoder/device pair proven usable by a runtime encode probe."""

    backend: str
    device: str
    label: str
    codec: str
    encoder: str
    rate_controls: frozenset[str]


@dataclass(frozen=True)
class ExportEncoderCapabilities:
    available_encoders: frozenset[str]
    hardware_encoders: tuple[HardwareEncoderCapability, ...]


_ALL_FORMATS = frozenset(FORMATS)
_MODERN_FORMATS = frozenset({"mkv", "mp4"})

VIDEO_CODEC_OPTIONS = (
    VideoCodec(
        "h264",
        _("H.264"),
        _("Best playback compatibility"),
        "libx264",
        "h264",
        _ALL_FORMATS,
        ("-preset", "medium"),
    ),
    VideoCodec(
        "hevc",
        _("H265"),
        _("Smaller files with slower encoding"),
        "libx265",
        "hevc",
        _ALL_FORMATS,
        ("-preset", "medium"),
    ),
    VideoCodec(
        "vp9",
        _("VP9"),
        _("Open codec suited to web playback"),
        "libvpx-vp9",
        "vp9",
        _MODERN_FORMATS,
        ("-deadline", "good", "-cpu-used", "4"),
    ),
    VideoCodec(
        "av1",
        _("AV1"),
        _("Best compression with the slowest encoding"),
        "libaom-av1",
        "av1",
        _MODERN_FORMATS,
        ("-usage", "good", "-cpu-used", "6"),
    ),
)

AUDIO_CODEC_OPTIONS = (
    AudioCodec(
        "aac",
        _("AAC"),
        _("Best playback compatibility"),
        "aac",
        "aac",
        _ALL_FORMATS,
        ("-b:a", "256k"),
    ),
    AudioCodec(
        "opus",
        _("Opus"),
        _("Efficient high-quality audio"),
        "libopus",
        "opus",
        _MODERN_FORMATS,
        ("-b:a", "192k"),
    ),
    AudioCodec(
        "flac",
        _("FLAC"),
        _("Lossless audio with larger files"),
        "flac",
        "flac",
        _MODERN_FORMATS,
        ("-compression_level", "5"),
    ),
    AudioCodec(
        "pcm_s16le",
        _("PCM 16-bit"),
        _("Uncompressed audio"),
        "pcm_s16le",
        "pcm_s16le",
        frozenset({"mkv", "mp4", "mov"}),
    ),
)

VIDEO_CODECS = {codec.id: codec for codec in VIDEO_CODEC_OPTIONS}
AUDIO_CODECS = {codec.id: codec for codec in AUDIO_CODEC_OPTIONS}

VAAPI_VIDEO_ENCODERS = {
    "h264": "h264_vaapi",
    "hevc": "hevc_vaapi",
    "vp9": "vp9_vaapi",
    "av1": "av1_vaapi",
}
NVENC_VIDEO_ENCODERS = {
    "h264": "h264_nvenc",
    "hevc": "hevc_nvenc",
    "av1": "av1_nvenc",
}
HARDWARE_VIDEO_ENCODERS = {
    "vaapi": VAAPI_VIDEO_ENCODERS,
    "nvenc": NVENC_VIDEO_ENCODERS,
}

_EXPORT_CAPABILITY_LOCK = threading.Lock()
_EXPORT_CAPABILITY_STARTED = False
_EXPORT_CAPABILITY_RESULT: ExportEncoderCapabilities | None = None


@dataclass(frozen=True)
class ExportOptions:
    folder: Path
    filename: str
    format: str = "mkv"
    quality_cqp: int = 23
    audio_layout: str = "separate"
    video_codec: str = "h264"
    audio_codec: str = "aac"
    rate_control: str = "cqp"
    video_bitrate: int = 12_000
    video_max_bitrate: int = 20_000
    video_encoder: str = "software"
    hardware_device: str | None = None
    output_width: int | None = None
    output_height: int | None = None

    @property
    def extension(self) -> str:
        try:
            return FORMATS[self.format]
        except KeyError as error:
            raise ExportValidationError(_("Unsupported export format")) from error


def _display_aspect_ratio(source) -> Fraction | None:
    """Return the visual width/height ratio, including pixels and rotation."""
    if source.width <= 0 or source.height <= 0:
        return None
    sar_num = source.sample_aspect_ratio_num
    sar_den = source.sample_aspect_ratio_den
    if sar_num <= 0 or sar_den <= 0:
        sar_num = sar_den = 1
    ratio = Fraction(source.width * sar_num, source.height * sar_den)
    if source.rotation % 180:
        ratio = 1 / ratio
    return ratio


def source_display_dimensions(source) -> tuple[int, int] | None:
    """Return rounded square-pixel dimensions for presenting the native size."""
    ratio = _display_aspect_ratio(source)
    if ratio is None:
        return None
    if source.rotation % 180:
        width = source.height
        height = max(1, round(Fraction(width) / ratio))
    else:
        height = source.height
        width = max(1, round(Fraction(height) * ratio))
    return width, height


def _nearest_even(value: Fraction) -> int:
    """Return the closest positive even integer, preferring the smaller on a tie."""
    lower = max(2, (value.numerator // value.denominator) // 2 * 2)
    upper = lower + 2
    return lower if value - lower <= upper - value else upper


def _resolution_for_short_edge(source, short_edge: int) -> tuple[int, int] | None:
    ratio = _display_aspect_ratio(source)
    if ratio is None or short_edge < 2 or short_edge % 2:
        return None
    if ratio >= 1:
        return _nearest_even(Fraction(short_edge) * ratio), short_edge
    return short_edge, _nearest_even(Fraction(short_edge) / ratio)


def export_resolution_options(source) -> tuple[tuple[int | None, int | None], ...]:
    """Return native plus standard lower sizes fitted to the source's exact DAR."""
    ratio = _display_aspect_ratio(source)
    dimensions = source_display_dimensions(source)
    if ratio is None or dimensions is None:
        return ((None, None),)

    native_short_edge = min(
        Fraction(dimensions[1]) * ratio,
        Fraction(dimensions[1]),
    )
    options: list[tuple[int | None, int | None]] = [(None, None)]
    for short_edge in _EXPORT_SHORT_EDGES:
        if short_edge >= native_short_edge:
            continue
        resolution = _resolution_for_short_edge(source, short_edge)
        if resolution is not None and resolution not in options:
            options.append(resolution)
    return tuple(options)


def _export_frame_dimensions(
    project: EditorProject, options: ExportOptions
) -> tuple[int, int] | None:
    if options.output_width is not None and options.output_height is not None:
        return options.output_width, options.output_height
    source = project.source
    if source.width <= 0 or source.height <= 0:
        return None
    if source.rotation % 180:
        return source.height, source.width
    return source.width, source.height


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _vaapi_hevc_surface_padding(
    project: EditorProject, options: ExportOptions
) -> tuple[int, int, int, int] | None:
    """Return an aligned HEVC surface and the padding hidden by its SPS crop."""
    if options.video_encoder != "vaapi" or options.video_codec != "hevc":
        return None
    dimensions = _export_frame_dimensions(project, options)
    if dimensions is None:
        return None
    width, height = dimensions
    if width % 2 or height % 2:
        return None
    surface_width = _align_up(width, _VAAPI_HEVC_WIDTH_ALIGNMENT)
    surface_height = _align_up(height, _VAAPI_HEVC_HEIGHT_ALIGNMENT)
    crop_right = surface_width - width
    crop_bottom = surface_height - height
    if crop_right == 0 and crop_bottom == 0:
        return None
    return surface_width, surface_height, crop_right, crop_bottom


def detect_export_encoders() -> frozenset[str]:
    """Return the FFmpeg encoder names available in the current runtime."""
    try:
        output = run_media_tool(
            ["ffmpeg", "-hide_banner", "-encoders"], timeout=8
        ).stdout
    except MediaToolError:
        return frozenset()

    encoders = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and len(fields[0]) == 6 and fields[0][0] in "VAS":
            encoders.add(fields[1])
    return frozenset(encoders)


def _vaapi_device_name(device: str) -> str:
    driver = ""
    try:
        uevent = (Path("/sys/class/drm") / Path(device).name / "device/uevent").read_text(
            encoding="utf-8"
        )
        for line in uevent.splitlines():
            if line.startswith("DRIVER="):
                driver = line.partition("=")[2].strip().lower()
                break
    except (OSError, UnicodeError):
        pass

    vendor = {
        "amdgpu": "AMD",
        "i915": "Intel",
        "xe": "Intel",
        "nouveau": "NVIDIA",
        "nvidia": "NVIDIA",
    }.get(driver)
    return f"VAAPI — {vendor}" if vendor else f"VAAPI — {Path(device).name}"


def _vaapi_cqp_arguments(encoder: str, quality: int) -> list[str]:
    if encoder == "av1_vaapi":
        # OBS exposes AV1 VAAPI quality on the familiar 0-51 QP scale, while
        # FFmpeg's global_quality is the AV1 base quantizer index (0-255).
        return [
            "-rc_mode",
            "CQP",
            "-global_quality",
            str(quality * _OBS_AV1_VAAPI_QP_SCALE),
        ]
    return ["-rc_mode", "CQP", "-qp", str(quality)]


def _vaapi_rate_control_arguments(rate_control: str, encoder: str) -> list[str]:
    if rate_control == "cqp":
        return _vaapi_cqp_arguments(encoder, 23)
    if rate_control == "cbr":
        return [
            "-rc_mode",
            "CBR",
            "-b:v",
            "1000k",
            "-minrate",
            "1000k",
            "-maxrate",
            "1000k",
            "-bufsize",
            "2000k",
        ]
    return [
        "-rc_mode",
        "VBR",
        "-b:v",
        "1000k",
        "-maxrate",
        "2000k",
        "-bufsize",
        "4000k",
    ]


def _probe_vaapi_encoder(
    device: str, encoder: str, rate_control: str | None = None
) -> bool:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-vaapi_device",
        device,
        "-f",
        "lavfi",
        "-i",
        _HARDWARE_PROBE_SOURCE,
        "-frames:v",
        "1",
        "-vf",
        "format=nv12,hwupload",
        "-an",
        "-c:v",
        encoder,
    ]
    if rate_control is not None:
        command.extend(_vaapi_rate_control_arguments(rate_control, encoder))
    command.extend(["-f", "null", "-"])
    try:
        result = run_media_tool(command, timeout=4, check=False)
    except MediaToolError as error:
        _LOG.info("VAAPI probe unavailable: device=%s encoder=%s mode=%s: %s",
                  device, encoder, rate_control, error)
        return False
    if result.returncode:
        _LOG.info("VAAPI probe rejected: device=%s encoder=%s mode=%s: %s",
                  device, encoder, rate_control or "default", probe_failure_reason(result.stderr))
    return result.returncode == 0


def _nvenc_rate_control_arguments(rate_control: str) -> list[str]:
    if rate_control == "cqp":
        return ["-rc", "constqp", "-qp", "23"]
    if rate_control == "cbr":
        return [
            "-rc",
            "cbr",
            "-b:v",
            "1000k",
            "-minrate",
            "1000k",
            "-maxrate",
            "1000k",
            "-bufsize",
            "2000k",
        ]
    return [
        "-rc",
        "vbr",
        "-b:v",
        "1000k",
        "-maxrate",
        "2000k",
        "-bufsize",
        "4000k",
    ]


def _probe_nvenc_encoder(encoder: str, rate_control: str | None = None) -> bool:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        _HARDWARE_PROBE_SOURCE,
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        encoder,
        "-preset",
        "p4",
    ]
    if rate_control is not None:
        command.extend(_nvenc_rate_control_arguments(rate_control))
    command.extend(["-f", "null", "-"])
    try:
        result = run_media_tool(command, timeout=4, check=False)
    except MediaToolError as error:
        _LOG.info("NVENC probe unavailable: encoder=%s mode=%s: %s",
                  encoder, rate_control, error)
        return False
    if result.returncode:
        _LOG.info("NVENC probe rejected: encoder=%s mode=%s: %s",
                  encoder, rate_control or "default", probe_failure_reason(result.stderr))
    return result.returncode == 0


@lru_cache(maxsize=4)
def detect_hardware_export_encoders(
    available_encoders: frozenset[str] | None = None,
    devices: tuple[str, ...] | None = None,
) -> tuple[HardwareEncoderCapability, ...]:
    """Probe real VAAPI encoding, excluding advertised but unusable encoders."""
    if available_encoders is None:
        available_encoders = detect_export_encoders()
    if devices is None:
        try:
            devices = tuple(
                str(path)
                for path in sorted(Path("/dev/dri").glob("renderD*"))
                if _VAAPI_DEVICE.fullmatch(str(path))
            )
        except OSError:
            devices = ()

    capabilities = []
    for device in devices:
        if not _VAAPI_DEVICE.fullmatch(device):
            continue
        label = _vaapi_device_name(device)
        for codec_id, encoder in VAAPI_VIDEO_ENCODERS.items():
            if encoder not in available_encoders:
                continue
            if not _probe_vaapi_encoder(device, encoder):
                continue
            rate_controls = frozenset(
                rate_control
                for rate_control in RATE_CONTROL_LABELS
                if _probe_vaapi_encoder(device, encoder, rate_control)
            )
            if rate_controls:
                capabilities.append(
                    HardwareEncoderCapability(
                        "vaapi",
                        device,
                        label,
                        codec_id,
                        encoder,
                        rate_controls,
                    )
                )

    nvenc_candidates = tuple(
        (codec_id, encoder)
        for codec_id, encoder in NVENC_VIDEO_ENCODERS.items()
        if encoder in available_encoders
    )
    nvenc_base_support = {}
    if nvenc_candidates:
        first_encoder = nvenc_candidates[0][1]
        nvenc_base_support[first_encoder] = _probe_nvenc_encoder(first_encoder)
        if not nvenc_base_support[first_encoder]:
            nvenc_candidates = ()

    for codec_id, encoder in nvenc_candidates:
        base_supported = nvenc_base_support.get(encoder)
        if base_supported is None:
            base_supported = _probe_nvenc_encoder(encoder)
            nvenc_base_support[encoder] = base_supported
        if not base_supported:
            continue
        rate_controls = frozenset(
            rate_control
            for rate_control in RATE_CONTROL_LABELS
            if _probe_nvenc_encoder(encoder, rate_control)
        )
        if rate_controls:
            capabilities.append(
                HardwareEncoderCapability(
                    "nvenc",
                    "any",
                    "NVIDIA NVENC",
                    codec_id,
                    encoder,
                    rate_controls,
                )
            )
    _LOG.info("Usable hardware export encoders: %s", "; ".join(
        f"{item.encoder}@{item.device} ({','.join(sorted(item.rate_controls))})"
        for item in capabilities
    ) or "none")
    return tuple(capabilities)


def probe_export_encoder_capabilities() -> ExportEncoderCapabilities:
    """Probe software listings and actual hardware initialization once."""
    available_encoders = detect_export_encoders()
    return ExportEncoderCapabilities(
        available_encoders,
        detect_hardware_export_encoders(available_encoders),
    )


def start_export_encoder_capability_probe() -> None:
    """Start the process-wide export probe without blocking application startup."""
    global _EXPORT_CAPABILITY_STARTED
    with _EXPORT_CAPABILITY_LOCK:
        if _EXPORT_CAPABILITY_STARTED:
            return
        _EXPORT_CAPABILITY_STARTED = True

    def worker() -> None:
        global _EXPORT_CAPABILITY_RESULT
        try:
            result = probe_export_encoder_capabilities()
        except Exception:
            result = ExportEncoderCapabilities(frozenset(), ())
        with _EXPORT_CAPABILITY_LOCK:
            _EXPORT_CAPABILITY_RESULT = result

    threading.Thread(
        target=worker,
        name="clipper-export-capability-probe",
        daemon=True,
    ).start()


def export_encoder_capability_result() -> ExportEncoderCapabilities | None:
    """Return the completed startup result, or None while it is still running."""
    with _EXPORT_CAPABILITY_LOCK:
        return _EXPORT_CAPABILITY_RESULT


def compatible_video_codecs(
    format_id: str, available_encoders: set[str] | frozenset[str] | None = None
) -> tuple[VideoCodec, ...]:
    return tuple(
        codec
        for codec in VIDEO_CODEC_OPTIONS
        if format_id in codec.formats
        and (available_encoders is None or codec.encoder in available_encoders)
    )


def compatible_audio_codecs(
    format_id: str, available_encoders: set[str] | frozenset[str] | None = None
) -> tuple[AudioCodec, ...]:
    return tuple(
        codec
        for codec in AUDIO_CODEC_OPTIONS
        if format_id in codec.formats
        and (available_encoders is None or codec.encoder in available_encoders)
    )


def validate_export_options(options: ExportOptions) -> None:
    if not options.extension:
        raise ExportValidationError(_("Unsupported export format"))
    if options.audio_layout not in {"separate", "mixed"}:
        raise ExportValidationError(_("Unsupported audio layout"))
    if options.video_codec not in VIDEO_CODECS:
        raise ExportValidationError(_("Unsupported video codec"))
    if options.audio_codec not in AUDIO_CODECS:
        raise ExportValidationError(_("Unsupported audio codec"))
    if options.format not in VIDEO_CODECS[options.video_codec].formats:
        raise ExportValidationError(_("The video codec is not supported by this format"))
    if options.format not in AUDIO_CODECS[options.audio_codec].formats:
        raise ExportValidationError(_("The audio codec is not supported by this format"))
    if options.rate_control not in RATE_CONTROL_LABELS:
        raise ExportValidationError(_("Unsupported rate control method"))
    if options.video_encoder not in {"software", *HARDWARE_VIDEO_ENCODERS}:
        raise ExportValidationError(_("Unsupported video encoder"))
    if options.video_encoder == "software" and options.hardware_device is not None:
        raise ExportValidationError(
            _("Software encoding cannot use a hardware device")
        )
    if options.video_encoder == "vaapi":
        if options.video_codec not in VAAPI_VIDEO_ENCODERS:
            raise ExportValidationError(_("The video codec cannot use VAAPI"))
        if not options.hardware_device or not _VAAPI_DEVICE.fullmatch(
            options.hardware_device
        ):
            raise ExportValidationError(_("Choose a valid VAAPI device"))
    if options.video_encoder == "nvenc":
        if options.video_codec not in NVENC_VIDEO_ENCODERS:
            raise ExportValidationError(_("The video codec cannot use NVENC"))
        if options.hardware_device != "any":
            raise ExportValidationError(_("Choose a valid NVENC device"))
    if not CQP_HIGH_QUALITY <= options.quality_cqp <= CQP_LOW_QUALITY:
        raise ExportValidationError(
            _("Quality must be between %(minimum)d and %(maximum)d")
            % {"minimum": CQP_HIGH_QUALITY, "maximum": CQP_LOW_QUALITY}
        )
    if not _BITRATE_MIN <= options.video_bitrate <= _BITRATE_MAX:
        raise ExportValidationError(_("Video bitrate is outside the supported range"))
    if not _BITRATE_MIN <= options.video_max_bitrate <= _BITRATE_MAX:
        raise ExportValidationError(
            _("Maximum video bitrate is outside the supported range")
        )
    if options.rate_control == "vbr" and options.video_max_bitrate < options.video_bitrate:
        raise ExportValidationError(
            _("Maximum bitrate cannot be lower than target bitrate")
        )
    output_dimensions = (options.output_width, options.output_height)
    if (options.output_width is None) != (options.output_height is None):
        raise ExportValidationError(_("Choose both output dimensions"))
    if options.output_width is not None and any(
        type(value) is not int or value < 2 or value % 2
        for value in output_dimensions
    ):
        raise ExportValidationError(
            _("Output dimensions must be positive even numbers")
        )


def validate_export_resolution(project: EditorProject, options: ExportOptions) -> None:
    """Reject upscaling or dimensions that would alter the source display ratio."""
    if options.output_width is None or options.output_height is None:
        return
    source_ratio = _display_aspect_ratio(project.source)
    native_dimensions = source_display_dimensions(project.source)
    if source_ratio is None or native_dimensions is None:
        raise ExportValidationError(_("The source resolution could not be determined"))

    short_edge = min(options.output_width, options.output_height)
    expected = _resolution_for_short_edge(project.source, short_edge)
    if expected != (options.output_width, options.output_height):
        raise ExportValidationError(
            _("The output resolution must preserve the source aspect ratio")
        )
    native_short_edge = min(
        Fraction(native_dimensions[1]) * source_ratio,
        Fraction(native_dimensions[1]),
    )
    if short_edge >= native_short_edge:
        raise ExportValidationError(_("The output resolution must be lower than native"))


def destination_path(options: ExportOptions, source_path: str | Path) -> Path:
    name = options.filename.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or _CONTROL.search(name):
        raise ExportValidationError(_("Enter a valid filename without folders"))
    suffix = options.extension
    if name.casefold().endswith(suffix.casefold()):
        name = name[: -len(suffix)].strip()
    if not name or name in {".", ".."}:
        raise ExportValidationError(_("Enter a valid filename"))

    folder = Path(options.folder).expanduser().resolve()
    destination = (folder / f"{name}{suffix}").resolve()
    if destination.parent != folder:
        raise ExportValidationError(_("The destination is outside the selected folder"))
    if destination == Path(source_path).expanduser().resolve():
        raise ExportValidationError(_("Export cannot overwrite the source clip"))
    if not folder.is_dir() or not os.access(folder, os.W_OK):
        raise ExportValidationError(_("The selected folder is not writable"))
    return destination


def _seconds(microseconds: int) -> str:
    return f"{microseconds / 1_000_000:.6f}"


def build_filter_graph(
    project: EditorProject,
    mixed: bool = False,
    hardware_upload: bool = False,
    input_start_us: int = 0,
    output_size: tuple[int, int] | None = None,
    hardware_surface_size: tuple[int, int] | None = None,
) -> tuple[str, list[str]]:
    project.validate()
    if not 0 <= input_start_us <= project.segments[0].source_start_us:
        raise ValueError("Input start must not be after the first segment")
    filters: list[str] = []
    video_segments = []
    for index, segment in enumerate(project.segments):
        label = f"v{index}"
        start_us = segment.source_start_us - input_start_us
        end_us = segment.source_end_us - input_start_us
        filters.append(
            f"[0:{project.source.video_stream_index}]trim=start={_seconds(start_us)}:"
            f"end={_seconds(end_us)},setpts=PTS-STARTPTS[{label}]"
        )
        video_segments.append(f"[{label}]")
    filters.append("".join(video_segments) + f"concat=n={len(project.segments)}:v=1:a=0[vout]")
    video_output = "vout"
    source = project.source
    if (
        not source.variable_frame_rate
        and source.frame_rate_num > 0
        and source.frame_rate_den > 0
    ):
        # concat does not retain the link's nominal frame rate. Restore a
        # known CFR source as an exact rational so hardware encoders write the
        # correct sequence-header rate instead of guessing (for example,
        # turning 60/1 into 60000/1001).
        video_output = "vout_cfr"
        filters.append(
            f"[vout]fps=fps={source.frame_rate_num}/{source.frame_rate_den}"
            f"[{video_output}]"
        )
    if output_size is not None:
        width, height = output_size
        scaled_output = "vout_scaled"
        filters.append(
            f"[{video_output}]scale={width}:{height}:flags=lanczos,"
            f"setsar=1[{scaled_output}]"
        )
        video_output = scaled_output
    completed_audio = []
    for track in project.source.audio_tracks:
        parts = []
        for index, segment in enumerate(project.segments):
            label = f"a{track.ordinal}s{index}"
            setting = segment.audio[track.id]
            gain = effective_audio_gain(setting)
            duration = _seconds(segment.duration_us)
            start_us = segment.source_start_us - input_start_us
            end_us = segment.source_end_us - input_start_us
            filters.append(
                f"[0:{track.stream_index}]atrim=start={_seconds(start_us)}:"
                f"end={_seconds(end_us)},asetpts=PTS-STARTPTS,"
                f"volume={gain:.6f},apad=whole_dur={duration},atrim=duration={duration}[{label}]"
            )
            parts.append(f"[{label}]")
        output = f"aout{track.ordinal}"
        filters.append("".join(parts) + f"concat=n={len(parts)}:v=0:a=1[{output}]")
        completed_audio.append(output)
    if hardware_surface_size is not None:
        surface_width, surface_height = hardware_surface_size
        padded_output = "vout_padded"
        filters.append(
            f"[{video_output}]pad={surface_width}:{surface_height}:0:0"
            f"[{padded_output}]"
        )
        video_output = padded_output
    if hardware_upload:
        filters.append(f"[{video_output}]format=nv12,hwupload[vout_hw]")
    maps = ["[vout_hw]" if hardware_upload else f"[{video_output}]"]
    if mixed and completed_audio:
        inputs = "".join(f"[{label}]" for label in completed_audio)
        filters.append(
            f"{inputs}amix=inputs={len(completed_audio)}:normalize=0,"
            "alimiter=limit=0.95:level=disabled[amixed]"
        )
        maps.append(_("[amixed]"))
    elif not mixed:
        maps.extend(f"[{label}]" for label in completed_audio)
    return ";".join(filters), maps


def _rate_control_arguments(options: ExportOptions, codec: VideoCodec) -> list[str]:
    if options.video_encoder == "vaapi":
        if options.rate_control == "cqp":
            encoder = VAAPI_VIDEO_ENCODERS[codec.id]
            return _vaapi_cqp_arguments(encoder, options.quality_cqp)
        arguments = [
            "-rc_mode",
            "CBR" if options.rate_control == "cbr" else "VBR",
        ]
        arguments.extend(_software_bitrate_arguments(options))
        return arguments

    if options.video_encoder == "nvenc":
        if options.rate_control == "cqp":
            return ["-rc", "constqp", "-qp", str(options.quality_cqp)]
        arguments = ["-rc", options.rate_control]
        arguments.extend(_software_bitrate_arguments(options))
        return arguments

    if options.rate_control == "cqp":
        arguments = ["-crf", str(options.quality_cqp)]
        if codec.id in {"vp9", "av1"}:
            arguments.extend(["-b:v", "0"])
        return arguments

    return _software_bitrate_arguments(options)


def _software_bitrate_arguments(options: ExportOptions) -> list[str]:
    target = f"{options.video_bitrate}k"
    if options.rate_control == "cbr":
        return [
            "-b:v",
            target,
            "-minrate",
            target,
            "-maxrate",
            target,
            "-bufsize",
            f"{options.video_bitrate * 2}k",
        ]
    return [
        "-b:v",
        target,
        "-maxrate",
        f"{options.video_max_bitrate}k",
        "-bufsize",
        f"{options.video_max_bitrate * 2}k",
    ]


def build_ffmpeg_command(
    project: EditorProject, options: ExportOptions, partial_path: str | Path
) -> list[str]:
    validate_export_options(options)
    validate_export_resolution(project, options)
    hardware_encoding = options.video_encoder != "software"
    hardware_upload = options.video_encoder == "vaapi"
    hevc_surface_padding = _vaapi_hevc_surface_padding(project, options)
    input_start_us = project.segments[0].source_start_us
    graph, maps = build_filter_graph(
        project,
        options.audio_layout == "mixed",
        hardware_upload,
        input_start_us,
        (
            (options.output_width, options.output_height)
            if options.output_width is not None and options.output_height is not None
            else None
        ),
        (
            hevc_surface_padding[:2]
            if hevc_surface_padding is not None
            else None
        ),
    )
    video_codec = VIDEO_CODECS[options.video_codec]
    audio_codec = AUDIO_CODECS[options.audio_codec]
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-nostdin",
    ]
    if options.video_encoder == "vaapi":
        hardware_device = options.hardware_device
        assert hardware_device is not None
        command.extend(["-vaapi_device", hardware_device])
    if input_start_us:
        # Seeking before opening the input avoids decoding footage that the
        # first retained segment has already discarded. FFmpeg's accurate
        # input seek still decodes from the preceding keyframe as needed.
        command.extend(["-ss", _seconds(input_start_us)])
    command.extend(["-i", project.source.path, "-filter_complex", graph])
    for output in maps:
        command.extend(["-map", output])
    if hardware_encoding:
        encoder = HARDWARE_VIDEO_ENCODERS[options.video_encoder][video_codec.id]
        command.extend(["-c:v", encoder])
        if options.video_encoder == "nvenc":
            command.extend(["-preset", "p4"])
    else:
        command.extend(["-c:v", video_codec.encoder, *video_codec.encoder_args])
    command.extend(_rate_control_arguments(options, video_codec))
    if hevc_surface_padding is not None:
        _surface_width, _surface_height, crop_right, crop_bottom = (
            hevc_surface_padding
        )
        crop_options = []
        if crop_right:
            crop_options.append(f"crop_right={crop_right}")
        if crop_bottom:
            crop_options.append(f"crop_bottom={crop_bottom}")
        command.extend(["-bsf:v", "hevc_metadata=" + ":".join(crop_options)])
    if options.video_encoder != "vaapi":
        command.extend(["-pix_fmt", "yuv420p"])
    if options.video_codec == "hevc" and options.format in {"mp4", "mov"}:
        command.extend(["-tag:v", "hvc1"])
    if project.source.audio_tracks:
        command.extend(["-c:a", audio_codec.encoder, *audio_codec.encoder_args])
    if options.format in {"mp4", "mov"}:
        command.extend(["-movflags", "+faststart"])
    if options.audio_layout == "separate":
        for ordinal, track in enumerate(project.source.audio_tracks):
            command.extend([f"-metadata:s:a:{ordinal}", f"title={track.title or track.label}"])
            command.extend([f"-metadata:s:a:{ordinal}", f"language={track.language}"])
    muxer = (
        "matroska"
        if options.format == "mkv"
        else "mpegts"
        if options.format == "ts"
        else options.format
    )
    command.extend(["-progress", "pipe:1", "-nostats", "-f", muxer, str(partial_path)])
    return command


def parse_progress_line(line: str, duration_us: int) -> float | None:
    key, separator, value = line.strip().partition("=")
    if not separator or key not in {"out_time_us", "out_time_ms"}:
        return None
    try:
        # FFmpeg's out_time_ms is historically microseconds despite its name.
        return min(1.0, max(0.0, int(value) / max(1, duration_us)))
    except ValueError:
        return None


def _export_error(stderr: str) -> str:
    ignored = (
        "Nothing was written into output file",
        "Task finished with error code",
        "Terminating thread with return code",
        "Error sending frames to consumers",
        "Conversion failed",
    )
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        if not any(fragment in line for fragment in ignored):
            return line[:500]
    return _("FFmpeg could not create the output file")


def _hardware_export_error(stderr: str) -> str:
    detail = _export_error(stderr)
    return (
        _("%(detail)s. The hardware encoder may not support this clip; "
          "choose Software as the video encoder and try again")
        % {"detail": detail}
    )


class ExportProcess:
    """Asynchronous-friendly FFmpeg owner with progress and atomic replacement."""

    def __init__(self, project: EditorProject, options: ExportOptions):
        validate_export_options(options)
        self.project = project
        self.options = options
        self.destination = destination_path(options, project.source.path)
        fd, partial = tempfile.mkstemp(
            prefix=f".{self.destination.name}.",
            suffix=".partial",
            dir=self.destination.parent,
        )
        os.close(fd)
        self.partial = Path(partial)
        self.process: subprocess.Popen | None = None
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self._cancel_requested = False
        self._started_at = time.monotonic()
        self._stage = "initialization"

    def start(self):
        if self.process is not None:
            raise RuntimeError(_("Export was already started"))
        if self._cancel_requested:
            self.cleanup()
            raise ExportCancelledError(_("Export cancelled"))
        try:
            self._started_at = time.monotonic()
            self._stage = "encoding"
            options = self.options
            _LOG.info("Starting export: source=%s; destination=%s; duration=%.2fs; segments=%s",
                      self.project.source.path, self.destination,
                      self.project.output_duration_us / 1_000_000, len(self.project.segments))
            _LOG.info("Video: codec=%s; encoder=%s; device=%s; resolution=%s; %s",
                      options.video_codec, options.video_encoder,
                      options.hardware_device or "automatic",
                      f"{options.output_width}x{options.output_height}"
                      if options.output_width else "source",
                      f"cqp={options.quality_cqp}" if options.rate_control == "cqp" else
                      f"{options.rate_control}={options.video_bitrate} kbps; "
                      f"max={options.video_max_bitrate} kbps")
            _LOG.info("Audio: codec=%s; layout=%s; source_tracks=%s; container=%s",
                      options.audio_codec, options.audio_layout,
                      len(self.project.source.audio_tracks), options.format)
            self.process = subprocess.Popen(
                build_ffmpeg_command(self.project, self.options, self.partial),
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                bufsize=1,
                env=clean_external_tool_env(),
                start_new_session=True,
            )
        except OSError as error:
            _LOG.error("Could not launch FFmpeg: %s", error)
            self.cleanup()
            raise RuntimeError(
                _("Could not start FFmpeg: %(error)s") % {"error": error}
            ) from error
        except Exception:
            _LOG.exception("Export command preparation failed: destination=%s", self.destination)
            self.cleanup()
            raise
        return self.process

    def cancel(self):
        if not self._cancel_requested:
            _LOG.info("Cancellation requested: destination=%s stage=%s",
                      self.destination, self._stage)
        self._cancel_requested = True
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def finish(self, progress_callback: Callable[[float], None] | None = None) -> Path:
        process = self.process
        if process is None:
            self.cleanup()
            raise RuntimeError(_("Export was not started"))

        last_progress = None
        if process.stdout is not None:
            try:
                for line in process.stdout:
                    progress = parse_progress_line(line, self.project.output_duration_us)
                    if (
                        progress is not None
                        and progress != last_progress
                        and progress_callback is not None
                    ):
                        progress_callback(progress)
                    if progress is not None:
                        last_progress = progress
            finally:
                process.stdout.close()

        return_code = process.wait()
        if return_code != 0 or self._cancel_requested:
            detail = self._read_stderr()
            _LOG.log(logging.INFO if self._cancel_requested else logging.ERROR,
                     "Export %s: destination=%s exit=%s elapsed=%.1fs progress=%s\n%s",
                     "cancelled" if self._cancel_requested else "encoding failed",
                     self.destination, return_code, time.monotonic() - self._started_at,
                     last_progress, diagnostic_tail(detail))
            self.cleanup()
            if self._cancel_requested:
                raise ExportCancelledError(_("Export cancelled"))
            error = (
                _hardware_export_error(detail)
                if self.options.video_encoder != "software"
                else _export_error(detail)
            )
            raise RuntimeError(error)

        self._close_stderr()
        try:
            self._stage = "validation"
            _LOG.info("Encoding finished; validating output: %s", self.destination)
            self._validate_partial()
            if self._cancel_requested:
                raise ExportCancelledError(_("Export cancelled"))
            self._stage = "publishing output"
            os.replace(self.partial, self.destination)
        except Exception as error:
            _LOG.log(logging.INFO if isinstance(error, ExportCancelledError) else logging.ERROR,
                     "Export stopped: destination=%s stage=%s reason=%s",
                     self.destination, self._stage, error)
            self.cleanup()
            raise
        _LOG.info("Export completed: destination=%s elapsed=%.1fs",
                  self.destination, time.monotonic() - self._started_at)
        if progress_callback is not None and last_progress != 1.0:
            progress_callback(1.0)
        return self.destination

    def _read_stderr(self) -> str:
        if self._stderr.closed:
            return ""
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()

    def _close_stderr(self) -> None:
        if not self._stderr.closed:
            self._stderr.close()

    def _validate_partial(self) -> None:
        result = run_media_tool(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,codec_name:frame=width,height",
                "-read_intervals",
                "%+#1",
                "-show_frames",
                "-of",
                "json",
                str(self.partial),
            ],
            timeout=30,
        )
        try:
            probe = json.loads(result.stdout)
            streams = probe["streams"]
            frames = probe.get("frames", [])
            duration_us = round(float(probe["format"]["duration"]) * 1_000_000)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.cleanup()
            raise RuntimeError(
                _("The exported file could not be validated")
            ) from error

        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        expected_audio = (
            0
            if not self.project.source.audio_tracks
            else 1
            if self.options.audio_layout == "mixed"
            else len(self.project.source.audio_tracks)
        )
        tolerance_us = max(150_000, self.project.source.frame_duration_us * 2)
        video_codec = VIDEO_CODECS[self.options.video_codec].probe_codec
        audio_codec = AUDIO_CODECS[self.options.audio_codec].probe_codec
        expected_dimensions = _export_frame_dimensions(self.project, self.options)
        frame_dimensions_match = (
            expected_dimensions is None
            or (
                len(frames) == 1
                and (frames[0].get("width"), frames[0].get("height"))
                == expected_dimensions
            )
        )
        if (
            len(video_streams) != 1
            or video_streams[0].get("codec_name") != video_codec
            or len(audio_streams) != expected_audio
            or any(stream.get("codec_name") != audio_codec for stream in audio_streams)
            or abs(duration_us - self.project.output_duration_us) > tolerance_us
            or not frame_dimensions_match
        ):
            _LOG.error("Output validation mismatch: expected video=%s dimensions=%s "
                       "audio=%s tracks=%s duration_us=%s; actual=%s",
                       video_codec, expected_dimensions, audio_codec, expected_audio,
                       self.project.output_duration_us, probe)
            self.cleanup()
            raise RuntimeError(_("The exported file did not match the selected options"))

    def cleanup(self):
        self._close_stderr()
        try:
            self.partial.unlink()
        except FileNotFoundError:
            pass
