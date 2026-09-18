import shutil
import struct
import subprocess
import wave
from types import SimpleNamespace

import pytest
from editor_waveform import (
    ANALYSIS_RATE,
    WAVEFORM_VERSION,
    pcm_peak_buckets,
    waveform_channel_count,
    waveform_decode_command,
    waveform_metadata_buckets,
    waveform_samples_per_bucket,
)


def test_waveform_analysis_preserves_the_full_audio_band_and_source_channels():
    source = SimpleNamespace(path="/clips/example.mkv")
    track = SimpleNamespace(ordinal=2, channels=2)

    channel_count = waveform_channel_count(track)
    command = waveform_decode_command(source, track, channel_count)

    assert ANALYSIS_RATE == 48_000
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-map") + 1] == "0:a:2"
    filter_graph = command[command.index("-af") + 1]
    assert "asetnsamples=n=960:pad=0" in filter_graph
    assert "astats=metadata=1:reset=1" in filter_graph
    assert "measure_perchannel=none:measure_overall=Min_level+Max_level" in filter_graph
    assert "ametadata=mode=print:file=-" in filter_graph


def test_stereo_waveform_envelope_keeps_opposing_channel_peaks():
    samples = struct.pack("<hhhh", 12_000, -12_000, 8_000, -8_000)

    buckets = pcm_peak_buckets(
        [samples],
        waveform_samples_per_bucket(channel_count=2),
    )

    assert buckets == [(-12_000, 12_000)]


def test_ffmpeg_peak_metadata_keeps_opposing_channel_peaks():
    lines = [
        b"frame:0    pts:0    pts_time:0",
        b"lavfi.astats.1.Min_level=-8000.000000",
        b"lavfi.astats.1.Max_level=6000.000000",
        b"lavfi.astats.2.Min_level=-12000.000000",
        b"lavfi.astats.2.Max_level=9000.000000",
        b"lavfi.astats.Overall.Min_level=-12000.000000",
        b"lavfi.astats.Overall.Max_level=9000.000000",
    ]

    assert waveform_metadata_buckets(lines) == [(-12_000, 9_000)]


def test_pcm_peak_buckets_handles_chunk_boundaries_without_per_sample_unpacking():
    samples = struct.pack("<hhhh", -3, 11, -7, 5)

    assert pcm_peak_buckets([samples[:3], samples[3:]], 2) == [(-3, 11), (-7, 5)]


def test_pcm_peak_buckets_ignores_incomplete_trailing_sample():
    samples = struct.pack("<hh", -3, 11)

    assert pcm_peak_buckets([samples + b"\x7f"], 2) == [(-3, 11)]


def test_unknown_waveform_channel_count_falls_back_to_mono():
    assert waveform_channel_count(SimpleNamespace(channels=0)) == 1
    assert waveform_channel_count(SimpleNamespace()) == 1


def test_full_band_waveforms_use_a_new_cache_generation():
    assert WAVEFORM_VERSION == 2


@pytest.mark.parametrize("channels", [1, 2, 6])
@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_selective_statistics_match_full_statistics(tmp_path, channels, rate):
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is not available")
    source_path = tmp_path / "peaks.wav"
    # Silence, clipping and opposite-phase channels, with a partial final bucket.
    samples = [
        (0 if frame < 1000 else (32767 if frame % 3 else -32768))
        if channel % 2 == 0 else (0 if frame < 1000 else -12000)
        for frame in range(rate // 10 + 137)
        for channel in range(channels)
    ]
    with wave.open(str(source_path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    command = waveform_decode_command(
        SimpleNamespace(path=str(source_path)), SimpleNamespace(ordinal=0), channels
    )
    old_command = list(command)
    filter_index = command.index("-af") + 1
    old_command[filter_index] = old_command[filter_index].replace(
        ":measure_perchannel=none:measure_overall=Min_level+Max_level", ""
    )
    old = subprocess.run(old_command, capture_output=True, check=True, timeout=30)
    new = subprocess.run(command, capture_output=True, check=True, timeout=30)
    expected = waveform_metadata_buckets(old.stdout.splitlines())
    assert expected
    assert waveform_metadata_buckets(new.stdout.splitlines()) == expected
    assert len(new.stdout) < len(old.stdout)
