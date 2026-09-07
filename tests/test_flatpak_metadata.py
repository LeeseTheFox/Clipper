import ast
import configparser
import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ID = "io.github.leesethefox.Clipper"
MANIFEST = REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.yml"
DESKTOP = REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.desktop"
METAINFO = REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.metainfo.xml"
ICON = REPO_ROOT / "ui/icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
ICON_128 = REPO_ROOT / "ui/icons/hicolor/128x128/apps" / f"{APP_ID}.png"
STEAM_ICON = REPO_ROOT / "ui/icons/hicolor/scalable/actions/steam-symbolic.svg"
VENDOR_DIR = REPO_ROOT / "vendor"
README = REPO_ROOT / "packaging" / "flatpak" / "README.md"
LINT_EXCEPTIONS = REPO_ROOT / "packaging" / "flatpak" / "lint-exceptions.json"
PAYLOAD_VALIDATOR = REPO_ROOT / "tools" / "validate_game_capture_payload.py"
PAYLOAD_BUILDER = REPO_ROOT / "tools" / "build_game_capture_payload.py"
CLIPPER_IPC_PATCH = (
    REPO_ROOT / "packaging/flatpak/patches/obs-vkcapture-clipper-ipc.patch"
)


def _load_icon_names_module():
    spec = importlib.util.spec_from_file_location(
        "icon_names",
        REPO_ROOT / "ui" / "icon_names.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_ui_modules() -> set[str]:
    ui_dir = REPO_ROOT / "ui"
    modules = {
        path.stem
        for path in ui_dir.glob("*.py")
        if not path.name.startswith("test_")
    }
    imported = {"main"}

    changed = True
    while changed:
        changed = False
        for module in tuple(imported):
            tree = ast.parse(
                (ui_dir / f"{module}.py").read_text(encoding="utf-8"),
                filename=f"ui/{module}.py",
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".", 1)[0]]
                else:
                    continue
                for name in names:
                    if name in modules and name not in imported:
                        imported.add(name)
                        changed = True

    return imported


def test_flatpak_exported_app_id_files_are_consistent():
    assert MANIFEST.exists()
    assert DESKTOP.exists()
    assert METAINFO.exists()
    assert README.exists()
    assert ICON.exists()
    assert ICON_128.exists()
    assert STEAM_ICON.exists()
    assert PAYLOAD_VALIDATOR.exists()
    assert PAYLOAD_BUILDER.exists()
    assert CLIPPER_IPC_PATCH.exists()
    assert sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "ui/icons").rglob("*")
        if path.is_file()
    ) == [
        f"ui/icons/hicolor/128x128/apps/{APP_ID}.png",
        "ui/icons/hicolor/scalable/actions/steam-symbolic.svg",
        f"ui/icons/hicolor/scalable/apps/{APP_ID}-symbolic.svg",
        f"ui/icons/hicolor/scalable/apps/{APP_ID}.svg",
    ]
    assert not list(VENDOR_DIR.glob("*"))


def test_icon_constants_are_valid_symbolic_names():
    icon_names = _load_icon_names_module()
    constants = {
        name: value
        for name, value in vars(icon_names).items()
        if name.isupper() and name != "APP_ID"
    }

    assert constants
    assert constants["APP_ICON"] == APP_ID
    assert constants["TRAY_IDLE"] == f"{APP_ID}-symbolic"
    for name, value in constants.items():
        assert isinstance(value, str), f"{name} must be a string"
        assert value, f"{name} must not be empty"
        if name != "APP_ICON":
            assert value.endswith("-symbolic"), f"{name} must use a symbolic icon"


def test_ui_uses_icon_name_constants_instead_of_literal_icon_names():
    literal_icon_call = re.compile(
        r"(?:new_from_icon_name|set_icon_name|set_from_icon_name)\(\s*['\"]"
    )
    offenders = []
    for source in sorted((REPO_ROOT / "ui").glob("*.py")):
        if source.name.startswith("test_"):
            continue
        text = source.read_text(encoding="utf-8")
        if literal_icon_call.search(text):
            offenders.append(source.relative_to(REPO_ROOT))

    assert offenders == []


