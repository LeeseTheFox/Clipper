"""
Event-driven watcher for runtime audio source changes.
"""

from collections.abc import Callable

from gi.repository import Gio, GLib

_AUDIO_EVENT_SUBJECTS = (
    "sink-input",
    "source-output",
    "client",
    "sink",
    "source",
    "server",
)


def audio_event_is_relevant(line: str) -> bool:
    """Return True when a pactl subscribe event can affect source discovery."""
    if not line:
        return False

    lowered = line.lower()
    if "event" not in lowered:
        return False

    return any(f" on {subject} " in lowered for subject in _AUDIO_EVENT_SUBJECTS)


class AudioSourceWatcher:
    """Watch PulseAudio/PipeWire-Pulse graph changes and debounce refreshes."""

    def __init__(
        self,
        refresh_callback: Callable[[], None],
        *,
        debounce_ms: int = 350,
        restart_delay_ms: int = 2000,
    ):
        self._refresh_callback = refresh_callback
        self._debounce_ms = debounce_ms
        self._restart_delay_ms = restart_delay_ms
        self._process = None
        self._stdout = None
        self._refresh_timeout_id = None
        self._restart_timeout_id = None
        self._stopping = False

    def start(self) -> None:
        if self._process is not None:
            return

        self._stopping = False
        try:
            self._process = Gio.Subprocess.new(
                ["pactl", "subscribe"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error as e:
            print(f"Audio source watcher unavailable: {e.message}")
            self._process = None
            return

        stdout_pipe = self._process.get_stdout_pipe()
        if stdout_pipe is None:
            self._process.force_exit()
            self._process = None
            return

        self._stdout = Gio.DataInputStream.new(stdout_pipe)
        self._read_next_line()

    def stop(self) -> None:
        self._stopping = True

        if self._refresh_timeout_id is not None:
            GLib.source_remove(self._refresh_timeout_id)
            self._refresh_timeout_id = None

        if self._restart_timeout_id is not None:
            GLib.source_remove(self._restart_timeout_id)
            self._restart_timeout_id = None

        self._stdout = None
        if self._process is not None:
            self._process.force_exit()
            self._process = None

    def _read_next_line(self) -> None:
        if self._stdout is None:
            return

        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line_read)

    def _on_line_read(self, stream, result) -> None:
        if self._stopping or stream is not self._stdout:
            return

        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            self._schedule_restart()
            return

        if line is None:
            self._schedule_restart()
            return

        if audio_event_is_relevant(line):
            self._schedule_refresh()

        self._read_next_line()

    def _schedule_refresh(self) -> None:
        if self._refresh_timeout_id is None:
            self._refresh_timeout_id = GLib.timeout_add(
                self._debounce_ms, self._run_refresh
            )

    def _run_refresh(self) -> bool:
        self._refresh_timeout_id = None
        self._refresh_callback()
        return False

    def _schedule_restart(self) -> None:
        self._stdout = None
        self._process = None

        if self._stopping or self._restart_timeout_id is not None:
            return

        self._restart_timeout_id = GLib.timeout_add(
            self._restart_delay_ms, self._restart
        )

    def _restart(self) -> bool:
        self._restart_timeout_id = None
        self.start()
        return False
