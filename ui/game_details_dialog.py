"""Adaptive game properties dialog using Clipper's standard Adwaita controls."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from capture_modes import capture_mode_for_entry, capture_mode_label
from game_details import steam_details
from gi.repository import Adw, GLib, Gtk
from icon_names import FOLDER_OPEN, GAME, PROCESS, STEAM

_DETAILS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="game-details")


class GameDetailsDialog(Adw.Dialog):
    def __init__(self, entry, name, artwork, save_executable):
        super().__init__()
        self._entry = dict(entry)
        self._save_executable = save_executable
        self._closed = False
        self.connect("closed", self._on_closed)
        self.set_title(_("Game details"))
        self.set_content_width(460)
        self._style_manager = Adw.StyleManager.get_default()
        self._style_handler = self._style_manager.connect("notify::dark", self._sync_theme)
        self._sync_theme()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        close = Gtk.Button(label=_("Close") if "appid" in entry else _("Cancel"))
        close.connect("clicked", lambda *_args: self.close())
        header.pack_start(close)
        self.set_focus(close)
        toolbar.add_top_bar(header)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(540)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(24)
        scrolled.set_child(content)
        toolbar.set_content(scrolled)
        self.set_child(toolbar)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        artwork.set_halign(Gtk.Align.CENTER)
        hero.append(artwork)
        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        identity.set_hexpand(True)
        identity.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=name, wrap=True, xalign=0)
        title.add_css_class("title-2")
        identity.append(title)
        self._source = self._metadata(
            identity,
            STEAM if "appid" in entry else PROCESS,
            _("Steam") if "appid" in entry else _("Manual"),
        )
        self._metadata(identity, GAME, capture_mode_label(capture_mode_for_entry(entry)))
        hero.append(identity)
        content.append(hero)

        if "appid" in entry:
            info = Adw.PreferencesGroup()
            content.append(info)
            self._account = self._info_row(
                info, _("Steam account"), str(entry.get("steam_account_id") or _("Not available"))
            )
            self._info_row(
                info, _("Executable"), str(entry.get("executable_path") or _("Not available"))
            )
            future = _DETAILS_EXECUTOR.submit(steam_details, dict(entry))
            future.add_done_callback(
                lambda completed: GLib.idle_add(self._apply_steam_details, completed)
            )
        else:
            executable_group = Adw.PreferencesGroup()
            content.append(executable_group)
            if entry.get("flatpak_id"):
                self._info_row(executable_group, _("Flatpak application"), entry["flatpak_id"])
            self.executable = Adw.EntryRow(title=_("Executable path"))
            self.executable.set_text(str(entry.get("executable_path") or entry.get("path") or ""))
            browse = Gtk.Button(icon_name=FOLDER_OPEN, valign=Gtk.Align.CENTER)
            browse.add_css_class("flat")
            browse.set_tooltip_text(_("Choose game executable"))
            browse.connect("clicked", self._browse)
            self.executable.add_suffix(browse)
            executable_group.add(self.executable)
            value = str(entry.get("executable_path") or entry.get("path") or "")
            self._info_row(
                executable_group,
                _("Executable name"),
                str(entry.get("executable_name") or Path(value).name or _("Not available")),
            )
            self.error = Gtk.Label(wrap=True, xalign=0)
            self.error.add_css_class("error")
            self.error.set_visible(False)
            content.append(self.error)
            self.save = Gtk.Button(label=_("Save"), halign=Gtk.Align.END)
            self.save.add_css_class("suggested-action")
            self.save.set_sensitive(False)
            self.save.connect("clicked", self._save)
            header.pack_end(self.save)
            self.set_default_widget(self.save)
            self.executable.connect("changed", self._changed)

    @staticmethod
    def _metadata(parent, icon_name, text):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.add_css_class("dim-label")
        box.append(Gtk.Image(icon_name=icon_name, pixel_size=16))
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.add_css_class("caption")
        box.append(label)
        parent.append(box)
        return label

    @staticmethod
    def _info_row(group, title, value):
        row = Adw.ActionRow(title=title, subtitle=value)
        row.set_use_markup(False)
        row.set_subtitle_selectable(True)
        group.add(row)
        return row

    def _on_closed(self, *_args):
        self._closed = True
        self._style_manager.disconnect(self._style_handler)

    def _sync_theme(self, *_args):
        if self._style_manager.get_dark():
            self.add_css_class("clipper-dark")
        else:
            self.remove_css_class("clipper-dark")

    def _apply_steam_details(self, future):
        if self._closed:
            return False
        try:
            source, account, _root = future.result()
        except Exception:  # noqa: BLE001
            source, account, _root = (
                _("Not available"),
                str(self._entry.get("steam_account_id") or _("Not available")),
                _("Not available"),
            )
        self._source.set_text(source)
        self._account.set_subtitle(account)
        return False

    def _changed(self, *_args):
        value = self.executable.get_text().strip()
        previous = str(self._entry.get("executable_path") or self._entry.get("path") or "")
        self.save.set_sensitive(bool(value) and value != previous)
        self.error.set_visible(False)

    def _browse(self, *_args):
        chooser = Gtk.FileDialog(title=_("Choose game executable"), accept_label=_("Choose"))
        chooser.open(self.get_root(), None, self._chosen)

    def _chosen(self, chooser, result):
        try:
            selected = chooser.open_finish(result)
        except GLib.Error:
            return
        if not self._closed and selected is not None and selected.get_path():
            self.executable.set_text(selected.get_path())

    def _save(self, *_args):
        try:
            self._save_executable(self._entry, self.executable.get_text())
        except ValueError as error:
            self.error.set_text(str(error))
            self.error.set_visible(True)
            return
        except OSError:
            self.error.set_text(_("Could not save the executable path. Try again."))
            self.error.set_visible(True)
            return
        self.close()
