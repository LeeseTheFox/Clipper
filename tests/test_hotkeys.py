import types

import hotkeys
import pytest
from hotkeys import (
    HotkeyError,
    display_hotkey,
    effective_hotkey_label,
    normalize_hotkey,
    parse_hotkey,
)


def test_normalize_hotkey_orders_modifiers():
    assert normalize_hotkey("shift+ctrl+S") == "ctrl+shift+s"


def test_display_hotkey_uses_user_facing_labels():
    assert display_hotkey("ctrl+alt+s") == "Ctrl+Alt+S"


def test_display_hotkey_uses_symbols_for_gdk_punctuation_names():
    assert display_hotkey("ctrl+shift+braceright") == "Ctrl+Shift+]"
    assert display_hotkey("ctrl+slash") == "Ctrl+/"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Ctrl+Shift+[", "Ctrl+Shift+["),
        ("Strg+Ö", "Strg+Ö"),
        ("Ctrl+Alt+F16", "Ctrl+Alt+F16"),
    ],
)
def test_effective_portal_label_is_preserved_verbatim(label, expected):
    assert effective_hotkey_label(
        "ctrl+alt+s", portal_managed=True, portal_label=label
    ) == expected


def test_effective_portal_label_reports_unbound_shortcut():
    assert effective_hotkey_label(
        "ctrl+alt+s", portal_managed=True, portal_label=None
    ) == "Not set"


@pytest.mark.parametrize(
    ("preferred", "portal_label", "expected"),
    [
        ("F15", "Launch (6)", "F15"),
        ("ctrl+alt+F15", "Ctrl+Alt+Launch (6)", "Ctrl+Alt+F15"),
        ("F16", "Launch7", "F16"),
        ("F15", "Ctrl+Alt+S", "Ctrl+Alt+S"),
    ],
)
def test_effective_portal_label_restores_virtual_function_key_name(
    preferred, portal_label, expected
):
    assert effective_hotkey_label(
        preferred, portal_managed=True, portal_label=portal_label
    ) == expected


@pytest.mark.parametrize(
    ("preferred", "portal_label", "expected"),
    [
        ("F15", "Press Launch6", "F15"),
        ("ctrl+alt+s", "<Control><Alt>s", "Ctrl+Alt+S"),
        ("ctrl+bracketleft", "bracketleft", "["),
    ],
)
def test_effective_portal_label_normalizes_technical_accelerators(
    preferred, portal_label, expected
):
    assert effective_hotkey_label(
        preferred, portal_managed=True, portal_label=portal_label
    ) == expected


def test_hotkey_capture_prefers_physical_keymap_over_translated_layout_keyval(
    monkeypatch,
):
    class FakeDisplay:
        def map_keycode(self, keycode):
            assert keycode == 30
            keys = [
                types.SimpleNamespace(group=1, level=0),
                types.SimpleNamespace(group=0, level=0),
            ]
            keyvals = [201, 101]
            return True, keys, keyvals

    fake_gdk = types.SimpleNamespace(
        Display=types.SimpleNamespace(get_default=lambda: FakeDisplay()),
        ModifierType=types.SimpleNamespace(
            CONTROL_MASK=1 << 0,
            ALT_MASK=1 << 1,
            SHIFT_MASK=1 << 2,
            SUPER_MASK=1 << 3,
            META_MASK=1 << 4,
        ),
        keyval_to_lower=lambda keyval: keyval,
        keyval_name=lambda keyval: {
            101: "a",
            201: "LayoutSpecific_symbol",
            999: "LayoutSpecific_symbol",
        }.get(keyval),
    )
    monkeypatch.setattr(hotkeys, "Gdk", fake_gdk)

    captured = hotkeys.hotkey_from_gdk_event(999, fake_gdk.ModifierType.CONTROL_MASK, 30)

    assert captured is not None
    assert captured.canonical == "ctrl+a"
    assert captured.display == "Ctrl+A"


def test_hotkey_capture_recovers_extended_function_key_from_linux_keycode(
    monkeypatch,
):
    class FakeDisplay:
        def map_keycode(self, keycode):
            assert keycode == 194
            return (
                True,
                [types.SimpleNamespace(group=0, level=0)],
                [0x1008FF47],
            )

    fake_gdk = types.SimpleNamespace(
        Display=types.SimpleNamespace(get_default=lambda: FakeDisplay()),
        ModifierType=types.SimpleNamespace(
            CONTROL_MASK=1 << 0,
            ALT_MASK=1 << 1,
            SHIFT_MASK=1 << 2,
            SUPER_MASK=1 << 3,
            META_MASK=1 << 4,
        ),
        keyval_to_lower=lambda keyval: keyval,
        keyval_name=lambda keyval: {0x1008FF47: "Launch7"}.get(keyval),
    )
    monkeypatch.setattr(hotkeys, "Gdk", fake_gdk)

    captured = hotkeys.hotkey_from_gdk_event(
        0x1008FF47,
        fake_gdk.ModifierType.CONTROL_MASK | fake_gdk.ModifierType.ALT_MASK,
        194,
    )

    assert captured is not None
    assert captured.canonical == "ctrl+alt+F16"
    assert captured.display == "Ctrl+Alt+F16"
    assert captured.xdg_trigger == "CTRL+ALT+XF86Launch7"


