"""
Tests for ui/steam.py — run headlessly (no GTK required).

Usage:
    PYTHONPATH=ui venv/bin/pytest tests/test_steam.py -v
"""

from pathlib import Path

import pytest
import steam as steam_mod
from config import ClipperConfig
from game_capture import EnvironmentKind
from steam import (
    CLIPPER_WRAPPER,
    AmbiguousSteamAccountError,
    CaptureProviderUnverifiedError,
    SteamGame,
    SteamInstallation,
    SteamProcessCheckError,
    SteamRestartError,
    _is_steam_running,
    compose_capture_options,
    discover_steam_installations,
    get_game_header_path,
    get_game_icon_path,
    get_installed_games,
    get_launch_options,
    inject_capture_wrapper,
    prepare_capture_injection,
    reconcile_pending_game_capture_change,
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
        opts = opts.replace("\\", "\\\\").replace('"', '\\"')
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
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda *args, **kwargs: False)


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
    artwork = steam_root / "appcache" / "librarycache" / "730" / "abcdef" / "library_600x900.jpg"
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
        steam_root / "appcache" / "librarycache" / "730" / "content-hash" / "library_header.jpg"
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


def test_discover_native_and_flatpak_installations_and_deduplicate_symlink(tmp_path):
    native = tmp_path / ".local/share/Steam"
    native.mkdir(parents=True)
    alias = tmp_path / ".steam/steam"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(native, target_is_directory=True)
    flatpak = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    flatpak.mkdir(parents=True)

    installations = discover_steam_installations(tmp_path)

    assert [item.environment for item in installations] == [
        EnvironmentKind.NATIVE_STEAM,
        EnvironmentKind.FLATPAK_STEAM_USER,
    ]
    assert len({item.stable_id for item in installations}) == 2


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


def test_get_launch_options_requires_choice_when_accounts_are_ambiguous(tmp_path):
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

    with pytest.raises(AmbiguousSteamAccountError):
        get_launch_options("730", steam_root)
    assert get_launch_options("730", steam_root, account_id="12345") == "from-12345"


def test_loginusers_steamid64_selects_matching_userdata_account(tmp_path):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "recent"})
    other = steam_root / "userdata" / "99999" / "config"
    other.mkdir(parents=True)
    source = steam_root / "userdata" / "12345678" / "config" / "localconfig.vdf"
    other.joinpath("localconfig.vdf").write_text(
        source.read_text(encoding="utf-8").replace("recent", "other"),
        encoding="utf-8",
    )
    loginusers = steam_root / "config" / "loginusers.vdf"
    loginusers.parent.mkdir(parents=True)
    steam_id64 = 76561197960265728 + 12345678
    loginusers.write_text(
        f'"users"\n{{\n\t"{steam_id64}"\n\t{{\n'
        '\t\t"AccountName"\t\t"recent-user"\n'
        '\t\t"MostRecent"\t\t"1"\n\t}\n}\n',
        encoding="utf-8",
    )

    assert get_launch_options("730", steam_root) == "recent"


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
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda *args, **kwargs: True)
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    with pytest.raises(RuntimeError, match="[Ss]team"):
        set_launch_options("730", "test", steam_root)


# ---------------------------------------------------------------------------
# 6. inject_capture_wrapper
# ---------------------------------------------------------------------------


def test_inject_adds_wrapper(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    added = inject_capture_wrapper("730", steam_root, wrapper_path="/usr/bin/obs-gamecapture")
    assert added is True
    assert get_launch_options("730", steam_root) == (
        compose_capture_options("%command%", "/usr/bin/obs-gamecapture")
    )


def test_inject_prepends_to_existing_options(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "%command%"})
    inject_capture_wrapper("730", steam_root, wrapper_path="/usr/bin/obs-gamecapture")
    opts = get_launch_options("730", steam_root)
    assert opts == compose_capture_options("%command%", "/usr/bin/obs-gamecapture")


