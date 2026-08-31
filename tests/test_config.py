"""
Tests for ui/config.py — run headlessly (no GTK required).

Usage:
    PYTHONPATH=ui venv/bin/pytest tests/ -v
"""

import json
from pathlib import Path

from config import (
    AUDIO_MAX_TRACKS,
    DEFAULTS,
    OUTPUT_FORMATS,
    VIDEO_RATE_CONTROLS,
    ClipperConfig,
    default_config_file,
    normalize_audio_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, filename: str = "config.json") -> ClipperConfig:
    """Return a ClipperConfig pointed at a temp directory."""
    return ClipperConfig(config_path=tmp_path / filename)


# ---------------------------------------------------------------------------
# 1. Fresh config — no file on disk
# ---------------------------------------------------------------------------


def test_defaults_when_no_file(tmp_path):
    """All keys should return the built-in default values when no file exists."""
    cfg = make_config(tmp_path)

    for key, expected in DEFAULTS.items():
        assert cfg[key] == expected, f"Default mismatch for '{key}'"

    assert cfg.load_status == "defaults_missing"
    assert cfg.saved_key_count == 0


def test_fresh_setup_has_no_default_hotkey(tmp_path):
    cfg = make_config(tmp_path)

    assert cfg["save_hotkey"] == ""


def test_replay_buffer_size_defaults_to_1024_mib(tmp_path):
    cfg = make_config(tmp_path)

    assert cfg["replay_buffer_size_mb"] == 1024


def test_no_file_created_on_init(tmp_path):
    """Creating a ClipperConfig should NOT write a file by itself."""
    cfg_path = tmp_path / "config.json"
    ClipperConfig(config_path=cfg_path)
    assert not cfg_path.exists()


def test_default_config_file_uses_xdg_config_home(tmp_path):
    path = default_config_file({"XDG_CONFIG_HOME": str(tmp_path / "xdg-config")})

    assert path == tmp_path / "xdg-config" / "clipper" / "config.json"


def test_default_config_path_is_resolved_at_construction(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    cfg = ClipperConfig()
    cfg.set("fps", 48)

    cfg_path = tmp_path / "xdg-config" / "clipper" / "config.json"
    assert cfg_path.exists()
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["fps"] == 48


# ---------------------------------------------------------------------------
# 2. Save + load round-trip
# ---------------------------------------------------------------------------


def test_save_and_reload(tmp_path):
    """Values written via set() should survive a fresh load()."""
    cfg = make_config(tmp_path)
    cfg.set("fps", 30)
    cfg.set("resolution", "2560x1440")

    cfg2 = make_config(tmp_path)
    assert cfg2["fps"] == 30
    assert cfg2["resolution"] == "2560x1440"
    assert cfg2.load_status == "loaded"
    assert cfg2.saved_key_count == len(json.loads((tmp_path / "config.json").read_text()))


def test_capture_mode_default_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["capture_mode"] == "display_capture"

    cfg.set("capture_mode", "game_capture")
    cfg2 = make_config(tmp_path)
    assert cfg2["capture_mode"] == "game_capture"


def test_pipewire_restore_token_default_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["pipewire_restore_token"] == ""

    cfg.set("pipewire_restore_token", "token-123")
    cfg2 = make_config(tmp_path)
    assert cfg2["pipewire_restore_token"] == "token-123"


def test_save_preserves_engine_managed_pipewire_restore_token(tmp_path):
    cfg = make_config(tmp_path)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"pipewire_restore_token": "engine-token"}),
        encoding="utf-8",
    )

    cfg.set("fps", 30)

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["fps"] == 30
    assert saved["pipewire_restore_token"] == "engine-token"


