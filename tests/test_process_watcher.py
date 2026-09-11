from pathlib import Path

from process_watcher import (
    entries_share_executable_identity,
    entry_matches_process,
    find_running_obs_studio,
    find_running_whitelist_entry,
    is_obs_studio_process,
    is_whitelisted_game_running,
    iter_processes,
    monitor_rule_id,
    process_choices,
    running_process_choices,
)


def test_flatpak_picker_prefers_app_over_renamed_helper_and_groups_children():
    app = {"comm": "feishin", "exe": "/app/main/feishin", "flatpak_id": "org.jeffvli.feishin"}
    choices = process_choices(
        [
            {**app, "pid": "1", "exe": "/usr/bin/bash"},
            {**app, "pid": "2"},
            {**app, "pid": "3"},
        ]
    )
    assert len(choices) == 1
    assert choices[0]["path"] == "/app/main/feishin"
    assert choices[0]["flatpak_id"] == "org.jeffvli.feishin"
    assert choices[0]["match_mode"] == "executable"


def test_picker_keeps_app_namespaces_separate_and_labels_helpers_honestly():
    choices = process_choices(
        [
            {
                "pid": "1",
                "comm": "feishin",
                "exe": "/usr/bin/bash",
                "flatpak_id": "org.jeffvli.feishin",
            },
            {"pid": "2", "comm": "feishin", "exe": "/app/main/feishin", "flatpak_id": "other.app"},
        ]
    )
    assert choices[0]["name"] == "bash"
    assert choices[0]["process_name"] == "feishin"
    first = {"executable_path": "/app/bin/game", "flatpak_id": "first.app"}
    second = {**first, "flatpak_id": "second.app"}
    assert not entries_share_executable_identity(first, second)


def test_picker_uses_windows_game_identity_instead_of_shared_wine_loader():
    choice = process_choices(
        [{"pid": "1", "comm": "game.exe", "exe": "/usr/bin/wine64-preloader"}]
    )[0]
    assert choice["path"] == "game.exe"
    assert "match_mode" not in choice


def test_flatpak_game_matches_appid_only_in_its_steam_environment():
    entry = {
        "appid": "730",
        "steam_installation": "flatpak_steam_user:test",
        "install_path": "/host/path/not-visible-inside-game",
    }
    process = {"comm": "game", "environ": "SteamAppId=730"}
    assert not entry_matches_process(entry, process)
    process["environ"] += " FLATPAK_ID=com.valvesoftware.Steam"
    assert entry_matches_process(entry, process)


def _add_proc(
    proc_root: Path,
    pid: str,
    *,
    comm: str = "",
    cmdline: str = "",
    environ: str = "",
    exe: str = "",
    cwd: str = "",
) -> None:
    proc_dir = proc_root / pid
    proc_dir.mkdir()
    (proc_dir / "comm").write_text(comm, encoding="utf-8")
    (proc_dir / "cmdline").write_bytes(cmdline.encode("utf-8").replace(b" ", b"\0"))
    (proc_dir / "environ").write_bytes(environ.encode("utf-8").replace(b" ", b"\0"))
    if exe:
        (proc_dir / "exe").symlink_to(exe)
    if cwd:
        (proc_dir / "cwd").symlink_to(cwd)


def test_entry_matches_process_name():
    assert entry_matches_process(
        {"name": "cs2", "path": "cs2"},
        {"comm": "cs2", "cmdline": "", "exe": "/games/cs2/cs2", "cwd": "/games/cs2"},
    )


def test_entry_matches_manual_executable_path():
    assert entry_matches_process(
        {"name": "Example", "path": "/opt/games/example/example-bin"},
        {
            "comm": "example-bin",
            "cmdline": "/opt/games/example/example-bin --fullscreen",
            "exe": "/opt/games/example/example-bin",
            "cwd": "/opt/games/example",
        },
    )


def test_entry_matches_executable_path_home_alias():
    assert entry_matches_process(
        {
            "name": "game-bin",
            "path": "/var/home/leese/Games/Example/game-bin",
            "executable_path": "/var/home/leese/Games/Example/game-bin",
            "executable_name": "game-bin",
        },
        {
            "comm": "game-bin",
            "cmdline": "/home/leese/Games/Example/game-bin --fullscreen",
            "exe": "/home/leese/Games/Example/game-bin",
            "cwd": "/home/leese/Games/Example",
        },
    )


