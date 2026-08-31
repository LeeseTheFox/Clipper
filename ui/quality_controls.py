"""Shared recording/export quality mapping."""

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
