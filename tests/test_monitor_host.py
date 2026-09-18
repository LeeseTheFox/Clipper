import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from process_watcher import (
    entry_matches_process,
    monitor_rule_id,
    process_choice_matches_search,
    process_choices,
    running_process_choices,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITOR_BINARY = REPO_ROOT / "engine" / "monitor" / "clipper-monitor-host"


def test_feishin_search_groups_sandbox_children_without_environment(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    for pid, name, exe, environ in (
        ("1", "feishin", "/app/main/feishin", "FLATPAK_ID=org.jeffvli.feishin"),
        ("2", "feishin", "/app/main/feishin", ""),
        ("3", "cat", "/usr/bin/cat", "FLATPAK_ID=org.jeffvli.feishin"),
        ("4", "feishin", "/usr/bin/bash", ""),
    ):
        _add_proc(
            proc_root, pid, comm=name, exe=exe, environ=environ, cmdline=f"{exe} /tmp/feishin.log"
        )
        root = proc_root / pid / "root"
        root.mkdir()
        (root / ".flatpak-info").write_text(
            "[Application]\nname=org.jeffvli.feishin\n[Instance]\ninstance-id=42\n",
            encoding="utf-8",
        )
    for choices in (
        running_process_choices(proc_root),
        process_choices(_list_processes(monitor_binary, proc_root)),
    ):
        matches = [p for p in choices if process_choice_matches_search(p, "feishin")]
        assert len(matches) == 1
        assert matches[0]["path"] == "/app/main/feishin"
        assert matches[0]["flatpak_id"] == "org.jeffvli.feishin"
        assert any(process_choice_matches_search(p, "cat") for p in choices)
    config = tmp_path / "config.json"
    _write_config(
        config,
        [
            {
                "executable_path": "/app/main/feishin",
                "flatpak_id": "org.jeffvli.feishin",
                "match_mode": "executable",
            }
        ],
    )
    # Only the environment-free app process remains executable.
    (proc_root / "1/exe").unlink()
    assert _probe(monitor_binary, config, proc_root)["matches"] == 1


@pytest.mark.parametrize(
    "process,expected",
    [
        ({"exe": "/app/main/feishin", "environ": "FLATPAK_ID=org.jeffvli.feishin"}, True),
        ({"exe": "/app/main/feishin (deleted)", "environ": "FLATPAK_ID=org.jeffvli.feishin"}, True),
        ({"exe": "/app/main/feishin", "environ": "FLATPAK_ID=other.app"}, False),
        ({"exe": "/app/main/Feishin", "environ": "FLATPAK_ID=org.jeffvli.feishin"}, False),
        (
            {
                "exe": "/usr/bin/bash",
                "comm": "feishin",
                "environ": "FLATPAK_ID=org.jeffvli.feishin",
                "cmdline": "/app/main/feishin",
                "cwd": "/app/main/feishin",
            },
            False,
        ),
        ({"exe": "/app/main/feishin", "environ": "FLATPAK_ID=org.jeffvli.feishin.extra"}, False),
    ],
)
def test_precise_flatpak_rules_agree_in_native_and_host_monitors(
    tmp_path, monitor_binary, process, expected
):
    entry = {
        "name": "feishin",
        "path": "/app/main/feishin",
        "executable_path": "/app/main/feishin",
        "executable_name": "feishin",
        "flatpak_id": "org.jeffvli.feishin",
        "match_mode": "executable",
    }
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(proc_root, "42", **process)
    config = tmp_path / "config.json"
    _write_config(config, [entry])
    assert entry_matches_process(entry, process) is expected
    assert _probe(monitor_binary, config, proc_root)["matches"] == int(expected)
    completed = subprocess.run(
        [str(monitor_binary), "--list-processes"],
        check=True,
        capture_output=True,
        text=True,
        env={"CLIPPER_CONFIG_FILE": str(config), "CLIPPER_PROC_ROOT": str(proc_root)},
    )
    listed = json.loads(completed.stdout)[0]
    assert listed["rule_ids"] == ([monitor_rule_id(entry, 0)] if expected else [])
    assert listed["flatpak_id"] == process["environ"].partition("=")[2]


@pytest.mark.parametrize(
    "comm,exe,expected",
    [
        ("feishin", "/usr/bin/bash", True),
        ("clipper", "/usr/bin/bash", False),
        ("bash", "/usr/bin/bash", False),
        ("feishin", "/usr/bin/bash/child", False),
    ],
)
def test_precise_renamed_runtime_requires_both_executable_and_process_name(
    tmp_path, monitor_binary, comm, exe, expected
):
    entry = {
        "executable_path": "/usr/bin/bash",
        "match_mode": "executable",
        "process_name": "feishin",
    }
    process = {"comm": comm, "exe": exe}
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(proc_root, "42", **process)
    config = tmp_path / "config.json"
    _write_config(config, [entry])
    assert entry_matches_process(entry, process) is expected
    assert _probe(monitor_binary, config, proc_root)["matches"] == int(expected)


@pytest.fixture(scope="module")
def monitor_binary() -> Path:
    subprocess.run(
        ["make", "-C", "engine/monitor"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return MONITOR_BINARY


def _write_config(path: Path, whitelist: list[dict]) -> None:
    path.write_text(json.dumps({"whitelist": whitelist}), encoding="utf-8")


def _add_proc(
    proc_root: Path,
    pid: str,
    *,
    comm: str = "",
    cmdline: str = "",
    environ: str = "",
    exe: str = "",
    cwd: str = "",
    namespace_pids: tuple[int, ...] = (),
) -> None:
    proc_dir = proc_root / pid
    proc_dir.mkdir()
    (proc_dir / "comm").write_text(comm, encoding="utf-8")
    (proc_dir / "cmdline").write_bytes(cmdline.encode("utf-8").replace(b" ", b"\0"))
    (proc_dir / "environ").write_bytes(environ.encode("utf-8").replace(b" ", b"\0"))
    namespace_pid_text = "\t".join(str(value) for value in namespace_pids)
    status = f"NSpid:\t{namespace_pid_text}\n"
    (proc_dir / "status").write_text(status if namespace_pids else "", encoding="utf-8")
    if exe:
        (proc_dir / "exe").symlink_to(exe)
    if cwd:
        (proc_dir / "cwd").symlink_to(cwd)


def _probe(monitor_binary: Path, config_path: Path, proc_root: Path) -> dict:
    completed = subprocess.run(
        [str(monitor_binary), "--probe"],
        check=True,
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
        },
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("rules,expected", [
    ([], 0),
    ([{"match_mode": "executable", "executable_path": "/games/example"}], 1),
])
def test_exact_and_obs_only_scans_skip_unused_fields(tmp_path, monitor_binary, rules, expected):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(proc_root, "123", comm="example", exe="/games/example")
    # Reading either FIFO would block. They are irrelevant to these matchers.
    for field in ("cmdline", "environ"):
        path = proc_root / "123" / field
        path.unlink()
        os.mkfifo(path)
    config = tmp_path / "config.json"
    _write_config(config, rules)
    result = subprocess.run(
        [str(monitor_binary), "--probe"], check=True, capture_output=True, text=True,
        env={"CLIPPER_CONFIG_FILE": str(config), "CLIPPER_PROC_ROOT": str(proc_root)},
        timeout=5,
    )
    assert json.loads(result.stdout)["matches"] == expected


def _list_processes(monitor_binary: Path, proc_root: Path) -> list[dict]:
    completed = subprocess.run(
        [str(monitor_binary), "--list-processes"],
        check=True,
        env={"CLIPPER_PROC_ROOT": str(proc_root)},
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_flatpak_game_matches_appid_in_its_own_steam_environment(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config = tmp_path / "config.json"
    _write_config(
        config,
        [
            {
                "appid": "730",
                "name": "Game",
                "steam_installation": "flatpak_steam_user:test",
                "install_path": "/host/path/not-visible-inside-game",
            }
        ],
    )
    _add_proc(proc_root, "12", comm="game", environ="SteamAppId=730")
    assert _probe(monitor_binary, config, proc_root)["matches"] == 0
    (proc_root / "12/environ").write_bytes(b"SteamAppId=730\0FLATPAK_ID=com.valvesoftware.Steam\0")
    assert _probe(monitor_binary, config, proc_root)["matches"] == 1


def test_monitor_host_lists_picker_process_metadata(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "123",
        comm="Example Game",
        cmdline="/games/example --fullscreen",
        environ="SECRET=value",
        exe="/games/example",
        cwd="/games",
    )

    assert _list_processes(monitor_binary, proc_root) == [
        {
            "pid": 123,
            "namespace_pids": [],
            "comm": "Example Game",
            "cmdline": "/games/example --fullscreen",
            "exe": "/games/example",
            "rule_ids": [],
        }
    ]


def test_monitor_host_lists_matching_rule_ids_without_exporting_environment(
    tmp_path, monitor_binary
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [{"name": "THE FINALS", "appid": "2073850"}],
    )
    _add_proc(
        proc_root,
        "2073",
        comm="Discovery-d.exe",
        cmdline="/usr/bin/wine64 Discovery-d.exe",
        environ="STEAM_COMPAT_APP_ID=2073850 SECRET=value",
        exe="/usr/bin/wine64-preloader",
        cwd="/games/The Finals",
        namespace_pids=(2073, 930),
    )

    completed = subprocess.run(
        [str(monitor_binary), "--list-processes"],
        check=True,
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
        },
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        {
            "pid": 2073,
            "namespace_pids": [2073, 930],
            "comm": "Discovery-d.exe",
            "cmdline": "/usr/bin/wine64 Discovery-d.exe",
            "exe": "/usr/bin/wine64-preloader",
            "rule_ids": ["steam-2073850"],
        }
    ]
    assert "SECRET" not in completed.stdout


def test_monitor_host_uses_same_installation_scoped_steam_rule_id(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Counter-Strike 2",
                "appid": "730",
                "steam_installation": "native:/steam",
                "install_path": "/steam/common/cs2",
            }
        ],
    )
    _add_proc(
        proc_root,
        "730",
        comm="cs2",
        cmdline="/steam/common/cs2/cs2",
        exe="/steam/common/cs2/cs2",
        cwd="/steam/common/cs2",
    )

    completed = subprocess.run(
        [str(monitor_binary), "--list-processes"],
        check=True,
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
        },
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result[0]["rule_ids"] == ["steam-74c90759ee5879db-730"]


@pytest.mark.parametrize("operation", ["--stop-steam-native", "--start-steam-native"])
def test_monitor_host_steam_lifecycle_uses_fixed_steam_command(tmp_path, monitor_binary, operation):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    steam_command = bin_dir / "steam"
    steam_command.write_text(
        '#!/bin/sh\nprintf "%s" "$*" > "$CLIPPER_TEST_LOG"\n',
        encoding="utf-8",
    )
    steam_command.chmod(0o755)
    command_log = tmp_path / "steam-command.log"
    if operation == "--start-steam-native":
        systemd_run = bin_dir / "systemd-run"
        systemd_run.write_text(
            '#!/bin/sh\nprintf "%s\\n%s\\n" "$DBUS_SESSION_BUS_ADDRESS" "$*" '
            '> "$CLIPPER_TEST_LOG"\n',
            encoding="utf-8",
        )
        systemd_run.chmod(0o755)
        steam_proc = proc_root / "123"
        steam_proc.mkdir()
        (steam_proc / "comm").write_text("steam\n", encoding="utf-8")

    completed = subprocess.run(
        [str(monitor_binary), operation],
        check=False,
        env={
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_TEST_LOG": str(command_log),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/flatpak/bus",
            "PATH": str(bin_dir),
            "XDG_RUNTIME_DIR": "/run/user/1234",
        },
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    if operation == "--start-steam-native":
        for _attempt in range(100):
            if command_log.exists():
                break
            time.sleep(0.01)
        assert command_log.read_text(encoding="utf-8").splitlines() == [
            "unix:path=/run/user/1234/bus",
            "--user --collect --quiet -- steam steam://open/main",
        ]
    else:
        assert not command_log.exists()


@pytest.mark.parametrize(
    ("operation", "process_environment", "expected_status"),
    [
        ("--steam-running-native", "", 0),
        (
            "--steam-running-flatpak",
            "FLATPAK_ID=com.valvesoftware.Steam",
            0,
        ),
        ("--steam-running-snap", "SNAP_NAME=steam", 0),
        ("--steam-running-native", "SNAP_NAME=steam", 1),
        (
            "--steam-running-snap",
            "FLATPAK_ID=com.valvesoftware.Steam",
            1,
        ),
    ],
)
def test_monitor_host_distinguishes_steam_packaging_variants(
    tmp_path, monitor_binary, operation, process_environment, expected_status
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(
        proc_root,
        "123",
        comm="steam",
        environ=process_environment,
    )

    completed = subprocess.run(
        [str(monitor_binary), operation],
        check=False,
        env={"CLIPPER_PROC_ROOT": str(proc_root)},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == expected_status


@pytest.mark.parametrize(
    ("operation", "process_environment", "expected_command"),
    [
        (
            "--start-steam-flatpak",
            "FLATPAK_ID=com.valvesoftware.Steam",
            "--user --collect --quiet -- flatpak run com.valvesoftware.Steam steam://open/main",
        ),
        (
            "--start-steam-snap",
            "SNAP_NAME=steam",
            "--user --collect --quiet -- snap run steam steam://open/main",
        ),
    ],
)
def test_monitor_host_starts_only_the_fixed_requested_variant(
    tmp_path, monitor_binary, operation, process_environment, expected_command
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_proc(proc_root, "123", comm="steam", environ=process_environment)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "steam-command.log"
    systemd_run = bin_dir / "systemd-run"
    systemd_run.write_text(
        '#!/bin/sh\nprintf "%s" "$*" > "$CLIPPER_TEST_LOG"\n',
        encoding="utf-8",
    )
    systemd_run.chmod(0o755)

    completed = subprocess.run(
        [str(monitor_binary), operation],
        check=False,
        env={
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_TEST_LOG": str(command_log),
            "PATH": str(bin_dir),
            "XDG_RUNTIME_DIR": "/run/user/1234",
        },
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    for _attempt in range(100):
        if command_log.exists():
            break
        time.sleep(0.01)
    assert command_log.read_text(encoding="utf-8") == expected_command


def _stable_path_rule_id(path: str) -> str:
    hash_value = 0xCBF29CE484222325
    for byte in path.encode("utf-8"):
        hash_value ^= byte
        hash_value = (hash_value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"path-{hash_value:016x}"


def test_monitor_host_matches_display_capture_executable_path(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Example",
                "capture_mode": "display_capture",
                "path": "/games/example/example-bin",
                "executable_path": "/games/example/example-bin",
                "executable_name": "example-bin",
            }
        ],
    )
    _add_proc(
        proc_root,
        "123",
        comm="example-bin",
        cmdline="/games/example/example-bin --fullscreen",
        exe="/games/example/example-bin",
        cwd="/games/example",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_matches_game_capture_rules(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Example",
                "capture_mode": "game_capture",
                "path": "/games/example/example-bin",
                "executable_path": "/games/example/example-bin",
                "executable_name": "example-bin",
            }
        ],
    )
    _add_proc(
        proc_root,
        "123",
        comm="example-bin",
        cmdline="/games/example/example-bin --fullscreen",
        exe="/games/example/example-bin",
        cwd="/games/example",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_matches_exact_steam_appid_only(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Counter-Strike 2",
                "capture_mode": "display_capture",
                "appid": "730",
            },
            {
                "name": "Near Miss",
                "capture_mode": "display_capture",
                "appid": "73",
            },
        ],
    )
    _add_proc(
        proc_root,
        "7300",
        comm="cs2",
        cmdline="/steam/common/cs2/game/bin/linuxsteamrt64/cs2",
        environ="SteamAppId=730 STEAM_COMPAT_APP_ID=730",
        exe="/steam/common/cs2/game/bin/linuxsteamrt64/cs2",
        cwd="/steam/steamapps/compatdata/730/pfx",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 2,
        "matches": 1,
    }


def test_monitor_host_matches_flatpak_steam_native_install_path(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    install_path = (
        "/var/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
        "steamapps/common/Counter-Strike Global Offensive"
    )
    runtime_path = (
        "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
        "steamapps/common/Counter-Strike Global Offensive"
    )
    _write_config(
        config_path,
        [
            {
                "name": "Counter-Strike 2",
                "capture_mode": "display_capture",
                "appid": "730",
                "install_path": install_path,
            }
        ],
    )
    _add_proc(
        proc_root,
        "7310",
        comm="cs2",
        cmdline=f"{runtime_path}/game/bin/linuxsteamrt64/cs2",
        environ="SteamAppId=730 SteamGameId=730",
        exe=f"{runtime_path}/game/bin/linuxsteamrt64/cs2",
        cwd=runtime_path,
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_matches_flatpak_steam_proton_compatdata(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    compat_path = (
        "/home/leese/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
        "steamapps/compatdata/1782210/pfx"
    )
    _write_config(
        config_path,
        [
            {
                "name": "Crab Game",
                "capture_mode": "display_capture",
                "appid": "1782210",
            },
            {
                "name": "Near Miss",
                "capture_mode": "display_capture",
                "appid": "178221",
            },
        ],
    )
    _add_proc(
        proc_root,
        "1782210",
        comm="Crab Game.exe",
        cmdline=(
            f"pressure-vessel-wrap {compat_path}/drive_c/Program Files/Crab Game/Crab Game.exe"
        ),
        environ="STEAM_COMPAT_APP_ID=1782210 SteamAppId=1782210",
        exe="/app/bin/pressure-vessel-wrap",
        cwd=compat_path,
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 2,
        "matches": 1,
    }


def test_monitor_host_matches_proton_launcher_child_environment(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Deep Rock Galactic",
                "capture_mode": "display_capture",
                "appid": "548430",
                "install_path": "/steam/common/Deep Rock Galactic",
            }
        ],
    )
    _add_proc(
        proc_root,
        "548430",
        comm="FSD-Win64-Shipping.exe",
        cmdline=(
            "Z:\\steam\\steamapps\\common\\Deep Rock Galactic\\FSD\\Binaries\\Win64\\"
            "FSD-Win64-Shipping.exe"
        ),
        environ=(
            "SteamAppId=548430 STEAM_COMPAT_APP_ID=548430 "
            "STEAM_COMPAT_DATA_PATH=/steam/steamapps/compatdata/548430"
        ),
        exe="/usr/bin/wineserver",
        cwd="/steam/steamapps/common/Deep Rock Galactic",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_install_path_does_not_match_neighbor_prefix(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Foo",
                "capture_mode": "display_capture",
                "install_path": "/games/Foo",
            }
        ],
    )
    _add_proc(
        proc_root,
        "200",
        comm="game-bin",
        cmdline="/games/Foo2/game-bin",
        exe="/games/Foo2/game-bin",
        cwd="/games/Foo2",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 0,
    }


def test_monitor_host_executable_name_does_not_match_longer_name(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "game.exe",
                "capture_mode": "display_capture",
                "path": "/games/Game/game.exe",
                "executable_path": "/games/Game/game.exe",
                "executable_name": "game.exe",
            }
        ],
    )
    _add_proc(
        proc_root,
        "201",
        comm="notgame.exe",
        cmdline="/games/Other/notgame.exe",
        exe="/games/Other/notgame.exe",
        cwd="/games/Other",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 0,
    }


def test_monitor_host_matches_windows_executable_name_in_wine_cmdline_after_move(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "Crab Game.exe",
                "capture_mode": "display_capture",
                "path": "/home/leese/Games/Crab Game/Crab Game.exe",
                "executable_path": "/home/leese/Games/Crab Game/Crab Game.exe",
                "executable_name": "Crab Game.exe",
            }
        ],
    )
    _add_proc(
        proc_root,
        "202",
        comm="wine64",
        cmdline="wine Z:\\mnt\\storage\\Games\\Crab Game\\Crab Game.exe",
        exe="/usr/bin/wine64",
        cwd="/mnt/storage/Games/Crab Game",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_matches_home_var_home_alias(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "game-bin",
                "capture_mode": "display_capture",
                "path": "/var/home/leese/Games/Example/game-bin",
                "executable_path": "/var/home/leese/Games/Example/game-bin",
                "executable_name": "game-bin",
            }
        ],
    )
    _add_proc(
        proc_root,
        "202",
        comm="game-bin",
        cmdline="/home/leese/Games/Example/game-bin --fullscreen",
        exe="/home/leese/Games/Example/game-bin",
        cwd="/home/leese/Games/Example",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_matches_deleted_proc_exe_suffix(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {
                "name": "game-bin",
                "capture_mode": "display_capture",
                "path": "/home/leese/Games/Example/game-bin",
                "executable_path": "/home/leese/Games/Example/game-bin",
                "executable_name": "game-bin",
            }
        ],
    )
    _add_proc(
        proc_root,
        "203",
        comm="game-bin",
        cmdline="/home/leese/Games/Example/game-bin --fullscreen",
        exe="/home/leese/Games/Example/game-bin (deleted)",
        cwd="/home/leese/Games/Example",
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_matches_symlinked_executable_path(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    real_dir = tmp_path / "real"
    link_dir = tmp_path / "link"
    real_dir.mkdir()
    executable = real_dir / "game-bin"
    executable.write_text("", encoding="utf-8")
    link_dir.symlink_to(real_dir)
    link_executable = link_dir / "game-bin"
    _write_config(
        config_path,
        [
            {
                "name": "Example",
                "capture_mode": "display_capture",
                "path": str(executable),
                "executable_path": str(executable),
            }
        ],
    )
    _add_proc(
        proc_root,
        "204",
        comm="renamed-game-process",
        cmdline=f"{link_executable} --fullscreen",
        exe=str(link_executable),
        cwd=str(link_dir),
    )

    assert _probe(monitor_binary, config_path, proc_root) == {
        "ok": True,
        "rules": 1,
        "matches": 1,
    }


def test_monitor_host_service_reloads_config_changes(tmp_path, monitor_binary):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])

    process = subprocess.Popen(
        [str(monitor_binary), "--service"],
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_MONITOR_INTERVAL_MS": "100",
            "CLIPPER_MONITOR_MAX_ITERATIONS": "6",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.15)
    _write_config(
        config_path,
        [
            {
                "name": "Example",
                "capture_mode": "display_capture",
                "path": "/games/example/example-bin",
            }
        ],
    )

    stdout, stderr = process.communicate(timeout=2)

    assert process.returncode == 0, (stdout, stderr)
    assert "[clipper-monitor] reloaded config rules=1" in stderr


