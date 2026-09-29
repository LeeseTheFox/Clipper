import socket

from monitor_ipc import (
    MonitorIpcServer,
    default_monitor_socket_path,
    validate_monitor_event,
)


def test_validate_monitor_event_accepts_strict_process_started():
    assert validate_monitor_event(
        {
            "event": "process_started",
            "rule_id": "steam-730",
            "pid": "123",
            "name": "Game",
            "ignored": "field",
        }
    ) == {
        "event": "process_started",
        "rule_id": "steam-730",
        "pid": 123,
        "name": "Game",
    }


def test_validate_monitor_event_rejects_unknown_event():
    assert validate_monitor_event({"event": "run", "rule_id": "steam-730"}) is None


def test_default_monitor_socket_path_uses_override(monkeypatch, tmp_path):
    socket_path = tmp_path / "custom" / "monitor.sock"
    monkeypatch.setenv("CLIPPER_MONITOR_SOCKET", str(socket_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert default_monitor_socket_path() == socket_path


def test_default_monitor_socket_path_uses_xdg_runtime_dir(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.delenv("CLIPPER_MONITOR_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    assert default_monitor_socket_path() == runtime_dir / "clipper" / "monitor.sock"


def test_monitor_ipc_server_creates_restrictive_socket_permissions(
    tmp_path,
    monkeypatch,
):
    socket_path = tmp_path / "clipper" / "monitor.sock"
    events = []
    fake_sockets = []

    class FakeSocket:
        def __init__(self, family, socket_type):
            self.family = family
            self.socket_type = socket_type
            self.bound_path = None
            self.closed = False
            fake_sockets.append(self)

        def bind(self, path):
            self.bound_path = path
            socket_path.write_text("", encoding="utf-8")

        def listen(self, backlog):
            self.backlog = backlog

        def settimeout(self, timeout):
            self.timeout = timeout

        def accept(self):
            raise OSError("stop")

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", FakeSocket)
    server = MonitorIpcServer(
        events.append,
        socket_path=socket_path,
        dispatcher=lambda callback, args: callback(*args),
    )

    assert server.start() is True
    try:
        assert (socket_path.parent.stat().st_mode & 0o777) == 0o700
        assert (socket_path.stat().st_mode & 0o777) == 0o600
        assert fake_sockets[0].family == socket.AF_UNIX
        assert fake_sockets[0].socket_type == socket.SOCK_STREAM
        assert fake_sockets[0].bound_path == str(socket_path)
        assert fake_sockets[0].backlog == 4
    finally:
        server.stop()

    assert fake_sockets[0].closed is True
    assert not socket_path.exists()


def test_monitor_ipc_server_dispatches_valid_json_lines(tmp_path):
    events = []
    server = MonitorIpcServer(
        events.append,
        socket_path=tmp_path / "clipper" / "monitor.sock",
        dispatcher=lambda callback, args: callback(*args),
    )

    server._handle_line(b'{"event":"process_started","rule_id":"rule-0","pid":42}')
    server._handle_line(b'{"event":"invalid","rule_id":"rule-0"}')
    server._handle_line(b"not-json")

    assert events == [
        {
            "event": "process_started",
            "rule_id": "rule-0",
            "pid": 42,
        }
    ]


def test_snapshot_is_atomic_and_rejects_invalid_rules():
    valid = {"event": "process_started", "rule_id": "game", "pid": 42}
    assert validate_monitor_event({"event": "snapshot", "rules": [valid]}) == {
        "event": "snapshot", "rules": [valid],
    }
    assert validate_monitor_event({"event": "snapshot", "rules": []}) == {
        "event": "snapshot", "rules": [],
    }
    assert validate_monitor_event({
        "event": "snapshot", "rules": [valid, {"event": "process_started", "rule_id": ""}],
    }) is None


def test_server_shutdown_interrupts_an_idle_connected_client(tmp_path):
    from threading import Event

    received = Event()
    server = MonitorIpcServer(
        lambda _event: received.set(), socket_path=tmp_path / "m.sock",
        dispatcher=lambda callback, args: callback(*args),
    )
    assert server.start()
    thread = server._thread
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(server.socket_path))
        client.sendall(b'{"event":"snapshot","rules":[]}\n')
        assert received.wait(2)
        server.stop()
        assert thread is not None and not thread.is_alive()
    assert not server.socket_path.exists()
