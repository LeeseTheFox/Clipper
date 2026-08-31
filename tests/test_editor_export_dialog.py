from pathlib import Path
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from editor_export import (
    ExportEncoderCapabilities,
    ExportOptions,
    HardwareEncoderCapability,
)
from editor_export_dialog import (
    EditorExportDialog,
    ExportEtaEstimator,
    format_time_remaining,
)
from editor_model import AudioTrack, EditorProject, Source
from editor_window import EditorWindow
from gi.repository import Adw

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def export_dialog(monkeypatch, tmp_path):
    available_encoders = frozenset(
        {
            "libx264",
            "libx265",
            "libvpx-vp9",
            "libaom-av1",
            "aac",
            "libopus",
            "flac",
            "pcm_s16le",
        }
    )
    capabilities = ExportEncoderCapabilities(available_encoders, ())
    monkeypatch.setattr(
        "editor_export_dialog.export_encoder_capability_result",
        lambda: capabilities,
    )
    monkeypatch.setattr(
        "editor_export_dialog.start_export_encoder_capability_probe", lambda: None
    )
    source = Source(
        str(tmp_path / "source.mkv"),
        1,
        2,
        5_000_000,
        0,
        [AudioTrack("a0", 0, 1, "Game")],
        width=3440,
        height=1440,
    )
    project = EditorProject.new(source)
    accepted = []
    dialog = EditorExportDialog(
        Adw.Window(),
        project,
        {
            "output_folder": str(tmp_path),
            "format": "mkv",
            "rate_control": "cqp",
            "quality_cqp": 23,
            "video_bitrate": 12_000,
            "video_max_bitrate": 20_000,
        },
        accepted.append,
    )
    return dialog, accepted


def test_export_uses_native_adwaita_dialog_and_forward_quality_scale(export_dialog):
    dialog, _accepted = export_dialog

    assert isinstance(dialog, Adw.Dialog)
    assert dialog.get_title() == "Export edited clip"
    assert dialog.quality.get_inverted() is False
    assert dialog.quality.get_adjustment().get_lower() == 20
    assert dialog.quality.get_adjustment().get_upper() == 30
    assert dialog._format_quality_value(None, dialog.quality.get_value()) == "23"
    assert dialog._format_quality_value(None, 20) == "30"
    assert dialog._format_quality_value(None, 30) == "20"
    assert dialog._options().quality_cqp == 23
    assert dialog._options().video_codec == "h264"
    assert dialog._options().video_encoder == "software"
    assert dialog._options().hardware_device is None
    assert dialog._options().output_width is None
    assert dialog._options().output_height is None
    assert dialog._options().audio_codec == "aac"
    assert dialog.video_codec_row.get_subtitle() == "Encoder implementation"
    assert dialog.hardware_acceleration_row.get_active() is False
    assert dialog.hardware_acceleration_row.get_sensitive() is False
    assert dialog.hardware_acceleration_row.get_subtitle() == (
        "libx264 — software encoding; hardware unavailable"
    )
    assert dialog.hardware_acceleration_row.get_tooltip_text() == (
        "libx264 — software encoding; hardware unavailable"
    )
    assert dialog.rate_control_row.get_subtitle() == "Encoder bitrate and quality mode"
    assert dialog.quality_row.get_subtitle() == (
        "Lower number = better quality, larger file size"
    )
    assert dialog.audio_codec_row.get_subtitle() == "Audio compression format"
    assert dialog.resolution_row.get_subtitle() == "Preserves the source aspect ratio"
    assert dialog.audio_layout_row.get_tooltip_text() == "Keep separate tracks"
    assert [
        dialog.resolution_row.get_model().get_string(index)
        for index in range(dialog.resolution_row.get_model().get_n_items())
    ] == [
        "3440 × 1440 (native)",
        "2580 × 1080",
        "1720 × 720",
        "1146 × 480",
        "860 × 360",
        "574 × 240",
    ]
    assert [
        dialog.rate_control_row.get_model().get_string(index)
        for index in range(dialog.rate_control_row.get_model().get_n_items())
    ] == ["CQP", "Constant bitrate", "Variable bitrate"]

    dialog.resolution_row.set_selected(2)
    assert dialog._options().output_width == 1720
    assert dialog._options().output_height == 720


