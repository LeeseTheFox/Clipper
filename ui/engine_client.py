#!/usr/bin/env python3
"""
IPC client for communicating with the Clipper engine.

Provides an async, GLib-integrated client for sending commands and
receiving events from the engine via Unix domain socket.
"""

import json
import socket
from collections import deque
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

# Connection states
STATE_DISCONNECTED = 0
STATE_RECONNECTING = 1
STATE_CONNECTED = 2


class EngineClient:
    """Async client for Clipper engine IPC.

    Integrates with GLib event loop for non-blocking I/O. Commands are
    sent asynchronously with callbacks, and events can be subscribed to.

    Example:
        client = EngineClient("/run/user/1000/clipper-engine.sock")
        client.connect()
        client.on("clip_saved", lambda data: print(f"Saved: {data['path']}"))
        client.save_replay_buffer(lambda result: print(f"Save result: {result}"))
    """

    def __init__(self, socket_path: str):
        """Initialize the client.

        Args:
            socket_path: Path to the engine's Unix domain socket
        """
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._watch_id: int | None = None
        self._buf = b""

        # Command callbacks are matched to responses in socket order.
        self._pending_commands: deque[Callable[[dict], None] | None] = deque()

        # Event handlers: maps event name to list of callbacks
        self._event_handlers: dict[str, list[Callable[[dict], None]]] = {}

        # Connection state
        self._connected = False
        self._state = STATE_DISCONNECTED
        self._error_callback: Callable[[str], None] | None = None
        self._state_callbacks: list[Callable[[int], None]] = []

        # Reconnection handling
        self._reconnect_timer_id: int | None = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._should_reconnect = True  # Can be disabled for manual disconnects

    def connect(self) -> bool:
        """Connect to the engine's Unix socket.

        Returns:
            True if connection succeeded, False otherwise
        """
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self._socket_path)
            self._sock.setblocking(False)
            self._connected = True

            # Register socket with GLib event loop
            self._watch_id = GLib.io_add_watch(
                self._sock.fileno(),
                GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                self._on_socket_readable,
            )

            # Connection successful - stop any reconnection attempts
            self._stop_reconnect_timer()
            self._reconnect_attempts = 0
            self._set_state(STATE_CONNECTED)

            return True

        except (OSError, FileNotFoundError) as e:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            # Only report error if this isn't a reconnection attempt
            if self._state != STATE_RECONNECTING:
                self._handle_error(f"Failed to connect to engine: {e}")
            return False

    def disconnect(self, auto_reconnect: bool = False) -> None:
        """Disconnect from the engine.

        Args:
            auto_reconnect: If True, attempt to reconnect automatically
        """
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        self._connected = False
        self._buf = b""
        self._pending_commands.clear()

        # Handle reconnection logic
        if auto_reconnect and self._should_reconnect:
            self._start_reconnect_timer()
        else:
            self._stop_reconnect_timer()
            self._set_state(STATE_DISCONNECTED)

    def is_connected(self) -> bool:
        """Check if currently connected to the engine."""
        return self._connected

    def on_error(self, callback: Callable[[str], None] | None) -> None:
        """Register a callback for connection/communication errors.

        Args:
            callback: Function to call with error message string
        """
        self._error_callback = callback

    def on_state_change(self, callback: Callable[[int], None] | None) -> None:
        """Register a callback for connection state changes.

        Args:
            callback: Function to call with new state (STATE_* constant)
        """
        if callback is None:
            self._state_callbacks.clear()
            return

        if callback not in self._state_callbacks:
            self._state_callbacks.append(callback)

    def set_auto_reconnect(self, enabled: bool) -> None:
        """Enable or disable reconnect attempts after socket disconnects."""
        self._should_reconnect = enabled
        if not enabled:
            self._stop_reconnect_timer()

    def get_state(self) -> int:
        """Get current connection state.

        Returns:
            One of STATE_DISCONNECTED, STATE_RECONNECTING, or STATE_CONNECTED
        """
        return self._state

    def on(self, event_name: str, callback: Callable[[dict], None]) -> None:
        """Register a callback for a specific event type.

        Args:
            event_name: Name of the event (e.g., "clip_saved", "game_connected")
            callback: Function to call when event is received, passed the event data dict
        """
        if event_name not in self._event_handlers:
            self._event_handlers[event_name] = []
        self._event_handlers[event_name].append(callback)

    def send_command(
        self, cmd: str, callback: Callable[[dict], None] | None = None, **params: Any
    ) -> bool:
        """Send a command to the engine.

        Args:
            cmd: Command name (e.g., "get_status", "save_replay_buffer")
            callback: Optional callback to invoke with the response dict
            **params: Additional command parameters

        Returns:
            True if command was sent, False if not connected

        """
        if not self._connected or self._sock is None:
            self._handle_error("Cannot send command: not connected")
            return False

        # Build command payload
        payload = {"cmd": cmd, **params}

        # Send command
        try:
            msg = json.dumps(payload) + "\n"
            self._sock.sendall(msg.encode("utf-8"))
            self._pending_commands.append(callback)
            return True
        except OSError as e:
            self.disconnect()
            self._handle_error(f"Failed to send command: {e}")
            return False

    # ------------------------------------------------------------------
    # Convenience methods for specific commands
    # ------------------------------------------------------------------

    def get_status(self, callback: Callable[[dict], None]) -> bool:
        """Get engine status.

        Args:
            callback: Called with response dict containing status info
        """
        return self.send_command("get_status", callback)

    def get_capabilities(self, callback: Callable[[dict], None]) -> bool:
        """Get engine-reported OBS/runtime capabilities."""
        return self.send_command("get_capabilities", callback)

    def get_audio_capabilities(self, callback: Callable[[dict], None]) -> bool:
        """Get engine-reported audio capture capabilities."""
        return self.send_command("get_audio_capabilities", callback)

    def list_audio_sources(self, callback: Callable[[dict], None]) -> bool:
        """List currently known runtime audio sources."""
        return self.send_command("list_audio_sources", callback)

    def update_audio_volumes(
        self, audio: dict, callback: Callable[[dict], None] | None = None
    ) -> bool:
        """Apply current audio volume settings without rebuilding sources."""
        return self.send_command("update_audio_volumes", callback, audio=audio)

    def start_replay_buffer(self, callback: Callable[[dict], None] | None = None) -> bool:
        """Start the replay buffer.

        Args:
            callback: Optional callback for the response
        """
        return self.send_command("start_replay_buffer", callback)

    def stop_replay_buffer(self, callback: Callable[[dict], None] | None = None) -> bool:
        """Stop the replay buffer.

        Args:
            callback: Optional callback for the response
        """
        return self.send_command("stop_replay_buffer", callback)

    def save_replay_buffer(
        self,
        callback: Callable[[dict], None] | None = None,
        game_name: str | None = None,
    ) -> bool:
        """Save the replay buffer to a clip file.

        Args:
            callback: Optional callback for the response
            game_name: Optional game name to include in the clip filename
        """
        params = {"game_name": game_name} if game_name else {}
        return self.send_command("save_replay_buffer", callback, **params)

    def get_preview_frame(
        self,
        callback: Callable[[dict], None],
        width: int = 360,
        height: int = 203,
        preserve_aspect: bool = False,
    ) -> bool:
        """Request one low-resolution BGRA preview frame from the engine."""
        params: dict[str, Any] = {"width": width, "height": height}
        if preserve_aspect:
            params["preserve_aspect"] = True
        return self.send_command(
            "get_preview_frame", callback, **params
        )

    def shutdown(
        self, callback: Callable[[dict], None] | None = None, restart: bool = False
    ) -> bool:
        """Shutdown the engine.

        Args:
            callback: Optional callback for the response
            restart: If True, engine will exit with code 42 to signal launcher to restart
        """
        return self.send_command("shutdown", callback, restart=restart)

    # ------------------------------------------------------------------
    # Internal I/O handling
    # ------------------------------------------------------------------

    def _on_socket_readable(self, fd: int, condition: GLib.IOCondition) -> bool:
        """GLib callback when socket has data available or closes.

        Returns:
            True to keep watching, False to remove watch
        """
        # Check for errors or hangup
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self._handle_error("Engine disconnected")
            self.disconnect(auto_reconnect=True)
            return False

        # Safety check (should never happen, but be defensive)
        if self._sock is None:
            self.disconnect()
            return False

        # Read available data
        try:
            chunk = self._sock.recv(4096)
            if not chunk:  # EOF
                self._handle_error("Engine closed connection")
                self.disconnect(auto_reconnect=True)
                return False

            self._buf += chunk

            # Process complete messages
            while b"\n" in self._buf:
                idx = self._buf.index(b"\n")
                line = self._buf[:idx].decode("utf-8").strip()
                self._buf = self._buf[idx + 1 :]

                if line:
                    self._process_message(line)

            return True

        except OSError as e:
            self._handle_error(f"Socket read error: {e}")
            self.disconnect(auto_reconnect=True)
            return False

    def _process_message(self, line: str) -> None:
        """Parse and dispatch a JSON message."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            self._handle_error(f"Invalid JSON from engine: {e}")
            return

        # Check if this is a response to a command
        if "ok" in msg:
            if self._pending_commands:
                callback = self._pending_commands.popleft()
                if callback is not None:
                    callback(msg)
            return

        # Otherwise, it's an async event
        if "event" in msg:
            event_name = msg["event"]
            if event_name in self._event_handlers:
                for callback in self._event_handlers[event_name]:
                    try:
                        callback(msg)
                    except Exception as e:
                        print(f"Error in event handler for {event_name}: {e}")

    def _handle_error(self, message: str) -> None:
        """Handle an error by calling the error callback if set."""
        if self._error_callback is not None:
            try:
                self._error_callback(message)
            except Exception as e:
                print(f"Error in error callback: {e}")
        else:
            # Fallback: just print to stderr
            print(f"EngineClient error: {message}")

    def _set_state(self, new_state: int) -> None:
        """Update connection state and notify callback if state changed."""
        if self._state != new_state:
            self._state = new_state
            for callback in list(self._state_callbacks):
                try:
                    callback(new_state)
                except Exception as e:
                    print(f"Error in state callback: {e}")

    def _start_reconnect_timer(self) -> None:
        """Start attempting to reconnect to the engine."""
        if self._reconnect_timer_id is None:
            self._set_state(STATE_RECONNECTING)
            self._reconnect_attempts = 0
            self._reconnect_timer_id = GLib.timeout_add(1000, self._try_reconnect)

    def _stop_reconnect_timer(self) -> None:
        """Stop reconnection attempts."""
        if self._reconnect_timer_id is not None:
            GLib.source_remove(self._reconnect_timer_id)
            self._reconnect_timer_id = None

    def _try_reconnect(self) -> bool:
        """Attempt to reconnect to the engine.

        Returns:
            True to continue trying, False to stop timer
        """
        self._reconnect_attempts += 1

        if self._reconnect_attempts > self._max_reconnect_attempts:
            # Give up after max attempts
            self._stop_reconnect_timer()
            self._set_state(STATE_DISCONNECTED)
            self._handle_error("Failed to reconnect to engine after 10 attempts")
            return False

        # Try to connect
        if self.connect():
            # Connection successful - connect() will stop the timer and update state
            return False

        # Continue trying
        return True