def test_entry_matches_executable_name_after_game_folder_moves():
    assert entry_matches_process(
        {
            "name": "game-bin",
            "path": "/home/leese/Games/OldLocation/game-bin",
            "executable_path": "/home/leese/Games/OldLocation/game-bin",
            "executable_name": "game-bin",
        },
        {
            "comm": "game-bin",
            "cmdline": "/mnt/storage/Games/NewLocation/game-bin --fullscreen",
            "exe": "/mnt/storage/Games/NewLocation/game-bin",
            "cwd": "/mnt/storage/Games/NewLocation",
        },
    )


def test_entry_matches_windows_executable_name_in_wine_cmdline_after_move():
    assert entry_matches_process(
        {
            "name": "Crab Game.exe",
            "path": "/home/leese/Games/Crab Game/Crab Game.exe",
            "executable_path": "/home/leese/Games/Crab Game/Crab Game.exe",
            "executable_name": "Crab Game.exe",
        },
        {
            "comm": "wine64",
            "cmdline": "wine Z:\\mnt\\storage\\Games\\Crab Game\\Crab Game.exe",
            "environ": "",
            "exe": "/usr/bin/wine64",
            "cwd": "/mnt/storage/Games/Crab Game",
        },
    )


def test_entry_does_not_match_executable_name_inside_longer_name():
    assert not entry_matches_process(
        {
            "name": "game.exe",
            "path": "/home/leese/Games/Game/game.exe",
            "executable_path": "/home/leese/Games/Game/game.exe",
            "executable_name": "game.exe",
        },
        {
            "comm": "notgame.exe",
            "cmdline": "/mnt/storage/notgame.exe",
            "exe": "/mnt/storage/notgame.exe",
            "cwd": "/mnt/storage",
        },
    )


def test_entry_matches_deleted_proc_exe_suffix():
    assert entry_matches_process(
        {
            "name": "game-bin",
            "path": "/home/leese/Games/Example/game-bin",
            "executable_path": "/home/leese/Games/Example/game-bin",
            "executable_name": "game-bin",
        },
        {
            "comm": "game-bin",
            "cmdline": "/home/leese/Games/Example/game-bin --fullscreen",
            "exe": "/home/leese/Games/Example/game-bin (deleted)",
            "cwd": "/home/leese/Games/Example",
        },
    )


def test_entry_path_does_not_match_neighbor_path_prefix():
    assert not entry_matches_process(
        {"name": "Game", "install_path": "/home/leese/Games/Foo"},
        {
            "comm": "game-bin",
            "cmdline": "/home/leese/Games/Foo2/game-bin",
            "environ": "",
            "exe": "/home/leese/Games/Foo2/game-bin",
            "cwd": "/home/leese/Games/Foo2",
        },
    )


def test_entries_share_identity_for_same_executable_home_alias():
    assert entries_share_executable_identity(
        {
            "name": "game-bin",
            "path": "/home/leese/Games/Example/game-bin",
            "executable_path": "/home/leese/Games/Example/game-bin",
            "executable_name": "game-bin",
        },
        {
            "name": "game-bin",
            "path": "/var/home/leese/Games/Example/game-bin",
            "executable_path": "/var/home/leese/Games/Example/game-bin",
            "executable_name": "game-bin",
        },
    )


