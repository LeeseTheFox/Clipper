import importlib.util
import sys
import types
from pathlib import Path


def _load_audio_tracks_module():
    module_name = "test_audio_tracks_view_isolated"
    module_path = Path(__file__).resolve().parents[1] / "ui" / "audio_tracks_view.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "gi",
            "gi.repository",
            "audio_source_discovery",
            "audio_source_watcher",
            "engine_client",
        )
    }

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")

    timeouts = []

    def timeout_add(_delay, callback):
        timeouts.append(callback)
        return len(timeouts)

    repository.GLib = types.SimpleNamespace(
        timeout_add=timeout_add,
        source_remove=lambda _source_id: None,
    )

    class StringList:
        @staticmethod
        def new(labels):
            return list(labels)

    repository.Gtk = types.SimpleNamespace(
        Box=type("Box", (), {}),
        StringList=StringList,
    )
    repository.Adw = types.SimpleNamespace()
    repository.Pango = types.SimpleNamespace(
        EllipsizeMode=types.SimpleNamespace(MIDDLE="middle")
    )
    gi.repository = repository

    watcher = types.ModuleType("audio_source_watcher")
    watcher.AudioSourceWatcher = type(
        "AudioSourceWatcher",
        (),
        {
            "__init__": lambda self, refresh_callback: setattr(
                self, "refresh_callback", refresh_callback
            ),
            "start": lambda self: None,
            "stop": lambda self: None,
        },
    )

    discovery = types.ModuleType("audio_source_discovery")
    discovery.sources = []
    discovery.list_runtime_audio_sources = lambda: list(discovery.sources)

    engine_client = types.ModuleType("engine_client")
    engine_client.STATE_CONNECTED = 2

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "audio_source_discovery": discovery,
            "audio_source_watcher": watcher,
            "engine_client": engine_client,
        }
    )

    try:
        module = importlib.util.module_from_spec(spec)
        module._test_timeouts = timeouts
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


audio_tracks_module = _load_audio_tracks_module()
AudioTracksView = audio_tracks_module.AudioTracksView
STATE_CONNECTED = audio_tracks_module.STATE_CONNECTED


class DropdownStub:
    def __init__(self):
        self.model = []
        self.selected = None

    def set_model(self, model):
        self.model = list(model)

    def set_selected(self, selected):
        self.selected = selected


class ComboRowStub:
    def __init__(self):
        self.model = []
        self.selected = 0
        self.model_updates = 0

    def get_selected(self):
        return self.selected

    def set_model(self, model):
        self.model = list(model)
        self.model_updates += 1

    def set_selected(self, selected):
        self.selected = selected


class BannerStub:
    def __init__(self):
        self.revealed = False

    def set_revealed(self, revealed):
        self.revealed = bool(revealed)


class SwitchStub:
    def __init__(self):
        self.active = False

    def set_active(self, active):
        self.active = bool(active)


class LabelStub:
    def __init__(self):
        self.text = ""
        self.tooltip_text = ""
        self.xalign = None
        self.hexpand = False
        self.width_chars = None
        self.ellipsize = None
        self.single_line_mode = False

    def set_text(self, text):
        self.text = text

    def set_tooltip_text(self, text):
        self.tooltip_text = text

    def set_xalign(self, xalign):
        self.xalign = xalign

    def set_hexpand(self, hexpand):
        self.hexpand = bool(hexpand)

    def set_width_chars(self, width_chars):
        self.width_chars = width_chars

    def set_ellipsize(self, ellipsize):
        self.ellipsize = ellipsize

    def set_single_line_mode(self, single_line_mode):
        self.single_line_mode = bool(single_line_mode)


class ListItemStub:
    def __init__(self, text=""):
        self.child = None
        self.item = types.SimpleNamespace(get_string=lambda: text)

    def set_child(self, child):
        self.child = child

    def get_child(self):
        return self.child

    def get_item(self):
        return self.item


