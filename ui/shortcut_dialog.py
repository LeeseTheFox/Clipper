"""Shared read-only keyboard shortcut presentation for Clipper windows."""

from __future__ import annotations

from dataclasses import dataclass

import gi
from i18n import _

gi.require_version("Adw", "1")

from gi.repository import Adw, Gio


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
    menu.append(_("Shortcuts"), "win.shortcuts")
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
