"""
System tray integration for Clipper via the StatusNotifierItem D-Bus spec.

Uses only Gio/GLib — no Gtk or Adw imports — so it is safe to import in
headless environments and does not conflict with GTK4.

If D-Bus or the StatusNotifierWatcher is unavailable the class degrades
gracefully: all public methods become no-ops and a warning is printed to
stderr.
"""

import sys

from gi.repository import Gio, GLib  # noqa: E402 (gi.require_version not needed for Gio/GLib)
from i18n import _, text_direction
from icon_names import APP_ID, TRAY_IDLE

# ---------------------------------------------------------------------------
# D-Bus interface XML
# ---------------------------------------------------------------------------

_SNI_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category"    type="s"           access="read"/>
    <property name="Id"          type="s"           access="read"/>
    <property name="Title"       type="s"           access="read"/>
    <property name="Status"      type="s"           access="read"/>
    <property name="IconName"    type="s"           access="read"/>
    <property name="IconPixmap"  type="a(iiay)"     access="read"/>
    <property name="IconThemePath" type="s"         access="read"/>
    <property name="Menu"        type="o"           access="read"/>
    <property name="ItemIsMenu"  type="b"           access="read"/>
    <property name="ToolTip"     type="(sa(iiay)ss)" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta"       type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewTitle"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

_MENU_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version"       type="u"  access="read"/>
    <property name="TextDirection" type="s"  access="read"/>
    <property name="Status"        type="s"  access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId"      type="i"          direction="in"/>
      <arg name="recursionDepth" type="i"          direction="in"/>
      <arg name="propertyNames" type="as"         direction="in"/>
      <arg name="revision"      type="u"          direction="out"/>
      <arg name="layout"        type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids"           type="ai"       direction="in"/>
      <arg name="propertyNames" type="as"       direction="in"/>
      <arg name="properties"    type="a(ia{sv})" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id"        type="i" direction="in"/>
      <arg name="eventId"   type="s" direction="in"/>
      <arg name="data"      type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events"   type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai"      direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id"         type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids"           type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors"      type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent"   type="i"/>
    </signal>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
  </interface>
</node>
"""


def _sni_icon_theme_path() -> str:
    """Use the StatusNotifier host's standard icon theme."""
    return ""

# ---------------------------------------------------------------------------
# ClipperTray
# ---------------------------------------------------------------------------