class EngineClientStub:
    def __init__(
        self,
        response,
        *,
        connected=True,
        volume_update_response=None,
        volume_update_succeeds=True,
    ):
        self.response = response
        self.connected = connected
        self.volume_update_response = volume_update_response or {"ok": True}
        self.volume_update_succeeds = volume_update_succeeds
        self.sent = 0
        self.volume_updates = []

    def is_connected(self):
        return self.connected

    def list_audio_sources(self, callback):
        self.sent += 1
        callback(self.response)
        return True

    def update_audio_volumes(self, audio, callback):
        if not self.volume_update_succeeds:
            return False
        self.volume_updates.append(audio)
        callback(self.volume_update_response)
        return True


class ConfigStub:
    def __init__(self, data):
        self.data = data
        self.saved = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.saved.append((key, value))


def _make_view(
    engine_response=None,
    *,
    engine_connected=True,
    capabilities_requested=None,
    config=None,
    restart_required=None,
    restart_requested=None,
    volume_update_response=None,
    volume_update_succeeds=True,
):
    dropdown = DropdownStub()
    view = object.__new__(AudioTracksView)
    view._config = config
    view._engine_client = EngineClientStub(
        engine_response or {"ok": True, "sources": []},
        connected=engine_connected,
        volume_update_response=volume_update_response,
        volume_update_succeeds=volume_update_succeeds,
    )
    view._capabilities_requested_callback = capabilities_requested
    view._capabilities_finished_callback = None
    view._engine_restart_required_callback = restart_required
    view._engine_restart_requested_callback = restart_requested
    view._restart_banner = BannerStub()
    view._mic_row = ComboRowStub()
    view._suppress_signals = False
    view._source_probe_requested = False
    view._source_retry_id = None
    view._source_retry_count = 0
    view._source_refresh_timeout_id = None
    view._source_request_in_flight = False
    view._source_refresh_pending = False
    view._audio = {
        "mode": "split_tracks",
        "microphone": {
            "enabled": False,
            "backend": "pulse",
            "device_id": "default",
            "display_name": "Default",
            "volume": 1.0,
        },
        "tracks": [
            {
                "track": idx,
                "label": f"Track {idx}",
                "enabled": False,
                "volume": 1.0,
                "sources": [],
            }
            for idx in range(1, audio_tracks_module.AUDIO_MAX_TRACKS + 1)
        ],
    }
    view._track_widgets = [{"source": dropdown, "enabled": SwitchStub()}]
    view._source_options = []
    view._build_source_options()
    view._rebuild_source_models()
    return view, dropdown


def test_audio_source_response_adds_application_to_dropdown_model():
    view, dropdown = _make_view()

    view._source_request_in_flight = True
    view._on_audio_sources(
        {
            "ok": True,
            "sources": [
                {
                    "kind": "application",
                    "display_name": "Zen",
                    "app_name": "Zen",
                    "binary": "zen",
                    "media_name": "Video",
                }
            ],
        }
    )

    assert "App: Zen - Video" in dropdown.model


def test_dropdown_labels_shrink_and_ellipsize_long_window_titles(monkeypatch):
    view, _dropdown = _make_view()
    monkeypatch.setattr(audio_tracks_module.Gtk, "Label", LabelStub, raising=False)
    long_title = (
        "App: Browser - A very long window title that should not force "
        "Clipper wider than its normal window"
    )
    list_item = ListItemStub(long_title)

    view._setup_dropdown_label(None, list_item)
    view._bind_dropdown_label(None, list_item)

    label = list_item.child
    assert label is not None
    assert label.text == long_title
    assert label.tooltip_text is None
    assert label.width_chars == 1
    assert label.ellipsize == audio_tracks_module.Pango.EllipsizeMode.MIDDLE
    assert label.single_line_mode is True


def test_discovered_input_devices_are_microphone_device_options():
    view, _dropdown = _make_view()

    view._build_source_options(
        discovered_sources=[
            {
                "kind": "input_device",
                "backend": "pulse",
                "device_id": "alsa_input.usb-test",
                "display_name": "USB Mic",
            }
        ]
    )

    assert [option["label"] for option in view._microphone_options()] == [
        "Off",
        "System default",
        "USB Mic",
    ]