def test_inject_idempotent_returns_false(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(
        tmp_path,
        launch_options={"730": compose_capture_options("%command%", "/usr/bin/obs-gamecapture")},
    )
    added = inject_capture_wrapper("730", steam_root, wrapper_path="/usr/bin/obs-gamecapture")
    assert added is False


def test_inject_no_double_add(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    inject_capture_wrapper("730", steam_root, wrapper_path="/usr/bin/obs-gamecapture")
    with pytest.raises(CaptureProviderUnverifiedError):
        inject_capture_wrapper("730", steam_root)
    assert get_launch_options("730", steam_root).count("/usr/bin/obs-gamecapture") == 1


def test_inject_raises_if_steam_running(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda *args, **kwargs: True)
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    with pytest.raises(RuntimeError):
        inject_capture_wrapper("730", steam_root, wrapper_path="/usr/bin/obs-gamecapture")


def test_unverified_injection_never_writes(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "keep  spacing"})
    with pytest.raises(CaptureProviderUnverifiedError):
        inject_capture_wrapper("730", steam_root)
    assert get_launch_options("730", steam_root) == "keep  spacing"


@pytest.mark.parametrize(
    "original",
    [
        "MANGOHUD=1  %command%   -novid",
        'env FOO="two words" %command%',
        "--аргумент  значення",
        'gamescope -- %command% --nested="a b"',
    ],
)
def test_compose_capture_options_preserves_original_exactly(original):
    applied = compose_capture_options(original, "/home/user/My Tools/obs-gamecapture")
    prefix = steam_mod.quote_wrapper_path("/home/user/My Tools/obs-gamecapture")
    if "%command%" in original:
        assert applied.replace(prefix + " ", "", 1) == original
    else:
        assert applied == f"{prefix} %command% {original}"


@pytest.mark.parametrize(
    "wrapper_path",
    [
        "/home/user/$TOOLS/obs-gamecapture",
        "/home/user/`tools`/obs-gamecapture",
        "/home/user/tools\\obs-gamecapture",
        '/home/user/"tools"/obs-gamecapture',
        "/home/user/tools\nobs-gamecapture",
    ],
)
def test_compose_capture_options_quotes_shell_active_wrapper_paths(wrapper_path):
    import shlex

    if "\n" in wrapper_path:
        with pytest.raises(ValueError):
            compose_capture_options("%command%", wrapper_path)
        return
    parts = shlex.split(compose_capture_options("%command%", wrapper_path))
    assert parts[3] == wrapper_path


# ---------------------------------------------------------------------------
# 7. remove_capture_wrapper
# ---------------------------------------------------------------------------


def test_remove_removes_wrapper(tmp_path, steam_not_running):
    original = "MANGOHUD=1  %command%"
    mutation = prepare_capture_injection(original, "/usr/bin/obs-gamecapture")
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    set_launch_options("730", mutation.applied, steam_root)
    removed = remove_capture_wrapper(
        "730",
        steam_root,
        integration={
            "managed_launch_options": True,
            "original_launch_options": original,
            "applied_launch_options": mutation.applied,
            "wrapper_prefix": mutation.wrapper_prefix,
        },
    )
    assert removed is True
    assert get_launch_options("730", steam_root) == original


def test_remove_preserves_remaining_options(tmp_path, steam_not_running):
    applied = compose_capture_options("%command%", "/usr/bin/obs-gamecapture")
    edited = f"{applied} --extra"
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    set_launch_options("730", edited, steam_root)
    remove_capture_wrapper(
        "730",
        steam_root,
        integration={
            "managed_launch_options": True,
            "original_launch_options": "",
            "applied_launch_options": applied,
            "wrapper_prefix": steam_mod.quote_wrapper_path("/usr/bin/obs-gamecapture"),
        },
    )
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
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda *args, **kwargs: True)
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": f"{CLIPPER_WRAPPER} %command%"})
    with pytest.raises(RuntimeError):
        remove_capture_wrapper(
            "730",
            steam_root,
            integration={
                "managed_launch_options": True,
                "original_launch_options": "",
                "applied_launch_options": f"{CLIPPER_WRAPPER} %command%",
                "wrapper_prefix": "obs-gamecapture",
            },
        )


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

        def stop_steam(self, environment):
            assert environment is EnvironmentKind.NATIVE_STEAM
            calls.append("stop")
            return Result()

        def start_steam(self, environment):
            assert environment is EnvironmentKind.NATIVE_STEAM
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

        def stop_steam(self, environment):
            assert environment is EnvironmentKind.NATIVE_STEAM
            calls.append("stop")
            return Result()

        def start_steam(self, environment):
            assert environment is EnvironmentKind.NATIVE_STEAM
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

        def stop_steam(self, environment):
            assert environment is EnvironmentKind.NATIVE_STEAM
            return Result()

    monkeypatch.setattr(steam_mod, "HostMonitorManager", HostManager)

    with pytest.raises(SteamRestartError, match="shutdown failed"):
        restart_steam_around(lambda: None)


