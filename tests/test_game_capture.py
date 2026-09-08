"""Exercise the exported payload and actual shell launch contract."""

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import game_capture
import pytest
import steam


def _payload(root: Path) -> Path:
    wrapper = root / "bin/clipper-gamecapture"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\nexport CLIPPER_TEST_HOOK=loaded\nexec "$@"\n')
    wrapper.chmod(0o755)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "files": {
                    "bin/clipper-gamecapture": hashlib.sha256(wrapper.read_bytes()).hexdigest()
                },
                "links": {},
            }
        )
    )
    return root


def test_payload_exports_atomically_and_repairs_damaged_copy(tmp_path):
    source = _payload(tmp_path / "packaged")
    destination = tmp_path / "Clipper data"
    wrapper = game_capture.ensure_payload(source, destination)
    first = wrapper.resolve()
    assert game_capture.ensure_payload(source, destination).resolve() == first
    first.write_text("damaged")
    repaired = game_capture.ensure_payload(source, destination)
    assert repaired.resolve() != first
    assert repaired.read_bytes() == (source / "bin/clipper-gamecapture").read_bytes()
    assert not list(destination.glob(".stage-*"))


def test_corrupt_package_does_not_replace_working_payload(tmp_path):
    source = _payload(tmp_path / "packaged")
    wrapper = game_capture.ensure_payload(source, tmp_path / "data")
    before = wrapper.resolve()
    (source / "bin/clipper-gamecapture").write_text("damaged")
    with pytest.raises(RuntimeError, match="incomplete"):
        game_capture.ensure_payload(source, tmp_path / "data")
    assert wrapper.resolve() == before
    assert "exec" in wrapper.read_text()


@pytest.mark.parametrize("installed", [False, True])
def test_launch_option_runs_game_with_or_without_clipper(tmp_path, installed):
    # Exercise quoting, literal shell characters, argument boundaries, the
    # environment-assignment prefix, and graceful removal with a real shell.
    source = _payload(tmp_path / "packaged")
    root = tmp_path / "game tools ' $dollar `literal`"
    wrapper = root / "current/bin/clipper-gamecapture"
    if installed:
        game_capture.ensure_payload(source, root)
    original = "FOO='two words' %command% 'argument with spaces'"
    options = steam.compose_capture_options(original, str(wrapper))
    program = ["sh", "-c", 'printf "%s|%s|%s" "$FOO" "$1" "${CLIPPER_TEST_HOOK-unset}"', "game"]
    command = options.replace("%command%", shlex.join(program))
    env = dict(os.environ)
    env.pop("CLIPPER_TEST_HOOK", None)
    result = subprocess.run(
        ["sh", "-c", command], check=True, text=True, capture_output=True, env=env
    )
    hook = "loaded" if installed else "unset"
    assert result.stdout == f"two words|argument with spaces|{hook}"


def test_flatpak_steam_permission_is_one_read_only_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("FLATPAK_ID", game_capture.APP_ID)
    monkeypatch.setattr(game_capture, "data_root", lambda: tmp_path / "capture data")
    monkeypatch.setattr(
        game_capture.subprocess,
        "run",
        lambda command, **kw: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    game_capture.allow_flatpak_steam()
    command = calls[0]
    assert command[:2] == ["flatpak-spawn", "--host"]
    # Never inherit the UI's sandbox-only /app/share/clipper/ui directory.
    assert "--directory=/" in command[2:-5]
    assert command[-5:] == [
        "flatpak",
        "override",
        "--user",
        f"--filesystem={tmp_path / 'capture data'}:ro",
        game_capture.STEAM_ID,
    ]
    assert not any("install" in arg for arg in command)


def test_failed_flatpak_permission_prevents_setup_success(monkeypatch):
    monkeypatch.setattr(game_capture, "ensure_payload", lambda: Path("/test/wrapper"))
    monkeypatch.setattr(
        game_capture.subprocess,
        "run",
        lambda *args, **kw: subprocess.CompletedProcess([], 1, "", "permission denied"),
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        game_capture.prepare(game_capture.EnvironmentKind.FLATPAK_STEAM_SYSTEM)
