import io
import json
from pathlib import Path
from types import SimpleNamespace

import editor_export
import pytest
from editor_export import (
    AUDIO_CODECS,
    VIDEO_CODECS,
    ExportCancelledError,
    ExportEncoderCapabilities,
    ExportOptions,
    ExportProcess,
    ExportValidationError,
    build_ffmpeg_command,
    build_filter_graph,
    compatible_audio_codecs,
    compatible_video_codecs,
    destination_path,
    detect_export_encoders,
    detect_hardware_export_encoders,
    export_resolution_options,
    parse_progress_line,
    source_display_dimensions,
    validate_export_options,
    validate_export_resolution,
)
from editor_model import AudioTrack, EditorProject, Source


def project():
    item = EditorProject.new(
        Source(
            "/source.mkv",
            1,
            2,
            5_000_000,
            0,
            [AudioTrack("a0", 0, 1, "Game", language="eng")],
            variable_frame_rate=True,
        )
    )
    _, right = item.split(item.segments[0].id, 2_000_000)
    item.set_muted(right, "a0", True)
    return item


def _option_value(command, option):
    return command[command.index(option) + 1]


def test_graph_maps_every_segment_and_track(tmp_path):
    item = project()
    graph, maps = build_filter_graph(item)
    assert "concat=n=2:v=1:a=0[vout]" in graph
    assert "volume=0.000000" in graph
    assert maps == ["[vout]", "[aout0]"]
    options = ExportOptions(tmp_path, "edited", "mp4")
    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    assert command[0] == "ffmpeg"
    assert "+faststart" in command
    assert destination_path(options, item.source.path) == tmp_path / "edited.mp4"


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    ((60, 1, "60/1"), (60_000, 1001, "60000/1001")),
)
def test_graph_restores_the_source_constant_frame_rate(
    numerator, denominator, expected
):
    item = project()
    item.source.frame_rate_num = numerator
    item.source.frame_rate_den = denominator
    item.source.variable_frame_rate = False

    graph, maps = build_filter_graph(item, hardware_upload=True)

    assert f"[vout]fps=fps={expected}[vout_cfr]" in graph
    assert "[vout_cfr]format=nv12,hwupload[vout_hw]" in graph
    assert maps[0] == "[vout_hw]"


def test_graph_does_not_flatten_variable_frame_rate_video():
    graph, maps = build_filter_graph(project())

    assert "fps=fps=" not in graph
    assert maps[0] == "[vout]"


def test_resolution_options_fit_standard_sizes_to_nonstandard_aspect_ratios():
    source = project().source
    source.width = 3440
    source.height = 1440

    assert source_display_dimensions(source) == (3440, 1440)
    assert export_resolution_options(source) == (
        (None, None),
        (2580, 1080),
        (1720, 720),
        (1146, 480),
        (860, 360),
        (574, 240),
    )


@pytest.mark.parametrize(
    ("width", "height", "rotation", "expected_native", "expected_720p"),
    (
        (1920, 1080, 0, (1920, 1080), (1280, 720)),
        (1080, 1920, 0, (1080, 1920), (720, 1280)),
        (1920, 1080, 90, (1080, 1920), (720, 1280)),
        (1024, 768, 0, (1024, 768), (960, 720)),
        (1919, 1079, 0, (1919, 1079), (1280, 720)),
    ),
)
def test_resolution_options_preserve_portrait_rotated_and_arbitrary_ratios(
    width, height, rotation, expected_native, expected_720p
):
    source = project().source
    source.width = width
    source.height = height
    source.rotation = rotation

    assert source_display_dimensions(source) == expected_native
    assert expected_720p in export_resolution_options(source)


def test_resolution_options_account_for_anamorphic_pixels():
    source = project().source
    source.width = 720
    source.height = 576
    source.sample_aspect_ratio_num = 64
    source.sample_aspect_ratio_den = 45

    assert source_display_dimensions(source) == (1024, 576)
    assert export_resolution_options(source) == (
        (None, None),
        (854, 480),
        (640, 360),
        (426, 240),
    )

    source.rotation = -90
    assert source_display_dimensions(source) == (576, 1024)
    assert (480, 854) in export_resolution_options(source)


