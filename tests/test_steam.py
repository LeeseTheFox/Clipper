"""
Tests for ui/steam.py — run headlessly (no GTK required).

Usage:
    PYTHONPATH=ui venv/bin/pytest tests/test_steam.py -v
"""

from pathlib import Path

import pytest
import steam as steam_mod
from steam import (
    CLIPPER_WRAPPER,
    SteamGame,
    SteamProcessCheckError,
    SteamRestartError,
    _is_steam_running,
    get_game_header_path,
    get_game_icon_path,
    get_installed_games,
    get_launch_options,
    inject_capture_wrapper,
    remove_capture_wrapper,
    restart_steam_around,
    set_launch_options,
    vkv_dump,
    vkv_parse,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_steam(
    tmp_path: Path,
    *,
    user_id: str = "12345678",
    games: list[dict] | None = None,
    launch_options: dict[str, str] | None = None,
) -> Path:
    """Build a minimal fake Steam directory tree under *tmp_path*."""
    steam_root = tmp_path / "steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)

    # libraryfolders.vdf — new (post-2021) format with nested "path" key
    lib_vdf = f'"libraryfolders"\n{{\n\t"1"\n\t{{\n\t\t"path"\t\t"{steam_root}"\n\t}}\n}}\n'
    (steamapps / "libraryfolders.vdf").write_text(lib_vdf, encoding="utf-8")

    # appmanifest_*.acf — one file per game
    for game in games or []:
        acf = (
            '"AppState"\n'
            "{\n"
            f'\t"appid"\t\t"{game["appid"]}"\n'
            f'\t"name"\t\t"{game["name"]}"\n'
            f'\t"installdir"\t\t"{game["installdir"]}"\n'
            "}\n"
        )
        (steamapps / f"appmanifest_{game['appid']}.acf").write_text(acf, encoding="utf-8")

    # localconfig.vdf
    userconfig_dir = steam_root / "userdata" / user_id / "config"
    userconfig_dir.mkdir(parents=True)

    app_lines: list[str] = []
    for appid, opts in (launch_options or {}).items():
        app_lines += [
            f'\t\t\t\t\t"{appid}"',
            "\t\t\t\t\t{",
            f'\t\t\t\t\t\t"LaunchOptions"\t\t"{opts}"',
            "\t\t\t\t\t}",
        ]

    lc = (
        '"UserLocalConfigStore"\n'
        "{\n"
        '\t"Software"\n'
        "\t{\n"
        '\t\t"Valve"\n'
        "\t\t{\n"
        '\t\t\t"Steam"\n'
        "\t\t\t{\n"
        '\t\t\t\t"apps"\n'
        "\t\t\t\t{\n" + ("\n".join(app_lines) + "\n" if app_lines else "") + "\t\t\t\t}\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    (userconfig_dir / "localconfig.vdf").write_text(lc, encoding="utf-8")

    return steam_root


@pytest.fixture()
def steam_not_running(monkeypatch):
    """Patch ``_is_steam_running`` to return False so writes succeed in tests."""
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda: False)


# ---------------------------------------------------------------------------
# 1. vkv_parse
# ---------------------------------------------------------------------------


def test_vkv_parse_basic_key_value():
    assert vkv_parse('"key" "value"') == {"key": "value"}


def test_vkv_parse_nested():
    text = '"outer"\n{\n\t"inner"\t\t"val"\n}'
    assert vkv_parse(text) == {"outer": {"inner": "val"}}


def test_vkv_parse_comments_ignored():
    text = '// This is a comment\n"key" "value"'
    assert vkv_parse(text) == {"key": "value"}


def test_vkv_parse_inline_comment_ignored():
    text = '"k1" "v1" // inline\n"k2" "v2"'
    assert vkv_parse(text) == {"k1": "v1", "k2": "v2"}


def test_vkv_parse_empty_string():
    assert vkv_parse("") == {}


def test_vkv_parse_empty_block():
    assert vkv_parse('"key"\n{}') == {"key": {}}


def test_vkv_parse_duplicate_keys_last_wins():
    text = '"k" "first"\n"k" "second"'
    assert vkv_parse(text) == {"k": "second"}


def test_vkv_parse_keys_lowercased():
    result = vkv_parse('"MyKey" "Value"')
    assert "mykey" in result
    assert "MyKey" not in result


def test_vkv_parse_values_preserve_case():
    result = vkv_parse('"key" "Hello World"')
    assert result["key"] == "Hello World"


def test_vkv_parse_deeply_nested():
    text = '"a"\n{\n\t"b"\n\t{\n\t\t"c"\t\t"deep"\n\t}\n}'
    assert vkv_parse(text) == {"a": {"b": {"c": "deep"}}}


def test_vkv_parse_multiple_top_level_pairs():
    text = '"k1" "v1"\n"k2" "v2"'
    assert vkv_parse(text) == {"k1": "v1", "k2": "v2"}


def test_vkv_parse_escape_sequence_in_value():
    # Backslash-escaped quote inside a string
    text = r'"key" "val\"ue"'
    assert vkv_parse(text) == {"key": 'val"ue'}


def test_vkv_parse_realistic_localconfig():
    """A realistic localconfig.vdf snippet round-trips correctly."""
    text = (
        '"UserLocalConfigStore"\n'
        "{\n"
        '\t"Software"\n'
        "\t{\n"
        '\t\t"Valve"\n'
        "\t\t{\n"
        '\t\t\t"Steam"\n'
        "\t\t\t{\n"
        '\t\t\t\t"apps"\n'
        "\t\t\t\t{\n"
        '\t\t\t\t\t"730"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"LaunchOptions"\t\t"obs-gamecapture %command%"\n'
        "\t\t\t\t\t}\n"
        "\t\t\t\t}\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    result = vkv_parse(text)
    opts = result["userlocalconfigstore"]["software"]["valve"]["steam"]["apps"]["730"][
        "launchoptions"
    ]
    assert opts == "obs-gamecapture %command%"


# ---------------------------------------------------------------------------
# 2. vkv_dump — output format and round-trips
# ---------------------------------------------------------------------------


def test_vkv_dump_flat_contains_key_and_value():
    text = vkv_dump({"key": "value"})
    assert '"key"' in text
    assert '"value"' in text


def test_vkv_dump_nested_contains_braces():
    text = vkv_dump({"outer": {"inner": "val"}})
    assert "{" in text and "}" in text
    assert '"inner"' in text


def test_vkv_dump_roundtrip_flat():
    data = {"a": "1", "b": "2"}
    assert vkv_parse(vkv_dump(data)) == data


def test_vkv_dump_roundtrip_nested():
    data = {"root": {"child": {"leaf": "value"}, "sibling": "other"}}
    assert vkv_parse(vkv_dump(data)) == data


def test_vkv_dump_roundtrip_empty_nested():
    data = {"key": {}}
    assert vkv_parse(vkv_dump(data)) == data


def test_vkv_dump_indentation_increases_with_depth():
    text = vkv_dump({"outer": {"inner": "val"}})
    # Inner key should be indented more than outer key
    outer_indent = len(text.split('"outer"')[0].split("\n")[-1])
    inner_line = [ln for ln in text.splitlines() if '"inner"' in ln][0]
    inner_indent = len(inner_line) - len(inner_line.lstrip("\t"))
    assert inner_indent > outer_indent


# ---------------------------------------------------------------------------
# 3. get_installed_games
# ---------------------------------------------------------------------------


def test_get_installed_games_returns_empty_when_no_steam(monkeypatch):
    """When find_steam_root returns None, the function returns an empty list."""
    monkeypatch.setattr(steam_mod, "find_steam_root", lambda: None)
    assert get_installed_games(steam_root=None) == []


def test_get_installed_games_returns_empty_when_missing_library_vdf(tmp_path):
    steam_root = tmp_path / "steam"
    (steam_root / "steamapps").mkdir(parents=True)
    assert get_installed_games(steam_root) == []


def test_get_installed_games_single_game(tmp_path):
    steam_root = _make_fake_steam(
        tmp_path,
        games=[{"appid": "730", "name": "Counter-Strike 2", "installdir": "cs2"}],
    )
    games = get_installed_games(steam_root)
    assert len(games) == 1
    assert games[0].appid == "730"
    assert games[0].name == "Counter-Strike 2"
    assert "cs2" in games[0].install_path


def test_get_installed_games_includes_local_steam_artwork(tmp_path):
    steam_root = _make_fake_steam(
        tmp_path,
        games=[{"appid": "730", "name": "Counter-Strike 2", "installdir": "cs2"}],
    )
    artwork = steam_root / "appcache" / "librarycache" / "730" / "library_600x900.jpg"
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"jpg")

    games = get_installed_games(steam_root)
    assert games[0].icon_path == str(artwork)


def test_get_game_icon_path_prefers_custom_grid_icon(tmp_path):
    steam_root = tmp_path / "steam"
    custom_icon = steam_root / "userdata" / "123" / "config" / "grid" / "730_icon.png"
    library_artwork = steam_root / "appcache" / "librarycache" / "730" / "library_600x900.jpg"
    custom_icon.parent.mkdir(parents=True)
    library_artwork.parent.mkdir(parents=True)
    custom_icon.write_bytes(b"png")
    library_artwork.write_bytes(b"jpg")

    assert get_game_icon_path("730", steam_root) == str(custom_icon)


def test_get_game_icon_path_prefers_app_icon_jpeg_over_library_artwork(tmp_path):
    steam_root = tmp_path / "steam"
    icon = steam_root / "appcache" / "librarycache" / "730" / "abcdef.jpg"
    library_artwork = steam_root / "appcache" / "librarycache" / "730" / "library_600x900.jpg"
    logo = steam_root / "appcache" / "librarycache" / "730" / "logo.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"jpg")
    library_artwork.write_bytes(b"poster")
    logo.write_bytes(b"logo")

    assert get_game_icon_path("730", steam_root) == str(icon)


def test_get_game_icon_path_does_not_use_library_logo(tmp_path):
    steam_root = tmp_path / "steam"
    logo = steam_root / "appcache" / "librarycache" / "730" / "logo.png"
    logo.parent.mkdir(parents=True)
    logo.write_bytes(b"logo")

    assert get_game_icon_path("730", steam_root) == ""


def test_get_game_icon_path_falls_back_to_header(tmp_path):
    steam_root = tmp_path / "steam"
    header = steam_root / "appcache" / "librarycache" / "730" / "header.jpg"
    header.parent.mkdir(parents=True)
    header.write_bytes(b"jpg")

    assert get_game_icon_path("730", steam_root) == str(header)


def test_get_game_icon_path_finds_nested_library_cache_artwork(tmp_path):
    steam_root = tmp_path / "steam"
    artwork = (
        steam_root
        / "appcache"
        / "librarycache"
        / "730"
        / "abcdef"
        / "library_600x900.jpg"
    )
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"jpg")

    assert get_game_icon_path("730", steam_root) == str(artwork)


