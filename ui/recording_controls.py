"""Builders for recording controls shared by setup and preferences."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from config import (
    REPLAY_BUFFER_SIZE_DEFAULT_MB,
    REPLAY_BUFFER_SIZE_MAX_MB,
    REPLAY_BUFFER_SIZE_MIN_MB,
)
from gi.repository import Adw, Gtk
from i18n import _


def create_integer_row(title, subtitle, value, minimum, maximum, step, page, unit, changed):
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    spin = Gtk.SpinButton(
        adjustment=Gtk.Adjustment.new(float(value), minimum, maximum, step, page, 0),
        digits=0,
    )
    spin.set_valign(Gtk.Align.CENTER)
    spin.set_numeric(True)
    if changed is not None:
        spin.connect("value-changed", changed)
    row.add_suffix(spin)
    if unit:
        label = Gtk.Label(label=unit)
        label.add_css_class("dim-label")
        label.set_valign(Gtk.Align.CENTER)
        row.add_suffix(label)
    row.set_activatable_widget(spin)
    return row, spin


def create_clip_length_row(value, changed, *, subtitle, show_unit=True):
    """Use the engine’s supported replay duration range in both screens."""
    return create_integer_row(
        _("Clip length"), subtitle, value, 5, 300, 5, 30,
        _("seconds") if show_unit else None, changed,
    )


def create_buffer_size_row(value=REPLAY_BUFFER_SIZE_DEFAULT_MB, *, changed):
    return create_integer_row(
        _("File size limit"), _("Maximum size retained for each clip"), value,
        REPLAY_BUFFER_SIZE_MIN_MB, REPLAY_BUFFER_SIZE_MAX_MB, 128, 1024,
        _("MiB"), changed,
    )


def create_combo_row(title, subtitle, labels, selected, changed, *signal_data):
    row = Adw.ComboRow(title=title, subtitle=subtitle)
    row.set_model(Gtk.StringList.new(labels))
    row.set_selected(selected)
    row.connect("notify::selected", changed, *signal_data)
    return row