def test_scaled_export_uses_exact_even_dimensions_and_square_pixels(tmp_path):
    item = project()
    item.source.width = 1920
    item.source.height = 1080
    options = ExportOptions(
        tmp_path,
        "scaled",
        output_width=1280,
        output_height=720,
    )

    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    graph = _option_value(command, "-filter_complex")

    assert "[vout]scale=1280:720:flags=lanczos,setsar=1[vout_scaled]" in graph
    assert "[vout_scaled]" in [
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "-map"
    ]


def test_scaled_vaapi_export_scales_before_uploading_frames(tmp_path):
    item = project()
    item.source.width = 1920
    item.source.height = 1080
    options = ExportOptions(
        tmp_path,
        "scaled-hardware",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
        output_width=1280,
        output_height=720,
    )

    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    graph = _option_value(command, "-filter_complex")

    assert "[vout]scale=1280:720:flags=lanczos,setsar=1[vout_scaled]" in graph
    assert "[vout_scaled]format=nv12,hwupload[vout_hw]" in graph


@pytest.mark.parametrize(
    ("width", "height", "surface_width", "surface_height", "crop_filter"),
    (
        (1920, 1080, 1920, 1088, "hevc_metadata=crop_bottom=8"),
        (854, 480, 896, 480, "hevc_metadata=crop_right=42"),
    ),
)
def test_hevc_vaapi_hides_aligned_surface_padding_in_the_bitstream(
    tmp_path, width, height, surface_width, surface_height, crop_filter
):
    item = project()
    item.source.width = 2560
    item.source.height = 1440
    options = ExportOptions(
        tmp_path,
        "scaled-hevc",
        video_codec="hevc",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
        output_width=width,
        output_height=height,
    )

    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    graph = _option_value(command, "-filter_complex")

    assert f"scale={width}:{height}:flags=lanczos,setsar=1" in graph
    assert f"pad={surface_width}:{surface_height}:0:0[vout_padded]" in graph
    assert "[vout_padded]format=nv12,hwupload[vout_hw]" in graph
    assert _option_value(command, "-bsf:v") == crop_filter


def test_aligned_hevc_vaapi_resolution_needs_no_padding_or_crop(tmp_path):
    item = project()
    item.source.width = 2560
    item.source.height = 1440
    options = ExportOptions(
        tmp_path,
        "scaled-hevc",
        video_codec="hevc",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
        output_width=1280,
        output_height=720,
    )

    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    graph = _option_value(command, "-filter_complex")

    assert "]pad=" not in graph
    assert "-bsf:v" not in command


def test_ultrawide_hevc_vaapi_crops_both_aligned_surface_edges(tmp_path):
    item = project()
    item.source.width = 3440
    item.source.height = 1440
    options = ExportOptions(
        tmp_path,
        "scaled-ultrawide",
        video_codec="hevc",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
        output_width=2580,
        output_height=1080,
    )

    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    graph = _option_value(command, "-filter_complex")

    assert "[vout_scaled]pad=2624:1088:0:0[vout_padded]" in graph
    assert _option_value(command, "-bsf:v") == (
        "hevc_metadata=crop_right=44:crop_bottom=8"
    )


def test_native_hevc_vaapi_export_also_hides_surface_padding(tmp_path):
    item = project()
    item.source.width = 1920
    item.source.height = 1080
    options = ExportOptions(
        tmp_path,
        "native-hevc",
        video_codec="hevc",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
    )

    command = build_ffmpeg_command(item, options, tmp_path / ".partial")
    graph = _option_value(command, "-filter_complex")

    assert "[vout]pad=1920:1088:0:0[vout_padded]" in graph
    assert _option_value(command, "-bsf:v") == "hevc_metadata=crop_bottom=8"