def test_steam_running_mock_triggers_runtime_error(monkeypatch):
    """When the mock says Steam is running, set_launch_options must raise."""
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda *args, **kwargs: True)

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
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda *args, **kwargs: False)
    steam_root = _make_fake_steam(tmp_path, launch_options={})
    set_launch_options("730", "hello", steam_root)
    assert get_launch_options("730", steam_root) == "hello"


def _bundled_wrapper() -> str:
    return (
        "/home/test/.var/app/io.github.leesethefox.Clipper/data/game-capture/"
        "current/bin/clipper-gamecapture"
    )


@pytest.mark.parametrize("home_prefix", ["/home", "/var/home"])
def test_flatpak_library_paths_are_mapped_to_the_host_data_root(tmp_path, home_prefix):
    root = _make_fake_steam(
        tmp_path, games=[{"appid": "730", "name": "Game", "installdir": "game"}]
    )
    visible = Path("/home/test/.local/share/Steam")
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{home_prefix}/test/.local/share/Steam" }} }}'
    )
    installation = SteamInstallation(
        "flatpak:test",
        EnvironmentKind.FLATPAK_STEAM_USER,
        root,
        game_visible_data_root=visible,
    )
    games = get_installed_games(installation, account_id="12345678")
    assert len(games) == 1
    assert games[0].install_path == str(root / "steamapps/common/game")
    assert games[0].steam_account_id == "12345678"


def test_legacy_wrapper_is_upgraded_and_removed_without_host_dependency(
    tmp_path, steam_not_running
):
    root = _make_fake_steam(tmp_path, launch_options={"730": "obs-gamecapture %command% -novid"})
    config = ClipperConfig(tmp_path / "config.json")
    previous = {"appid": "730", "name": "Game", "capture_mode": "game_capture"}
    config.set("whitelist", [previous])
    entry = dict(previous, steam_installation="native:test", steam_account_id="12345678")
    updated = steam_mod.apply_game_capture_update_transaction(
        config, previous, entry, _bundled_wrapper(), root
    )
    assert not get_launch_options("730", root).startswith("obs-gamecapture")
    steam_mod.apply_game_capture_remove_transaction(config, updated, root)
    assert get_launch_options("730", root) == "%command% -novid"
    assert config.get("whitelist") == []


def test_existing_managed_wrapper_migration_restores_original_options(tmp_path, steam_not_running):
    root = _make_fake_steam(tmp_path, launch_options={"730": "FOO=1 %command% -arg"})
    config = ClipperConfig(tmp_path / "config.json")
    entry = {
        "appid": "730",
        "name": "Game",
        "capture_mode": "game_capture",
        "steam_installation": "native:test",
        "steam_account_id": "12345678",
    }
    previous = steam_mod.apply_game_capture_add_transaction(config, entry, "/old/wrapper", root)
    updated = steam_mod.apply_game_capture_update_transaction(
        config, previous, previous, _bundled_wrapper(), root
    )
    assert "/old/wrapper" not in get_launch_options("730", root)
    steam_mod.apply_game_capture_remove_transaction(config, updated, root)
    assert get_launch_options("730", root) == "FOO=1 %command% -arg"


def test_add_transaction_persists_exact_ownership_and_clears_journal(tmp_path, steam_not_running):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": ""})
    set_launch_options("730", 'MANGOHUD=1  %command%  "two words"', steam_root)
    config = ClipperConfig(tmp_path / "config.json")
    entry = {
        "name": "Game",
        "appid": "730",
        "capture_mode": "game_capture",
        "steam_installation": "native:test",
        "steam_environment": "native_steam",
        "steam_account_id": "12345678",
    }

    committed = steam_mod.apply_game_capture_add_transaction(
        config, entry, _bundled_wrapper(), steam_root
    )

    integration = committed["game_capture_integration"]
    assert integration["original_launch_options"] == ('MANGOHUD=1  %command%  "two words"')
    assert (
        get_launch_options("730", steam_root).replace(integration["wrapper_prefix"] + " ", "", 1)
        == integration["original_launch_options"]
    )
    assert config.get("pending_game_capture_change") is None
    assert config.get("whitelist") == [committed]


