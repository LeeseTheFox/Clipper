"""
Steam VDF (Valve Data Format / KeyValues) parsing and game management.

Reads installed games from libraryfolders.vdf + appmanifest_*.acf.
Reads/writes per-game launch options from localconfig.vdf.
Composes provider-verified game-capture wrappers without normalising the
user's launch-option text.

Safe to import without a display — no GTK dependency.
"""

import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

import game_capture
import vdf
from game_capture import EnvironmentKind
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


_STEAM_ENVIRONMENTS = {
    EnvironmentKind.NATIVE_STEAM,
    EnvironmentKind.FLATPAK_STEAM_USER,
    EnvironmentKind.FLATPAK_STEAM_SYSTEM,
    EnvironmentKind.STEAM_SNAP,
}


def _process_environment(text: str) -> EnvironmentKind:
    values = text.replace("\0", " ").split()
    if "FLATPAK_ID=com.valvesoftware.Steam" in values:
        return EnvironmentKind.FLATPAK_STEAM_USER
    if "SNAP_NAME=steam" in values or "SNAP_INSTANCE_NAME=steam" in values:
        return EnvironmentKind.STEAM_SNAP
    return EnvironmentKind.NATIVE_STEAM


def _is_steam_running(
    proc_root: Path = Path("/proc"),
    environment: EnvironmentKind | None = None,
) -> bool:
    """Return whether Steam is running, optionally for one packaging variant.

    Checks both 'steam' and 'steamwebhelper' processes since Steam uses
    multiple processes and steamwebhelper is active when Steam is open.
    Flatpak uses Clipper's fixed host-helper operation because the sandbox's
    ``/proc`` only contains container processes. Native builds scan local
    ``/proc/*/comm`` directly — no psutil required.
    """
    monitor_manager = HostMonitorManager()
    if monitor_manager.available():
        if environment is not None:
            running = monitor_manager.steam_running(environment)
            if running is None:
                raise SteamProcessCheckError(
                    "Could not check whether the selected Steam client is running."
                )
            return running
        processes = monitor_manager.list_processes()
        if processes is None:
            raise SteamProcessCheckError("Could not check whether Steam is running on the host.")
        return any(
            str(process.get("comm") or "").strip().lower() in {"steam", "steamwebhelper"}
            for process in processes
        )

    for comm_path in proc_root.glob("*/comm"):
        try:
            comm_name = comm_path.read_text().strip().lower()
            if comm_name not in ("steam", "steamwebhelper"):
                continue
            if environment is None:
                return True
            environ_path = comm_path.parent / "environ"
            try:
                process_environment = _process_environment(environ_path.read_text())
            except OSError:
                process_environment = EnvironmentKind.NATIVE_STEAM
            if environment in {
                EnvironmentKind.FLATPAK_STEAM_USER,
                EnvironmentKind.FLATPAK_STEAM_SYSTEM,
            }:
                if process_environment in {
                    EnvironmentKind.FLATPAK_STEAM_USER,
                    EnvironmentKind.FLATPAK_STEAM_SYSTEM,
                }:
                    return True
            elif process_environment is environment:
                return True
        except OSError:
            pass
    return False


def _direct_steam_command(environment: EnvironmentKind, *, shutdown: bool) -> list[str]:
    suffix = ["-shutdown"] if shutdown else ["steam://open/main"]
    if environment is EnvironmentKind.NATIVE_STEAM:
        return ["steam", *suffix]
    if environment in {
        EnvironmentKind.FLATPAK_STEAM_USER,
        EnvironmentKind.FLATPAK_STEAM_SYSTEM,
    }:
        return ["flatpak", "run", "com.valvesoftware.Steam", *suffix]
    if environment is EnvironmentKind.STEAM_SNAP:
        return ["snap", "run", "steam", *suffix]
    raise SteamRestartError("Unsupported Steam environment")