@pytest.mark.parametrize(
    "options",
    (
        ExportOptions(Path("/tmp"), "x", output_width=1280),
        ExportOptions(Path("/tmp"), "x", output_width=1279, output_height=720),
    ),
)
def test_invalid_output_dimension_pairs_are_rejected(options):
    with pytest.raises(ExportValidationError):
        validate_export_options(options)


def test_export_resolution_validation_rejects_stretching_and_upscaling():
    item = project()
    item.source.width = 1920
    item.source.height = 1080

    with pytest.raises(ExportValidationError, match="preserve"):
        validate_export_resolution(
            item,
            ExportOptions(
                Path("/tmp"), "x", output_width=1024, output_height=720
            ),
        )
    with pytest.raises(ExportValidationError, match="lower"):
        validate_export_resolution(
            item,
            ExportOptions(
                Path("/tmp"), "x", output_width=2560, output_height=1440
            ),
        )


def test_export_seeks_to_first_retained_segment_before_decoding(tmp_path):
    item = project()
    item.delete(item.segments[0].id)

    command = build_ffmpeg_command(
        item, ExportOptions(tmp_path, "edited"), tmp_path / ".partial"
    )
    graph = _option_value(command, "-filter_complex")

    assert _option_value(command, "-ss") == "2.000000"
    assert command.index("-ss") < command.index("-i")
    assert "trim=start=0.000000:end=3.000000" in graph
    assert "atrim=start=0.000000:end=3.000000" in graph


def test_export_does_not_add_a_zero_input_seek(tmp_path):
    command = build_ffmpeg_command(
        project(), ExportOptions(tmp_path, "edited"), tmp_path / ".partial"
    )

    assert "-ss" not in command


def test_safe_destination_and_progress(tmp_path):
    with pytest.raises(ExportValidationError):
        destination_path(ExportOptions(tmp_path, "../escape"), "/source.mkv")
    with pytest.raises(ExportValidationError):
        destination_path(ExportOptions(tmp_path, ".mkv"), "/source.mkv")
    source = tmp_path / "same.mkv"
    with pytest.raises(ExportValidationError):
        destination_path(ExportOptions(tmp_path, "same"), source)
    assert parse_progress_line("out_time_us=250", 1000) == 0.25
    assert parse_progress_line("out_time_ms=2000", 1000) == 1.0
    assert parse_progress_line("out_time_us=invalid", 1000) is None
    assert parse_progress_line("frame=42", 1000) is None


def test_format_compatibility_filters_video_and_audio_codecs():
    all_encoders = {
        "libx264",
        "libx265",
        "libvpx-vp9",
        "libaom-av1",
        "aac",
        "libopus",
        "flac",
        "pcm_s16le",
    }
    assert [codec.id for codec in compatible_video_codecs("mkv", all_encoders)] == [
        "h264",
        "hevc",
        "vp9",
        "av1",
    ]
    assert [codec.id for codec in compatible_video_codecs("mov", all_encoders)] == [
        "h264",
        "hevc",
    ]
    assert [codec.id for codec in compatible_audio_codecs("mov", all_encoders)] == [
        "aac",
        "pcm_s16le",
    ]
    assert [codec.id for codec in compatible_audio_codecs("mp4", all_encoders)] == [
        "aac",
        "opus",
        "flac",
        "pcm_s16le",
    ]
    assert [codec.id for codec in compatible_audio_codecs("ts", all_encoders)] == [
        "aac"
    ]


def test_encoder_detection_parses_ffmpeg_capability_output(monkeypatch):
    output = """
 V....D libx264              H.264 encoder
 V..... libsvtav1            AV1 encoder
 A....D aac                  AAC encoder
 S..... srt                  subtitle encoder
 """
    monkeypatch.setattr(
        "editor_export.run_media_tool",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
    )
    assert detect_export_encoders() == frozenset(
        {"libx264", "libsvtav1", "aac", "srt"}
    )


