"""
Process matching helpers for whitelist-gated display capture.

Safe to import without GTK.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from capture_modes import capture_mode_for_entry

_DELETED_PATH_SUFFIX = " (deleted)"
_EXECUTABLE_NAME_BOUNDARY = r"A-Za-z0-9_.-"
_OBS_STUDIO_EXECUTABLE_NAMES = {"obs", "obs-studio"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_cmdline(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _read_environ(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _strip_deleted_path_suffix(value: str) -> str:
    value = value.strip()
    if value.endswith(_DELETED_PATH_SUFFIX):
        return value[: -len(_DELETED_PATH_SUFFIX)]
    return value


def _basename(value: str) -> str:
    value = _strip_deleted_path_suffix(str(value or "")).replace("\\", "/")
    return Path(value).name.lower()


def _normalize_path(value: str) -> str:
    value = _strip_deleted_path_suffix(value).strip()
    if len(value) > 1:
        value = value.rstrip("/")
    return value.lower()


def _home_path_aliases(path: str) -> set[str]:
    if path.startswith("/home/"):
        return {"/var/home/" + path[len("/home/") :]}
    if path.startswith("/var/home/"):
        return {"/home/" + path[len("/var/home/") :]}
    return set()


def _path_variants(value: object) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()

    variants: set[str] = set()
    pending = [raw, os.path.expanduser(raw)]
    seen: set[str] = set()
    while pending:
        candidate = _strip_deleted_path_suffix(pending.pop()).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        variants.add(_normalize_path(candidate))

        pending.extend(_home_path_aliases(candidate))
        real_candidate = os.path.realpath(candidate)
        if real_candidate != candidate:
            pending.append(real_candidate)

    return {variant for variant in variants if variant}


def _same_or_descendant_path(path: str, root: str) -> bool:
    if not path or not root:
        return False

    path = path.rstrip("/")
    root = root.rstrip("/")
    if root == "/":
        return path == "/"
    return path == root or path.startswith(root + "/")


def _path_matches(path: str, root: str) -> bool:
    return any(
        _same_or_descendant_path(path_variant, root_variant)
        for path_variant in _path_variants(path)
        for root_variant in _path_variants(root)
    )


def _path_is_mentioned(haystack: str, path: str) -> bool:
    haystack = _norm(haystack)
    if not haystack:
        return False
    return any(
        re.search(rf"{re.escape(variant)}(?=$|[\s\"';:]|/)", haystack) is not None
        for variant in _path_variants(path)
    )


def _executable_name_is_mentioned(haystack: str, executable_name: str) -> bool:
    haystack = _norm(haystack)
    executable_name = _norm(executable_name)
    if not haystack or not executable_name:
        return False

    for token in haystack.split():
        if _basename(token.strip("\"'")) == executable_name:
            return True

    pattern = (
        rf"(?<![{_EXECUTABLE_NAME_BOUNDARY}])"
        rf"{re.escape(executable_name)}"
        rf"(?![{_EXECUTABLE_NAME_BOUNDARY}])"
    )
    return re.search(pattern, haystack) is not None


def _process_references_path(process: dict[str, str], path: str) -> bool:
    return (
        _path_matches(str(process.get("exe") or ""), path)
        or _path_matches(str(process.get("cwd") or ""), path)
        or _path_is_mentioned(str(process.get("cmdline") or ""), path)
        or _path_is_mentioned(str(process.get("environ") or ""), path)
    )


def _process_references_executable_name(
    process: dict[str, str],
    executable_name: str,
) -> bool:
    executable_name = _norm(executable_name)
    if not executable_name:
        return False

    comm = _norm(process.get("comm"))
    exe_name = _basename(str(process.get("exe") or ""))
    return (
        executable_name == comm
        or executable_name == exe_name
        or _executable_name_is_mentioned(
            str(process.get("cmdline") or ""), executable_name
        )
        or _executable_name_is_mentioned(
            str(process.get("environ") or ""), executable_name
        )
    )


def _entry_executable_paths(entry: dict) -> set[str]:
    paths: set[str] = set()
    for key in ("executable_path", "path"):
        value = str(entry.get(key) or "").strip()
        if value and ("/" in value or "\\" in value):
            paths.add(value)
    return paths


def _entry_executable_names(entry: dict) -> set[str]:
    names: set[str] = set()
    executable_name = _norm(entry.get("executable_name"))
    if executable_name:
        names.add(executable_name)

    for key in ("executable_path", "path"):
        value = str(entry.get(key) or "").strip()
        if not value:
            continue
        if "/" in value or "\\" in value:
            basename = _basename(value)
            if basename:
                names.add(basename)
        else:
            names.add(_norm(value))

    return names


def entries_share_executable_identity(first: dict, second: dict) -> bool:
    """Return True when two whitelist entries identify the same executable."""
    first_appid = str(first.get("appid") or "").strip()
    second_appid = str(second.get("appid") or "").strip()
    if first_appid and first_appid == second_appid:
        return True

    first_paths = _entry_executable_paths(first)
    second_paths = _entry_executable_paths(second)
    for first_path in first_paths:
        first_variants = _path_variants(first_path)
        if not first_variants:
            continue
        for second_path in second_paths:
            if first_variants & _path_variants(second_path):
                return True

    first_install_path = str(first.get("install_path") or "").strip()
    second_install_path = str(second.get("install_path") or "").strip()
    if first_install_path:
        for second_path in second_paths:
            if _path_matches(second_path, first_install_path):
                return True
    if second_install_path:
        for first_path in first_paths:
            if _path_matches(first_path, second_install_path):
                return True

    if not first_paths or not second_paths:
        return bool(_entry_executable_names(first) & _entry_executable_names(second))

    return False


def monitor_rule_id(entry: dict, index: int) -> str:
    """Return the host-monitor rule id for a whitelist entry."""
    appid = str(entry.get("appid") or "").strip()
    if appid:
        return f"steam-{appid}"

    executable_path = str(entry.get("executable_path") or "").strip()
    if executable_path:
        return f"path-{_stable_rule_hash(executable_path)}"

    return f"rule-{index}"


def _stable_rule_hash(value: str) -> str:
    """Return the same FNV-1a 64-bit hash used by clipper-monitor-host."""
    hash_value = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        hash_value ^= byte
        hash_value = (hash_value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{hash_value:016x}"


def _contains_steam_app_id(haystack: str, appid: str) -> bool:
    if not haystack or not appid:
        return False

    tokens = haystack.replace(";", " ").split()
    appid_keys = {
        "steamappid",
        "steamgameid",
        "steam_app_id",
        "steam_game_id",
        "steam_compat_app_id",
        "steam_compat_data_app_id",
        "steamoverlaygameid",
    }
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep and key.lower() in appid_keys and value == appid:
            return True

    return (
        f"/compatdata/{appid}/" in haystack
        or f"/compatdata/{appid} " in haystack
        or haystack.endswith(f"/compatdata/{appid}")
    )


def iter_processes(proc_root: Path = Path("/proc")) -> list[dict[str, str]]:
    """Return process metadata relevant to whitelist matching."""
    processes: list[dict[str, str]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return processes

    for proc_dir in sorted(
        entries, key=lambda path: int(path.name) if path.name.isdigit() else -1
    ):
        if not proc_dir.name.isdigit():
            continue

        comm = _read_text(proc_dir / "comm")
        cmdline = _read_cmdline(proc_dir / "cmdline")
        environ = _read_environ(proc_dir / "environ")
        exe = _readlink(proc_dir / "exe")
        cwd = _readlink(proc_dir / "cwd")

        if not any((comm, cmdline, environ, exe, cwd)):
            continue

        processes.append(
            {
                "pid": proc_dir.name,
                "comm": comm,
                "cmdline": cmdline,
                "environ": environ,
                "exe": exe,
                "cwd": cwd,
            }
        )

    return processes


def is_obs_studio_process(process: dict[str, str]) -> bool:
    """Return whether process metadata identifies the OBS Studio executable.

    Deliberately inspect only exact executable names. Looking for ``obs`` in
    command lines or environments would mistake Clipper's own libobs-backed
    engine (and games launched through obs-gamecapture) for OBS Studio.
    """
    comm = _norm(process.get("comm"))
    exe_name = _basename(str(process.get("exe") or ""))
    return (
        comm in _OBS_STUDIO_EXECUTABLE_NAMES
        or exe_name in _OBS_STUDIO_EXECUTABLE_NAMES
    )


def find_running_obs_studio(
    proc_root: Path = Path("/proc"),
) -> dict[str, str] | None:
    """Return the first running OBS Studio process visible in ``proc_root``."""
    return next(
        (process for process in iter_processes(proc_root) if is_obs_studio_process(process)),
        None,
    )


def _display_name_for_process(process: dict[str, str]) -> str:
    comm = str(process.get("comm") or "").strip()
    if comm:
        return comm

    exe_name = Path(str(process.get("exe") or "")).name
    if exe_name:
        return exe_name

    cmdline = str(process.get("cmdline") or "").strip()
    if cmdline:
        return Path(cmdline.split()[0]).name

    return ""


def _match_path_for_process(process: dict[str, str], name: str) -> str:
    exe = str(process.get("exe") or "").strip()
    if exe:
        return exe
    return name


def process_choices(processes: list[dict[str, str]]) -> list[dict[str, str]]:
    """Convert process metadata into display-safe whitelist picker choices."""
    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for process in processes:
        name = _display_name_for_process(process)
        if not name:
            continue

        path = _match_path_for_process(process, name)
        pid = str(process.get("pid") or "").strip()
        key = (name.lower(), path.lower())
        if key in seen:
            continue
        seen.add(key)

        choices.append(
            {
                "pid": pid,
                "name": name,
                "path": path,
                "cmdline": str(process.get("cmdline") or ""),
            }
        )

    return sorted(
        choices,
        key=lambda item: (item["name"].lower(), item["path"].lower(), item["pid"]),
    )


def running_process_choices(proc_root: Path = Path("/proc")) -> list[dict[str, str]]:
    """Return display-safe running process choices from the local /proc."""
    return process_choices(iter_processes(proc_root))


def entry_matches_process(entry: dict, process: dict[str, str]) -> bool:
    """Return True when a whitelist entry appears to describe a process."""
    comm = _norm(process.get("comm"))
    cmdline = str(process.get("cmdline") or "")
    environ = str(process.get("environ") or "")
    exe = str(process.get("exe") or "")
    cwd = str(process.get("cwd") or "")
    cmdline_norm = _norm(cmdline)
    environ_norm = _norm(environ)
    exe_norm = _norm(exe)
    cwd_norm = _norm(cwd)
    exe_name = _basename(exe)

    install_path = str(entry.get("install_path") or "").strip()
    if install_path:
        if (
            _path_matches(exe, install_path)
            or _path_matches(cwd, install_path)
            or _path_is_mentioned(cmdline, install_path)
            or _path_is_mentioned(environ, install_path)
        ):
            return True

    appid = str(entry.get("appid") or "").strip()
    if appid and (
        _contains_steam_app_id(cmdline_norm, appid)
        or _contains_steam_app_id(environ_norm, appid)
        or _contains_steam_app_id(exe_norm, appid)
        or _contains_steam_app_id(cwd_norm, appid)
    ):
        return True

    executable_names: list[str] = []
    executable_name = _norm(entry.get("executable_name"))
    if executable_name:
        executable_names.append(executable_name)

    for key in ("executable_path", "path"):
        value = str(entry.get(key) or "").strip()
        if not value:
            continue
        if "/" in value or "\\" in value:
            if _process_references_path(process, value):
                return True
            basename = _basename(value)
            if basename:
                executable_names.append(basename)
        else:
            executable_names.append(_norm(value))

    for executable_name in executable_names:
        if _process_references_executable_name(process, executable_name):
            return True

    for key in ("path", "name"):
        value = _norm(entry.get(key))
        if not value:
            continue

        if "/" in value or "\\" in value:
            if _process_references_path(process, value):
                return True
            continue

        if value == comm or value == exe_name:
            return True

        if value in cmdline_norm.split() or value in environ_norm.split():
            return True

    return False


def is_whitelisted_game_running(
    whitelist: list[dict],
    proc_root: Path = Path("/proc"),
    capture_mode: str | None = None,
) -> bool:
    """Return True if any whitelist entry matches a currently running process."""
    return find_running_whitelist_entry(whitelist, proc_root, capture_mode) is not None


def find_running_whitelist_entry(
    whitelist: list[dict],
    proc_root: Path = Path("/proc"),
    capture_mode: str | None = None,
) -> dict | None:
    """Return the first whitelist entry that matches a running process."""
    entries = [entry for entry in whitelist if isinstance(entry, dict)]
    if capture_mode is not None:
        entries = [
            entry for entry in entries if capture_mode_for_entry(entry) == capture_mode
        ]
    if not entries:
        return None

    processes = iter_processes(proc_root)
    for entry in entries:
        if any(entry_matches_process(entry, process) for process in processes):
            return entry

    return None