def test_monitor_host_service_detects_obs_studio_without_a_config_rule(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])
    _add_proc(
        proc_root,
        "410",
        comm="obs",
        cmdline="/app/bin/obs",
        exe="/app/bin/obs",
    )

    completed = subprocess.run(
        [str(monitor_binary), "--service"],
        check=True,
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_MONITOR_MAX_ITERATIONS": "1",
        },
        capture_output=True,
        text=True,
    )

    assert "[clipper-monitor] started clipper-observer-obs-studio pid=410" in completed.stderr


def test_monitor_host_service_uses_one_snapshot_for_obs_and_all_rules(
    tmp_path,
    monitor_binary,
):
    initial_proc = tmp_path / "proc-initial"
    initial_proc.mkdir()
    empty_proc = tmp_path / "proc-empty"
    empty_proc.mkdir()
    proc_root = tmp_path / "proc"
    proc_root.symlink_to(initial_proc, target_is_directory=True)
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        [
            {"name": "obs"},
            {"name": "obs-studio", "executable_name": "obs"},
        ],
    )
    _add_proc(
        initial_proc,
        "410",
        comm="obs",
        cmdline="/app/bin/obs",
    )
    environ_fifo = initial_proc / "410" / "environ"
    environ_fifo.unlink()
    os.mkfifo(environ_fifo)

    def replace_proc_root_during_scan() -> None:
        with environ_fifo.open("wb"):
            replacement = tmp_path / "proc-replacement"
            replacement.symlink_to(empty_proc, target_is_directory=True)
            os.replace(replacement, proc_root)

    replacer = threading.Thread(target=replace_proc_root_during_scan, daemon=True)
    replacer.start()
    completed = subprocess.run(
        [str(monitor_binary), "--service"],
        check=True,
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_MONITOR_MAX_ITERATIONS": "1",
        },
        capture_output=True,
        text=True,
        timeout=2,
    )
    replacer.join(timeout=1)

    assert not replacer.is_alive()
    assert "[clipper-monitor] started clipper-observer-obs-studio pid=410" in completed.stderr
    assert "[clipper-monitor] started rule-0 pid=410" in completed.stderr
    assert "[clipper-monitor] started rule-1 pid=410" in completed.stderr


