"""Compact preferences dialog for the main Clipper window."""

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk


class PreferencesDialog(Adw.Dialog):
    """Host Clipper's existing settings controls in a compact dialog."""

    def __init__(self, settings_view):
        super().__init__()
        self.settings_view = settings_view

        self.set_title(_("Preferences"))
        self.set_content_width(500)
        self.set_content_height(630)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title=_("Preferences")))

        self.close_button = Gtk.Button(label=_("Close"))
        self.close_button.connect("clicked", lambda *_args: self.close())
        header.pack_start(self.close_button)
        toolbar.add_top_bar(header)

        toolbar.set_content(self.settings_view)
        self.set_child(toolbar)
