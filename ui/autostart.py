"""Desktop autostart integration for Clipper.

Flatpak applications must ask the Background portal to manage login startup.
Native source builds use the freedesktop autostart directory so the same
setting remains useful while developing outside the sandbox.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from i18n import _
from icon_names import APP_ICON, APP_ID

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):  # pragma: no cover - exercised without PyGObject
    Gio = None
    GLib = None


NATIVE_AUTOSTART_BASENAME = f"{APP_ID}-native.desktop"
PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_BACKGROUND_INTERFACE = "org.freedesktop.portal.Background"
PORTAL_REQUEST_INTERFACE = "org.freedesktop.portal.Request"

AutostartCallback = Callable[[bool, str | None], None]


def running_in_flatpak(env: Mapping[str, str] | None = None) -> bool:
    environment = env if env is not None else os.environ
    return bool(environment.get("FLATPAK_ID"))


def default_autostart_dir(env: Mapping[str, str] | None = None) -> Path:
    environment = env if env is not None else os.environ
    config_home = environment.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "autostart"
    return Path.home() / ".config" / "autostart"


def _desktop_exec_argument(value: str) -> str:
    """Quote one Desktop Entry Exec argument without involving a shell."""
    escaped = value.replace("\\", "\\\\")
    for character in ('"', "`", "$"):
        escaped = escaped.replace(character, f"\\{character}")
    return f'"{escaped}"'


def _default_native_executable() -> str:
    source_launcher = Path(__file__).resolve().parents[1] / "clipper"
    if source_launcher.is_file():
        return str(source_launcher)
    return shutil.which("clipper") or "clipper"


def _variant_value(value):
    unpack = getattr(value, "unpack", None)
    return unpack() if unpack is not None else value


class BackgroundPortal:
    """Asynchronous client for ``org.freedesktop.portal.Background``."""

    def set_enabled(self, enabled: bool, callback: AutostartCallback) -> None:
        gio = Gio
        glib = GLib
        if gio is None or glib is None:
            callback(False, _("Desktop portal support requires PyGObject"))
            return

        try:
            bus = gio.bus_get_sync(gio.BusType.SESSION, None)
            proxy = gio.DBusProxy.new_sync(
                bus,
                gio.DBusProxyFlags.NONE,
                None,
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                PORTAL_BACKGROUND_INTERFACE,
                None,
            )
            unique_name = bus.get_unique_name()
            if not unique_name:
                raise RuntimeError("session bus did not provide a unique name")
        except Exception as exc:  # noqa: BLE001
            callback(
                False,
                _("Could not connect to the desktop portal: %(error)s")
                % {"error": exc},
            )
            return

        token = f"clipper_autostart_{secrets.token_hex(12)}"
        sender = unique_name.removeprefix(":").replace(".", "_")
        expected_path = f"{PORTAL_OBJECT_PATH}/request/{sender}/{token}"
        state: dict[str, object] = {"done": False, "signal_id": None}

        def finish(success: bool, error: str | None = None) -> None:
            if state["done"]:
                return
            state["done"] = True
            signal_id = state.get("signal_id")
            if signal_id is not None:
                bus.signal_unsubscribe(signal_id)
                state["signal_id"] = None
            callback(success, error)

        def on_response(
            _connection,
            _sender_name,
            _object_path,
            _interface_name,
            _signal_name,
            parameters,
        ) -> None:
            try:
                response, results = parameters.unpack()
            except Exception as exc:  # noqa: BLE001
                finish(
                    False,
                    _("Could not read the desktop portal response: %(error)s")
                    % {"error": exc},
                )
                return

            if response == 1:
                finish(False, _("The start-on-boot request was cancelled"))
                return
            if response != 0:
                finish(False, _("The desktop could not apply the start-on-boot setting"))
                return

            actual = _variant_value(results.get("autostart"))
            if actual is None:
                finish(
                    False,
                    _("The desktop portal did not report the autostart setting"),
                )
                return
            if bool(actual) != enabled:
                finish(
                    False,
                    _("The desktop did not allow Clipper to start automatically")
                    if enabled
                    else _("The desktop did not disable automatic startup"),
                )
                return
            finish(True)

        def subscribe(request_path: str) -> int:
            return bus.signal_subscribe(
                PORTAL_BUS_NAME,
                PORTAL_REQUEST_INTERFACE,
                "Response",
                request_path,
                None,
                gio.DBusSignalFlags.NONE,
                on_response,
            )

        # The token-derived path lets us subscribe before the method call and
        # avoid losing a very fast Response signal.
        state["signal_id"] = subscribe(expected_path)
        options = {
            "handle_token": glib.Variant("s", token),
            "reason": glib.Variant(
                "s", _("Start Clipper after login so it is ready to save clips")
            ),
            "autostart": glib.Variant("b", enabled),
            "commandline": glib.Variant("as", ["clipper", "--background"]),
        }
        parameters = glib.Variant("(sa{sv})", ("", options))

        def on_request_started(request_proxy, result, _user_data=None) -> None:
            if state["done"]:
                return
            try:
                request_path = request_proxy.call_finish(result).unpack()[0]
            except Exception as exc:  # noqa: BLE001
                finish(
                    False,
                    _("Could not request the start-on-boot setting: %(error)s")
                    % {"error": exc},
                )
                return

            if request_path != expected_path:
                signal_id = state.get("signal_id")
                if signal_id is not None:
                    bus.signal_unsubscribe(signal_id)
                state["signal_id"] = subscribe(request_path)

        try:
            proxy.call(
                "RequestBackground",
                parameters,
                gio.DBusCallFlags.NONE,
                -1,
                None,
                on_request_started,
            )
        except Exception as exc:  # noqa: BLE001
            finish(
                False,
                _("Could not request the start-on-boot setting: %(error)s")
                % {"error": exc},
            )


class AutostartManager:
    """Choose the sandbox-safe or native autostart implementation."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        portal: BackgroundPortal | None = None,
        autostart_dir: Path | None = None,
        native_executable: str | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._portal = portal or BackgroundPortal()
        self._autostart_dir = (
            Path(autostart_dir)
            if autostart_dir is not None
            else default_autostart_dir(self._env)
        )
        self._native_executable = native_executable or _default_native_executable()

    @property
    def native_desktop_file(self) -> Path:
        # xdg-desktop-portal owns <app-id>.desktop on the host. Keep the native
        # fallback separate so either backend can be disabled without deleting
        # or replacing the other backend's registration.
        return self._autostart_dir / NATIVE_AUTOSTART_BASENAME

    def set_enabled(self, enabled: bool, callback: AutostartCallback) -> None:
        if running_in_flatpak(self._env):
            self._portal.set_enabled(bool(enabled), callback)
            return

        try:
            self._set_native_enabled(bool(enabled))
        except OSError as exc:
            callback(
                False,
                _("Could not update the autostart file: %(error)s") % {"error": exc},
            )
            return
        callback(True, None)

    def _set_native_enabled(self, enabled: bool) -> None:
        path = self.native_desktop_file
        if not enabled:
            path.unlink(missing_ok=True)
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        contents = "\n".join(
            (
                "[Desktop Entry]",
                "Type=Application",
                "Name=Clipper",
                "Comment=" + _("Record clips on Linux"),
                f"Exec={_desktop_exec_argument(self._native_executable)} --background",
                f"Icon={APP_ICON}",
                "Terminal=false",
                "X-GNOME-Autostart-enabled=true",
                "X-Clipper-Autostart-Owner=native",
                "",
            )
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
        ) as temporary:
            temporary.write(contents)
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
