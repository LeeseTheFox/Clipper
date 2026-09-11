"""Safe helpers for invoking media tools outside Clipper's runtime environment."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from logs import diagnostic_tail

_LOG = logging.getLogger("clipper.editor.media")

EXTERNAL_TOOL_ENV_OVERRIDES = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "GI_TYPELIB_PATH",
    "GIO_MODULE_DIR",
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_REGISTRY",
)


class MediaToolError(RuntimeError):
    """A concise, user-presentable media tool failure."""


@dataclass(frozen=True)
class EditorCapabilities:
    ffmpeg: bool
    ffprobe: bool
    gstreamer: bool
    gtk4paintablesink: bool
    h264_encoder: bool
    aac_encoder: bool

    @property
    def can_probe(self) -> bool:
        return self.ffprobe

    @property
    def can_export(self) -> bool:
        return self.ffmpeg and self.ffprobe and self.h264_encoder and self.aac_encoder

    @property
    def can_preview(self) -> bool:
        return self.gstreamer and self.gtk4paintablesink


def clean_external_tool_env(source: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in EXTERNAL_TOOL_ENV_OVERRIDES:
        env.pop(key, None)
    return env


def find_executable(name: str) -> str | None:
    return shutil.which(name, path=clean_external_tool_env().get("PATH"))


def concise_error(stderr: str, fallback: str = "Media tool failed") -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return (lines[-1] if lines else fallback)[:500]


def run_media_tool(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    text: bool = True,
    input_data=None,
) -> subprocess.CompletedProcess:
    if not args or not isinstance(args[0], str):
        raise ValueError("A media command must be a non-empty argument list")
    try:
        result = subprocess.run(
            list(args),
            input=input_data,
            capture_output=True,
            text=text,
            timeout=timeout,
            env=clean_external_tool_env(),
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _LOG.warning("Media tool could not complete: tool=%s timeout=%s reason=%s",
                     args[0], timeout, error)
        raise MediaToolError(str(error)) from error
    if check and result.returncode:
        error_text = result.stderr if isinstance(result.stderr, str) else ""
        _LOG.error("Media tool failed: tool=%s exit=%s\n%s",
                   args[0], result.returncode, diagnostic_tail(error_text))
        raise MediaToolError(concise_error(error_text))
    return result


def _contains(command: Sequence[str], needles: Iterable[str]) -> bool:
    try:
        output = run_media_tool(command, timeout=8).stdout
    except MediaToolError:
        return False
    return all(needle in output for needle in needles)


def check_editor_capabilities() -> EditorCapabilities:
    ffmpeg = find_executable("ffmpeg") is not None
    ffprobe = find_executable("ffprobe") is not None
    gst = find_executable("gst-inspect-1.0") is not None
    return EditorCapabilities(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        gstreamer=gst,
        gtk4paintablesink=gst
        and _contains(["gst-inspect-1.0", "gtk4paintablesink"], ["gtk4paintablesink"]),
        h264_encoder=ffmpeg and _contains(["ffmpeg", "-hide_banner", "-encoders"], ["libx264"]),
        aac_encoder=ffmpeg and _contains(["ffmpeg", "-hide_banner", "-encoders"], ["aac"]),
    )
