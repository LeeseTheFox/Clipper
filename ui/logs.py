"""In-memory application log storage suitable for a live log viewer."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime
from threading import Lock
from typing import IO

MAX_LOG_LINES = 2_000


def probe_failure_reason(stderr: str) -> str:
    """Prefer the cause over FFmpeg's cascading shutdown messages."""
    cleanup = (
        "Task finished with error code", "Terminating thread with return code",
        "Nothing was written into output file", "Error sending frames to consumers",
        "Error while opening encoder", "Could not open encoder", "Conversion failed",
    )
    reasons = []
    for line in stderr.splitlines():
        if not line.strip() or any(fragment in line for fragment in cleanup):
            continue
        line = re.sub(r"\s*@\s*0x[0-9a-fA-F]+", "", line.strip())
        if line not in reasons:
            reasons.append(line[:400])
    return "; ".join(reasons[:2]) or "no specific driver reason reported"


class ApplicationLogHandler(logging.Handler):
    """Route component diagnostics into the same log as engine output."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self.callback = callback
        self.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.callback(self.format(record))


def install_diagnostics(callback: Callable[[str], None]) -> None:
    logger = logging.getLogger("clipper")
    for handler in list(logger.handlers):
        if isinstance(handler, ApplicationLogHandler):
            logger.removeHandler(handler)
            handler.close()
    logger.addHandler(ApplicationLogHandler(callback))
    logger.setLevel(logging.INFO)
    logger.propagate = False


def diagnostic_tail(text: str, limit: int = 20) -> str:
    """Keep bounded failure context, without adjacent duplicate tool output."""
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()[:1000]
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    prefix = "[earlier diagnostic lines omitted]\n" if len(lines) > limit else ""
    return prefix + "\n".join(lines[-limit:])


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
        self._last_message: str | None = None
        self._repeat_count = 0
        self._repeat_since = ""

    def add(self, message: str) -> None:
        """Add a message, splitting it into individual display lines."""
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        lines = str(message).splitlines() or [""]
        timestamped_lines = [f"{timestamp} {line}" for line in lines]
        with self._lock:
            if len(lines) == 1 and message == self._last_message:
                self._repeat_count += 1
                self._lines[-1] = (
                    f"{timestamp} {lines[0]} "
                    f"[repeated {self._repeat_count} times since {self._repeat_since}; "
                    "latest occurrence]"
                )
            else:
                self._lines.extend(timestamped_lines)
                self._repeat_count = 1
                self._repeat_since = timestamp
            self._last_message = message if len(lines) == 1 else None
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