def test_hardware_detection_requires_a_successful_probe_for_each_rate_mode(monkeypatch):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        supported = command[command.index("-c:v") + 1] == "h264_vaapi"
        if "-rc_mode" in command:
            supported = supported and command[command.index("-rc_mode") + 1] in {
                "CQP",
                "VBR",
            }
        return SimpleNamespace(returncode=0 if supported else 1, stderr="unsupported mode")

    monkeypatch.setattr("editor_export.run_media_tool", run)
    detect_hardware_export_encoders.cache_clear()
    capabilities = detect_hardware_export_encoders(
        frozenset({"h264_vaapi", "hevc_vaapi"}),
        ("/dev/dri/renderD128", "/tmp/not-a-render-device"),
    )
    detect_hardware_export_encoders.cache_clear()

    assert len(capabilities) == 1
    assert capabilities[0].backend == "vaapi"
    assert capabilities[0].device == "/dev/dri/renderD128"
    assert capabilities[0].codec == "h264"
    assert capabilities[0].encoder == "h264_vaapi"
    assert capabilities[0].rate_controls == frozenset({"cqp", "vbr"})
    assert all("format=nv12,hwupload" in command for command in commands)
    assert all("color=c=black:s=640x360:r=30" in command for command in commands)


def test_vaapi_detection_uses_codec_specific_quality_controls(monkeypatch):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("editor_export.run_media_tool", run)
    detect_hardware_export_encoders.cache_clear()
    capabilities = detect_hardware_export_encoders(
        frozenset({"hevc_vaapi", "av1_vaapi"}),
        ("/dev/dri/renderD128",),
    )
    detect_hardware_export_encoders.cache_clear()

    assert [capability.codec for capability in capabilities] == ["hevc", "av1"]
    assert all(
        capability.rate_controls == frozenset({"cqp", "cbr", "vbr"})
        for capability in capabilities
    )
    cqp_commands = [command for command in commands if "CQP" in command]
    hevc_command = next(command for command in cqp_commands if "hevc_vaapi" in command)
    av1_command = next(command for command in cqp_commands if "av1_vaapi" in command)
    assert "-qp" in hevc_command
    assert "-global_quality" not in hevc_command
    assert _option_value(hevc_command, "-qp") == "23"
    assert _option_value(av1_command, "-global_quality") == "115"
    assert "-qp" not in av1_command


def test_nvenc_detection_hides_advertised_encoders_when_initialization_fails(
    monkeypatch,
):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        encoder = command[command.index("-c:v") + 1]
        supported = encoder == "h264_nvenc"
        if "-rc" in command:
            rate_control = command[command.index("-rc") + 1]
            supported = supported and rate_control in {"constqp", "cbr"}
        return SimpleNamespace(returncode=0 if supported else 1, stderr="NVENC unavailable")

    monkeypatch.setattr("editor_export.run_media_tool", run)
    detect_hardware_export_encoders.cache_clear()
    capabilities = detect_hardware_export_encoders(
        frozenset({"h264_nvenc", "hevc_nvenc"}), ()
    )
    detect_hardware_export_encoders.cache_clear()

    assert len(capabilities) == 1
    assert capabilities[0].backend == "nvenc"
    assert capabilities[0].device == "any"
    assert capabilities[0].label == "NVIDIA NVENC"
    assert capabilities[0].codec == "h264"
    assert capabilities[0].rate_controls == frozenset({"cqp", "cbr"})
    assert all("hwupload" not in " ".join(command) for command in commands)


def test_process_wide_capability_probe_runs_only_once(monkeypatch):
    expected = ExportEncoderCapabilities(frozenset({"libx264", "aac"}), ())
    calls = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(editor_export, "_EXPORT_CAPABILITY_STARTED", False)
    monkeypatch.setattr(editor_export, "_EXPORT_CAPABILITY_RESULT", None)
    monkeypatch.setattr(editor_export.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        editor_export,
        "probe_export_encoder_capabilities",
        lambda: calls.append(True) or expected,
    )

    editor_export.start_export_encoder_capability_probe()
    editor_export.start_export_encoder_capability_probe()

    assert calls == [True]
    assert editor_export.export_encoder_capability_result() is expected