def test_private_icon_resource_is_registered_for_main_app_and_player():
    app_icon_source = (REPO_ROOT / "ui" / "app_icon.py").read_text(encoding="utf-8")
    player_source = (REPO_ROOT / "ui" / "video_player_window.py").read_text(
        encoding="utf-8"
    )

    assert "icon-development-kit.gresource" in app_icon_source
    assert "Gio.Resource.load" in app_icon_source
    assert "_ui_icon_resource._register()" in app_icon_source
    assert "theme.add_resource_path(_UI_ICON_RESOURCE_PATH)" in app_icon_source
    assert "register_app_icon()" in player_source


def test_flatpak_manifest_uses_exported_app_id():
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert manifest.startswith(f"id: {APP_ID}\n")
    assert f"packaging/flatpak/{APP_ID}.desktop" in manifest
    assert f"/app/share/applications/{APP_ID}.desktop" in manifest
    assert f"packaging/flatpak/{APP_ID}.metainfo.xml" in manifest
    assert f"/app/share/metainfo/{APP_ID}.metainfo.xml" in manifest
    assert "LICENSE" in manifest
    assert f"/app/share/licenses/{APP_ID}/LICENSE" in manifest
    assert f"ui/icons/hicolor/scalable/apps/{APP_ID}.svg" in manifest
    assert f"ui/icons/hicolor/scalable/apps/{APP_ID}-symbolic.svg" in manifest
    assert f"ui/icons/hicolor/128x128/apps/{APP_ID}.png" in manifest
    assert (
        "install -Dm644 ui/icons/hicolor/scalable/actions/steam-symbolic.svg "
        "/app/share/icons/hicolor/scalable/actions/steam-symbolic.svg"
    ) in manifest
    assert f"/app/share/licenses/{APP_ID}/vdf-LICENSE" in manifest
    assert "- name: icon-development-kit" in manifest
    assert "icon-library-0.0.22.tar.xz" in manifest
    assert "icon-development-kit.gresource" in manifest
    assert f"/app/share/licenses/{APP_ID}/icon-library-LICENSE.md" in manifest
    assert "- name: xapp-trash-icon" in manifest
    assert "xapp-project/xapp-symbolic-icons/archive/" in manifest
    assert (
        "install -Dm644 icons/symbolic/xsi-user-trash-symbolic.svg "
        "/app/share/icons/hicolor/scalable/actions/xsi-user-trash-symbolic.svg"
    ) in manifest
    assert f"/app/share/licenses/{APP_ID}/xapp-symbolic-icons-LICENSE" in manifest
    assert f"/app/share/licenses/{APP_ID}/xapp-symbolic-icons-COPYRIGHT" in manifest


def test_game_capture_permissions_are_narrow_and_documented():
    manifest = MANIFEST.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert "--filesystem=xdg-data/Steam:rw" in manifest
    assert (
        "--filesystem=~/.var/app/com.valvesoftware.Steam/.local/share/Steam:rw"
        in manifest
    )
    assert "--filesystem=~/snap/steam/common/.local/share/Steam:rw" in manifest
    assert "--talk-name=org.freedesktop.Flatpak" in manifest
    assert "--filesystem=home" not in manifest
    assert "--filesystem=xdg-data:rw" not in manifest
    assert "data/game-capture" in readme
    assert "read access" in readme


def test_game_capture_permission_linter_exceptions_are_exact_and_documented():
    import json

    exceptions = json.loads(LINT_EXCEPTIONS.read_text(encoding="utf-8"))[APP_ID]
    expected = {
        "finish-args-flatpak-spawn-access",
        "finish-args-flatpak-appdata-folder-com.valvesoftware.Steam-.local-share-Steam-rw-access",
        "finish-args-unnecessary-xdg-data-Steam-rw-access",
    }
    readme = README.read_text(encoding="utf-8")

    assert set(exceptions) == expected
    assert all(exception in readme for exception in expected)