def test_clear_pipewire_restore_token_bypasses_preservation(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("pipewire_restore_token", "engine-token")

    cfg.clear_pipewire_restore_token()

    cfg2 = make_config(tmp_path)
    assert cfg2["pipewire_restore_token"] == ""


def test_reset_to_defaults_preserves_pipewire_restore_token(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("pipewire_restore_token", "engine-token")
    cfg.set("fps", 30)

    cfg.reset_to_defaults()

    cfg2 = make_config(tmp_path)
    assert cfg2["fps"] == DEFAULTS["fps"]
    assert cfg2["pipewire_restore_token"] == "engine-token"


def test_reset_to_factory_settings_clears_pipewire_restore_token(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("pipewire_restore_token", "engine-token")
    cfg.set("fps", 30)

    cfg.reset_to_factory_settings()

    cfg2 = make_config(tmp_path)
    assert cfg2["fps"] == DEFAULTS["fps"]
    assert cfg2["pipewire_restore_token"] == ""


def test_invalid_capture_mode_rejected(tmp_path):
    cfg = make_config(tmp_path)

    try:
        cfg.set("capture_mode", "window_capture")
    except ValueError as exc:
        assert "capture_mode" in str(exc)
    else:
        raise AssertionError("Expected invalid capture_mode to raise ValueError")


def test_output_formats_support_multiple_audio_tracks():
    assert OUTPUT_FORMATS == ("mkv", "mp4", "mov", "ts")


def test_unsupported_output_format_is_rejected(tmp_path):
    cfg = make_config(tmp_path)

    try:
        cfg.set("format", "flv")
    except ValueError as exc:
        assert "format" in str(exc)
    else:
        raise AssertionError("Expected unsupported format to raise ValueError")


def test_legacy_flv_output_format_loads_as_default(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"format": "flv"}), encoding="utf-8")

    cfg = ClipperConfig(config_path=config_path)

    assert cfg["format"] == DEFAULTS["format"]


def test_bracket_set_saves(tmp_path):
    """config["key"] = value should persist to disk."""
    cfg = make_config(tmp_path)
    cfg["save_hotkey"] = "ctrl+shift+f12"

    cfg2 = make_config(tmp_path)
    assert cfg2["save_hotkey"] == "ctrl+shift+f12"


def test_portal_hotkey_display_state_round_trips_verbatim(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("save_hotkey_portal_label", "Strg+Ö")
    cfg.set("save_hotkey_portal_managed", True)

    cfg2 = make_config(tmp_path)
    assert cfg2["save_hotkey"] == DEFAULTS["save_hotkey"]
    assert cfg2["save_hotkey_portal_label"] == "Strg+Ö"
    assert cfg2["save_hotkey_portal_managed"] is True


def test_minimize_to_tray_on_close_default_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["minimize_to_tray_on_close"] is False

    cfg.set("minimize_to_tray_on_close", True)

    cfg2 = make_config(tmp_path)
    assert cfg2["minimize_to_tray_on_close"] is True


def test_remember_window_sizes_default_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["remember_window_sizes"] is True

    cfg.set("remember_window_sizes", False)

    cfg2 = make_config(tmp_path)
    assert cfg2["remember_window_sizes"] is False


def test_clip_capture_notification_default_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["notify_on_clip_saved"] is True

    cfg.set("notify_on_clip_saved", False)

    cfg2 = make_config(tmp_path)
    assert cfg2["notify_on_clip_saved"] is False


def test_clip_capture_sound_default_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["play_sound_on_clip_saved"] is False

    cfg.set("play_sound_on_clip_saved", True)

    cfg2 = make_config(tmp_path)
    assert cfg2["play_sound_on_clip_saved"] is True


def test_rate_control_defaults_and_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg["rate_control"] == "cqp"
    assert cfg["quality_cqp"] == 23
    assert cfg["video_bitrate"] == 12000
    assert cfg["video_max_bitrate"] == 20000

    cfg.set("rate_control", "vbr")
    cfg.set("video_bitrate", 16000)
    cfg.set("video_max_bitrate", 24000)

    cfg2 = make_config(tmp_path)
    assert cfg2["rate_control"] == "vbr"
    assert cfg2["video_bitrate"] == 16000
    assert cfg2["video_max_bitrate"] == 24000


def test_invalid_rate_control_rejected(tmp_path):
    cfg = make_config(tmp_path)

    try:
        cfg.set("rate_control", "abr")
    except ValueError as exc:
        assert "rate_control" in str(exc)
        assert ", ".join(VIDEO_RATE_CONTROLS) in str(exc)
    else:
        raise AssertionError("Expected invalid rate_control to raise ValueError")


def test_invalid_rate_control_in_file_uses_default(tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"rate_control": "abr"}), encoding="utf-8")

    cfg = ClipperConfig(config_path=cfg_path)

    captured = capsys.readouterr()
    assert "rate_control" in captured.err
    assert cfg["rate_control"] == DEFAULTS["rate_control"]


