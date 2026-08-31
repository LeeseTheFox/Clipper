"""Incremental PCM waveform bucket generation and compact caching."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

from editor_drafts import cache_path, source_fingerprint
from media_tools import clean_external_tool_env

WAVEFORM_VERSION = 2
ANALYSIS_RATE = 48000
BUCKET_MILLISECONDS = 20


def pcm_peak_buckets(chunks, samples_per_bucket: int):
    """Return min/max buckets without running a Python loop per sample.

    ``generate_waveform`` uses FFmpeg's native ``astats`` implementation for
    the actual decode path.  This implementation remains useful for callers
    that already have PCM bytes (and for small unit-test fixtures), so keep it
    efficient too: ``min``/``max`` consume one native array-sized bucket at a
    time instead of executing Python byte-unpacking and comparisons for every
    sample.
    """
    if samples_per_bucket <= 0:
        raise ValueError("samples_per_bucket must be positive")
    buckets = []
    bucket_bytes = samples_per_bucket * 2
    remainder = b""
    for chunk in chunks:
        data = remainder + chunk
        usable = len(data) - len(data) % 2
        offset = 0
        while offset + bucket_bytes <= usable:
            values = array("h")
            values.frombytes(data[offset : offset + bucket_bytes])
            if sys.byteorder != "little":
                values.byteswap()
            buckets.append((min(values), max(values)))
            offset += bucket_bytes
        remainder = data[offset:usable] + data[usable:]
    if remainder:
        values = array("h")
        values.frombytes(remainder[: len(remainder) - len(remainder) % 2])
        if values:
            if sys.byteorder != "little":
                values.byteswap()
            buckets.append((min(values), max(values)))
    return buckets


def aggregate_buckets(buckets, factor: int):
    if factor <= 1:
        return list(buckets)
    return [
        (min(item[0] for item in group), max(item[1] for item in group))
        for start in range(0, len(buckets), factor)
        if (group := buckets[start : start + factor])
    ]


def waveform_channel_count(track) -> int:
    """Return a deterministic PCM channel count while preserving known channels."""
    try:
        return max(1, int(track.channels))
    except (AttributeError, TypeError, ValueError):
        return 1


def waveform_samples_per_bucket(channel_count: int) -> int:
    """Return the scalar sample count in one interleaved 20 ms PCM bucket."""
    return ANALYSIS_RATE * BUCKET_MILLISECONDS // 1000 * channel_count


def waveform_decode_command(source, track, channel_count: int) -> list[str]:
    """Build a full-band decode command whose peaks are computed in FFmpeg."""
    bucket_samples = ANALYSIS_RATE * BUCKET_MILLISECONDS // 1000
    channel_layout = {1: "mono", 2: "stereo"}.get(channel_count)
    format_options = f"sample_rates={ANALYSIS_RATE}:sample_fmts=s16"
    if channel_layout is not None:
        format_options += f":channel_layouts={channel_layout}"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        source.path,
        "-map",
        f"0:a:{track.ordinal}",
        "-vn",
        "-ac",
        str(channel_count),
        "-ar",
        str(ANALYSIS_RATE),
        "-af",
        (
            f"aformat={format_options},"
            f"asetnsamples=n={bucket_samples}:pad=0,"
            "astats=metadata=1:reset=1,"
            "ametadata=mode=print:file=-"
        ),
        "-f",
        "null",
        "-",
    ]


def waveform_metadata_buckets(lines):
    """Parse FFmpeg ``astats`` min/max metadata into waveform buckets."""
    buckets = []
    minimum = maximum = None
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("ascii", errors="replace")
        line = line.strip()
        if line.startswith("frame:"):
            minimum = maximum = None
            continue
        if line.startswith("lavfi.astats.Overall.Min_level="):
            value = line.partition("=")[2]
            try:
                minimum = round(float(value))
            except ValueError:
                minimum = None
        elif line.startswith("lavfi.astats.Overall.Max_level="):
            value = line.partition("=")[2]
            try:
                maximum = round(float(value))
            except ValueError:
                maximum = None
        if minimum is not None and maximum is not None:
            buckets.append((minimum, maximum))
            minimum = maximum = None
    return buckets


def save_cache(path: Path, fingerprint: str, buckets) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(
                {
                    "version": WAVEFORM_VERSION,
                    "fingerprint": fingerprint,
                    "buckets": buckets,
                },
                output,
            )
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_cache(path: Path, fingerprint: str):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("version") != WAVEFORM_VERSION or data.get("fingerprint") != fingerprint:
        return None
    return [tuple(bucket) for bucket in data.get("buckets", [])]


def waveform_cache_file(source, track, env=None) -> Path:
    return cache_path(source, env) / f"audio-{track.ordinal}-v{WAVEFORM_VERSION}.json"


def generate_waveform(source, track, cancel_event=None, env=None):
    """Decode one audio ordinal incrementally and return min/max PCM buckets."""
    fingerprint = source_fingerprint(source)
    location = waveform_cache_file(source, track, env)
    cached = load_cache(location, fingerprint)
    if cached is not None:
        return cached
    channel_count = waveform_channel_count(track)
    process = subprocess.Popen(
        waveform_decode_command(source, track, channel_count),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_external_tool_env(),
    )

    def metadata_lines():
        assert process.stdout is not None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                raise InterruptedError("Waveform generation cancelled")
            line = process.stdout.readline()
            if not line:
                break
            yield line

    try:
        buckets = waveform_metadata_buckets(metadata_lines())
        if process.wait() != 0:
            error = (
                process.stderr.read().decode(errors="replace") if process.stderr is not None else ""
            )
            raise RuntimeError(error.strip().splitlines()[-1] if error.strip() else "FFmpeg failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    save_cache(location, fingerprint, buckets)
    return buckets
