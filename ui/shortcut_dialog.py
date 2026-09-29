"""Shared read-only keyboard shortcut presentation for Clipper windows."""

from __future__ import annotations

from dataclasses import dataclass

import gi
from i18n import _

gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from playback_controls import _matches_base_key

SHORTCUTS_ACCELERATOR = "<Control>question"


def matches_shortcuts_key(keyval: int, keycode: int, state: int) -> bool:
    """Recognize Ctrl+Shift+/ using the physical key across input layouts."""
    modifiers = state & Gtk.accelerator_get_default_mod_mask()
    return (
        modifiers == (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        and _matches_base_key(keyval, keycode, Gdk.KEY_slash, Gdk.KEY_question)
    )


@dataclass(frozen=True)
class Shortcut:
    """One user-facing keyboard shortcut."""

    title: str
    accelerator: str


@dataclass(frozen=True)
class ShortcutSection:
    """A named group of related shortcuts."""

    title: str
    shortcuts: tuple[Shortcut, ...]


def create_shortcuts_menu(
    additional_items: tuple[tuple[str, str], ...] = (),
) -> Gio.Menu:
    """Build a window menu led by the standard shortcuts action."""
    menu = Gio.Menu()
    item = Gio.MenuItem.new(_("Shortcuts"), "win.shortcuts")
    item.set_attribute_value("accel", GLib.Variant("s", SHORTCUTS_ACCELERATOR))
    menu.append_item(item)
    for label, action in additional_items:
        menu.append(label, action)
    return menu


def create_shortcuts_dialog(
    sections: tuple[ShortcutSection, ...],
) -> Adw.ShortcutsDialog:
    """Build a native, searchable, read-only shortcuts dialog."""
    dialog = Adw.ShortcutsDialog(title=_("Keyboard shortcuts"))
    for section_definition in sections:
        section = Adw.ShortcutsSection(title=section_definition.title)
        for shortcut_definition in section_definition.shortcuts:
            shortcut = Adw.ShortcutsItem(
                title=shortcut_definition.title,
                accelerator=shortcut_definition.accelerator,
            )
            section.add(shortcut)
        dialog.add(section)
    return dialog
