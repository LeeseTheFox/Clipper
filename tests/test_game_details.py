from pathlib import Path
from types import SimpleNamespace

import game_details
import pytest
from game_details import replace_executable, steam_details


def test_replace_updates_detection_preserves_settings_and_does_not_mutate(tmp_path):
    executable = tmp_path / "New.exe"
    executable.touch()
    original = {"path": "/old/Old.exe", "name": "Old.exe"}
    current = {
        **original,
        "capture_mode": "game_capture",
        "audio_process": "old",
        "icon_path": "/old/icon.png",
        "extra": "concurrent change",
    }
    result = replace_executable([current], original, str(executable))[0]
    assert result["path"] == result["executable_path"] == str(executable)
    assert result["name"] == result["executable_name"] == "New.exe"
    assert result["capture_mode"] == "game_capture"
    assert result["extra"] == "concurrent change"
    assert "icon_path" not in result and "audio_process" not in result
    assert current["path"] == original["path"]


def test_replace_preserves_custom_game_name(tmp_path):
    executable = tmp_path / "new.exe"
    executable.touch()
    entry = {"path": "old.exe", "name": "My game"}
    assert replace_executable([entry], entry, str(executable))[0]["name"] == "My game"


def test_replace_scoped_path_targets_only_original_app_and_clears_old_scope(tmp_path):
    executable = tmp_path / "game"
    executable.touch(mode=0o755)
    first = {"path": "/app/bin/game", "flatpak_id": "first.app",
             "match_mode": "executable", "process_name": "old"}
    second = {**first, "flatpak_id": "second.app"}
    result = replace_executable([first, second], first, str(executable))
    assert result[1] == second
    assert result[0]["match_mode"] == "executable"
    assert "flatpak_id" not in result[0]
    assert "process_name" not in result[0]
    with pytest.raises(ValueError, match="already"):
        replace_executable([first, {"path": str(executable)}], first, str(executable))


@pytest.mark.parametrize("value", ["", "relative.exe", "/missing/game.exe"])
def test_replace_rejects_invalid_paths(value):
    with pytest.raises(ValueError):
        replace_executable([{"path": "old"}], {"path": "old"}, value)


def test_replace_rejects_directory_and_non_executable(tmp_path):
    text = tmp_path / "text.txt"
    text.touch()
    for value in (tmp_path, text):
        with pytest.raises(ValueError):
            replace_executable([{"path": "old"}], {"path": "old"}, str(value))


def test_replace_rejects_duplicates_steam_and_stale_entries(tmp_path):
    executable = tmp_path / "new.exe"
    executable.touch()
    entry = {"path": "old"}
    with pytest.raises(ValueError, match="already"):
        replace_executable([entry, {"path": str(executable)}], entry, str(executable))
    with pytest.raises(ValueError, match="Steam"):
        replace_executable([{"appid": "123"}], {"appid": "123"}, str(executable))
    with pytest.raises(ValueError, match="changed"):
        replace_executable([], entry, str(executable))


@pytest.mark.parametrize(
    "environment,label",
    [
        ("native_steam", "Native Steam"),
        ("flatpak_steam_user", "Flatpak Steam (user)"),
        ("flatpak_steam_system", "Flatpak Steam (system)"),
        ("steam_snap", "Steam Snap"),
    ],
)
def test_steam_details_uses_exact_installation_and_account(monkeypatch, environment, label):
    installation = SimpleNamespace(
        stable_id="chosen",
        data_root=Path("/steam/chosen"),
        environment=SimpleNamespace(value=environment),
    )
    monkeypatch.setattr(game_details.steam, "discover_steam_installations", lambda: [installation])

    def select(root, account):
        assert root == Path("/steam/chosen") and account == "42"
        return SimpleNamespace(account_name="player")

    monkeypatch.setattr(game_details.steam, "select_steam_account", select)
    assert steam_details({"steam_installation": "chosen", "steam_account_id": "42"}) == (
        label,
        "player (42)",
        "/steam/chosen",
    )


def test_missing_steam_metadata_does_not_guess(monkeypatch):
    monkeypatch.setattr(game_details.steam, "discover_steam_installations", lambda: [])
    assert steam_details({}) == ("Not available",) * 3
    assert steam_details({"steam_account_id": "42", "steam_environment": "steam_snap"}) == (
        "Steam Snap",
        "42",
        "Not available",
    )
