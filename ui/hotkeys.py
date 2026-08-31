"""
Global hotkey support for Clipper.

The parser/formatter is intentionally independent of the runtime backends so it
can be tested without a display server. Runtime support prefers the desktop
portal on Wayland and X11 key grabs on X11 sessions.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from i18n import _

Gdk: Any = None
Gio: Any = None
GLib: Any = None

try:
    import gi

    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib
except (ImportError, ValueError):
    Gdk = None
    Gio = None
    GLib = None


MODIFIER_ORDER = ("ctrl", "alt", "shift", "super")
DEFAULT_SHORTCUT_ID = "save-replay-buffer"
MODIFIER_LABELS = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "super": "Super",
}
MODIFIER_DISPLAY_LABELS = {
    "ctrl": _("Ctrl"),
    "alt": _("Alt"),
    "shift": _("Shift"),
    "super": _("Super"),
}
MODIFIER_ALIASES = {
    "control": "ctrl",
    "ctrl": "ctrl",
    "primary": "ctrl",
    "alt": "alt",
    "mod1": "alt",
    "option": "alt",
    "shift": "shift",
    "super": "super",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "logo": "super",
    "cmd": "super",
    "command": "super",
}
MODIFIER_KEY_NAMES = {
    "Control_L",
    "Control_R",
    "Shift_L",
    "Shift_R",
    "Alt_L",
    "Alt_R",
    "Meta_L",
    "Meta_R",
    "Super_L",
    "Super_R",
    "Hyper_L",
    "Hyper_R",
    "ISO_Level3_Shift",
}
KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "space": "space",
    "spacebar": "space",
    "tab": "Tab",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "pgup": "Page_Up",
    "pagedown": "Page_Down",
    "pgdn": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "print": "Print",
    "printscreen": "Print",
    "prtsc": "Print",
    "pause": "Pause",
}
BASE_PUNCTUATION_KEY_NAMES = {
    "grave",
    "minus",
    "equal",
    "bracketleft",
    "bracketright",
    "backslash",
    "semicolon",
    "apostrophe",
    "comma",
    "period",
    "slash",
}
SPECIAL_KEY_NAMES = {
    "space",
    "Tab",
    "BackSpace",
    "Delete",
    "Insert",
    "Home",
    "End",
    "Page_Up",
    "Page_Down",
    "Up",
    "Down",
    "Left",
    "Right",
    "Print",
    "Pause",
    "Escape",
    "Return",
}

# Linux evdev reserves KEY_F13 through KEY_F24 as consecutive codes 183-194.
# XKB/GDK hardware keycodes add 8 to evdev codes, but the default `inet`
# symbols file assigns several of these keys legacy XF86 names (for example,
# KEY_F16 can surface as XF86Launch7). Recover the physical function-key name
# before consulting that translated keymap.
EXTENDED_FUNCTION_KEYCODE_FIRST = 191
EXTENDED_FUNCTION_KEYCODE_LAST = 202
EXTENDED_FUNCTION_KEY_FIRST = 13

@dataclass(frozen=True)
class Hotkey:
    """Normalized hotkey definition."""

    modifiers: frozenset[str]
    key: str

    @property
    def canonical(self) -> str:
        parts = [mod for mod in MODIFIER_ORDER if mod in self.modifiers]
        parts.append(self.key.lower() if len(self.key) == 1 else self.key)
        return "+".join(parts)

    @property
    def display(self) -> str:
        parts = [
            MODIFIER_DISPLAY_LABELS[mod]
            for mod in MODIFIER_ORDER
            if mod in self.modifiers
        ]
        parts.append(_display_key(self.key))
        return "+".join(parts)

    @property
    def xdg_trigger(self) -> str:
        parts = [MODIFIER_LABELS[mod].upper() for mod in MODIFIER_ORDER if mod in self.modifiers]
        parts.append(_xdg_trigger_key(self.key))
        return "+".join(parts)


class HotkeyError(ValueError):
    """Raised when a hotkey string cannot be parsed or is unsafe."""


def parse_hotkey(value: str | None) -> Hotkey | None:
    """Parse a user/config hotkey string into a normalized object."""
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    # Accept GTK accelerator-style strings such as "<Control><Alt>S".
    text = re.sub(r"<([^>]+)>", r"\1+", text)
    pieces = [piece.strip() for piece in text.replace("-", "+").split("+") if piece.strip()]
    if not pieces:
        return None

    modifiers: set[str] = set()
    key: str | None = None
    for piece in pieces:
        folded = piece.casefold()
        if folded in MODIFIER_ALIASES:
            modifiers.add(MODIFIER_ALIASES[folded])
            continue
        if key is not None:
            raise HotkeyError(
                _("Use one main key for the shortcut: %(shortcut)s")
                % {"shortcut": value}
            )
        key = _normalize_key_name(piece)

    if key is None:
        raise HotkeyError(_("Choose a key too"))

    hotkey = Hotkey(frozenset(modifiers), key)
    validate_hotkey(hotkey)
    return hotkey


def normalize_hotkey(value: str | None) -> str:
    """Return the canonical config representation for a hotkey string."""
    hotkey = parse_hotkey(value)
    return hotkey.canonical if hotkey else ""


def display_hotkey(value: str | None) -> str:
    """Return a user-facing label for a hotkey string."""
    hotkey = parse_hotkey(value)
    return hotkey.display if hotkey else _("Not set")


def effective_hotkey_label(
    preferred_trigger: str | None,
    *,
    portal_managed: bool = False,
    portal_label: str | None = None,
) -> str:
    """Return a readable label for the effective portal shortcut.

    ``trigger_description`` may be localized presentation text or a technical
    GTK/XKB accelerator. Normalize only forms Clipper can identify safely, and
    leave localized or layout-specific labels untouched.
    """
    if portal_managed:
        label = portal_label.strip() if isinstance(portal_label, str) else ""
        return _display_portal_hotkey(preferred_trigger, label) or _("Not set")
    return display_hotkey(preferred_trigger)


def _display_portal_hotkey(
    preferred_trigger: str | None, portal_label: str
) -> str:
    """Normalize known portal accelerator forms without changing their binding."""
    label = re.sub(r"^Press\s+", "", portal_label, flags=re.IGNORECASE).strip()
    label = _readable_extended_function_label(preferred_trigger, label)
    if not label:
        return label

    try:
        return display_hotkey(label)
    except HotkeyError:
        # A portal may return a bare XKB keysym such as ``bracketleft``.
        # It is useful as display text even though Clipper intentionally does
        # not allow that unmodified typing key to be configured locally.
        if label in BASE_PUNCTUATION_KEY_NAMES or label in {
            "braceleft",
            "braceright",
            "parenleft",
            "parenright",
            "less",
            "greater",
            "colon",
            "quotedbl",
            "question",
            "asciitilde",
            "bar",
            "plus",
            "underscore",
        }:
            return _display_key(label)
        return label


def _readable_extended_function_label(
    preferred_trigger: str | None, portal_label: str
) -> str:
    """Restore F13..F24 names hidden by XKB's legacy ``inet`` aliases."""
    try:
        hotkey = parse_hotkey(preferred_trigger)
    except HotkeyError:
        return portal_label
    if hotkey is None:
        return portal_label

    aliases = {
        "F13": ("Tools", "XF86Tools"),
        "F14": ("Launch (5)", "Launch5", "Launch 5", "XF86Launch5"),
        "F15": ("Launch (6)", "Launch6", "Launch 6", "XF86Launch6"),
        "F16": ("Launch (7)", "Launch7", "Launch 7", "XF86Launch7"),
        "F17": ("Launch (8)", "Launch8", "Launch 8", "XF86Launch8"),
        "F18": ("Launch (9)", "Launch9", "Launch 9", "XF86Launch9"),
        "F20": ("Microphone Mute", "AudioMicMute", "XF86AudioMicMute"),
        "F21": ("Touchpad Toggle", "TouchpadToggle", "XF86TouchpadToggle"),
        "F22": ("Touchpad On", "TouchpadOn", "XF86TouchpadOn"),
    }
    for alias in aliases.get(hotkey.key, ()):
        if portal_label == alias:
            return hotkey.key
        suffix = f"+{alias}"
        if portal_label.endswith(suffix):
            return f"{portal_label[:-len(alias)]}{hotkey.key}"
    return portal_label


