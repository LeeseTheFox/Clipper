"""Learn the live PipeWire identity for a running whitelisted game."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, cast

from process_watcher import entry_matches_process

_EXECUTABLE_TOKEN = re.compile(
    r"""(?ix)
    (?:
        ["']([^"']+\.(?:exe|x86_64|x86|appimage))["']
        |
        ([^\s"']+\.(?:exe|x86_64|x86|appimage))
    )
    """
)
_GENERIC_AUDIO_IDENTITIES = {
    "",
    "alsa playback",
    "audioipc server",
    "pipewire",
    "pipewire-pulse",
    "wine",
    "wine64",
    "wine64-preloader",
    "wineserver",
}


def _basename(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return Path(text).name


def _identity_key(value: object) -> str:
    return _basename(value).casefold()


def _positive_int(value: object) -> int:
    try:
        parsed = int(cast(Any, value))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _process_identity_keys(process: dict[str, Any]) -> set[str]:
    identities = {
        _identity_key(process.get("comm")),
        _identity_key(process.get("exe")),
    }
    cmdline = str(process.get("cmdline") or "")
    for match in _EXECUTABLE_TOKEN.finditer(cmdline):
        identities.add(_identity_key(match.group(1) or match.group(2)))
    return {identity for identity in identities if identity}


def _process_audio_pids(process: dict[str, Any]) -> set[int]:
    values = [process.get("pid")]
    namespace_pids = process.get("namespace_pids")
    if isinstance(namespace_pids, list):
        values.extend(namespace_pids)

    pids: set[int] = set()
    for value in values:
        pid = _positive_int(value)
        if pid > 0:
            pids.add(pid)
    return pids


def _process_belongs_to_rule(process: dict[str, Any], entry: dict, rule_id: str) -> bool:
    rule_ids = process.get("rule_ids")
    if isinstance(rule_ids, list) and rule_id:
        return rule_id in rule_ids
    return entry_matches_process(entry, process)


def _source_identity_candidates(source: dict[str, Any]) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for score, field in (
        (300, "binary"),
        (200, "app_name"),
        (100, "display_name"),
    ):
        value = str(source.get(field) or "").strip()
        key = _identity_key(value)
        if not key or key in _GENERIC_AUDIO_IDENTITIES:
            continue
        if key.endswith((".exe", ".x86_64", ".x86", ".appimage")):
            score += 20
        candidates.append((score, _basename(value)))
    return candidates


def probe_game_audio_identity(
    entry: dict,
    rule_id: str,
    processes: list[dict[str, Any]],
    audio_sources: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Return the best live playback identity and safe probe diagnostics.

    The host helper marks every process with the whitelist rules it matches.
    Its namespace PID aliases let PipeWire's container-local process ID be
    joined back to the verified host process. Exact executable identities are
    retained as a fallback for audio servers that omit process IDs.
    """
    matched_processes: list[dict[str, Any]] = []
    game_process_identities: set[str] = set()
    game_process_pids: set[int] = set()
    for process in processes:
        if _process_belongs_to_rule(process, entry, rule_id):
            game_process_identities.update(_process_identity_keys(process))
            process_pids = _process_audio_pids(process)
            game_process_pids.update(process_pids)
            matched_processes.append(
                {
                    "pid": str(process.get("pid") or ""),
                    "namespace_pids": sorted(process_pids),
                    "comm": str(process.get("comm") or ""),
                    "exe": _basename(process.get("exe")),
                }
            )

    playback_sources = [
        {
            "pid": _positive_int(source.get("pid")),
            "binary": str(source.get("binary") or ""),
            "app_name": str(source.get("app_name") or ""),
            "display_name": str(source.get("display_name") or ""),
        }
        for source in audio_sources
        if source.get("kind") == "application"
    ]

    candidates: list[tuple[int, str, str, int]] = []
    for source in audio_sources:
        if source.get("kind") != "application":
            continue

        source_pid = _positive_int(source.get("pid"))

        for score, identity in _source_identity_candidates(source):
            identity_matches = _identity_key(identity) in game_process_identities
            pid_matches = source_pid > 0 and source_pid in game_process_pids
            if pid_matches:
                candidates.append((score + 1000, identity, "namespace PID", source_pid))
            elif identity_matches:
                candidates.append((score, identity, "process identity", source_pid))

    diagnostics = {
        "matched_processes": matched_processes,
        "playback_sources": playback_sources,
        "match_method": "",
        "match_pid": 0,
    }
    if not candidates:
        return "", diagnostics
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    _score, identity, method, pid = candidates[0]
    diagnostics["match_method"] = method
    diagnostics["match_pid"] = pid
    return identity, diagnostics


def discover_game_audio_identity(
    entry: dict,
    rule_id: str,
    processes: list[dict[str, Any]],
    audio_sources: list[dict[str, Any]],
) -> str:
    """Return the best live playback identity owned by ``entry``."""
    identity, _diagnostics = probe_game_audio_identity(entry, rule_id, processes, audio_sources)
    return identity


def format_game_audio_probe(diagnostics: dict[str, Any]) -> str:
    """Format safe, concise process and playback metadata for user logs."""
    process_labels = []
    for process in diagnostics.get("matched_processes", []):
        if not isinstance(process, dict):
            continue
        pids = ",".join(str(pid) for pid in process.get("namespace_pids", []))
        identity = str(process.get("comm") or process.get("exe") or "unknown")
        process_labels.append(f"{identity} pid={process.get('pid') or '?'} ns={pids or '-'}")

    source_labels = []
    for source in diagnostics.get("playback_sources", []):
        if not isinstance(source, dict):
            continue
        identity = str(
            source.get("binary")
            or source.get("app_name")
            or source.get("display_name")
            or "unknown"
        )
        source_labels.append(f"{identity} pid={source.get('pid') or '?'}")

    processes_text = ", ".join(process_labels[:6]) or "none"
    sources_text = ", ".join(source_labels[:6]) or "none"
    return f"game processes [{processes_text}]; playback streams [{sources_text}]"


def audio_uses_whitelisted_game(audio: object, entry: dict) -> bool:
    """Return whether enabled audio routing contains this game's source."""
    if not isinstance(audio, dict):
        return False
    appid = str(entry.get("appid") or "").strip()
    install_path = str(entry.get("install_path") or "").strip()
    for track in audio.get("tracks", []):
        if not isinstance(track, dict) or not track.get("enabled"):
            continue
        for source in track.get("sources", []):
            if not isinstance(source, dict) or source.get("kind") != "game_app":
                continue
            learned = source.get("learned_from")
            if not isinstance(learned, dict):
                continue
            if appid and str(learned.get("steam_appid") or "") == appid:
                return True
            if install_path and str(learned.get("install_path") or "") == install_path:
                return True
    return False


def apply_learned_game_audio_identity(
    whitelist: object,
    audio: object,
    entry: dict,
    identity: str,
) -> tuple[list[dict], dict, bool]:
    """Return config copies with ``identity`` persisted for ``entry``."""
    whitelist_copy = copy.deepcopy(whitelist) if isinstance(whitelist, list) else []
    audio_copy = copy.deepcopy(audio) if isinstance(audio, dict) else {}
    identity = _basename(identity)
    if not identity:
        return whitelist_copy, audio_copy, False

    appid = str(entry.get("appid") or "").strip()
    install_path = str(entry.get("install_path") or "").strip()
    changed = False

    for whitelist_entry in whitelist_copy:
        if not isinstance(whitelist_entry, dict):
            continue
        same_entry = (appid and str(whitelist_entry.get("appid") or "").strip() == appid) or (
            not appid
            and install_path
            and str(whitelist_entry.get("install_path") or "").strip() == install_path
        )
        if same_entry and whitelist_entry.get("audio_process") != identity:
            whitelist_entry["audio_process"] = identity
            changed = True
            break

    for track in audio_copy.get("tracks", []):
        if not isinstance(track, dict):
            continue
        for source in track.get("sources", []):
            if not isinstance(source, dict) or source.get("kind") != "game_app":
                continue
            learned = source.get("learned_from")
            if not isinstance(learned, dict):
                continue
            same_source = (appid and str(learned.get("steam_appid") or "").strip() == appid) or (
                not appid
                and install_path
                and str(learned.get("install_path") or "").strip() == install_path
            )
            if not same_source:
                continue
            match = source.setdefault("match", {})
            if (
                match.get("type") != "process_name"
                or match.get("value") != identity
                or match.get("priority") != "binary_first"
            ):
                source["match"] = {
                    "type": "process_name",
                    "value": identity,
                    "priority": "binary_first",
                }
                changed = True

    return whitelist_copy, audio_copy, changed