def test_extended_function_key_portal_trigger_falls_back_to_readable_name(
    monkeypatch,
):
    monkeypatch.setattr(hotkeys, "Gdk", None)

    captured = parse_hotkey("F15")

    assert captured is not None
    assert captured.xdg_trigger == "F15"


def test_x11_extended_function_key_uses_active_keymap_keysym(monkeypatch):
    class FakeDisplay:
        def map_keycode(self, keycode):
            assert keycode == 193
            return (
                True,
                [types.SimpleNamespace(group=0, level=0)],
                [0x1008FF46],
            )

    fake_gdk = types.SimpleNamespace(
        Display=types.SimpleNamespace(get_default=lambda: FakeDisplay()),
        keyval_name=lambda keyval: {0x1008FF46: "Launch6"}.get(keyval),
    )
    monkeypatch.setattr(hotkeys, "Gdk", fake_gdk)

    assert hotkeys._x11_keysym_name("F15") == "XF86Launch6"


@pytest.mark.parametrize("number", range(13, 25))
def test_extended_function_keys_can_be_captured_without_modifiers(
    monkeypatch,
    number,
):
    fake_gdk = types.SimpleNamespace(
        ModifierType=types.SimpleNamespace(
            CONTROL_MASK=1 << 0,
            ALT_MASK=1 << 1,
            SHIFT_MASK=1 << 2,
            SUPER_MASK=1 << 3,
            META_MASK=1 << 4,
        ),
    )
    monkeypatch.setattr(hotkeys, "Gdk", fake_gdk)

    keycode = 191 + number - 13
    captured = hotkeys.hotkey_from_gdk_event(0, 0, keycode)

    assert captured is not None
    assert captured.canonical == f"F{number}"
    assert captured.display == f"F{number}"


def test_parse_gtk_accelerator_style_hotkey():
    hotkey = parse_hotkey("<Control><Alt>S")

    assert hotkey is not None
    assert hotkey.canonical == "ctrl+alt+s"


def test_function_key_can_be_used_without_modifier():
    assert normalize_hotkey("F12") == "F12"


def test_plain_typing_key_requires_modifier():
    with pytest.raises(HotkeyError, match="Add Ctrl, Alt, or Super"):
        parse_hotkey("s")


def test_shift_only_typing_key_requires_non_typing_modifier():
    with pytest.raises(HotkeyError, match="Add Ctrl, Alt, or Super"):
        parse_hotkey("shift+s")


def test_modifier_only_hotkey_is_rejected():
    with pytest.raises(HotkeyError, match="Choose a key too"):
        parse_hotkey("ctrl+alt")


def test_portal_reports_the_desktop_assigned_shortcut():
    statuses = []
    backend = hotkeys.PortalGlobalHotkeyBackend(
        parse_hotkey("ctrl+alt+s"),
        lambda: None,
        status_callback=lambda status, trigger: statuses.append((status, trigger)),
    )
    backend.registration_pending = True

    backend._report_shortcuts(
        [
            (
                "save-replay-buffer",
                {"trigger_description": "Ctrl+Shift+F10"},
            )
        ],
        "registered",
    )

    assert statuses == [("registered", "Ctrl+Shift+F10")]
    assert backend.registration_pending is False


def test_portal_reports_an_explicitly_unbound_shortcut_without_an_error():
    statuses = []
    backend = hotkeys.PortalGlobalHotkeyBackend(
        parse_hotkey("F15"),
        lambda: None,
        status_callback=lambda status, trigger: statuses.append((status, trigger)),
    )
    backend.registration_pending = True

    backend._report_shortcuts([], "changed")

    assert statuses == [("changed", None)]
    assert backend.registration_pending is False
    assert backend.last_error is None


def test_portal_binding_uses_selected_binding_identity(monkeypatch):
    class VariantStub:
        def __init__(self, signature, value):
            self.signature = signature
            self.value = value

    monkeypatch.setattr(hotkeys, "GLib", types.SimpleNamespace(Variant=VariantStub))
    monkeypatch.setattr(hotkeys, "Gdk", None)
    binding_id = hotkeys.shortcut_id_for_hotkey("F16")
    backend = hotkeys.PortalGlobalHotkeyBackend(
        parse_hotkey("F16"), lambda: None, shortcut_id=binding_id
    )
    backend._session_handle = "/org/freedesktop/portal/session/clipper"
    calls = []
    backend._call_request = lambda method, params, callback: calls.append(
        (method, params, callback)
    )

    backend._bind_shortcut()

    method, params, _callback = calls[0]
    shortcuts = params.value[1]
    assert method == "BindShortcuts"
    assert shortcuts[0][0] == binding_id
    assert shortcuts[0][1]["preferred_trigger"].value == "F16"