def shortcut_id_for_hotkey(value: str | None) -> str:
    """Return the portal identity for one explicitly selected binding.

    Portals persist user decisions by application action id. A new preferred
    trigger therefore needs a new binding identity to produce an approval
    request instead of silently restoring a previous trigger.
    """
    normalized = normalize_hotkey(value)
    if not normalized:
        return DEFAULT_SHORTCUT_ID
    identity = normalized
    if re.search(r"(?:^|\+)F(?:1[3-9]|2[0-4])$", normalized):
        # Do not restore portal entries created before extended function keys
        # were registered using their real XKB/XF86 keysyms.
        identity = f"extended-function-xkb-v2:{normalized}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{DEFAULT_SHORTCUT_ID}-{digest}"


def hotkey_from_gdk_event(keyval: int, state: Any, keycode: int | None = None) -> Hotkey | None:
    """Build a hotkey from a GTK key event."""
    if Gdk is None:
        raise HotkeyError(_("GTK key handling is unavailable"))

    gdk = Gdk
    key_name = _physical_key_name(keycode)
    if key_name is None:
        key_name = gdk.keyval_name(gdk.keyval_to_lower(keyval)) or gdk.keyval_name(keyval)
    if not key_name or key_name in MODIFIER_KEY_NAMES:
        return None

    modifiers: set[str] = set()
    if state & gdk.ModifierType.CONTROL_MASK:
        modifiers.add("ctrl")
    if state & gdk.ModifierType.ALT_MASK:
        modifiers.add("alt")
    if state & gdk.ModifierType.SHIFT_MASK:
        modifiers.add("shift")
    if state & gdk.ModifierType.SUPER_MASK or state & gdk.ModifierType.META_MASK:
        modifiers.add("super")

    hotkey = Hotkey(frozenset(modifiers), _normalize_key_name(key_name))
    validate_hotkey(hotkey)
    return hotkey


