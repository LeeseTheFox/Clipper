"""In-memory application log storage suitable for a live log viewer."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime
from threading import Lock
from typing import IO

MAX_LOG_LINES = 2_000


class LogBuffer:
    """A bounded, thread-safe log buffer with change listeners."""

    def __init__(
        self,
        max_lines: int = MAX_LOG_LINES,
        *,
        initial_lines: Iterable[str] = (),
    ) -> None:
        self._max_lines = max(1, max_lines)
        self._lines = [str(line) for line in initial_lines][-self._max_lines :]
        self._listeners: list[Callable[[], None]] = []
        self._lock = Lock()

    def add(self, message: str) -> None:
        """Add a message, splitting it into individual display lines."""
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        lines = str(message).splitlines() or [""]
        timestamped_lines = [f"{timestamp} {line}" for line in lines]
        with self._lock:
            self._lines.extend(timestamped_lines)
            del self._lines[: -self._max_lines]
            listeners = list(self._listeners)

        for listener in listeners:
            listener()

    def text(self) -> str:
        """Return the complete current log text."""
        with self._lock:
            return "\n".join(self._lines)

    def snapshot(self) -> list[str]:
        """Return a stable copy of the buffered lines."""
        with self._lock:
            return list(self._lines)

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a change listener and return an unsubscribe callback."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe


def create_handoff(buffer: LogBuffer) -> IO[str]:
    """Serialize a buffer into an anonymous file that survives one exec."""
    handoff = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    try:
        json.dump(buffer.snapshot(), handoff)
        handoff.flush()
        handoff.seek(0)
        os.set_inheritable(handoff.fileno(), True)
        return handoff
    except Exception:
        handoff.close()
        raise


def consume_handoff(file_descriptor: int) -> LogBuffer:
    """Restore and close an inherited log handoff file descriptor."""
    if file_descriptor <= 2:
        raise ValueError("log handoff must not use a standard stream")

    with os.fdopen(file_descriptor, mode="r", encoding="utf-8") as handoff:
        lines = json.load(handoff)
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ValueError("invalid log handoff contents")
    return LogBuffer(initial_lines=lines)
