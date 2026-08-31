from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import main_window as main_window_module
from gi.repository import Adw, Gdk, Gio, Gtk
from main_window import MainWindow
from preferences_dialog import PreferencesDialog
from settings_view import SettingsView

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_preferences_use_a_compact_native_dialog():
    settings_view = Gtk.Box()
    dialog = PreferencesDialog(settings_view)

    assert isinstance(dialog, Adw.Dialog)
    assert dialog.get_title() == "Preferences"
    assert dialog.get_content_width() == 500
    assert dialog.get_content_height() == 630
    assert dialog.settings_view is settings_view
    assert dialog.close_button.get_label() == "Close"


def test_main_window_moves_settings_out_of_the_view_switcher():
    source = (REPO_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")

    assert 'menu.append(_("Preferences"), "win.preferences")' in source
    assert 'Gio.SimpleAction.new("preferences", None)' in source
    assert "self._preferences_dialog.present(self)" in source
    assert '"settings", "Settings"' not in source
    assert source.count("add_titled_with_icon(") == 3


def test_main_window_has_bazaar_style_responsive_navigation(monkeypatch):
    monkeypatch.setattr(
        main_window_module,
        "ClipsView",
        lambda **_kwargs: Gtk.Box(),
    )
    application = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
    assert application.register(None)
    window = MainWindow(application, config={})

    assert window.get_default_size() == (800, 600)
    assert window.get_size_request() == (512, 500)
    assert window.get_content() is window.toolbar_view
    assert window.header.get_title_widget() is window.header_view_switcher
    assert window.header_view_switcher.get_stack() is window.view_stack
    assert window.bottom_view_switcher.get_stack() is window.view_stack
    assert window.bottom_view_switcher.get_reveal() is True
    assert window.toolbar_view.get_reveal_bottom_bars() is False
    assert window._compact_breakpoint.get_condition().to_string() == "max-width: 600sp"

    window.destroy()


def test_settings_quality_slider_fits_the_compact_preferences_dialog():
    settings_view = SettingsView(config={})

    assert settings_view._quality_scale.get_size_request()[0] == 200
    assert settings_view._remember_window_sizes_switch.get_active() is True


def test_settings_language_picker_is_catalog_driven():
    selected = []
    settings_view = SettingsView(
        config={}, language_changed_callback=lambda language: selected.append(language)
    )

    model = settings_view._language_row.get_model()
    assert [model.get_string(index) for index in range(model.get_n_items())] == [
        "System language",
        "English",
        "Русский",
        "Українська",
    ]

    settings_view._language_row.set_selected(1)

    assert selected == ["en"]


def test_main_menu_uses_the_standard_gtk_menu_icon():
    source = (REPO_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "menu_button.set_icon_name(HAMBURGER_MENU)" in source
    assert main_window_module.HAMBURGER_MENU == "open-menu-symbolic"


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
