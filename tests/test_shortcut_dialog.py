from types import SimpleNamespace

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

import editor_window
import shortcut_dialog
import video_player_window
from editor_window import EDITOR_SHORTCUT_SECTIONS, EditorWindow
from gi.repository import Adw, Gtk
from shortcut_dialog import Shortcut, ShortcutSection, create_shortcuts_dialog
from video_player_window import PLAYER_SHORTCUT_SECTIONS, VideoPlayerWindow


def _accelerators(sections):
    return {
        shortcut.accelerator
        for section in sections
        for shortcut in section.shortcuts
    }


def test_shortcuts_menu_starts_with_the_read_only_shortcuts_view(monkeypatch):
    class MenuStub:
        def __init__(self):
            self.items = []

        def append(self, label, action):
            self.items.append((label, action))

    monkeypatch.setattr(
        shortcut_dialog,
        "Gio",
        SimpleNamespace(Menu=MenuStub),
    )
    menu = shortcut_dialog.create_shortcuts_menu(
        (("Wipe all edits", "win.wipe-all-edits"),)
    )

    assert menu.items == [
        ("Shortcuts", "win.shortcuts"),
        ("Wipe all edits", "win.wipe-all-edits"),
    ]


def test_shortcuts_dialog_builds_native_read_only_sections():
    dialog = create_shortcuts_dialog(
        (
            ShortcutSection(
                "Playback",
                (Shortcut("Play or pause", "space"),),
            ),
        )
    )

    assert isinstance(dialog, Adw.ShortcutsDialog)
    assert dialog.get_title() == "Keyboard shortcuts"
    dialog.close()


def test_every_editor_keyboard_shortcut_is_listed():
    assert _accelerators(EDITOR_SHORTCUT_SECTIONS) == {
        "<Control>a",
        "<Control>minus",
        "<Control>plus",
        "<Control><Shift>z",
        "<Control>z",
        "Delete",
        "Down",
        "Escape",
        "Left",
        "Right",
        "Up",
        "comma",
        "f",
        "m",
        "period",
        "space",
        "x",
    }


def test_every_player_keyboard_shortcut_is_listed():
    assert _accelerators(PLAYER_SHORTCUT_SECTIONS) == {
        "Down",
        "Escape",
        "Left",
        "Right",
        "Up",
        "f",
        "m",
        "space",
    }


def test_all_listed_accelerators_are_valid_gtk_accelerators():
    for accelerator in _accelerators(
        EDITOR_SHORTCUT_SECTIONS + PLAYER_SHORTCUT_SECTIONS
    ):
        parsed, _key, _modifiers = Gtk.accelerator_parse(accelerator)
        assert parsed, accelerator


def test_editor_shortcuts_action_presents_the_editor_list(monkeypatch):
    presented = []
    dialog = SimpleNamespace(present=presented.append)
    monkeypatch.setattr(
        editor_window,
        "create_shortcuts_dialog",
        lambda sections: presented.append(sections) or dialog,
    )
    window = SimpleNamespace()

    EditorWindow._show_shortcuts(window)

    assert presented == [EDITOR_SHORTCUT_SECTIONS, window]
    assert window._shortcuts_dialog is dialog


def test_player_has_a_burger_menu_and_connected_shortcuts_action(monkeypatch):
    presented = []
    dialog = SimpleNamespace(present=presented.append)
    monkeypatch.setattr(
        video_player_window,
        "create_shortcuts_dialog",
        lambda sections: presented.append(sections) or dialog,
    )
    window = VideoPlayerWindow()

    assert window._menu_button.get_icon_name() == video_player_window.HAMBURGER_MENU
    assert window._menu_button.get_tooltip_text() == "Player menu"
    assert window._menu_button.get_menu_model().get_n_items() == 1
    assert window.activate_action("win.shortcuts", None) is True
    assert presented == [PLAYER_SHORTCUT_SECTIONS, window]
    assert window._shortcuts_dialog is dialog
    window.destroy()