# ---------------------------------------------------------------------------
# 3. Partial config on disk — missing keys filled with defaults
# ---------------------------------------------------------------------------


def test_partial_config_filled_with_defaults(tmp_path):
    """Keys absent from the saved file should be filled with defaults."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"fps": 30}), encoding="utf-8")

    cfg = ClipperConfig(config_path=cfg_path)

    assert cfg["fps"] == 30  # saved value wins
    # Every other key should still be the default
    for key, expected in DEFAULTS.items():
        if key == "fps":
            continue
        assert cfg[key] == expected, f"Expected default for '{key}'"


# ---------------------------------------------------------------------------
# 4. Corrupt JSON — falls back to defaults without crashing
# ---------------------------------------------------------------------------


def test_corrupt_json_falls_back_to_defaults(tmp_path, capsys):
    """Unreadable JSON should not crash; defaults should be used instead."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{ this is not valid JSON !!!", encoding="utf-8")

    cfg = ClipperConfig(config_path=cfg_path)

    # A warning must be printed to stderr
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()

    # All values should be defaults
    for key, expected in DEFAULTS.items():
        assert cfg[key] == expected, f"Expected default for '{key}' after corrupt file"
    assert cfg.load_status == "defaults_invalid"
    assert cfg.saved_key_count == 0


