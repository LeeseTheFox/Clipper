"""Persist top-level window sizes safely across display changes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REMEMBER_WINDOW_SIZES_KEY = "remember_window_sizes"
WINDOW_STATE_FILENAME = "window-state.json"
_MAX_WINDOW_DIMENSION = 32_768


def window_state_path(config) -> Path | None:
    """Return the state path next to *config*, or ``None`` for test mappings."""
    config_path = getattr(config, "path", None)
    if config_path is None:
        return None
    return Path(config_path).with_name(WINDOW_STATE_FILENAME)


def clear_window_state(config_path: Path) -> None:
    """Remove saved non-preference window state during a config reset."""
    state_path = Path(config_path).with_name(WINDOW_STATE_FILENAME)
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass


class WindowStateStore:
    """Small cross-process-safe JSON store used by the isolated player."""

    def __init__(self, path: Path | None):
        self.path = path

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if self.path is None:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def load(self, window_name: str) -> dict[str, Any] | None:
        with self._lock():
            value = self._read().get(window_name)
        return value if isinstance(value, dict) else None

    def save(self, window_name: str, state: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self._lock():
            data = self._read()
            data[window_name] = state
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
                suffix=".tmp",
            ) as temporary:
                json.dump(data, temporary, indent=2)
                temporary_name = temporary.name
            os.replace(temporary_name, self.path)


def _dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result < 1 or result > _MAX_WINDOW_DIMENSION:
        return None
    return result


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def monitor_snapshot(monitor) -> dict[str, Any]:
    geometry = monitor.get_geometry()
    return {
        "connector": _text(getattr(monitor, "get_connector", lambda: "")()),
        "manufacturer": _text(getattr(monitor, "get_manufacturer", lambda: "")()),
        "model": _text(getattr(monitor, "get_model", lambda: "")()),
        "width": int(geometry.width),
        "height": int(geometry.height),
        "scale": int(getattr(monitor, "get_scale_factor", lambda: 1)()),
    }


def display_monitors(display) -> list[tuple[object, dict[str, Any]]]:
    if display is None:
        return []
    model = display.get_monitors()
    monitors = []
    for index in range(model.get_n_items()):
        monitor = model.get_item(index)
        if monitor is not None:
            monitors.append((monitor, monitor_snapshot(monitor)))
    return monitors


def select_monitor(
    saved: dict[str, Any] | None,
    monitors: list[tuple[object, dict[str, Any]]],
) -> tuple[object, dict[str, Any]] | None:
    """Find the old monitor after connector or resolution changes."""
    if not monitors:
        return None
    if not isinstance(saved, dict):
        return monitors[0]

    connector = _text(saved.get("connector"))
    if connector:
        for candidate in monitors:
            if candidate[1]["connector"] == connector:
                return candidate

    manufacturer = _text(saved.get("manufacturer"))
    model = _text(saved.get("model"))
    if manufacturer or model:
        for candidate in monitors:
            details = candidate[1]
            if (
                details["manufacturer"] == manufacturer
                and details["model"] == model
            ):
                return candidate
    return monitors[0]


def restored_size(
    state: dict[str, Any],
    monitor: dict[str, Any] | None,
    minimum_size: tuple[int, int],
) -> tuple[int, int] | None:
    """Validate a saved size and clamp it to the current display."""
    width = _dimension(state.get("width"))
    height = _dimension(state.get("height"))
    if width is None or height is None:
        return None

    width = max(minimum_size[0], width)
    height = max(minimum_size[1], height)
    if monitor is not None:
        width = min(width, monitor["width"])
        height = min(height, monitor["height"])
    return width, height


class WindowSizeManager:
    """Attach persistent normal size and maximized state to a window."""

    def __init__(
        self,
        window,
        config,
        window_name: str,
        *,
        minimum_size: tuple[int, int],
    ):
        self.window = window
        self.config = config
        self.window_name = window_name
        self.minimum_size = minimum_size
        self.store = WindowStateStore(window_state_path(config))
        self._normal_size: tuple[int, int] | None = None
        self.restored = self.restore()
        window.connect("notify::width", self._on_size_changed)
        window.connect("notify::height", self._on_size_changed)
        window.connect("hide", self._on_hide)

    def _enabled(self) -> bool:
        return bool(
            self.config is not None
            and self.config.get(REMEMBER_WINDOW_SIZES_KEY, True)
        )

    def restore(self) -> bool:
        if not self._enabled():
            return False
        state = self.store.load(self.window_name)
        if state is None:
            return False
        monitors = display_monitors(self.window.get_display())
        selected = select_monitor(state.get("monitor"), monitors)
        details = selected[1] if selected is not None else None
        size = restored_size(state, details, self.minimum_size)
        if size is None:
            return False
        self.window.set_default_size(*size)
        self._normal_size = size
        if bool(state.get("maximized", False)):
            self.window.maximize()
        return True

    def _on_size_changed(self, *_args) -> None:
        if self.window.is_maximized() or self.window.is_fullscreen():
            return
        width = int(self.window.get_width())
        height = int(self.window.get_height())
        if width > 0 and height > 0:
            self._normal_size = (width, height)

    def _on_hide(self, *_args) -> None:
        self.save()

    def save(self) -> None:
        if not self._enabled():
            return
        maximized = bool(self.window.is_maximized())
        fullscreen = bool(self.window.is_fullscreen())
        if not maximized and not fullscreen:
            self._on_size_changed()
        size = self._normal_size
        if size is None:
            width, height = self.window.get_default_size()
            size = (max(self.minimum_size[0], width), max(self.minimum_size[1], height))

        surface = self.window.get_surface()
        monitor = None
        if surface is not None:
            monitor = self.window.get_display().get_monitor_at_surface(surface)
        monitors = display_monitors(self.window.get_display())
        if monitor is None and monitors:
            monitor = monitors[0][0]

        state: dict[str, Any] = {
            "width": size[0],
            "height": size[1],
            "maximized": maximized,
        }
        if monitor is not None:
            state["monitor"] = monitor_snapshot(monitor)
        self.store.save(self.window_name, state)
