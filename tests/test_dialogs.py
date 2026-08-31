import importlib.util
import sys
import types
from pathlib import Path

from capture_modes import CAPTURE_MODE_DISPLAY, CAPTURE_MODE_GAME


class WidgetStub:
    def __init__(self, natural_height=0, **properties):
        self.properties = properties
        self.children = []
        self.css_classes = []
        self.editable = True
        self.icon_name = properties.get("icon_name", "")
        self.text = ""
        self.tooltip = ""
        self.visible = True
        self.natural_height = natural_height

    @classmethod
    def new_from_icon_name(cls, icon_name):
        return cls(icon_name=icon_name)

    def append(self, child):
        self.children.append(child)

    def add_css_class(self, css_class):
        self.css_classes.append(css_class)

    def remove_css_class(self, css_class):
        if css_class in self.css_classes:
            self.css_classes.remove(css_class)

    def set_editable(self, editable):
        self.editable = editable

    def set_text(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_tooltip_text(self, tooltip):
        self.tooltip = tooltip

    def set_icon_name(self, icon_name):
        self.icon_name = icon_name

    def set_visible(self, visible):
        self.visible = visible

    def measure(self, orientation, for_size):
        return 0, self.natural_height, -1, -1

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda *_args, **_kwargs: None
        raise AttributeError(name)


class DropdownStub:
    def __init__(self, selected):
        self._clipper_option_ids = [CAPTURE_MODE_DISPLAY, CAPTURE_MODE_GAME]
        self.selected = selected

    def get_selected(self):
        return self.selected


def _load_dialogs_module():
    module_name = "test_dialogs_isolated"
    module_path = Path(__file__).resolve().parents[1] / "ui" / "dialogs.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    stubbed_modules = (
        "gi",
        "gi.repository",
        "steam",
        "focus_helpers",
        "game_icons",
        "monitor_manager",
        "process_watcher",
        "text_helpers",
    )
    original_modules = {name: sys.modules.get(name) for name in stubbed_modules}

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")
    repository.Adw = types.SimpleNamespace()
    repository.Gdk = types.SimpleNamespace()
    repository.GLib = types.SimpleNamespace()
    repository.GObject = types.SimpleNamespace(SIGNAL_RUN_FIRST=0)
    repository.Gtk = types.SimpleNamespace(
        Align=types.SimpleNamespace(CENTER=0, START=0),
        Box=WidgetStub,
        Button=WidgetStub,
        DropDown=WidgetStub,
        Entry=WidgetStub,
        Image=WidgetStub,
        Label=WidgetStub,
        Orientation=types.SimpleNamespace(HORIZONTAL=0, VERTICAL=1),
        Window=type("Window", (), {}),
    )
    repository.Pango = types.SimpleNamespace()
    gi.repository = repository

    steam = types.ModuleType("steam")
    steam.CLIPPER_WRAPPER = "obs-gamecapture %command%"

    focus_helpers = types.ModuleType("focus_helpers")
    focus_helpers.dropdown_active_id = lambda dropdown, default=None: (
        dropdown._clipper_option_ids[dropdown.get_selected()]
        if 0 <= dropdown.get_selected() < len(dropdown._clipper_option_ids)
        else default
    )
    focus_helpers.new_id_dropdown = lambda *_args, **_kwargs: None

    game_icons = types.ModuleType("game_icons")
    game_icons.cached_icon_path_for_game = lambda _game_data: ""
    game_icons.submit_icon_resolution = lambda _game_data: None

    monitor_manager = types.ModuleType("monitor_manager")
    monitor_manager.HostMonitorManager = type("HostMonitorManager", (), {})

    process_watcher = types.ModuleType("process_watcher")
    process_watcher.process_choices = lambda _processes: []
    process_watcher.running_process_choices = lambda: []

    text_helpers = types.ModuleType("text_helpers")
    text_helpers.configure_single_line_ellipsis = lambda *_args, **_kwargs: None

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "steam": steam,
            "focus_helpers": focus_helpers,
            "game_icons": game_icons,
            "monitor_manager": monitor_manager,
            "process_watcher": process_watcher,
            "text_helpers": text_helpers,
        }
    )

    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


dialogs = _load_dialogs_module()