def test_hardware_encoder_defaults_on_and_filters_unsupported_modes(export_dialog):
    dialog, _accepted = export_dialog
    capability = HardwareEncoderCapability(
        "vaapi",
        "/dev/dri/renderD128",
        "VAAPI — AMD",
        "h264",
        "h264_vaapi",
        frozenset({"cqp", "vbr"}),
    )

    dialog._apply_encoder_capabilities(
        ExportEncoderCapabilities(dialog.available_encoders, (capability,))
    )

    assert dialog.hardware_acceleration_row.get_sensitive() is True
    assert dialog.hardware_acceleration_row.get_active() is True
    assert dialog.hardware_acceleration_row.get_subtitle() == (
        "h264_vaapi (VAAPI — AMD)"
    )
    assert dialog.hardware_acceleration_row.get_tooltip_text() == (
        "h264_vaapi (VAAPI — AMD)"
    )
    assert dialog._options().video_encoder == "vaapi"
    assert dialog._options().hardware_device == "/dev/dri/renderD128"
    assert [
        dialog.rate_control_row.get_model().get_string(index)
        for index in range(dialog.rate_control_row.get_model().get_n_items())
    ] == ["CQP", "Variable bitrate"]

    dialog.video_codec_row.set_selected(1)
    assert dialog._options().video_codec == "hevc"
    assert dialog._options().video_encoder == "software"
    assert dialog.hardware_acceleration_row.get_active() is False
    assert dialog.hardware_acceleration_row.get_sensitive() is False
    assert dialog.hardware_acceleration_row.get_subtitle() == (
        "libx265 — software encoding; hardware unavailable"
    )
    assert dialog.hardware_acceleration_row.get_tooltip_text() == (
        "libx265 — software encoding; hardware unavailable"
    )

    dialog.video_codec_row.set_selected(0)
    assert dialog.hardware_acceleration_row.get_active() is True
    assert dialog._options().video_encoder == "vaapi"

    dialog.hardware_acceleration_row.set_active(False)
    assert dialog._options().video_encoder == "software"
    assert dialog._options().hardware_device is None
    assert dialog.hardware_acceleration_row.get_subtitle() == (
        "libx264 — software encoding"
    )


def test_format_selection_keeps_only_compatible_codecs(export_dialog):
    dialog, _accepted = export_dialog

    dialog.format_row.set_selected(2)
    assert dialog._format_id() == "mov"
    assert dialog.extension_label.get_text() == ".mov"
    assert dialog._video_codec_ids == ("h264", "hevc")
    assert dialog._audio_codec_ids == ("aac", "pcm_s16le")

    dialog.format_row.set_selected(1)
    assert dialog._video_codec_ids == ("h264", "hevc", "vp9", "av1")
    assert dialog._audio_codec_ids == ("aac", "opus", "flac", "pcm_s16le")

    dialog.format_row.set_selected(3)
    assert dialog._audio_codec_ids == ("aac",)


def test_rate_control_reveals_only_relevant_rows(export_dialog):
    dialog, _accepted = export_dialog

    dialog.rate_control_row.set_selected(1)
    assert dialog.quality_row.get_visible() is False
    assert dialog.bitrate_row.get_visible() is True
    assert dialog.max_bitrate_row.get_visible() is False
    assert dialog.bitrate_row.get_title() == "Constant bitrate"

    dialog.rate_control_row.set_selected(2)
    dialog.max_bitrate.set_value(10_000)
    dialog.bitrate.set_value(24_000)
    assert dialog.quality_row.get_visible() is False
    assert dialog.bitrate_row.get_visible() is True
    assert dialog.max_bitrate_row.get_visible() is True
    assert dialog.max_bitrate.get_value() == 24_000

    dialog.audio_layout_row.set_selected(1)
    assert dialog.audio_layout_row.get_tooltip_text() == "Combine into one track"