def test_get_game_header_path_prefers_horizontal_header(tmp_path):
    steam_root = tmp_path / "steam"
    cache = steam_root / "appcache" / "librarycache" / "730"
    icon = cache / "abcdef.jpg"
    portrait = cache / "library_600x900.jpg"
    header = cache / "header.jpg"
    cache.mkdir(parents=True)
    icon.write_bytes(b"icon")
    portrait.write_bytes(b"portrait")
    header.write_bytes(b"header")

    assert get_game_header_path("730", steam_root) == str(header)


def test_get_game_header_path_finds_nested_steam_cache_layout(tmp_path):
    steam_root = tmp_path / "steam"
    header = (
        steam_root
        / "appcache"
        / "librarycache"
        / "730"
        / "content-hash"
        / "library_header.jpg"
    )
    header.parent.mkdir(parents=True)
    header.write_bytes(b"header")

    assert get_game_header_path("730", steam_root) == str(header)


def test_get_game_header_path_does_not_fall_back_to_small_icon(tmp_path):
    steam_root = tmp_path / "steam"
    icon = steam_root / "appcache" / "librarycache" / "730" / "abcdef.jpg"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"icon")

    assert get_game_header_path("730", steam_root) == ""


def test_get_installed_games_sorted_by_name(tmp_path):
    steam_root = _make_fake_steam(
        tmp_path,
        games=[
            {"appid": "730", "name": "Zebra Game", "installdir": "zebra"},
            {"appid": "440", "name": "Alpha Game", "installdir": "alpha"},
        ],
    )
    games = get_installed_games(steam_root)
    assert len(games) == 2
    assert games[0].name == "Alpha Game"
    assert games[1].name == "Zebra Game"


