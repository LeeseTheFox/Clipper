"""
Config persistence for Clipper.

Reads/writes JSON to $XDG_CONFIG_HOME/clipper/config.json, falling back to
~/.config/clipper/config.json when XDG_CONFIG_HOME is unset.
Safe to import without GTK — suitable for headless testing.
"""

import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from capture_modes import CAPTURE_MODE_GAME, CAPTURE_MODES, DEFAULT_CAPTURE_MODE

_INTEGRATION_ENVIRONMENTS = {
    "native_steam",
    "flatpak_steam_user",
    "flatpak_steam_system",
    "native_launcher",
    "unsupported_sandbox",
}
_INTEGRATION_PROVIDERS = {
    "external_obs_gamecapture",
    "clipper_native_payload",
    "freedesktop_vulkan_layer",
    "manual",
}
_JOURNAL_PHASES = {"prepared", "launch_options_applied", "entry_persisted"}


def normalize_game_capture_integration(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schema") != 1:
        return None
    environment = _clean_string(value.get("environment"))
    provider = _clean_string(value.get("provider"))
    if environment not in _INTEGRATION_ENVIRONMENTS:
        return None
    if provider not in _INTEGRATION_PROVIDERS:
        return None
    managed = bool(value.get("managed_launch_options", False))
    original = value.get("original_launch_options")
    applied = value.get("applied_launch_options")
    wrapper_id = value.get("wrapper_id")
    wrapper_prefix = value.get("wrapper_prefix")
    payload_version = value.get("payload_version")
    if managed and (
        not isinstance(original, str)
        or not isinstance(applied, str)
        or not applied
        or not isinstance(wrapper_id, str)
        or not wrapper_id
        or not isinstance(wrapper_prefix, str)
        or not wrapper_prefix
    ):
        return None
    return {
        "schema": 1,
        "environment": environment,
        "provider": provider,
        "managed_launch_options": managed,
        "original_launch_options": _clean_string(original),
        "applied_launch_options": _clean_string(applied),
        "wrapper_id": _clean_string(wrapper_id),
        "wrapper_prefix": _clean_string(wrapper_prefix),
        "payload_version": _clean_string(payload_version),
    }


def normalize_whitelist(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_entry in value:
        if not isinstance(raw_entry, dict):
            continue
        entry = deepcopy(raw_entry)
        integration = normalize_game_capture_integration(entry.get("game_capture_integration"))
        if integration is not None:
            entry["game_capture_integration"] = integration
        elif (
            entry.get("capture_mode") == CAPTURE_MODE_GAME and str(entry.get("appid") or "").strip()
        ):
            # Legacy literal wrappers have no ownership proof.  Preserve them
            # as external/unmanaged so removal cannot edit Steam implicitly.
            legacy_environment = _clean_string(entry.get("steam_environment"), "native_steam")
            if legacy_environment not in _INTEGRATION_ENVIRONMENTS:
                legacy_environment = "unsupported_sandbox"
            entry["game_capture_integration"] = {
                "schema": 1,
                "environment": legacy_environment,
                "provider": "external_obs_gamecapture",
                "managed_launch_options": False,
                "original_launch_options": "",
                "applied_launch_options": "",
                "wrapper_id": "legacy-external-unmanaged",
                "wrapper_prefix": "",
                "payload_version": "",
            }
        else:
            entry.pop("game_capture_integration", None)
        normalized.append(entry)
    return normalized


def normalize_pending_game_capture_change(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema") != 1:
        return None
    operation = _clean_string(value.get("operation"))
    phase = _clean_string(value.get("phase"))
    appid = _clean_string(value.get("steam_appid"))
    installation = _clean_string(value.get("steam_installation"))
    account_id = _clean_string(value.get("steam_account_id"))
    original = value.get("original_launch_options")
    intended = value.get("intended_launch_options")
    snapshot = value.get("entry_snapshot")
    if (
        operation not in {"add", "remove", "replace", "disable"}
        or phase not in _JOURNAL_PHASES
        or not appid.isdigit()
        or not installation
        or not isinstance(original, str)
        or not isinstance(intended, str)
        or not isinstance(snapshot, dict)
    ):
        return None
    normalized = {
        "schema": 1,
        "operation": operation,
        "steam_installation": installation,
        "steam_account_id": account_id,
        "steam_appid": appid,
        "original_launch_options": original,
        "intended_launch_options": intended,
        "entry_snapshot": deepcopy(snapshot),
        "phase": phase,
    }
    previous_snapshot = value.get("previous_entry_snapshot")
    if operation in {"replace", "disable"}:
        if not isinstance(previous_snapshot, dict):
            return None
        normalized["previous_entry_snapshot"] = deepcopy(previous_snapshot)
    return normalized


def default_config_dir(env: dict[str, str] | None = None) -> Path:
    environment = env if env is not None else os.environ
    config_home = environment.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "clipper"
    return Path.home() / ".config" / "clipper"


def default_config_file(env: dict[str, str] | None = None) -> Path:
    return default_config_dir(env) / "config.json"


CONFIG_DIR = default_config_dir()
CONFIG_FILE = default_config_file()
ENGINE_MANAGED_KEYS = ("pipewire_restore_token",)
AUDIO_MAX_TRACKS = 6
AUDIO_MODES = ("single_mix", "split_tracks")
AUDIO_SOURCE_KINDS = (
    "output_device",
    "input_device",
    "selected_input_device",
    "application",
    "game_app",
)
AUDIO_BACKENDS = ("pulse", "pipewire")
MIN_AUDIO_VOLUME = 0.0
MAX_AUDIO_VOLUME = 2.0
MIN_CLIP_SOUND_VOLUME = 0.0
MAX_CLIP_SOUND_VOLUME = 2.0
DEFAULT_CLIP_SOUND_VOLUME = 1.0
VIDEO_RATE_CONTROLS = ("cqp", "cbr", "vbr")
OUTPUT_FORMATS = ("mkv", "mp4", "mov", "ts")
REPLAY_BUFFER_SIZE_DEFAULT_MB = 1024
REPLAY_BUFFER_SIZE_MIN_MB = 128
REPLAY_BUFFER_SIZE_MAX_MB = 8192


def _default_audio_tracks() -> list[dict[str, Any]]:
    return [
        {
            "track": idx,
            "label": f"Track {idx}",
            "enabled": False,
            "volume": 1.0,
            "sources": [],
        }
        for idx in range(1, AUDIO_MAX_TRACKS + 1)
    ]


def default_audio_config() -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "single_mix",
        "microphone": {
            "enabled": False,
            "backend": "pulse",
            "device_id": "default",
            "display_name": "Default",
            "volume": 1.0,
        },
        "tracks": _default_audio_tracks(),
    }


def _clamp_audio_volume(value, default: float = 1.0) -> float:
    try:
        volume = float(value)
    except (TypeError, ValueError):
        volume = default
    return max(MIN_AUDIO_VOLUME, min(MAX_AUDIO_VOLUME, volume))


def normalize_clip_sound_volume(value) -> float:
    """Return a valid capture-feedback gain between silence and 200%."""
    try:
        volume = float(value)
    except (TypeError, ValueError):
        volume = DEFAULT_CLIP_SOUND_VOLUME
    return max(MIN_CLIP_SOUND_VOLUME, min(MAX_CLIP_SOUND_VOLUME, volume))


def _clean_string(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _normalize_audio_source(value) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    kind = _clean_string(value.get("kind"))
    if kind not in AUDIO_SOURCE_KINDS:
        return None

    source: dict[str, Any] = {"kind": kind}
    if kind == "selected_input_device":
        source["display_name"] = _clean_string(value.get("display_name"), "Microphone")
        return source

    if kind in ("output_device", "input_device"):
        backend = _clean_string(value.get("backend"), "pulse")
        if backend not in AUDIO_BACKENDS:
            backend = "pulse"
        source["backend"] = backend
        source["device_id"] = _clean_string(value.get("device_id"), "default")
        source["display_name"] = _clean_string(value.get("display_name"), "Default")
        return source

    source["display_name"] = _clean_string(
        value.get("display_name"),
        "Application audio" if kind == "application" else "Game audio",
    )
    match = value.get("match")
    if isinstance(match, dict):
        match_type = _clean_string(match.get("type"))
        match_value = _clean_string(match.get("value"))
        if match_type and match_value:
            source["match"] = {
                "type": match_type,
                "value": match_value,
                "priority": _clean_string(match.get("priority"), "binary_first"),
            }
    learned_from = value.get("learned_from")
    if isinstance(learned_from, dict):
        cleaned: dict[str, str] = {}
        for key in ("steam_appid", "install_path"):
            string_value = _clean_string(learned_from.get(key))
            if string_value:
                cleaned[key] = string_value
        if cleaned:
            source["learned_from"] = cleaned

    return source


def normalize_audio_config(value) -> dict[str, Any]:
    default = default_audio_config()
    if not isinstance(value, dict):
        return default

    audio = default_audio_config()
    mode = _clean_string(value.get("mode"), audio["mode"])
    if mode in AUDIO_MODES:
        audio["mode"] = mode

    mic = value.get("microphone")
    if isinstance(mic, dict):
        audio["microphone"]["enabled"] = bool(mic.get("enabled", False))
        backend = _clean_string(mic.get("backend"), "pulse")
        if backend in AUDIO_BACKENDS:
            audio["microphone"]["backend"] = backend
        audio["microphone"]["device_id"] = _clean_string(mic.get("device_id"), "default")
        audio["microphone"]["display_name"] = _clean_string(mic.get("display_name"), "Default")
        audio["microphone"]["volume"] = _clamp_audio_volume(mic.get("volume"), 1.0)

    incoming_tracks = value.get("tracks")
    if isinstance(incoming_tracks, list):
        seen_tracks: set[int] = set()
        normalized_by_track: dict[int, dict] = {}
        for item in incoming_tracks:
            if not isinstance(item, dict):
                continue
            try:
                track_number = int(item.get("track", 0))
            except (TypeError, ValueError):
                continue
            if track_number < 1 or track_number > AUDIO_MAX_TRACKS or track_number in seen_tracks:
                continue
            seen_tracks.add(track_number)

            sources = []
            raw_sources = item.get("sources", [])
            if isinstance(raw_sources, list):
                for source_item in raw_sources:
                    source = _normalize_audio_source(source_item)
                    if source is not None:
                        sources.append(source)

            label = _clean_string(item.get("label"), f"Track {track_number}")
            source_role = _clean_string(item.get("source_role"))
            if source_role not in {
                "none",
                "system_audio",
                "microphone",
                "whitelisted_games",
                "application",
                "custom",
            }:
                # Migrate the one legacy display label that used to control
                # aggregate whitelist behavior. New code never branches on a
                # translated label.
                source_role = (
                    "whitelisted_games"
                    if label == "Whitelisted games"
                    else ("custom" if sources else "none")
                )
            if (
                label.startswith("Microphone (")
                and len(sources) == 1
                and sources[0].get("kind") == "input_device"
            ):
                sources = [
                    {
                        "kind": "selected_input_device",
                        "display_name": "Microphone",
                    }
                ]
                label = "Microphone"

            normalized_track = {
                "track": track_number,
                "label": label,
                "enabled": bool(item.get("enabled", bool(sources))),
                "volume": _clamp_audio_volume(item.get("volume"), 1.0),
                "sources": sources,
            }
            if source_role != "none":
                normalized_track["source_role"] = source_role
            normalized_by_track[track_number] = normalized_track

        audio["tracks"] = [
            normalized_by_track.get(track["track"], track) for track in default["tracks"]
        ]

    return audio


DEFAULTS: dict[str, Any] = {
    "auto_check_updates": True,
    "ui_language": "system",
    "capture_mode": DEFAULT_CAPTURE_MODE,
    "pipewire_restore_token": "",
    "replay_buffer_length": 60,
    "replay_buffer_size_mb": REPLAY_BUFFER_SIZE_DEFAULT_MB,
    "fps": 60,
    "resolution": "1920x1080",
    "format": "mkv",
    "video_encoder": "obs_x264",
    "audio_encoder": "ffmpeg_aac",
    "rate_control": "cqp",
    "quality_cqp": 23,
    "video_bitrate": 12000,
    "video_max_bitrate": 20000,
    "vaapi_device": "auto",
    "output_folder": str(Path.home() / "Videos" / "Clipper"),
    "save_hotkey": "",
    "hotkey_binding_id": "save-replay-buffer",
    "save_hotkey_portal_managed": False,
    "save_hotkey_portal_label": "",
    "notify_on_clip_saved": True,
    "play_sound_on_clip_saved": False,
    "clip_sound_volume": DEFAULT_CLIP_SOUND_VOLUME,
    "start_on_boot": False,
    "autostart_background_mode_configured": False,
    "setup_completed": False,
    "display_capture_guidance_seen": False,
    "minimize_to_tray_on_close": False,
    "remember_window_sizes": True,
    "whitelist": [],
    "pending_game_capture_change": None,
    "clip_game_metadata": {"clips": {}},
    "audio": default_audio_config(),
    "audio_assignments": [],
    "fruit_drop_high_score": 0,
}


class ClipperConfig:
    """Persistent configuration backed by a JSON file.

    Parameters
    ----------
    config_path:
        Path to the JSON config file. Defaults to
        ``$XDG_CONFIG_HOME/clipper/config.json`` or
        ``~/.config/clipper/config.json`` when XDG_CONFIG_HOME is unset. Pass
        a custom path in tests to avoid touching the real config directory.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._path: Path = Path(config_path) if config_path is not None else default_config_file()
        self._data: dict = {}
        self._load_status = "defaults_missing"
        self._saved_key_count = 0
        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Return the config file consumed by Clipper and its host monitor."""
        return self._path

    @property
    def load_status(self) -> str:
        """Describe whether the current data came from disk or defaults."""
        return self._load_status

    @property
    def saved_key_count(self) -> int:
        """Return the number of top-level keys read from the saved config."""
        return self._saved_key_count

    def load(self) -> None:
        """Load config from disk, merging with defaults."""
        self._data = deepcopy(DEFAULTS)
        self._load_status = "defaults_missing"
        self._saved_key_count = 0

        if not self._path.exists():
            return

        try:
            text = self._path.read_text(encoding="utf-8")
            saved = json.loads(text)
            if not isinstance(saved, dict):
                raise ValueError("Config root must be a JSON object")
            self._load_status = "loaded"
            self._saved_key_count = len(saved)
            self._data.update(saved)
            if self._data.get("capture_mode") not in CAPTURE_MODES:
                print(
                    "clipper: warning: invalid capture_mode; using default",
                    file=sys.stderr,
                )
                self._data["capture_mode"] = DEFAULTS["capture_mode"]
            if self._data.get("rate_control") not in VIDEO_RATE_CONTROLS:
                print(
                    "clipper: warning: invalid rate_control; using default",
                    file=sys.stderr,
                )
                self._data["rate_control"] = DEFAULTS["rate_control"]
            if self._data.get("format") not in OUTPUT_FORMATS:
                print(
                    "clipper: warning: invalid format; using default",
                    file=sys.stderr,
                )
                self._data["format"] = DEFAULTS["format"]
            self._data["audio"] = normalize_audio_config(self._data.get("audio"))
            self._data["clip_sound_volume"] = normalize_clip_sound_volume(
                self._data.get("clip_sound_volume")
            )
            self._data["whitelist"] = normalize_whitelist(self._data.get("whitelist"))
            self._data["pending_game_capture_change"] = normalize_pending_game_capture_change(
                self._data.get("pending_game_capture_change")
            )
            high_score = self._data.get("fruit_drop_high_score")
            self._data["fruit_drop_high_score"] = (
                high_score if type(high_score) is int and high_score >= 0 else 0
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"clipper: warning: could not read config ({exc}); using defaults",
                file=sys.stderr,
            )
            self._data = deepcopy(DEFAULTS)
            self._load_status = "defaults_invalid"
            self._saved_key_count = 0

    def save(self) -> None:
        """Write config to disk atomically (temp-file + rename)."""
        self._save_data(preserve_engine_managed=True)

    def _save_data(self, preserve_engine_managed: bool) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data_to_save = dict(self._data)

        if preserve_engine_managed:
            try:
                existing = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                existing = {}

            if isinstance(existing, dict):
                for key in ENGINE_MANAGED_KEYS:
                    if not data_to_save.get(key) and existing.get(key):
                        data_to_save[key] = existing[key]

        dir_fd = self._path.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dir_fd,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(data_to_save, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name

        os.replace(tmp_name, self._path)
        os.chmod(self._path, 0o600)
        directory_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._data = data_to_save

    def get(self, key: str, default=None):
        """Return the value for *key*, or *default* if not present."""
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        """Set *key* to *value* and immediately persist to disk."""
        if key == "capture_mode" and value not in CAPTURE_MODES:
            raise ValueError(f"capture_mode must be one of: {', '.join(CAPTURE_MODES)}")
        if key == "rate_control" and value not in VIDEO_RATE_CONTROLS:
            raise ValueError(f"rate_control must be one of: {', '.join(VIDEO_RATE_CONTROLS)}")
        if key == "format" and value not in OUTPUT_FORMATS:
            raise ValueError(f"format must be one of: {', '.join(OUTPUT_FORMATS)}")
        if key == "audio":
            value = normalize_audio_config(value)
        if key == "clip_sound_volume":
            value = normalize_clip_sound_volume(value)
        if key == "whitelist":
            value = normalize_whitelist(value)
        if key == "pending_game_capture_change":
            value = normalize_pending_game_capture_change(value)
        if key == "fruit_drop_high_score":
            value = value if type(value) is int and value >= 0 else 0
        previous = self._data.copy()
        self._data[key] = value
        try:
            self.save()
        except OSError:
            self._data = previous
            raise

    def begin_game_capture_change(self, journal: dict[str, Any]) -> None:
        normalized = normalize_pending_game_capture_change(journal)
        if normalized is None:
            raise ValueError("Invalid game-capture recovery journal")
        self.set("pending_game_capture_change", normalized)

    def update_game_capture_change_phase(self, phase: str) -> None:
        journal = self._data.get("pending_game_capture_change")
        if not isinstance(journal, dict) or phase not in _JOURNAL_PHASES:
            raise ValueError("No valid pending game-capture change")
        journal = dict(journal)
        journal["phase"] = phase
        self.set("pending_game_capture_change", journal)

    def clear_game_capture_change(self) -> None:
        self.set("pending_game_capture_change", None)

    def reset_to_defaults(self) -> None:
        """Reset all settings to built-in defaults and save."""
        from window_state import clear_window_state

        clear_window_state(self._path)
        self._data = deepcopy(DEFAULTS)
        self.save()

    def clear_pipewire_restore_token(self) -> None:
        """Clear the persisted PipeWire portal restore token."""
        self._data["pipewire_restore_token"] = ""
        self._save_data(preserve_engine_managed=False)

    def reset_to_factory_settings(self) -> None:
        """Reset all settings and clear engine-managed persisted state."""
        from window_state import clear_window_state

        clear_window_state(self._path)
        self._data = deepcopy(DEFAULTS)
        self._save_data(preserve_engine_managed=False)

    # ------------------------------------------------------------------
    # Mapping-style access
    # ------------------------------------------------------------------

    def __getitem__(self, key: str):
        return self._data[key]

    def __setitem__(self, key: str, value) -> None:
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:  # pragma: no cover
        return f"ClipperConfig({self._path!r}, {self._data!r})"
