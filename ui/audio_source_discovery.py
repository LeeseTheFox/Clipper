"""
Lightweight runtime audio source discovery for the GTK picker.
"""

import json
import subprocess
from typing import Any

_GENERIC_AUDIO_CLIENT_NAMES = {
    "",
    "WEBRTC VoiceEngine",
    "Chromium",
    "Chromium input",
    "Chromium output",
    "AudioIPC Server",
    "ALSA Playback",
}

_HIDDEN_AUDIO_CLIENT_NAMES = {
    "clipper-engine",
    "kded6",
    "kwin_wayland",
    "libcanberra",
    "pactl",
    "pipewire",
    "pipewire-pulse",
    "plasmashell",
    "uresourced",
    "wireplumber",
    "WirePlumber",
    "WirePlumber [export]",
    "xdg-desktop-portal",
}


def _run_pactl_json(*args: str) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _flatpak_app_name(app_id: str) -> str:
    if not app_id:
        return ""
    return app_id.rsplit(".", 1)[-1]


def _identity_key(value: str) -> str:
    return value.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _friendly_audio_display_name(app_name: str, binary: str, node_name: str, app_id: str) -> str:
    portal_name = _flatpak_app_name(app_id)
    if (
        binary
        and portal_name
        and _identity_key(binary) == _identity_key(portal_name)
        and _identity_key(app_name) != _identity_key(binary)
    ):
        return binary
    if app_name not in _GENERIC_AUDIO_CLIENT_NAMES:
        return app_name
    if binary:
        return binary
    return portal_name or app_name or node_name


def _is_hidden_audio_client(name: str) -> bool:
    return bool(name) and name in _HIDDEN_AUDIO_CLIENT_NAMES


def _has_application_identity(
    sources: list[dict[str, Any]], binary: str, app_name: str, display_name: str
) -> bool:
    for source in sources:
        if source.get("kind") != "application":
            continue
        if binary and source.get("binary") == binary:
            return True
        if app_name and source.get("app_name") == app_name:
            return True
        if display_name and source.get("display_name") == display_name:
            return True
    return False


def _add_application_source(
    sources: list[dict[str, Any]],
    *,
    source_id: str,
    display_name: str,
    app_name: str,
    binary: str,
    pid: int,
    client_id: int,
    node_id: int,
    media_name: str = "",
    backend: str = "pulse",
) -> None:
    if not source_id or not display_name:
        return
    if _has_application_identity(sources, binary, app_name, display_name):
        return

    sources.append(
        {
            "id": source_id,
            "kind": "application",
            "display_name": display_name,
            "app_name": app_name,
            "binary": binary,
            "pid": pid,
            "client_id": client_id,
            "node_id": node_id,
            "media_name": media_name,
            "backend": backend,
        }
    )


def _backend_for_item(item: dict[str, Any]) -> str:
    driver = _string(item.get("driver")).lower()
    return "pipewire" if "pipewire" in driver else "pulse"


def _add_sink_input_source(sources: list[dict[str, Any]], sink_input: dict[str, Any]) -> None:
    props = sink_input.get("properties")
    if not isinstance(props, dict):
        return

    index = _int(sink_input.get("index"))
    app_name = _string(props.get("application.name"))
    binary = _string(props.get("application.process.binary"))
    media_name = _string(props.get("media.name"))
    node_name = _string(props.get("node.name"))
    app_id = _string(props.get("pipewire.access.portal.app_id"))
    display_name = _friendly_audio_display_name(app_name, binary, node_name, app_id)

    if index <= 0:
        return

    _add_application_source(
        sources,
        source_id=f"sink_input_{index}",
        display_name=display_name,
        app_name=app_name,
        binary=binary,
        pid=_int(props.get("application.process.id")),
        client_id=_int(props.get("client.id")),
        node_id=_int(props.get("object.id")),
        media_name=media_name,
        backend=_backend_for_item(sink_input),
    )


def _add_client_source(sources: list[dict[str, Any]], client: dict[str, Any]) -> None:
    props = client.get("properties")
    if not isinstance(props, dict):
        return

    index = _int(client.get("index"))
    app_name = _string(props.get("application.name"))
    binary = _string(props.get("application.process.binary"))
    node_name = _string(props.get("node.name"))
    app_id = _string(props.get("pipewire.access.portal.app_id"))
    display_name = _friendly_audio_display_name(app_name, binary, node_name, app_id)

    if index <= 0:
        return
    if any(_is_hidden_audio_client(name) for name in (display_name, app_name, binary)):
        return

    _add_application_source(
        sources,
        source_id=f"client_{index}",
        display_name=display_name,
        app_name=app_name,
        binary=binary,
        pid=_int(props.get("application.process.id")),
        client_id=index,
        node_id=0,
        backend=_backend_for_item(client),
    )


def _add_input_device_source(sources: list[dict[str, Any]], source: dict[str, Any]) -> None:
    name = _string(source.get("name"))
    description = _string(source.get("description"))
    props = source.get("properties")
    if not isinstance(props, dict):
        props = {}

    if not name or name.endswith(".monitor"):
        return
    if _string(props.get("device.class")) == "monitor":
        return

    sources.append(
        {
            "id": f"source_{_int(source.get('index'))}" if _int(source.get("index")) else name,
            "kind": "input_device",
            "backend": _backend_for_item(source),
            "device_id": name,
            "display_name": description or name,
        }
    )


def list_runtime_audio_sources() -> list[dict[str, Any]]:
    """Return currently visible runtime audio sources."""
    sources: list[dict[str, Any]] = []

    for source in _run_pactl_json("sources"):
        _add_input_device_source(sources, source)

    for sink_input in _run_pactl_json("sink-inputs"):
        _add_sink_input_source(sources, sink_input)

    for client in _run_pactl_json("clients"):
        _add_client_source(sources, client)

    return sources


def list_playback_audio_sources() -> list[dict[str, Any]]:
    """Return applications that currently own a playback stream.

    Unlike :func:`list_runtime_audio_sources`, this deliberately excludes
    clients without an active sink input. It is used when learning which
    process in a launcher/game process tree is actually producing audio.
    """
    sources: list[dict[str, Any]] = []
    for sink_input in _run_pactl_json("sink-inputs"):
        _add_sink_input_source(sources, sink_input)
    return sources
