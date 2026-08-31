"""Tests for tray status presentation logic."""

import sys
import types

gi = types.ModuleType("gi")
repository = types.ModuleType("gi.repository")
repository.Gio = types.SimpleNamespace()
repository.GLib = types.SimpleNamespace()
gi.repository = repository
sys.modules.setdefault("gi", gi)
sys.modules.setdefault("gi.repository", repository)

import tray as tray_module
from tray import ClipperTray


class VariantStub:
    def __init__(self, signature, value):
        self.signature = signature
        self.value = value

    def unpack(self):
        return self.value


class ConnectionStub:
    def __init__(self) -> None:
        self.signals: list[dict] = []

    def emit_signal(self, destination, path, interface, signal_name, parameters):
        self.signals.append(
            {
                "destination": destination,
                "path": path,
                "interface": interface,
                "signal_name": signal_name,
                "parameters": parameters,
            }
        )


class SetupConnectionStub:
    def __init__(self) -> None:
        self.calls = []
        self.registered = []
        self.unregistered = []

    def register_object(self, path, interface, method_call, get_property, set_property):
        self.registered.append(path)
        return len(self.registered)

    def unregister_object(self, registration_id):
        self.unregistered.append(registration_id)

    def call_sync(
        self,
        bus_name,
        object_path,
        interface,
        method,
        parameters,
        reply_type,
        flags,
        timeout,
        cancellable,
    ):
        self.calls.append(
            {
                "bus_name": bus_name,
                "object_path": object_path,
                "interface": interface,
                "method": method,
                "parameters": parameters,
            }
        )
        return VariantStub("()", ())


class InvocationStub:
    def __init__(self) -> None:
        self.returned = None
        self.return_calls = 0

    def return_value(self, value) -> None:
        self.returned = value
        self.return_calls += 1


tray_module.GLib.Variant = VariantStub


def make_tray(status: str = ClipperTray.STATUS_IDLE) -> ClipperTray:
    tray = object.__new__(ClipperTray)
    tray._status = status
    tray._window_visible_callback = None
    return tray


def test_idle_tray_state_is_neutral():
    tray = make_tray(ClipperTray.STATUS_IDLE)

    assert tray._icon_name() == "io.github.leesethefox.Clipper-symbolic"
    assert tray._sni_status_name() == "Active"


def test_recording_tray_state_keeps_regular_icon():
    tray = make_tray(ClipperTray.STATUS_RECORDING)

    assert tray._icon_name() == "io.github.leesethefox.Clipper-symbolic"
    assert tray._sni_status_name() == "Active"


def test_unknown_tray_state_falls_back_to_idle_presentation():
    tray = make_tray("unexpected")

    assert tray._icon_name() == "io.github.leesethefox.Clipper-symbolic"
    assert tray._sni_status_name() == "Active"


def test_icon_pixmap_property_is_empty_so_host_recolours_symbolic_icon():
    tray = make_tray()

    value = tray._on_sni_get_property(None, None, None, None, "IconPixmap")

    assert value.signature == "a(iiay)"
    assert value.value == []


def test_unchanged_status_does_not_emit_tray_signals():
    tray = make_tray(ClipperTray.STATUS_IDLE)
    tray._available = True
    tray._connection = ConnectionStub()
    tray._menu_revision = 1

    tray.set_status(ClipperTray.STATUS_IDLE)

    assert tray._connection.signals == []
    assert tray._menu_revision == 1


def test_status_change_with_same_icon_only_updates_tooltip_and_menu():
    tray = make_tray(ClipperTray.STATUS_IDLE)
    tray._available = True
    tray._connection = ConnectionStub()
    tray._menu_revision = 1

    tray.set_status(ClipperTray.STATUS_RECORDING)

    assert [signal["signal_name"] for signal in tray._connection.signals] == [
        "NewToolTip",
        "LayoutUpdated",
    ]
    assert tray._menu_revision == 2


def test_icon_name_property_exposes_symbolic_icon_for_host_recolouring():
    tray = make_tray()

    value = tray._on_sni_get_property(None, None, None, None, "IconName")

    assert value.signature == "s"
    assert value.value == "io.github.leesethefox.Clipper-symbolic"


def test_flatpak_uses_the_status_notifier_hosts_icon_theme(monkeypatch):
    tray = make_tray()
    monkeypatch.setenv("FLATPAK_ID", "io.github.leesethefox.Clipper")

    value = tray._on_sni_get_property(None, None, None, None, "IconThemePath")

    assert value.signature == "s"
    assert value.value == ""


def test_native_run_uses_the_status_notifier_hosts_icon_theme(monkeypatch):
    tray = make_tray()
    monkeypatch.delenv("FLATPAK_ID", raising=False)

    value = tray._on_sni_get_property(None, None, None, None, "IconThemePath")

    assert value.signature == "s"
    assert value.value == ""


def test_visibility_action_label_shows_clipper_when_window_is_hidden():
    tray = make_tray()
    tray._window_visible_callback = lambda: False

    assert tray._visibility_action_label() == "Show Clipper"