def test_split_track_microphone_source_uses_selected_microphone_device():
    view, dropdown = _make_view()

    mic_index = dropdown.model.index("Microphone")
    view._on_track_source(0, mic_index)

    assert view._audio["tracks"][0]["label"] == "Microphone"
    assert view._audio["tracks"][0]["enabled"] is True
    assert view._audio["tracks"][0]["sources"] == [
        {
            "kind": "selected_input_device",
            "display_name": "Microphone",
        }
    ]


def test_whitelisted_games_option_is_available_with_empty_whitelist():
    config = ConfigStub({"whitelist": []})
    _view, dropdown = _make_view(config=config)

    assert "Whitelisted games" in dropdown.model


def test_empty_whitelisted_games_selection_survives_track_model_rebuild():
    config = ConfigStub({"whitelist": []})
    view, dropdown = _make_view(config=config)

    game_index = dropdown.model.index("Whitelisted games")
    view._on_track_source(0, game_index)
    view._rebuild_source_models()

    assert view._audio["tracks"][0]["label"] == "Whitelisted games"
    assert view._audio["tracks"][0]["sources"] == []
    assert dropdown.selected == game_index


def test_empty_whitelisted_games_track_populates_when_first_game_is_added():
    config = ConfigStub({"whitelist": []})
    view, dropdown = _make_view(config=config)

    game_index = dropdown.model.index("Whitelisted games")
    view._on_track_source(0, game_index)
    config.saved.clear()

    config.data["whitelist"] = [
        {
            "name": "Bloons TD 6",
            "appid": "960090",
            "install_path": "/games/SteamLibrary/steamapps/common/BloonsTD6",
        }
    ]
    view.on_whitelist_changed()

    sources = view._audio["tracks"][0]["sources"]
    assert sources == [
        {
            "kind": "game_app",
            "display_name": "Bloons TD 6",
            "match": {
                "type": "process_name",
                "value": "BloonsTD6",
                "priority": "binary_first",
            },
            "learned_from": {
                "steam_appid": "960090",
                "install_path": "/games/SteamLibrary/steamapps/common/BloonsTD6",
            },
        }
    ]
    assert config.saved[-1][0] == "audio"


def test_whitelisted_games_selection_survives_removing_all_games():
    config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Bloons TD 6",
                    "appid": "960090",
                    "install_path": "/games/SteamLibrary/steamapps/common/BloonsTD6",
                }
            ]
        }
    )
    view, dropdown = _make_view(config=config)
    game_index = dropdown.model.index("Whitelisted games")
    view._on_track_source(0, game_index)

    config.data["whitelist"] = []
    view.on_whitelist_changed()

    assert view._audio["tracks"][0]["label"] == "Whitelisted games"
    assert view._audio["tracks"][0]["sources"] == []
    assert dropdown.selected == dropdown.model.index("Whitelisted games")


def test_split_tracks_show_default_row_when_no_tracks_added():
    view, _dropdown = _make_view()

    assert view._visible_track_indexes() == [0]


def test_adding_track_keeps_default_track_and_enables_next_track():
    view, _dropdown = _make_view()
    rebuilds = []
    loads = []
    view._rebuild_track_groups = lambda: rebuilds.append(True)
    view._load_from_config = lambda: loads.append(True)

    view._on_add_track(None)

    assert view._visible_track_indexes() == [0, 1]
    assert view._audio["tracks"][0]["enabled"] is True
    assert view._audio["tracks"][1]["enabled"] is True
    assert rebuilds == [True]
    assert loads == [True]


def test_removing_first_track_moves_existing_tracks_up():
    view, _dropdown = _make_view()
    view._audio["tracks"][0] = {
        "track": 1,
        "label": "System Audio",
        "enabled": True,
        "volume": 0.8,
        "sources": [{"kind": "output_device", "backend": "pulse", "device_id": "default"}],
    }
    view._audio["tracks"][1] = {
        "track": 2,
        "label": "Microphone",
        "enabled": True,
        "volume": 0.7,
        "sources": [{"kind": "selected_input_device", "display_name": "Microphone"}],
    }
    view._audio["tracks"][2] = {
        "track": 3,
        "label": "App: Discord",
        "enabled": True,
        "volume": 1.0,
        "sources": [
            {
                "kind": "application",
                "display_name": "Discord",
                "match": {"type": "pipewire_app", "value": "Discord"},
            }
        ],
    }
    view._rebuild_track_groups = lambda: None
    view._load_from_config = lambda: None

    view._on_remove_track(0)

    assert view._visible_track_indexes() == [0, 1]
    assert view._audio["tracks"][0]["track"] == 1
    assert view._audio["tracks"][0]["label"] == "Microphone"
    assert view._audio["tracks"][0]["volume"] == 0.7
    assert view._audio["tracks"][1]["track"] == 2
    assert view._audio["tracks"][1]["label"] == "App: Discord"
    assert view._audio["tracks"][2] == {
        "track": 3,
        "label": "Track 3",
        "enabled": False,
        "volume": 1.0,
        "sources": [],
    }


