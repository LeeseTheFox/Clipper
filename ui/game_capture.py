"""Export the bundled game hooks into Clipper's own application data.

Games cannot load libraries from Clipper's /app mount. Keep one relocatable
copy in our data directory and give Flatpak Steam read access to that directory.
No host packages, extensions, downloads, or files in Steam's data are needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from enum import Enum
from pathlib import Path

APP_ID = "io.github.leesethefox.Clipper"
STEAM_ID = "com.valvesoftware.Steam"
PAYLOAD_VERSION = "obs-vkcapture-1.5.6-clipper.1"
BUNDLED_PAYLOAD = Path("/app/share/clipper/game-capture/payloads") / PAYLOAD_VERSION


class EnvironmentKind(Enum):
    NATIVE_STEAM = "native_steam"
    FLATPAK_STEAM_USER = "flatpak_steam_user"
    FLATPAK_STEAM_SYSTEM = "flatpak_steam_system"
    STEAM_SNAP = "steam_snap"


def data_root() -> Path:
    # Use the host-visible Flatpak app-data path even for a native development
    # run, so launch options remain valid when moving between the two builds.
    return Path.home() / ".var/app" / APP_ID / "data/game-capture"


def wrapper_path() -> Path:
    return data_root() / "current/bin/clipper-gamecapture"


def launch_prefix(path: Path | None = None) -> str:
    """A removed Flatpak must not prevent a configured game from starting."""
    path = wrapper_path() if path is None else path
    if not path.is_absolute() or any(c in str(path) for c in "\0\n\r"):
        raise ValueError("Game capture requires an absolute wrapper path")
    script = 'if [ -x "$0" ]; then exec "$0" "$@"; else exec "$@"; fi'
    return f"sh -c {shlex.quote(script)} {shlex.quote(str(path))}"


def launch_options() -> str:
    return f"{launch_prefix()} %command%"


def _matches(root: Path, manifest: dict) -> bool:
    try:
        for relative, digest in manifest["files"].items():
            path = root / relative
            if path.is_symlink() or not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        for relative, target in manifest["links"].items():
            if os.readlink(root / relative) != target:
                return False
        return os.access(root / "bin/clipper-gamecapture", os.X_OK)
    except (OSError, KeyError):
        return False


def ensure_payload(source: Path = BUNDLED_PAYLOAD, destination: Path | None = None) -> Path:
    """Verify and atomically activate a copy of the packaged hooks.

    Retain older versions while games may still be loading their libraries.
    All copies are removed with Clipper's Flatpak application data.
    """
    destination = data_root() if destination is None else destination
    raw_manifest = (source / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if not _matches(source, manifest):
        raise RuntimeError("The bundled game capture files are incomplete")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError("Game capture data directory must not be a symlink")
    version = hashlib.sha256(raw_manifest).hexdigest()[:16]
    target = destination / version
    if target.is_symlink() or not _matches(target, manifest):
        # A fresh name also repairs a damaged copy without replacing libraries
        # that an already-running game may have mapped.
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=destination) as temp:
            staged = Path(temp) / "payload"
            shutil.copytree(source, staged, symlinks=True)
            if not _matches(staged, manifest):
                raise RuntimeError("Could not copy the bundled game capture files")
            if target.exists() or target.is_symlink():
                target = destination / (version + "-" + Path(temp).name[7:])
            os.replace(staged, target)
    with tempfile.TemporaryDirectory(prefix=".activate-", dir=destination) as temp:
        link = Path(temp) / "current"
        link.symlink_to(target.name)
        os.replace(link, destination / "current")
    return destination / "current/bin/clipper-gamecapture"


def allow_flatpak_steam() -> None:
    """Grant only our exported hooks; existing Steam overrides are preserved."""
    command = [
        "flatpak",
        "override",
        "--user",
        f"--filesystem={data_root()}:ro",
        STEAM_ID,
    ]
    if os.environ.get("FLATPAK_ID"):
        command = [
            "flatpak-spawn",
            "--host",
            # The UI's /app working directory does not exist on the host.
            "--directory=/",
            f"--env=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus",
            f"--env=XDG_DATA_HOME={Path.home() / '.local/share'}",
            f"--env=XDG_CONFIG_HOME={Path.home() / '.config'}",
            *command,
        ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Could not share game capture with Steam: {result.stderr.strip()}")


def prepare(environment: EnvironmentKind = EnvironmentKind.NATIVE_STEAM) -> Path:
    if environment is EnvironmentKind.STEAM_SNAP:
        raise RuntimeError("Steam Snap does not allow loading Clipper's bundled hooks")
    path = ensure_payload()
    if environment in {
        EnvironmentKind.FLATPAK_STEAM_USER,
        EnvironmentKind.FLATPAK_STEAM_SYSTEM,
    }:
        allow_flatpak_steam()
    return path
