from pathlib import Path
from types import SimpleNamespace

import game_icons
from game_icons import (
    cached_icon_path_for_executable,
    cached_icon_path_for_game,
    executable_path_for_game,
    icon_path_for_executable,
    icon_path_for_game,
)


def test_executable_path_prefers_matching_game_executable(tmp_path):
    install_path = tmp_path / "BloonsTD6"
    install_path.mkdir()
    crash_handler = install_path / "UnityCrashHandler64.exe"
    game_exe = install_path / "BloonsTD6.exe"
    crash_handler.write_text("", encoding="utf-8")
    game_exe.write_text("", encoding="utf-8")

    assert executable_path_for_game(
        {"name": "Bloons TD 6", "install_path": str(install_path)}
    ) == game_exe


def test_executable_path_finds_nested_proton_executable(tmp_path):
    install_path = tmp_path / "Game"
    nested_path = install_path / "Binaries" / "Win64"
    nested_path.mkdir(parents=True)
    game_exe = nested_path / "Game-Win64-Shipping.exe"
    game_exe.write_text("", encoding="utf-8")

    assert executable_path_for_game(
        {"name": "Game", "install_path": str(install_path)}
    ) == game_exe


def test_executable_path_ignores_unrelated_executable_bit_files(tmp_path):
    install_path = tmp_path / "Game"
    install_path.mkdir()
    video = install_path / "Game_Credits.bk2"
    game_exe = install_path / "Game.exe"
    video.write_text("", encoding="utf-8")
    video.chmod(0o755)
    game_exe.write_text("", encoding="utf-8")

    assert executable_path_for_game({"name": "Game", "install_path": str(install_path)}) == game_exe


def test_executable_path_uses_direct_process_path(tmp_path):
    executable = tmp_path / "game-bin"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)

    assert executable_path_for_game({"name": "Game", "path": str(executable)}) == executable


def test_icon_path_for_executable_extracts_png_and_caches_windows_icon(tmp_path, monkeypatch):
    executable = tmp_path / "Game.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(game_icons.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if "icotool" in args[0]:
            output_dir = args[args.index("--output") + 1]
            (Path(output_dir) / "icon_1_32x32x32.png").write_bytes(b"small")
            (Path(output_dir) / "icon_2_256x256x32.png").write_bytes(b"larger-icon")
        return SimpleNamespace(returncode=0, stdout=b"\x00\x00\x01\x00icon-bytes")

    monkeypatch.setattr(game_icons.subprocess, "run", fake_run)

    icon_path = icon_path_for_executable(executable)
    assert icon_path.endswith(".png")
    assert (tmp_path / "cache" / "clipper" / "game-icons").is_dir()
    assert calls[0] == (
        ["/usr/bin/wrestool", "--extract", "--type=14", str(executable)],
        {"check": False, "capture_output": True, "timeout": 5},
    )
    icotool_args, icotool_kwargs = calls[1]
    assert icotool_args[:3] == ["/usr/bin/icotool", "--extract", "--output"]
    assert icotool_args[4].endswith("icon.ico")
    assert icotool_kwargs == {"check": False, "capture_output": True, "timeout": 5}
    assert Path(icon_path).read_bytes() == b"larger-icon"

    calls.clear()
    assert icon_path_for_executable(executable) == icon_path
    assert calls == []


def test_icon_path_for_game_uses_existing_icon_path(tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"icon")

    assert icon_path_for_game({"icon_path": str(icon)}) == str(icon)


def test_cached_icon_path_for_game_uses_existing_png_without_discovery(tmp_path, monkeypatch):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"icon")
    monkeypatch.setattr(
        game_icons,
        "executable_path_for_game",
        lambda _game_data: (_ for _ in ()).throw(AssertionError("should not scan")),
    )

    assert cached_icon_path_for_game({"icon_path": str(icon)}) == str(icon)


def test_cached_icon_path_for_game_does_not_scan_install_directory(tmp_path, monkeypatch):
    install_path = tmp_path / "Game"
    install_path.mkdir()
    (install_path / "Game.exe").write_bytes(b"MZ")
    monkeypatch.setattr(
        game_icons,
        "executable_path_for_game",
        lambda _game_data: (_ for _ in ()).throw(AssertionError("should not scan")),
    )

    assert cached_icon_path_for_game({"name": "Game", "install_path": str(install_path)}) == ""


def test_cached_icon_path_for_executable_uses_existing_png_cache(tmp_path, monkeypatch):
    executable = tmp_path / "Game.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache_path = game_icons._cached_icon_path(executable)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"cached")

    assert cached_icon_path_for_executable(executable) == str(cache_path)


def test_icon_path_for_game_ignores_stale_ico_path(tmp_path, monkeypatch):
    icon = tmp_path / "icon.ico"
    executable = tmp_path / "Game.exe"
    icon.write_bytes(b"icon")
    executable.write_bytes(b"MZ")
    monkeypatch.setattr(game_icons, "icon_path_for_executable", lambda path: f"{path}.png")

    assert icon_path_for_game({"icon_path": str(icon), "executable_path": str(executable)}) == (
        f"{executable}.png"
    )


def test_icon_path_for_executable_skips_native_binary(tmp_path, monkeypatch):
    executable = tmp_path / "game-bin"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(game_icons.subprocess, "run", lambda *_args, **_kwargs: None)

    assert icon_path_for_executable(executable) == ""


def test_extracted_game_icon_cache_is_bounded(monkeypatch, tmp_path):
    cache_dir = tmp_path / "game-icons"
    cache_dir.mkdir()
    monkeypatch.setattr(game_icons, "MAX_GAME_ICON_CACHE_ENTRIES", 2)
    paths = []
    for index in range(3):
        path = cache_dir / f"icon-{index}.png"
        path.write_bytes(b"png")
        path.touch()
        paths.append(path)

    game_icons._prune_icon_cache(cache_dir, protected=paths[-1])

    assert paths[-1].exists() is True
    assert sum(path.exists() for path in paths) == 2


def test_extracted_game_icon_cache_obeys_total_byte_limit(monkeypatch, tmp_path):
    cache_dir = tmp_path / "game-icons"
    cache_dir.mkdir()
    monkeypatch.setattr(game_icons, "MAX_GAME_ICON_CACHE_BYTES", 15)
    paths = []
    for index in range(2):
        path = cache_dir / f"icon-{index}.png"
        path.write_bytes(b"x" * 10)
        paths.append(path)

    game_icons._prune_icon_cache(cache_dir, protected=paths[-1])

    assert paths[-1].exists() is True
    assert paths[0].exists() is False
