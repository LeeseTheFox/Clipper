"""Shared native language selector used by Preferences and first-run setup."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk
from i18n import SYSTEM_LANGUAGE, _, available_languages


def create_language_row(config, changed_callback):
    """Return an Adw.ComboRow whose values are discovered from catalogs."""
    languages = available_languages()
    values = [SYSTEM_LANGUAGE, *(language.code for language in languages)]
    labels = [
        _("System language"),
        *(language.name for language in languages),
    ]

    row = Adw.ComboRow()
    row.set_title(_("Language"))
    row.set_subtitle(_("Language used by Clipper"))
    row.set_model(Gtk.StringList.new(labels))
    selected = str(config.get("ui_language", SYSTEM_LANGUAGE))
    row.set_selected(values.index(selected) if selected in values else 0)

    def on_selected(combo, _param) -> None:
        index = int(combo.get_selected())
        if 0 <= index < len(values):
            changed_callback(values[index])

    row.connect("notify::selected", on_selected)
    row._clipper_language_values = values
    return row
