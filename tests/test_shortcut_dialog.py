from types import SimpleNamespace

import gi
import pytest

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

import editor_window
import video_player
from editor_window import EDITOR_SHORTCUT_SECTIONS, EditorWindow
from gi.repository import Gdk, Gtk
from shortcut_dialog import matches_shortcuts_key
from video_player import PLAYER_SHORTCUT_SECTIONS, VideoPlayer


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
        video_player,
        "create_shortcuts_dialog",
        lambda sections: presented.append(sections) or dialog,
    )
    window = VideoPlayer()

    assert window.activate_action("win.shortcuts", None) is True
    assert presented == [PLAYER_SHORTCUT_SECTIONS, window]
    assert window._shortcuts_dialog is dialog
    window.cleanup()


@pytest.mark.parametrize("window_type", [EditorWindow, VideoPlayer])
def test_shortcuts_chord_opens_the_window_list_under_non_latin_layout(
    monkeypatch, window_type
):
    # The translated symbol must not prevent access to shortcut help, even
    # before the player has loaded media.
    monkeypatch.setattr(
        Gdk.Display,
        "get_default",
        lambda: SimpleNamespace(
            map_keycode=lambda code: (True, [], [Gdk.KEY_slash])
        ),
    )
    presented = []
    dialog = SimpleNamespace(present=presented.append)
    module = editor_window if window_type is EditorWindow else video_player
    sections = (
        EDITOR_SHORTCUT_SECTIONS
        if window_type is EditorWindow
        else PLAYER_SHORTCUT_SECTIONS
    )
    monkeypatch.setattr(
        module, "create_shortcuts_dialog",
        lambda items: presented.append(items) or dialog,
    )
    if window_type is EditorWindow:
        window = SimpleNamespace()
        window.activate_action = lambda action, parameter: (
            window_type._show_shortcuts(window) or True
        )
    else:
        window = VideoPlayer()
    try:
        assert window_type._on_key_pressed(
            window, None, Gdk.KEY_Cyrillic_yu, 61,
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
        )
        assert presented == [sections, window]
    finally:
        if window_type is VideoPlayer:
            window.cleanup()


def test_shortcuts_chord_requires_control_shift_and_the_physical_slash(monkeypatch):
    monkeypatch.setattr(
        Gdk.Display, "get_default",
        lambda: SimpleNamespace(
            map_keycode=lambda code: (True, [], [
                Gdk.KEY_slash if code == 61 else Gdk.KEY_a
            ])
        ),
    )
    ctrl = Gdk.ModifierType.CONTROL_MASK
    shift = Gdk.ModifierType.SHIFT_MASK
    for modifiers in (0, ctrl, shift, ctrl | shift | Gdk.ModifierType.ALT_MASK):
        assert not matches_shortcuts_key(Gdk.KEY_question, 61, modifiers)
    assert not matches_shortcuts_key(Gdk.KEY_question, 38, ctrl | shift)
    assert matches_shortcuts_key(
        Gdk.KEY_question, 61, ctrl | shift | Gdk.ModifierType.LOCK_MASK
    )
