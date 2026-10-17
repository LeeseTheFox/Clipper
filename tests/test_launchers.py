import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shell_launchers_have_valid_syntax():
    for script in (
        REPO_ROOT / "clipper",
        REPO_ROOT / "engine" / "src" / "run_engine.sh",
        REPO_ROOT / "tools" / "run_flatpak_build_quiet.sh",
        REPO_ROOT / "tools" / "run_pytest_quiet.sh",
        REPO_ROOT / "tools" / "configure_engine_clangd.sh",
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_obs_helpers_are_executable_beside_built_and_installed_engine(tmp_path):
    helpers = ("obs-ffmpeg-mux", "obs-nvenc-test")
    obs_prefix = tmp_path / "obs"
    (obs_prefix / "bin").mkdir(parents=True)
    for name in helpers:
        helper = obs_prefix / "bin" / name
        helper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}'\n", encoding="utf-8")
        helper.chmod(0o755)

    build_dir = tmp_path / "engine"
    build_dir.mkdir()
    # Use a prebuilt stand-in: this checks helper placement, not C compilation.
    engine = build_dir / "clipper-engine"
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o755)
    staging_dir = tmp_path / "staging"
    command = [
        "make", "--no-print-directory",
        "-f", str(REPO_ROOT / "engine" / "src" / "Makefile"),
        "-o", "clipper-engine",
        f"OBS_PREFIX={obs_prefix}", "PREFIX=/app", f"DESTDIR={staging_dir}",
        "GLIB_CFLAGS=", "GLIB_LIBS=", "WAYLAND_CFLAGS=", "WAYLAND_LIBS=",
    ]

    for target, engine_dir in (
        ("all", build_dir),
        ("install", staging_dir / "app" / "libexec" / "clipper"),
    ):
        subprocess.run([*command, target], cwd=build_dir, check=True, capture_output=True)
        assert (engine_dir / "clipper-engine").is_file()
        for name in helpers:
            # libobs searches here regardless of PATH, including for NVENC probing.
            result = subprocess.run(
                [str(engine_dir / name)], check=True, capture_output=True, text=True,
            )
            assert result.stdout.strip() == name

    subprocess.run([*command, "clean"], cwd=build_dir, check=True, capture_output=True)
    assert all(not (build_dir / name).is_symlink() for name in helpers)


@pytest.mark.skipif(shutil.which("ostree") is None, reason="OSTree CLI is unavailable")
def test_clangd_headers_survive_cached_build_without_unpacked_sources(tmp_path):
    root = tmp_path / "checkout"
    tools = root / "tools"
    tools.mkdir(parents=True)
    for name in ("configure_engine_clangd.sh", "generate_native_test_headers.py"):
        shutil.copy2(REPO_ROOT / "tools" / name, tools / name)
    shutil.copytree(REPO_ROOT / "packaging/flatpak/patches", root / "packaging/flatpak/patches")
    (root / "venv").symlink_to(sys.prefix, target_is_directory=True)
    cache = root / ".flatpak-builder/cache"
    cache.parent.mkdir()
    module = tmp_path / "module"
    headers = module / "files/include/obs"
    (headers / "graphics").mkdir(parents=True)
    for name in ("obs.h", "obsconfig.h", "graphics/vec2.h"):
        (headers / name).write_text(f"/* cached {name} */\n", encoding="utf-8")

    subprocess.run(["ostree", f"--repo={cache}", "init", "--mode=bare-user-only"],
                   check=True, capture_output=True)
    subprocess.run([
        "ostree", f"--repo={cache}", "commit",
        f"--branch={os.uname().machine}-clipper/build-obs-studio-libobs",
        "--subject=Cached OBS module", f"--tree=dir={module}",
    ], check=True, capture_output=True)

    result = subprocess.run([str(tools / "configure_engine_clangd.sh")],
                            cwd=root, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    exposed = root / ".flatpak-builder/clangd-include"
    for name in ("obs.h", "obsconfig.h", "graphics/vec2.h"):
        assert (exposed / "obs" / name).read_bytes() == (headers / name).read_bytes()
    assert (exposed / "obsconfig.h").read_bytes() == (headers / "obsconfig.h").read_bytes()
