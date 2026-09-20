import gi
import language_row

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk
from i18n import Language
from main_window import MainWindow
from settings_view import SettingsView


def test_settings_language_picker_is_catalog_driven(monkeypatch):
    monkeypatch.setattr(
        language_row, "available_languages", lambda: [Language("uk", "Українська")]
    )
    selected = []
    settings_view = SettingsView(
        config={}, language_changed_callback=lambda language: selected.append(language)
    )

    model = settings_view._language_row.get_model()
    assert [model.get_string(index) for index in range(model.get_n_items())] == [
        "System language",
        "Українська",
    ]

    settings_view._language_row.set_selected(1)

    assert selected == ["uk"]


def test_main_window_tracks_held_shift_keys_for_clip_edit_buttons():
    class StackStub:
        def get_visible_child_name(self):
            return "clips"

    states = []
    window = type(
        "WindowStub",
        (),
        {
            "_clips_shift_keycodes": set(),
            "view_stack": StackStub(),
            "_set_clips_shift_edit_mode": lambda self, active: states.append(active),
        },
    )()

    assert MainWindow._on_clips_key_pressed(
        window, None, Gdk.KEY_Shift_L, 50, 0
    ) is False
    assert MainWindow._on_clips_key_pressed(
        window, None, Gdk.KEY_Shift_R, 62, 0
    ) is False
    MainWindow._on_clips_key_released(window, None, Gdk.KEY_Shift_L, 50, 0)
    MainWindow._on_clips_key_released(window, None, Gdk.KEY_Shift_R, 62, 0)

    assert states == [True, True, True, False]