def test_clipper_defers_export_capability_probe_until_after_application_startup():
    source = (Path(__file__).resolve().parents[1] / "ui" / "main.py").read_text(
        encoding="utf-8"
    )
    startup = source.split("    def do_startup", 1)[1].split(
        "    def _presentation_state", 1
    )[0]

    assert "GLib.timeout_add_seconds" in startup
    assert "from editor_export" not in startup
    assert "def _start_export_capability_probe" in source


@pytest.mark.parametrize(
    ("video_codec", "encoder"),
    (
        ("h264", "libx264"),
        ("hevc", "libx265"),
        ("vp9", "libvpx-vp9"),
        ("av1", "libaom-av1"),
    ),
)
def test_cqp_value_matches_recording_backend_for_every_video_codec(
    tmp_path, video_codec, encoder
):
    options = ExportOptions(
        tmp_path,
        "edited",
        video_codec=video_codec,
        audio_codec="opus",
    )
    command = build_ffmpeg_command(project(), options, tmp_path / ".partial")
    assert _option_value(command, "-c:v") == encoder
    assert _option_value(command, "-crf") == "23"
    assert _option_value(command, "-c:a") == "libopus"
    assert _option_value(command, "-pix_fmt") == "yuv420p"
    if video_codec in {"vp9", "av1"}:
        assert _option_value(command, "-b:v") == "0"


def test_cbr_and_vbr_commands_use_selected_bitrate_controls(tmp_path):
    cbr = ExportOptions(
        tmp_path,
        "constant",
        rate_control="cbr",
        video_bitrate=8_000,
    )
    command = build_ffmpeg_command(project(), cbr, tmp_path / ".partial-cbr")
    assert _option_value(command, "-b:v") == "8000k"
    assert _option_value(command, "-minrate") == "8000k"
    assert _option_value(command, "-maxrate") == "8000k"
    assert _option_value(command, "-bufsize") == "16000k"
    assert "-crf" not in command

    vbr = ExportOptions(
        tmp_path,
        "variable",
        format="mp4",
        video_codec="hevc",
        rate_control="vbr",
        video_bitrate=10_000,
        video_max_bitrate=16_000,
    )
    command = build_ffmpeg_command(project(), vbr, tmp_path / ".partial-vbr")
    assert _option_value(command, "-b:v") == "10000k"
    assert "-minrate" not in command
    assert _option_value(command, "-maxrate") == "16000k"
    assert _option_value(command, "-bufsize") == "32000k"
    assert command[command.index("-tag:v") + 1] == "hvc1"


def test_vaapi_command_uploads_filtered_frames_and_uses_exact_cqp(tmp_path):
    options = ExportOptions(
        tmp_path,
        "hardware",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
        quality_cqp=24,
    )

    command = build_ffmpeg_command(project(), options, tmp_path / ".partial-hardware")
    graph = _option_value(command, "-filter_complex")

    assert _option_value(command, "-vaapi_device") == "/dev/dri/renderD128"
    assert command.index("-vaapi_device") < command.index("-i")
    assert _option_value(command, "-c:v") == "h264_vaapi"
    assert "[vout]format=nv12,hwupload[vout_hw]" in graph
    assert "[vout_hw]" in [
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "-map"
    ]
    assert _option_value(command, "-rc_mode") == "CQP"
    assert _option_value(command, "-qp") == "24"
    assert "-crf" not in command
    assert "-pix_fmt" not in command


def test_av1_vaapi_matches_obs_quality_scale_for_the_selected_cqp(tmp_path):
    options = ExportOptions(
        tmp_path,
        "hardware-av1",
        video_codec="av1",
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
        quality_cqp=24,
    )

    command = build_ffmpeg_command(project(), options, tmp_path / ".partial-av1")

    assert _option_value(command, "-c:v") == "av1_vaapi"
    assert _option_value(command, "-rc_mode") == "CQP"
    assert _option_value(command, "-global_quality") == "120"
    assert "-qp" not in command