def test_dialog_restores_all_export_options_from_editor_session(
    export_dialog, tmp_path
):
    dialog, _accepted = export_dialog
    remembered = ExportOptions(
        folder=tmp_path,
        filename="remembered-export",
        format="mp4",
        quality_cqp=29,
        audio_layout="mixed",
        video_codec="av1",
        audio_codec="opus",
        rate_control="vbr",
        video_bitrate=18_000,
        video_max_bitrate=26_000,
        video_encoder="software",
        output_width=1720,
        output_height=720,
    )

    restored = EditorExportDialog(
        dialog.parent_window,
        dialog.project,
        {},
        lambda _options: None,
        initial_options=remembered,
    )

    assert restored.snapshot_options() == remembered
    assert restored.hardware_acceleration_row.get_active() is False


def test_editor_reuses_export_snapshot_when_dialog_is_reopened(monkeypatch):
    remembered = ExportOptions(Path("/tmp"), "remembered")
    first_dialog = SimpleNamespace(snapshot_options=lambda: remembered)
    window = SimpleNamespace(
        _cleaned=False,
        _exporter=None,
        _export_dialog=first_dialog,
        _export_options=None,
        project=object(),
        config={},
        _start_export=lambda _options: None,
        _cancel_export=lambda: None,
        _on_export_dialog_closed=lambda _dialog: None,
    )
    EditorWindow._on_export_dialog_closed(window, first_dialog)

    created = []

    class DialogStub:
        def __init__(self, *args, **kwargs):
            created.append((args, kwargs))

        def connect(self, *_args):
            pass

        def present(self, _parent):
            pass

    monkeypatch.setattr("editor_export_dialog.EditorExportDialog", DialogStub)
    EditorWindow._show_export(window)

    assert created[0][1]["initial_options"] is remembered


def test_editor_cleanup_discards_export_snapshot():
    remembered = ExportOptions(Path("/tmp"), "remembered")
    window = SimpleNamespace(
        _cleaned=False,
        _export_options=remembered,
        _gain_drag_key=None,
        _cancel_gain_redraw=lambda: None,
        _cancel_zoom_update=lambda: None,
        _cancel_discrete_zoom_settle=lambda: None,
        _cancel_waveform_detail_restore=lambda: None,
        _waveform_cancel=SimpleNamespace(set=lambda: None),
        _cancel_scrub_preview_seek=lambda: None,
        _stop_edge_scroll=lambda: None,
        _zoom_anchor_timeout_id=None,
        _waveform_pool=SimpleNamespace(
            shutdown=lambda **_kwargs: None,
        ),
        _exporter=None,
        _source_monitor=None,
        preview=SimpleNamespace(cleanup=lambda: None),
    )

    EditorWindow.cleanup(window)

    assert window._export_options is None


def test_closed_dialog_does_not_restore_snapshot_after_editor_cleanup():
    remembered = ExportOptions(Path("/tmp"), "remembered")
    dialog = SimpleNamespace(snapshot_options=lambda: remembered)
    window = SimpleNamespace(
        _cleaned=True,
        _export_dialog=dialog,
        _export_options=None,
    )

    EditorWindow._on_export_dialog_closed(window, dialog)

    assert window._export_options is None


def test_existing_destination_requires_explicit_replace(export_dialog, tmp_path):
    dialog, accepted = export_dialog
    destination = tmp_path / "source-edited.mkv"
    destination.write_bytes(b"existing")

    dialog._accept()

    assert accepted == []
    assert dialog.export_button.get_label() == "Replace"
    assert dialog.export_button.has_css_class("destructive-action")
    assert dialog.banner.get_revealed() is True
    assert destination.name in dialog.banner.get_title()


