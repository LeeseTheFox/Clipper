import configparser
import importlib.util
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ID = "io.github.leesethefox.Clipper"
MANIFEST = REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.yml"
DESKTOP = REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.desktop"
METAINFO = REPO_ROOT / "packaging" / "flatpak" / f"{APP_ID}.metainfo.xml"


def _load_icon_names_module():
    spec = importlib.util.spec_from_file_location(
        "icon_names",
        REPO_ROOT / "ui" / "icon_names.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_host_helpers_are_statically_linked_in_packaged_build():
    manifest = MANIFEST.read_text(encoding="utf-8")

    command = next(line.strip()[2:] for line in manifest.splitlines()
                   if line.strip().startswith("- make -C engine/monitor "))
    flags = next(arg.split("=", 1)[1] for arg in shlex.split(command)
                 if arg.startswith("LDFLAGS="))
    assert "-static" in shlex.split(flags)



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

    assert desktop["Desktop Entry"]["Icon"] == icon_names.APP_ICON
    assert metainfo.findtext("id") == APP_ID
    assert launchable is not None
    assert launchable.text == f"{APP_ID}.desktop"
    assert icon_names.APP_ID == APP_ID
    assert MANIFEST.read_text(encoding="utf-8").startswith(f"id: {APP_ID}\n")


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
        "--filesystem=~/.var/app/com.valvesoftware.Steam/.local/share/Steam:rw",
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
        "--filesystem=xdg-data:rw",
        "--socket=session-bus",
        "--socket=system-bus",
        "--device=all",
    }

    for permission in required:
        assert f"  - {permission}\n" in manifest
    for permission in forbidden:
        assert permission not in manifest
