#!/usr/bin/env python3
"""clipper_ctl.py — command-line client for the clipper-engine IPC socket.

Usage:
    python tools/clipper_ctl.py [--socket PATH] <command>

Commands:
    status    Send get_status and print the response
    start     Send start_replay_buffer and print the response
    stop      Send stop_replay_buffer and print the response
    save      Send save_replay_buffer and print the response
    shutdown  Send shutdown and print the response
    listen    Print all incoming messages until Ctrl-C or EOF
"""

import argparse
import json
import os
import select
import socket
import sys

# ---------------------------------------------------------------------------
# Command name mapping (user-friendly → engine command)
# ---------------------------------------------------------------------------

CMD_MAP = {
    "status": "get_status",
    "start": "start_replay_buffer",
    "stop": "stop_replay_buffer",
    "save": "save_replay_buffer",
    "shutdown": "shutdown",
}


def default_socket_path() -> str:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "clipper-engine.sock")
    return f"/tmp/clipper-engine-{os.getuid()}.sock"


# ---------------------------------------------------------------------------
# Buffered socket client
# ---------------------------------------------------------------------------


class SocketClient:
    """Buffered newline-delimited JSON reader over a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(socket_path)
        self._buf = b""

    def send(self, payload: dict) -> None:
        self._sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))

    def readline(self, timeout: float | None = 5.0) -> str | None:
        """Return one decoded line, or None on timeout/connection close.

        Pass timeout=None to block indefinitely (useful for listen mode;
        KeyboardInterrupt will surface normally from the select call).
        """
        while b"\n" not in self._buf:
            if timeout is not None:
                ready, _, _ = select.select([self._sock], [], [], timeout)
                if not ready:
                    return None
            else:
                # Block until data; SIGINT → KeyboardInterrupt bubbles up.
                ready, _, _ = select.select([self._sock], [], [])
                if not ready:  # shouldn't happen with timeout=None, but be safe
                    return None

            try:
                chunk = self._sock.recv(4096)
            except OSError:
                return None
            if not chunk:  # remote closed
                return None
            self._buf += chunk

        idx = self._buf.index(b"\n")
        line = self._buf[:idx].decode("utf-8").strip()
        self._buf = self._buf[idx + 1 :]
        return line

    def read_message(self, timeout: float | None = 5.0) -> dict | None:
        line = self.readline(timeout=timeout)
        if line is None:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[warn] malformed JSON from engine: {exc}", file=sys.stderr)
            return None

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def run_command(client: SocketClient, engine_cmd: str) -> int:
    """Send one engine command; print any events then the response.

    Returns 0 if the response has ok=true, 1 otherwise.
    """
    client.send({"cmd": engine_cmd})
    while True:
        msg = client.read_message(timeout=5.0)
        if msg is None:
            print("ERROR: no response received within timeout", file=sys.stderr)
            return 1
        print(json.dumps(msg, indent=2))
        if "ok" in msg:
            return 0 if msg["ok"] else 1


def run_listen(client: SocketClient) -> int:
    """Print all incoming messages until EOF or Ctrl-C."""
    try:
        while True:
            msg = client.read_message(timeout=None)
            if msg is None:
                print("[connection closed]", file=sys.stderr)
                break
            print(json.dumps(msg, indent=2))
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clipper_ctl",
        description="CLI client for the clipper-engine IPC socket",
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        default=default_socket_path(),
        help="Unix socket path (default: %(default)s)",
    )
    parser.add_argument(
        "command",
        choices=[*CMD_MAP, "listen"],
        help="Command to send to the engine",
    )
    args = parser.parse_args()

    try:
        client = SocketClient(args.socket)
    except OSError as exc:
        print(f"ERROR: cannot connect to {args.socket!r}: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "listen":
            return run_listen(client)
        return run_command(client, CMD_MAP[args.command])
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
