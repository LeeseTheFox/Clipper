from types import SimpleNamespace

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

import editor_window
import video_player_window
from editor_window import EDITOR_SHORTCUT_SECTIONS, EditorWindow
from gi.repository import Gtk
from video_player_window import PLAYER_SHORTCUT_SECTIONS, VideoPlayerWindow


def _accelerators(sections):
    return {
        shortcut.accelerator
        for section in sections
        for shortcut in section.shortcuts
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


def test_player_shortcuts_action_presents_the_player_list(monkeypatch):
    presented = []
    dialog = SimpleNamespace(present=presented.append)
    monkeypatch.setattr(
        video_player_window,
        "create_shortcuts_dialog",
        lambda sections: presented.append(sections) or dialog,
    )
    window = VideoPlayerWindow()

    assert window.activate_action("win.shortcuts", None) is True
    assert presented == [PLAYER_SHORTCUT_SECTIONS, window]
    assert window._shortcuts_dialog is dialog
    window.destroy()