def test_get_installed_games_returns_steam_game_dataclass(tmp_path):
    steam_root = _make_fake_steam(
        tmp_path,
        games=[{"appid": "1", "name": "Test", "installdir": "test"}],
    )
    games = get_installed_games(steam_root)
    assert isinstance(games[0], SteamGame)


def test_get_installed_games_install_path_under_common(tmp_path):
    steam_root = _make_fake_steam(
        tmp_path,
        games=[{"appid": "1", "name": "MyGame", "installdir": "mygame"}],
    )
    games = get_installed_games(steam_root)
    assert "common" in games[0].install_path
    assert "mygame" in games[0].install_path


def test_get_installed_games_skips_corrupt_acf(tmp_path):
    """A garbled ACF file must not crash the scan."""
    steam_root = _make_fake_steam(
        tmp_path,
        games=[{"appid": "730", "name": "Good Game", "installdir": "good"}],
    )
    # Drop in a corrupt ACF alongside the valid one
    bad_acf = steam_root / "steamapps" / "appmanifest_0.acf"
    bad_acf.write_text("{{{{ not valid vkv", encoding="utf-8")
    games = get_installed_games(steam_root)
    assert any(g.name == "Good Game" for g in games)


# ---------------------------------------------------------------------------
# 4. get_launch_options
# ---------------------------------------------------------------------------