def test_host_helpers_are_statically_linked_in_packaged_build():
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert "make -C engine/monitor LDFLAGS=-static" in manifest



def test_flatpak_builds_complete_multiarch_payload_from_one_pinned_source():
    manifest = MANIFEST.read_text(encoding="utf-8")
    builder = PAYLOAD_BUILDER.read_text(encoding="utf-8")
    patch = CLIPPER_IPC_PATCH.read_text(encoding="utf-8")

    assert "- name: obs-vkcapture" in manifest
    assert "path: patches/obs-vkcapture-clipper-ipc.patch" in manifest
    assert "receiver-build/linux-vkcapture.so" in manifest
    assert "/app/lib/obs-plugins/linux-vkcapture.so" in manifest
    assert "build_game_capture_payload.py" in manifest
    assert "glibc-2.17-317.el7.x86_64.rpm" in manifest
    assert "glibc-2.17-317.el7.i686.rpm" in manifest
    assert "validate_game_capture_payload.py /app --self-test" in manifest

    assert 'PAYLOAD_VERSION = "obs-vkcapture-1.5.6-clipper.1"' in builder
    assert '"-march=x86-64"' in builder
    assert '"-m32"' in builder
    assert '"-march=i686"' in builder
    assert '"linker_emulation": "elf_i386"' in builder
    assert '"-mno-sse2"' in builder
    assert '"--build-id=sha1"' in builder
    assert "libVkLayer_clipper_vkcapture.so" in builder
    assert "libclipper_glcapture.so" in builder
    assert "CLIPPER_GAME_CAPTURE=1" in builder
    assert '"exec \\"$@\\"\\n"' in builder

    assert patch.count("/io/github/leesethefox/Clipper/vkcapture/") == 2
    assert "cred.uid != getuid()" in patch
    assert patch.count("/com/obsproject/vkcapture") == 2


def test_flatpak_installs_only_the_selected_xapp_icon():
    manifest = MANIFEST.read_text(encoding="utf-8")
    xapp_module = manifest.split("  - name: xapp-trash-icon\n", 1)[1].split(
        "\n  - name:", 1
    )[0]

    installed_xapp_icons = re.findall(
        r"install -Dm644 (icons/symbolic/[^ ]+\.svg)", xapp_module
    )
    assert installed_xapp_icons == ["icons/symbolic/xsi-user-trash-symbolic.svg"]


def test_desktop_metainfo_and_icon_constant_use_exported_app_id():
    desktop = configparser.ConfigParser(interpolation=None)
    desktop.optionxform = str
    desktop.read(DESKTOP, encoding="utf-8")

    metainfo = ET.parse(METAINFO).getroot()
    launchable = metainfo.find("launchable")
    icon_names = _load_icon_names_module()
    main_source = (REPO_ROOT / "ui" / "main.py").read_text(encoding="utf-8")

    assert desktop["Desktop Entry"]["Icon"] == icon_names.APP_ICON
    assert metainfo.findtext("id") == APP_ID
    assert launchable is not None
    assert launchable.text == f"{APP_ID}.desktop"
    assert icon_names.APP_ID == APP_ID
    assert "https://github.com/LeeseTheFox/Clipper" in main_source
    assert "https://github.com/clipper/clipper" not in main_source