def restart_steam_around(
    action: Callable[[], _Result],
    environment: EnvironmentKind = EnvironmentKind.NATIVE_STEAM,
    *,
    rollback: Callable[[], None] | None = None,
) -> _Result:
    """Restart one Steam variant around an action, rolling back start failures."""
    if environment not in _STEAM_ENVIRONMENTS:
        raise SteamRestartError("Unsupported Steam environment")
    monitor_manager = HostMonitorManager()
    using_host_helper = monitor_manager.available()

    if using_host_helper:
        stopped = monitor_manager.stop_steam(environment)
        if not stopped.ok:
            raise SteamRestartError(stopped.stderr or "Could not close Steam.")
    else:
        try:
            subprocess.run(
                _direct_steam_command(environment, shutdown=True),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SteamRestartError(f"Could not close Steam: {exc}") from exc

        deadline = time.monotonic() + 30
        while _is_steam_running(environment=environment) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _is_steam_running(environment=environment):
            raise SteamRestartError("Steam did not close within 30 seconds.")

    action_error: BaseException | None = None
    result: _Result | None = None
    try:
        result = action()
    except BaseException as exc:  # Relaunch Steam before preserving the failure.
        action_error = exc

    try:
        if using_host_helper:
            started = monitor_manager.start_steam(environment)
            if not started.ok:
                raise SteamRestartError(started.stderr or "Could not restart Steam.")
        else:
            subprocess.Popen(
                _direct_steam_command(environment, shutdown=False),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30
            while not _is_steam_running(environment=environment) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not _is_steam_running(environment=environment):
                raise SteamRestartError("Steam did not start within 30 seconds.")
    except (OSError, SteamRestartError) as exc:
        if action_error is None and rollback is not None:
            try:
                rollback()
            except BaseException as rollback_error:
                raise SteamRestartError(
                    f"Could not restart Steam and rollback failed: {rollback_error}"
                ) from exc
        if isinstance(exc, SteamRestartError):
            raise
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
    steam_installation: str = ""
    steam_environment: str = EnvironmentKind.NATIVE_STEAM.value
    steam_account_id: str = ""


@dataclass(frozen=True)
class SteamInstallation:
    stable_id: str
    environment: EnvironmentKind
    data_root: Path
    flatpak_scope: str = ""
    runtime_branch: str = ""
    logical_data_root: Path | None = None
    game_visible_data_root: Path | None = None


@dataclass(frozen=True)
class SteamAccount:
    account_id: str
    account_name: str
    localconfig_path: Path
    most_recent: bool = False
    timestamp: int = 0


class AmbiguousSteamAccountError(RuntimeError):
    """Raised when Steam account evidence cannot select one safe target."""

    def __init__(self, accounts: list[SteamAccount]):
        super().__init__("Multiple Steam accounts are active; choose an account")
        self.accounts = accounts


class LaunchOptionConflictError(RuntimeError):
    """Raised when launch options changed after Clipper prepared a mutation."""


class CaptureProviderUnverifiedError(RuntimeError):
    """Raised when code attempts to write an unverified capture wrapper."""


@dataclass(frozen=True)
class LaunchOptionMutation:
    original: str
    applied: str
    wrapper_prefix: str
    managed: bool


def _installation_id(environment: EnvironmentKind, data_root: Path) -> str:
    digest = sha256(str(data_root).encode("utf-8")).hexdigest()[:12]
    return f"{environment.value}:{digest}"


def discover_steam_installations(home: Path | None = None) -> list[SteamInstallation]:
    """Discover native, Flatpak, and Snap Steam data roots and path mappings."""
    home = Path.home() if home is None else Path(home)
    candidates = (
        (
            EnvironmentKind.NATIVE_STEAM,
            home / ".local/share/Steam",
            "",
            home / ".local/share/Steam",
        ),
        (
            EnvironmentKind.NATIVE_STEAM,
            home / ".steam/root",
            "",
            home / ".steam/root",
        ),
        (
            EnvironmentKind.NATIVE_STEAM,
            home / ".steam/steam",
            "",
            home / ".steam/steam",
        ),
        (
            EnvironmentKind.NATIVE_STEAM,
            home / ".steam/debian-installation",
            "",
            home / ".steam/debian-installation",
        ),
        (
            EnvironmentKind.FLATPAK_STEAM_USER,
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
            "user",
            home / ".local/share/Steam",
        ),
        (
            EnvironmentKind.STEAM_SNAP,
            home / "snap/steam/common/.local/share/Steam",
            "",
            home / "snap/steam/common/.local/share/Steam",
        ),
    )
    installations: list[SteamInstallation] = []
    seen: set[tuple[EnvironmentKind, Path]] = set()
    for environment, candidate, scope, game_visible_root in candidates:
        if not candidate.exists():
            continue
        try:
            canonical = candidate.resolve(strict=True)
        except OSError:
            continue
        identity = (environment, canonical)
        if identity in seen:
            continue
        seen.add(identity)
        installations.append(
            SteamInstallation(
                stable_id=_installation_id(environment, canonical),
                environment=environment,
                data_root=canonical,
                flatpak_scope=scope,
                logical_data_root=candidate,
                game_visible_data_root=game_visible_root,
            )
        )
    return installations


def installation_display_name(installation: SteamInstallation) -> str:
    """Return a concise non-localized installation name for diagnostics."""
    if installation.environment is EnvironmentKind.NATIVE_STEAM:
        return "Native Steam"
    if installation.environment is EnvironmentKind.STEAM_SNAP:
        return "Steam Snap"
    scope = installation.flatpak_scope or "user"
    return f"Flatpak Steam ({scope})"


def find_installed_game(installation: SteamInstallation, appid: str) -> SteamGame | None:
    """Return an installed game by App ID without selecting an account."""
    appid = str(appid or "").strip()
    return next(
        (game for game in get_installed_games(installation) if game.appid == appid),
        None,
    )


def installation_for_entry(entry: dict) -> SteamInstallation:
    """Bind old entries by their installed path; never guess between clients."""
    installations = discover_steam_installations()
    stored_id = entry.get("steam_installation")
    if stored_id:
        matches = [item for item in installations if item.stable_id == stored_id]
    else:
        matches = [
            item
            for item in installations
            if any(
                game.appid == str(entry.get("appid"))
                and (
                    not entry.get("install_path")
                    or Path(game.install_path).resolve() == Path(entry["install_path"]).resolve()
                )
                for game in get_installed_games(item)
            )
        ]
    if len(matches) != 1:
        raise ValueError(
            "Could not identify this game's Steam installation; add it from the Steam game picker"
        )
    return matches[0]


def recover_capture_change(config) -> None:
    result = reconcile_pending_game_capture_change(config)
    if result not in {"none", "completed", "aborted", "rolled_back"}:
        raise LaunchOptionConflictError(f"An interrupted Steam change needs recovery: {result}")


def capture_updates(config) -> list[tuple[dict, SteamInstallation, bool]]:
    """Find existing entries whose launch options need the bundled wrapper."""
    from capture_modes import CAPTURE_MODE_GAME, capture_mode_for_entry

    game_capture.ensure_payload()
    updates = []
    journal = config.get("pending_game_capture_change")
    if journal:
        installation = next(
            (
                item
                for item in discover_steam_installations()
                if item.stable_id == journal["steam_installation"]
            ),
            None,
        )
        if installation is None:
            raise LaunchOptionConflictError("Steam is unavailable for an interrupted change")
        if _is_steam_running(environment=installation.environment):
            updates.append(({}, installation, True))
        else:
            recover_capture_change(config)
    prepared = set()
    for entry in config.get("whitelist", []):
        if not entry.get("appid") or capture_mode_for_entry(entry) != CAPTURE_MODE_GAME:
            continue
        try:
            installation = installation_for_entry(entry)
        except (ValueError, OSError) as exc:
            # A removed Steam installation is not a reason to show setup
            # dialogs on every application launch or block other games.
            print(f"[clipper] skipping unavailable Steam entry: {exc}")
            continue
        if installation.environment not in prepared:
            game_capture.prepare(installation.environment)
            prepared.add(installation.environment)
        current = get_launch_options(
            entry["appid"], installation.data_root, account_id=entry.get("steam_account_id")
        )
        if game_capture.launch_prefix() not in current:
            updates.append(
                (entry, installation, _is_steam_running(environment=installation.environment))
            )
    return updates


def apply_capture_updates(config, updates: list[tuple[dict, SteamInstallation, bool]]) -> None:
    for environment in dict.fromkeys(item.environment for _entry, item, _running in updates):
        group = [item for item in updates if item[1].environment == environment]

        def commit(group=group, environment=environment):
            recover_capture_change(config)
            for previous, installation, _running in group:
                if not previous:
                    continue  # Recovery-only work, including an interrupted first add.
                entry = dict(
                    previous,
                    steam_installation=installation.stable_id,
                    steam_environment=environment.value,
                )
                entry["steam_account_id"] = select_steam_account(
                    installation.data_root, entry.get("steam_account_id")
                ).account_id
                apply_game_capture_update_transaction(
                    config,
                    previous,
                    entry,
                    str(game_capture.wrapper_path()),
                    installation.data_root,
                )

        if any(running for _entry, _installation, running in group):
            restart_steam_around(commit, environment)
        else:
            commit()


def remove_game_capture_entry(
    config, previous: dict, entry: dict, installation: SteamInstallation
) -> None:
    """Remove an owned launch option, including the exact legacy Clipper prefix."""
    integration = entry.get("game_capture_integration", {})
    if not integration.get("managed_launch_options"):
        current = get_launch_options(
            entry["appid"], installation.data_root, account_id=entry["steam_account_id"]
        )
        if current.startswith("obs-gamecapture "):
            entry["game_capture_integration"] = {
                "managed_launch_options": True,
                "original_launch_options": current.removeprefix("obs-gamecapture "),
                "applied_launch_options": current,
                "wrapper_prefix": "obs-gamecapture",
            }
        else:
            config.set(
                "whitelist", [item for item in config.get("whitelist", []) if item != previous]
            )
            return
    apply_game_capture_remove_transaction(config, entry, installation.data_root)


def find_steam_root() -> Path | None:
    """Return a Steam root for legacy callers, including Flatpak-only installs."""
    return next(
        (item.data_root for item in discover_steam_installations()),
        None,
    )


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
        user_grid_icons = sorted(
            path
            for path in grid_dir.glob(f"*/config/grid/{appid}_icon.*")
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
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


def get_installed_games(
    steam_root: Path | SteamInstallation | None = None,
    *,
    account_id: str = "",
) -> list[SteamGame]:
    """Return all installed Steam games, sorted alphabetically by name.

    Parses ``steamapps/libraryfolders.vdf`` to discover every library path,
    then reads ``appmanifest_*.acf`` files within each library's ``steamapps/``
    directory.  Returns ``[]`` if Steam is not installed or no games are found.
    """
    installation: SteamInstallation | None = None
    if isinstance(steam_root, SteamInstallation):
        installation = steam_root
        steam_root = installation.data_root
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

    library_paths: list[Path] = [steam_root]
    for key, value in root.items():
        if isinstance(value, dict) and "path" in value:
            library_paths.append(Path(value["path"]))
        elif isinstance(value, str) and key.isdigit():
            library_paths.append(Path(value))

    games: list[SteamGame] = []
    seen_libraries: set[Path] = set()
    for lib_path in library_paths:
        if installation is not None and installation.game_visible_data_root:
            try:
                # Immutable distros often alias /home to /var/home. Steam can
                # persist either spelling, even inside its private home.
                library = Path(str(lib_path).replace("/var/home/", "/home/", 1))
                visible = Path(
                    str(installation.game_visible_data_root).replace("/var/home/", "/home/", 1)
                )
                relative = library.relative_to(visible)
                lib_path = installation.data_root / relative
            except ValueError:
                pass
        if lib_path in seen_libraries:
            continue
        seen_libraries.add(lib_path)
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
                            steam_installation=(
                                installation.stable_id if installation is not None else ""
                            ),
                            steam_environment=(
                                installation.environment.value
                                if installation is not None
                                else EnvironmentKind.NATIVE_STEAM.value
                            ),
                            steam_account_id=account_id,
                        )
                    )
            except Exception:  # noqa: BLE001
                continue

    return sorted(games, key=lambda g: g.name.lower())


# ---------------------------------------------------------------------------
# localconfig.vdf helpers
# ---------------------------------------------------------------------------


def discover_steam_accounts(steam_root: Path) -> list[SteamAccount]:
    """Return local accounts enriched with Steam's login evidence."""
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        raise FileNotFoundError(f"No userdata directory at {userdata}")

    evidence: dict[str, tuple[str, bool, int]] = {}
    loginusers = steam_root / "config" / "loginusers.vdf"
    try:
        parsed = vkv_parse(loginusers.read_text(encoding="utf-8"))
        users = parsed.get("users", {})
        if isinstance(users, dict):
            for account_id, raw in users.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    timestamp = int(str(raw.get("timestamp") or "0"))
                except ValueError:
                    timestamp = 0
                identity = str(account_id)
                account_evidence = (
                    str(raw.get("accountname") or raw.get("personaname") or account_id),
                    str(raw.get("mostrecent") or "0") == "1",
                    timestamp,
                )
                evidence[identity] = account_evidence
                # loginusers.vdf is keyed by SteamID64 while userdata uses the
                # lower 32-bit account ID. Record both representations.
                if identity.isdigit():
                    evidence[str(int(identity) & 0xFFFFFFFF)] = account_evidence
    except (OSError, AttributeError, TypeError):
        pass

    accounts: list[SteamAccount] = []
    for user_dir in sorted(userdata.iterdir(), key=lambda path: path.name):
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        lc = user_dir / "config" / "localconfig.vdf"
        if lc.exists():
            name, most_recent, timestamp = evidence.get(user_dir.name, (user_dir.name, False, 0))
            accounts.append(
                SteamAccount(
                    account_id=user_dir.name,
                    account_name=name,
                    localconfig_path=lc,
                    most_recent=most_recent,
                    timestamp=timestamp,
                )
            )

    if not accounts:
        raise FileNotFoundError(f"No localconfig.vdf found under {userdata}")
    return accounts


def select_steam_account(steam_root: Path, account_id: str | None = None) -> SteamAccount:
    accounts = discover_steam_accounts(steam_root)
    if account_id:
        for account in accounts:
            if account.account_id == str(account_id):
                return account
        raise FileNotFoundError(f"Steam account {account_id} has no localconfig.vdf")
    if len(accounts) == 1:
        return accounts[0]

    recent = [account for account in accounts if account.most_recent]
    if len(recent) == 1:
        return recent[0]
    highest_timestamp = max(account.timestamp for account in accounts)
    newest = [
        account
        for account in accounts
        if highest_timestamp > 0 and account.timestamp == highest_timestamp
    ]
    if len(newest) == 1:
        return newest[0]
    raise AmbiguousSteamAccountError(accounts)


def _find_localconfig(steam_root: Path, account_id: str | None = None) -> Path:
    """Return an evidence-selected account config or require explicit choice."""
    return select_steam_account(steam_root, account_id).localconfig_path


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


def get_launch_options(
    appid: str,
    steam_root: Path | None = None,
    *,
    account_id: str | None = None,
) -> str:
    """Return the launch option string for *appid* from ``localconfig.vdf``.

    Returns ``""`` if the option is not set.
    Raises ``FileNotFoundError`` if ``localconfig.vdf`` cannot be found.

    Checks for both "LaunchOptions" and "launchoptions" for compatibility.
    """
    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        raise FileNotFoundError("Steam root not found")

    localconfig_path = _find_localconfig(steam_root, account_id)
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
    *,
    account_id: str | None = None,
    environment: EnvironmentKind | None = None,
) -> None:
    """Write launch options for *appid* to ``localconfig.vdf`` using the vdf library.

    Parses the VDF file using ValvePython/vdf library, modifies the launch options
    for the specified game, and writes back with proper formatting.

    Uses a temp-file + ``os.replace`` strategy so that a crash mid-write
    cannot corrupt the config file.

    Raises ``RuntimeError`` if Steam is currently running — Steam rewrites
    ``localconfig.vdf`` on exit and would clobber any changes made here.
    """
    if _is_steam_running(environment=environment):
        raise RuntimeError(
            "Steam is currently running. Close Steam before modifying launch options — "
            "the game capture method modifies launch options that Steam locks."
        )

    if steam_root is None:
        steam_root = find_steam_root()
    if steam_root is None:
        raise FileNotFoundError("Steam root not found")

    localconfig_path = _find_localconfig(steam_root, account_id)

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
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name

    os.replace(tmp_name, localconfig_path)
    os.chmod(localconfig_path, 0o600)
    directory_fd = os.open(localconfig_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


# ---------------------------------------------------------------------------
# Capture wrapper helpers
# ---------------------------------------------------------------------------

CLIPPER_WRAPPER = game_capture.launch_options()


def quote_wrapper_path(wrapper_path: str) -> str:
    return game_capture.launch_prefix(Path(wrapper_path))


def compose_capture_options(original: str, wrapper_path: str) -> str:
    """Insert at the game command, retaining environment settings and wrappers."""
    prefix = quote_wrapper_path(wrapper_path)
    if "%command%" in original:
        return original.replace("%command%", f"{prefix} %command%", 1)
    return f"{prefix} %command%" + (f" {original}" if original else "")


def has_external_capture_wrapper(options: str) -> bool:
    return options == "obs-gamecapture" or options.startswith("obs-gamecapture ")


def prepare_capture_injection(original: str, wrapper_path: str) -> LaunchOptionMutation:
    prefix = quote_wrapper_path(wrapper_path)
    # Upgrade the exact wrapper used by older Clipper versions. Keep its old
    # text in the ownership record so removal can restore the user's options.
    base = (
        original.removeprefix("obs-gamecapture ")
        if has_external_capture_wrapper(original)
        else original
    )
    if prefix in base:
        return LaunchOptionMutation(original, original, prefix, False)
    return LaunchOptionMutation(original, compose_capture_options(base, wrapper_path), prefix, True)


def remove_managed_capture_options(
    current: str,
    *,
    original: str,
    applied: str,
    wrapper_prefix: str,
) -> str:
    """Restore exact owned text, or remove only Clipper's exact leading prefix."""
    if current == applied:
        return original
    exact_prefix = f"{wrapper_prefix} "
    if wrapper_prefix and current.count(exact_prefix) == 1:
        return current.replace(exact_prefix, "", 1)
    raise LaunchOptionConflictError(
        "Steam launch options changed and Clipper cannot prove what to remove"
    )


def inject_capture_wrapper(
    appid: str,
    steam_root: Path | None = None,
    *,
    wrapper_path: str | None = None,
    account_id: str | None = None,
    environment: EnvironmentKind | None = None,
) -> bool:
    """Add ``CLIPPER_WRAPPER`` to the game's launch options if not already present.

    Returns ``True`` if the wrapper was added, ``False`` if it was already there.
    Raises ``RuntimeError`` if Steam is running (via ``set_launch_options``).
    """
    if wrapper_path is None:
        raise CaptureProviderUnverifiedError(
            "Refusing to write a game-capture wrapper before provider verification"
        )
    current = get_launch_options(appid, steam_root, account_id=account_id)
    mutation = prepare_capture_injection(current, wrapper_path)
    if not mutation.managed:
        return False
    # Compare immediately before writing so picker previews cannot overwrite a
    # concurrent Steam/user edit.
    if get_launch_options(appid, steam_root, account_id=account_id) != mutation.original:
        raise LaunchOptionConflictError("Steam launch options changed before commit")
    set_launch_options(
        appid,
        mutation.applied,
        steam_root,
        account_id=account_id,
        environment=environment,
    )
    return True


def remove_capture_wrapper(
    appid: str,
    steam_root: Path | None = None,
    *,
    integration: dict | None = None,
    account_id: str | None = None,
    environment: EnvironmentKind | None = None,
) -> bool:
    """Remove ``CLIPPER_WRAPPER`` from the game's launch options if present.

    Returns ``True`` if the wrapper was removed, ``False`` if it was not present.
    Raises ``RuntimeError`` if Steam is running (via ``set_launch_options``).
    """
    if not isinstance(integration, dict) or not integration.get("managed_launch_options"):
        return False
    original_value = integration.get("original_launch_options")
    applied_value = integration.get("applied_launch_options")
    wrapper_prefix_value = integration.get("wrapper_prefix")
    if not isinstance(original_value, str):
        raise LaunchOptionConflictError("Integration ownership record is incomplete")
    if not isinstance(applied_value, str) or not isinstance(wrapper_prefix_value, str):
        raise LaunchOptionConflictError("Integration ownership record is incomplete")
    current = get_launch_options(appid, steam_root, account_id=account_id)
    new_opts = remove_managed_capture_options(
        current,
        original=original_value,
        applied=applied_value,
        wrapper_prefix=wrapper_prefix_value,
    )
    set_launch_options(
        appid,
        new_opts,
        steam_root,
        account_id=account_id,
        environment=environment,
    )
    return True


def _entry_identity_matches(entry: dict, appid: str, installation_id: str) -> bool:
    if str(entry.get("appid") or "").strip() != appid:
        return False
    stored_installation = str(entry.get("steam_installation") or "").strip()
    return not stored_installation or stored_installation == installation_id


def _environment_for_entry(entry: dict) -> EnvironmentKind:
    try:
        environment = EnvironmentKind(
            str(entry.get("steam_environment") or EnvironmentKind.NATIVE_STEAM.value)
        )
    except ValueError:
        return EnvironmentKind.NATIVE_STEAM
    return environment if environment in _STEAM_ENVIRONMENTS else EnvironmentKind.NATIVE_STEAM


def _replace_whitelist_entry(
    whitelist: list[dict], previous_entry: dict, replacement_entry: dict
) -> list[dict]:
    """Replace one exact entry, with a stable-identity fallback after reload."""
    for index, entry in enumerate(whitelist):
        if entry == previous_entry:
            result = list(whitelist)
            result[index] = replacement_entry
            return result

    previous_appid = str(previous_entry.get("appid") or "").strip()
    previous_installation = str(previous_entry.get("steam_installation") or "").strip()
    previous_path = str(
        previous_entry.get("executable_path") or previous_entry.get("path") or ""
    ).strip()
    for index, entry in enumerate(whitelist):
        if previous_appid:
            if str(entry.get("appid") or "").strip() != previous_appid:
                continue
            if str(entry.get("steam_installation") or "").strip() != previous_installation:
                continue
        elif previous_path:
            if (
                str(entry.get("executable_path") or entry.get("path") or "").strip()
                != previous_path
            ):
                continue
        else:
            continue
        result = list(whitelist)
        result[index] = replacement_entry
        return result
    raise ValueError("Configured game entry changed before commit")


def _integration_for_mutation(
    entry: dict,
    mutation: LaunchOptionMutation,
) -> dict:
    updated = dict(entry)
    updated["game_capture_integration"] = {
        "schema": 1,
        "environment": _environment_for_entry(entry).value,
        "provider": "clipper_native_payload",
        "managed_launch_options": mutation.managed,
        "original_launch_options": mutation.original.removeprefix("obs-gamecapture "),
        "applied_launch_options": mutation.applied,
        "wrapper_id": "clipper-gamecapture-v1",
        "wrapper_prefix": mutation.wrapper_prefix,
        "payload_version": game_capture.PAYLOAD_VERSION,
    }
    return updated


def _commit_capture_change(
    config,
    entry: dict,
    steam_root: Path,
    mutation: LaunchOptionMutation,
    operation: str,
    whitelist: list[dict],
    previous: dict | None = None,
) -> dict:
    """One write-ahead transaction for Steam options and Clipper configuration."""
    appid = str(entry.get("appid") or "")
    installation_id = str(entry.get("steam_installation") or "")
    account_id = str(entry.get("steam_account_id") or "") or None
    environment = _environment_for_entry(entry)
    if not appid.isdigit() or not installation_id:
        raise ValueError("Steam game identity is incomplete")
    if _is_steam_running(environment=environment):
        raise RuntimeError("Steam must be closed before applying launch options")
    if config.get("pending_game_capture_change") is not None:
        raise LaunchOptionConflictError("An interrupted Steam change must be recovered first")
    journal = {
        "schema": 1,
        "operation": operation,
        "steam_installation": installation_id,
        "steam_account_id": account_id or "",
        "steam_appid": appid,
        "original_launch_options": mutation.original,
        "intended_launch_options": mutation.applied,
        "entry_snapshot": entry,
        "phase": "prepared",
    }
    if previous is not None:
        journal["previous_entry_snapshot"] = previous
    config.begin_game_capture_change(journal)
    if get_launch_options(appid, steam_root, account_id=account_id) != mutation.original:
        config.clear_game_capture_change()
        raise LaunchOptionConflictError("Steam launch options changed before commit")
    set_launch_options(
        appid, mutation.applied, steam_root, account_id=account_id, environment=environment
    )
    config.update_game_capture_change_phase("launch_options_applied")
    config.set("whitelist", whitelist)
    config.update_game_capture_change_phase("entry_persisted")
    config.clear_game_capture_change()
    return entry


def apply_game_capture_add_transaction(
    config,
    game_data: dict,
    wrapper_path: str,
    steam_root: Path,
) -> dict:
    original = get_launch_options(
        game_data["appid"], steam_root, account_id=game_data.get("steam_account_id")
    )
    mutation = prepare_capture_injection(original, wrapper_path)
    entry = _integration_for_mutation(game_data, mutation)
    whitelist = list(config.get("whitelist", []))
    if any(
        _entry_identity_matches(item, entry["appid"], entry["steam_installation"])
        for item in whitelist
    ):
        raise ValueError("Steam game is already configured for this installation")
    return _commit_capture_change(config, entry, steam_root, mutation, "add", [*whitelist, entry])


def apply_game_capture_update_transaction(
    config,
    previous_entry: dict,
    game_data: dict,
    wrapper_path: str,
    steam_root: Path,
) -> dict:
    original = get_launch_options(
        game_data["appid"], steam_root, account_id=game_data.get("steam_account_id")
    )
    integration = previous_entry.get("game_capture_integration", {})
    base = original
    if integration.get("managed_launch_options"):
        base = remove_managed_capture_options(
            original,
            original=integration["original_launch_options"],
            applied=integration["applied_launch_options"],
            wrapper_prefix=integration["wrapper_prefix"],
        )
    prepared = prepare_capture_injection(base, wrapper_path)
    mutation = LaunchOptionMutation(original, prepared.applied, prepared.wrapper_prefix, True)
    entry = _integration_for_mutation(game_data, prepared)
    whitelist = _replace_whitelist_entry(list(config.get("whitelist", [])), previous_entry, entry)
    return _commit_capture_change(
        config, entry, steam_root, mutation, "replace", whitelist, previous_entry
    )


def apply_game_capture_remove_transaction(config, game_data: dict, steam_root: Path) -> None:
    integration = game_data.get("game_capture_integration", {})
    if not integration.get("managed_launch_options"):
        raise LaunchOptionConflictError("Clipper does not own this launch option")
    current = get_launch_options(
        game_data["appid"], steam_root, account_id=game_data.get("steam_account_id")
    )
    restored = remove_managed_capture_options(
        current,
        original=integration["original_launch_options"],
        applied=integration["applied_launch_options"],
        wrapper_prefix=integration["wrapper_prefix"],
    )
    whitelist = [
        item
        for item in config.get("whitelist", [])
        if not _entry_identity_matches(item, game_data["appid"], game_data["steam_installation"])
    ]
    _commit_capture_change(
        config,
        game_data,
        steam_root,
        LaunchOptionMutation(current, restored, integration["wrapper_prefix"], True),
        "remove",
        whitelist,
    )


def reconcile_pending_game_capture_change(
    config,
    installations: list[SteamInstallation] | None = None,
) -> str:
    """Conservatively reconcile one interrupted Steam mutation at startup."""
    journal = config.get("pending_game_capture_change")
    if not isinstance(journal, dict):
        return "none"

    installations = installations or discover_steam_installations()
    installation = next(
        (item for item in installations if item.stable_id == journal.get("steam_installation")),
        None,
    )
    if installation is None:
        return "installation_missing"
    if _is_steam_running(environment=installation.environment):
        return "steam_running"
    appid = str(journal["steam_appid"])
    account_id = str(journal.get("steam_account_id") or "") or None
    current = get_launch_options(appid, installation.data_root, account_id=account_id)
    original = str(journal["original_launch_options"])
    intended = str(journal["intended_launch_options"])
    if current == original:
        config.clear_game_capture_change()
        return "aborted"
    if current != intended:
        return "conflict"

    whitelist = list(config.get("whitelist", []))
    operation = journal.get("operation")
    if operation in {"replace", "disable"}:
        intended_entry = journal.get("entry_snapshot")
        if isinstance(intended_entry, dict) and any(entry == intended_entry for entry in whitelist):
            config.clear_game_capture_change()
            return "completed"
        set_launch_options(
            appid,
            original,
            installation.data_root,
            account_id=account_id,
            environment=installation.environment,
        )
        config.clear_game_capture_change()
        return "rolled_back"

    entry_exists = any(
        _entry_identity_matches(entry, appid, installation.stable_id) for entry in whitelist
    )
    if operation == "add" and not entry_exists:
        # VDF changed but no owned entry exists: rollback is the only state
        # that cannot orphan an untracked wrapper.
        set_launch_options(
            appid,
            original,
            installation.data_root,
            account_id=account_id,
            environment=installation.environment,
        )
        config.clear_game_capture_change()
        return "rolled_back"
    if operation == "remove" and entry_exists:
        # Removal did not reach config persistence. Restore the exact previous
        # launch options and keep the entry rather than partially removing it.
        set_launch_options(
            appid,
            original,
            installation.data_root,
            account_id=account_id,
            environment=installation.environment,
        )
        config.clear_game_capture_change()
        return "rolled_back"

    config.clear_game_capture_change()
    return "completed"
