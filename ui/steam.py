"""
Steam VDF (Valve Data Format / KeyValues) parsing and game management.

Reads installed games from libraryfolders.vdf + appmanifest_*.acf.
Reads/writes per-game launch options from localconfig.vdf.
Injects/removes the obs-gamecapture wrapper in launch commands.

Safe to import without a display — no GTK dependency.
"""

import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import vdf
from monitor_manager import HostMonitorManager

# ---------------------------------------------------------------------------
# VKV / KeyValues tokeniser
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Return a flat list of (kind, value) tokens from a VKV text string."""
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]

        # Whitespace
        if c in " \t\r\n":
            i += 1
            continue

        # Line comment
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue

        # Braces
        if c == "{":
            tokens.append(("open", "{"))
            i += 1
            continue
        if c == "}":
            tokens.append(("close", "}"))
            i += 1
            continue

        # Quoted string
        if c == '"':
            i += 1
            parts: list[str] = []
            while i < n:
                ch = text[i]
                if ch == "\\":
                    i += 1
                    if i < n:
                        parts.append(text[i])
                        i += 1
                elif ch == '"':
                    i += 1
                    break
                else:
                    parts.append(ch)
                    i += 1
            tokens.append(("str", "".join(parts)))
            continue

        # Unknown character — skip silently
        i += 1

    return tokens


# ---------------------------------------------------------------------------
# VKV / KeyValues parser
# ---------------------------------------------------------------------------

# Track original key casings to preserve them during dump
_CANONICAL_KEYS: dict[str, str] = {}


def _parse_block(tokens: list[tuple[str, str]], pos: int) -> tuple[dict, int]:
    """
    Consume key-value pairs from *pos* until a closing brace or end-of-tokens.

    Returns ``(result_dict, new_pos)``.  Duplicate keys at the same level are
    handled by letting the last value win, matching Steam's own behaviour.
    """
    result: dict = {}
    n = len(tokens)
    while pos < n:
        kind, val = tokens[pos]

        if kind == "close":
            return result, pos + 1

        if kind == "str":
            key = val.lower()
            _CANONICAL_KEYS[key] = val  # preserve original casing
            pos += 1
            if pos >= n:
                break
            nk, nv = tokens[pos]
            if nk == "str":
                result[key] = nv  # last duplicate wins
                pos += 1
            elif nk == "open":
                sub, pos = _parse_block(tokens, pos + 1)
                result[key] = sub
            else:
                pos += 1  # unexpected token — skip
        else:
            pos += 1  # unexpected token at key position — skip

    return result, pos


def vkv_parse(text: str) -> dict:
    """Parse a VKV text string into a nested dict.  Keys are lowercased."""
    global _CANONICAL_KEYS
    _CANONICAL_KEYS = {}  # reset for each parse
    tokens = _tokenize(text)
    result, _ = _parse_block(tokens, 0)
    return result


def vkv_dump(data: dict, indent: int = 0) -> str:
    """Serialise a nested dict back to VKV text."""
    lines: list[str] = []
    pad = "\t" * indent
    for key, value in data.items():
        # Use original casing if available, otherwise use lowercase
        original_key = _CANONICAL_KEYS.get(key, key)
        if isinstance(value, dict):
            lines.append(f'{pad}"{original_key}"')
            lines.append(f"{pad}{{")
            inner = vkv_dump(value, indent + 1)
            if inner:
                lines.append(inner)
            lines.append(f"{pad}}}")
        else:
            lines.append(f'{pad}"{original_key}"\t\t"{value}"')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Steam process check
# ---------------------------------------------------------------------------


class SteamProcessCheckError(RuntimeError):
    """Raised when Flatpak cannot safely determine whether Steam is running."""


class SteamRestartError(RuntimeError):
    """Raised when Clipper cannot safely complete a requested Steam restart."""


_Result = TypeVar("_Result")


def _is_steam_running(proc_root: Path = Path("/proc")) -> bool:
    """Return True if Steam or steamwebhelper is running.

    Checks both 'steam' and 'steamwebhelper' processes since Steam uses
    multiple processes and steamwebhelper is active when Steam is open.
    Flatpak uses Clipper's fixed host-helper operation because the sandbox's
    ``/proc`` only contains container processes. Native builds scan local
    ``/proc/*/comm`` directly — no psutil required.
    """
    monitor_manager = HostMonitorManager()
    if monitor_manager.available():
        processes = monitor_manager.list_processes()
        if processes is None:
            raise SteamProcessCheckError(
                "Could not check whether Steam is running on the host."
            )
        return any(
            str(process.get("comm") or "").strip().lower()
            in {"steam", "steamwebhelper"}
            for process in processes
        )

    for comm_path in proc_root.glob("*/comm"):
        try:
            comm_name = comm_path.read_text().strip().lower()
            if comm_name in ("steam", "steamwebhelper"):
                return True
        except OSError:
            pass
    return False