def _physical_key_name(keycode: int | None) -> str | None:
    if keycode is None:
        return None

    keycode = int(keycode)
    if EXTENDED_FUNCTION_KEYCODE_FIRST <= keycode <= EXTENDED_FUNCTION_KEYCODE_LAST:
        number = EXTENDED_FUNCTION_KEY_FIRST + keycode - EXTENDED_FUNCTION_KEYCODE_FIRST
        return f"F{number}"

    display = Gdk.Display.get_default() if Gdk is not None else None
    if display is None or not hasattr(display, "map_keycode"):
        return None

    try:
        found, keys, keyvals = display.map_keycode(keycode)
    except Exception:
        return None

    if not found:
        return None

    candidates = []
    for index, keyval in enumerate(keyvals):
        name = _key_name_from_keyval(keyval)
        if name is None:
            continue

        key = keys[index] if index < len(keys) else None
        level = getattr(key, "level", 0) if key is not None else 0
        group = getattr(key, "group", 0) if key is not None else 0
        score = _physical_key_candidate_score(name, level, group)
        if score is not None:
            candidates.append((score, name))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _xdg_trigger_key(key: str) -> str:
    """Return the keysym the desktop receives for a normalized key.

    GTK capture intentionally names Linux KEY_F13..KEY_F24 as function keys so
    they remain understandable in Clipper.  The standard XKB ``inet`` map,
    however, exposes several of those physical keys as XF86 keysyms.  KDE's
    portal registers the supplied keysym literally, so use the level-zero
    keysym from the active keymap for the portal while retaining F13..F24 in
    Clipper's config and UI.
    """
    match = re.fullmatch(r"F(1[3-9]|2[0-4])", key, re.IGNORECASE)
    if match is None:
        return key.lower() if len(key) == 1 else key

    number = int(match.group(1))
    keycode = EXTENDED_FUNCTION_KEYCODE_FIRST + number - EXTENDED_FUNCTION_KEY_FIRST
    gdk = Gdk
    if gdk is None:
        return f"F{number}"
    display = gdk.Display.get_default()
    if display is None or not hasattr(display, "map_keycode"):
        return f"F{number}"

    try:
        found, keys, keyvals = display.map_keycode(keycode)
    except Exception:
        return f"F{number}"
    if not found:
        return f"F{number}"

    for index, keyval in enumerate(keyvals):
        mapped_key = keys[index] if index < len(keys) else None
        group = getattr(mapped_key, "group", 0) if mapped_key is not None else 0
        level = getattr(mapped_key, "level", 0) if mapped_key is not None else 0
        if group != 0 or level != 0:
            continue
        name = gdk.keyval_name(keyval)
        if name:
            # GTK 4 omits the conventional XF86 prefix from these names, but
            # xkbcommon (and therefore KDE's portal backend) requires it.
            if (int(keyval) & 0xFFFF0000) == 0x10080000 and not name.startswith("XF86"):
                name = f"XF86{name}"
            return name
    return f"F{number}"


