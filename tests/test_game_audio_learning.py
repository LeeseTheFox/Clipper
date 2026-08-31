from game_audio_learning import (
    apply_learned_game_audio_identity,
    audio_uses_whitelisted_game,
    discover_game_audio_identity,
    format_game_audio_probe,
    probe_game_audio_identity,
)


def _entry():
    return {
        "name": "THE FINALS",
        "appid": "2073850",
        "install_path": "/games/The Finals",
    }


def _source(binary: str, *, app_name: str | None = None, pid: int = 0):
    return {
        "id": "sink_input_42",
        "kind": "application",
        "binary": binary,
        "app_name": app_name or binary,
        "display_name": app_name or binary,
        "pid": pid,
        "node_id": 9,
    }


def test_discovers_audio_identity_from_verified_steam_game_process():
    processes = [
        {
            "pid": "100",
            "comm": "Discovery-d.exe",
            "cmdline": "/usr/bin/wine64 Discovery-d.exe",
            "exe": "/usr/bin/wine64-preloader",
            "rule_ids": ["steam-2073850"],
        },
        {
            "pid": "200",
            "comm": "discord",
            "cmdline": "discord",
            "exe": "/usr/bin/discord",
            "rule_ids": [],
        },
    ]

    assert (
        discover_game_audio_identity(
            _entry(),
            "steam-2073850",
            processes,
            [_source("Discovery-d.exe"), _source("Discord")],
        )
        == "Discovery-d.exe"
    )


def test_does_not_learn_matching_name_from_an_unrelated_process():
    processes = [
        {
            "pid": "200",
            "comm": "Discovery-d.exe",
            "cmdline": "Discovery-d.exe",
            "exe": "/games/Other/Discovery-d.exe",
            "rule_ids": ["steam-999"],
        }
    ]

    assert (
        discover_game_audio_identity(
            _entry(),
            "steam-2073850",
            processes,
            [_source("Discovery-d.exe")],
        )
        == ""
    )


def test_discovers_audio_identity_through_nested_steam_namespace_pid():
    processes = [
        {
            "pid": "627095",
            "namespace_pids": ["627095", "930"],
            "comm": "Discovery-d.exe",
            "cmdline": "/usr/bin/wine64 Discovery-d.exe",
            "exe": "/usr/bin/wine64-preloader",
            "rule_ids": ["steam-2073850"],
        }
    ]

    identity, diagnostics = probe_game_audio_identity(
        _entry(),
        "steam-2073850",
        processes,
        [_source("wine-game-audio", app_name="THE FINALS", pid=930)],
    )

    assert identity == "wine-game-audio"
    assert diagnostics["match_method"] == "namespace PID"
    assert diagnostics["match_pid"] == 930
    assert format_game_audio_probe(diagnostics) == (
        "game processes [Discovery-d.exe pid=627095 ns=930,627095]; "
        "playback streams [wine-game-audio pid=930]"
    )


def test_native_process_fallback_uses_existing_entry_matching():
    processes = [
        {
            "pid": "100",
            "comm": "Discovery-d.exe",
            "cmdline": "Discovery-d.exe",
            "environ": "STEAM_COMPAT_APP_ID=2073850",
            "exe": "/usr/bin/wine64-preloader",
            "cwd": "/games/The Finals",
        }
    ]

    assert (
        discover_game_audio_identity(
            _entry(),
            "steam-2073850",
            processes,
            [_source("", app_name="Discovery-d.exe")],
        )
        == "Discovery-d.exe"
    )


def test_apply_learned_identity_updates_whitelist_and_matching_track_source():
    entry = _entry()
    whitelist = [
        entry,
        {"name": "Bloons TD 6", "appid": "960090", "audio_process": "BloonsTD6.exe"},
    ]
    audio = {
        "tracks": [
            {
                "track": 1,
                "enabled": True,
                "sources": [
                    {
                        "kind": "game_app",
                        "display_name": "THE FINALS",
                        "match": {
                            "type": "process_name",
                            "value": "Discovery.exe",
                            "priority": "binary_first",
                        },
                        "learned_from": {"steam_appid": "2073850"},
                    },
                    {
                        "kind": "game_app",
                        "display_name": "Bloons TD 6",
                        "match": {
                            "type": "process_name",
                            "value": "BloonsTD6.exe",
                            "priority": "binary_first",
                        },
                        "learned_from": {"steam_appid": "960090"},
                    },
                ],
            }
        ]
    }

    updated_whitelist, updated_audio, changed = apply_learned_game_audio_identity(
        whitelist, audio, entry, "Discovery-d.exe"
    )

    assert changed is True
    assert updated_whitelist[0]["audio_process"] == "Discovery-d.exe"
    assert updated_whitelist[1]["audio_process"] == "BloonsTD6.exe"
    assert updated_audio["tracks"][0]["sources"][0]["match"]["value"] == "Discovery-d.exe"
    assert updated_audio["tracks"][0]["sources"][1]["match"]["value"] == "BloonsTD6.exe"
    assert "audio_process" not in whitelist[0]
    assert audio["tracks"][0]["sources"][0]["match"]["value"] == "Discovery.exe"


def test_audio_usage_requires_enabled_matching_game_source():
    audio = {
        "tracks": [
            {
                "enabled": False,
                "sources": [
                    {
                        "kind": "game_app",
                        "learned_from": {"steam_appid": "2073850"},
                    }
                ],
            }
        ]
    }

    assert audio_uses_whitelisted_game(audio, _entry()) is False
    audio["tracks"][0]["enabled"] = True
    assert audio_uses_whitelisted_game(audio, _entry()) is True