def test_get_launch_options_returns_existing_value(tmp_path):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "obs-gamecapture %command%"})
    assert get_launch_options("730", steam_root) == "obs-gamecapture %command%"


def test_get_launch_options_returns_empty_for_missing_appid(tmp_path):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "some-option"})
    assert get_launch_options("999", steam_root) == ""


def test_get_launch_options_returns_empty_when_no_options_set(tmp_path):
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    assert get_launch_options("730", steam_root) == ""


def test_get_launch_options_raises_when_no_localconfig(tmp_path):
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    with pytest.raises(FileNotFoundError):
        get_launch_options("730", steam_root)


def test_get_launch_options_prefers_lowest_user_id(tmp_path):
    """When multiple user directories exist, the lowest numeric ID wins."""
    steam_root = tmp_path / "steam"
    (steam_root / "steamapps").mkdir(parents=True)

    lc_text = (
        '"UserLocalConfigStore"\n'
        "{\n"
        '\t"Software"\n'
        "\t{\n"
        '\t\t"Valve"\n'
        "\t\t{\n"
        '\t\t\t"Steam"\n'
        "\t\t\t{\n"
        '\t\t\t\t"apps"\n'
        "\t\t\t\t{\n"
        '\t\t\t\t\t"730"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"LaunchOptions"\t\t"{marker}"\n'
        "\t\t\t\t\t}\n"
        "\t\t\t\t}\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )

    for uid, marker in [("99999", "from-99999"), ("12345", "from-12345")]:
        d = steam_root / "userdata" / uid / "config"
        d.mkdir(parents=True)
        (d / "localconfig.vdf").write_text(lc_text.replace("{marker}", marker), encoding="utf-8")

    assert get_launch_options("730", steam_root) == "from-12345"


# ---------------------------------------------------------------------------
# 5. set_launch_options
# ---------------------------------------------------------------------------


def test_set_launch_options_persists_to_disk(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    set_launch_options("730", "obs-gamecapture %command%", steam_root)
    assert get_launch_options("730", steam_root) == "obs-gamecapture %command%"


def test_set_launch_options_no_tmp_leftover(tmp_path, steam_not_running):
    """Atomic write must leave no .tmp files behind."""
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    set_launch_options("730", "test", steam_root)

    userconfig_dir = steam_root / "userdata" / "12345678" / "config"
    assert list(userconfig_dir.glob("*.tmp")) == []


def test_set_launch_options_localconfig_is_valid_vkv_after_write(tmp_path, steam_not_running):
    """The written file must be parseable VKV."""
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    set_launch_options("730", "test", steam_root)

    userconfig = steam_root / "userdata" / "12345678" / "config" / "localconfig.vdf"
    data = vkv_parse(userconfig.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data  # non-empty, valid


def test_set_launch_options_preserves_other_apps(tmp_path, steam_not_running):
    """Modifying one app's options must not clobber another app's options."""
    steam_root = _make_fake_steam(
        tmp_path,
        launch_options={"730": "original-730", "440": "original-440"},
    )
    set_launch_options("730", "new-730", steam_root)

    assert get_launch_options("730", steam_root) == "new-730"
    assert get_launch_options("440", steam_root) == "original-440"


def test_set_launch_options_overwrites_existing(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "old-option"})
    set_launch_options("730", "new-option", steam_root)
    assert get_launch_options("730", steam_root) == "new-option"


def test_set_launch_options_raises_if_steam_running(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda: True)
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    with pytest.raises(RuntimeError, match="[Ss]team"):
        set_launch_options("730", "test", steam_root)


# ---------------------------------------------------------------------------
# 6. inject_capture_wrapper
# ---------------------------------------------------------------------------


def test_inject_adds_wrapper(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    added = inject_capture_wrapper("730", steam_root)
    assert added is True
    assert get_launch_options("730", steam_root) == CLIPPER_WRAPPER


def test_inject_prepends_to_existing_options(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "%command%"})
    inject_capture_wrapper("730", steam_root)
    opts = get_launch_options("730", steam_root)
    assert opts.startswith(CLIPPER_WRAPPER)
    assert "%command%" in opts


def test_inject_idempotent_returns_false(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": f"{CLIPPER_WRAPPER} %command%"})
    added = inject_capture_wrapper("730", steam_root)
    assert added is False


def test_inject_no_double_add(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    inject_capture_wrapper("730", steam_root)
    inject_capture_wrapper("730", steam_root)
    assert get_launch_options("730", steam_root).count(CLIPPER_WRAPPER) == 1


def test_inject_raises_if_steam_running(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda: True)
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    with pytest.raises(RuntimeError):
        inject_capture_wrapper("730", steam_root)


# ---------------------------------------------------------------------------
# 7. remove_capture_wrapper
# ---------------------------------------------------------------------------


def test_remove_removes_wrapper(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": f"{CLIPPER_WRAPPER} %command%"})
    removed = remove_capture_wrapper("730", steam_root)
    assert removed is True
    opts = get_launch_options("730", steam_root)
    assert CLIPPER_WRAPPER not in opts.split()


def test_remove_preserves_remaining_options(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(
        tmp_path, launch_options={"730": f"{CLIPPER_WRAPPER} --extra %command%"}
    )
    remove_capture_wrapper("730", steam_root)
    opts = get_launch_options("730", steam_root)
    assert "--extra" in opts
    assert "%command%" in opts


def test_remove_noop_if_wrapper_absent(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "%command%"})
    removed = remove_capture_wrapper("730", steam_root)
    assert removed is False
    # Options should be unchanged
    assert get_launch_options("730", steam_root) == "%command%"


def test_remove_raises_if_steam_running(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda: True)
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": f"{CLIPPER_WRAPPER} %command%"})
    with pytest.raises(RuntimeError):
        remove_capture_wrapper("730", steam_root)


# ---------------------------------------------------------------------------
# 8. Steam-running check (_is_steam_running)
# ---------------------------------------------------------------------------


def test_is_steam_running_returns_bool():
    """`_is_steam_running()` must return a plain bool without raising."""
    result = _is_steam_running()
    assert isinstance(result, bool)


def test_is_steam_running_uses_host_processes_in_flatpak(monkeypatch):
    class HostManager:
        def available(self):
            return True

        def list_processes(self):
            return [
                {"pid": "10", "comm": "unrelated"},
                {"pid": "11", "comm": "steamwebhelper"},
            ]

    monkeypatch.setattr(steam_mod, "HostMonitorManager", HostManager)

    assert _is_steam_running() is True


def test_is_steam_running_uses_local_proc_for_native_builds(tmp_path, monkeypatch):
    class NativeManager:
        def available(self):
            return False

    monkeypatch.setattr(steam_mod, "HostMonitorManager", NativeManager)
    proc_dir = tmp_path / "321"
    proc_dir.mkdir()
    (proc_dir / "comm").write_text("steam\n", encoding="utf-8")

    assert _is_steam_running(tmp_path) is True


def test_is_steam_running_fails_closed_when_host_list_is_unavailable(monkeypatch):
    class FailedHostManager:
        def available(self):
            return True

        def list_processes(self):
            return None

    monkeypatch.setattr(steam_mod, "HostMonitorManager", FailedHostManager)

    with pytest.raises(SteamProcessCheckError, match="host"):
        _is_steam_running()


def test_restart_steam_around_uses_host_helper_and_runs_action(monkeypatch):
    calls = []

    class Result:
        ok = True
        stderr = ""

    class HostManager:
        def available(self):
            return True

        def stop_steam(self):
            calls.append("stop")
            return Result()

        def start_steam(self):
            calls.append("start")
            return Result()

    monkeypatch.setattr(steam_mod, "HostMonitorManager", HostManager)

    result = restart_steam_around(lambda: calls.append("change") or "done")

    assert result == "done"
    assert calls == ["stop", "change", "start"]


def test_restart_steam_around_relaunches_after_action_failure(monkeypatch):
    calls = []

    class Result:
        ok = True
        stderr = ""

    class HostManager:
        def available(self):
            return True

        def stop_steam(self):
            calls.append("stop")
            return Result()

        def start_steam(self):
            calls.append("start")
            return Result()

    def fail():
        calls.append("change")
        raise ValueError("write failed")

    monkeypatch.setattr(steam_mod, "HostMonitorManager", HostManager)

    with pytest.raises(ValueError, match="write failed"):
        restart_steam_around(fail)
    assert calls == ["stop", "change", "start"]


def test_restart_steam_around_stops_when_shutdown_fails(monkeypatch):
    class Result:
        ok = False
        stderr = "shutdown failed"

    class HostManager:
        def available(self):
            return True

        def stop_steam(self):
            return Result()

    monkeypatch.setattr(steam_mod, "HostMonitorManager", HostManager)

    with pytest.raises(SteamRestartError, match="shutdown failed"):
        restart_steam_around(lambda: None)


def test_steam_running_mock_triggers_runtime_error(monkeypatch):
    """When the mock says Steam is running, set_launch_options must raise."""
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda: True)

    # We need a valid steam root for this check to reach the RuntimeError.
    # Use a dummy path — the error fires before any I/O.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "steam"
        (root / "steamapps").mkdir(parents=True)
        lc_text = (
            '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
            '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n'
            "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n"
        )
        uc = root / "userdata" / "1" / "config"
        uc.mkdir(parents=True)
        (uc / "localconfig.vdf").write_text(lc_text, encoding="utf-8")

        with pytest.raises(RuntimeError, match="[Ss]team"):
            set_launch_options("730", "test", root)


def test_steam_not_running_mock_allows_write(monkeypatch, tmp_path):
    """When the mock says Steam is not running, set_launch_options succeeds."""
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda: False)
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    set_launch_options("730", "hello", steam_root)
    assert get_launch_options("730", steam_root) == "hello"