class ClipperTray:
    """System-tray icon for Clipper using the StatusNotifierItem D-Bus spec."""

    STATUS_IDLE = "idle"
    STATUS_RECORDING = "recording"

    # Keep Clipper's regular tray icon in every state. Status remains available
    # through the tooltip and StatusNotifierItem state without swapping artwork.
    _ICON_NAMES = {
        "idle": TRAY_IDLE,
        "recording": TRAY_IDLE,
    }
    _SNI_STATUSES = {
        # GNOME AppIndicator hosts may hide Passive items completely.  Clipper
        # must remain reachable whenever tray mode is enabled.
        "idle": "Active",
        "recording": "Active",
    }
    # Human-readable tooltip description per status
    _TOOLTIP_DESCRIPTIONS = {
        "idle": _("Idle"),
        "recording": _("Recording"),
    }

    def __init__(
        self,
        show_callback=None,
        save_clip_callback=None,
        quit_callback=None,
        window_visible_callback=None,
    ) -> None:
        self._show_callback = show_callback
        self._save_clip_callback = save_clip_callback
        self._quit_callback = quit_callback
        self._window_visible_callback = window_visible_callback

        self._status: str = self.STATUS_IDLE
        self._available: bool = False
        self._connection: Gio.DBusConnection | None = None
        self._sni_reg_id: int = 0
        self._menu_reg_id: int = 0
        self._menu_revision: int = 1

        self._setup()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_status(self, status: str) -> None:
        """Update the tray icon and tooltip. status: 'idle' | 'recording'."""
        if status not in self._ICON_NAMES:
            status = self.STATUS_IDLE
        previous_status = self._status
        if status == previous_status:
            return

        previous_icon = self._ICON_NAMES.get(
            previous_status, self._ICON_NAMES[self.STATUS_IDLE]
        )
        previous_sni_status = self._SNI_STATUSES.get(
            previous_status, self._SNI_STATUSES[self.STATUS_IDLE]
        )
        self._status = status
        if not self._available:
            return

        icon_changed = self._icon_name() != previous_icon
        sni_status_changed = self._sni_status_name() != previous_sni_status

        self._menu_revision += 1
        try:
            assert self._connection is not None
            if icon_changed:
                self._connection.emit_signal(
                    None,
                    "/StatusNotifierItem",
                    "org.kde.StatusNotifierItem",
                    "NewIcon",
                    None,
                )
            self._connection.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                "NewToolTip",
                None,
            )
            if sni_status_changed:
                self._connection.emit_signal(
                    None,
                    "/StatusNotifierItem",
                    "org.kde.StatusNotifierItem",
                    "NewStatus",
                    GLib.Variant("(s)", (self._sni_status_name(),)),
                )
            # Tell the menu host that the layout changed (e.g. Save Clip enabled state)
            self._connection.emit_signal(
                None,
                "/MenuBar",
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._menu_revision, 0)),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"clipper: tray signal error: {exc}", file=sys.stderr)

    def refresh_menu(self) -> None:
        """Notify tray hosts that dynamic menu labels may have changed."""
        if not self._available:
            return
        self._menu_revision += 1
        try:
            assert self._connection is not None
            self._connection.emit_signal(
                None,
                "/MenuBar",
                "com.canonical.dbusmenu",
                "ItemsPropertiesUpdated",
                GLib.Variant(
                    "(a(ia{sv})a(ias))",
                    (
                        [
                            (1, self._item_props(1)),
                            (3, self._item_props(3)),
                        ],
                        [],
                    ),
                ),
            )
            self._connection.emit_signal(
                None,
                "/MenuBar",
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._menu_revision, 0)),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"clipper: tray menu signal error: {exc}", file=sys.stderr)

    def cleanup(self, *, preserve_registration: bool = False) -> None:
        """Stop tray integration, optionally leaving objects until disconnect."""
        if not self._available:
            return
        self._available = False
        if preserve_registration:
            return
        self._unregister_objects()

    def is_available(self) -> bool:
        """Return whether tray registration succeeded."""
        return self._available

    # ------------------------------------------------------------------
    # Internal setup / teardown
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            assert self._connection is not None

            sni_node = Gio.DBusNodeInfo.new_for_xml(_SNI_XML)
            menu_node = Gio.DBusNodeInfo.new_for_xml(_MENU_XML)

            self._sni_reg_id = self._connection.register_object(
                "/StatusNotifierItem",
                sni_node.interfaces[0],
                self._on_sni_method_call,
                self._on_sni_get_property,
                None,
            )

            self._menu_reg_id = self._connection.register_object(
                "/MenuBar",
                menu_node.interfaces[0],
                self._on_menu_method_call,
                self._on_menu_get_property,
                None,
            )

            # Passing the exported object path makes the watcher use this
            # connection's unique D-Bus name. Unlike claiming a dynamic
            # org.kde.StatusNotifierItem-* name, this works through Flatpak's
            # filtered session bus without broad own-name permission.
            self._connection.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", ("/StatusNotifierItem",)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )

            self._available = True

        except Exception as exc:  # noqa: BLE001
            print(f"clipper: system tray unavailable: {exc}", file=sys.stderr)
            self._unregister_objects()

    def _unregister_objects(self) -> None:
        if self._connection is None:
            return
        if self._sni_reg_id:
            try:
                self._connection.unregister_object(self._sni_reg_id)
            except Exception:  # noqa: BLE001
                pass
            self._sni_reg_id = 0
        if self._menu_reg_id:
            try:
                self._connection.unregister_object(self._menu_reg_id)
            except Exception:  # noqa: BLE001
                pass
            self._menu_reg_id = 0

    # ------------------------------------------------------------------
    # StatusNotifierItem D-Bus callbacks
    # ------------------------------------------------------------------

    def _on_sni_method_call(
        self,
        connection,  # noqa: ANN001
        sender,  # noqa: ANN001
        path,  # noqa: ANN001
        iface,  # noqa: ANN001
        method,  # noqa: ANN001
        params,  # noqa: ANN001
        invocation,  # noqa: ANN001
    ) -> None:
        # Activate (left-click) uses the same visibility action as the menu.
        invocation.return_value(None)
        if method == "Activate" and self._show_callback:
            self._schedule_callback(self._show_callback)

    def _on_sni_get_property(
        self,
        connection,  # noqa: ANN001
        sender,  # noqa: ANN001
        path,  # noqa: ANN001
        iface,  # noqa: ANN001
        prop_name,  # noqa: ANN001
    ) -> GLib.Variant | None:
        status = self._status
        if prop_name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if prop_name == "Id":
            return GLib.Variant("s", APP_ID)
        if prop_name == "Title":
            return GLib.Variant("s", _("Clipper"))
        if prop_name == "Status":
            return GLib.Variant("s", self._sni_status_name())
        if prop_name == "IconName":
            return GLib.Variant("s", self._icon_name())
        if prop_name == "IconPixmap":
            # The symbolic IconName is authoritative. An empty pixmap keeps
            # the StatusNotifier host responsible for theme recolouring.
            return GLib.Variant("a(iiay)", [])
        if prop_name == "IconThemePath":
            return GLib.Variant("s", _sni_icon_theme_path())
        if prop_name == "Menu":
            return GLib.Variant("o", "/MenuBar")
        if prop_name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if prop_name == "ToolTip":
            desc = self._TOOLTIP_DESCRIPTIONS.get(status, "")
            return GLib.Variant("(sa(iiay)ss)", ("", [], _("Clipper"), desc))
        return None

    def _icon_name(self) -> str:
        return self._ICON_NAMES.get(self._status, self._ICON_NAMES[self.STATUS_IDLE])

    def _sni_status_name(self) -> str:
        return self._SNI_STATUSES.get(self._status, self._SNI_STATUSES[self.STATUS_IDLE])

    def _visibility_action_label(self) -> str:
        if self._window_is_visible():
            return _("Hide Clipper")
        return _("Show Clipper")

    def _window_is_visible(self) -> bool:
        if self._window_visible_callback is None:
            return False
        try:
            return bool(self._window_visible_callback())
        except Exception as exc:  # noqa: BLE001
            print(f"clipper: tray visibility callback error: {exc}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # dbusmenu D-Bus callbacks
    # ------------------------------------------------------------------

    def _on_menu_method_call(
        self,
        connection,  # noqa: ANN001
        sender,  # noqa: ANN001
        path,  # noqa: ANN001
        iface,  # noqa: ANN001
        method,  # noqa: ANN001
        params,  # noqa: ANN001
        invocation,  # noqa: ANN001
    ) -> None:
        if method == "GetLayout":
            self._handle_get_layout(invocation)
        elif method == "Event":
            self._handle_event(params, invocation)
        elif method == "EventGroup":
            self._handle_event_group(params, invocation)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            ids = params.unpack()[0]
            invocation.return_value(GLib.Variant("(aiai)", (ids, [])))
        elif method == "GetGroupProperties":
            self._handle_get_group_properties(params, invocation)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method: {method}",
            )

    def _on_menu_get_property(
        self,
        connection,  # noqa: ANN001
        sender,  # noqa: ANN001
        path,  # noqa: ANN001
        iface,  # noqa: ANN001
        prop_name,  # noqa: ANN001
    ) -> GLib.Variant | None:
        if prop_name == "Version":
            return GLib.Variant("u", 3)
        if prop_name == "TextDirection":
            return GLib.Variant("s", text_direction())
        if prop_name == "Status":
            return GLib.Variant("s", "normal")
        if prop_name == "IconThemePath":
            icon_theme_path = _sni_icon_theme_path()
            return GLib.Variant("as", [icon_theme_path] if icon_theme_path else [])
        return None

    # ------------------------------------------------------------------
    # Menu method implementations
    # ------------------------------------------------------------------

    def _handle_get_layout(self, invocation) -> None:  # noqa: ANN001
        """Return the full menu layout as required by com.canonical.dbusmenu."""
        save_enabled = self._status == self.STATUS_RECORDING

        # Build child variants — each is (ia{sv}av)
        children = [
            GLib.Variant(
                "(ia{sv}av)",
                (
                    1,
                    {
                        "label": GLib.Variant("s", self._visibility_action_label()),
                        "enabled": GLib.Variant("b", True),
                        "visible": GLib.Variant("b", True),
                        "type": GLib.Variant("s", "standard"),
                    },
                    [],
                ),
            ),
            GLib.Variant(
                "(ia{sv}av)",
                (
                    2,
                    {
                        "type": GLib.Variant("s", "separator"),
                    },
                    [],
                ),
            ),
            GLib.Variant(
                "(ia{sv}av)",
                (
                    3,
                    {
                        "label": GLib.Variant("s", _("Save clip")),
                        "enabled": GLib.Variant("b", save_enabled),
                        "visible": GLib.Variant("b", True),
                        "type": GLib.Variant("s", "standard"),
                    },
                    [],
                ),
            ),
            GLib.Variant(
                "(ia{sv}av)",
                (
                    4,
                    {
                        "type": GLib.Variant("s", "separator"),
                    },
                    [],
                ),
            ),
            GLib.Variant(
                "(ia{sv}av)",
                (
                    5,
                    {
                        "label": GLib.Variant("s", _("Quit")),
                        "enabled": GLib.Variant("b", True),
                        "visible": GLib.Variant("b", True),
                        "type": GLib.Variant("s", "standard"),
                    },
                    [],
                ),
            ),
        ]

        # Root container with children embedded in 'av'
        layout = GLib.Variant(
            "(u(ia{sv}av))",
            (
                self._menu_revision,
                (0, {"children-display": GLib.Variant("s", "submenu")}, children),
            ),
        )
        invocation.return_value(layout)

    def _handle_event(self, params, invocation) -> None:  # noqa: ANN001
        """Dispatch menu-item click events to the appropriate callback."""
        item_id, event_id, _data, _timestamp = params.unpack()
        callback = self._callback_for_event(item_id, event_id)
        invocation.return_value(None)
        if callback is not None:
            self._schedule_callback(callback, flush_dbus=item_id == 5)

    def _handle_event_group(self, params, invocation) -> None:  # noqa: ANN001
        """Dispatch batched dbusmenu events used by libdbusmenu hosts."""
        callbacks = []
        for item_id, event_id, _data, _timestamp in params.unpack()[0]:
            callback = self._callback_for_event(item_id, event_id)
            if callback is not None:
                callbacks.append((callback, item_id == 5))

        invocation.return_value(GLib.Variant("(ai)", ([],)))
        for callback, flush_dbus in callbacks:
            self._schedule_callback(callback, flush_dbus=flush_dbus)

    def _callback_for_event(self, item_id: int, event_id: str):  # noqa: ANN201
        if event_id != "clicked":
            return None
        if item_id == 1:
            return self._show_callback
        if item_id == 3:
            return self._save_clip_callback
        if item_id == 5:
            return self._quit_callback
        return None

    @staticmethod
    def _schedule_callback(callback, *, flush_dbus: bool = False) -> None:  # noqa: ANN001
        """Reply to the tray host before running a potentially heavy action."""
        def run_callback() -> bool:
            callback()
            return False

        if flush_dbus:
            # Returning a GDBus invocation only queues its reply. Cinnamon's
            # libdbusmenu client waits synchronously for that reply, so give
            # the session bus one turn to deliver it before Quit tears down
            # the connection.
            timeout_add = getattr(GLib, "timeout_add", None)
            if callable(timeout_add):
                timeout_add(100, run_callback)
                return

        idle_add = getattr(GLib, "idle_add", None)
        if not callable(idle_add):
            callback()
            return

        idle_add(run_callback)

    def _handle_get_group_properties(self, params, invocation) -> None:  # noqa: ANN001
        """Return properties for a set of item IDs."""
        ids, _prop_names = params.unpack()
        result = []
        for item_id in ids:
            props = self._item_props(item_id)
            if props is not None:
                result.append((item_id, props))
        invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))

    def _item_props(self, item_id: int) -> dict | None:
        """Return the property dict for a given menu item ID, or None if unknown."""
        save_enabled = self._status == self.STATUS_RECORDING
        table: dict[int, dict] = {
            0: {"children-display": GLib.Variant("s", "submenu")},
            1: {
                "label": GLib.Variant("s", self._visibility_action_label()),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
                "type": GLib.Variant("s", "standard"),
            },
            2: {"type": GLib.Variant("s", "separator")},
            3: {
                "label": GLib.Variant("s", _("Save clip")),
                "enabled": GLib.Variant("b", save_enabled),
                "visible": GLib.Variant("b", True),
                "type": GLib.Variant("s", "standard"),
            },
            4: {"type": GLib.Variant("s", "separator")},
            5: {
                "label": GLib.Variant("s", _("Quit")),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
                "type": GLib.Variant("s", "standard"),
            },
        }
        return table.get(item_id)
