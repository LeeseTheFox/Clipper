"""Display authentication helpers for X11-backed runtime components."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


def valid_xauthority_path(env: MutableMapping[str, str] | None = None) -> str:
    """Return a readable Xauthority path for display-backed libobs/X11 startup."""
    values = env if env is not None else os.environ
    existing = values.get("XAUTHORITY", "")
    if existing and Path(existing).is_file():
        return existing

    runtime_dir = values.get("XDG_RUNTIME_DIR", "")
    if not runtime_dir:
        return existing

    try:
        candidates = sorted(
            Path(runtime_dir).glob("xauth_*"),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return existing

    for candidate in reversed(candidates):
        if candidate.is_file():
            return str(candidate)

    return existing


def normalize_display_auth_env(
    env: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Set a valid XAUTHORITY value in *env* when one can be discovered."""
    values = env if env is not None else os.environ
    xauthority = valid_xauthority_path(values)
    if xauthority:
        values["XAUTHORITY"] = xauthority
    return values
