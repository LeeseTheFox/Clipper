"""Native, compact export options dialog for the single-clip editor."""

from __future__ import annotations

import math
import time
from pathlib import Path

import gi
from i18n import _, ngettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from editor_export import (
    FORMAT_LABELS,
    FORMATS,
    VIDEO_CODECS,
    ExportEncoderCapabilities,
    ExportOptions,
    ExportValidationError,
    HardwareEncoderCapability,
    compatible_audio_codecs,
    compatible_video_codecs,
    destination_path,
    export_encoder_capability_result,
    export_resolution_options,
    source_display_dimensions,
    start_export_encoder_capability_probe,
    validate_export_options,
)
from gi.repository import Adw, Gio, GLib, Gtk, Pango
from icon_names import EXPORT_LINEAR
from icon_widgets import TintedIcon
from quality_controls import (
    CBR_BITRATE_SUBTITLE,
    CBR_BITRATE_TITLE,
    CQP_DEFAULT,
    CQP_HIGH_QUALITY,
    CQP_LOW_QUALITY,
    CQP_MEDIUM_QUALITY,
    MAX_BITRATE_SUBTITLE,
    MAX_BITRATE_TITLE,
    QUALITY_SUBTITLE,
    RATE_CONTROL_LABELS,
    RATE_CONTROL_SUBTITLE,
    VBR_BITRATE_SUBTITLE,
    VBR_BITRATE_TITLE,
    cqp_to_slider_value,
    slider_value_to_cqp,
)
from text_helpers import middle_truncate_text

_FORMAT_IDS = tuple(FORMATS)
_RATE_CONTROL_IDS = tuple(RATE_CONTROL_LABELS)
_AUDIO_LAYOUT_IDS = ("separate", "mixed")
_AUDIO_LAYOUT_LABELS = (_("Keep separate tracks"), _("Combine into one track"))
_BITRATE_MIN = 1_000
_BITRATE_MAX = 50_000
_BITRATE_STEP = 500
_FOLDER_LABEL_MAX_CHARS = 64
_CAPABILITY_POLL_MS = 100
_SAFE_INITIAL_ENCODERS = frozenset({"libx264", "aac"})
_EXPORT_OPTIONS_HEIGHT = 630
_ETA_UPDATE_MS = 1_000
_ETA_MIN_SAMPLES = 4
_ETA_MIN_SAMPLE_SPAN_SECONDS = 2.0
_ETA_SAMPLE_WINDOW_SECONDS = 12.0
_ETA_SAMPLE_HALF_LIFE_SECONDS = 3.0
_ETA_FASTER_HALF_LIFE_SECONDS = 1.0
_ETA_SLOWER_HALF_LIFE_SECONDS = 6.0
_ETA_STALL_GRACE_SECONDS = 2.0


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _index(values, preferred, default=0):
    try:
        return values.index(preferred)
    except ValueError:
        return default


def format_time_remaining(seconds: float) -> str:
    """Format an approximate ETA using natural, sentence-case UI text."""
    seconds = max(1, round(float(seconds)))
    if seconds < 60:
        duration = ngettext("%(count)d second", "%(count)d seconds", seconds) % {
            "count": seconds
        }
        return _("About %(duration)s remaining") % {"duration": duration}

    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        minute_text = ngettext("%(count)d minute", "%(count)d minutes", minutes) % {
            "count": minutes
        }
        if seconds == 0:
            return _("About %(duration)s remaining") % {"duration": minute_text}
        second_text = ngettext("%(count)d second", "%(count)d seconds", seconds) % {
            "count": seconds
        }
        duration = _("%(first)s, %(second)s") % {
            "first": minute_text,
            "second": second_text,
        }
        return _("About %(duration)s remaining") % {"duration": duration}

    hours, minutes = divmod(minutes, 60)
    hour_text = ngettext("%(count)d hour", "%(count)d hours", hours) % {
        "count": hours
    }
    if minutes == 0:
        return _("About %(duration)s remaining") % {"duration": hour_text}
    minute_text = ngettext("%(count)d minute", "%(count)d minutes", minutes) % {
        "count": minutes
    }
    duration = _("%(first)s, %(second)s") % {
        "first": hour_text,
        "second": minute_text,
    }
    return _("About %(duration)s remaining") % {"duration": duration}