def test_entries_share_identity_for_symlinked_executable(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    executable = real_dir / "game-bin"
    executable.write_text("", encoding="utf-8")

    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    assert entries_share_executable_identity(
        {"name": "game-bin", "path": str(executable)},
        {"name": "game-bin", "path": str(link_dir / "game-bin")},
    )


def test_entries_share_identity_when_executable_is_inside_steam_install_path():
    assert entries_share_executable_identity(
        {
            "name": "Steam Game",
            "appid": "123",
            "install_path": "/run/media/system/Games/SteamLibrary/common/Game",
        },
        {
            "name": "game-bin",
            "path": "/run/media/system/Games/SteamLibrary/common/Game/bin/game-bin",
            "executable_path": "/run/media/system/Games/SteamLibrary/common/Game/bin/game-bin",
            "executable_name": "game-bin",
        },
    )


def test_entries_do_not_share_identity_for_same_basename_in_different_paths():
    assert not entries_share_executable_identity(
        {
            "name": "game.exe",
            "path": "/run/media/system/Games/First/game.exe",
            "executable_path": "/run/media/system/Games/First/game.exe",
            "executable_name": "game.exe",
        },
        {
            "name": "game.exe",
            "path": "/run/media/system/Games/Second/game.exe",
            "executable_path": "/run/media/system/Games/Second/game.exe",
            "executable_name": "game.exe",
        },
    )


def test_entries_share_identity_with_legacy_name_only_entry():
    assert entries_share_executable_identity(
        {
            "name": "game-bin",
            "path": "/run/media/system/Games/Example/game-bin",
            "executable_path": "/run/media/system/Games/Example/game-bin",
            "executable_name": "game-bin",
        },
        {"name": "Example", "path": "game-bin"},
    )


def test_monitor_rule_id_for_executable_path_is_stable_across_indexes():
    entry = {
        "name": "Example",
        "path": "/games/example/game-bin",
        "executable_path": "/games/example/game-bin",
    }

    assert monitor_rule_id(entry, 0) == monitor_rule_id(entry, 3)
    assert monitor_rule_id(entry, 0) == "path-c5a72f500eeddda3"


def test_monitor_rule_id_scopes_same_appid_to_steam_installation():
    native = {"appid": "730", "steam_installation": "native:/steam"}
    flatpak = {"appid": "730", "steam_installation": "flatpak:user"}

    assert monitor_rule_id(native, 0) == "steam-74c90759ee5879db-730"
    assert monitor_rule_id(flatpak, 0) == "steam-84ecec948babd13b-730"
    assert monitor_rule_id({"appid": "730"}, 0) == "steam-730"


def test_entry_matches_steam_install_path():
    assert entry_matches_process(
        {"name": "Counter-Strike 2", "appid": "730", "install_path": "/steam/common/cs2"},
        {
            "comm": "cs2",
            "cmdline": "/steam/common/cs2/game/bin/linuxsteamrt64/cs2",
            "environ": "",
            "exe": "/steam/common/cs2/game/bin/linuxsteamrt64/cs2",
            "cwd": "/steam/common/cs2",
        },
    )


def test_entry_matches_steam_appid_environment():
    assert entry_matches_process(
        {
            "name": "Crab Game",
            "appid": "1782210",
            "install_path": "/steam/common/Crab Game",
        },
        {
            "comm": "Crab Game.exe",
            "cmdline": "Z:\\games\\Crab Game.exe",
            "environ": "SteamAppId=1782210 STEAM_COMPAT_APP_ID=1782210",
            "exe": "/usr/bin/wineserver",
            "cwd": "/steam/steamapps/compatdata/1782210/pfx",
        },
    )


def test_entry_matches_steam_compatdata_path():
    assert entry_matches_process(
        {"name": "Crab Game", "appid": "1782210"},
        {
            "comm": "Crab Game.exe",
            "cmdline": "/usr/bin/wine /steam/steamapps/compatdata/1782210/pfx/game.exe",
            "environ": "",
            "exe": "/usr/bin/wine",
            "cwd": "/steam/steamapps/compatdata/1782210/pfx",
        },
    )


def test_entry_matches_flatpak_steam_native_game_install_path():
    assert entry_matches_process(
        {
            "name": "Counter-Strike 2",
            "appid": "730",
            "install_path": (
                "/var/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/common/Counter-Strike Global Offensive"
            ),
        },
        {
            "comm": "cs2",
            "cmdline": (
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/common/Counter-Strike Global Offensive/game/bin/linuxsteamrt64/cs2"
            ),
            "environ": "SteamAppId=730 SteamGameId=730",
            "exe": (
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/common/Counter-Strike Global Offensive/game/bin/linuxsteamrt64/cs2"
            ),
            "cwd": (
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/common/Counter-Strike Global Offensive"
            ),
        },
    )


def test_entry_matches_flatpak_steam_proton_compatdata_path():
    assert entry_matches_process(
        {"name": "Crab Game", "appid": "1782210"},
        {
            "comm": "Crab Game.exe",
            "cmdline": (
                "pressure-vessel-wrap "
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/compatdata/1782210/pfx/drive_c/Program Files/"
                "Crab Game/Crab Game.exe"
            ),
            "environ": "STEAM_COMPAT_APP_ID=1782210 SteamAppId=1782210",
            "exe": "/app/bin/pressure-vessel-wrap",
            "cwd": (
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/compatdata/1782210/pfx"
            ),
        },
    )


def test_entry_matches_proton_launcher_child_by_steam_appid_environment():
    assert entry_matches_process(
        {
            "name": "Deep Rock Galactic",
            "appid": "548430",
            "install_path": "/steam/common/Deep Rock Galactic",
        },
        {
            "comm": "FSD-Win64-Shipping.exe",
            "cmdline": (
                "Z:\\steam\\steamapps\\common\\Deep Rock Galactic\\FSD\\Binaries\\Win64\\"
                "FSD-Win64-Shipping.exe"
            ),
            "environ": (
                "SteamAppId=548430 STEAM_COMPAT_APP_ID=548430 "
                "STEAM_COMPAT_DATA_PATH=/steam/steamapps/compatdata/548430"
            ),
            "exe": "/usr/bin/wineserver",
            "cwd": "/steam/steamapps/common/Deep Rock Galactic",
        },
    )


def test_entry_rejects_nearby_flatpak_steam_compatdata_appid():
    assert not entry_matches_process(
        {"name": "Near Miss", "appid": "178221"},
        {
            "comm": "Crab Game.exe",
            "cmdline": (
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/compatdata/1782210/pfx/drive_c/Game.exe"
            ),
            "environ": "SteamAppId=1782210 STEAM_COMPAT_APP_ID=1782210",
            "exe": "/usr/bin/wine",
            "cwd": (
                "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                "steamapps/compatdata/1782210/pfx"
            ),
        },
    )


def test_is_whitelisted_game_running_uses_proc_root(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "100",
        comm="bash",
        cmdline="bash",
        exe="/usr/bin/bash",
        cwd="/tmp",
    )
    _add_proc(
        proc_root,
        "200",
        comm="game-bin",
        cmdline="/opt/game/game-bin",
        exe="/opt/game/game-bin",
        cwd="/opt/game",
    )

    assert is_whitelisted_game_running([{"path": "game-bin"}], proc_root)
    assert not is_whitelisted_game_running([{"path": "missing-game"}], proc_root)


def test_iter_processes_includes_pid_for_picker(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "200",
        comm="game-bin",
        cmdline="/opt/game/game-bin",
        exe="/opt/game/game-bin",
        cwd="/opt/game",
    )

    assert iter_processes(proc_root)[0]["pid"] == "200"


def test_obs_studio_detection_uses_exact_executable_identity(tmp_path):
    assert is_obs_studio_process({"comm": "obs", "exe": "/app/bin/obs"})
    assert is_obs_studio_process({"comm": "bwrap", "exe": "/usr/bin/obs-studio"})

    assert not is_obs_studio_process(
        {
            "comm": "clipper-engine",
            "exe": "/app/libexec/clipper/clipper-engine",
            "cmdline": "clipper-engine --using-libobs",
            "environ": "CLIPPER_OBS_DATADIR=/app/share/obs",
        }
    )
    assert not is_obs_studio_process(
        {
            "comm": "game-bin",
            "exe": "/games/game-bin",
            "cmdline": "obs-gamecapture /games/game-bin",
        }
    )


def test_find_running_obs_studio_returns_matching_process(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "100",
        comm="clipper-engine",
        cmdline="/app/libexec/clipper/clipper-engine",
        environ="CLIPPER_OBS_DATADIR=/app/share/obs",
        exe="/app/libexec/clipper/clipper-engine",
    )
    _add_proc(
        proc_root,
        "200",
        comm="obs",
        cmdline="/app/bin/obs",
        exe="/app/bin/obs",
    )

    process = find_running_obs_studio(proc_root)

    assert process is not None
    assert process["pid"] == "200"


def test_running_process_choices_use_real_process_metadata(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "200",
        comm="game-bin",
        cmdline="/opt/game/game-bin --fullscreen",
        exe="/opt/game/game-bin",
        cwd="/opt/game",
    )
    _add_proc(
        proc_root,
        "300",
        comm="game-bin",
        cmdline="/opt/game/game-bin --fullscreen",
        exe="/opt/game/game-bin",
        cwd="/opt/game",
    )
    _add_proc(
        proc_root,
        "400",
        comm="",
        cmdline="/usr/bin/python3 script.py",
        cwd="/tmp",
    )

    assert running_process_choices(proc_root) == [
        {
            "pid": "200",
            "name": "game-bin",
            "path": "/opt/game/game-bin",
            "cmdline": "/opt/game/game-bin --fullscreen",
            "match_mode": "executable",
        },
        {
            "pid": "400",
            "name": "python3",
            "path": "python3",
            "cmdline": "/usr/bin/python3 script.py",
        },
    ]


def test_find_running_whitelist_entry_respects_capture_mode(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "100",
        comm="game-bin",
        cmdline="/opt/game/game-bin",
        exe="/opt/game/game-bin",
        cwd="/opt/game",
    )

    display_entry = {
        "name": "Game",
        "path": "game-bin",
        "capture_mode": "display_capture",
    }
    game_entry = {
        "name": "Game",
        "path": "game-bin",
        "capture_mode": "game_capture",
    }

    assert (
        find_running_whitelist_entry([display_entry], proc_root, "display_capture") == display_entry
    )
    assert find_running_whitelist_entry([display_entry], proc_root, "game_capture") is None
    assert find_running_whitelist_entry([game_entry], proc_root, "game_capture") == game_entry
