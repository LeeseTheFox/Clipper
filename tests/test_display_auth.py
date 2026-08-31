import os

from display_auth import normalize_display_auth_env, valid_xauthority_path


def test_valid_xauthority_path_keeps_existing_file(tmp_path):
    existing = tmp_path / "existing-xauth"
    runtime = tmp_path / "runtime"
    existing.write_text("", encoding="utf-8")
    runtime.mkdir()

    assert valid_xauthority_path(
        {"XAUTHORITY": str(existing), "XDG_RUNTIME_DIR": str(runtime)}
    ) == str(existing)


def test_valid_xauthority_path_finds_newest_runtime_xauth(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    old = runtime / "xauth_old"
    new = runtime / "xauth_new"
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    old.touch()
    new.touch()
    old_mtime = old.stat().st_mtime
    newer_mtime = old_mtime + 10
    os.utime(new, (newer_mtime, newer_mtime))

    assert valid_xauthority_path(
        {"XAUTHORITY": str(tmp_path / "missing"), "XDG_RUNTIME_DIR": str(runtime)}
    ) == str(new)


def test_normalize_display_auth_env_updates_stale_xauthority(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    xauth = runtime / "xauth_valid"
    xauth.write_text("", encoding="utf-8")
    env = {"XAUTHORITY": str(tmp_path / "missing"), "XDG_RUNTIME_DIR": str(runtime)}

    assert normalize_display_auth_env(env) is env
    assert env["XAUTHORITY"] == str(xauth)