def test_game_capture_launch_option_field_is_read_only_and_uses_required_flag():
    launch_option_box, launch_option_entry, copy_button = (
        dialogs._new_game_capture_launch_option()
    )

    assert launch_option_entry.text == "obs-gamecapture %command%"
    assert launch_option_entry.editable is False
    launch_option_row = launch_option_box.children[-1]
    assert launch_option_row.children == [launch_option_entry, copy_button]
    assert copy_button.properties["icon_name"] == "copy-symbolic"


def test_capture_method_tooltip_is_contextual_for_non_steam_games():
    _steam_box, steam_dropdown = dialogs._new_capture_method_row()
    non_steam_box, non_steam_dropdown = dialogs._new_capture_method_row(
        dialogs.NON_STEAM_CAPTURE_METHOD_HELP_TEXT
    )

    steam_info_icon = _steam_box.children[1]
    non_steam_info_icon = non_steam_box.children[1]
    assert steam_info_icon.tooltip == dialogs.CAPTURE_METHOD_HELP_TEXT
    assert non_steam_info_icon.tooltip == dialogs.NON_STEAM_CAPTURE_METHOD_HELP_TEXT
    assert non_steam_info_icon.tooltip != steam_info_icon.tooltip
    assert "non-Steam games" in non_steam_info_icon.tooltip
    assert "Game capture" in non_steam_info_icon.tooltip
    assert "launch" in non_steam_info_icon.tooltip
    assert "Clipper" in non_steam_info_icon.tooltip
    assert steam_dropdown is None
    assert non_steam_dropdown is None


def test_copy_launch_option_button_writes_the_field_text_to_the_clipboard():
    copied = []
    timeout_callbacks = []
    clipboard = types.SimpleNamespace(set=lambda text: copied.append(text))
    display = types.SimpleNamespace(get_clipboard=lambda: clipboard)
    dialogs.GLib.SOURCE_REMOVE = False
    dialogs.GLib.timeout_add = lambda delay, callback: (
        timeout_callbacks.append((delay, callback)) or len(timeout_callbacks)
    )
    dialogs.GLib.source_remove = lambda _source_id: None
    dialog = object.__new__(dialogs.ProcessPickerDialog)
    dialog.get_display = lambda: display
    dialog.game_capture_launch_option_entry = WidgetStub()
    dialog.game_capture_launch_option_entry.set_text("obs-gamecapture %command%")
    dialog.game_capture_launch_option_copy_button = WidgetStub.new_from_icon_name(
        "copy-symbolic"
    )
    dialog._copy_feedback_timeout_id = None

    dialog._on_copy_game_capture_launch_option(None)

    assert copied == ["obs-gamecapture %command%"]
    assert dialog.game_capture_launch_option_copy_button.icon_name == (
        "checkmark-symbolic"
    )
    assert dialog.game_capture_launch_option_copy_button.tooltip == "Copied"
    assert dialog.game_capture_launch_option_copy_button.css_classes == [
        "suggested-action"
    ]
    assert timeout_callbacks[0][0] == 1500

    assert timeout_callbacks[0][1]() is False
    assert dialog.game_capture_launch_option_copy_button.icon_name == "copy-symbolic"
    assert dialog.game_capture_launch_option_copy_button.tooltip == "Copy launch option"
    assert dialog.game_capture_launch_option_copy_button.css_classes == []


def test_non_steam_launch_option_is_only_visible_for_game_capture():
    dialog = object.__new__(dialogs.ProcessPickerDialog)
    dialog.game_capture_launch_option_box = WidgetStub(natural_height=96)
    dialog.capture_mode_dropdown = DropdownStub(selected=0)
    requested_sizes = []
    dialog.set_default_size = lambda width, height: requested_sizes.append(
        (width, height)
    )

    dialog._update_game_capture_launch_option_visibility()
    assert dialog.game_capture_launch_option_box.visible is False
    assert requested_sizes[-1] == (400, 500)

    dialog.capture_mode_dropdown.selected = 1
    dialog._on_capture_mode_changed(dialog.capture_mode_dropdown, None)
    assert dialog.game_capture_launch_option_box.visible is True
    assert requested_sizes[-1] == (400, 596)