@pytest.mark.parametrize(
    ("rate_control", "rc_mode"), (("cbr", "CBR"), ("vbr", "VBR"))
)
def test_vaapi_bitrate_modes_are_explicit(tmp_path, rate_control, rc_mode):
    options = ExportOptions(
        tmp_path,
        "hardware-bitrate",
        rate_control=rate_control,
        video_encoder="vaapi",
        hardware_device="/dev/dri/renderD128",
    )
    command = build_ffmpeg_command(project(), options, tmp_path / ".partial")

    assert _option_value(command, "-rc_mode") == rc_mode
    assert _option_value(command, "-b:v") == "12000k"


def test_nvenc_command_uses_software_frames_and_exact_cqp(tmp_path):
    options = ExportOptions(
        tmp_path,
        "nvenc",
        video_codec="hevc",
        video_encoder="nvenc",
        hardware_device="any",
        quality_cqp=25,
    )
    command = build_ffmpeg_command(project(), options, tmp_path / ".partial-nvenc")
    graph = _option_value(command, "-filter_complex")

    assert _option_value(command, "-c:v") == "hevc_nvenc"
    assert _option_value(command, "-preset") == "p4"
    assert _option_value(command, "-rc") == "constqp"
    assert _option_value(command, "-qp") == "25"
    assert _option_value(command, "-pix_fmt") == "yuv420p"
    assert "hwupload" not in graph
    assert "-vaapi_device" not in command


@pytest.mark.parametrize(
    "options",
    (
        ExportOptions(Path("/tmp"), "x", video_codec="bad"),
        ExportOptions(Path("/tmp"), "x", audio_codec="bad"),
        ExportOptions(Path("/tmp"), "x", format="mov", video_codec="av1"),
        ExportOptions(Path("/tmp"), "x", format="mov", audio_codec="opus"),
        ExportOptions(Path("/tmp"), "x", rate_control="abr"),
        ExportOptions(Path("/tmp"), "x", quality_cqp=19),
        ExportOptions(Path("/tmp"), "x", quality_cqp=31),
        ExportOptions(Path("/tmp"), "x", video_encoder="automatic"),
        ExportOptions(
            Path("/tmp"),
            "x",
            video_encoder="software",
            hardware_device="/dev/dri/renderD128",
        ),
        ExportOptions(Path("/tmp"), "x", video_encoder="vaapi"),
        ExportOptions(Path("/tmp"), "x", video_encoder="nvenc"),
        ExportOptions(
            Path("/tmp"),
            "x",
            video_codec="vp9",
            video_encoder="nvenc",
            hardware_device="any",
        ),
        ExportOptions(
            Path("/tmp"),
            "x",
            video_encoder="vaapi",
            hardware_device="/dev/dri/card0",
        ),
        ExportOptions(
            Path("/tmp"),
            "x",
            rate_control="vbr",
            video_bitrate=20_000,
            video_max_bitrate=10_000,
        ),
    ),
)
def test_invalid_codec_rate_and_container_combinations_are_rejected(options):
    with pytest.raises(ExportValidationError):
        validate_export_options(options)


def test_pcm_is_uncompressed_and_has_no_bitrate_argument(tmp_path):
    options = ExportOptions(
        tmp_path,
        "pcm",
        format="mov",
        audio_codec="pcm_s16le",
    )
    command = build_ffmpeg_command(project(), options, tmp_path / ".partial")

    assert _option_value(command, "-c:a") == "pcm_s16le"
    audio_encoder_index = command.index("-c:a")
    assert "-b:a" not in command[audio_encoder_index:]


class _FinishedProcess:
    def __init__(self, stdout, return_code=0):
        self.stdout = io.StringIO(stdout)
        self.return_code = return_code
        self.pid = 1234

    def wait(self):
        return self.return_code

    def poll(self):
        return self.return_code