def test_completed_export_keeps_preserved_options_editable_for_another_export(
    export_dialog, tmp_path
):
    dialog, accepted = export_dialog
    dialog.resolution_row.set_selected(2)
    dialog.quality.set_value(25)

    dialog._accept()

    assert len(accepted) == 1
    assert dialog._export_active is True
    assert dialog.get_can_close() is False
    assert dialog.options_page.get_sensitive() is False
    assert dialog.options_page.get_visible() is True
    assert dialog.progress_revealer.get_reveal_child() is True
    assert dialog.export_button.get_visible() is False
    assert dialog.export_progress.get_show_text() is False
    assert dialog.export_progress.get_valign().value_nick == "center"
    assert dialog.progress_percentage.get_valign().value_nick == "center"
    assert dialog.progress_cancel_button.get_label() == "Cancel"
    assert dialog.progress_show_button.get_visible() is False

    dialog.update_export_progress(0.42)
    assert dialog.export_progress.get_fraction() == pytest.approx(0.42)
    assert dialog.progress_percentage.get_text() == "42%"

    dialog._on_close_attempt()
    assert dialog.banner.get_revealed() is True
    assert dialog.banner.get_title() == "Cancel the export before closing the editor"

    destination = tmp_path / "source-edited.mkv"
    dialog.complete_export(destination)
    assert dialog._export_active is False
    assert dialog.get_can_close() is True
    assert dialog.options_page.get_sensitive() is True
    assert dialog.export_button.get_visible() is True
    assert dialog.export_button.get_label() == "Export"
    assert dialog.export_button.has_css_class("suggested-action")
    assert dialog.progress_destination.get_text() == "Exported source-edited.mkv"
    assert dialog.progress_percentage.get_text() == "100%"
    assert dialog.progress_eta.get_text() == "Finished"
    assert dialog.progress_show_button.get_visible() is True
    assert dialog.progress_cancel_button.get_label() == "Close"
    assert not dialog.progress_cancel_button.has_css_class("suggested-action")
    assert dialog._completed_destination == destination
    assert dialog.resolution_row.get_selected() == 2
    assert dialog.quality.get_value() == 25

    dialog.filename.set_text("source-second-export")
    dialog.quality.set_value(27)
    dialog._accept()

    assert len(accepted) == 2
    assert accepted[1].filename == "source-second-export"
    assert accepted[1].quality_cqp == 23
    assert accepted[1].output_width == 1720
    assert accepted[1].output_height == 720
    assert dialog._export_active is True
    assert dialog.options_page.get_sensitive() is False
    assert dialog.export_button.get_visible() is False
    assert dialog.progress_destination.get_text() == "source-second-export.mkv"
    assert dialog.progress_show_button.get_visible() is False
    assert dialog.progress_cancel_button.get_label() == "Cancel"


def test_export_cancellation_returns_to_preserved_options(export_dialog):
    dialog, accepted = export_dialog
    cancellations = []
    dialog.cancelled_callback = lambda: cancellations.append(True)

    dialog._accept()
    dialog._on_progress_button_clicked()

    assert len(accepted) == 1
    assert cancellations == [True]
    assert dialog._export_cancelling is True
    assert dialog.progress_destination.get_text() == "source-edited.mkv"
    assert dialog.progress_eta.get_text() == ""
    assert dialog.progress_eta.get_visible() is False
    assert dialog.progress_cancel_button.get_sensitive() is False

    dialog.export_cancelled()
    assert dialog._export_active is False
    assert dialog.get_can_close() is True
    assert dialog.options_page.get_sensitive() is True
    assert dialog.progress_revealer.get_reveal_child() is False
    assert dialog.progress_show_button.get_visible() is False
    assert dialog.export_button.get_visible() is True
    assert dialog.banner.get_title() == "Export cancelled"
    assert dialog.banner.get_revealed() is True
    assert dialog.filename.get_text() == "source-edited"


def test_eta_uses_a_smoothed_deadline_and_clear_approximate_text():
    estimator = ExportEtaEstimator(clock=lambda: 0.0)

    assert estimator.observe(0.01, now=1.0) is None
    assert estimator.observe(0.02, now=2.0) is None
    assert estimator.observe(0.03, now=3.0) is None
    assert estimator.observe(0.04, now=4.0) == pytest.approx(96.0)
    assert estimator.estimate(now=5.0) == pytest.approx(95.0)
    assert estimator.estimate(now=8.0) == pytest.approx(94.0)
    assert estimator.observe(1.0, now=100.0) == 0.0

    assert format_time_remaining(1) == "About 1 second remaining"
    assert format_time_remaining(65) == "About 1 minute, 5 seconds remaining"
    assert format_time_remaining(3_600) == "About 1 hour remaining"
    assert format_time_remaining(7_260) == "About 2 hours, 1 minute remaining"


