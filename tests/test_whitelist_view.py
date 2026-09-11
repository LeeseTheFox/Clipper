import importlib.util
import sys
import types
from pathlib import Path


def _load_whitelist_module():
    module_name = "test_whitelist_view_isolated"
    module_path = Path(__file__).resolve().parents[1] / "ui" / "whitelist_view.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "gi",
            "gi.repository",
            "dialogs",
            "game_icons",
            "steam",
        )
    }

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")
    repository.Adw = types.SimpleNamespace()
    repository.Gdk = types.SimpleNamespace(Texture=type("Texture", (), {}))
    repository.Gio = types.SimpleNamespace()
    repository.GLib = types.SimpleNamespace(idle_add=lambda *_args, **_kwargs: None)

    class Image:
        def __init__(self, icon_name):
            self.icon_name = icon_name
            self.pixel_size = None
            self.css_classes = []

        @classmethod
        def new_from_icon_name(cls, icon_name):
            return cls(icon_name)

        def set_pixel_size(self, pixel_size):
            self.pixel_size = pixel_size

        def add_css_class(self, css_class):
            self.css_classes.append(css_class)

    repository.Gtk = types.SimpleNamespace(
        Box=type("Box", (), {}),
        Image=Image,
        Widget=type("Widget", (), {}),
    )
    gi.repository = repository

    dialogs = types.ModuleType("dialogs")
    dialogs.ProcessPickerDialog = type("ProcessPickerDialog", (), {})
    dialogs.SteamGamePickerDialog = type("SteamGamePickerDialog", (), {})
    dialogs.show_steam_restart_dialog = lambda *_args, **_kwargs: None
    dialogs.show_warning_dialog = lambda *_args, **_kwargs: None

    game_icons = types.ModuleType("game_icons")
    game_icons.cached_icon_path_for_game = lambda _game_data: ""
    game_icons.submit_icon_resolution = lambda _game_data: None

    steam = types.ModuleType("steam")
    steam.remove_capture_wrapper = lambda _appid: False

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "dialogs": dialogs,
            "game_icons": game_icons,
            "steam": steam,
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


whitelist_module = _load_whitelist_module()
WhitelistView = whitelist_module.WhitelistView
_entry_detail = whitelist_module._entry_detail
_entry_matches_search = whitelist_module._entry_matches_search
_placeholder_color_class = whitelist_module._placeholder_color_class


class ConfigStub:
    def __init__(self, data=None):
        self.saved = []
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.saved.append((key, value))


class GamesListStub:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.removed = []

    def get_first_child(self):
        return self.rows[0] if self.rows else None

    def remove(self, row):
        self.removed.append(row)
        if row in self.rows:
            self.rows.remove(row)


class RowStub:
    def __init__(self, index):
        self.index = index

    def get_index(self):
        return self.index


def _make_view():
    view = object.__new__(WhitelistView)
    view._config = ConfigStub()
    view._whitelist_changed_callback = None
    view._whitelist = []
    view.games_list = GamesListStub()
    view._duplicate_entry_for = lambda _game_data: None
    view._add_entry = lambda game_data: view._whitelist.append(game_data)
    view._update_content_state = lambda: None
    view._show_toast = lambda message: view.toasts.append(message)
    view.toasts = []
    return view


def test_process_selected_uses_executable_basename_when_name_is_empty():
    view = _make_view()

    view.on_process_selected(
        None,
        "",
        "/games/Example/Example Game.exe",
        "display_capture",
    )

    assert view._whitelist[0]["name"] == "Example Game.exe"
    assert view._whitelist[0]["executable_name"] == "Example Game.exe"
    assert view.toasts == ["Added Example Game.exe"]


def test_process_selection_persists_scoped_identity():
    view = _make_view()
    dialog = types.SimpleNamespace(selection_identity={
        "match_mode": "executable", "flatpak_id": "org.jeffvli.feishin",
    })
    view.on_process_selected(dialog, "feishin", "/app/main/feishin", "display_capture")
    entry = view._whitelist[0]
    assert entry["flatpak_id"] == "org.jeffvli.feishin"
    assert entry["match_mode"] == "executable"
    assert entry["executable_path"] == "/app/main/feishin"


def test_remove_game_toast_uses_executable_basename_for_empty_legacy_name():
    view = _make_view()
    row = RowStub(0)
    view._whitelist = [
        {
            "name": "",
            "path": "/games/Example/Example Game.exe",
            "executable_path": "/games/Example/Example Game.exe",
            "executable_name": "Example Game.exe",
        }
    ]

    row._clipper_game_data = view._whitelist[0]
    view.on_remove_game(None, row)

    assert view.games_list.removed == [row]
    assert view._whitelist == []
    assert view.toasts == ["Removed Example Game.exe"]


def test_config_reload_can_suppress_change_callback_during_factory_reset():
    view = _make_view()
    old_row = object()
    callbacks = []
    view.games_list = GamesListStub([old_row])
    view._config = ConfigStub({"whitelist": [{"name": "Factory game"}]})
    view._whitelist = [{"name": "Stale game"}]
    view._whitelist_changed_callback = lambda: callbacks.append(True)

    view.reload_from_config(notify=False)

    assert view.games_list.removed == [old_row]
    assert view._whitelist == [{"name": "Factory game"}]
    assert callbacks == []


def test_entry_detail_prefers_install_or_executable_path():
    assert _entry_detail(
        {
            "name": "Steam Game",
            "install_path": "/games/Steam Game",
            "path": "/less/useful/path",
        }
    ) == "/games/Steam Game"
    assert _entry_detail(
        {
            "name": "Manual Game",
            "executable_path": "/games/manual/game-bin",
            "path": "game-bin",
        }
    ) == "/games/manual/game-bin"


def test_entry_search_matches_names_and_paths_case_insensitively():
    game = {
        "name": "Counter-Strike 2",
        "install_path": "/mnt/Games/SteamLibrary/common/CS2",
        "executable_name": "cs2",
    }

    assert _entry_matches_search(game, "STRIKE")
    assert _entry_matches_search(game, "steamlibrary")
    assert _entry_matches_search(game, "CS2")
    assert not _entry_matches_search(game, "portal")


def test_placeholder_color_is_stable_for_the_same_game_identity():
    first = {
        "name": "Example Game",
        "executable_path": "/games/Example/game-bin",
    }
    renamed = {
        "name": "Renamed Game",
        "executable_path": "/games/Example/game-bin",
    }

    assert _placeholder_color_class(first) == _placeholder_color_class(dict(first))
    assert _placeholder_color_class(first) == _placeholder_color_class(renamed)
    assert _placeholder_color_class(first).startswith("clipper-placeholder-")
    assert _placeholder_color_class(first) in {
        f"clipper-placeholder-{color}"
        for color in whitelist_module._GAME_PLACEHOLDER_COLORS
    }
    assert _placeholder_color_class(renamed) in {
        f"clipper-placeholder-{color}"
        for color in whitelist_module._GAME_PLACEHOLDER_COLORS
    }


def test_game_source_icons_follow_the_gtk_theme_color():
    view = object.__new__(WhitelistView)

    icon = view._new_action_icon(whitelist_module.STEAM, size=18)

    assert icon.icon_name == "steam-symbolic"
    assert icon.pixel_size == 18
    assert icon.css_classes == ["dim-label"]
