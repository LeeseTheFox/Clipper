"""Unix-socket IPC server for events from ``clipper-monitor-host``."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

MonitorEventCallback = Callable[[dict], object]
Dispatcher = Callable[[Callable, tuple], object]

_VALID_EVENTS = {"process_started", "process_stopped"}


def default_monitor_socket_path() -> Path:
    override = os.environ.get("CLIPPER_MONITOR_SOCKET")
    if override:
        return Path(override)

    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        return Path(xdg_runtime_dir) / "clipper" / "monitor.sock"

    return Path(f"/tmp/clipper-monitor-{os.getuid()}.sock")


def validate_monitor_event(value: object) -> dict | None:
    """Return a strict normalized monitor event, or None when invalid."""
    if not isinstance(value, dict):
        return None

    event = value.get("event")
    rule_id = value.get("rule_id")
    if event not in _VALID_EVENTS or not isinstance(rule_id, str) or not rule_id:
        return None

    normalized: dict = {
        "event": event,
        "rule_id": rule_id,
    }

    pid = value.get("pid")
    if pid is not None:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return None
        if pid_int <= 0:
            return None
        normalized["pid"] = pid_int

    name = value.get("name")
    if name is not None:
        if not isinstance(name, str):
            return None
        normalized["name"] = name[:256]

    return normalized


class MonitorIpcServer:
    """Accept newline-delimited JSON events from the host monitor."""

    def __init__(
        self,
        callback: MonitorEventCallback,
        *,
        socket_path: Path | None = None,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self._callback = callback
        self._socket_path = socket_path or default_monitor_socket_path()
        self._dispatcher = dispatcher or self._default_dispatcher
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def start(self) -> bool:
        if self._sock is not None:
            return True

        self._socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600)
            sock.listen(4)
            sock.settimeout(0.2)
        except OSError:
            sock.close()
            return False

        self._sock = sock
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="clipper-monitor-ipc",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop_event.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._handle_connection(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        buffer = b""
        while not self._stop_event.is_set():
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._handle_line(line)
            if len(buffer) > 65536:
                return

    def _handle_line(self, line: bytes) -> None:
        try:
            raw_event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        event = validate_monitor_event(raw_event)
        if event is None:
            return
        self._dispatcher(self._callback, (event,))

    @staticmethod
    def _default_dispatcher(callback: Callable, args: tuple) -> object:
        from gi.repository import GLib

        return GLib.idle_add(callback, *args)