def test_update_transaction_replaces_entry_only_after_launch_option_commit(
    tmp_path, steam_not_running
):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "MANGOHUD=1"})
    config = ClipperConfig(tmp_path / "config.json")
    previous = {
        "name": "Game",
        "appid": "730",
        "capture_mode": "display_capture",
        "steam_installation": "native:test",
        "steam_environment": "native_steam",
        "steam_account_id": "12345678",
    }
    config.set("whitelist", [previous])
    replacement = dict(previous, capture_mode="game_capture")

    committed = steam_mod.apply_game_capture_update_transaction(
        config, previous, replacement, _bundled_wrapper(), steam_root
    )

    assert config.get("whitelist") == [committed]
    assert committed["capture_mode"] == "game_capture"
    assert get_launch_options("730", steam_root) == compose_capture_options(
        "MANGOHUD=1", _bundled_wrapper()
    )
    assert config.get("pending_game_capture_change") is None


@pytest.mark.parametrize("running", [False, True])
def test_interrupted_add_reconciliation_rolls_back_untracked_wrapper(
    tmp_path, steam_not_running, monkeypatch, running
):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "keep  %command%"})
    config_path = tmp_path / "config.json"
    config = ClipperConfig(config_path)
    real_set = config.set

    def fail_whitelist(key, value):
        if key == "whitelist":
            raise OSError("injected persistence failure")
        return real_set(key, value)

    monkeypatch.setattr(config, "set", fail_whitelist)
    entry = {
        "name": "Game",
        "appid": "730",
        "capture_mode": "game_capture",
        "steam_installation": "native:test",
        "steam_environment": "native_steam",
        "steam_account_id": "12345678",
    }
    with pytest.raises(OSError, match="injected"):
        steam_mod.apply_game_capture_add_transaction(config, entry, _bundled_wrapper(), steam_root)
    assert steam_mod.quote_wrapper_path(_bundled_wrapper()) in get_launch_options("730", steam_root)

    recovered = ClipperConfig(config_path)
    installation = SteamInstallation("native:test", EnvironmentKind.NATIVE_STEAM, steam_root)
    monkeypatch.setattr(steam_mod, "discover_steam_installations", lambda: [installation])
    monkeypatch.setattr(steam_mod.game_capture, "ensure_payload", lambda: None)
    monkeypatch.setattr(steam_mod, "_is_steam_running", lambda **kwargs: running)
    updates = steam_mod.capture_updates(recovered)
    if running:
        assert updates == [({}, installation, True)]

        def restart(action, environment):
            assert environment == installation.environment
            monkeypatch.setattr(steam_mod, "_is_steam_running", lambda **kwargs: False)
            action()

        monkeypatch.setattr(steam_mod, "restart_steam_around", restart)
        steam_mod.apply_capture_updates(recovered, updates)
    else:
        assert updates == []
    assert get_launch_options("730", steam_root) == "keep  %command%"
    assert recovered.get("pending_game_capture_change") is None


def test_interrupted_update_reconciliation_keeps_old_entry_and_options(
    tmp_path, steam_not_running, monkeypatch
):
    steam_root = _make_fake_steam(tmp_path, launch_options={"730": "keep"})
    config_path = tmp_path / "config.json"
    config = ClipperConfig(config_path)
    previous = {
        "name": "Game",
        "appid": "730",
        "capture_mode": "display_capture",
        "steam_installation": "native:test",
        "steam_environment": "native_steam",
        "steam_account_id": "12345678",
    }
    config.set("whitelist", [previous])
    real_set = config.set

    def fail_replacement(key, value):
        if key == "whitelist":
            raise OSError("injected replacement failure")
        return real_set(key, value)

    monkeypatch.setattr(config, "set", fail_replacement)
    with pytest.raises(OSError, match="replacement"):
        steam_mod.apply_game_capture_update_transaction(
            config,
            previous,
            dict(previous, capture_mode="game_capture"),
            _bundled_wrapper(),
            steam_root,
        )

    recovered = ClipperConfig(config_path)
    installation = SteamInstallation("native:test", EnvironmentKind.NATIVE_STEAM, steam_root)
    assert reconcile_pending_game_capture_change(recovered, [installation]) == ("rolled_back")
    assert get_launch_options("730", steam_root) == "keep"
    assert recovered.get("whitelist") == [previous]


def test_managed_removal_refuses_unrelated_user_edit():
    with pytest.raises(steam_mod.LaunchOptionConflictError):
        steam_mod.remove_managed_capture_options(
            "gamescope %command%",
            original="%command%",
            applied='"/managed/wrapper" %command%',
            wrapper_prefix='"/managed/wrapper"',
        )
