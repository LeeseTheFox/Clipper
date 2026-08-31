import types
from pathlib import Path

import autostart as autostart_module
from autostart import (
    APP_ICON,
    APP_ID,
    NATIVE_AUTOSTART_BASENAME,
    AutostartManager,
    BackgroundPortal,
    default_autostart_dir,
    running_in_flatpak,
)


class PortalStub:
    def __init__(self) -> None:
        self.requests = []

    def set_enabled(self, enabled, callback) -> None:
        self.requests.append((enabled, callback))


def test_flatpak_uses_background_portal_without_writing_host_file(tmp_path):
    portal = PortalStub()
    manager = AutostartManager(
        env={"FLATPAK_ID": APP_ID},
        portal=portal,
        autostart_dir=tmp_path,
    )
    results = []

    manager.set_enabled(True, lambda success, error: results.append((success, error)))

    assert len(portal.requests) == 1
    assert portal.requests[0][0] is True
    assert not manager.native_desktop_file.exists()
    assert results == []

    portal.requests[0][1](True, None)
    assert results == [(True, None)]


def test_native_autostart_writes_and_removes_desktop_entry(tmp_path):
    manager = AutostartManager(
        env={},
        autostart_dir=tmp_path,
        native_executable="/home/Clipper Project/clipper",
    )
    results = []

    manager.set_enabled(True, lambda success, error: results.append((success, error)))

    desktop_file = tmp_path / NATIVE_AUTOSTART_BASENAME
    contents = desktop_file.read_text(encoding="utf-8")
    assert results == [(True, None)]
    assert "Type=Application" in contents
    assert 'Exec="/home/Clipper Project/clipper" --background' in contents
    assert f"Icon={APP_ICON}" in contents
    assert "X-Clipper-Autostart-Owner=native" in contents

    manager.set_enabled(False, lambda success, error: results.append((success, error)))

    assert not desktop_file.exists()
    assert results[-1] == (True, None)


def test_native_toggle_never_modifies_portal_owned_entry(tmp_path):
    portal_file = tmp_path / f"{APP_ID}.desktop"
    portal_contents = "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Name=Clipper",
            f"X-XDP-Autostart={APP_ID}",
            "Exec=flatpak run io.github.leesethefox.Clipper",
            "",
        )
    )
    portal_file.write_text(portal_contents, encoding="utf-8")
    manager = AutostartManager(
        env={},
        autostart_dir=tmp_path,
        native_executable="/usr/bin/clipper",
    )

    manager.set_enabled(True, lambda _success, _error: None)

    assert manager.native_desktop_file.name == NATIVE_AUTOSTART_BASENAME
    assert manager.native_desktop_file != portal_file
    assert manager.native_desktop_file.exists()
    assert portal_file.read_text(encoding="utf-8") == portal_contents

    manager.set_enabled(False, lambda _success, _error: None)

    assert not manager.native_desktop_file.exists()
    assert portal_file.read_text(encoding="utf-8") == portal_contents


def test_native_autostart_reports_filesystem_failure(tmp_path):
    not_a_directory = tmp_path / "config-file"
    not_a_directory.write_text("occupied", encoding="utf-8")
    manager = AutostartManager(env={}, autostart_dir=not_a_directory)
    results = []

    manager.set_enabled(True, lambda success, error: results.append((success, error)))

    assert results[0][0] is False
    assert "Could not update the autostart file" in results[0][1]


def test_autostart_environment_helpers_use_xdg_and_flatpak_variables(tmp_path):
    assert running_in_flatpak({"FLATPAK_ID": APP_ID}) is True
    assert running_in_flatpak({}) is False
    assert default_autostart_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        Path(tmp_path) / "autostart"
    )


class VariantStub:
    def __init__(self, signature, value):
        self.signature = signature
        self.value = value

    def unpack(self):
        return self.value


class PortalBusStub:
    def __init__(self) -> None:
        self.subscriptions = {}
        self.unsubscribed = []

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, *args):
        signal_id = len(self.subscriptions) + 1
        self.subscriptions[signal_id] = {"args": args, "callback": args[-1]}
        return signal_id

    def signal_unsubscribe(self, signal_id):
        self.unsubscribed.append(signal_id)


class PortalProxyStub:
    def __init__(self) -> None:
        self.calls = []
        self.request_path = None

    def call(self, method, parameters, flags, timeout, cancellable, callback):
        self.calls.append((method, parameters, flags, timeout, cancellable))
        token = parameters.value[1]["handle_token"].value
        self.request_path = (
            "/org/freedesktop/portal/desktop/request/1_42/" + token
        )
        callback(self, object(), None)

    def call_finish(self, _result):
        return VariantStub("(o)", (self.request_path,))


def _portal_stubs(monkeypatch):
    bus = PortalBusStub()
    proxy = PortalProxyStub()
    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SESSION=0),
        DBusProxyFlags=types.SimpleNamespace(NONE=0),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        DBusProxy=types.SimpleNamespace(new_sync=lambda *_args: proxy),
        bus_get_sync=lambda *_args: bus,
    )
    monkeypatch.setattr(autostart_module, "Gio", gio)
    monkeypatch.setattr(
        autostart_module,
        "GLib",
        types.SimpleNamespace(Variant=VariantStub),
    )
    monkeypatch.setattr(autostart_module.secrets, "token_hex", lambda _size: "abc123")
    return bus, proxy


def test_background_portal_requests_effective_autostart_value(monkeypatch):
    bus, proxy = _portal_stubs(monkeypatch)
    results = []

    BackgroundPortal().set_enabled(
        True, lambda success, error: results.append((success, error))
    )

    method, parameters, _flags, timeout, cancellable = proxy.calls[0]
    assert method == "RequestBackground"
    assert parameters.signature == "(sa{sv})"
    parent_window, options = parameters.value
    assert parent_window == ""
    assert options["autostart"].value is True
    assert options["commandline"].value == ["clipper", "--background"]
    assert timeout == -1
    assert cancellable is None

    subscription = next(iter(bus.subscriptions.values()))
    subscription["callback"](
        None,
        None,
        None,
        None,
        None,
        VariantStub("(ua{sv})", (0, {"autostart": VariantStub("b", True)})),
    )

    assert results == [(True, None)]
    assert bus.unsubscribed == [1]


def test_background_portal_reports_denied_autostart(monkeypatch):
    bus, _proxy = _portal_stubs(monkeypatch)
    results = []

    BackgroundPortal().set_enabled(
        True, lambda success, error: results.append((success, error))
    )
    subscription = next(iter(bus.subscriptions.values()))
    subscription["callback"](
        None,
        None,
        None,
        None,
        None,
        VariantStub("(ua{sv})", (0, {"autostart": VariantStub("b", False)})),
    )

    assert results[0][0] is False
    assert "did not allow Clipper" in results[0][1]