def _x11_keysym_name(key: str) -> str:
    """Return the active X11 keysym for a normalized shortcut key.

    Some physical extended function keys are labelled as ``F13`` through
    ``F24`` by Clipper, while XKB assigns them XF86 keysyms.  XGrabKey needs
    that assigned keysym to locate the keycode; asking for the literal F-key
    can therefore return no keycode on Cinnamon and other X11 desktops.
    """
    if re.fullmatch(r"F(1[3-9]|2[0-4])", key, re.IGNORECASE):
        return _xdg_trigger_key(key)
    return key


def _key_name_from_keyval(keyval: int) -> str | None:
    gdk = Gdk
    if gdk is None:
        return None
    return gdk.keyval_name(gdk.keyval_to_lower(keyval)) or gdk.keyval_name(keyval)


def _physical_key_candidate_score(name: str, level: int, group: int):
    if len(name) == 1 and name.isascii() and name.isalnum():
        return (0, level, group, name)
    if name in BASE_PUNCTUATION_KEY_NAMES:
        return (1, level, group, name)
    if _is_function_key(name):
        return (2, level, group, name)
    if name in SPECIAL_KEY_NAMES:
        return (3, level, group, name)
    return None


def validate_hotkey(hotkey: Hotkey) -> None:
    """Reject hotkeys that are likely to interfere with normal typing."""
    if not hotkey.key or hotkey.key in MODIFIER_KEY_NAMES:
        raise HotkeyError(_("Choose a key too"))

    if not (hotkey.modifiers - {"shift"}) and not _is_function_key(hotkey.key):
        raise HotkeyError(_("Add Ctrl, Alt, or Super"))


def _normalize_key_name(key: str) -> str:
    folded = key.strip().casefold()
    if not folded:
        raise HotkeyError(_("Hotkey key cannot be empty"))
    if folded in KEY_ALIASES:
        return KEY_ALIASES[folded]
    if len(key) == 1:
        return key.lower()

    name = key.strip()
    if _is_function_key(name.upper()):
        return name.upper()

    gdk = Gdk
    keyval = gdk.keyval_from_name(name) if gdk is not None else 0
    if keyval and gdk is not None:
        resolved = gdk.keyval_name(gdk.keyval_to_lower(keyval)) or gdk.keyval_name(keyval)
        if resolved:
            return resolved

    # Keep known GDK-style key names such as XF86AudioPlay intact.
    if name and name.replace("_", "").replace("-", "").isalnum():
        return name

    raise HotkeyError(_("Unknown key: %(key)s") % {"key": key})


def _display_key(key: str) -> str:
    if len(key) == 1:
        return key.upper()
    labels = {
        "space": _("Space"),
        "Tab": _("Tab"),
        "Page_Up": _("Page Up"),
        "Page_Down": _("Page Down"),
        "BackSpace": _("Backspace"),
        "Delete": _("Delete"),
        "Insert": _("Insert"),
        "Home": _("Home"),
        "End": _("End"),
        "Up": _("Up"),
        "Down": _("Down"),
        "Left": _("Left"),
        "Right": _("Right"),
        "Print": _("Print"),
        "Pause": _("Pause"),
        "Escape": _("Escape"),
        "Return": _("Enter"),
        "grave": "`",
        "minus": "-",
        "equal": "=",
        "bracketleft": "[",
        "bracketright": "]",
        "backslash": "\\",
        "semicolon": ";",
        "apostrophe": "'",
        "comma": ",",
        "period": ".",
        "slash": "/",
        "braceleft": "[",
        "braceright": "]",
        "parenleft": "9",
        "parenright": "0",
        "less": ",",
        "greater": ".",
        "colon": ";",
        "quotedbl": "'",
        "question": "/",
        "asciitilde": "`",
        "bar": "\\",
        "plus": "=",
        "underscore": "-",
    }
    return labels.get(key, key.replace("_", " "))