def test_microphone_selection_does_not_rebuild_open_combo_model():
    view, _dropdown = _make_view()
    view._microphone_device_options = [
        {
            "label": "USB Mic",
            "microphone": {
                "enabled": True,
                "backend": "pulse",
                "device_id": "alsa_input.usb-test",
                "display_name": "USB Mic",
            },
        }
    ]
    view._mic_row.selected = 2

    view._on_microphone_changed(view._mic_row, None)

    assert view._audio["microphone"]["device_id"] == "alsa_input.usb-test"
    assert view._mic_row.model_updates == 0


def test_audio_source_refresh_rebuilds_microphone_combo_only_when_options_change():
    view, _dropdown = _make_view()

    view._on_audio_sources({"ok": True, "sources": []})

    assert view._mic_row.model_updates == 0

    view._on_audio_sources(
        {
            "ok": True,
            "sources": [
                {
                    "kind": "input_device",
                    "backend": "pulse",
                    "device_id": "alsa_input.usb-test",
                    "display_name": "USB Mic",
                }
            ],
        }
    )

    assert view._mic_row.model_updates == 1
    assert view._mic_row.model == ["Off", "System default", "USB Mic"]


def test_suppressed_source_change_does_not_clear_persisted_selection():
    view, _dropdown = _make_view()
    view._audio["tracks"][0]["label"] = "App: Discord"
    view._audio["tracks"][0]["sources"] = [
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

    view._suppress_signals = True
    view._on_track_source(0, 0)

    assert view._audio["tracks"][0]["label"] == "App: Discord"
    assert view._audio["tracks"][0]["sources"] == [
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


def test_configured_application_source_remains_selected_when_offline():
    view, dropdown = _make_view()
    view._audio["tracks"][0]["label"] = "App: Discord"
    view._audio["tracks"][0]["sources"] = [
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

    view._build_source_options(discovered_sources=[])
    view._rebuild_source_models()

    assert "App: Discord" in dropdown.model
    assert dropdown.selected == dropdown.model.index("App: Discord")


def test_track_volume_change_updates_live_without_restart_banner():
    view, _dropdown = _make_view(restart_required=lambda: True)

    view._on_track_volume(0, 0.35)

    assert view._engine_client.volume_updates[-1]["tracks"][0]["volume"] == 0.35
    assert view._restart_banner.revealed is False


def test_mic_volume_change_updates_live_without_restart_banner():
    scale = type("Scale", (), {"get_value": lambda self: 55.0})()
    view, _dropdown = _make_view(restart_required=lambda: True)

    view._on_mic_volume_changed(scale)

    assert view._engine_client.volume_updates[-1]["microphone"]["volume"] == 0.55
    assert view._restart_banner.revealed is False


def test_volume_change_falls_back_to_restart_banner_when_live_update_fails():
    view, _dropdown = _make_view(
        restart_required=lambda: True,
        volume_update_succeeds=False,
    )

    view._on_track_volume(0, 0.35)

    assert view._engine_client.volume_updates == []
    assert view._restart_banner.revealed is True


def test_whitelisted_games_prefer_steam_install_dir_over_title():
    config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Bloons TD 6",
                    "appid": "960090",
                    "install_path": "/games/SteamLibrary/steamapps/common/BloonsTD6",
                }
            ]
        }
    )
    view, dropdown = _make_view(config=config)

    assert "Whitelisted games" in dropdown.model
    option = next(
        option
        for option in view._source_options
        if option["label"] == "Whitelisted games"
    )
    assert option["sources"][0]["match"]["value"] == "BloonsTD6"


