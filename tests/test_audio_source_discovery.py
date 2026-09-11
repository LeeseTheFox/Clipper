import json
import subprocess
from types import SimpleNamespace

import audio_source_discovery


def test_framework_name_yields_to_corroborated_application_identity():
    assert (
        audio_source_discovery._friendly_audio_display_name(
            "Framework Audio", "Acme Player", "framework-output", "org.acme.Acme Player"
        )
        == "Acme Player"
    )


def test_generic_chromium_name_uses_application_binary():
    assert (
        audio_source_discovery._friendly_audio_display_name(
            "Chromium", "ExampleApp", "Chromium", ""
        )
        == "ExampleApp"
    )


def test_list_runtime_audio_sources_maps_sink_inputs(monkeypatch):
    def fake_run(cmd, **_kwargs):
        if cmd[-1] == "sink-inputs":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "index": 42,
                            "driver": "PipeWire",
                            "properties": {
                                "application.name": "Zen",
                                "application.process.binary": "zen",
                                "application.process.id": "123",
                                "client.id": "7",
                                "object.id": "9",
                                "media.name": "Video",
                            },
                        }
                    ]
                ),
            )
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert audio_source_discovery.list_runtime_audio_sources() == [
        {
            "id": "sink_input_42",
            "kind": "application",
            "display_name": "Zen",
            "app_name": "Zen",
            "binary": "zen",
            "pid": 123,
            "client_id": 7,
            "node_id": 9,
            "media_name": "Video",
            "backend": "pipewire",
        }
    ]


def test_list_playback_audio_sources_excludes_idle_clients_and_devices(monkeypatch):
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd[-1])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "index": 42,
                        "driver": "PipeWire",
                        "properties": {
                            "application.name": "Discovery-d.exe",
                            "application.process.binary": "Discovery-d.exe",
                            "application.process.id": "1848",
                            "client.id": "7",
                            "object.id": "9",
                        },
                    }
                ]
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert audio_source_discovery.list_playback_audio_sources()[0]["binary"] == ("Discovery-d.exe")
    assert calls == ["sink-inputs"]


def test_list_runtime_audio_sources_hides_system_clients(monkeypatch):
    def fake_run(cmd, **_kwargs):
        if cmd[-1] == "clients":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "index": 1,
                            "driver": "PipeWire",
                            "properties": {
                                "application.name": "pipewire",
                                "application.process.binary": "pipewire",
                            },
                        }
                    ]
                ),
            )
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert audio_source_discovery.list_runtime_audio_sources() == []


def test_list_runtime_audio_sources_maps_recording_devices_and_skips_monitors(
    monkeypatch,
):
    def fake_run(cmd, **_kwargs):
        if cmd[-1] == "sources":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "index": 11,
                            "name": "alsa_input.usb-test",
                            "description": "USB Mic",
                            "driver": "PipeWire",
                            "properties": {"device.class": "sound"},
                        },
                        {
                            "index": 12,
                            "name": "alsa_output.test.monitor",
                            "description": "Monitor",
                            "driver": "PipeWire",
                            "properties": {"device.class": "monitor"},
                        },
                    ]
                ),
            )
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert audio_source_discovery.list_runtime_audio_sources() == [
        {
            "id": "source_11",
            "kind": "input_device",
            "backend": "pipewire",
            "device_id": "alsa_input.usb-test",
            "display_name": "USB Mic",
        }
    ]