def test_locally_selected_hotkeys_get_distinct_stable_binding_ids():
    assert hotkeys.shortcut_id_for_hotkey("F15") == hotkeys.shortcut_id_for_hotkey(
        "f15"
    )
    assert hotkeys.shortcut_id_for_hotkey("F15") != hotkeys.shortcut_id_for_hotkey(
        "ctrl+alt+s"
    )


def test_portal_request_handles_response_emitted_before_method_return():
    request_path = "/org/freedesktop/portal/desktop/request/1_2/clipper"
    results = {"session_handle": "/org/freedesktop/portal/session/1_2/clipper"}

    class VariantStub:
        def __init__(self, value):
            self.value = value

        def unpack(self):
            return self.value

    class BusStub:
        def __init__(self):
            self.callback = None
            self.unsubscribed = []

        def signal_subscribe(self, *_args):
            self.callback = _args[-1]
            return 7

        def signal_unsubscribe(self, signal_id):
            self.unsubscribed.append(signal_id)

    bus = BusStub()

    class ProxyStub:
        def call_sync(self, *_args):
            callback = bus.callback
            assert callback is not None
            callback(
                None,
                None,
                request_path,
                None,
                None,
                VariantStub((0, results)),
            )
            return VariantStub((request_path,))

    received = []
    backend = hotkeys.PortalGlobalHotkeyBackend(
        parse_hotkey("ctrl+alt+s"), lambda: None
    )
    backend._bus = bus
    backend._proxy = ProxyStub()

    backend._call_request("CreateSession", None, received.append)

    assert received == [results]
    assert bus.unsubscribed == [7]
    assert backend._signal_ids == []


def test_portal_ignores_bind_cancellation_after_shortcut_change_confirmed():
    request_path = "/org/freedesktop/portal/desktop/request/1_2/clipper"

    class VariantStub:
        def __init__(self, value):
            self.value = value

        def unpack(self):
            return self.value

    class BusStub:
        def signal_subscribe(self, *_args):
            self.callback = _args[-1]
            return 7

        def signal_unsubscribe(self, _signal_id):
            pass

    bus = BusStub()

    class ProxyStub:
        def call_sync(self, *_args):
            return VariantStub((request_path,))

    errors = []
    statuses = []
    backend = hotkeys.PortalGlobalHotkeyBackend(
        parse_hotkey("ctrl+alt+s"),
        lambda: None,
        error_callback=errors.append,
        status_callback=lambda status, trigger: statuses.append((status, trigger)),
    )
    backend._bus = bus
    backend._proxy = ProxyStub()
    backend.registration_pending = True

    backend._call_request("BindShortcuts", None, lambda _results: None)
    backend._report_shortcuts(
        [(backend._shortcut_id, {"trigger_description": "<Control><Alt>s"})],
        "changed",
    )
    bus.callback(
        None,
        None,
        request_path,
        None,
        None,
        VariantStub((2, {})),
    )

    assert statuses == [("changed", "<Control><Alt>s")]
    assert errors == []
    assert backend.last_error is None


def test_portal_lists_effective_shortcut_after_bind_cancellation():
    request_path = "/org/freedesktop/portal/desktop/request/1_2/clipper"

    class VariantStub:
        def __init__(self, value):
            self.value = value

        def unpack(self):
            return self.value

    class BusStub:
        def signal_subscribe(self, *_args):
            self.callback = _args[-1]
            return 7

        def signal_unsubscribe(self, _signal_id):
            pass

    bus = BusStub()

    class ProxyStub:
        def call_sync(self, *_args):
            return VariantStub((request_path,))

    errors = []
    backend = hotkeys.PortalGlobalHotkeyBackend(
        parse_hotkey("ctrl+alt+a"),
        lambda: None,
        error_callback=errors.append,
    )
    backend._bus = bus
    backend._proxy = ProxyStub()
    backend._session_handle = "/org/freedesktop/portal/session/1_2/clipper"
    backend.registration_pending = True
    list_calls = []
    backend._list_shortcuts = lambda: list_calls.append(True)

    backend._call_request("BindShortcuts", None, lambda _results: None)
    bus.callback(
        None,
        None,
        request_path,
        None,
        None,
        VariantStub((1, {})),
    )

    assert errors == ["Portal request was denied or cancelled (1)"]
    assert list_calls == [True]
    assert backend.registration_pending is False