def test_monitor_host_does_not_mistake_clipper_libobs_engine_for_obs(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])
    _add_proc(
        proc_root,
        "411",
        comm="clipper-engine",
        cmdline="/app/libexec/clipper/clipper-engine --libobs",
        environ="CLIPPER_OBS_DATADIR=/app/share/obs",
        exe="/app/libexec/clipper/clipper-engine",
    )

    completed = subprocess.run(
        [str(monitor_binary), "--service"],
        check=True,
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_MONITOR_MAX_ITERATIONS": "1",
        },
        capture_output=True,
        text=True,
    )

    assert "clipper-observer-obs-studio" not in completed.stderr


def test_monitor_host_service_preserves_active_state_across_reload(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    rule = {
        "name": "Example",
        "capture_mode": "display_capture",
        "path": "/games/example/example-bin",
        "executable_path": "/games/example/example-bin",
    }
    _write_config(config_path, [rule])
    _add_proc(
        proc_root,
        "301",
        comm="example-bin",
        cmdline="/games/example/example-bin --fullscreen",
        exe="/games/example/example-bin",
        cwd="/games/example",
    )

    process = subprocess.Popen(
        [str(monitor_binary), "--service"],
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_MONITOR_INTERVAL_MS": "100",
            "CLIPPER_MONITOR_MAX_ITERATIONS": "6",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.15)
    config_path.write_text(
        json.dumps({"whitelist": [rule], "fps": 60}),
        encoding="utf-8",
    )

    stdout, stderr = process.communicate(timeout=2)

    assert process.returncode == 0, (stdout, stderr)
    assert "[clipper-monitor] reloaded config rules=1" in stderr
    assert (
        stderr.count(
            f"[clipper-monitor] started {_stable_path_rule_id(rule['executable_path'])} pid=301"
        )
        == 1
    )


def test_monitor_host_service_preserves_active_state_when_path_rules_reorder(
    tmp_path,
    monitor_binary,
):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = tmp_path / "config.json"
    active_rule = {
        "name": "Active",
        "capture_mode": "display_capture",
        "path": "/games/active/game-bin",
        "executable_path": "/games/active/game-bin",
    }
    inactive_rule = {
        "name": "Inactive",
        "capture_mode": "display_capture",
        "path": "/games/inactive/game-bin",
        "executable_path": "/games/inactive/game-bin",
    }
    active_rule_id = _stable_path_rule_id(active_rule["executable_path"])
    inactive_rule_id = _stable_path_rule_id(inactive_rule["executable_path"])
    _write_config(config_path, [active_rule, inactive_rule])
    _add_proc(
        proc_root,
        "302",
        comm="game-bin",
        cmdline="/games/active/game-bin --fullscreen",
        exe="/games/active/game-bin",
        cwd="/games/active",
    )

    process = subprocess.Popen(
        [str(monitor_binary), "--service"],
        env={
            "CLIPPER_CONFIG_FILE": str(config_path),
            "CLIPPER_PROC_ROOT": str(proc_root),
            "CLIPPER_MONITOR_INTERVAL_MS": "100",
            "CLIPPER_MONITOR_MAX_ITERATIONS": "6",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.15)
    _write_config(config_path, [inactive_rule, active_rule])

    stdout, stderr = process.communicate(timeout=2)

    assert process.returncode == 0, (stdout, stderr)
    assert "[clipper-monitor] reloaded config rules=2" in stderr
    assert stderr.count(f"[clipper-monitor] started {active_rule_id} pid=302") == 1
    assert f"[clipper-monitor] stopped {inactive_rule_id}" not in stderr
