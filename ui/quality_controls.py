"""Shared recording/export quality controls and bitrate constraints."""

from i18n import _

CQP_HIGH_QUALITY = 20
CQP_MEDIUM_QUALITY = 25
CQP_LOW_QUALITY = 30
CQP_DEFAULT = 23

RATE_CONTROL_OPTIONS = (
    ("CQP", "cqp"),
    (_("Constant bitrate"), "cbr"),
    (_("Variable bitrate"), "vbr"),
)
RATE_CONTROL_LABELS = {value: label for label, value in RATE_CONTROL_OPTIONS}

RATE_CONTROL_SUBTITLE = _("Encoder bitrate and quality mode")
QUALITY_SUBTITLE = _("Lower number = better quality, larger file size")
CBR_BITRATE_TITLE = _("Constant bitrate")
CBR_BITRATE_SUBTITLE = _("Fixed video bitrate used for the entire recording")
VBR_BITRATE_TITLE = _("Target bitrate")
VBR_BITRATE_SUBTITLE = _("Target video bitrate the encoder will aim for")
MAX_BITRATE_TITLE = _("Max bitrate")
MAX_BITRATE_SUBTITLE = _("Maximum video bitrate cap")


def cqp_to_slider_value(cqp: int) -> int:
    return CQP_HIGH_QUALITY + CQP_LOW_QUALITY - int(cqp)


def slider_value_to_cqp(value: int) -> int:
    return CQP_HIGH_QUALITY + CQP_LOW_QUALITY - int(value)


BITRATE_MIN = 1000
BITRATE_MAX = 50000


def format_quality_value(_scale, value, _user_data=None):
    return str(slider_value_to_cqp(value))


def create_quality_row(cqp=CQP_DEFAULT, *, width=200):
    from gi.repository import Adw, Gtk

    row = Adw.ActionRow(title=_("Quality"), subtitle=QUALITY_SUBTITLE)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_valign(Gtk.Align.CENTER)
    scale = Gtk.Scale.new_with_range(
        Gtk.Orientation.HORIZONTAL, CQP_HIGH_QUALITY, CQP_LOW_QUALITY, 1
    )
    scale.set_value(cqp_to_slider_value(cqp))
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.RIGHT)
    scale.set_size_request(width, -1)
    scale.get_adjustment().set_page_increment(1)
    for value, label in (
        (CQP_LOW_QUALITY, _("Low")),
        (CQP_MEDIUM_QUALITY, _("Medium")),
        (CQP_HIGH_QUALITY, _("High")),
    ):
        scale.add_mark(cqp_to_slider_value(value), Gtk.PositionType.BOTTOM, label)
    scale.set_format_value_func(format_quality_value)
    box.append(scale)
    row.add_suffix(box)
    return row, scale


def create_bitrate_row(title, subtitle, value, *, changed=None):
    from recording_controls import create_integer_row

    row, spin = create_integer_row(
        title, subtitle, value, BITRATE_MIN, BITRATE_MAX, 500, 2000, _("Kbps"), changed
    )
    spin.set_width_chars(6)
    return row, spin


def update_rate_control_rows(rate_control, quality_row, bitrate_row, max_bitrate_row):
    quality_row.set_visible(rate_control == "cqp")
    bitrate_row.set_visible(rate_control in ("cbr", "vbr"))
    max_bitrate_row.set_visible(rate_control == "vbr")
    bitrate_row.set_title(CBR_BITRATE_TITLE if rate_control == "cbr" else VBR_BITRATE_TITLE)
    bitrate_row.set_subtitle(
        CBR_BITRATE_SUBTITLE if rate_control == "cbr" else VBR_BITRATE_SUBTITLE
    )


def sync_max_bitrate(rate_control, bitrate, max_bitrate):
    """Apply the mode's lower bound; return whether the value was clamped.

    Callers own signal suppression and persistence because export options are
    temporary while recording controls save settings immediately.
    """
    minimum = int(bitrate.get_value()) if rate_control == "vbr" else BITRATE_MIN
    previous = int(max_bitrate.get_value())
    max_bitrate.set_range(minimum, BITRATE_MAX)
    if max_bitrate.get_value() < minimum:
        max_bitrate.set_value(minimum)
    return previous < minimum