def test_manifest_network_sources_are_pinned():
    manifest = MANIFEST.read_text(encoding="utf-8").splitlines()

    source_blocks: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] | None = None
    for raw_line in manifest:
        line = raw_line.strip()
        if line == "- type: git":
            current = {"type": "git", "tag": False, "commit": False}
            source_blocks.append(current)
            continue
        if line == "- type: archive":
            current = {"type": "archive", "sha256": False}
            source_blocks.append(current)
            continue
        if line == "- type: file":
            current = {"type": "file", "url": False, "sha256": False}
            source_blocks.append(current)
            continue
        if current is None:
            continue
        if line.startswith("- type: ") and line not in (
            "- type: git",
            "- type: archive",
            "- type: file",
        ):
            current = None
            continue
        for key in tuple(current):
            if line.startswith(f"{key}: "):
                current[key] = True

    assert source_blocks
    for source in source_blocks:
        if source["type"] == "git":
            assert source["tag"], source
            assert source["commit"], source
        if source["type"] == "archive":
            assert source["sha256"], source
        if source["type"] == "file" and source.get("url"):
            assert source["sha256"], source


def test_manifest_installs_required_python_dependencies():
    manifest = MANIFEST.read_text(encoding="utf-8")
    requirements = (REPO_ROOT / "ui" / "requirements.txt").read_text(encoding="utf-8")
    steam_source = (REPO_ROOT / "ui" / "steam.py").read_text(encoding="utf-8")

    assert "import vdf" in steam_source
    assert "vdf==3.4" in requirements
    assert "- name: python3-vdf" in manifest
    assert "vdf-3.4-py2.py3-none-any.whl" in manifest
    assert (
        "python3 -m pip install --no-index --no-deps --prefix=/app "
        "vdf-3.4-py2.py3-none-any.whl"
    ) in manifest
    assert "sha256: 68c1a125cc49e343d535af2dd25074e9cb0908c6607f073947c4a04bbe234534" in manifest


def test_manifest_installed_ui_layout_matches_launcher_expectation():
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert "install -d /app/share/clipper/ui" in manifest
    assert (
        "find ui -maxdepth 1 -type f -name '*.py' ! -name 'test_*.py' "
        "-exec install -Dm644 '{}' '/app/share/clipper/{}' ';'"
    ) in manifest
    assert f"ui/icons/hicolor/scalable/apps/{APP_ID}.svg" in manifest
    assert f"ui/icons/hicolor/128x128/apps/{APP_ID}.png" in manifest
    assert (
        "install -Dm644 ui/sounds/clip-saved.wav "
        "/app/share/clipper/ui/sounds/clip-saved.wav"
    ) in manifest
    assert (
        "install -Dm644 icon-development-kit.gresource "
        "/app/share/clipper/ui/icon-development-kit.gresource"
    ) in manifest

    launcher = (REPO_ROOT / "clipper").read_text(encoding="utf-8")
    assert 'UI_DIR="/app/share/clipper/ui"' in launcher
    assert '"$PYTHON" main.py' in launcher


def test_manifest_ui_install_rule_covers_runtime_imports_only():
    manifest = MANIFEST.read_text(encoding="utf-8")
    ui_dir = REPO_ROOT / "ui"
    installed_by_rule = {
        path.name
        for path in ui_dir.glob("*.py")
        if not path.name.startswith("test_")
    }
    required_runtime_files = {f"{module}.py" for module in _runtime_ui_modules()}

    assert required_runtime_files <= installed_by_rule
    assert "test_client.py" not in installed_by_rule
    assert "test_steam_integration.py" not in installed_by_rule
    assert "find ui -maxdepth 1 -type f -name '*.py' ! -name 'test_*.py'" in manifest