def _is_function_key(key: str) -> bool:
    folded = key.upper()
    if not folded.startswith("F"):
        return False
    try:
        number = int(folded[1:])
    except ValueError:
        return False
    return 1 <= number <= 35


class GlobalHotkeyManager:
    """Owns the active global hotkey backend and dispatches save requests."""

    def __init__(
        self,
        callback: Callable[[], None],
        error_callback: Callable[[str], None] | None = None,
        status_callback: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._callback = callback
        self._error_callback = error_callback
        self._status_callback = status_callback
        self._backend = None
        self._last_triggered = 0.0
        self.last_error: str | None = None

    @property
    def backend_name(self) -> str | None:
        return self._backend.name if self._backend else None

    @property
    def registration_pending(self) -> bool:
        return bool(self._backend and getattr(self._backend, "registration_pending", False))

    def set_hotkey(
        self, value: str | None, shortcut_id: str = DEFAULT_SHORTCUT_ID
    ) -> bool:
        self.stop()
        self.last_error = None

        try:
            hotkey = parse_hotkey(value)
        except HotkeyError as exc:
            self.last_error = str(exc)
            return False

        if hotkey is None:
            return True

        for backend in _candidate_backends(
            hotkey,
            self._on_triggered,
            self._on_backend_error,
            self._on_backend_status,
            shortcut_id,
        ):
            if backend.start():
                self._backend = backend
                return True
            self.last_error = backend.last_error

        if self.last_error is None:
            self.last_error = _("No global hotkey backend is available")
        return False

    def stop(self) -> None:
        if self._backend:
            self._backend.stop()
            self._backend = None

    def _on_triggered(self) -> None:
        now = time.monotonic()
        if now - self._last_triggered < 0.75:
            return
        self._last_triggered = now
        self._callback()

    def _on_backend_error(self, message: str) -> None:
        self.last_error = message
        if self._error_callback:
            self._error_callback(message)

    def _on_backend_status(self, status: str, trigger: str | None) -> None:
        if self._status_callback:
            self._status_callback(status, trigger)


def _candidate_backends(
    hotkey: Hotkey,
    callback: Callable[[], None],
    error_callback: Callable[[str], None],
    status_callback: Callable[[str, str | None], None],
    shortcut_id: str = DEFAULT_SHORTCUT_ID,
):
    session_type = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    if session_type == "wayland":
        yield PortalGlobalHotkeyBackend(
            hotkey, callback, error_callback, status_callback, shortcut_id
        )
        if os.environ.get("DISPLAY"):
            yield X11GlobalHotkeyBackend(hotkey, callback)
    else:
        if os.environ.get("DISPLAY"):
            yield X11GlobalHotkeyBackend(hotkey, callback)
        yield PortalGlobalHotkeyBackend(
            hotkey, callback, error_callback, status_callback, shortcut_id
        )


class PortalGlobalHotkeyBackend:
    """XDG desktop portal GlobalShortcuts backend."""

    name = "XDG GlobalShortcuts portal"

    def __init__(
        self,
        hotkey: Hotkey,
        callback: Callable[[], None],
        error_callback: Callable[[str], None] | None = None,
        status_callback: Callable[[str, str | None], None] | None = None,
        shortcut_id: str = DEFAULT_SHORTCUT_ID,
    ) -> None:
        self._hotkey = hotkey
        self._callback = callback
        self._error_callback = error_callback
        self._status_callback = status_callback
        self._shortcut_id = shortcut_id
        self._bus = None
        self._proxy = None
        self._session_handle: str | None = None
        self._signal_ids: list[int] = []
        self.last_error: str | None = None
        self.registration_pending = False

    def start(self) -> bool:
        if Gio is None or GLib is None:
            self.last_error = _("Portal hotkeys require PyGObject")
            return False

        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._proxy = Gio.DBusProxy.new_sync(
                self._bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.GlobalShortcuts",
                None,
            )
            self.registration_pending = True
            self._create_session()
        except Exception as exc:  # noqa: BLE001
            self.last_error = _("Portal hotkeys unavailable: %(error)s") % {
                "error": exc
            }
            self.stop()
            return False
        return True

    def stop(self) -> None:
        if self._bus:
            for signal_id in self._signal_ids:
                self._bus.signal_unsubscribe(signal_id)
        self._signal_ids = []

        if self._bus and self._session_handle:
            try:
                self._bus.call_sync(
                    "org.freedesktop.portal.Desktop",
                    self._session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
            except Exception:
                pass

        self._session_handle = None
        self.registration_pending = False
        self._proxy = None
        self._bus = None

    def _create_session(self) -> None:
        token = _portal_token("clipper_session")
        params = GLib.Variant(
            "(a{sv})",
            (
                {
                    "handle_token": GLib.Variant("s", _portal_token("clipper_request")),
                    "session_handle_token": GLib.Variant("s", token),
                },
            ),
        )
        self._call_request("CreateSession", params, self._on_session_created)

    def _on_session_created(self, results: dict) -> None:
        session_handle = _variant_value(results.get("session_handle"))
        if not session_handle:
            self._fail(_("Portal did not return a shortcut session"))
            return

        self._session_handle = session_handle
        self._subscribe_activated()
        self._subscribe_shortcuts_changed()
        self._bind_shortcut()

    def _bind_shortcut(self) -> None:
        if not self._session_handle:
            return

        shortcuts = [
            (
                self._shortcut_id,
                {
                    "description": GLib.Variant("s", _("Save clip")),
                    "preferred_trigger": GLib.Variant("s", self._hotkey.xdg_trigger),
                },
            )
        ]
        params = GLib.Variant(
            "(oa(sa{sv})sa{sv})",
            (self._session_handle, shortcuts, "", {}),
        )
        self._call_request("BindShortcuts", params, self._on_shortcut_bound)

    def _on_shortcut_bound(self, results: dict) -> None:
        shortcuts = _variant_value(results.get("shortcuts"))
        if shortcuts is not None:
            self._report_shortcuts(shortcuts, "registered")
            return
        self._list_shortcuts()

    def _list_shortcuts(self) -> None:
        if not self._session_handle:
            return
        params = GLib.Variant(
            "(oa{sv})",
            (
                self._session_handle,
                {"handle_token": GLib.Variant("s", _portal_token("clipper_request"))},
            ),
        )
        self._call_request("ListShortcuts", params, self._on_shortcuts_listed)

    def _on_shortcuts_listed(self, results: dict) -> None:
        self._report_shortcuts(_variant_value(results.get("shortcuts")), "registered")

    def _report_shortcuts(self, shortcuts: Any, status: str) -> None:
        trigger = _shortcut_trigger_description(shortcuts, self._shortcut_id)
        self.registration_pending = False
        if self._status_callback:
            self._status_callback(status, trigger)

    def _subscribe_activated(self) -> None:
        if not self._bus:
            return
        signal_id = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.GlobalShortcuts",
            "Activated",
            "/org/freedesktop/portal/desktop",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_activated,
        )
        self._signal_ids.append(signal_id)

    def _subscribe_shortcuts_changed(self) -> None:
        if not self._bus:
            return
        signal_id = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.GlobalShortcuts",
            "ShortcutsChanged",
            "/org/freedesktop/portal/desktop",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_shortcuts_changed,
        )
        self._signal_ids.append(signal_id)

    def _on_shortcuts_changed(
        self,
        connection,
        sender_name,
        object_path,
        interface_name,
        signal_name,
        parameters,
    ) -> None:
        try:
            session_handle, shortcuts = parameters.unpack()
        except Exception:
            return
        if session_handle == self._session_handle:
            self._report_shortcuts(shortcuts, "changed")

    def _on_activated(
        self,
        connection,
        sender_name,
        object_path,
        interface_name,
        signal_name,
        parameters,
    ) -> None:
        try:
            session_handle, shortcut_id, _timestamp, _options = parameters.unpack()
        except Exception:
            return

        if session_handle == self._session_handle and shortcut_id == self._shortcut_id:
            GLib.idle_add(self._callback)

    def _call_request(
        self,
        method: str,
        params: Any,
        callback: Callable[[dict], None],
    ) -> None:
        if not self._proxy or not self._bus:
            return

        request_path: str | None = None
        queued_responses: list[tuple[str, Any]] = []
        response_handled = False
        signal_id: int | None = None

        def unsubscribe_response() -> None:
            if self._bus and signal_id is not None:
                self._bus.signal_unsubscribe(signal_id)
            if signal_id in self._signal_ids:
                self._signal_ids.remove(signal_id)

        def handle_response(parameters: Any) -> None:
            nonlocal response_handled
            if response_handled:
                return
            response_handled = True
            unsubscribe_response()
            try:
                response, results = parameters.unpack()
            except Exception as exc:  # noqa: BLE001
                self._fail(_("Portal response parse failed: %(error)s") % {"error": exc})
                return

            if response != 0:
                # GNOME can confirm a newly accepted binding with the
                # ShortcutsChanged signal and then close the BindShortcuts
                # request with response 2. The signal is the authoritative
                # effective state, so do not turn that completed change into
                # a contradictory error.
                if method == "BindShortcuts" and not self.registration_pending:
                    return
                self._fail(
                    _("Portal request was denied or cancelled (%(response)s)")
                    % {"response": response}
                )
                if method == "BindShortcuts":
                    # A rejected dialog may still change the portal's stored
                    # action. Query the session so the UI can show the
                    # shortcut that is actually effective.
                    self._list_shortcuts()
                return

            callback(results)

        def on_response(
            connection,
            sender_name,
            object_path,
            interface_name,
            signal_name,
            parameters,
        ):
            if request_path is None:
                queued_responses.append((object_path, parameters))
                return
            if object_path != request_path:
                return
            handle_response(parameters)

        # Subscribe before calling the portal. Fast implementations can emit a
        # response before the synchronous method call has returned its handle.
        subscribed_signal_id: int = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        )
        signal_id = subscribed_signal_id
        self._signal_ids.append(subscribed_signal_id)
        try:
            result = self._proxy.call_sync(
                method,
                params,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            request_path = result.unpack()[0]
        except Exception:
            unsubscribe_response()
            raise

        for queued_path, queued_parameters in queued_responses:
            if queued_path == request_path:
                handle_response(queued_parameters)
                break

    def _fail(self, message: str) -> None:
        self.last_error = message
        self.registration_pending = False
        if self._error_callback:
            self._error_callback(message)


class X11GlobalHotkeyBackend:
    """Global hotkey backend based on XGrabKey."""

    name = "X11"

    _KEY_PRESS = 2
    _GRAB_MODE_ASYNC = 1
    _LOCK_MASK = 1 << 1
    _CONTROL_MASK = 1 << 2
    _MOD1_MASK = 1 << 3
    _MOD2_MASK = 1 << 4
    _MOD4_MASK = 1 << 6

    def __init__(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        self._hotkey = hotkey
        self._callback = callback
        self._x11: Any = None
        self._display: Any = None
        self._root = 0
        self._keycode = 0
        self._modifiers = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._error_handler: Any = None
        self._grab_error = False
        self.last_error: str | None = None

    def start(self) -> bool:
        if Gdk is None or GLib is None:
            self.last_error = _("X11 hotkeys require PyGObject")
            return False

        try:
            self._load_x11()
            self._display = self._x11.XOpenDisplay(None)
            if not self._display:
                self.last_error = _("Could not open X11 display")
                return False

            keyval = Gdk.keyval_from_name(_x11_keysym_name(self._hotkey.key))
            if not keyval and len(self._hotkey.key) == 1:
                keyval = ord(self._hotkey.key)
            if not keyval:
                self.last_error = _("Could not resolve key %(key)s") % {
                    "key": self._hotkey.key
                }
                self.stop()
                return False

            self._keycode = self._x11.XKeysymToKeycode(self._display, keyval)
            if not self._keycode:
                self.last_error = _("X11 has no keycode for %(shortcut)s") % {
                    "shortcut": self._hotkey.display
                }
                self.stop()
                return False

            self._root = self._x11.XDefaultRootWindow(self._display)
            self._modifiers = self._x11_modifier_mask(self._hotkey.modifiers)
            if not self._grab_key():
                self.stop()
                return False

            self._running = True
            self._thread = threading.Thread(target=self._event_loop, daemon=True)
            self._thread.start()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = _("X11 hotkey unavailable: %(error)s") % {"error": exc}
            self.stop()
            return False

    def stop(self) -> None:
        self._running = False
        if self._display and self._x11 and self._keycode:
            for mask in self._ignored_lock_masks():
                self._x11.XUngrabKey(
                    self._display,
                    self._keycode,
                    self._modifiers | mask,
                    self._root,
                )
            self._x11.XFlush(self._display)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

        if self._display and self._x11:
            self._x11.XCloseDisplay(self._display)
        self._display = None

    def _load_x11(self) -> None:
        path = ctypes.util.find_library("X11")
        if not path:
            raise RuntimeError("libX11 was not found")

        self._x11 = ctypes.CDLL(path)
        self._x11.XInitThreads()
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XKeysymToKeycode.restype = ctypes.c_uint
        self._x11.XGrabKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._x11.XUngrabKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ulong,
        ]
        self._x11.XPending.argtypes = [ctypes.c_void_p]
        self._x11.XPending.restype = ctypes.c_int
        self._x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._x11.XFlush.argtypes = [ctypes.c_void_p]
        self._x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        self._x11.XSetErrorHandler.restype = ctypes.c_void_p

    def _grab_key(self) -> bool:
        self._grab_error = False

        error_callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def on_x_error(display, event):
            self._grab_error = True
            return 0

        self._error_handler = error_callback_type(on_x_error)
        previous_handler = self._x11.XSetErrorHandler(self._error_handler)
        for mask in self._ignored_lock_masks():
            self._x11.XGrabKey(
                self._display,
                self._keycode,
                self._modifiers | mask,
                self._root,
                True,
                self._GRAB_MODE_ASYNC,
                self._GRAB_MODE_ASYNC,
            )
        self._x11.XSync(self._display, False)
        self._x11.XSetErrorHandler(previous_handler)

        if self._grab_error:
            self.last_error = _(
                "%(shortcut)s is already registered by another app"
            ) % {"shortcut": self._hotkey.display}
            return False
        return True

    def _event_loop(self) -> None:
        event = _XEvent()
        while self._running:
            if not self._x11.XPending(self._display):
                time.sleep(0.05)
                continue

            self._x11.XNextEvent(self._display, ctypes.byref(event))
            if event.type != self._KEY_PRESS:
                continue

            state = event.xkey.state & ~(self._LOCK_MASK | self._MOD2_MASK)
            if event.xkey.keycode == self._keycode and state == self._modifiers:
                GLib.idle_add(self._callback)

    def _ignored_lock_masks(self):
        return (0, self._LOCK_MASK, self._MOD2_MASK, self._LOCK_MASK | self._MOD2_MASK)

    def _x11_modifier_mask(self, modifiers: frozenset[str]) -> int:
        mask = 0
        if "ctrl" in modifiers:
            mask |= self._CONTROL_MASK
        if "alt" in modifiers:
            mask |= self._MOD1_MASK
        if "shift" in modifiers:
            mask |= 1
        if "super" in modifiers:
            mask |= self._MOD4_MASK
        return mask


class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xkey", _XKeyEvent),
        ("pad", ctypes.c_long * 24),
    ]


def _portal_token(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _variant_value(value: Any) -> Any:
    if GLib is None:
        return value
    if isinstance(value, GLib.Variant):
        return value.unpack()
    return value


def _shortcut_trigger_description(shortcuts: Any, shortcut_id: str) -> str | None:
    """Return the desktop-assigned, user-readable trigger for a shortcut."""
    shortcuts = _variant_value(shortcuts)
    if not isinstance(shortcuts, (list, tuple)):
        return None

    for item in shortcuts:
        item = _variant_value(item)
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        item_id, properties = item
        if str(_variant_value(item_id)) != shortcut_id:
            continue
        properties = _variant_value(properties)
        if not isinstance(properties, dict):
            return None
        trigger = _variant_value(properties.get("trigger_description"))
        label = str(trigger or "").strip()
        return label or None
    return None