def test_eta_adapts_to_non_linear_progress_without_large_single_sample_jumps():
    estimator = ExportEtaEstimator(clock=lambda: 0.0)
    estimates = []

    # Encode at 2%/s, then encounter a sustained section at half that speed.
    for second in range(1, 41):
        progress = second * 0.02 if second <= 20 else 0.4 + (second - 20) * 0.01
        estimate = estimator.observe(progress, now=float(second))
        if estimate is not None:
            estimates.append(estimate)

    changes = [
        later - earlier
        for earlier, later in zip(estimates, estimates[1:], strict=False)
    ]
    assert max(changes) <= 1.0
    assert estimates[-1] > estimates[19]
    assert estimates[-1] == pytest.approx(36.0, abs=3.0)


def test_eta_ignores_startup_delay_and_quickly_discards_a_stale_high_forecast():
    estimator = ExportEtaEstimator(clock=lambda: 0.0)

    # FFmpeg takes three seconds to emit its first useful timestamp, then
    # advances at a steady 2%/s. The fixed startup cost must not be treated as
    # the speed of the remaining encode.
    estimates = []
    for second in range(3, 11):
        progress = 0.002 if second == 3 else 0.02 * (second - 3)
        estimate = estimator.observe(progress, now=float(second))
        if estimate is not None:
            estimates.append(estimate)

    assert estimates[-1] == pytest.approx(43.0, abs=3.0)

    # Even if an earlier phase produced a pessimistic deadline, a current fast
    # rate must pull it down promptly rather than leaving minutes at 99%.
    estimator._deadline = 240.0
    for second in range(11, 16):
        estimator.observe(0.89 + (second - 11) * 0.025, now=float(second))
    assert estimator.estimate(now=15.0) < 8.0


def test_editor_blocks_close_while_export_is_running():
    warnings = []
    dialog = SimpleNamespace(show_close_warning=lambda: warnings.append(True))
    window = SimpleNamespace(
        _exporter=object(),
        _export_dialog=dialog,
        _show_toast=lambda _message: None,
    )

    assert EditorWindow._block_close_during_export(window) is True
    assert warnings == [True]

    window._exporter = None
    assert EditorWindow._block_close_during_export(window) is False


def test_cancelling_close_notifies_session_end_caller():
    cancelled = []
    window = SimpleNamespace(
        _close_cancelled_callback=lambda: cancelled.append(True),
        save=lambda: None,
        _finish=lambda: None,
    )
    dialog = SimpleNamespace(choose_finish=lambda _result: "cancel")

    EditorWindow._close_response(window, dialog, object())

    assert cancelled == [True]
    assert window._close_cancelled_callback is None


def test_editor_routes_export_progress_and_cancellation_to_dialog():
    source = (REPO_ROOT / "ui" / "editor_window.py").read_text(encoding="utf-8")

    assert "self.export_status_revealer" not in source
    assert "export_dialog.update_export_progress(progress)" in source
    assert "exporter.finish(report_progress)" in source
    assert "self._cancel_export," in source
    assert "exporter.cancel()" in source
    assert "if self._block_close_during_export():" in source
    assert "dialog.present(self)" in source


def test_starting_export_saves_current_draft_before_snapshotting(monkeypatch):
    events = []

    class ExportProcessStub:
        def __init__(self, project, options):
            events.append(("create exporter", project, options))

    class ThreadStub:
        def __init__(self, *, target, daemon):
            assert callable(target)
            assert daemon is True

        def start(self):
            events.append(("start worker",))

    class ProjectStub:
        def clone(self):
            events.append(("clone project",))
            return "export snapshot"

    class WidgetStub:
        def set_text(self, _text):
            pass

        def set_sensitive(self, _sensitive):
            pass

    window = SimpleNamespace(
        project=ProjectStub(),
        save=lambda: events.append(("save draft",)),
        _exporter=None,
        _export_dialog=None,
        export_button_label=WidgetStub(),
        export_button=WidgetStub(),
        set_deletable=lambda _deletable: None,
        _source_invalid=False,
        _cleaned=False,
    )
    options = object()
    monkeypatch.setattr("editor_export.ExportProcess", ExportProcessStub)
    monkeypatch.setattr("editor_window.threading.Thread", ThreadStub)

    EditorWindow._start_export(window, options)

    assert events == [
        ("save draft",),
        ("clone project",),
        ("create exporter", "export snapshot", options),
        ("start worker",),
    ]