def test_manifest_engine_install_layout_matches_runtime_expectation():
    manifest = MANIFEST.read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "engine" / "src" / "Makefile").read_text(encoding="utf-8")
    engine_manager = (REPO_ROOT / "ui" / "engine_manager.py").read_text(
        encoding="utf-8"
    )

    assert "make -C engine/src OBS_PREFIX=/app" in manifest
    assert "make -C engine/src OBS_PREFIX=/app install" in manifest
    assert "LIBEXECDIR = $(PREFIX)/libexec/clipper" in makefile
    assert "install -Dm755 $(TARGET) $(DESTDIR)$(LIBEXECDIR)/$(TARGET)" in makefile
    assert (
        "ln -sf $(OBS_PREFIX)/bin/obs-ffmpeg-mux "
        "$(DESTDIR)$(LIBEXECDIR)/obs-ffmpeg-mux"
    ) in makefile
    assert 'FLATPAK_ENGINE_PATH = Path("/app/libexec/clipper/clipper-engine")' in (
        engine_manager
    )


def test_manifest_monitor_install_layout_matches_host_spawn_expectation():
    manifest = MANIFEST.read_text(encoding="utf-8")
    monitor_manager = (REPO_ROOT / "ui" / "monitor_manager.py").read_text(
        encoding="utf-8"
    )
    assert "make -C engine/monitor" in manifest
    assert (
        "install -Dm755 engine/monitor/clipper-monitor-host "
        "/app/libexec/clipper/clipper-monitor-host"
    ) in manifest
    assert (
        'MONITOR_HELPER_RELATIVE = Path("libexec/clipper/clipper-monitor-host")'
        in monitor_manager
    )
    assert "host monitor operation" in monitor_manager


def test_manifest_finish_args_keep_narrow_flatpak_permissions():
    manifest = MANIFEST.read_text(encoding="utf-8")

    required = {
        "--socket=wayland",
        "--socket=fallback-x11",
        "--share=ipc",
        "--share=network",
        "--device=dri",
        "--socket=pulseaudio",
        "--filesystem=xdg-videos:create",
        "--filesystem=xdg-data/Steam:rw",
        "--filesystem=~/snap/steam/common/.local/share/Steam:rw",
        "--filesystem=/mnt:ro",
        "--filesystem=/run/media:ro",
        "--filesystem=xdg-run/pipewire-0",
        "--filesystem=xdg-run/clipper:create",
        "--talk-name=org.freedesktop.Flatpak",
        "--talk-name=org.kde.StatusNotifierWatcher",
        "--talk-name=org.freedesktop.Notifications",
    }
    forbidden = {
        "--filesystem=host",
        "--filesystem=home",
        "--socket=session-bus",
        "--socket=system-bus",
        "--device=all",
    }

    for permission in required:
        assert f"  - {permission}\n" in manifest
    for permission in forbidden:
        assert permission not in manifest


def test_manifest_and_engine_use_bundled_obs_paths():
    manifest = MANIFEST.read_text(encoding="utf-8")
    engine_source = (REPO_ROOT / "engine" / "src" / "clipper_engine.c").read_text(
        encoding="utf-8"
    )

    for module_name in (
        "obs-studio-libobs",
        "obs-vkcapture",
        "obs-pipewire-audio-capture",
    ):
        assert f"- name: {module_name}" in manifest

    assert "tag: v1.5.6" in manifest
    assert "commit: a9ea91fe1994708067e95d4159852b11b4209a16" in manifest

    assert '#define CLIPPER_PREFIX_DEFAULT "/app"' in engine_source
    assert '#define OBS_LIBDIR_DEFAULT CLIPPER_PREFIX_DEFAULT "/lib"' in engine_source
    assert (
        '#define OBS_DATADIR_DEFAULT CLIPPER_PREFIX_DEFAULT "/share/obs"'
        in engine_source
    )
    assert 'OBS_LIBDIR_DEFAULT "/obs-plugins/linux-vkcapture.so"' in engine_source
    assert 'OBS_DATADIR_DEFAULT "/obs-plugins/linux-vkcapture"' in engine_source
    assert 'env_or_default("CLIPPER_OBS_LIBDIR", OBS_LIBDIR_DEFAULT)' in engine_source
    assert 'env_or_default("CLIPPER_OBS_DATADIR", OBS_DATADIR_DEFAULT)' in engine_source
    assert (
        'env_or_default("CLIPPER_VKCAPTURE_PLUGIN", VKC_PLUGIN_SO_DEFAULT)'
        in engine_source
    )
    assert "obs_source_get_width(state->vkc_source)" in engine_source
    assert "obs_source_get_height(state->vkc_source)" in engine_source
    assert 'getenv("CLIPPER_PIPEWIRE_AUDIO_PLUGIN")' in engine_source
    assert (
        '"%s/obs-plugins/%s", obs_data_dir, plugin_names[i]'
        in engine_source
    )
    assert (
        "obs_open_module(&mod, plugin_paths[i], plugin_data_paths[i])"
        in engine_source
    )


