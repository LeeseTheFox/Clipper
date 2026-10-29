import importlib.util
import sys
import types
from concurrent.futures import Future
from pathlib import Path

import pytest
import steam
from config import ClipperConfig
from game_capture import EnvironmentKind


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
        self.data[key] = value


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


@pytest.mark.parametrize("save_fails", [False, True])
def test_display_batch_publishes_games_only_after_saving_without_a_steam_change(save_fails):
    view = _make_view()
    existing = {"appid": "0", "name": "Existing", "audio_process": "recently-learned"}
    stale = dict(existing, audio_process="old")
    view._config.data["whitelist"] = [existing]
    view._whitelist = [stale]
    closed = []
    dialog = types.SimpleNamespace(
        close=lambda: closed.append(True), refresh_existing_games=lambda _games: None,
    )
    games = [
        {"appid": "1", "name": "First", "capture_mode": "display_capture"},
        {"appid": "2", "name": "Second", "capture_mode": "display_capture"},
    ]
    if save_fails:
        def fail_save(*_args):
            raise OSError("read-only configuration")
        view._config.set = fail_save
    view.on_steam_games_selected(dialog, games)
    assert view._config.saved == ([] if save_fails else [("whitelist", [existing, *games])])
    assert view._whitelist == ([stale] if save_fails else [existing, *games])
    assert closed == ([] if save_fails else [True])


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as error:
            future.set_exception(error)
        return future


def test_batch_restart_cancellation_preserves_selection_and_configuration(monkeypatch):
    view = _make_view()
    view.set_sensitive = lambda _value: None
    view.get_root = lambda: None
    monkeypatch.setattr(whitelist_module, "_STEAM_RESTART_EXECUTOR", ImmediateExecutor())
    monkeypatch.setattr(whitelist_module.GLib, "idle_add", lambda function, *args: function(*args))
    installation = types.SimpleNamespace(
        stable_id="chosen", data_root=Path("/steam/chosen"),
        environment=EnvironmentKind.NATIVE_STEAM,
    )
    monkeypatch.setattr(
        whitelist_module.steam, "installation_for_entry", lambda _entry: installation, raising=False
    )
    monkeypatch.setattr(whitelist_module.steam, "select_steam_account",
                        lambda *_args: types.SimpleNamespace(account_id="42"), raising=False)
    monkeypatch.setattr(whitelist_module.steam, "_is_steam_running", lambda **_kwargs: True,
                        raising=False)
    monkeypatch.setattr(whitelist_module.steam, "CaptureChange",
                        lambda entry, _installation, running, *_args:
                        types.SimpleNamespace(entry=entry, running=running), raising=False)
    monkeypatch.setattr(whitelist_module.game_capture, "prepare", lambda *_args: None)
    confirmations = []
    monkeypatch.setattr(whitelist_module, "show_steam_restart_dialog",
                        lambda _parent, apply, on_cancel:
                        confirmations.append((apply, on_cancel)))
    pending, closed = [], []
    dialog = types.SimpleNamespace(
        set_pending=lambda value: pending.append(value), close=lambda: closed.append(True),
    )
    games = [{"appid": str(index), "name": str(index), "capture_mode": "game_capture"}
             for index in (1, 2)]

    view.on_steam_games_selected(dialog, games)
    assert len(confirmations) == 1
    assert pending == [True]
    assert view._config.saved == []
    confirmations[0][1]()
    assert pending == [True, False]
    assert not view._steam_change_pending
    assert view._config.saved == []
    assert closed == []