def test_visibility_action_label_hides_clipper_when_window_is_visible():
    tray = make_tray()
    tray._window_visible_callback = lambda: True

    assert tray._visibility_action_label() == "Hide Clipper"


def test_setup_registers_object_path_without_claiming_dynamic_bus_name(monkeypatch):
    connection = SetupConnectionStub()
    node = types.SimpleNamespace(interfaces=[object()])
    monkeypatch.setattr(
        tray_module.Gio,
        "BusType",
        types.SimpleNamespace(SESSION=0),
        raising=False,
    )
    monkeypatch.setattr(
        tray_module.Gio,
        "DBusCallFlags",
        types.SimpleNamespace(NONE=0),
        raising=False,
    )
    monkeypatch.setattr(
        tray_module.Gio,
        "DBusNodeInfo",
        types.SimpleNamespace(new_for_xml=lambda _xml: node),
        raising=False,
    )
    monkeypatch.setattr(
        tray_module.Gio,
        "bus_get_sync",
        lambda _bus_type, _cancellable: connection,
        raising=False,
    )

    tray = ClipperTray()

    assert tray.is_available() is True
    assert connection.registered == ["/StatusNotifierItem", "/MenuBar"]
    assert [call["method"] for call in connection.calls] == [
        "RegisterStatusNotifierItem"
    ]
    assert connection.calls[0]["parameters"].value == ("/StatusNotifierItem",)

    tray.cleanup()
    assert connection.unregistered == [1, 2]


def test_shutdown_can_preserve_registered_objects_until_dbus_disconnects():
    tray = make_tray()
    connection = SetupConnectionStub()
    tray._available = True
    tray._connection = connection
    tray._sni_reg_id = 1
    tray._menu_reg_id = 2

    tray.cleanup(preserve_registration=True)

    assert tray.is_available() is False
    assert connection.unregistered == []
    assert tray._sni_reg_id == 1
    assert tray._menu_reg_id == 2


def test_refresh_menu_emits_visibility_item_property_update():
    tray = make_tray()
    tray._available = True
    tray._connection = ConnectionStub()
    tray._menu_revision = 1
    tray._window_visible_callback = lambda: True

    tray.refresh_menu()

    property_signal = tray._connection.signals[0]
    assert property_signal["signal_name"] == "ItemsPropertiesUpdated"
    updated, removed = property_signal["parameters"].value
    visibility_item_id, visibility_props = updated[0]
    assert visibility_item_id == 1
    assert visibility_props["label"].value == "Hide Clipper"
    assert removed == []
    assert tray._connection.signals[1]["signal_name"] == "LayoutUpdated"


def test_about_to_show_reports_menu_needs_update():
    tray = make_tray()
    invocation = InvocationStub()

    tray._on_menu_method_call(
        None,
        None,
        None,
        None,
        "AboutToShow",
        None,
        invocation,
    )

    assert invocation.returned is not None
    assert invocation.returned.value == (True,)


def test_activate_replies_before_scheduling_window_callback(monkeypatch):
    scheduled = []
    callback_observations = []
    tray = make_tray()
    invocation = InvocationStub()
    tray._show_callback = lambda: callback_observations.append(invocation.return_calls)
    monkeypatch.setattr(
        tray_module.GLib,
        "idle_add",
        lambda callback: scheduled.append(callback),
        raising=False,
    )

    tray._on_sni_method_call(
        None, None, None, None, "Activate", None, invocation
    )

    assert invocation.return_calls == 1
    assert callback_observations == []
    assert len(scheduled) == 1
    assert scheduled[0]() is False
    assert callback_observations == [1]


def test_quit_menu_allows_dbus_reply_to_flush_before_shutdown(monkeypatch):
    scheduled = []
    callback_observations = []
    tray = make_tray()
    invocation = InvocationStub()
    tray._quit_callback = lambda: callback_observations.append(invocation.return_calls)
    monkeypatch.setattr(
        tray_module.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)),
        raising=False,
    )
    params = VariantStub("(isvu)", (5, "clicked", None, 0))

    tray._handle_event(params, invocation)

    assert invocation.return_calls == 1
    assert callback_observations == []
    assert len(scheduled) == 1
    assert scheduled[0][0] == 100
    assert scheduled[0][1]() is False
    assert callback_observations == [1]


def test_event_group_dispatches_cinnamon_libdbusmenu_quit(monkeypatch):
    scheduled = []
    callbacks = []
    tray = make_tray()
    invocation = InvocationStub()
    tray._quit_callback = lambda: callbacks.append("quit")
    monkeypatch.setattr(
        tray_module.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)),
        raising=False,
    )
    params = VariantStub(
        "(a(isvu))",
        ([(5, "clicked", None, 0)],),
    )

    tray._on_menu_method_call(
        None, None, None, None, "EventGroup", params, invocation
    )

    assert invocation.return_calls == 1
    assert invocation.returned is not None
    assert invocation.returned.signature == "(ai)"
    assert invocation.returned.value == ([],)
    assert callbacks == []
    assert scheduled[0][0] == 100
    assert scheduled[0][1]() is False
    assert callbacks == ["quit"]
