"""Capture mode constants and whitelist-entry helpers."""

from __future__ import annotations

from i18n import _

CAPTURE_MODE_GAME = "game_capture"
CAPTURE_MODE_DISPLAY = "display_capture"
CAPTURE_MODES = (CAPTURE_MODE_GAME, CAPTURE_MODE_DISPLAY)
DEFAULT_CAPTURE_MODE = CAPTURE_MODE_DISPLAY

CAPTURE_MODE_OPTIONS = (
    (_("Display capture"), CAPTURE_MODE_DISPLAY),
    (_("Game capture"), CAPTURE_MODE_GAME),
)


def capture_mode_label(mode: object) -> str:
    if mode == CAPTURE_MODE_GAME:
        return _("Game capture")
    return _("Display capture")


def capture_mode_from_method_label(method: object) -> str | None:
    value = str(method or "").strip().lower()
    if value in {"game capture", "game capture (advanced)", "vkcapture"}:
        return CAPTURE_MODE_GAME
    if value in {"display capture", "pipewire"}:
        return CAPTURE_MODE_DISPLAY
    return None


def normalize_capture_mode(value: object, method: object = None) -> str:
    if value in CAPTURE_MODES:
        return str(value)

    mode = capture_mode_from_method_label(method)
    if mode:
        return mode

    return DEFAULT_CAPTURE_MODE


def capture_mode_for_entry(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return DEFAULT_CAPTURE_MODE
    return normalize_capture_mode(entry.get("capture_mode"), entry.get("method"))


def with_capture_mode(entry: dict, mode: object) -> dict:
    normalized = normalize_capture_mode(mode)
    result = dict(entry)
    result["capture_mode"] = normalized
    result.pop("method", None)
    return result