@pytest.mark.parametrize("prepare_fails", [False, True])
def test_manual_capture_edit_waits_for_hooks_and_preserves_settings_on_failure(
    monkeypatch, prepare_fails
):
    view = _make_view()
    original = {"name": "Game", "path": "/app/bin/game", "flatpak_id": "fixture.game",
                "capture_mode": "display_capture", "match_mode": "executable"}
    view._config = ConfigStub({"whitelist": [original]})
    view.set_sensitive = lambda _value: None
    view.reload_from_config = lambda: None
    pending = Future()
    monkeypatch.setattr(whitelist_module, "_STEAM_RESTART_EXECUTOR",
                        types.SimpleNamespace(submit=lambda *_args: pending))
    monkeypatch.setattr(whitelist_module.GLib, "idle_add", lambda function, *args: function(*args))
    results = []
    view._save_game_details(
        original, original["path"], "game_capture",
        lambda success, error=None: results.append((success, error)),
    )
    assert view._config.saved == []
    assert results == []
    assert view._steam_change_pending
    learned = dict(original, audio_process="learned-while-preparing")
    view._config.data["whitelist"] = [learned]

    if prepare_fails:
        error = OSError("missing hooks")
        pending.set_exception(error)
        assert results == [(False, error)]
        assert view._config.get("whitelist") == [learned]
    else:
        pending.set_result(None)
        assert results == [(True, None)]
        assert view._config.get("whitelist") == [dict(learned, capture_mode="game_capture")]
    assert not view._steam_change_pending


def test_steam_details_switch_capture_both_ways_without_removing_game(tmp_path, monkeypatch):
    root = tmp_path / "steam"
    localconfig = root / "userdata/42/config/localconfig.vdf"
    localconfig.parent.mkdir(parents=True)
    localconfig.write_text(steam.vkv_dump({"UserLocalConfigStore": {
        "Software": {"Valve": {"Steam": {"apps": {"1": {
            "LaunchOptions": "MANGOHUD=1 %command% -arg",
        }}}}},
    }}))
    installation = steam.SteamInstallation("chosen", EnvironmentKind.NATIVE_STEAM, root)
    monkeypatch.setattr(whitelist_module, "steam", steam)
    monkeypatch.setattr(steam, "installation_for_entry", lambda _entry: installation)
    monkeypatch.setattr(steam, "_is_steam_running", lambda **_kwargs: False)
    monkeypatch.setattr(whitelist_module.game_capture, "prepare", lambda *_args: None)
    monkeypatch.setattr(whitelist_module.game_capture, "wrapper_path",
                        lambda: tmp_path / "clipper-gamecapture")
    monkeypatch.setattr(whitelist_module, "_STEAM_RESTART_EXECUTOR", ImmediateExecutor())
    monkeypatch.setattr(whitelist_module.GLib, "idle_add", lambda function, *args: function(*args))
    view = _make_view()
    view._config = ClipperConfig(tmp_path / "config.json")
    original = {
        "appid": "1", "name": "Game", "steam_installation": "chosen",
        "steam_account_id": "42", "capture_mode": "display_capture", "audio_process": "keep-audio",
    }
    view._config.set("whitelist", [original])
    view.set_sensitive = lambda _value: None
    reloads, results = [], []
    view.reload_from_config = lambda: reloads.append(view._config.get("whitelist"))
    def completed(success, error=None):
        results.append((success, error))

    view._save_game_details(original, None, "game_capture", completed)
    hooked = view._config.get("whitelist")[0]
    assert hooked["capture_mode"] == "game_capture"
    assert hooked["game_capture_integration"]["managed_launch_options"]
    assert "clipper-gamecapture" in steam.get_launch_options("1", root, account_id="42")
    view._save_game_details(hooked, None, "display_capture", completed)

    assert results == [(True, None), (True, None)]
    assert [entries[0]["capture_mode"] for entries in reloads] == [
        "game_capture", "display_capture",
    ]
    restored = view._config.get("whitelist")
    assert len(restored) == 1
    assert restored[0]["capture_mode"] == "display_capture"
    assert restored[0]["audio_process"] == "keep-audio"
    assert "game_capture_integration" not in restored[0]
    assert steam.get_launch_options("1", root, account_id="42") == "MANGOHUD=1 %command% -arg"


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
