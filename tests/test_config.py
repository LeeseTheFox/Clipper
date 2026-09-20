"""Configuration persistence, recovery, validation and schema migration contracts."""

import json
from pathlib import Path

import pytest
from config import (
    AUDIO_MAX_TRACKS,
    DEFAULTS,
    VIDEO_RATE_CONTROLS,
    ClipperConfig,
    normalize_audio_config,
    normalize_game_capture_integration,
    normalize_pending_game_capture_change,
    normalize_whitelist,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, filename: str = "config.json") -> ClipperConfig:
    """Return a ClipperConfig pointed at a temp directory."""
    return ClipperConfig(config_path=tmp_path / filename)


def test_failed_save_does_not_leave_a_phantom_whitelist_entry(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.set("whitelist", [])

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(config, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        config.set("whitelist", [{"appid": "730", "capture_mode": "game_capture"}])
    assert config.get("whitelist") == []
    assert make_config(tmp_path).get("whitelist") == []


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
    assert not (tmp_path / "config.json").exists()


def test_fresh_setup_has_no_default_hotkey(tmp_path):
    cfg = make_config(tmp_path)

    assert cfg["save_hotkey"] == ""


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
    """Persist mixed settings without changing values or JSON types."""
    cfg = make_config(tmp_path)
    settings = {
        "fps": 30,
        "resolution": "2560x1440",
        "capture_mode": "game_capture",
        "pipewire_restore_token": "token-123",
        "minimize_to_tray_on_close": True,
        "remember_window_sizes": False,
        "notify_on_clip_saved": False,
        "play_sound_on_clip_saved": True,
        "clip_sound_volume": 1.75,
        "rate_control": "vbr",
        "video_bitrate": 16000,
        "video_max_bitrate": 24000,
        "replay_buffer_size_mb": 2048,
        "replay_buffer_length": 120,
        "start_on_boot": True,
        "fruit_drop_high_score": 42,
        "whitelist": [{"name": "Game", "path": "/usr/bin/game", "method": "executable"}],
        "custom_flag": "future setting",
    }
    for key, value in settings.items():
        cfg.set(key, value)
    cfg["save_hotkey"] = "ctrl+shift+f12"

    cfg2 = make_config(tmp_path)
    for key, expected in settings.items():
        assert cfg2[key] == expected, key
        assert type(cfg2[key]) is type(expected), key
    assert cfg2["save_hotkey"] == "ctrl+shift+f12"
    assert cfg2.load_status == "loaded"
    assert cfg2.saved_key_count == len(json.loads((tmp_path / "config.json").read_text()))
    assert not list(tmp_path.glob("*.tmp"))


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


def test_portal_hotkey_display_state_round_trips_verbatim(tmp_path):
    cfg = make_config(tmp_path)
    cfg.set("save_hotkey_portal_label", "Strg+Ö")
    cfg.set("save_hotkey_portal_managed", True)

    cfg2 = make_config(tmp_path)
    assert cfg2["save_hotkey"] == DEFAULTS["save_hotkey"]
    assert cfg2["save_hotkey_portal_label"] == "Strg+Ö"
    assert cfg2["save_hotkey_portal_managed"] is True


def test_clip_capture_sound_volume_is_clamped(tmp_path):
    cfg = make_config(tmp_path)

    cfg.set("clip_sound_volume", 3)

    assert cfg["clip_sound_volume"] == 2.0


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
# 5. Reset settings
# ---------------------------------------------------------------------------


def test_reset_to_defaults(tmp_path):
    """After reset_to_defaults(), every value should equal the built-in default."""
    cfg = make_config(tmp_path)
    cfg.set("fps", 30)
    cfg.set("resolution", "3840x2160")
    cfg.set("start_on_boot", True)

    cfg.reset_to_defaults()

    reloaded = make_config(tmp_path)
    for key, expected in DEFAULTS.items():
        assert cfg[key] == expected, f"'{key}' not reset to default"
        assert reloaded[key] == expected, f"'{key}' reset was not persisted"


# ---------------------------------------------------------------------------
# 6. Audio schema and game capture ownership
# ---------------------------------------------------------------------------


def test_audio_default_schema(tmp_path):
    cfg = make_config(tmp_path)
    audio = cfg["audio"]

    assert audio["version"] == 1
    assert audio["mode"] == "single_mix"
    assert audio["microphone"]["enabled"] is False
    assert audio["microphone"]["device_id"] == "default"
    assert audio["microphone"]["volume"] == 1.0
    assert len(audio["tracks"]) == AUDIO_MAX_TRACKS
    assert [track["track"] for track in audio["tracks"]] == list(range(1, AUDIO_MAX_TRACKS + 1))


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


def test_legacy_game_capture_migration_never_persists_unknown_environment():
    entries = normalize_whitelist(
        [
            {
                "name": "Legacy game",
                "appid": "730",
                "capture_mode": "game_capture",
                "steam_environment": "attacker_controlled",
            }
        ]
    )

    integration = entries[0]["game_capture_integration"]
    assert integration["environment"] == "unsupported_sandbox"
    assert integration["provider"] == "external_obs_gamecapture"
    assert integration["managed_launch_options"] is False


def test_game_capture_integration_rejects_unknown_schema_values():
    base = {
        "schema": 1,
        "environment": "native_steam",
        "provider": "external_obs_gamecapture",
    }

    assert normalize_game_capture_integration({**base, "schema": 2}) is None
    assert normalize_game_capture_integration({**base, "environment": "unknown"}) is None
    assert normalize_game_capture_integration({**base, "provider": "unknown"}) is None


def test_game_capture_integration_rejects_incomplete_managed_ownership():
    assert (
        normalize_game_capture_integration(
            {
                "schema": 1,
                "environment": "native_steam",
                "provider": "external_obs_gamecapture",
                "managed_launch_options": True,
                "original_launch_options": "",
                "applied_launch_options": '"/wrapper" %command%',
                "wrapper_id": "obs-gamecapture-external-v1",
                "wrapper_prefix": "",
            }
        )
        is None
    )


def test_pending_game_capture_journal_rejects_incomplete_identity():
    journal = {
        "schema": 1,
        "operation": "add",
        "steam_installation": "",
        "steam_appid": "730",
        "original_launch_options": "",
        "intended_launch_options": '"/wrapper" %command%',
        "entry_snapshot": {},
        "phase": "prepared",
    }

    assert normalize_pending_game_capture_change(journal) is None