def test_non_object_json_falls_back_to_defaults(tmp_path, capsys):
    """A JSON file whose root is not an object should also fall back."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    cfg = ClipperConfig(config_path=cfg_path)

    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()

    for key, expected in DEFAULTS.items():
        assert cfg[key] == expected


# ---------------------------------------------------------------------------
# 5. Atomic write — file exists and is valid JSON after save
# ---------------------------------------------------------------------------


def test_save_creates_valid_json_file(tmp_path):
    """After save(), the config file must exist and parse as valid JSON."""
    cfg = make_config(tmp_path)
    cfg.save()

    cfg_path = tmp_path / "config.json"
    assert cfg_path.exists()

    parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)


def test_atomic_write_no_tmp_leftover(tmp_path):
    """No .tmp file should remain after a successful save."""
    cfg = make_config(tmp_path)
    cfg.save()

    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], f"Unexpected temp files: {leftover}"


# ---------------------------------------------------------------------------
# 6. reset_to_defaults()
# ---------------------------------------------------------------------------


def test_reset_to_defaults(tmp_path):
    """After reset_to_defaults(), every value should equal the built-in default."""
    cfg = make_config(tmp_path)
    cfg.set("fps", 30)
    cfg.set("resolution", "3840x2160")
    cfg.set("start_on_boot", True)

    cfg.reset_to_defaults()

    for key, expected in DEFAULTS.items():
        assert cfg[key] == expected, f"'{key}' not reset to default"


def test_reset_persists_to_disk(tmp_path):
    """reset_to_defaults() should write defaults to disk so a reload agrees."""
    cfg = make_config(tmp_path)
    cfg.set("fps", 30)
    cfg.reset_to_defaults()

    cfg2 = make_config(tmp_path)
    assert cfg2["fps"] == DEFAULTS["fps"]


# ---------------------------------------------------------------------------
# 7. Type preservation across a round-trip
# ---------------------------------------------------------------------------


def test_int_type_preserved(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("replay_buffer_length", 120)
    cfg2 = make_config(tmp_path)
    value = cfg2["replay_buffer_length"]
    assert isinstance(value, int), f"Expected int, got {type(value)}"
    assert value == 120


def test_replay_buffer_size_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("replay_buffer_size_mb", 2048)

    cfg2 = make_config(tmp_path)
    assert cfg2["replay_buffer_size_mb"] == 2048


def test_bool_type_preserved(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("start_on_boot", True)
    cfg2 = make_config(tmp_path)
    value = cfg2["start_on_boot"]
    # JSON booleans must not be decoded as integers
    assert isinstance(value, bool), f"Expected bool, got {type(value)}"
    assert value is True


def test_list_type_preserved(tmp_path):
    cfg = make_config(tmp_path)
    entries = [{"name": "Game", "path": "/usr/bin/game", "method": "executable"}]
    cfg.set("whitelist", entries)
    cfg2 = make_config(tmp_path)
    value = cfg2["whitelist"]
    assert isinstance(value, list), f"Expected list, got {type(value)}"
    assert value == entries


def test_audio_default_schema(tmp_path):
    cfg = make_config(tmp_path)
    audio = cfg["audio"]

    assert audio["version"] == 1
    assert audio["mode"] == "single_mix"
    assert audio["microphone"]["enabled"] is False
    assert audio["microphone"]["device_id"] == "default"
    assert audio["microphone"]["volume"] == 1.0
    assert len(audio["tracks"]) == AUDIO_MAX_TRACKS
    assert [track["track"] for track in audio["tracks"]] == list(
        range(1, AUDIO_MAX_TRACKS + 1)
    )


def test_audio_config_round_trip_and_validation(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set(
        "audio",
        {
            "version": 1,
            "mode": "split_tracks",
            "microphone": {
                "enabled": True,
                "backend": "pulse",
                "device_id": "alsa_input.usb-test",
                "display_name": "USB Mic",
                "volume": 1.25,
            },
            "tracks": [
                {
                    "track": 2,
                    "label": "Mic",
                    "enabled": True,
                    "volume": 0.75,
                    "sources": [
                        {
                            "kind": "input_device",
                            "backend": "pulse",
                            "device_id": "alsa_input.usb-test",
                            "display_name": "USB Mic",
                        }
                    ],
                }
            ],
        },
    )

    cfg2 = make_config(tmp_path)
    audio = cfg2["audio"]
    assert audio["mode"] == "split_tracks"
    assert audio["microphone"]["enabled"] is True
    assert audio["microphone"]["volume"] == 1.25
    assert audio["tracks"][1]["label"] == "Mic"
    assert audio["tracks"][1]["volume"] == 0.75
    assert audio["tracks"][1]["sources"][0]["kind"] == "input_device"


def test_audio_config_migrates_legacy_microphone_track_source(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set(
        "audio",
        {
            "mode": "split_tracks",
            "tracks": [
                {
                    "track": 2,
                    "label": "Microphone (Default)",
                    "enabled": True,
                    "sources": [
                        {
                            "kind": "input_device",
                            "backend": "pulse",
                            "device_id": "default",
                            "display_name": "Default",
                        }
                    ],
                }
            ],
        },
    )

    cfg2 = make_config(tmp_path)
    track = cfg2["audio"]["tracks"][1]

    assert track["label"] == "Microphone"
    assert track["sources"] == [
        {
            "kind": "selected_input_device",
            "display_name": "Microphone",
        }
    ]


def test_audio_config_preserves_game_app_source_set(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set(
        "audio",
        {
            "mode": "split_tracks",
            "tracks": [
                {
                    "track": 1,
                    "label": "Games",
                    "enabled": True,
                    "sources": [
                        {
                            "kind": "game_app",
                            "display_name": "Counter-Strike 2",
                            "match": {
                                "type": "process_name",
                                "value": "cs2",
                                "priority": "binary_first",
                            },
                            "learned_from": {
                                "steam_appid": "730",
                                "install_path": "/games/Counter-Strike 2",
                            },
                        },
                        {
                            "kind": "game_app",
                            "display_name": "Manual Game",
                            "match": {
                                "type": "process_name",
                                "value": "manual-game",
                            },
                        },
                    ],
                }
            ],
        },
    )

    cfg2 = make_config(tmp_path)
    track = cfg2["audio"]["tracks"][0]
    assert track["label"] == "Games"
    assert track["enabled"] is True
    assert [source["kind"] for source in track["sources"]] == ["game_app", "game_app"]
    assert [source["match"]["value"] for source in track["sources"]] == [
        "cs2",
        "manual-game",
    ]
    assert track["sources"][0]["learned_from"]["steam_appid"] == "730"


def test_audio_config_preserves_application_source_match(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set(
        "audio",
        {
            "mode": "split_tracks",
            "tracks": [
                {
                    "track": 1,
                    "label": "App: Discord",
                    "enabled": True,
                    "sources": [
                        {
                            "kind": "application",
                            "display_name": "Discord",
                            "match": {
                                "type": "pipewire_app",
                                "value": "Discord",
                                "priority": "binary_first",
                            },
                        }
                    ],
                }
            ],
        },
    )

    cfg2 = make_config(tmp_path)
    track = cfg2["audio"]["tracks"][0]

    assert track["label"] == "App: Discord"
    assert track["enabled"] is True
    assert track["sources"] == [
        {
            "kind": "application",
            "display_name": "Discord",
            "match": {
                "type": "pipewire_app",
                "value": "Discord",
                "priority": "binary_first",
            },
        }
    ]


def test_audio_config_normalizes_bad_data(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "audio": {
                    "version": 1,
                    "mode": "bad-mode",
                    "microphone": {"enabled": True, "volume": 5},
                    "tracks": [
                        {"track": 0, "label": "Bad", "sources": []},
                        {"track": 7, "label": "Bad", "sources": []},
                        {
                            "track": 1,
                            "label": "Game",
                            "volume": -4,
                            "sources": [
                                {"kind": "bogus"},
                                {
                                    "kind": "output_device",
                                    "backend": "bogus",
                                    "device_id": 12,
                                    "display_name": "Speakers",
                                },
                            ],
                        },
                        {"track": 1, "label": "Duplicate", "sources": []},
                    ],
                },
                "audio_assignments": [{"legacy": True}],
            }
        ),
        encoding="utf-8",
    )

    cfg = ClipperConfig(config_path=cfg_path)
    audio = cfg["audio"]

    assert audio["mode"] == "single_mix"
    assert audio["microphone"]["enabled"] is True
    assert audio["microphone"]["volume"] == 2.0
    assert len(audio["tracks"]) == AUDIO_MAX_TRACKS
    assert audio["tracks"][0]["label"] == "Game"
    assert audio["tracks"][0]["volume"] == 0.0
    assert audio["tracks"][0]["sources"] == [
        {
            "kind": "output_device",
            "backend": "pulse",
            "device_id": "default",
            "display_name": "Speakers",
        }
    ]
    assert cfg["audio_assignments"] == [{"legacy": True}]


def test_normalize_audio_config_rejects_non_object():
    assert normalize_audio_config(None) == DEFAULTS["audio"]


def test_string_type_preserved(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("video_encoder", "x264")
    cfg2 = make_config(tmp_path)
    value = cfg2["video_encoder"]
    assert isinstance(value, str), f"Expected str, got {type(value)}"
    assert value == "x264"


# ---------------------------------------------------------------------------
# Extra: API surface
# ---------------------------------------------------------------------------


def test_get_with_default_for_unknown_key(tmp_path):
    """.get() should return the supplied default for unknown keys."""
    cfg = make_config(tmp_path)
    assert cfg.get("nonexistent_key", "fallback") == "fallback"


def test_get_without_default_returns_none(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.get("nonexistent_key") is None


def test_contains(tmp_path):
    cfg = make_config(tmp_path)
    assert "fps" in cfg
    assert "nonexistent_key" not in cfg


def test_set_unknown_key(tmp_path):
    """Custom (non-default) keys should round-trip correctly."""
    cfg = make_config(tmp_path)
    cfg.set("custom_flag", "hello")
    cfg2 = make_config(tmp_path)
    assert cfg2.get("custom_flag") == "hello"