def test_packaged_runtime_files_do_not_depend_on_obs_studio_flatpak_paths():
    forbidden = (
        "/var/lib/flatpak/app/com.obsproject.Studio",
        "com.obsproject.Studio.Plugin.OBSVkCapture",
    )
    packaged_runtime_files = [
        REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.yml",
        REPO_ROOT / "clipper",
        REPO_ROOT / "engine" / "src" / "clipper_engine.c",
    ]

    for path in packaged_runtime_files:
        text = path.read_text(encoding="utf-8")
        if path.name == "clipper":
            text = text.split('if [ -n "${FLATPAK_ID:-}" ]', 1)[1].split(
                "else",
                1,
            )[0]
        for value in forbidden:
            assert value not in text, path


def test_manifest_cleanup_policy_removes_development_artifacts():
    manifest = MANIFEST.read_text(encoding="utf-8")

    for cleanup_entry in (
        "/include",
        "/lib/pkgconfig",
        "/share/man",
        "'*.a'",
        "'*.la'",
    ):
        assert f"  - {cleanup_entry}\n" in manifest

    assert "  - /bin/obs\n" in manifest
    assert "  - /share/applications/com.obsproject.Studio.desktop\n" in manifest
    assert "  - /share/metainfo/com.obsproject.Studio.appdata.xml\n" in manifest


def test_manifest_dir_source_skips_generated_local_artifacts():
    manifest = MANIFEST.read_text(encoding="utf-8")

    for skip_entry in (
        ".git",
        ".flatpak-builder",
        ".pytest_cache",
        "build-dir",
        "build-dir-bounded",
        "build-dir-download",
        "repo",
        "venv",
        "engine/monitor/clipper-monitor-host",
        "engine/src/clipper-engine",
        "engine/src/obs-ffmpeg-mux",
        "'**/__pycache__'",
        "'**/*.pyc'",
    ):
        assert f"          - {skip_entry}\n" in manifest


def test_flatpak_readme_mentions_current_validation_targets():
    readme = README.read_text(encoding="utf-8")

    assert f"packaging/flatpak/{APP_ID}.yml" in readme
    assert f"packaging/flatpak/{APP_ID}.desktop" in readme
    assert f"packaging/flatpak/{APP_ID}.metainfo.xml" in readme
    assert "tests/test_flatpak_metadata.py" in readme
    assert "./tools/flatpak_smoke_test.py" in readme
    assert "--isolated-data" in readme
    assert "desktop-file-validate" in readme
    assert "appstreamcli validate" in readme
    assert "flatpak-builder-lint" in readme
    assert "Before a Flathub submission" in readme
    assert "screenshots captured from the real installed Flatpak" in readme
    assert "build-dir-bounded" in readme
    assert "bundled third-party licenses" in readme


def test_flatpak_documentation_tracks_current_release_blockers():
    documentation = README.read_text(encoding="utf-8")

    assert "flatpak-builder-lint" in documentation
    assert "host-monitor helper" in documentation
    assert "host-monitor IPC" in documentation
    assert "Flathub" in documentation
    assert "screenshots captured from the real installed Flatpak" in documentation
    assert "publicly reachable" in documentation
    assert "bundled third-party licenses" in documentation