def test_export_process_drains_progress_before_atomic_replacement(tmp_path, monkeypatch):
    exporter = ExportProcess(project(), ExportOptions(tmp_path, "finished"))
    exporter.process = _FinishedProcess(
        "out_time_us=1000000\nprogress=continue\nout_time_us=5000000\nprogress=end\n"
    )
    monkeypatch.setattr(exporter, "_validate_partial", lambda: None)
    progress = []

    destination = exporter.finish(progress.append)

    assert destination == tmp_path / "finished.mkv"
    assert destination.exists()
    assert progress == [0.2, 1.0]
    assert not exporter.partial.exists()


def test_export_process_reports_ffmpeg_detail_and_removes_partial(tmp_path, caplog):
    caplog.set_level("INFO", logger="clipper.editor.export")
    # Application logging may already be installed by another test.
    caplog.handler.setLevel("INFO")
    editor_export._LOG.addHandler(caplog.handler)
    exporter = ExportProcess(project(), ExportOptions(tmp_path, "failed"))
    exporter.process = _FinishedProcess("", return_code=1)
    exporter._stderr.write(
        "[encoder] The selected encoder could not be opened\n"
        "Nothing was written into output file\n"
    )

    try:
        with pytest.raises(RuntimeError, match="selected encoder could not be opened"):
            exporter.finish()
    finally:
        editor_export._LOG.removeHandler(caplog.handler)

    assert not exporter.partial.exists()
    assert "encoding failed" in caplog.text
    assert "exit=1" in caplog.text
    assert "selected encoder could not be opened" in caplog.text
    assert "Nothing was written into output file" in caplog.text


def test_hardware_export_failure_recommends_the_safe_software_path(tmp_path):
    exporter = ExportProcess(
        project(),
        ExportOptions(
            tmp_path,
            "failed-hardware",
            video_encoder="vaapi",
            hardware_device="/dev/dri/renderD128",
        ),
    )
    exporter.process = _FinishedProcess("", return_code=1)
    exporter._stderr.write("Failed to create VAAPI encode context\n")

    with pytest.raises(RuntimeError, match="choose Software"):
        exporter.finish()

    assert not exporter.partial.exists()


def test_export_process_cancel_has_a_distinct_result(tmp_path, monkeypatch):
    exporter = ExportProcess(project(), ExportOptions(tmp_path, "cancelled"))
    exporter.process = _FinishedProcess("", return_code=-15)
    exporter.process.return_code = None
    killed = []
    monkeypatch.setattr("editor_export.os.killpg", lambda pid, sig: killed.append((pid, sig)))

    exporter.cancel()
    exporter.process.return_code = -15

    with pytest.raises(ExportCancelledError, match="Export cancelled"):
        exporter.finish()
    assert killed and killed[0][0] == 1234
    assert not exporter.partial.exists()


def test_export_validation_checks_selected_stream_codecs(tmp_path, monkeypatch):
    exporter = ExportProcess(
        project(),
        ExportOptions(tmp_path, "validated", video_codec="vp9", audio_codec="opus"),
    )
    payload = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": VIDEO_CODECS["vp9"].probe_codec},
            {"index": 1, "codec_type": "audio", "codec_name": AUDIO_CODECS["opus"].probe_codec},
        ],
        "format": {"duration": "5.000000"},
    }
    monkeypatch.setattr(
        "editor_export.run_media_tool",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )

    exporter._validate_partial()

    payload["streams"][0]["codec_name"] = "h264"
    with pytest.raises(RuntimeError, match="did not match the selected options"):
        exporter._validate_partial()


def test_export_validation_decodes_a_frame_to_catch_surface_padding(
    tmp_path, monkeypatch
):
    item = project()
    item.source.width = 2560
    item.source.height = 1440
    exporter = ExportProcess(
        item,
        ExportOptions(
            tmp_path,
            "validated-size",
            output_width=1920,
            output_height=1080,
        ),
    )
    payload = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
        "frames": [{"width": 1920, "height": 1080}],
        "format": {"duration": "5.000000"},
    }
    monkeypatch.setattr(
        "editor_export.run_media_tool",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )

    exporter._validate_partial()

    payload["frames"][0]["height"] = 1088
    with pytest.raises(RuntimeError, match="did not match the selected options"):
        exporter._validate_partial()