def test_whitelisted_games_prefer_install_executable_when_present(tmp_path):
    install_path = tmp_path / "BloonsTD6"
    install_path.mkdir()
    (install_path / "BloonsTD6.exe").write_text("", encoding="utf-8")
    (install_path / "UnityCrashHandler64.exe").write_text("", encoding="utf-8")
    config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Bloons TD 6",
                    "appid": "960090",
                    "install_path": str(install_path),
                }
            ]
        }
    )
    view, _dropdown = _make_view(config=config)

    option = next(
        option
        for option in view._source_options
        if option["label"] == "Whitelisted games"
    )
    assert option["sources"][0]["match"]["value"] == "BloonsTD6.exe"


def test_stale_install_directory_match_updates_to_install_executable(tmp_path):
    restart_requests = []
    install_path = tmp_path / "BloonsTD6"
    install_path.mkdir()
    (install_path / "BloonsTD6.exe").write_text("", encoding="utf-8")
    config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Bloons TD 6",
                    "appid": "960090",
                    "install_path": str(install_path),
                }
            ]
        }
    )
    view, _dropdown = _make_view(
        config=config,
        restart_required=lambda: True,
        restart_requested=lambda: restart_requests.append(True),
    )
    view._audio["tracks"][0] = {
        "track": 1,
        "label": "Whitelisted games",
        "enabled": True,
        "volume": 1.0,
        "sources": [
            {
                "kind": "game_app",
                "display_name": "Bloons TD 6",
                "match": {
                    "type": "process_name",
                    "value": "BloonsTD6",
                    "priority": "binary_first",
                },
                "learned_from": {
                    "steam_appid": "960090",
                    "install_path": str(install_path),
                },
            }
        ],
    }

    view._on_audio_sources({"ok": True, "sources": []})

    sources = view._audio["tracks"][0]["sources"]
    assert sources[0]["match"]["value"] == "BloonsTD6.exe"
    assert config.saved[-1][0] == "audio"
    assert restart_requests == []
    assert view._restart_banner.revealed is False


def test_live_audio_source_refresh_does_not_rewrite_whitelisted_game_track():
    restart_requests = []
    config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Crab Game",
                    "appid": "1782210",
                    "install_path": "/games/SteamLibrary/steamapps/common/Crab Game",
                }
            ]
        }
    )
    view, dropdown = _make_view(
        config=config,
        restart_required=lambda: True,
        restart_requested=lambda: restart_requests.append(True),
    )
    view._audio["tracks"][0] = {
        "track": 1,
        "label": "Whitelisted games",
        "enabled": True,
        "volume": 1.0,
        "sources": [
            {
                "kind": "game_app",
                "display_name": "Crab Game",
                "match": {
                    "type": "process_name",
                    "value": "Crab Game.x86_64",
                    "priority": "binary_first",
                },
                "learned_from": {
                    "steam_appid": "1782210",
                    "install_path": "/games/SteamLibrary/steamapps/common/Crab Game",
                },
            }
        ],
    }

    view._on_audio_sources(
        {
            "ok": True,
            "sources": [
                {
                    "kind": "application",
                    "display_name": "Crab Game",
                    "app_name": "Crab Game",
                    "binary": "Crab Game.x86_64",
                    "media_name": "",
                }
            ],
        }
    )

    sources = view._audio["tracks"][0]["sources"]
    assert sources[0]["match"]["value"] == "Crab Game.x86_64"
    assert dropdown.selected == dropdown.model.index("Whitelisted games")
    assert config.saved == []
    assert restart_requests == []
    assert view._restart_banner.revealed is False