def restart_steam_around(action: Callable[[], _Result]) -> _Result:
    """Close Steam, run ``action``, then relaunch Steam even if it fails."""
    monitor_manager = HostMonitorManager()
    using_host_helper = monitor_manager.available()

    if using_host_helper:
        stopped = monitor_manager.stop_steam()
        if not stopped.ok:
            raise SteamRestartError(stopped.stderr or "Could not close Steam.")
    else:
        try:
            subprocess.run(
                ["steam", "-shutdown"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SteamRestartError(f"Could not close Steam: {exc}") from exc

        deadline = time.monotonic() + 30
        while _is_steam_running() and time.monotonic() < deadline:
            time.sleep(0.1)
        if _is_steam_running():
            raise SteamRestartError("Steam did not close within 30 seconds.")

    action_error: BaseException | None = None
    result: _Result | None = None
    try:
        result = action()
    except BaseException as exc:  # Relaunch Steam before preserving the failure.
        action_error = exc

    try:
        if using_host_helper:
            started = monitor_manager.start_steam()
            if not started.ok:
                raise SteamRestartError(started.stderr or "Could not restart Steam.")
        else:
            subprocess.Popen(
                ["steam", "steam://open/main"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30
            while not _is_steam_running() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not _is_steam_running():
                raise SteamRestartError("Steam did not start within 30 seconds.")
    except OSError as exc:
        raise SteamRestartError(f"Could not restart Steam: {exc}") from exc

    if action_error is not None:
        raise action_error
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Game discovery
# ---------------------------------------------------------------------------


@dataclass
class SteamGame:
    appid: str
    name: str
    install_path: str  # full path to the game directory
    icon_path: str = ""  # local Steam artwork path, if available


def find_steam_root() -> Path | None:
    """Return ``~/.local/share/Steam`` if it exists, else ``None``."""
    candidate = Path.home() / ".local" / "share" / "Steam"
    return candidate if candidate.exists() else None


def get_game_icon_path(appid: str, steam_root: Path | None = None) -> str:
    """Return a local Steam artwork path for *appid*, or ``""`` if none exists."""
    appid = str(appid or "").strip()
    if not appid:
        return ""

    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        return ""

    grid_dir = steam_root / "userdata"
    try:
        user_grid_icons = sorted(grid_dir.glob(f"*/config/grid/{appid}_icon.*"))
    except OSError:
        user_grid_icons = []

    library_cache = steam_root / "appcache" / "librarycache" / appid
    try:
        app_icon_jpegs = sorted(
            path
            for path in library_cache.glob("*.jpg")
            if not path.name.startswith(("header", "library_"))
        )
    except OSError:
        app_icon_jpegs = []

    candidates = [
        *user_grid_icons,
        *app_icon_jpegs,
        library_cache / "library_600x900.jpg",
        library_cache / "header.jpg",
    ]
    for pattern in (
        "*/library_600x900.jpg",
        "*/library_capsule.jpg",
        "*/library_header.jpg",
    ):
        try:
            candidates.extend(sorted(library_cache.glob(pattern)))
        except OSError:
            pass

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return ""


def get_game_header_path(appid: str, steam_root: Path | None = None) -> str:
    """Return Steam's cached horizontal header artwork for *appid*."""
    appid = str(appid or "").strip()
    if not appid:
        return ""

    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        return ""

    library_cache = steam_root / "appcache" / "librarycache" / appid
    candidates = [
        library_cache / "header.jpg",
        library_cache / "library_header.jpg",
    ]
    for pattern in ("*/header.jpg", "*/library_header.jpg"):
        try:
            candidates.extend(sorted(library_cache.glob(pattern)))
        except OSError:
            pass

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def get_installed_games(steam_root: Path | None = None) -> list[SteamGame]:
    """Return all installed Steam games, sorted alphabetically by name.

    Parses ``steamapps/libraryfolders.vdf`` to discover every library path,
    then reads ``appmanifest_*.acf`` files within each library's ``steamapps/``
    directory.  Returns ``[]`` if Steam is not installed or no games are found.
    """
    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        return []

    library_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not library_vdf.exists():
        return []

    try:
        data = vkv_parse(library_vdf.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []

    # Root value contains numbered entries, each with a "path" key (new format)
    # or a plain string value (old format pre-2021).
    root = next(iter(data.values()), {}) if data else {}

    library_paths: list[Path] = []
    for key, value in root.items():
        if isinstance(value, dict) and "path" in value:
            library_paths.append(Path(value["path"]))
        elif isinstance(value, str) and key.isdigit():
            library_paths.append(Path(value))

    games: list[SteamGame] = []
    for lib_path in library_paths:
        steamapps = lib_path / "steamapps"
        if not steamapps.is_dir():
            continue
        for acf in steamapps.glob("appmanifest_*.acf"):
            try:
                acf_data = vkv_parse(acf.read_text(encoding="utf-8"))
                app_state = next(iter(acf_data.values()), {}) if acf_data else {}
                if not isinstance(app_state, dict):
                    continue
                appid = str(app_state.get("appid", ""))
                name = str(app_state.get("name", ""))
                installdir = str(app_state.get("installdir", ""))
                if appid and name:
                    install_path = str(steamapps / "common" / installdir)
                    games.append(
                        SteamGame(
                            appid=appid,
                            name=name,
                            install_path=install_path,
                            icon_path=get_game_icon_path(appid, steam_root),
                        )
                    )
            except Exception:  # noqa: BLE001
                continue

    return sorted(games, key=lambda g: g.name.lower())


# ---------------------------------------------------------------------------
# localconfig.vdf helpers
# ---------------------------------------------------------------------------


def _find_localconfig(steam_root: Path) -> Path:
    """Return the ``localconfig.vdf`` for the numerically lowest Steam user ID.

    Raises ``FileNotFoundError`` if no valid file can be located.
    """
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        raise FileNotFoundError(f"No userdata directory at {userdata}")

    candidates = sorted(
        (d for d in userdata.iterdir() if d.is_dir() and d.name.isdigit()),
        key=lambda d: int(d.name),
    )
    for user_dir in candidates:
        lc = user_dir / "config" / "localconfig.vdf"
        if lc.exists():
            return lc

    raise FileNotFoundError(f"No localconfig.vdf found under {userdata}")


def _get_apps_node(data: dict) -> dict:
    """Navigate parsed localconfig data to the 'apps' dict and return it.

    Path traversed (all keys are lowercase after parsing):
    ``root → software → valve → steam → apps``
    """
    root: dict = next(iter(data.values()), {})
    return root["software"]["valve"]["steam"]["apps"]


# ---------------------------------------------------------------------------
# Launch option I/O
# ---------------------------------------------------------------------------


def get_launch_options(appid: str, steam_root: Path | None = None) -> str:
    """Return the launch option string for *appid* from ``localconfig.vdf``.

    Returns ``""`` if the option is not set.
    Raises ``FileNotFoundError`` if ``localconfig.vdf`` cannot be found.

    Checks for both "LaunchOptions" and "launchoptions" for compatibility.
    """
    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        raise FileNotFoundError("Steam root not found")

    localconfig_path = _find_localconfig(steam_root)
    data = vkv_parse(localconfig_path.read_text(encoding="utf-8"))

    try:
        apps = _get_apps_node(data)
        app = apps.get(appid, {})
        if not isinstance(app, dict):
            return ""
        # Check both casings for compatibility
        return app.get("launchoptions", "") or app.get("LaunchOptions", "")
    except (KeyError, AttributeError, TypeError, StopIteration):
        return ""


def set_launch_options(
    appid: str,
    options: str,
    steam_root: Path | None = None,
) -> None:
    """Write launch options for *appid* to ``localconfig.vdf`` using the vdf library.

    Parses the VDF file using ValvePython/vdf library, modifies the launch options
    for the specified game, and writes back with proper formatting.

    Uses a temp-file + ``os.replace`` strategy so that a crash mid-write
    cannot corrupt the config file.

    Raises ``RuntimeError`` if Steam is currently running — Steam rewrites
    ``localconfig.vdf`` on exit and would clobber any changes made here.
    """
    if _is_steam_running():
        raise RuntimeError(
            "Steam is currently running. Close Steam before modifying launch options — "
            "the game capture method modifies launch options that Steam locks."
        )

    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        raise FileNotFoundError("Steam root not found")

    localconfig_path = _find_localconfig(steam_root)

    # Parse with vdf library
    with open(localconfig_path, encoding="utf-8") as f:
        config = vdf.load(f)

    # Navigate to Apps section
    try:
        apps = config["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]
    except KeyError as e:
        raise ValueError(f"Unexpected localconfig.vdf structure: missing key {e}") from e

    # Set launch options (create app entry if needed)
    if appid not in apps:
        apps[appid] = {}
    apps[appid]["LaunchOptions"] = options

    # Write back atomically with proper formatting
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=localconfig_path.parent,
        delete=False,
        suffix=".tmp",
    ) as tmp:
        vdf.dump(config, tmp, pretty=True)
        tmp_name = tmp.name

    os.replace(tmp_name, localconfig_path)


# ---------------------------------------------------------------------------
# Capture wrapper helpers
# ---------------------------------------------------------------------------

CLIPPER_WRAPPER = "obs-gamecapture %command%"


def inject_capture_wrapper(appid: str, steam_root: Path | None = None) -> bool:
    """Add ``CLIPPER_WRAPPER`` to the game's launch options if not already present.

    Returns ``True`` if the wrapper was added, ``False`` if it was already there.
    Raises ``RuntimeError`` if Steam is running (via ``set_launch_options``).
    """
    current = get_launch_options(appid, steam_root)

    # Check if obs-gamecapture is already present
    if "obs-gamecapture" in current:
        return False

    # Handle existing %command% properly.
    if "%command%" in current:
        before, after = current.split("%command%", 1)
        parts = ["obs-gamecapture"]
        parts.extend(before.split())
        parts.append("%command%")
        parts.extend(after.split())
        new_opts = " ".join(parts)
    elif current:
        # No %command% - assume current options are game arguments
        new_opts = f"{CLIPPER_WRAPPER} {current}"
    else:
        # Empty - just use our wrapper
        new_opts = CLIPPER_WRAPPER

    set_launch_options(appid, new_opts, steam_root)
    return True


def remove_capture_wrapper(appid: str, steam_root: Path | None = None) -> bool:
    """Remove ``CLIPPER_WRAPPER`` from the game's launch options if present.

    Returns ``True`` if the wrapper was removed, ``False`` if it was not present.
    Raises ``RuntimeError`` if Steam is running (via ``set_launch_options``).
    """
    current = get_launch_options(appid, steam_root)

    # Check if obs-gamecapture is present
    if "obs-gamecapture" not in current:
        return False

    # Remove the "obs-gamecapture" token from space-separated options
    parts = current.split()
    filtered = [p for p in parts if p != "obs-gamecapture"]
    new_opts = " ".join(filtered)

    # If only %command% remains, remove that too (means we added both)
    if new_opts == "%command%":
        new_opts = ""

    set_launch_options(appid, new_opts, steam_root)
    return True