class ExportEtaEstimator:
    """Estimate remaining time from a weighted regression of recent progress."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._started_at = self._clock()
        self._last_time = self._started_at
        self._last_progress = 0.0
        self._samples: list[tuple[float, float]] = []
        self._deadline: float | None = None

    @staticmethod
    def _alpha(elapsed: float, half_life: float) -> float:
        return 1.0 - math.exp(-math.log(2.0) * elapsed / half_life)

    def _recent_rate(self) -> float | None:
        if len(self._samples) < _ETA_MIN_SAMPLES:
            return None
        first_time = self._samples[0][0]
        last_time = self._samples[-1][0]
        if last_time - first_time < _ETA_MIN_SAMPLE_SPAN_SECONDS:
            return None

        weights = [
            math.exp(
                -math.log(2.0)
                * (last_time - sample_time)
                / _ETA_SAMPLE_HALF_LIFE_SECONDS
            )
            for sample_time, _progress in self._samples
        ]
        weight_sum = sum(weights)
        mean_time = sum(
            weight * sample_time
            for weight, (sample_time, _progress) in zip(
                weights, self._samples, strict=True
            )
        ) / weight_sum
        mean_progress = sum(
            weight * progress
            for weight, (_sample_time, progress) in zip(
                weights, self._samples, strict=True
            )
        ) / weight_sum
        covariance = sum(
            weight * (sample_time - mean_time) * (progress - mean_progress)
            for weight, (sample_time, progress) in zip(
                weights, self._samples, strict=True
            )
        )
        variance = sum(
            weight * (sample_time - mean_time) ** 2
            for weight, (sample_time, _progress) in zip(
                weights, self._samples, strict=True
            )
        )
        if variance <= 0.0:
            return None
        rate = covariance / variance
        return rate if rate > 0.0 and math.isfinite(rate) else None

    def observe(self, progress: float, now: float | None = None) -> float | None:
        now = self._clock() if now is None else float(now)
        progress = float(progress)
        if not math.isfinite(progress):
            return self.estimate(now)
        progress = max(0.0, min(1.0, progress))
        if progress >= 1.0:
            self._last_time = now
            self._last_progress = progress
            self._deadline = now
            return 0.0

        interval = now - self._last_time
        advanced = progress - self._last_progress
        if interval <= 0.0 or advanced < 0.0:
            return self.estimate(now)

        if advanced > 0.0:
            self._samples.append((now, progress))
            cutoff = now - _ETA_SAMPLE_WINDOW_SECONDS
            self._samples = [
                sample for sample in self._samples if sample[0] >= cutoff
            ]
            recent_rate = self._recent_rate()
            if recent_rate is not None:
                raw_remaining = (1.0 - progress) / recent_rate
                projected_deadline = now + raw_remaining
                if self._deadline is None:
                    self._deadline = projected_deadline
                else:
                    half_life = (
                        _ETA_FASTER_HALF_LIFE_SECONDS
                        if projected_deadline < self._deadline
                        else _ETA_SLOWER_HALF_LIFE_SECONDS
                    )
                    alpha = self._alpha(interval, half_life)
                    self._deadline += alpha * (
                        projected_deadline - self._deadline
                    )

                # A stale high forecast is particularly misleading near the
                # end. Keep smoothing, but never let it remain far above the
                # estimate supported by the current rolling rate.
                maximum_remaining = raw_remaining * 1.25 + 2.0
                self._deadline = min(
                    self._deadline,
                    now + maximum_remaining,
                )

        self._last_time = now
        self._last_progress = progress
        return self.estimate(now)

    def estimate(self, now: float | None = None) -> float | None:
        now = self._clock() if now is None else float(now)
        if self._deadline is None:
            return None
        # During a reporting stall, stop the visible countdown after a short
        # grace period instead of promising completion while no work is observed.
        stall = max(0.0, now - self._last_time - _ETA_STALL_GRACE_SECONDS)
        return max(0.0, self._deadline - now + stall)


class EditorExportDialog(Adw.Dialog):
    def __init__(
        self,
        parent,
        project,
        config,
        accepted_callback,
        cancelled_callback=None,
        initial_options: ExportOptions | None = None,
    ):
        super().__init__()
        self.parent_window = parent
        self.project = project
        self.accepted_callback = accepted_callback
        self.cancelled_callback = cancelled_callback
        self._export_active = False
        self._export_cancelling = False
        self._eta_estimator: ExportEtaEstimator | None = None
        self._eta_timer_id: int | None = None
        self._export_progress_value = 0.0
        self._completed_destination: Path | None = None
        capabilities = export_encoder_capability_result()
        self.available_encoders = (
            capabilities.available_encoders
            if capabilities is not None
            else _SAFE_INITIAL_ENCODERS
        )
        self.hardware_encoders: tuple[HardwareEncoderCapability, ...] = (
            capabilities.hardware_encoders if capabilities is not None else ()
        )
        self._capability_probe_complete = capabilities is not None
        self._capability_poll_id: int | None = None
        self._video_codec_ids: tuple[str, ...] = ()
        self._audio_codec_ids: tuple[str, ...] = ()
        self._rate_control_ids = _RATE_CONTROL_IDS
        self._resolution_options = export_resolution_options(project.source)
        self._replace_destination: Path | None = None
        self._updating_codecs = False
        self._updating_hardware_acceleration = False
        self._updating_rate_controls = False
        self._prefer_hardware_acceleration = (
            initial_options is None
            or initial_options.video_encoder != "software"
        )

        configured_folder = Path(
            initial_options.folder
            if initial_options is not None
            else config.get("output_folder", Path(project.source.path).parent)
        ).expanduser()
        self.folder = (
            configured_folder
            if configured_folder.is_dir()
            else Path(project.source.path).parent
        )
        self._preferred_video_codec = (
            initial_options.video_codec
            if initial_options is not None
            else "h264"
        )
        self._preferred_audio_codec = (
            initial_options.audio_codec
            if initial_options is not None
            else {
                "ffmpeg_opus": "opus",
                "ffmpeg_flac": "flac",
                "ffmpeg_pcm_s16le": "pcm_s16le",
            }.get(config.get("audio_encoder", "ffmpeg_aac"), "aac")
        )
        configured_rate_control = (
            initial_options.rate_control
            if initial_options is not None
            else config.get("rate_control", "cqp")
        )
        self._preferred_rate_control = (
            configured_rate_control
            if configured_rate_control in RATE_CONTROL_LABELS
            else "cqp"
        )

        self.set_title(_("Export edited clip"))
        self.set_content_width(560)
        self.set_content_height(_EXPORT_OPTIONS_HEIGHT)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title=_("Export edited clip")))

        self.cancel_button = Gtk.Button(label=_("Cancel"))
        self.cancel_button.connect("clicked", lambda *_args: self.close())
        header.pack_start(self.cancel_button)

        self.export_button = Gtk.Button(label=_("Export"))
        export_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        export_content.set_halign(Gtk.Align.CENTER)
        export_content.append(TintedIcon(EXPORT_LINEAR))
        self.export_button_label = Gtk.Label(label=_("Export"))
        export_content.append(self.export_button_label)
        self.export_button.set_child(export_content)
        self.export_button.add_css_class("suggested-action")
        self.export_button.connect("clicked", self._accept)
        header.pack_end(self.export_button)
        toolbar.add_top_bar(header)
        self.set_default_widget(self.export_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        content.append(self.banner)

        self.options_page = Adw.PreferencesPage()
        self.options_page.set_vexpand(True)
        content.append(self.options_page)
        toolbar.set_content(content)

        self.progress_revealer = Gtk.Revealer()
        self.progress_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_UP
        )
        self.progress_revealer.set_child(self._build_progress_bar())
        toolbar.add_bottom_bar(self.progress_revealer)
        self.set_child(toolbar)

        destination_group = Adw.PreferencesGroup()
        destination_group.set_title(_("Destination"))
        self.options_page.add(destination_group)

        self.filename = Adw.EntryRow()
        self.filename.set_title(_("Filename"))
        self.filename.set_text(
            initial_options.filename
            if initial_options is not None
            else f"{Path(project.source.path).stem}-edited"
        )
        self.filename.set_activates_default(True)
        self.filename.connect("changed", self._on_destination_changed)
        self.extension_label = Gtk.Label()
        self.extension_label.add_css_class("dim-label")
        self.filename.add_suffix(self.extension_label)
        destination_group.add(self.filename)

        self.folder_row = Adw.ActionRow()
        self.folder_row.set_title(_("Folder"))
        choose_button = Gtk.Button(label=_("Choose…"))
        choose_button.set_valign(Gtk.Align.CENTER)
        choose_button.connect("clicked", self._choose_folder)
        self.folder_row.add_suffix(choose_button)
        self.folder_row.set_activatable_widget(choose_button)
        destination_group.add(self.folder_row)
        self._set_folder_subtitle()

        self.format_row = Adw.ComboRow()
        self.format_row.set_title(_("File format"))
        self.format_row.set_subtitle(_("Container used for the exported file"))
        self.format_row.set_model(
            Gtk.StringList.new([FORMAT_LABELS[format_id] for format_id in _FORMAT_IDS])
        )
        configured_format = (
            initial_options.format
            if initial_options is not None
            else config.get("format", "mkv")
        )
        self.format_row.set_selected(
            _index(_FORMAT_IDS, configured_format if configured_format in FORMATS else "mkv")
        )
        self.format_row.connect("notify::selected", self._on_format_selected)
        destination_group.add(self.format_row)

        encoding_group = Adw.PreferencesGroup()
        encoding_group.set_title(_("Encoding"))
        self.options_page.add(encoding_group)

        self.resolution_row = Adw.ComboRow()
        self.resolution_row.set_title(_("Resolution"))
        self.resolution_row.set_subtitle(_("Preserves the source aspect ratio"))
        native_dimensions = source_display_dimensions(project.source)
        resolution_labels = []
        for width, height in self._resolution_options:
            if width is None or height is None:
                resolution_labels.append(
                    _("%(width)d × %(height)d (native)")
                    % {"width": native_dimensions[0], "height": native_dimensions[1]}
                    if native_dimensions is not None
                    else _("Native")
                )
            else:
                resolution_labels.append(f"{width} × {height}")
        self.resolution_row.set_model(Gtk.StringList.new(resolution_labels))
        remembered_resolution = (
            (initial_options.output_width, initial_options.output_height)
            if initial_options is not None
            else (None, None)
        )
        self.resolution_row.set_selected(
            _index(self._resolution_options, remembered_resolution)
        )
        encoding_group.add(self.resolution_row)

        self.video_codec_row = Adw.ComboRow()
        self.video_codec_row.set_title(_("Video codec"))
        self.video_codec_row.set_subtitle(_("Encoder implementation"))
        self.video_codec_row.connect(
            "notify::selected", self._on_video_codec_selected
        )
        encoding_group.add(self.video_codec_row)

        self.hardware_acceleration_row = Adw.SwitchRow()
        self.hardware_acceleration_row.set_title(_("Hardware acceleration"))
        self.hardware_acceleration_row.set_subtitle(_("libx264 — software encoding"))
        self.hardware_acceleration_row.set_active(False)
        self.hardware_acceleration_row.set_sensitive(False)
        self.hardware_acceleration_row.connect(
            "notify::active", self._on_hardware_acceleration_changed
        )
        encoding_group.add(self.hardware_acceleration_row)

        self.rate_control_row = Adw.ComboRow()
        self.rate_control_row.set_title(_("Rate control"))
        self.rate_control_row.set_subtitle(RATE_CONTROL_SUBTITLE)
        self.rate_control_row.set_model(
            Gtk.StringList.new(
                [RATE_CONTROL_LABELS[rate_control] for rate_control in _RATE_CONTROL_IDS]
            )
        )
        self.rate_control_row.set_selected(
            _index(
                _RATE_CONTROL_IDS,
                self._preferred_rate_control,
            )
        )
        self.rate_control_row.connect("notify::selected", self._on_rate_control_selected)
        encoding_group.add(self.rate_control_row)

        self.quality_row = Adw.ActionRow()
        self.quality_row.set_title(_("Quality"))
        self.quality_row.set_subtitle(QUALITY_SUBTITLE)
        quality_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        quality_box.set_valign(Gtk.Align.CENTER)
        self.quality = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            CQP_HIGH_QUALITY,
            CQP_LOW_QUALITY,
            1,
        )
        configured_quality = _bounded_int(
            initial_options.quality_cqp
            if initial_options is not None
            else config.get("quality_cqp", CQP_DEFAULT),
            CQP_DEFAULT,
            CQP_HIGH_QUALITY,
            CQP_LOW_QUALITY,
        )
        self.quality.set_value(cqp_to_slider_value(configured_quality))
        self.quality.set_draw_value(True)
        self.quality.set_value_pos(Gtk.PositionType.RIGHT)
        self.quality.set_size_request(240, -1)
        self.quality.get_adjustment().set_page_increment(1)
        for value, label in (
            (cqp_to_slider_value(CQP_LOW_QUALITY), _("Low")),
            (cqp_to_slider_value(CQP_MEDIUM_QUALITY), _("Medium")),
            (cqp_to_slider_value(CQP_HIGH_QUALITY), _("High")),
        ):
            self.quality.add_mark(value, Gtk.PositionType.BOTTOM, label)
        self.quality.set_format_value_func(self._format_quality_value)
        quality_box.append(self.quality)
        self.quality_row.add_suffix(quality_box)
        encoding_group.add(self.quality_row)

        configured_bitrate = _bounded_int(
            initial_options.video_bitrate
            if initial_options is not None
            else config.get("video_bitrate", 12_000),
            12_000,
            _BITRATE_MIN,
            _BITRATE_MAX,
        )
        configured_max_bitrate = _bounded_int(
            initial_options.video_max_bitrate
            if initial_options is not None
            else config.get("video_max_bitrate", 20_000),
            20_000,
            _BITRATE_MIN,
            _BITRATE_MAX,
        )
        configured_max_bitrate = max(configured_bitrate, configured_max_bitrate)
        self.bitrate_row, self.bitrate = self._make_bitrate_row(
            VBR_BITRATE_TITLE,
            VBR_BITRATE_SUBTITLE,
            configured_bitrate,
        )
        self.bitrate.connect("value-changed", self._on_bitrate_changed)
        encoding_group.add(self.bitrate_row)

        self.max_bitrate_row, self.max_bitrate = self._make_bitrate_row(
            MAX_BITRATE_TITLE,
            MAX_BITRATE_SUBTITLE,
            configured_max_bitrate,
        )
        encoding_group.add(self.max_bitrate_row)

        self.audio_codec_row = Adw.ComboRow()
        self.audio_codec_row.set_title(_("Audio codec"))
        self.audio_codec_row.set_subtitle(_("Audio compression format"))
        self.audio_codec_row.connect("notify::selected", self._on_audio_codec_selected)
        encoding_group.add(self.audio_codec_row)

        self.audio_layout_row = Adw.ComboRow()
        self.audio_layout_row.set_title(_("Audio tracks"))
        self.audio_layout_row.set_subtitle(_("Preserve tracks or mix them into one"))
        self.audio_layout_row.set_model(Gtk.StringList.new(list(_AUDIO_LAYOUT_LABELS)))
        self.audio_layout_row.set_selected(
            _index(
                _AUDIO_LAYOUT_IDS,
                initial_options.audio_layout
                if initial_options is not None
                else "separate",
            )
        )
        self.audio_layout_row.connect(
            "notify::selected", self._on_audio_layout_selected
        )
        self._set_audio_layout_tooltip()
        self.audio_layout_row.set_sensitive(bool(project.source.audio_tracks))
        if not project.source.audio_tracks:
            self.audio_layout_row.set_subtitle(_("This clip has no audio tracks"))
            self.audio_layout_row.set_tooltip_text(_("This clip has no audio tracks"))
        encoding_group.add(self.audio_layout_row)

        self._refresh_codec_rows()
        self._update_rate_control_rows()
        self.extension_label.set_text(self._extension())
        self.filename.grab_focus()
        self.connect("closed", self._on_closed)
        self.connect("close-attempt", self._on_close_attempt)
        if not self._capability_probe_complete:
            start_export_encoder_capability_probe()
            self._capability_poll_id = GLib.timeout_add(
                _CAPABILITY_POLL_MS, self._poll_encoder_capabilities
            )

    def _build_progress_bar(self):
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        details.set_margin_top(9)
        details.set_margin_bottom(9)
        details.set_margin_start(12)
        details.set_margin_end(12)

        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.progress_destination = Gtk.Label(xalign=0)
        self.progress_destination.set_hexpand(True)
        self.progress_destination.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        summary.append(self.progress_destination)
        self.progress_eta = Gtk.Label(label=_("Estimating time remaining…"), xalign=1)
        self.progress_eta.add_css_class("dim-label")
        self.progress_eta.add_css_class("numeric")
        summary.append(self.progress_eta)
        details.append(summary)

        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.export_progress = Gtk.ProgressBar()
        self.export_progress.set_hexpand(True)
        self.export_progress.set_valign(Gtk.Align.CENTER)
        self.export_progress.set_show_text(False)
        self.export_progress.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Export progress")],
        )
        progress_row.append(self.export_progress)
        self.progress_percentage = Gtk.Label(label="0%", xalign=0)
        self.progress_percentage.set_width_chars(4)
        self.progress_percentage.set_valign(Gtk.Align.CENTER)
        self.progress_percentage.add_css_class("numeric")
        progress_row.append(self.progress_percentage)

        self.progress_show_button = Gtk.Button(label=_("Show in folder"))
        self.progress_show_button.set_visible(False)
        self.progress_show_button.connect("clicked", self._show_export_in_folder)
        progress_row.append(self.progress_show_button)

        self.progress_cancel_button = Gtk.Button(label=_("Cancel"))
        self.progress_cancel_button.connect(
            "clicked", self._on_progress_button_clicked
        )
        progress_row.append(self.progress_cancel_button)
        details.append(progress_row)
        return details

    def _begin_export(self, destination: Path) -> None:
        self._export_active = True
        self._export_cancelling = False
        self._eta_estimator = ExportEtaEstimator()
        self._export_progress_value = 0.0
        self._completed_destination = None
        self.set_can_close(False)
        self.options_page.set_sensitive(False)
        self.cancel_button.set_visible(False)
        self.export_button.set_visible(False)
        self.set_default_widget(None)
        self.banner.set_revealed(False)
        self.progress_destination.set_text(destination.name)
        self.progress_destination.set_tooltip_text(str(destination))
        self.export_progress.set_fraction(0.0)
        self.export_progress.set_tooltip_text(_("0% complete"))
        self.progress_percentage.set_text(_("0%"))
        self.progress_eta.set_visible(True)
        self.progress_eta.set_text(_("Estimating time remaining…"))
        self.progress_show_button.set_visible(False)
        self.progress_cancel_button.set_label(_("Cancel"))
        self.progress_cancel_button.set_sensitive(True)
        self.progress_cancel_button.remove_css_class("suggested-action")
        self.progress_revealer.set_reveal_child(True)
        self._stop_eta_timer()
        self._eta_timer_id = GLib.timeout_add(
            _ETA_UPDATE_MS, self._update_eta_clock
        )

    def update_export_progress(self, progress: float) -> None:
        if not self._export_active:
            return
        progress = max(0.0, min(1.0, float(progress)))
        progress = max(self.export_progress.get_fraction(), progress)
        self._export_progress_value = progress
        percentage = round(progress * 100)
        self.export_progress.set_fraction(progress)
        self.export_progress.set_tooltip_text(
            _("%(percentage)d%% complete") % {"percentage": percentage}
        )
        self.progress_percentage.set_text(_("%(percentage)d%%") % {"percentage": percentage})

        if self._eta_estimator is not None:
            self._eta_estimator.observe(progress)
        self._render_eta()

    def _update_eta_clock(self) -> bool:
        if not self._export_active:
            self._eta_timer_id = None
            return GLib.SOURCE_REMOVE
        self._render_eta()
        return GLib.SOURCE_CONTINUE

    def _render_eta(self) -> None:
        if self._export_cancelling:
            self.progress_eta.set_text("")
            self.progress_eta.set_visible(False)
            return
        self.progress_eta.set_visible(True)
        if self._export_progress_value >= 1.0:
            self.progress_eta.set_text(_("Finishing export…"))
            return
        eta = self._eta_estimator.estimate() if self._eta_estimator else None
        self.progress_eta.set_text(
            format_time_remaining(eta)
            if eta is not None
            else "Estimating time remaining…"
        )

    def _stop_eta_timer(self) -> None:
        if self._eta_timer_id is not None:
            GLib.source_remove(self._eta_timer_id)
            self._eta_timer_id = None

    def complete_export(self, destination: Path) -> None:
        self._export_active = False
        self._export_cancelling = False
        self._eta_estimator = None
        self._completed_destination = Path(destination)
        self._stop_eta_timer()
        self.set_can_close(True)
        self.options_page.set_sensitive(True)
        self.export_button.set_visible(True)
        self._reset_replace_confirmation()
        self.banner.set_revealed(False)
        self.progress_destination.set_text(
            _("Exported %(name)s") % {"name": destination.name}
        )
        self.progress_destination.set_tooltip_text(str(destination))
        self.export_progress.set_fraction(1.0)
        self.export_progress.set_tooltip_text(_("100% complete"))
        self.progress_percentage.set_text(_("100%"))
        self.progress_eta.set_visible(True)
        self.progress_eta.set_text(_("Finished"))
        self.progress_show_button.set_visible(True)
        self.progress_cancel_button.set_label(_("Close"))
        self.progress_cancel_button.set_sensitive(True)
        self.progress_cancel_button.remove_css_class("suggested-action")
        self.set_default_widget(self.export_button)
        self.export_button.grab_focus()

    def fail_export(self, message: str) -> None:
        self._restore_options_after_export()
        self._show_message(message)

    def export_cancelled(self) -> None:
        self._restore_options_after_export()
        self._show_message(_("Export cancelled"))

    def show_close_warning(self) -> None:
        if not self._export_active:
            return
        self._show_message(_("Cancel the export before closing the editor"))
        self.progress_cancel_button.grab_focus()

    def _restore_options_after_export(self) -> None:
        self._export_active = False
        self._export_cancelling = False
        self._eta_estimator = None
        self._completed_destination = None
        self._stop_eta_timer()
        self.set_can_close(True)
        self.options_page.set_sensitive(True)
        self.cancel_button.set_visible(True)
        self.export_button.set_visible(True)
        self._reset_replace_confirmation()
        self.set_default_widget(self.export_button)
        self.progress_show_button.set_visible(False)
        self.progress_cancel_button.set_label(_("Cancel"))
        self.progress_cancel_button.set_sensitive(True)
        self.progress_cancel_button.remove_css_class("suggested-action")
        self.progress_revealer.set_reveal_child(False)

    def _on_progress_button_clicked(self, *_args) -> None:
        if not self._export_active:
            self.close()
            return
        if self._export_cancelling:
            return
        self._export_cancelling = True
        self.banner.set_revealed(False)
        self.progress_eta.set_text("")
        self.progress_eta.set_visible(False)
        self.progress_cancel_button.set_label(_("Cancelling…"))
        self.progress_cancel_button.set_sensitive(False)
        if self.cancelled_callback is not None:
            self.cancelled_callback()
        else:
            self.export_cancelled()

    def _show_export_in_folder(self, *_args) -> None:
        destination = self._completed_destination
        if destination is None:
            return

        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(destination)))
        launcher.open_containing_folder(
            self.parent_window,
            None,
            self._on_show_export_in_folder_finished,
            destination,
        )

    def _on_show_export_in_folder_finished(
        self,
        launcher,
        result,
        destination: Path,
    ) -> None:
        try:
            launcher.open_containing_folder_finish(result)
        except Exception:
            folder_launcher = Gtk.FileLauncher.new(
                Gio.File.new_for_path(str(destination.parent))
            )
            folder_launcher.launch(
                self.parent_window,
                None,
                self._on_open_export_folder_finished,
                None,
            )

    def _on_open_export_folder_finished(self, launcher, result, _data) -> None:
        try:
            launcher.launch_finish(result)
        except Exception:
            self._show_message(_("Could not show exported file"))

    def _on_close_attempt(self, *_args) -> None:
        self.show_close_warning()

    def _make_bitrate_row(self, title, subtitle, value):
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        spin = Gtk.SpinButton.new_with_range(_BITRATE_MIN, _BITRATE_MAX, _BITRATE_STEP)
        spin.set_value(value)
        spin.set_numeric(True)
        spin.set_digits(0)
        spin.set_width_chars(6)
        spin.set_valign(Gtk.Align.CENTER)
        row.add_suffix(spin)
        unit = Gtk.Label(label=_("Kbps"))
        unit.add_css_class("dim-label")
        unit.set_valign(Gtk.Align.CENTER)
        row.add_suffix(unit)
        return row, spin

    def _format_id(self):
        selected = self.format_row.get_selected()
        return _FORMAT_IDS[selected] if 0 <= selected < len(_FORMAT_IDS) else "mkv"

    def _extension(self):
        return FORMATS[self._format_id()]

    def _selected_video_codec(self) -> str | None:
        selected = self.video_codec_row.get_selected()
        if 0 <= selected < len(self._video_codec_ids):
            return self._video_codec_ids[selected]
        return None

    def _selected_hardware_encoder(self):
        if not self.hardware_acceleration_row.get_active():
            return None
        return self._available_hardware_encoder()

    def _available_hardware_encoder(self):
        codec_id = self._selected_video_codec()
        return next(
            (
                capability
                for capability in self.hardware_encoders
                if capability.codec == codec_id
            ),
            None,
        )

    def _selected_audio_codec(self):
        selected = self.audio_codec_row.get_selected()
        if 0 <= selected < len(self._audio_codec_ids):
            return self._audio_codec_ids[selected]
        return None

    def _set_codec_model(self, row, codecs, preferred):
        ids = tuple(codec.id for codec in codecs)
        if codecs:
            row.set_model(Gtk.StringList.new([codec.label for codec in codecs]))
            row.set_selected(_index(ids, preferred))
            row.set_sensitive(True)
        else:
            row.set_model(Gtk.StringList.new([_("No compatible codec available")]))
            row.set_selected(0)
            row.set_sensitive(False)
        return ids

    def _refresh_codec_rows(self):
        format_id = self._format_id()
        previous_video = self._selected_video_codec() or self._preferred_video_codec
        previous_audio = self._selected_audio_codec() or self._preferred_audio_codec
        video_codecs = compatible_video_codecs(format_id, self.available_encoders)
        audio_codecs = compatible_audio_codecs(
            format_id,
            self.available_encoders if self.project.source.audio_tracks else None,
        )
        self._updating_codecs = True
        try:
            self._video_codec_ids = self._set_codec_model(
                self.video_codec_row, video_codecs, previous_video
            )
            self._audio_codec_ids = self._set_codec_model(
                self.audio_codec_row, audio_codecs, previous_audio
            )
        finally:
            self._updating_codecs = False
        self.audio_codec_row.set_sensitive(
            bool(self.project.source.audio_tracks and self._audio_codec_ids)
        )
        self._refresh_hardware_acceleration_row()
        self._refresh_rate_control_row()
        available = bool(self._video_codec_ids) and (
            bool(self._audio_codec_ids) or not self.project.source.audio_tracks
        )
        self.export_button.set_sensitive(available)
        if not available:
            self._show_message(_("No compatible FFmpeg encoder is available"))

    def _refresh_hardware_acceleration_row(self):
        codec_id = self._selected_video_codec()
        codec = VIDEO_CODECS.get(codec_id) if codec_id is not None else None
        capability = self._available_hardware_encoder()
        active = bool(capability and self._prefer_hardware_acceleration)

        self._updating_hardware_acceleration = True
        try:
            self.hardware_acceleration_row.set_active(active)
            self.hardware_acceleration_row.set_sensitive(capability is not None)
        finally:
            self._updating_hardware_acceleration = False

        if active and capability is not None:
            subtitle = f"{capability.encoder} ({capability.label})"
        elif codec is not None:
            if self._capability_probe_complete and capability is None:
                subtitle = _(
                    "%(encoder)s — software encoding; hardware unavailable"
                ) % {"encoder": codec.encoder}
            elif not self._capability_probe_complete:
                subtitle = _("%(encoder)s — software encoding; checking hardware") % {
                    "encoder": codec.encoder
                }
            else:
                subtitle = _("%(encoder)s — software encoding") % {
                    "encoder": codec.encoder
                }
        else:
            subtitle = _("Software encoding")
        self.hardware_acceleration_row.set_subtitle(subtitle)
        self.hardware_acceleration_row.set_tooltip_text(subtitle)

    def _poll_encoder_capabilities(self):
        capabilities = export_encoder_capability_result()
        if capabilities is None:
            return GLib.SOURCE_CONTINUE
        self._capability_poll_id = None
        self._apply_encoder_capabilities(capabilities)
        return GLib.SOURCE_REMOVE

    def _apply_encoder_capabilities(
        self, capabilities: ExportEncoderCapabilities
    ) -> None:
        self.available_encoders = capabilities.available_encoders
        self.hardware_encoders = capabilities.hardware_encoders
        self._capability_probe_complete = True
        self._refresh_codec_rows()

    def _refresh_rate_control_row(self):
        capability = self._selected_hardware_encoder()
        supported = (
            frozenset(_RATE_CONTROL_IDS)
            if capability is None
            else capability.rate_controls
        )
        rate_control_ids = tuple(
            rate_control
            for rate_control in _RATE_CONTROL_IDS
            if rate_control in supported
        )
        if not rate_control_ids:
            rate_control_ids = ("cqp",)
        current = self._rate_control_id()
        preferred = (
            current if current in rate_control_ids else self._preferred_rate_control
        )

        self._updating_rate_controls = True
        try:
            self._rate_control_ids = rate_control_ids
            self.rate_control_row.set_model(
                Gtk.StringList.new(
                    [RATE_CONTROL_LABELS[item] for item in self._rate_control_ids]
                )
            )
            self.rate_control_row.set_selected(
                _index(self._rate_control_ids, preferred)
            )
        finally:
            self._updating_rate_controls = False
        self._update_rate_control_rows()

    def _on_closed(self, *_args):
        self._stop_eta_timer()
        if self._capability_poll_id is not None:
            GLib.source_remove(self._capability_poll_id)
            self._capability_poll_id = None

    def _rate_control_id(self):
        selected = self.rate_control_row.get_selected()
        if 0 <= selected < len(self._rate_control_ids):
            return self._rate_control_ids[selected]
        return "cqp"

    def _update_rate_control_rows(self):
        rate_control = self._rate_control_id()
        self.quality_row.set_visible(rate_control == "cqp")
        self.bitrate_row.set_visible(rate_control in {"cbr", "vbr"})
        self.max_bitrate_row.set_visible(rate_control == "vbr")
        if rate_control == "cbr":
            self.bitrate_row.set_title(CBR_BITRATE_TITLE)
            self.bitrate_row.set_subtitle(CBR_BITRATE_SUBTITLE)
        else:
            self.bitrate_row.set_title(VBR_BITRATE_TITLE)
            self.bitrate_row.set_subtitle(VBR_BITRATE_SUBTITLE)
        self._sync_max_bitrate()

    def _sync_max_bitrate(self):
        minimum = (
            int(self.bitrate.get_value())
            if self._rate_control_id() == "vbr"
            else _BITRATE_MIN
        )
        self.max_bitrate.set_range(minimum, _BITRATE_MAX)
        if self.max_bitrate.get_value() < minimum:
            self.max_bitrate.set_value(minimum)

    def _format_quality_value(self, _scale, value, _user_data=None):
        return str(slider_value_to_cqp(value))

    def _on_bitrate_changed(self, _spin):
        self._sync_max_bitrate()
        self._clear_message()

    def _on_rate_control_selected(self, *_args):
        if not self._updating_rate_controls:
            self._preferred_rate_control = self._rate_control_id()
        self._update_rate_control_rows()
        self._clear_message()

    def _on_video_codec_selected(self, *_args):
        if not self._updating_codecs:
            self._preferred_video_codec = self._selected_video_codec() or "h264"
            self._refresh_hardware_acceleration_row()
            self._refresh_rate_control_row()
        self._clear_message()

    def _on_hardware_acceleration_changed(self, *_args):
        if self._updating_hardware_acceleration:
            return
        self._prefer_hardware_acceleration = (
            self.hardware_acceleration_row.get_active()
        )
        self._refresh_hardware_acceleration_row()
        self._refresh_rate_control_row()
        self._clear_message()

    def _on_audio_codec_selected(self, *_args):
        if not self._updating_codecs:
            self._preferred_audio_codec = self._selected_audio_codec() or "aac"
        self._clear_message()

    def _set_audio_layout_tooltip(self):
        selected = self.audio_layout_row.get_selected()
        tooltip = (
            _AUDIO_LAYOUT_LABELS[selected]
            if 0 <= selected < len(_AUDIO_LAYOUT_LABELS)
            else None
        )
        self.audio_layout_row.set_tooltip_text(tooltip)

    def _on_audio_layout_selected(self, *_args):
        self._set_audio_layout_tooltip()

    def _on_format_selected(self, *_args):
        self.extension_label.set_text(self._extension())
        self._reset_replace_confirmation()
        self._refresh_codec_rows()

    def _on_destination_changed(self, *_args):
        self._reset_replace_confirmation()

    def _set_folder_subtitle(self):
        path = str(self.folder)
        self.folder_row.set_subtitle(
            middle_truncate_text(path, _FOLDER_LABEL_MAX_CHARS)
        )
        self.folder_row.set_tooltip_text(path)

    def _choose_folder(self, *_args):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Choose export folder"))
        dialog.set_modal(True)
        if self.folder.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.folder)))
        dialog.select_folder(self.parent_window, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        path = folder.get_path()
        if not path:
            self._show_message(_("Choose a local export folder"))
            return
        self.folder = Path(path)
        self._set_folder_subtitle()
        self._reset_replace_confirmation()

    def _options(self):
        video_codec = self._selected_video_codec()
        audio_codec = self._selected_audio_codec()
        if video_codec is None or audio_codec is None:
            raise ExportValidationError(_("No compatible codec is available"))
        audio_selected = self.audio_layout_row.get_selected()
        audio_layout = (
            _AUDIO_LAYOUT_IDS[audio_selected]
            if 0 <= audio_selected < len(_AUDIO_LAYOUT_IDS)
            else "separate"
        )
        hardware_encoder = self._selected_hardware_encoder()
        resolution_index = self.resolution_row.get_selected()
        output_width, output_height = (
            self._resolution_options[resolution_index]
            if 0 <= resolution_index < len(self._resolution_options)
            else (None, None)
        )
        return ExportOptions(
            folder=self.folder,
            filename=self.filename.get_text(),
            format=self._format_id(),
            quality_cqp=slider_value_to_cqp(round(self.quality.get_value())),
            audio_layout=audio_layout,
            video_codec=video_codec,
            audio_codec=audio_codec,
            rate_control=self._rate_control_id(),
            video_bitrate=round(self.bitrate.get_value()),
            video_max_bitrate=round(self.max_bitrate.get_value()),
            video_encoder=(
                hardware_encoder.backend
                if hardware_encoder is not None
                else "software"
            ),
            hardware_device=(
                hardware_encoder.device if hardware_encoder is not None else None
            ),
            output_width=output_width,
            output_height=output_height,
        )

    def snapshot_options(self) -> ExportOptions | None:
        """Return the current controls as an in-memory editor-session snapshot."""
        try:
            return self._options()
        except ExportValidationError:
            return None

    def _show_message(self, message):
        self.banner.set_title(str(message))
        self.banner.set_revealed(True)

    def _clear_message(self):
        if self._replace_destination is None:
            self.banner.set_revealed(False)

    def _reset_replace_confirmation(self):
        self._replace_destination = None
        self._set_export_button_label("Export")
        self.export_button.remove_css_class("destructive-action")
        self.export_button.add_css_class("suggested-action")
        self.banner.set_revealed(False)

    def _accept(self, *_args):
        try:
            options = self._options()
            validate_export_options(options)
            destination = destination_path(options, self.project.source.path)
            if destination.is_dir():
                raise ExportValidationError(_("A folder already uses this filename"))
        except ExportValidationError as error:
            self._show_message(error)
            return

        if destination.exists() and self._replace_destination != destination:
            self._replace_destination = destination
            self._show_message(
                _("%(name)s already exists. Choose Replace to overwrite it")
                % {"name": destination.name}
            )
            self._set_export_button_label(_("Replace"))
            self.export_button.remove_css_class("suggested-action")
            self.export_button.add_css_class("destructive-action")
            return

        try:
            self._begin_export(destination)
            self.accepted_callback(options)
        except Exception as error:
            self._restore_options_after_export()
            self._show_message(error)
            return

    def _set_export_button_label(self, label: str) -> None:
        self.export_button.set_label(label)
        self.export_button_label.set_text(label)