def test_missing_live_audio_source_does_not_revert_learned_whitelisted_match():
    restart_requests = []
    config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Crab Game",
                    "appid": "1782210",
                    "install_path": "/games/SteamLibrary/steamapps/common/Crab Game",
                }
            ]
        }
    )
    view, _dropdown = _make_view(
        config=config,
        restart_required=lambda: True,
        restart_requested=lambda: restart_requests.append(True),
    )
    view._audio["tracks"][0] = {
        "track": 1,
        "label": "Whitelisted games",
        "enabled": True,
        "volume": 1.0,
        "sources": [
            {
                "kind": "game_app",
                "display_name": "Crab Game",
                "match": {
                    "type": "process_name",
                    "value": "Crab Game.x86_64",
                    "priority": "binary_first",
                },
                "learned_from": {
                    "steam_appid": "1782210",
                    "install_path": "/games/SteamLibrary/steamapps/common/Crab Game",
                },
            }
        ],
    }

    view._on_audio_sources({"ok": True, "sources": []})

    assert view._audio["tracks"][0]["sources"][0]["match"]["value"] == "Crab Game.x86_64"
    assert config.saved == []
    assert restart_requests == []
    assert view._restart_banner.revealed is False


def test_engine_connect_during_probe_fetches_applications_immediately():
    view, dropdown = _make_view(
        {
            "ok": True,
            "sources": [
                {
                    "kind": "application",
                    "display_name": "Discord",
                    "app_name": "Discord",
                    "binary": "Discord",
                    "media_name": "",
                }
            ],
        }
    )

    view._source_probe_requested = True
    view._on_engine_state_change(STATE_CONNECTED)

    assert view._engine_client.sent == 1
    assert "App: Discord" in dropdown.model


def test_reconnect_after_dropped_audio_source_request_fetches_applications():
    view, dropdown = _make_view(
        {
            "ok": True,
            "sources": [
                {
                    "kind": "application",
                    "display_name": "Discord",
                    "app_name": "Discord",
                    "binary": "Discord",
                    "media_name": "",
                }
            ],
        }
    )

    view._source_request_in_flight = True
    view._on_engine_state_change(0)
    view._on_engine_state_change(STATE_CONNECTED)

    assert view._engine_client.sent == 1
    assert "App: Discord" in dropdown.model


def test_passive_audio_refresh_updates_locally_without_starting_temporary_engine():
    requested = []
    audio_tracks_module.list_runtime_audio_sources = lambda: [
        {
            "kind": "application",
            "display_name": "Zen",
            "app_name": "Zen",
            "binary": "zen",
            "media_name": "Video",
        }
    ]
    view, _dropdown = _make_view(
        engine_connected=False,
        capabilities_requested=lambda: requested.append(True) or True,
    )

    view._run_scheduled_audio_source_refresh()

    assert requested == []
    assert view._engine_client.sent == 0
    assert "App: Zen - Video" in _dropdown.model


def test_cleanup_stops_pactl_watcher_and_removes_refresh_sources(monkeypatch):
    removed = []
    watcher = types.SimpleNamespace(stopped=False)
    watcher.stop = lambda: setattr(watcher, "stopped", True)
    view = object.__new__(AudioTracksView)
    view._source_watcher = watcher
    view._source_retry_id = 11
    view._source_refresh_timeout_id = 12
    monkeypatch.setattr(audio_tracks_module.GLib, "source_remove", removed.append)

    view.cleanup()

    assert watcher.stopped is True
    assert removed == [11, 12]
    assert view._source_retry_id is None
    assert view._source_refresh_timeout_id is None


def test_audio_source_work_runs_only_while_page_is_active(monkeypatch):
    events = []
    watcher = types.SimpleNamespace(
        start=lambda: events.append("start"),
        stop=lambda: events.append("stop"),
    )
    view = object.__new__(AudioTracksView)
    view._active = False
    view._source_watcher = watcher
    view._source_retry_id = None
    view._source_refresh_timeout_id = None
    view._source_refresh_pending = False
    view._source_probe_requested = False
    view._request_audio_capabilities = lambda: events.append("probe")

    view.set_active(True)
    view.set_active(True)
    view.set_active(False)

    assert events == ["start", "probe", "stop"]


def test_hidden_audio_page_ignores_source_events(monkeypatch):
    timeouts = []
    monkeypatch.setattr(
        audio_tracks_module.GLib,
        "timeout_add",
        lambda delay, callback: timeouts.append((delay, callback)) or 1,
    )
    view = object.__new__(AudioTracksView)
    view._active = False
    view._source_refresh_timeout_id = None

    view._schedule_audio_source_refresh()

    assert timeouts == []
